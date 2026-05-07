from __future__ import annotations

import argparse
import csv
import json
import random
import re
import shutil
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from datasets import Dataset, DatasetDict, get_dataset_config_names, load_dataset
from huggingface_hub import hf_hub_download
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from tqdm.auto import tqdm


SEED = 20260503

ZALO_REPO = "GreenNode/zalo-ai-legal-text-retrieval-vn"
YUITC_REPO = "YuITC/Vietnamese-Legal-Documents"

VNMTEB_REPOS = [
    "GreenNode/arguana-vn",
    "GreenNode/fiqa-vn",
    "GreenNode/nfcorpus-vn",
    "GreenNode/scifact-vn",
    "GreenNode/nano-msmarco-vn",
    "GreenNode/nano-nq-vn",
    "GreenNode/nano-hotpotqa-vn",
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_qrels(path: Path) -> dict[str, list[str]]:
    qrels: dict[str, list[str]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as f:
        next(f, None)
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3 and parts[2] != "0":
                qrels[parts[0]].append(parts[1])
    return qrels


def strip_prefix(text: str, prefix: str) -> str:
    return text[len(prefix) :] if text.startswith(prefix) else text


def ensure_query(text: str) -> str:
    text = strip_prefix(text.strip(), "query: ").strip()
    return f"query: {text}"


def ensure_passage(text: str) -> str:
    text = strip_prefix(text.strip(), "passage: ").strip()
    return f"passage: {text}"


def shorten_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def remove_find_prefix(text: str) -> str:
    text = strip_prefix(text, "query: ").strip()
    prefixes = [
        "Tìm tài liệu đề cập đến ",
        "Tìm tài liệu nói về ",
        "Tìm văn bản đề cập đến ",
        "Tìm văn bản nói về ",
    ]
    for prefix in prefixes:
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def diversify_kho_query(text: str, index: int) -> str:
    core = remove_find_prefix(text).rstrip(". ").strip()
    original = strip_prefix(text, "query: ").strip()
    bucket = index % 20
    if bucket < 6:
        out = original
    elif bucket < 10:
        out = f"Văn bản nào nói về {core}"
    elif bucket < 14:
        out = core
    elif bucket < 17:
        out = f"Nội dung liên quan đến {core}"
    elif bucket < 19:
        out = f"Cần tìm văn bản có nội dung {core}"
    else:
        out = f"{core} văn bản nào"
    return ensure_query(shorten_ws(out))


def corpus_text(row: dict[str, Any]) -> str:
    title = str(row.get("title") or "").strip()
    text = str(row.get("text") or "").strip()
    if title and text:
        return f"{title}\n{text}"
    return text or title


def row_id(row: dict[str, Any]) -> str:
    if "id" in row:
        return str(row["id"])
    if "_id" in row:
        return str(row["_id"])
    raise KeyError(f"Row has neither id nor _id: {list(row)}")


def load_config_split(repo: str, config: str, available_configs: set[str]) -> Dataset:
    resolved_config = config
    if resolved_config not in available_configs and config == "qrels" and "default" in available_configs:
        resolved_config = "default"
    data = load_dataset(repo, resolved_config)
    if isinstance(data, Dataset):
        return data
    if not isinstance(data, DatasetDict):
        raise TypeError(f"Unexpected dataset type for {repo}/{config}: {type(data)}")
    if "test" in data:
        return data["test"]
    if resolved_config in data:
        return data[resolved_config]
    first_split = next(iter(data))
    return data[first_split]


def load_vnmteb_repo(repo: str) -> tuple[dict[str, str], dict[str, str], list[tuple[str, str, float]]]:
    available_configs = set(get_dataset_config_names(repo))
    corpus_ds = load_config_split(repo, "corpus", available_configs)
    queries_ds = load_config_split(repo, "queries", available_configs)
    qrels_ds = load_config_split(repo, "qrels", available_configs)
    docs = {row_id(dict(row)): corpus_text(dict(row)) for row in corpus_ds}
    queries = {row_id(dict(row)): str(row["text"]) for row in queries_ds}
    qrels: list[tuple[str, str, float]] = []
    for row in qrels_ds:
        qid = str(row["query-id"])
        did = str(row["corpus-id"])
        score = float(row.get("score") or 0.0)
        if score > 0 and qid in queries and did in docs:
            qrels.append((qid, did, score))
    return docs, queries, qrels


def load_zalo() -> tuple[dict[str, str], dict[str, str], list[tuple[str, str]], list[tuple[str, str]]]:
    base = f"hf://datasets/{ZALO_REPO}"
    corpus_ds = load_dataset("parquet", data_files=f"{base}/corpus/test-00000-of-00001.parquet", split="train")
    queries_ds = load_dataset("parquet", data_files=f"{base}/queries/test-00000-of-00001.parquet", split="train")
    docs = {str(row["id"]): corpus_text(dict(row)) for row in corpus_ds}
    queries = {str(row["id"]): str(row["text"]) for row in queries_ds}

    def read_qrels(filename: str) -> list[tuple[str, str]]:
        path = Path(hf_hub_download(ZALO_REPO, filename, repo_type="dataset"))
        pairs: list[tuple[str, str]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                qid = str(row.get("query-id") or row.get("query_id"))
                did = str(row.get("corpus-id") or row.get("corpus_id"))
                score = float(row.get("score") or 0.0)
                if score > 0 and qid in queries and did in docs:
                    pairs.append((qid, did))
        return pairs

    return docs, queries, read_qrels("qrels/train.jsonl"), read_qrels("qrels/test.jsonl")


def as_first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def load_yuitc(train_limit: int, eval_limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    eval_rows: list[dict[str, Any]] = []
    for split, limit, target in [("train", train_limit, rows), ("test", eval_limit, eval_rows)]:
        ds = load_dataset(YUITC_REPO, split=split)
        count = 0
        for raw in ds:
            q = str(raw["question"]).strip()
            context = as_first(raw["context_list"])
            cid = as_first(raw["cid"])
            if not q or not context or cid is None:
                continue
            target.append(
                {
                    "source": "yuitc",
                    "query_id": f"yuitc:{split}:q:{raw['qid']}",
                    "doc_id": f"yuitc:{split}:d:{cid}",
                    "query": ensure_query(q),
                    "positive": ensure_passage(str(context)),
                }
            )
            count += 1
            if count >= limit:
                break
    return rows, eval_rows


def split_eval_rows(rows: list[dict[str, Any]], val_size: int, test_size: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(SEED)
    rows = list(rows)
    rng.shuffle(rows)
    return rows[:val_size], rows[val_size : val_size + test_size]


def mine_negatives(
    pairs: list[dict[str, Any]],
    doc_text_by_id: dict[str, str],
    negative_count: int,
    batch_size: int = 512,
) -> list[dict[str, Any]]:
    doc_ids = list(doc_text_by_id)
    doc_texts = [strip_prefix(doc_text_by_id[doc_id], "passage: ") for doc_id in doc_ids]
    vectorizer = TfidfVectorizer(
        lowercase=True,
        strip_accents="unicode",
        ngram_range=(1, 2),
        min_df=1,
        max_df=0.95,
        max_features=240_000,
    )
    doc_matrix = vectorizer.fit_transform(doc_texts)
    id_to_pos = {doc_id: i for i, doc_id in enumerate(doc_ids)}
    mined: list[dict[str, Any]] = []
    for start in tqdm(range(0, len(pairs), batch_size), desc="mine negatives", leave=False):
        batch = pairs[start : start + batch_size]
        query_texts = [remove_find_prefix(pair["query"]) for pair in batch]
        query_matrix = vectorizer.transform(query_texts)
        scores = cosine_similarity(query_matrix, doc_matrix)
        for row_idx, pair in enumerate(batch):
            positives = set(pair.get("positive_doc_ids") or [pair["positive_doc_id"]])
            positive_positions = {id_to_pos[d] for d in positives if d in id_to_pos}
            order = np_argtop(scores[row_idx], top_n=min(80, len(doc_ids)))
            negatives: list[str] = []
            for pos in order:
                doc_id = doc_ids[int(pos)]
                if int(pos) in positive_positions:
                    continue
                if doc_id in positives:
                    continue
                negatives.append(doc_text_by_id[doc_id])
                if len(negatives) >= negative_count:
                    break
            if len(negatives) < negative_count:
                for doc_id in doc_ids:
                    if doc_id not in positives:
                        negatives.append(doc_text_by_id[doc_id])
                    if len(negatives) >= negative_count:
                        break
            out = dict(pair)
            out["negatives"] = negatives[:negative_count]
            mined.append(out)
    return mined


def np_argtop(values: Any, top_n: int) -> list[int]:
    import numpy as np

    if top_n <= 0:
        return []
    top_n = min(top_n, values.shape[0])
    top = np.argpartition(-values, top_n - 1)[:top_n]
    top = top[np.argsort(-values[top])]
    return [int(x) for x in top]


def build_kho_pairs(kho_data_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queries = read_jsonl(kho_data_dir / "queries.jsonl")
    corpus_rows = read_jsonl(kho_data_dir / "corpus.jsonl")
    qrels = load_qrels(kho_data_dir / "qrels.tsv")
    corpus_by_id = {f"kho:{row['doc_id']}": ensure_passage(row["passage"]) for row in corpus_rows}
    raw_corpus_by_id = {row["doc_id"]: ensure_passage(row["passage"]) for row in corpus_rows}
    pairs: list[dict[str, Any]] = []
    for idx, query in enumerate(queries):
        raw_doc_ids = qrels.get(query["query_id"], [])
        if not raw_doc_ids:
            continue
        raw_doc_id = raw_doc_ids[0]
        positive = raw_corpus_by_id.get(raw_doc_id)
        if not positive:
            continue
        query_id = f"kho:{query['query_id']}"
        doc_id = f"kho:{raw_doc_id}"
        pairs.append(
            {
                "source": "kho",
                "query_id": query_id,
                "query": diversify_kho_query(query.get("query_text_no_prefix") or query["query"], idx),
                "positive_doc_id": doc_id,
                "positive_doc_ids": [doc_id],
                "positive": positive,
            }
        )
    corpus = [{"doc_id": doc_id, "passage": text, "source": "kho"} for doc_id, text in corpus_by_id.items()]
    return pairs, corpus


def build_public_candidates(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(SEED)
    train_pairs: list[dict[str, Any]] = []
    eval_pairs: list[dict[str, Any]] = []
    corpus: dict[str, dict[str, Any]] = {}

    def add_doc(doc_id: str, text: str, source: str) -> None:
        if doc_id not in corpus:
            corpus[doc_id] = {"doc_id": doc_id, "passage": ensure_passage(text), "source": source}

    def add_pair(pair: dict[str, Any], target: list[dict[str, Any]]) -> None:
        target.append(pair)

    print("loading ZaloAI Legal...", flush=True)
    try:
        zalo_docs, zalo_queries, zalo_train, zalo_test = load_zalo()
        rng.shuffle(zalo_train)
        rng.shuffle(zalo_test)
        for qid, did in zalo_train[: args.zalo_train_limit]:
            doc_id = f"zalo:d:{did}"
            query_id = f"zalo:train:q:{qid}:{did}"
            add_doc(doc_id, zalo_docs[did], "zalo")
            add_pair(
                {
                    "source": "zalo",
                    "query_id": query_id,
                    "query": ensure_query(zalo_queries[qid]),
                    "positive_doc_id": doc_id,
                    "positive_doc_ids": [doc_id],
                    "positive": ensure_passage(zalo_docs[did]),
                },
                train_pairs,
            )
        for qid, did in zalo_test[: args.zalo_eval_limit]:
            doc_id = f"zalo:d:{did}"
            query_id = f"zalo:test:q:{qid}:{did}"
            add_doc(doc_id, zalo_docs[did], "zalo")
            add_pair(
                {
                    "source": "zalo_eval",
                    "query_id": query_id,
                    "query": ensure_query(zalo_queries[qid]),
                    "positive_doc_id": doc_id,
                    "positive_doc_ids": [doc_id],
                    "positive": ensure_passage(zalo_docs[did]),
                },
                eval_pairs,
            )
    except Exception as exc:
        print(f"WARNING: skipped ZaloAI Legal: {exc!r}", flush=True)

    print("loading YuITC Legal...", flush=True)
    try:
        yuitc_train, yuitc_eval = load_yuitc(args.yuitc_train_limit, args.yuitc_eval_limit)
        for pair in yuitc_train:
            add_doc(pair["doc_id"], pair["positive"], "yuitc")
            add_pair({**pair, "positive_doc_id": pair["doc_id"], "positive_doc_ids": [pair["doc_id"]]}, train_pairs)
        for pair in yuitc_eval:
            add_doc(pair["doc_id"], pair["positive"], "yuitc")
            add_pair(
                {**pair, "source": "yuitc_eval", "positive_doc_id": pair["doc_id"], "positive_doc_ids": [pair["doc_id"]]},
                eval_pairs,
            )
    except Exception as exc:
        print(f"WARNING: skipped YuITC Legal: {exc!r}", flush=True)

    print("loading VN-MTEB public retrieval...", flush=True)
    per_repo_limit = max(1, args.vnmteb_train_limit // max(1, len(VNMTEB_REPOS)))
    for repo in VNMTEB_REPOS:
        try:
            docs, queries, qrels = load_vnmteb_repo(repo)
            rng.shuffle(qrels)
            slug = repo.split("/", 1)[1]
            for qid, did, _score in qrels[:per_repo_limit]:
                doc_id = f"vnmteb:{slug}:d:{did}"
                query_id = f"vnmteb:{slug}:q:{qid}:{did}"
                add_doc(doc_id, docs[did], f"vnmteb:{slug}")
                add_pair(
                    {
                        "source": f"vnmteb:{slug}",
                        "query_id": query_id,
                        "query": ensure_query(queries[qid]),
                        "positive_doc_id": doc_id,
                        "positive_doc_ids": [doc_id],
                        "positive": ensure_passage(docs[did]),
                    },
                    train_pairs,
                )
            for qid, did, _score in qrels[per_repo_limit : per_repo_limit + args.vnmteb_eval_per_repo]:
                doc_id = f"vnmteb:{slug}:d:{did}"
                query_id = f"vnmteb:{slug}:eval:q:{qid}:{did}"
                add_doc(doc_id, docs[did], f"vnmteb:{slug}")
                add_pair(
                    {
                        "source": f"vnmteb_eval:{slug}",
                        "query_id": query_id,
                        "query": ensure_query(queries[qid]),
                        "positive_doc_id": doc_id,
                        "positive_doc_ids": [doc_id],
                        "positive": ensure_passage(docs[did]),
                    },
                    eval_pairs,
                )
        except Exception as exc:
            print(f"WARNING: skipped {repo}: {exc!r}", flush=True)
    return train_pairs, eval_pairs, list(corpus.values())


def sample_public_train(public_pairs: list[dict[str, Any]], target_count: int) -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in public_pairs:
        by_source[pair["source"]].append(pair)
    for rows in by_source.values():
        rng.shuffle(rows)

    source_groups = {
        "zalo": ["zalo"],
        "yuitc": ["yuitc"],
        "vnmteb": [source for source in by_source if source.startswith("vnmteb:")],
    }
    quotas = {
        "zalo": int(target_count * 0.20),
        "yuitc": int(target_count * 0.45),
        "vnmteb": target_count - int(target_count * 0.20) - int(target_count * 0.45),
    }
    selected: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for group, quota in quotas.items():
        candidates: list[dict[str, Any]] = []
        for source in source_groups[group]:
            candidates.extend(by_source.get(source, []))
        rng.shuffle(candidates)
        for pair in candidates:
            if len([x for x in selected if x["source"] in source_groups[group]]) >= quota:
                break
            selected.append(pair)
            used_ids.add(pair["query_id"])
    if len(selected) < target_count:
        remaining = [pair for pair in public_pairs if pair["query_id"] not in used_ids]
        rng.shuffle(remaining)
        selected.extend(remaining[: target_count - len(selected)])
    rng.shuffle(selected)
    return selected[:target_count]


def write_training_files(
    output_data_dir: Path,
    corpus_rows: list[dict[str, Any]],
    train_pairs: list[dict[str, Any]],
    val_pairs: list[dict[str, Any]],
    test_pairs: list[dict[str, Any]],
) -> None:
    output_data_dir.mkdir(parents=True, exist_ok=True)
    corpus_rows = sorted({row["doc_id"]: row for row in corpus_rows}.values(), key=lambda row: row["doc_id"])
    write_jsonl(output_data_dir / "corpus.jsonl", corpus_rows)
    write_jsonl(
        output_data_dir / "train_pairs.jsonl",
        [
            {
                "query_id": row["query_id"],
                "query": row["query"],
                "positive_doc_id": row["positive_doc_id"],
                "positive": row["positive"],
                "negatives": row["negatives"],
                "source": row["source"],
            }
            for row in train_pairs
        ],
    )
    write_jsonl(
        output_data_dir / "val_queries.jsonl",
        [{"query_id": row["query_id"], "query": row["query"], "source": row["source"]} for row in val_pairs],
    )
    write_jsonl(
        output_data_dir / "test_queries.jsonl",
        [{"query_id": row["query_id"], "query": row["query"], "source": row["source"]} for row in test_pairs],
    )
    write_jsonl(
        output_data_dir / "train_queries.jsonl",
        [{"query_id": row["query_id"], "query": row["query"], "source": row["source"]} for row in train_pairs],
    )
    qrels_path = output_data_dir / "qrels.tsv"
    with qrels_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("query-id\tcorpus-id\tscore\n")
        for row in train_pairs + val_pairs + test_pairs:
            for doc_id in row.get("positive_doc_ids") or [row["positive_doc_id"]]:
                f.write(f"{row['query_id']}\t{doc_id}\t1\n")
    # Compatibility files for scripts that expect split qrels/corpus.
    shutil.copyfile(qrels_path, output_data_dir / "train_qrels.tsv")
    shutil.copyfile(qrels_path, output_data_dir / "val_qrels.tsv")
    shutil.copyfile(qrels_path, output_data_dir / "test_qrels.tsv")
    write_jsonl(output_data_dir / "train_corpus.jsonl", corpus_rows)
    write_jsonl(output_data_dir / "val_corpus.jsonl", corpus_rows)
    write_jsonl(output_data_dir / "test_corpus.jsonl", corpus_rows)


def make_zip(source_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in source_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(source_dir.parent))


def main(args: argparse.Namespace) -> None:
    random.seed(SEED)
    output_dir = Path(args.output_dir)
    output_data_dir = output_dir / "data"
    report_dir = output_dir / "reports"
    runpod_dir = output_dir / "runpod"
    if output_dir.exists() and args.clean:
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    runpod_dir.mkdir(parents=True, exist_ok=True)

    print("building Kho pairs...", flush=True)
    kho_pairs, kho_corpus = build_kho_pairs(Path(args.kho_data_dir))
    public_target = len(kho_pairs)

    print("building public candidates...", flush=True)
    public_candidates, public_eval_candidates, public_corpus = build_public_candidates(args)
    public_train = sample_public_train(public_candidates, public_target)
    val_pairs, test_pairs = split_eval_rows(public_eval_candidates, args.val_size, args.test_size)

    all_corpus_rows = kho_corpus + public_corpus
    doc_text_by_id = {row["doc_id"]: row["passage"] for row in all_corpus_rows}

    train_without_negatives = kho_pairs + public_train
    random.Random(SEED).shuffle(train_without_negatives)
    train_pairs = mine_negatives(train_without_negatives, doc_text_by_id, args.negatives)

    write_training_files(output_data_dir, all_corpus_rows, train_pairs, val_pairs, test_pairs)

    source_counts: dict[str, int] = defaultdict(int)
    for row in train_pairs:
        source_counts[row["source"]] += 1
    eval_counts: dict[str, int] = defaultdict(int)
    for row in val_pairs + test_pairs:
        eval_counts[row["source"]] += 1
    report = {
        "seed": SEED,
        "kho_train_pairs": len(kho_pairs),
        "public_train_pairs": len(public_train),
        "total_train_pairs": len(train_pairs),
        "ratio_kho": len(kho_pairs) / max(1, len(train_pairs)),
        "ratio_public": len(public_train) / max(1, len(train_pairs)),
        "corpus_docs": len({row["doc_id"] for row in all_corpus_rows}),
        "val_queries": len(val_pairs),
        "test_queries": len(test_pairs),
        "train_source_counts": dict(sorted(source_counts.items())),
        "eval_source_counts": dict(sorted(eval_counts.items())),
        "query_style_note": "Kho queries are diversified deterministically: about 30% original 'Tìm tài liệu...' style, remainder rephrased/bare/natural variants.",
    }
    write_json(report_dir / "mixed_dataset_report.json", report)
    with (report_dir / "train_source_counts.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["source", "count"])
        writer.writerows(sorted(source_counts.items()))

    # Copy runpod scripts needed for training.
    source_runpod = Path(args.source_runpod_dir)
    for name in ["train_e5_small.py", "export_onnx_fp32.py", "evaluate_onnx_retrieval.py"]:
        src = source_runpod / name
        if src.exists():
            shutil.copy2(src, runpod_dir / name)
    readme = output_dir / "RUNPOD_COMMANDS.md"
    readme.write_text(
        "\n".join(
            [
                "# RunPod commands",
                "",
                "```bash",
                "cd /workspace/e5_finetune_mix50_v2",
                "python3 runpod/train_e5_small.py --data-dir data --output-dir outputs/e5-small-mix50-v2 --epochs 2 --batch-size 32 --grad-accum 4 --negatives 3 --fp16",
                "python3 runpod/export_onnx_fp32.py --model-dir outputs/e5-small-mix50-v2/best --output-dir outputs/e5-small-mix50-v2-onnx-fp32",
                "python3 runpod/evaluate_onnx_retrieval.py --onnx-dir outputs/e5-small-mix50-v2-onnx-fp32 --data-dir data --split test",
                "```",
                "",
                "Suggested first run is 2 epochs. Stop if public validation drops while Kho gains only marginally.",
            ]
        ),
        encoding="utf-8",
    )

    zip_path = Path(args.zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    make_zip(output_dir, zip_path)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(f"wrote {output_dir}", flush=True)
    print(f"wrote {zip_path}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kho-data-dir", default=r"D:\App\ocrtool\temp\e5_finetune_llm_v1\data")
    parser.add_argument("--source-runpod-dir", default=r"D:\App\ocrtool\temp\e5_finetune_llm_v1\runpod")
    parser.add_argument("--output-dir", default=r"D:\App\ocrtool\temp\e5_finetune_mix50_v2")
    parser.add_argument("--zip-path", default=r"D:\App\ocrtool\temp\e5_finetune_mix50_v2_runpod.zip")
    parser.add_argument("--negatives", type=int, default=3)
    parser.add_argument("--zalo-train-limit", type=int, default=2600)
    parser.add_argument("--zalo-eval-limit", type=int, default=800)
    parser.add_argument("--yuitc-train-limit", type=int, default=7000)
    parser.add_argument("--yuitc-eval-limit", type=int, default=2400)
    parser.add_argument("--vnmteb-train-limit", type=int, default=5000)
    parser.add_argument("--vnmteb-eval-per-repo", type=int, default=120)
    parser.add_argument("--val-size", type=int, default=1000)
    parser.add_argument("--test-size", type=int, default=1000)
    parser.add_argument("--clean", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


if __name__ == "__main__":
    main(parse_args())
