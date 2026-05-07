from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import benchmark_current_table_pipeline as gt_bench  # noqa: E402
from benchmark_groundtruth_engine_matrix import run_detector  # noqa: E402
from table_anchored_merger import TextLine, detect_tables  # noqa: E402


class QuietLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, msg: str) -> None:
        self.lines.append(str(msg))


VARIANTS = [
    {"id": "gmft", "label": "GMFT raw", "kind": "engine", "engine": "gmft", "feed_ocr": False},
    {"id": "img2table", "label": "img2tables raw", "kind": "engine", "engine": "img2table", "feed_ocr": False},
    {
        "id": "rapidtable",
        "label": "RapidTable SLANet+ raw",
        "kind": "engine",
        "engine": "rapidtable",
        "feed_ocr": False,
    },
    {
        "id": "rapidtable_ocr",
        "label": "RapidTable SLANet+ OCR-aware",
        "kind": "engine",
        "engine": "rapidtable",
        "feed_ocr": True,
    },
    {
        "id": "docling_tableformer_ocr",
        "label": "Docling TableFormer OCR-aware",
        "kind": "engine",
        "engine": "docling_tableformer",
        "feed_ocr": True,
    },
    {
        "id": "current_hybrid_post",
        "label": "Current hybrid + postprocess",
        "kind": "current",
    },
]


def image_to_pdf(image_path: Path, pdf_path: Path, dpi: int) -> tuple[float, float]:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as im:
        rgb = im.convert("RGB")
        width_pt = rgb.width * 72.0 / float(dpi)
        height_pt = rgb.height * 72.0 / float(dpi)
        rgb.save(pdf_path, "PDF", resolution=float(dpi))
    return width_pt, height_pt


def full_page_layout(width: float, height: float, dpi: int) -> dict[int, list[dict[str, Any]]]:
    scale = float(dpi) / 72.0
    return {
        1: [
            {
                "type": "table",
                "bbox_pdf": [0.0, 0.0, float(width), float(height)],
                "bbox": [0.0, 0.0, float(width) * scale, float(height) * scale],
                "confidence": 1.0,
            }
        ]
    }


def gt_ocr_lines(gt_path: Path) -> list[TextLine]:
    if not gt_path.exists():
        return []
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    lines: list[TextLine] = []
    order = 0
    source = str(gt.get("source_dataset", ""))

    if source == "docling-project/PubTables-1M_OTSL":
        raw_cells = gt.get("cells") or []
        if raw_cells and isinstance(raw_cells[0], list):
            raw_cells = raw_cells[0]
        for cell in raw_cells:
            if not isinstance(cell, dict):
                continue
            bbox = cell.get("bbox") or []
            if len(bbox) < 4:
                continue
            text = "".join(cell.get("tokens") or []).strip()
            if not text:
                continue
            x0, y0, x1, y1 = [float(v) for v in bbox[:4]]
            if x1 <= x0 or y1 <= y0:
                continue
            lines.append(
                TextLine(
                    text=text,
                    x=x0,
                    y=y0,
                    width=x1 - x0,
                    height=y1 - y0,
                    page=1,
                    confidence=1.0,
                    order=order,
                )
            )
            order += 1

    if source == "katphlab/fintabnet-pubtables-full":
        words = gt.get("ocr_words") or []
        boxes = gt.get("ocr_boxes") or []
        for word, bbox in zip(words, boxes):
            if len(bbox) < 4:
                continue
            text = str(word or "").strip()
            if not text:
                continue
            x0, y0, x1, y1 = [float(v) for v in bbox[:4]]
            if x1 <= x0 or y1 <= y0:
                continue
            lines.append(
                TextLine(
                    text=text,
                    x=x0,
                    y=y0,
                    width=x1 - x0,
                    height=y1 - y0,
                    page=1,
                    confidence=1.0,
                    order=order,
                )
            )
            order += 1

    return lines


def shape_score(gt_rows: int, gt_cols: int, out_rows: int, out_cols: int) -> float:
    if gt_rows <= 0 or gt_cols <= 0 or out_rows <= 0 or out_cols <= 0:
        return 0.0
    return (min(gt_rows, out_rows) / max(gt_rows, out_rows)) * (
        min(gt_cols, out_cols) / max(gt_cols, out_cols)
    )


def best_table(tables: list[Any]) -> Any | None:
    candidates = [t for t in tables if not getattr(t, "skip_render", False)]
    if not candidates:
        return None

    def area(table: Any) -> float:
        x_left = float(getattr(table, "x_left", 0.0) or 0.0)
        x_right = float(getattr(table, "x_right", 0.0) or 0.0)
        y_top = float(getattr(table, "y_top", 0.0) or 0.0)
        y_bottom = float(getattr(table, "y_bottom", 0.0) or 0.0)
        if x_right <= x_left:
            xs: list[float] = []
            for row in getattr(table, "cell_bboxes", []) or []:
                for bx in row:
                    if len(bx) >= 4 and bx[2] > bx[0]:
                        xs.extend([float(bx[0]), float(bx[2])])
            if xs:
                x_left, x_right = min(xs), max(xs)
        return max(0.0, x_right - x_left) * max(0.0, y_bottom - y_top)

    return max(candidates, key=area)


def run_variant(
    variant: dict[str, Any],
    pdf_path: Path,
    page_info: dict[int, dict[str, float]],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    pdf_lines: list[TextLine],
    args: argparse.Namespace,
) -> dict[str, Any]:
    logger = QuietLogger()
    start = time.perf_counter()
    if variant["kind"] == "current":
        raw_tables = detect_tables(str(pdf_path), logger, page_info, pdf_lines, layout_regions_by_page)
        post_start = time.perf_counter()
        tables = gt_bench.postprocess_current_tables(
            copy.deepcopy(raw_tables),
            layout_regions_by_page,
            pdf_lines,
            page_info,
            logger,
        )
        post_sec = time.perf_counter() - post_start
    else:
        tables = run_detector(
            variant["engine"],
            pdf_path,
            logger,
            page_info,
            pdf_lines,
            layout_regions_by_page,
            bool(variant.get("feed_ocr")),
            args,
        )
        post_sec = 0.0
    elapsed = time.perf_counter() - start
    table = best_table(tables)
    pred_rows = int(getattr(table, "row_count", 0) or 0) if table is not None else 0
    pred_cols = int(getattr(table, "col_count", 0) or 0) if table is not None else 0
    return {
        "variant_id": variant["id"],
        "label": variant["label"],
        "pred_rows": pred_rows,
        "pred_cols": pred_cols,
        "pred_count": len([t for t in tables if not getattr(t, "skip_render", False)]),
        "elapsed_sec": elapsed,
        "postprocess_sec": post_sec,
        "log_tail": logger.lines[-40:],
    }


def load_samples(manifest_path: Path, include_datasets: set[str]) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples: list[dict[str, Any]] = []
    for dataset in manifest.get("datasets", []):
        dataset_key = str(dataset.get("dataset", "")).lower()
        if include_datasets and dataset_key not in include_datasets:
            continue
        if dataset.get("status") != "ready":
            continue
        for sample in dataset.get("samples", []):
            image = Path(sample.get("image", ""))
            gt_path = Path(sample.get("groundtruth", ""))
            if not image.exists() or not gt_path.exists():
                continue
            samples.append(
                {
                    "dataset": dataset.get("dataset"),
                    "id": sample.get("id"),
                    "image": image,
                    "groundtruth": gt_path,
                    "gt_rows": int(sample.get("rows") or 0),
                    "gt_cols": int(sample.get("cols") or 0),
                }
            )
    return samples


def write_outputs(results: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "details.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    per_case_fields = [
        "dataset",
        "id",
        "variant_id",
        "label",
        "gt_shape",
        "pred_shape",
        "shape_acc",
        "exact_shape",
        "pred_count",
        "elapsed_sec",
        "postprocess_sec",
        "image",
        "groundtruth",
    ]
    with (out_dir / "per_case.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=per_case_fields)
        writer.writeheader()
        for row in results:
            writer.writerow({field: row[field] for field in per_case_fields})

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in results:
        grouped.setdefault((row["dataset"], row["variant_id"]), []).append(row)
    summary: list[dict[str, Any]] = []
    for (dataset, variant_id), rows in grouped.items():
        n = len(rows)
        summary.append(
            {
                "dataset": dataset,
                "variant_id": variant_id,
                "label": rows[0]["label"],
                "cases": n,
                "shape_acc": sum(float(r["shape_acc"]) for r in rows) / n,
                "exact_shape_rate": sum(1.0 if r["exact_shape"] else 0.0 for r in rows) / n,
                "elapsed_sec_total": sum(float(r["elapsed_sec"]) for r in rows),
                "elapsed_sec_avg": sum(float(r["elapsed_sec"]) for r in rows) / n,
            }
        )
    summary.sort(key=lambda row: (row["dataset"], row["variant_id"]))
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_fields = [
        "dataset",
        "variant_id",
        "label",
        "cases",
        "shape_acc",
        "exact_shape_rate",
        "elapsed_sec_total",
        "elapsed_sec_avg",
    ]
    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        for row in summary:
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "temp" / "external" / "table_gt_samples" / "manifest.json")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "temp" / "external_table_gt_benchmark")
    parser.add_argument("--pdf-dpi", type=int, default=72)
    parser.add_argument("--rapidtable-dpi", type=int, default=200)
    parser.add_argument("--rapidtable-pad", type=float, default=0.0)
    parser.add_argument("--wired-dpi", type=int, default=200)
    parser.add_argument("--wired-pad", type=float, default=0.0)
    parser.add_argument("--docling-dpi", type=int, default=144)
    parser.add_argument("--docling-pad", type=float, default=0.0)
    parser.add_argument("--docling-mode", choices=["fast", "accurate"], default="accurate")
    parser.add_argument("--docling-threads", type=int, default=4)
    parser.add_argument("--datasets", nargs="*", default=[])
    parser.add_argument("--max-cases-per-dataset", type=int, default=10)
    args = parser.parse_args()

    include_datasets = {item.lower() for item in args.datasets}
    samples = load_samples(args.manifest, include_datasets)
    if args.max_cases_per_dataset > 0:
        limited: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for sample in samples:
            dataset = str(sample["dataset"])
            counts[dataset] = counts.get(dataset, 0) + 1
            if counts[dataset] <= args.max_cases_per_dataset:
                limited.append(sample)
        samples = limited

    results: list[dict[str, Any]] = []
    for idx, sample in enumerate(samples, 1):
        print(f"[{idx}/{len(samples)}] {sample['dataset']} {sample['id']} gt={sample['gt_rows']}x{sample['gt_cols']}", flush=True)
        pdf_path = args.out_dir / "pdfs" / str(sample["dataset"]).replace(" ", "_").lower() / f"{sample['id']}.pdf"
        width_pt, height_pt = image_to_pdf(sample["image"], pdf_path, args.pdf_dpi)
        page_info = {1: {"width": width_pt, "height": height_pt}}
        layout_regions_by_page = full_page_layout(width_pt, height_pt, args.pdf_dpi)
        pdf_lines = gt_ocr_lines(sample["groundtruth"])
        for variant in VARIANTS:
            try:
                out = run_variant(variant, pdf_path, page_info, layout_regions_by_page, pdf_lines, args)
                score = shape_score(sample["gt_rows"], sample["gt_cols"], out["pred_rows"], out["pred_cols"])
                results.append(
                    {
                        **out,
                        "dataset": sample["dataset"],
                        "id": sample["id"],
                        "image": str(sample["image"]),
                        "groundtruth": str(sample["groundtruth"]),
                        "gt_shape": f"{sample['gt_rows']}x{sample['gt_cols']}",
                        "pred_shape": f"{out['pred_rows']}x{out['pred_cols']}",
                        "shape_acc": score,
                        "exact_shape": sample["gt_rows"] == out["pred_rows"] and sample["gt_cols"] == out["pred_cols"],
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "dataset": sample["dataset"],
                        "id": sample["id"],
                        "variant_id": variant["id"],
                        "label": variant["label"],
                        "image": str(sample["image"]),
                        "groundtruth": str(sample["groundtruth"]),
                        "gt_shape": f"{sample['gt_rows']}x{sample['gt_cols']}",
                        "pred_shape": "0x0",
                        "shape_acc": 0.0,
                        "exact_shape": False,
                        "pred_rows": 0,
                        "pred_cols": 0,
                        "pred_count": 0,
                        "elapsed_sec": 0.0,
                        "postprocess_sec": 0.0,
                        "log_tail": [repr(exc)],
                    }
                )
            write_outputs(results, args.out_dir)
    write_outputs(results, args.out_dir)
    print(json.dumps({"out_dir": str(args.out_dir), "rows": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
