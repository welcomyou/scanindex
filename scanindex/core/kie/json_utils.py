import copy
import hashlib
import json
import os
import re
from datetime import datetime


SCHEMA_VERSION = "ocr_kie_document_v3"
ANNOTATION_SCHEMA = "kie_vi_official_v2"
KIE_TOKEN_RE = re.compile(
    r"[A-Za-zÀ-ỹĐđ0-9]+(?:/[A-Za-zÀ-ỹĐđ0-9]+)*|[-–—]|[^\w\s]",
    re.UNICODE,
)


def _utc_now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def compute_sha256(path):
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def bbox_from_xywh(x, y, w, h, digits=2):
    return [
        round(x, digits),
        round(y, digits),
        round(x + w, digits),
        round(y + h, digits),
    ]


def merge_bboxes(boxes, digits=2):
    valid = [b for b in boxes if b and len(b) == 4]
    if not valid:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        round(min(b[0] for b in valid), digits),
        round(min(b[1] for b in valid), digits),
        round(max(b[2] for b in valid), digits),
        round(max(b[3] for b in valid), digits),
    ]


def make_page_id(page_index):
    return f"p{page_index}"


def make_line_id(page_index, line_index):
    return f"p{page_index}_l{line_index}"


def make_word_id(page_index, line_index, word_index):
    return f"p{page_index}_l{line_index}_w{word_index}"


def make_region_id(page_index, region_index):
    return f"p{page_index}_r{region_index}"


def make_kie_token_id(page_index, token_index):
    return f"p{page_index}_t{token_index}"


def line_text_from_words(words):
    parts = []
    for word in words:
        parts.append(word.get("text", ""))
        if word.get("has_space_after", True):
            parts.append(" ")
    return "".join(parts).rstrip()


def make_document_stub(input_path, engine, ocr_dpi,
                       source_path=None,
                       text_normalization=None,
                       raw_text_preserved=False):
    abs_input = os.path.abspath(input_path)
    abs_source = os.path.abspath(source_path or input_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "input_path": abs_input,
        "document": {
            "source_path": abs_source,
            "source_name": os.path.basename(abs_source),
            "source_sha256": compute_sha256(abs_source),
        },
        "pipeline": {
            "ocr": {
                "engine": engine,
                "dpi": ocr_dpi,
                "input_path": abs_input,
                "text_normalization": text_normalization,
                "raw_text_preserved": bool(raw_text_preserved),
                "completed_at": _utc_now_iso(),
            },
            "correction": {
                "applied": False,
                "status": "not_run",
                "engine": None,
                "mode": "v8_final",
                "updated_at": None,
                "output_pdf": None,
            },
        },
        "annotations": {
            "schema": ANNOTATION_SCHEMA,
            "status": "unlabeled",
            "source": None,
            "field_instances": [],
            "relations": [],
        },
        "pages": [],
    }


def make_page_record(page_index, width, height, render_width, render_height,
                     applied_rotation=0):
    """Build a canonical page record.

    ``applied_rotation`` is the cardinal rotation (0/90/180/270) that
    preprocessing applied to the source page before OCR. Downstream viewers
    need this to render the original PDF in the same orientation as the OCR
    bboxes.
    """
    record = {
        "id": make_page_id(page_index),
        "page_index": page_index,
        "width": round(width, 2),
        "height": round(height, 2),
        "render_width": int(render_width),
        "render_height": int(render_height),
        "lines": [],
        "words": [],
        "kie_tokens": [],
        "layout_regions": [],
    }
    try:
        rot = int(applied_rotation) % 360
    except (TypeError, ValueError):
        rot = 0
    if rot:
        record["applied_rotation"] = rot
    return record


def make_word_record(page_index, line_index, word_index, text, x, y, w, h,
                     has_space_after, confidence, fg_gray, content_type,
                     ocr_text=None):
    return {
        "id": make_word_id(page_index, line_index, word_index),
        "page_id": make_page_id(page_index),
        "line_id": make_line_id(page_index, line_index),
        "order": word_index,
        "text": text,
        "ocr_text": ocr_text if ocr_text is not None else text,
        "bbox": bbox_from_xywh(x, y, w, h),
        "x": round(x, 2),
        "y": round(y, 2),
        "w": round(w, 2),
        "h": round(h, 2),
        "has_space_after": has_space_after,
        "confidence": round(confidence, 4),
        "fg_gray": fg_gray,
        "content_type": content_type,
    }


def make_line_record(page_index, line_index, text, x, y, w, h, font_size,
                     block_id, paragraph_id, confidence, content_type,
                     fg_gray, word_ids, ocr_text=None):
    return {
        "id": make_line_id(page_index, line_index),
        "page_id": make_page_id(page_index),
        "order": line_index,
        "text": text,
        "ocr_text": ocr_text if ocr_text is not None else text,
        "bbox": bbox_from_xywh(x, y, w, h),
        "x": round(x, 2),
        "y": round(y, 2),
        "w": round(w, 2),
        "h": round(h, 2),
        "font_size": round(font_size, 2),
        "block_id": block_id,
        "paragraph_id": paragraph_id,
        "confidence": round(confidence, 4),
        "content_type": content_type,
        "fg_gray": fg_gray,
        "word_ids": word_ids,
    }


def decorate_layout_regions(layout_regions, page_index, scale_x, scale_y):
    result = []
    for region_index, region in enumerate(layout_regions or []):
        item = dict(region)
        item["id"] = item.get("id", make_region_id(page_index, region_index))
        if "bbox" in item:
            bx = item["bbox"]
            item["bbox_px"] = list(bx)
            item["bbox_pdf"] = [
                round(bx[0] * scale_x, 2),
                round(bx[1] * scale_y, 2),
                round(bx[2] * scale_x, 2),
                round(bx[3] * scale_y, 2),
            ]
        result.append(item)
    return result


def _expected_word_count(line):
    if line.get("word_ids"):
        return len(line["word_ids"])
    base_text = line.get("ocr_text") or line.get("text", "")
    return len(base_text.split())


def _line_id_from_word_id(word_id):
    if not isinstance(word_id, str):
        return None
    match = re.match(r"^(p\d+_l\d+)(?:_w\d+)?$", word_id)
    return match.group(1) if match else None


def _slice_bbox(token_bbox, char_start, char_end, part_start, part_end):
    token_len = max(1, char_end - char_start)
    start_ratio = (part_start - char_start) / token_len
    end_ratio = (part_end - char_start) / token_len
    x0, y0, x1, y1 = token_bbox
    part_x0 = x0 + (x1 - x0) * start_ratio
    part_x1 = x0 + (x1 - x0) * end_ratio
    return [round(part_x0, 2), round(y0, 2), round(part_x1, 2), round(y1, 2)]


def _split_kie_token(token):
    text = token.get("text", "")
    matches = list(KIE_TOKEN_RE.finditer(text))
    if len(matches) <= 1:
        return [token]

    parts = []
    for match in matches:
        part_text = match.group(0)
        if not part_text.strip():
            continue
        start = token["char_start"] + match.start()
        end = token["char_start"] + match.end()
        part = dict(token)
        part["text"] = part_text
        part["bbox"] = _slice_bbox(
            token["bbox"],
            token["char_start"],
            token["char_end"],
            start,
            end,
        )
        part["char_start"] = start
        part["char_end"] = end
        parts.append(part)
    return parts or [token]


def _build_page_kie_tokens(page):
    words_by_id = {word["id"]: word for word in page.get("words", [])}
    ordered_lines = sorted(page.get("lines", []), key=lambda item: item.get("order", 0))
    tokens = []
    token_index = 0

    for line in ordered_lines:
        cursor = 0
        raw_tokens = []
        for word_order, word_id in enumerate(line.get("word_ids", [])):
            word = words_by_id.get(word_id)
            if not word:
                continue
            text = word.get("text", "")
            start = cursor
            end = start + len(text)
            raw_tokens.append({
                "page_id": page["id"],
                "line_id": line["id"],
                "line_order": line.get("order", 0),
                "word_order": word_order,
                "order": token_index,
                "text": text,
                "ocr_text": word.get("ocr_text", text),
                "bbox": list(word.get("bbox", [0.0, 0.0, 0.0, 0.0])),
                "source_word_ids": [word_id],
                "char_start": start,
                "char_end": end,
            })
            token_index += 1
            cursor = end + (1 if word.get("has_space_after", True) else 0)

        split_tokens = []
        for token in raw_tokens:
            split_tokens.extend(_split_kie_token(token))

        for token in split_tokens:
            token["id"] = make_kie_token_id(page["page_index"], len(tokens))
            token["order"] = len(tokens)
            tokens.append(token)

    return tokens


def _page_index_from_ref(ref):
    if not isinstance(ref, str) or not ref.startswith("p"):
        return None
    digits = []
    for ch in ref[1:]:
        if ch.isdigit():
            digits.append(ch)
        else:
            break
    if not digits:
        return None
    return int("".join(digits))


def _field_text_from_refs(field, line_lookup, word_lookup):
    text = (field.get("text") or "").strip()
    if text:
        return text

    word_ids = list(field.get("word_ids") or [])
    if word_ids:
        words = [word_lookup[word_id] for word_id in word_ids if word_id in word_lookup]
        if words:
            return line_text_from_words(words)

    line_texts = []
    for line_id in field.get("line_ids") or []:
        line = line_lookup.get(line_id)
        if line and line.get("text"):
            line_texts.append(line["text"])
    return "\n".join(line_texts).strip()


def _normalize_field_instance(field, field_index, line_lookup, word_lookup):
    item = dict(field or {})
    item.setdefault("field_id", f"field_{field_index}")
    item["label"] = (item.get("label") or "").strip()
    item["line_ids"] = list(item.get("line_ids") or [])
    item["word_ids"] = list(item.get("word_ids") or [])

    if item.get("page_index") is None:
        for ref in item["word_ids"] + item["line_ids"]:
            page_index = _page_index_from_ref(ref)
            if page_index is not None:
                item["page_index"] = page_index
                break

    item["text"] = _field_text_from_refs(item, line_lookup, word_lookup)
    item.setdefault("normalized_value", None)
    item.setdefault("source", None)
    item.setdefault("confidence", None)
    item.setdefault("review_status", "draft")
    if "is_manual" in item and item["is_manual"] and item["review_status"] == "draft":
        item["review_status"] = "reviewed"
    return item


def _normalize_relation(relation, relation_index):
    item = dict(relation or {})
    item.setdefault("relation_id", f"relation_{relation_index}")
    item.setdefault("type", None)
    item.setdefault("from_field_id", None)
    item.setdefault("to_field_id", None)
    return item


def upgrade_ocr_data_in_place(ocr_data):
    if not isinstance(ocr_data, dict):
        return ocr_data

    ocr_data["schema_version"] = SCHEMA_VERSION
    document = ocr_data.setdefault("document", {})
    source_path = document.get("source_path") or ocr_data.get("input_path")
    document.setdefault("source_path", source_path)
    document.setdefault("source_name", os.path.basename(source_path or ""))
    document.setdefault("source_sha256", compute_sha256(source_path or ""))

    pipeline = ocr_data.setdefault("pipeline", {})
    ocr_pipeline = pipeline.setdefault("ocr", {})
    ocr_pipeline.setdefault("engine", ocr_data.get("engine"))
    ocr_pipeline.setdefault("dpi", None)
    ocr_pipeline.setdefault("input_path", ocr_data.get("input_path"))
    ocr_pipeline.setdefault("text_normalization", None)
    ocr_pipeline.setdefault("raw_text_preserved", False)
    ocr_pipeline.setdefault("completed_at", None)

    correction = pipeline.setdefault("correction", {})
    correction.setdefault("applied", False)
    correction.setdefault("status", "unknown")
    correction.setdefault("engine", None)
    correction.setdefault("mode", "accent_only")
    correction.setdefault("updated_at", None)
    correction.setdefault("output_pdf", None)
    ocr_data.setdefault("annotations", {
        "schema": ANNOTATION_SCHEMA,
        "status": "unlabeled",
        "source": None,
        "field_instances": [],
        "relations": [],
    })

    for page_index, page in enumerate(ocr_data.get("pages", [])):
        page.setdefault("id", make_page_id(page_index))
        page.setdefault("page_index", page_index)

        lines = page.get("lines", [])
        words = page.get("words", [])

        had_line_word_ids = any(line.get("word_ids") for line in lines)

        for line_index, line in enumerate(lines):
            line.setdefault("id", make_line_id(page_index, line_index))
            line.setdefault("page_id", page["id"])
            line.setdefault("order", line_index)
            line.setdefault("ocr_text", line.get("text", ""))
            line.setdefault("bbox", bbox_from_xywh(
                line.get("x", 0.0), line.get("y", 0.0),
                line.get("w", 0.0), line.get("h", 0.0)))
            line.setdefault("word_ids", [])

        line_ids = {str(line["id"]) for line in lines}
        line_index_by_id = {str(line["id"]): i for i, line in enumerate(lines)}
        has_word_line_links = any(
            (str(word.get("line_id") or "") in line_ids)
            or (_line_id_from_word_id(word.get("id")) in line_ids)
            for word in words
        )

        if words and not had_line_word_ids and has_word_line_links:
            line_word_ids = {line_id: [] for line_id in line_ids}
            local_orders = {line_id: 0 for line_id in line_ids}
            for word in words:
                parsed_line_id = _line_id_from_word_id(word.get("id"))
                current_line_id = str(word.get("line_id") or "")
                if parsed_line_id in line_ids:
                    line_id = parsed_line_id
                elif current_line_id in line_ids:
                    line_id = current_line_id
                else:
                    continue
                line_index = line_index_by_id[line_id]
                local_order = local_orders[line_id]
                word.setdefault("id", make_word_id(page_index, line_index, local_order))
                word.setdefault("page_id", page["id"])
                word["line_id"] = line_id
                word.setdefault("order", local_order)
                word.setdefault("ocr_text", word.get("text", ""))
                word.setdefault("bbox", bbox_from_xywh(
                    word.get("x", 0.0), word.get("y", 0.0),
                    word.get("w", 0.0), word.get("h", 0.0)))
                line_word_ids[line_id].append(word["id"])
                local_orders[line_id] += 1
            for line in lines:
                line["word_ids"] = line_word_ids.get(str(line["id"]), [])
        else:
            word_cursor = 0
            for line_index, line in enumerate(lines):
                line_id = line["id"]
                expected = _expected_word_count(line)
                line_word_ids = []
                for local_order in range(expected):
                    if word_cursor >= len(words):
                        break
                    word = words[word_cursor]
                    word_cursor += 1
                    word.setdefault("id", make_word_id(page_index, line_index, local_order))
                    word.setdefault("page_id", page["id"])
                    word["line_id"] = line_id
                    word.setdefault("order", local_order)
                    word.setdefault("ocr_text", word.get("text", ""))
                    word.setdefault("bbox", bbox_from_xywh(
                        word.get("x", 0.0), word.get("y", 0.0),
                        word.get("w", 0.0), word.get("h", 0.0)))
                    line_word_ids.append(word["id"])
                line["word_ids"] = line_word_ids

            for orphan_index in range(word_cursor, len(words)):
                word = words[orphan_index]
                word.setdefault("id", make_word_id(page_index, len(lines), orphan_index - word_cursor))
                word.setdefault("page_id", page["id"])
                word.setdefault("order", orphan_index - word_cursor)
                word.setdefault("ocr_text", word.get("text", ""))
                word.setdefault("bbox", bbox_from_xywh(
                    word.get("x", 0.0), word.get("y", 0.0),
                    word.get("w", 0.0), word.get("h", 0.0)))

        for region_index, region in enumerate(page.get("layout_regions", [])):
            region.setdefault("id", make_region_id(page_index, region_index))
            if "bbox" in region and "bbox_px" not in region:
                region["bbox_px"] = list(region["bbox"])

        page["kie_tokens"] = _build_page_kie_tokens(page)

    line_lookup = {}
    word_lookup = {}
    for page in ocr_data.get("pages", []):
        for line in page.get("lines", []):
            line_lookup[line["id"]] = line
        for word in page.get("words", []):
            word_lookup[word["id"]] = word

    annotations = ocr_data.setdefault("annotations", {})
    annotations["field_instances"] = [
        _normalize_field_instance(field, index, line_lookup, word_lookup)
        for index, field in enumerate(annotations.get("field_instances", []))
        if isinstance(field, dict)
    ]
    annotations["relations"] = [
        _normalize_relation(relation, index)
        for index, relation in enumerate(annotations.get("relations", []))
        if isinstance(relation, dict)
    ]

    return ocr_data


def _record_bbox(record):
    bbox = record.get("bbox") if isinstance(record, dict) else None
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        try:
            return [round(float(v), 2) for v in bbox[:4]]
        except (TypeError, ValueError):
            pass
    if isinstance(record, dict) and all(k in record for k in ("x", "y", "w", "h")):
        try:
            return bbox_from_xywh(
                float(record.get("x", 0.0)),
                float(record.get("y", 0.0)),
                float(record.get("w", 0.0)),
                float(record.get("h", 0.0)),
            )
        except (TypeError, ValueError):
            return None
    return None


def _copy_if_present(src, dst, keys):
    for key in keys:
        if key in src and src.get(key) is not None:
            dst[key] = src.get(key)


def _slim_word_record(word):
    out = {}
    _copy_if_present(
        word,
        out,
        ("id", "line_id", "order", "text", "fg_gray"),
    )
    bbox = _record_bbox(word)
    if bbox is not None:
        out["bbox"] = bbox
    return out


def _slim_line_record(line):
    out = {}
    _copy_if_present(
        line,
        out,
        ("id", "order", "text", "font_size", "fg_gray"),
    )
    bbox = _record_bbox(line)
    if bbox is not None:
        out["bbox"] = bbox
    return out


def slim_canonical_for_layoutlmv3_runtime_in_place(ocr_data):
    """Drop runtime-unused canonical fields while preserving LayoutLMv3 inputs.

    Kept data:
      - page geometry/source fields for PDF/image rendering
      - word text/bbox/line_id/style metadata for LayoutLMv3 text and visual
      - line text/bbox/font/gray metadata for style lookup, splitter, and editor
      - annotations and document/pipeline metadata

    Removed data:
      - page.kie_tokens and layout_regions
      - OCR raw duplicates, confidence/content_type, x/y/w/h/page_id fields
      - line.word_ids because word.line_id is the canonical ownership link
    """
    if not isinstance(ocr_data, dict):
        return ocr_data

    pipeline = ocr_data.setdefault("pipeline", {})
    ocr_pipeline = pipeline.setdefault("ocr", {})
    ocr_pipeline["canonical_profile"] = "layoutlmv3_runtime_v1"

    page_keep = (
        "id",
        "page_index",
        "width",
        "height",
        "render_width",
        "render_height",
        "coord_origin",
        "coord_origin_source",
        "applied_rotation",
        "image_path",
        "render_path",
        "page_image",
        "image",
        "ocr_render_annots",
        "kie_render_annots",
        "kie_ocr_override",
    )
    for page in ocr_data.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        slim_page = {}
        _copy_if_present(page, slim_page, page_keep)
        slim_page["lines"] = [
            _slim_line_record(line)
            for line in (page.get("lines") or [])
            if isinstance(line, dict)
        ]
        slim_page["words"] = [
            _slim_word_record(word)
            for word in (page.get("words") or [])
            if isinstance(word, dict)
        ]
        page.clear()
        page.update(slim_page)
    return ocr_data


def rebuild_lines_from_words(original_lines, corrected_words):
    if not original_lines or not corrected_words:
        return original_lines or []

    word_map = {word.get("id"): word for word in corrected_words if word.get("id")}
    result_lines = []
    word_idx = 0

    for line in original_lines:
        line_words = []
        word_ids = line.get("word_ids") or []
        for word_id in word_ids:
            word = word_map.get(word_id)
            if word:
                line_words.append(word)

        if not line_words:
            count = _expected_word_count(line)
            line_words = corrected_words[word_idx:word_idx + count]
            word_idx += len(line_words)

        updated = copy.deepcopy(line)
        if "ocr_text" not in updated:
            updated["ocr_text"] = updated.get("text", "")
        if line_words:
            updated["text"] = line_text_from_words(line_words)
            updated["bbox"] = merge_bboxes([word.get("bbox") for word in line_words])
            if updated["bbox"] != [0.0, 0.0, 0.0, 0.0]:
                updated["x"] = updated["bbox"][0]
                updated["y"] = updated["bbox"][1]
                updated["w"] = round(updated["bbox"][2] - updated["bbox"][0], 2)
                updated["h"] = round(updated["bbox"][3] - updated["bbox"][1], 2)
        result_lines.append(updated)

    return result_lines


def _apply_phrase_replacements(words, replacements):
    for old_phrase, new_phrase in replacements.items():
        if " " not in old_phrase:
            continue
        old_words = old_phrase.split()
        new_words = new_phrase.split()
        n = len(old_words)
        i = 0
        while i <= len(words) - n:
            if all(words[i + j].get("text") == old_words[j] for j in range(n)):
                if len(new_words) == n:
                    for j in range(n):
                        words[i + j]["text"] = new_words[j]
                i += n
            else:
                i += 1


def apply_replacements_to_ocr_data(ocr_data, replacements, output_pdf_path=None,
                                   correction_engine="proton_ct2_opt",
                                   correction_mode="v8_final"):
    upgrade_ocr_data_in_place(ocr_data)

    for page in ocr_data.get("pages", []):
        corrected_words = []
        for word in page.get("words", []):
            updated = copy.deepcopy(word)
            if "ocr_text" not in updated:
                updated["ocr_text"] = updated.get("text", "")
            updated["text"] = replacements.get(updated.get("text", ""), updated.get("text", ""))
            corrected_words.append(updated)

        _apply_phrase_replacements(corrected_words, replacements)
        page["words"] = corrected_words
        page["lines"] = rebuild_lines_from_words(page.get("lines", []), corrected_words)

    correction = ocr_data.setdefault("pipeline", {}).setdefault("correction", {})
    correction["applied"] = bool(replacements)
    correction["status"] = "applied" if replacements else "no_changes"
    correction["engine"] = correction_engine
    correction["mode"] = correction_mode
    correction["updated_at"] = _utc_now_iso()
    if output_pdf_path:
        correction["output_pdf"] = os.path.abspath(output_pdf_path)

    return upgrade_ocr_data_in_place(ocr_data)


def write_corrected_companion_json(source_json_path, target_json_path, replacements,
                                   output_pdf_path=None,
                                   correction_engine="proton_ct2_opt",
                                   correction_mode="v8_final"):
    if not source_json_path or not os.path.exists(source_json_path):
        return False

    with open(source_json_path, "r", encoding="utf-8") as f:
        ocr_data = json.load(f)

    apply_replacements_to_ocr_data(
        ocr_data,
        replacements or {},
        output_pdf_path=output_pdf_path,
        correction_engine=correction_engine,
        correction_mode=correction_mode,
    )

    target_json_path = target_json_path or source_json_path
    target_dir = os.path.dirname(target_json_path)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)
    temp_json_path = target_json_path + ".tmp"
    with open(temp_json_path, "w", encoding="utf-8") as f:
        json.dump(ocr_data, f, ensure_ascii=False)
    os.replace(temp_json_path, target_json_path)
    return True
