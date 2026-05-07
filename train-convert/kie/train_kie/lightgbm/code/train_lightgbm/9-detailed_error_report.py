from __future__ import annotations

import argparse
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


def _ascii(text: object) -> str:
    raw = "" if text is None else str(text)
    raw = raw.replace("Đ", "D").replace("đ", "d")
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


def _best_oracle(candidates: list[CandidatePrediction], gt: dict) -> tuple[float, str]:
    best_f1 = 0.0
    best_text = ""
    for cand in candidates:
        _, _, f1 = _word_f1(cand.word_ids, gt.get("word_ids") or [])
        if f1 > best_f1:
            best_f1 = f1
            best_text = cand.text
    return best_f1, best_text


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


def _compare_doc(gt: dict, decoded: dict[str, list[CandidatePrediction]], scored_fields: dict[str, list[CandidatePrediction]]) -> dict:
    gt_by_field: dict[str, list[dict]] = defaultdict(list)
    pred_by_field: dict[str, list[dict]] = defaultdict(list)
    for inst in gt.get("field_instances", []):
        if inst.get("label") in LABELS:
            gt_by_field[inst["label"]].append(inst)
    for field, preds in decoded.items():
        for pred in preds:
            pred_by_field[field].append(
                {
                    "label": field,
                    "page_index": pred.page_index,
                    "line_ids": pred.line_ids,
                    "word_ids": pred.word_ids,
                    "bbox": pred.bbox,
                    "text": pred.text,
                    "confidence": pred.score,
                    "candidate_id": pred.candidate_id,
                    "source_kind": _source_kind(pred.candidate_id),
                }
            )

    tp_words = pred_words = gt_words = exact = matched = 0
    gt_inst = pred_inst = 0
    errors = []
    for field in LABELS:
        gts = list(gt_by_field.get(field, []))
        preds = list(pred_by_field.get(field, []))
        gt_inst += len(gts)
        pred_inst += len(preds)
        gt_words += sum(len(item.get("word_ids") or []) for item in gts)
        pred_words += sum(len(item.get("word_ids") or []) for item in preds)
        used_gt: set[int] = set()
        for pred in preds:
            pred_set = set(pred.get("word_ids") or [])
            best_i = None
            best_overlap = 0
            for idx, gt_item in enumerate(gts):
                if idx in used_gt:
                    continue
                overlap = len(pred_set & set(gt_item.get("word_ids") or []))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_i = idx
            if best_i is None:
                errors.append(
                    {
                        "kind": "EXTRA",
                        "field": field,
                        "page_index": None,
                        "pred_page_index": pred.get("page_index"),
                        "f1": 0.0,
                        "oracle_f1": 0.0,
                        "gt_text": "",
                        "pred_text": pred.get("text") or "",
                        "source_kind": pred.get("source_kind"),
                    }
                )
                continue
            gt_item = gts[best_i]
            used_gt.add(best_i)
            gt_set = set(gt_item.get("word_ids") or [])
            precision, recall, f1 = _word_f1(pred.get("word_ids") or [], gt_item.get("word_ids") or [])
            tp_words += best_overlap
            matched += 1
            if pred_set == gt_set:
                exact += 1
            else:
                oracle_f1, oracle_text = _best_oracle(scored_fields.get(field, []), gt_item)
                errors.append(
                    {
                        "kind": "BOUNDARY",
                        "field": field,
                        "page_index": gt_item.get("page_index"),
                        "pred_page_index": pred.get("page_index"),
                        "precision": precision,
                        "recall": recall,
                        "f1": f1,
                        "oracle_f1": oracle_f1,
                        "oracle_text": oracle_text,
                        "gt_text": gt_item.get("text") or "",
                        "pred_text": pred.get("text") or "",
                        "source_kind": pred.get("source_kind"),
                    }
                )
        for idx, gt_item in enumerate(gts):
            if idx in used_gt:
                continue
            oracle_f1, oracle_text = _best_oracle(scored_fields.get(field, []), gt_item)
            errors.append(
                {
                    "kind": "MISSING",
                    "field": field,
                    "page_index": gt_item.get("page_index"),
                    "pred_page_index": None,
                    "f1": 0.0,
                    "oracle_f1": oracle_f1,
                    "oracle_text": oracle_text,
                    "gt_text": gt_item.get("text") or "",
                    "pred_text": "",
                    "source_kind": "",
                }
            )
    precision = tp_words / pred_words if pred_words else 0.0
    recall = tp_words / gt_words if gt_words else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision and recall else 0.0
    return {
        "doc_id": gt["doc_id"],
        "file": gt["relative_pdf_path"],
        "split": gt["split"],
        "metrics": {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "exact": exact / gt_inst if gt_inst else 0.0,
            "tp_words": tp_words,
            "pred_words": pred_words,
            "gt_words": gt_words,
            "exact_count": exact,
            "gt_inst": gt_inst,
            "pred_inst": pred_inst,
            "matched_inst": matched,
        },
        "error_count": len(errors),
        "errors": errors,
    }


def _summary(documents: list[dict]) -> dict:
    out = {}
    for split in ["all", "train", "val", "test"]:
        docs = documents if split == "all" else [doc for doc in documents if doc["split"] == split]
        tp_words = pred_words = gt_words = gt_inst = pred_inst = exact = errors = 0
        for doc in docs:
            metrics = doc["metrics"]
            tp_words += metrics["tp_words"]
            pred_words += metrics["pred_words"]
            gt_words += metrics["gt_words"]
            gt_inst += metrics["gt_inst"]
            pred_inst += metrics["pred_inst"]
            exact += metrics["exact_count"]
            errors += doc["error_count"]
        precision = tp_words / pred_words if pred_words else 0.0
        recall = tp_words / gt_words if gt_words else 0.0
        f1 = 2.0 * precision * recall / (precision + recall) if precision and recall else 0.0
        out[split] = {
            "docs": len(docs),
            "f1": f1,
            "precision": precision,
            "recall": recall,
            "exact_acc": exact / gt_inst if gt_inst else 0.0,
            "gt_inst": gt_inst,
            "pred_inst": pred_inst,
            "errors": errors,
        }
    return out


def _write_markdown(path: Path, report: dict, max_docs: int) -> None:
    documents = report["documents"]
    field_errors = Counter()
    kind_errors = Counter()
    bucket_errors = Counter()
    for doc in documents:
        for error in doc["errors"]:
            field_errors[error["field"]] += 1
            kind_errors[error["kind"]] += 1
            bucket_errors[_bucket(error)] += 1
    lines = ["# LightGBM detailed error report", ""]
    lines.append("Ghi chu: tat ca text trong report nay da bo dau de de doc/log.")
    lines.append("")
    lines.append("## Summary")
    for split, metrics in report["summary"].items():
        lines.append(
            f"- {split}: docs={metrics['docs']} f1={metrics['f1']:.6f} "
            f"precision={metrics['precision']:.6f} recall={metrics['recall']:.6f} "
            f"exact={metrics['exact_acc']:.6f} errors={metrics['errors']}"
        )
    lines.append("")
    lines.append("## Error counts")
    lines.append("- By field: " + ", ".join(f"{key}={value}" for key, value in field_errors.most_common()))
    lines.append("- By kind: " + ", ".join(f"{key}={value}" for key, value in kind_errors.most_common()))
    lines.append("- By fixability: " + ", ".join(f"{key}={value}" for key, value in bucket_errors.most_common()))
    lines.append("")
    lines.append("## Worst files")
    for doc in sorted(documents, key=lambda item: (-item["error_count"], item["metrics"]["f1"], item["file"]))[:max_docs]:
        lines.append(
            f"### {doc['split']} {doc['file']} | f1={doc['metrics']['f1']:.6f} "
            f"exact={doc['metrics']['exact']:.3f} errors={doc['error_count']}"
        )
        for error in doc["errors"][:10]:
            lines.append(
                f"- {error['kind']} {error['field']} | f1={float(error.get('f1') or 0):.3f} "
                f"oracle={float(error.get('oracle_f1') or 0):.3f} bucket={_bucket(error)} source={error.get('source_kind','')}"
            )
            lines.append(f"  GT: {_ascii((error.get('gt_text') or '').replace(chr(10), ' / ')[:260])}")
            lines.append(f"  PRED: {_ascii((error.get('pred_text') or '').replace(chr(10), ' / ')[:260])}")
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write detailed decoded error report for LightGBM KIE.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--splits", nargs="+", default=["train", "val", "test"], choices=["train", "val", "test"])
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    parser.add_argument("--max-docs", type=int, default=250)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = build_paths(args.project_root)
    models_dir = paths.models_root / "fieldwise"
    thresholds = read_json(models_dir / "thresholds.json", default={})
    all_documents = []
    for split in args.splits:
        gt_rows, scored_by_doc = _load_scored_candidates(args.project_root, models_dir, split)
        decoded_docs = {
            gt["doc_id"]: decode_document_predictions(scored_by_doc.get(gt["doc_id"], {}), thresholds)
            for gt in gt_rows
        }
        for gt in gt_rows:
            all_documents.append(_compare_doc(gt, decoded_docs.get(gt["doc_id"], {}), scored_by_doc.get(gt["doc_id"], {})))
    report = {
        "project_root": str(Path(args.project_root).resolve()),
        "splits": args.splits,
        "summary": _summary(all_documents),
        "documents": all_documents,
    }
    output_json = Path(args.output_json) if args.output_json else paths.reports_root / "detailed_error_report_current.json"
    output_md = Path(args.output_md) if args.output_md else paths.reports_root / "detailed_error_report_current.md"
    write_json(output_json, report)
    _write_markdown(output_md, report, args.max_docs)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Wrote {output_json}")
    print(f"Wrote {output_md}")


if __name__ == "__main__":
    main()
