# LightGBM KIE

Day la pipeline production hien tai: field-wise LightGBM region classifier + schema decoder.

## Noi dung

```text
code/train_lightgbm/      # snapshot code train/evaluate/export thuc te
artifacts/final_model/    # copy tu D:\App\ocrtool\models\lightgbm
```

## Ket qua

- Test word-F1: `0.9364`
- Test exact instance: `0.8655`
- Batch0027 F1: `0.9808`
- Batch0027 exact: `0.9213`
- Batch0027 errors: `129`
- Speed CPU pipeline dung: `~111 ms/page`

Day la default production vi nhanh nhat va on dinh nhat tren CPU. Diem yeu chinh la boundary du/thieu word o mot so field kho.

## Data format

Huong nay train candidate-level. Ground truth strict theo `word_ids`; moi candidate co `field`, `candidate_id`, `word_ids`, `bbox`, `features`, `target`.

Sample format xem:

```text
..\samples\digital_140\converted\lightgbm\
```

## Command mau

```powershell
$env:TRAIN_KIE_ROOT = "D:\App\ocrtool\train_kie"
$env:KIE_PROJECT_ROOT = "D:\tmp\Train_20260413_143844_kie"
$env:LIGHTGBM_PROJECT_ROOT = "D:\tmp\Train_20260413_143844_LightGBM"

python $env:TRAIN_KIE_ROOT\lightgbm\code\train_lightgbm\1-setup_project.py `
  --source-project-root $env:KIE_PROJECT_ROOT `
  --project-root $env:LIGHTGBM_PROJECT_ROOT

python $env:TRAIN_KIE_ROOT\lightgbm\code\train_lightgbm\2-build_fieldwise_dataset.py `
  --project-root $env:LIGHTGBM_PROJECT_ROOT `
  --max-workers 6
```
