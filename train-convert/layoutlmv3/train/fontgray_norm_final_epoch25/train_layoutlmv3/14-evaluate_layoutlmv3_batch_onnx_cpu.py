from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate and benchmark LayoutLMv3 ONNX INT8 on one labeled batch.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--onnx-path", required=True)
    parser.add_argument("--label-rel-prefix", default="batch_0027/")
    parser.add_argument("--output", required=True)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--inter-op-threads", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    parser.add_argument("--warmup", type=int, default=3)
    return parser.parse_args()


args = parse_args()
for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[key] = str(args.threads)
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402
from torch.utils.data import DataLoader  # noqa: E402
from transformers import AutoTokenizer, DataCollatorForTokenClassification  # noqa: E402

from train_layoutlmv3.common import (  # noqa: E402
    apply_cardinality,
    compute_kie_metrics,
    decode_bio_spans,
    load_dataset_split,
    write_json,
    write_jsonl,
)
from train_layoutlmv3.hf_utils import TokenizedPageDataset, aggregate_logits_to_words, label_maps_from_model  # noqa: E402


def normalized_prefix(prefix: str) -> str:
    prefix = prefix.replace("\\", "/")
    return prefix if prefix.endswith("/") else prefix + "/"


def load_batch_rows(project_root: str | Path, split: str, prefix: str) -> list[dict[str, Any]]:
    source_splits = ["train", "val", "test"] if split == "all" else [split]
    rows: list[dict[str, Any]] = []
    for source_split in source_splits:
        for row in load_dataset_split(project_root, source_split):
            label_rel = str(row.get("label_rel") or "").replace("\\", "/")
            if label_rel.startswith(prefix):
                rows.append(row)
    return rows


def load_batch_rows_by_split(project_root: str | Path, prefix: str) -> dict[str, list[dict[str, Any]]]:
    rows = {split: load_batch_rows(project_root, split, prefix) for split in ("train", "val", "test")}
    rows["all"] = rows["train"] + rows["val"] + rows["test"]
    return rows


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


def evaluate_predictions(rows: list[dict[str, Any]], pred_labels: list[list[str]], pred_scores: list[list[float]]) -> tuple[dict[str, Any], dict[str, Any]]:
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
        "errors": len(metrics.get("errors", [])),
        "missing_word_rate": metrics["missing_word_rate"],
        "extra_word_rate": metrics["extra_word_rate"],
        "bbox_iou_mean": metrics["bbox_iou_mean"],
        "gold_instances": metrics["gold_instances"],
        "pred_instances": metrics["pred_instances"],
        "fragmented_fields": fragmentation.get("fragmented_fields", 0),
        "fragmentation_count": fragmentation.get("fragmentation_count", {}),
        "per_field_span": metrics["span"]["per_field"],
    }


def main() -> None:
    prefix = normalized_prefix(args.label_rel_prefix)
    load_start = time.perf_counter()
    rows_by_split = load_batch_rows_by_split(args.project_root, prefix)
    rows = rows_by_split["all"]
    load_rows_seconds = time.perf_counter() - load_start
    if not rows:
        raise SystemExit(f"No rows found for {prefix}")

    label_list, label2id, id2label = label_maps_from_model(args.model_path)
    tokenizer = AutoTokenizer.from_pretrained(args.model_path, use_fast=True)
    tokenize_start = time.perf_counter()
    dataset = TokenizedPageDataset(rows, tokenizer, label2id, args.max_length, args.stride, "same")
    collator = DataCollatorForTokenClassification(
        tokenizer=tokenizer,
        padding="max_length",
        max_length=args.max_length,
        label_pad_token_id=-100,
    )
    loader = DataLoader(dataset, batch_size=args.batch_size, shuffle=False, collate_fn=collator)
    tokenize_seconds = time.perf_counter() - tokenize_start

    session_start = time.perf_counter()
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.intra_op_num_threads = args.threads
    options.inter_op_num_threads = args.inter_op_threads
    session = ort.InferenceSession(str(args.onnx_path), sess_options=options, providers=["CPUExecutionProvider"])
    input_names = {item.name for item in session.get_inputs()}
    session_load_seconds = time.perf_counter() - session_start

    batch_start = time.perf_counter()
    cached_batches = []
    for batch in loader:
        batch.pop("labels", None)
        candidate = {
            "input_ids": batch["input_ids"].cpu().numpy().astype(np.int64),
            "attention_mask": batch["attention_mask"].cpu().numpy().astype(np.int64),
            "bbox": batch["bbox"].cpu().numpy().astype(np.int64),
        }
        cached_batches.append({key: value for key, value in candidate.items() if key in input_names})
    batch_cache_seconds = time.perf_counter() - batch_start

    warmup_start = time.perf_counter()
    for batch in cached_batches[: max(0, args.warmup)]:
        session.run(None, batch)
    warmup_seconds = time.perf_counter() - warmup_start

    logits_parts = []
    model_start = time.perf_counter()
    for batch in cached_batches:
        logits_parts.append(session.run(None, batch)[0])
    model_seconds = time.perf_counter() - model_start

    aggregate_start = time.perf_counter()
    logits = np.concatenate(logits_parts, axis=0) if logits_parts else np.zeros((0, args.max_length, len(label_list)), dtype=np.float32)
    pred_labels, pred_scores = aggregate_logits_to_words(rows, dataset, logits, id2label)
    aggregate_seconds = time.perf_counter() - aggregate_start

    row_key_to_prediction = {
        (row["doc_id"], row["page_index"]): (pred_labels[idx], pred_scores[idx])
        for idx, row in enumerate(rows)
    }
    split_reports: dict[str, Any] = {}
    metrics_start = time.perf_counter()
    reports_dir = Path(args.project_root) / "reports"
    for split in ("all", "train", "val", "test"):
        split_rows = rows_by_split[split]
        split_labels = []
        split_scores = []
        for row in split_rows:
            labels, scores = row_key_to_prediction[(row["doc_id"], row["page_index"])]
            split_labels.append(labels)
            split_scores.append(scores)
        metrics, fragmentation = evaluate_predictions(split_rows, split_labels, split_scores)
        split_reports[split] = compact_metrics(metrics, fragmentation, split_rows)
        write_jsonl(reports_dir / f"errors_{prefix.strip('/').replace('/', '_')}_int8_cpu6_total_{split}.jsonl", metrics.get("errors", []))
    metrics_seconds = time.perf_counter() - metrics_start

    pages = len(rows)
    total_seconds = load_rows_seconds + tokenize_seconds + session_load_seconds + batch_cache_seconds + model_seconds + aggregate_seconds + metrics_seconds
    timing = {
        "rows_load_seconds": load_rows_seconds,
        "tokenize_dataset_seconds": tokenize_seconds,
        "session_load_seconds": session_load_seconds,
        "batch_cache_seconds": batch_cache_seconds,
        "warmup_seconds_excluded": warmup_seconds,
        "model_session_run_seconds": model_seconds,
        "aggregate_logits_to_words_seconds": aggregate_seconds,
        "evaluation_metrics_seconds": metrics_seconds,
        "total_seconds_excluding_warmup": total_seconds,
        "total_ms_per_page_excluding_warmup": total_seconds * 1000.0 / pages if pages else 0.0,
        "model_ms_per_page": model_seconds * 1000.0 / pages if pages else 0.0,
    }
    report = {
        "method": "layoutlmv3_onnx_int8",
        "project_root": str(Path(args.project_root).resolve()),
        "model_path": str(Path(args.model_path).resolve()),
        "onnx_path": str(Path(args.onnx_path).resolve()),
        "label_rel_prefix": prefix,
        "threads": args.threads,
        "inter_op_threads": args.inter_op_threads,
        "batch_size": args.batch_size,
        "max_length": args.max_length,
        "stride": args.stride,
        "docs": len({row["doc_id"] for row in rows}),
        "pages": pages,
        "chunks": len(dataset),
        "timing": timing,
        "splits": split_reports,
    }
    write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
