# LayoutLMv3 Fontgray Norm Final Epoch25

This folder keeps the current KIE train/export code that produced the model
loaded by default in `kie_engine.py`:

```text
models/layoutlmv3_fontgray_norm_final_epoch25/layoutlmv3_fontgray_norm_final_epoch25.int8.onnx
```

Code is packaged as importable folders:

```text
train_layoutlmv3_style/   # current font/gray/linebucket pipeline
train_layoutlmv3/         # base dependency used by the style pipeline
```

Plain LayoutLMv3 base and visual-only experiment were moved to
`temp/legacy_model_train_20260504/`.

## Why This Model

It keeps the default runtime text/layout-only, but adds OCR-derived style and
line-position signals:

- `font_size`
- `fg_gray`
- `word_height`
- `confidence`
- `content_type`
- line bucket parsed from `line_id`

The combined style/line id is fed through `token_type_ids`. The default model
does not use rendered page images, so it is much faster than the visual variant.

Key results:

```text
Test word F1: 0.9780
Test span F1: 0.9425
Test exact: 0.9225
Batch0027 word F1: 0.9926
Batch0027 span F1: 0.9871
Batch0027 exact: 0.9810
FINAL10 aggregate exact over 1729 docs / 3661 pages: ~0.9840
CPU INT8 speed: ~658-729 ms/page depending benchmark set
```

Visual variant result on batches 0001/0006/0027 tied no-image exact (`0.9859`)
but took `~1619 ms/page` vs no-image `~775 ms/page`, so visual is not the
default.

## Build Dataset

```powershell
.\.venv_build\Scripts\python.exe train-convert\layoutlmv3\train\fontgray_norm_final_epoch25\train_layoutlmv3_style\1-build_dataset.py `
  --source-root D:\tmp\Train_20260413_143844_kie\json_output_labeled `
  --project-root D:\tmp\Train_20260413_143844_LayoutLMv3_fontgray_norm

.\.venv_build\Scripts\python.exe train-convert\layoutlmv3\train\fontgray_norm_final_epoch25\train_layoutlmv3_style\2-sanity_check.py `
  --project-root D:\tmp\Train_20260413_143844_LayoutLMv3_fontgray_norm
```

The builder writes:

```text
<PROJECT_ROOT>/exports/dataset/train.jsonl
<PROJECT_ROOT>/exports/dataset/val.jsonl
<PROJECT_ROOT>/exports/dataset/test.jsonl
```

## Train

Typical RunPod command:

```bash
python /workspace/ocrtool/train-convert/layoutlmv3/train/fontgray_norm_final_epoch25/train_layoutlmv3_style/3-train_layoutlmv3_style.py \
  --project-root /workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm \
  --model-name microsoft/layoutlmv3-base \
  --output-dir /workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm/models/layoutlmv3_fontgray_norm_run1 \
  --epochs 25 \
  --batch-size 4 \
  --eval-batch-size 4 \
  --loss weighted_ce \
  --boundary-token-weight 1.5
```

## Evaluate And Export

```bash
python /workspace/ocrtool/train-convert/layoutlmv3/train/fontgray_norm_final_epoch25/train_layoutlmv3_style/4-evaluate_layoutlmv3_style.py \
  --project-root /workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm \
  --model-path /workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm/models/layoutlmv3_fontgray_norm_run1

python /workspace/ocrtool/train-convert/layoutlmv3/train/fontgray_norm_final_epoch25/train_layoutlmv3_style/6-export_onnx.py \
  --project-root /workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm \
  --model-path /workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm/models/layoutlmv3_fontgray_norm_run1 \
  --onnx-threads 9
```

Copy exported result into:

```text
models/layoutlmv3_fontgray_norm_final_epoch25/
```
