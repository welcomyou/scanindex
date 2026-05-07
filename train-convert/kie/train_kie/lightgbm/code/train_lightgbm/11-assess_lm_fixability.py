from __future__ import annotations

import argparse
import csv
import json
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import build_paths, read_json, write_json
from train_lightgbm.config import LABELS
from train_lightgbm.schema_decoder import CandidatePrediction, decode_document_predictions
from train_lightgbm.training import _load_scored_candidates


SEMANTIC_HIGH_FIELDS = {"DOC_SUBJECT", "ADDRESSEE", "RECIPIENTS", "SIGNER_ROLE"}
SEMANTIC_MEDIUM_FIELDS = {"SIGNER_NAME", "ISSUE_ORG_NAME", "ISSUE_ORG_SUPERIOR"}
LOW_LM_FIELDS = {"REGIME_HEADER", "DOC_NUMBER_SYMBOL", "PLACE_DATE"}


def _ascii(text: object) -> str:
    raw = "" if text is None else str(text)
    raw = raw.replace("Ä", "D").replace("Ä‘", "d")
    decomposed = unicodedata.normalize("NFKD", raw)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _word_f1(pred_words: list[str] | tuple[str, ...], gt_words: list[str] | tuple[str, ...]) -> tuple[float, float, float]:
    pred_set = set(pred_words or [])
    gt_set = set(gt_words or [])
    if not pred_set and not gt_set:
        return 1.0, 1.0, 1.0
    overlap = len(pred_set & gt_set)
    precision = overlap / len(pred_set) if pred_set else 0.0
    recall = overlap / len(gt_set) if gt_set else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision and recall else 0.0
    return precision, recall, f1


def _source_kind(candidate_id: str) -> str:
    parts = candidate_id.split(":")
    return parts[2] if len(parts) >= 4 else "unknown"


def _candidate_stats(candidates: list[CandidatePrediction], gt: dict) -> dict:
    gt_words = set(gt.get("word_ids") or [])
    sorted_candidates = sorted(candidates, key=lambda item: item.score, reverse=True)
    best = None
    best_f1 = -1.0
    exact = None
    contains = None
    subset = None
    for rank, cand in enumerate(sorted_candidates, start=1):
        cand_words = set(cand.word_ids or [])
        precision, recall, f1 = _word_f1(cand.word_ids, gt.get("word_ids") or [])
        if f1 > best_f1:
            best_f1 = f1
            best = (rank, cand, precision, recall, f1)
        if exact is None and cand_words == gt_words:
            exact = (rank, cand, precision, recall, f1)
        if contains is None and gt_words and gt_words.issubset(cand_words) and cand_words != gt_words:
            contains = (rank, cand, precision, recall, f1)
        if subset is None and cand_words and cand_words.issubset(gt_words) and cand_words != gt_words:
            subset = (rank, cand, precision, recall, f1)
    def pack(item):
        if item is None:
            return None
        rank, cand, precision, recall, f1 = item
        return {
            "rank": rank,
            "score": float(cand.score),
            "f1": float(f1),
            "precision": float(precision),
            "recall": float(recall),
            "text": cand.text,
            "candidate_id": cand.candidate_id,
            "source_kind": _source_kind(cand.candidate_id),
            "word_count": len(cand.word_ids or []),
        }
    return {
        "candidate_count": len(sorted_candidates),
        "best": pack(best),
        "exact": pack(exact),
        "contains_gt": pack(contains),
        "subset_of_gt": pack(subset),
        "best_f1": float(max(best_f1, 0.0)),
        "exact_exists": exact is not None,
        "contains_gt_exists": contains is not None,
        "subset_of_gt_exists": subset is not None,
    }


def _lm_field_level(field: str) -> str:
    if field in SEMANTIC_HIGH_FIELDS:
        return "high"
    if field in SEMANTIC_MEDIUM_FIELDS:
        return "medium"
    if field in LOW_LM_FIELDS:
        return "low"
    return "medium"


def _classify_lm(error: dict) -> tuple[str, str, str]:
    field = error["field"]
    kind = error["kind"]
    stats = error.get("candidate_stats") or {}
    field_level = _lm_field_level(field)
    if kind == "EXTRA":
        if field_level == "low":
            return "LM_LOW_VALUE_EXTRA", "low", "No GT instance; regex/layout threshold should reject cheaper than LM."
        return "LM_CAN_REJECT_EXTRA", field_level, "No GT instance; LM may reject if candidate text is semantically not this field."
    if stats.get("exact_exists"):
        if field_level == "low":
            return "LM_CAN_CHOOSE_EXACT_BUT_RULE_BETTER", "low", "Exact candidate exists, but this field is mostly regex/layout."
        return "LM_CAN_CHOOSE_EXACT", field_level, "Exact GT candidate exists in candidate list; LM reranker can choose it."
    if stats.get("contains_gt_exists"):
        if field_level == "low":
            return "LM_CAN_TRIM_SUPERSET_BUT_RULE_BETTER", "low", "A candidate contains all GT plus extra; trimming is mostly deterministic."
        return "LM_CAN_TRIM_SUPERSET", field_level, "Candidate contains full GT plus extra words; LM can help only if decoder may trim/refine boundary."
    best_f1 = float(stats.get("best_f1") or 0.0)
    if best_f1 >= 0.85:
        if field_level == "low":
            return "LM_NEAR_CANDIDATE_LOW_VALUE", "low", "Good but not exact candidate exists; deterministic boundary logic is likely better."
        return "LM_MAY_IMPROVE_NEAR_CANDIDATE", field_level, "Good but not exact candidate exists; LM may improve choice but cannot guarantee exact span."
    if stats.get("subset_of_gt_exists") or best_f1 > 0:
        return "NOT_LM_ENOUGH_CONTEXT_PARTIAL_ONLY", "low", "Candidates miss some GT words; LM cannot recover absent words by reranking."
    return "NOT_LM_NO_USEFUL_CANDIDATE", "none", "Candidate list does not contain useful GT coverage; fix OCR/candidate generator/GT."


def _add_error(
    rows: list[dict],
    *,
    doc: dict,
    field: str,
    kind: str,
    gt: dict | None,
    pred: CandidatePrediction | None,
    candidates: list[CandidatePrediction],
    precision: float = 0.0,
    recall: float = 0.0,
    f1: float = 0.0,
) -> None:
    stats = _candidate_stats(candidates, gt) if gt is not None else {}
    error = {
        "doc_id": doc["doc_id"],
        "file": doc["relative_pdf_path"],
        "split": doc["split"],
        "field": field,
        "kind": kind,
        "page_index": gt.get("page_index") if gt else None,
        "pred_page_index": pred.page_index if pred else None,
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "gt_text": gt.get("text") if gt else "",
        "pred_text": pred.text if pred else "",
        "pred_score": float(pred.score) if pred else None,
        "pred_source_kind": _source_kind(pred.candidate_id) if pred else "",
        "candidate_stats": stats,
    }
    lm_class, lm_value, lm_reason = _classify_lm(error)
    error["lm_class"] = lm_class
    error["lm_value"] = lm_value
    error["lm_reason"] = lm_reason
    rows.append(error)


def _collect_errors_for_split(project_root: Path, models_dir: Path, thresholds: dict[str, float], split: str) -> list[dict]:
    gt_rows, scored_by_doc = _load_scored_candidates(project_root, models_dir, split)
    rows: list[dict] = []
    for doc in gt_rows:
        decoded = decode_document_predictions(scored_by_doc.get(doc["doc_id"], {}), thresholds)
        gt_by_field: dict[str, list[dict]] = defaultdict(list)
        for inst in doc.get("field_instances", []):
            if inst.get("label") in LABELS:
                gt_by_field[inst["label"]].append(inst)
        for field in LABELS:
            candidates = scored_by_doc.get(doc["doc_id"], {}).get(field, [])
            preds = list(decoded.get(field, []))
            gts = list(gt_by_field.get(field, []))
            used_gt: set[int] = set()
            for pred in preds:
                pred_set = set(pred.word_ids or [])
                best_i = None
                best_overlap = 0
                for idx, gt in enumerate(gts):
                    if idx in used_gt:
                        continue
                    overlap = len(pred_set & set(gt.get("word_ids") or []))
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_i = idx
                if best_i is None:
                    _add_error(rows, doc=doc, field=field, kind="EXTRA", gt=None, pred=pred, candidates=candidates)
                    continue
                gt = gts[best_i]
                used_gt.add(best_i)
                gt_set = set(gt.get("word_ids") or [])
                precision, recall, f1 = _word_f1(pred.word_ids, gt.get("word_ids") or [])
                if pred_set != gt_set:
                    _add_error(
                        rows,
                        doc=doc,
                        field=field,
                        kind="BOUNDARY",
                        gt=gt,
                        pred=pred,
                        candidates=candidates,
                        precision=precision,
                        recall=recall,
                        f1=f1,
                    )
            for idx, gt in enumerate(gts):
                if idx not in used_gt:
                    _add_error(rows, doc=doc, field=field, kind="MISSING", gt=gt, pred=None, candidates=candidates)
    return rows


def _summarize(rows: list[dict]) -> dict:
    summary = {
        "total_errors": len(rows),
        "by_lm_class": Counter(row["lm_class"] for row in rows),
        "by_lm_value": Counter(row["lm_value"] for row in rows),
        "by_field": Counter(row["field"] for row in rows),
        "by_kind": Counter(row["kind"] for row in rows),
        "exact_candidate_by_field": Counter(row["field"] for row in rows if row.get("candidate_stats", {}).get("exact_exists")),
        "contains_candidate_by_field": Counter(row["field"] for row in rows if row.get("candidate_stats", {}).get("contains_gt_exists")),
    }
    high_roi = [
        row
        for row in rows
        if row["lm_class"] in {"LM_CAN_CHOOSE_EXACT", "LM_CAN_TRIM_SUPERSET", "LM_MAY_IMPROVE_NEAR_CANDIDATE"}
        and row["lm_value"] in {"high", "medium"}
    ]
    exact_high_roi = [row for row in high_roi if row["lm_class"] == "LM_CAN_CHOOSE_EXACT"]
    summary["high_roi_lm_errors"] = len(high_roi)
    summary["exact_high_roi_lm_errors"] = len(exact_high_roi)
    summary["high_roi_by_field"] = Counter(row["field"] for row in high_roi)
    summary["exact_high_roi_by_field"] = Counter(row["field"] for row in exact_high_roi)
    return {key: dict(value) if isinstance(value, Counter) else value for key, value in summary.items()}


def _flatten_row(row: dict) -> dict:
    stats = row.get("candidate_stats") or {}
    best = stats.get("best") or {}
    exact = stats.get("exact") or {}
    contains = stats.get("contains_gt") or {}
    subset = stats.get("subset_of_gt") or {}
    return {
        "split": row["split"],
        "file": row["file"],
        "field": row["field"],
        "kind": row["kind"],
        "lm_class": row["lm_class"],
        "lm_value": row["lm_value"],
        "f1": row["f1"],
        "pred_score": row["pred_score"],
        "pred_source_kind": row["pred_source_kind"],
        "candidate_count": stats.get("candidate_count"),
        "best_f1": stats.get("best_f1"),
        "best_rank": best.get("rank"),
        "best_score": best.get("score"),
        "best_source_kind": best.get("source_kind"),
        "exact_rank": exact.get("rank"),
        "exact_score": exact.get("score"),
        "contains_rank": contains.get("rank"),
        "contains_score": contains.get("score"),
        "subset_rank": subset.get("rank"),
        "subset_score": subset.get("score"),
        "gt_text": _ascii(row.get("gt_text")),
        "pred_text": _ascii(row.get("pred_text")),
        "best_text": _ascii(best.get("text")),
        "exact_text": _ascii(exact.get("text")),
        "contains_text": _ascii(contains.get("text")),
        "lm_reason": _ascii(row.get("lm_reason")),
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flat_rows = [_flatten_row(row) for row in rows]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(flat_rows[0].keys()) if flat_rows else [])
        writer.writeheader()
        writer.writerows(flat_rows)


def _write_markdown(path: Path, rows: list[dict], summary: dict, max_examples: int) -> None:
    lines: list[str] = []
    lines.append("# LM fixability assessment")
    lines.append("")
    lines.append("Ghi chu: report nay danh gia kha nang LM reranker sua loi dua tren candidate coverage, khong phai da chay LM nho.")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- Total errors: {summary['total_errors']}")
    lines.append(f"- High/medium ROI LM candidates: {summary['high_roi_lm_errors']}")
    lines.append(f"- Exact high/medium ROI candidates: {summary['exact_high_roi_lm_errors']}")
    lines.append("- By LM class: " + ", ".join(f"{k}={v}" for k, v in Counter(summary["by_lm_class"]).most_common()))
    lines.append("- By LM value: " + ", ".join(f"{k}={v}" for k, v in Counter(summary["by_lm_value"]).most_common()))
    lines.append("- High ROI by field: " + ", ".join(f"{k}={v}" for k, v in Counter(summary["high_roi_by_field"]).most_common()))
    lines.append("- Exact high ROI by field: " + ", ".join(f"{k}={v}" for k, v in Counter(summary["exact_high_roi_by_field"]).most_common()))
    lines.append("")
    lines.append("## Field detail")
    field_counts = Counter(row["field"] for row in rows)
    for field, total in field_counts.most_common():
        field_rows = [row for row in rows if row["field"] == field]
        exact = sum(1 for row in field_rows if row.get("candidate_stats", {}).get("exact_exists"))
        contains = sum(1 for row in field_rows if row.get("candidate_stats", {}).get("contains_gt_exists"))
        high_roi = sum(
            1
            for row in field_rows
            if row["lm_class"] in {"LM_CAN_CHOOSE_EXACT", "LM_CAN_TRIM_SUPERSET", "LM_MAY_IMPROVE_NEAR_CANDIDATE"}
            and row["lm_value"] in {"high", "medium"}
        )
        lines.append(f"- {field}: total={total} exact_candidate={exact} contains_gt={contains} high_roi_lm={high_roi}")
    lines.append("")
    lines.append("## Representative LM-fixable examples")
    candidates = [
        row
        for row in rows
        if row["lm_class"] in {"LM_CAN_CHOOSE_EXACT", "LM_CAN_TRIM_SUPERSET", "LM_MAY_IMPROVE_NEAR_CANDIDATE", "LM_CAN_REJECT_EXTRA"}
        and row["lm_value"] in {"high", "medium"}
    ]
    for row in candidates[:max_examples]:
        flat = _flatten_row(row)
        lines.append(f"### {flat['split']} {flat['file']} | {flat['field']} {flat['kind']} | {flat['lm_class']}")
        lines.append(f"- GT: {flat['gt_text']}")
        lines.append(f"- Pred: {flat['pred_text']}")
        lines.append(f"- Best candidate f1={flat['best_f1']} rank={flat['best_rank']}: {flat['best_text']}")
        if flat.get("exact_text"):
            lines.append(f"- Exact candidate rank={flat['exact_rank']}: {flat['exact_text']}")
        if flat.get("contains_text"):
            lines.append(f"- Contains candidate rank={flat['contains_rank']}: {flat['contains_text']}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assess which LightGBM errors are realistically fixable by an LM reranker.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--splits", nargs="+", default=["train", "val", "test"], choices=["train", "val", "test"])
    parser.add_argument("--max-examples", type=int, default=80)
    parser.add_argument("--output-name", default="lm_fixability_report_current")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = build_paths(args.project_root)
    models_dir = paths.models_root / "fieldwise"
    thresholds = read_json(models_dir / "thresholds.json", default={})
    rows: list[dict] = []
    for split in args.splits:
        print(f"[train_lightgbm] assessing LM fixability split={split}", flush=True)
        rows.extend(_collect_errors_for_split(paths.root, models_dir, thresholds, split))
    summary = _summarize(rows)
    report = {
        "project_root": str(paths.root),
        "splits": args.splits,
        "summary": summary,
        "errors": rows,
    }
    json_path = paths.reports_root / f"{args.output_name}.json"
    csv_path = paths.reports_root / f"{args.output_name}.csv"
    md_path = paths.reports_root / f"{args.output_name}.md"
    write_json(json_path, report)
    _write_csv(csv_path, rows)
    _write_markdown(md_path, rows, summary, args.max_examples)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
