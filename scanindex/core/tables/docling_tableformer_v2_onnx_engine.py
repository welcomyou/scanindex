"""ONNX Runtime adapter for Docling TableFormerV2 table structure recognition.

The model itself does not consume OCR boxes. OCR text is assigned later by the
existing geometry-based pipeline postprocess, using the predicted cell boxes.
The OTSL-to-cell conversion mirrors Docling's TableStructureModelV2 logic.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz
import numpy as np
import onnxruntime as ort
from PIL import Image
from tokenizers import Tokenizer

from scanindex.core.tables.docling_tableformer_engine import (
    BBox,
    DoclingTableFormerRegion,
    _layout_table_bboxes,
    _ocr_text_cells_for_table,
    _render_crop,
)


try:
    from scanindex.infra.paths import get_base_dir
    ROOT = Path(get_base_dir())
except Exception:
    ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACT_DIR = ROOT / "temp" / "docling_onnx_export_probe"
DEFAULT_ENCODER = DEFAULT_ARTIFACT_DIR / "tableformer_v2_encoder.onnx"
DEFAULT_DECODER = DEFAULT_ARTIFACT_DIR / "tableformer_v2_decoder_dynamo.onnx"
DEFAULT_BBOX_HEAD = DEFAULT_ARTIFACT_DIR / "tableformer_v2_bbox_head_dynamo.onnx"

CELL_TOKENS = {"fcel", "ecel", "ched", "rhed", "srow"}
SKIP_TOKENS = {"<pad>", "[UNK]", "<start>", "<end>"}


@dataclass(frozen=True)
class _OnnxPaths:
    encoder: Path
    decoder: Path
    bbox_head: Path
    tokenizer: Optional[Path]
    num_threads: int


class TableFormerV2OnnxRunner:
    def __init__(
        self,
        encoder_path: Path = DEFAULT_ENCODER,
        decoder_path: Path = DEFAULT_DECODER,
        bbox_head_path: Path = DEFAULT_BBOX_HEAD,
        tokenizer_path: Optional[Path] = None,
        num_threads: int = 4,
    ):
        self.encoder_path = Path(encoder_path)
        self.decoder_path = Path(decoder_path)
        self.bbox_head_path = Path(bbox_head_path)
        self.num_threads = max(1, int(num_threads))
        self.tokenizer_path = Path(tokenizer_path) if tokenizer_path else _default_tokenizer_path()

        for path in (self.encoder_path, self.decoder_path, self.bbox_head_path, self.tokenizer_path):
            if not path.exists():
                raise FileNotFoundError(path)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.num_threads
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = ["CPUExecutionProvider"]
        self.encoder = ort.InferenceSession(str(self.encoder_path), sess_options=opts, providers=providers)
        self.decoder = ort.InferenceSession(str(self.decoder_path), sess_options=opts, providers=providers)
        self.bbox_head = ort.InferenceSession(str(self.bbox_head_path), sess_options=opts, providers=providers)
        self.tokenizer = Tokenizer.from_file(str(self.tokenizer_path))

        self.bos_id = _required_token_id(self.tokenizer, "<start>")
        self.eos_id = _required_token_id(self.tokenizer, "<end>")
        self.cell_token_ids = np.array(
            [_required_token_id(self.tokenizer, f"<{token}>") for token in sorted(CELL_TOKENS)],
            dtype=np.int64,
        )

    def predict(self, image: Image.Image, table_bbox: BBox, max_length: int = 512) -> DoclingTableFormerRegion:
        image_tensor = _preprocess_image(image)
        encoder_hidden = self.encoder.run(None, {"images": image_tensor})[0]

        ids = np.array([[self.bos_id]], dtype=np.int64)
        decoded = None
        for _ in range(max_length):
            logits, decoded = self.decoder.run(
                None,
                {"encoder_hidden": encoder_hidden, "input_ids": ids},
            )
            next_id = int(np.argmax(logits[0, -1, :]))
            ids = np.concatenate([ids, np.array([[next_id]], dtype=np.int64)], axis=1)
            if next_id == self.eos_id:
                break

        if decoded is None or decoded.shape[1] != ids.shape[1]:
            _logits, decoded = self.decoder.run(
                None,
                {"encoder_hidden": encoder_hidden, "input_ids": ids},
            )

        cell_mask = np.isin(ids[0], self.cell_token_ids)
        cell_embeddings = decoded[0, cell_mask, :].astype(np.float32)
        if cell_embeddings.shape[0]:
            cell_batch_indices = np.zeros((cell_embeddings.shape[0],), dtype=np.int64)
            try:
                pred_bboxes = self.bbox_head.run(
                    None,
                    {
                        "cell_embeddings": cell_embeddings,
                        "encoder_hidden": encoder_hidden,
                        "cell_batch_indices": cell_batch_indices,
                    },
                )[0]
                pred_bboxes = pred_bboxes[np.sum(pred_bboxes, axis=-1) > 0]
            except Exception:
                pred_bboxes = np.empty((0, 4), dtype=np.float32)
        else:
            pred_bboxes = np.empty((0, 4), dtype=np.float32)

        otsl_seq = self.decode_otsl_sequence(ids[0])
        return _build_region_from_otsl(otsl_seq, pred_bboxes, table_bbox)

    def decode_otsl_sequence(self, token_ids: np.ndarray) -> List[str]:
        tags: List[str] = []
        for token_id in token_ids.tolist():
            token = (self.tokenizer.id_to_token(int(token_id)) or "").strip()
            if token in SKIP_TOKENS:
                continue
            if token.startswith("<") and token.endswith(">"):
                token = token[1:-1]
            if token:
                tags.append(token)
        return tags


_RUNNER_CACHE: Dict[_OnnxPaths, TableFormerV2OnnxRunner] = {}
_RUNNER_LOCK = threading.Lock()


def is_docling_tableformer_v2_onnx_available(
    encoder_path: Path = DEFAULT_ENCODER,
    decoder_path: Path = DEFAULT_DECODER,
    bbox_head_path: Path = DEFAULT_BBOX_HEAD,
) -> bool:
    try:
        _ = ort.__version__
        _default_tokenizer_path()
        return Path(encoder_path).exists() and Path(decoder_path).exists() and Path(bbox_head_path).exists()
    except Exception:
        return False


def _get_runner(
    encoder_path: Path = DEFAULT_ENCODER,
    decoder_path: Path = DEFAULT_DECODER,
    bbox_head_path: Path = DEFAULT_BBOX_HEAD,
    tokenizer_path: Optional[Path] = None,
    num_threads: int = 4,
) -> TableFormerV2OnnxRunner:
    key = _OnnxPaths(
        Path(encoder_path).resolve(),
        Path(decoder_path).resolve(),
        Path(bbox_head_path).resolve(),
        Path(tokenizer_path).resolve() if tokenizer_path else None,
        max(1, int(num_threads)),
    )
    with _RUNNER_LOCK:
        runner = _RUNNER_CACHE.get(key)
        if runner is None:
            runner = TableFormerV2OnnxRunner(
                key.encoder,
                key.decoder,
                key.bbox_head,
                key.tokenizer,
                key.num_threads,
            )
            _RUNNER_CACHE[key] = runner
        return runner


def _default_tokenizer_path() -> Path:
    cache_root = Path.home() / ".cache" / "huggingface" / "hub"
    matches = sorted(
        cache_root.glob("models--docling-project--TableFormerV2/snapshots/*/tokenizer.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if matches:
        return matches[0]

    from huggingface_hub import hf_hub_download

    return Path(hf_hub_download("docling-project/TableFormerV2", "tokenizer.json"))


def _required_token_id(tokenizer: Tokenizer, token: str) -> int:
    token_id = tokenizer.token_to_id(token)
    if token_id is None:
        raise ValueError(f"Missing tokenizer token: {token}")
    return int(token_id)


def _preprocess_image(image: Image.Image) -> np.ndarray:
    pil = image.convert("RGB").resize((448, 448), Image.Resampling.BILINEAR)
    arr = np.asarray(pil, dtype=np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    return np.transpose(arr, (2, 0, 1))[None, :, :, :].astype(np.float32)


def _build_region_from_otsl(
    otsl_seq: List[str],
    bboxes: np.ndarray,
    table_bbox: BBox,
    page_num: int = 0,
) -> DoclingTableFormerRegion:
    rows = [list(group) for is_break, group in groupby(otsl_seq, lambda tag: tag == "nl") if not is_break]
    if not rows:
        return DoclingTableFormerRegion(
            page=page_num,
            y_top=table_bbox[1],
            y_bottom=table_bbox[3],
            cells=[],
            row_count=0,
            col_count=0,
            cell_bboxes=[],
            x_left=table_bbox[0],
            x_right=table_bbox[2],
            source="docling_tableformer",
        )

    row_count = len(rows)
    col_count = max(len(row) for row in rows)
    grid = [row + [""] * (col_count - len(row)) for row in rows]
    cells = [[""] * col_count for _ in range(row_count)]
    cell_bboxes: List[List[BBox]] = [[(0.0, 0.0, 0.0, 0.0)] * col_count for _ in range(row_count)]
    cell_slots: List[Tuple[int, int, BBox]] = []

    t_x1, t_y1, t_x2, t_y2 = [float(value) for value in table_bbox]
    t_w = t_x2 - t_x1
    t_h = t_y2 - t_y1
    bbox_idx = 0

    for row_idx, row in enumerate(grid):
        for col_idx, tag in enumerate(row):
            if tag not in CELL_TOKENS:
                continue

            colspan = 1
            for c in range(col_idx + 1, col_count):
                if grid[row_idx][c] == "lcel":
                    colspan += 1
                else:
                    break

            rowspan = 1
            for r in range(row_idx + 1, row_count):
                if grid[r][col_idx] == "ucel":
                    rowspan += 1
                else:
                    break

            if bbox_idx < int(bboxes.shape[0]):
                raw = [float(value) for value in bboxes[bbox_idx].tolist()]
                bbox: BBox = (
                    t_x1 + raw[0] * t_w,
                    t_y1 + raw[1] * t_h,
                    t_x1 + raw[2] * t_w,
                    t_y1 + raw[3] * t_h,
                )
                bbox_idx += 1
            else:
                bbox = (
                    t_x1 + (col_idx / max(col_count, 1)) * t_w,
                    t_y1 + (row_idx / max(row_count, 1)) * t_h,
                    t_x1 + ((col_idx + colspan) / max(col_count, 1)) * t_w,
                    t_y1 + ((row_idx + rowspan) / max(row_count, 1)) * t_h,
                )
            cell_slots.append((row_idx, col_idx, bbox))

            for r in range(row_idx, min(row_count, row_idx + rowspan)):
                for c in range(col_idx, min(col_count, col_idx + colspan)):
                    cell_bboxes[r][c] = bbox

    valid = [bbox for row in cell_bboxes for bbox in row if bbox[2] > bbox[0] and bbox[3] > bbox[1]]
    if valid:
        x_left = min(bbox[0] for bbox in valid)
        x_right = max(bbox[2] for bbox in valid)
        y_top = min(bbox[1] for bbox in valid)
        y_bottom = max(bbox[3] for bbox in valid)
    else:
        x_left, y_top, x_right, y_bottom = table_bbox

    region = DoclingTableFormerRegion(
        page=page_num,
        y_top=y_top,
        y_bottom=y_bottom,
        cells=cells,
        row_count=row_count,
        col_count=col_count,
        cell_bboxes=cell_bboxes,
        x_left=x_left,
        x_right=x_right,
        source="docling_tableformer",
    )
    setattr(region, "_docling_cell_slots", cell_slots)
    return region


def _area(bbox: BBox) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _intersection(a: BBox, b: BBox) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0,
        min(a[3], b[3]) - max(a[1], b[1]),
    )


def _text_cell_bbox(text_cell: object) -> BBox:
    bbox = text_cell.rect.to_bounding_box()
    return (float(bbox.l), float(bbox.t), float(bbox.r), float(bbox.b))


def _assign_docling_overlap_text(
    region: DoclingTableFormerRegion,
    text_cells: List[object],
    textcell_overlap: float = 0.3,
) -> None:
    slots = getattr(region, "_docling_cell_slots", []) or []
    for row_idx, col_idx, bbox in slots:
        if row_idx >= region.row_count or col_idx >= region.col_count:
            continue
        parts: List[str] = []
        for text_cell in text_cells:
            tc_bbox = _text_cell_bbox(text_cell)
            tc_area = _area(tc_bbox)
            if tc_area <= 0:
                continue
            if _intersection(tc_bbox, bbox) / tc_area > textcell_overlap:
                text = str(getattr(text_cell, "text", "") or "").strip()
                if text:
                    parts.append(text)
        region.cells[row_idx][col_idx] = " ".join(parts)


def detect_tables_docling_tableformer_v2_onnx(
    pdf_path: str,
    logger,
    page_info: dict,
    pdf_lines: List[object],
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None,
    dpi: int = 144,
    pad_pt: float = 0.0,
    num_threads: int = 4,
    encoder_path: Path = DEFAULT_ENCODER,
    decoder_path: Path = DEFAULT_DECODER,
    bbox_head_path: Path = DEFAULT_BBOX_HEAD,
    tokenizer_path: Optional[Path] = None,
) -> List[DoclingTableFormerRegion]:
    del page_info
    if not layout_regions_by_page or not is_docling_tableformer_v2_onnx_available(
        encoder_path,
        decoder_path,
        bbox_head_path,
    ):
        return []

    runner = _get_runner(
        encoder_path=encoder_path,
        decoder_path=decoder_path,
        bbox_head_path=bbox_head_path,
        tokenizer_path=tokenizer_path,
        num_threads=num_threads,
    )
    lines_by_page: Dict[int, List[object]] = {}
    for line in pdf_lines or []:
        lines_by_page.setdefault(int(getattr(line, "page", 0) or 0), []).append(line)

    tables: List[DoclingTableFormerRegion] = []
    doc = fitz.open(pdf_path)
    try:
        for page_num in range(1, len(doc) + 1):
            page = doc[page_num - 1]
            layout_bboxes = _layout_table_bboxes(layout_regions_by_page, page_num)
            if not layout_bboxes:
                continue
            for bbox in layout_bboxes:
                try:
                    image, crop = _render_crop(page, bbox, dpi, pad_pt)
                    cluster_bbox = (float(crop.x0), float(crop.y0), float(crop.x1), float(crop.y1))
                    region = runner.predict(image, cluster_bbox)
                    text_cells = _ocr_text_cells_for_table(lines_by_page.get(page_num, []), cluster_bbox)
                    _assign_docling_overlap_text(region, text_cells)
                    region.page = page_num
                    region.source = "docling_tableformer"
                    setattr(region, "engine", "docling_tableformer_v2_onnx")
                    tables.append(region)
                except Exception as exc:
                    if logger is not None:
                        logger.log(f"docling_tableformer_v2_onnx crop failed on page {page_num}: {exc}")
    finally:
        doc.close()

    if logger is not None:
        logger.log(f"docling_tableformer_v2_onnx structure recognizer found {len(tables)} tables")
    return tables
