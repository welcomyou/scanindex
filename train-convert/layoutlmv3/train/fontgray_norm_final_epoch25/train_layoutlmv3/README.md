# LayoutLMv3 KIE experiment

Independent LayoutLMv3 token-classification pipeline for the labeled KIE JSON project.

This code does not modify `train_kie` or `train_lightgbm`. It reads:

- labeled JSON: `D:\tmp\Train_20260413_143844_kie\json_output_labeled`
- matching label inputs: sibling `json_input`
- canonical OCR JSON: `source_canonical_json` inside each label input or manifest

Expected commands:

```powershell
python train_layoutlmv3/1-build_dataset.py --source-root D:\tmp\Train_20260413_143844_kie\json_output_labeled --project-root D:\tmp\Train_20260413_143844_LayoutLMv3
python train_layoutlmv3/2-sanity_check.py --project-root D:\tmp\Train_20260413_143844_LayoutLMv3
python train_layoutlmv3/3-train_layoutlmv3.py --project-root D:\tmp\Train_20260413_143844_LayoutLMv3 --model-name microsoft/layoutlmv3-base --epochs 20
python train_layoutlmv3/4-evaluate_layoutlmv3.py --project-root D:\tmp\Train_20260413_143844_LayoutLMv3 --model-path D:\tmp\Train_20260413_143844_LayoutLMv3\models\layoutlmv3_base_run1
python train_layoutlmv3/6-export_onnx.py --project-root D:\tmp\Train_20260413_143844_LayoutLMv3 --model-path D:\tmp\Train_20260413_143844_LayoutLMv3\models\layoutlmv3_base_run1
```

Smoke test:

```powershell
python train_layoutlmv3/1-build_dataset.py --source-root D:\tmp\Train_20260413_143844_kie\json_output_labeled --project-root D:\tmp\Train_20260413_143844_LayoutLMv3 --limit-docs 20
python train_layoutlmv3/2-sanity_check.py --project-root D:\tmp\Train_20260413_143844_LayoutLMv3
python train_layoutlmv3/3-train_layoutlmv3.py --project-root D:\tmp\Train_20260413_143844_LayoutLMv3 --dry-run
```

Notes:

- Dataset rows are word-level pages with normalized int bboxes in `[0,1000]`.
- Labels are BIO over the requested fields.
- Ground truth uses `word_ids` first. Bbox overlap is used when available, and `line_ids` are only a counted last-resort fallback.
- Training chunks over tokenizer overflow with stride and merges predictions back to word-level for metrics.
- Page images are not exported by default; the model is run with text+layout inputs and no `pixel_values`.

