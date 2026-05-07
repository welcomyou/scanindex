"""
RapidTable SLANet+ structure recognizer for detected table regions.

This module intentionally does not detect tables by itself. It consumes table
layout boxes from the main document layout stage, crops each box, and uses the
RapidTable ONNX structure model to infer the cell grid. Text assignment is kept
geometry-based and uses the OCR boxes already produced by the main pipeline.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import re

import fitz
import numpy as np


BBox = Tuple[float, float, float, float]
TextResolver = Callable[[int, BBox], str]


@dataclass
class RapidTableRegion:
    page: int
    y_top: float
    y_bottom: float
    cells: List[List[str]]
    row_count: int
    col_count: int
    cell_bboxes: List[List[BBox]] = field(default_factory=list)
    x_left: float = 0.0
    x_right: float = 0.0
    source: str = "rapidtable_slanet"


_ENGINE = None
_ENGINE_CACHE = {}
_ENGINE_LOCK = threading.Lock()


def is_rapidtable_available() -> bool:
    try:
        from rapid_table import EngineType, ModelType, RapidTable, RapidTableInput  # noqa: F401
        return True
    except Exception:
        return False


def _get_engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE

        from rapid_table import EngineType, ModelType, RapidTable, RapidTableInput

        cfg = RapidTableInput(
            model_type=ModelType.SLANETPLUS,
            engine_type=EngineType.ONNXRUNTIME,
            use_ocr=False,
            engine_cfg={"intra_op_num_threads": 4, "inter_op_num_threads": 1},
        )
        _ENGINE = RapidTable(cfg)
        return _ENGINE


def _get_rapidtable_engine(model_dir_or_path: Optional[str] = None,
                           use_ocr_results: bool = False):
    cache_key = ("rapidtable_slanet", str(model_dir_or_path or ""), bool(use_ocr_results))
    with _ENGINE_LOCK:
        if cache_key in _ENGINE_CACHE:
            return _ENGINE_CACHE[cache_key]

        from rapid_table import EngineType, ModelType, RapidTable, RapidTableInput

        cfg = RapidTableInput(
            model_type=ModelType.SLANETPLUS,
            model_dir_or_path=model_dir_or_path,
            engine_type=EngineType.ONNXRUNTIME,
            use_ocr=bool(use_ocr_results),
            engine_cfg={"intra_op_num_threads": 4, "inter_op_num_threads": 1},
        )
        engine = RapidTable(cfg)
        _ENGINE_CACHE[cache_key] = engine
        return engine


class _StructureOutput:
    def __init__(self, logic_points, cell_bboxes, elapse: float = 0.0):
        self.logic_points = logic_points
        self.cell_bboxes = cell_bboxes
        self.pred_htmls = []
        self.imgs = []
        self.elapse = elapse


class _SlanextWiredOnnxEngine:
    def __init__(self, model_path: str, dict_path: Optional[str] = None):
        import onnxruntime as ort
        from rapid_table.table_matcher import TableMatch
        from rapid_table.table_structure.pp_structure.post_process import TableLabelDecode
        from rapid_table.table_structure.pp_structure.pre_process import TablePreprocess

        resolved_dict = Path(dict_path) if dict_path else _default_slanext_dict_path()
        if not resolved_dict or not resolved_dict.exists():
            raise FileNotFoundError(
                "SLANeXt_wired ONNX requires table_structure_dict_ch.txt"
            )
        character = resolved_dict.read_text(encoding="utf-8").splitlines()

        sess_opts = ort.SessionOptions()
        sess_opts.log_severity_level = 4
        sess_opts.intra_op_num_threads = 4
        sess_opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name
        self.preprocess_op = TablePreprocess(max_len=512)
        self.postprocess_op = TableLabelDecode(character, {"model_type": None})
        self.table_matcher = TableMatch()

    def __call__(self, img, batch_size: int = 1):
        s = time.perf_counter()
        imgs = img if isinstance(img, list) else [img]
        processed_imgs, shape_lists = self.preprocess_op(imgs)
        if not processed_imgs:
            return _StructureOutput([], [], 0.0)
        input_arr = np.asarray(processed_imgs, dtype=np.float32)
        bbox_preds, struct_probs = self.session.run(None, {self.input_name: input_arr})
        table_structs, cell_bboxes = self.postprocess_op(
            bbox_preds, struct_probs, shape_lists, imgs
        )
        logic_points = self.table_matcher.decode_logic_points(table_structs)
        elapsed = (time.perf_counter() - s) / max(len(imgs), 1)
        return _StructureOutput(logic_points, cell_bboxes, elapsed)


class _WiredTableRecV2Engine:
    def __init__(self, model_path: Optional[str] = None):
        from wired_table_rec.main import WiredTableInput, WiredTableRecognition

        cfg = WiredTableInput(
            model_type="unet",
            model_path=model_path,
            use_cuda=False,
            device="cpu",
        )
        self.engine = WiredTableRecognition(cfg)

    def __call__(self, img, ocr_result=None, need_ocr: bool = False, **kwargs):
        s = time.perf_counter()
        if ocr_result is None:
            ocr_result = []
        result = self.engine(
            img,
            ocr_result=ocr_result,
            need_ocr=need_ocr,
            **kwargs,
        )
        elapsed = float(getattr(result, "elapse", 0.0) or (time.perf_counter() - s))
        logic_points = getattr(result, "logic_points", None)
        cell_bboxes = getattr(result, "cell_bboxes", None)
        if logic_points is None or cell_bboxes is None:
            return _StructureOutput([], [], elapsed)
        return _StructureOutput([logic_points], [cell_bboxes], elapsed)


def _default_slanext_dict_path() -> Optional[Path]:
    try:
        from scanindex.infra.paths import get_base_dir
        base = Path(get_base_dir())
    except Exception:
        base = Path(__file__).resolve().parents[3]
    candidates = [
        base
        / "temp"
        / "external"
        / "paddle_to_onnx_models"
        / "table_structure_dict_ch.txt",
        Path(r"D:\soft\Python3.12\Lib\site-packages\paddleocr\ppocr\utils\dict\table_structure_dict_ch.txt"),
    ]
    for path in candidates:
        if path.exists():
            return path
    try:
        import paddleocr

        root = Path(paddleocr.__file__).resolve().parent
        path = root / "ppocr" / "utils" / "dict" / "table_structure_dict_ch.txt"
        if path.exists():
            return path
    except Exception:
        pass
    return None


def _get_slanext_wired_engine(model_path: str, dict_path: Optional[str] = None):
    cache_key = ("slanext_wired", str(model_path), str(dict_path or ""))
    with _ENGINE_LOCK:
        if cache_key in _ENGINE_CACHE:
            return _ENGINE_CACHE[cache_key]
        engine = _SlanextWiredOnnxEngine(model_path, dict_path)
        _ENGINE_CACHE[cache_key] = engine
        return engine


def _get_wired_table_rec_v2_engine(model_path: Optional[str] = None):
    cache_key = ("wired_table_rec_v2", str(model_path or ""))
    with _ENGINE_LOCK:
        if cache_key in _ENGINE_CACHE:
            return _ENGINE_CACHE[cache_key]
        engine = _WiredTableRecV2Engine(model_path)
        _ENGINE_CACHE[cache_key] = engine
        return engine


def _layout_table_bboxes(layout_regions_by_page: Optional[Dict[int, List[dict]]],
                         page: int) -> List[BBox]:
    out: List[BBox] = []
    for region in (layout_regions_by_page or {}).get(page, []):
        if region.get("type") != "table":
            continue
        bbox = region.get("bbox_pdf")
        if bbox and len(bbox) >= 4:
            out.append(tuple(float(v) for v in bbox[:4]))
    out.sort(key=lambda b: (b[1], b[0]))
    return out


def _crop_rect(page: fitz.Page, bbox: BBox, pad_pt: float) -> fitz.Rect:
    return fitz.Rect(
        max(0.0, bbox[0] - pad_pt),
        max(0.0, bbox[1] - pad_pt),
        min(page.rect.width, bbox[2] + pad_pt),
        min(page.rect.height, bbox[3] + pad_pt),
    )


def _render_crop(page: fitz.Page, bbox: BBox, dpi: int, pad_pt: float):
    scale = dpi / 72.0
    rect = _crop_rect(page, bbox, pad_pt)
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]
    elif pix.n == 1:
        img = np.repeat(img, 3, axis=2)

    try:
        import cv2
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    except Exception:
        img = img[:, :, ::-1]
    return img, rect, scale


def _line_xywh(line: object) -> Tuple[float, float, float, float]:
    if all(hasattr(line, attr) for attr in ("x", "y", "width", "height")):
        return (
            float(getattr(line, "x", 0.0) or 0.0),
            float(getattr(line, "y", 0.0) or 0.0),
            float(getattr(line, "width", 0.0) or 0.0),
            float(getattr(line, "height", 0.0) or 0.0),
        )
    bbox = getattr(line, "bbox", None)
    if bbox and len(bbox) >= 4:
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
        return x0, y0, x1 - x0, y1 - y0
    return 0.0, 0.0, 0.0, 0.0


def _line_text(line: object) -> str:
    text = getattr(line, "text", None)
    if text is None:
        text = getattr(line, "ocr_text", "")
    return str(text or "").strip()


def _line_score(line: object) -> float:
    for attr in ("confidence", "score", "ocr_confidence"):
        value = getattr(line, attr, None)
        if value is not None:
            try:
                return float(value)
            except Exception:
                pass
    return 1.0


def _ocr_results_for_crop(page_lines: List[object], crop: fitz.Rect, scale: float):
    boxes = []
    texts = []
    scores = []
    for line in page_lines:
        text = _line_text(line)
        if not text:
            continue
        x, y, w, h = _line_xywh(line)
        if w <= 0 or h <= 0:
            continue
        cx = x + w / 2.0
        cy = y + h / 2.0
        if not (crop.x0 <= cx <= crop.x1 and crop.y0 <= cy <= crop.y1):
            continue
        x0 = (max(x, crop.x0) - crop.x0) * scale
        y0 = (max(y, crop.y0) - crop.y0) * scale
        x1 = (min(x + w, crop.x1) - crop.x0) * scale
        y1 = (min(y + h, crop.y1) - crop.y0) * scale
        if x1 <= x0 or y1 <= y0:
            continue
        boxes.append([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
        texts.append(text)
        scores.append(_line_score(line))
    return np.asarray(boxes, dtype=np.float32), tuple(texts), tuple(scores)


def _ocr_result_list_for_crop(page_lines: List[object], crop: fitz.Rect, scale: float):
    boxes, texts, scores = _ocr_results_for_crop(page_lines, crop, scale)
    return list(zip(boxes.tolist(), texts, scores))


def _logic_shape(points: np.ndarray) -> Tuple[int, int]:
    if points is None or len(points) == 0:
        return (0, 0)
    return (int(points[:, 1].max()) + 1, int(points[:, 3].max()) + 1)


def _rapid_cell_to_pdf_bbox(cell_bbox: np.ndarray, crop: fitz.Rect, scale: float) -> BBox:
    values = np.asarray(cell_bbox, dtype=float).reshape(-1)
    if values.size >= 8:
        xs = values[0::2]
        ys = values[1::2]
    elif values.size >= 4:
        xs = values[[0, 2]]
        ys = values[[1, 3]]
    else:
        return (crop.x0, crop.y0, crop.x1, crop.y1)

    x0 = crop.x0 + float(xs.min()) / scale
    y0 = crop.y0 + float(ys.min()) / scale
    x1 = crop.x0 + float(xs.max()) / scale
    y1 = crop.y0 + float(ys.max()) / scale
    return (x0, y0, x1, y1)


def _simple_cell_text(page_lines: List[object], bbox: BBox) -> str:
    x0, y0, x1, y1 = bbox
    found = []
    for line in page_lines:
        text = str(getattr(line, "text", "") or "").strip()
        if not text:
            continue
        lx = float(getattr(line, "x", 0.0))
        ly = float(getattr(line, "y", 0.0))
        lw = float(getattr(line, "width", 0.0))
        lh = float(getattr(line, "height", 0.0))
        cx = lx + lw / 2.0
        cy = ly + lh / 2.0
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            order = int(getattr(line, "order", 0) or 0)
            found.append((order, ly, lx, text))
    if any(item[0] for item in found):
        found.sort(key=lambda item: (item[0], item[1], item[2]))
    else:
        found.sort(key=lambda item: (item[1], item[2]))
    return "\n".join(item[3] for item in found)


def _clean_html_cell_text(text: str) -> str:
    text = unescape(str(text or ""))
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:%)\]])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    return text.strip()


class _RapidTableHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._row_idx = -1
        self._col_idx = 0
        self._occupied: Dict[Tuple[int, int], bool] = {}
        self._active_cell: Optional[Dict[str, object]] = None

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag == "tr":
            self._row_idx += 1
            self._col_idx = 0
            self._ensure_cell(self._row_idx, 0)
            return

        if tag not in {"td", "th"} or self._row_idx < 0:
            return

        attr_map = {str(k).lower(): str(v) for k, v in attrs}
        rowspan = self._positive_int(attr_map.get("rowspan"), 1)
        colspan = self._positive_int(attr_map.get("colspan"), 1)
        self._active_cell = {
            "rowspan": rowspan,
            "colspan": colspan,
            "parts": [],
        }

    def handle_data(self, data: str):
        if self._active_cell is not None and data:
            parts = self._active_cell["parts"]
            if isinstance(parts, list):
                parts.append(data)

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag not in {"td", "th"} or self._active_cell is None or self._row_idx < 0:
            return

        while self._occupied.get((self._row_idx, self._col_idx), False):
            self._col_idx += 1

        parts = self._active_cell.get("parts")
        raw_text = "".join(parts) if isinstance(parts, list) else ""
        text = _clean_html_cell_text(raw_text)
        rowspan = int(self._active_cell.get("rowspan", 1) or 1)
        colspan = int(self._active_cell.get("colspan", 1) or 1)

        for rr in range(self._row_idx, self._row_idx + rowspan):
            for cc in range(self._col_idx, self._col_idx + colspan):
                self._ensure_cell(rr, cc)
                self.rows[rr][cc] = text
                self._occupied[(rr, cc)] = True

        self._col_idx += colspan
        self._active_cell = None

    def _ensure_cell(self, row: int, col: int):
        while len(self.rows) <= row:
            self.rows.append([])
        while len(self.rows[row]) <= col:
            self.rows[row].append("")

    @staticmethod
    def _positive_int(value: Optional[str], default: int) -> int:
        try:
            parsed = int(str(value))
        except Exception:
            return default
        return max(parsed, 1)


def _html_table_to_grid(html_text: str) -> List[List[str]]:
    if not html_text:
        return []
    parser = _RapidTableHtmlParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return []
    rows = [row for row in parser.rows if row]
    if not rows:
        return []
    col_count = max(len(row) for row in rows)
    return [row + [""] * (col_count - len(row)) for row in rows]


def _official_html_grid(result) -> List[List[str]]:
    pred_htmls = getattr(result, "pred_htmls", None) or []
    if not pred_htmls:
        return []
    html_text = pred_htmls[0]
    if not html_text:
        return []
    return _html_table_to_grid(str(html_text))


def _result_to_region(result,
                      page_num: int,
                      crop: fitz.Rect,
                      scale: float,
                      page_lines: List[object],
                      text_resolver: Optional[TextResolver]) -> Optional[RapidTableRegion]:
    logic_points = result.logic_points[0] if result.logic_points else np.empty((0, 4))
    cell_bboxes = result.cell_bboxes[0] if result.cell_bboxes else np.empty((0, 4))
    if logic_points is None or len(logic_points) == 0:
        return None
    if cell_bboxes is None or len(cell_bboxes) == 0:
        return None

    rows, cols = _logic_shape(logic_points)
    if rows <= 0 or cols <= 0:
        return None

    cells = [[""] * cols for _ in range(rows)]
    bboxes = [[(0.0, 0.0, 0.0, 0.0)] * cols for _ in range(rows)]
    official_grid = _official_html_grid(result)

    for idx, point in enumerate(logic_points):
        if idx >= len(cell_bboxes):
            break
        r0, r1, c0, c1 = [int(v) for v in point[:4]]
        if r0 < 0 or c0 < 0 or r1 < r0 or c1 < c0:
            continue
        if r0 >= rows or c0 >= cols:
            continue

        pdf_bbox = _rapid_cell_to_pdf_bbox(cell_bboxes[idx], crop, scale)
        if text_resolver is not None:
            text = text_resolver(page_num, pdf_bbox)
        else:
            text = _simple_cell_text(page_lines, pdf_bbox)

        for r in range(max(0, r0), min(rows, r1 + 1)):
            for c in range(max(0, c0), min(cols, c1 + 1)):
                cells[r][c] = text
                bboxes[r][c] = pdf_bbox

    nonempty_boxes = [bbox for row in bboxes for bbox in row if any(bbox)]
    if nonempty_boxes:
        y_top = min(b[1] for b in nonempty_boxes)
        y_bottom = max(b[3] for b in nonempty_boxes)
        x_left = min(b[0] for b in nonempty_boxes)
        x_right = max(b[2] for b in nonempty_boxes)
    else:
        y_top, y_bottom = crop.y0, crop.y1
        x_left, x_right = crop.x0, crop.x1

    region = RapidTableRegion(
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
    if official_grid:
        setattr(region, "rapidtable_official_cells", official_grid)
        setattr(region, "rapidtable_used_official_html", True)
    return region


def detect_tables_rapidtable_slanet(
    pdf_path: str,
    logger,
    page_info: dict,
    pdf_lines: List[object],
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None,
    dpi: int = 200,
    pad_pt: float = 6.0,
    text_resolver: Optional[TextResolver] = None,
    model_dir_or_path: Optional[str] = None,
    use_ocr_results: bool = False,
) -> List[RapidTableRegion]:
    return detect_tables_structure_recognizer(
        pdf_path=pdf_path,
        logger=logger,
        page_info=page_info,
        pdf_lines=pdf_lines,
        layout_regions_by_page=layout_regions_by_page,
        recognizer="rapidtable_slanet",
        dpi=dpi,
        pad_pt=pad_pt,
        text_resolver=text_resolver,
        model_path=model_dir_or_path,
        rapidtable_use_ocr_results=use_ocr_results,
    )


def detect_tables_slanext_wired_onnx(
    pdf_path: str,
    logger,
    page_info: dict,
    pdf_lines: List[object],
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None,
    dpi: int = 200,
    pad_pt: float = 6.0,
    text_resolver: Optional[TextResolver] = None,
    model_path: Optional[str] = None,
    dict_path: Optional[str] = None,
) -> List[RapidTableRegion]:
    if not model_path:
        raise ValueError("model_path is required for SLANeXt_wired ONNX")
    return detect_tables_structure_recognizer(
        pdf_path=pdf_path,
        logger=logger,
        page_info=page_info,
        pdf_lines=pdf_lines,
        layout_regions_by_page=layout_regions_by_page,
        recognizer="slanext_wired",
        dpi=dpi,
        pad_pt=pad_pt,
        text_resolver=text_resolver,
        model_path=model_path,
        dict_path=dict_path,
    )


def detect_tables_wired_table_rec_v2(
    pdf_path: str,
    logger,
    page_info: dict,
    pdf_lines: List[object],
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None,
    dpi: int = 200,
    pad_pt: float = 6.0,
    text_resolver: Optional[TextResolver] = None,
    model_path: Optional[str] = None,
    use_ocr_results: bool = False,
) -> List[RapidTableRegion]:
    return detect_tables_structure_recognizer(
        pdf_path=pdf_path,
        logger=logger,
        page_info=page_info,
        pdf_lines=pdf_lines,
        layout_regions_by_page=layout_regions_by_page,
        recognizer="wired_table_rec_v2",
        dpi=dpi,
        pad_pt=pad_pt,
        text_resolver=text_resolver,
        model_path=model_path,
        wired_use_ocr_results=use_ocr_results,
    )


def detect_tables_structure_recognizer(
    pdf_path: str,
    logger,
    page_info: dict,
    pdf_lines: List[object],
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None,
    recognizer: str = "rapidtable_slanet",
    dpi: int = 200,
    pad_pt: float = 6.0,
    text_resolver: Optional[TextResolver] = None,
    model_path: Optional[str] = None,
    dict_path: Optional[str] = None,
    rapidtable_use_ocr_results: bool = False,
    wired_use_ocr_results: bool = False,
) -> List[RapidTableRegion]:
    if not layout_regions_by_page:
        return []
    if recognizer == "rapidtable_slanet" and not is_rapidtable_available():
        return []

    lines_by_page: Dict[int, List[object]] = {}
    for line in pdf_lines or []:
        lines_by_page.setdefault(int(getattr(line, "page", 0) or 0), []).append(line)

    if recognizer == "rapidtable_slanet":
        engine = _get_rapidtable_engine(model_path, rapidtable_use_ocr_results)
        source_name = "rapidtable_slanet_ocr" if rapidtable_use_ocr_results else "rapidtable_slanet"
    elif recognizer == "slanext_wired":
        if not model_path:
            raise ValueError("model_path is required for SLANeXt_wired ONNX")
        engine = _get_slanext_wired_engine(model_path, dict_path)
        source_name = "slanext_wired"
    elif recognizer == "wired_table_rec_v2":
        engine = _get_wired_table_rec_v2_engine(model_path)
        source_name = "wired_table_rec_v2_ocr" if wired_use_ocr_results else "wired_table_rec_v2"
    else:
        raise ValueError(f"Unsupported table structure recognizer: {recognizer}")

    tables: List[RapidTableRegion] = []
    doc = fitz.open(pdf_path)
    try:
        for page_num in range(1, len(doc) + 1):
            layout_bboxes = _layout_table_bboxes(layout_regions_by_page, page_num)
            if not layout_bboxes:
                continue
            page = doc[page_num - 1]
            for bbox in layout_bboxes:
                try:
                    img, crop, scale = _render_crop(page, bbox, dpi, pad_pt)
                    if recognizer == "rapidtable_slanet" and rapidtable_use_ocr_results:
                        ocr_result = _ocr_results_for_crop(lines_by_page.get(page_num, []), crop, scale)
                        result = engine(img, ocr_results=[ocr_result], batch_size=1)
                    elif recognizer == "wired_table_rec_v2":
                        ocr_result = _ocr_result_list_for_crop(
                            lines_by_page.get(page_num, []), crop, scale
                        )
                        result = engine(
                            img,
                            ocr_result=ocr_result,
                            need_ocr=bool(wired_use_ocr_results),
                        )
                    else:
                        result = engine(img, batch_size=1)
                    table = _result_to_region(
                        result,
                        page_num,
                        crop,
                        scale,
                        lines_by_page.get(page_num, []),
                        text_resolver,
                    )
                    if table is not None:
                        table.source = source_name
                        tables.append(table)
                except Exception as e:
                    if logger is not None:
                        logger.log(f"{source_name} crop failed on page {page_num}: {e}")
    finally:
        doc.close()

    if logger is not None:
        logger.log(f"{source_name} structure recognizer found {len(tables)} tables")
    return tables
