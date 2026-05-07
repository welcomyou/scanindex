from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import fitz

from scanindex.core.kie.json_utils import merge_bboxes, upgrade_ocr_data_in_place
from scanindex.core.kie.common import read_json, write_json, write_jsonl
from scanindex.core.kie.ontology import (
    BLOCK_LINE_LABELS,
    MULTI_LINE_LABELS,
    TRAINING_EXCLUDED_LABELS,
    paddle_export_label_for_fields,
)


SURFACE_TOKEN_RE = re.compile(
    r"[A-Za-zÀ-ỹĐđ0-9]+(?:/[A-Za-zÀ-ỹĐđ0-9]+)*|[-–—]|[^\w\s]",
    re.UNICODE,
)


def _strip_accents(text: str) -> str:
    text = (text or "").replace("đ", "d").replace("Đ", "D")
    return "".join(
        char for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    )


def _normalized_match_text(text: str) -> str:
    return " ".join(_strip_accents(text).lower().split())


def load_canonical_json(path: str | os.PathLike[str]) -> dict:
    data = read_json(path)
    if not data:
        raise FileNotFoundError(f"Canonical JSON not found: {path}")
    return upgrade_ocr_data_in_place(data)


def _choose_existing_pdf_path(doc: dict) -> str | None:
    candidates = [
        doc.get("pipeline", {}).get("correction", {}).get("output_pdf"),
        doc.get("input_path"),
        doc.get("document", {}).get("source_path"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return next((candidate for candidate in candidates if candidate), None)


def normalize_bbox(bbox, width, height, scale=1000):
    x0, y0, x1, y1 = bbox
    if width <= 0 or height <= 0:
        return [0, 0, 0, 0]
    return [
        max(0, min(scale, int(round(x0 / width * scale)))),
        max(0, min(scale, int(round(y0 / height * scale)))),
        max(0, min(scale, int(round(x1 / width * scale)))),
        max(0, min(scale, int(round(y1 / height * scale)))),
    ]


PROJECTION_STATUSES = [
    "exact_text_match",
    "word_id_match",
    "block_line_match",
    "line_fallback",
    "metadata_only",
    "unresolved",
]


def _empty_projection_bucket() -> dict:
    bucket = {"total": 0}
    for status in PROJECTION_STATUSES:
        bucket[status] = 0
    return bucket


def _init_projection_report(track: str) -> dict:
    return {
        "track": track,
        "documents": 0,
        "pages": 0,
        "summary": _empty_projection_bucket(),
        "by_label": {},
        "problem_fields": [],
        "_doc_keys": set(),
    }


def _record_projection_page(
    report: dict,
    *,
    doc_id: str,
    input_path: str,
    source_pdf_path: str | None,
    page_index: int,
    field_resolutions: list[dict],
) -> None:
    report["pages"] += 1
    report["_doc_keys"].add(input_path)

    for field in field_resolutions:
        status = field["resolution"]
        label = field["label"]
        report["summary"]["total"] += 1
        report["summary"][status] += 1

        label_bucket = report["by_label"].setdefault(label, _empty_projection_bucket())
        label_bucket["total"] += 1
        label_bucket[status] += 1

        if status not in {"line_fallback", "unresolved"}:
            continue

        report["problem_fields"].append({
            "doc_id": doc_id,
            "input_path": os.path.abspath(input_path),
            "source_pdf_path": source_pdf_path,
            "page_index": page_index,
            "field_id": field.get("field_id"),
            "label": label,
            "text": field.get("text"),
            "resolution": status,
            "line_ids": list(field.get("line_ids") or []),
            "word_ids": list(field.get("word_ids") or []),
            "matched_token_count": field.get("matched_token_count", 0),
            "match_reason": field.get("match_reason"),
        })


def _finalize_projection_report(report: dict) -> dict:
    report["documents"] = len(report.pop("_doc_keys"))
    total_fields = report["summary"]["total"] or 0
    report["summary"]["line_fallback_rate"] = (
        report["summary"]["line_fallback"] / total_fields if total_fields else 0.0
    )
    report["summary"]["unresolved_rate"] = (
        report["summary"]["unresolved"] / total_fields if total_fields else 0.0
    )

    for label, bucket in report["by_label"].items():
        total = bucket["total"] or 0
        bucket["line_fallback_rate"] = bucket["line_fallback"] / total if total else 0.0
        bucket["unresolved_rate"] = bucket["unresolved"] / total if total else 0.0
    return report


def _page_lookup(doc):
    return {page["page_index"]: page for page in doc.get("pages", [])}


def _match_tokens_by_text(line_tokens, line_text, field_text):
    needle = (field_text or "").strip()
    if not needle:
        return []
    start = line_text.lower().find(needle.lower())
    if start < 0:
        return []
    end = start + len(needle)
    return [
        token
        for token in line_tokens
        if token["text"].strip() and not (token["char_end"] <= start or token["char_start"] >= end)
    ]


def _prepare_page_surface_tokens(page):
    lines = sorted(page.get("lines", []), key=lambda item: item.get("order", 0))
    line_tokens = {}
    line_texts = {}
    for line in lines:
        line_tokens.setdefault(line["id"], [])
        line_texts[line["id"]] = line.get("text", "")

    page_tokens = sorted(page.get("kie_tokens", []), key=lambda item: item.get("order", 0))
    for token in page_tokens:
        line_id = token["line_id"]
        line_tokens.setdefault(line_id, []).append(token)
    return line_tokens, line_texts


def _page_fields(page, doc):
    return [
        field
        for field in doc.get("annotations", {}).get("field_instances", [])
        if field.get("page_index") == page.get("page_index")
        and field.get("label") not in TRAINING_EXCLUDED_LABELS
    ]


def _page_line_labels(page, doc):
    line_labels = defaultdict(set)
    field_ids_by_line = defaultdict(set)
    for field in _page_fields(page, doc):  # already filtered by TRAINING_EXCLUDED_LABELS
        label = field.get("label")
        if not label:
            continue
        for line_id in field.get("line_ids") or []:
            line_labels[line_id].add(label)
            field_ids_by_line[line_id].add(field.get("field_id"))
    return line_labels, field_ids_by_line


def _label_page_surface_tokens(page, doc):
    line_tokens, line_texts = _prepare_page_surface_tokens(page)
    page_fields = _page_fields(page, doc)

    token_label_map = {}
    field_token_map = {}
    touched_line_ids = set()
    unresolved = []
    field_resolutions = []

    fields_sorted = sorted(
        page_fields,
        key=lambda field: (
            0 if field.get("word_ids") else 1,
            len(field.get("text") or ""),
        ),
    )

    for field in fields_sorted:
        touched_line_ids.update(field.get("line_ids") or [])
        matched = []
        resolution = "unresolved"
        match_reason = None
        label = field.get("label", "")

        if not matched and len(field.get("line_ids") or []) == 1 and field.get("text"):
            line_id = field["line_ids"][0]
            matched = _match_tokens_by_text(
                line_tokens.get(line_id, []),
                line_texts.get(line_id, ""),
                field.get("text", ""),
            )
            if matched:
                resolution = "exact_text_match"
                match_reason = "matched OCR surface text within a single line"

        if not matched and field.get("word_ids"):
            word_ids = set(field.get("word_ids") or [])
            for tokens in line_tokens.values():
                for token in tokens:
                    if word_ids.intersection(token.get("source_word_ids", [])):
                        matched.append(token)
            if matched:
                resolution = "word_id_match"
                match_reason = "matched source OCR word ids"

        if not matched and field.get("line_ids"):
            if label in BLOCK_LINE_LABELS:
                for line_id in field["line_ids"]:
                    matched.extend(line_tokens.get(line_id, []))
                if matched:
                    resolution = "block_line_match"
                    match_reason = "matched all KIE tokens on labeled block lines"
            elif label in MULTI_LINE_LABELS and not field.get("word_ids"):
                print(
                    f"Warning: skipping line_fallback for multi-line field '{label}' "
                    f"(field_id={field.get('field_id')}) — word_ids is empty. "
                    "Add word_ids to annotation to get training signal.",
                    file=sys.stderr,
                )
            else:
                for line_id in field["line_ids"]:
                    matched.extend(line_tokens.get(line_id, []))
                if matched:
                    resolution = "line_fallback"
                    match_reason = "fell back to all KIE tokens on the labeled line ids"

        matched = [token for token in matched if token.get("text", "").strip()]
        if not matched:
            field_resolution = {
                "field_id": field.get("field_id"),
                "label": field.get("label"),
                "text": field.get("text"),
                "line_ids": list(field.get("line_ids") or []),
                "word_ids": list(field.get("word_ids") or []),
                "resolution": "unresolved",
                "matched_token_count": 0,
                "match_reason": "no KIE token match from text, word_ids, or line_ids",
            }
            unresolved.append(field_resolution)
            field_resolutions.append(field_resolution)
            continue

        field_token_map[field["field_id"]] = [token["id"] for token in matched]
        field_resolutions.append({
            "field_id": field.get("field_id"),
            "label": field.get("label"),
            "text": field.get("text"),
            "line_ids": list(field.get("line_ids") or []),
            "word_ids": list(field.get("word_ids") or []),
            "resolution": resolution,
            "matched_token_count": len(matched),
            "match_reason": match_reason,
        })
        for index, token in enumerate(matched):
            label = f"{'B' if index == 0 else 'I'}-{field['label']}"
            previous = token_label_map.get(token["id"])
            if previous and previous != label:
                print(
                    f"Warning: token {token['id']} already labeled as {previous}, keeping first label.",
                    file=sys.stderr,
                )
                continue
            token_label_map[token["id"]] = label

    ordered_tokens = []
    for line in sorted(page.get("lines", []), key=lambda item: item.get("order", 0)):
        ordered_tokens.extend(sorted(line_tokens.get(line["id"], []), key=lambda token: token["char_start"]))

    return ordered_tokens, token_label_map, field_token_map, touched_line_ids, unresolved, field_resolutions


def _repair_bio_tags(tags):
    repaired = []
    previous = "O"
    for tag in tags:
        if tag.startswith("I-"):
            label = tag[2:]
            if previous not in {f"B-{label}", f"I-{label}"}:
                tag = f"B-{label}"
        repaired.append(tag)
        previous = tag
    return repaired


def _tag_to_entity_label(tag):
    if not tag or tag == "O":
        return "OTHER"
    return tag.split("-", 1)[1]


def export_lilt_xlmr(input_paths, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    label_set = {"O"}
    projection_report = _init_projection_report("lilt_xlmr")

    for input_path in input_paths:
        doc = load_canonical_json(input_path)
        doc_id = Path(input_path).stem
        source_pdf_path = _choose_existing_pdf_path(doc)
        for page in doc.get("pages", []):
            tokens, token_label_map, _, _, _, field_resolutions = _label_page_surface_tokens(page, doc)
            _record_projection_page(
                projection_report,
                doc_id=doc_id,
                input_path=input_path,
                source_pdf_path=source_pdf_path,
                page_index=page["page_index"],
                field_resolutions=field_resolutions,
            )
            tokens = [token for token in tokens if token.get("text", "").strip()]
            if not tokens:
                continue
            ner_tags = _repair_bio_tags([token_label_map.get(token["id"], "O") for token in tokens])
            label_set.update(ner_tags)
            records.append({
                "id": f"{doc_id}_{page['id']}",
                "doc_path": os.path.abspath(input_path),
                "page_index": page["page_index"],
                "tokens": [token["text"] for token in tokens],
                "line_ids": [token["line_id"] for token in tokens],
                "bboxes": [
                    normalize_bbox(token["bbox"], page["width"], page["height"])
                    for token in tokens
                ],
                "ner_tags": ner_tags,
                "token_ids": [token["id"] for token in tokens],
                "source_word_ids": [token.get("source_word_ids", []) for token in tokens],
            })

    write_jsonl(output_path, records)
    with output_path.with_name(output_path.stem + "_labels.json").open("w", encoding="utf-8") as f:
        json.dump(sorted(label_set), f, ensure_ascii=False, indent=2)
    projection_report = _finalize_projection_report(projection_report)
    write_json(
        output_path.with_name(output_path.stem + "_projection_report.json"),
        projection_report,
    )
    print(
        f"[lilt_xlmr] projection unresolved={projection_report['summary']['unresolved']} "
        f"line_fallback={projection_report['summary']['line_fallback']}"
    )


def _segment_line_text(text, segmenter_mode):
    text = (text or "").strip()
    if not text:
        return []
    if segmenter_mode == "underthesea":
        try:
            from underthesea import word_tokenize
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "segmenter_mode=underthesea requires the underthesea package. "
                "Install the KIE training requirements first or rerun with --segmenter whitespace."
            ) from exc
        return word_tokenize(text, format="text").split()
    return text.split()


def _choose_token_label(surface_tokens, label_map):
    labels = [label_map.get(token["id"], "O") for token in surface_tokens]
    non_o = [label for label in labels if label != "O"]
    return non_o[0] if non_o else "O"


def _align_segmented_surface_tokens(line_tokens, line_text, segmenter_mode):
    base = [(token["text"], [token]) for token in line_tokens if token.get("text", "").strip()]
    if segmenter_mode == "whitespace" or not line_tokens:
        return base

    segmented = _segment_line_text(line_text, segmenter_mode)
    if not segmented:
        return base

    aligned = []
    start = 0
    for token_text in segmented:
        needle = token_text.replace("_", " ").strip()
        if not needle:
            continue
        found = line_text.lower().find(needle.lower(), start)
        if found < 0:
            return base
        end = found + len(needle)
        candidates = [
            token
            for token in line_tokens
            if token.get("text", "").strip() and not (token["char_end"] <= found or token["char_start"] >= end)
        ]
        if not candidates:
            return base
        aligned.append((token_text, candidates))
        start = end
    return aligned or base


def export_lilt_phobert(input_paths, output_path, segmenter_mode="underthesea"):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    label_set = {"O"}
    projection_report = _init_projection_report("lilt_phobert")

    for input_path in input_paths:
        doc = load_canonical_json(input_path)
        doc_id = Path(input_path).stem
        source_pdf_path = _choose_existing_pdf_path(doc)
        for page in doc.get("pages", []):
            ordered_tokens, token_label_map, _, _, _, field_resolutions = _label_page_surface_tokens(page, doc)
            _record_projection_page(
                projection_report,
                doc_id=doc_id,
                input_path=input_path,
                source_pdf_path=source_pdf_path,
                page_index=page["page_index"],
                field_resolutions=field_resolutions,
            )
            if not ordered_tokens:
                continue

            line_groups = defaultdict(list)
            line_texts = {}
            for token in ordered_tokens:
                line_groups[token["line_id"]].append(token)
            for line in page.get("lines", []):
                line_texts[line["id"]] = line.get("text", "")

            tokens = []
            bboxes = []
            ner_tags = []
            token_ids = []
            line_ids = []
            for line in sorted(page.get("lines", []), key=lambda item: item.get("order", 0)):
                line_tokens = line_groups.get(line["id"], [])
                aligned = _align_segmented_surface_tokens(
                    line_tokens,
                    line_texts.get(line["id"], ""),
                    segmenter_mode,
                )
                for token_text, surface_tokens in aligned:
                    token_text = token_text.strip()
                    if not token_text:
                        continue
                    label = _choose_token_label(surface_tokens, token_label_map)
                    tokens.append(token_text)
                    bboxes.append(normalize_bbox(
                        merge_bboxes([token["bbox"] for token in surface_tokens]),
                        page["width"],
                        page["height"],
                    ))
                    ner_tags.append(label)
                    token_ids.append([token["id"] for token in surface_tokens])
                    line_ids.append(line["id"])

            if tokens:
                ner_tags = _repair_bio_tags(ner_tags)
                label_set.update(ner_tags)
                records.append({
                    "id": f"{doc_id}_{page['id']}",
                    "doc_path": os.path.abspath(input_path),
                    "page_index": page["page_index"],
                    "tokens": tokens,
                    "line_ids": line_ids,
                    "bboxes": bboxes,
                    "ner_tags": ner_tags,
                    "token_ids": token_ids,
                })

    write_jsonl(output_path, records)
    with output_path.with_name(output_path.stem + "_labels.json").open("w", encoding="utf-8") as f:
        json.dump(sorted(label_set), f, ensure_ascii=False, indent=2)
    projection_report = _finalize_projection_report(projection_report)
    write_json(
        output_path.with_name(output_path.stem + "_projection_report.json"),
        projection_report,
    )
    print(
        f"[lilt_phobert] projection unresolved={projection_report['summary']['unresolved']} "
        f"line_fallback={projection_report['summary']['line_fallback']}"
    )


def _render_page_image(pdf_path, page_index, output_path, dpi):
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        pix = page.get_pixmap(dpi=dpi)
        pix.save(output_path)
    finally:
        doc.close()


def export_paddle_kie(input_paths, output_dir, render_dpi=200):
    output_dir = Path(output_dir)
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    label_file = output_dir / "labels.txt"
    class_list_path = output_dir / "class_list.txt"

    labels_seen = {"OTHER"}
    rows = []

    for input_path in input_paths:
        doc = load_canonical_json(input_path)
        doc_id = Path(input_path).stem
        pdf_path = _choose_existing_pdf_path(doc)

        for page in doc.get("pages", []):
            lines = sorted(page.get("lines", []), key=lambda item: item.get("order", 0))
            line_label_map, field_ids_by_line = _page_line_labels(page, doc)

            image_name = f"{doc_id}_{page['id']}.png"
            image_path = images_dir / image_name
            if pdf_path and os.path.exists(pdf_path):
                _render_page_image(pdf_path, page["page_index"], str(image_path), render_dpi)

            field_items = []
            field_id_to_item_ids = defaultdict(list)
            next_item_id = 1

            for line in lines:
                text = (line.get("text") or "").strip()
                bbox = line.get("bbox", [0.0, 0.0, 0.0, 0.0])
                if not text or bbox == [0.0, 0.0, 0.0, 0.0]:
                    continue

                x0 = round(bbox[0] * render_dpi / 72.0)
                y0 = round(bbox[1] * render_dpi / 72.0)
                x1 = round(bbox[2] * render_dpi / 72.0)
                y1 = round(bbox[3] * render_dpi / 72.0)
                label = paddle_export_label_for_fields(line_label_map.get(line["id"], set())).upper()
                labels_seen.add(label)
                item = {
                    "transcription": text,
                    "label": label,
                    "points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                    "id": next_item_id,
                    "linking": [],
                }
                field_items.append(item)

                if label != "OTHER":
                    for field_id in field_ids_by_line.get(line["id"], set()):
                        field_id_to_item_ids[field_id].append(next_item_id)
                next_item_id += 1

            for relation in doc.get("annotations", {}).get("relations", []):
                src_ids = field_id_to_item_ids.get(relation.get("from_field_id"), [])
                dst_ids = field_id_to_item_ids.get(relation.get("to_field_id"), [])
                if not src_ids or not dst_ids:
                    continue
                for item in field_items:
                    if item["id"] in src_ids:
                        for dst_id in dst_ids:
                            if item["id"] == dst_id:
                                continue
                            item["linking"].append([item["id"], dst_id])

            # PaddleOCR resolves each labels.txt path relative to dataset.data_dir.
            # Writing absolute local Windows paths breaks Linux training hosts.
            rows.append(f"{image_name}\t{json.dumps(field_items, ensure_ascii=False)}")

    with label_file.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(row + "\n")

    with class_list_path.open("w", encoding="utf-8") as f:
        for label in sorted(labels_seen, key=lambda item: (item != "OTHER", item)):
            f.write(label + "\n")
