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
from docling_tableformer_v2_onnx_engine import (  # noqa: E402
    detect_tables_docling_tableformer_v2_onnx,
)
from docling_tableformer_v2_torch_engine import (  # noqa: E402
    detect_tables_docling_tableformer_v2_torch,
)
from gmft_onnx_table_engine import detect_tables_gmft_onnx_on_layout_regions  # noqa: E402
from table_anchored_merger import (  # noqa: E402
    _choose_docling_first_candidates,
    _group_tables_by_page,
    _layout_pages_with_tables,
    _mark_table_source,
    detect_tables,
)
from table_eval_metrics import compare_table_grid_lists  # noqa: E402


Detector = Callable[
    [Path, Any, dict, list[Any], dict[int, list[dict[str, Any]]], argparse.Namespace],
    list[Any],
]


def _docling_v1_torch(
    pdf_path: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> list[Any]:
    return detect_tables_docling_tableformer(
        str(pdf_path),
        logger,
        page_info,
        pdf_lines,
        layout_regions_by_page,
        dpi=args.docling_dpi,
        pad_pt=args.docling_pad,
        mode=args.docling_mode,
        num_threads=args.threads,
    )


def _docling_v1_onnx(
    pdf_path: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> list[Any]:
    return detect_tables_docling_tableformer_v1_onnx(
        str(pdf_path),
        logger,
        page_info,
        pdf_lines,
        layout_regions_by_page,
        dpi=args.docling_dpi,
        pad_pt=args.docling_pad,
        num_threads=args.threads,
    )


def _docling_v1_onnx_fixed_cache(
    pdf_path: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> list[Any]:
    return detect_tables_docling_tableformer_v1_onnx(
        str(pdf_path),
        logger,
        page_info,
        pdf_lines,
        layout_regions_by_page,
        dpi=args.docling_dpi,
        pad_pt=args.docling_pad,
        num_threads=args.threads,
        artifact_dir=ROOT / "models" / "docling_tableformer_v1_onnx",
    )


def _docling_v1_onnx_temp_tableformer(
    pdf_path: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> list[Any]:
    return detect_tables_docling_tableformer_v1_onnx(
        str(pdf_path),
        logger,
        page_info,
        pdf_lines,
        layout_regions_by_page,
        dpi=args.docling_dpi,
        pad_pt=args.docling_pad,
        num_threads=args.threads,
        artifact_dir=ROOT / "temp" / "tableformer_onnx",
    )


def _docling_v2_torch(
    pdf_path: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> list[Any]:
    return detect_tables_docling_tableformer_v2_torch(
        str(pdf_path),
        logger,
        page_info,
        pdf_lines,
        layout_regions_by_page,
        dpi=args.docling_dpi,
        pad_pt=args.docling_pad,
        num_threads=args.threads,
    )


def _docling_v2_onnx(
    pdf_path: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> list[Any]:
    return detect_tables_docling_tableformer_v2_onnx(
        str(pdf_path),
        logger,
        page_info,
        pdf_lines,
        layout_regions_by_page,
        dpi=args.docling_dpi,
        pad_pt=args.docling_pad,
        num_threads=args.threads,
    )


def _gmft_only(
    pdf_path: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> list[Any]:
    del args
    return detect_tables_gmft_onnx_on_layout_regions(
        str(pdf_path),
        logger,
        page_info,
        pdf_lines,
        layout_regions_by_page,
        "cpu",
    )


def _current_combo_v1(
    pdf_path: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> list[Any]:
    del args
    return detect_tables(str(pdf_path), logger, page_info, pdf_lines, layout_regions_by_page)


def _combo_with_docling(
    docling_detector: Detector,
    docling_source: str,
) -> Detector:
    def run(
        pdf_path: Path,
        logger: Any,
        page_info: dict,
        pdf_lines: list[Any],
        layout_regions_by_page: dict[int, list[dict[str, Any]]],
        args: argparse.Namespace,
    ) -> list[Any]:
        import concurrent.futures

        layout_pages = _layout_pages_with_tables(layout_regions_by_page)
        if not layout_pages:
            return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_gmft = executor.submit(
                _gmft_only,
                pdf_path,
                logger,
                page_info,
                pdf_lines,
                layout_regions_by_page,
                args,
            )
            future_docling = executor.submit(
                docling_detector,
                pdf_path,
                logger,
                page_info,
                pdf_lines,
                layout_regions_by_page,
                args,
            )
            tables_gmft = _mark_table_source(future_gmft.result(), "gmft_onnx_layout")
            tables_docling = _mark_table_source(future_docling.result(), docling_source)

        gmft_by_page = _group_tables_by_page(tables_gmft)
        docling_by_page = _group_tables_by_page(tables_docling)
        final_tables: list[Any] = []
        for page in layout_pages:
            final_tables.extend(
                _choose_docling_first_candidates(
                    page,
                    {
                        "gmft_onnx_layout": gmft_by_page.get(page, []),
                        "docling_tableformer": docling_by_page.get(page, []),
                    },
                    layout_regions_by_page,
                    pdf_lines,
                    logger,
                )
            )
        return final_tables

    return run


VARIANTS: list[dict[str, Any]] = [
    {
        "id": "docling_v1_torch",
        "label": "Docling current TableFormer PyTorch + OCR-aware + Postprocess V2",
        "detector": _docling_v1_torch,
    },
    {
        "id": "docling_v1_onnx",
        "label": "Docling v1 accurate ONNX production step-cache + official postprocess + Postprocess V2",
        "detector": _docling_v1_onnx,
    },
    {
        "id": "docling_v1_onnx_fixed_cache",
        "label": "Docling v1 accurate ONNX fixed-cache + official postprocess + Postprocess V2",
        "detector": _docling_v1_onnx_fixed_cache,
    },
    {
        "id": "docling_v1_onnx_temp_tableformer",
        "label": "Docling v1 accurate ONNX temp/tableformer_onnx step-cache + official postprocess + Postprocess V2",
        "detector": _docling_v1_onnx_temp_tableformer,
    },
    {
        "id": "docling_v2_torch",
        "label": "Docling TableFormerV2 PyTorch + OCR-aware + Postprocess V2",
        "detector": _docling_v2_torch,
    },
    {
        "id": "docling_v2_onnx",
        "label": "Docling TableFormerV2 ONNX + OCR-aware + Postprocess V2",
        "detector": _docling_v2_onnx,
    },
    {
        "id": "gmft_docling_v1_current",
        "label": "Current GMFT + Docling v1 accurate ONNX + Postprocess V2",
        "detector": _current_combo_v1,
    },
    {
        "id": "gmft_docling_v1_onnx",
        "label": "GMFT + Docling v1 accurate ONNX + Postprocess V2",
        "detector": _combo_with_docling(_docling_v1_onnx, "docling_tableformer"),
    },
    {
        "id": "gmft_docling_v2_torch",
        "label": "GMFT + Docling TableFormerV2 PyTorch + Postprocess V2",
        "detector": _combo_with_docling(_docling_v2_torch, "docling_tableformer"),
    },
    {
        "id": "gmft_docling_v2_onnx",
        "label": "GMFT + Docling TableFormerV2 ONNX + Postprocess V2",
        "detector": _combo_with_docling(_docling_v2_onnx, "docling_tableformer"),
    },
]


def _select_variants(ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return VARIANTS
    wanted = set(ids)
    selected = [variant for variant in VARIANTS if variant["id"] in wanted]
    missing = sorted(wanted - {variant["id"] for variant in selected})
    if missing:
        raise ValueError(f"Unknown variant ids: {missing}")
    return selected


def _exact_shape_rate(cmp: dict[str, Any]) -> float:
    details = cmp.get("details") or []
    if not details:
        return 1.0
    return sum(1.0 if item.get("gt_shape") == item.get("out_shape") else 0.0 for item in details) / len(details)


def _sources(tables: list[Any]) -> str:
    values = []
    for table in tables:
        engine = str(getattr(table, "engine", "") or "")
        source = str(getattr(table, "source", "") or "")
        values.append(engine or source)
    return "|".join(values)


def _run_variant(
    variant: dict[str, Any],
    ocr_pdf: Path,
    logger: Any,
    page_info: dict,
    pdf_lines: list[Any],
    layout_regions_by_page: dict[int, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> tuple[list[Any], list[Any], dict[str, float]]:
    detect_t0 = time.perf_counter()
    raw_tables = variant["detector"](
        ocr_pdf,
        logger,
        page_info,
        pdf_lines,
        layout_regions_by_page,
        args,
    )
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
    return raw_tables, post_tables, {
        "detect_sec": detect_sec,
        "postprocess_sec": postprocess_sec,
        "table_total_sec": detect_sec + postprocess_sec,
    }


def _warmup(
    selected: list[dict[str, Any]],
    args: argparse.Namespace,
) -> None:
    scan = str(args.scans[0]).zfill(2)
    ocr_pdf = args.ocr_dir / f"scan{scan}_ocr.pdf"
    logger = gt_bench.QuietLogger()
    pdf_lines, layout_regions_by_page, page_info = gt_bench.prepare_pipeline_inputs(ocr_pdf, logger)
    for variant in selected:
        print(f"[warmup] {variant['id']}", flush=True)
        try:
            variant["detector"](ocr_pdf, logger, page_info, pdf_lines, layout_regions_by_page, args)
        except Exception as exc:
            print(f"[warmup failed] {variant['id']}: {exc}", flush=True)


def run_groundtruth(args: argparse.Namespace) -> list[dict[str, Any]]:
    selected = _select_variants(args.variants)
    if args.warmup:
        _warmup(selected, args)

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

        for variant in selected:
            print(f"  {variant['id']}", flush=True)
            raw_tables, post_tables, timing = _run_variant(
                variant,
                ocr_pdf,
                logger,
                page_info,
                pdf_lines,
                layout_regions_by_page,
                args,
            )
            raw_cmp = compare_table_grid_lists(gt_grid, gt_bench.table_region_grids(raw_tables))
            post_cmp = compare_table_grid_lists(gt_grid, gt_bench.table_region_grids(post_tables))
            rows.append(
                {
                    "dataset": "5 Groundtruth",
                    "case_id": f"scan{scan}",
                    "variant_id": variant["id"],
                    "label": variant["label"],
                    "gt_count": len(gt_grid),
                    "raw_pred_count": raw_cmp["count"],
                    "pred_count": post_cmp["count"],
                    "raw_shape_acc": raw_cmp["shape_acc"],
                    "raw_exact_shape_rate": _exact_shape_rate(raw_cmp),
                    "shape_acc": post_cmp["shape_acc"],
                    "exact_shape_rate": _exact_shape_rate(post_cmp),
                    "cell_text_acc": post_cmp["cell_text_acc"],
                    "cell_exact_rate": post_cmp["cell_exact_rate"],
                    "table_exact_text_rate": post_cmp["table_exact_text_rate"],
                    "compared_cells": post_cmp["compared_cells"],
                    "preprocess_sec": preprocess_sec,
                    **timing,
                    "end_to_end_sec": preprocess_sec + timing["table_total_sec"],
                    "sources": _sources(post_tables),
                }
            )
    return rows


def _mean(rows: list[dict[str, Any]], key: str) -> float:
    values = [float(row[key]) for row in rows if row.get(key, "") != ""]
    return sum(values) / len(values) if values else 0.0


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["variant_id"]), []).append(row)
    label_by_id = {row["variant_id"]: row["label"] for row in rows}
    out = []
    for variant_id, items in grouped.items():
        out.append(
            {
                "dataset": "5 Groundtruth",
                "variant_id": variant_id,
                "label": label_by_id[variant_id],
                "cases": len(items),
                "gt_count": sum(int(item["gt_count"]) for item in items),
                "pred_count": sum(int(item["pred_count"]) for item in items),
                "raw_shape_acc": _mean(items, "raw_shape_acc"),
                "raw_exact_shape_rate": _mean(items, "raw_exact_shape_rate"),
                "shape_acc": _mean(items, "shape_acc"),
                "exact_shape_rate": _mean(items, "exact_shape_rate"),
                "cell_text_acc": _mean(items, "cell_text_acc"),
                "cell_exact_rate": _mean(items, "cell_exact_rate"),
                "table_exact_text_rate": _mean(items, "table_exact_text_rate"),
                "compared_cells": sum(int(item["compared_cells"]) for item in items),
                "preprocess_sec": sum(float(item["preprocess_sec"]) for item in items),
                "detect_sec": sum(float(item["detect_sec"]) for item in items),
                "postprocess_sec": sum(float(item["postprocess_sec"]) for item in items),
                "table_total_sec": sum(float(item["table_total_sec"]) for item in items),
                "end_to_end_sec": sum(float(item["end_to_end_sec"]) for item in items),
                "avg_table_total_sec": _mean(items, "table_total_sec"),
            }
        )
    out.sort(key=lambda row: (-float(row["cell_exact_rate"]), float(row["table_total_sec"])))
    return out


def write_outputs(rows: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(rows)
    (out_dir / "details.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    details_fields = [
        "dataset",
        "case_id",
        "variant_id",
        "label",
        "gt_count",
        "raw_pred_count",
        "pred_count",
        "raw_shape_acc",
        "raw_exact_shape_rate",
        "shape_acc",
        "exact_shape_rate",
        "cell_text_acc",
        "cell_exact_rate",
        "table_exact_text_rate",
        "compared_cells",
        "preprocess_sec",
        "detect_sec",
        "postprocess_sec",
        "table_total_sec",
        "end_to_end_sec",
        "sources",
    ]
    with (out_dir / "per_case.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=details_fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in details_fields})

    summary_fields = [
        "dataset",
        "variant_id",
        "label",
        "cases",
        "gt_count",
        "pred_count",
        "raw_shape_acc",
        "raw_exact_shape_rate",
        "shape_acc",
        "exact_shape_rate",
        "cell_text_acc",
        "cell_exact_rate",
        "table_exact_text_rate",
        "compared_cells",
        "preprocess_sec",
        "detect_sec",
        "postprocess_sec",
        "table_total_sec",
        "end_to_end_sec",
        "avg_table_total_sec",
    ]
    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=summary_fields)
        writer.writeheader()
        for row in summary:
            writer.writerow({field: row.get(field, "") for field in summary_fields})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=ROOT / "temp" / "docling_v2_onnx_5gt_benchmark")
    parser.add_argument("--ocr-dir", type=Path, default=ROOT / "temp" / "groundtruth5_pipeline")
    parser.add_argument("--groundtruth-dir", type=Path, default=Path(r"C:\Users\nhquan\Downloads\groundtruth"))
    parser.add_argument("--gt03-docx", type=Path, default=ROOT / "temp" / "groundtruth4_scan_word" / "groundtruth03_converted.docx")
    parser.add_argument("--scans", nargs="+", default=["01", "02", "03", "04", "05"])
    parser.add_argument("--variants", nargs="*", default=[])
    parser.add_argument("--docling-dpi", type=int, default=144)
    parser.add_argument("--docling-pad", type=float, default=0.0)
    parser.add_argument("--docling-mode", choices=["fast", "accurate"], default="accurate")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--warmup", action="store_true")
    args = parser.parse_args()

    rows = run_groundtruth(args)
    write_outputs(rows, args.out_dir)
    print(json.dumps({"out_dir": str(args.out_dir), "rows": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
