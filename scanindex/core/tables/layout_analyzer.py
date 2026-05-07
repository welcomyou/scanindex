"""
ONNX runtime layout analysis for DocLayout-YOLO.

The portable runtime is intentionally PyTorch-free. Conversion from the
original `.pt` checkpoint is handled by
train-convert/doclayoutyolo/convert/export_doclayout_yolo_to_onnx.py
in a dev environment; this module only loads the exported ONNX graph.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_analyzer_instance = None
_doclaynet_analyzer_instance = None
_init_lock = threading.Lock()
_doclaynet_init_lock = threading.Lock()

_DEFAULT_NAMES = {
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


def _base_dir() -> Path:
    try:
        from scanindex.infra.paths import get_base_dir
        return Path(get_base_dir())
    except Exception:
        return Path(__file__).resolve().parents[3]


def _find_onnx_model_path() -> Path | None:
    base = _base_dir()
    candidates = [
        base / "models" / "doclayout_yolo_onnx_dynamic" / "doclayout_yolo_docstructbench_imgsz1024_dynamic.onnx",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _find_doclaynet_onnx_model_path() -> Path | None:
    """Find the auxiliary DocLayNet model used for non-table regions."""
    base = _base_dir()
    doclaynet_dir = base / "models" / "doclayout_yolo_doclaynet_onnx_dynamic"
    candidates = [
        doclaynet_dir / "doclayout_yolo_doclaynet_imgsz1120_from_scratch_dynamic.onnx",
    ]

    for path in candidates:
        if path.exists():
            return path
    return None


def _load_names(model_path: Path) -> dict[int, str]:
    candidates = [
        model_path.with_suffix(".names.json"),
        model_path.parent / "doclayout_yolo_docstructbench_imgsz1024.names.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {int(k): str(v) for k, v in raw.items()}
        except Exception as e:
            logger.warning("Failed to read DocLayout names %s: %s", path, e)
    return dict(_DEFAULT_NAMES)


def is_available() -> bool:
    """Return True only when the ONNX runtime path is usable."""
    if _find_onnx_model_path() is None:
        return False
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def is_doclaynet_available() -> bool:
    """Return True when the auxiliary DocLayNet ONNX model is usable."""
    if _find_doclaynet_onnx_model_path() is None:
        return False
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def get_analyzer():
    """Get or create singleton LayoutAnalyzer (thread-safe, lazy-loaded)."""
    global _analyzer_instance
    if _analyzer_instance is not None:
        return _analyzer_instance
    with _init_lock:
        if _analyzer_instance is not None:
            return _analyzer_instance
        model_path = _find_onnx_model_path()
        if not model_path:
            return None
        try:
            _analyzer_instance = LayoutAnalyzer(model_path)
            return _analyzer_instance
        except Exception as e:
            logger.warning("Failed to initialize ONNX LayoutAnalyzer: %s", e)
            return None


def get_doclaynet_analyzer():
    """Get or create the auxiliary DocLayNet analyzer for semantic regions."""
    global _doclaynet_analyzer_instance
    if _doclaynet_analyzer_instance is not None:
        return _doclaynet_analyzer_instance
    with _doclaynet_init_lock:
        if _doclaynet_analyzer_instance is not None:
            return _doclaynet_analyzer_instance
        model_path = _find_doclaynet_onnx_model_path()
        if not model_path:
            return None
        try:
            _doclaynet_analyzer_instance = LayoutAnalyzer(model_path)
            return _doclaynet_analyzer_instance
        except Exception as e:
            logger.warning("Failed to initialize DocLayNet ONNX LayoutAnalyzer: %s", e)
            return None


def merge_auxiliary_layout_regions(primary_regions, auxiliary_regions):
    """
    Merge DocLayNet semantic regions without changing table detection.

    Table extraction in the DOCX pipeline relies on DocStructBench regions with
    type exactly "table". DocLayNet is added only for semantic flow hints.
    """
    merged = list(primary_regions or [])
    semantic_types = {"Footnote", "Page-header", "Page-footer"}
    existing = {
        (
            str(region.get("type", "")),
            tuple(round(float(v), 1) for v in region.get("bbox", [])[:4]),
        )
        for region in merged
        if region.get("bbox")
    }
    for region in auxiliary_regions or []:
        region_type = str(region.get("type", ""))
        if region_type not in semantic_types:
            continue
        bbox = region.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        key = (region_type, tuple(round(float(v), 1) for v in bbox[:4]))
        if key in existing:
            continue
        item = dict(region)
        item["source"] = item.get("source", "doclaynet")
        item["id"] = f"doclaynet_{item.get('id', len(merged))}"
        merged.append(item)
        existing.add(key)
    return merged


class LayoutAnalyzer:
    def __init__(self, model_path: str | os.PathLike[str] | None = None):
        import onnxruntime as ort

        self.model_path = Path(model_path) if model_path else _find_onnx_model_path()
        if not self.model_path or not self.model_path.exists():
            raise FileNotFoundError("DocLayout-YOLO ONNX model is missing")
        self.names = _load_names(self.model_path)

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        try:
            threads = int(os.environ.get("DOCLAYOUT_ONNX_THREADS", "0"))
        except ValueError:
            threads = 0
        if threads > 0:
            opts.intra_op_num_threads = threads
            opts.inter_op_num_threads = 1

        self.session = ort.InferenceSession(
            str(self.model_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name
        shape = self.session.get_inputs()[0].shape
        self.dynamic_shape = not (isinstance(shape[-2], int) and isinstance(shape[-1], int))
        self.input_height = int(shape[-2]) if isinstance(shape[-2], int) else 1024
        self.input_width = int(shape[-1]) if isinstance(shape[-1], int) else 1024
        self.stride = 32
        logger.info("DocLayout-YOLO ONNX loaded from %s", self.model_path)

    @staticmethod
    def _to_rgb_array(pil_image: Any) -> np.ndarray:
        if hasattr(pil_image, "convert"):
            return np.asarray(pil_image.convert("RGB"))
        arr = np.asarray(pil_image)
        if arr.ndim == 2:
            arr = np.repeat(arr[:, :, None], 3, axis=2)
        if arr.shape[2] == 4:
            arr = arr[:, :, :3]
        return arr.astype(np.uint8, copy=False)

    @staticmethod
    def _resize_bilinear(arr: np.ndarray, width: int, height: int) -> np.ndarray:
        try:
            import cv2
            return cv2.resize(arr, (width, height), interpolation=cv2.INTER_LINEAR)
        except Exception:
            from PIL import Image
            return np.asarray(Image.fromarray(arr).resize((width, height)))

    def _letterbox(self, arr: np.ndarray) -> tuple[np.ndarray, float, tuple[int, int]]:
        h, w = arr.shape[:2]
        target_h, target_w = self.input_height, self.input_width
        ratio = min(target_h / h, target_w / w)
        new_w, new_h = int(round(w * ratio)), int(round(h * ratio))
        resized = self._resize_bilinear(arr, new_w, new_h)
        pad_w = target_w - new_w
        pad_h = target_h - new_h
        if self.dynamic_shape:
            pad_w = int(np.mod(pad_w, self.stride))
            pad_h = int(np.mod(pad_h, self.stride))
        pad_w /= 2
        pad_h /= 2
        pad_x = int(round(pad_w - 0.1))
        pad_y = int(round(pad_h - 0.1))
        right = int(round(pad_w + 0.1))
        bottom = int(round(pad_h + 0.1))
        canvas = np.full((new_h + pad_y + bottom, new_w + pad_x + right, 3), 114, dtype=np.uint8)
        canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
        return canvas, ratio, (pad_x, pad_y)

    def analyze_page(self, pil_image, conf: float = 0.25):
        """
        Detect layout regions in a page image.

        Returns:
            list of dicts: [{type, bbox, confidence}, ...]
            bbox is [x0, y0, x1, y1] in image pixel coordinates.
        """
        arr = self._to_rgb_array(pil_image)
        src_h, src_w = arr.shape[:2]
        img, _ratio, _pad = self._letterbox(arr)
        model_h, model_w = img.shape[:2]
        gain = min(model_h / src_h, model_w / src_w)
        pad_x = round((model_w - src_w * gain) / 2 - 0.1)
        pad_y = round((model_h - src_h * gain) / 2 - 0.1)
        inp = img.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out = self.session.run(None, {self.input_name: inp})[0]

        regions = []
        for row in out[0]:
            x0, y0, x1, y1, score, cls = row.tolist()
            if score < conf:
                continue
            x0 = max(0.0, min(src_w, (x0 - pad_x) / gain))
            x1 = max(0.0, min(src_w, (x1 - pad_x) / gain))
            y0 = max(0.0, min(src_h, (y0 - pad_y) / gain))
            y1 = max(0.0, min(src_h, (y1 - pad_y) / gain))
            if x1 <= x0 or y1 <= y0:
                continue
            regions.append({
                "type": self.names.get(int(cls), str(int(cls))),
                "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
                "confidence": round(float(score), 4),
            })
        return regions


def match_lines_to_regions(lines, layout_regions, scale_x, scale_y):
    """
    Assign a semantic_type to each TextLine based on overlap with layout regions.

    Args:
        lines: list of TextLine (coordinates in PDF points)
        layout_regions: list of {type, bbox} (bbox in image pixels)
        scale_x, scale_y: PDF points / image pixel (e.g. 72/200 = 0.36)

    Modifies lines in-place: sets line.semantic_type.
    """
    if not layout_regions:
        return

    priority = {
        "footnote": 100,
        "table_footnote": 95,
        "page-footer": 90,
        "page-header": 85,
        "table": 80,
        "title": 70,
        "section-header": 68,
        "caption": 65,
        "table_caption": 65,
        "figure_caption": 65,
    }

    for line in lines:
        lx0 = line.x / scale_x
        ly0 = line.y / scale_y
        lx1 = (line.x + line.width) / scale_x
        ly1 = (line.y + line.height) / scale_y
        line_area = max((lx1 - lx0) * (ly1 - ly0), 1)

        best_type = ""
        best_overlap_ratio = 0.0
        best_priority = -1

        for region in layout_regions:
            rx0, ry0, rx1, ry1 = region["bbox"]
            ix0 = max(lx0, rx0)
            iy0 = max(ly0, ry0)
            ix1 = min(lx1, rx1)
            iy1 = min(ly1, ry1)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            overlap = (ix1 - ix0) * (iy1 - iy0)
            overlap_ratio = overlap / line_area
            region_type = str(region.get("type", ""))
            region_priority = priority.get(region_type.lower(), 0)
            if (
                overlap_ratio > best_overlap_ratio
                or (
                    abs(overlap_ratio - best_overlap_ratio) <= 0.03
                    and region_priority > best_priority
                )
            ):
                best_overlap_ratio = overlap_ratio
                best_priority = region_priority
                best_type = region_type

        if best_overlap_ratio > 0.3:
            line.semantic_type = best_type
