from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import build_paths, write_json
from train_lightgbm.reranker_common import read_jsonl


def _configure_threads(threads: int) -> dict:
    if threads <= 0:
        threads = os.cpu_count() or 1
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = str(threads)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    import torch

    torch.set_num_threads(threads)
    torch.set_num_interop_threads(min(4, threads))
    return {"threads": torch.get_num_threads(), "interop_threads": torch.get_num_interop_threads()}


def _select_device(device: str):
    import torch

    requested = (device or "auto").lower()
    if requested == "auto":
        requested = "cuda" if torch.cuda.is_available() else "cpu"
    selected = torch.device(requested)
    if selected.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA device requested but torch.cuda.is_available() is false.")
    return selected


def _sample_rows(rows: list[dict], max_rows: int | None, seed: int) -> list[dict]:
    if max_rows is None or max_rows <= 0 or len(rows) <= max_rows:
        return rows
    positives = [row for row in rows if float(row.get("label", 0.0)) >= 0.999]
    rest = [row for row in rows if float(row.get("label", 0.0)) < 0.999]
    rng = random.Random(seed)
    rng.shuffle(rest)
    selected = positives + rest[: max(0, max_rows - len(positives))]
    rng.shuffle(selected)
    return selected[:max_rows]


def _pearson(preds: list[float], labels: list[float]) -> float:
    if len(preds) < 2:
        return 0.0
    mp = sum(preds) / len(preds)
    ml = sum(labels) / len(labels)
    num = sum((p - mp) * (l - ml) for p, l in zip(preds, labels))
    den_p = math.sqrt(sum((p - mp) ** 2 for p in preds))
    den_l = math.sqrt(sum((l - ml) ** 2 for l in labels))
    return num / max(den_p * den_l, 1e-12)


def _make_loader(rows: list[dict], tokenizer, max_length: int, batch_size: int, shuffle: bool, seed: int):
    import torch
    from torch.utils.data import DataLoader

    if shuffle:
        rng = random.Random(seed)
        rows = list(rows)
        rng.shuffle(rows)

    def collate(batch: list[dict]) -> dict:
        encoded = tokenizer(
            [row["query"] for row in batch],
            [row["candidate"] for row in batch],
            padding=True,
            truncation=True,
            max_length=max_length,
            return_tensors="pt",
        )
        labels = torch.tensor([float(row["label"]) for row in batch], dtype=torch.float32)
        encoded["labels"] = labels
        return encoded

    return DataLoader(rows, batch_size=batch_size, shuffle=False, collate_fn=collate)


def _evaluate(model, loader, device, use_amp: bool = False) -> dict:
    import torch

    model.eval()
    preds: list[float] = []
    labels: list[float] = []
    total_loss = 0.0
    total = 0
    loss_fn = torch.nn.MSELoss()
    with torch.no_grad():
        for batch in loader:
            y = batch.pop("labels").to(device)
            batch = {key: value.to(device) for key, value in batch.items()}
            with torch.cuda.amp.autocast(enabled=use_amp):
                out = model(**batch)
            logits = out.logits.view(-1).float()
            loss = loss_fn(logits, y)
            total_loss += float(loss.item()) * len(y)
            total += len(y)
            preds.extend(float(v) for v in logits.detach().cpu().tolist())
            labels.extend(float(v) for v in y.detach().cpu().tolist())
    return {
        "loss": total_loss / max(total, 1),
        "pearson": _pearson(preds, labels),
        "rows": total,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fine-tune a lightweight multilingual CrossEncoder reranker on KIE candidates.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--dataset-subdir", default="reranker_mminilm")
    parser.add_argument("--model-name", default="cross-encoder/mmarco-mMiniLMv2-L12-H384-v1")
    parser.add_argument("--output-name", default="mminilm_kie_reranker_dryrun")
    parser.add_argument("--max-length", type=int, default=384)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--max-train-rows", type=int, default=2500)
    parser.add_argument("--max-val-rows", type=int, default=800)
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"], help="Training device. auto uses CUDA when available.")
    parser.add_argument("--fp16", action="store_true", help="Use CUDA fp16 autocast during training/evaluation.")
    parser.add_argument("--patience", type=int, default=0, help="Early-stop after this many non-improving epochs. 0 disables early stopping.")
    parser.add_argument("--min-delta", type=float, default=1e-5, help="Minimum val loss improvement for best checkpoint.")
    parser.add_argument("--eval-before-train", action="store_true", help="Evaluate and save the initial model before additional fine-tuning.")
    parser.add_argument("--seed", type=int, default=1337)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    thread_info = _configure_threads(args.threads)
    import torch
    from torch.optim import AdamW
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.manual_seed(args.seed)
    device = _select_device(args.device)
    use_amp = bool(args.fp16 and device.type == "cuda")
    paths = build_paths(args.project_root)
    dataset_dir = paths.exports_root / args.dataset_subdir
    train_rows = _sample_rows(read_jsonl(dataset_dir / "train.jsonl"), args.max_train_rows, args.seed)
    val_rows = _sample_rows(read_jsonl(dataset_dir / "val.jsonl"), args.max_val_rows, args.seed + 1)
    print(f"[reranker_train] train_rows={len(train_rows)} val_rows={len(val_rows)} threads={thread_info} device={device} fp16={use_amp}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    model = AutoModelForSequenceClassification.from_pretrained(args.model_name, num_labels=1)
    model.config.problem_type = "regression"
    model.to(device)
    train_loader = _make_loader(train_rows, tokenizer, args.max_length, args.batch_size, True, args.seed)
    val_loader = _make_loader(val_rows, tokenizer, args.max_length, args.batch_size, False, args.seed)
    optimizer = AdamW(model.parameters(), lr=args.lr)
    loss_fn = torch.nn.MSELoss()
    scaler = torch.cuda.amp.GradScaler(enabled=use_amp)
    history = []
    best_val = None
    bad_epochs = 0
    out_dir = paths.models_root / "rerankers" / args.output_name
    t0 = time.perf_counter()
    if args.eval_before_train:
        val_metrics = _evaluate(model, val_loader, device, use_amp)
        history.append({"epoch": 0, "train": {"loss": None, "rows": 0}, "val": val_metrics})
        best_val = val_metrics["loss"]
        out_dir.mkdir(parents=True, exist_ok=True)
        model.save_pretrained(out_dir)
        tokenizer.save_pretrained(out_dir)
        print(json.dumps(history[-1], ensure_ascii=False), flush=True)
    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss = 0.0
        total = 0
        for step, batch in enumerate(train_loader, start=1):
            y = batch.pop("labels").to(device)
            batch = {key: value.to(device) for key, value in batch.items()}
            optimizer.zero_grad(set_to_none=True)
            with torch.cuda.amp.autocast(enabled=use_amp):
                out = model(**batch)
                logits = out.logits.view(-1).float()
                loss = loss_fn(logits, y)
            if use_amp:
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
            else:
                loss.backward()
                optimizer.step()
            total_loss += float(loss.item()) * len(y)
            total += len(y)
            if step % 50 == 0:
                print(f"[reranker_train] epoch={epoch} step={step} loss={total_loss / max(total, 1):.6f}", flush=True)
        train_metrics = {"loss": total_loss / max(total, 1), "rows": total}
        val_metrics = _evaluate(model, val_loader, device, use_amp)
        epoch_report = {"epoch": epoch, "train": train_metrics, "val": val_metrics}
        history.append(epoch_report)
        print(json.dumps(epoch_report, ensure_ascii=False), flush=True)
        improved = best_val is None or val_metrics["loss"] < (best_val - args.min_delta)
        if improved:
            best_val = val_metrics["loss"]
            bad_epochs = 0
            out_dir.mkdir(parents=True, exist_ok=True)
            model.save_pretrained(out_dir)
            tokenizer.save_pretrained(out_dir)
        else:
            bad_epochs += 1
            if args.patience > 0 and bad_epochs >= args.patience:
                print(
                    f"[reranker_train] early_stop epoch={epoch} bad_epochs={bad_epochs} best_val_loss={best_val:.6f}",
                    flush=True,
                )
                break
    elapsed = time.perf_counter() - t0
    report = {
        "model_name": args.model_name,
        "output_dir": str(out_dir),
        "dataset_dir": str(dataset_dir),
        "thread_info": thread_info,
        "device": str(device),
        "fp16": use_amp,
        "max_length": args.max_length,
        "batch_size": args.batch_size,
        "epochs": args.epochs,
        "patience": args.patience,
        "min_delta": args.min_delta,
        "eval_before_train": args.eval_before_train,
        "lr": args.lr,
        "train_rows": len(train_rows),
        "val_rows": len(val_rows),
        "elapsed_sec": elapsed,
        "history": history,
    }
    write_json(out_dir / "train_report.json", report)
    write_json(paths.reports_root / f"{args.output_name}_train_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
