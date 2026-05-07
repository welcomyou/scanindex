from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, precision_score, recall_score

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from kie_core.labeling_workspace import analyze_page_selection


START_LABELS = {
    "REGIME_HEADER",
    "ISSUE_ORG_SUPERIOR",
    "ISSUE_ORG_NAME",
    "DOC_NUMBER_SYMBOL",
    "PLACE_DATE",
    "DOC_SUBJECT",
}
SIGNER_LABELS = {"SIGNER_ROLE", "SIGNER_NAME"}


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def label_instances(payload: dict[str, Any]) -> list[dict[str, Any]]:
    annotation = payload.get("annotation", payload)
    instances = annotation.get("field_instances") or []
    return instances if isinstance(instances, list) else []


def doc_id_from_path(path: Path, payload: dict[str, Any]) -> str:
    if payload.get("doc_id"):
        return str(payload["doc_id"])
    match = re.search(r"__([0-9a-fA-F]+)\.json$", path.name)
    return match.group(1) if match else path.stem


def canonical_for_label(path: Path, payload: dict[str, Any], ocr_root: Path) -> tuple[Path | None, dict[str, Any] | None]:
    if payload.get("pages"):
        return path, payload
    source = payload.get("source_canonical_json")
    if source and Path(source).exists():
        canonical_path = Path(source)
        return canonical_path, read_json(canonical_path)
    stem = path.name.split("__", 1)[0]
    digital_match = re.search(r"DIGITAL_(\d+)", path.name, re.IGNORECASE)
    candidates = [
        ocr_root / f"{stem}_ocr.pdf.json",
        ocr_root / f"{stem}_ocr.json",
        ocr_root / f"{stem}.json",
    ]
    if digital_match:
        idx = int(digital_match.group(1))
        candidates.extend(
            [
                ocr_root / f"DIGITAL ({idx})_ocr.pdf.json",
                ocr_root / f"DIGITAL ({idx})_ocr.json",
                ocr_root / f"DIGITAL ({idx}).json",
            ]
        )
    for candidate in candidates:
        if candidate.exists():
            return candidate, read_json(candidate)
    matches = sorted(ocr_root.rglob(f"{stem}_ocr*.json"))
    if matches:
        return matches[0], read_json(matches[0])
    return None, None


def gold_pages(instances: list[dict[str, Any]]) -> tuple[set[int], set[int]]:
    start_pages: set[int] = set()
    signer_pages: set[int] = set()
    for inst in instances:
        label = inst.get("label")
        try:
            page_index = int(inst.get("page_index"))
        except Exception:
            continue
        if label in START_LABELS:
            start_pages.add(page_index)
        if label in SIGNER_LABELS:
            signer_pages.add(page_index)
    return start_pages, signer_pages


def binary_metrics(y_true: list[int], y_pred: list[int]) -> dict[str, Any]:
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    return {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "tn": int(tn),
        "fp": int(fp),
        "fn": int(fn),
        "tp": int(tp),
    }


def benchmark_current_heuristic(label_root: Path, ocr_root: Path, split_by_doc: dict[str, str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path in sorted(p for p in label_root.rglob("*.json") if not p.name.startswith("_sample")):
        try:
            payload = read_json(path)
            instances = label_instances(payload)
            canonical_path, canonical = canonical_for_label(path, payload, ocr_root)
            if canonical_path is None or canonical is None:
                skipped.append({"file": str(path), "reason": "canonical OCR JSON not found"})
                continue
            pages = canonical.get("pages") or []
            if not pages or not instances:
                skipped.append({"file": str(path), "reason": "missing pages or field_instances"})
                continue
            doc_id = doc_id_from_path(path, payload)
            split = split_by_doc.get(doc_id, "unknown")
            start_pages, signer_pages = gold_pages(instances)
            if not start_pages:
                start_pages = {int(pages[0].get("page_index", 0))}
            first_signer = min(signer_pages) if signer_pages else None
            selection = analyze_page_selection(canonical)
            selected_pages = {int(p) for p in (selection.get("selected_pages") or [])}
            primary_page = selection.get("primary_page")
            primary_page = int(primary_page) if primary_page is not None else None
            signature_page = selection.get("signature_page")
            signature_page = int(signature_page) if signature_page is not None else None
            rows.append(
                {
                    "doc_id": doc_id,
                    "split": split,
                    "file": str(path),
                    "page_count": len(pages),
                    "gold_start_pages": sorted(start_pages),
                    "gold_first_signer_page": first_signer,
                    "heuristic_selected_pages": sorted(selected_pages),
                    "heuristic_primary_page": primary_page,
                    "heuristic_signature_page": signature_page,
                    "strategy": selection.get("strategy"),
                    "primary_exact": primary_page in start_pages if primary_page is not None else False,
                    "start_in_selected": bool(start_pages & selected_pages),
                    "signer_exact": first_signer is not None and signature_page == first_signer,
                    "signer_in_selected": first_signer is not None and first_signer in selected_pages,
                    "selected_page_count": len(selected_pages),
                }
            )
        except Exception as exc:
            skipped.append({"file": str(path), "reason": repr(exc)})

    def summarize(subrows: list[dict[str, Any]]) -> dict[str, Any]:
        signer_docs = [r for r in subrows if r["gold_first_signer_page"] is not None]
        return {
            "docs": len(subrows),
            "signer_docs": len(signer_docs),
            "primary_exact_accuracy": sum(1 for r in subrows if r["primary_exact"]) / len(subrows) if subrows else 0.0,
            "start_in_selected_accuracy": sum(1 for r in subrows if r["start_in_selected"]) / len(subrows) if subrows else 0.0,
            "signer_exact_accuracy": sum(1 for r in signer_docs if r["signer_exact"]) / len(signer_docs) if signer_docs else 0.0,
            "signer_in_selected_accuracy": sum(1 for r in signer_docs if r["signer_in_selected"]) / len(signer_docs) if signer_docs else 0.0,
            "avg_selected_pages": sum(r["selected_page_count"] for r in subrows) / len(subrows) if subrows else 0.0,
            "strategy_counts": {
                key: sum(1 for r in subrows if r["strategy"] == key)
                for key in sorted({r["strategy"] for r in subrows})
            },
        }

    by_split = {split: summarize([r for r in rows if r["split"] == split]) for split in ("train", "val", "test")}
    return {
        "overall": summarize(rows),
        "by_split": by_split,
        "skipped_count": len(skipped),
        "skipped_examples": skipped[:20],
        "mistakes": {
            "primary_not_exact": [r for r in rows if not r["primary_exact"]][:30],
            "signer_not_in_selected": [r for r in rows if r["gold_first_signer_page"] is not None and not r["signer_in_selected"]][:30],
            "signer_not_exact": [r for r in rows if r["gold_first_signer_page"] is not None and not r["signer_exact"]][:30],
        },
    }


def load_split_map(project_root: Path) -> dict[str, str]:
    manifest = read_json(project_root / "dataset" / "manifest.json")
    return {doc["doc_id"]: doc["split"] for doc in manifest.get("documents", [])}


def benchmark_lgbm_task(project_root: Path, task: str, first_hit: bool = False) -> dict[str, Any]:
    model_dir = project_root / "models" / task
    metadata = read_json(model_dir / "metadata.json")
    features = metadata["features"]
    target = metadata["target"]
    threshold = float(metadata["threshold"])
    model = joblib.load(model_dir / "model.joblib")
    csv_name = "doc_start_pages.csv" if task == "doc_start" else "signer_pages.csv"
    df = pd.read_csv(project_root / "dataset" / csv_name)
    for feature in features:
        df[feature] = pd.to_numeric(df[feature], errors="coerce").fillna(0.0)
    df[target] = pd.to_numeric(df[target], errors="coerce").fillna(0).astype(int)
    prob = model.predict_proba(df[features])[:, 1]
    df["prob"] = prob
    df["pred"] = (df["prob"] >= threshold).astype(int)

    def summarize(split_df: pd.DataFrame) -> dict[str, Any]:
        out = binary_metrics(split_df[target].astype(int).tolist(), split_df["pred"].astype(int).tolist())
        total = hit1 = hit2 = first_hit_correct = early_false = miss_or_late = 0
        for _, group in split_df.groupby("doc_id", sort=False):
            positives = set(group.loc[group[target] == 1, "page_index"].astype(int).tolist())
            if not positives:
                continue
            total += 1
            ranked = group.sort_values("prob", ascending=False)
            top1 = set(ranked.head(1)["page_index"].astype(int).tolist())
            top2 = set(ranked.head(2)["page_index"].astype(int).tolist())
            if positives & top1:
                hit1 += 1
            if positives & top2:
                hit2 += 1
            if first_hit:
                gold = min(positives)
                first = group[group["pred"] == 1].sort_values("page_ordinal").head(1)
                if first.empty:
                    miss_or_late += 1
                else:
                    pred_page = int(first.iloc[0]["page_index"])
                    if pred_page == gold:
                        first_hit_correct += 1
                    elif pred_page < gold:
                        early_false += 1
                    else:
                        miss_or_late += 1
        out["doc_top1_recall"] = hit1 / total if total else 0.0
        out["doc_top2_recall"] = hit2 / total if total else 0.0
        if first_hit:
            out["first_hit_accuracy"] = first_hit_correct / total if total else 0.0
            out["first_hit_correct"] = int(first_hit_correct)
            out["early_false_hit"] = int(early_false)
            out["miss_or_late"] = int(miss_or_late)
        out["docs_with_positive"] = int(total)
        out["threshold"] = threshold
        return out

    by_split = {split: summarize(df[df["split"] == split].copy()) for split in ("train", "val", "test")}
    return {
        "task": task,
        "features": features,
        "threshold": threshold,
        "overall": summarize(df.copy()),
        "by_split": by_split,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark current OCR heuristic page selection against splitter LightGBM.")
    parser.add_argument("--label-root", default=r"D:\tmp\Train_20260413_143844_kie\json_output_labeled")
    parser.add_argument("--ocr-root", default=r"D:\tmp\Train_20260413_143844_kie\ocr")
    parser.add_argument("--project-root", default=r"D:\tmp\Train_20260413_143844_LGBM_SPLITTER_RELPOS")
    parser.add_argument("--output", default=r"D:\tmp\Train_20260413_143844_LGBM_SPLITTER_RELPOS\reports\page_selection_benchmark.json")
    args = parser.parse_args()

    project_root = Path(args.project_root)
    split_by_doc = load_split_map(project_root)
    report = {
        "label_root": args.label_root,
        "ocr_root": args.ocr_root,
        "project_root": args.project_root,
        "current_heuristic": benchmark_current_heuristic(Path(args.label_root), Path(args.ocr_root), split_by_doc),
        "lgbm_doc_start": benchmark_lgbm_task(project_root, "doc_start", first_hit=False),
        "lgbm_signer_page": benchmark_lgbm_task(project_root, "signer_page", first_hit=True),
    }
    write_json(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
