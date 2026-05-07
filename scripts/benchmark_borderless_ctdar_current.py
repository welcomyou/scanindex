from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import fitz
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from table_anchored_merger import (  # noqa: E402
    detect_tables,
    filter_false_positive_tables,
    postprocess_table_layout_grids,
    repair_continued_tables,
    split_stacked_tables,
)


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".JPG", ".JPEG", ".TIFF")


class QuietLogger:
    def __init__(self):
        self.lines: list[str] = []

    def log(self, msg: str):
        self.lines.append(str(msg))


@dataclass
class GtTable:
    rows: int
    cols: int
    bbox_px: tuple[float, float, float, float]
    bbox_pdf: tuple[float, float, float, float]
    cell_count: int
    span_cell_count: int


def parse_points(points: str) -> list[tuple[float, float]]:
    out = []
    for item in points.strip().split():
        if "," not in item:
            continue
        x, y = item.split(",", 1)
        out.append((float(x), float(y)))
    return out


def points_bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def scale_bbox(bbox: tuple[float, float, float, float], scale: float) -> tuple[float, float, float, float]:
    return tuple(float(v) * scale for v in bbox)  # type: ignore[return-value]


def parse_ctdar_xml(xml_path: Path, pdf_scale: float) -> list[GtTable]:
    root = ET.parse(xml_path).getroot()
    tables: list[GtTable] = []
    for table in root.findall(".//table"):
        coords = table.find("Coords")
        if coords is None or "points" not in coords.attrib:
            continue
        table_bbox_px = points_bbox(parse_points(coords.attrib["points"]))
        max_row = -1
        max_col = -1
        cell_count = 0
        span_cell_count = 0
        for cell in table.findall("cell"):
            try:
                sr = int(cell.attrib.get("start-row", "0"))
                er = int(cell.attrib.get("end-row", str(sr)))
                sc = int(cell.attrib.get("start-col", "0"))
                ec = int(cell.attrib.get("end-col", str(sc)))
            except ValueError:
                continue
            max_row = max(max_row, er)
            max_col = max(max_col, ec)
            cell_count += 1
            if er > sr or ec > sc:
                span_cell_count += 1
        if max_row < 0 or max_col < 0:
            continue
        tables.append(
            GtTable(
                rows=max_row + 1,
                cols=max_col + 1,
                bbox_px=table_bbox_px,
                bbox_pdf=scale_bbox(table_bbox_px, pdf_scale),
                cell_count=cell_count,
                span_cell_count=span_cell_count,
            )
        )
    return tables


def bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max((ax1 - ax0) * (ay1 - ay0), 1e-6)
    area_b = max((bx1 - bx0) * (by1 - by0), 1e-6)
    return inter / (area_a + area_b - inter)


def table_bbox(table: Any, page_width: float) -> tuple[float, float, float, float]:
    x_left = getattr(table, "x_left", None)
    x_right = getattr(table, "x_right", None)
    y_top = float(getattr(table, "y_top", 0.0) or 0.0)
    y_bottom = float(getattr(table, "y_bottom", 0.0) or 0.0)
    if x_left is not None and x_right is not None:
        return float(x_left), y_top, float(x_right), y_bottom

    xs: list[float] = []
    ys: list[float] = []
    for row in getattr(table, "cell_bboxes", []) or []:
        for bx in row:
            if len(bx) >= 4 and bx[2] > bx[0] and bx[3] > bx[1]:
                xs.extend([float(bx[0]), float(bx[2])])
                ys.extend([float(bx[1]), float(bx[3])])
    if xs and ys:
        return min(xs), min(ys), max(xs), max(ys)
    return 0.0, y_top, float(page_width), y_bottom


def shape_score(gt_rows: int, gt_cols: int, out_rows: int, out_cols: int) -> float:
    if gt_rows <= 0 or gt_cols <= 0 or out_rows <= 0 or out_cols <= 0:
        return 0.0
    return (min(gt_rows, out_rows) / max(gt_rows, out_rows)) * (
        min(gt_cols, out_cols) / max(gt_cols, out_cols)
    )


def evaluate(gt_tables: list[GtTable], pred_tables: list[Any], page_width: float) -> dict[str, Any]:
    pred = [
        {
            "rows": int(getattr(t, "row_count", 0) or 0),
            "cols": int(getattr(t, "col_count", 0) or 0),
            "bbox": table_bbox(t, page_width),
            "source": getattr(t, "source", ""),
        }
        for t in pred_tables
        if not getattr(t, "skip_render", False)
    ]
    details = []
    used_pred: set[int] = set()
    shape_scores = []
    ious = []
    exact_shapes = 0
    iou50 = 0
    for gt in gt_tables:
        best_idx = -1
        best_iou = 0.0
        for idx, p in enumerate(pred):
            if idx in used_pred:
                continue
            iou = bbox_iou(gt.bbox_pdf, p["bbox"])
            if iou > best_iou:
                best_iou = iou
                best_idx = idx
        if best_idx >= 0:
            used_pred.add(best_idx)
            p = pred[best_idx]
            s = shape_score(gt.rows, gt.cols, p["rows"], p["cols"])
            exact = gt.rows == p["rows"] and gt.cols == p["cols"]
            if best_iou >= 0.5:
                iou50 += 1
            if exact:
                exact_shapes += 1
            details.append(
                {
                    "gt_shape": [gt.rows, gt.cols],
                    "pred_shape": [p["rows"], p["cols"]],
                    "iou": best_iou,
                    "shape_score": s,
                    "source": p["source"],
                }
            )
        else:
            s = 0.0
            details.append(
                {
                    "gt_shape": [gt.rows, gt.cols],
                    "pred_shape": [0, 0],
                    "iou": 0.0,
                    "shape_score": 0.0,
                    "source": "",
                }
            )
        shape_scores.append(s)
        ious.append(best_iou)

    return {
        "gt_count": len(gt_tables),
        "pred_count": len(pred),
        "matched_iou50": iou50,
        "recall_iou50": iou50 / len(gt_tables) if gt_tables else 1.0,
        "mean_best_iou": sum(ious) / len(ious) if ious else 1.0,
        "shape_acc": sum(shape_scores) / len(shape_scores) if shape_scores else 1.0,
        "exact_shape_rate": exact_shapes / len(gt_tables) if gt_tables else 1.0,
        "pred_shapes": [[p["rows"], p["cols"]] for p in pred],
        "sources": [p["source"] for p in pred],
        "details": details,
    }


def line_density_for_bbox(image: Image.Image, bbox: tuple[float, float, float, float]) -> dict[str, float]:
    import cv2

    x0, y0, x1, y1 = [int(round(v)) for v in bbox]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(image.width, x1), min(image.height, y1)
    if x1 <= x0 or y1 <= y0:
        return {"line_density": 1.0, "h_lines": 0.0, "v_lines": 0.0}
    crop = np.asarray(image.crop((x0, y0, x1, y1)).convert("L"))
    binary = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    h, w = binary.shape[:2]
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 30), 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, h // 30)))
    h_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel, iterations=1)
    v_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel, iterations=1)
    line_mask = cv2.bitwise_or(h_mask, v_mask)
    density = float(np.count_nonzero(line_mask)) / max(float(w * h), 1.0)
    h_proj = np.count_nonzero(h_mask, axis=1)
    v_proj = np.count_nonzero(v_mask, axis=0)
    h_lines = float(np.count_nonzero(h_proj > max(10, w * 0.2)))
    v_lines = float(np.count_nonzero(v_proj > max(10, h * 0.2)))
    return {"line_density": density, "h_lines": h_lines, "v_lines": v_lines}


def find_image_for_xml(xml_path: Path, image_dir: Path) -> Path | None:
    stem = xml_path.stem
    for ext in IMAGE_EXTS:
        p = image_dir / f"{stem}{ext}"
        if p.exists():
            return p
    return None


def collect_candidates(root: Path, track: str, modern_only: bool, pdf_dpi: int, selection: str) -> list[dict[str, Any]]:
    image_dir = root / "test" / track
    gt_dir = root / "test_ground_truth" / track
    if not image_dir.exists() or not gt_dir.exists():
        raise FileNotFoundError(f"Missing cTDaR {track} directories under {root}")
    scale = 72.0 / float(pdf_dpi)
    candidates = []
    for xml_path in sorted(gt_dir.glob("*.xml")):
        if modern_only and not xml_path.stem.startswith("cTDaR_t1"):
            continue
        image_path = find_image_for_xml(xml_path, image_dir)
        if image_path is None:
            continue
        try:
            with Image.open(image_path) as im:
                im = im.convert("RGB")
                gt_tables = parse_ctdar_xml(xml_path, scale)
                if not gt_tables:
                    continue
                densities = [line_density_for_bbox(im, t.bbox_px) for t in gt_tables]
                avg_density = sum(d["line_density"] for d in densities) / len(densities)
                candidates.append(
                    {
                        "id": xml_path.stem,
                        "image": str(image_path),
                        "xml": str(xml_path),
                        "table_count": len(gt_tables),
                        "rows": max(t.rows for t in gt_tables),
                        "cols": max(t.cols for t in gt_tables),
                        "cell_count": sum(t.cell_count for t in gt_tables),
                        "span_cell_count": sum(t.span_cell_count for t in gt_tables),
                        "line_density": avg_density,
                        "h_lines": sum(d["h_lines"] for d in densities),
                        "v_lines": sum(d["v_lines"] for d in densities),
                    }
                )
        except Exception as exc:
            candidates.append(
                {
                    "id": xml_path.stem,
                    "image": str(image_path),
                    "xml": str(xml_path),
                    "error": str(exc),
                    "line_density": math.inf,
                }
            )
    candidates = [c for c in candidates if "error" not in c]
    reverse = selection == "highest"
    return sorted(
        candidates,
        key=lambda c: (c["line_density"], -c["span_cell_count"], c["id"]),
        reverse=reverse,
    )


def image_to_pdf(image_path: Path, pdf_path: Path, dpi: int) -> tuple[float, float]:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        width_pt = im.width * 72.0 / float(dpi)
        height_pt = im.height * 72.0 / float(dpi)
        im.save(pdf_path, "PDF", resolution=float(dpi))
    return width_pt, height_pt


def render_pdf_page(pdf_path: Path) -> Image.Image:
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
        return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    finally:
        doc.close()


def analyze_layout(page_image: Image.Image, conf: float) -> list[dict[str, Any]]:
    try:
        from layout_analyzer import get_analyzer

        analyzer = get_analyzer()
        if analyzer is None:
            return []
        regions = analyzer.analyze_page(page_image, conf=conf)
        out = []
        for r in regions:
            bbox = r.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            out.append(
                {
                    "type": r.get("type", ""),
                    "bbox": bbox,
                    "bbox_pdf": bbox,
                    "confidence": r.get("confidence", 0.0),
                }
            )
        return out
    except Exception:
        return []


def postprocess_current_tables(tables: list[Any], layout_regions_by_page: dict[int, list[dict[str, Any]]], page_info: dict[int, dict[str, float]], logger: QuietLogger):
    processed = repair_continued_tables(tables, layout_regions_by_page, [], page_info, logger)
    processed = split_stacked_tables(processed, logger)
    processed = postprocess_table_layout_grids(processed, layout_regions_by_page, logger)
    processed = filter_false_positive_tables(processed, layout_regions_by_page, logger)
    return [table for table in processed if not getattr(table, "skip_render", False)]


def evaluate_doclayout(gt_tables: list[GtTable], layout_regions: list[dict[str, Any]]) -> dict[str, Any]:
    preds = [tuple(float(v) for v in r["bbox_pdf"][:4]) for r in layout_regions if r.get("type") == "table"]
    best_ious = []
    iou50 = 0
    used: set[int] = set()
    for gt in gt_tables:
        best_idx = -1
        best = 0.0
        for idx, bbox in enumerate(preds):
            if idx in used:
                continue
            iou = bbox_iou(gt.bbox_pdf, bbox)
            if iou > best:
                best = iou
                best_idx = idx
        if best_idx >= 0:
            used.add(best_idx)
        if best >= 0.5:
            iou50 += 1
        best_ious.append(best)
    return {
        "pred_count": len(preds),
        "matched_iou50": iou50,
        "recall_iou50": iou50 / len(gt_tables) if gt_tables else 1.0,
        "mean_best_iou": sum(best_ious) / len(best_ious) if best_ious else 1.0,
    }


def run_case(candidate: dict[str, Any], args) -> dict[str, Any]:
    image_path = Path(candidate["image"])
    xml_path = Path(candidate["xml"])
    pdf_scale = 72.0 / float(args.pdf_dpi)
    gt_tables = parse_ctdar_xml(xml_path, pdf_scale)
    pdf_path = args.out_dir / "pdfs" / f"{candidate['id']}.pdf"

    width_pt, height_pt = image_to_pdf(image_path, pdf_path, args.pdf_dpi)
    page_info = {1: {"width": width_pt, "height": height_pt}}
    page_image = render_pdf_page(pdf_path)

    layout_t0 = time.perf_counter()
    layout_regions = analyze_layout(page_image, args.doclayout_conf)
    layout_sec = time.perf_counter() - layout_t0
    layout_regions_by_page = {1: layout_regions}

    logger = QuietLogger()
    detect_t0 = time.perf_counter()
    raw_tables = detect_tables(str(pdf_path), logger, page_info, [], layout_regions_by_page)
    detect_sec = time.perf_counter() - detect_t0

    post_t0 = time.perf_counter()
    post_tables = postprocess_current_tables(copy.deepcopy(raw_tables), layout_regions_by_page, page_info, logger)
    post_sec = time.perf_counter() - post_t0

    raw_eval = evaluate(gt_tables, raw_tables, width_pt)
    post_eval = evaluate(gt_tables, post_tables, width_pt)
    layout_eval = evaluate_doclayout(gt_tables, layout_regions)

    return {
        "id": candidate["id"],
        "image": str(image_path),
        "xml": str(xml_path),
        "line_density": candidate["line_density"],
        "h_lines": candidate["h_lines"],
        "v_lines": candidate["v_lines"],
        "gt_count": len(gt_tables),
        "gt_shapes": [[t.rows, t.cols] for t in gt_tables],
        "gt_cell_count": sum(t.cell_count for t in gt_tables),
        "gt_span_cell_count": sum(t.span_cell_count for t in gt_tables),
        "layout": layout_eval,
        "raw": raw_eval,
        "post": post_eval,
        "timing": {
            "layout_sec": layout_sec,
            "detect_sec": detect_sec,
            "postprocess_sec": post_sec,
            "total_sec": layout_sec + detect_sec + post_sec,
        },
        "log_tail": logger.lines[-40:],
    }


def write_outputs(results: list[dict[str, Any]], out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "details.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = []
    for r in results:
        rows.append(
            {
                "id": r["id"],
                "line_density": r["line_density"],
                "gt_shapes": json.dumps(r["gt_shapes"]),
                "gt_span_cell_count": r["gt_span_cell_count"],
                "layout_pred_count": r["layout"]["pred_count"],
                "layout_recall_iou50": r["layout"]["recall_iou50"],
                "layout_mean_iou": r["layout"]["mean_best_iou"],
                "raw_pred_count": r["raw"]["pred_count"],
                "raw_shape_acc": r["raw"]["shape_acc"],
                "raw_exact_shape_rate": r["raw"]["exact_shape_rate"],
                "raw_recall_iou50": r["raw"]["recall_iou50"],
                "raw_mean_iou": r["raw"]["mean_best_iou"],
                "post_pred_count": r["post"]["pred_count"],
                "post_shape_acc": r["post"]["shape_acc"],
                "post_exact_shape_rate": r["post"]["exact_shape_rate"],
                "post_recall_iou50": r["post"]["recall_iou50"],
                "post_mean_iou": r["post"]["mean_best_iou"],
                "sources": "|".join(r["post"]["sources"]),
                "layout_sec": r["timing"]["layout_sec"],
                "detect_sec": r["timing"]["detect_sec"],
                "postprocess_sec": r["timing"]["postprocess_sec"],
                "total_sec": r["timing"]["total_sec"],
            }
        )
    if not rows:
        return
    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    aggregate = {
        "cases": len(results),
        "layout_recall_iou50_avg": sum(r["layout"]["recall_iou50"] for r in results) / len(results),
        "layout_mean_iou_avg": sum(r["layout"]["mean_best_iou"] for r in results) / len(results),
        "raw_shape_acc_avg": sum(r["raw"]["shape_acc"] for r in results) / len(results),
        "raw_exact_shape_rate_avg": sum(r["raw"]["exact_shape_rate"] for r in results) / len(results),
        "raw_recall_iou50_avg": sum(r["raw"]["recall_iou50"] for r in results) / len(results),
        "post_shape_acc_avg": sum(r["post"]["shape_acc"] for r in results) / len(results),
        "post_exact_shape_rate_avg": sum(r["post"]["exact_shape_rate"] for r in results) / len(results),
        "post_recall_iou50_avg": sum(r["post"]["recall_iou50"] for r in results) / len(results),
        "total_sec": sum(r["timing"]["total_sec"] for r in results),
    }
    (out_dir / "aggregate.json").write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ctdar-root", type=Path, default=ROOT / "temp" / "external" / "ICDAR2019_cTDaR")
    parser.add_argument("--track", default="TRACKB2", choices=["TRACKB1", "TRACKB2"])
    parser.add_argument("--out-dir", type=Path, default=ROOT / "temp" / "borderless_ctdar_current")
    parser.add_argument("--max-cases", type=int, default=20)
    parser.add_argument("--selection", choices=["lowest", "highest"], default="lowest")
    parser.add_argument("--modern-only", action="store_true", default=True)
    parser.add_argument("--include-historical", action="store_false", dest="modern_only")
    parser.add_argument("--pdf-dpi", type=int, default=300)
    parser.add_argument("--doclayout-conf", type=float, default=0.25)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    candidates = collect_candidates(args.ctdar_root, args.track, args.modern_only, args.pdf_dpi, args.selection)
    (args.out_dir / "candidate_ranking.json").write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    selected = candidates[: args.max_cases]
    results = []
    for idx, candidate in enumerate(selected, 1):
        print(f"[{idx}/{len(selected)}] {candidate['id']} density={candidate['line_density']:.6f}")
        try:
            results.append(run_case(candidate, args))
            write_outputs(results, args.out_dir)
        except Exception as exc:
            results.append({"id": candidate["id"], "error": repr(exc), "candidate": candidate})
            write_outputs([r for r in results if "error" not in r], args.out_dir)
            print(f"  ERROR {candidate['id']}: {exc!r}")
    write_outputs([r for r in results if "error" not in r], args.out_dir)
    print(f"Wrote {args.out_dir}")


if __name__ == "__main__":
    main()
