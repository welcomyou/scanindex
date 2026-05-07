# Portable training runbook

Dung cac bien duong dan nay de tranh hardcode trong lenh chay:

```powershell
$env:REPO_ROOT = "D:\App\ocrtool"
$env:TRAIN_KIE_ROOT = "$env:REPO_ROOT\train_kie"
$env:KIE_PROJECT_ROOT = "D:\tmp\Train_20260413_143844_kie"
$env:LIGHTGBM_PROJECT_ROOT = "D:\tmp\Train_20260413_143844_LightGBM"
$env:LAYOUTLMV3_PROJECT_ROOT = "D:\tmp\Train_20260413_143844_LayoutLMv3_fontgray_norm"
```

Tren Linux/RunPod:

```bash
export REPO_ROOT=/workspace/ocrtool
export TRAIN_KIE_ROOT="$REPO_ROOT/train_kie"
export KIE_PROJECT_ROOT=/workspace/Train_20260413_143844_kie
export LIGHTGBM_PROJECT_ROOT=/workspace/Train_20260413_143844_LightGBM
export LAYOUTLMV3_PROJECT_ROOT=/workspace/Train_20260413_143844_LayoutLMv3_fontgray_norm
```

## Dataset bootstrap ban dau

Code: `train_kie/dataset_bootstrap`

Dung khi bat dau tu folder PDF goc va can tao project KIE co canonical OCR + label input:

```powershell
$env:SOURCE_PDF_ROOT = "D:\tmp\Train_20260413_143844"

python $env:TRAIN_KIE_ROOT\dataset_bootstrap\1-setup_project.py `
  --input-root $env:SOURCE_PDF_ROOT `
  --project-root $env:KIE_PROJECT_ROOT

python $env:TRAIN_KIE_ROOT\dataset_bootstrap\2-run_ocr_pipeline.py `
  --project-root $env:KIE_PROJECT_ROOT

python $env:TRAIN_KIE_ROOT\dataset_bootstrap\3-prepare_label_inputs.py `
  --project-root $env:KIE_PROJECT_ROOT
```

Sau khi human label/review xong va co `json_output_labeled`, moi chay builder rieng cua LightGBM/LayoutLMv3/VI-LayoutXLM.

## LightGBM final

Code: `train_kie/lightgbm/code/train_lightgbm`

Input can co:

```text
$KIE_PROJECT_ROOT/
  json_input/
  json_output_labeled/
  ocr/
```

Build/train/eval:

```powershell
python $env:TRAIN_KIE_ROOT\lightgbm\code\train_lightgbm\1-setup_project.py `
  --source-project-root $env:KIE_PROJECT_ROOT `
  --project-root $env:LIGHTGBM_PROJECT_ROOT

python $env:TRAIN_KIE_ROOT\lightgbm\code\train_lightgbm\2-build_fieldwise_dataset.py `
  --project-root $env:LIGHTGBM_PROJECT_ROOT `
  --max-workers 6

python $env:TRAIN_KIE_ROOT\lightgbm\code\train_lightgbm\3-train_field_models.py `
  --project-root $env:LIGHTGBM_PROJECT_ROOT

python $env:TRAIN_KIE_ROOT\lightgbm\code\train_lightgbm\4-evaluate_models.py `
  --project-root $env:LIGHTGBM_PROJECT_ROOT
```

Output chinh:

```text
$LIGHTGBM_PROJECT_ROOT/exports/fieldwise/<FIELD>/<split>.jsonl
$LIGHTGBM_PROJECT_ROOT/exports/ground_truth/<split>.jsonl
$LIGHTGBM_PROJECT_ROOT/models/fieldwise/*.joblib
```

## LayoutLMv3 base

Code: `train_kie/layoutlmv3_base/code/train_layoutlmv3`

```powershell
python $env:TRAIN_KIE_ROOT\layoutlmv3_base\code\train_layoutlmv3\1-build_dataset.py `
  --source-root $env:KIE_PROJECT_ROOT\json_output_labeled `
  --project-root D:\tmp\Train_20260413_143844_LayoutLMv3

python $env:TRAIN_KIE_ROOT\layoutlmv3_base\code\train_layoutlmv3\2-sanity_check.py `
  --project-root D:\tmp\Train_20260413_143844_LayoutLMv3
```

## LayoutLMv3 fontgray_norm

Code: `train_kie/layoutlmv3_fontgray_norm/code/train_layoutlmv3_style`

Input can co:

```text
$KIE_PROJECT_ROOT/json_output_labeled/
$KIE_PROJECT_ROOT/ocr/
```

Build dataset:

```powershell
python $env:TRAIN_KIE_ROOT\layoutlmv3_fontgray_norm\code\train_layoutlmv3_style\1-build_dataset.py `
  --source-root $env:KIE_PROJECT_ROOT\json_output_labeled `
  --project-root $env:LAYOUTLMV3_PROJECT_ROOT

python $env:TRAIN_KIE_ROOT\layoutlmv3_fontgray_norm\code\train_layoutlmv3_style\2-sanity_check.py `
  --project-root $env:LAYOUTLMV3_PROJECT_ROOT
```

Train tren GPU:

```bash
python "$TRAIN_KIE_ROOT/layoutlmv3_fontgray_norm/code/train_layoutlmv3_style/3-train_layoutlmv3_style.py" \
  --project-root "$LAYOUTLMV3_PROJECT_ROOT" \
  --model-name microsoft/layoutlmv3-base \
  --output-dir "$LAYOUTLMV3_PROJECT_ROOT/models/layoutlmv3_fontgray_norm_run1" \
  --epochs 80 \
  --batch-size 4 \
  --eval-batch-size 4 \
  --loss weighted_ce \
  --boundary-token-weight 1.5
```

Evaluate/export:

```bash
python "$TRAIN_KIE_ROOT/layoutlmv3_fontgray_norm/code/train_layoutlmv3_style/4-evaluate_layoutlmv3_style.py" \
  --project-root "$LAYOUTLMV3_PROJECT_ROOT" \
  --model-path "$LAYOUTLMV3_PROJECT_ROOT/models/layoutlmv3_fontgray_norm_run1"

python "$TRAIN_KIE_ROOT/layoutlmv3_fontgray_norm/code/train_layoutlmv3_style/6-export_onnx.py" \
  --project-root "$LAYOUTLMV3_PROJECT_ROOT" \
  --model-path "$LAYOUTLMV3_PROJECT_ROOT/models/layoutlmv3_fontgray_norm_run1" \
  --onnx-threads 9
```

## LiLT + XLM-R / PhoBERT

Code:

- `train_kie/lilt_xlmr/code/train_kie`
- `train_kie/lilt_phobert/code/train_kie`

Input can co:

```text
$KIE_PROJECT_ROOT/exports/lilt_xlmr/
$KIE_PROJECT_ROOT/exports/lilt_phobert/
```

Neu can rebuild export tu canonical/labeled:

```powershell
python $env:TRAIN_KIE_ROOT\dataset_bootstrap\5-export_training_sets.py `
  --project-root $env:KIE_PROJECT_ROOT
```

Run tren RunPod bang runbook da tach:

```bash
bash "$TRAIN_KIE_ROOT/lilt_xlmr/runbooks/fullrun/1_setup_env.sh"
bash "$TRAIN_KIE_ROOT/lilt_xlmr/runbooks/fullrun/2_build_backbone.sh"
bash "$TRAIN_KIE_ROOT/lilt_xlmr/runbooks/fullrun/3_train.sh"
bash "$TRAIN_KIE_ROOT/lilt_xlmr/runbooks/fullrun/4_eval.sh"

bash "$TRAIN_KIE_ROOT/lilt_phobert/runbooks/fullrun/1_setup_env.sh"
bash "$TRAIN_KIE_ROOT/lilt_phobert/runbooks/fullrun/2_build_backbone.sh"
bash "$TRAIN_KIE_ROOT/lilt_phobert/runbooks/fullrun/3_train.sh"
bash "$TRAIN_KIE_ROOT/lilt_phobert/runbooks/fullrun/4_eval.sh"
```

Ghi chu: cac shell script cu co the con bien `PROJECT_ROOT` mac dinh. Khi port sang may khac, sua bien o dau script hoac tao wrapper export env tuong ung.

## PaddleOCR VI-LayoutXLM plain/style

Code:

- `train_kie/vilayoutxlm_plain/code/train_vilayoutxlm_plain`
- `train_kie/vilayoutxlm_style/code/train_vilayoutxlm`

Input can co:

```text
$KIE_PROJECT_ROOT/json_output_labeled/
$KIE_PROJECT_ROOT/ocr/
```

Plain build:

```powershell
python $env:TRAIN_KIE_ROOT\vilayoutxlm_plain\code\train_vilayoutxlm_plain\1-build_dataset.py `
  --source-root $env:KIE_PROJECT_ROOT\json_output_labeled `
  --project-root D:\tmp\Train_20260413_143844_VILayoutXLM_plain
```

Style build:

```powershell
python $env:TRAIN_KIE_ROOT\vilayoutxlm_style\code\train_vilayoutxlm\1-build_dataset.py `
  --source-root $env:KIE_PROJECT_ROOT\json_output_labeled `
  --project-root D:\tmp\Train_20260413_143844_VILayoutXLM_style
```

Train/export can PaddleOCR source checkout va Paddle/PaddleNLP dung version. Ket qua hien tai khong du tot de production, nen chi giu lam experiment.
