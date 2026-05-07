# LayoutLMv3 Font/Gray Normalized Style Experiment

Independent LayoutLMv3 branch that keeps the first LayoutLMv3 and LightGBM
outputs untouched.

This branch maps OCR style metadata from canonical JSON by exact `word_id`:

- `font_size`
- `fg_gray`
- `word_height`
- `confidence`
- `content_type`

Training feeds the derived categorical style id through LayoutLMv3
`token_type_ids`. `font_size`, `fg_gray`, and `word_height` are bucketed relative
to page medians before the style id is built. It does not feed page images and
does not append extra style tokens to OCR text, so sequence length and CPU
latency should stay close to the normal LayoutLMv3 branch.

Build local dataset:

```powershell
python train_layoutlmv3_style/1-build_dataset.py --source-root D:\tmp\Train_20260413_143844_kie\json_output_labeled --project-root D:\tmp\Train_20260413_143844_LayoutLMv3_fontgray_norm
python train_layoutlmv3_style/2-sanity_check.py --project-root D:\tmp\Train_20260413_143844_LayoutLMv3_fontgray_norm
```

Train on RunPod:

```bash
python /workspace/ocrtool/train_layoutlmv3_style/3-train_layoutlmv3_style.py \
  --project-root /workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm \
  --model-name microsoft/layoutlmv3-base \
  --output-dir /workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm/models/layoutlmv3_fontgray_norm_run1 \
  --epochs 20 \
  --batch-size 4 \
  --eval-batch-size 4 \
  --loss weighted_ce \
  --boundary-token-weight 1.5
```

Evaluate/export:

```bash
python /workspace/ocrtool/train_layoutlmv3_style/4-evaluate_layoutlmv3_style.py \
  --project-root /workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm \
  --model-path /workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm/models/layoutlmv3_fontgray_norm_run1

python /workspace/ocrtool/train_layoutlmv3_style/6-export_onnx.py \
  --project-root /workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm \
  --model-path /workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm/models/layoutlmv3_fontgray_norm_run1 \
  --onnx-threads 9
```
