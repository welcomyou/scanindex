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
    QuietLogger,
    ensure_ocr_pdf,
    image_to_pdf,
    prepare_inputs,
)
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


def run_case(candidate: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    image_path = Path(candidate["image"])
    xml_path = Path(candidate["xml"])
    pdf_scale = 72.0 / float(args.pdf_dpi)
    gt_tables = ctdar_bench.parse_ctdar_xml(xml_path, pdf_scale)

    raw_pdf = args.out_dir / "raw_pdfs" / f"{candidate['id']}.pdf"
    ocr_pdf = args.out_dir / "ocr_pdfs" / f"{candidate['id']}_ocr.pdf"
    width_pt, _height_pt = image_to_pdf(image_path, raw_pdf, args.pdf_dpi)
    ocr_status = ensure_ocr_pdf(raw_pdf, ocr_pdf, image_path, args.force_ocr)
    benchmark_pdf = ocr_pdf if ocr_status["ok"] and ocr_pdf.exists() else raw_pdf

    prep_t0 = time.perf_counter()
    pdf_lines, layout_regions_by_page, page_info, ocr_line_count = prepare_inputs(benchmark_pdf, args)
    preprocess_sec = time.perf_counter() - prep_t0

    logger = QuietLogger()
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
        eval_result = ctdar_bench.evaluate(gt_tables, out_tables, width_pt)
        engine_time = engine_result["timing"]["engine_total_sec"]
        total_sec = engine_time + postprocess_sec
        rows.append(
            {
                "id": candidate["id"],
                "variant_id": variant["id"],
                "label": variant["label"],
                "variant_order": int(variant["order"]),
                "image": str(image_path),
                "xml": str(xml_path),
                "line_density": candidate.get("line_density", 0.0),
                "gt_count": len(gt_tables),
                "gt_shapes": [[t.rows, t.cols] for t in gt_tables],
                "gt_cell_count": sum(t.cell_count for t in gt_tables),
                "gt_span_cell_count": sum(t.span_cell_count for t in gt_tables),
                "ocr": ocr_status,
                "ocr_line_count": ocr_line_count,
                "feed_ocr_available": ocr_line_count > 0,
                "comparison": eval_result,
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
                "sources": eval_result["sources"],
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

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        grouped.setdefault(item["variant_id"], []).append(item)

    summary = []
    for variant_id, items in grouped.items():
        n = len(items)
        summary.append(
            {
                "variant_id": variant_id,
                "label": items[0]["label"],
                "order": items[0]["variant_order"],
                "cases": n,
                "shape_acc": sum(i["comparison"]["shape_acc"] for i in items) / n,
                "exact_shape_rate": sum(i["comparison"]["exact_shape_rate"] for i in items) / n,
                "recall_iou50": sum(i["comparison"]["recall_iou50"] for i in items) / n,
                "mean_iou": sum(i["comparison"]["mean_best_iou"] for i in items) / n,
                "pred_count": sum(i["comparison"]["pred_count"] for i in items),
                "gt_count": sum(i["comparison"]["gt_count"] for i in items),
                "ocr_line_count_avg": sum(i["ocr_line_count"] for i in items) / n,
                "ocr_sec": sum(i["timing"]["ocr_sec"] for i in items),
                "preprocess_sec": sum(i["timing"]["preprocess_sec"] for i in items),
                "detect_sec": sum(i["timing"]["detect_sec"] for i in items),
                "text_map_sec": sum(i["timing"]["text_map_sec"] for i in items),
                "postprocess_sec": sum(i["timing"]["postprocess_sec"] for i in items),
                "table_total_sec": sum(i["timing"]["table_total_sec"] for i in items),
                "end_to_end_sec": sum(i["timing"]["end_to_end_sec"] for i in items),
            }
        )
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
        "exact_shape_rate",
        "recall_iou50",
        "mean_iou",
        "pred_count",
        "gt_count",
        "ocr_line_count_avg",
        "ocr_sec",
        "preprocess_sec",
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

    per_case_fields = [
        "variant_id",
        "label",
        "id",
        "shape_acc",
        "exact_shape_rate",
        "recall_iou50",
        "mean_iou",
        "gt_shapes",
        "pred_shapes",
        "sources",
        "ocr_line_count",
        "ocr_sec",
        "table_total_sec",
        "end_to_end_sec",
    ]
    with (out_dir / "per_case.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=per_case_fields)
        writer.writeheader()
        for item in results:
            cmp = item["comparison"]
            writer.writerow(
                {
                    "variant_id": item["variant_id"],
                    "label": item["label"],
                    "id": item["id"],
                    "shape_acc": cmp["shape_acc"],
                    "exact_shape_rate": cmp["exact_shape_rate"],
                    "recall_iou50": cmp["recall_iou50"],
                    "mean_iou": cmp["mean_best_iou"],
                    "gt_shapes": json.dumps(item["gt_shapes"]),
                    "pred_shapes": json.dumps(cmp["pred_shapes"]),
                    "sources": "|".join(cmp["sources"]),
                    "ocr_line_count": item["ocr_line_count"],
                    "ocr_sec": item["timing"]["ocr_sec"],
                    "table_total_sec": item["timing"]["table_total_sec"],
                    "end_to_end_sec": item["timing"]["end_to_end_sec"],
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ctdar-root", type=Path, default=ROOT / "temp" / "external" / "ICDAR2019_cTDaR")
    parser.add_argument("--track", default="TRACKB2", choices=["TRACKB1", "TRACKB2"])
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--variants", nargs="*", default=[])
    parser.add_argument("--max-cases", type=int, default=20)
    parser.add_argument("--selection", choices=["lowest", "highest"], default="highest")
    parser.add_argument("--modern-only", action="store_true", default=True)
    parser.add_argument("--include-historical", action="store_false", dest="modern_only")
    parser.add_argument("--pdf-dpi", type=int, default=300)
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
    wanted_variants = {item.lower() for item in args.variants}
    args.selected_variants = [
        variant for variant in SINGLE_DEFS if not wanted_variants or variant["id"].lower() in wanted_variants
    ]
    missing_variants = sorted(wanted_variants - {variant["id"].lower() for variant in args.selected_variants})
    if missing_variants:
        raise ValueError(f"Unknown variant ids: {missing_variants}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    candidates = ctdar_bench.collect_candidates(
        args.ctdar_root,
        args.track,
        args.modern_only,
        args.pdf_dpi,
        args.selection,
    )
    (args.out_dir / "candidate_ranking.json").write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    selected = candidates[: args.max_cases]
    results: list[dict[str, Any]] = []
    for idx, candidate in enumerate(selected, 1):
        print(f"[{idx}/{len(selected)}] {candidate['id']} density={candidate['line_density']:.6f}", flush=True)
        try:
            results.extend(run_case(candidate, args))
        except Exception as exc:
            print(f"  ERROR {candidate['id']}: {exc!r}", flush=True)
        write_outputs(results, args.out_dir)
    write_outputs(results, args.out_dir)
    print(json.dumps({"out_dir": str(args.out_dir), "rows": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
