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
from benchmark_external_pair_ensembles import (  # noqa: E402
    QuietLogger,
    best_table_result,
    compare_text_grid,
    ensure_ocr_pdf,
    full_page_layout_from_page_info,
    image_to_pdf,
    load_gt_text_grid,
)
from benchmark_external_table_gt_samples import load_samples, shape_score  # noqa: E402
from benchmark_pair_ensembles import ENGINE_SPECS, run_engine  # noqa: E402


SINGLE_DEFS: list[dict[str, Any]] = [
    {
        "order": 1,
        "id": "gmft_post",
        "label": "GMFT + OCR-aware + Postprocessing V2",
        "spec_id": "gmft_ocr",
    },
    {
        "order": 2,
        "id": "img2table_post",
        "label": "img2table + OCR-aware + Postprocessing V2",
        "spec_id": "img2table_ocr",
    },
    {
        "order": 3,
        "id": "rapidtable_ocr_post",
        "label": "RapidTable SLANet+ + OCR-aware + Postprocessing V2",
        "spec_id": "rapidtable_ocr",
    },
    {
        "order": 4,
        "id": "wired_v2_ocr_post",
        "label": "wired_v2 + OCR-aware + Postprocessing V2",
        "spec_id": "wired_v2_ocr",
    },
    {
        "order": 5,
        "id": "docling_fixed_onnx_ocr_post",
        "label": "Docling v1 ONNX fixed-cache + OCR-aware + Postprocessing V2",
        "spec_id": "docling_tableformer_fixed_onnx_ocr",
    },
    {
        "order": 6,
        "id": "docling_stepcache_onnx_ocr_post",
        "label": "Docling v1 ONNX step-cache + OCR-aware + Postprocessing V2",
        "spec_id": "docling_tableformer_ocr",
    },
    {
        "order": 7,
        "id": "docling_torch_ocr_post",
        "label": "Docling v1 PyTorch + OCR-aware + Postprocessing V2",
        "spec_id": "docling_tableformer_torch_ocr",
    },
]


def _run_engine_safe(
    spec_id: str,
    benchmark_pdf: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    spec = ENGINE_SPECS[spec_id]
    original_feed = bool(spec["feed_ocr"])
    if original_feed and not pdf_lines:
        ENGINE_SPECS[spec_id] = {**spec, "feed_ocr": False}
    try:
        return run_engine(
            spec_id,
            benchmark_pdf,
            logger,
            page_info,
            pdf_lines,
            layout_regions_by_page,
            args,
        )
    finally:
        if ENGINE_SPECS[spec_id]["feed_ocr"] != original_feed:
            ENGINE_SPECS[spec_id] = {**ENGINE_SPECS[spec_id], "feed_ocr": original_feed}


def run_sample(sample: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    dataset_slug = str(sample["dataset"]).replace(" ", "_").replace(".", "_").lower()
    raw_pdf = args.out_dir / "raw_pdfs" / dataset_slug / f"{sample['id']}.pdf"
    ocr_pdf = args.out_dir / "ocr_pdfs" / dataset_slug / f"{sample['id']}_ocr.pdf"
    image_to_pdf(Path(sample["image"]), raw_pdf, args.pdf_dpi)
    ocr_status = ensure_ocr_pdf(raw_pdf, ocr_pdf, Path(sample["image"]), args.force_ocr)
    benchmark_pdf = ocr_pdf if ocr_status["ok"] and ocr_pdf.exists() else raw_pdf

    logger = QuietLogger()
    prep_t0 = time.perf_counter()
    pdf_lines, _json_layout_regions, page_info = gt_bench.prepare_pipeline_inputs(benchmark_pdf, logger)
    layout_regions_by_page = full_page_layout_from_page_info(page_info, args.pdf_dpi)
    preprocess_sec = time.perf_counter() - prep_t0
    gt_grid = load_gt_text_grid(Path(sample["groundtruth"]))

    rows: list[dict[str, Any]] = []
    for variant in args.selected_variants:
        spec_id = str(variant["spec_id"])
        print(f"  engine {variant['label']}", flush=True)
        engine_result = _run_engine_safe(
            spec_id,
            benchmark_pdf,
            logger,
            page_info,
            pdf_lines,
            layout_regions_by_page,
            args,
        )

        post_t0 = time.perf_counter()
        out_tables = gt_bench.postprocess_current_tables(
            copy.deepcopy(engine_result["tables"]),
            layout_regions_by_page,
            pdf_lines,
            page_info,
            logger,
        )
        postprocess_sec = time.perf_counter() - post_t0

        pred_rows, pred_cols, pred_count, sources, pred_grid = best_table_result(out_tables)
        shape = shape_score(sample["gt_rows"], sample["gt_cols"], pred_rows, pred_cols)
        text_cmp = compare_text_grid(gt_grid, pred_grid)
        engine_time = engine_result["timing"]["engine_total_sec"]
        total_sec = engine_time + postprocess_sec
        rows.append(
            {
                "dataset": sample["dataset"],
                "id": sample["id"],
                "variant_id": variant["id"],
                "label": variant["label"],
                "variant_order": int(variant["order"]),
                "gt_shape": f"{sample['gt_rows']}x{sample['gt_cols']}",
                "pred_shape": f"{pred_rows}x{pred_cols}",
                "shape_acc": shape,
                "exact_shape": sample["gt_rows"] == pred_rows and sample["gt_cols"] == pred_cols,
                "cell_exact_rate": text_cmp["cell_exact_rate"],
                "cell_text_acc": text_cmp["cell_text_acc"],
                "table_exact_text": text_cmp["table_exact_text"],
                "compared_cells": text_cmp["compared_cells"],
                "pred_count": pred_count,
                "sources": sources,
                "ocr_line_count": len(pdf_lines),
                "feed_ocr_available": bool(pdf_lines),
                "timing": {
                    "ocr_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0),
                    "preprocess_sec": preprocess_sec,
                    "detect_sec": engine_result["timing"]["detect_sec"],
                    "text_map_sec": engine_result["timing"]["text_map_sec"],
                    "postprocess_sec": postprocess_sec,
                    "table_total_sec": total_sec,
                    "end_to_end_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0)
                    + preprocess_sec
                    + total_sec,
                },
                "image": str(sample["image"]),
                "groundtruth": str(sample["groundtruth"]),
                "text_mismatches": text_cmp.get("mismatches", []),
                "log_tail": logger.lines[-80:],
            }
        )
    return rows


def write_outputs(results: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "details.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    per_case_fields = [
        "dataset",
        "id",
        "variant_id",
        "label",
        "gt_shape",
        "pred_shape",
        "shape_acc",
        "exact_shape",
        "cell_exact_rate",
        "cell_text_acc",
        "table_exact_text",
        "compared_cells",
        "pred_count",
        "ocr_line_count",
        "table_total_sec",
        "end_to_end_sec",
        "sources",
        "image",
        "groundtruth",
    ]
    with (out_dir / "per_case.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=per_case_fields)
        writer.writeheader()
        for row in results:
            writer.writerow(
                {
                    "dataset": row["dataset"],
                    "id": row["id"],
                    "variant_id": row["variant_id"],
                    "label": row["label"],
                    "gt_shape": row["gt_shape"],
                    "pred_shape": row["pred_shape"],
                    "shape_acc": row["shape_acc"],
                    "exact_shape": row["exact_shape"],
                    "cell_exact_rate": row["cell_exact_rate"],
                    "cell_text_acc": row["cell_text_acc"],
                    "table_exact_text": row["table_exact_text"],
                    "compared_cells": row["compared_cells"],
                    "pred_count": row["pred_count"],
                    "ocr_line_count": row["ocr_line_count"],
                    "table_total_sec": row["timing"]["table_total_sec"],
                    "end_to_end_sec": row["timing"]["end_to_end_sec"],
                    "sources": "|".join(row["sources"]),
                    "image": row["image"],
                    "groundtruth": row["groundtruth"],
                }
            )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    overall: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        grouped.setdefault((str(row["dataset"]), str(row["variant_id"])), []).append(row)
        overall.setdefault(str(row["variant_id"]), []).append(row)

    def summarize(dataset: str, variant_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rows)
        return {
            "dataset": dataset,
            "variant_id": variant_id,
            "label": rows[0]["label"],
            "order": rows[0]["variant_order"],
            "cases": n,
            "shape_acc": sum(float(r["shape_acc"]) for r in rows) / n,
            "exact_shape_rate": sum(1.0 if r["exact_shape"] else 0.0 for r in rows) / n,
            "cell_exact_rate": sum(float(r["cell_exact_rate"]) for r in rows) / n,
            "cell_text_acc": sum(float(r["cell_text_acc"]) for r in rows) / n,
            "table_exact_text_rate": sum(1.0 if r["table_exact_text"] else 0.0 for r in rows) / n,
            "compared_cells": sum(int(r["compared_cells"]) for r in rows),
            "pred_count": sum(int(r["pred_count"]) for r in rows),
            "ocr_line_count_avg": sum(int(r["ocr_line_count"]) for r in rows) / n,
            "detect_sec": sum(float(r["timing"]["detect_sec"]) for r in rows),
            "text_map_sec": sum(float(r["timing"]["text_map_sec"]) for r in rows),
            "postprocess_sec": sum(float(r["timing"]["postprocess_sec"]) for r in rows),
            "table_total_sec": sum(float(r["timing"]["table_total_sec"]) for r in rows),
            "end_to_end_sec": sum(float(r["timing"]["end_to_end_sec"]) for r in rows),
        }

    summary = [
        summarize(dataset, variant_id, rows)
        for (dataset, variant_id), rows in grouped.items()
    ]
    summary.extend(summarize("__overall__", variant_id, rows) for variant_id, rows in overall.items())
    summary.sort(key=lambda row: (row["dataset"], int(row["order"])))
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary_fields = [
        "dataset",
        "variant_id",
        "label",
        "cases",
        "shape_acc",
        "exact_shape_rate",
        "cell_exact_rate",
        "cell_text_acc",
        "table_exact_text_rate",
        "compared_cells",
        "pred_count",
        "ocr_line_count_avg",
        "detect_sec",
        "text_map_sec",
        "postprocess_sec",
        "table_total_sec",
        "end_to_end_sec",
    ]
    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        for row in summary:
            writer.writerow({field: row[field] for field in summary_fields})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "temp" / "external" / "table_gt_samples" / "manifest.json")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--datasets", nargs="*", default=[])
    parser.add_argument("--variants", nargs="*", default=[])
    parser.add_argument("--max-cases-per-dataset", type=int, default=10)
    parser.add_argument("--pdf-dpi", type=int, default=144)
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
    wanted_variants = {item.lower() for item in args.variants}
    args.selected_variants = [
        variant for variant in SINGLE_DEFS if not wanted_variants or variant["id"].lower() in wanted_variants
    ]
    missing_variants = sorted(wanted_variants - {variant["id"].lower() for variant in args.selected_variants})
    if missing_variants:
        raise ValueError(f"Unknown variant ids: {missing_variants}")

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
        print(
            f"[{idx}/{len(samples)}] {sample['dataset']} {sample['id']} gt={sample['gt_rows']}x{sample['gt_cols']}",
            flush=True,
        )
        try:
            results.extend(run_sample(sample, args))
        except Exception as exc:
            print(f"  ERROR {sample['id']}: {exc!r}", flush=True)
        write_outputs(results, args.out_dir)
    write_outputs(results, args.out_dir)
    print(json.dumps({"out_dir": str(args.out_dir), "rows": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
