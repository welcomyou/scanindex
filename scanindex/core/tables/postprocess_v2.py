from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


BBox = Tuple[float, float, float, float]


@dataclass
class OcrFragment:
    text: str
    bbox: BBox
    page: int
    line_order: int
    word_order: int
    source_id: int
    has_space_after: bool = True
    is_word: bool = False


def _clean_text(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = re.sub(r"\s+([,.;:%)\]])", r"\1", value)
    value = re.sub(r"([(\[])\s+", r"\1", value)
    return value


def _area(bbox: BBox) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _intersection(a: BBox, b: BBox) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1])
    )


def _center(bbox: BBox) -> Tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _bbox_from_table(table: Any) -> Optional[BBox]:
    xs: List[float] = []
    ys: List[float] = []
    for row in getattr(table, "cell_bboxes", []) or []:
        for raw in row:
            if len(raw) >= 4:
                bbox = tuple(float(v) for v in raw[:4])
                if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                    xs.extend([bbox[0], bbox[2]])
                    ys.extend([bbox[1], bbox[3]])
    if xs and ys:
        return (min(xs), min(ys), max(xs), max(ys))
    x_left = float(getattr(table, "x_left", 0.0) or 0.0)
    x_right = float(getattr(table, "x_right", 0.0) or 0.0)
    y_top = float(getattr(table, "y_top", 0.0) or 0.0)
    y_bottom = float(getattr(table, "y_bottom", 0.0) or 0.0)
    if x_right > x_left and y_bottom > y_top:
        return (x_left, y_top, x_right, y_bottom)
    return None


def _iter_cell_bboxes(table: Any) -> Iterable[Tuple[int, int, BBox]]:
    rows = int(getattr(table, "row_count", 0) or 0)
    cols = int(getattr(table, "col_count", 0) or 0)
    boxes = getattr(table, "cell_bboxes", []) or []
    for r in range(rows):
        if r >= len(boxes):
            continue
        for c in range(cols):
            if c >= len(boxes[r]):
                continue
            raw = boxes[r][c]
            if len(raw) < 4:
                continue
            bbox = tuple(float(v) for v in raw[:4])
            if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                yield r, c, bbox


def _line_bbox(line: Any) -> BBox:
    if all(hasattr(line, attr) for attr in ("x", "y", "width", "height")):
        x = float(getattr(line, "x", 0.0) or 0.0)
        y = float(getattr(line, "y", 0.0) or 0.0)
        w = float(getattr(line, "width", 0.0) or 0.0)
        h = float(getattr(line, "height", 0.0) or 0.0)
        return (x, y, x + w, y + h)
    bbox = getattr(line, "bbox", None)
    if bbox and len(bbox) >= 4:
        return tuple(float(v) for v in bbox[:4])
    return (0.0, 0.0, 0.0, 0.0)


def _fragments_from_lines(pdf_lines: List[Any], page: int) -> List[OcrFragment]:
    fragments: List[OcrFragment] = []
    for line in pdf_lines:
        if int(getattr(line, "page", 0) or 0) != page:
            continue
        line_order = int(getattr(line, "order", 0) or 0)
        words = [
            word
            for word in (getattr(line, "word_items", None) or [])
            if _clean_text(str(word.get("text", "") or ""))
        ]
        if words:
            for idx, word in enumerate(words):
                x = float(word.get("x", 0.0) or 0.0)
                y = float(word.get("y", 0.0) or 0.0)
                w = float(word.get("w", 0.0) or 0.0)
                h = float(word.get("h", 0.0) or 0.0)
                if w <= 0 or h <= 0:
                    continue
                fragments.append(
                    OcrFragment(
                        text=_clean_text(str(word.get("text", "") or "")),
                        bbox=(x, y, x + w, y + h),
                        page=page,
                        line_order=line_order,
                        word_order=int(word.get("order", idx) or idx),
                        source_id=id(line),
                        has_space_after=bool(word.get("has_space_after", True)),
                        is_word=True,
                    )
                )
            continue

        text = _clean_text(str(getattr(line, "text", "") or ""))
        bbox = _line_bbox(line)
        if text and bbox[2] > bbox[0] and bbox[3] > bbox[1]:
            fragments.append(
                OcrFragment(
                    text=text,
                    bbox=bbox,
                    page=page,
                    line_order=line_order,
                    word_order=0,
                    source_id=id(line),
                    is_word=False,
                )
            )
    return fragments


def _cell_match_score(fragment: OcrFragment, cell_bbox: BBox, table_bbox: BBox) -> Optional[float]:
    f_area = _area(fragment.bbox)
    c_area = _area(cell_bbox)
    if f_area <= 0 or c_area <= 0:
        return None

    table_pad_x = max(2.0, (table_bbox[2] - table_bbox[0]) * 0.01)
    table_pad_y = max(2.0, (table_bbox[3] - table_bbox[1]) * 0.01)
    cx, cy = _center(fragment.bbox)
    inside_table = (
        table_bbox[0] - table_pad_x <= cx <= table_bbox[2] + table_pad_x
        and table_bbox[1] - table_pad_y <= cy <= table_bbox[3] + table_pad_y
    )
    if not inside_table:
        return None

    inter = _intersection(fragment.bbox, cell_bbox)
    coverage = inter / f_area
    iou = inter / max(f_area + c_area - inter, 1e-6)
    center_inside = cell_bbox[0] <= cx <= cell_bbox[2] and cell_bbox[1] <= cy <= cell_bbox[3]
    x_overlap = max(0.0, min(fragment.bbox[2], cell_bbox[2]) - max(fragment.bbox[0], cell_bbox[0]))
    y_overlap = max(0.0, min(fragment.bbox[3], cell_bbox[3]) - max(fragment.bbox[1], cell_bbox[1]))
    x_band = x_overlap / max(fragment.bbox[2] - fragment.bbox[0], 1e-6)
    y_band = y_overlap / max(fragment.bbox[3] - fragment.bbox[1], 1e-6)

    if inter <= 0 and not center_inside:
        cell_cx, cell_cy = _center(cell_bbox)
        norm_dx = abs(cx - cell_cx) / max(cell_bbox[2] - cell_bbox[0], 1.0)
        norm_dy = abs(cy - cell_cy) / max(cell_bbox[3] - cell_bbox[1], 1.0)
        dist = math.sqrt(norm_dx * norm_dx + norm_dy * norm_dy)
        if dist > 1.25:
            return None
        return 0.12 - dist * 0.05

    return coverage * 3.0 + iou + x_band * 0.35 + y_band * 0.35 + (0.75 if center_inside else 0.0)


def _best_cell_for_fragment(fragment: OcrFragment, table: Any, table_bbox: BBox) -> Optional[Tuple[int, int, float]]:
    best: Optional[Tuple[int, int, float]] = None
    for r, c, cell_bbox in _iter_cell_bboxes(table):
        score = _cell_match_score(fragment, cell_bbox, table_bbox)
        if score is None:
            continue
        if best is None or score > best[2]:
            best = (r, c, score)
    return best


def _rebuild_cell_text(fragments: List[OcrFragment]) -> str:
    if not fragments:
        return ""
    fragments = sorted(fragments, key=lambda f: (f.line_order, f.bbox[1], f.bbox[0], f.word_order))
    lines: List[str] = []
    current_source: Optional[int] = None
    current_parts: List[str] = []
    for frag in fragments:
        if current_source is None:
            current_source = frag.source_id
        if frag.source_id != current_source:
            lines.append(_clean_text(" ".join(current_parts)))
            current_parts = []
            current_source = frag.source_id
        current_parts.append(frag.text)
    if current_parts:
        lines.append(_clean_text(" ".join(current_parts)))
    return "\n".join(part for part in lines if part)


def match_table_ocr_v2(
    table: Any,
    pdf_lines: List[Any],
    logger: Any = None,
    replace_existing: bool = False,
) -> Dict[str, Any]:
    table_bbox = _bbox_from_table(table)
    if table_bbox is None:
        return {"assigned": 0, "orphan": 0, "coverage": 0.0}

    page = int(getattr(table, "page", 0) or 0)
    fragments = _fragments_from_lines(pdf_lines, page)
    in_table = [
        frag
        for frag in fragments
        if _cell_match_score(frag, table_bbox, table_bbox) is not None
    ]

    rows = int(getattr(table, "row_count", 0) or 0)
    cols = int(getattr(table, "col_count", 0) or 0)
    if rows <= 0 or cols <= 0:
        return {"assigned": 0, "orphan": len(in_table), "coverage": 0.0}

    buckets: Dict[Tuple[int, int], List[OcrFragment]] = {}
    assigned = 0
    orphan = 0
    weak = 0
    for frag in in_table:
        best = _best_cell_for_fragment(frag, table, table_bbox)
        if best is None:
            orphan += 1
            continue
        r, c, score = best
        if score < 0.05:
            orphan += 1
            continue
        if score < 0.2:
            weak += 1
        buckets.setdefault((r, c), []).append(frag)
        assigned += 1

    existing = getattr(table, "cells", []) or []
    rebuilt = [[""] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            text = _rebuild_cell_text(buckets.get((r, c), []))
            existing_text = ""
            if r < len(existing) and c < len(existing[r]):
                existing_text = str(existing[r][c])
            if replace_existing and text:
                rebuilt[r][c] = text
            elif existing_text:
                rebuilt[r][c] = existing_text
            elif text:
                rebuilt[r][c] = text
    table.cells = rebuilt
    stats = {
        "assigned": assigned,
        "orphan": orphan,
        "weak": weak,
        "in_table": len(in_table),
        "coverage": assigned / len(in_table) if in_table else 1.0,
    }
    if logger is not None and in_table:
        logger.log(
            "V2 cell matcher: "
            f"assigned={assigned}/{len(in_table)} orphan={orphan} weak={weak}"
        )
    return stats


def postprocess_tables_v2(
    tables: List[Any],
    pdf_lines: List[Any],
    logger: Any = None,
    replace_existing: bool = False,
) -> List[Any]:
    for table in tables:
        if getattr(table, "skip_render", False):
            continue
        match_table_ocr_v2(table, pdf_lines, logger, replace_existing=replace_existing)
    return tables


def table_ocr_fit_score(table: Any, pdf_lines: List[Any]) -> float:
    table_bbox = _bbox_from_table(table)
    if table_bbox is None:
        return -5.0
    page = int(getattr(table, "page", 0) or 0)
    fragments = [
        frag
        for frag in _fragments_from_lines(pdf_lines, page)
        if _cell_match_score(frag, table_bbox, table_bbox) is not None
    ]
    if not fragments:
        return 0.0
    assigned = 0
    weak = 0
    for frag in fragments:
        best = _best_cell_for_fragment(frag, table, table_bbox)
        if best is None:
            continue
        if best[2] >= 0.2:
            assigned += 1
        elif best[2] >= 0.05:
            assigned += 1
            weak += 1
    coverage = assigned / len(fragments)
    weak_ratio = weak / max(assigned, 1)
    rows = max(0, int(getattr(table, "row_count", 0) or 0))
    cols = max(0, int(getattr(table, "col_count", 0) or 0))
    structure_bonus = min(rows * cols, 120) / 120.0
    return coverage * 6.0 - weak_ratio * 1.5 + structure_bonus
