"""Docling TableFormer structure recognizer for detected table regions.

This adapter uses Docling's official TableFormer stage directly. It does not
detect tables by itself; table boxes come from the document-layout stage. OCR
word/line boxes from the existing pipeline are passed as Docling TextCells so
Docling's own cell matcher and matching postprocessor can run.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import fitz
import numpy as np
from PIL import Image


BBox = Tuple[float, float, float, float]


@dataclass
class DoclingTableFormerRegion:
    page: int
    y_top: float
    y_bottom: float
    cells: List[List[str]]
    row_count: int
    col_count: int
    cell_bboxes: List[List[BBox]] = field(default_factory=list)
    x_left: float = 0.0
    x_right: float = 0.0
    source: str = "docling_tableformer"


_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()


def is_docling_tableformer_available() -> bool:
    try:
        import docling  # noqa: F401
        import docling_ibm_models  # noqa: F401

        return True
    except Exception:
        return False


def _get_model(mode: str = "accurate", num_threads: int = 4):
    key = (str(mode).lower(), int(num_threads))
    with _MODEL_LOCK:
        if key in _MODEL_CACHE:
            return _MODEL_CACHE[key]

        from docling.datamodel.accelerator_options import (
            AcceleratorDevice,
            AcceleratorOptions,
        )
        from docling.datamodel.pipeline_options import (
            TableFormerMode,
            TableStructureOptions,
        )
        from docling.models.stages.table_structure.table_structure_model import (
            TableStructureModel,
        )

        tf_mode = TableFormerMode.FAST if str(mode).lower() == "fast" else TableFormerMode.ACCURATE
        options = TableStructureOptions(do_cell_matching=True, mode=tf_mode)
        accelerator = AcceleratorOptions(
            device=AcceleratorDevice.CPU,
            num_threads=max(1, int(num_threads)),
        )
        model = TableStructureModel(True, None, options, accelerator)
        _MODEL_CACHE[key] = model
        return model


def _layout_table_bboxes(
    layout_regions_by_page: Optional[Dict[int, List[dict]]],
    page: int,
) -> List[BBox]:
    out: List[BBox] = []
    for region in (layout_regions_by_page or {}).get(page, []):
        if region.get("type") != "table":
            continue
        bbox = region.get("bbox_pdf")
        if bbox and len(bbox) >= 4:
            x0, y0, x1, y1 = [float(v) for v in bbox[:4]]
            if x1 > x0 and y1 > y0:
                out.append((x0, y0, x1, y1))
    out.sort(key=lambda b: (b[1], b[0]))
    return out


def _crop_rect(page: fitz.Page, bbox: BBox, pad_pt: float) -> fitz.Rect:
    return fitz.Rect(
        max(0.0, bbox[0] - pad_pt),
        max(0.0, bbox[1] - pad_pt),
        min(page.rect.width, bbox[2] + pad_pt),
        min(page.rect.height, bbox[3] + pad_pt),
    )


def _render_crop(page: fitz.Page, bbox: BBox, dpi: int, pad_pt: float) -> tuple[Image.Image, fitz.Rect]:
    scale = dpi / 72.0
    rect = _crop_rect(page, bbox, pad_pt)
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        arr = arr[:, :, :3]
    elif pix.n == 1:
        arr = np.repeat(arr, 3, axis=2)
    return Image.fromarray(arr, "RGB"), rect


def _line_xywh(line: object) -> BBox:
    if all(hasattr(line, attr) for attr in ("x", "y", "width", "height")):
        x = float(getattr(line, "x", 0.0) or 0.0)
        y = float(getattr(line, "y", 0.0) or 0.0)
        w = float(getattr(line, "width", 0.0) or 0.0)
        h = float(getattr(line, "height", 0.0) or 0.0)
        return (x, y, x + w, y + h)
    bbox = getattr(line, "bbox", None)
    if bbox and len(bbox) >= 4:
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
        return (x0, y0, x1, y1)
    return (0.0, 0.0, 0.0, 0.0)


def _line_text(line: object) -> str:
    return str(getattr(line, "text", "") or "").strip()


def _line_score(line: object) -> float:
    for attr in ("confidence", "score", "ocr_confidence"):
        value = getattr(line, attr, None)
        if value is not None:
            try:
                return float(value)
            except Exception:
                pass
    return 1.0


def _bbox_center_in(bbox: BBox, region: BBox) -> bool:
    x0, y0, x1, y1 = bbox
    rx0, ry0, rx1, ry1 = region
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    return rx0 <= cx <= rx1 and ry0 <= cy <= ry1


def _ocr_text_cells_for_table(page_lines: List[object], table_bbox: BBox):
    from docling_core.types.doc import BoundingBox
    from docling_core.types.doc.page import BoundingRectangle, TextCell

    cells = []
    idx = 0
    for line in page_lines:
        words = [
            word
            for word in (getattr(line, "word_items", None) or [])
            if str(word.get("text", "") or "").strip()
        ]
        if words:
            for word in words:
                x = float(word.get("x", 0.0) or 0.0)
                y = float(word.get("y", 0.0) or 0.0)
                w = float(word.get("w", 0.0) or 0.0)
                h = float(word.get("h", 0.0) or 0.0)
                bbox = (x, y, x + w, y + h)
                text = str(word.get("text", "") or "").strip()
                if not text or w <= 0 or h <= 0 or not _bbox_center_in(bbox, table_bbox):
                    continue
                bb = BoundingBox(l=bbox[0], t=bbox[1], r=bbox[2], b=bbox[3])
                cells.append(
                    TextCell(
                        index=idx,
                        rect=BoundingRectangle.from_bounding_box(bb),
                        text=text,
                        orig=text,
                        from_ocr=True,
                        confidence=_line_score(line),
                    )
                )
                idx += 1
            continue

        text = _line_text(line)
        bbox = _line_xywh(line)
        if not text or bbox[2] <= bbox[0] or bbox[3] <= bbox[1] or not _bbox_center_in(bbox, table_bbox):
            continue
        bb = BoundingBox(l=bbox[0], t=bbox[1], r=bbox[2], b=bbox[3])
        cells.append(
            TextCell(
                index=idx,
                rect=BoundingRectangle.from_bounding_box(bb),
                text=text,
                orig=text,
                from_ocr=True,
                confidence=_line_score(line),
            )
        )
        idx += 1
    return cells


def _table_to_region(table, page_num: int, table_bbox: BBox) -> Optional[DoclingTableFormerRegion]:
    rows = int(getattr(table, "num_rows", 0) or 0)
    cols = int(getattr(table, "num_cols", 0) or 0)
    if rows <= 0 or cols <= 0:
        return None

    cells = [[""] * cols for _ in range(rows)]
    bboxes: List[List[BBox]] = [[(0.0, 0.0, 0.0, 0.0)] * cols for _ in range(rows)]
    for tc in getattr(table, "table_cells", []) or []:
        r0 = max(0, int(getattr(tc, "start_row_offset_idx", 0) or 0))
        r1 = max(r0 + 1, int(getattr(tc, "end_row_offset_idx", r0 + 1) or (r0 + 1)))
        c0 = max(0, int(getattr(tc, "start_col_offset_idx", 0) or 0))
        c1 = max(c0 + 1, int(getattr(tc, "end_col_offset_idx", c0 + 1) or (c0 + 1)))
        bbox = getattr(tc, "bbox", None)
        if bbox is not None:
            pdf_bbox = tuple(float(v) for v in bbox.as_tuple()[:4])
        else:
            pdf_bbox = table_bbox
        text = str(getattr(tc, "text", "") or "")

        if r0 < rows and c0 < cols:
            cells[r0][c0] = text
        for r in range(r0, min(rows, r1)):
            for c in range(c0, min(cols, c1)):
                bboxes[r][c] = pdf_bbox

    valid = [bbox for row in bboxes for bbox in row if bbox[2] > bbox[0] and bbox[3] > bbox[1]]
    if valid:
        x_left = min(b[0] for b in valid)
        x_right = max(b[2] for b in valid)
        y_top = min(b[1] for b in valid)
        y_bottom = max(b[3] for b in valid)
    else:
        x_left, y_top, x_right, y_bottom = table_bbox

    return DoclingTableFormerRegion(
        page=page_num,
        y_top=y_top,
        y_bottom=y_bottom,
        cells=cells,
        row_count=rows,
        col_count=cols,
        cell_bboxes=bboxes,
        x_left=x_left,
        x_right=x_right,
    )


def detect_tables_docling_tableformer(
    pdf_path: str,
    logger,
    page_info: dict,
    pdf_lines: List[object],
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None,
    dpi: int = 144,
    pad_pt: float = 0.0,
    mode: str = "accurate",
    num_threads: int = 4,
) -> List[DoclingTableFormerRegion]:
    if not layout_regions_by_page or not is_docling_tableformer_available():
        return []

    from docling.datamodel.base_models import Cluster
    from docling_core.types.doc import BoundingBox, DocItemLabel

    lines_by_page: Dict[int, List[object]] = {}
    for line in pdf_lines or []:
        lines_by_page.setdefault(int(getattr(line, "page", 0) or 0), []).append(line)

    model = _get_model(mode=mode, num_threads=num_threads)
    tables: List[DoclingTableFormerRegion] = []
    doc = fitz.open(pdf_path)
    try:
        for page_num in range(1, len(doc) + 1):
            page = doc[page_num - 1]
            layout_bboxes = _layout_table_bboxes(layout_regions_by_page, page_num)
            if not layout_bboxes:
                continue
            page_lines = lines_by_page.get(page_num, [])
            for idx, bbox in enumerate(layout_bboxes, 1):
                try:
                    image, crop = _render_crop(page, bbox, dpi, pad_pt)
                    cluster_bbox = (float(crop.x0), float(crop.y0), float(crop.x1), float(crop.y1))
                    text_cells = _ocr_text_cells_for_table(page_lines, cluster_bbox)
                    cluster = Cluster(
                        id=idx,
                        label=DocItemLabel.TABLE,
                        bbox=BoundingBox(
                            l=cluster_bbox[0],
                            t=cluster_bbox[1],
                            r=cluster_bbox[2],
                            b=cluster_bbox[3],
                        ),
                        cells=text_cells,
                    )
                    table = model._do_prediction_on_image_to_table(
                        table_image=image,
                        table_cluster=cluster,
                        page_no=page_num,
                    )
                    region = _table_to_region(table, page_num, cluster_bbox)
                    if region is not None:
                        region.source = "docling_tableformer"
                        tables.append(region)
                except Exception as exc:
                    if logger is not None:
                        logger.log(f"docling_tableformer crop failed on page {page_num}: {exc}")
    finally:
        doc.close()

    if logger is not None:
        logger.log(f"docling_tableformer structure recognizer found {len(tables)} tables")
    return tables
