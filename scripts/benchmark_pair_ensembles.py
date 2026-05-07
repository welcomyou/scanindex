from __future__ import annotations

import argparse
import copy
import csv
import json
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

import benchmark_current_table_pipeline as gt_bench  # noqa: E402
from benchmark_groundtruth_engine_matrix import (  # noqa: E402
    QuietLogger,
    map_ocr_text_to_tables,
    run_detector,
)
from table_anchored_merger import _select_table_candidates_for_page  # noqa: E402
from table_anchored_merger import _candidate_set_score, _layout_table_bboxes  # noqa: E402
from table_postprocess_v2 import table_ocr_fit_score  # noqa: E402


ENGINE_SPECS: dict[str, dict[str, Any]] = {
    "gmft_ocr": {
        "label": "GMFT OCR-map",
        "engine": "gmft",
        "feed_ocr": False,
        "source": "gmft_onnx",
    },
    "img2table_ocr": {
        "label": "img2tables OCR-map",
        "engine": "img2table",
        "feed_ocr": False,
        "source": "img2table",
    },
    "rapidtable_ocr": {
        "label": "RapidTable SLANet+ OCR-aware",
        "engine": "rapidtable",
        "feed_ocr": True,
        "source": "rapidtable_slanet",
    },
    "wired_v2_ocr": {
        "label": "wired_table_rec_v2 OCR-aware",
        "engine": "wired_v2",
        "feed_ocr": True,
        "source": "wired_table_rec_v2",
    },
    "docling_tableformer_ocr": {
        "label": "Docling TableFormer v1 ONNX step-cache OCR-aware",
        "engine": "docling_tableformer",
        "feed_ocr": True,
        "source": "docling_tableformer",
    },
    "docling_tableformer_fixed_onnx_ocr": {
        "label": "Docling TableFormer v1 ONNX fixed-cache OCR-aware",
        "engine": "docling_tableformer_fixed_onnx",
        "feed_ocr": True,
        "source": "docling_tableformer",
    },
    "docling_tableformer_temp_onnx_ocr": {
        "label": "Docling TableFormer v1 ONNX step-cache OCR-aware",
        "engine": "docling_tableformer_temp_onnx",
        "feed_ocr": True,
        "source": "docling_tableformer",
    },
    "docling_tableformer_torch_ocr": {
        "label": "Docling TableFormer v1 PyTorch OCR-aware",
        "engine": "docling_tableformer_torch",
        "feed_ocr": True,
        "source": "docling_tableformer",
    },
}


PAIR_DEFS: list[dict[str, Any]] = [
    {
        "order": 1,
        "id": "gmft_img2table_post",
        "label": "GMFT OCR-aware + img2tables OCR-aware + post process",
        "engines": ("gmft_ocr", "img2table_ocr"),
    },
    {
        "order": 2,
        "id": "gmft_rapidtable_post",
        "label": "GMFT OCR-aware + RapidTable SLANet+ OCR-aware + post process",
        "engines": ("gmft_ocr", "rapidtable_ocr"),
    },
    {
        "order": 3,
        "id": "gmft_wired_v2_post",
        "label": "GMFT OCR-aware + wired_table_rec_v2 OCR-aware + post process",
        "engines": ("gmft_ocr", "wired_v2_ocr"),
    },
    {
        "order": 4,
        "id": "img2table_rapidtable_post",
        "label": "img2tables OCR-aware + RapidTable SLANet+ OCR-aware + post process",
        "engines": ("img2table_ocr", "rapidtable_ocr"),
    },
    {
        "order": 5,
        "id": "img2table_wired_v2_post",
        "label": "img2tables OCR-aware + wired_table_rec_v2 OCR-aware + post process",
        "engines": ("img2table_ocr", "wired_v2_ocr"),
    },
    {
        "order": 6,
        "id": "rapidtable_wired_v2_post",
        "label": "RapidTable SLANet+ OCR-aware + wired_table_rec_v2 OCR-aware + post process",
        "engines": ("rapidtable_ocr", "wired_v2_ocr"),
    },
    {
        "order": 7,
        "id": "gmft_docling_post",
        "label": "GMFT OCR-aware + Docling TableFormer OCR-aware + post process",
        "engines": ("gmft_ocr", "docling_tableformer_ocr"),
    },
    {
        "order": 8,
        "id": "img2table_docling_post",
        "label": "img2tables OCR-aware + Docling TableFormer OCR-aware + post process",
        "engines": ("img2table_ocr", "docling_tableformer_ocr"),
    },
    {
        "order": 9,
        "id": "rapidtable_docling_post",
        "label": "RapidTable SLANet+ OCR-aware + Docling TableFormer OCR-aware + post process",
        "engines": ("rapidtable_ocr", "docling_tableformer_ocr"),
    },
    {
        "order": 10,
        "id": "wired_v2_docling_post",
        "label": "wired_table_rec_v2 OCR-aware + Docling TableFormer OCR-aware + post process",
        "engines": ("wired_v2_ocr", "docling_tableformer_ocr"),
    },
]


def mark_source(tables: list[Any], source: str) -> list[Any]:
    out = copy.deepcopy(tables)
    for table in out:
        setattr(table, "source", source)
    return out


def group_by_page(tables: list[Any]) -> dict[int, list[Any]]:
    by_page: dict[int, list[Any]] = {}
    for table in tables:
        by_page.setdefault(int(getattr(table, "page", 0) or 0), []).append(table)
    for page_tables in by_page.values():
        page_tables.sort(key=lambda table: (float(getattr(table, "y_top", 0.0) or 0.0), float(getattr(table, "x_left", 0.0) or 0.0)))
    return by_page


def run_engine(
    spec_id: str,
    ocr_pdf: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    spec = ENGINE_SPECS[spec_id]
    detect_t0 = time.perf_counter()
    raw_tables = run_detector(
        spec["engine"],
        ocr_pdf,
        logger,
        page_info,
        pdf_lines,
        layout_regions_by_page,
        bool(spec["feed_ocr"]),
        args,
    )
    detect_sec = time.perf_counter() - detect_t0

    map_t0 = time.perf_counter()
    mapped = map_ocr_text_to_tables(raw_tables, pdf_lines, logger)
    text_map_sec = time.perf_counter() - map_t0

    tables = mark_source(mapped, spec["source"])
    return {
        "spec_id": spec_id,
        "label": spec["label"],
        "source": spec["source"],
        "feed_ocr_to_model": bool(spec["feed_ocr"]),
        "tables": tables,
        "timing": {
            "detect_sec": detect_sec,
            "text_map_sec": text_map_sec,
            "engine_total_sec": detect_sec + text_map_sec,
        },
    }


def combine_pair(
    pair: dict[str, Any],
    engine_results: dict[str, dict[str, Any]],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    logger: Any,
    pdf_lines: list[Any] | None = None,
) -> tuple[list[Any], float]:
    left_id, right_id = pair["engines"]
    left = engine_results[left_id]
    right = engine_results[right_id]
    left_by_page = group_by_page(left["tables"])
    right_by_page = group_by_page(right["tables"])
    pages = sorted(set(left_by_page) | set(right_by_page))

    selected: list[Any] = []
    select_t0 = time.perf_counter()
    for page in pages:
        candidate_sets = {
            left["source"]: left_by_page.get(page, []),
            right["source"]: right_by_page.get(page, []),
        }
        if pdf_lines:
            layout_bboxes = _layout_table_bboxes(layout_regions_by_page, page)
            scored = []
            for source, tables in candidate_sets.items():
                if not tables:
                    continue
                layout_score = _candidate_set_score(tables, layout_bboxes)
                ocr_score = sum(table_ocr_fit_score(table, pdf_lines) for table in tables) / max(len(tables), 1)
                scored.append((layout_score + ocr_score, source, tables, layout_score, ocr_score))
            if scored:
                scored.sort(key=lambda item: item[0], reverse=True)
                _score, source, chosen_tables, layout_score, ocr_score = scored[0]
                summary = ", ".join(
                    f"{src}:{score:.2f}=L{ls:.2f}+O{os:.2f}/{len(tables)}"
                    for score, src, tables, ls, os in scored
                )
                logger.log(f"  Page {page}: V2 selected {source} by layout+OCR-fit ({summary})")
                chosen = chosen_tables
            else:
                chosen = []
        else:
            chosen = _select_table_candidates_for_page(
                page,
                candidate_sets,
                layout_regions_by_page,
                logger,
            )
        selected.extend(copy.deepcopy(chosen))
    select_sec = time.perf_counter() - select_t0
    return selected, select_sec


def run_scan(scan: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    ocr_pdf = args.ocr_dir / f"scan{scan}_ocr.pdf"
    logger = QuietLogger()

    prep_t0 = time.perf_counter()
    pdf_lines, layout_regions_by_page, page_info = gt_bench.prepare_pipeline_inputs(ocr_pdf, logger)
    preprocess_sec = time.perf_counter() - prep_t0

    engine_results: dict[str, dict[str, Any]] = {}
    for spec_id in ENGINE_SPECS:
        print(f"  engine {ENGINE_SPECS[spec_id]['label']}", flush=True)
        engine_results[spec_id] = run_engine(
            spec_id,
            ocr_pdf,
            logger,
            page_info,
            pdf_lines,
            layout_regions_by_page,
            args,
        )

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

    rows: list[dict[str, Any]] = []
    for pair in PAIR_DEFS:
        raw_tables, select_sec = combine_pair(pair, engine_results, layout_regions_by_page, logger, pdf_lines)

        post_t0 = time.perf_counter()
        out_tables = gt_bench.postprocess_current_tables(
            copy.deepcopy(raw_tables),
            layout_regions_by_page,
            pdf_lines,
            page_info,
            logger,
        )
        postprocess_sec = time.perf_counter() - post_t0

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
        left_id, right_id = pair["engines"]
        left_time = engine_results[left_id]["timing"]["engine_total_sec"]
        right_time = engine_results[right_id]["timing"]["engine_total_sec"]
        detect_sec = engine_results[left_id]["timing"]["detect_sec"] + engine_results[right_id]["timing"]["detect_sec"]
        text_map_sec = engine_results[left_id]["timing"]["text_map_sec"] + engine_results[right_id]["timing"]["text_map_sec"]
        pair_engine_seq_sec = left_time + right_time
        pair_engine_parallel_est_sec = max(left_time, right_time)

        rows.append({
            "scan": scan,
            "variant_id": pair["id"],
            "label": pair["label"],
            "variant_order": int(pair["order"]),
            "engines": list(pair["engines"]),
            "comparison": cmp,
            "gt_shapes": [list(t[:2]) for t in gt_data],
            "out_shapes": [list(t[:2]) for t in out_data],
            "sources": [getattr(table, "source", "") for table in out_tables],
            "timing": {
                "preprocess_sec": preprocess_sec,
                "detect_sec": detect_sec,
                "text_map_sec": text_map_sec,
                "select_sec": select_sec,
                "postprocess_sec": postprocess_sec,
                "engine_sequential_sec": pair_engine_seq_sec,
                "engine_parallel_est_sec": pair_engine_parallel_est_sec,
                "table_total_sequential_sec": pair_engine_seq_sec + select_sec + postprocess_sec,
                "table_total_parallel_est_sec": pair_engine_parallel_est_sec + select_sec + postprocess_sec,
            },
            "engine_timing": {
                left_id: engine_results[left_id]["timing"],
                right_id: engine_results[right_id]["timing"],
            },
            "log_tail": logger.lines[-80:],
        })
    return rows


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
            "shape_acc": sum(i["comparison"]["shape_acc"] for i in items) / len(items),
            "text_acc": sum(i["comparison"]["text_acc"] for i in items) / len(items),
            "cell_exact_rate": sum(i["comparison"]["cell_exact_rate"] for i in items) / len(items),
            "cell_text_acc": sum(i["comparison"]["cell_text_acc"] for i in items) / len(items),
            "table_exact_text_rate": sum(i["comparison"]["table_exact_text_rate"] for i in items) / len(items),
            "compared_cells": sum(i["comparison"]["compared_cells"] for i in items),
            "detect_sec": sum(i["timing"]["detect_sec"] for i in items),
            "text_map_sec": sum(i["timing"]["text_map_sec"] for i in items),
            "select_sec": sum(i["timing"]["select_sec"] for i in items),
            "postprocess_sec": sum(i["timing"]["postprocess_sec"] for i in items),
            "table_total_sequential_sec": sum(i["timing"]["table_total_sequential_sec"] for i in items),
            "table_total_parallel_est_sec": sum(i["timing"]["table_total_parallel_est_sec"] for i in items),
        })
    summary.sort(key=lambda row: row["order"])
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary_fields = [
        "variant_id",
        "label",
        "cases",
        "shape_acc",
        "text_acc",
        "cell_exact_rate",
        "cell_text_acc",
        "table_exact_text_rate",
        "compared_cells",
        "detect_sec",
        "text_map_sec",
        "select_sec",
        "postprocess_sec",
        "table_total_sequential_sec",
        "table_total_parallel_est_sec",
    ]
    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        for row in summary:
            writer.writerow({field: row[field] for field in summary_fields})

    scan_fields = [
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
        "select_sec",
        "postprocess_sec",
        "table_total_sequential_sec",
        "table_total_parallel_est_sec",
        "out_shapes",
        "sources",
    ]
    with (out_dir / "per_scan.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=scan_fields)
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
                "select_sec": item["timing"]["select_sec"],
                "postprocess_sec": item["timing"]["postprocess_sec"],
                "table_total_sequential_sec": item["timing"]["table_total_sequential_sec"],
                "table_total_parallel_est_sec": item["timing"]["table_total_parallel_est_sec"],
                "out_shapes": json.dumps(item["out_shapes"]),
                "sources": "|".join(item["sources"]),
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
    results: list[dict[str, Any]] = []
    for idx, scan in enumerate(scans, 1):
        print(f"[scan{scan}] ({idx}/{len(scans)})", flush=True)
        results.extend(run_scan(scan, args))
        write_outputs(results, args.out_dir)
    write_outputs(results, args.out_dir)
    print(json.dumps({"out_dir": str(args.out_dir), "rows": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
