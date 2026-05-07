"""PyTorch-free TATR/GMFT-style table detector and structure formatter.

This is not a wrapper around the `gmft` Python package because gmft imports
torch at module import time. It uses the same Microsoft Table Transformer
detector/structure models exported to ONNX, then performs lightweight geometry
assembly and text assignment from the PDF text layer/OCR positions.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import fitz
import numpy as np


@dataclass
class OnnxTableRegion:
    page: int
    y_top: float
    y_bottom: float
    cells: List[List[str]]
    row_count: int
    col_count: int
    cell_bboxes: List[List[Tuple[float, float, float, float]]] = field(default_factory=list)
    x_left: float = 0.0
    x_right: float = 0.0
    source: str = "gmft_onnx"


def _base_dir() -> Path:
    try:
        from scanindex.infra.paths import get_base_dir
        return Path(get_base_dir())
    except Exception:
        return Path(__file__).resolve().parent


def _model_dir(kind: str) -> Path:
    return _base_dir() / "models" / "gmft_onnx" / kind


def is_gmft_onnx_available() -> bool:
    if not (_model_dir("detection") / "model.onnx").exists():
        return False
    if not (_model_dir("structure") / "model.onnx").exists():
        return False
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _softmax(x: np.ndarray) -> np.ndarray:
    x = x - np.max(x, axis=-1, keepdims=True)
    ex = np.exp(x)
    return ex / np.sum(ex, axis=-1, keepdims=True)


def _cxcywh_to_xyxy(boxes: np.ndarray) -> np.ndarray:
    cx, cy, w, h = np.split(boxes, 4, axis=-1)
    return np.concatenate([cx - 0.5 * w, cy - 0.5 * h, cx + 0.5 * w, cy + 0.5 * h], axis=-1)


def _resize_rgb(arr: np.ndarray, width: int, height: int) -> np.ndarray:
    from PIL import Image

    try:
        resample = Image.Resampling.BILINEAR
    except AttributeError:
        resample = Image.BILINEAR
    return np.asarray(Image.fromarray(arr).resize((width, height), resample=resample))


class _TatrOnnxModel:
    def __init__(self, kind: str):
        import onnxruntime as ort

        self.kind = kind
        self.dir = _model_dir(kind)
        model_path = self.dir / "model.onnx"
        if not model_path.exists():
            raise FileNotFoundError(model_path)
        cfg = json.loads((self.dir / "config.json").read_text(encoding="utf-8"))
        self.id2label = {int(k): str(v) for k, v in cfg.get("id2label", {}).items()}

        opts = ort.SessionOptions()
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        try:
            threads = int(os.environ.get("GMFT_ONNX_THREADS", "0"))
        except ValueError:
            threads = 0
        if threads > 0:
            opts.intra_op_num_threads = threads
            opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

    @staticmethod
    def _preprocess(image) -> tuple[np.ndarray, np.ndarray]:
        arr = np.asarray(image.convert("RGB"))
        h, w = arr.shape[:2]
        scale = 800.0 / max(h, w)
        new_h, new_w = int(round(h * scale)), int(round(w * scale))
        arr = _resize_rgb(arr, new_w, new_h).astype(np.float32) / 255.0
        mean = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
        arr = (arr - mean) / std
        pixel_values = arr.transpose(2, 0, 1)[None].astype(np.float32)
        pixel_mask = np.ones((1, new_h, new_w), dtype=np.int64)
        return pixel_values, pixel_mask

    def predict(self, image, threshold: float) -> list[dict]:
        h, w = image.size[1], image.size[0]
        pixel_values, pixel_mask = self._preprocess(image)
        logits, pred_boxes = self.session.run(
            ["logits", "pred_boxes"],
            {"pixel_values": pixel_values, "pixel_mask": pixel_mask},
        )
        probs = _softmax(logits)[0, :, :-1]
        scores = probs.max(axis=-1)
        labels = probs.argmax(axis=-1)
        boxes = _cxcywh_to_xyxy(pred_boxes[0]) * np.asarray([w, h, w, h], dtype=np.float32)
        out: list[dict] = []
        for score, label, bbox in zip(scores, labels, boxes):
            if float(score) < threshold:
                continue
            x0, y0, x1, y1 = [float(v) for v in bbox.tolist()]
            if x1 <= x0 or y1 <= y0:
                continue
            out.append({
                "label": self.id2label.get(int(label), str(int(label))),
                "confidence": float(score),
                "bbox": [x0, y0, x1, y1],
            })
        return out


_detector: _TatrOnnxModel | None = None
_structor: _TatrOnnxModel | None = None

GMFT_DETECTOR_BASE_THRESHOLD = 0.9
GMFT_FORMATTER_BASE_THRESHOLD = 0.3


def _models() -> tuple[_TatrOnnxModel, _TatrOnnxModel]:
    global _detector, _structor
    if _detector is None:
        _detector = _TatrOnnxModel("detection")
    if _structor is None:
        _structor = _TatrOnnxModel("structure")
    return _detector, _structor


def _structure_model() -> _TatrOnnxModel:
    global _structor
    if _structor is None:
        _structor = _TatrOnnxModel("structure")
    return _structor


def _nms_1d(items: list[dict], axis: str, threshold: float = 0.55) -> list[dict]:
    if axis == "y":
        start, end = 1, 3
    else:
        start, end = 0, 2
    items = sorted(items, key=lambda r: float(r["confidence"]), reverse=True)
    kept: list[dict] = []
    for item in items:
        a0, a1 = item["bbox"][start], item["bbox"][end]
        alen = max(1.0, a1 - a0)
        duplicate = False
        for prev in kept:
            b0, b1 = prev["bbox"][start], prev["bbox"][end]
            inter = max(0.0, min(a1, b1) - max(a0, b0))
            if inter / min(alen, max(1.0, b1 - b0)) >= threshold:
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
    return sorted(kept, key=lambda r: r["bbox"][start])


def _line_entries_from_pdf_lines(pdf_lines: Iterable, page_num: int) -> list[tuple[float, float, float, float, str]]:
    entries = []
    for line in pdf_lines or []:
        if getattr(line, "page", None) != page_num:
            continue
        text = (getattr(line, "text", "") or "").strip()
        if not text:
            continue
        x = float(getattr(line, "x", 0.0))
        y = float(getattr(line, "y", 0.0))
        w = float(getattr(line, "width", 0.0))
        h = float(getattr(line, "height", 0.0))
        entries.append((x, y, x + w, y + h, text))
    return entries


def _word_entries_from_page(page: fitz.Page) -> list[tuple[float, float, float, float, str]]:
    entries = []
    for w in page.get_text("words"):
        text = (w[4] or "").strip()
        if text:
            entries.append((float(w[0]), float(w[1]), float(w[2]), float(w[3]), text))
    return entries


def _text_for_cell(entries: Sequence[tuple[float, float, float, float, str]],
                   bbox: tuple[float, float, float, float]) -> str:
    x0, y0, x1, y1 = bbox
    hits = []
    for ex0, ey0, ex1, ey1, text in entries:
        cx = (ex0 + ex1) / 2.0
        cy = (ey0 + ey1) / 2.0
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            hits.append((ey0, ex0, text))
    hits.sort(key=lambda t: (round(t[0], 1), t[1]))
    return " ".join(t[2] for t in hits).strip()


def _crop_page(page: fitz.Page, bbox: Sequence[float], dpi: int = 144):
    from PIL import Image, ImageOps

    rect = fitz.Rect(
        max(0.0, float(bbox[0])),
        max(0.0, float(bbox[1])),
        min(page.rect.width, float(bbox[2])),
        min(page.rect.height, float(bbox[3])),
    )
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=rect, alpha=False)
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    pad = int(max(image.size) * 0.1)
    padding = (pad, pad, pad, pad)
    if pad > 0:
        image = ImageOps.expand(image, padding, fill="white")
    return image, rect, dpi / 72, padding


def _line_positions(mask: np.ndarray, axis: int, min_fraction: float, gap: int = 3) -> list[int]:
    proj = mask.sum(axis=axis) / 255.0
    line_len = mask.shape[1 - axis]
    idx = np.where(proj > line_len * min_fraction)[0]
    groups: list[list[int]] = []
    for i in idx:
        if not groups or i - groups[-1][-1] > gap:
            groups.append([int(i)])
        else:
            groups[-1].append(int(i))
    return [int(round(sum(g) / len(g))) for g in groups if g]


def _grid_lines_from_image(image) -> tuple[list[int], list[int]]:
    """Detect ruled-table grid lines in crop pixel coordinates."""
    try:
        import cv2
    except Exception:
        return [], []
    arr = np.asarray(image.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    h, w = binary.shape[:2]
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 30), 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, h // 20)))
    h_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel, iterations=1)
    v_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel, iterations=1)
    ys = _line_positions(h_lines, axis=1, min_fraction=0.25)
    xs = _line_positions(v_lines, axis=0, min_fraction=0.10)
    return xs, ys


def _layout_table_bboxes(
    layout_regions_by_page: Optional[Dict[int, List[dict]]],
    page_num: int,
) -> list[tuple[float, float, float, float]]:
    out: list[tuple[float, float, float, float]] = []
    for region in (layout_regions_by_page or {}).get(page_num, []):
        if region.get("type") != "table":
            continue
        bbox = region.get("bbox_pdf")
        if bbox and len(bbox) >= 4:
            x0, y0, x1, y1 = [float(v) for v in bbox[:4]]
            if x1 > x0 and y1 > y0:
                out.append((x0, y0, x1, y1))
    out.sort(key=lambda b: (b[1], b[0]))
    return out


def _structure_table_from_bbox(
    page: fitz.Page,
    page_num: int,
    bbox: Sequence[float],
    table_idx: int,
    structor: _TatrOnnxModel,
    text_entries: Sequence[tuple[float, float, float, float, str]],
    logger,
    source_label: str,
) -> OnnxTableRegion | None:
    crop_img, crop_rect, crop_scale, crop_padding = _crop_page(page, bbox)
    pad_left, pad_top = crop_padding[0], crop_padding[1]
    preds = structor.predict(crop_img, threshold=GMFT_FORMATTER_BASE_THRESHOLD)
    rows = _nms_1d(
        [p for p in preds if p["label"] in {"table row", "table projected row header"}],
        "y",
    )
    cols = _nms_1d([p for p in preds if p["label"] == "table column"], "x")

    grid_xs, grid_ys = _grid_lines_from_image(crop_img)
    use_grid = (
        len(grid_xs) >= 3
        and len(grid_ys) >= 3
        and (len(grid_xs) - 1) >= max(1, len(cols))
        and (len(grid_ys) - 1) >= max(1, len(rows))
    )
    if use_grid:
        row_intervals = [(float(grid_ys[i]), float(grid_ys[i + 1])) for i in range(len(grid_ys) - 1)]
        col_intervals = [(float(grid_xs[i]), float(grid_xs[i + 1])) for i in range(len(grid_xs) - 1)]
        logger.log(
            f"    Table {table_idx}: grid refinement {len(row_intervals)}x{len(col_intervals)}"
        )
    else:
        row_intervals = [(float(r["bbox"][1]), float(r["bbox"][3])) for r in rows]
        col_intervals = [(float(c["bbox"][0]), float(c["bbox"][2])) for c in cols]

    if not row_intervals or not col_intervals:
        logger.log(f"    Table {table_idx}: no row/column structure, skipping")
        return None

    cell_bboxes: list[list[tuple[float, float, float, float]]] = []
    cells: list[list[str]] = []
    for ry0, ry1 in row_intervals:
        row_cells = []
        row_boxes = []
        for cx0, cx1 in col_intervals:
            pdf_bbox = (
                crop_rect.x0 + (cx0 - pad_left) / crop_scale,
                crop_rect.y0 + (ry0 - pad_top) / crop_scale,
                crop_rect.x0 + (cx1 - pad_left) / crop_scale,
                crop_rect.y0 + (ry1 - pad_top) / crop_scale,
            )
            row_boxes.append(pdf_bbox)
            row_cells.append(_text_for_cell(text_entries, pdf_bbox))
        cell_bboxes.append(row_boxes)
        cells.append(row_cells)

    x_left, y_top, x_right, y_bottom = [float(v) for v in bbox[:4]]
    logger.log(
        f"    Table {table_idx}: {len(cells)}x{len(cells[0]) if cells else 0} "
        f"@ y={y_top:.1f}-{y_bottom:.1f} [{source_label}]"
    )
    return OnnxTableRegion(
        page=page_num,
        x_left=x_left,
        x_right=x_right,
        y_top=y_top,
        y_bottom=y_bottom,
        cells=cells,
        row_count=len(cells),
        col_count=len(cells[0]) if cells else 0,
        cell_bboxes=cell_bboxes,
    )


def detect_tables_gmft_onnx(pdf_path: str, logger, page_info: dict, pdf_lines: list,
                            device: str = "cpu") -> List[OnnxTableRegion]:
    del device
    if not is_gmft_onnx_available():
        logger.log("gmft_onnx not available, skipping table detection")
        return []

    detector, structor = _models()
    tables: list[OnnxTableRegion] = []
    doc = fitz.open(pdf_path)
    try:
        for page_idx, page in enumerate(doc):
            page_num = page_idx + 1
            pix = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
            from PIL import Image
            page_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            detections = [
                d for d in detector.predict(page_img, threshold=GMFT_DETECTOR_BASE_THRESHOLD)
                if d["label"] in {"table", "table rotated"}
            ]
            logger.log(f"  gmft_onnx: Page {page_num} - {len(detections)} tables detected")
            text_entries = _line_entries_from_pdf_lines(pdf_lines, page_num) or _word_entries_from_page(page)

            for t_idx, det in enumerate(detections):
                region = _structure_table_from_bbox(
                    page,
                    page_num,
                    det["bbox"],
                    t_idx + 1,
                    structor,
                    text_entries,
                    logger,
                    "GMFT-ONNX",
                )
                if region is not None:
                    tables.append(region)
    finally:
        doc.close()
    logger.log(f"gmft_onnx: Total {len(tables)} tables extracted")
    return tables


def detect_tables_gmft_onnx_on_layout_regions(
    pdf_path: str,
    logger,
    page_info: dict,
    pdf_lines: list,
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None,
    device: str = "cpu",
) -> List[OnnxTableRegion]:
    del device, page_info
    if not layout_regions_by_page:
        return []
    if not is_gmft_onnx_available():
        logger.log("gmft_onnx not available, skipping layout-anchored structure recognition")
        return []

    structor = _structure_model()
    tables: list[OnnxTableRegion] = []
    doc = fitz.open(pdf_path)
    try:
        for page_idx, page in enumerate(doc):
            page_num = page_idx + 1
            layout_bboxes = _layout_table_bboxes(layout_regions_by_page, page_num)
            if not layout_bboxes:
                continue
            logger.log(f"  gmft_onnx/layout: Page {page_num} - {len(layout_bboxes)} DocLayout table boxes")
            text_entries = _line_entries_from_pdf_lines(pdf_lines, page_num) or _word_entries_from_page(page)
            for t_idx, bbox in enumerate(layout_bboxes, 1):
                try:
                    region = _structure_table_from_bbox(
                        page,
                        page_num,
                        bbox,
                        t_idx,
                        structor,
                        text_entries,
                        logger,
                        "GMFT-ONNX/LAYOUT",
                    )
                    if region is not None:
                        region.source = "gmft_onnx_layout"
                        tables.append(region)
                except Exception as exc:
                    logger.log(f"    Table {t_idx}: gmft_onnx/layout failed: {exc}")
    finally:
        doc.close()
    logger.log(f"gmft_onnx/layout: Total {len(tables)} tables extracted")
    return tables
