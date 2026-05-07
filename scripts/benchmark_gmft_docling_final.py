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
from docling_tableformer_engine import detect_tables_docling_tableformer  # noqa: E402
from gmft_onnx_table_engine import detect_tables_gmft_onnx_on_layout_regions  # noqa: E402
from table_anchored_merger import detect_tables  # noqa: E402
from table_eval_metrics import compare_table_grid_lists  # noqa: E402


VARIANTS = [
    {
        "id": "gmft_only",
        "label": "GMFT only + OCR-aware + Postprocess V2",
    },
    {
        "id": "docling_only",
        "label": "Docling TableFormer only + OCR-aware + Postprocess V2",
    },
    {
        "id": "gmft_docling",
        "label": "GMFT + Docling + OCR-aware + Postprocess V2",
    },
]


def _run_variant(
    variant_id: str,
    pdf_path: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> tuple[list[Any], dict[str, float]]:
    detect_t0 = time.perf_counter()
    if variant_id == "gmft_only":
        raw_tables = detect_tables_gmft_onnx_on_layout_regions(
            str(pdf_path),
            logger,
            page_info,
            pdf_lines,
            layout_regions_by_page,
            "cpu",
        )
    elif variant_id == "docling_only":
        raw_tables = detect_tables_docling_tableformer(
            str(pdf_path),
            logger,
            page_info,
            pdf_lines,
            layout_regions_by_page,
            dpi=args.docling_dpi,
            pad_pt=args.docling_pad,
            mode=args.docling_mode,
            num_threads=args.docling_threads,
        )
    elif variant_id == "gmft_docling":
        raw_tables = detect_tables(str(pdf_path), logger, page_info, pdf_lines, layout_regions_by_page)
    else:
        raise ValueError(variant_id)
    detect_sec = time.perf_counter() - detect_t0

    post_t0 = time.perf_counter()
    post_tables = gt_bench.postprocess_current_tables(
        copy.deepcopy(raw_tables),
        layout_regions_by_page,
        pdf_lines,
        page_info,
        logger,
    )
    postprocess_sec = time.perf_counter() - post_t0
    return post_tables, {
        "detect_sec": detect_sec,
        "postprocess_sec": postprocess_sec,
        "table_total_sec": detect_sec + postprocess_sec,
    }


def _sources(tables: list[Any]) -> str:
    return "|".join(str(getattr(table, "source", "") or "") for table in tables)


def _exact_shape_rate_from_grid_cmp(cmp: dict[str, Any]) -> float:
    details = cmp.get("details") or []
    if not details:
        return 1.0
    return sum(1.0 if item.get("gt_shape") == item.get("out_shape") else 0.0 for item in details) / len(details)


def run_groundtruth(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    scans = [str(scan).zfill(2) for scan in args.scans]
    for idx, scan in enumerate(scans, 1):
        print(f"[5GT {idx}/{len(scans)}] scan{scan}", flush=True)
        ocr_pdf = args.ocr_dir / f"scan{scan}_ocr.pdf"
        logger = gt_bench.QuietLogger()
        prep_t0 = time.perf_counter()
        pdf_lines, layout_regions_by_page, page_info = gt_bench.prepare_pipeline_inputs(ocr_pdf, logger)
        preprocess_sec = time.perf_counter() - prep_t0
        gt_docx = gt_bench.groundtruth_docx_for_scan(scan, args.groundtruth_dir, args.gt03_docx)
        gt_grid = gt_bench.data_table_grids(gt_bench.docx_table_grids(gt_docx))

        for variant in VARIANTS:
            print(f"  {variant['label']}", flush=True)
            tables, timing = _run_variant(
                variant["id"],
                ocr_pdf,
                logger,
                page_info,
                pdf_lines,
                layout_regions_by_page,
                args,
            )
            pred_grid = gt_bench.table_region_grids(tables)
            cmp = compare_table_grid_lists(gt_grid, pred_grid)
            rows.append(
                {
                    "dataset": "5 Groundtruth",
                    "case_id": f"scan{scan}",
                    "variant_id": variant["id"],
                    "label": variant["label"],
                    "gt_count": len(gt_grid),
                    "pred_count": cmp["count"],
                    "shape_acc": cmp["shape_acc"],
                    "exact_shape_rate": _exact_shape_rate_from_grid_cmp(cmp),
                    "cell_text_acc": cmp["cell_text_acc"],
                    "cell_exact_rate": cmp["cell_exact_rate"],
                    "table_exact_text_rate": cmp["table_exact_text_rate"],
                    "compared_cells": cmp["compared_cells"],
                    "recall_iou50": "",
                    "mean_iou": "",
                    "ocr_line_count": len(pdf_lines),
                    "preprocess_sec": preprocess_sec,
                    **timing,
                    "end_to_end_sec": preprocess_sec + timing["table_total_sec"],
                    "sources": _sources(tables),
                }
            )
    return rows


def run_external(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    samples = load_samples(args.manifest, set(args.datasets or []))
    if args.max_cases_per_dataset:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for sample in samples:
            grouped.setdefault(str(sample["dataset"]), []).append(sample)
        samples = [
            sample
            for dataset in sorted(grouped)
            for sample in grouped[dataset][: args.max_cases_per_dataset]
        ]

    for idx, sample in enumerate(samples, 1):
        print(f"[External {idx}/{len(samples)}] {sample['dataset']} {sample['id']}", flush=True)
        dataset_slug = str(sample["dataset"]).replace(" ", "_").replace(".", "_").lower()
        raw_pdf = args.out_dir / "external_raw_pdfs" / dataset_slug / f"{sample['id']}.pdf"
        ocr_pdf = args.out_dir / "external_ocr_pdfs" / dataset_slug / f"{sample['id']}_ocr.pdf"
        external_image_to_pdf(Path(sample["image"]), raw_pdf, args.pdf_dpi)
        ocr_status = ensure_external_ocr_pdf(raw_pdf, ocr_pdf, Path(sample["image"]), args.force_ocr)
        benchmark_pdf = ocr_pdf if ocr_status["ok"] and ocr_pdf.exists() else raw_pdf

        logger = gt_bench.QuietLogger()
        prep_t0 = time.perf_counter()
        pdf_lines, _layout_from_json, page_info = gt_bench.prepare_pipeline_inputs(benchmark_pdf, logger)
        layout_regions_by_page = full_page_layout_from_page_info(page_info, args.pdf_dpi)
        preprocess_sec = time.perf_counter() - prep_t0
        gt_grid = load_gt_text_grid(Path(sample["groundtruth"]))

        for variant in VARIANTS:
            print(f"  {variant['label']}", flush=True)
            tables, timing = _run_variant(
                variant["id"],
                benchmark_pdf,
                logger,
                page_info,
                pdf_lines,
                layout_regions_by_page,
                args,
            )
            pred_rows, pred_cols, pred_count, sources, pred_grid = best_table_result(tables)
            text_cmp = compare_text_grid(gt_grid, pred_grid)
            exact_shape = sample["gt_rows"] == pred_rows and sample["gt_cols"] == pred_cols
            rows.append(
                {
                    "dataset": f"External Exact Cell:{sample['dataset']}",
                    "case_id": str(sample["id"]),
                    "variant_id": variant["id"],
                    "label": variant["label"],
                    "gt_count": 1,
                    "pred_count": pred_count,
                    "shape_acc": shape_score(sample["gt_rows"], sample["gt_cols"], pred_rows, pred_cols),
                    "exact_shape_rate": 1.0 if exact_shape else 0.0,
                    "cell_text_acc": text_cmp["cell_text_acc"],
                    "cell_exact_rate": text_cmp["cell_exact_rate"],
                    "table_exact_text_rate": 1.0 if text_cmp["table_exact_text"] else 0.0,
                    "compared_cells": text_cmp["compared_cells"],
                    "recall_iou50": "",
                    "mean_iou": "",
                    "ocr_line_count": len(pdf_lines),
                    "preprocess_sec": preprocess_sec,
                    **timing,
                    "end_to_end_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0)
                    + preprocess_sec
                    + timing["table_total_sec"],
                    "sources": "|".join(sources),
                }
            )
    return rows


def run_ctdar(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    candidates = ctdar_bench.collect_candidates(
        args.ctdar_root,
        args.track,
        args.modern_only,
        args.pdf_dpi,
        args.selection,
    )[: args.max_ctdar_cases]
    (args.out_dir / "ctdar_candidate_ranking.json").write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for idx, candidate in enumerate(candidates, 1):
        print(f"[cTDaR {idx}/{len(candidates)}] {candidate['id']}", flush=True)
        image_path = Path(candidate["image"])
        xml_path = Path(candidate["xml"])
        pdf_scale = 72.0 / float(args.pdf_dpi)
        gt_tables = ctdar_bench.parse_ctdar_xml(xml_path, pdf_scale)
        raw_pdf = args.out_dir / "ctdar_raw_pdfs" / f"{candidate['id']}.pdf"
        ocr_pdf = args.out_dir / "ctdar_ocr_pdfs" / f"{candidate['id']}_ocr.pdf"
        width_pt, _height_pt = ctdar_image_to_pdf(image_path, raw_pdf, args.pdf_dpi)
        ocr_status = ensure_ctdar_ocr_pdf(raw_pdf, ocr_pdf, image_path, args.force_ocr)
        benchmark_pdf = ocr_pdf if ocr_status["ok"] and ocr_pdf.exists() else raw_pdf

        prep_t0 = time.perf_counter()
        pdf_lines, layout_regions_by_page, page_info, ocr_line_count = prepare_ctdar_inputs(benchmark_pdf, args)
        preprocess_sec = time.perf_counter() - prep_t0
        logger = gt_bench.QuietLogger()

        for variant in VARIANTS:
            print(f"  {variant['label']}", flush=True)
            tables, timing = _run_variant(
                variant["id"],
                benchmark_pdf,
                logger,
                page_info,
                pdf_lines,
                layout_regions_by_page,
                args,
            )
            cmp = ctdar_bench.evaluate(gt_tables, tables, width_pt)
            rows.append(
                {
                    "dataset": f"cTDaR:{args.track}",
                    "case_id": str(candidate["id"]),
                    "variant_id": variant["id"],
                    "label": variant["label"],
                    "gt_count": cmp["gt_count"],
                    "pred_count": cmp["pred_count"],
                    "shape_acc": cmp["shape_acc"],
                    "exact_shape_rate": cmp["exact_shape_rate"],
                    "cell_text_acc": "",
                    "cell_exact_rate": "",
                    "table_exact_text_rate": "",
                    "compared_cells": "",
                    "recall_iou50": cmp["recall_iou50"],
                    "mean_iou": cmp["mean_best_iou"],
                    "ocr_line_count": ocr_line_count,
                    "preprocess_sec": preprocess_sec,
                    **timing,
                    "end_to_end_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0)
                    + preprocess_sec
                    + timing["table_total_sec"],
                    "sources": "|".join(cmp["sources"]),
                }
            )
    return rows


def _mean(rows: list[dict[str, Any]], key: str) -> float | str:
    values = []
    for row in rows:
        value = row.get(key, "")
        if value == "":
            continue
        values.append(float(value))
    return sum(values) / len(values) if values else ""


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    overall: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((str(row["dataset"]), str(row["variant_id"])), []).append(row)
        overall.setdefault(str(row["variant_id"]), []).append(row)

    label_by_id = {row["variant_id"]: row["label"] for row in rows}
    order_by_id = {variant["id"]: idx for idx, variant in enumerate(VARIANTS)}

    def make(dataset: str, variant_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(items)
        return {
            "dataset": dataset,
            "variant_id": variant_id,
            "label": label_by_id[variant_id],
            "cases": n,
            "gt_count": sum(int(item["gt_count"]) for item in items),
            "pred_count": sum(int(item["pred_count"]) for item in items),
            "shape_acc": _mean(items, "shape_acc"),
            "exact_shape_rate": _mean(items, "exact_shape_rate"),
            "cell_text_acc": _mean(items, "cell_text_acc"),
            "cell_exact_rate": _mean(items, "cell_exact_rate"),
            "table_exact_text_rate": _mean(items, "table_exact_text_rate"),
            "recall_iou50": _mean(items, "recall_iou50"),
            "mean_iou": _mean(items, "mean_iou"),
            "compared_cells": sum(int(item["compared_cells"]) for item in items if item["compared_cells"] != ""),
            "ocr_line_count_avg": _mean(items, "ocr_line_count"),
            "detect_sec": sum(float(item["detect_sec"]) for item in items),
            "postprocess_sec": sum(float(item["postprocess_sec"]) for item in items),
            "table_total_sec": sum(float(item["table_total_sec"]) for item in items),
            "end_to_end_sec": sum(float(item["end_to_end_sec"]) for item in items),
            "avg_table_total_sec": sum(float(item["table_total_sec"]) for item in items) / n if n else 0.0,
        }

    out = [make(dataset, variant_id, items) for (dataset, variant_id), items in grouped.items()]
    out.extend(make("__overall__", variant_id, items) for variant_id, items in overall.items())
    out.sort(key=lambda row: (row["dataset"], order_by_id.get(row["variant_id"], 99)))
    return out


def write_outputs(rows: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "details.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = summarize(rows)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    per_fields = [
        "dataset",
        "case_id",
        "variant_id",
        "label",
        "gt_count",
        "pred_count",
        "shape_acc",
        "exact_shape_rate",
        "cell_text_acc",
        "cell_exact_rate",
        "table_exact_text_rate",
        "compared_cells",
        "recall_iou50",
        "mean_iou",
        "ocr_line_count",
        "detect_sec",
        "postprocess_sec",
        "table_total_sec",
        "end_to_end_sec",
        "sources",
    ]
    with (out_dir / "per_case.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=per_fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in per_fields})

    summary_fields = [
        "dataset",
        "variant_id",
        "label",
        "cases",
        "gt_count",
        "pred_count",
        "shape_acc",
        "exact_shape_rate",
        "cell_text_acc",
        "cell_exact_rate",
        "table_exact_text_rate",
        "recall_iou50",
        "mean_iou",
        "compared_cells",
        "ocr_line_count_avg",
        "detect_sec",
        "postprocess_sec",
        "table_total_sec",
        "end_to_end_sec",
        "avg_table_total_sec",
    ]
    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        for row in summary:
            writer.writerow({field: row.get(field, "") for field in summary_fields})


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
    parser.add_argument("--ctdar-root", type=Path, default=ROOT / "temp" / "external" / "ICDAR2019_cTDaR")
    parser.add_argument("--track", default="TRACKB2", choices=["TRACKB1", "TRACKB2"])
    parser.add_argument("--max-ctdar-cases", type=int, default=20)
    parser.add_argument("--selection", choices=["lowest", "highest"], default="highest")
    parser.add_argument("--modern-only", action="store_true", default=True)
    parser.add_argument("--include-historical", action="store_false", dest="modern_only")
    parser.add_argument("--pdf-dpi", type=int, default=300)
    parser.add_argument("--doclayout-conf", type=float, default=0.25)
    parser.add_argument("--docling-dpi", type=int, default=144)
    parser.add_argument("--docling-pad", type=float, default=0.0)
    parser.add_argument("--docling-mode", choices=["fast", "accurate"], default="accurate")
    parser.add_argument("--docling-threads", type=int, default=4)
    parser.add_argument("--force-ocr", action="store_true")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    if args.suite in {"all", "groundtruth"}:
        rows.extend(run_groundtruth(args))
        write_outputs(rows, args.out_dir)
    if args.suite in {"all", "external"}:
        rows.extend(run_external(args))
        write_outputs(rows, args.out_dir)
    if args.suite in {"all", "ctdar"}:
        rows.extend(run_ctdar(args))
        write_outputs(rows, args.out_dir)
    write_outputs(rows, args.out_dir)
    print(json.dumps({"out_dir": str(args.out_dir), "rows": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
