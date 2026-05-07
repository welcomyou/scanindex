from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import build_paths, read_json, write_json
from train_lightgbm.reranker_common import RERANK_FIELDS, candidate_payload, query_for, write_jsonl
from train_lightgbm.training import load_models, score_rows


def _sample_group(rows: list[dict], max_per_doc_field: int, rng: random.Random) -> list[dict]:
    positives = [row for row in rows if float(row.get("relevance", 0.0)) >= 0.999]
    near = [row for row in rows if 0.65 <= float(row.get("relevance", 0.0)) < 0.999]
    negatives = [row for row in rows if float(row.get("relevance", 0.0)) < 0.20]
    middle = [row for row in rows if 0.20 <= float(row.get("relevance", 0.0)) < 0.65]
    selected: list[dict] = []
    selected.extend(positives)
    selected.extend(near[: max(2, max_per_doc_field // 4)])
    # Hard negatives are highest LightGBM score but low relevance.
    selected.extend(sorted(negatives, key=lambda item: item.get("lgbm_score", 0.0), reverse=True)[: max(4, max_per_doc_field // 2)])
    selected.extend(sorted(middle, key=lambda item: item.get("lgbm_score", 0.0), reverse=True)[: max(2, max_per_doc_field // 4)])
    seen = set()
    deduped = []
    for row in selected:
        cid = row["candidate_id"]
        if cid in seen:
            continue
        seen.add(cid)
        deduped.append(row)
    if len(deduped) > max_per_doc_field:
        pinned = [row for row in deduped if float(row.get("relevance", 0.0)) >= 0.999]
        rest = [row for row in deduped if row not in pinned]
        rng.shuffle(rest)
        return (pinned + rest)[:max_per_doc_field]
    return deduped


def _build_split(
    *,
    project_root: Path,
    models_dir: Path,
    split: str,
    fields: list[str],
    max_docs: int | None,
    max_per_doc_field: int,
    seed: int,
) -> list[dict]:
    export_root = project_root / "exports" / "fieldwise"
    models = load_models(models_dir)
    feature_names_by_field = {field: read_json(models_dir / f"{field}.meta.json")["feature_names"] for field in fields}
    rng = random.Random(f"{seed}:{split}")
    out: list[dict] = []
    for field in fields:
        print(f"[reranker_dataset] split={split} field={field}", flush=True)
        rows = []
        raw_rows = []
        with (export_root / field / f"{split}.jsonl").open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    raw_rows.append(json.loads(line))
        scores = score_rows(models[field], raw_rows, feature_names_by_field[field])
        for row, score in zip(raw_rows, scores):
            item = dict(row)
            item["lgbm_score"] = float(score)
            rows.append(item)
        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in rows:
            grouped[(row["doc_id"], row["field"])].append(row)
        keys = sorted(grouped)
        if max_docs is not None:
            allowed_docs = set(sorted({doc_id for doc_id, _ in keys})[:max_docs])
            keys = [key for key in keys if key[0] in allowed_docs]
        for key in keys:
            group = sorted(grouped[key], key=lambda item: item.get("lgbm_score", 0.0), reverse=True)
            for row in _sample_group(group, max_per_doc_field, rng):
                relevance = float(row.get("relevance", row.get("match", {}).get("f1", 0.0)))
                out.append(
                    {
                        "doc_id": row["doc_id"],
                        "relative_pdf_path": row.get("relative_pdf_path"),
                        "split": split,
                        "field": field,
                        "candidate_id": row["candidate_id"],
                        "query": query_for(field),
                        "candidate": candidate_payload(
                            field=field,
                            text=row.get("text"),
                            candidate_id=row["candidate_id"],
                            page_index=row.get("page_index"),
                            bbox=row.get("bbox"),
                            line_ids=row.get("line_ids"),
                            word_ids=row.get("word_ids"),
                            lgbm_score=row.get("lgbm_score"),
                            page_role=row.get("page_role"),
                            features=row.get("features"),
                        ),
                        "label": relevance,
                        "binary_label": 1 if relevance >= 0.999 else 0,
                        "lgbm_score": row.get("lgbm_score"),
                        "relevance": relevance,
                    }
                )
    rng.shuffle(out)
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build cross-encoder reranker dataset from LightGBM candidate exports.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--output-subdir", default="reranker_mminilm")
    parser.add_argument("--fields", nargs="+", default=RERANK_FIELDS)
    parser.add_argument("--max-train-docs", type=int, default=240)
    parser.add_argument("--max-eval-docs", type=int, default=80)
    parser.add_argument("--max-per-doc-field", type=int, default=12)
    parser.add_argument("--seed", type=int, default=1337)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = build_paths(args.project_root)
    models_dir = paths.models_root / "fieldwise"
    out_dir = paths.exports_root / args.output_subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {}
    for split in ("train", "val", "test"):
        max_docs = args.max_train_docs if split == "train" else args.max_eval_docs
        if max_docs is not None and max_docs <= 0:
            max_docs = None
        rows = _build_split(
            project_root=paths.root,
            models_dir=models_dir,
            split=split,
            fields=args.fields,
            max_docs=max_docs,
            max_per_doc_field=args.max_per_doc_field,
            seed=args.seed,
        )
        write_jsonl(out_dir / f"{split}.jsonl", rows)
        summary[split] = {
            "rows": len(rows),
            "fields": dict(Counter(row["field"] for row in rows)),
            "binary_positive": sum(1 for row in rows if row["binary_label"] == 1),
            "avg_relevance": sum(float(row["label"]) for row in rows) / max(len(rows), 1),
        }
    report = {
        "project_root": str(paths.root),
        "output_dir": str(out_dir),
        "fields": args.fields,
        "max_train_docs": args.max_train_docs,
        "max_eval_docs": args.max_eval_docs,
        "max_per_doc_field": args.max_per_doc_field,
        "summary": summary,
    }
    write_json(out_dir / "dataset_report.json", report)
    write_json(paths.reports_root / "reranker_dataset_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
