from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import benchmark_borderless_ctdar_current as ctdar_bench  # noqa: E402
import benchmark_current_table_pipeline as gt_bench  # noqa: E402
from benchmark_external_table_gt_samples import load_samples  # noqa: E402


BBox = tuple[float, float, float, float]


@dataclass
class DetectionCase:
    dataset: str
    case_id: str
    image_path: Path | None
    pdf_path: Path | None
    gt_bboxes: list[BBox]
    gt_count: int


def bbox_iou(a: BBox, b: BBox) -> float:
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


def nms_detections(detections: list[dict[str, Any]], iou_threshold: float) -> list[dict[str, Any]]:
    if iou_threshold >= 1.0 or len(detections) <= 1:
        return detections
    ordered = sorted(
        detections,
        key=lambda item: float(item.get("confidence", item.get("score", 0.0))),
        reverse=True,
    )
    kept: list[dict[str, Any]] = []
    for det in ordered:
        bbox = tuple(float(v) for v in det["bbox"][:4])
        if all(bbox_iou(bbox, tuple(float(v) for v in other["bbox"][:4])) < iou_threshold for other in kept):
            kept.append(det)
    return kept


def bbox_union(boxes: list[BBox]) -> BBox | None:
    valid = [box for box in boxes if box[2] > box[0] and box[3] > box[1]]
    if not valid:
        return None
    return (
        min(box[0] for box in valid),
        min(box[1] for box in valid),
        max(box[2] for box in valid),
        max(box[3] for box in valid),
    )


def evaluate_detections(gt_bboxes: list[BBox], pred_bboxes: list[BBox], iou_threshold: float = 0.5) -> dict[str, Any]:
    used_pred: set[int] = set()
    best_ious: list[float] = []
    matched = 0
    for gt in gt_bboxes:
        best_idx = -1
        best_iou = 0.0
        for idx, pred in enumerate(pred_bboxes):
            if idx in used_pred:
                continue
            iou = bbox_iou(gt, pred)
            if iou > best_iou:
                best_iou = iou
                best_idx = idx
        best_ious.append(best_iou)
        if best_idx >= 0 and best_iou >= iou_threshold:
            used_pred.add(best_idx)
            matched += 1
    recall = matched / len(gt_bboxes) if gt_bboxes else 1.0
    precision = matched / len(pred_bboxes) if pred_bboxes else (1.0 if not gt_bboxes else 0.0)
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall > 0 else 0.0
    return {
        "gt_count": len(gt_bboxes),
        "pred_count": len(pred_bboxes),
        "matched_iou50": matched,
        "recall_iou50": recall,
        "precision_iou50": precision,
        "f1_iou50": f1,
        "mean_best_iou": sum(best_ious) / len(best_ious) if best_ious else 1.0,
        "best_ious": best_ious,
    }


def render_pdf_page(pdf_path: Path, page_idx: int, dpi: int) -> Image.Image:
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_idx]
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
        return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    finally:
        doc.close()


def render_pdf_page_count(pdf_path: Path) -> int:
    doc = fitz.open(str(pdf_path))
    try:
        return len(doc)
    finally:
        doc.close()


def scale_pdf_bbox_to_image(bbox: BBox, dpi: int) -> BBox:
    scale = dpi / 72.0
    return tuple(float(v) * scale for v in bbox)  # type: ignore[return-value]


def gt_bboxes_from_external_gt(gt_path: Path, image_size: tuple[int, int]) -> list[BBox]:
    data = json.loads(gt_path.read_text(encoding="utf-8"))
    table_bbox = data.get("table_bbox")
    if isinstance(table_bbox, list) and len(table_bbox) >= 4:
        return [tuple(float(v) for v in table_bbox[:4])]  # type: ignore[list-item]

    boxes = data.get("boxes") or []
    category_ids = data.get("category_ids") or []
    table_boxes = [
        tuple(float(v) for v in box[:4])
        for cat, box in zip(category_ids, boxes)
        if int(cat) == 1 and len(box) >= 4
    ]
    if table_boxes:
        return table_boxes  # type: ignore[return-value]

    cell_boxes: list[BBox] = []
    raw_cells = data.get("cells") or []
    if raw_cells and isinstance(raw_cells, list) and isinstance(raw_cells[0], list):
        raw_cells = raw_cells[0]
    for cell in raw_cells:
        if not isinstance(cell, dict):
            continue
        bbox = cell.get("bbox") or cell.get("pdf_bbox") or []
        if len(bbox) >= 4:
            cell_boxes.append(tuple(float(v) for v in bbox[:4]))  # type: ignore[arg-type]
    union = bbox_union(cell_boxes)
    if union is not None:
        return [union]

    # PubTabNet samples in this local manifest are already cropped table images.
    width, height = image_size
    return [(0.0, 0.0, float(width), float(height))]


def load_external_cases(args: argparse.Namespace) -> list[DetectionCase]:
    samples = load_samples(args.manifest, {item.lower() for item in args.datasets})
    if args.max_cases_per_dataset > 0:
        limited: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for sample in samples:
            dataset = str(sample["dataset"])
            counts[dataset] = counts.get(dataset, 0) + 1
            if counts[dataset] <= args.max_cases_per_dataset:
                limited.append(sample)
        samples = limited
    cases: list[DetectionCase] = []
    for sample in samples:
        image_path = Path(sample["image"])
        with Image.open(image_path) as im:
            gt_bboxes = gt_bboxes_from_external_gt(Path(sample["groundtruth"]), im.size)
        cases.append(
            DetectionCase(
                dataset=f"external:{sample['dataset']}",
                case_id=str(sample["id"]),
                image_path=image_path,
                pdf_path=None,
                gt_bboxes=gt_bboxes,
                gt_count=len(gt_bboxes),
            )
        )
    return cases


def load_ctdar_cases(args: argparse.Namespace) -> list[DetectionCase]:
    selected = ctdar_bench.collect_candidates(
        args.ctdar_root,
        args.track,
        args.modern_only,
        args.ctdar_pdf_dpi,
        args.selection,
    )[: args.max_ctdar_cases]
    cases: list[DetectionCase] = []
    for item in selected:
        gt_tables = ctdar_bench.parse_ctdar_xml(Path(item["xml"]), 1.0)
        cases.append(
            DetectionCase(
                dataset=f"cTDaR:{args.track}",
                case_id=str(item["id"]),
                image_path=Path(item["image"]),
                pdf_path=None,
                gt_bboxes=[tuple(t.bbox_px) for t in gt_tables],
                gt_count=len(gt_tables),
            )
        )
    return cases


def load_groundtruth_count_cases(args: argparse.Namespace) -> list[DetectionCase]:
    cases: list[DetectionCase] = []
    scans = [str(scan).zfill(2) for scan in args.scans]
    for scan in scans:
        pdf_path = args.ocr_dir / f"scan{scan}_ocr.pdf"
        gt_docx = gt_bench.groundtruth_docx_for_scan(scan, args.groundtruth_dir, args.gt03_docx)
        gt_count = len(gt_bench.data_tables(gt_bench.docx_tables(gt_docx)))
        cases.append(
            DetectionCase(
                dataset="5_groundtruth_count_only",
                case_id=f"scan{scan}",
                image_path=None,
                pdf_path=pdf_path,
                gt_bboxes=[],
                gt_count=gt_count,
            )
        )
    return cases


class DetectorRunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._doclayout = None
        self._docling = None
        self._gmft_detector = None
        self.init_times: dict[str, float] = {}

    def _init_doclayout(self):
        if self._doclayout is None:
            t0 = time.perf_counter()
            from scanindex.core.tables.layout_analyzer import get_analyzer

            self._doclayout = get_analyzer()
            self.init_times["doclayout"] = time.perf_counter() - t0
        return self._doclayout

    def _init_docling(self):
        if self._docling is None:
            t0 = time.perf_counter()
            from docling_ibm_models.layoutmodel.layout_predictor import LayoutPredictor

            artifact = self._docling_layout_artifact()
            self._docling = LayoutPredictor(
                str(artifact),
                device="cpu",
                num_threads=self.args.docling_threads,
                base_threshold=self.args.docling_conf,
            )
            self.init_times["docling"] = time.perf_counter() - t0
        return self._docling

    def _docling_layout_artifact(self) -> Path:
        if self.args.docling_layout_dir:
            return Path(self.args.docling_layout_dir)
        root = Path.home() / ".cache" / "huggingface" / "hub" / "models--docling-project--docling-layout-heron" / "snapshots"
        candidates = [path.parent for path in root.glob("*/model.safetensors")]
        if candidates:
            return candidates[0]
        from huggingface_hub import snapshot_download

        return Path(snapshot_download("docling-project/docling-layout-heron"))

    def _init_gmft(self):
        if self._gmft_detector is None:
            t0 = time.perf_counter()
            from scanindex.core.tables.gmft_onnx_table_engine import _models

            self._gmft_detector = _models()[0]
            self.init_times["gmft"] = time.perf_counter() - t0
        return self._gmft_detector

    def predict(self, detector_id: str, image: Image.Image) -> tuple[list[BBox], float]:
        t0 = time.perf_counter()
        if detector_id == "doclayout":
            analyzer = self._init_doclayout()
            if analyzer is None:
                return [], time.perf_counter() - t0
            regions = analyzer.analyze_page(image, conf=self.args.doclayout_conf)
            boxes = [
                tuple(float(v) for v in region["bbox"][:4])
                for region in regions
                if region.get("type") == "table"
            ]
            return boxes, time.perf_counter() - t0

        if detector_id == "docling":
            predictor = self._init_docling()
            preds = list(predictor.predict(image))
            boxes = [
                (float(p["l"]), float(p["t"]), float(p["r"]), float(p["b"]))
                for p in preds
                if str(p.get("label", "")).lower() == "table"
            ]
            return boxes, time.perf_counter() - t0

        if detector_id == "gmft":
            detector = self._init_gmft()
            preds = detector.predict(image, threshold=self.args.gmft_conf)
            preds = nms_detections(preds, self.args.gmft_nms_iou)
            boxes = [
                tuple(float(v) for v in pred["bbox"][:4])
                for pred in preds
                if str(pred.get("label", "")).lower() in {"table", "table rotated"}
            ]
            return boxes, time.perf_counter() - t0

        raise ValueError(detector_id)


def iter_case_images(case: DetectionCase, dpi: int) -> list[tuple[str, Image.Image, list[BBox], int]]:
    if case.image_path is not None:
        image = Image.open(case.image_path).convert("RGB")
        return [(case.case_id, image, case.gt_bboxes, case.gt_count)]
    if case.pdf_path is None:
        return []
    out = []
    page_count = render_pdf_page_count(case.pdf_path)
    for page_idx in range(page_count):
        out.append((f"{case.case_id}_p{page_idx + 1}", render_pdf_page(case.pdf_path, page_idx, dpi), [], case.gt_count))
    return out


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    cases: list[DetectionCase] = []
    if args.suite in {"all", "external"}:
        cases.extend(load_external_cases(args))
    if args.suite in {"all", "ctdar"}:
        cases.extend(load_ctdar_cases(args))
    if args.suite in {"all", "groundtruth"}:
        cases.extend(load_groundtruth_count_cases(args))

    runner = DetectorRunner(args)
    detector_ids = args.detectors
    per_case: list[dict[str, Any]] = []
    for idx, case in enumerate(cases, 1):
        images = iter_case_images(case, args.groundtruth_render_dpi)
        print(f"[{idx}/{len(cases)}] {case.dataset} {case.case_id} pages={len(images)}", flush=True)
        if case.pdf_path is not None and case.gt_count and not case.gt_bboxes:
            for detector_id in detector_ids:
                total_count = 0
                total_sec = 0.0
                page_pred_bboxes: list[dict[str, Any]] = []
                try:
                    for image_id, image, _gt_bboxes, _gt_count in images:
                        pred_bboxes, sec = runner.predict(detector_id, image)
                        total_count += len(pred_bboxes)
                        total_sec += sec
                        page_pred_bboxes.append({"image_id": image_id, "bboxes": pred_bboxes})
                    count_abs_error = abs(int(total_count) - int(case.gt_count))
                    per_case.append(
                        {
                            "dataset": case.dataset,
                            "case_id": case.case_id,
                            "image_id": case.case_id,
                            "detector": detector_id,
                            "gt_count": case.gt_count,
                            "pred_count": total_count,
                            "matched_iou50": 0,
                            "recall_iou50": "",
                            "precision_iou50": "",
                            "f1_iou50": "",
                            "mean_best_iou": "",
                            "count_abs_error": count_abs_error,
                            "count_exact": count_abs_error == 0,
                            "detect_sec": total_sec,
                            "pred_bboxes": page_pred_bboxes,
                            "gt_bboxes": [],
                        }
                    )
                except Exception as exc:
                    per_case.append(
                        {
                            "dataset": case.dataset,
                            "case_id": case.case_id,
                            "image_id": case.case_id,
                            "detector": detector_id,
                            "gt_count": case.gt_count,
                            "pred_count": 0,
                            "matched_iou50": 0,
                            "recall_iou50": "",
                            "precision_iou50": "",
                            "f1_iou50": "",
                            "mean_best_iou": "",
                            "count_abs_error": case.gt_count,
                            "count_exact": False,
                            "detect_sec": total_sec,
                            "error": repr(exc),
                            "pred_bboxes": page_pred_bboxes,
                            "gt_bboxes": [],
                        }
                    )
            continue
        for image_id, image, gt_bboxes, gt_count in images:
            for detector_id in detector_ids:
                try:
                    pred_bboxes, sec = runner.predict(detector_id, image)
                    if gt_bboxes:
                        cmp = evaluate_detections(gt_bboxes, pred_bboxes)
                    else:
                        cmp = {
                            "gt_count": gt_count,
                            "pred_count": len(pred_bboxes),
                            "matched_iou50": 0,
                            "recall_iou50": "",
                            "precision_iou50": "",
                            "f1_iou50": "",
                            "mean_best_iou": "",
                            "best_ious": [],
                        }
                    per_case.append(
                        {
                            "dataset": case.dataset,
                            "case_id": case.case_id,
                            "image_id": image_id,
                            "detector": detector_id,
                            "gt_count": cmp["gt_count"],
                            "pred_count": cmp["pred_count"],
                            "matched_iou50": cmp["matched_iou50"],
                            "recall_iou50": cmp["recall_iou50"],
                            "precision_iou50": cmp["precision_iou50"],
                            "f1_iou50": cmp["f1_iou50"],
                            "mean_best_iou": cmp["mean_best_iou"],
                            "count_abs_error": abs(int(cmp["pred_count"]) - int(gt_count)),
                            "count_exact": int(cmp["pred_count"]) == int(gt_count),
                            "detect_sec": sec,
                            "pred_bboxes": pred_bboxes,
                            "gt_bboxes": gt_bboxes,
                        }
                    )
                except Exception as exc:
                    per_case.append(
                        {
                            "dataset": case.dataset,
                            "case_id": case.case_id,
                            "image_id": image_id,
                            "detector": detector_id,
                            "gt_count": gt_count,
                            "pred_count": 0,
                            "matched_iou50": 0,
                            "recall_iou50": "",
                            "precision_iou50": "",
                            "f1_iou50": "",
                            "mean_best_iou": "",
                            "count_abs_error": gt_count,
                            "count_exact": False,
                            "detect_sec": 0.0,
                            "error": repr(exc),
                            "pred_bboxes": [],
                            "gt_bboxes": gt_bboxes,
                        }
                    )
    summary = summarize(per_case, runner.init_times)
    return {"per_case": per_case, "summary": summary, "init_times": runner.init_times}


def _mean_numeric(rows: list[dict[str, Any]], key: str) -> float:
    values = []
    for row in rows:
        value = row.get(key, "")
        if value == "":
            continue
        values.append(float(value))
    return sum(values) / len(values) if values else 0.0


def summarize(per_case: list[dict[str, Any]], init_times: dict[str, float]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    overall: dict[str, list[dict[str, Any]]] = {}
    for row in per_case:
        grouped.setdefault((str(row["dataset"]), str(row["detector"])), []).append(row)
        overall.setdefault(str(row["detector"]), []).append(row)

    def make(dataset: str, detector: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rows)
        bbox_rows = [row for row in rows if row.get("recall_iou50") != ""]
        return {
            "dataset": dataset,
            "detector": detector,
            "cases": n,
            "bbox_cases": len(bbox_rows),
            "gt_count": sum(int(row["gt_count"]) for row in rows),
            "pred_count": sum(int(row["pred_count"]) for row in rows),
            "recall_iou50": _mean_numeric(bbox_rows, "recall_iou50"),
            "precision_iou50": _mean_numeric(bbox_rows, "precision_iou50"),
            "f1_iou50": _mean_numeric(bbox_rows, "f1_iou50"),
            "mean_best_iou": _mean_numeric(bbox_rows, "mean_best_iou"),
            "count_exact_rate": sum(1.0 if row["count_exact"] else 0.0 for row in rows) / n if n else 0.0,
            "count_abs_error_avg": sum(float(row["count_abs_error"]) for row in rows) / n if n else 0.0,
            "detect_sec": sum(float(row["detect_sec"]) for row in rows),
            "avg_detect_sec": sum(float(row["detect_sec"]) for row in rows) / n if n else 0.0,
            "init_sec": init_times.get(detector, 0.0),
        }

    summary = [make(dataset, detector, rows) for (dataset, detector), rows in grouped.items()]
    summary.extend(make("__overall__", detector, rows) for detector, rows in overall.items())
    summary.sort(key=lambda row: (row["dataset"], row["detector"]))
    return summary


def write_outputs(result: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "details.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, rows in [("per_case.csv", result["per_case"]), ("summary.csv", result["summary"])]:
        if not rows:
            (out_dir / name).write_text("", encoding="utf-8")
            continue
        fields = list(rows[0].keys())
        with (out_dir / name).open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fields})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--suite", choices=["all", "external", "ctdar", "groundtruth"], default="all")
    parser.add_argument("--manifest", type=Path, default=ROOT / "temp" / "external" / "table_gt_samples" / "manifest.json")
    parser.add_argument("--datasets", nargs="*", default=[])
    parser.add_argument("--max-cases-per-dataset", type=int, default=10)
    parser.add_argument("--ctdar-root", type=Path, default=ROOT / "temp" / "external" / "ICDAR2019_cTDaR")
    parser.add_argument("--track", default="TRACKB2", choices=["TRACKB1", "TRACKB2"])
    parser.add_argument("--max-ctdar-cases", type=int, default=20)
    parser.add_argument("--selection", choices=["lowest", "highest"], default="highest")
    parser.add_argument("--modern-only", action="store_true", default=True)
    parser.add_argument("--include-historical", action="store_false", dest="modern_only")
    parser.add_argument("--ctdar-pdf-dpi", type=int, default=300)
    parser.add_argument("--ocr-dir", type=Path, default=ROOT / "temp" / "groundtruth5_pipeline")
    parser.add_argument("--groundtruth-dir", type=Path, default=Path(r"C:\Users\nhquan\Downloads\groundtruth"))
    parser.add_argument("--gt03-docx", type=Path, default=ROOT / "temp" / "groundtruth4_scan_word" / "groundtruth03_converted.docx")
    parser.add_argument("--scans", nargs="+", default=["01", "02", "03", "04", "05"])
    parser.add_argument("--groundtruth-render-dpi", type=int, default=144)
    parser.add_argument("--detectors", nargs="+", choices=["doclayout", "docling", "gmft"], default=["doclayout", "docling", "gmft"])
    parser.add_argument("--conf", type=float, default=None, help="Deprecated shared detector threshold.")
    parser.add_argument("--doclayout-conf", type=float, default=0.25)
    parser.add_argument("--docling-conf", type=float, default=0.25)
    parser.add_argument("--gmft-conf", type=float, default=0.9)
    parser.add_argument("--gmft-nms-iou", type=float, default=1.0)
    parser.add_argument("--docling-layout-dir", default="")
    parser.add_argument("--docling-threads", type=int, default=4)
    args = parser.parse_args()
    if args.conf is not None:
        args.doclayout_conf = args.conf
        args.docling_conf = args.conf
        args.gmft_conf = args.conf
    result = run_benchmark(args)
    write_outputs(result, args.out_dir)
    print(json.dumps({"out_dir": str(args.out_dir), "rows": len(result["per_case"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
