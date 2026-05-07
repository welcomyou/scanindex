from __future__ import annotations

import json
import os
import shutil

import fitz

from scanindex.core.kie.json_utils import (
    make_document_stub,
    make_line_record,
    make_page_record,
    make_word_record,
    merge_bboxes,
    slim_canonical_for_layoutlmv3_runtime_in_place,
    upgrade_ocr_data_in_place,
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

    Đọc canonical JSON `<ocr_pdf_path>.json`, check `document.engine`.
    Trả True nếu engine = DIGITAL_TEXT_ENGINE → caller nên skip correction.
    """
    json_path = ocr_pdf_path + ".json"
    if not os.path.exists(json_path):
        return False
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("document", {}).get("engine") == DIGITAL_TEXT_ENGINE
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


def _extract_page_words(page):
    items: list[dict] = []
    for x0, y0, x1, y1, text, block_no, line_no, word_no in page.get_text("words", sort=True) or []:
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
    return _group_visual_items(items)


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

        if not line_words:
            continue

        page_record["words"].extend(line_words)
        line_bbox = merge_bboxes([word["bbox"] for word in line_words])
        line_text = " ".join(word["text"] for word in line_words).strip()
        line_ocr_text = " ".join(word.get("ocr_text", word["text"]) for word in line_words).strip()
        line_height = max(1.0, line_bbox[3] - line_bbox[1])
        fg_values = [int(word.get("fg_gray", DEFAULT_DIGITAL_FG_GRAY) or DEFAULT_DIGITAL_FG_GRAY) for word in line_words]
        fg_gray = round(sum(fg_values) / len(fg_values)) if fg_values else DEFAULT_DIGITAL_FG_GRAY
        page_record["lines"].append(make_line_record(
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
        ))
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
            "source_layer": source_layer,
        })
    return items


def merge_native_text_layer_into_canonical_json(
    canonical_json_path: str,
    source_pdf_path: str,
    *,
    merge_pages: list[int] | None = None,
    canonical_profile: str | None = None,
) -> dict:
    with open(canonical_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

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
            native_boxes = [item["bbox"] for item in native_items]
            ocr_only: list[dict] = []
            for item in ocr_items:
                box = item["bbox"]
                if any(_bbox_coverage(box, native_box) >= 0.55 or _bbox_coverage(native_box, box) >= 0.55 for native_box in native_boxes):
                    continue
                ocr_only.append(item)
            merged_record = _build_page_record_from_line_groups(
                page_index,
                pdf_page,
                render_width,
                render_height,
                _group_visual_items(native_items + ocr_only),
            )
            merged_record["coord_origin"] = "top-left"
            merged_record["text_layers"] = {
                "primary": "native",
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
                "merged_ocr_only_words": len(ocr_only),
            })
    finally:
        doc.close()

    data.setdefault("pipeline", {}).setdefault("ocr", {})["digital_layer_merge"] = details
    upgrade_ocr_data_in_place(data)
    if canonical_profile in {"layoutlmv3_runtime", "layoutlmv3_runtime_v1"}:
        slim_canonical_for_layoutlmv3_runtime_in_place(data)
    tmp_path = canonical_json_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, canonical_json_path)
    return {"pages": details}


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
        upgrade_ocr_data_in_place(ocr_data)
        if canonical_profile == "layoutlmv3_runtime":
            slim_canonical_for_layoutlmv3_runtime_in_place(ocr_data)
        json_path = output_path + ".json"
        json_tmp_path = json_path + ".tmp"
        with open(json_tmp_path, "w", encoding="utf-8") as f:
            json.dump(ocr_data, f, ensure_ascii=False)
        os.replace(json_tmp_path, json_path)

        log(f"Digital extraction completed: {output_path}", "success")
        return True, None
    except Exception as exc:
        return False, str(exc)
