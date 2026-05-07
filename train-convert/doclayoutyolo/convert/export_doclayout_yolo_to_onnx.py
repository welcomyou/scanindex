"""Export DocLayout-YOLO .pt checkpoint to ONNX for PyTorch-free runtime.

This script is dev-only. The portable app should ship only the generated
`.onnx` and `.names.json` files, not torch/doclayout-yolo/ultralytics.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--pt",
        default="models/doclayout_yolo_docstructbench_imgsz1024.pt",
        help="Source DocLayout-YOLO .pt checkpoint.",
    )
    ap.add_argument(
        "--out-dir",
        default="models/doclayout_yolo_onnx",
        help="Directory for exported ONNX runtime files.",
    )
    ap.add_argument("--imgsz", type=int, default=1024)
    ap.add_argument("--opset", type=int, default=18)
    ap.add_argument("--simplify", action="store_true")
    ap.add_argument(
        "--prefix",
        default="doclayout_yolo_docstructbench_imgsz1024",
        help="Base filename for exported ONNX and .names.json files.",
    )
    ap.add_argument(
        "--dynamic",
        action="store_true",
        help="Export dynamic H/W ONNX so runtime can match YOLOv10 rectangular letterbox.",
    )
    args = ap.parse_args()

    pt_path = Path(args.pt)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not pt_path.exists():
        raise FileNotFoundError(pt_path)

    from doclayout_yolo import YOLOv10

    model = YOLOv10(str(pt_path))
    exported = model.export(
        format="onnx",
        imgsz=args.imgsz,
        opset=args.opset,
        simplify=args.simplify,
        dynamic=args.dynamic,
    )
    exported_path = Path(exported) if exported else pt_path.with_suffix(".onnx")
    if not exported_path.exists():
        raise RuntimeError(f"ONNX export did not create {exported_path}")

    suffix = "_dynamic" if args.dynamic else ""
    dest = out_dir / f"{args.prefix}{suffix}.onnx"
    if exported_path.resolve() != dest.resolve():
        shutil.copy2(exported_path, dest)
        data_path = exported_path.with_name(exported_path.name + ".data")
        if data_path.exists():
            shutil.copy2(data_path, dest.with_name(dest.name + ".data"))

    names = getattr(model, "names", None) or {
        0: "title",
        1: "plain text",
        2: "abandon",
        3: "figure",
        4: "figure_caption",
        5: "table",
        6: "table_caption",
        7: "table_footnote",
        8: "isolate_formula",
        9: "formula_caption",
    }
    names_path = out_dir / f"{args.prefix}{suffix}.names.json"
    names_path.write_text(json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"ONNX: {dest} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"Names: {names_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
