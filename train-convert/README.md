# Train / Convert

Thu muc nay la decision record va code train/convert cho cac model dang dung
trong ban hien tai. Runtime app load artifact tu `models/`; portable build
khong can dong goi thu muc nay.

Run commands from repository root: `D:\App\ocrtool`.

## Current Structure

```text
train-convert/
  doclayoutyolo/
    convert/
      export_doclayout_yolo_to_onnx.py
  gmft/
    convert/
      export_gmft_tatr_to_onnx.py
  docling-tableformer-v1/
    convert/
      export_docling_v1_tableformer_stepcache_onnx.py
      onnx_stepcache_runner_reference.py
  archive-embedder/
    train/mix50_v2/
    convert/export_e5_mix50_to_onnx_fp32.py
  archive-page-splitter/
    train/lightgbm_splitter/
  kie/
    train_kie/
  layoutlmv3/
    train/fontgray_norm_final_epoch25/
      train_layoutlmv3/
      train_layoutlmv3_style/
  _tools/
    ocr/
    benchmark/
  _archived-runtime-models/
    README.md
```

Legacy/experiment code da dua sang:

```text
temp/legacy_model_train_20260504/
```

Runtime-only shared KIE code was split out to root `kie_core/` so the desktop
app no longer imports from a training workspace.

## Current Runtime Models

| Component | Current artifact | Current decision |
| --- | --- | --- |
| KIE | `models/layoutlmv3_fontgray_norm_final_epoch25/layoutlmv3_fontgray_norm_final_epoch25.int8.onnx` | LayoutLMv3 no-image/fontgray/linebucket la mac dinh vi chinh xac cao hon LightGBM va nhanh hon visual. |
| Archive embedding | `models/archive_models/e5-small-mix50-v2-onnx-fp32/` | Fine-tuned multilingual E5-small, 384d, ONNX FP32. Chon vi nhanh hon cac 1024d Vietnamese embedding va du tot cho CPU desktop. |
| Layout detection | `models/doclayout_yolo_onnx_dynamic/doclayout_yolo_docstructbench_imgsz1024_dynamic.onnx` | ONNX dynamic FP32 de bo PyTorch/doclayout-yolo khoi portable. |
| Table extraction | `models/gmft_onnx/detection/model.onnx`, `models/gmft_onnx/structure/model.onnx` | ONNX FP32 reimplementation cua GMFT/TATR de bo PyTorch/GMFT runtime. |
| Table structure consensus | `models/docling_tableformer_v1_stepcache_onnx/onnx/accurate/` | Docling TableFormer v1 accurate step-cache ONNX. Chay cung GMFT tren bbox cua DocLayout, roi chon bang scorer hinh hoc/OCR-fit. |

Table alternatives with weaker benchmark/runtime tradeoffs were moved to
`temp/unused_models_20260506/` or kept as benchmark-only scripts. Production
does not fall back to Img2Table, RapidTable SLANet+, wired_table_rec, legacy
PyTorch GMFT, Docling fixed-cache ONNX, or static DocLayout fallbacks.

## KIE History: Why LayoutLMv3 Won

Data contract chung:

- Source project: `D:\tmp\Train_20260413_143844_kie`.
- Input label task: `json_input/`.
- Final human-corrected labels: `json_output_labeled/`.
- Canonical OCR: `ocr/`.
- Viewer: `kie_viewer/` is part of the train workflow. It opens `json_input`,
  saves final labels to `json_output_labeled`, validates with
  `kie_core.labeling_workspace`, and lets users click OCR words/bboxes to edit
  KIE spans.
- Training labels: 10 KIE labels only: `REGIME_HEADER`,
  `ISSUE_ORG_SUPERIOR`, `ISSUE_ORG_NAME`, `DOC_NUMBER_SYMBOL`, `PLACE_DATE`,
  `DOC_SUBJECT`, `ADDRESSEE`, `RECIPIENTS`, `SIGNER_ROLE`, `SIGNER_NAME`.
- Rule/output fields such as urgency/secrecy/circulation marks and `DOC_TYPE`
  are not hand-labeled train labels.
- Evaluation uses strict word/span/instance matching. Thua/thieu 1 word in an
  instance is counted as wrong for exact.

Main KIE attempts:

| Attempt | Result | Decision |
| --- | ---: | --- |
| LightGBM field-wise | Batch0027 exact `0.9213`, speed `~111 ms/page`; optimized pass reached aggregate exact `~0.9363` on batches 0001/0006/0027 | Keep as fastest fallback, not best accuracy. |
| LiLT + XLM-R | Test F1 `0.7646` | Rejected: BIO fragmentation, slower/larger. |
| LiLT + PhoBERT | Test F1 `0.7727` | Rejected: still fragmented, recipients weak. |
| PaddleOCR VI-LayoutXLM plain INT8 | Full test span F1 `0.8833`, exact `0.8151`; batch0027 exact `0.9350`, speed `~489 ms/page` | Rejected: token-level ok but span/instance not enough. |
| YOLO11s strict + LightGBM | Exact `0.1493`, `~706 ms/page` on 0001/0006/0027 | Rejected: strict containment loses too many OCR words. |
| YOLO26n pad10 + LightGBM | Exact `0.8459`, `~493 ms/page` on 0001/0006/0027 | Rejected: better than YOLO11 but below LightGBM pure and LayoutLMv3. |
| LayoutLMv3 base | Batch0027 exact `0.9589`, `~684 ms/page` | Good baseline, but fontgray/linebucket better. |
| LayoutLMv3 fontgray_norm epoch 25 | Batch0027 exact `0.9810`, `~658 ms/page`; FINAL10 aggregate exact `~0.9840` over 1729 docs/3661 pages | Selected production KIE model. |
| LayoutLMv3 visual | Exact tied no-image (`0.9859`) on 0001/0006/0027 but `~1619 ms/page` vs no-image `~775 ms/page` | Moved to legacy; not the current runtime path. |

The final no-image LayoutLMv3 model uses OCR text/layout plus style/line
features:

- `font_size`, `fg_gray`, `word_height`, `confidence`, `content_type`.
- Line-position bucket from `line_id` such as `p0_l3`.
- Combined `token_type_ids` with `style_type_vocab_size=1024`.
- No rendered page image in default runtime, so it avoids the visual model's CPU
  cost.

Train/eval split:

- Dataset builder writes `exports/dataset/train.jsonl`, `val.jsonl`, `test.jsonl`.
- Visual experiment reused the same split and added rendered page images:
  train `2905` pages, val `408`, test `348`, total `3661`.
- Large final benchmark measured batches `0001-0016` plus `0027`: docs `1729`,
  pages `3661`, chunks `13690`, word F1 `~0.9936`, span F1 `~0.9855`, exact
  `~0.9840`, latency `~729 ms/page`.

Current train/export code:

```text
train-convert/kie/train_kie/
train-convert/layoutlmv3/train/fontgray_norm_final_epoch25/train_layoutlmv3_style/
train-convert/layoutlmv3/train/fontgray_norm_final_epoch25/train_layoutlmv3/
```

The duplicated-looking split is intentional:

- `kie_core/` is runtime/shared code imported by the app and KIE viewer.
- `train-convert/kie/train_kie/` is the preserved training workspace, reports,
  runbooks, sample data, and historical train package.

Example:

```powershell
.\.venv_build\Scripts\python.exe train-convert\layoutlmv3\train\fontgray_norm_final_epoch25\train_layoutlmv3_style\1-build_dataset.py `
  --source-root D:\tmp\Train_20260413_143844_kie\json_output_labeled `
  --project-root D:\tmp\Train_20260413_143844_LayoutLMv3_fontgray_norm
```

## Archive Embedding History: Why E5-small Mix50

Goal: semantic search in Kho luu tru on CPU-only desktop, preferably without
PyTorch in portable runtime.

Models tried/considered from local evidence:

| Model | Dim | Internal result / speed | Decision |
| --- | ---: | --- | --- |
| `AITeamVN/Vietnamese_Embedding` ONNX FP32 | 1024 | 350 docs encode `~425s`, 500 queries `~41s`, R@1 `0.672`, R@10 `0.904` | Accurate enough but too slow/heavy for CPU indexing. |
| `intfloat/multilingual-e5-small` ONNX FP32 | 384 | 350 docs encode `~50s`, 500 queries `~4s`, R@1 `0.662`, R@10 `0.886` | Much faster; good base. |
| Fine-tuned E5 Kho ONNX FP32 | 384 | 350 docs encode `~58s`, 500 queries `~4.7s`, R@1 `0.724`, R@10 `0.920` | Better internal retrieval while still small/fast. |
| E5-small mix50 v2 ONNX FP32 | 384 | Mixed test R@1 `53.7%`, R@5 `76.7%`, R@10 `82.8%`; semantic_v2 test R@10 `91.0%`; ONNX parity cosine mean `0.99999994` | Selected current backend. |
| HaLong/Vietnamese alternatives | - | No retained benchmark artifact found in repo after cleanup | Not selected until rebenchmarked locally. |
| Harrier/VietLegal/large reranker style models | larger | Tested as research path, but heavier for CPU portable | Not current embedding backend. |

How mix50 data was prepared:

- Start from internal Kho documents and LLM-generated natural Vietnamese
  retrieval questions.
- Mine hard negatives with E5 so near-miss documents are included in training.
- Mix internal Kho data with public Vietnamese retrieval/legal data.
- `mix50_v2` dataset report: `10924` Kho train pairs + `10924` public train
  pairs, `16718` corpus docs, `1000` val queries, `1000` test queries.
- Public sources in the prepared dataset include VN-MTEB style datasets,
  YuITC Vietnamese legal documents, and Zalo legal retrieval data.
- Training command used E5 prefixes: `query: ...` and `passage: ...`.
- RunPod training: 2 epochs suggested, batch size `32`, grad accumulation `4`,
  `3` negatives, fp16. Best epoch was `1`.

Current code:

```text
train-convert/archive-embedder/train/mix50_v2/
train-convert/archive-embedder/convert/export_e5_mix50_to_onnx_fp32.py
```

Current runtime reads:

```text
models/archive_models/e5-small-mix50-v2-onnx-fp32/
```

## DocLayout-YOLO ONNX

Reason: layout detection should not force portable builds to ship PyTorch,
Ultralytics/doclayout-yolo, or GPU-oriented dependencies.

Use dynamic export:

```powershell
.\.venv_build\Scripts\python.exe train-convert\doclayoutyolo\convert\export_doclayout_yolo_to_onnx.py --dynamic --out-dir models\doclayout_yolo_onnx_dynamic
```

Why dynamic: the original YOLOv10 predictor uses rectangular letterbox
(`1024x736` portrait, `736x1024` landscape). Static `1024x1024` ONNX introduced
small bbox drift. Dynamic H/W plus the same letterbox/inverse scaling matched the
original runtime.

Verified result on OCR temp of `05khbcd204.pdf`:

```text
139/139 regions, mismatch pages 0, mean IoU 1.0
```

Runtime file: `layout_analyzer.py`; it does not import `torch` or
`doclayout_yolo`.

## GMFT / TATR ONNX

Reason: GMFT's Python runtime depends on PyTorch/TATR. For portable CPU build,
we exported the underlying TATR detection and structure models to ONNX and
reimplemented the GMFT preprocessing/postprocessing path.

Export:

```powershell
.\.venv_build\Scripts\python.exe train-convert\gmft\convert\export_gmft_tatr_to_onnx.py
```

Artifacts:

```text
models/gmft_onnx/detection/model.onnx
models/gmft_onnx/detection/model.onnx.data
models/gmft_onnx/structure/model.onnx
models/gmft_onnx/structure/model.onnx.data
```

Important matching details:

- Render/crop at 144 DPI.
- Add 10% white padding.
- Use PIL bilinear resize to match HuggingFace processor.
- Keep detection and structure as two ONNX graphs because GMFT/TATR has two
  separate stages.

Verified result on OCR temp of `05khbcd204.pdf`:

```text
15/15 tables, same rows/cols/non-empty cells as GMFT PyTorch
```

Runtime file: `gmft_onnx_table_engine.py`; it does not import `torch` or `gmft`.
The old PyTorch GMFT dev comparison helper was moved to
`temp/legacy_model_train_20260504/root_cleanup/antigravity-gmft/`.

## Verification Tools

For scanned PDFs, first create an OCR temp PDF so table extraction has a text
layer:

```powershell
.\.venv_build\Scripts\python.exe train-convert\_tools\ocr\run_ocr_temp_pdf.py "C:\path\input.pdf" temp\input_ocr_temp.pdf --workers 2
```

Then compare ONNX runtimes with original PyTorch-backed runtimes:

```powershell
$env:OCRTOOL_ALLOW_PYTORCH_GMFT="1"
.\.venv_build\Scripts\python.exe train-convert\_tools\benchmark\benchmark_onnx_vs_original_layout_tables.py temp\input_ocr_temp.pdf --out temp\benchmark_onnx_vs_original.json
```

## Legacy Location

Moved out of the current tree:

```text
temp/legacy_model_train_20260504/train-convert/layoutlmv3_base_plain/
temp/legacy_model_train_20260504/train-convert/layoutlmv3_visual_final10/
temp/legacy_model_train_20260504/train-convert/archive-embedder/convert/convert_embedder_to_onnx_int8.py
temp/legacy_model_train_20260504/train-convert/archive-embedder/convert/convert_aiteam_vietnamese_embedding_to_onnx_fp32.py
temp/legacy_model_train_20260504/train_kie/layoutlmv3_base/
temp/legacy_model_train_20260504/train_kie/lilt_phobert/
temp/legacy_model_train_20260504/train_kie/lilt_xlmr/
temp/legacy_model_train_20260504/train_kie/vilayoutxlm_plain/
temp/legacy_model_train_20260504/train_kie/vilayoutxlm_style/
temp/legacy_model_train_20260504/root_cleanup/train_lightgbm/
temp/legacy_model_train_20260504/root_cleanup/train_yolo_kie/
temp/legacy_model_train_20260504/root_cleanup/train_vilayoutxlm/
temp/legacy_model_train_20260504/root_cleanup/train_vilayoutxlm_plain/
temp/legacy_model_train_20260504/root_cleanup/train_fasttext/
temp/legacy_model_train_20260504/root_cleanup/tools/
temp/legacy_model_train_20260504/root_cleanup/antigravity-gmft/
```
