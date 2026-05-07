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
from rapidtable_structure_engine import detect_tables_structure_recognizer  # noqa: E402
from table_anchored_merger import (  # noqa: E402
    _text_from_table_cell_lines,
    get_lines_in_rect,
)


def table_text_resolver(pdf_lines: list[Any], logger: Any):
    lines_by_page: dict[int, list[Any]] = {}
    for line in pdf_lines:
        lines_by_page.setdefault(int(getattr(line, "page", 0) or 0), []).append(line)

    def resolve(page: int, bbox: tuple[float, float, float, float]) -> str:
        cell_lines = get_lines_in_rect(bbox, lines_by_page.get(page, []))
        return _text_from_table_cell_lines(cell_lines, page, bbox[0], bbox[2], logger)

    return resolve


def run_groundtruth_scan(scan: str, args: argparse.Namespace) -> dict[str, Any]:
    ocr_pdf = args.ocr_dir / f"scan{scan}_ocr.pdf"
    logger = gt_bench.QuietLogger()

    prep_t0 = time.perf_counter()
    pdf_lines, layout_regions_by_page, page_info = gt_bench.prepare_pipeline_inputs(ocr_pdf, logger)
    preprocess_sec = time.perf_counter() - prep_t0

    raw_t0 = time.perf_counter()
    raw_tables = detect_tables_structure_recognizer(
        str(ocr_pdf),
        logger,
        page_info,
        pdf_lines,
        layout_regions_by_page,
        recognizer=args.recognizer,
        dpi=args.dpi,
        pad_pt=args.pad_pt,
        text_resolver=table_text_resolver(pdf_lines, logger),
        model_path=str(args.model_path) if args.model_path else None,
        dict_path=str(args.dict_path) if args.dict_path else None,
        rapidtable_use_ocr_results=args.rapidtable_use_ocr_results,
        wired_use_ocr_results=args.wired_use_ocr_results,
    )
    raw_detect_sec = time.perf_counter() - raw_t0

    post_t0 = time.perf_counter()
    post_tables = gt_bench.postprocess_current_tables(
        copy.deepcopy(raw_tables),
        layout_regions_by_page,
        pdf_lines,
        page_info,
        logger,
    )
    postprocess_sec = time.perf_counter() - post_t0

    gt_docx = gt_bench.groundtruth_docx_for_scan(scan, args.groundtruth_dir, args.gt03_docx)
    gt_data = gt_bench.data_tables(gt_bench.docx_tables(gt_docx))
    raw_data = gt_bench.table_region_data(raw_tables)
    post_data = gt_bench.table_region_data(post_tables)

    return {
        "scan": scan,
        "gt_shapes": [list(t[:2]) for t in gt_data],
        "raw_shapes": [list(t[:2]) for t in raw_data],
        "post_shapes": [list(t[:2]) for t in post_data],
        "raw": gt_bench.compare_tables(gt_data, raw_data),
        "post": gt_bench.compare_tables(gt_data, post_data),
        "raw_sources": [getattr(table, "source", "") for table in raw_tables],
        "post_sources": [getattr(table, "source", "") for table in post_tables],
        "timing": {
            "preprocess_sec": preprocess_sec,
            "raw_detect_sec": raw_detect_sec,
            "postprocess_sec": postprocess_sec,
            "table_total_sec": raw_detect_sec + postprocess_sec,
        },
        "log_tail": logger.lines[-80:],
    }


def write_groundtruth_outputs(results: list[dict[str, Any]], args: argparse.Namespace) -> None:
    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": args.recognizer,
        "model_path": str(args.model_path) if args.model_path else "",
        "dict_path": str(args.dict_path) if args.dict_path else "",
        "dpi": args.dpi,
        "pad_pt": args.pad_pt,
        "rapidtable_use_ocr_results": args.rapidtable_use_ocr_results,
        "wired_use_ocr_results": args.wired_use_ocr_results,
        "comparison": results,
        "aggregate": {
            "cases": len(results),
            "raw_shape_acc_avg": sum(r["raw"]["shape_acc"] for r in results) / len(results) if results else 0.0,
            "post_shape_acc_avg": sum(r["post"]["shape_acc"] for r in results) / len(results) if results else 0.0,
            "raw_text_acc_avg": sum(r["raw"]["text_acc"] for r in results) / len(results) if results else 0.0,
            "post_text_acc_avg": sum(r["post"]["text_acc"] for r in results) / len(results) if results else 0.0,
            "table_total_sec": sum(r["timing"]["table_total_sec"] for r in results),
            "raw_detect_sec": sum(r["timing"]["raw_detect_sec"] for r in results),
            "postprocess_sec": sum(r["timing"]["postprocess_sec"] for r in results),
        },
    }
    (args.out_dir / "comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (args.out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "scan",
                "gt_count",
                "raw_count",
                "post_count",
                "raw_shape_acc",
                "post_shape_acc",
                "raw_text_acc",
                "post_text_acc",
                "preprocess_sec",
                "raw_detect_sec",
                "postprocess_sec",
                "table_total_sec",
                "raw_shapes",
                "post_shapes",
                "sources",
            ]
        )
        for item in results:
            writer.writerow(
                [
                    item["scan"],
                    item["raw"]["gt_count"],
                    item["raw"]["count"],
                    item["post"]["count"],
                    item["raw"]["shape_acc"],
                    item["post"]["shape_acc"],
                    item["raw"]["text_acc"],
                    item["post"]["text_acc"],
                    item["timing"]["preprocess_sec"],
                    item["timing"]["raw_detect_sec"],
                    item["timing"]["postprocess_sec"],
                    item["timing"]["table_total_sec"],
                    json.dumps(item["raw_shapes"]),
                    json.dumps(item["post_shapes"]),
                    "|".join(item["post_sources"]),
                ]
            )


def run_groundtruth(args: argparse.Namespace) -> int:
    scans = [str(scan).zfill(2) for scan in args.scans]
    results: list[dict[str, Any]] = []
    for idx, scan in enumerate(scans, 1):
        print(f"[{idx}/{len(scans)}] scan{scan} {args.recognizer}", flush=True)
        results.append(run_groundtruth_scan(scan, args))
        write_groundtruth_outputs(results, args)
    write_groundtruth_outputs(results, args)
    print(json.dumps({"out_dir": str(args.out_dir), "cases": len(results)}, ensure_ascii=False))
    return 0


def run_ctdar_case(candidate: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    image_path = Path(candidate["image"])
    xml_path = Path(candidate["xml"])
    pdf_scale = 72.0 / float(args.pdf_dpi)
    gt_tables = ctdar_bench.parse_ctdar_xml(xml_path, pdf_scale)
    pdf_path = args.out_dir / "pdfs" / f"{candidate['id']}.pdf"

    width_pt, height_pt = ctdar_bench.image_to_pdf(image_path, pdf_path, args.pdf_dpi)
    page_info = {1: {"width": width_pt, "height": height_pt}}
    page_image = ctdar_bench.render_pdf_page(pdf_path)

    layout_t0 = time.perf_counter()
    layout_regions = ctdar_bench.analyze_layout(page_image, args.doclayout_conf)
    layout_sec = time.perf_counter() - layout_t0
    layout_regions_by_page = {1: layout_regions}

    logger = ctdar_bench.QuietLogger()
    detect_t0 = time.perf_counter()
    raw_tables = detect_tables_structure_recognizer(
        str(pdf_path),
        logger,
        page_info,
        [],
        layout_regions_by_page,
        recognizer=args.recognizer,
        dpi=args.dpi,
        pad_pt=args.pad_pt,
        text_resolver=None,
        model_path=str(args.model_path) if args.model_path else None,
        dict_path=str(args.dict_path) if args.dict_path else None,
        rapidtable_use_ocr_results=False,
        wired_use_ocr_results=False,
    )
    detect_sec = time.perf_counter() - detect_t0

    post_t0 = time.perf_counter()
    post_tables = ctdar_bench.postprocess_current_tables(
        copy.deepcopy(raw_tables),
        layout_regions_by_page,
        page_info,
        logger,
    )
    post_sec = time.perf_counter() - post_t0

    raw_eval = ctdar_bench.evaluate(gt_tables, raw_tables, width_pt)
    post_eval = ctdar_bench.evaluate(gt_tables, post_tables, width_pt)
    layout_eval = ctdar_bench.evaluate_doclayout(gt_tables, layout_regions)

    return {
        "id": candidate["id"],
        "image": str(image_path),
        "xml": str(xml_path),
        "line_density": candidate["line_density"],
        "h_lines": candidate["h_lines"],
        "v_lines": candidate["v_lines"],
        "gt_count": len(gt_tables),
        "gt_shapes": [[t.rows, t.cols] for t in gt_tables],
        "gt_cell_count": sum(t.cell_count for t in gt_tables),
        "gt_span_cell_count": sum(t.span_cell_count for t in gt_tables),
        "layout": layout_eval,
        "raw": raw_eval,
        "post": post_eval,
        "timing": {
            "layout_sec": layout_sec,
            "detect_sec": detect_sec,
            "postprocess_sec": post_sec,
            "total_sec": layout_sec + detect_sec + post_sec,
        },
        "log_tail": logger.lines[-40:],
    }


def run_ctdar(args: argparse.Namespace) -> int:
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
        print(f"[{idx}/{len(selected)}] {candidate['id']} {args.recognizer}", flush=True)
        try:
            results.append(run_ctdar_case(candidate, args))
            ctdar_bench.write_outputs(results, args.out_dir)
        except Exception as exc:
            print(f"  ERROR {candidate['id']}: {exc!r}", flush=True)
    ctdar_bench.write_outputs(results, args.out_dir)
    metadata = {
        "model": args.recognizer,
        "model_path": str(args.model_path) if args.model_path else "",
        "dict_path": str(args.dict_path) if args.dict_path else "",
        "dpi": args.dpi,
        "pad_pt": args.pad_pt,
        "wired_use_ocr_results": args.wired_use_ocr_results,
        "cases": len(results),
    }
    (args.out_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out_dir": str(args.out_dir), "cases": len(results)}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["groundtruth", "ctdar"], default="groundtruth")
    parser.add_argument(
        "--recognizer",
        choices=["rapidtable_slanet", "slanext_wired", "wired_table_rec_v2"],
        required=True,
    )
    parser.add_argument("--model-path", type=Path, default=None)
    parser.add_argument("--dict-path", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--pad-pt", type=float, default=6.0)
    parser.add_argument(
        "--rapidtable-use-ocr-results",
        action="store_true",
        help="Pass existing OCR boxes/texts into RapidTable SLANet+; no extra OCR is run.",
    )
    parser.add_argument(
        "--wired-use-ocr-results",
        action="store_true",
        help="Pass existing OCR boxes/texts into wired_table_rec_v2; no extra OCR is run.",
    )

    parser.add_argument("--ocr-dir", type=Path, default=ROOT / "temp" / "groundtruth5_pipeline")
    parser.add_argument("--groundtruth-dir", type=Path, default=Path(r"C:\Users\nhquan\Downloads\groundtruth"))
    parser.add_argument("--gt03-docx", type=Path, default=ROOT / "temp" / "groundtruth4_scan_word" / "groundtruth03_converted.docx")
    parser.add_argument("--scans", nargs="+", default=["01", "02", "03", "04", "05"])

    parser.add_argument("--ctdar-root", type=Path, default=ROOT / "temp" / "external" / "ICDAR2019_cTDaR")
    parser.add_argument("--track", default="TRACKB2", choices=["TRACKB1", "TRACKB2"])
    parser.add_argument("--max-cases", type=int, default=20)
    parser.add_argument("--selection", choices=["lowest", "highest"], default="highest")
    parser.add_argument("--modern-only", action="store_true", default=True)
    parser.add_argument("--include-historical", action="store_false", dest="modern_only")
    parser.add_argument("--pdf-dpi", type=int, default=300)
    parser.add_argument("--doclayout-conf", type=float, default=0.25)
    args = parser.parse_args()

    if args.recognizer == "slanext_wired" and not args.model_path:
        args.model_path = ROOT / "temp" / "external" / "paddle_to_onnx_models" / "SLANeXt_wired.onnx"

    if args.suite == "groundtruth":
        return run_groundtruth(args)
    return run_ctdar(args)


if __name__ == "__main__":
    raise SystemExit(main())
