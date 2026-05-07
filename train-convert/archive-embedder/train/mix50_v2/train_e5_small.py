from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.data import DataLoader, Dataset
from tqdm.auto import tqdm
from transformers import AutoModel, AutoTokenizer, get_linear_schedule_with_warmup


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


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def strip_prefix(text: str) -> str:
    if text.startswith("query: "):
        return text[len("query: ") :]
    if text.startswith("passage: "):
        return text[len("passage: ") :]
    return text


class PairDataset(Dataset):
    def __init__(self, rows: list[dict[str, Any]], negatives: int) -> None:
        self.rows = rows
        self.negatives = negatives

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, Any]:
        row = self.rows[index]
        negatives = list(row["negatives"][: self.negatives])
        if len(negatives) < self.negatives:
            negatives.extend([negatives[-1]] * (self.negatives - len(negatives)))
        return {
            "query": row["query"],
            "positive": row["positive"],
            "negatives": negatives,
            "query_id": row["query_id"],
            "positive_doc_id": row["positive_doc_id"],
        }


class Collator:
    def __init__(self, tokenizer: AutoTokenizer, max_query_len: int, max_passage_len: int) -> None:
        self.tokenizer = tokenizer
        self.max_query_len = max_query_len
        self.max_passage_len = max_passage_len

    def __call__(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        queries = [r["query"] for r in rows]
        passages: list[str] = []
        for row in rows:
            passages.append(row["positive"])
            passages.extend(row["negatives"])

        query_tokens = self.tokenizer(
            queries,
            padding=True,
            truncation=True,
            max_length=self.max_query_len,
            return_tensors="pt",
        )
        passage_tokens = self.tokenizer(
            passages,
            padding=True,
            truncation=True,
            max_length=self.max_passage_len,
            return_tensors="pt",
        )
        return {
            "query_tokens": query_tokens,
            "passage_tokens": passage_tokens,
            "group_size": 1 + len(rows[0]["negatives"]),
            "query_ids": [r["query_id"] for r in rows],
            "positive_doc_ids": [r["positive_doc_id"] for r in rows],
        }


def mean_pool(last_hidden_state: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    mask = attention_mask.unsqueeze(-1).to(last_hidden_state.dtype)
    summed = torch.sum(last_hidden_state * mask, dim=1)
    counts = torch.clamp(mask.sum(dim=1), min=1e-9)
    return summed / counts


class E5BiEncoder(nn.Module):
    def __init__(self, model_name_or_path: str) -> None:
        super().__init__()
        self.model = AutoModel.from_pretrained(model_name_or_path)

    def encode(self, tokens: dict[str, torch.Tensor]) -> torch.Tensor:
        outputs = self.model(**tokens)
        pooled = mean_pool(outputs.last_hidden_state, tokens["attention_mask"])
        return F.normalize(pooled, p=2, dim=1)

    def save_pretrained(self, path: Path) -> None:
        self.model.save_pretrained(path)


@torch.no_grad()
def encode_texts(
    model: E5BiEncoder,
    tokenizer: AutoTokenizer,
    texts: list[str],
    batch_size: int,
    max_length: int,
    device: torch.device,
    desc: str,
) -> np.ndarray:
    model.eval()
    all_embeddings: list[np.ndarray] = []
    for start in tqdm(range(0, len(texts), batch_size), desc=desc, leave=False):
        batch = texts[start : start + batch_size]
        tokens = tokenizer(
            batch,
            padding=True,
            truncation=True,
            max_length=max_length,
            return_tensors="pt",
        )
        tokens = {k: v.to(device) for k, v in tokens.items()}
        emb = model.encode(tokens).detach().cpu().float().numpy()
        all_embeddings.append(emb)
    return np.vstack(all_embeddings).astype(np.float32)


def retrieval_metrics(
    query_embeddings: np.ndarray,
    doc_embeddings: np.ndarray,
    queries: list[dict[str, Any]],
    doc_ids: list[str],
    qrels: dict[str, set[str]],
    top_k: int = 20,
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
        limit = min(top_k, len(scores))
        top = np.argpartition(-scores, limit - 1)[:limit]
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


@torch.no_grad()
def evaluate(
    model: E5BiEncoder,
    tokenizer: AutoTokenizer,
    data_dir: Path,
    split: str,
    batch_size: int,
    max_query_len: int,
    max_passage_len: int,
    device: torch.device,
) -> dict[str, float]:
    corpus = read_jsonl(data_dir / "corpus.jsonl")
    queries = read_jsonl(data_dir / f"{split}_queries.jsonl")
    qrels = load_qrels(data_dir / "qrels.tsv")
    doc_ids = [d["doc_id"] for d in corpus]
    doc_embeddings = encode_texts(
        model,
        tokenizer,
        [d["passage"] for d in corpus],
        batch_size,
        max_passage_len,
        device,
        f"eval {split} docs",
    )
    query_embeddings = encode_texts(
        model,
        tokenizer,
        [q["query"] for q in queries],
        batch_size,
        max_query_len,
        device,
        f"eval {split} queries",
    )
    return retrieval_metrics(query_embeddings, doc_embeddings, queries, doc_ids, qrels)


def train(args: argparse.Namespace) -> None:
    set_seed(args.seed)
    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    device = torch.device("cuda" if torch.cuda.is_available() and not args.cpu else "cpu")
    amp_dtype = torch.bfloat16 if args.bf16 else torch.float16
    use_amp = device.type == "cuda" and (args.fp16 or args.bf16)

    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
    model = E5BiEncoder(args.model_name_or_path).to(device)

    train_rows = read_jsonl(data_dir / "train_pairs.jsonl")
    if args.max_train_pairs:
        train_rows = train_rows[: args.max_train_pairs]
    dataset = PairDataset(train_rows, args.negatives)
    loader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        pin_memory=device.type == "cuda",
        collate_fn=Collator(tokenizer, args.max_query_len, args.max_passage_len),
    )

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    total_update_steps = math.ceil(len(loader) / args.grad_accum) * args.epochs
    warmup_steps = int(total_update_steps * args.warmup_ratio)
    scheduler = get_linear_schedule_with_warmup(optimizer, warmup_steps, total_update_steps)
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp and args.fp16 and not args.bf16)

    best_val = -1.0
    best_epoch = 0
    history: list[dict[str, Any]] = []
    global_step = 0
    start_time = time.perf_counter()

    print(
        json.dumps(
            {
                "device": str(device),
                "train_pairs": len(train_rows),
                "epochs": args.epochs,
                "batch_size": args.batch_size,
                "grad_accum": args.grad_accum,
                "effective_batch": args.batch_size * args.grad_accum,
                "negatives_per_query": args.negatives,
                "total_update_steps": total_update_steps,
                "warmup_steps": warmup_steps,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    for epoch in range(1, args.epochs + 1):
        model.train()
        running_loss = 0.0
        optimizer.zero_grad(set_to_none=True)
        progress = tqdm(loader, desc=f"epoch {epoch}/{args.epochs}")

        for step, batch in enumerate(progress, start=1):
            query_tokens = {k: v.to(device) for k, v in batch["query_tokens"].items()}
            passage_tokens = {k: v.to(device) for k, v in batch["passage_tokens"].items()}
            group_size = int(batch["group_size"])
            targets = torch.arange(query_tokens["input_ids"].shape[0], device=device) * group_size

            with torch.autocast(device_type="cuda", dtype=amp_dtype, enabled=use_amp):
                query_embeddings = model.encode(query_tokens)
                passage_embeddings = model.encode(passage_tokens)
                scores = torch.matmul(query_embeddings, passage_embeddings.T) / args.temperature
                loss = F.cross_entropy(scores, targets) / args.grad_accum

            if scaler.is_enabled():
                scaler.scale(loss).backward()
            else:
                loss.backward()
            running_loss += float(loss.detach().cpu()) * args.grad_accum

            if step % args.grad_accum == 0 or step == len(loader):
                if scaler.is_enabled():
                    scaler.unscale_(optimizer)
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                    optimizer.step()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)
                global_step += 1

            if step % args.log_every == 0:
                progress.set_postfix(loss=f"{running_loss / step:.4f}", lr=scheduler.get_last_lr()[0])

        val_metrics = evaluate(
            model,
            tokenizer,
            data_dir,
            "val",
            args.eval_batch_size,
            args.max_query_len,
            args.max_passage_len,
            device,
        )
        epoch_record = {
            "epoch": epoch,
            "train_loss": running_loss / max(1, len(loader)),
            "val": val_metrics,
            "global_step": global_step,
        }
        history.append(epoch_record)
        print(json.dumps(epoch_record, ensure_ascii=False, indent=2))

        score = val_metrics["mrr@20"]
        if score > best_val:
            best_val = score
            best_epoch = epoch
            best_dir = output_dir / "best"
            model.save_pretrained(best_dir)
            tokenizer.save_pretrained(best_dir)
            write_json(best_dir / "training_selection.json", epoch_record)
            print(f"saved best checkpoint: {best_dir}")

    best_dir = output_dir / "best"
    if best_dir.exists():
        tokenizer = AutoTokenizer.from_pretrained(best_dir)
        model = E5BiEncoder(str(best_dir)).to(device)

    test_metrics = evaluate(
        model,
        tokenizer,
        data_dir,
        "test",
        args.eval_batch_size,
        args.max_query_len,
        args.max_passage_len,
        device,
    )
    final_report = {
        "model_name_or_path": args.model_name_or_path,
        "best_epoch": best_epoch,
        "best_val_mrr@20": best_val,
        "test": test_metrics,
        "history": history,
        "elapsed_seconds": round(time.perf_counter() - start_time, 2),
        "output_dir": str(output_dir),
        "best_dir": str(best_dir),
    }
    write_json(output_dir / "training_report.json", final_report)
    print(json.dumps(final_report, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/workspace/e5_finetune_llm_v1/data")
    parser.add_argument("--output-dir", default="/workspace/e5_finetune_llm_v1/outputs/e5-small-kho-llm-v1")
    parser.add_argument("--model-name-or-path", default="intfloat/multilingual-e5-small")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--eval-batch-size", type=int, default=64)
    parser.add_argument("--grad-accum", type=int, default=4)
    parser.add_argument("--negatives", type=int, default=3)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--temperature", type=float, default=0.02)
    parser.add_argument("--warmup-ratio", type=float, default=0.05)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--max-query-len", type=int, default=160)
    parser.add_argument("--max-passage-len", type=int, default=512)
    parser.add_argument("--max-grad-norm", type=float, default=1.0)
    parser.add_argument("--log-every", type=int, default=10)
    parser.add_argument("--num-workers", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260503)
    parser.add_argument("--fp16", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--bf16", action="store_true")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--max-train-pairs", type=int, default=0)
    return parser.parse_args()


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    train(parse_args())
