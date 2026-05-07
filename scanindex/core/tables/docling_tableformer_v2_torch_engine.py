"""PyTorch adapter for Docling TableFormerV2, used for ONNX parity benchmarks."""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

import fitz

from scanindex.core.tables.docling_tableformer_engine import (
    BBox,
    DoclingTableFormerRegion,
    _layout_table_bboxes,
    _ocr_text_cells_for_table,
    _render_crop,
    _table_to_region,
)


_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()


def is_docling_tableformer_v2_torch_available() -> bool:
    try:
        import docling  # noqa: F401
        import docling_ibm_models  # noqa: F401
        import torch  # noqa: F401

        return True
    except Exception:
        return False


def _get_model(num_threads: int = 4):
    key = int(num_threads)
    with _MODEL_LOCK:
        if key in _MODEL_CACHE:
            return _MODEL_CACHE[key]

        from docling.datamodel.accelerator_options import (
            AcceleratorDevice,
            AcceleratorOptions,
        )
        from docling.datamodel.pipeline_options import TableStructureV2Options
        from docling.models.stages.table_structure.table_structure_model_v2 import (
            TableStructureModelV2,
        )

        options = TableStructureV2Options(do_cell_matching=True)
        accelerator = AcceleratorOptions(
            device=AcceleratorDevice.CPU,
            num_threads=max(1, int(num_threads)),
        )
        model = TableStructureModelV2(True, None, options, accelerator)
        _MODEL_CACHE[key] = model
        return model


def detect_tables_docling_tableformer_v2_torch(
    pdf_path: str,
    logger,
    page_info: dict,
    pdf_lines: List[object],
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None,
    dpi: int = 144,
    pad_pt: float = 0.0,
    num_threads: int = 4,
) -> List[DoclingTableFormerRegion]:
    del page_info
    if not layout_regions_by_page or not is_docling_tableformer_v2_torch_available():
        return []

    from docling.datamodel.base_models import Cluster
    from docling_core.types.doc import BoundingBox, DocItemLabel

    lines_by_page: Dict[int, List[object]] = {}
    for line in pdf_lines or []:
        lines_by_page.setdefault(int(getattr(line, "page", 0) or 0), []).append(line)

    model = _get_model(num_threads=num_threads)
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
                    cluster_bbox: BBox = (float(crop.x0), float(crop.y0), float(crop.x1), float(crop.y1))
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
                        setattr(region, "engine", "docling_tableformer_v2_torch")
                        tables.append(region)
                except Exception as exc:
                    if logger is not None:
                        logger.log(f"docling_tableformer_v2_torch crop failed on page {page_num}: {exc}")
    finally:
        doc.close()

    if logger is not None:
        logger.log(f"docling_tableformer_v2_torch structure recognizer found {len(tables)} tables")
    return tables
