from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader
from transformers import AutoModelForTokenClassification, AutoTokenizer, DataCollatorForTokenClassification

from train_layoutlmv3.common import compute_kie_metrics, ensure_project_dirs, load_dataset_split, write_json, write_layout_report
from train_layoutlmv3.hf_utils import (
    TokenizedPageDataset,
    aggregate_logits_to_words,
    label_maps_from_model,
    predict_onnx,
    predict_pytorch,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export LayoutLMv3 KIE model to ONNX and benchmark CPU latency.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--limit-docs", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--onnx-threads", type=int, default=0, help="ORT intra-op thread count. 0 keeps ONNX Runtime default.")
    parser.add_argument("--onnx-inter-op-threads", type=int, default=0, help="ORT inter-op thread count. 0 keeps ONNX Runtime default.")
    parser.add_argument("--onnx-graph-optimization-level", choices=["disable", "basic", "extended", "all"], default="all")
    parser.add_argument("--skip-optimized-fp32", action="store_true", help="Do not save and benchmark an ORT optimized FP32 graph.")
    parser.add_argument("--subword-label-strategy", choices=["same", "first"], default="same")
    return parser.parse_args()


class TokenClassifierOnnxWrapper(nn.Module):
    def __init__(self, model: nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, input_ids, attention_mask, bbox):
        return self.model(input_ids=input_ids, attention_mask=attention_mask, bbox=bbox).logits


def export_onnx(model_path: Path, onnx_path: Path, rows: list[dict[str, Any]], max_length: int, opset: int) -> None:
    tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=True)
    model = AutoModelForTokenClassification.from_pretrained(model_path)
    model.eval()
    wrapper = TokenClassifierOnnxWrapper(model)
    wrapper.eval()
    row = rows[0]
    encoded = tokenizer(
        row["tokens"],
        boxes=row["bboxes"],
        truncation=True,
        padding="max_length",
        max_length=max_length,
        return_tensors="pt",
    )
    inputs = (encoded["input_ids"], encoded["attention_mask"], encoded["bbox"])
    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        wrapper,
        inputs,
        str(onnx_path),
        input_names=["input_ids", "attention_mask", "bbox"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "sequence"},
            "attention_mask": {0: "batch", 1: "sequence"},
            "bbox": {0: "batch", 1: "sequence"},
            "logits": {0: "batch", 1: "sequence"},
        },
        opset_version=opset,
        do_constant_folding=True,
    )


def quantize_int8(onnx_path: Path, int8_path: Path) -> str | None:
    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic

        quantize_dynamic(str(onnx_path), str(int8_path), weight_type=QuantType.QInt8)
        return None
    except Exception as exc:
        return repr(exc)


def run_onnx(
    onnx_path: Path,
    rows: list[dict[str, Any]],
    model_path: Path,
    max_length: int,
    stride: int,
    batch_size: int,
    warmup: int,
    subword_label_strategy: str,
    intra_op_num_threads: int = 0,
    inter_op_num_threads: int = 0,
    graph_optimization_level: str = "all",
    optimized_model_path: Path | None = None,
) -> tuple[list[list[str]], list[list[float]], dict[str, Any]]:
    return predict_onnx(
        rows,
        onnx_path,
        model_path,
        max_length=max_length,
        stride=stride,
        batch_size=batch_size,
        warmup=warmup,
        subword_label_strategy=subword_label_strategy,
        intra_op_num_threads=intra_op_num_threads,
        inter_op_num_threads=inter_op_num_threads,
        graph_optimization_level=graph_optimization_level,
        optimized_model_path=optimized_model_path,
    )


def metric_summary(rows: list[dict[str, Any]], labels: list[list[str]], scores: list[list[float]]) -> dict[str, float]:
    metrics = compute_kie_metrics(rows, labels, scores)
    return {
        "word_f1": metrics["word"]["overall"]["f1"],
        "span_f1": metrics["span"]["overall"]["f1"],
        "exact_instance_accuracy": metrics["exact_instance_accuracy"],
    }


def main() -> None:
    args = parse_args()
    dirs = ensure_project_dirs(args.project_root)
    model_path = Path(args.model_path)
    rows = load_dataset_split(args.project_root, args.split, limit_docs=args.limit_docs)
    if not rows:
        raise SystemExit(f"No rows found for split {args.split}. Run 1-build_dataset.py first.")

    onnx_path = dirs["onnx"] / "layoutlmv3_base_run1.fp32.onnx"
    int8_path = dirs["onnx"] / "layoutlmv3_base_run1.int8.onnx"
    optimized_fp32_path = dirs["onnx"] / "layoutlmv3_base_run1.fp32.ort_optimized.onnx"
    report: dict[str, Any] = {
        "project_root": str(Path(args.project_root).resolve()),
        "model_path": str(model_path.resolve()),
        "split": args.split,
        "rows": len(rows),
        "docs": len({row["doc_id"] for row in rows}),
        "onnx_fp32": str(onnx_path),
        "onnx_fp32_ort_optimized": str(optimized_fp32_path),
        "onnx_int8": str(int8_path),
        "latency": {},
        "accuracy": {},
        "accuracy_delta": {},
        "errors": {},
    }

    if not onnx_path.exists():
        export_onnx(model_path, onnx_path, rows, args.max_length, args.opset)

    pytorch_labels, pytorch_scores, pytorch_latency = predict_pytorch(
        rows,
        model_path,
        max_length=args.max_length,
        stride=args.stride,
        batch_size=args.batch_size,
        subword_label_strategy=args.subword_label_strategy,
        device="cpu",
    )
    report["latency"]["pytorch"] = pytorch_latency
    report["accuracy"]["pytorch"] = metric_summary(rows, pytorch_labels, pytorch_scores)

    try:
        onnx_labels, onnx_scores, onnx_latency = run_onnx(
            onnx_path,
            rows,
            model_path,
            args.max_length,
            args.stride,
            args.batch_size,
            args.warmup,
            args.subword_label_strategy,
            args.onnx_threads,
            args.onnx_inter_op_threads,
            args.onnx_graph_optimization_level,
            None if args.skip_optimized_fp32 else optimized_fp32_path,
        )
        report["latency"]["onnx_fp32"] = onnx_latency
        report["accuracy"]["onnx_fp32"] = metric_summary(rows, onnx_labels, onnx_scores)
        report["accuracy_delta"]["onnx_fp32_vs_pytorch"] = {
            key: report["accuracy"]["onnx_fp32"][key] - report["accuracy"]["pytorch"][key]
            for key in report["accuracy"]["pytorch"]
        }
    except Exception as exc:
        report["errors"]["onnx_fp32"] = repr(exc)

    if optimized_fp32_path.exists() and not args.skip_optimized_fp32:
        try:
            optimized_labels, optimized_scores, optimized_latency = run_onnx(
                optimized_fp32_path,
                rows,
                model_path,
                args.max_length,
                args.stride,
                args.batch_size,
                args.warmup,
                args.subword_label_strategy,
                args.onnx_threads,
                args.onnx_inter_op_threads,
                args.onnx_graph_optimization_level,
            )
            report["latency"]["onnx_fp32_ort_optimized"] = optimized_latency
            report["accuracy"]["onnx_fp32_ort_optimized"] = metric_summary(rows, optimized_labels, optimized_scores)
            report["accuracy_delta"]["onnx_fp32_ort_optimized_vs_pytorch"] = {
                key: report["accuracy"]["onnx_fp32_ort_optimized"][key] - report["accuracy"]["pytorch"][key]
                for key in report["accuracy"]["pytorch"]
            }
        except Exception as exc:
            report["errors"]["onnx_fp32_ort_optimized"] = repr(exc)

    quant_error = quantize_int8(onnx_path, int8_path)
    if quant_error:
        report["errors"]["onnx_int8_quantization"] = quant_error
    elif int8_path.exists():
        try:
            int8_labels, int8_scores, int8_latency = run_onnx(
                int8_path,
                rows,
                model_path,
                args.max_length,
                args.stride,
                args.batch_size,
                args.warmup,
                args.subword_label_strategy,
                args.onnx_threads,
                args.onnx_inter_op_threads,
                args.onnx_graph_optimization_level,
            )
            report["latency"]["onnx_int8"] = int8_latency
            report["accuracy"]["onnx_int8"] = metric_summary(rows, int8_labels, int8_scores)
            report["accuracy_delta"]["onnx_int8_vs_pytorch"] = {
                key: report["accuracy"]["onnx_int8"][key] - report["accuracy"]["pytorch"][key]
                for key in report["accuracy"]["pytorch"]
            }
        except Exception as exc:
            report["errors"]["onnx_int8"] = repr(exc)

    write_json(dirs["reports"] / "onnx_export_report.json", report)
    write_layout_report(args.project_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
