from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import build_paths, read_json, write_json


def _ascii(text: object) -> str:
    raw = "" if text is None else str(text)
    raw = raw.replace("Đ", "D").replace("đ", "d")
    decomposed = unicodedata.normalize("NFKD", raw)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _norm(text: object) -> str:
    return re.sub(r"\s+", " ", _ascii(text).lower()).strip()


def _tokens(text: object) -> list[str]:
    return [tok.strip(".,;:()[]{}<>-*") for tok in _norm(text).replace("/", " ").split() if tok.strip(".,;:()[]{}<>-*")]


def _bucket(error: dict) -> str:
    oracle = float(error.get("oracle_f1") or 0.0)
    if error["kind"] == "EXTRA":
        return "EXTRA_NO_GT"
    if oracle >= 0.999:
        return "ORACLE_EXACT_EXISTS"
    if oracle >= 0.85:
        return "ORACLE_GOOD_CANDIDATE"
    if oracle > 0:
        return "CANDIDATE_PARTIAL_OR_OCR"
    return "NO_CANDIDATE_OR_LABEL"


def _contains_signer_context(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:t/?m|k/?t|t/?l|tuq|q\.|bi thu|chu tich|pho chu tich|chanh van phong|pho chanh|truong ban|pho truong|giam doc|cuc truong)\b",
            text,
            re.I,
        )
    )


def _contains_org_context(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:uy ban|dang uy|thanh uy|tinh uy|huyen uy|thi uy|bo khoa hoc|so khoa hoc|ban chi dao|van phong|hoi dong|mat tran)\b",
            text,
            re.I,
        )
    )


def _is_clean_name_subset(gt: str, pred: str) -> bool:
    gt_tokens = _tokens(gt)
    pred_tokens = _tokens(pred)
    if not gt_tokens or not pred_tokens or len(gt_tokens) <= len(pred_tokens):
        return False
    gt_join = " ".join(gt_tokens)
    pred_join = " ".join(pred_tokens)
    return pred_join in gt_join


def _classify(error: dict) -> dict:
    field = error["field"]
    kind = error["kind"]
    oracle = float(error.get("oracle_f1") or 0.0)
    f1 = float(error.get("f1") or 0.0)
    gt = _norm(error.get("gt_text") or "")
    pred = _norm(error.get("pred_text") or "")
    bucket = _bucket(error)

    cause = "unknown"
    simple_fix = "unknown"
    confidence = "medium"
    action = "review manually"
    topk_assessment = "not_topk_limited_in_eval"

    if kind == "EXTRA":
        if field in {"ISSUE_ORG_SUPERIOR", "REGIME_HEADER"} and _contains_org_context(pred):
            cause = "gt_policy_or_false_positive_top_header"
            simple_fix = "maybe"
            action = "review label policy; if GT intentionally missing, add stricter top-header cardinality/negative blocker"
        elif field in {"SIGNER_ROLE", "SIGNER_NAME"}:
            cause = "false_positive_noisy_signature_or_footer"
            simple_fix = "yes"
            action = "tighten signer pairing/page gating; require valid paired name/role geometry"
        elif field in {"ADDRESSEE", "RECIPIENTS"}:
            cause = "false_positive_anchor_block"
            simple_fix = "yes"
            action = "raise threshold or require anchor and plausible page band"
        else:
            cause = "threshold_false_positive"
            simple_fix = "yes"
            action = "raise field threshold or add schema blocker"
        return {
            "cause": cause,
            "fixability": "decoder_or_gt_policy",
            "simple_fix": simple_fix,
            "action": action,
            "confidence": confidence,
            "oracle_bucket": bucket,
            "topk_assessment": topk_assessment,
        }

    if oracle >= 0.999:
        fixability = "simple_decoder_or_ranking"
    elif oracle >= 0.85:
        fixability = "decoder_or_candidate_boundary"
    elif oracle > 0:
        fixability = "candidate_generator_or_ocr"
    else:
        fixability = "no_candidate_or_gt_mismatch"

    if kind == "MISSING":
        if oracle >= 0.999:
            cause = "exact_candidate_exists_but_threshold_or_schema_suppressed"
            simple_fix = "yes"
            action = "lower threshold for this context or add repair that restores exact candidate"
        elif oracle >= 0.85:
            cause = "good_candidate_exists_but_threshold_or_schema_suppressed"
            simple_fix = "maybe"
            action = "lower threshold or add field-specific repair"
        elif oracle > 0:
            cause = "only_partial_candidate_available"
            simple_fix = "no"
            action = "improve candidate generation/OCR grouping"
        else:
            cause = "no_candidate_or_label_mismatch"
            simple_fix = "no"
            action = "review GT/canonical OCR; add candidate source if GT is valid"
        return {
            "cause": cause,
            "fixability": fixability,
            "simple_fix": simple_fix,
            "action": action,
            "confidence": confidence,
            "oracle_bucket": bucket,
            "topk_assessment": topk_assessment,
        }

    # Boundary errors.
    if field == "DOC_SUBJECT":
        if "-----" in pred or re.search(r"\bcan cu\b", pred):
            cause = "subject_swallowed_separator_or_body"
            action = "prefer candidate before separator/body; trim trailing separator/body"
            simple_fix = "yes"
        elif re.search(r"\bso\s*[:.]?\s*\d", pred) and not re.search(r"\bso\s*[:.]?\s*\d", gt):
            cause = "subject_swallowed_doc_number"
            action = "penalize subject candidates overlapping DOC_NUMBER_SYMBOL"
            simple_fix = "yes"
        elif oracle >= 0.999:
            cause = "subject_exact_candidate_exists_but_ranker_chose_wrong_boundary"
            action = "add subject boundary rerank: prefer shorter title candidate with subject keyword"
            simple_fix = "yes"
        else:
            cause = "subject_candidate_boundary_or_ocr"
            action = "improve subject candidate spans"
            simple_fix = "maybe" if oracle >= 0.85 else "no"
    elif field == "ADDRESSEE":
        if "kinh gui" in pred and not pred.startswith("kinh gui"):
            cause = "addressee_has_prefix_noise_before_anchor"
            action = "trim candidate to start at Kinh gui anchor"
            simple_fix = "yes"
        elif "kinh gui" in gt and "kinh gui" not in pred:
            cause = "addressee_missing_anchor_line"
            action = "prefer anchor_block candidate containing Kinh gui"
            simple_fix = "yes" if oracle >= 0.85 else "maybe"
        elif oracle >= 0.999:
            cause = "addressee_exact_candidate_exists_but_wrong_span_selected"
            action = "rerank by anchor start and same-column continuation"
            simple_fix = "yes"
        else:
            cause = "addressee_boundary_or_ocr"
            action = "review candidate generation"
            simple_fix = "maybe" if oracle >= 0.85 else "no"
    elif field == "RECIPIENTS":
        if _contains_signer_context(pred):
            cause = "recipients_swallowed_signature_block"
            action = "skip right-column signer lines after Noi nhan anchor"
            simple_fix = "yes"
        elif f1 >= 0.90:
            cause = "recipients_tail_boundary_small"
            action = "tune continuation/stop rule; prefer exact anchor block if exists"
            simple_fix = "yes" if oracle >= 0.999 else "maybe"
        elif oracle >= 0.999:
            cause = "recipients_exact_candidate_exists_but_ranker_chose_wrong_span"
            action = "rerank anchor_block/same-column by no signer-context and full tail"
            simple_fix = "yes"
        else:
            cause = "recipients_candidate_boundary_or_ocr"
            action = "improve anchor block generation"
            simple_fix = "maybe" if oracle >= 0.85 else "no"
    elif field == "DOC_NUMBER_SYMBOL":
        if re.search(r"\bso\s*[:.]?\s*(?:[-/]\s*)+\w", pred) or (re.search(r"\bso\b", pred) and not re.search(r"\bso\s*[:.]?\s*\d", pred)):
            cause = "doc_number_digit_missed_or_word_order_bad"
            action = "prefer y-band/window candidate with numeric token immediately after So"
            simple_fix = "yes" if oracle >= 0.85 else "maybe"
        elif pred.startswith("*") or pred.startswith("-"):
            cause = "doc_number_has_decorative_prefix"
            action = "trim decorative prefix before So"
            simple_fix = "yes"
        else:
            cause = "doc_number_boundary"
            action = "rerank document-number candidates"
            simple_fix = "yes" if oracle >= 0.999 else "maybe"
    elif field == "PLACE_DATE":
        if re.search(r"\bngay\s+thang\b|\bngay\s+nam\b|\bthang\s+nam\b", pred):
            cause = "place_date_word_order_bad_or_number_late"
            action = "prefer candidate matching ngay <num> thang <num> nam <num>; soft only"
            simple_fix = "yes" if oracle >= 0.85 else "maybe"
        else:
            cause = "place_date_boundary"
            action = "rerank complete date candidate"
            simple_fix = "yes" if oracle >= 0.999 else "maybe"
    elif field == "SIGNER_NAME":
        if _is_clean_name_subset(gt, pred):
            cause = "signer_name_missing_first_or_last_token"
            action = "prefer longer clean name on same line/under paired role"
            simple_fix = "yes" if oracle >= 0.85 else "maybe"
        elif _contains_org_context(pred):
            cause = "signer_name_has_org_noise_prefix"
            action = "trim org/noise prefix or require clean-name window"
            simple_fix = "yes" if oracle >= 0.85 else "maybe"
        else:
            cause = "signer_name_ocr_or_pairing"
            action = "improve name-role pairing; avoid strict OCR rules"
            simple_fix = "maybe" if oracle >= 0.85 else "no"
    elif field == "SIGNER_ROLE":
        if oracle >= 0.999:
            cause = "signer_role_exact_candidate_exists_but_wrong_span_selected"
            action = "rerank role candidates by paired name geometry and role-prefix/context"
            simple_fix = "yes"
        elif oracle >= 0.85:
            cause = "signer_role_good_candidate_exists_but_boundary_noisy"
            action = "soft trim noisy neighbor lines; keep OCR-tolerant"
            simple_fix = "maybe"
        else:
            cause = "signer_role_ocr_or_candidate_partial"
            action = "needs OCR/candidate improvement; do not add strict rule"
            simple_fix = "no"
    elif field in {"ISSUE_ORG_NAME", "ISSUE_ORG_SUPERIOR"}:
        if f1 >= 0.70 and oracle >= 0.999:
            cause = "org_hierarchy_split_or_missing_line"
            action = "jointly decode superior/name; merge/repair same top-left column"
            simple_fix = "yes"
        elif _contains_org_context(pred) and oracle >= 0.85:
            cause = "org_candidate_boundary"
            action = "rerank top-left hierarchy candidates"
            simple_fix = "maybe"
        else:
            cause = "org_policy_or_candidate_boundary"
            action = "review GT policy or candidate generation"
            simple_fix = "maybe" if oracle >= 0.85 else "no"
    elif field == "REGIME_HEADER":
        if oracle >= 0.999:
            cause = "regime_exact_candidate_exists_but_wrong_header_split"
            action = "prefer right/top cluster containing regime keywords only"
            simple_fix = "yes"
        else:
            cause = "regime_ocr_or_policy"
            action = "review top header split/GT"
            simple_fix = "maybe" if oracle >= 0.85 else "no"

    return {
        "cause": cause,
        "fixability": fixability,
        "simple_fix": simple_fix,
        "action": action,
        "confidence": confidence,
        "oracle_bucket": bucket,
        "topk_assessment": topk_assessment,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify root causes for detailed LightGBM KIE errors.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--input-json", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = build_paths(args.project_root)
    input_json = Path(args.input_json) if args.input_json else paths.reports_root / "detailed_error_report_current.json"
    report = read_json(input_json)

    rows = []
    for doc in report["documents"]:
        for index, error in enumerate(doc["errors"], start=1):
            cls = _classify(error)
            rows.append(
                {
                    "split": doc["split"],
                    "file": doc["file"],
                    "doc_f1": doc["metrics"]["f1"],
                    "doc_exact": doc["metrics"]["exact"],
                    "doc_error_count": doc["error_count"],
                    "error_index": index,
                    "field": error["field"],
                    "kind": error["kind"],
                    "f1": float(error.get("f1") or 0.0),
                    "oracle_f1": float(error.get("oracle_f1") or 0.0),
                    "source_kind": error.get("source_kind") or "",
                    "cause": cls["cause"],
                    "fixability": cls["fixability"],
                    "simple_fix": cls["simple_fix"],
                    "action": cls["action"],
                    "oracle_bucket": cls["oracle_bucket"],
                    "topk_assessment": cls["topk_assessment"],
                    "gt_text_ascii": _ascii((error.get("gt_text") or "").replace("\n", " / ")),
                    "pred_text_ascii": _ascii((error.get("pred_text") or "").replace("\n", " / ")),
                }
            )

    cause_counts = Counter(row["cause"] for row in rows)
    fix_counts = Counter(row["fixability"] for row in rows)
    simple_counts = Counter(row["simple_fix"] for row in rows)
    field_cause: dict[str, Counter] = defaultdict(Counter)
    field_fix: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        field_cause[row["field"]][row["cause"]] += 1
        field_fix[row["field"]][row["fixability"]] += 1

    json_out = paths.reports_root / "error_root_cause_current.json"
    csv_out = paths.reports_root / "error_root_cause_current.csv"
    md_out = paths.reports_root / "error_root_cause_current.md"
    write_json(
        json_out,
        {
            "source_report": str(input_json),
            "total_errors": len(rows),
            "cause_counts": dict(cause_counts.most_common()),
            "fixability_counts": dict(fix_counts.most_common()),
            "simple_fix_counts": dict(simple_counts.most_common()),
            "field_fixability": {field: dict(counter.most_common()) for field, counter in sorted(field_fix.items())},
            "field_causes": {field: dict(counter.most_common()) for field, counter in sorted(field_cause.items())},
            "rows": rows,
        },
    )
    with csv_out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)

    lines = ["# Error root cause current", "", "Text GT/PRED trong report nay da bo dau.", ""]
    lines.append("## Top-k assessment")
    lines.append("- Detailed eval dung toan bo exported candidates, khong prune top-k khi inference/eval.")
    lines.append("- Top-K pruning chi dung trong threshold tuning de tang toc. Neu loi con candidate dung nhung khong duoc chon, nguyen nhan chinh la ranking/threshold/decoder, khong phai candidate bi cat khoi eval.")
    lines.append("- Neu oracle_f1 thap hoac bang 0, loi nam o candidate generation/OCR/GT, khong phai top-k.")
    lines.append("")
    lines.append("## Fixability counts")
    for key, value in fix_counts.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("## Simple fix counts")
    for key, value in simple_counts.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("## Cause counts")
    for key, value in cause_counts.most_common(40):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("## Field x fixability")
    for field, counter in sorted(field_fix.items(), key=lambda item: -sum(item[1].values())):
        lines.append(f"- {field}: " + ", ".join(f"{key}={value}" for key, value in counter.most_common()))
    lines.append("")
    lines.append("## Recommended decoder/postprocess fixes")
    recommendations = [
        ("SIGNER_ROLE", "Soft rerank by signer-name pairing; avoid strict OCR text rules; trim noisy neighbor lines only when paired geometry remains valid."),
        ("DOC_SUBJECT", "Prefer shorter title candidate before separator/body; penalize candidates swallowing doc number, separator, or legal-body lines."),
        ("SIGNER_NAME", "Prefer longer clean name on same line below paired role; repair missing first token like 'Minh Thong' -> 'Dang Minh Thong' only when candidate exists."),
        ("ISSUE_ORG_NAME/SUPERIOR", "Joint top-left hierarchy decoder; merge same-column org lines and avoid splitting superior/name inconsistently."),
        ("RECIPIENTS", "Anchor-block rerank: start at 'Noi nhan', skip right-column signer block, keep tail lines in same column."),
        ("DOC_NUMBER_SYMBOL", "Prefer y-band candidate where numeric token follows 'So'; trim decorative prefix."),
        ("PLACE_DATE", "Soft prefer complete date pattern 'ngay <num> thang <num> nam <num>'; do not hard reject OCR-bad dates."),
    ]
    for field, text in recommendations:
        lines.append(f"- {field}: {text}")
    lines.append("")
    lines.append("## Worst rows")
    for row in sorted(rows, key=lambda item: (-item["doc_error_count"], item["doc_f1"], item["file"]))[:250]:
        lines.append(
            f"### {row['split']} {row['file']} #{row['error_index']} | {row['kind']} {row['field']} | "
            f"f1={row['f1']:.3f} oracle={row['oracle_f1']:.3f}"
        )
        lines.append(f"- cause: {row['cause']}")
        lines.append(f"- fixability: {row['fixability']}; simple_fix={row['simple_fix']}")
        lines.append(f"- action: {row['action']}")
        lines.append(f"- GT: {row['gt_text_ascii'][:260]}")
        lines.append(f"- PRED: {row['pred_text_ascii'][:260]}")
    md_out.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({
        "total_errors": len(rows),
        "fixability_counts": dict(fix_counts.most_common()),
        "simple_fix_counts": dict(simple_counts.most_common()),
        "outputs": {
            "json": str(json_out),
            "csv": str(csv_out),
            "md": str(md_out),
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
