"""
Archived UVDoc dewarp code removed from the main application.

This file is kept only as a reference copy under temp. It is not imported by
ScanIndex and should not be wired back into the runtime unless dewarp support
is intentionally restored.
"""

import os
from functools import lru_cache
from typing import Optional, Tuple

import cv2
import numpy as np


_UVDOC_MODEL_PATH = os.path.join("models", "uvdoc", "uvdoc_fp32.onnx")
_UVDOC_INPUT_W = 488
_UVDOC_INPUT_H = 712
_UVDOC_MIN_PAGE_AREA_RATIO = 0.18
_UVDOC_MAX_PAGE_AREA_RATIO = 0.88
_UVDOC_MAX_SIDE_COVERAGE = 0.95
_UVDOC_MIN_CONTOUR_CONFIDENCE = 0.04
_UVDOC_FULL_PAGE_IMAGE_COVERAGE = 0.98
_UVDOC_FLAT_SCAN_BORDER_WHITE_RATIO = 0.75
_UVDOC_FLAT_SCAN_BORDER_LIGHT_RATIO = 0.85
_UVDOC_FLAT_SCAN_BORDER_DARK_RATIO = 0.18


@lru_cache(maxsize=1)
def _get_uvdoc_session():
    import onnxruntime as ort

    if not os.path.exists(_UVDOC_MODEL_PATH):
        return None
    sess_opts = ort.SessionOptions()
    sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess_opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
    return ort.InferenceSession(
        _UVDOC_MODEL_PATH,
        sess_options=sess_opts,
        providers=["CPUExecutionProvider"],
    )


def _uvdoc_enabled(config_value: Optional[bool] = None) -> bool:
    if config_value is not None:
        return bool(config_value)
    value = os.environ.get("OCRTOOL_UVDOC_DEWARP", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _flat_full_page_scan_guard(
    image: np.ndarray,
    dominant_coverage: Optional[float],
    *,
    resize_for_orientation_analysis,
    orientation_guard_max_side: int,
) -> Tuple[bool, dict]:
    meta = {
        "dominant_coverage": round(float(dominant_coverage), 4)
        if dominant_coverage is not None else None,
        "border_white_ratio": None,
        "border_light_ratio": None,
        "border_dark_ratio": None,
    }
    if dominant_coverage is None or dominant_coverage < _UVDOC_FULL_PAGE_IMAGE_COVERAGE:
        return False, meta

    h, w = image.shape[:2]
    if h < 80 or w < 80:
        return False, meta

    sample = resize_for_orientation_analysis(image, max_side=orientation_guard_max_side)
    sh, sw = sample.shape[:2]
    border = max(2, int(round(min(sh, sw) * 0.04)))
    mask = np.zeros((sh, sw), dtype=bool)
    mask[:border, :] = True
    mask[-border:, :] = True
    mask[:, :border] = True
    mask[:, -border:] = True

    if len(sample.shape) == 3:
        hsv = cv2.cvtColor(sample, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
        saturation = hsv[:, :, 1]
    else:
        gray = sample
        saturation = np.zeros_like(gray)

    border_gray = gray[mask]
    border_sat = saturation[mask]
    if border_gray.size == 0:
        return False, meta

    white_ratio = float(np.mean((border_gray > 235) & (border_sat < 35)))
    light_ratio = float(np.mean(border_gray > 220))
    dark_ratio = float(np.mean(border_gray < 80))
    meta["border_white_ratio"] = round(white_ratio, 4)
    meta["border_light_ratio"] = round(light_ratio, 4)
    meta["border_dark_ratio"] = round(dark_ratio, 4)

    is_flat_scan = (
        white_ratio >= _UVDOC_FLAT_SCAN_BORDER_WHITE_RATIO
        or (
            light_ratio >= _UVDOC_FLAT_SCAN_BORDER_LIGHT_RATIO
            and dark_ratio <= _UVDOC_FLAT_SCAN_BORDER_DARK_RATIO
        )
    )
    return bool(is_flat_scan), meta


def _largest_document_quad(image: np.ndarray, *, resize_for_orientation_analysis) -> Optional[np.ndarray]:
    h, w = image.shape[:2]
    if h < 40 or w < 40:
        return None
    sample = resize_for_orientation_analysis(image)
    sh, sw = sample.shape[:2]
    gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY) if len(sample.shape) == 3 else sample
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 40, 140)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    page_area = float(sh * sw)
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:8]:
        area = float(cv2.contourArea(contour))
        if area < page_area * _UVDOC_MIN_PAGE_AREA_RATIO:
            continue
        peri = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.025 * peri, True)
        if len(approx) != 4:
            continue
        quad = approx.reshape(4, 2).astype(np.float32)
        x, y, bw, bh = cv2.boundingRect(quad.astype(np.int32))
        confidence = area / max(1.0, float(bw * bh))
        if confidence < _UVDOC_MIN_CONTOUR_CONFIDENCE:
            continue
        quad[:, 0] *= w / float(sw)
        quad[:, 1] *= h / float(sh)
        return quad
    return None


def _apply_uvdoc_dewarp(image: np.ndarray) -> np.ndarray:
    session = _get_uvdoc_session()
    if session is None:
        return image
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB) if len(image.shape) == 3 else cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    inp = cv2.resize(rgb.astype(np.float32) / 255.0, (_UVDOC_INPUT_W, _UVDOC_INPUT_H), interpolation=cv2.INTER_LINEAR)
    inp = inp.transpose(2, 0, 1)[None].astype(np.float32)
    grid = session.run(None, {session.get_inputs()[0].name: inp})[0][0].transpose(1, 2, 0).astype(np.float32)
    h, w = image.shape[:2]
    grid = cv2.resize(grid, (w, h), interpolation=cv2.INTER_LINEAR)
    map_x = ((grid[:, :, 0] + 1.0) * 0.5 * (w - 1)).astype(np.float32)
    map_y = ((grid[:, :, 1] + 1.0) * 0.5 * (h - 1)).astype(np.float32)
    return cv2.remap(
        image, map_x, map_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
