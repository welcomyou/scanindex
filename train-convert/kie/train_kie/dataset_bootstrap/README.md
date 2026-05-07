# Dataset Bootstrap

Day la code dung chung de tao dataset/label workspace ban dau truoc khi train tung method.

## Vai tro

```text
1-setup_project.py          # tao KIE project manifest tu folder PDF goc
2-run_ocr_pipeline.py       # chay OCR/canonical JSON cho tung PDF
3-prepare_label_inputs.py   # tao json_input cho label/review tool
4-adjudicate_votes.py       # hop nhat/cap nhat ground truth sau review
5-export_training_sets.py   # export dataset chung cho LiLT/Paddle legacy tracks
batch_autolabel_v3.py       # helper autolabel/audit hang loat
batch_rerun_canonical.py    # helper chay lai canonical OCR
AUDIT_GUIDE.md              # huong dan audit label
requirements-train-kie.txt  # dependency cho bootstrap/legacy LiLT tools
```

Nhung script nay khong phai mot method train rieng. Output cua chung la project KIE co `json_input`, `json_output_labeled`, `ocr`, `exports`; cac method folder sau do doc project nay de convert/train theo format rieng.

## Data flow

```text
PDF folder
  -> 1-setup_project.py
  -> 2-run_ocr_pipeline.py
  -> 3-prepare_label_inputs.py
  -> human label/review
  -> json_output_labeled/
  -> method-specific dataset builder
```

## Command mau

```powershell
$env:TRAIN_KIE_ROOT = "D:\App\ocrtool\train_kie"
$env:SOURCE_PDF_ROOT = "D:\tmp\Train_20260413_143844"
$env:KIE_PROJECT_ROOT = "D:\tmp\Train_20260413_143844_kie"

python $env:TRAIN_KIE_ROOT\dataset_bootstrap\1-setup_project.py `
  --input-root $env:SOURCE_PDF_ROOT `
  --project-root $env:KIE_PROJECT_ROOT

python $env:TRAIN_KIE_ROOT\dataset_bootstrap\2-run_ocr_pipeline.py `
  --project-root $env:KIE_PROJECT_ROOT

python $env:TRAIN_KIE_ROOT\dataset_bootstrap\3-prepare_label_inputs.py `
  --project-root $env:KIE_PROJECT_ROOT
```

Sau khi co label human-corrected trong `json_output_labeled`, chay builder cua tung method:

- `lightgbm/code/train_lightgbm/2-build_fieldwise_dataset.py`
- `layoutlmv3_fontgray_norm/code/train_layoutlmv3_style/1-build_dataset.py`

Legacy builders da dua sang `temp/legacy_model_train_20260504/train_kie/`:

- `layoutlmv3_base`
- `lilt_phobert`
- `lilt_xlmr`
- `vilayoutxlm_plain`
- `vilayoutxlm_style`

Sample output de xem format nam tai `../samples/digital_140`.
