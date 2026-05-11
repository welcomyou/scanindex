from __future__ import annotations

import argparse
import inspect
import json
from collections import Counter, defaultdict
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import torch
from torch import nn
from transformers import (
    AutoConfig,
    AutoModelForTokenClassification,
    AutoTokenizer,
    DataCollatorForTokenClassification,
    EarlyStoppingCallback,
    Trainer,
    TrainingArguments,
)

from train_layoutlmv3_style.common import (
    LABEL_LIST,
    LINE_EXACT_PREFIX_BUCKETS,
    LINE_POSITION_BUCKET_COUNT,
    STYLE_BASE_TYPE_VOCAB_SIZE,
    STYLE_TYPE_VOCAB_SIZE,
    compute_kie_metrics,
    ensure_project_dirs,
    label_counts,
    load_dataset_split,
    now_iso,
    write_json,
    write_style_report,
)
from train_layoutlmv3_style.hf_utils import StyleTokenizedPageDataset, aggregate_logits_to_words, softmax_np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fine-tune LayoutLMv3-base with OCR font/gray token_type style features.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model-name", default="microsoft/layoutlmv3-base")
    parser.add_argument("--output-dir", help="Default: <project-root>/models/layoutlmv3_fontgray_norm_run1")
    parser.add_argument("--epochs", type=float, default=20)
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--eval-batch-size", type=int, default=4)
    parser.add_argument("--gradient-accumulation", type=int, default=1)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    parser.add_argument("--subword-label-strategy", choices=["same", "first"], default="same")
    parser.add_argument("--loss", choices=["ce", "weighted_ce", "focal"], default="weighted_ce")
    parser.add_argument("--focal-gamma", type=float, default=2.0)
    parser.add_argument(
        "--o-label-weight-floor",
        type=float,
        default=0.0,
        help="Optional floor for the normalized O-label class weight; useful to reduce over-predicted KIE spans.",
    )
    parser.add_argument("--positive-token-weight", type=float, default=1.0)
    parser.add_argument("--b-token-weight", type=float, default=1.25)
    parser.add_argument("--boundary-token-weight", type=float, default=1.5)
    parser.add_argument(
        "--extra-field-penalty",
        type=float,
        default=0.0,
        help="Soft penalty for assigning any KIE field probability to gold-O tokens.",
    )
    parser.add_argument(
        "--missing-field-penalty",
        type=float,
        default=0.0,
        help="Soft penalty for assigning O probability to gold-KIE tokens.",
    )
    parser.add_argument(
        "--wrong-field-penalty",
        type=float,
        default=0.0,
        help="Soft penalty for assigning another KIE field probability to gold-KIE tokens.",
    )
    parser.add_argument("--hard-example-jsonl", action="append")
    parser.add_argument("--hard-example-repeat", type=int, default=1)
    parser.add_argument("--focus-fields", nargs="*", default=[])
    parser.add_argument("--focus-repeat", type=int, default=1)
    parser.add_argument("--dataloader-num-workers", type=int, default=0)
    parser.add_argument("--early-stopping-patience", type=int, default=5)
    parser.add_argument(
        "--metric-for-best-model",
        default="exact_instance_accuracy",
        choices=[
            "exact_instance_accuracy",
            "span_f1",
            "word_f1",
            "span_precision",
            "span_recall",
            "word_precision",
            "word_recall",
        ],
        help="Validation metric used by Trainer to keep the best checkpoint.",
    )
    parser.add_argument("--save-total-limit", type=int, default=2)
    parser.add_argument(
        "--save-only-model",
        action="store_true",
        help="Save model-only checkpoints without optimizer/scheduler state to reduce disk usage.",
    )
    parser.add_argument("--save-steps", type=int, default=0, help="0 = save/evaluate once per epoch.")
    parser.add_argument("--eval-steps", type=int, default=0, help="Only used when --save-steps > 0.")
    parser.add_argument("--limit-train-docs", type=int)
    parser.add_argument("--limit-eval-docs", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-steps", type=int, default=-1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--resume-from-checkpoint",
        help="Resume Trainer state from a checkpoint directory. Use with --epochs as the total target epoch count.",
    )
    return parser.parse_args()


def prediction_metrics_fn(eval_dataset: StyleTokenizedPageDataset, id2label: dict[int, str]):
    def compute(eval_pred) -> dict[str, float]:
        logits = eval_pred.predictions[0] if isinstance(eval_pred.predictions, tuple) else eval_pred.predictions
        pred_labels, pred_scores = aggregate_logits_to_words(eval_dataset.rows, eval_dataset, np.asarray(logits), id2label)
        metrics = compute_kie_metrics(eval_dataset.rows, pred_labels, pred_scores)
        return {
            "word_f1": metrics["word"]["overall"]["f1"],
            "word_precision": metrics["word"]["overall"]["precision"],
            "word_recall": metrics["word"]["overall"]["recall"],
            "span_f1": metrics["span"]["overall"]["f1"],
            "span_precision": metrics["span"]["overall"]["precision"],
            "span_recall": metrics["span"]["overall"]["recall"],
            "exact_instance_accuracy": metrics["exact_instance_accuracy"],
            "missing_word_rate": metrics["missing_word_rate"],
            "extra_word_rate": metrics["extra_word_rate"],
        }

    return compute


def class_weights_from_rows(rows: list[dict[str, Any]], o_label_weight_floor: float = 0.0) -> torch.Tensor:
    counts = label_counts(rows)
    total = sum(counts.values())
    weights = []
    for label in LABEL_LIST:
        count = max(1, counts.get(label, 0))
        weight = (total / max(1, len(LABEL_LIST) * count)) ** 0.5
        if label == "O":
            weight = min(weight, 1.0)
        weights.append(weight)
    arr = np.asarray(weights, dtype=np.float32)
    arr = arr / max(arr.mean(), 1e-6)
    if o_label_weight_floor > 0:
        arr[LABEL_LIST.index("O")] = max(float(arr[LABEL_LIST.index("O")]), float(o_label_weight_floor))
    return torch.tensor(arr, dtype=torch.float32)


def _field_id_tensor(labels: torch.Tensor) -> torch.Tensor:
    field_ids = torch.full_like(labels, -1)
    positive = labels > 0
    field_ids[positive] = (labels[positive] - 1) // 2
    return field_ids


def token_region_weights(labels: torch.Tensor, positive_weight: float, b_weight: float, boundary_weight: float) -> torch.Tensor:
    valid = labels != -100
    weights = torch.ones(labels.shape, dtype=torch.float32, device=labels.device)
    if positive_weight != 1.0:
        weights = torch.where(valid & (labels > 0), weights * float(positive_weight), weights)
    if b_weight != 1.0:
        is_b = valid & (labels > 0) & ((labels % 2) == 1)
        weights = torch.where(is_b, torch.maximum(weights, torch.full_like(weights, float(b_weight))), weights)
    if boundary_weight != 1.0:
        field_ids = _field_id_tensor(labels)
        prev_field = torch.roll(field_ids, shifts=1, dims=1)
        next_field = torch.roll(field_ids, shifts=-1, dims=1)
        prev_valid = torch.roll(valid, shifts=1, dims=1)
        next_valid = torch.roll(valid, shifts=-1, dims=1)
        prev_valid[:, 0] = False
        next_valid[:, -1] = False
        boundary = valid & (labels > 0) & (
            (~prev_valid | (prev_field != field_ids)) | (~next_valid | (next_field != field_ids))
        )
        weights = torch.where(boundary, torch.maximum(weights, torch.full_like(weights, float(boundary_weight))), weights)
    return torch.where(valid, weights, torch.zeros_like(weights))


def field_constraint_penalty(
    logits: torch.Tensor,
    labels: torch.Tensor,
    extra_penalty: float,
    missing_penalty: float,
    wrong_penalty: float,
) -> torch.Tensor:
    if extra_penalty <= 0 and missing_penalty <= 0 and wrong_penalty <= 0:
        return logits.new_tensor(0.0)

    probs = torch.softmax(logits, dim=-1)
    valid = labels != -100
    gold_o = valid & (labels == 0)
    gold_kie = valid & (labels > 0)
    loss = logits.new_tensor(0.0)

    if extra_penalty > 0 and bool(gold_o.any()):
        kie_prob = probs[..., 1:].sum(dim=-1)
        loss = loss + float(extra_penalty) * kie_prob[gold_o].mean()

    if missing_penalty > 0 and bool(gold_kie.any()):
        loss = loss + float(missing_penalty) * probs[..., 0][gold_kie].mean()

    if wrong_penalty > 0 and bool(gold_kie.any()):
        field_ids = _field_id_tensor(labels).clamp_min(0)
        same_b_ids = 1 + (field_ids * 2)
        same_i_ids = same_b_ids + 1
        same_b_prob = probs.gather(-1, same_b_ids.unsqueeze(-1)).squeeze(-1)
        same_i_prob = probs.gather(-1, same_i_ids.unsqueeze(-1)).squeeze(-1)
        same_field_prob = same_b_prob + same_i_prob
        any_kie_prob = probs[..., 1:].sum(dim=-1)
        wrong_field_prob = (any_kie_prob - same_field_prob).clamp_min(0.0)
        loss = loss + float(wrong_penalty) * wrong_field_prob[gold_kie].mean()

    return loss


class WeightedLossTrainer(Trainer):
    def __init__(
        self,
        *args,
        class_weights: torch.Tensor | None = None,
        loss_name: str = "ce",
        focal_gamma: float = 2.0,
        positive_token_weight: float = 1.0,
        b_token_weight: float = 1.0,
        boundary_token_weight: float = 1.0,
        extra_field_penalty: float = 0.0,
        missing_field_penalty: float = 0.0,
        wrong_field_penalty: float = 0.0,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.class_weights = class_weights
        self.loss_name = loss_name
        self.focal_gamma = focal_gamma
        self.positive_token_weight = positive_token_weight
        self.b_token_weight = b_token_weight
        self.boundary_token_weight = boundary_token_weight
        self.extra_field_penalty = extra_field_penalty
        self.missing_field_penalty = missing_field_penalty
        self.wrong_field_penalty = wrong_field_penalty

    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
        labels = inputs.pop("labels")
        outputs = model(**inputs)
        logits = outputs.logits
        weight = self.class_weights.to(logits.device) if self.class_weights is not None and self.loss_name != "ce" else None
        token_weights = token_region_weights(
            labels,
            positive_weight=self.positive_token_weight,
            b_weight=self.b_token_weight,
            boundary_weight=self.boundary_token_weight,
        ).view(-1)
        valid = labels.view(-1) != -100
        loss_fct = nn.CrossEntropyLoss(weight=weight, ignore_index=-100, reduction="none")
        flat_loss = loss_fct(logits.view(-1, logits.shape[-1]), labels.view(-1))
        if self.loss_name == "focal":
            with torch.no_grad():
                pt = torch.exp(-flat_loss)
            flat_loss = (1 - pt) ** self.focal_gamma * flat_loss
        weighted = (flat_loss * token_weights)[valid]
        loss = weighted.sum() / token_weights[valid].sum().clamp_min(1.0)
        loss = loss + field_constraint_penalty(
            logits,
            labels,
            extra_penalty=self.extra_field_penalty,
            missing_penalty=self.missing_field_penalty,
            wrong_penalty=self.wrong_field_penalty,
        )
        return (loss, outputs) if return_outputs else loss


def error_doc_ids(paths: list[str] | None) -> set[str]:
    doc_ids: set[str] = set()
    for raw_path in paths or []:
        path = Path(raw_path)
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for side in ("gold", "pred"):
                    item = payload.get(side)
                    if isinstance(item, dict) and item.get("doc_id"):
                        doc_ids.add(str(item["doc_id"]))
    return doc_ids


def row_has_focus_field(row: dict[str, Any], focus_fields: set[str]) -> bool:
    return any("-" in label and label.split("-", 1)[1] in focus_fields for label in row.get("labels", []))


def oversample_rows(rows: list[dict[str, Any]], hard_doc_ids: set[str], hard_repeat: int, focus_fields: set[str], focus_repeat: int) -> list[dict[str, Any]]:
    out = list(rows)
    if hard_doc_ids and hard_repeat > 1:
        hard_rows = [row for row in rows if str(row.get("doc_id")) in hard_doc_ids]
        for _ in range(hard_repeat - 1):
            out.extend(hard_rows)
    if focus_fields and focus_repeat > 1:
        focus_rows = [row for row in rows if row_has_focus_field(row, focus_fields)]
        for _ in range(focus_repeat - 1):
            out.extend(focus_rows)
    return out


def training_arguments(args: argparse.Namespace, output_dir: Path, fp16: bool, bf16: bool) -> TrainingArguments:
    kwargs = {
        "output_dir": str(output_dir),
        "num_train_epochs": args.epochs,
        "learning_rate": args.learning_rate,
        "per_device_train_batch_size": args.batch_size,
        "per_device_eval_batch_size": args.eval_batch_size,
        "gradient_accumulation_steps": args.gradient_accumulation,
        "logging_steps": 25,
        "save_total_limit": getattr(args, "save_total_limit", 2),
        "load_best_model_at_end": True,
        "metric_for_best_model": getattr(args, "metric_for_best_model", "span_f1"),
        "greater_is_better": getattr(args, "greater_is_better", True),
        "seed": args.seed,
        "fp16": fp16,
        "bf16": bf16,
        "max_steps": args.max_steps,
        "report_to": [],
        "remove_unused_columns": False,
    }
    params = inspect.signature(TrainingArguments.__init__).parameters
    dataloader_num_workers = int(getattr(args, "dataloader_num_workers", 0) or 0)
    if "dataloader_num_workers" in params:
        kwargs["dataloader_num_workers"] = dataloader_num_workers
    if dataloader_num_workers > 0 and "dataloader_prefetch_factor" in params:
        kwargs["dataloader_prefetch_factor"] = 2
    if dataloader_num_workers > 0 and "dataloader_persistent_workers" in params:
        kwargs["dataloader_persistent_workers"] = True
    if getattr(args, "save_only_model", False) and "save_only_model" in params:
        kwargs["save_only_model"] = True
    save_steps = int(getattr(args, "save_steps", 0) or 0)
    eval_steps = int(getattr(args, "eval_steps", 0) or 0)
    if save_steps > 0:
        kwargs["save_strategy"] = "steps"
        kwargs["save_steps"] = save_steps
        if "eval_strategy" in params:
            kwargs["eval_strategy"] = "steps"
        else:
            kwargs["evaluation_strategy"] = "steps"
        kwargs["eval_steps"] = eval_steps if eval_steps > 0 else save_steps
    else:
        kwargs["save_strategy"] = "epoch"
        if "eval_strategy" in params:
            kwargs["eval_strategy"] = "epoch"
        else:
            kwargs["evaluation_strategy"] = "epoch"
    return TrainingArguments(**kwargs)


def trainer_tokenizer_kwargs(tokenizer: Any) -> dict[str, Any]:
    params = inspect.signature(Trainer.__init__).parameters
    if "processing_class" in params:
        return {"processing_class": tokenizer}
    if "tokenizer" in params:
        return {"tokenizer": tokenizer}
    return {}


def main() -> None:
    args = parse_args()
    dirs = ensure_project_dirs(args.project_root)
    output_dir = Path(args.output_dir) if args.output_dir else dirs["models"] / "layoutlmv3_fontgray_norm_run1"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        args.limit_train_docs = args.limit_train_docs or 20
        args.limit_eval_docs = args.limit_eval_docs or 20
        args.epochs = min(args.epochs, 1)
        args.max_steps = args.max_steps if args.max_steps and args.max_steps > 0 else 5

    train_rows = load_dataset_split(args.project_root, "train", limit_docs=args.limit_train_docs)
    val_rows = load_dataset_split(args.project_root, "val", limit_docs=args.limit_eval_docs)
    if not train_rows or not val_rows:
        raise SystemExit("Dataset is missing train/val rows. Run train_layoutlmv3_style/1-build_dataset.py first.")

    hard_doc_ids = error_doc_ids(args.hard_example_jsonl)
    focus_fields = {field for field in args.focus_fields if field}
    original_train_rows = len(train_rows)
    train_rows = oversample_rows(train_rows, hard_doc_ids, args.hard_example_repeat, focus_fields, args.focus_repeat)

    label2id = {label: idx for idx, label in enumerate(LABEL_LIST)}
    id2label = {idx: label for label, idx in label2id.items()}
    tokenizer = AutoTokenizer.from_pretrained(args.model_name, use_fast=True)
    config = AutoConfig.from_pretrained(
        args.model_name,
        num_labels=len(LABEL_LIST),
        id2label=id2label,
        label2id=label2id,
        type_vocab_size=STYLE_TYPE_VOCAB_SIZE,
    )
    model = AutoModelForTokenClassification.from_pretrained(
        args.model_name,
        config=config,
        ignore_mismatched_sizes=True,
    )

    train_dataset = StyleTokenizedPageDataset(
        train_rows,
        tokenizer,
        label2id,
        max_length=args.max_length,
        stride=args.stride,
        subword_label_strategy=args.subword_label_strategy,
    )
    val_dataset = StyleTokenizedPageDataset(
        val_rows,
        tokenizer,
        label2id,
        max_length=args.max_length,
        stride=args.stride,
        subword_label_strategy=args.subword_label_strategy,
    )
    label_freq = dict(label_counts(train_rows))
    write_json(dirs["reports"] / "train_label_frequency.json", label_freq)

    class_weights = (
        class_weights_from_rows(train_rows, o_label_weight_floor=args.o_label_weight_floor)
        if args.loss in {"weighted_ce", "focal"}
        else None
    )
    if class_weights is not None:
        write_json(
            dirs["reports"] / "class_weights.json",
            {label: float(class_weights[idx].item()) for label, idx in label2id.items()},
        )

    has_cuda = torch.cuda.is_available()
    bf16 = bool(has_cuda and torch.cuda.is_bf16_supported())
    fp16 = bool(has_cuda and not bf16)
    train_args = training_arguments(args, output_dir, fp16=fp16, bf16=bf16)
    collator = DataCollatorForTokenClassification(tokenizer=tokenizer, label_pad_token_id=-100)
    trainer = WeightedLossTrainer(
        model=model,
        args=train_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        **trainer_tokenizer_kwargs(tokenizer),
        data_collator=collator,
        compute_metrics=prediction_metrics_fn(val_dataset, id2label),
        callbacks=[EarlyStoppingCallback(early_stopping_patience=args.early_stopping_patience)],
        class_weights=class_weights,
        loss_name=args.loss,
        focal_gamma=args.focal_gamma,
        positive_token_weight=args.positive_token_weight,
        b_token_weight=args.b_token_weight,
        boundary_token_weight=args.boundary_token_weight,
        extra_field_penalty=args.extra_field_penalty,
        missing_field_penalty=args.missing_field_penalty,
        wrong_field_penalty=args.wrong_field_penalty,
    )

    train_result = trainer.train(resume_from_checkpoint=args.resume_from_checkpoint)
    trainer.save_model(output_dir)
    tokenizer.save_pretrained(output_dir)
    write_json(output_dir / "label_list.json", LABEL_LIST)
    write_json(
        output_dir / "layoutlmv3_fontgray_config.json",
        {
            "model_name": args.model_name,
            "label_list": LABEL_LIST,
            "max_length": args.max_length,
            "stride": args.stride,
            "subword_label_strategy": args.subword_label_strategy,
            "loss": args.loss,
            "o_label_weight_floor": args.o_label_weight_floor,
            "positive_token_weight": args.positive_token_weight,
            "b_token_weight": args.b_token_weight,
            "boundary_token_weight": args.boundary_token_weight,
            "extra_field_penalty": args.extra_field_penalty,
            "missing_field_penalty": args.missing_field_penalty,
            "wrong_field_penalty": args.wrong_field_penalty,
            "style_input": "token_type_ids",
            "style_base_type_vocab_size": STYLE_BASE_TYPE_VOCAB_SIZE,
            "line_position_bucket_count": LINE_POSITION_BUCKET_COUNT,
            "line_exact_prefix_buckets": LINE_EXACT_PREFIX_BUCKETS,
            "style_type_vocab_size": STYLE_TYPE_VOCAB_SIZE,
            "style_source_features": ["font_size", "fg_gray", "word_height", "line_ids"],
            "style_normalization": "font_size, fg_gray, and word_height are bucketed relative to page medians; line_ids are parsed into stable line-position buckets",
        },
    )
    val_metrics = trainer.evaluate()
    summary = {
        "created_at": now_iso(),
        "output_dir": str(output_dir.resolve()),
        "config": {
            "model_name": args.model_name,
            "epochs": args.epochs,
            "learning_rate": args.learning_rate,
            "batch_size": args.batch_size,
            "eval_batch_size": args.eval_batch_size,
            "gradient_accumulation": args.gradient_accumulation,
            "max_length": args.max_length,
            "stride": args.stride,
            "subword_label_strategy": args.subword_label_strategy,
            "loss": args.loss,
            "o_label_weight_floor": args.o_label_weight_floor,
            "positive_token_weight": args.positive_token_weight,
            "b_token_weight": args.b_token_weight,
            "boundary_token_weight": args.boundary_token_weight,
            "extra_field_penalty": args.extra_field_penalty,
            "missing_field_penalty": args.missing_field_penalty,
            "wrong_field_penalty": args.wrong_field_penalty,
            "style_base_type_vocab_size": STYLE_BASE_TYPE_VOCAB_SIZE,
            "line_position_bucket_count": LINE_POSITION_BUCKET_COUNT,
            "line_exact_prefix_buckets": LINE_EXACT_PREFIX_BUCKETS,
            "style_type_vocab_size": STYLE_TYPE_VOCAB_SIZE,
            "hard_example_jsonl": args.hard_example_jsonl,
            "hard_example_repeat": args.hard_example_repeat,
            "hard_example_doc_count": len(hard_doc_ids),
            "focus_fields": sorted(focus_fields),
            "focus_repeat": args.focus_repeat,
            "fp16": fp16,
            "bf16": bf16,
            "dry_run": args.dry_run,
            "resume_from_checkpoint": args.resume_from_checkpoint,
        },
        "train_rows_original": original_train_rows,
        "train_rows": len(train_rows),
        "val_rows": len(val_rows),
        "train_chunks": len(train_dataset),
        "val_chunks": len(val_dataset),
        "train_result": train_result.metrics,
        "best_metrics": val_metrics,
        "label_frequency": label_freq,
    }
    write_json(dirs["reports"] / "training_summary.json", summary)
    write_style_report(args.project_root)
    print(json.dumps({"output_dir": str(output_dir), "val_metrics": val_metrics}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
