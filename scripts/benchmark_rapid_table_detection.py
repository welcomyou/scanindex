from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import fitz
import numpy as np
from rapid_table import EngineType, ModelType, RapidTable, RapidTableInput
from rapid_table_det.inference import TableDetector

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark_rapidtable_slanet import (  # noqa: E402
    QuietLogger,
    compare_tables,
    crop_and_ocr,
    crop_rect,
    data_tables,
    docx_tables,
    iter_ocr_lines,
    load_layout_regions_by_page,
    postprocess_pipeline_tables,
    rapid_result_to_table,
    table_region_data,
)
from scanindex.core.tables.docx_exporter import extract_pdf_lines  # noqa: E402


def render_page_bgr(page: fitz.Page, dpi: int) -> np.ndarray:
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


def pixel_box_to_pdf(box: list[int], dpi: int) -> tuple[float, float, float, float]:
    scale = dpi / 72.0
    x0, y0, x1, y1 = [float(v) for v in box]
    return (x0 / scale, y0 / scale, x1 / scale, y1 / scale)


def detected_layout_regions(detections_by_page: dict[int, list[dict[str, Any]]]) -> dict[int, list[dict[str, Any]]]:
    regions: dict[int, list[dict[str, Any]]] = {}
    for page, detections in detections_by_page.items():
        page_regions = []
        for det in detections:
            page_regions.append({
                "type": "table",
                "bbox_pdf": list(det["bbox_pdf"]),
                "score": det.get("score", 1.0),
            })
        if page_regions:
            regions[page] = page_regions
    return regions


def bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    denom = area_a + area_b - inter
    return inter / denom if denom > 0 else 0.0


def doclayout_table_bboxes(json_path: Path) -> dict[int, list[tuple[float, float, float, float]]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    out: dict[int, list[tuple[float, float, float, float]]] = {}
    for page_idx, page in enumerate(data.get("pages", []), 1):
        for region in page.get("layout_regions", []):
            if region.get("type") != "table":
                continue
            bbox = region.get("bbox_pdf")
            if bbox and len(bbox) >= 4:
                out.setdefault(page_idx, []).append(tuple(float(v) for v in bbox[:4]))
    return out


def detection_iou_summary(
    detected_by_page: dict[int, list[dict[str, Any]]],
    doclayout_by_page: dict[int, list[tuple[float, float, float, float]]],
) -> dict[str, Any]:
    pages = sorted(set(detected_by_page) | set(doclayout_by_page))
    matched = []
    total_detected = 0
    total_doclayout = 0
    for page in pages:
        dets = [tuple(item["bbox_pdf"]) for item in detected_by_page.get(page, [])]
        refs = doclayout_by_page.get(page, [])
        total_detected += len(dets)
        total_doclayout += len(refs)
        used = set()
        for det in dets:
            best_iou = 0.0
            best_idx = None
            for idx, ref in enumerate(refs):
                if idx in used:
                    continue
                iou = bbox_iou(det, ref)
                if iou > best_iou:
                    best_iou = iou
                    best_idx = idx
            if best_idx is not None:
                used.add(best_idx)
            matched.append(best_iou)
    return {
        "detected_count": total_detected,
        "doclayout_count": total_doclayout,
        "mean_best_iou": sum(matched) / len(matched) if matched else 0.0,
        "matched_iou_ge_05": sum(1 for value in matched if value >= 0.5),
    }


def run_scan(scan: str, args, detector: TableDetector, table_engine: RapidTable) -> dict[str, Any]:
    ocr_pdf = args.ocr_dir / f"scan{scan}_ocr.pdf"
    json_path = Path(str(ocr_pdf) + ".json")
    lines_by_page = iter_ocr_lines(json_path)
    logger = QuietLogger()
    pdf_lines, page_info = extract_pdf_lines(str(ocr_pdf), logger)
    pdf_lines_by_page: dict[int, list[Any]] = {}
    for line in pdf_lines:
        pdf_lines_by_page.setdefault(line.page, []).append(line)

    detected_by_page: dict[int, list[dict[str, Any]]] = {}
    detect_elapsed = 0.0
    doc = fitz.open(str(ocr_pdf))
    try:
        for page_idx, page in enumerate(doc, 1):
            img_bgr = render_page_bgr(page, args.dpi)
            start = time.perf_counter()
            result, elapse = detector(
                img_bgr,
                det_accuracy=args.det_threshold,
                use_obj_det=True,
                use_edge_det=args.use_edge,
                use_cls_det=args.use_cls,
            )
            detect_elapsed += time.perf_counter() - start
            page_dets = []
            for det in result:
                bbox_pdf = pixel_box_to_pdf(det["box"], args.dpi)
                x0, y0, x1, y1 = bbox_pdf
                if x1 <= x0 or y1 <= y0:
                    continue
                page_dets.append({
                    "bbox_px": det["box"],
                    "bbox_pdf": bbox_pdf,
                    "lt": det.get("lt"),
                    "rt": det.get("rt"),
                    "rb": det.get("rb"),
                    "lb": det.get("lb"),
                })
            if page_dets:
                detected_by_page[page_idx] = page_dets

        table_elapsed = 0.0
        table_regions = []
        for page_num, detections in detected_by_page.items():
            for det_idx, det in enumerate(detections):
                bbox = tuple(det["bbox_pdf"])
                crop = crop_rect(doc[page_num - 1], bbox, args.pad_pt)
                img, ocr_result = crop_and_ocr(
                    doc[page_num - 1],
                    lines_by_page.get(page_num, []),
                    bbox,
                    args.dpi,
                    args.pad_pt,
                )
                img_for_rapidtable = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                start = time.perf_counter()
                result = table_engine(img_for_rapidtable, ocr_results=[ocr_result], batch_size=1)
                table_elapsed += time.perf_counter() - start
                table = rapid_result_to_table(
                    result,
                    page_num,
                    crop,
                    pdf_lines_by_page.get(page_num, []),
                    args.dpi,
                    logger,
                )
                if table is not None:
                    table_regions.append(table)
                if args.save_crops:
                    args.out_dir.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(args.out_dir / f"{scan}_p{page_num}_det{det_idx}.png"), img_for_rapidtable)
    finally:
        doc.close()

    layout_from_detected = detected_layout_regions(detected_by_page)
    post_start = time.perf_counter()
    post_tables = postprocess_pipeline_tables(table_regions, layout_from_detected, pdf_lines, page_info, logger)
    post_elapsed = time.perf_counter() - post_start

    gt_docx = args.gt03_docx if scan == "03" else args.groundtruth_dir / f"groundtruth{scan}.docx"
    gt_data = data_tables(docx_tables(gt_docx))
    raw_data = table_region_data(table_regions)
    post_data = table_region_data(post_tables)
    current_data = data_tables(docx_tables(args.current_dir / f"scan{scan}_final.docx"))

    return {
        "scan": scan,
        "detection": detection_iou_summary(detected_by_page, doclayout_table_bboxes(json_path)),
        "detected_pages": {str(page): [list(det["bbox_pdf"]) for det in dets] for page, dets in detected_by_page.items()},
        "raw": compare_tables(gt_data, raw_data),
        "postprocess": compare_tables(gt_data, post_data),
        "current": compare_tables(gt_data, current_data),
        "raw_shapes": [list(item[:2]) for item in raw_data],
        "post_shapes": [list(item[:2]) for item in post_data],
        "current_shapes": [list(item[:2]) for item in current_data],
        "gt_shapes": [list(item[:2]) for item in gt_data],
        "detect_time_sec": detect_elapsed,
        "table_time_sec": table_elapsed,
        "postprocess_time_sec": post_elapsed,
        "total_time_sec": detect_elapsed + table_elapsed + post_elapsed,
        "logs": logger.lines[-80:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ocr-dir", type=Path, default=Path(r"D:\App\ocrtool\temp\groundtruth5_pipeline"))
    parser.add_argument("--current-dir", type=Path, default=Path(r"D:\App\ocrtool\temp\groundtruth5_pipeline"))
    parser.add_argument("--groundtruth-dir", type=Path, default=Path(r"C:\Users\nhquan\Downloads\groundtruth"))
    parser.add_argument("--gt03-docx", type=Path, default=Path(r"D:\App\ocrtool\temp\groundtruth4_scan_word\groundtruth03_converted.docx"))
    parser.add_argument("--out-dir", type=Path, default=Path(r"D:\App\ocrtool\temp\rapid_table_detection_bench"))
    parser.add_argument("--scans", nargs="+", default=["01", "02", "03", "04", "05"])
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--pad-pt", type=float, default=6.0)
    parser.add_argument("--det-threshold", type=float, default=0.7)
    parser.add_argument("--use-edge", action="store_true")
    parser.add_argument("--use-cls", action="store_true")
    parser.add_argument("--save-crops", action="store_true")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    detector = TableDetector()
    detector_init_sec = time.perf_counter() - start

    cfg = RapidTableInput(
        model_type=ModelType.SLANETPLUS,
        engine_type=EngineType.ONNXRUNTIME,
        use_ocr=True,
        engine_cfg={"intra_op_num_threads": 4, "inter_op_num_threads": 1},
    )
    start = time.perf_counter()
    table_engine = RapidTable(cfg)
    table_init_sec = time.perf_counter() - start

    scans = [str(scan).zfill(2) for scan in args.scans]
    comparison = [run_scan(scan, args, detector, table_engine) for scan in scans]
    result = {
        "model": "RapidTableDetection object detector + RapidTable slanet_plus",
        "det_threshold": args.det_threshold,
        "use_edge": args.use_edge,
        "use_cls": args.use_cls,
        "detector_init_sec": detector_init_sec,
        "table_init_sec": table_init_sec,
        "comparison": comparison,
    }
    (args.out_dir / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    with (args.out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "scan",
            "detected_count",
            "doclayout_table_count",
            "mean_best_iou",
            "matched_iou_ge_05",
            "raw_count",
            "post_count",
            "gt_count",
            "raw_shape_acc",
            "post_shape_acc",
            "current_shape_acc",
            "raw_text_acc",
            "post_text_acc",
            "current_text_acc",
            "detect_time_sec",
            "table_time_sec",
            "postprocess_time_sec",
            "total_time_sec",
        ])
        for item in comparison:
            det = item["detection"]
            writer.writerow([
                item["scan"],
                det["detected_count"],
                det["doclayout_count"],
                det["mean_best_iou"],
                det["matched_iou_ge_05"],
                item["raw"]["count"],
                item["postprocess"]["count"],
                item["raw"]["gt_count"],
                item["raw"]["shape_acc"],
                item["postprocess"]["shape_acc"],
                item["current"]["shape_acc"],
                item["raw"]["text_acc"],
                item["postprocess"]["text_acc"],
                item["current"]["text_acc"],
                item["detect_time_sec"],
                item["table_time_sec"],
                item["postprocess_time_sec"],
                item["total_time_sec"],
            ])

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
