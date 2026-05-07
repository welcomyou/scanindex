from __future__ import annotations

import os
import sys
from pathlib import Path

from kie_json_utils import merge_bboxes
from train_kie.common import read_json, write_json
from train_kie.exporters import (
    _align_segmented_surface_tokens,
    _page_line_labels,
    _label_page_surface_tokens,
    _render_page_image,
    load_canonical_json,
    normalize_bbox,
)
from train_kie.ontology import (
    ONTOLOGY_ID,
    normalize_value,
    paddle_export_label_for_fields,
)
from train_kie.semantic_fields import (
    collect_doc_type_semantic_evidence,
    extract_explicit_doc_type_from_line,
    find_contiguous_word_ids_for_text,
    infer_semantic_doc_type_field,
)


def _ordered_unique(values):
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _join_surface_text(tokens: list[str]) -> str:
    if not tokens:
        return ""

    no_space_before = {".", ",", ";", ":", "!", "?", ")", "]", "}", "%"}
    no_space_after = {"(", "[", "{", "\"", "'"}
    joiner_tokens = {"-", "–", "—", "/"}

    result = tokens[0]
    for prev, current in zip(tokens, tokens[1:]):
        if current in no_space_before or prev in no_space_after or current in joiner_tokens or prev in joiner_tokens:
            result += current
        else:
            result += " " + current
    return result.strip()


def _word_surface_map(page: dict):
    ordered_tokens, _, _, _, _, _ = _label_page_surface_tokens(page, {"annotations": {"field_instances": []}})
    mapping = {}
    for token in ordered_tokens:
        for word_id in token.get("source_word_ids", []):
            mapping.setdefault(word_id, set()).add(token["id"])
    return mapping


def _line_lookup(page: dict):
    return {line["id"]: line for line in page.get("lines", [])}


def _surface_span_to_field(page_index: int, label: str, span_items: list[dict], page_word_surface_map: dict, field_index: int, source: str):
    line_ids = _ordered_unique([
        line_id
        for item in span_items
        for line_id in item.get("line_ids", [])
        if line_id
    ])

    span_surface_ids = {
        surface_id
        for item in span_items
        for surface_id in item.get("surface_token_ids", [])
    }
    candidate_word_ids = _ordered_unique([
        word_id
        for item in span_items
        for word_id in item.get("source_word_ids", [])
        if word_id
    ])
    word_ids = [
        word_id
        for word_id in candidate_word_ids
        if page_word_surface_map.get(word_id, set()) and page_word_surface_map[word_id].issubset(span_surface_ids)
    ]
    text = _join_surface_text([item.get("text", "") for item in span_items if item.get("text", "").strip()])
    return {
        "field_id": f"f{field_index}",
        "label": label,
        "page_index": page_index,
        "line_ids": line_ids,
        "word_ids": word_ids,
        "text": text,
        "normalized_value": normalize_value(label, text),
        "confidence": None,
        "source": source,
        "review_status": "predicted",
    }


def _line_span_to_field(page_index: int, label: str, span_items: list[dict], line_lookup: dict, field_index: int, source: str):
    line_ids = _ordered_unique([item.get("line_id") for item in span_items if item.get("line_id")])
    word_ids = _ordered_unique([
        word_id
        for line_id in line_ids
        for word_id in (line_lookup.get(line_id, {}).get("word_ids") or [])
    ])
    texts = [item.get("text", "").strip() for item in span_items if item.get("text", "").strip()]
    text = "\n".join(texts)
    return {
        "field_id": f"f{field_index}",
        "label": label,
        "page_index": page_index,
        "line_ids": line_ids,
        "word_ids": word_ids,
        "text": text,
        "normalized_value": normalize_value(label, text),
        "confidence": None,
        "source": source,
        "review_status": "predicted",
    }


def _normalize_paddle_item_label(label: str | None) -> str:
    normalized = (label or "").strip().upper()
    if normalized in {"", "O", "OTHER", "OTHERS", "IGNORE"}:
        return "OTHER"
    return normalized


def _infer_relation_type(left_label: str, right_label: str):
    if left_label == "SIGNER_ROLE" and right_label == "SIGNER_NAME":
        return "signed_by", "forward"
    if left_label == "SIGNER_NAME" and right_label == "SIGNER_ROLE":
        return "signed_by", "reverse"
    return None, None


def prepare_lilt_xlmr_inputs(canonical_json_path: str) -> dict:
    doc = load_canonical_json(canonical_json_path)
    pages = []
    doc_id = Path(canonical_json_path).stem

    for page in doc.get("pages", []):
        ordered_tokens, _, _, _, _, _ = _label_page_surface_tokens(page, doc)
        ordered_tokens = [token for token in ordered_tokens if token.get("text", "").strip()]
        if not ordered_tokens:
            continue
        pages.append({
            "record_id": f"{doc_id}_{page['id']}",
            "page_index": page["page_index"],
            "tokens": [token["text"] for token in ordered_tokens],
            "bboxes": [normalize_bbox(token["bbox"], page["width"], page["height"]) for token in ordered_tokens],
            "surface_token_ids": [[token["id"]] for token in ordered_tokens],
            "source_word_ids": [list(token.get("source_word_ids", [])) for token in ordered_tokens],
            "line_ids": [[token["line_id"]] for token in ordered_tokens],
        })

    return {
        "track": "lilt_xlmr",
        "source_canonical_json": str(Path(canonical_json_path).resolve()),
        "pages": pages,
    }


def prepare_lilt_phobert_inputs(canonical_json_path: str, segmenter_mode: str = "underthesea") -> dict:
    doc = load_canonical_json(canonical_json_path)
    pages = []
    doc_id = Path(canonical_json_path).stem

    for page in doc.get("pages", []):
        ordered_tokens, _, _, _, _, _ = _label_page_surface_tokens(page, doc)
        if not ordered_tokens:
            continue

        line_groups = {}
        line_texts = {}
        for token in ordered_tokens:
            line_groups.setdefault(token["line_id"], []).append(token)
        for line in page.get("lines", []):
            line_texts[line["id"]] = line.get("text", "")

        tokens = []
        bboxes = []
        surface_token_ids = []
        source_word_ids = []
        line_ids = []
        for line in sorted(page.get("lines", []), key=lambda item: item.get("order", 0)):
            aligned = _align_segmented_surface_tokens(
                line_groups.get(line["id"], []),
                line_texts.get(line["id"], ""),
                segmenter_mode,
            )
            for token_text, surface_tokens in aligned:
                clean_text = token_text.strip()
                if not clean_text:
                    continue
                tokens.append(clean_text)
                bboxes.append(normalize_bbox(
                    merge_bboxes([token["bbox"] for token in surface_tokens]),
                    page["width"],
                    page["height"],
                ))
                surface_token_ids.append([token["id"] for token in surface_tokens])
                source_word_ids.append(_ordered_unique([
                    word_id
                    for token in surface_tokens
                    for word_id in token.get("source_word_ids", [])
                ]))
                line_ids.append(_ordered_unique([token["line_id"] for token in surface_tokens]))

        if tokens:
            pages.append({
                "record_id": f"{doc_id}_{page['id']}",
                "page_index": page["page_index"],
                "segmenter_mode": segmenter_mode,
                "tokens": tokens,
                "bboxes": bboxes,
                "surface_token_ids": surface_token_ids,
                "source_word_ids": source_word_ids,
                "line_ids": line_ids,
            })

    return {
        "track": "lilt_phobert",
        "source_canonical_json": str(Path(canonical_json_path).resolve()),
        "segmenter_mode": segmenter_mode,
        "pages": pages,
    }


def prepare_paddle_inputs(canonical_json_path: str, output_dir: str | None = None, render_dpi: int = 200) -> dict:
    doc = load_canonical_json(canonical_json_path)
    pdf_path = (
        doc.get("pipeline", {}).get("correction", {}).get("output_pdf")
        or doc.get("input_path")
        or doc.get("document", {}).get("source_path")
    )
    pages = []
    doc_id = Path(canonical_json_path).stem
    images_dir = Path(output_dir).resolve() if output_dir else None
    if images_dir:
        images_dir.mkdir(parents=True, exist_ok=True)

    for page in doc.get("pages", []):
        line_label_map, _ = _page_line_labels(page, doc)
        items = []
        next_item_id = 1
        for line in sorted(page.get("lines", []), key=lambda item: item.get("order", 0)):
            text = (line.get("text") or "").strip()
            bbox = line.get("bbox", [0.0, 0.0, 0.0, 0.0])
            if not text or bbox == [0.0, 0.0, 0.0, 0.0]:
                continue
            x0 = round(bbox[0] * render_dpi / 72.0)
            y0 = round(bbox[1] * render_dpi / 72.0)
            x1 = round(bbox[2] * render_dpi / 72.0)
            y1 = round(bbox[3] * render_dpi / 72.0)
            items.append({
                "id": next_item_id,
                "text": text,
                "transcription": text,
                "points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                "line_id": line["id"],
                "line_ids": [line["id"]],
                "source_word_ids": list(line.get("word_ids", [])),
                "label": paddle_export_label_for_fields(line_label_map.get(line["id"], set())),
                "linking": [],
            })
            next_item_id += 1

        image_path = None
        if images_dir and pdf_path and os.path.exists(pdf_path):
            image_path = images_dir / f"{doc_id}_{page['id']}.png"
            _render_page_image(pdf_path, page["page_index"], str(image_path), render_dpi)

        pages.append({
            "page_index": page["page_index"],
            "image_path": str(image_path.resolve()) if image_path else None,
            "items": items,
        })

    return {
        "track": "paddle_kie",
        "source_canonical_json": str(Path(canonical_json_path).resolve()),
        "render_dpi": render_dpi,
        "pages": pages,
    }


def decode_lilt_predictions(prepared_payload: dict, model_name: str) -> dict:
    canonical_json_path = prepared_payload["source_canonical_json"]
    doc = load_canonical_json(canonical_json_path)
    page_lookup = {page["page_index"]: page for page in doc.get("pages", [])}

    fields = []
    field_index = 1
    for page_payload in prepared_payload.get("pages", []):
        page_index = page_payload["page_index"]
        page = page_lookup.get(page_index)
        if not page:
            continue
        tags = page_payload.get("predicted_tags") or []
        if len(tags) != len(page_payload.get("tokens", [])):
            raise ValueError(f"predicted_tags length mismatch on page {page_index}")

        entries = []
        for idx, tag in enumerate(tags):
            entries.append({
                "tag": tag,
                "text": page_payload["tokens"][idx],
                "surface_token_ids": list(page_payload.get("surface_token_ids", [])[idx]),
                "source_word_ids": list(page_payload.get("source_word_ids", [])[idx]),
                "line_ids": list(page_payload.get("line_ids", [])[idx]),
            })

        current_label = None
        current_items = []
        page_word_surface_map = _word_surface_map(page)

        for entry in entries:
            tag = entry["tag"]
            if not tag or tag == "O":
                if current_items:
                    fields.append(_surface_span_to_field(
                        page_index,
                        current_label,
                        current_items,
                        page_word_surface_map,
                        field_index,
                        model_name,
                    ))
                    field_index += 1
                    current_label = None
                    current_items = []
                continue

            if "-" not in tag:
                print(
                    f"Warning: skipping malformed tag '{tag}' on page {page_index}.",
                    file=sys.stderr,
                )
                if current_items:
                    fields.append(_surface_span_to_field(
                        page_index,
                        current_label,
                        current_items,
                        page_word_surface_map,
                        field_index,
                        model_name,
                    ))
                    field_index += 1
                    current_label = None
                    current_items = []
                continue

            prefix, label = tag.split("-", 1)
            if prefix == "B" or current_label != label:
                if current_items:
                    fields.append(_surface_span_to_field(
                        page_index,
                        current_label,
                        current_items,
                        page_word_surface_map,
                        field_index,
                        model_name,
                    ))
                    field_index += 1
                current_label = label
                current_items = [entry]
            else:
                current_items.append(entry)

        if current_items:
            fields.append(_surface_span_to_field(
                page_index,
                current_label,
                current_items,
                page_word_surface_map,
                field_index,
                model_name,
            ))
            field_index += 1

    return {
        "schema": ONTOLOGY_ID,
        "field_instances": fields,
        "relations": [],
    }


def decode_paddle_predictions(prepared_payload: dict, model_name: str) -> dict:
    canonical_json_path = prepared_payload["source_canonical_json"]
    doc = load_canonical_json(canonical_json_path)
    page_lookup = {page["page_index"]: page for page in doc.get("pages", [])}

    fields = []
    item_to_field = {}
    field_index = 1
    relations = []
    relation_index = 1
    seen_relation_keys = set()

    for page_payload in prepared_payload.get("pages", []):
        page_index = page_payload["page_index"]
        page = page_lookup.get(page_index)
        if not page:
            continue
        line_lookup = _line_lookup(page)

        current_label = None
        current_items = []
        for item in page_payload.get("items", []):
            label = _normalize_paddle_item_label(item.get("predicted_label") or item.get("label") or "OTHER")
            if label == "OTHER":
                if current_items:
                    field = _line_span_to_field(
                        page_index,
                        current_label,
                        current_items,
                        line_lookup,
                        field_index,
                        model_name,
                    )
                    fields.append(field)
                    for span_item in current_items:
                        item_to_field[span_item["item_id"]] = field["field_id"]
                    field_index += 1
                    current_label = None
                    current_items = []
                continue

            entry = {
                "item_id": item["id"],
                "text": item.get("text") or item.get("transcription", ""),
                "source_word_ids": list(item.get("source_word_ids", [])),
                "line_id": item.get("line_id") or (item.get("line_ids") or [None])[0],
            }

            if current_items and current_label != label:
                field = _line_span_to_field(
                    page_index,
                    current_label,
                    current_items,
                    line_lookup,
                    field_index,
                    model_name,
                )
                fields.append(field)
                for span_item in current_items:
                    item_to_field[span_item["item_id"]] = field["field_id"]
                field_index += 1
                current_items = []

            current_label = label
            current_items.append(entry)

        if current_items:
            field = _line_span_to_field(
                page_index,
                current_label,
                current_items,
                line_lookup,
                field_index,
                model_name,
            )
            fields.append(field)
            for span_item in current_items:
                item_to_field[span_item["item_id"]] = field["field_id"]
            field_index += 1

    field_lookup = {field["field_id"]: field for field in fields}

    for page_payload in prepared_payload.get("pages", []):
        predicted_links = page_payload.get("predicted_links", [])
        for item in page_payload.get("items", []):
            for target_id in item.get("predicted_link_ids", []):
                predicted_links.append([item["id"], target_id])

        for source_item_id, target_item_id in predicted_links:
            source_field_id = item_to_field.get(source_item_id)
            target_field_id = item_to_field.get(target_item_id)
            if not source_field_id or not target_field_id or source_field_id == target_field_id:
                continue
            source_field = field_lookup[source_field_id]
            target_field = field_lookup[target_field_id]
            relation_type, direction = _infer_relation_type(source_field["label"], target_field["label"])
            if not relation_type:
                continue
            if direction == "reverse":
                source_field_id, target_field_id = target_field_id, source_field_id
            relation = {
                "relation_id": f"r{relation_index}",
                "type": relation_type,
                "from_field_id": source_field_id,
                "to_field_id": target_field_id,
                "confidence": None,
                "source": model_name,
                "review_status": "predicted",
            }
            relation_key = (relation_type, source_field_id, target_field_id)
            if relation_key not in seen_relation_keys:
                relations.append(relation)
                seen_relation_keys.add(relation_key)
                relation_index += 1

    return {
        "schema": ONTOLOGY_ID,
        "field_instances": fields,
        "relations": relations,
    }


def write_prepared_payload(payload: dict, output_path: str) -> None:
    write_json(output_path, payload)


def load_prepared_payload(path: str) -> dict:
    payload = read_json(path)
    if not payload:
        raise FileNotFoundError(f"Prepared inference payload not found: {path}")
    return payload


def _next_predicted_field_id(annotation: dict) -> str:
    existing_ids = {
        field.get("field_id")
        for field in annotation.get("field_instances", [])
        if field.get("field_id")
    }
    index = 1
    while f"f{index}" in existing_ids:
        index += 1
    return f"f{index}"


def _page_line_maps(doc: dict) -> dict[int, tuple[list[dict], dict[str, int], dict[str, dict]]]:
    lookup = {}
    for page in doc.get("pages", []):
        lines = sorted(page.get("lines", []), key=lambda item: item.get("order", 0))
        line_index = {(line.get("line_id") or line.get("id")): index for index, line in enumerate(lines)}
        line_by_id = {(line.get("line_id") or line.get("id")): line for line in lines}
        lookup[page.get("page_index")] = (lines, line_index, line_by_id)
    return lookup


def _is_noise_text_for_subject(text: str) -> bool:
    normalized = " ".join((text or "").split()).strip()
    if not normalized:
        return True
    return normalized in {"*", "**", "***", "-", "--", "---", "/"}


def _line_word_ids(line: dict) -> list[str]:
    words = list(line.get("words") or [])
    if words:
        return [
            word.get("word_id") or word.get("id")
            for word in words
            if word.get("word_id") or word.get("id")
        ]
    return list(line.get("word_ids") or [])


def apply_output_field_derivations(doc: dict, annotation: dict, *, model_name: str) -> dict:
    """Add output-only fields and restore semantic overlaps needed by consumers."""
    page_maps = _page_line_maps(doc)
    fields = annotation.setdefault("field_instances", [])

    subject_fields = [field for field in fields if field.get("label") == "DOC_SUBJECT"]
    for subject_field in subject_fields:
        page_index = subject_field.get("page_index")
        page_info = page_maps.get(page_index)
        if not page_info:
            continue
        lines, line_index, line_by_id = page_info
        subject_line_ids = list(subject_field.get("line_ids") or [])
        if not subject_line_ids:
            continue
        first_subject_line_id = subject_line_ids[0]
        first_subject_index = line_index.get(first_subject_line_id)
        if first_subject_index is None:
            continue

        prepend_line = None
        probe_index = first_subject_index - 1
        while probe_index >= 0 and _is_noise_text_for_subject(lines[probe_index].get("text", "")):
            probe_index -= 1
        if probe_index >= 0:
            candidate_line = lines[probe_index]
            candidate_line_id = candidate_line.get("line_id") or candidate_line.get("id")
            if candidate_line_id not in subject_line_ids and extract_explicit_doc_type_from_line(candidate_line):
                prepend_line = candidate_line

        if not prepend_line:
            continue

        prepend_line_id = prepend_line.get("line_id") or prepend_line.get("id")
        prepend_line = line_by_id.get(prepend_line_id)
        if not prepend_line:
            continue

        subject_field["line_ids"] = [prepend_line_id] + subject_line_ids
        combined_word_ids = _line_word_ids(prepend_line)
        for word_id in subject_field.get("word_ids") or []:
            if word_id not in combined_word_ids:
                combined_word_ids.append(word_id)
        subject_field["word_ids"] = combined_word_ids
        original_text = (subject_field.get("text") or "").strip()
        prepend_text = (prepend_line.get("text") or "").strip()
        subject_field["text"] = "\n".join(part for part in [prepend_text, original_text] if part)
        subject_field["normalized_value"] = normalize_value("DOC_SUBJECT", subject_field["text"])

    return annotation


def apply_semantic_field_inference(doc: dict, annotation: dict, *, model_name: str) -> dict:
    """Infer deterministic document-level fields that do not require model training.

    Current scope:
    - DOC_TYPE from an explicit uppercase title line near the top of page 1.
    - DOC_TYPE = "CÔNG VĂN" when `CV/*` or a `V/v` / `Về việc` subject appears
      directly below the number/symbol line.
    """
    existing_field_ids = {
        field.get("field_id")
        for field in annotation.get("field_instances", [])
        if field.get("field_id")
    }
    inferred_doc_type = infer_semantic_doc_type_field(
        doc,
        annotation,
        model_name=model_name,
        existing_field_ids=existing_field_ids,
    )
    if inferred_doc_type:
        annotation.setdefault("field_instances", []).append(inferred_doc_type)
        annotation.setdefault("semantic_inference", {})["doc_type"] = collect_doc_type_semantic_evidence(doc, annotation)
    return annotation


# ---------------------------------------------------------------------------
# Rule-based marks (URGENCY_MARK, SECRECY_MARK, CIRCULATION_MARK)
# ---------------------------------------------------------------------------

import re as _re
import unicodedata as _unicodedata

_SECRECY_KEYWORDS = ["TUYỆT MẬT", "TỐI MẬT", "MẬT"]
_URGENCY_KEYWORDS = ["HỎA TỐC", "THƯỢNG KHẨN", "KHẨN"]
_CIRCULATION_KEYWORDS = [
    "LƯU HÀNH NỘI BỘ",
    "XEM XONG TRẢ LẠI",
    "TÀI LIỆU THU HỒI",
    "XONG HỘI NGHỊ TRẢ LẠI",
    "KHÔNG PHỔ BIẾN TRÊN CÁC PHƯƠNG TIỆN THÔNG TIN ĐẠI CHÚNG",
]

_MARK_CONFIGS = [
    ("SECRECY_MARK", _SECRECY_KEYWORDS),
    ("URGENCY_MARK", _URGENCY_KEYWORDS),
    ("CIRCULATION_MARK", _CIRCULATION_KEYWORDS),
]


def _strip_accents_upper(text: str) -> str:
    nfd = _unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in nfd if _unicodedata.category(c) != "Mn").upper()


def _tokenize_for_stamp_match(text: str) -> list[str]:
    """Normalize + split into alnum tokens for stamp-keyword comparison.

    Substring matching on the normalised text is unsafe because Vietnamese
    diacritic stripping collapses distinct vowels: "MẬT" (secret) and
    "MẶT" (face) both become "MAT", so "MAT TRAN TO QUOC" (Fatherland Front
    organisation header) falsely matched the keyword "MẬT". Token-level
    equality on whole lines avoids this entire class of collision.
    """
    return _re.findall(r"\w+", _strip_accents_upper(text))


def _line_matches_stamp_keyword(line_text: str, keyword: str) -> bool:
    """Return True iff ``line_text`` equals ``keyword`` at the token level.

    Real stamps ("TUYỆT MẬT", "HỎA TỐC", …) are standalone phrases — one
    line of the OCR output, optionally wrapped in decoration like
    parentheses or dashes. Token equality captures that exactly while
    rejecting running text that happens to contain a homograph.
    """
    return _tokenize_for_stamp_match(line_text) == _tokenize_for_stamp_match(keyword)


def _is_stamp_like_line(text: str) -> bool:
    """Heuristic: short line, mostly uppercase → likely a stamp, not body text."""
    if not text or len(text) > 60:
        return False
    upper_count = sum(1 for c in text if c.isupper())
    alpha_count = sum(1 for c in text if c.isalpha())
    if alpha_count == 0:
        return False
    return upper_count / alpha_count > 0.6


def _line_center(line: dict, page_width: float, page_height: float) -> tuple[float, float]:
    bbox = line.get("bbox") or [0, 0, 0, 0]
    cx = ((bbox[0] + bbox[2]) / 2) / page_width if page_width else 0.5
    cy = ((bbox[1] + bbox[3]) / 2) / page_height if page_height else 0.5
    return cx, cy


def detect_secrecy_mark(canonical_doc: dict) -> str | None:
    """Detection-only variant of the SECRECY_MARK rule from
    :func:`apply_rule_based_marks`. Returns the matched keyword
    (``"TUYỆT MẬT"`` / ``"TỐI MẬT"`` / ``"MẬT"``) or ``None``.

    Pure function — never mutates the input. Intended for UI layers (e.g.
    the KIE viewer) that need to know whether a document is classified
    without re-running the full auto-label pipeline.

    The rule is intentionally identical to the one in apply_rule_based_marks
    so a positive here matches exactly what the pipeline would add to
    ``field_instances``. Keep the two in sync.
    """
    if not isinstance(canonical_doc, dict):
        return None
    pages = canonical_doc.get("pages") or []
    page0 = next((p for p in pages if p.get("page_index") == 0), None)
    if not page0:
        return None
    page_width = page0.get("width") or 595.28
    page_height = page0.get("height") or 841.89
    for line in page0.get("lines") or []:
        text = line.get("text", "")
        if not _is_stamp_like_line(text):
            continue
        cx, cy = _line_center(line, page_width, page_height)
        if cx > 0.5 or cy > 0.33:
            continue
        # Token-level equality (see _line_matches_stamp_keyword) — longest
        # keyword first because "TUYỆT MẬT" also contains "MẬT" as tokens
        # and we want the more specific classification.
        for kw in _SECRECY_KEYWORDS:
            if _line_matches_stamp_keyword(text, kw):
                return kw
    return None


def apply_rule_based_marks(doc: dict, annotation: dict) -> dict:
    """Scan page 0 for URGENCY_MARK, SECRECY_MARK, CIRCULATION_MARK via ROI rule-based.

    Appends matched field instances to *annotation* (mutates in place).
    Skips if the label+line already exists in annotation (model already predicted).
    """
    pages = doc.get("pages", [])
    page0 = next((p for p in pages if p.get("page_index") == 0), None)
    if not page0:
        return annotation

    page_width = page0.get("width") or 595.28
    page_height = page0.get("height") or 841.89
    lines = page0.get("lines") or []

    existing = {
        (f.get("label"), tuple(f.get("line_ids") or []))
        for f in annotation.get("field_instances", [])
    }
    existing_labels = {f.get("label") for f in annotation.get("field_instances", [])}

    rb_index = 1
    for label, keywords in _MARK_CONFIGS:
        if label in existing_labels:
            continue  # model already predicted this label

        # Determine ROI
        use_roi = label in ("SECRECY_MARK", "URGENCY_MARK")

        for line in lines:
            text = line.get("text", "")
            if not _is_stamp_like_line(text):
                continue

            if use_roi:
                cx, cy = _line_center(line, page_width, page_height)
                if cx > 0.5 or cy > 0.33:
                    continue  # outside ROI for 10a/10b

            matched_kw = None
            for kw in keywords:  # longest-first (list is pre-sorted)
                if _line_matches_stamp_keyword(text, kw):
                    matched_kw = kw
                    break
            if not matched_kw:
                continue

            line_id = line.get("id", "")
            key = (label, (line_id,))
            if key in existing:
                continue

            field = {
                "field_id": f"rb_{rb_index}",
                "label": label,
                "page_index": 0,
                "line_ids": [line_id] if line_id else [],
                "word_ids": [],
                "text": text.strip(),
                "normalized_value": normalize_value(label, text),
                "confidence": 0.9,
                "source": "rule_based",
                "review_status": "predicted",
            }
            annotation.setdefault("field_instances", []).append(field)
            existing.add(key)
            existing_labels.add(label)
            rb_index += 1
            break  # one match per label

    return annotation


def inject_annotation_into_canonical(canonical_json_path: str, annotation: dict, output_path: str | None = None) -> dict:
    doc = load_canonical_json(canonical_json_path)
    doc["annotations"] = {
        "schema": annotation.get("schema", ONTOLOGY_ID),
        "status": "predicted",
        "source": annotation.get("field_instances", [{}])[0].get("source") if annotation.get("field_instances") else None,
        "field_instances": annotation.get("field_instances", []),
        "relations": annotation.get("relations", []),
    }
    if output_path:
        write_json(output_path, doc)
    return doc


# ────────────────────────────────────────────────────────────────────
# High-level GUI entry points
# ────────────────────────────────────────────────────────────────────

_kie_warmup_state: dict = {"loaded": False, "ort_session": None, "tokenizer": None,
                            "label_list": None, "model_dir": None}


def _resolve_kie_model_dir() -> str | None:
    """Resolve the LiLT model directory used for inference. Prefers the
    PhoBERT-based fine-tune; falls back to XLM-R if PhoBERT is missing."""
    try:
        from portable_utils import get_base_dir
        base = get_base_dir()
    except Exception:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(base, "models", "lilt_phobert_run2"),
        os.path.join(base, "models", "lilt_xlmr_run2"),
    ]
    for path in candidates:
        if os.path.isdir(path) and os.path.exists(os.path.join(path, "model.onnx")):
            return path
    return None


def warmup_kie_models(log_cb=None) -> bool:
    """Pre-load LiLT ONNX session + tokenizer + label list into module cache.
    Returns True if loaded; False if model files unavailable.
    Subsequent calls are no-ops."""
    log_cb = log_cb or (lambda m: None)
    if _kie_warmup_state["loaded"]:
        return True

    model_dir = _resolve_kie_model_dir()
    if not model_dir:
        log_cb("KIE model dir not found — fallback to rule-based metadata extractor")
        return False

    try:
        import onnxruntime as ort
        log_cb(f"Loading LiLT ONNX from {os.path.basename(model_dir)}...")
        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(
            os.path.join(model_dir, "model.onnx"),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )

        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(model_dir)

        label_list_path = os.path.join(model_dir, "label_list.json")
        with open(label_list_path, "r", encoding="utf-8") as f:
            label_list = json.load(f)

        _kie_warmup_state.update({
            "loaded": True,
            "ort_session": session,
            "tokenizer": tokenizer,
            "label_list": label_list,
            "model_dir": model_dir,
        })
        log_cb(f"KIE warmup OK ({len(label_list)} labels)")
        return True
    except Exception as e:
        log_cb(f"KIE warmup failed: {e}")
        return False


def is_kie_ready() -> bool:
    return bool(_kie_warmup_state.get("loaded"))


def kie_state() -> dict:
    return dict(_kie_warmup_state)
