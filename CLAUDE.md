# Lightweight OCR - Project Guide

## Overview
Desktop OCR application (Windows) built with **PySide6**. Converts scanned/image-based PDFs to searchable PDFs using Google's ScreenAI engine (offline, CPU-only). Supports Vietnamese text correction, metadata extraction, and Word (DOCX) export with table layout preservation.

## Entry Point
- **ocr_app.py** -> `gui/main_window.py` -> tabs, widgets, dialogs

## Architecture

### Processing Pipeline
```
Input PDF -> Preprocessing (rotate/deskew)
          -> OCR (direct_ocr_engine / chrome_ocr_engine)
          -> Correction (correction_engine) [optional]
          -> PDF correction (pdf_utils) + Metadata (document_metadata_extractor) [optional]
          -> Table extraction + DOCX export (table_anchored_merger) [optional]
```

### Threading Model
- **Main thread:** PySide6 Qt event loop (GUI)
- **OCR:** ThreadPoolExecutor across input PDFs; each PDF uses about 4 internal ScreenAI workers
- **Export:** ProcessPoolExecutor (1-2 workers) via workers.py
- **Correction:** Single-threaded (model lock)
- Cross-thread communication: `gui/signals.py` (AppSignals)

### Output Files
- `{name}_ocr.pdf` — OCR text overlay PDF; also the corrected output target when correction is enabled
- `{name}_ocr.pdf.json` — OCR data (lines/words/bboxes)
- `{name}.docx` — Word export with tables [if export on]

## Project Structure

### GUI Layer (PySide6)
| File | Purpose |
|------|---------|
| `gui/main_window.py` | Central window + ProcessingPipeline (~1467 lines) |
| `gui/theme.py` | Dark theme QSS + design tokens (COLOR_*, SP, RADIUS) |
| `gui/signals.py` | Custom Qt signals for worker-GUI communication |
| `gui/splash_screen.py` | Startup splash with background model loading |
| `gui/icons.py` | Icon loader (assets/*.png) |

### Tabs / Screens
| File | Purpose |
|------|---------|
| `gui/tabs/dnd_tab.py` | Drag & Drop single-file processing |
| `gui/tabs/archive_tab.py` | "Số hóa lưu trữ" — 3-step pipeline (split → KIE → sign) |
| `gui/tabs/settings_tab.py` | App configuration UI |
| `gui/tabs/about_tab.py` | About page |
| `gui/screens/home_screen.py` | Tile dashboard (entry point) |
| `gui/screens/accuracy_screen.py` | OCR accuracy benchmark |
| `gui/screens/kho_luu_tru_screen.py` | "Kho lưu trữ" — search archive (Tantivy + FAISS) |

### Widgets
| File | Purpose |
|------|---------|
| `gui/widgets/file_item_widget.py` | File row (status pill + action buttons) |
| `gui/widgets/file_list_widget.py` | File list with drag-drop support |
| `gui/widgets/log_panel.py` | Color-coded activity log |
| `gui/widgets/section_card.py` | Elevated card container |
| `gui/widgets/status_pill.py` | Colored status badge |

### Dialogs
| File | Purpose |
|------|---------|
| `gui/dialogs/comparison_dialog.py` | Side-by-side diff (raw vs corrected) |
| `gui/dialogs/text_preview_dialog.py` | Read-only text viewer |
| `gui/dialogs/metadata_dialog.py` | Document metadata display |

### OCR Engines
| File | Purpose |
|------|---------|
| `direct_ocr_engine.py` | **Primary.** ScreenAI DLL via ctypes (no browser) |
| `chrome_ocr_engine.py` | Legacy. Chrome+Selenium OCR (fallback) |
| `screen_ai_ocr.py` | Low-level ctypes binding to chrome_screen_ai.dll |
| `screen_ai_downloader.py` | Auto-download ScreenAI from Google CDN |

### Business Logic
| File | Purpose |
|------|---------|
| `correction_engine.py` | Vietnamese text correction (Proton CT2 model, CTranslate2) |
| `layout_analyzer.py` | DocLayout-YOLO region detection |
| `table_anchored_merger.py` | Table extraction + DOCX creation (GMFT + img2table) |
| `document_metadata_extractor.py` | Vietnamese admin doc metadata extraction |
| `pdf_utils.py` | PDF text replacement (pikepdf + fitz fallback) |
| `workers.py` | Picklable worker functions for ProcessPoolExecutor |

### Kho lưu trữ (search archive) — `archive_store/`
| File | Purpose |
|------|---------|
| `archive_store/constants.py` | Versions, paths, search params, per-field weights |
| `archive_store/schema.sql` | SQLite DDL — dossiers / documents / chunks / vector_mapping / index_meta / import_history |
| `archive_store/store.py` | `ArchiveStore` — SQLite source-of-truth + version checks + tombstone ratio |
| `archive_store/tokenizer.py` | `to_no_diacritic` (handles đ/Đ) + underthesea segment (optional) |
| `archive_store/chunker.py` | Block-aware chunker — merge short adjacent blocks, split >500 words, log merge_reason |
| `archive_store/embedder.py` | `Embedder` — ONNX E5-small Mix50 backend, lazy load, idle unload |
| `archive_store/indexer.py` | `HybridIndex` — Tantivy (9 fields, per-field boost) + FAISS HNSW + tombstone bitmap |
| `archive_store/importer.py` | `Importer` — read folder + xlsx (Hồ sơ + Văn bản sheets), copy PDF, chunk, embed, index |
| `archive_store/repair.py` | Startup reconcile SQLite ↔ Tantivy ↔ FAISS |
| `archive_store/reranker.py` | `Reranker` — bge-reranker-v2-m3 cross-encoder, lazy load |
| `archive_store/filter_builder.py` | Build SQL WHERE clause from advance-filter UI dict |
| `archive_store/search_engine.py` | Hybrid pipeline — SQL scope → parallel Tantivy + FAISS → RRF k=60 → rerank (Accurate mode) |

### Utilities
| File | Purpose |
|------|---------|
| `file_utils.py` | Recursive PDF scanning, output dir creation |
| `portable_utils.py` | Frozen exe support, offline mode, path helpers |
| `translations.py` | i18n: English + Vietnamese |
| `src/preprocessing.py` | PDF image extraction, rotation/deskew fix |
| `kie_core/` | Runtime KIE ontology, validators, and postprocess helpers used by the app/viewer |
| `train-convert/` | Model train/convert decision records and reproducible scripts; not part of portable runtime |

### Setup / Build
| File | Purpose |
|------|---------|
| `build_portable.bat` | PyInstaller build script |
| `Lightweight_OCR.spec` | PyInstaller spec |
| `scripts/download_offline_models.py` | Download models for offline use |
| `scripts/fix_cpu_libs.bat` | Fix CPU library issues |

### Config & Data
| File | Purpose |
|------|---------|
| `settings.ini` | Runtime settings (gitignored) |
| `settings.ini.example` | Template settings |
| `dictionary.txt` | Custom word replacements |
| `ignored_words.txt` | Words to skip during correction |

## Key Dependencies
- **PySide6** — GUI framework
- **PyMuPDF (fitz)** — PDF rendering and manipulation
- **pikepdf** — PDF content stream editing
- **CTranslate2** — Fast CPU inference for text correction
- **Transformers** — Tokenizer for correction model
- **ONNX Runtime** — 4-angle document orientation classifier
- **DocLayout-YOLO** — Layout region detection
- **GMFT** — Table detection (Microsoft Table Transformer)
- **img2table** — Secondary table detection
- **Selenium** — Chrome OCR fallback
- **OpenCV** — Image processing in preprocessing

## Models (bundled in models/)
- `screen_ai/140.21/` — Google ScreenAI OCR (chrome_screen_ai.dll + tflite models)
- `proton_ct2_opt/` — Vietnamese correction model (CTranslate2)
- `doclayout_yolo_docstructbench_imgsz1024.pt` — Layout detection
- `gmft_models/` — Microsoft Table Transformer (detection + structure)

## Rules
- GUI framework: **PySide6** (NOT Tkinter)
- All temp/test/debug files go in `temp/` directory
- `temp/` is gitignored
- CPU-only: CUDA_VISIBLE_DEVICES="" set at startup
- If adding/removing a library, update `build_portable.bat` and `requirements_qt.txt`
- Settings changes must update `settings.ini.example` as reference
