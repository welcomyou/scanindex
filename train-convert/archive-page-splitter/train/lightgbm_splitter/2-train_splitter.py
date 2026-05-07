from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)


TASKS = {
    "doc_start": {
        "csv": "doc_start_pages.csv",
        "target": "target_doc_start",
        "features": [
            "regime_score",
            "org_header_score",
            "doc_number_standalone_score",
            "place_date_standalone_score",
            "subject_score",
            "doc_number_date_alignment_score",
            "header_completeness_score",
            "reference_line_penalty",
        ],
    },
    "signer_page": {
        "csv": "signer_pages.csv",
        "target": "target_signer_page",
        "features": [
            "recipients_score",
            "signer_role_score",
            "signer_name_score",
            "has_noi_nhan_regex",
            "has_tm_kt_tl_tuq_regex",
            "relative_page_position",
        ],
    },
}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def binary_metrics(y_true: np.ndarray, y_prob: np.ndarray, threshold: float) -> dict[str, Any]:
    y_pred = (y_prob >= threshold).astype(np.int32)
    labels = [0, 1]
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=labels).ravel()
    metrics = {
        "threshold": float(threshold),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "tn": int(tn),
        "fp": int(fp),
        "fn": int(fn),
        "tp": int(tp),
        "support_negative": int((y_true == 0).sum()),
        "support_positive": int((y_true == 1).sum()),
    }
    try:
        metrics["roc_auc"] = float(roc_auc_score(y_true, y_prob))
    except Exception:
        metrics["roc_auc"] = None
    try:
        metrics["average_precision"] = float(average_precision_score(y_true, y_prob))
    except Exception:
        metrics["average_precision"] = None
    return metrics


def threshold_grid(y_prob: np.ndarray) -> list[float]:
    candidates = set(float(x) for x in np.linspace(0.05, 0.95, 91))
    if y_prob.size:
        quantiles = np.quantile(y_prob, np.linspace(0.01, 0.99, 99))
        candidates.update(float(x) for x in quantiles)
    return sorted(x for x in candidates if 0.0 <= x <= 1.0)


def tune_threshold(y_true: np.ndarray, y_prob: np.ndarray, min_recall: float) -> tuple[float, dict[str, Any], list[dict[str, Any]]]:
    trials = [binary_metrics(y_true, y_prob, t) for t in threshold_grid(y_prob)]
    eligible = [m for m in trials if m["recall"] >= min_recall]
    if not eligible:
        eligible = trials
    best = max(
        eligible,
        key=lambda m: (
            m["f1"],
            m["precision"],
            m["recall"],
            -abs(m["threshold"] - 0.5),
        ),
    )
    return float(best["threshold"]), best, trials


def doc_topk_metrics(df: pd.DataFrame, y_prob: np.ndarray, target: str) -> list[dict[str, Any]]:
    work = df[["doc_id", "page_index", target]].copy()
    work["prob"] = y_prob
    rows = []
    for k in (1, 2, 3):
        total = hit = exact_top1 = 0
        for _, group in work.groupby("doc_id", sort=False):
            positives = set(group.loc[group[target] == 1, "page_index"].astype(int).tolist())
            if not positives:
                continue
            total += 1
            ranked = group.sort_values("prob", ascending=False).head(k)
            pred_pages = set(ranked["page_index"].astype(int).tolist())
            if positives & pred_pages:
                hit += 1
            if k == 1 and positives == pred_pages:
                exact_top1 += 1
        rows.append(
            {
                "k": k,
                "docs_with_positive": int(total),
                "hit": int(hit),
                "recall": float(hit / total) if total else 0.0,
                "exact_top1": float(exact_top1 / total) if k == 1 and total else None,
            }
        )
    return rows


def split_frame(df: pd.DataFrame, split: str) -> pd.DataFrame:
    out = df[df["split"] == split].copy()
    if out.empty:
        raise RuntimeError(f"missing split: {split}")
    return out


def train_task(task_name: str, dataset_root: Path, models_root: Path, min_recall: float) -> dict[str, Any]:
    spec = TASKS[task_name]
    df = pd.read_csv(dataset_root / spec["csv"])
    features = list(spec["features"])
    target = str(spec["target"])
    for col in features:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df[target] = pd.to_numeric(df[target], errors="coerce").fillna(0).astype(int)

    train_df = split_frame(df, "train")
    val_df = split_frame(df, "val")
    test_df = split_frame(df, "test")

    X_train = train_df[features].to_numpy(dtype=np.float32)
    y_train = train_df[target].to_numpy(dtype=np.int32)
    X_val = val_df[features].to_numpy(dtype=np.float32)
    y_val = val_df[target].to_numpy(dtype=np.int32)
    X_test = test_df[features].to_numpy(dtype=np.float32)
    y_test = test_df[target].to_numpy(dtype=np.int32)

    negative = max(1, int((y_train == 0).sum()))
    positive = max(1, int((y_train == 1).sum()))
    scale_pos_weight = negative / positive

    model = LGBMClassifier(
        objective="binary",
        n_estimators=400,
        learning_rate=0.035,
        num_leaves=15,
        max_depth=4,
        min_child_samples=20,
        subsample=0.90,
        colsample_bytree=1.0,
        reg_alpha=0.05,
        reg_lambda=0.20,
        scale_pos_weight=scale_pos_weight,
        random_state=42,
        n_jobs=-1,
        verbosity=-1,
    )
    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
        eval_metric="binary_logloss",
        callbacks=[],
    )

    prob_train = model.predict_proba(X_train)[:, 1]
    threshold, threshold_train_metrics, trials = tune_threshold(y_train, prob_train, min_recall=min_recall)
    prob_val = model.predict_proba(X_val)[:, 1]
    prob_test = model.predict_proba(X_test)[:, 1]

    task_dir = models_root / task_name
    task_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, task_dir / "model.joblib")
    metadata = {
        "task": task_name,
        "features": features,
        "target": target,
        "threshold": threshold,
        "min_recall_for_threshold": min_recall,
        "scale_pos_weight": scale_pos_weight,
        "train_rows": int(len(train_df)),
        "val_rows": int(len(val_df)),
        "test_rows": int(len(test_df)),
        "model_params": model.get_params(),
    }
    write_json(task_dir / "metadata.json", metadata)

    report = {
        "task": task_name,
        "features": features,
        "target": target,
        "threshold": threshold,
        "threshold_train_metrics": threshold_train_metrics,
        "splits": {
            "train": {
                **binary_metrics(y_train, prob_train, threshold),
                "topk": doc_topk_metrics(train_df, prob_train, target),
            },
            "val": {
                **binary_metrics(y_val, prob_val, threshold),
                "topk": doc_topk_metrics(val_df, prob_val, target),
            },
            "test": {
                **binary_metrics(y_test, prob_test, threshold),
                "topk": doc_topk_metrics(test_df, prob_test, target),
            },
        },
        "feature_importance_gain": {
            feature: float(value)
            for feature, value in zip(features, model.booster_.feature_importance(importance_type="gain"))
        },
        "feature_importance_split": {
            feature: int(value)
            for feature, value in zip(features, model.booster_.feature_importance(importance_type="split"))
        },
        "threshold_trials_top10": sorted(trials, key=lambda m: m["f1"], reverse=True)[:10],
    }
    write_json(task_dir / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Train page-level LightGBM splitter models.")
    parser.add_argument("--project-root", default=r"D:\tmp\Train_20260413_143844_LGBM_SPLITTER")
    parser.add_argument("--min-recall-doc-start", type=float, default=0.985)
    parser.add_argument("--min-recall-signer", type=float, default=0.990)
    args = parser.parse_args()

    project_root = Path(args.project_root)
    dataset_root = project_root / "dataset"
    models_root = project_root / "models"
    reports_root = project_root / "reports"

    reports = {
        "doc_start": train_task("doc_start", dataset_root, models_root, args.min_recall_doc_start),
        "signer_page": train_task("signer_page", dataset_root, models_root, args.min_recall_signer),
    }
    summary = {
        task: {
            "threshold": report["threshold"],
            "train": report["splits"]["train"],
            "val": report["splits"]["val"],
            "test": report["splits"]["test"],
            "feature_importance_gain": report["feature_importance_gain"],
        }
        for task, report in reports.items()
    }
    write_json(reports_root / "training_report.json", reports)
    write_json(reports_root / "training_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
