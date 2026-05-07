# Archive Embedder: E5-small Mix50 v2

Current Kho semantic backend:

```text
models/archive_models/e5-small-mix50-v2-onnx-fp32/
```

Runtime constants:

```text
EMBEDDING_MODEL   = intfloat/multilingual-e5-small
EMBEDDING_VERSION = 2.0-e5-small-mix50-v2-onnx-fp32
EMBEDDING_DIM     = 384
```

## Why E5-small

The goal is CPU desktop semantic search with a small portable dependency set.
E5-small was selected because it is much faster than 1024-dim Vietnamese
embedding models while still giving acceptable retrieval after fine-tuning.

Local comparison on `temp/embedding_eval_batch0001_0006_0027`:

| Model | Dim | Doc encode | Query encode | R@1 | R@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| AITeamVN/Vietnamese_Embedding ONNX FP32 | 1024 | `~425s / 350 docs` | `~41s / 500 queries` | `0.672` | `0.904` |
| multilingual-e5-small ONNX FP32 | 384 | `~50s / 350 docs` | `~4s / 500 queries` | `0.662` | `0.886` |
| fine-tuned E5 Kho ONNX FP32 | 384 | `~58s / 350 docs` | `~4.7s / 500 queries` | `0.724` | `0.920` |

HaLong/Vietnamese alternatives were discussed, but no retained HaLong benchmark
artifact was found in this repo. It should be rebenchmarked before being treated
as a candidate.

## Dataset

The latest selected dataset is `mix50_v2`:

```text
temp/e5_finetune_mix50_v2/data/
temp/e5_finetune_mix50_v2/reports/mixed_dataset_report.json
```

Summary:

```text
Kho train pairs: 10924
Public train pairs: 10924
Total train pairs: 21848
Corpus docs: 16718
Val queries: 1000
Test queries: 1000
Negatives per query: 3
```

The public half mixes Vietnamese retrieval/legal data; the Kho half comes from
internal OCR archive documents and LLM-generated natural search questions.
Queries are diversified so the model sees both "Tim tai lieu..." style and more
natural/bare phrasings.

## Train

```bash
cd /workspace/e5_finetune_mix50_v2
python3 runpod/train_e5_small.py --data-dir data --output-dir outputs/e5-small-mix50-v2 --epochs 2 --batch-size 32 --grad-accum 4 --negatives 3 --fp16
```

Best selected epoch was `1`.

## Export ONNX FP32

```bash
python3 runpod/export_onnx_fp32.py --model-dir outputs/e5-small-mix50-v2/best --output-dir outputs/e5-small-mix50-v2-onnx-fp32
```

The export keeps the transformer backbone in ONNX and runtime does mean pooling
plus L2 normalization. ONNX parity was effectively exact:

```text
cosine mean: 0.99999994
cosine min: 0.99999988
```

## Evaluate

```bash
python3 runpod/evaluate_onnx_retrieval.py --onnx-dir outputs/e5-small-mix50-v2-onnx-fp32 --data-dir data --split test
```

RunPod summary:

```text
Mixed training test: R@1 53.7%, R@5 76.7%, R@10 82.8%, MRR@20 64.05%
semantic_v2_test_389: R@1 69.92%, R@5 84.83%, R@10 91.0%
```

Public VN-MTEB-style validation dropped versus base E5 on several non-Kho
datasets, so this model is selected for the internal Kho product, not as a
general-purpose Vietnamese retrieval model.

## Files Here

```text
train/mix50_v2/prep_mixed_e5_dataset.py
train/mix50_v2/train_e5_small.py
train/mix50_v2/evaluate_onnx_retrieval.py
convert/export_e5_mix50_to_onnx_fp32.py
```
