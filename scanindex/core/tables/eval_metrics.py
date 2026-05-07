from __future__ import annotations

import html
import re
import unicodedata
from typing import Any

from rapidfuzz.distance import Levenshtein


def repair_mojibake(text: str) -> str:
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
    value = repair_mojibake(value)
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


def text_similarity(gt: Any, pred: Any) -> float:
    gt_norm = normalize_cell_text(gt)
    pred_norm = normalize_cell_text(pred)
    if not gt_norm and not pred_norm:
        return 1.0
    if not gt_norm:
        return 0.0 if pred_norm else 1.0
    return max(0.0, 1.0 - Levenshtein.distance(gt_norm, pred_norm) / len(gt_norm))


def compare_cell_grids(gt_grid: list[list[Any]], pred_grid: list[list[Any]]) -> dict[str, Any]:
    gt_rows = len(gt_grid)
    gt_cols = max((len(row) for row in gt_grid), default=0)
    pred_rows = len(pred_grid)
    pred_cols = max((len(row) for row in pred_grid), default=0)
    rows = max(gt_rows, pred_rows)
    cols = max(gt_cols, pred_cols)
    if rows <= 0 or cols <= 0:
        return {
            "cell_exact_rate": 1.0,
            "cell_text_acc": 1.0,
            "table_exact_text": True,
            "compared_cells": 0,
            "mismatches": [],
        }

    exact_count = 0
    sim_total = 0.0
    mismatches: list[dict[str, Any]] = []
    for r in range(rows):
        for c in range(cols):
            gt_text = gt_grid[r][c] if r < gt_rows and c < len(gt_grid[r]) else ""
            pred_text = pred_grid[r][c] if r < pred_rows and c < len(pred_grid[r]) else ""
            gt_norm = normalize_cell_text(gt_text)
            pred_norm = normalize_cell_text(pred_text)
            if gt_norm == pred_norm:
                exact_count += 1
            else:
                mismatches.append({"row": r, "col": c, "gt": gt_norm, "pred": pred_norm})
            sim_total += text_similarity(gt_text, pred_text)

    compared = rows * cols
    return {
        "cell_exact_rate": exact_count / compared,
        "cell_text_acc": sim_total / compared,
        "table_exact_text": bool(
            gt_rows == pred_rows and gt_cols == pred_cols and exact_count == compared
        ),
        "compared_cells": compared,
        "mismatches": mismatches[:20],
    }


def shape_score(gt: tuple[int, int], pred: tuple[int, int]) -> float:
    gr, gc = gt
    pr, pc = pred
    if gr <= 0 or gc <= 0 or pr <= 0 or pc <= 0:
        return 0.0
    return (min(gr, pr) / max(gr, pr)) * (min(gc, pc) / max(gc, pc))


def compare_table_grid_lists(
    gt_tables: list[list[list[Any]]],
    pred_tables: list[list[list[Any]]],
) -> dict[str, Any]:
    n = max(len(gt_tables), len(pred_tables))
    if n == 0:
        return {
            "shape_acc": 1.0,
            "cell_exact_rate": 1.0,
            "cell_text_acc": 1.0,
            "table_exact_text_rate": 1.0,
            "compared_cells": 0,
            "details": [],
        }

    details = []
    shape_scores = []
    exact_rates = []
    text_scores = []
    table_exact = []
    compared_cells = 0
    for idx in range(n):
        gt = gt_tables[idx] if idx < len(gt_tables) else []
        pred = pred_tables[idx] if idx < len(pred_tables) else []
        gt_shape = (len(gt), max((len(row) for row in gt), default=0))
        pred_shape = (len(pred), max((len(row) for row in pred), default=0))
        ss = shape_score(gt_shape, pred_shape)
        cmp = compare_cell_grids(gt, pred)
        shape_scores.append(ss)
        exact_rates.append(float(cmp["cell_exact_rate"]))
        text_scores.append(float(cmp["cell_text_acc"]))
        table_exact.append(1.0 if cmp["table_exact_text"] else 0.0)
        compared_cells += int(cmp["compared_cells"])
        details.append(
            {
                "index": idx,
                "gt_shape": list(gt_shape),
                "out_shape": list(pred_shape),
                "shape_score": ss,
                "cell_exact_rate": cmp["cell_exact_rate"],
                "cell_text_acc": cmp["cell_text_acc"],
                "table_exact_text": cmp["table_exact_text"],
            }
        )

    return {
        "count": len(pred_tables),
        "gt_count": len(gt_tables),
        "shape_acc": sum(shape_scores) / n,
        "cell_exact_rate": sum(exact_rates) / n,
        "cell_text_acc": sum(text_scores) / n,
        "table_exact_text_rate": sum(table_exact) / n,
        "compared_cells": compared_cells,
        "details": details,
    }
