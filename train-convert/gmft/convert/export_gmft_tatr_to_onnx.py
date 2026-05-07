"""Export GMFT's TATR detector and structure models to ONNX.

Dev-only: requires torch + transformers. The portable runtime uses
gmft_onnx_table_engine.py with only onnxruntime/numpy/PyMuPDF/Pillow.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _export_one(kind: str, repo: str, revision: str, processor_repo: str, out_root: Path) -> None:
    import torch
    from transformers import AutoImageProcessor, TableTransformerForObjectDetection

    out_dir = out_root / kind
    out_dir.mkdir(parents=True, exist_ok=True)

    processor = AutoImageProcessor.from_pretrained(processor_repo, local_files_only=True)
    model = TableTransformerForObjectDetection.from_pretrained(
        repo,
        revision=revision,
        local_files_only=True,
    ).eval()

    processor.save_pretrained(str(out_dir))
    model.config.to_json_file(str(out_dir / "config.json"))

    pixel_values = torch.zeros((1, 3, 800, 800), dtype=torch.float32)
    pixel_mask = torch.ones((1, 800, 800), dtype=torch.long)
    onnx_path = out_dir / "model.onnx"
    torch.onnx.export(
        model,
        (pixel_values, pixel_mask),
        str(onnx_path),
        input_names=["pixel_values", "pixel_mask"],
        output_names=["logits", "pred_boxes"],
        dynamic_axes={
            "pixel_values": {0: "batch", 2: "height", 3: "width"},
            "pixel_mask": {0: "batch", 1: "height", 2: "width"},
            "logits": {0: "batch", 1: "queries"},
            "pred_boxes": {0: "batch", 1: "queries"},
        },
        opset_version=18,
        do_constant_folding=True,
    )
    data_path = out_dir / "model.onnx.data"
    size = onnx_path.stat().st_size + (data_path.stat().st_size if data_path.exists() else 0)
    print(f"{kind}: {onnx_path} ({size / 1024 / 1024:.1f} MB incl. external data)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="models/gmft_onnx")
    ap.add_argument("--revision", default="no_timm")
    args = ap.parse_args()

    out_root = Path(args.out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    _export_one(
        "detection",
        "microsoft/table-transformer-detection",
        args.revision,
        "microsoft/table-transformer-detection",
        out_root,
    )
    _export_one(
        "structure",
        "microsoft/table-transformer-structure-recognition",
        args.revision,
        "microsoft/table-transformer-detection",
        out_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
