from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import defaultdict
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_layoutlmv3_style.common import (
    compute_kie_metrics,
    ensure_project_dirs,
    load_dataset_split,
    write_json,
)
from train_layoutlmv3_style.hf_utils import decode_schema_spans, predict_onnx


FIELDS = [
    "REGIME_HEADER",
    "ISSUE_ORG_SUPERIOR",
    "ISSUE_ORG_NAME",
    "DOC_NUMBER_SYMBOL",
    "PLACE_DATE",
    "DOC_SUBJECT",
    "ADDRESSEE",
    "RECIPIENTS",
    "SIGNER_ROLE",
    "SIGNER_NAME",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark LayoutLMv3 ONNX KIE over selected labeled batches.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model-path", required=True, help="HF/tokenizer model directory.")
    parser.add_argument("--onnx-path", required=True)
    parser.add_argument("--batches", nargs="+", default=[*(f"batch_{i:04d}" for i in range(1, 17)), "batch_0027"])
    parser.add_argument("--output-dir", help="Default: reports/benchmark_onnx_batches")
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--onnx-threads", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    parser.add_argument("--onnx-inter-op-threads", type=int, default=1)
    parser.add_argument("--onnx-graph-optimization-level", choices=["disable", "basic", "extended", "all"], default="extended")
    parser.add_argument("--subword-label-strategy", choices=["same", "first"], default="same")
    return parser.parse_args()


def normalized_batch(raw: str) -> str:
    raw = raw.strip().replace("\\", "/").strip("/")
    if raw.startswith("batch_"):
        return raw
    return f"batch_{int(raw):04d}"


def load_rows_by_batch(project_root: str | Path, batches: list[str]) -> dict[str, list[dict[str, Any]]]:
    wanted = set(batches)
    rows_by_batch: dict[str, list[dict[str, Any]]] = {batch: [] for batch in batches}
    for split in ("train", "val", "test"):
        for row in load_dataset_split(project_root, split):
            rel = str(row.get("label_rel") or "").replace("\\", "/")
            batch = rel.split("/", 1)[0]
            if batch in wanted:
                row = dict(row)
                row["_source_split"] = split
                rows_by_batch[batch].append(row)
    return rows_by_batch


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
    rows: list[dict[str, Any]],
    pred_labels: list[list[str]],
    pred_scores: list[list[float]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    schema_spans, fragmentation = decode_schema_spans(rows, pred_labels, pred_scores)
    schema_labels = labels_from_schema_spans(rows, schema_spans)
    metrics = compute_kie_metrics(rows, schema_labels, pred_scores)
    return metrics, fragmentation


def per_field_summary(metrics: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    per_field = metrics["span"]["per_field"]
    error_counts = defaultdict(int)
    for err in metrics.get("errors", []):
        if err.get("field"):
            error_counts[str(err["field"])] += 1
    for field in FIELDS:
        item = dict(per_field.get(field, {}))
        gold = int(item.get("gold", 0))
        tp = int(item.get("tp", 0))
        fp = int(item.get("fp", 0))
        fn = int(item.get("fn", 0))
        item["exact"] = tp / gold if gold else 0.0
        item["errors"] = int(error_counts.get(field, fp + fn))
        out[field] = item
    return out


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
        "per_field": per_field_summary(metrics),
    }


def latency_total(latencies: list[dict[str, Any]]) -> dict[str, Any]:
    pages = sum(int(item.get("pages", 0)) for item in latencies)
    chunks = sum(int(item.get("chunks", 0)) for item in latencies)
    seconds = sum(float(item.get("seconds", 0.0)) for item in latencies)
    return {
        "chunks": chunks,
        "pages": pages,
        "seconds": seconds,
        "ms_per_page": seconds * 1000.0 / pages if pages else 0.0,
        "ms_per_chunk": seconds * 1000.0 / chunks if chunks else 0.0,
    }


def write_csv(output_dir: Path, batch_rows: list[dict[str, Any]], overall: dict[str, Any]) -> None:
    summary_path = output_dir / "batch_summary.csv"
    with summary_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "batch",
                "docs",
                "pages",
                "chunks",
                "seconds",
                "ms_per_page",
                "word_f1",
                "span_f1",
                "exact",
                "errors",
                "missing_word_rate",
                "extra_word_rate",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in batch_rows:
            writer.writerow(row)
        writer.writerow(overall)

    field_path = output_dir / "per_field_summary.csv"
    with field_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "scope",
                "field",
                "precision",
                "recall",
                "f1",
                "exact",
                "tp",
                "fp",
                "fn",
                "gold",
                "pred",
                "errors",
            ],
        )
        writer.writeheader()
        for row in batch_rows:
            for field, item in row["per_field"].items():
                writer.writerow({"scope": row["batch"], "field": field, **item})
        for field, item in overall["per_field"].items():
            writer.writerow({"scope": "ALL", "field": field, **item})


def main() -> None:
    args = parse_args()
    dirs = ensure_project_dirs(args.project_root)
    output_dir = Path(args.output_dir) if args.output_dir else dirs["reports"] / "benchmark_onnx_batches"
    output_dir.mkdir(parents=True, exist_ok=True)
    batches = [normalized_batch(batch) for batch in args.batches]
    rows_by_batch = load_rows_by_batch(args.project_root, batches)

    report: dict[str, Any] = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "project_root": str(Path(args.project_root).resolve()),
        "model_path": str(Path(args.model_path).resolve()),
        "onnx_path": str(Path(args.onnx_path).resolve()),
        "backend": "onnx",
        "onnx_settings": {
            "batch_size": args.batch_size,
            "threads": args.onnx_threads,
            "inter_op_threads": args.onnx_inter_op_threads,
            "graph_optimization_level": args.onnx_graph_optimization_level,
            "max_length": args.max_length,
            "stride": args.stride,
        },
        "batches": {},
    }
    all_rows: list[dict[str, Any]] = []
    all_pred_labels: list[list[str]] = []
    all_pred_scores: list[list[float]] = []
    all_latencies: list[dict[str, Any]] = []
    csv_rows: list[dict[str, Any]] = []

    for batch in batches:
        rows = rows_by_batch.get(batch, [])
        if not rows:
            report["batches"][batch] = {"error": "no_rows"}
            print(f"{batch}: no rows", flush=True)
            continue
        started = time.perf_counter()
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
        )
        metrics, fragmentation = evaluate_predictions(rows, pred_labels, pred_scores)
        item = compact_metrics(metrics, fragmentation, rows)
        item["latency"] = latency
        item["wall_seconds"] = time.perf_counter() - started
        report["batches"][batch] = item
        write_json(output_dir / f"{batch}.json", item)

        all_rows.extend(rows)
        all_pred_labels.extend(pred_labels)
        all_pred_scores.extend(pred_scores)
        all_latencies.append(latency)

        csv_row = {
            "batch": batch,
            "docs": item["docs"],
            "pages": item["pages"],
            "chunks": latency.get("chunks", 0),
            "seconds": latency.get("seconds", 0.0),
            "ms_per_page": latency.get("ms_per_page", 0.0),
            "word_f1": item["word_f1"],
            "span_f1": item["span_f1"],
            "exact": item["exact_instance_accuracy"],
            "errors": item["errors"],
            "missing_word_rate": item["missing_word_rate"],
            "extra_word_rate": item["extra_word_rate"],
            "per_field": item["per_field"],
        }
        csv_rows.append(csv_row)
        print(
            f"{batch}: docs={item['docs']} pages={item['pages']} "
            f"exact={item['exact_instance_accuracy']:.6f} span_f1={item['span_f1']:.6f} "
            f"errors={item['errors']} ms/page={latency.get('ms_per_page', 0.0):.1f}",
            flush=True,
        )

    if all_rows:
        overall_metrics, overall_fragmentation = evaluate_predictions(all_rows, all_pred_labels, all_pred_scores)
        overall = compact_metrics(overall_metrics, overall_fragmentation, all_rows)
        overall["batch"] = "ALL"
        overall["latency"] = latency_total(all_latencies)
        report["overall"] = overall
        csv_overall = {
            "batch": "ALL",
            "docs": overall["docs"],
            "pages": overall["pages"],
            "chunks": overall["latency"]["chunks"],
            "seconds": overall["latency"]["seconds"],
            "ms_per_page": overall["latency"]["ms_per_page"],
            "word_f1": overall["word_f1"],
            "span_f1": overall["span_f1"],
            "exact": overall["exact_instance_accuracy"],
            "errors": overall["errors"],
            "missing_word_rate": overall["missing_word_rate"],
            "extra_word_rate": overall["extra_word_rate"],
            "per_field": overall["per_field"],
        }
        write_csv(output_dir, csv_rows, csv_overall)

    output_path = output_dir / "benchmark_batches_0001_0016_0027_onnx.json"
    write_json(output_path, report)
    print(f"wrote {output_path}", flush=True)


if __name__ == "__main__":
    main()
