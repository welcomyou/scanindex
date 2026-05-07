from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_layoutlmv3.common import apply_cardinality, bbox_union, decode_bio_spans, write_json
from train_layoutlmv3.hf_utils import predict_onnx, predict_pytorch, rows_from_canonical


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run LayoutLMv3 KIE inference from canonical OCR JSON.")
    parser.add_argument("--input", required=True, help="Canonical OCR JSON, or a PDF with a nearby canonical JSON companion.")
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--backend", choices=["pytorch", "onnx"], default="pytorch")
    parser.add_argument("--onnx-path", help="ONNX model path when --backend onnx is used.")
    parser.add_argument("--output", help="Optional output JSON path. Defaults to stdout.")
    parser.add_argument("--selected-pages", nargs="*", type=int, help="Optional page indices to run.")
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--onnx-threads", type=int, default=0, help="ORT intra-op thread count. 0 keeps ONNX Runtime default.")
    parser.add_argument("--onnx-inter-op-threads", type=int, default=0, help="ORT inter-op thread count. 0 keeps ONNX Runtime default.")
    parser.add_argument("--onnx-graph-optimization-level", choices=["disable", "basic", "extended", "all"], default="all")
    parser.add_argument("--onnx-optimized-model-path", help="Optional path where ORT saves its optimized graph.")
    parser.add_argument("--subword-label-strategy", choices=["same", "first"], default="same")
    return parser.parse_args()


def resolve_canonical(input_path: str | Path) -> Path:
    path = Path(input_path)
    if path.suffix.lower() == ".json":
        return path
    candidates = [
        Path(str(path) + ".json"),
        path.with_name(path.stem + "_ocr.pdf.json"),
        path.with_name(path.stem + ".pdf.json"),
        path.with_name(path.stem + "_ocr_corrected.pdf.json"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    matches = sorted(path.parent.glob(f"{path.stem}*ocr*.json"))
    if matches:
        return matches[0]
    raise FileNotFoundError(f"Could not find canonical JSON companion for {path}")


def fields_from_spans(rows: list[dict[str, Any]], spans: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    row_lookup = {(row["doc_id"], row["page_index"]): row for row in rows}
    fields: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for span in spans:
        row = row_lookup.get((span.get("doc_id"), span.get("page_index")))
        if not row:
            continue
        index_by_word = {wid: idx for idx, wid in enumerate(row["word_ids"])}
        raw_boxes = []
        for word_id in span.get("word_ids", []):
            idx = index_by_word.get(word_id)
            if idx is not None:
                raw_boxes.append(row["raw_bboxes"][idx])
        fields[span["field"]].append(
            {
                "text": span.get("text", ""),
                "word_ids": span.get("word_ids", []),
                "bbox": bbox_union([tuple(box) for box in raw_boxes]),
                "page_index": span.get("page_index"),
                "confidence": span.get("confidence"),
            }
        )
    return dict(fields)


def main() -> None:
    args = parse_args()
    canonical_json = resolve_canonical(args.input)
    rows = rows_from_canonical(canonical_json, selected_pages=args.selected_pages)
    if not rows:
        raise SystemExit(f"No OCR words found in {canonical_json}")
    if args.backend == "onnx":
        if not args.onnx_path:
            raise SystemExit("--onnx-path is required when --backend onnx is used.")
        pred_labels, pred_scores, latency = predict_onnx(
            rows,
            args.onnx_path,
            args.model_path,
            max_length=args.max_length,
            stride=args.stride,
            batch_size=args.batch_size,
            warmup=args.warmup,
            subword_label_strategy=args.subword_label_strategy,
            intra_op_num_threads=args.onnx_threads,
            inter_op_num_threads=args.onnx_inter_op_threads,
            graph_optimization_level=args.onnx_graph_optimization_level,
            optimized_model_path=args.onnx_optimized_model_path,
        )
    else:
        pred_labels, pred_scores, latency = predict_pytorch(
            rows,
            args.model_path,
            max_length=args.max_length,
            stride=args.stride,
            batch_size=args.batch_size,
            subword_label_strategy=args.subword_label_strategy,
        )
    raw_spans: list[dict[str, Any]] = []
    for row, labels, scores in zip(rows, pred_labels, pred_scores):
        raw_spans.extend(decode_bio_spans(row, labels, scores))
    schema_spans, fragmentation = apply_cardinality(raw_spans)
    payload = {
        "source_file": str(canonical_json),
        "model_path": str(Path(args.model_path).resolve()),
        "pages": len(rows),
        "latency": latency,
        "fragmentation": fragmentation,
        "fields": fields_from_spans(rows, schema_spans),
    }
    if args.output:
        write_json(args.output, payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
