# START HERE

File nay la duong dan nhanh cho nguoi moi clone repo va muon hieu/chay lai cac huong train KIE.

## 1. Can clone full repo

`train-convert/kie/train_kie` khong nen chay nhu mot folder doc lap. Mot so buoc bootstrap OCR dung module o repo root nhu `direct_ocr_engine.py`, `pdf_utils.py`, `kie_json_utils.py`. Runtime shared helpers cua app nam o `kie_core/`.

Gia dinh repo:

```text
ocrtool/
  direct_ocr_engine.py
  kie_json_utils.py
  kie_core/
  train-convert/kie/train_kie/
```

Khi chay script trong workspace nay, them parent cua package `train_kie` vao
`PYTHONPATH`:

```powershell
$env:PYTHONPATH = "D:\App\ocrtool;D:\App\ocrtool\train-convert\kie"
```

## 2. Neu bat dau tu PDF goc

Dung shared bootstrap:

```text
train-convert/kie/train_kie/dataset_bootstrap/
  1-setup_project.py
  2-run_ocr_pipeline.py
  3-prepare_label_inputs.py
  4-adjudicate_votes.py
  5-export_training_sets.py
```

Output project data can co:

```text
<KIE_PROJECT_ROOT>/
  json_input/
  json_output_labeled/
  ocr/
  exports/
```

Lenh mau:

```powershell
$env:REPO_ROOT = "D:\App\ocrtool"
$env:TRAIN_KIE_ROOT = "$env:REPO_ROOT\train_kie"
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

Sau khi human label/review xong, ground truth chinh nam o:

```text
<KIE_PROJECT_ROOT>/json_output_labeled/
```

## 3. Neu da co labeled data san

Co the bo qua bootstrap va chay thang converter/train cua tung method. Moi method doc `json_output_labeled` va `ocr` theo `word_ids`.

## 4. Convert/train theo tung huong

| Huong | Code | Khi nao dung |
|---|---|---|
| LightGBM | `lightgbm/code/train_lightgbm` | Production default, nhanh CPU |
| LayoutLMv3 fontgray_norm | `layoutlmv3_fontgray_norm/code/train_layoutlmv3_style` | Deep model tot nhat ve dung instance |

Da thu nhung da dua sang `temp/legacy_model_train_20260504/train_kie/`:

- `layoutlmv3_base`: baseline deep layout-aware, thua fontgray_norm.
- `lilt_xlmr`, `lilt_phobert`: F1 thap, BIO fragmentation.
- `vilayoutxlm_plain`, `vilayoutxlm_style`: span/exact khong dat production.
- YOLO KIE 11/26 nam o `../train_yolo_kie/` de doi chieu, khong phai production.

Lenh chi tiet nam o:

```text
train_kie/runbooks/portable_training.md
```

README cua tung method la tai lieu chinh cho method do.

## 5. Sample data de xem format

Sample day du nam o:

```text
train_kie/samples/digital_140/
```

No gom:

- PDF goc
- canonical OCR JSON
- label input JSON
- labeled ground truth JSON
- converted JSONL cho LayoutLMv3/LiLT
- candidate JSONL cho LightGBM

Dung sample nay de hieu format truoc khi doc code.

## 5a. KIE Viewer

Dung viewer de review/sua label truoc khi train:

```powershell
python -m kie_viewer
```

Viewer lam viec tren:

```text
json_input/
json_output_labeled/
ocr/
```

Khong luu annotation-only JSON; output can la canonical OCR JSON day du co
annotations injected. `gui/widgets/kie_archive_viewer.py` trong app hien nay
reuse nhieu hanh vi cua viewer nay cho Step 2.

## 6. Artifact va GitHub

Mot so artifact trong `train_kie` rat lon:

- LiLT XLM-R ONNX: hon 1 GB
- LayoutLMv3 checkpoint/final model: hang tram MB den gan 1 GB

GitHub thuong khong nhan file tren 100 MB. Neu can upload artifact:

- dung Git LFS cho `*.onnx`, `*.safetensors`, `*.bin`, `*.pdparams`, `*.joblib`; hoac
- khong commit artifact lon, chi commit code/docs/sample va dua model len GitHub Release/Drive/MinIO.

Danh sach artifact hien tai xem:

```text
train_kie/results/artifacts_manifest.json
```

## 7. Ket luan model

Quyet dinh hien tai:

- Production default: LightGBM.
- Deep layout-aware tot nhat ve dung instance: LayoutLMv3 fontgray_norm epoch 25.
- LiLT va VI-LayoutXLM hien khong thay production.

So lieu chi tiet xem:

```text
train_kie/results/README.md
```
