# ScanIndex

Desktop OCR + KIE + searchable archive cho văn bản hành chính tiếng Việt
(Windows, PySide6, CPU-only).

Pipeline chính:

```
PDF (scan/digital)
   → preprocess (rotate / deskew / orientation)
   → OCR  (Chrome ScreenAI, offline DLL)
   → text correction  (CTranslate2, distilled-protonx)
   → layout + tables  (DocLayout-YOLO + GMFT/Docling TableFormer)
   → KIE  (LayoutLMv3 fine-tune trên văn bản hành chính VN)
   → searchable PDF + DOCX export
   → indexed search archive  (Tantivy + FAISS, optional reranker)
```

## Cài đặt từ source

```powershell
git clone https://github.com/welcomyou/scanindex.git
cd scanindex
python -m venv .venv_build
.venv_build\Scripts\activate
pip install -r requirements.txt
pip install -r requirements_qt.txt
```

### Tải model (~1.9 GB)

```powershell
python scripts\download_offline_models.py
```

Script này sẽ:

- Kéo bundle model từ Hugging Face
  [`welcomyou/scanindex-models`](https://huggingface.co/welcomyou/scanindex-models)
  về `models/`
- Tải Chrome ScreenAI DLL từ Google CDN về `models/screen_ai/`
  (license của Google không cho redistribute nên không bundle vào HF)
- Copy `chromedriver.exe` vào `drivers/`

### Chạy

```powershell
python ocr_app.py
```

## Build portable EXE

```powershell
build_portable.bat
```

Output ở `dist/Lightweight_OCR/`. Spec: [Lightweight_OCR.spec](Lightweight_OCR.spec).

## Cấu trúc

| Thư mục | Vai trò |
|---|---|
| [scanindex/app/](scanindex/app/) | App-level glue / entry helpers |
| [scanindex/core/](scanindex/core/) | OCR, correction, KIE, tables, repository (search) |
| [scanindex/ui/](scanindex/ui/) | PySide6 — main window, screens, tabs, widgets |
| [scanindex/infra/](scanindex/infra/) | Đường dẫn portable, Chrome profile, i18n |
| [scanindex/tools/](scanindex/tools/) | CLI tools |
| [config/](config/) | Default `settings.ini`, sign templates |
| [assets/](assets/) | Icon, mẫu MetaDuLieu.xlsx |
| [scripts/](scripts/) | Download models, benchmark, tooling |
| [tests/](tests/) | pytest |
| [train-convert/](train-convert/) | Decision records + scripts để retrain / re-export model (artifacts không kèm) |

## Models

Tổng hợp ở Collection [welcomyou/scanindex](https://huggingface.co/collections/welcomyou/scanindex). Gồm:

| Repo | Vai trò trong pipeline |
|---|---|
| [welcomyou/layoutlmv3-vn-admin-kie](https://huggingface.co/welcomyou/layoutlmv3-vn-admin-kie) | KIE LayoutLMv3 fine-tune |
| [welcomyou/e5-small-vn-archive-mix50](https://huggingface.co/welcomyou/e5-small-vn-archive-mix50) | Embedder cho search Kho lưu trữ |
| [welcomyou/distilled-protonx-vn-correction-ct2](https://huggingface.co/welcomyou/distilled-protonx-vn-correction-ct2) | Correction CTranslate2 |
| [welcomyou/lightgbm-vn-page-splitter](https://huggingface.co/welcomyou/lightgbm-vn-page-splitter) | Tách văn bản trong batch scan |
| [welcomyou/doclayout-yolo-onnx-dynamic](https://huggingface.co/welcomyou/doclayout-yolo-onnx-dynamic) | Layout YOLO (dynamic axes ONNX) |
| [welcomyou/gmft-tatr-onnx](https://huggingface.co/welcomyou/gmft-tatr-onnx) | Bảng — TATR detection + structure |
| [welcomyou/docling-tableformer-v1-onnx-stepcache](https://huggingface.co/welcomyou/docling-tableformer-v1-onnx-stepcache) | Bảng — Docling TableFormer (stepcache) |
| [welcomyou/scanindex-models](https://huggingface.co/welcomyou/scanindex-models) | Bundle: PaddleOCR orientation + `manifest.json` |

Hai model nằm ngoài HF (lý do license / kích thước):

- Chrome ScreenAI OCR — auto download từ Google CDN bởi [scanindex/core/ocr/screen_ai_downloader.py](scanindex/core/ocr/screen_ai_downloader.py)
- BAAI/bge-reranker-v2-m3 — pull lazy từ upstream khi user dùng search "Accurate"

Upload model sau khi retrain:

```powershell
huggingface-cli login
python scripts\upload_models_to_hf.py            # tất cả
python scripts\upload_models_to_hf.py --only welcomyou/layoutlmv3-vn-admin-kie   # 1 repo
python scripts\upload_models_to_hf.py --dry-run  # xem trước
```

## Settings

Copy `settings.ini.example` → `settings.ini` để chỉnh runtime config (ngôn ngữ,
correction model, số worker, v.v.). `settings.ini` được gitignored.

## Phụ thuộc chính

PySide6 · PyMuPDF · pikepdf · CTranslate2 · Transformers · ONNX Runtime ·
DocLayout-YOLO · GMFT · img2table · Selenium · OpenCV · sentence-transformers ·
tantivy · faiss-cpu

## License

Code: TBD. Model weights: xem từng subdir trong
[welcomyou/scanindex-models](https://huggingface.co/welcomyou/scanindex-models#licenses).
