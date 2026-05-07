from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a fine-tuned CrossEncoder reranker to ONNX and optional INT8.")
    parser.add_argument("--model-path", required=True, help="Fine-tuned Transformers/CrossEncoder model directory.")
    parser.add_argument("--output-dir", required=True, help="Directory to write ONNX artifacts.")
    parser.add_argument("--max-length", type=int, default=384)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--quantize-int8", action="store_true")
    parser.add_argument("--optimize", action="store_true", help="Save ONNX Runtime optimized FP32/INT8 graphs.")
    return parser.parse_args()


class _LogitsWrapper:
    def __init__(self, model):
        import torch

        class Wrapped(torch.nn.Module):
            def __init__(self, inner):
                super().__init__()
                self.inner = inner

            def forward(self, input_ids, attention_mask):
                return self.inner(input_ids=input_ids, attention_mask=attention_mask).logits

        self.module = Wrapped(model)


def _file_info(path: Path) -> dict:
    return {"path": str(path), "bytes": path.stat().st_size if path.exists() else None}


def _optimize_onnx(input_path: Path, output_path: Path) -> None:
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.optimized_model_filepath = str(output_path)
    ort.InferenceSession(str(input_path), sess_options=options, providers=["CPUExecutionProvider"])


def _validate(model, tokenizer, onnx_path: Path, max_length: int) -> dict:
    import numpy as np
    import onnxruntime as ort
    import torch

    pairs = [
        ("Chon ung vien la co quan ban hanh van ban.", "FIELD=ISSUE_ORG_NAME\nTEXT:\nUY BAN NHAN DAN QUAN 1\nLAYOUT: page=0"),
        ("Chon ung vien la trich yeu cua van ban.", "FIELD=DOC_SUBJECT\nTEXT:\nVe viec kiem tra cong tac nam 2026\nLAYOUT: page=0"),
        ("Chon ung vien la ho ten nguoi ky van ban.", "FIELD=SIGNER_NAME\nTEXT:\nNguyen Van A\nLAYOUT: page=1"),
    ]
    encoded = tokenizer(
        [item[0] for item in pairs],
        [item[1] for item in pairs],
        padding=True,
        truncation=True,
        max_length=max_length,
        return_tensors="pt",
    )
    model.eval()
    with torch.no_grad():
        torch_logits = model(**encoded).logits.detach().cpu().numpy().reshape(-1)

    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    input_names = {item.name for item in session.get_inputs()}
    np_inputs = {key: value.cpu().numpy().astype(np.int64) for key, value in encoded.items() if key in input_names}
    ort_logits = session.run([session.get_outputs()[0].name], np_inputs)[0].reshape(-1)
    diff = np.abs(torch_logits - ort_logits)
    return {
        "torch_logits": [float(value) for value in torch_logits.tolist()],
        "onnx_logits": [float(value) for value in ort_logits.tolist()],
        "max_abs_diff": float(diff.max()),
        "mean_abs_diff": float(diff.mean()),
    }


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fp32_path = output_dir / "model_fp32.onnx"
    int8_path = output_dir / "model_int8.onnx"
    optimized_fp32_path = output_dir / "model_fp32_optimized.onnx"
    optimized_int8_path = output_dir / "model_int8_optimized.onnx"

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    t0 = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(args.model_path)
    model = AutoModelForSequenceClassification.from_pretrained(args.model_path)
    model.eval()
    wrapper = _LogitsWrapper(model).module
    wrapper.eval()
    sample = tokenizer(
        ["Chon ung vien dung cho truong KIE."],
        ["FIELD=DOC_SUBJECT\nTEXT:\nMau export ONNX\nLAYOUT: page=0"],
        padding=True,
        truncation=True,
        max_length=args.max_length,
        return_tensors="pt",
    )
    dynamic_axes = {
        "input_ids": {0: "batch", 1: "sequence"},
        "attention_mask": {0: "batch", 1: "sequence"},
        "logits": {0: "batch"},
    }
    torch.onnx.export(
        wrapper,
        (sample["input_ids"], sample["attention_mask"]),
        str(fp32_path),
        input_names=["input_ids", "attention_mask"],
        output_names=["logits"],
        dynamic_axes=dynamic_axes,
        opset_version=args.opset,
        do_constant_folding=True,
        dynamo=False,
    )
    validation = _validate(model, tokenizer, fp32_path, args.max_length)

    artifacts = {"fp32": _file_info(fp32_path)}
    if args.optimize:
        _optimize_onnx(fp32_path, optimized_fp32_path)
        artifacts["fp32_optimized"] = _file_info(optimized_fp32_path)
    if args.quantize_int8:
        from onnxruntime.quantization import QuantType, quantize_dynamic

        quantize_dynamic(str(fp32_path), str(int8_path), weight_type=QuantType.QInt8, op_types_to_quantize=["MatMul"])
        artifacts["int8"] = _file_info(int8_path)
        if args.optimize:
            _optimize_onnx(int8_path, optimized_int8_path)
            artifacts["int8_optimized"] = _file_info(optimized_int8_path)

    report = {
        "model_path": str(Path(args.model_path).resolve()),
        "output_dir": str(output_dir.resolve()),
        "max_length": args.max_length,
        "opset": args.opset,
        "quantize_int8": args.quantize_int8,
        "optimize": args.optimize,
        "elapsed_sec": time.perf_counter() - t0,
        "validation": validation,
        "artifacts": artifacts,
    }
    write_json(output_dir / "export_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
