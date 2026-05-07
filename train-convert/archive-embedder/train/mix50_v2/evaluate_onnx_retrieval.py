from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from tqdm.auto import tqdm
from transformers import AutoTokenizer


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_qrels(path: Path) -> dict[str, set[str]]:
    qrels: dict[str, set[str]] = defaultdict(set)
    with path.open("r", encoding="utf-8") as f:
        next(f, None)
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3 and parts[2] != "0":
                qrels[parts[0]].add(parts[1])
    return qrels


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def l2_normalize(x: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.clip(norm, 1e-12, None)


def mean_pool(last_hidden_state: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    mask = attention_mask[..., None].astype(np.float32)
    pooled = (last_hidden_state * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
    return l2_normalize(pooled.astype(np.float32))


class OnnxEncoder:
    def __init__(self, model_dir: Path, batch_size: int, max_length: int) -> None:
        self.model_dir = model_dir
        self.batch_size = batch_size
        self.max_length = max_length
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(model_dir / "model.onnx"),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = {i.name for i in self.session.get_inputs()}

    def encode(self, texts: list[str], desc: str) -> np.ndarray:
        embeddings: list[np.ndarray] = []
        for start in tqdm(range(0, len(texts), self.batch_size), desc=desc, leave=False):
            batch = texts[start : start + self.batch_size]
            tokens = self.tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="np",
            )
            feeds = {
                name: tokens[name].astype(np.int64)
                for name in self.input_names
                if name in tokens
            }
            if "token_type_ids" in self.input_names and "token_type_ids" not in feeds:
                feeds["token_type_ids"] = np.zeros_like(feeds["input_ids"], dtype=np.int64)
            last_hidden = self.session.run(None, feeds)[0].astype(np.float32)
            embeddings.append(mean_pool(last_hidden, feeds["attention_mask"]))
        return np.vstack(embeddings).astype(np.float32)


def metrics(
    query_embeddings: np.ndarray,
    doc_embeddings: np.ndarray,
    queries: list[dict[str, Any]],
    doc_ids: list[str],
    qrels: dict[str, set[str]],
) -> dict[str, float]:
    doc_index = {doc_id: i for i, doc_id in enumerate(doc_ids)}
    r1 = r3 = r5 = r10 = 0
    mrr = 0.0
    evaluated = 0
    for q_idx, query in enumerate(queries):
        positives = {doc_index[d] for d in qrels.get(query["query_id"], set()) if d in doc_index}
        if not positives:
            continue
        scores = doc_embeddings @ query_embeddings[q_idx]
        top = np.argpartition(-scores, min(20, len(scores)) - 1)[: min(20, len(scores))]
        top = top[np.argsort(-scores[top])]
        rank = next((i for i, doc_pos in enumerate(top, start=1) if int(doc_pos) in positives), None)
        evaluated += 1
        if rank is not None:
            mrr += 1.0 / rank
            r1 += int(rank <= 1)
            r3 += int(rank <= 3)
            r5 += int(rank <= 5)
            r10 += int(rank <= 10)
    denom = evaluated or 1
    return {
        "queries_evaluated": evaluated,
        "recall@1": r1 / denom,
        "recall@3": r3 / denom,
        "recall@5": r5 / denom,
        "recall@10": r10 / denom,
        "mrr@20": mrr / denom,
    }


def evaluate(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir)
    model_dir = Path(args.model_dir)
    encoder = OnnxEncoder(model_dir, args.batch_size, args.max_passage_len)
    corpus = read_jsonl(data_dir / "corpus.jsonl")
    qrels = load_qrels(data_dir / "qrels.tsv")
    doc_ids = [d["doc_id"] for d in corpus]
    doc_embeddings = encoder.encode([d["passage"] for d in corpus], "docs")

    report: dict[str, Any] = {"model_dir": str(model_dir), "data_dir": str(data_dir)}
    for split in args.splits.split(","):
        split = split.strip()
        if not split:
            continue
        queries = read_jsonl(data_dir / f"{split}_queries.jsonl")
        encoder.max_length = args.max_query_len
        query_embeddings = encoder.encode([q["query"] for q in queries], f"{split} queries")
        encoder.max_length = args.max_passage_len
        report[split] = metrics(query_embeddings, doc_embeddings, queries, doc_ids, qrels)

    write_json(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default="/workspace/e5_finetune_llm_v1/outputs/e5-small-kho-llm-v1-onnx-fp32")
    parser.add_argument("--data-dir", default="/workspace/e5_finetune_llm_v1/data")
    parser.add_argument("--output", default="/workspace/e5_finetune_llm_v1/outputs/e5-small-kho-llm-v1-onnx-fp32/retrieval_report.json")
    parser.add_argument("--splits", default="val,test")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-query-len", type=int, default=160)
    parser.add_argument("--max-passage-len", type=int, default=512)
    return parser.parse_args()


if __name__ == "__main__":
    evaluate(parse_args())
