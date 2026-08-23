from __future__ import annotations

import json
import os
import shutil
import unicodedata

import fitz

from scanindex.core.canonical_io import (
    save_canonical,
    load_canonical,
    resolve_companion,
)
from scanindex.core.kie.json_utils import (
    make_document_stub,
    make_line_record,
    make_page_record,
    make_word_record,
    merge_bboxes,
)
from scanindex.core.ocr.text_normalizer import OCR_TEXT_NORMALIZATION, sanitize_ocr_surface_text


DIGITAL_TEXT_ENGINE = "digital_pdf_text"
DEFAULT_DIGITAL_CONFIDENCE = 1.0
DEFAULT_DIGITAL_CONTENT_TYPE = 0
DEFAULT_DIGITAL_FG_GRAY = 128
DEFAULT_FONT_SIZE = 11.0
OCR_DPI = 200


def is_digital_ocr_output(ocr_pdf_path: str) -> bool:
    """Kiểm tra _ocr.pdf là output của digital text extraction (không phải ScreenAI).

    Resolve canonical `.json.zst` companion, check `document.engine`.
    Trả True nếu engine = DIGITAL_TEXT_ENGINE → caller nên skip correction.
    """
    companion = resolve_companion(ocr_pdf_path)
    if companion is None:
        return False
    try:
        data = load_canonical(companion)
        return (
            data.get("document", {}).get("engine") == DIGITAL_TEXT_ENGINE
            or data.get("pipeline", {}).get("ocr", {}).get("engine") == DIGITAL_TEXT_ENGINE
            or data.get("engine") == DIGITAL_TEXT_ENGINE
        )
    except Exception:
        return False


def _page_render_size(page) -> tuple[int, int]:
    return (
        max(1, int(round(page.rect.width * OCR_DPI / 72.0))),
        max(1, int(round(page.rect.height * OCR_DPI / 72.0))),
    )


def _visual_line_sort_key(items: list[dict]) -> tuple[int, float, float]:
    y0 = min(float(item["bbox"][1]) for item in items)
    y1 = max(float(item["bbox"][3]) for item in items)
    center_y = (y0 + y1) / 2.0
    return (round(center_y / 8.0), min(float(item["bbox"][0]) for item in items), center_y)


def _median(values: list[float], default: float = 0.0) -> float:
    clean = sorted(float(value) for value in values)
    if not clean:
        return default
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def _items_bbox(items: list[dict]) -> tuple[float, float, float, float]:
    return (
        min(float(item["bbox"][0]) for item in items),
        min(float(item["bbox"][1]) for item in items),
        max(float(item["bbox"][2]) for item in items),
        max(float(item["bbox"][3]) for item in items),
    )


def _group_visual_items(items: list[dict]) -> dict[tuple[int, int], list[dict]]:
    visual_groups: list[list[dict]] = []
    for item in items:
        x0, y0, x1, y1 = item["bbox"]
        item["cy"] = (float(y0) + float(y1)) / 2.0
        item["height"] = max(1.0, float(y1) - float(y0))
        visual_groups.append([item])

    merged: list[list[dict]] = []
    for group in sorted(visual_groups, key=lambda items: (items[0]["cy"], items[0]["bbox"][0])):
        word = group[0]
        placed = False
        for existing in merged:
            centers = [float(item["cy"]) for item in existing]
            heights = [float(item["height"]) for item in existing]
            center = sum(centers) / max(1, len(centers))
            line_h = max(1.0, sum(heights) / max(1, len(heights)))
            if abs(float(word["cy"]) - center) <= max(2.5, line_h * 0.45):
                existing.append(word)
                placed = True
                break
        if not placed:
            merged.append(group)

    visual_lines: list[list[dict]] = []
    for group in sorted(merged, key=_visual_line_sort_key):
        ordered = sorted(group, key=lambda item: (item["bbox"][0], item["bbox"][1], item["block_no"], item["line_no"], item["word_no"]))
        gaps = [
            max(0.0, float(ordered[idx]["bbox"][0]) - float(ordered[idx - 1]["bbox"][2]))
            for idx in range(1, len(ordered))
        ]
        median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
        segment: list[dict] = []
        for item in ordered:
            if segment:
                prev = segment[-1]
                gap = float(item["bbox"][0]) - float(prev["bbox"][2])
                avg_h = (float(item["height"]) + float(prev["height"])) / 2.0
                split_gap = max(12.0, median_gap * 3.0, avg_h * 0.75)
                if gap > split_gap:
                    visual_lines.append(segment)
                    segment = []
            segment.append(item)
        if segment:
            visual_lines.append(segment)

    grouped = {}
    for line_idx, ordered in enumerate(sorted(visual_lines, key=_visual_line_sort_key)):
        for word_idx, item in enumerate(ordered):
            item["word_no"] = word_idx
            item.pop("cy", None)
            item.pop("height", None)
        grouped[(0, line_idx)] = ordered
    return grouped


def _group_native_block_lines(items: list[dict]) -> dict[tuple[int, int], list[dict]]:
    grouped: dict[tuple[int, int], list[dict]] = {}
    for item in items:
        grouped.setdefault((int(item["block_no"]), int(item["line_no"])), []).append(item)
    for group in grouped.values():
        group.sort(key=lambda item: (int(item["word_no"]), float(item["bbox"][0]), float(item["bbox"][1])))
    return grouped


def _native_block_lines_are_usable(groups: dict[tuple[int, int], list[dict]]) -> bool:
    if not groups:
        return False
    sizes = [len(group) for group in groups.values() if group]
    if not sizes:
        return False
    one_word_ratio = sum(1 for size in sizes if size <= 1) / len(sizes)
    avg_words = sum(sizes) / len(sizes)
    return avg_words >= 2.0 and one_word_ratio <= 0.45


def _detect_two_column_group_profile(
    groups: dict[tuple[int, int], list[dict]],
    page_width: float,
    page_height: float,
) -> dict | None:
    candidates: list[tuple[float, float, float, float]] = []
    for group in groups.values():
        if not group:
            continue
        text_len = sum(len(str(item.get("text") or "")) for item in group)
        if text_len < 3:
            continue
        x0, y0, x1, y1 = _items_bbox(group)
        width = x1 - x0
        height = y1 - y0
        if width <= 0 or height <= 0:
            continue
        if y0 < page_height * 0.07 or y1 > page_height * 0.94:
            continue
        if width > page_width * 0.58:
            continue
        center_x = (x0 + x1) / 2.0
        candidates.append((x0, y0, x1, y1))

    if len(candidates) < 12:
        return None

    left = [
        box for box in candidates
        if box[0] < page_width * 0.45 and ((box[0] + box[2]) / 2.0) < page_width * 0.52
    ]
    right = [
        box for box in candidates
        if box[0] > page_width * 0.43 and ((box[0] + box[2]) / 2.0) > page_width * 0.52
    ]
    if min(len(left), len(right)) < max(5, int(len(candidates) * 0.16)):
        return None

    left_start = _median([box[0] for box in left])
    right_start = _median([box[0] for box in right])
    if right_start - left_start < page_width * 0.24:
        return None

    start_tolerance = page_width * 0.08
    left_core = [box for box in left if abs(box[0] - left_start) <= start_tolerance]
    right_core = [box for box in right if abs(box[0] - right_start) <= start_tolerance]
    if min(len(left_core), len(right_core)) < max(4, int(min(len(left), len(right)) * 0.55)):
        return None

    left_end = _median([box[2] for box in left_core], left_start + page_width * 0.35)
    gutter = (left_end + right_start) / 2.0
    if not (page_width * 0.38 <= gutter <= page_width * 0.62):
        return None

    return {
        "gutter": gutter,
        "start_y": min(min(box[1] for box in left_core), min(box[1] for box in right_core)),
        "end_y": max(max(box[3] for box in left_core), max(box[3] for box in right_core)),
    }


def _split_two_column_line_groups(
    groups: dict[tuple[int, int], list[dict]],
    page_width: float,
    page_height: float,
) -> dict[tuple[int, int], list[dict]]:
    profile = _detect_two_column_group_profile(groups, page_width, page_height)
    if not profile:
        return groups

    gutter = float(profile["gutter"])
    start_y = float(profile["start_y"])
    end_y = float(profile["end_y"])
    split_min_width = page_width * 0.40
    y_margin = max(8.0, page_height * 0.01)
    result: dict[tuple[int, int], list[dict]] = {}

    for key, group in groups.items():
        if not group:
            continue
        x0, y0, x1, y1 = _items_bbox(group)
        should_consider = (
            start_y - y_margin <= y0 <= end_y + y_margin
            and x0 < gutter < x1
            and (x1 - x0) >= split_min_width
        )
        if not should_consider:
            segments = [sorted(group, key=lambda item: (float(item["bbox"][0]), float(item["bbox"][1]), int(item["word_no"])))]
        else:
            ordered = sorted(group, key=lambda item: (float(item["bbox"][0]), float(item["bbox"][1]), int(item["word_no"])))
            gaps = [
                max(0.0, float(ordered[idx]["bbox"][0]) - float(ordered[idx - 1]["bbox"][2]))
                for idx in range(1, len(ordered))
            ]
            median_gap = _median(gaps, 0.0)
            split_idx = None
            best_gap = 0.0
            for idx in range(1, len(ordered)):
                prev = ordered[idx - 1]
                item = ordered[idx]
                gap = max(0.0, float(item["bbox"][0]) - float(prev["bbox"][2]))
                gap_mid = (float(prev["bbox"][2]) + float(item["bbox"][0])) / 2.0
                split_gap = max(8.0, median_gap * 1.8)
                if page_width * 0.38 <= gap_mid <= page_width * 0.62 and gap >= split_gap and gap > best_gap:
                    split_idx = idx
                    best_gap = gap
            if split_idx is not None:
                segments = [ordered[:split_idx], ordered[split_idx:]]
            else:
                segments = [ordered]

        base_line = int(key[1]) * 10
        block_no = int(key[0])
        for seg_idx, segment in enumerate(segments):
            if not segment:
                continue
            for word_idx, item in enumerate(segment):
                item["word_no"] = word_idx
            new_key = (block_no, base_line + seg_idx)
            while new_key in result:
                new_key = (new_key[0], new_key[1] + 1)
            result[new_key] = segment

    return result or groups


def _extract_page_words(page):
    items: list[dict] = []
    for x0, y0, x1, y1, text, block_no, line_no, word_no in page.get_text("words", sort=False) or []:
        items.append({
            "text": text or "",
            "ocr_text": text or "",
            "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
            "block_no": int(block_no),
            "line_no": int(line_no),
            "word_no": int(word_no),
            "confidence": DEFAULT_DIGITAL_CONFIDENCE,
            "fg_gray": DEFAULT_DIGITAL_FG_GRAY,
            "content_type": DEFAULT_DIGITAL_CONTENT_TYPE,
            "source_layer": "native",
        })
    native_groups = _group_native_block_lines(items)
    if _native_block_lines_are_usable(native_groups):
        groups = native_groups
    else:
        groups = _group_visual_items(items)
    return _split_two_column_line_groups(groups, float(page.rect.width), float(page.rect.height))



def _build_page_record_from_line_groups(
    page_index: int,
    page,
    render_width: int,
    render_height: int,
    line_groups: dict[tuple[int, int], list[dict]],
) -> dict:
    page_record = make_page_record(
        page_index=page_index,
        width=page.rect.width,
        height=page.rect.height,
        render_width=render_width,
        render_height=render_height,
    )
    if any(block_no != 0 for block_no, _ in line_groups):
        sorted_groups = sorted(line_groups.items(), key=lambda item: (item[0][0], item[0][1], *_visual_line_sort_key(item[1])))
    else:
        sorted_groups = sorted(line_groups.items(), key=lambda item: (*_visual_line_sort_key(item[1]), item[0][0], item[0][1]))

    for line_index, ((block_no, line_no), group_words) in enumerate(sorted_groups):
        ordered_words = sorted(group_words, key=lambda item: item["word_no"])
        line_words = []
        for word_index, word in enumerate(ordered_words):
            raw_text = (word.get("ocr_text") or word["text"] or "").strip()
            text = sanitize_ocr_surface_text((word["text"] or "").strip())
            if not text:
                continue

            x0, y0, x1, y1 = word["bbox"]
            line_words.append(make_word_record(
                page_index=page_index,
                line_index=line_index,
                word_index=len(line_words),
                text=text,
                x=x0,
                y=y0,
                w=x1 - x0,
                h=y1 - y0,
                has_space_after=(word_index < len(ordered_words) - 1),
                confidence=float(word.get("confidence", DEFAULT_DIGITAL_CONFIDENCE) or 0.0),
                fg_gray=int(word.get("fg_gray", DEFAULT_DIGITAL_FG_GRAY) or DEFAULT_DIGITAL_FG_GRAY),
                content_type=int(word.get("content_type", DEFAULT_DIGITAL_CONTENT_TYPE) or 0),
                ocr_text=raw_text,
            ))
            line_words[-1]["source_layer"] = word.get("source_layer") or "native"

        if not line_words:
            continue

        page_record["words"].extend(line_words)
        line_bbox = merge_bboxes([word["bbox"] for word in line_words])
        line_text = " ".join(word["text"] for word in line_words).strip()
        line_ocr_text = " ".join(word.get("ocr_text", word["text"]) for word in line_words).strip()
        line_height = max(1.0, line_bbox[3] - line_bbox[1])
        fg_values = [int(word.get("fg_gray", DEFAULT_DIGITAL_FG_GRAY) or DEFAULT_DIGITAL_FG_GRAY) for word in line_words]
        fg_gray = round(sum(fg_values) / len(fg_values)) if fg_values else DEFAULT_DIGITAL_FG_GRAY
        line_source_layers = {str(word.get("source_layer") or "native") for word in line_words}
        line_record = make_line_record(
            page_index=page_index,
            line_index=line_index,
            text=line_text,
            x=line_bbox[0],
            y=line_bbox[1],
            w=line_bbox[2] - line_bbox[0],
            h=line_height,
            font_size=max(DEFAULT_FONT_SIZE, line_height * 0.78),
            block_id=block_no,
            paragraph_id=line_no,
            confidence=DEFAULT_DIGITAL_CONFIDENCE,
            content_type=DEFAULT_DIGITAL_CONTENT_TYPE,
            fg_gray=fg_gray,
            word_ids=[word["id"] for word in line_words],
            ocr_text=line_ocr_text,
        )
        line_record["source_layer"] = next(iter(line_source_layers)) if len(line_source_layers) == 1 else "mixed"
        page_record["lines"].append(line_record)
    return page_record


def build_native_page_record(page_index: int, page) -> dict:
    render_width, render_height = _page_render_size(page)
    page_record = _build_page_record_from_line_groups(
        page_index,
        page,
        render_width,
        render_height,
        _extract_page_words(page),
    )
    page_record["coord_origin"] = "top-left"
    page_record["source_mode"] = "digital"
    return page_record


def _bbox_area(box: list[float]) -> float:
    return max(0.0, float(box[2]) - float(box[0])) * max(0.0, float(box[3]) - float(box[1]))


def _bbox_intersection(a: list[float], b: list[float]) -> float:
    return max(0.0, min(float(a[2]), float(b[2])) - max(float(a[0]), float(b[0]))) * max(0.0, min(float(a[3]), float(b[3])) - max(float(a[1]), float(b[1])))


def _bbox_coverage(inner: list[float], outer: list[float]) -> float:
    area = _bbox_area(inner)
    if area <= 0.0:
        return 0.0
    return _bbox_intersection(inner, outer) / area


def _record_bbox(record: dict) -> list[float] | None:
    box = record.get("bbox")
    if isinstance(box, list) and len(box) == 4:
        try:
            return [float(v) for v in box]
        except (TypeError, ValueError):
            return None
    try:
        x = float(record.get("x", 0.0) or 0.0)
        y = float(record.get("y", 0.0) or 0.0)
        w = float(record.get("w", 0.0) or 0.0)
        h = float(record.get("h", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if w <= 0.0 or h <= 0.0:
        return None
    return [x, y, x + w, y + h]


def _word_items_from_page_record(page: dict, source_layer: str) -> list[dict]:
    items: list[dict] = []
    for word_no, word in enumerate(page.get("words") or []):
        text = str(word.get("text") or word.get("ocr_text") or "").strip()
        box = _record_bbox(word)
        if not text or box is None:
            continue
        items.append({
            "text": text,
            "ocr_text": str(word.get("ocr_text") or text),
            "bbox": [round(float(v), 2) for v in box],
            "block_no": int(word.get("block_id", 0) or 0),
            "line_no": int(word.get("paragraph_id", word_no) or word_no),
            "word_no": word_no,
            "confidence": float(word.get("confidence", DEFAULT_DIGITAL_CONFIDENCE) or DEFAULT_DIGITAL_CONFIDENCE),
            "fg_gray": int(word.get("fg_gray", DEFAULT_DIGITAL_FG_GRAY) or DEFAULT_DIGITAL_FG_GRAY),
            "content_type": int(word.get("content_type", DEFAULT_DIGITAL_CONTENT_TYPE) or DEFAULT_DIGITAL_CONTENT_TYPE),
            "source_layer": str(word.get("source_layer") or source_layer),
        })
    return items


def merge_native_and_ocr_page_records(page_index: int, native_page, ocr_record: dict) -> dict:
    render_width, render_height = _page_render_size(native_page)
    native_record = _build_page_record_from_line_groups(
        page_index,
        native_page,
        render_width,
        render_height,
        _extract_page_words(native_page),
    )
    native_items = _word_items_from_page_record(native_record, "native")
    ocr_items = _word_items_from_page_record(ocr_record or {}, "ocr")
    rejection = _native_rejection_stats(native_items, ocr_items)
    if rejection["reject_native"]:
        merge_items = ocr_items
        ocr_only: list[dict] = []
        primary_layer = "ocr"
    else:
        native_boxes = [item["bbox"] for item in native_items]
        ocr_only = []
        for item in ocr_items:
            box = item["bbox"]
            if any(
                _bbox_coverage(box, native_box) >= 0.55
                or _bbox_coverage(native_box, box) >= 0.55
                for native_box in native_boxes
            ):
                continue
            ocr_only.append(item)
        merge_items = native_items + ocr_only
        primary_layer = "native"
    merged_record = _build_page_record_from_line_groups(
        page_index,
        native_page,
        render_width,
        render_height,
        _group_visual_items(merge_items),
    )
    merged_record["coord_origin"] = "top-left"
    merged_record["source_mode"] = "mixed"
    merged_record["text_layers"] = {
        "primary": primary_layer,
        "native_mojibake_rejected": rejection["native_mojibake_rejected"],
        "native_spacing_rejected": rejection["native_spacing_rejected"],
        "native_joined_token_count": rejection["native_joined_token_count"],
        "native_ocr_word_ratio": rejection["ocr_word_ratio"],
        "merged_ocr_only_words": len(ocr_only),
    }
    return merged_record


def _items_text(items: list[dict]) -> str:
    return " ".join(str(item.get("text") or "") for item in items).strip()


def _is_mojibake_char(ch: str) -> bool:
    code = ord(ch)
    return (
        0x0300 <= code <= 0x036F      # combining marks from broken extraction
        or 0x0370 <= code <= 0x052F   # Greek/Cyrillic substitutions
        or 0x02B0 <= code <= 0x02FF   # modifier letters such as ˱/˯
        or ch in {"ÿ", "þ", "ð"}
    )


def _looks_like_broken_native_text(text: str) -> bool:
    chars = [ch for ch in (text or "") if not ch.isspace()]
    if len(chars) < 40:
        return False
    suspicious = sum(1 for ch in chars if _is_mojibake_char(ch))
    return suspicious >= max(8, int(len(chars) * 0.025))


def _diacritic_mark_count(text: str) -> int:
    return sum(
        1
        for ch in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(ch) == "Mn"
    )


def _has_letter_digit_join(text: str) -> bool:
    chars = list(text or "")
    for idx in range(1, len(chars)):
        prev = chars[idx - 1]
        cur = chars[idx]
        if (prev.isalpha() and cur.isdigit()) or (prev.isdigit() and cur.isalpha()):
            return True
    return False


def _has_lower_to_upper_join(text: str) -> bool:
    chars = list(text or "")
    for idx in range(1, len(chars)):
        prev = chars[idx - 1]
        cur = chars[idx]
        if prev.islower() and cur.isupper():
            return True
    return False


def _looks_like_joined_native_token(text: str) -> bool:
    token = str(text or "").strip().strip(".,;:()[]{}\"'")
    if len(token) < 4:
        return False
    if _has_letter_digit_join(token) or _has_lower_to_upper_join(token):
        return True

    letters = [ch for ch in token if ch.isalpha()]
    if len(letters) < 6 or len(letters) != len(token):
        return False

    marks = _diacritic_mark_count(token)
    if marks < 2:
        return False

    # Vietnamese text is syllable-spaced. Long all-letter tokens with multiple
    # tone/diacritic marks are often two or more syllables glued by a broken
    # native text layer.
    return True


def _native_spacing_damage(native_items: list[dict], ocr_items: list[dict]) -> dict:
    tokens = [
        str(item.get("text") or item.get("ocr_text") or "").strip()
        for item in native_items or []
    ]
    tokens = [token for token in tokens if token]
    ocr_count = len([
        item for item in (ocr_items or [])
        if str(item.get("text") or item.get("ocr_text") or "").strip()
    ])
    native_count = len(tokens)
    if native_count < 40 or ocr_count <= native_count:
        return {
            "rejected": False,
            "joined_token_count": 0,
            "native_word_count": native_count,
            "ocr_word_count": ocr_count,
            "ocr_word_ratio": 0.0 if native_count <= 0 else round(ocr_count / native_count, 4),
        }

    joined_tokens = [token for token in tokens if _looks_like_joined_native_token(token)]
    joined_count = len(joined_tokens)
    ocr_ratio = ocr_count / max(1, native_count)

    # The OCR words are already available here. If OCR sees materially more
    # words and native has several glued tokens, prefer the OCR cache without
    # triggering another OCR pass.
    rejected = (
        ocr_ratio >= 1.08
        and joined_count >= max(4, int(native_count * 0.015))
    ) or (
        ocr_ratio >= 1.03
        and joined_count >= max(10, int(native_count * 0.035))
    )
    return {
        "rejected": bool(rejected),
        "joined_token_count": joined_count,
        "native_word_count": native_count,
        "ocr_word_count": ocr_count,
        "ocr_word_ratio": round(ocr_ratio, 4),
        "joined_token_examples": joined_tokens[:8],
    }


def _native_rejection_stats(native_items: list[dict], ocr_items: list[dict]) -> dict:
    native_text = _items_text(native_items)
    mojibake_rejected = bool(ocr_items and _looks_like_broken_native_text(native_text))
    spacing = _native_spacing_damage(native_items, ocr_items)
    return {
        "reject_native": bool(ocr_items and (mojibake_rejected or spacing["rejected"])),
        "native_mojibake_rejected": mojibake_rejected,
        "native_spacing_rejected": bool(spacing["rejected"]),
        "native_joined_token_count": spacing["joined_token_count"],
        "native_word_count": spacing["native_word_count"],
        "ocr_word_count": spacing["ocr_word_count"],
        "ocr_word_ratio": spacing["ocr_word_ratio"],
        "native_joined_token_examples": spacing.get("joined_token_examples", []),
    }


def merge_native_text_layer_into_canonical_json(
    canonical_json_path: str,
    source_pdf_path: str,
    *,
    merge_pages: list[int] | None = None,
    canonical_profile: str | None = None,
) -> dict:
    data = load_canonical(canonical_json_path)

    doc = fitz.open(source_pdf_path)
    try:
        pages_by_index = {
            int(page.get("page_index", idx)): page
            for idx, page in enumerate(data.get("pages") or [])
        }
        target_pages = sorted(set(merge_pages or [0]))
        details: list[dict] = []
        for page_index in target_pages:
            if page_index < 0 or page_index >= len(doc):
                continue
            pdf_page = doc[page_index]
            render_width, render_height = _page_render_size(pdf_page)
            native_record = _build_page_record_from_line_groups(
                page_index,
                pdf_page,
                render_width,
                render_height,
                _extract_page_words(pdf_page),
            )
            native_items = _word_items_from_page_record(native_record, "native")
            ocr_items = _word_items_from_page_record(pages_by_index.get(page_index, {}), "ocr")
            rejection = _native_rejection_stats(native_items, ocr_items)
            if rejection["reject_native"]:
                merge_items = ocr_items
                ocr_only: list[dict] = []
                primary_layer = "ocr"
            else:
                native_boxes = [item["bbox"] for item in native_items]
                ocr_only = []
                for item in ocr_items:
                    box = item["bbox"]
                    if any(
                        _bbox_coverage(box, native_box) >= 0.55
                        or _bbox_coverage(native_box, box) >= 0.55
                        for native_box in native_boxes
                    ):
                        continue
                    ocr_only.append(item)
                merge_items = native_items + ocr_only
                primary_layer = "native"
            merged_record = _build_page_record_from_line_groups(
                page_index,
                pdf_page,
                render_width,
                render_height,
                _group_visual_items(merge_items),
            )
            merged_record["coord_origin"] = "top-left"
            merged_record["text_layers"] = {
                "primary": primary_layer,
                "native_mojibake_rejected": rejection["native_mojibake_rejected"],
                "native_spacing_rejected": rejection["native_spacing_rejected"],
                "native_joined_token_count": rejection["native_joined_token_count"],
                "native_ocr_word_ratio": rejection["ocr_word_ratio"],
                "merged_ocr_only_words": len(ocr_only),
            }

            replaced = False
            pages = data.setdefault("pages", [])
            for idx, page in enumerate(pages):
                if int(page.get("page_index", idx)) == page_index:
                    pages[idx] = merged_record
                    replaced = True
                    break
            if not replaced:
                pages.append(merged_record)
                pages.sort(key=lambda page: int(page.get("page_index", 0)))
            details.append({
                "page_index": page_index,
                "native_words": len(native_items),
                "ocr_words": len(ocr_items),
                "native_mojibake_rejected": rejection["native_mojibake_rejected"],
                "native_spacing_rejected": rejection["native_spacing_rejected"],
                "native_joined_token_count": rejection["native_joined_token_count"],
                "native_ocr_word_ratio": rejection["ocr_word_ratio"],
                "native_joined_token_examples": rejection["native_joined_token_examples"],
                "merged_ocr_only_words": len(ocr_only),
            })
    finally:
        doc.close()

    ocr_pipeline = data.setdefault("pipeline", {}).setdefault("ocr", {})
    ocr_pipeline["digital_layer_merge"] = details
    ocr_pipeline["source_mode"] = "mixed"
    save_canonical(canonical_json_path, data, profile=canonical_profile)
    return {"pages": details}


def write_canonical_from_text_layer(
    pdf_path: str,
    *,
    canonical_profile: str | None = "layoutlmv3_runtime",
) -> tuple[bool, str | None, int]:
    """Synthesize a canonical `.json.zst` companion from the PDF's existing
    text layer — no OCR, no PDF copy.

    Used by the Kho import path for legacy archive ZIPs (exported without
    `.json.zst` sidecars): the exported PDFs already carry the invisible
    word-level text layer written by the OCR engine (see
    `direct_engine._build_text_page_words`), so the same native-word
    machinery as `extract_digital_pdf_as_ocr` can rebuild a full canonical
    document (pages → lines + words, bbox, reading order, two-column
    splitting) without re-running anything.

    Writes `<pdf_path>.json.zst` next to the PDF (matching the companion
    layout `resolve_companion` expects).

    Returns `(ok, error, word_count)` — `word_count` is the total number of
    extracted words; 0 means the PDF has no usable text layer (pure scan).
    """
    try:
        doc = fitz.open(pdf_path)
        try:
            ocr_data = make_document_stub(
                input_path=pdf_path,
                engine=DIGITAL_TEXT_ENGINE,
                ocr_dpi=OCR_DPI,
                source_path=pdf_path,
                text_normalization=OCR_TEXT_NORMALIZATION,
                raw_text_preserved=True,
                source_mode="digital",
            )
            word_count = 0
            for page_index in range(len(doc)):
                page = doc[page_index]
                render_width, render_height = _page_render_size(page)
                page_record = _build_page_record_from_line_groups(
                    page_index,
                    page,
                    render_width,
                    render_height,
                    _extract_page_words(page),
                )
                page_record["coord_origin"] = "top-left"
                page_record["source_mode"] = "digital"
                word_count += len(page_record.get("words") or [])
                ocr_data["pages"].append(page_record)
        finally:
            doc.close()
        save_canonical(pdf_path + ".json.zst", ocr_data, profile=canonical_profile)
        return True, None, word_count
    except Exception as exc:
        return False, str(exc), 0


def extract_digital_pdf_as_ocr(
    input_path: str,
    output_path: str,
    *,
    source_document_path: str | None = None,
    update_callback=None,
    canonical_profile=None,
):
    def log(msg, level="info"):
        if update_callback:
            try:
                update_callback(msg, level)
            except Exception:
                try:
                    update_callback(msg)
                except Exception:
                    pass

    try:
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        shutil.copy2(input_path, output_path)

        doc = fitz.open(input_path)
        ocr_data = make_document_stub(
            input_path=input_path,
            engine=DIGITAL_TEXT_ENGINE,
            ocr_dpi=OCR_DPI,
            source_path=source_document_path or input_path,
            text_normalization=OCR_TEXT_NORMALIZATION,
            raw_text_preserved=True,
            source_mode="digital",
        )

        total_pages = len(doc)
        log(f"Digital PDF path: extracting text+bbox from {total_pages} pages...", "info")

        for page_index in range(total_pages):
            page = doc[page_index]
            render_width, render_height = _page_render_size(page)
            page_record = _build_page_record_from_line_groups(
                page_index,
                page,
                render_width,
                render_height,
                _extract_page_words(page),
            )
            ocr_data["pages"].append(page_record)

        doc.close()
        json_path = output_path + ".json.zst"
        save_canonical(json_path, ocr_data, profile=canonical_profile)

        log(f"Digital extraction completed: {output_path}", "success")
        return True, None
    except Exception as exc:
        return False, str(exc)
