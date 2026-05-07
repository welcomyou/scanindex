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
)
from train_layoutlmv3.hf_utils import predict_onnx, predict_pytorch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate LayoutLMv3 KIE on a labeled batch prefix.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--label-rel-prefix", required=True, help="Example: batch_0027/")
    parser.add_argument("--backend", choices=["pytorch", "onnx"], default="onnx")
    parser.add_argument("--onnx", action="append", help="For ONNX backend: variant in name=path format. Repeatable.")
    parser.add_argument("--splits", nargs="+", default=["all", "train", "val", "test"], choices=["all", "train", "val", "test"])
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--onnx-threads", type=int, default=0)
    parser.add_argument("--onnx-inter-op-threads", type=int, default=0)
    parser.add_argument("--onnx-graph-optimization-level", choices=["disable", "basic", "extended", "all"], default="all")
    parser.add_argument("--subword-label-strategy", choices=["same", "first"], default="same")
    parser.add_argument("--output", help="Defaults to reports/evaluation_<batch>_<backend>.json")
    return parser.parse_args()


def normalized_prefix(prefix: str) -> str:
    prefix = prefix.replace("\\", "/")
    return prefix if prefix.endswith("/") else prefix + "/"


def parse_variant(raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("--onnx must use name=path format")
    name, path = raw.split("=", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError("ONNX variant name cannot be empty")
    return name, Path(path)


def load_batch_rows(project_root: str | Path, split: str, prefix: str) -> list[dict[str, Any]]:
    source_splits = ["train", "val", "test"] if split == "all" else [split]
    rows: list[dict[str, Any]] = []
    for source_split in source_splits:
        for row in load_dataset_split(project_root, source_split):
            label_rel = str(row.get("label_rel") or "").replace("\\", "/")
            if label_rel.startswith(prefix):
                rows.append(row)
    return rows


def load_batch_rows_by_source_split(project_root: str | Path, prefix: str) -> dict[str, list[dict[str, Any]]]:
    return {split: load_batch_rows(project_root, split, prefix) for split in ("train", "val", "test")}


def labels_from_schema_spans(rows: list[dict[str, Any]], spans: list[dict[str, Any]]) -> list[list[str]]:
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
            indices = sorted(word_index[wid] for wid in span.get("word_ids", []) if wid in word_index)
            for offset, index in enumerate(indices):
                labels[row_idx][index] = f"{'B' if offset == 0 else 'I'}-{span['field']}"
    return labels


def evaluate_predictions(
    rows: list[dict[str, Any]], pred_labels: list[list[str]], pred_scores: list[list[float]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_spans: list[dict[str, Any]] = []
    for row, row_labels, row_scores in zip(rows, pred_labels, pred_scores):
        raw_spans.extend(decode_bio_spans(row, row_labels, row_scores))
    schema_spans, fragmentation = apply_cardinality(raw_spans)
    schema_labels = labels_from_schema_spans(rows, schema_spans)
    metrics = compute_kie_metrics(rows, schema_labels, pred_scores)
    return metrics, fragmentation


def compact_metrics(metrics: dict[str, Any], fragmentation: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    word = metrics["word"]["overall"]
    span = metrics["span"]["overall"]
    return {
        "docs": len({row["doc_id"] for row in rows}),
        "pages": len(rows),
        "word_f1": word["f1"],
        "span_f1": span["f1"],
        "precision": span["precision"],
        "recall": span["recall"],
        "exact_instance_accuracy": metrics["exact_instance_accuracy"],
        "missing_word_rate": metrics["missing_word_rate"],
        "extra_word_rate": metrics["extra_word_rate"],
        "bbox_iou_mean": metrics["bbox_iou_mean"],
        "gold_instances": metrics["gold_instances"],
        "pred_instances": metrics["pred_instances"],
        "errors": len(metrics.get("errors", [])),
        "fragmented_fields": fragmentation.get("fragmented_fields", 0),
        "fragmentation_count": fragmentation.get("fragmentation_count", {}),
        "per_field_span": metrics["span"]["per_field"],
    }


def main() -> None:
    args = parse_args()
    dirs = ensure_project_dirs(args.project_root)
    prefix = normalized_prefix(args.label_rel_prefix)
    if args.backend == "onnx" and not args.onnx:
        raise SystemExit("--onnx is required for --backend onnx")

    rows_by_source_split = load_batch_rows_by_source_split(args.project_root, prefix)
    rows_by_split = {
        "train": rows_by_source_split["train"],
        "val": rows_by_source_split["val"],
        "test": rows_by_source_split["test"],
        "all": rows_by_source_split["train"] + rows_by_source_split["val"] + rows_by_source_split["test"],
    }
    rows_by_split = {split: rows_by_split[split] for split in args.splits}
    empty = [split for split, rows in rows_by_split.items() if not rows]
    if empty:
        raise SystemExit(f"No rows found for splits: {', '.join(empty)} with prefix {prefix}")
    prediction_rows = rows_by_source_split["train"] + rows_by_source_split["val"] + rows_by_source_split["test"]

    report: dict[str, Any] = {
        "project_root": str(Path(args.project_root).resolve()),
        "model_path": str(Path(args.model_path).resolve()),
        "label_rel_prefix": prefix,
        "backend": args.backend,
        "splits": {},
        "variants": {},
        "errors": {},
    }

    if args.backend == "pytorch":
        variants = [("pytorch", None)]
    else:
        variants = [parse_variant(raw) for raw in args.onnx or []]

    for variant_name, variant_path in variants:
        report["variants"][variant_name] = {"path": str(variant_path.resolve()) if variant_path else str(Path(args.model_path).resolve())}
        report["splits"][variant_name] = {}
        try:
            if args.backend == "pytorch":
                all_pred_labels, all_pred_scores, latency = predict_pytorch(
                    prediction_rows,
                    args.model_path,
                    max_length=args.max_length,
                    stride=args.stride,
                    batch_size=args.batch_size,
                    subword_label_strategy=args.subword_label_strategy,
                )
            else:
                all_pred_labels, all_pred_scores, latency = predict_onnx(
                    prediction_rows,
                    variant_path,
                    args.model_path,
                    max_length=args.max_length,
                    stride=args.stride,
                    batch_size=args.batch_size,
                    warmup=args.warmup,
                    subword_label_strategy=args.subword_label_strategy,
                    intra_op_num_threads=args.onnx_threads,
                    inter_op_num_threads=args.onnx_inter_op_threads,
                    graph_optimization_level=args.onnx_graph_optimization_level,
                )
        except Exception as exc:
            report["errors"][variant_name] = repr(exc)
            continue

        row_key_to_prediction = {
            (row["doc_id"], row["page_index"]): (all_pred_labels[idx], all_pred_scores[idx])
            for idx, row in enumerate(prediction_rows)
        }
        for split, rows in rows_by_split.items():
            try:
                pred_labels = []
                pred_scores = []
                for row in rows:
                    labels, scores = row_key_to_prediction[(row["doc_id"], row["page_index"])]
                    pred_labels.append(labels)
                    pred_scores.append(scores)
                metrics, fragmentation = evaluate_predictions(rows, pred_labels, pred_scores)
                report["splits"][variant_name][split] = compact_metrics(metrics, fragmentation, rows)
                split_latency = dict(latency)
                split_latency["pages"] = len(rows)
                split_latency["seconds"] = latency.get("ms_per_page", 0.0) * len(rows) / 1000.0
                report["splits"][variant_name][split]["latency"] = split_latency
                if split == "all":
                    report["splits"][variant_name][split]["latency"] = latency
                write_jsonl(
                    dirs["reports"] / f"errors_{prefix.strip('/').replace('/', '_')}_{variant_name}_{split}.jsonl",
                    metrics.get("errors", []),
                )
            except Exception as exc:
                report["errors"][f"{variant_name}:{split}"] = repr(exc)

    output = (
        Path(args.output)
        if args.output
        else dirs["reports"] / f"evaluation_{prefix.strip('/').replace('/', '_')}_{args.backend}.json"
    )
    write_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
