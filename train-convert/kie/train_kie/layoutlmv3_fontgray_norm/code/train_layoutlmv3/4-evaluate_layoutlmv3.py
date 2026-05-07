from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_layoutlmv3.common import (
    apply_cardinality,
    compute_kie_metrics,
    decode_bio_spans,
    ensure_project_dirs,
    load_dataset_split,
    write_json,
    write_jsonl,
    write_layout_report,
)
from train_layoutlmv3.hf_utils import predict_pytorch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate a fine-tuned LayoutLMv3 KIE model at word/span level.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--limit-docs", type=int)
    parser.add_argument("--subword-label-strategy", choices=["same", "first"], default="same")
    return parser.parse_args()


def labels_from_spans(rows: list[dict[str, Any]], spans: list[dict[str, Any]]) -> list[list[str]]:
    labels = [["O"] * len(row["tokens"]) for row in rows]
    row_lookup = {(row["doc_id"], row["page_index"]): idx for idx, row in enumerate(rows)}
    spans_by_row: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for span in spans:
        row_idx = row_lookup.get((span.get("doc_id"), span.get("page_index")))
        if row_idx is not None:
            spans_by_row[row_idx].append(span)
    for row_idx, row_spans in spans_by_row.items():
        row = rows[row_idx]
        word_index = {wid: idx for idx, wid in enumerate(row["word_ids"])}
        for span in row_spans:
            indices = [word_index[wid] for wid in span.get("word_ids", []) if wid in word_index]
            indices.sort()
            for offset, index in enumerate(indices):
                labels[row_idx][index] = f"{'B' if offset == 0 else 'I'}-{span['field']}"
    return labels


def main() -> None:
    args = parse_args()
    dirs = ensure_project_dirs(args.project_root)
    rows = load_dataset_split(args.project_root, args.split, limit_docs=args.limit_docs)
    if not rows:
        raise SystemExit(f"No rows found for split {args.split}. Run 1-build_dataset.py first.")

    pred_labels_raw, pred_scores, latency = predict_pytorch(
        rows,
        args.model_path,
        max_length=args.max_length,
        stride=args.stride,
        batch_size=args.batch_size,
        subword_label_strategy=args.subword_label_strategy,
    )
    raw_metrics = compute_kie_metrics(rows, pred_labels_raw, pred_scores)

    raw_spans: list[dict[str, Any]] = []
    for row, row_labels, row_scores in zip(rows, pred_labels_raw, pred_scores):
        raw_spans.extend(decode_bio_spans(row, row_labels, row_scores))
    schema_spans, fragmentation = apply_cardinality(raw_spans)
    pred_labels_schema = labels_from_spans(rows, schema_spans)
    schema_metrics = compute_kie_metrics(rows, pred_labels_schema, pred_scores)

    report = {
        "project_root": str(Path(args.project_root).resolve()),
        "model_path": str(Path(args.model_path).resolve()),
        "split": args.split,
        "rows": len(rows),
        "docs": len({row["doc_id"] for row in rows}),
        "latency": latency,
        "fragmentation": fragmentation,
        "metrics": {k: v for k, v in schema_metrics.items() if k != "errors"},
        "raw_metrics": {k: v for k, v in raw_metrics.items() if k != "errors"},
        "error_report": str((dirs["reports"] / f"errors_{args.split}.jsonl").resolve()),
        "notes": [
            "metrics are word-level/schema-decoded unless raw_metrics is explicitly referenced",
            "schema decoder merges same-page single-field fragments and chooses the best cross-page fragment",
        ],
    }
    write_json(dirs["reports"] / f"evaluation_{args.split}.json", report)
    write_jsonl(dirs["reports"] / f"errors_{args.split}.jsonl", schema_metrics.get("errors", []))
    write_layout_report(args.project_root)

    word = schema_metrics["word"]["overall"]
    span = schema_metrics["span"]["overall"]
    print(
        json.dumps(
            {
                "split": args.split,
                "word_f1": word["f1"],
                "span_f1": span["f1"],
                "exact_instance_accuracy": schema_metrics["exact_instance_accuracy"],
                "missing_word_rate": schema_metrics["missing_word_rate"],
                "extra_word_rate": schema_metrics["extra_word_rate"],
                "fragmentation": fragmentation,
                "latency": latency,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
