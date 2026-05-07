from __future__ import annotations

from pathlib import Path
from typing import Iterable
import warnings

import numpy as np
from joblib import dump, load
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from .common import read_json, read_jsonl, write_json
from .config import LABELS, MULTI_INSTANCE_FIELDS, SINGLE_INSTANCE_FIELDS
from .schema_decoder import CandidatePrediction, decode_document_predictions

try:
    import lightgbm as lgb  # type: ignore
except Exception:  # pragma: no cover - local fallback
    lgb = None


def _rows_to_matrix(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    feature_names = sorted(rows[0]["features"].keys()) if rows else []
    matrix = np.asarray([[float(row["features"].get(name, 0.0)) for name in feature_names] for row in rows], dtype=np.float32)
    targets = np.asarray([int(row["target"]) for row in rows], dtype=np.int32)
    return matrix, targets, feature_names


def _rows_to_rank_matrix(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, list[str], list[int]]:
    rows = sorted(rows, key=lambda row: (row["doc_id"], row["field"], row["candidate_id"]))
    feature_names = sorted(rows[0]["features"].keys()) if rows else []
    matrix = np.asarray([[float(row["features"].get(name, 0.0)) for name in feature_names] for row in rows], dtype=np.float32)
    relevance = np.asarray([int(round(float(row.get("relevance", row.get("match", {}).get("f1", 0.0))) * 10.0)) for row in rows], dtype=np.int32)
    group: list[int] = []
    current_doc = None
    current_count = 0
    for row in rows:
        doc_id = row["doc_id"]
        if current_doc is None:
            current_doc = doc_id
        if doc_id != current_doc:
            group.append(current_count)
            current_doc = doc_id
            current_count = 0
        current_count += 1
    if current_count:
        group.append(current_count)
    return matrix, relevance, feature_names, group


def _train_single_field(field: str, train_rows: list[dict], val_rows: list[dict], output_dir: Path, seed: int = 1337) -> dict:
    x_train, y_train, feature_names = _rows_to_matrix(train_rows)
    x_val, y_val, _ = _rows_to_matrix(val_rows)
    if x_train.size == 0:
        raise RuntimeError(f"No training rows for {field}")
    if lgb is not None:
        pos = int(y_train.sum())
        neg = int(len(y_train) - pos)
        x_rank_train, rel_train, feature_names, train_group = _rows_to_rank_matrix(train_rows)
        x_rank_val, rel_val, _, val_group = _rows_to_rank_matrix(val_rows) if val_rows else (np.asarray([]), np.asarray([]), feature_names, [])
        model = lgb.LGBMRanker(
            objective="lambdarank",
            metric="ndcg",
            n_estimators=300,
            learning_rate=0.05,
            num_leaves=63,
            max_depth=-1,
            min_child_samples=20,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=seed,
            n_jobs=-1,
            verbosity=-1,
        )
        fit_kwargs = {}
        if len(val_rows):
            fit_kwargs["eval_set"] = [(x_rank_val, rel_val)]
            fit_kwargs["eval_group"] = [val_group]
            fit_kwargs["eval_at"] = [1, 3, 5]
            fit_kwargs["callbacks"] = [lgb.early_stopping(30, verbose=False)]
        model.fit(x_rank_train, rel_train, group=train_group, **fit_kwargs)
        model_kind = "lightgbm_ranker"
    else:
        model = HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_depth=8,
            max_iter=300,
            random_state=seed,
        )
        model.fit(x_train, y_train)
        model_kind = "hist_gradient_boosting"
    dump(model, output_dir / f"{field}.joblib")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="X does not have valid feature names.*")
        train_scores = score_rows(model, train_rows, feature_names)
        val_scores = score_rows(model, val_rows, feature_names) if len(val_rows) else []
    return {
        "field": field,
        "model_kind": model_kind,
        "train_rows": len(train_rows),
        "val_rows": len(val_rows),
        "train_positive": int(y_train.sum()),
        "val_positive": int(y_val.sum()) if len(val_rows) else 0,
        "feature_names": feature_names,
        "train_ap": float(average_precision_score(y_train, train_scores)) if len(np.unique(y_train)) > 1 else None,
        "val_ap": float(average_precision_score(y_val, val_scores)) if len(val_rows) and len(np.unique(y_val)) > 1 else None,
        "val_auc": float(roc_auc_score(y_val, val_scores)) if len(val_rows) and len(np.unique(y_val)) > 1 else None,
    }


def load_models(models_dir: str | Path) -> dict[str, object]:
    models_dir = Path(models_dir)
    return {field: load(models_dir / f"{field}.joblib") for field in LABELS if (models_dir / f"{field}.joblib").exists()}


def score_rows(model, rows: list[dict], feature_names: list[str]) -> list[float]:
    if not rows:
        return []
    matrix = np.asarray([[float(row["features"].get(name, 0.0)) for name in feature_names] for row in rows], dtype=np.float32)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="X does not have valid feature names.*")
        if hasattr(model, "predict_proba"):
            return model.predict_proba(matrix)[:, 1].tolist()
        return model.predict(matrix).tolist()


def _row_to_prediction(field: str, row: dict, score: float) -> CandidatePrediction:
    return CandidatePrediction(
        field=field,
        score=score,
        page_index=row["page_index"],
        line_ids=row["line_ids"],
        word_ids=row["word_ids"],
        bbox=row["bbox"],
        text=row["text"],
        candidate_id=row["candidate_id"],
    )


def _bbox_iou(a: list[float] | tuple[float, ...], b: list[float] | tuple[float, ...]) -> float:
    if not a or not b:
        return 0.0
    ax0, ay0, ax1, ay1 = [float(value) for value in a]
    bx0, by0, bx1, by1 = [float(value) for value in b]
    inter_w = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    inter_h = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = inter_w * inter_h
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    denom = area_a + area_b - inter
    return inter / denom if denom else 0.0


def _match_instances(preds: list[dict], gts: list[dict]) -> dict[str, float]:
    if not preds and not gts:
        return {
            "tp_words": 0.0,
            "pred_words": 0.0,
            "gt_words": 0.0,
            "exact_match": 0.0,
            "gt_instances": 0.0,
            "pred_instances": 0.0,
            "matched_instances": 0.0,
            "missing_words": 0.0,
            "extra_words": 0.0,
            "bbox_iou_sum": 0.0,
            "bbox_iou_count": 0.0,
            "bbox_iou80": 0.0,
        }
    unmatched_gts = list(gts)
    tp_words = 0.0
    pred_words = 0.0
    gt_words = 0.0
    exact_match = 0.0
    matched_instances = 0.0
    missing_words = 0.0
    extra_words = 0.0
    bbox_iou_sum = 0.0
    bbox_iou80 = 0.0
    for gt in gts:
        gt_words += len(gt["word_ids"])
    for pred in preds:
        pred_words += len(pred["word_ids"])
        pred_set = set(pred["word_ids"])
        best_idx = None
        best_overlap = 0
        for idx, gt in enumerate(unmatched_gts):
            overlap = len(pred_set & set(gt["word_ids"]))
            if overlap > best_overlap:
                best_overlap = overlap
                best_idx = idx
        if best_idx is not None:
            gt = unmatched_gts[best_idx]
            gt_set = set(gt["word_ids"])
            tp_words += best_overlap
            matched_instances += 1.0
            missing_words += len(gt_set - pred_set)
            extra_words += len(pred_set - gt_set)
            if pred_set == gt_set:
                exact_match += 1.0
            iou = _bbox_iou(pred.get("bbox", []), gt.get("bbox", []))
            bbox_iou_sum += iou
            if iou >= 0.80:
                bbox_iou80 += 1.0
            unmatched_gts.pop(best_idx)
    missing_words += sum(len(gt["word_ids"]) for gt in unmatched_gts)
    return {
        "tp_words": tp_words,
        "pred_words": pred_words,
        "gt_words": gt_words,
        "exact_match": exact_match,
        "gt_instances": float(len(gts)),
        "pred_instances": float(len(preds)),
        "matched_instances": matched_instances,
        "missing_words": missing_words,
        "extra_words": extra_words,
        "bbox_iou_sum": bbox_iou_sum,
        "bbox_iou_count": matched_instances,
        "bbox_iou80": bbox_iou80,
    }


def _aggregate_word_f1(stats: Iterable[dict[str, float]]) -> dict[str, float]:
    stats = list(stats)
    tp = sum(item["tp_words"] for item in stats)
    pred = sum(item["pred_words"] for item in stats)
    gt = sum(item["gt_words"] for item in stats)
    gt_instances = sum(item.get("gt_instances", 0.0) for item in stats)
    pred_instances = sum(item.get("pred_instances", 0.0) for item in stats)
    matched_instances = sum(item.get("matched_instances", 0.0) for item in stats)
    exact_match = sum(item.get("exact_match", 0.0) for item in stats)
    missing_words = sum(item.get("missing_words", 0.0) for item in stats)
    extra_words = sum(item.get("extra_words", 0.0) for item in stats)
    bbox_iou_sum = sum(item.get("bbox_iou_sum", 0.0) for item in stats)
    bbox_iou_count = sum(item.get("bbox_iou_count", 0.0) for item in stats)
    bbox_iou80 = sum(item.get("bbox_iou80", 0.0) for item in stats)
    precision = tp / pred if pred else 0.0
    recall = tp / gt if gt else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision and recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp_words": tp,
        "pred_words": pred,
        "gt_words": gt,
        "exact_instance_accuracy": exact_match / gt_instances if gt_instances else 0.0,
        "instance_recall": matched_instances / gt_instances if gt_instances else 0.0,
        "instance_precision": matched_instances / pred_instances if pred_instances else 0.0,
        "missing_word_rate": missing_words / gt if gt else 0.0,
        "extra_word_rate": extra_words / pred if pred else 0.0,
        "bbox_iou_avg": bbox_iou_sum / bbox_iou_count if bbox_iou_count else 0.0,
        "bbox_iou80_recall": bbox_iou80 / gt_instances if gt_instances else 0.0,
        "gt_instances": gt_instances,
        "pred_instances": pred_instances,
        "matched_instances": matched_instances,
    }


def _load_scored_candidates(
    project_root: str | Path,
    models_dir: str | Path,
    split: str,
) -> tuple[list[dict], dict[str, dict[str, list[CandidatePrediction]]]]:
    project_root = Path(project_root)
    models_dir = Path(models_dir)
    export_root = project_root / "exports"
    gt_rows = read_jsonl(export_root / "ground_truth" / f"{split}.jsonl")
    scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]] = {row["doc_id"]: {} for row in gt_rows}
    models = load_models(models_dir)
    feature_names_by_field = {field: read_json(models_dir / f"{field}.meta.json")["feature_names"] for field in LABELS}
    for field in LABELS:
        rows = read_jsonl(export_root / "fieldwise" / field / f"{split}.jsonl")
        model = models[field]
        scores = score_rows(model, rows, feature_names_by_field[field])
        grouped: dict[str, list[CandidatePrediction]] = {}
        for row, score in zip(rows, scores):
            grouped.setdefault(row["doc_id"], []).append(_row_to_prediction(field, row, score))
        for doc_id in scored_by_doc:
            scored_by_doc[doc_id][field] = grouped.get(doc_id, [])
    return gt_rows, scored_by_doc


def _prune_scored_candidates_for_tuning(
    scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]],
    *,
    top_k_default: int = 32,
) -> dict[str, dict[str, list[CandidatePrediction]]]:
    # Threshold tuning only needs candidates that can realistically survive
    # schema decoding. Keeping every low-score region makes the coordinate
    # search quadratic in practice after candidate generation was expanded.
    top_k_by_field = {
        "ADDRESSEE": 64,
        "RECIPIENTS": 80,
        "SIGNER_ROLE": 64,
        "SIGNER_NAME": 64,
        "DOC_SUBJECT": 64,
    }
    pruned: dict[str, dict[str, list[CandidatePrediction]]] = {}
    for doc_id, fields in scored_by_doc.items():
        pruned[doc_id] = {}
        for field, candidates in fields.items():
            top_k = top_k_by_field.get(field, top_k_default)
            pruned[doc_id][field] = sorted(candidates, key=lambda item: item.score, reverse=True)[:top_k]
    return pruned


def _evaluate_scored_candidates(
    gt_rows: list[dict],
    scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]],
    thresholds: dict[str, float],
    split: str,
) -> dict:
    decoded_docs = {
        gt["doc_id"]: decode_document_predictions(scored_by_doc.get(gt["doc_id"], {}), thresholds)
        for gt in gt_rows
    }
    per_field_stats = {field: [] for field in LABELS}
    for gt in gt_rows:
        gt_fields_by_label = {field: [] for field in LABELS}
        for inst in gt["field_instances"]:
            gt_fields_by_label[inst["label"]].append(inst)
        decoded = decoded_docs.get(gt["doc_id"], {})
        for field in LABELS:
            preds = [
                {
                    "word_ids": pred.word_ids,
                    "line_ids": pred.line_ids,
                    "page_index": pred.page_index,
                    "score": pred.score,
                    "bbox": pred.bbox,
                }
                for pred in decoded.get(field, [])
            ]
            per_field_stats[field].append(_match_instances(preds, gt_fields_by_label[field]))
    field_metrics = {field: _aggregate_word_f1(stats) for field, stats in per_field_stats.items()}
    overall = _aggregate_word_f1(metric for stats in per_field_stats.values() for metric in stats)
    return {"split": split, "overall": overall, "field_metrics": field_metrics, "doc_count": len(gt_rows)}


def _threshold_objective(overall: dict[str, float]) -> float:
    # F1 remains primary, but the production target is exact localization with low over/under-selection.
    return (
        overall.get("f1", 0.0)
        + 0.20 * overall.get("exact_instance_accuracy", 0.0)
        + 0.10 * overall.get("bbox_iou80_recall", 0.0)
        - 0.10 * overall.get("missing_word_rate", 0.0)
        - 0.10 * overall.get("extra_word_rate", 0.0)
    )


def _score_threshold_grid(scores: Iterable[float]) -> list[float]:
    values = sorted(float(score) for score in scores)
    if not values:
        return [0.5]
    quantiles = [0.00, 0.01, 0.03, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.97, 0.99]
    grid = {values[0] - 1e-6, values[-1] + 1e-6}
    n = len(values)
    for q in quantiles:
        grid.add(values[min(n - 1, max(0, int(round((n - 1) * q))))])
    if values[0] <= 0.95 and values[-1] >= 0.05:
        grid.update(round(value, 2) for value in np.arange(0.05, 0.96, 0.05))
    return sorted(grid)


def tune_thresholds_for_output(
    project_root: str | Path,
    models_dir: str | Path,
    base_thresholds: dict[str, float],
    split: str = "val",
    max_passes: int = 2,
) -> dict:
    gt_rows, scored_by_doc = _load_scored_candidates(project_root, models_dir, split)
    scored_by_doc = _prune_scored_candidates_for_tuning(scored_by_doc)
    threshold_grids = {
        field: _score_threshold_grid(cand.score for doc_fields in scored_by_doc.values() for cand in doc_fields.get(field, []))
        for field in LABELS
    }
    current = {field: float(base_thresholds.get(field, 0.5)) for field in LABELS}
    initial_report = _evaluate_scored_candidates(gt_rows, scored_by_doc, current, split)
    best_report = initial_report
    best_objective = _threshold_objective(best_report["overall"])
    trials = []
    for pass_index in range(max_passes):
        improved = False
        for field in LABELS:
            print(f"[train_lightgbm] tune pass {pass_index + 1}/{max_passes} field {field}", flush=True)
            field_best_threshold = current[field]
            field_best_report = best_report
            field_best_objective = best_objective
            for threshold in threshold_grids[field]:
                trial_thresholds = dict(current)
                trial_thresholds[field] = threshold
                report = _evaluate_scored_candidates(gt_rows, scored_by_doc, trial_thresholds, split)
                objective = _threshold_objective(report["overall"])
                if objective > field_best_objective + 1e-9:
                    field_best_threshold = threshold
                    field_best_report = report
                    field_best_objective = objective
            if field_best_threshold != current[field]:
                current[field] = field_best_threshold
                best_report = field_best_report
                best_objective = field_best_objective
                improved = True
            trials.append(
                {
                    "pass": pass_index + 1,
                    "field": field,
                    "threshold": current[field],
                    "objective": best_objective,
                    "overall_f1": best_report["overall"]["f1"],
                    "exact_instance_accuracy": best_report["overall"]["exact_instance_accuracy"],
                    "bbox_iou80_recall": best_report["overall"]["bbox_iou80_recall"],
                }
            )
        if not improved:
            break
    return {
        "split": split,
        "objective": "f1 + 0.20*exact + 0.10*bbox_iou80 - 0.10*missing - 0.10*extra",
        "initial_thresholds": base_thresholds,
        "thresholds": current,
        "threshold_grid_sizes": {field: len(values) for field, values in threshold_grids.items()},
        "initial_overall": initial_report["overall"],
        "tuned_overall": best_report["overall"],
        "trials": trials,
    }


def train_all_fields(project_root: str | Path, seed: int = 1337) -> dict:
    project_root = Path(project_root)
    export_root = project_root / "exports" / "fieldwise"
    models_dir = project_root / "models" / "fieldwise"
    models_dir.mkdir(parents=True, exist_ok=True)
    metrics = {}
    thresholds = {}
    for index, field in enumerate(LABELS, start=1):
        print(f"[train_lightgbm] training {index}/{len(LABELS)} {field}", flush=True)
        train_rows = read_jsonl(export_root / field / "train.jsonl")
        val_rows = read_jsonl(export_root / field / "val.jsonl")
        field_metrics = _train_single_field(field, train_rows, val_rows, models_dir, seed=seed)
        model = load(models_dir / f"{field}.joblib")
        feature_names = field_metrics["feature_names"]
        val_scores = score_rows(model, val_rows, feature_names)
        best_threshold = 0.5
        best_f1 = -1.0
        if val_rows:
            y_true = np.asarray([row["target"] for row in val_rows], dtype=np.int32)
            for threshold in _score_threshold_grid(val_scores):
                y_pred = np.asarray([1 if score >= threshold else 0 for score in val_scores], dtype=np.int32)
                tp = int(((y_true == 1) & (y_pred == 1)).sum())
                fp = int(((y_true == 0) & (y_pred == 1)).sum())
                fn = int(((y_true == 1) & (y_pred == 0)).sum())
                precision = tp / max(tp + fp, 1)
                recall = tp / max(tp + fn, 1)
                f1 = 2.0 * precision * recall / max(precision + recall, 1e-9) if precision and recall else 0.0
                if f1 > best_f1:
                    best_f1 = f1
                    best_threshold = threshold
        thresholds[field] = best_threshold
        field_metrics["threshold"] = best_threshold
        metrics[field] = field_metrics
        write_json(models_dir / f"{field}.meta.json", field_metrics)
    write_json(models_dir / "thresholds.json", thresholds)
    print("[train_lightgbm] tuning decoded thresholds", flush=True)
    threshold_tuning = tune_thresholds_for_output(project_root, models_dir, thresholds, split="val", max_passes=1)
    thresholds = threshold_tuning["thresholds"]
    for field in LABELS:
        metrics[field]["threshold"] = thresholds[field]
        write_json(models_dir / f"{field}.meta.json", metrics[field])
    write_json(models_dir / "thresholds.json", thresholds)
    write_json(models_dir / "threshold_tuning.json", threshold_tuning)
    return {"field_metrics": metrics, "thresholds": thresholds, "threshold_tuning": threshold_tuning}


def evaluate_split(project_root: str | Path, models_dir: str | Path, thresholds: dict[str, float], split: str) -> dict:
    gt_rows, scored_by_doc = _load_scored_candidates(project_root, models_dir, split)
    return _evaluate_scored_candidates(gt_rows, scored_by_doc, thresholds, split)


def _candidate_source_kind(candidate_id: str) -> str:
    parts = candidate_id.split(":")
    return parts[2] if len(parts) >= 4 else "unknown"


def candidate_oracle_split(project_root: str | Path, split: str) -> dict:
    project_root = Path(project_root)
    export_root = project_root / "exports"
    gt_rows = read_jsonl(export_root / "ground_truth" / f"{split}.jsonl")
    gt_by_doc = {row["doc_id"]: row for row in gt_rows}
    field_reports: dict[str, dict] = {}
    for field in LABELS:
        rows = read_jsonl(export_root / "fieldwise" / field / f"{split}.jsonl")
        candidates_by_doc: dict[str, list[dict]] = {}
        for row in rows:
            candidates_by_doc.setdefault(row["doc_id"], []).append(row)
        gt_count = 0
        exact = 0
        iou80 = 0
        best_f1_sum = 0.0
        best_iou_sum = 0.0
        missing_words = 0
        extra_words = 0
        gt_words_total = 0
        pred_words_total = 0
        tp_words_total = 0
        source_hits: dict[str, int] = {}
        zero_candidate_gts = 0
        for doc_id, gt in gt_by_doc.items():
            candidates = candidates_by_doc.get(doc_id, [])
            for inst in [item for item in gt["field_instances"] if item["label"] == field]:
                gt_count += 1
                gt_set = set(inst.get("word_ids") or [])
                gt_words_total += len(gt_set)
                best = None
                best_stats = {"f1": 0.0, "precision": 0.0, "recall": 0.0, "overlap": 0, "iou": 0.0}
                for cand in candidates:
                    cand_set = set(cand.get("word_ids") or [])
                    if not cand_set or not gt_set:
                        continue
                    overlap = len(cand_set & gt_set)
                    if overlap == 0:
                        continue
                    precision = overlap / max(len(cand_set), 1)
                    recall = overlap / max(len(gt_set), 1)
                    f1 = 2.0 * precision * recall / max(precision + recall, 1e-9)
                    iou = _bbox_iou(cand.get("bbox", []), inst.get("bbox", []))
                    if (f1, iou) > (best_stats["f1"], best_stats["iou"]):
                        best = cand
                        best_stats = {
                            "f1": f1,
                            "precision": precision,
                            "recall": recall,
                            "overlap": overlap,
                            "iou": iou,
                        }
                if best is None:
                    zero_candidate_gts += 1
                    missing_words += len(gt_set)
                    continue
                cand_set = set(best.get("word_ids") or [])
                best_f1_sum += best_stats["f1"]
                best_iou_sum += best_stats["iou"]
                tp_words_total += best_stats["overlap"]
                pred_words_total += len(cand_set)
                missing_words += len(gt_set - cand_set)
                extra_words += len(cand_set - gt_set)
                if cand_set == gt_set:
                    exact += 1
                if best_stats["iou"] >= 0.80:
                    iou80 += 1
                source = _candidate_source_kind(best.get("candidate_id", ""))
                source_hits[source] = source_hits.get(source, 0) + 1
        precision = tp_words_total / pred_words_total if pred_words_total else 0.0
        recall = tp_words_total / gt_words_total if gt_words_total else 0.0
        f1 = 2.0 * precision * recall / (precision + recall) if precision and recall else 0.0
        field_reports[field] = {
            "gt_instances": gt_count,
            "candidate_rows": len(rows),
            "zero_candidate_gts": zero_candidate_gts,
            "oracle_word_precision": precision,
            "oracle_word_recall": recall,
            "oracle_word_f1": f1,
            "oracle_exact_instance_accuracy": exact / gt_count if gt_count else 0.0,
            "oracle_bbox_iou80_recall": iou80 / gt_count if gt_count else 0.0,
            "oracle_best_f1_avg": best_f1_sum / gt_count if gt_count else 0.0,
            "oracle_bbox_iou_avg": best_iou_sum / max(gt_count - zero_candidate_gts, 1),
            "oracle_missing_word_rate": missing_words / gt_words_total if gt_words_total else 0.0,
            "oracle_extra_word_rate": extra_words / pred_words_total if pred_words_total else 0.0,
            "oracle_tp_words": tp_words_total,
            "oracle_pred_words": pred_words_total,
            "oracle_gt_words": gt_words_total,
            "best_source_kind_counts": source_hits,
        }
    weighted_gt = sum(report["gt_instances"] for report in field_reports.values())
    tp_total = sum(report["oracle_tp_words"] for report in field_reports.values())
    pred_total = sum(report["oracle_pred_words"] for report in field_reports.values())
    gt_total = sum(report["oracle_gt_words"] for report in field_reports.values())
    oracle_precision = tp_total / pred_total if pred_total else 0.0
    oracle_recall = tp_total / gt_total if gt_total else 0.0
    oracle_f1 = (
        2.0 * oracle_precision * oracle_recall / (oracle_precision + oracle_recall)
        if oracle_precision and oracle_recall
        else 0.0
    )
    return {
        "split": split,
        "doc_count": len(gt_rows),
        "field_reports": field_reports,
        "overall_oracle_word_precision": oracle_precision,
        "overall_oracle_word_recall": oracle_recall,
        "overall_oracle_word_f1": oracle_f1,
        "macro_oracle_word_f1": sum(report["oracle_word_f1"] for report in field_reports.values()) / max(len(field_reports), 1),
        "weighted_oracle_exact_instance_accuracy": sum(
            report["oracle_exact_instance_accuracy"] * report["gt_instances"] for report in field_reports.values()
        )
        / max(weighted_gt, 1),
        "weighted_oracle_bbox_iou80_recall": sum(
            report["oracle_bbox_iou80_recall"] * report["gt_instances"] for report in field_reports.values()
        )
        / max(weighted_gt, 1),
    }


def candidate_oracle_report(project_root: str | Path, splits: Iterable[str] = ("val", "test")) -> dict:
    return {split: candidate_oracle_split(project_root, split) for split in splits}
