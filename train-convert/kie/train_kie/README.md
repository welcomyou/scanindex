# train_kie

Thu muc nay nam trong `train-convert/kie/train_kie/` va giu ho so train/eval KIE dang can cho ban hien tai. Runtime app khong import truc tiep tu day nua; cac shared utility runtime da tach sang `kie_core/`. Cac method train cu da duoc dua sang `temp/legacy_model_train_20260504/train_kie/` va `temp/legacy_model_train_20260504/root_cleanup/`.

Nguoi moi nen doc truoc [START_HERE.md](START_HERE.md), sau do moi vao README cua tung method.

KIE Viewer nam o root `kie_viewer/` va la mot phan cua workflow train: dung de
mo `json_input`, sua bbox/word_ids, luu ground truth vao `json_output_labeled`,
va chay validator truoc khi save. Step 2 trong app hien nay reuse nhieu UX/code
tu viewer nay qua `gui/widgets/kie_archive_viewer.py`.

## Ket luan chot

Production KIE hien tai:

1. `layoutlmv3_fontgray_norm`: model mac dinh trong app, chay no-image/fontgray/linebucket ONNX int8.
2. `lightgbm`: giu ho so train/eval va mot so benchmark lich su; khong con la KIE mac dinh.

Da dua sang temp vi khong phai current production:

- `layoutlmv3_base`: baseline deep tot, nhung instance/exact kem hon ban fontgray_norm.
- `lilt_xlmr`, `lilt_phobert`: test F1 thap, BIO fragmentation nang.
- `vilayoutxlm_plain`, `vilayoutxlm_style`: la experiment PaddleOCR VI-LayoutXLM, chua dat span/instance du tot de thay production.
- YOLO KIE region models (`YOLO11s`, `YOLO26n`) da benchmark voi LightGBM nhung khong vuot LayoutLMv3.

Chi tiet metric xem [results/README.md](results/README.md).

## Cau truc

```text
train-convert/kie/train_kie/
  lightgbm/
    README.md
    code/train_lightgbm/
    artifacts/final_model/
  layoutlmv3_fontgray_norm/
    README.md
    code/train_layoutlmv3_style/
    code/train_layoutlmv3/        # dependency bundled
    artifacts/final_model/
    artifacts/checkpoint-34000/
  dataset_bootstrap/
    README.md
    1-setup_project.py
    2-run_ocr_pipeline.py
    3-prepare_label_inputs.py
    4-adjudicate_votes.py
    5-export_training_sets.py
  samples/
  results/
  runbooks/
  START_HERE.md
  common.py                  # shared JSON/path helper
  ontology.py                # shared ontology/schema/text normalize
  labeling_workspace.py      # shared label validation/page-selection helper
  adjudication.py            # shared adjudication helper for bootstrap labels
  inference_pipeline.py      # shared decode/secrecy helper imported by viewer
  exporters.py               # shared export helpers used by legacy LiLT/Paddle code
  semantic_fields.py         # shared field text utilities
  portable_config.example.json
```

`dataset_bootstrap` la nhom code dung chung de tao project data ban dau, nen duoc dat ngoai cac method train. Root Python modules o tren la shared utility, khong phai entrypoint train rieng. Neu sau nay them Python script o root `train_kie`, chi nen them neu script do dung chung cho app hoac nhieu method. Method-specific script phai nam trong method folder tuong ung.

Legacy methods moved out:

```text
temp/legacy_model_train_20260504/train_kie/layoutlmv3_base/
temp/legacy_model_train_20260504/train_kie/lilt_phobert/
temp/legacy_model_train_20260504/train_kie/lilt_xlmr/
temp/legacy_model_train_20260504/train_kie/vilayoutxlm_plain/
temp/legacy_model_train_20260504/train_kie/vilayoutxlm_style/
```

YOLO KIE experiment code van nam o `../train_yolo_kie/` de doi chieu lich su,
nhung khong phai current production KIE model. Ket qua chot tu benchmark
0001/0006/0027: YOLO11s strict + LightGBM exact `0.1493`; YOLO26n pad10 +
LightGBM exact `0.8459`; LayoutLMv3 no-image exact `0.9859`.

## Data contract chung

Nguon du lieu goc cua cac huong train:

```text
<KIE_PROJECT_ROOT>/
  json_input/              # input cho label/review tool
  json_output_labeled/     # ground truth da gan nhan, uu tien word_ids
  ocr/                     # canonical OCR JSON
```

Nguyen tac:

- Ground truth uu tien `word_ids`; khong phu thuoc `line_ids`.
- Bbox normalize ve `[0,1000]` cho transformer layout models.
- Cung ontology 10 field: `REGIME_HEADER`, `ISSUE_ORG_SUPERIOR`, `ISSUE_ORG_NAME`, `DOC_NUMBER_SYMBOL`, `PLACE_DATE`, `DOC_SUBJECT`, `ADDRESSEE`, `RECIPIENTS`, `SIGNER_ROLE`, `SIGNER_NAME`.
- Exact instance strict: du/thieu 1 word la sai exact.
- Single-instance fields chi lay 1 vung sau decoder, tru `SIGNER_ROLE` va `SIGNER_NAME`.

## Label/review tool

Run viewer from repo root:

```powershell
python -m kie_viewer
```

Viewer config points to:

```text
json_input/           # label tasks, one document per task
json_output_labeled/  # final human-corrected labels
ocr/                  # canonical OCR JSON
```

Output labels should remain full canonical JSON files with annotations injected,
not annotation-only payloads. This keeps OCR, label review, train export,
evaluation, and GUI Step 2 on the same schema.

## Sample data

Sample nam o [samples/digital_140](samples/digital_140):

- PDF goc: `source_pdf/SYNTHETIC (DEMO).pdf`
- Canonical OCR JSON: `canonical_json/SYNTHETIC (DEMO)_ocr.pdf.json`
- Label input: `label_input/digitalpdf__SYNTHETIC_DEMO__0000000000000001.json`
- Labeled ground truth: `labeled_json/digitalpdf__SYNTHETIC_DEMO__0000000000000001.json`
- Converted train rows theo method trong `converted/`

Sample nay du de nguoi doc hieu canonical OCR, labeled JSON, LayoutLMv3 JSONL, LiLT JSONL va LightGBM candidate-level JSONL.

## Portable commands

Dung bien moi truong thay cho hardcode duong dan:

```powershell
$env:REPO_ROOT = "D:\App\ocrtool"
$env:TRAIN_KIE_ROOT = "$env:REPO_ROOT\train_kie"
$env:KIE_PROJECT_ROOT = "D:\tmp\Train_20260413_143844_kie"
$env:LIGHTGBM_PROJECT_ROOT = "D:\tmp\Train_20260413_143844_LightGBM"
$env:LAYOUTLMV3_PROJECT_ROOT = "D:\tmp\Train_20260413_143844_LayoutLMv3_fontgray_norm"
```

Lenh theo tung huong xem [runbooks/portable_training.md](runbooks/portable_training.md) va README trong moi method folder.

## Artifact final

- LightGBM final: `lightgbm/artifacts/final_model`
- LayoutLMv3 fontgray_norm final: `layoutlmv3_fontgray_norm/artifacts/final_model`
- LayoutLMv3 checkpoint epoch 25: `layoutlmv3_fontgray_norm/artifacts/checkpoint-34000`

Artifact cu cua LiLT/VI-LayoutXLM/LayoutLMv3 base nam trong `temp/legacy_model_train_20260504/train_kie/`.

## Vi sao chon LightGBM va LayoutLMv3

LightGBM la default vi test word-F1 `0.9364`, exact `0.8655`, batch0027 F1 `0.9808`, exact `0.9213`, toc do CPU khoang `111 ms/page`.

LayoutLMv3 fontgray_norm epoch 25 la deep model tot nhat ve dung instance: test span F1 `0.9425`, exact `0.9225`, batch0027 span F1 `0.9871`, exact `0.9810`, errors `38`, doi lai CPU INT8 khoang `658 ms/page`.
