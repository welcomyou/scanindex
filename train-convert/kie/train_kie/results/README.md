# KIE training results

Bang nay la ket qua chot tu cac huong train da thu tren cung bai toan OCR KIE van ban hanh chinh tieng Viet.

Artifact inventory theo cau truc moi nam o `artifacts_manifest.json`. Moi artifact duoc dat trong method folder tuong ung, khong con dung `train_kie/artifacts`.

## Summary

| Model | Test F1 / exact | Batch0027 F1 / exact | Speed CPU | Ket luan |
|---|---:|---:|---:|---|
| LightGBM final | word-F1 `0.9364`, exact `0.8655` | F1 `0.9808`, exact `0.9213`, errors `129` | `~111 ms/page` | Production default |
| LayoutLMv3 goc | test span F1 `0.9347`, exact `0.9125` | span F1 `0.9749`, exact `0.9589`, errors `83` | `~684 ms/page` | Baseline deep |
| LayoutLMv3 fontgray_norm epoch 25 | test span F1 `0.9425`, exact `0.9225` | span F1 `0.9871`, exact `0.9810`, errors `38` | `~658 ms/page` | Deep model tot nhat ve instance |
| LiLT + XLM-R | test F1 `0.7646` | khong chot | cham, model lon | Loai |
| LiLT + PhoBERT | test F1 `0.7727` | khong chot | cham, model lon | Loai |
| PaddleOCR VI-LayoutXLM plain INT8 | full test span F1 `0.8833`, exact `0.8151` | span F1 `0.9637`, exact `0.9350`, errors `104` | `~489 ms/page` | Loai |

## LightGBM final

Method folder: `train_kie/lightgbm`

Artifact: `train_kie/lightgbm/artifacts/final_model`

Production source: `D:\App\ocrtool\models\lightgbm`

Data source: `D:\tmp\Train_20260413_143844_kie\json_output_labeled`

Training format:

- Candidate-level, field-wise.
- Moi field co model LightGBM rieng.
- Candidate tao tu line/span/region, gan target theo word_ids.
- Exact instance strict theo toan bo `word_ids`.

Ket qua:

| Tap danh gia | Word-F1 | Exact instance accuracy | Errors |
|---|---:|---:|---:|
| Val | `0.9346` | `0.8662` | - |
| Test | `0.9364` | `0.8655` | - |
| Batch0027 | `0.9808` | `0.9213` | `129` |
| Full 1732 docs | `0.9465` | `0.8913` | `2654` |

Speed:

- Batch0027 pipeline LightGBM dung: `36.76s / 151 files / 331 pages`, khoang `111 ms/page`.
- Safe candidate filter tren `669.pdf`: candidates `3005 -> 1449`, median runtime `765 ms -> 343 ms`, khong giam accuracy tren val/test/batch0027.

Da loai:

- Style features `font_size`, `fg_gray`: khong on dinh hon ban goc.
- mMiniLM/reranker: tang do phuc tap va latency, chua chung minh loi ich du.
- ONNX/lleaves LightGBM: khong dang vi `model.predict()` chi khoang `1.6%` runtime.

## LayoutLMv3

Final method folder: `train_kie/layoutlmv3_fontgray_norm`

Final artifact: `train_kie/layoutlmv3_fontgray_norm/artifacts/final_model`

Production source: `D:\App\ocrtool\models\layoutlmv3_fontgray_norm_final_epoch25`

Final ONNX INT8:

```text
D:\App\ocrtool\models\layoutlmv3_fontgray_norm_final_epoch25\layoutlmv3_fontgray_norm_final_epoch25.int8.onnx
```

### LayoutLMv3 goc

Method folder: `train_kie/layoutlmv3_base`

- Dung `microsoft/layoutlmv3-base`.
- Khong them font size/font gray.
- Dung BIO token classification, eval word/span/instance.
- Chi luu code va ket qua; artifact final goc khong co trong `D:\App\ocrtool\models` tai thoi diem dong goi.

Ket qua:

| Tap | Word F1 | Span F1 | Exact |
|---|---:|---:|---:|
| Val | `0.9702` | `0.9421` | `0.9366` |
| Test | `0.9734` | `0.9347` | `0.9125` |
| Batch0027 INT8 | `0.9961` | `0.9749` | `0.9589` |

Batch0027 errors: `83`, speed `683.6 ms/page`.

### LayoutLMv3 fontgray_norm final epoch 25

- Dung canonical style metadata theo `word_id`.
- Feature: `font_size`, `fg_gray`, `word_height`, `confidence`, `content_type`.
- `font_size`, `fg_gray`, `word_height` normalize/bucket theo page median.
- Style id dua vao `token_type_ids`, khong them token text moi.

Ket qua:

| Tap | Word F1 | Span F1 | Exact | Missing | Extra |
|---|---:|---:|---:|---:|---:|
| Val | `0.9710` | `0.9478` | `0.9230` | `0.0640` | `0.0563` |
| Test | `0.9780` | `0.9425` | `0.9225` | `0.0403` | `0.0540` |
| Batch0027 INT8 | `0.9926` | `0.9871` | `0.9810` | `0.0075` | `0.0152` |

Batch0027 errors: `38`, speed `658.0 ms/page`.

Train tiep sau epoch 25:

| Metric val | Epoch 25 | Epoch 34 | Epoch 35 |
|---|---:|---:|---:|
| Word F1 | `0.9710` | `0.9726` | `0.9722` |
| Span F1 | `0.9478` | `0.9434` | `0.9405` |
| Exact | `0.9230` | `0.9186` | `0.9197` |

Epoch 25 la checkpoint chot vi train tiep khong cai thien span/exact.

## LiLT

Method folders:

- `train_kie/lilt_xlmr`
- `train_kie/lilt_phobert`

Artifacts:

- `train_kie/lilt_xlmr/artifacts/run2`
- `train_kie/lilt_phobert/artifacts/run2`

Muc tieu ban dau:

- Train hai huong song song tren cung ontology, split, canonical OCR, metric va postprocess.
- LiLT + XLM-R la baseline chinh cho noisy OCR multilingual.
- LiLT + PhoBERT la benchmark phu cho tieng Viet.

Ket qua:

| Model | Test F1 | Van de chinh |
|---|---:|---|
| LiLT + XLM-R | `0.7646` | Fragment BIO, cham, model lon |
| LiLT + PhoBERT | `0.7727` | Fragment BIO, RECIPIENTS kem |

Ket luan: khong dung production. XLM-R la huong baseline hop ly hon ve ly thuyet cho OCR noisy, nhung ket qua local khong dat.

## PaddleOCR VI-LayoutXLM

Method folders:

- `train_kie/vilayoutxlm_style`
- `train_kie/vilayoutxlm_plain`

Ket qua plain INT8:

| Tap | Word F1 | Span F1 | Exact | Errors | Speed |
|---|---:|---:|---:|---:|---:|
| Full test 168 docs / 346 pages | `0.9669` | `0.8833` | `0.8151` | - | `~480 ms/page` |
| Batch0027 151 docs / 323 pages | `0.9870` | `0.9637` | `0.9350` | `104` | `~489 ms/page` |

Ghi chu:

- Best train val hmean `0.94592` tai epoch 52, dung som o epoch 79 vi overfit.
- FP32 ONNX export OK, INT8 dynamic checker OK.
- INT8 file van khoang `1.88 GB`, dynamic quantization khong giam size dang ke.
- Token-level tot nhung instance/span full test thap; single-field multi-line van bi thieu/gay khi decode.

Ket luan: khong dung production.
