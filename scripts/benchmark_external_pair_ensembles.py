from __future__ import annotations

import argparse
import copy
import csv
import html
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

from rapidfuzz.distance import Levenshtein

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import benchmark_current_table_pipeline as gt_bench  # noqa: E402
from benchmark_external_table_gt_samples import (  # noqa: E402
    image_to_pdf,
    load_samples,
    shape_score,
)
from benchmark_pair_ensembles import (  # noqa: E402
    ENGINE_SPECS,
    PAIR_DEFS,
    combine_pair,
    run_engine,
)
from scanindex.core.tables.eval_metrics import compare_cell_grids


class QuietLogger(gt_bench.QuietLogger):
    pass


def full_page_layout_from_page_info(
    page_info: dict[int, dict[str, float]],
    dpi: int,
) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    scale = float(dpi) / 72.0
    for page, info in page_info.items():
        width = float(info.get("width") or 0.0)
        height = float(info.get("height") or 0.0)
        out[int(page)] = [
            {
                "type": "table",
                "bbox_pdf": [0.0, 0.0, width, height],
                "bbox": [0.0, 0.0, width * scale, height * scale],
                "confidence": 1.0,
            }
        ]
    return out


def ensure_ocr_pdf(raw_pdf: Path, ocr_pdf: Path, source_image: Path, force: bool) -> dict[str, Any]:
    json_path = Path(str(ocr_pdf) + ".json")
    if not force and ocr_pdf.exists() and json_path.exists():
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
    return {
        "ok": bool(ok),
        "cached": False,
        "ocr_sec": time.perf_counter() - t0,
        "message": msg or "",
        "log_tail": logs[-20:],
    }


def _area_for_table(table: Any) -> float:
    xs: list[float] = []
    ys: list[float] = []
    for row in getattr(table, "cell_bboxes", []) or []:
        for bx in row:
            if len(bx) >= 4 and bx[2] > bx[0] and bx[3] > bx[1]:
                xs.extend([float(bx[0]), float(bx[2])])
                ys.extend([float(bx[1]), float(bx[3])])
    if xs and ys:
        return (max(xs) - min(xs)) * (max(ys) - min(ys))
    x_left = float(getattr(table, "x_left", 0.0) or 0.0)
    x_right = float(getattr(table, "x_right", 0.0) or 0.0)
    y_top = float(getattr(table, "y_top", 0.0) or 0.0)
    y_bottom = float(getattr(table, "y_bottom", 0.0) or 0.0)
    return max(0.0, x_right - x_left) * max(0.0, y_bottom - y_top)


def best_table_result(tables: list[Any]) -> tuple[int, int, int, list[str], list[list[str]]]:
    candidates = [t for t in tables if not getattr(t, "skip_render", False)]
    if not candidates:
        return 0, 0, 0, [], []

    best = max(candidates, key=_area_for_table)
    rows = int(getattr(best, "row_count", 0) or 0)
    cols = int(getattr(best, "col_count", 0) or 0)
    raw_cells = getattr(best, "cells", []) or []
    cells = [
        [str(raw_cells[r][c]) if r < len(raw_cells) and c < len(raw_cells[r]) else "" for c in range(cols)]
        for r in range(rows)
    ]
    return (
        rows,
        cols,
        len(candidates),
        [str(getattr(t, "source", "")) for t in candidates],
        cells,
    )


def _repair_mojibake(text: str) -> str:
    if not any(token in text for token in ("Ã", "Â", "â", "Î")):
        return text
    try:
        repaired = text.encode("latin1").decode("utf-8")
    except Exception:
        return text

    def weird_score(value: str) -> int:
        return sum(value.count(token) for token in ("Ã", "Â", "â", "Î", "�"))

    return repaired if weird_score(repaired) < weird_score(text) else text


def normalize_cell_text(text: Any) -> str:
    value = html.unescape(str(text or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    value = _repair_mojibake(value)
    value = unicodedata.normalize("NFKC", value)
    replacements = {
        "\u00a0": " ",
        "\u2212": "-",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u00d7": "x",
        "\u2217": "*",
        "\u2022": "",
    }
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+([,.;:%)\]])", r"\1", value)
    value = re.sub(r"([(\[])\s+", r"\1", value)
    return value.strip()


def text_similarity(gt: str, pred: str) -> float:
    gt_norm = normalize_cell_text(gt)
    pred_norm = normalize_cell_text(pred)
    if not gt_norm and not pred_norm:
        return 1.0
    if not gt_norm:
        return 0.0 if pred_norm else 1.0
    return max(0.0, 1.0 - Levenshtein.distance(gt_norm, pred_norm) / len(gt_norm))


def load_gt_text_grid(gt_path: Path) -> list[list[str]]:
    data = json.loads(gt_path.read_text(encoding="utf-8"))
    grid = data.get("cell_text_grid")
    if isinstance(grid, list) and grid:
        return [[str(cell or "") for cell in row] for row in grid if isinstance(row, list)]

    category_ids = data.get("category_ids") or []
    boxes = data.get("boxes") or []
    row_boxes = [box for cat, box in zip(category_ids, boxes) if int(cat) == 3 and len(box) >= 4]
    col_boxes = [box for cat, box in zip(category_ids, boxes) if int(cat) == 2 and len(box) >= 4]
    words = data.get("ocr_words") or []
    word_boxes = data.get("ocr_boxes") or []
    if not row_boxes or not col_boxes or not words or not word_boxes:
        return []

    row_boxes = sorted(row_boxes, key=lambda bx: (float(bx[1]), float(bx[0])))
    col_boxes = sorted(col_boxes, key=lambda bx: (float(bx[0]), float(bx[1])))
    buckets: list[list[list[tuple[float, float, str]]]] = [
        [[] for _ in col_boxes] for _ in row_boxes
    ]

    for word, box in zip(words, word_boxes):
        if len(box) < 4:
            continue
        text = str(word or "").strip()
        if not text:
            continue
        x0, y0, x1, y1 = [float(v) for v in box[:4]]
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        row_idx = next(
            (idx for idx, bx in enumerate(row_boxes) if float(bx[1]) <= cy <= float(bx[3])),
            None,
        )
        col_idx = next(
            (idx for idx, bx in enumerate(col_boxes) if float(bx[0]) <= cx <= float(bx[2])),
            None,
        )
        if row_idx is None or col_idx is None:
            continue
        buckets[row_idx][col_idx].append((y0, x0, text))

    out: list[list[str]] = []
    for row in buckets:
        out_row: list[str] = []
        for cell_words in row:
            cell_words.sort(key=lambda item: (item[0], item[1]))
            out_row.append(" ".join(item[2] for item in cell_words))
        out.append(out_row)
    return out


def compare_text_grid(gt_grid: list[list[str]], pred_grid: list[list[str]]) -> dict[str, Any]:
    if not gt_grid:
        return {
            "has_text_gt": False,
            "cell_exact_rate": 0.0,
            "cell_text_acc": 0.0,
            "table_exact_text": False,
            "compared_cells": 0,
        }
    cmp = compare_cell_grids(gt_grid, pred_grid)
    return {
        "has_text_gt": True,
        "cell_exact_rate": cmp["cell_exact_rate"],
        "cell_text_acc": cmp["cell_text_acc"],
        "table_exact_text": cmp["table_exact_text"],
        "compared_cells": cmp["compared_cells"],
        "mismatches": cmp.get("mismatches", []),
    }


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
    feed_ocr_available = bool(pdf_lines)
    gt_grid = load_gt_text_grid(Path(sample["groundtruth"]))

    engine_results: dict[str, dict[str, Any]] = {}
    for spec_id, spec in ENGINE_SPECS.items():
        print(f"  engine {spec['label']}", flush=True)
        original_feed = bool(spec["feed_ocr"])
        if original_feed and not feed_ocr_available:
            ENGINE_SPECS[spec_id] = {**spec, "feed_ocr": False}
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
            if ENGINE_SPECS[spec_id]["feed_ocr"] != original_feed:
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

        pred_rows, pred_cols, pred_count, sources, pred_grid = best_table_result(out_tables)
        left_id, right_id = pair["engines"]
        left_time = engine_results[left_id]["timing"]["engine_total_sec"]
        right_time = engine_results[right_id]["timing"]["engine_total_sec"]
        detect_sec = (
            engine_results[left_id]["timing"]["detect_sec"]
            + engine_results[right_id]["timing"]["detect_sec"]
        )
        text_map_sec = (
            engine_results[left_id]["timing"]["text_map_sec"]
            + engine_results[right_id]["timing"]["text_map_sec"]
        )
        sequential_sec = left_time + right_time + select_sec + postprocess_sec
        parallel_sec = max(left_time, right_time) + select_sec + postprocess_sec
        score = shape_score(sample["gt_rows"], sample["gt_cols"], pred_rows, pred_cols)
        text_cmp = compare_text_grid(gt_grid, pred_grid)

        rows.append(
            {
                "dataset": sample["dataset"],
                "id": sample["id"],
                "variant_id": pair["id"],
                "label": pair["label"],
                "variant_order": int(pair["order"]),
                "gt_shape": f"{sample['gt_rows']}x{sample['gt_cols']}",
                "pred_shape": f"{pred_rows}x{pred_cols}",
                "shape_acc": score,
                "exact_shape": sample["gt_rows"] == pred_rows and sample["gt_cols"] == pred_cols,
                "has_text_gt": text_cmp["has_text_gt"],
                "cell_exact_rate": text_cmp["cell_exact_rate"],
                "cell_text_acc": text_cmp["cell_text_acc"],
                "table_exact_text": text_cmp["table_exact_text"],
                "compared_cells": text_cmp["compared_cells"],
                "pred_count": pred_count,
                "sources": sources,
                "ocr_line_count": len(pdf_lines),
                "feed_ocr_available": feed_ocr_available,
                "timing": {
                    "ocr_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0),
                    "preprocess_sec": preprocess_sec,
                    "detect_sec": detect_sec,
                    "text_map_sec": text_map_sec,
                    "select_sec": select_sec,
                    "postprocess_sec": postprocess_sec,
                    "table_total_sequential_sec": sequential_sec,
                    "table_total_parallel_est_sec": parallel_sec,
                    "end_to_end_sequential_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0)
                    + preprocess_sec
                    + sequential_sec,
                    "end_to_end_parallel_est_sec": float(ocr_status.get("ocr_sec", 0.0) or 0.0)
                    + preprocess_sec
                    + parallel_sec,
                },
                "engine_timing": {
                    left_id: engine_results[left_id]["timing"],
                    right_id: engine_results[right_id]["timing"],
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
        "table_total_parallel_est_sec",
        "end_to_end_parallel_est_sec",
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
                    "table_total_parallel_est_sec": row["timing"]["table_total_parallel_est_sec"],
                    "end_to_end_parallel_est_sec": row["timing"]["end_to_end_parallel_est_sec"],
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
            "select_sec": sum(float(r["timing"]["select_sec"]) for r in rows),
            "postprocess_sec": sum(float(r["timing"]["postprocess_sec"]) for r in rows),
            "table_total_parallel_est_sec": sum(
                float(r["timing"]["table_total_parallel_est_sec"]) for r in rows
            ),
            "end_to_end_parallel_est_sec": sum(
                float(r["timing"]["end_to_end_parallel_est_sec"]) for r in rows
            ),
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
        "select_sec",
        "postprocess_sec",
        "table_total_parallel_est_sec",
        "end_to_end_parallel_est_sec",
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
