from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

import fitz
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import benchmark_borderless_ctdar_current as ctdar_bench  # noqa: E402
import benchmark_current_table_pipeline as gt_bench  # noqa: E402
from benchmark_pair_ensembles import (  # noqa: E402
    ENGINE_SPECS,
    PAIR_DEFS,
    combine_pair,
    run_engine,
)


class QuietLogger(gt_bench.QuietLogger):
    pass


def image_to_pdf(image_path: Path, pdf_path: Path, dpi: int) -> tuple[float, float]:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        width_pt = im.width * 72.0 / float(dpi)
        height_pt = im.height * 72.0 / float(dpi)
        im.save(pdf_path, "PDF", resolution=float(dpi))
    return width_pt, height_pt


def render_pdf_page(pdf_path: Path) -> Image.Image:
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
        return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    finally:
        doc.close()


def ensure_ocr_pdf(raw_pdf: Path, ocr_pdf: Path, source_image: Path, force: bool) -> dict[str, Any]:
    if not force and ocr_pdf.exists() and Path(str(ocr_pdf) + ".json").exists():
        return {"ok": True, "cached": True, "ocr_sec": 0.0, "message": "cached"}

    from scanindex.core.ocr import direct_engine as direct_ocr_engine

    ocr_pdf.parent.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []
    t0 = time.perf_counter()
    ok, msg = direct_ocr_engine.process_pdf(
        str(raw_pdf),
        str(ocr_pdf),
        num_pages=0,
        update_callback=lambda message, level="info": logs.append(str(message)),
        source_document_path=str(source_image),
        allow_page_parallel=False,
    )
    ocr_sec = time.perf_counter() - t0
    return {
        "ok": bool(ok),
        "cached": False,
        "ocr_sec": ocr_sec,
        "message": msg or "",
        "log_tail": logs[-20:],
    }


def fallback_layout_regions(pdf_path: Path, conf: float) -> dict[int, list[dict[str, Any]]]:
    page_image = render_pdf_page(pdf_path)
    return {1: ctdar_bench.analyze_layout(page_image, conf)}


def prepare_inputs(ocr_pdf: Path, args: argparse.Namespace) -> tuple[list[Any], dict[int, list[dict[str, Any]]], dict, int]:
    logger = QuietLogger()
    pdf_lines, layout_regions_by_page, page_info = gt_bench.prepare_pipeline_inputs(ocr_pdf, logger)
    if not layout_regions_by_page or not layout_regions_by_page.get(1):
        layout_regions_by_page = fallback_layout_regions(ocr_pdf, args.doclayout_conf)
    line_count = len(pdf_lines)
    return pdf_lines, layout_regions_by_page, page_info, line_count


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
    feed_ocr_available = ocr_line_count > 0

    logger = QuietLogger()
    engine_results: dict[str, dict[str, Any]] = {}
    for spec_id, spec in ENGINE_SPECS.items():
        print(f"  engine {spec['label']}", flush=True)
        # If OCR failed, structure recognizers still run, but no model receives
        # fabricated OCR boxes.
        original_feed = spec["feed_ocr"]
        if original_feed and not feed_ocr_available:
            spec = {**spec, "feed_ocr": False}
            ENGINE_SPECS[spec_id] = spec
        try:
            engine_results[spec_id] = run_engine(
                spec_id,
                benchmark_pdf,
                logger,
                page_info,
                pdf_lines,
                layout_regions_by_page,
                args,
            )
        finally:
            if original_feed != ENGINE_SPECS[spec_id]["feed_ocr"]:
                ENGINE_SPECS[spec_id] = {**ENGINE_SPECS[spec_id], "feed_ocr": original_feed}

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

        eval_result = ctdar_bench.evaluate(gt_tables, out_tables, width_pt)
        left_id, right_id = pair["engines"]
        left_time = engine_results[left_id]["timing"]["engine_total_sec"]
        right_time = engine_results[right_id]["timing"]["engine_total_sec"]
        detect_sec = engine_results[left_id]["timing"]["detect_sec"] + engine_results[right_id]["timing"]["detect_sec"]
        text_map_sec = engine_results[left_id]["timing"]["text_map_sec"] + engine_results[right_id]["timing"]["text_map_sec"]
        sequential_sec = left_time + right_time + select_sec + postprocess_sec
        parallel_est_sec = max(left_time, right_time) + select_sec + postprocess_sec

        rows.append({
            "id": candidate["id"],
            "variant_id": pair["id"],
            "label": pair["label"],
            "variant_order": int(pair["order"]),
            "image": str(image_path),
            "xml": str(xml_path),
            "line_density": candidate.get("line_density", 0.0),
            "gt_count": len(gt_tables),
            "gt_shapes": [[t.rows, t.cols] for t in gt_tables],
            "gt_cell_count": sum(t.cell_count for t in gt_tables),
            "gt_span_cell_count": sum(t.span_cell_count for t in gt_tables),
            "ocr": ocr_status,
            "ocr_line_count": ocr_line_count,
            "feed_ocr_available": feed_ocr_available,
            "comparison": eval_result,
            "timing": {
                "ocr_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0),
                "preprocess_sec": preprocess_sec,
                "detect_sec": detect_sec,
                "text_map_sec": text_map_sec,
                "select_sec": select_sec,
                "postprocess_sec": postprocess_sec,
                "table_total_sequential_sec": sequential_sec,
                "table_total_parallel_est_sec": parallel_est_sec,
                "end_to_end_sequential_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0) + preprocess_sec + sequential_sec,
                "end_to_end_parallel_est_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0) + preprocess_sec + parallel_est_sec,
            },
            "engine_timing": {
                left_id: engine_results[left_id]["timing"],
                right_id: engine_results[right_id]["timing"],
            },
            "sources": eval_result["sources"],
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
        n = len(items)
        summary.append({
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
            "select_sec": sum(i["timing"]["select_sec"] for i in items),
            "postprocess_sec": sum(i["timing"]["postprocess_sec"] for i in items),
            "table_total_sequential_sec": sum(i["timing"]["table_total_sequential_sec"] for i in items),
            "table_total_parallel_est_sec": sum(i["timing"]["table_total_parallel_est_sec"] for i in items),
            "end_to_end_sequential_sec": sum(i["timing"]["end_to_end_sequential_sec"] for i in items),
            "end_to_end_parallel_est_sec": sum(i["timing"]["end_to_end_parallel_est_sec"] for i in items),
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
        "select_sec",
        "postprocess_sec",
        "table_total_sequential_sec",
        "table_total_parallel_est_sec",
        "end_to_end_sequential_sec",
        "end_to_end_parallel_est_sec",
    ]
    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        for row in summary:
            writer.writerow({field: row[field] for field in summary_fields})

    scan_fields = [
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
        "table_total_parallel_est_sec",
        "end_to_end_parallel_est_sec",
    ]
    with (out_dir / "per_case.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=scan_fields)
        writer.writeheader()
        for item in results:
            cmp = item["comparison"]
            writer.writerow({
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
                "table_total_parallel_est_sec": item["timing"]["table_total_parallel_est_sec"],
                "end_to_end_parallel_est_sec": item["timing"]["end_to_end_parallel_est_sec"],
            })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ctdar-root", type=Path, default=ROOT / "temp" / "external" / "ICDAR2019_cTDaR")
    parser.add_argument("--track", default="TRACKB2", choices=["TRACKB1", "TRACKB2"])
    parser.add_argument("--out-dir", type=Path, required=True)
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
            write_outputs(results, args.out_dir)
        except Exception as exc:
            print(f"  ERROR {candidate['id']}: {exc!r}", flush=True)
    write_outputs(results, args.out_dir)
    print(json.dumps({"out_dir": str(args.out_dir), "rows": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
