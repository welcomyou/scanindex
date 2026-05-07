from __future__ import annotations

import argparse
import copy
import csv
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import benchmark_borderless_ctdar_current as ctdar_bench  # noqa: E402
import benchmark_current_table_pipeline as gt_bench  # noqa: E402
from benchmark_ctdar_pair_ensembles_ocr import (  # noqa: E402
    ensure_ocr_pdf as ensure_ctdar_ocr_pdf,
    image_to_pdf as ctdar_image_to_pdf,
    prepare_inputs as prepare_ctdar_inputs,
)
from benchmark_external_pair_ensembles import (  # noqa: E402
    best_table_result,
    compare_text_grid,
    ensure_ocr_pdf as ensure_external_ocr_pdf,
    full_page_layout_from_page_info,
    image_to_pdf as external_image_to_pdf,
    load_gt_text_grid,
)
from benchmark_external_table_gt_samples import load_samples, shape_score  # noqa: E402
from benchmark_groundtruth_engine_matrix import QuietLogger  # noqa: E402
from benchmark_pair_ensembles import ENGINE_SPECS, group_by_page, run_engine  # noqa: E402
from table_anchored_merger import (  # noqa: E402
    _candidate_set_score,
    _layout_table_bboxes,
    _table_bbox,
)
from table_postprocess_v2 import table_ocr_fit_score  # noqa: E402


TRIPLE_ENGINE_IDS = ("gmft_ocr", "docling_tableformer_ocr", "rapidtable_ocr")

TRIPLE_DEFS: list[dict[str, Any]] = [
    {
        "order": 1,
        "id": "gmft_docling_rapidtable_selector",
        "label": "GMFT + Docling + RapidTable selector",
        "mode": "selector",
    },
    {
        "order": 2,
        "id": "gmft_docling_rapidtable_consensus",
        "label": "GMFT + Docling + RapidTable consensus repair",
        "mode": "consensus",
    },
]


def _bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
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


def _line_bbox(line: Any) -> tuple[float, float, float, float]:
    x = float(getattr(line, "x", 0.0) or 0.0)
    y = float(getattr(line, "y", 0.0) or 0.0)
    w = float(getattr(line, "width", 0.0) or 0.0)
    h = float(getattr(line, "height", 0.0) or 0.0)
    return x, y, x + w, y + h


def _median_ocr_height(pdf_lines: list[Any], page: int, bbox: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = bbox
    heights: list[float] = []
    for line in pdf_lines or []:
        if int(getattr(line, "page", 0) or 0) != page:
            continue
        words = getattr(line, "word_items", None) or []
        if words:
            for word in words:
                wx = float(word.get("x", 0.0) or 0.0)
                wy = float(word.get("y", 0.0) or 0.0)
                ww = float(word.get("w", 0.0) or 0.0)
                wh = float(word.get("h", 0.0) or 0.0)
                if ww <= 0 or wh <= 0:
                    continue
                cx, cy = wx + ww / 2.0, wy + wh / 2.0
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    heights.append(wh)
            continue
        lx0, ly0, lx1, ly1 = _line_bbox(line)
        cx, cy = (lx0 + lx1) / 2.0, (ly0 + ly1) / 2.0
        if x0 <= cx <= x1 and y0 <= cy <= y1 and ly1 > ly0:
            heights.append(ly1 - ly0)
    return statistics.median(heights) if heights else 8.0


def _cell_boundaries(table: Any, axis: str) -> list[float]:
    idxs = (0, 2) if axis == "x" else (1, 3)
    values: list[float] = []
    for row in getattr(table, "cell_bboxes", []) or []:
        for raw in row:
            if len(raw) < 4:
                continue
            x0, y0, x1, y1 = [float(v) for v in raw[:4]]
            if x1 <= x0 or y1 <= y0:
                continue
            values.extend([float(raw[idxs[0]]), float(raw[idxs[1]])])
    if not values:
        bbox = _table_bbox(table)
        values = [bbox[idxs[0]], bbox[idxs[1]]]
    return sorted(values)


def _cluster_positions(values: list[tuple[float, str]], tolerance: float) -> list[dict[str, Any]]:
    if not values:
        return []
    values = sorted(values, key=lambda item: item[0])
    clusters: list[list[tuple[float, str]]] = [[values[0]]]
    for pos, source in values[1:]:
        mean = sum(item[0] for item in clusters[-1]) / len(clusters[-1])
        if abs(pos - mean) <= tolerance:
            clusters[-1].append((pos, source))
        else:
            clusters.append([(pos, source)])
    out: list[dict[str, Any]] = []
    for cluster in clusters:
        out.append(
            {
                "pos": sum(item[0] for item in cluster) / len(cluster),
                "sources": {item[1] for item in cluster},
                "count": len(cluster),
            }
        )
    return out


def _unique_boundaries(values: list[float], tolerance: float) -> list[float]:
    clusters = _cluster_positions([(v, "base") for v in values], tolerance)
    return [float(cluster["pos"]) for cluster in clusters]


def _nearest_supported(pos: float, supported: list[dict[str, Any]], tolerance: float) -> float:
    if not supported:
        return pos
    nearest = min(supported, key=lambda item: abs(float(item["pos"]) - pos))
    if abs(float(nearest["pos"]) - pos) <= tolerance and len(nearest["sources"]) >= 2:
        return float(nearest["pos"])
    return pos


def _snap_value(value: float, old_lines: list[float], new_lines: list[float], tolerance: float) -> float:
    if not old_lines or len(old_lines) != len(new_lines):
        return value
    idx = min(range(len(old_lines)), key=lambda i: abs(old_lines[i] - value))
    if abs(old_lines[idx] - value) <= tolerance * 1.5:
        return new_lines[idx]
    return value


def _matching_tables_by_source(
    target: Any,
    candidate_sets: dict[str, list[Any]],
    min_iou: float = 0.35,
) -> list[tuple[str, Any]]:
    target_bbox = _table_bbox(target)
    matches: list[tuple[str, Any]] = []
    for source, tables in candidate_sets.items():
        best = None
        best_iou = 0.0
        for table in tables:
            if int(getattr(table, "page", 0) or 0) != int(getattr(target, "page", 0) or 0):
                continue
            iou = _bbox_iou(target_bbox, _table_bbox(table))
            if iou > best_iou:
                best_iou = iou
                best = table
        if best is not None and best_iou >= min_iou:
            matches.append((source, best))
    return matches


def _snap_table_boundaries_to_consensus(
    table: Any,
    matches: list[tuple[str, Any]],
    pdf_lines: list[Any],
    logger: Any,
) -> None:
    if len({source for source, _table in matches}) < 2:
        return
    table_bbox = _table_bbox(table)
    width = max(table_bbox[2] - table_bbox[0], 1.0)
    height = max(table_bbox[3] - table_bbox[1], 1.0)
    median_h = _median_ocr_height(pdf_lines, int(getattr(table, "page", 0) or 0), table_bbox)
    x_tol = max(width * 0.006, median_h * 0.35, 1.0)
    y_tol = max(height * 0.006, median_h * 0.5, 1.0)

    old_x = _unique_boundaries(_cell_boundaries(table, "x"), x_tol)
    old_y = _unique_boundaries(_cell_boundaries(table, "y"), y_tol)
    if len(old_x) < 2 or len(old_y) < 2:
        return

    x_votes: list[tuple[float, str]] = []
    y_votes: list[tuple[float, str]] = []
    for source, candidate in matches:
        x_votes.extend((value, source) for value in _unique_boundaries(_cell_boundaries(candidate, "x"), x_tol))
        y_votes.extend((value, source) for value in _unique_boundaries(_cell_boundaries(candidate, "y"), y_tol))

    supported_x = _cluster_positions(x_votes, x_tol)
    supported_y = _cluster_positions(y_votes, y_tol)
    new_x = [_nearest_supported(value, supported_x, x_tol) for value in old_x]
    new_y = [_nearest_supported(value, supported_y, y_tol) for value in old_y]
    if any(new_x[i] >= new_x[i + 1] for i in range(len(new_x) - 1)):
        return
    if any(new_y[i] >= new_y[i + 1] for i in range(len(new_y) - 1)):
        return

    changed = False
    snapped_boxes = []
    for row in getattr(table, "cell_bboxes", []) or []:
        snapped_row = []
        for raw in row:
            if len(raw) < 4:
                snapped_row.append(raw)
                continue
            x0, y0, x1, y1 = [float(v) for v in raw[:4]]
            sx0 = _snap_value(x0, old_x, new_x, x_tol)
            sx1 = _snap_value(x1, old_x, new_x, x_tol)
            sy0 = _snap_value(y0, old_y, new_y, y_tol)
            sy1 = _snap_value(y1, old_y, new_y, y_tol)
            if sx1 <= sx0 or sy1 <= sy0:
                snapped_row.append(raw)
                continue
            changed = changed or any(abs(a - b) > 0.25 for a, b in ((x0, sx0), (x1, sx1), (y0, sy0), (y1, sy1)))
            snapped_row.append((sx0, sy0, sx1, sy1))
        snapped_boxes.append(snapped_row)
    if not changed:
        return

    table.cell_bboxes = snapped_boxes
    valid = [bbox for row in snapped_boxes for bbox in row if len(bbox) >= 4 and bbox[2] > bbox[0] and bbox[3] > bbox[1]]
    if valid:
        setattr(table, "x_left", min(b[0] for b in valid))
        setattr(table, "x_right", max(b[2] for b in valid))
        table.y_top = min(b[1] for b in valid)
        table.y_bottom = max(b[3] for b in valid)
    if logger is not None:
        logger.log(f"  Triple consensus snapped table boundaries on page {getattr(table, 'page', 0)}")


def _score_candidate_set(
    source: str,
    tables: list[Any],
    layout_bboxes: list[tuple[float, float, float, float]],
    pdf_lines: list[Any],
) -> tuple[float, float, float, str, list[Any]]:
    layout_score = _candidate_set_score(tables, layout_bboxes)
    ocr_score = sum(table_ocr_fit_score(table, pdf_lines) for table in tables) / max(len(tables), 1)
    return layout_score + ocr_score, layout_score, ocr_score, source, tables


def combine_triple(
    mode: str,
    engine_results: dict[str, dict[str, Any]],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    logger: Any,
    pdf_lines: list[Any],
) -> tuple[list[Any], float]:
    by_source = {
        result["source"]: group_by_page(result["tables"])
        for result in engine_results.values()
    }
    pages = sorted(set().union(*(set(page_map.keys()) for page_map in by_source.values())))
    selected: list[Any] = []
    t0 = time.perf_counter()
    for page in pages:
        candidate_sets = {source: page_map.get(page, []) for source, page_map in by_source.items()}
        layout_bboxes = _layout_table_bboxes(layout_regions_by_page, page)
        scored = [
            _score_candidate_set(source, tables, layout_bboxes, pdf_lines)
            for source, tables in candidate_sets.items()
            if tables
        ]
        if not scored:
            continue
        scored.sort(key=lambda item: item[0], reverse=True)
        _score, _layout_score, _ocr_score, source, chosen_tables = scored[0]
        summary = ", ".join(
            f"{src}:{score:.2f}=L{ls:.2f}+O{os:.2f}/{len(tables)}"
            for score, ls, os, src, tables in scored
        )
        logger.log(f"  Page {page}: Triple {mode} selected {source} ({summary})")
        chosen = copy.deepcopy(chosen_tables)
        if mode == "consensus":
            for table in chosen:
                matches = _matching_tables_by_source(table, candidate_sets)
                _snap_table_boundaries_to_consensus(table, matches, pdf_lines, logger)
        selected.extend(chosen)
    return selected, time.perf_counter() - t0


def run_triple_engines(
    benchmark_pdf: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for spec_id in TRIPLE_ENGINE_IDS:
        print(f"  engine {ENGINE_SPECS[spec_id]['label']}", flush=True)
        results[spec_id] = run_engine(
            spec_id,
            benchmark_pdf,
            logger,
            page_info,
            pdf_lines,
            layout_regions_by_page,
            args,
        )
    return results


def run_groundtruth_scan(scan: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    ocr_pdf = args.ocr_dir / f"scan{scan}_ocr.pdf"
    logger = QuietLogger()
    prep_t0 = time.perf_counter()
    pdf_lines, layout_regions_by_page, page_info = gt_bench.prepare_pipeline_inputs(ocr_pdf, logger)
    preprocess_sec = time.perf_counter() - prep_t0

    engine_results = run_triple_engines(ocr_pdf, logger, page_info, pdf_lines, layout_regions_by_page, args)
    gt_docx = gt_bench.groundtruth_docx_for_scan(scan, args.groundtruth_dir, args.gt03_docx)
    gt_data = gt_bench.data_tables(gt_bench.docx_tables(gt_docx))
    gt_grids = gt_bench.data_table_grids(gt_bench.docx_table_grids(gt_docx))

    rows: list[dict[str, Any]] = []
    for variant in TRIPLE_DEFS:
        raw_tables, select_sec = combine_triple(variant["mode"], engine_results, layout_regions_by_page, logger, pdf_lines)
        post_t0 = time.perf_counter()
        out_tables = gt_bench.postprocess_current_tables(
            copy.deepcopy(raw_tables),
            layout_regions_by_page,
            pdf_lines,
            page_info,
            logger,
        )
        post_sec = time.perf_counter() - post_t0
        out_data = gt_bench.table_region_data(out_tables)
        out_grids = gt_bench.table_region_grids(out_tables)
        cmp = gt_bench.compare_tables(gt_data, out_data)
        cell_cmp = gt_bench.compare_table_cells(gt_grids, out_grids)
        cmp.update(
            {
                "cell_exact_rate": cell_cmp["cell_exact_rate"],
                "cell_text_acc": cell_cmp["cell_text_acc"],
                "table_exact_text_rate": cell_cmp["table_exact_text_rate"],
                "compared_cells": cell_cmp["compared_cells"],
                "cell_details": cell_cmp["details"],
            }
        )
        engine_parallel = max(result["timing"]["engine_total_sec"] for result in engine_results.values())
        engine_seq = sum(result["timing"]["engine_total_sec"] for result in engine_results.values())
        rows.append(
            {
                "dataset": "5_groundtruth",
                "id": f"scan{scan}",
                "variant_id": variant["id"],
                "label": variant["label"],
                "variant_order": variant["order"],
                "comparison": cmp,
                "timing": {
                    "preprocess_sec": preprocess_sec,
                    "engine_parallel_est_sec": engine_parallel,
                    "engine_sequential_sec": engine_seq,
                    "select_sec": select_sec,
                    "postprocess_sec": post_sec,
                    "table_total_parallel_est_sec": engine_parallel + select_sec + post_sec,
                    "table_total_sequential_sec": engine_seq + select_sec + post_sec,
                },
                "out_shapes": [list(t[:2]) for t in out_data],
                "sources": [getattr(t, "source", "") for t in out_tables],
                "log_tail": logger.lines[-80:],
            }
        )
    return rows


def run_external_sample(sample: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    dataset_slug = str(sample["dataset"]).replace(" ", "_").replace(".", "_").lower()
    raw_pdf = args.out_dir / "external_raw_pdfs" / dataset_slug / f"{sample['id']}.pdf"
    ocr_pdf = args.out_dir / "external_ocr_pdfs" / dataset_slug / f"{sample['id']}_ocr.pdf"
    external_image_to_pdf(Path(sample["image"]), raw_pdf, args.pdf_dpi)
    ocr_status = ensure_external_ocr_pdf(raw_pdf, ocr_pdf, Path(sample["image"]), args.force_ocr)
    benchmark_pdf = ocr_pdf if ocr_status["ok"] and ocr_pdf.exists() else raw_pdf

    logger = QuietLogger()
    prep_t0 = time.perf_counter()
    pdf_lines, _json_layout_regions, page_info = gt_bench.prepare_pipeline_inputs(benchmark_pdf, logger)
    layout_regions_by_page = full_page_layout_from_page_info(page_info, args.pdf_dpi)
    preprocess_sec = time.perf_counter() - prep_t0
    gt_grid = load_gt_text_grid(Path(sample["groundtruth"]))
    engine_results = run_triple_engines(benchmark_pdf, logger, page_info, pdf_lines, layout_regions_by_page, args)

    rows: list[dict[str, Any]] = []
    for variant in TRIPLE_DEFS:
        raw_tables, select_sec = combine_triple(variant["mode"], engine_results, layout_regions_by_page, logger, pdf_lines)
        post_t0 = time.perf_counter()
        out_tables = gt_bench.postprocess_current_tables(
            copy.deepcopy(raw_tables),
            layout_regions_by_page,
            pdf_lines,
            page_info,
            logger,
        )
        post_sec = time.perf_counter() - post_t0
        pred_rows, pred_cols, pred_count, sources, pred_grid = best_table_result(out_tables)
        text_cmp = compare_text_grid(gt_grid, pred_grid)
        engine_parallel = max(result["timing"]["engine_total_sec"] for result in engine_results.values())
        engine_seq = sum(result["timing"]["engine_total_sec"] for result in engine_results.values())
        rows.append(
            {
                "dataset": sample["dataset"],
                "id": sample["id"],
                "variant_id": variant["id"],
                "label": variant["label"],
                "variant_order": variant["order"],
                "gt_shape": f"{sample['gt_rows']}x{sample['gt_cols']}",
                "pred_shape": f"{pred_rows}x{pred_cols}",
                "shape_acc": shape_score(sample["gt_rows"], sample["gt_cols"], pred_rows, pred_cols),
                "exact_shape": sample["gt_rows"] == pred_rows and sample["gt_cols"] == pred_cols,
                "cell_exact_rate": text_cmp["cell_exact_rate"],
                "cell_text_acc": text_cmp["cell_text_acc"],
                "table_exact_text": text_cmp["table_exact_text"],
                "compared_cells": text_cmp["compared_cells"],
                "pred_count": pred_count,
                "sources": sources,
                "ocr_line_count": len(pdf_lines),
                "timing": {
                    "ocr_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0),
                    "preprocess_sec": preprocess_sec,
                    "engine_parallel_est_sec": engine_parallel,
                    "engine_sequential_sec": engine_seq,
                    "select_sec": select_sec,
                    "postprocess_sec": post_sec,
                    "table_total_parallel_est_sec": engine_parallel + select_sec + post_sec,
                    "table_total_sequential_sec": engine_seq + select_sec + post_sec,
                    "end_to_end_parallel_est_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0)
                    + preprocess_sec
                    + engine_parallel
                    + select_sec
                    + post_sec,
                },
                "image": str(sample["image"]),
                "groundtruth": str(sample["groundtruth"]),
                "text_mismatches": text_cmp.get("mismatches", []),
                "log_tail": logger.lines[-80:],
            }
        )
    return rows


def run_ctdar_case(candidate: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    image_path = Path(candidate["image"])
    xml_path = Path(candidate["xml"])
    pdf_scale = 72.0 / float(args.ctdar_pdf_dpi)
    gt_tables = ctdar_bench.parse_ctdar_xml(xml_path, pdf_scale)

    raw_pdf = args.out_dir / "ctdar_raw_pdfs" / f"{candidate['id']}.pdf"
    ocr_pdf = args.out_dir / "ctdar_ocr_pdfs" / f"{candidate['id']}_ocr.pdf"
    width_pt, _height_pt = ctdar_image_to_pdf(image_path, raw_pdf, args.ctdar_pdf_dpi)
    ocr_status = ensure_ctdar_ocr_pdf(raw_pdf, ocr_pdf, image_path, args.force_ocr)
    benchmark_pdf = ocr_pdf if ocr_status["ok"] and ocr_pdf.exists() else raw_pdf

    prep_t0 = time.perf_counter()
    pdf_lines, layout_regions_by_page, page_info, ocr_line_count = prepare_ctdar_inputs(benchmark_pdf, args)
    preprocess_sec = time.perf_counter() - prep_t0

    logger = QuietLogger()
    engine_results = run_triple_engines(benchmark_pdf, logger, page_info, pdf_lines, layout_regions_by_page, args)
    rows: list[dict[str, Any]] = []
    for variant in TRIPLE_DEFS:
        raw_tables, select_sec = combine_triple(variant["mode"], engine_results, layout_regions_by_page, logger, pdf_lines)
        post_t0 = time.perf_counter()
        out_tables = gt_bench.postprocess_current_tables(
            copy.deepcopy(raw_tables),
            layout_regions_by_page,
            pdf_lines,
            page_info,
            logger,
        )
        post_sec = time.perf_counter() - post_t0
        cmp = ctdar_bench.evaluate(gt_tables, out_tables, width_pt)
        engine_parallel = max(result["timing"]["engine_total_sec"] for result in engine_results.values())
        engine_seq = sum(result["timing"]["engine_total_sec"] for result in engine_results.values())
        rows.append(
            {
                "dataset": "cTDaR_TRACKB2",
                "id": candidate["id"],
                "variant_id": variant["id"],
                "label": variant["label"],
                "variant_order": variant["order"],
                "comparison": cmp,
                "gt_shapes": [[t.rows, t.cols] for t in gt_tables],
                "ocr_line_count": ocr_line_count,
                "timing": {
                    "ocr_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0),
                    "preprocess_sec": preprocess_sec,
                    "engine_parallel_est_sec": engine_parallel,
                    "engine_sequential_sec": engine_seq,
                    "select_sec": select_sec,
                    "postprocess_sec": post_sec,
                    "table_total_parallel_est_sec": engine_parallel + select_sec + post_sec,
                    "table_total_sequential_sec": engine_seq + select_sec + post_sec,
                    "end_to_end_parallel_est_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0)
                    + preprocess_sec
                    + engine_parallel
                    + select_sec
                    + post_sec,
                },
                "sources": cmp["sources"],
                "log_tail": logger.lines[-80:],
            }
        )
    return rows


def write_groundtruth_outputs(results: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "details.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        grouped.setdefault(row["variant_id"], []).append(row)
    summary = []
    for variant_id, rows in grouped.items():
        n = len(rows)
        summary.append(
            {
                "variant_id": variant_id,
                "label": rows[0]["label"],
                "cases": n,
                "shape_acc": sum(r["comparison"]["shape_acc"] for r in rows) / n,
                "text_acc": sum(r["comparison"]["text_acc"] for r in rows) / n,
                "cell_exact_rate": sum(r["comparison"]["cell_exact_rate"] for r in rows) / n,
                "cell_text_acc": sum(r["comparison"]["cell_text_acc"] for r in rows) / n,
                "table_exact_text_rate": sum(r["comparison"]["table_exact_text_rate"] for r in rows) / n,
                "compared_cells": sum(r["comparison"]["compared_cells"] for r in rows),
                "select_sec": sum(r["timing"]["select_sec"] for r in rows),
                "postprocess_sec": sum(r["timing"]["postprocess_sec"] for r in rows),
                "table_total_parallel_est_sec": sum(r["timing"]["table_total_parallel_est_sec"] for r in rows),
                "table_total_sequential_sec": sum(r["timing"]["table_total_sequential_sec"] for r in rows),
            }
        )
    summary.sort(key=lambda row: row["variant_id"])
    _write_csv(out_dir / "summary.csv", summary)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def write_external_outputs(results: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "details.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    overall: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        grouped.setdefault((str(row["dataset"]), row["variant_id"]), []).append(row)
        overall.setdefault(row["variant_id"], []).append(row)

    def summarize(dataset: str, variant_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rows)
        return {
            "dataset": dataset,
            "variant_id": variant_id,
            "label": rows[0]["label"],
            "cases": n,
            "shape_acc": sum(float(r["shape_acc"]) for r in rows) / n,
            "exact_shape_rate": sum(1.0 if r["exact_shape"] else 0.0 for r in rows) / n,
            "cell_exact_rate": sum(float(r["cell_exact_rate"]) for r in rows) / n,
            "cell_text_acc": sum(float(r["cell_text_acc"]) for r in rows) / n,
            "table_exact_text_rate": sum(1.0 if r["table_exact_text"] else 0.0 for r in rows) / n,
            "compared_cells": sum(int(r["compared_cells"]) for r in rows),
            "pred_count": sum(int(r["pred_count"]) for r in rows),
            "ocr_line_count_avg": sum(int(r["ocr_line_count"]) for r in rows) / n,
            "select_sec": sum(float(r["timing"]["select_sec"]) for r in rows),
            "postprocess_sec": sum(float(r["timing"]["postprocess_sec"]) for r in rows),
            "table_total_parallel_est_sec": sum(float(r["timing"]["table_total_parallel_est_sec"]) for r in rows),
            "table_total_sequential_sec": sum(float(r["timing"]["table_total_sequential_sec"]) for r in rows),
            "end_to_end_parallel_est_sec": sum(float(r["timing"]["end_to_end_parallel_est_sec"]) for r in rows),
        }

    summary = [summarize(dataset, variant_id, rows) for (dataset, variant_id), rows in grouped.items()]
    summary.extend(summarize("__overall__", variant_id, rows) for variant_id, rows in overall.items())
    summary.sort(key=lambda row: (row["dataset"], row["variant_id"]))
    _write_csv(out_dir / "summary.csv", summary)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def write_ctdar_outputs(results: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "details.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        grouped.setdefault(row["variant_id"], []).append(row)
    summary = []
    for variant_id, rows in grouped.items():
        n = len(rows)
        summary.append(
            {
                "variant_id": variant_id,
                "label": rows[0]["label"],
                "cases": n,
                "shape_acc": sum(r["comparison"]["shape_acc"] for r in rows) / n,
                "exact_shape_rate": sum(r["comparison"]["exact_shape_rate"] for r in rows) / n,
                "recall_iou50": sum(r["comparison"]["recall_iou50"] for r in rows) / n,
                "mean_iou": sum(r["comparison"]["mean_best_iou"] for r in rows) / n,
                "pred_count": sum(r["comparison"]["pred_count"] for r in rows),
                "gt_count": sum(r["comparison"]["gt_count"] for r in rows),
                "ocr_line_count_avg": sum(r["ocr_line_count"] for r in rows) / n,
                "select_sec": sum(r["timing"]["select_sec"] for r in rows),
                "postprocess_sec": sum(r["timing"]["postprocess_sec"] for r in rows),
                "table_total_parallel_est_sec": sum(r["timing"]["table_total_parallel_est_sec"] for r in rows),
                "table_total_sequential_sec": sum(r["timing"]["table_total_sequential_sec"] for r in rows),
                "end_to_end_parallel_est_sec": sum(r["timing"]["end_to_end_parallel_est_sec"] for r in rows),
            }
        )
    summary.sort(key=lambda row: row["variant_id"])
    _write_csv(out_dir / "summary.csv", summary)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def run_groundtruth(args: argparse.Namespace) -> None:
    out_dir = args.out_dir / "groundtruth"
    scans = [str(scan).zfill(2) for scan in args.scans]
    results: list[dict[str, Any]] = []
    for idx, scan in enumerate(scans, 1):
        print(f"[groundtruth {idx}/{len(scans)}] scan{scan}", flush=True)
        results.extend(run_groundtruth_scan(scan, args))
        write_groundtruth_outputs(results, out_dir)
    write_groundtruth_outputs(results, out_dir)


def run_external(args: argparse.Namespace) -> None:
    out_dir = args.out_dir / "external"
    include_datasets = {item.lower() for item in args.datasets}
    samples = load_samples(args.manifest, include_datasets)
    if args.max_cases_per_dataset > 0:
        limited: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for sample in samples:
            dataset = str(sample["dataset"])
            counts[dataset] = counts.get(dataset, 0) + 1
            if counts[dataset] <= args.max_cases_per_dataset:
                limited.append(sample)
        samples = limited
    results: list[dict[str, Any]] = []
    for idx, sample in enumerate(samples, 1):
        print(f"[external {idx}/{len(samples)}] {sample['dataset']} {sample['id']}", flush=True)
        try:
            results.extend(run_external_sample(sample, args))
        except Exception as exc:
            print(f"  ERROR {sample['id']}: {exc!r}", flush=True)
        write_external_outputs(results, out_dir)
    write_external_outputs(results, out_dir)


def run_ctdar(args: argparse.Namespace) -> None:
    out_dir = args.out_dir / "ctdar"
    candidates = ctdar_bench.collect_candidates(
        args.ctdar_root,
        args.track,
        args.modern_only,
        args.ctdar_pdf_dpi,
        args.selection,
    )
    selected = candidates[: args.max_ctdar_cases]
    results: list[dict[str, Any]] = []
    for idx, candidate in enumerate(selected, 1):
        print(f"[ctdar {idx}/{len(selected)}] {candidate['id']}", flush=True)
        try:
            results.extend(run_ctdar_case(candidate, args))
        except Exception as exc:
            print(f"  ERROR {candidate['id']}: {exc!r}", flush=True)
        write_ctdar_outputs(results, out_dir)
    write_ctdar_outputs(results, out_dir)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--suite", choices=["all", "groundtruth", "external", "ctdar"], default="all")
    parser.add_argument("--ocr-dir", type=Path, default=ROOT / "temp" / "groundtruth5_pipeline")
    parser.add_argument("--groundtruth-dir", type=Path, default=Path(r"C:\Users\nhquan\Downloads\groundtruth"))
    parser.add_argument("--gt03-docx", type=Path, default=ROOT / "temp" / "groundtruth4_scan_word" / "groundtruth03_converted.docx")
    parser.add_argument("--scans", nargs="+", default=["01", "02", "03", "04", "05"])
    parser.add_argument("--manifest", type=Path, default=ROOT / "temp" / "external" / "table_gt_samples" / "manifest.json")
    parser.add_argument("--datasets", nargs="*", default=[])
    parser.add_argument("--max-cases-per-dataset", type=int, default=10)
    parser.add_argument("--pdf-dpi", type=int, default=144)
    parser.add_argument("--ctdar-root", type=Path, default=ROOT / "temp" / "external" / "ICDAR2019_cTDaR")
    parser.add_argument("--track", default="TRACKB2", choices=["TRACKB1", "TRACKB2"])
    parser.add_argument("--max-ctdar-cases", type=int, default=20)
    parser.add_argument("--selection", choices=["lowest", "highest"], default="highest")
    parser.add_argument("--modern-only", action="store_true", default=True)
    parser.add_argument("--include-historical", action="store_false", dest="modern_only")
    parser.add_argument("--ctdar-pdf-dpi", type=int, default=300)
    parser.add_argument("--doclayout-conf", type=float, default=0.25)
    parser.add_argument("--rapidtable-dpi", type=int, default=200)
    parser.add_argument("--rapidtable-pad", type=float, default=6.0)
    parser.add_argument("--wired-dpi", type=int, default=200)
    parser.add_argument("--wired-pad", type=float, default=0.0)
    parser.add_argument("--docling-dpi", type=int, default=144)
    parser.add_argument("--docling-pad", type=float, default=0.0)
    parser.add_argument("--docling-mode", choices=["fast", "accurate"], default="accurate")
    parser.add_argument("--docling-threads", type=int, default=4)
    parser.add_argument("--force-ocr", action="store_true")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.suite in {"all", "groundtruth"}:
        run_groundtruth(args)
    if args.suite in {"all", "external"}:
        run_external(args)
    if args.suite in {"all", "ctdar"}:
        run_ctdar(args)
    print(json.dumps({"out_dir": str(args.out_dir), "suite": args.suite}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
