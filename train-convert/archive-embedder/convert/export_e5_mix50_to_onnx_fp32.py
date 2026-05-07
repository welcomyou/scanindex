from __future__ import annotations

import argparse
import inspect
import json
import shutil
from pathlib import Path

import numpy as np
import onnxruntime as ort
import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def mean_pool_np(last_hidden_state: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    mask = attention_mask[..., None].astype(np.float32)
    pooled = (last_hidden_state * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
    norm = np.linalg.norm(pooled, axis=1, keepdims=True)
    return pooled / np.clip(norm, 1e-12, None)


def mean_pool_torch(last_hidden_state: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    mask = attention_mask.unsqueeze(-1).to(last_hidden_state.dtype)
    pooled = (last_hidden_state * mask).sum(dim=1) / torch.clamp(mask.sum(dim=1), min=1e-9)
    return F.normalize(pooled, p=2, dim=1)


class OnnxBackboneWrapper(torch.nn.Module):
    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self.model = model
        self.accepts_token_type_ids = "token_type_ids" in inspect.signature(model.forward).parameters

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        token_type_ids: torch.Tensor,
    ) -> torch.Tensor:
        kwargs = {"input_ids": input_ids, "attention_mask": attention_mask}
        if self.accepts_token_type_ids:
            kwargs["token_type_ids"] = token_type_ids
        return self.model(**kwargs).last_hidden_state


def export(args: argparse.Namespace) -> None:
    model_dir = Path(args.model_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = output_dir / "model.onnx"

    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    try:
        model = AutoModel.from_pretrained(model_dir, attn_implementation="eager").eval()
    except TypeError:
        model = AutoModel.from_pretrained(model_dir).eval()
        if hasattr(model.config, "_attn_implementation"):
            model.config._attn_implementation = "eager"
    tokenizer.save_pretrained(output_dir)
    model.config.save_pretrained(output_dir)

    encoded = tokenizer(
        ["query: kiểm tra mô hình sau fine tune", "passage: văn bản về công tác lưu trữ"],
        padding=True,
        truncation=True,
        max_length=32,
        return_tensors="pt",
    )
    input_ids = encoded["input_ids"]
    attention_mask = encoded["attention_mask"]
    token_type_ids = encoded.get("token_type_ids", torch.zeros_like(input_ids))

    wrapper = OnnxBackboneWrapper(model).eval()
    dynamic_axes = {
        "input_ids": {0: "batch_size", 1: "sequence_length"},
        "attention_mask": {0: "batch_size", 1: "sequence_length"},
        "token_type_ids": {0: "batch_size", 1: "sequence_length"},
        "last_hidden_state": {0: "batch_size", 1: "sequence_length"},
    }
    torch.onnx.export(
        wrapper,
        (input_ids, attention_mask, token_type_ids),
        str(onnx_path),
        input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["last_hidden_state"],
        dynamic_axes=dynamic_axes,
        opset_version=args.opset,
        do_constant_folding=True,
    )

    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    session_input_names = {i.name for i in sess.get_inputs()}
    feeds = {
        "input_ids": input_ids.numpy().astype(np.int64),
        "attention_mask": attention_mask.numpy().astype(np.int64),
    }
    if "token_type_ids" in session_input_names:
        feeds["token_type_ids"] = token_type_ids.numpy().astype(np.int64)

    with torch.no_grad():
        pt_last_hidden = wrapper(input_ids, attention_mask, token_type_ids)
        pt_embeddings = mean_pool_torch(pt_last_hidden, attention_mask).cpu().numpy()
    onnx_last_hidden = sess.run(None, feeds)[0].astype(np.float32)
    onnx_embeddings = mean_pool_np(onnx_last_hidden, feeds["attention_mask"])
    cosine = np.sum(pt_embeddings * onnx_embeddings, axis=1)
    max_abs_diff = float(np.max(np.abs(pt_embeddings - onnx_embeddings)))

    metadata = {
        "source_model_dir": str(model_dir),
        "onnx_path": str(onnx_path),
        "pooling": "mean",
        "normalize": True,
        "opset": args.opset,
        "embedding_dim": int(pt_embeddings.shape[1]),
        "parity_cosine_min": float(np.min(cosine)),
        "parity_cosine_mean": float(np.mean(cosine)),
        "parity_max_abs_diff": max_abs_diff,
    }
    write_json(output_dir / "onnx_metadata.json", metadata)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))

    if args.copy_to:
        target = Path(args.copy_to)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(output_dir, target)
        print(f"copied ONNX export to {target}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default="/workspace/e5_finetune_llm_v1/outputs/e5-small-kho-llm-v1/best")
    parser.add_argument("--output-dir", default="/workspace/e5_finetune_llm_v1/outputs/e5-small-kho-llm-v1-onnx-fp32")
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--copy-to", default="")
    return parser.parse_args()


if __name__ == "__main__":
    export(parse_args())
