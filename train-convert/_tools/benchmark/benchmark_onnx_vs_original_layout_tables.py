"""Benchmark original PyTorch layout/table models against ONNX runtimes.

Dev-only diagnostic script. It intentionally imports PyTorch-backed packages
for the "original" side, while the ONNX side uses the portable runtime modules.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "ocr_app.py").exists():
            return parent
    return Path.cwd()


ROOT = _repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fitz
from PIL import Image


def _resolve_pdf(path_or_glob: str) -> Path:
    p = Path(path_or_glob)
    if p.exists():
        return p
    parent = p.parent if str(p.parent) not in {"", "."} else Path.cwd()
    matches = sorted(parent.glob(p.name))
    if not matches:
        raise FileNotFoundError(path_or_glob)
    return matches[0]


def _render_pages(pdf_path: Path, dpi: int) -> tuple[list[Image.Image], list[dict]]:
    doc = fitz.open(str(pdf_path))
    pages: list[Image.Image] = []
    infos: list[dict] = []
    t0 = time.perf_counter()
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
        pages.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
        infos.append({
            "page": i + 1,
            "rect": [page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y1],
            "text_chars": len(page.get_text()),
            "image_size": [pix.width, pix.height],
        })
    render_s = time.perf_counter() - t0
    doc.close()
    return pages, [{"render_s": render_s, "pages": infos}]


def _iou(a: list[float], b: list[float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    return inter / max(1e-6, area_a + area_b - inter)


def _compare_regions(pt_pages: list[list[dict]], onnx_pages: list[list[dict]]) -> list[dict]:
    out = []
    for idx, (pt, ox) in enumerate(zip(pt_pages, onnx_pages), 1):
        used = set()
        matches = []
        for p in pt:
            best_j, best_iou = None, 0.0
            for j, o in enumerate(ox):
                if j in used or o["type"] != p["type"]:
                    continue
                iou = _iou(p["bbox"], o["bbox"])
                if iou > best_iou:
                    best_j, best_iou = j, iou
            if best_j is not None and best_iou >= 0.90:
                used.add(best_j)
                matches.append(best_iou)
        out.append({
            "page": idx,
            "pt_count": len(pt),
            "onnx_count": len(ox),
            "pt_types": dict(Counter(r["type"] for r in pt)),
            "onnx_types": dict(Counter(r["type"] for r in ox)),
            "matched_iou90": len(matches),
            "mean_iou_matched": round(sum(matches) / len(matches), 4) if matches else 0.0,
        })
    return out


def benchmark_doclayout(pages: list[Image.Image]) -> dict:
    from doclayout_yolo import YOLOv10
    from layout_analyzer import LayoutAnalyzer

    pt_path = Path("models/doclayout_yolo_docstructbench_imgsz1024.pt")

    t0 = time.perf_counter()
    pt_model = YOLOv10(str(pt_path))
    pt_load_s = time.perf_counter() - t0
    pt_pages = []
    pt_times = []
    for img in pages:
        t = time.perf_counter()
        result = pt_model.predict(img, imgsz=1024, conf=0.25, verbose=False)
        pt_times.append(time.perf_counter() - t)
        regs = []
        for box in result[0].boxes:
            regs.append({
                "type": result[0].names[int(box.cls)],
                "bbox": [round(float(c), 1) for c in box.xyxy[0].tolist()],
                "confidence": round(float(box.conf), 4),
            })
        pt_pages.append(regs)

    t0 = time.perf_counter()
    onnx_model = LayoutAnalyzer()
    onnx_load_s = time.perf_counter() - t0
    onnx_pages = []
    onnx_times = []
    for img in pages:
        t = time.perf_counter()
        regs = onnx_model.analyze_page(img, conf=0.25)
        onnx_times.append(time.perf_counter() - t)
        onnx_pages.append(regs)

    return {
        "pt_load_s": pt_load_s,
        "onnx_load_s": onnx_load_s,
        "pt_total_s": sum(pt_times),
        "onnx_total_s": sum(onnx_times),
        "pt_avg_page_s": sum(pt_times) / len(pt_times),
        "onnx_avg_page_s": sum(onnx_times) / len(onnx_times),
        "pt_regions_total": sum(len(x) for x in pt_pages),
        "onnx_regions_total": sum(len(x) for x in onnx_pages),
        "comparison": _compare_regions(pt_pages, onnx_pages),
        "pt_pages": pt_pages,
        "onnx_pages": onnx_pages,
    }


def _page_info(pdf_path: Path) -> dict:
    doc = fitz.open(str(pdf_path))
    info = {}
    for i, page in enumerate(doc, 1):
        info[i] = {"width": page.rect.width, "height": page.rect.height}
    doc.close()
    return info


def benchmark_gmft(pdf_path: Path) -> dict:
    from table_anchored_merger import Logger, extract_pdf_lines
    from gmft_onnx_table_engine import detect_tables_gmft_onnx

    sys.path.insert(
        0,
        os.path.join(
            os.getcwd(),
            "temp",
            "legacy_model_train_20260504",
            "root_cleanup",
            "antigravity-gmft",
        ),
    )
    from gmft_table_engine import detect_tables_gmft

    logger = Logger(None)
    lines, info = extract_pdf_lines(str(pdf_path), logger)

    onnx_logger = Logger(None)
    t = time.perf_counter()
    onnx_tables = detect_tables_gmft_onnx(str(pdf_path), onnx_logger, info, lines, device="cpu")
    onnx_s = time.perf_counter() - t

    pt_logger = Logger(None)
    t = time.perf_counter()
    pt_tables = detect_tables_gmft(str(pdf_path), pt_logger, info, lines, device="cpu")
    pt_s = time.perf_counter() - t

    def table_summary(tables: list[Any]) -> list[dict]:
        out = []
        for t in tables:
            out.append({
                "page": int(getattr(t, "page", 0)),
                "rows": int(getattr(t, "row_count", getattr(t, "rows", 0)) or 0),
                "cols": int(getattr(t, "col_count", getattr(t, "cols", 0)) or 0),
                "y_top": round(float(getattr(t, "y_top", 0.0)), 1),
                "y_bottom": round(float(getattr(t, "y_bottom", 0.0)), 1),
                "nonempty_cells": sum(
                    1 for row in (getattr(t, "cells", None) or [])
                    for cell in row if str(cell or "").strip()
                ),
            })
        return out

    return {
        "text_lines": len(lines),
        "onnx_total_s": onnx_s,
        "pt_total_s": pt_s,
        "onnx_tables": table_summary(onnx_tables),
        "pt_tables": table_summary(pt_tables),
        "onnx_logs": onnx_logger.lines,
        "pt_logs": pt_logger.lines,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default="temp/benchmark_onnx_vs_original_layout_tables.json")
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    pdf_path = _resolve_pdf(args.pdf)
    pages, meta = _render_pages(pdf_path, args.dpi)
    result = {
        "pdf": str(pdf_path),
        "page_count": len(pages),
        "render": meta[0],
        "doclayout": benchmark_doclayout(pages),
        "gmft": benchmark_gmft(pdf_path),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    dl = result["doclayout"]
    gm = result["gmft"]
    print("PDF", pdf_path)
    print("pages", result["page_count"], "render_s", round(result["render"]["render_s"], 2))
    print("DocLayout PT total", round(dl["pt_total_s"], 2), "ONNX total", round(dl["onnx_total_s"], 2))
    print("DocLayout regions PT", dl["pt_regions_total"], "ONNX", dl["onnx_regions_total"])
    mismatches = [x for x in dl["comparison"] if x["pt_count"] != x["onnx_count"] or x["matched_iou90"] != min(x["pt_count"], x["onnx_count"])]
    print("DocLayout mismatch pages", len(mismatches), [x["page"] for x in mismatches[:20]])
    print("GMFT text_lines", gm["text_lines"])
    print("GMFT ONNX total", round(gm["onnx_total_s"], 2), "PT total", round(gm["pt_total_s"], 2))
    print("GMFT ONNX tables", gm["onnx_tables"])
    print("GMFT PT tables", gm["pt_tables"])
    print("saved", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
