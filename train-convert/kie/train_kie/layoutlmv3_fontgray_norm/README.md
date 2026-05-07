# LayoutLMv3 Fontgray Norm

Day la deep model chot hien tai neu uu tien lay dung vung/instance, it du/thieu word.

## Noi dung

```text
code/train_layoutlmv3_style/   # snapshot code train/evaluate/export thuc te
code/train_layoutlmv3/         # dependency base bundled de style code import duoc khi clone rieng train_kie
artifacts/final_model/         # copy tu D:\App\ocrtool\models\layoutlmv3_fontgray_norm_final_epoch25
artifacts/checkpoint-34000/    # checkpoint epoch 25 tren RunPod
```

## Ket qua

- Test word F1: `0.9780`
- Test span F1: `0.9425`
- Test exact: `0.9225`
- Batch0027 word F1: `0.9926`
- Batch0027 span F1: `0.9871`
- Batch0027 exact: `0.9810`
- Batch0027 errors: `38`
- Missing word rate batch0027: `0.0075`
- Extra word rate batch0027: `0.0152`
- Speed CPU INT8: `~658 ms/page`

Train tiep den epoch 35 khong vuot epoch 25 ve span/exact, nen epoch 25 la checkpoint chot.

## Khac LayoutLMv3 base

Feature style lay tu canonical OCR theo `word_id`: `font_size`, `fg_gray`, `word_height`, `confidence`, `content_type`. Cac gia tri `font_size`, `fg_gray`, `word_height` duoc normalize/bucket theo page median va dua vao model qua `token_type_ids`.

## Data format

Sample:

```text
..\samples\digital_140\converted\layoutlmv3_fontgray_norm\train_sample.jsonl
```

## Command mau

```powershell
$env:TRAIN_KIE_ROOT = "D:\App\ocrtool\train_kie"
$env:KIE_PROJECT_ROOT = "D:\tmp\Train_20260413_143844_kie"
$env:LAYOUTLMV3_PROJECT_ROOT = "D:\tmp\Train_20260413_143844_LayoutLMv3_fontgray_norm"

python $env:TRAIN_KIE_ROOT\layoutlmv3_fontgray_norm\code\train_layoutlmv3_style\1-build_dataset.py `
  --source-root $env:KIE_PROJECT_ROOT\json_output_labeled `
  --project-root $env:LAYOUTLMV3_PROJECT_ROOT
```
