from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz
import numpy as np
from docx import Document
from lxml import html
from rapid_table import EngineType, ModelType, RapidTable, RapidTableInput
from rapidfuzz.distance import Levenshtein
from table_anchored_merger import (
    TableRegion,
    TextLine,
    clean_ocr_cell_text,
    extract_pdf_lines,
    filter_false_positive_tables,
    get_lines_in_rect,
    merge_raw_paragraphs,
    postprocess_table_layout_grids,
    repair_continued_tables,
    split_stacked_tables,
)


@dataclass
class TableSummary:
    scan: str
    index: int
    page: int
    bbox_pdf: tuple[float, float, float, float]
    rows: int
    cols: int
    text: str
    elapsed: float


class QuietLogger:
    def __init__(self):
        self.lines: list[str] = []

    def log(self, msg: str):
        self.lines.append(str(msg))


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "")
    return text.strip()


def docx_tables(path: Path) -> list[tuple[int, int, str]]:
    doc = Document(str(path))
    out = []
    for table in doc.tables:
        rows = len(table.rows)
        cols = len(table.columns)
        text = clean_text(" ".join(cell.text for row in table.rows for cell in row.cells))
        out.append((rows, cols, text))
    return out


def data_tables(tables: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    return [t for t in tables if t[:2] != (1, 2)]


def shape_score(gt: tuple[int, int], out: tuple[int, int]) -> float:
    gr, gc = gt
    or_, oc = out
    if gr <= 0 or gc <= 0 or or_ <= 0 or oc <= 0:
        return 0.0
    return (min(gr, or_) / max(gr, or_)) * (min(gc, oc) / max(gc, oc))


def text_acc(gt: str, out: str) -> float:
    gt = clean_text(gt)
    out = clean_text(out)
    if not gt and not out:
        return 1.0
    if not gt:
        return 0.0
    return max(0.0, 1.0 - Levenshtein.distance(gt, out) / len(gt))


def compare_tables(gt_tables: list[tuple[int, int, str]], out_tables: list[tuple[int, int, str]]) -> dict[str, Any]:
    n = max(len(gt_tables), len(out_tables))
    details = []
    shape_scores = []
    text_scores = []
    for idx in range(n):
        gt = gt_tables[idx] if idx < len(gt_tables) else (0, 0, "")
        out = out_tables[idx] if idx < len(out_tables) else (0, 0, "")
        ss = shape_score(gt[:2], out[:2])
        ta = text_acc(gt[2], out[2])
        shape_scores.append(ss)
        text_scores.append(ta)
        details.append({
            "index": idx,
            "gt_shape": list(gt[:2]),
            "out_shape": list(out[:2]),
            "shape_score": ss,
            "text_acc": ta,
        })
    return {
        "count": len(out_tables),
        "gt_count": len(gt_tables),
        "shape_acc": sum(shape_scores) / n if n else 1.0,
        "text_acc": sum(text_scores) / n if n else 1.0,
        "details": details,
    }


def load_layout_table_bboxes(json_path: Path) -> list[tuple[int, tuple[float, float, float, float]]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    out = []
    for page_idx, page in enumerate(data.get("pages", []), 1):
        for region in page.get("layout_regions", []):
            if region.get("type") != "table":
                continue
            bbox = region.get("bbox_pdf")
            if bbox and len(bbox) >= 4:
                out.append((page_idx, tuple(float(v) for v in bbox[:4])))
    out.sort(key=lambda item: (item[0], item[1][1]))
    return out


def load_layout_regions_by_page(json_path: Path) -> dict[int, list[dict[str, Any]]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return {
        page_idx: page.get("layout_regions", [])
        for page_idx, page in enumerate(data.get("pages", []), 1)
    }


def iter_ocr_lines(json_path: Path) -> dict[int, list[dict[str, Any]]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    by_page: dict[int, list[dict[str, Any]]] = {}
    for page_idx, page in enumerate(data.get("pages", []), 1):
        by_page[page_idx] = page.get("lines", [])
    return by_page


def line_xywh(line: dict[str, Any]) -> tuple[float, float, float, float]:
    if all(k in line for k in ("x", "y", "w", "h")):
        return (float(line["x"]), float(line["y"]), float(line["w"]), float(line["h"]))
    bbox = line.get("bbox") or [0, 0, 0, 0]
    x0, y0, x1, y1 = (float(v) for v in bbox[:4])
    return (x0, y0, x1 - x0, y1 - y0)


def crop_rect(page: fitz.Page, bbox: tuple[float, float, float, float], pad_pt: float) -> fitz.Rect:
    return fitz.Rect(
        max(0.0, bbox[0] - pad_pt),
        max(0.0, bbox[1] - pad_pt),
        min(page.rect.width, bbox[2] + pad_pt),
        min(page.rect.height, bbox[3] + pad_pt),
    )


def crop_and_ocr(page: fitz.Page,
                 page_lines: list[dict[str, Any]],
                 bbox: tuple[float, float, float, float],
                 dpi: int,
                 pad_pt: float) -> tuple[np.ndarray, tuple[np.ndarray, tuple[str, ...], tuple[float, ...]]]:
    scale = dpi / 72.0
    rect = crop_rect(page, bbox, pad_pt)
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]

    boxes = []
    texts = []
    scores = []
    for line in page_lines:
        text = clean_text(line.get("text") or line.get("ocr_text") or "")
        if not text:
            continue
        x, y, w, h = line_xywh(line)
        cx, cy = x + w / 2.0, y + h / 2.0
        if not (rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1):
            continue
        x0 = (max(x, rect.x0) - rect.x0) * scale
        y0 = (max(y, rect.y0) - rect.y0) * scale
        x1 = (min(x + w, rect.x1) - rect.x0) * scale
        y1 = (min(y + h, rect.y1) - rect.y0) * scale
        if x1 <= x0 or y1 <= y0:
            continue
        boxes.append([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
        texts.append(text)
        scores.append(float(line.get("confidence", 1.0) or 1.0))
    return img, (np.asarray(boxes, dtype=np.float32), tuple(texts), tuple(scores))


def html_text(pred_html: str | None) -> str:
    if not pred_html:
        return ""
    try:
        root = html.fromstring(pred_html)
        return clean_text(" ".join(root.itertext()))
    except Exception:
        return clean_text(re.sub(r"<[^>]+>", " ", pred_html))


def logic_shape(points: np.ndarray) -> tuple[int, int]:
    if points is None or len(points) == 0:
        return (0, 0)
    return (int(points[:, 1].max()) + 1, int(points[:, 3].max()) + 1)


def _rapid_cell_to_pdf_bbox(cell_bbox: np.ndarray,
                            crop: fitz.Rect,
                            scale: float) -> tuple[float, float, float, float]:
    values = np.asarray(cell_bbox, dtype=float).reshape(-1)
    if values.size >= 8:
        xs = values[0::2]
        ys = values[1::2]
    else:
        xs = values[[0, 2]]
        ys = values[[1, 3]]
    x0 = crop.x0 + float(xs.min()) / scale
    y0 = crop.y0 + float(ys.min()) / scale
    x1 = crop.x0 + float(xs.max()) / scale
    y1 = crop.y0 + float(ys.max()) / scale
    return (x0, y0, x1, y1)


def _cell_text_from_pdf_lines(lines: list[TextLine],
                              bbox: tuple[float, float, float, float],
                              page_num: int,
                              logger: QuietLogger) -> str:
    cell_lines = get_lines_in_rect(bbox, lines)
    if not cell_lines:
        return ""
    paragraphs = merge_raw_paragraphs(cell_lines, {page_num: (bbox[0], bbox[2])}, logger)
    return clean_ocr_cell_text("\n".join(p[0] for p in paragraphs))


def rapid_result_to_table(result: Any,
                          page_num: int,
                          crop: fitz.Rect,
                          page_lines: list[TextLine],
                          dpi: int,
                          logger: QuietLogger) -> TableRegion | None:
    logic_points = result.logic_points[0] if result.logic_points else np.empty((0, 4))
    cell_bboxes = result.cell_bboxes[0] if result.cell_bboxes else np.empty((0, 4))
    if logic_points is None or len(logic_points) == 0 or cell_bboxes is None or len(cell_bboxes) == 0:
        return None

    rows, cols = logic_shape(logic_points)
    if rows <= 0 or cols <= 0:
        return None

    cells = [[""] * cols for _ in range(rows)]
    bboxes = [[(0.0, 0.0, 0.0, 0.0)] * cols for _ in range(rows)]
    scale = dpi / 72.0

    for idx, point in enumerate(logic_points):
        if idx >= len(cell_bboxes):
            break
        r0, r1, c0, c1 = [int(v) for v in point[:4]]
        if r0 < 0 or c0 < 0 or r1 < r0 or c1 < c0:
            continue
        pdf_bbox = _rapid_cell_to_pdf_bbox(cell_bboxes[idx], crop, scale)
        text = _cell_text_from_pdf_lines(page_lines, pdf_bbox, page_num, logger)
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
        y_top = crop.y0
        y_bottom = crop.y1
        x_left = crop.x0
        x_right = crop.x1

    table = TableRegion(
        page=page_num,
        y_top=y_top,
        y_bottom=y_bottom,
        cells=cells,
        row_count=rows,
        col_count=cols,
        cell_bboxes=bboxes,
    )
    setattr(table, "x_left", x_left)
    setattr(table, "x_right", x_right)
    setattr(table, "source", "rapidtable_slanet_plus")
    return table


def postprocess_pipeline_tables(tables: list[TableRegion],
                                layout_regions_by_page: dict[int, list[dict[str, Any]]],
                                pdf_lines: list[TextLine],
                                page_info: dict,
                                logger: QuietLogger) -> list[TableRegion]:
    processed = repair_continued_tables(tables, layout_regions_by_page, pdf_lines, page_info, logger)
    processed = split_stacked_tables(processed, logger)
    processed = postprocess_table_layout_grids(processed, layout_regions_by_page, logger)
    processed = filter_false_positive_tables(processed, layout_regions_by_page, logger)
    return [t for t in processed if not getattr(t, "skip_render", False)]


def table_region_data(tables: list[TableRegion]) -> list[tuple[int, int, str]]:
    out = []
    for table in tables:
        text = clean_text(" ".join(str(cell) for row in table.cells for cell in row))
        out.append((table.row_count, table.col_count, text))
    return data_tables(out)


def run_scan(engine: RapidTable,
             scan: str,
             ocr_pdf: Path,
             out_dir: Path,
             dpi: int,
             pad_pt: float) -> tuple[list[TableSummary], list[TableRegion], list[str]]:
    bboxes = load_layout_table_bboxes(Path(str(ocr_pdf) + ".json"))
    lines_by_page = iter_ocr_lines(Path(str(ocr_pdf) + ".json"))
    logger = QuietLogger()
    pdf_lines, _ = extract_pdf_lines(str(ocr_pdf), logger)
    pdf_lines_by_page: dict[int, list[TextLine]] = {}
    for line in pdf_lines:
        pdf_lines_by_page.setdefault(line.page, []).append(line)
    summaries: list[TableSummary] = []
    table_regions: list[TableRegion] = []
    doc = fitz.open(str(ocr_pdf))
    try:
        for idx, (page_num, bbox) in enumerate(bboxes):
            crop = crop_rect(doc[page_num - 1], bbox, pad_pt)
            img, ocr_result = crop_and_ocr(doc[page_num - 1], lines_by_page.get(page_num, []), bbox, dpi, pad_pt)
            import cv2
            img_for_rapidtable = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            t0 = time.perf_counter()
            result = engine(img_for_rapidtable, ocr_results=[ocr_result], batch_size=1)
            elapsed = time.perf_counter() - t0
            rows, cols = logic_shape(result.logic_points[0] if result.logic_points else np.empty((0, 4)))
            text = html_text(result.pred_htmls[0] if result.pred_htmls else "")
            summaries.append(TableSummary(scan, idx, page_num, bbox, rows, cols, text, elapsed))
            table = rapid_result_to_table(result, page_num, crop, pdf_lines_by_page.get(page_num, []), dpi, logger)
            if table is not None:
                table_regions.append(table)
            crop_path = out_dir / f"{scan}_table{idx:02d}_p{page_num}.png"
            if not crop_path.exists():
                cv2.imwrite(str(crop_path), img_for_rapidtable)
    finally:
        doc.close()
    return summaries, table_regions, logger.lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ocr-dir", type=Path, default=Path(r"D:\App\ocrtool\temp\groundtruth4_pipeline"))
    parser.add_argument("--groundtruth-dir", type=Path, default=Path(r"C:\Users\nhquan\Downloads\groundtruth"))
    parser.add_argument("--gt03-docx", type=Path, default=Path(r"D:\App\ocrtool\temp\groundtruth4_scan_word\groundtruth03_converted.docx"))
    parser.add_argument("--current-dir", type=Path, default=Path(r"D:\App\ocrtool\temp\groundtruth4_pipeline"))
    parser.add_argument("--out-dir", type=Path, default=Path(r"D:\App\ocrtool\temp\rapidtable_slanet_bench"))
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--pad-pt", type=float, default=2.0)
    parser.add_argument("--scans", nargs="+", default=["01", "02", "03", "04"])
    parser.add_argument(
        "--model-type",
        choices=[item.value for item in ModelType],
        default=ModelType.SLANETPLUS.value,
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    cfg = RapidTableInput(
        model_type=ModelType(args.model_type),
        engine_type=EngineType.ONNXRUNTIME,
        use_ocr=True,
        engine_cfg={"intra_op_num_threads": 4, "inter_op_num_threads": 1},
    )
    t0 = time.perf_counter()
    engine = RapidTable(cfg)
    init_elapsed = time.perf_counter() - t0

    scans = [str(scan).zfill(2) for scan in args.scans]
    all_summaries: dict[str, list[TableSummary]] = {}
    all_pipeline_tables: dict[str, list[TableRegion]] = {}
    all_pipeline_logs: dict[str, list[str]] = {}
    comparison = []

    for scan in scans:
        ocr_pdf = args.ocr_dir / f"scan{scan}_ocr.pdf"
        summaries, rapid_tables, rapid_logs = run_scan(engine, scan, ocr_pdf, args.out_dir, args.dpi, args.pad_pt)
        layout_regions_by_page = load_layout_regions_by_page(Path(str(ocr_pdf) + ".json"))
        pipeline_logger = QuietLogger()
        pdf_lines, page_info = extract_pdf_lines(str(ocr_pdf), pipeline_logger)
        post_t0 = time.perf_counter()
        rapid_pipeline_tables = postprocess_pipeline_tables(
            rapid_tables,
            layout_regions_by_page,
            pdf_lines,
            page_info,
            pipeline_logger,
        )
        post_elapsed = time.perf_counter() - post_t0
        all_summaries[scan] = summaries
        all_pipeline_tables[scan] = rapid_pipeline_tables
        all_pipeline_logs[scan] = rapid_logs + pipeline_logger.lines

        gt_docx = args.gt03_docx if scan == "03" else args.groundtruth_dir / f"groundtruth{scan}.docx"
        gt_data = data_tables(docx_tables(gt_docx))
        cur_data = data_tables(docx_tables(args.current_dir / f"scan{scan}_final.docx"))
        rt_data = [(s.rows, s.cols, s.text) for s in summaries]
        rt_pipeline_data = table_region_data(rapid_pipeline_tables)

        comparison.append({
            "scan": scan,
            "rapidtable": compare_tables(gt_data, rt_data),
            "rapidtable_pipeline_postprocess": compare_tables(gt_data, rt_pipeline_data),
            "current": compare_tables(gt_data, cur_data),
            "rapidtable_shapes": [[s.rows, s.cols] for s in summaries],
            "rapidtable_pipeline_shapes": [list(t[:2]) for t in rt_pipeline_data],
            "current_shapes": [list(t[:2]) for t in cur_data],
            "gt_shapes": [list(t[:2]) for t in gt_data],
            "rapidtable_time_sec": sum(s.elapsed for s in summaries),
            "rapidtable_avg_table_sec": (sum(s.elapsed for s in summaries) / len(summaries)) if summaries else 0.0,
            "rapidtable_postprocess_time_sec": post_elapsed,
            "rapidtable_total_with_postprocess_sec": sum(s.elapsed for s in summaries) + post_elapsed,
        })

    with (args.out_dir / "rapidtable_tables.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["scan", "index", "page", "rows", "cols", "elapsed", "text"])
        for summaries in all_summaries.values():
            for s in summaries:
                writer.writerow([s.scan, s.index, s.page, s.rows, s.cols, f"{s.elapsed:.6f}", s.text])

    with (args.out_dir / "rapidtable_pipeline_tables.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["scan", "index", "page", "rows", "cols", "text"])
        for scan, tables in all_pipeline_tables.items():
            for idx, table in enumerate(tables):
                writer.writerow([
                    scan,
                    idx,
                    table.page,
                    table.row_count,
                    table.col_count,
                    clean_text(" ".join(str(cell) for row in table.cells for cell in row)),
                ])

    (args.out_dir / "rapidtable_pipeline_logs.json").write_text(
        json.dumps(all_pipeline_logs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result = {
        "model": f"RapidTable {args.model_type} ONNX",
        "init_elapsed_sec": init_elapsed,
        "dpi": args.dpi,
        "pad_pt": args.pad_pt,
        "comparison": comparison,
    }
    (args.out_dir / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    with (args.out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "scan",
            "rt_table_count",
            "rt_pipe_table_count",
            "gt_data_count",
            "rt_shape_acc",
            "rt_pipe_shape_acc",
            "cur_shape_acc",
            "rt_text_acc",
            "rt_pipe_text_acc",
            "cur_text_acc",
            "rt_time_sec",
            "rt_avg_table_sec",
            "rt_postprocess_time_sec",
            "rt_total_with_postprocess_sec",
        ])
        for item in comparison:
            writer.writerow([
                item["scan"],
                item["rapidtable"]["count"],
                item["rapidtable_pipeline_postprocess"]["count"],
                item["rapidtable"]["gt_count"],
                item["rapidtable"]["shape_acc"],
                item["rapidtable_pipeline_postprocess"]["shape_acc"],
                item["current"]["shape_acc"],
                item["rapidtable"]["text_acc"],
                item["rapidtable_pipeline_postprocess"]["text_acc"],
                item["current"]["text_acc"],
                item["rapidtable_time_sec"],
                item["rapidtable_avg_table_sec"],
                item["rapidtable_postprocess_time_sec"],
                item["rapidtable_total_with_postprocess_sec"],
            ])

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
