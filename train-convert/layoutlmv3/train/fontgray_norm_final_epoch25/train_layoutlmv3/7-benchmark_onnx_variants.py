from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_layoutlmv3.common import compute_kie_metrics, ensure_project_dirs, load_dataset_split, write_json
from train_layoutlmv3.hf_utils import predict_onnx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark multiple LayoutLMv3 ONNX variants on a dataset split.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model-path", required=True, help="HF model directory used for tokenizer and label mapping.")
    parser.add_argument(
        "--onnx",
        action="append",
        required=True,
        help="Variant in name=path format. Can be passed multiple times.",
    )
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--limit-docs", type=int, default=20)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--onnx-threads", type=int, default=0)
    parser.add_argument("--onnx-inter-op-threads", type=int, default=0)
    parser.add_argument("--onnx-graph-optimization-level", choices=["disable", "basic", "extended", "all"], default="all")
    parser.add_argument("--subword-label-strategy", choices=["same", "first"], default="same")
    parser.add_argument("--output", help="Defaults to reports/onnx_variant_benchmark.json")
    return parser.parse_args()


def parse_variant(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--onnx must use name=path format")
    name, path = value.split("=", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError("ONNX variant name cannot be empty")
    return name, Path(path)


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
    rows = load_dataset_split(args.project_root, args.split, limit_docs=args.limit_docs)
    if not rows:
        raise SystemExit(f"No rows found for split {args.split}.")

    report: dict[str, Any] = {
        "project_root": str(Path(args.project_root).resolve()),
        "model_path": str(Path(args.model_path).resolve()),
        "split": args.split,
        "rows": len(rows),
        "docs": len({row["doc_id"] for row in rows}),
        "variants": {},
        "errors": {},
    }
    for raw_variant in args.onnx:
        name, onnx_path = parse_variant(raw_variant)
        try:
            labels, scores, latency = predict_onnx(
                rows,
                onnx_path,
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
            report["variants"][name] = {
                "path": str(onnx_path.resolve()),
                "latency": latency,
                "accuracy": metric_summary(rows, labels, scores),
            }
        except Exception as exc:
            report["errors"][name] = repr(exc)

    output = Path(args.output) if args.output else dirs["reports"] / "onnx_variant_benchmark.json"
    write_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
