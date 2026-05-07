import argparse
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from kie_json_utils import merge_bboxes, upgrade_ocr_data_in_place


def load_canonical_json(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return upgrade_ocr_data_in_place(data)


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


def _choose_token_label(span_words, label_map):
    labels = [label_map.get(word["id"], "O") for word in span_words]
    non_o = [label for label in labels if label != "O"]
    return non_o[0] if non_o else "O"


def _words_by_id(page):
    return {word["id"]: word for word in page.get("words", [])}


def _lines_by_id(page):
    return {line["id"]: line for line in page.get("lines", [])}


def _sorted_page_words(page):
    lines = {line["id"]: line.get("order", 0) for line in page.get("lines", [])}
    return sorted(
        page.get("words", []),
        key=lambda w: (
            lines.get(w.get("line_id"), 10**9),
            w.get("order", 10**9),
        ),
    )


def _collect_field_word_ids(doc):
    page_words = {}
    for page in doc.get("pages", []):
        page_words[page["page_index"]] = _words_by_id(page)
    field_map = {}

    for field in doc.get("annotations", {}).get("field_instances", []):
        label = (field.get("label") or "").strip().upper()
        if not label:
            continue
        field_id = field.get("field_id") or f"field_{len(field_map)}"
        word_ids = list(field.get("word_ids") or [])

        if not word_ids and field.get("line_ids"):
            line_ids = set(field.get("line_ids") or [])
            for page in doc.get("pages", []):
                lines = _lines_by_id(page)
                for line_id in line_ids:
                    line = lines.get(line_id)
                    if line:
                        word_ids.extend(line.get("word_ids") or [])

        field_map[field_id] = {
            "label": label,
            "word_ids": word_ids,
        }

    return field_map


def build_word_label_maps(doc):
    field_map = _collect_field_word_ids(doc)
    word_order = {}
    for page in doc.get("pages", []):
        for idx, word in enumerate(_sorted_page_words(page)):
            word_order[word["id"]] = (page["page_index"], idx)

    label_map = {}
    for field in field_map.values():
        ordered = sorted(
            [wid for wid in field["word_ids"] if wid in word_order],
            key=lambda wid: word_order[wid],
        )
        for idx, word_id in enumerate(ordered):
            prefix = "B-" if idx == 0 else "I-"
            label_map[word_id] = f"{prefix}{field['label']}"

    return label_map


def export_lilt_xlmr(input_paths, output_path):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    records = []
    for input_path in input_paths:
        doc = load_canonical_json(input_path)
        label_map = build_word_label_maps(doc)
        doc_id = Path(input_path).stem
        for page in doc.get("pages", []):
            page_words = _sorted_page_words(page)
            if not page_words:
                continue
            records.append({
                "id": f"{doc_id}_{page['id']}",
                "doc_path": os.path.abspath(input_path),
                "page_index": page["page_index"],
                "tokens": [word["text"] for word in page_words],
                "bboxes": [
                    normalize_bbox(word["bbox"], page["width"], page["height"])
                    for word in page_words
                ],
                "ner_tags": [label_map.get(word["id"], "O") for word in page_words],
                "word_ids": [word["id"] for word in page_words],
            })

    with open(output_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _segment_line_text(text, segmenter_mode):
    text = (text or "").strip()
    if not text:
        return []
    if segmenter_mode == "underthesea":
        from underthesea import word_tokenize
        return word_tokenize(text, format="text").split()
    return text.split()


def _align_segmented_line(line_words, segmenter_mode):
    base = [(word["text"], [word]) for word in line_words]
    if segmenter_mode == "whitespace" or not line_words:
        return base

    line_text = " ".join(word["text"] for word in line_words)
    segmented = _segment_line_text(line_text, segmenter_mode)
    if not segmented:
        return base

    aligned = []
    cursor = 0
    for token in segmented:
        target = token.replace("_", " ").strip()
        if not target:
            continue
        start = cursor
        parts = []
        while cursor < len(line_words):
            parts.append(line_words[cursor])
            joined = " ".join(word["text"] for word in parts)
            if joined == target:
                aligned.append((token, parts))
                cursor += 1
                break
            cursor += 1
        else:
            return base

        if start == cursor and parts:
            cursor += 1

    if cursor != len(line_words):
        leftovers = line_words[cursor:]
        aligned.extend((word["text"], [word]) for word in leftovers)

    return aligned or base


def export_lilt_phobert(input_paths, output_path, segmenter_mode="whitespace"):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    records = []
    for input_path in input_paths:
        doc = load_canonical_json(input_path)
        label_map = build_word_label_maps(doc)
        doc_id = Path(input_path).stem

        for page in doc.get("pages", []):
            words_by_id = _words_by_id(page)
            tokens = []
            bboxes = []
            ner_tags = []
            token_word_ids = []

            for line in sorted(page.get("lines", []), key=lambda line: line.get("order", 0)):
                line_words = [words_by_id[word_id] for word_id in line.get("word_ids", []) if word_id in words_by_id]
                for token, span_words in _align_segmented_line(line_words, segmenter_mode):
                    tokens.append(token)
                    bboxes.append(normalize_bbox(
                        merge_bboxes([word["bbox"] for word in span_words]),
                        page["width"],
                        page["height"],
                    ))
                    ner_tags.append(_choose_token_label(span_words, label_map))
                    token_word_ids.append([word["id"] for word in span_words])

            if tokens:
                records.append({
                    "id": f"{doc_id}_{page['id']}",
                    "doc_path": os.path.abspath(input_path),
                    "page_index": page["page_index"],
                    "tokens": tokens,
                    "bboxes": bboxes,
                    "ner_tags": ner_tags,
                    "token_word_ids": token_word_ids,
                })

    with open(output_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _field_label_for_line(line, word_label_map):
    labels = [
        word_label_map.get(word_id, "O")
        for word_id in line.get("word_ids", [])
        if word_label_map.get(word_id, "O") != "O"
    ]
    if not labels:
        return "other"
    distinct = {label.split("-", 1)[1].lower() for label in labels}
    if len(distinct) > 1:
        print(
            f"Warning: mixed labels on line {line.get('id')}: {sorted(distinct)}; "
            f"using {labels[0]}",
            file=sys.stderr,
        )
    return labels[0].split("-", 1)[1].lower()


def _relation_links_for_page(doc, page):
    line_to_numeric = {}
    for idx, line in enumerate(sorted(page.get("lines", []), key=lambda line: line.get("order", 0)), start=1):
        line_to_numeric[line["id"]] = idx

    field_map = _collect_field_word_ids(doc)
    field_lines = {}
    page_line_ids = set(line_to_numeric.keys())
    for field_id, field in field_map.items():
        collected = []
        for field_instance in doc.get("annotations", {}).get("field_instances", []):
            if field_instance.get("field_id") != field_id:
                continue
            for line_id in field_instance.get("line_ids") or []:
                if line_id in page_line_ids:
                    collected.append(line_id)
        field_lines[field_id] = collected

    links = set()
    for relation in doc.get("annotations", {}).get("relations", []):
        src = relation.get("from_field_id")
        dst = relation.get("to_field_id")
        for src_line in field_lines.get(src, []):
            for dst_line in field_lines.get(dst, []):
                if src_line in line_to_numeric and dst_line in line_to_numeric:
                    links.add((line_to_numeric[src_line], line_to_numeric[dst_line]))

    return links


def _render_page_image(pdf_path, page_index, output_path, dpi):
    import fitz

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
    train_txt = output_dir / "train.txt"
    class_list_path = output_dir / "class_list.txt"

    labels_seen = {"other"}
    rows = []

    for input_path in input_paths:
        doc = load_canonical_json(input_path)
        word_label_map = build_word_label_maps(doc)
        pdf_path = (
            doc.get("pipeline", {}).get("correction", {}).get("output_pdf")
            or doc.get("input_path")
            or doc.get("document", {}).get("source_path")
        )
        doc_stem = Path(input_path).stem

        for page in doc.get("pages", []):
            image_name = f"{doc_stem}_{page['id']}.png"
            image_path = images_dir / image_name
            if pdf_path and os.path.exists(pdf_path):
                _render_page_image(pdf_path, page["page_index"], str(image_path), render_dpi)

            links = _relation_links_for_page(doc, page)
            annotation = []
            ordered_lines = sorted(page.get("lines", []), key=lambda line: line.get("order", 0))
            scale = render_dpi / 72.0
            for idx, line in enumerate(ordered_lines, start=1):
                bbox = line["bbox"]
                label = _field_label_for_line(line, word_label_map)
                labels_seen.add(label)
                x0 = round(bbox[0] * scale)
                y0 = round(bbox[1] * scale)
                x1 = round(bbox[2] * scale)
                y1 = round(bbox[3] * scale)
                item = {
                    "transcription": line["text"],
                    "label": label,
                    "points": [
                        [x0, y0],
                        [x1, y0],
                        [x1, y1],
                        [x0, y1],
                    ],
                    "id": idx,
                    "linking": [],
                }
                for src, dst in sorted(links):
                    if src == idx or dst == idx:
                        item["linking"].append([src, dst])
                annotation.append(item)

            rows.append(f"{image_path.as_posix()}\t{json.dumps(annotation, ensure_ascii=False)}")

    with open(train_txt, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(row + "\n")

    with open(class_list_path, "w", encoding="utf-8") as f:
        for label in sorted(labels_seen, key=lambda value: (value != "other", value)):
            f.write(label.upper() + "\n")


def collect_input_paths(path):
    path = Path(path)
    if path.is_file():
        return [str(path)]
    return [str(p) for p in sorted(path.rglob("*.json"))]


def main():
    parser = argparse.ArgumentParser(description="Convert canonical OCR/KIE JSON into training formats.")
    parser.add_argument("--input", required=True, help="Canonical JSON file or directory.")
    parser.add_argument("--format", required=True, choices=["lilt_xlmr", "lilt_phobert", "paddle_kie"])
    parser.add_argument("--output", required=True, help="Output file for LiLT, or output directory for Paddle KIE.")
    parser.add_argument("--segmenter", default="whitespace", choices=["whitespace", "underthesea"],
                        help="PhoBERT word segmentation mode.")
    parser.add_argument("--render-dpi", type=int, default=200, help="Render DPI for Paddle page images.")
    args = parser.parse_args()

    input_paths = collect_input_paths(args.input)
    if args.format == "lilt_xlmr":
        export_lilt_xlmr(input_paths, args.output)
    elif args.format == "lilt_phobert":
        export_lilt_phobert(input_paths, args.output, segmenter_mode=args.segmenter)
    else:
        export_paddle_kie(input_paths, args.output, render_dpi=args.render_dpi)


if __name__ == "__main__":
    main()
