from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import benchmark_current_table_pipeline as gt_bench  # noqa: E402
from docling_tableformer_engine import detect_tables_docling_tableformer  # noqa: E402
from docling_tableformer_v1_onnx_engine import (  # noqa: E402
    detect_tables_docling_tableformer_v1_onnx,
)
from gmft_onnx_table_engine import detect_tables_gmft_onnx  # noqa: E402
from rapidtable_structure_engine import detect_tables_structure_recognizer  # noqa: E402
from table_anchored_merger import (  # noqa: E402
    _text_from_table_cell_lines,
    assign_ocr_lines_to_table_cells_by_geometry,
    detect_tables_img2table,
    get_lines_in_rect,
)


class QuietLogger(gt_bench.QuietLogger):
    pass


def build_text_resolver(pdf_lines: list[Any], logger: Any):
    lines_by_page: dict[int, list[Any]] = {}
    for line in pdf_lines:
        lines_by_page.setdefault(int(getattr(line, "page", 0) or 0), []).append(line)

    def resolve(page: int, bbox: tuple[float, float, float, float]) -> str:
        cell_lines = get_lines_in_rect(bbox, lines_by_page.get(page, []))
        return _text_from_table_cell_lines(cell_lines, page, bbox[0], bbox[2], logger)

    return resolve


def blank_table_text(tables: list[Any]) -> list[Any]:
    out = copy.deepcopy(tables)
    for table in out:
        rows = int(getattr(table, "row_count", 0) or 0)
        cols = int(getattr(table, "col_count", 0) or 0)
        table.cells = [[""] * cols for _ in range(rows)]
    return out


def map_ocr_text_to_tables(tables: list[Any], pdf_lines: list[Any], logger: Any) -> list[Any]:
    out = copy.deepcopy(tables)
    assign_ocr_lines_to_table_cells_by_geometry(out, pdf_lines, logger)
    return out


def run_detector(
    engine: str,
    ocr_pdf: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    feed_ocr_to_model: bool,
    args: argparse.Namespace,
) -> list[Any]:
    if engine == "gmft":
        return detect_tables_gmft_onnx(str(ocr_pdf), logger, page_info, [], "cpu")

    if engine == "img2table":
        return detect_tables_img2table(str(ocr_pdf), logger, page_info, [])

    if engine == "rapidtable":
        if feed_ocr_to_model:
            return detect_tables_structure_recognizer(
                str(ocr_pdf),
                logger,
                page_info,
                pdf_lines,
                layout_regions_by_page,
                recognizer="rapidtable_slanet",
                dpi=args.rapidtable_dpi,
                pad_pt=args.rapidtable_pad,
                text_resolver=lambda page, bbox: "",
                rapidtable_use_ocr_results=True,
            )
        return detect_tables_structure_recognizer(
            str(ocr_pdf),
            logger,
            page_info,
            [],
            layout_regions_by_page,
            recognizer="rapidtable_slanet",
            dpi=args.rapidtable_dpi,
            pad_pt=args.rapidtable_pad,
            text_resolver=None,
            rapidtable_use_ocr_results=False,
        )

    if engine == "wired_v2":
        if feed_ocr_to_model:
            return detect_tables_structure_recognizer(
                str(ocr_pdf),
                logger,
                page_info,
                pdf_lines,
                layout_regions_by_page,
                recognizer="wired_table_rec_v2",
                dpi=args.wired_dpi,
                pad_pt=args.wired_pad,
                text_resolver=lambda page, bbox: "",
                wired_use_ocr_results=True,
            )
        return detect_tables_structure_recognizer(
            str(ocr_pdf),
            logger,
            page_info,
            [],
            layout_regions_by_page,
            recognizer="wired_table_rec_v2",
            dpi=args.wired_dpi,
            pad_pt=args.wired_pad,
            text_resolver=None,
            wired_use_ocr_results=False,
        )

    if engine == "docling_tableformer":
        return detect_tables_docling_tableformer_v1_onnx(
            str(ocr_pdf),
            logger,
            page_info,
            pdf_lines if feed_ocr_to_model else [],
            layout_regions_by_page,
            dpi=getattr(args, "docling_dpi", 144),
            pad_pt=getattr(args, "docling_pad", 0.0),
            num_threads=getattr(args, "docling_threads", 4),
        )

    if engine == "docling_tableformer_fixed_onnx":
        return detect_tables_docling_tableformer_v1_onnx(
            str(ocr_pdf),
            logger,
            page_info,
            pdf_lines if feed_ocr_to_model else [],
            layout_regions_by_page,
            dpi=getattr(args, "docling_dpi", 144),
            pad_pt=getattr(args, "docling_pad", 0.0),
            num_threads=getattr(args, "docling_threads", 4),
            artifact_dir=ROOT / "models" / "docling_tableformer_v1_onnx",
        )

    if engine == "docling_tableformer_temp_onnx":
        return detect_tables_docling_tableformer_v1_onnx(
            str(ocr_pdf),
            logger,
            page_info,
            pdf_lines if feed_ocr_to_model else [],
            layout_regions_by_page,
            dpi=getattr(args, "docling_dpi", 144),
            pad_pt=getattr(args, "docling_pad", 0.0),
            num_threads=getattr(args, "docling_threads", 4),
            artifact_dir=ROOT / "temp" / "tableformer_onnx",
        )

    if engine == "docling_tableformer_torch":
        return detect_tables_docling_tableformer(
            str(ocr_pdf),
            logger,
            page_info,
            pdf_lines if feed_ocr_to_model else [],
            layout_regions_by_page,
            dpi=getattr(args, "docling_dpi", 144),
            pad_pt=getattr(args, "docling_pad", 0.0),
            mode=getattr(args, "docling_mode", "accurate"),
            num_threads=getattr(args, "docling_threads", 4),
        )

    raise ValueError(engine)


def variant_defs() -> list[dict[str, Any]]:
    return [
        {"order": 1, "id": "gmft", "label": "GMFT", "engine": "gmft", "feed_ocr": False, "map_ocr": False, "post": False},
        {"order": 2, "id": "gmft_ocr", "label": "GMFT + OCR-aware", "engine": "gmft", "feed_ocr": False, "map_ocr": True, "post": False},
        {"order": 3, "id": "gmft_post", "label": "GMFT + post process", "engine": "gmft", "feed_ocr": False, "map_ocr": True, "post": True},
        {"order": 4, "id": "img2table", "label": "img2tables", "engine": "img2table", "feed_ocr": False, "map_ocr": False, "post": False},
        {"order": 5, "id": "img2table_ocr", "label": "img2tables + OCR-aware", "engine": "img2table", "feed_ocr": False, "map_ocr": True, "post": False},
        {"order": 6, "id": "img2table_post", "label": "img2tables + post process", "engine": "img2table", "feed_ocr": False, "map_ocr": True, "post": True},
        {"order": 7, "id": "rapidtable", "label": "RapidTable SLANet+", "engine": "rapidtable", "feed_ocr": False, "map_ocr": False, "post": False},
        {"order": 8, "id": "rapidtable_ocr", "label": "RapidTable SLANet+ + OCR-aware", "engine": "rapidtable", "feed_ocr": True, "map_ocr": True, "post": False},
        {"order": 9, "id": "rapidtable_ocr_post", "label": "RapidTable SLANet+ + OCR-aware + post process", "engine": "rapidtable", "feed_ocr": True, "map_ocr": True, "post": True},
        {"order": 10, "id": "wired_v2", "label": "wired_table_rec_v2", "engine": "wired_v2", "feed_ocr": False, "map_ocr": False, "post": False},
        {"order": 11, "id": "wired_v2_ocr", "label": "wired_table_rec_v2 + OCR-aware", "engine": "wired_v2", "feed_ocr": True, "map_ocr": True, "post": False},
        {"order": 12, "id": "wired_v2_ocr_post", "label": "wired_table_rec_v2 + OCR-aware + post process", "engine": "wired_v2", "feed_ocr": True, "map_ocr": True, "post": True},
        {"order": 13, "id": "docling_tableformer", "label": "Docling TableFormer", "engine": "docling_tableformer", "feed_ocr": False, "map_ocr": False, "post": False},
        {"order": 14, "id": "docling_tableformer_ocr", "label": "Docling TableFormer + OCR-aware", "engine": "docling_tableformer", "feed_ocr": True, "map_ocr": True, "post": False},
        {"order": 15, "id": "docling_tableformer_ocr_post", "label": "Docling TableFormer + OCR-aware + post process", "engine": "docling_tableformer", "feed_ocr": True, "map_ocr": True, "post": True},
    ]


def run_variant(scan: str, variant: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    ocr_pdf = args.ocr_dir / f"scan{scan}_ocr.pdf"
    logger = QuietLogger()

    prep_t0 = time.perf_counter()
    pdf_lines, layout_regions_by_page, page_info = gt_bench.prepare_pipeline_inputs(ocr_pdf, logger)
    preprocess_sec = time.perf_counter() - prep_t0

    detect_t0 = time.perf_counter()
    raw_tables = run_detector(
        variant["engine"],
        ocr_pdf,
        logger,
        page_info,
        pdf_lines,
        layout_regions_by_page,
        bool(variant["feed_ocr"]),
        args,
    )
    detect_sec = time.perf_counter() - detect_t0

    text_map_sec = 0.0
    if variant["map_ocr"]:
        map_t0 = time.perf_counter()
        raw_tables = map_ocr_text_to_tables(raw_tables, pdf_lines, logger)
        text_map_sec = time.perf_counter() - map_t0
    else:
        raw_tables = blank_table_text(raw_tables)

    post_sec = 0.0
    out_tables = raw_tables
    if variant["post"]:
        post_t0 = time.perf_counter()
        out_tables = gt_bench.postprocess_current_tables(
            copy.deepcopy(raw_tables),
            layout_regions_by_page,
            pdf_lines,
            page_info,
            logger,
        )
        post_sec = time.perf_counter() - post_t0

    gt_data = gt_bench.data_tables(
        gt_bench.docx_tables(
            gt_bench.groundtruth_docx_for_scan(scan, args.groundtruth_dir, args.gt03_docx)
        )
    )
    gt_grids = gt_bench.data_table_grids(
        gt_bench.docx_table_grids(
            gt_bench.groundtruth_docx_for_scan(scan, args.groundtruth_dir, args.gt03_docx)
        )
    )
    out_data = gt_bench.table_region_data(out_tables)
    out_grids = gt_bench.table_region_grids(out_tables)
    cmp = gt_bench.compare_tables(gt_data, out_data)
    cell_cmp = gt_bench.compare_table_cells(gt_grids, out_grids)
    cmp.update({
        "cell_exact_rate": cell_cmp["cell_exact_rate"],
        "cell_text_acc": cell_cmp["cell_text_acc"],
        "table_exact_text_rate": cell_cmp["table_exact_text_rate"],
        "compared_cells": cell_cmp["compared_cells"],
        "cell_details": cell_cmp["details"],
    })

    return {
        "scan": scan,
        "variant_id": variant["id"],
        "label": variant["label"],
        "engine": variant["engine"],
        "variant_order": int(variant["order"]),
        "feed_ocr_to_model": bool(variant["feed_ocr"]),
        "map_ocr_text": bool(variant["map_ocr"]),
        "postprocess": bool(variant["post"]),
        "gt_shapes": [list(t[:2]) for t in gt_data],
        "out_shapes": [list(t[:2]) for t in out_data],
        "comparison": cmp,
        "timing": {
            "preprocess_sec": preprocess_sec,
            "detect_sec": detect_sec,
            "text_map_sec": text_map_sec,
            "postprocess_sec": post_sec,
            "table_total_sec": detect_sec + text_map_sec + post_sec,
        },
        "sources": [getattr(t, "source", "") for t in out_tables],
        "log_tail": logger.lines[-60:],
    }


def write_outputs(results: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "details.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        grouped.setdefault(item["variant_id"], []).append(item)

    summary = []
    for variant_id, items in grouped.items():
        summary.append({
            "variant_id": variant_id,
            "label": items[0]["label"],
            "order": items[0]["variant_order"],
            "cases": len(items),
            "feed_ocr_to_model": items[0]["feed_ocr_to_model"],
            "map_ocr_text": items[0]["map_ocr_text"],
            "postprocess": items[0]["postprocess"],
            "shape_acc": sum(i["comparison"]["shape_acc"] for i in items) / len(items),
            "text_acc": sum(i["comparison"]["text_acc"] for i in items) / len(items),
            "cell_exact_rate": sum(i["comparison"]["cell_exact_rate"] for i in items) / len(items),
            "cell_text_acc": sum(i["comparison"]["cell_text_acc"] for i in items) / len(items),
            "table_exact_text_rate": sum(i["comparison"]["table_exact_text_rate"] for i in items) / len(items),
            "compared_cells": sum(i["comparison"]["compared_cells"] for i in items),
            "detect_sec": sum(i["timing"]["detect_sec"] for i in items),
            "text_map_sec": sum(i["timing"]["text_map_sec"] for i in items),
            "postprocess_sec": sum(i["timing"]["postprocess_sec"] for i in items),
            "table_total_sec": sum(i["timing"]["table_total_sec"] for i in items),
        })
    summary.sort(key=lambda row: row["order"])
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        fields = [
            "variant_id",
            "label",
            "cases",
            "feed_ocr_to_model",
            "map_ocr_text",
            "postprocess",
            "shape_acc",
            "text_acc",
            "cell_exact_rate",
            "cell_text_acc",
            "table_exact_text_rate",
            "compared_cells",
            "detect_sec",
            "text_map_sec",
            "postprocess_sec",
            "table_total_sec",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in summary:
            writer.writerow({field: row[field] for field in fields})

    with (out_dir / "per_scan.csv").open("w", newline="", encoding="utf-8") as f:
        fields = [
            "variant_id",
            "label",
            "scan",
            "shape_acc",
            "text_acc",
            "cell_exact_rate",
            "cell_text_acc",
            "table_exact_text_rate",
            "compared_cells",
            "detect_sec",
            "text_map_sec",
            "postprocess_sec",
            "table_total_sec",
            "out_shapes",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in results:
            writer.writerow({
                "variant_id": item["variant_id"],
                "label": item["label"],
                "scan": item["scan"],
                "shape_acc": item["comparison"]["shape_acc"],
                "text_acc": item["comparison"]["text_acc"],
                "cell_exact_rate": item["comparison"]["cell_exact_rate"],
                "cell_text_acc": item["comparison"]["cell_text_acc"],
                "table_exact_text_rate": item["comparison"]["table_exact_text_rate"],
                "compared_cells": item["comparison"]["compared_cells"],
                "detect_sec": item["timing"]["detect_sec"],
                "text_map_sec": item["timing"]["text_map_sec"],
                "postprocess_sec": item["timing"]["postprocess_sec"],
                "table_total_sec": item["timing"]["table_total_sec"],
                "out_shapes": json.dumps(item["out_shapes"]),
            })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ocr-dir", type=Path, default=ROOT / "temp" / "groundtruth5_pipeline")
    parser.add_argument("--groundtruth-dir", type=Path, default=Path(r"C:\Users\nhquan\Downloads\groundtruth"))
    parser.add_argument("--gt03-docx", type=Path, default=ROOT / "temp" / "groundtruth4_scan_word" / "groundtruth03_converted.docx")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--scans", nargs="+", default=["01", "02", "03", "04", "05"])
    parser.add_argument("--rapidtable-dpi", type=int, default=200)
    parser.add_argument("--rapidtable-pad", type=float, default=6.0)
    parser.add_argument("--wired-dpi", type=int, default=200)
    parser.add_argument("--wired-pad", type=float, default=0.0)
    parser.add_argument("--docling-dpi", type=int, default=144)
    parser.add_argument("--docling-pad", type=float, default=0.0)
    parser.add_argument("--docling-mode", choices=["fast", "accurate"], default="accurate")
    parser.add_argument("--docling-threads", type=int, default=4)
    args = parser.parse_args()

    scans = [str(scan).zfill(2) for scan in args.scans]
    variants = variant_defs()
    results: list[dict[str, Any]] = []
    for variant in variants:
        for idx, scan in enumerate(scans, 1):
            print(f"[{variant['label']}] scan{scan} ({idx}/{len(scans)})", flush=True)
            results.append(run_variant(scan, variant, args))
            write_outputs(results, args.out_dir)
    write_outputs(results, args.out_dir)
    print(json.dumps({"out_dir": str(args.out_dir), "rows": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
