from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch
from torch import nn
from transformers import AutoModelForTokenClassification, AutoTokenizer

from train_layoutlmv3_style.common import compute_kie_metrics, ensure_project_dirs, load_dataset_split, write_json, write_style_report
from train_layoutlmv3_style.hf_utils import predict_onnx, predict_pytorch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export LayoutLMv3 font/gray style model to ONNX and benchmark CPU latency.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--limit-docs", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--onnx-threads", type=int, default=0)
    parser.add_argument("--onnx-inter-op-threads", type=int, default=0)
    parser.add_argument("--onnx-graph-optimization-level", choices=["disable", "basic", "extended", "all"], default="all")
    parser.add_argument("--skip-optimized-fp32", action="store_true")
    parser.add_argument("--subword-label-strategy", choices=["same", "first"], default="same")
    return parser.parse_args()


class TokenClassifierOnnxWrapper(nn.Module):
    def __init__(self, model: nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, input_ids, attention_mask, bbox, token_type_ids):
        return self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            bbox=bbox,
            token_type_ids=token_type_ids,
        ).logits


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
    style = row.get("layoutlmv3_style_type_id") or [0] * len(row["tokens"])
    word_ids = encoded.word_ids(batch_index=0)
    token_type_ids = torch.tensor(
        [[0 if word_id is None else int(style[int(word_id)]) for word_id in word_ids]],
        dtype=torch.long,
    )
    inputs = (encoded["input_ids"], encoded["attention_mask"], encoded["bbox"], token_type_ids)
    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        wrapper,
        inputs,
        str(onnx_path),
        input_names=["input_ids", "attention_mask", "bbox", "token_type_ids"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "sequence"},
            "attention_mask": {0: "batch", 1: "sequence"},
            "bbox": {0: "batch", 1: "sequence"},
            "token_type_ids": {0: "batch", 1: "sequence"},
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


def metric_summary(rows: list[dict[str, Any]], labels: list[list[str]], scores: list[list[float]]) -> dict[str, float]:
    metrics = compute_kie_metrics(rows, labels, scores)
    return {
        "word_f1": metrics["word"]["overall"]["f1"],
        "span_f1": metrics["span"]["overall"]["f1"],
        "exact_instance_accuracy": metrics["exact_instance_accuracy"],
        "missing_word_rate": metrics["missing_word_rate"],
        "extra_word_rate": metrics["extra_word_rate"],
    }


def main() -> None:
    args = parse_args()
    dirs = ensure_project_dirs(args.project_root)
    model_path = Path(args.model_path)
    rows = load_dataset_split(args.project_root, args.split, limit_docs=args.limit_docs)
    if not rows:
        raise SystemExit(f"No rows found for split {args.split}. Run 1-build_dataset.py first.")

    onnx_path = dirs["onnx"] / "layoutlmv3_fontgray_norm_run1.fp32.onnx"
    int8_path = dirs["onnx"] / "layoutlmv3_fontgray_norm_run1.int8.onnx"
    optimized_fp32_path = dirs["onnx"] / "layoutlmv3_fontgray_norm_run1.fp32.ort_optimized.onnx"
    report: dict[str, Any] = {
        "project_root": str(Path(args.project_root).resolve()),
        "model_path": str(model_path.resolve()),
        "split": args.split,
        "rows": len(rows),
        "docs": len({row["doc_id"] for row in rows}),
        "onnx_fp32": str(onnx_path),
        "onnx_fp32_ort_optimized": str(optimized_fp32_path),
        "onnx_int8": str(int8_path),
        "style_input": "token_type_ids",
        "style_normalization": "font_size, fg_gray, and word_height are bucketed relative to page medians",
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
        onnx_labels, onnx_scores, onnx_latency = predict_onnx(
            rows,
            onnx_path,
            model_path,
            max_length=args.max_length,
            stride=args.stride,
            batch_size=args.batch_size,
            warmup=args.warmup,
            subword_label_strategy=args.subword_label_strategy,
            intra_op_num_threads=args.onnx_threads,
            inter_op_num_threads=args.onnx_inter_op_threads,
            graph_optimization_level=args.onnx_graph_optimization_level,
            optimized_model_path=None if args.skip_optimized_fp32 else optimized_fp32_path,
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
            optimized_labels, optimized_scores, optimized_latency = predict_onnx(
                rows,
                optimized_fp32_path,
                model_path,
                max_length=args.max_length,
                stride=args.stride,
                batch_size=args.batch_size,
                warmup=args.warmup,
                subword_label_strategy=args.subword_label_strategy,
                intra_op_num_threads=args.onnx_threads,
                inter_op_num_threads=args.onnx_inter_op_threads,
                graph_optimization_level=args.onnx_graph_optimization_level,
            )
            report["latency"]["onnx_fp32_ort_optimized"] = optimized_latency
            report["accuracy"]["onnx_fp32_ort_optimized"] = metric_summary(rows, optimized_labels, optimized_scores)
        except Exception as exc:
            report["errors"]["onnx_fp32_ort_optimized"] = repr(exc)

    quant_error = quantize_int8(onnx_path, int8_path)
    if quant_error:
        report["errors"]["onnx_int8_quantization"] = quant_error
    elif int8_path.exists():
        try:
            int8_labels, int8_scores, int8_latency = predict_onnx(
                rows,
                int8_path,
                model_path,
                max_length=args.max_length,
                stride=args.stride,
                batch_size=args.batch_size,
                warmup=args.warmup,
                subword_label_strategy=args.subword_label_strategy,
                intra_op_num_threads=args.onnx_threads,
                inter_op_num_threads=args.onnx_inter_op_threads,
                graph_optimization_level=args.onnx_graph_optimization_level,
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
    write_style_report(args.project_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
