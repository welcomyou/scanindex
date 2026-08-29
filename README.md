# ScanIndex

Desktop OCR + KIE + searchable archive cho văn bản hành chính tiếng Việt
(Windows, PySide6, CPU-only).

Pipeline chính:

```
PDF (scan/digital)
   → preprocess (rotate / deskew / orientation)
   → OCR  (Chrome ScreenAI, offline DLL, Authenticode-verified)
   → text correction  (CTranslate2, distilled-protonx)
   → layout + tables  (DocLayout-YOLO + GMFT/Docling TableFormer)
   → KIE  (LayoutLMv3 fine-tune trên văn bản hành chính VN)
   → PDF/A + ký số  (pyHanko, Windows Cert Store, TSA)
   → searchable PDF + DOCX export
   → indexed search archive  (Tantivy + SQLite, full-text + filters)
```

Các màn hình UI chính:

- **Chuyển scan PDF → Word** — drag-drop OCR đơn lẻ, xuất searchable PDF + DOCX
- **Số hóa lưu trữ** — pipeline 3 bước: split PDF dài → KIE/metadata → ký số + đóng gói HSLTCQ
- **Kho lưu trữ** — search metadata + full-text trên kho nội bộ
- **Đo độ chính xác OCR** — so PDF OCR vs ground truth (CER/WER)
- **Phát hiện file mật** — quét folder, OCR trang đầu, nhận dạng dấu MẬT/TỐI MẬT/TUYỆT MẬT
- **Công cụ hỗ trợ** — utilities khác

## Lưu ý bản quyền: Google ScreenAI

ScanIndex hiện dựa vào một runtime component thuộc hệ sinh thái Google/Chrome:

- **Chrome ScreenAI** — DLL/runtime OCR, được lấy qua
  [scanindex/core/ocr/screen_ai_downloader.py](scanindex/core/ocr/screen_ai_downloader.py)
  (tải trực tiếp từ Google CDN, verify SHA256 theo updater XML của Google + chữ ký
  Authenticode) hoặc copy từ thư mục Chrome local, dùng để OCR offline.

Component này **không thuộc sở hữu của dự án ScanIndex** và không được cấp
license bởi README này. Google, Chrome, ScreenAI và các binary/model liên quan
thuộc quyền sở hữu/điều khoản của Google LLC hoặc các bên cấp phép tương ứng.
Cơ chế tải/copy trong repo chỉ kiểm tra nguồn gốc và tính toàn vẹn file; việc
verify SHA256 hoặc Authenticode **không tạo thêm quyền sử dụng, phân phối hoặc
thương mại hóa** component đó.

### Chính sách phân phối (bắt buộc)

- **Repo GitHub và bản release chỉ chứa source code + downloader.** KHÔNG đính
  kèm `chrome_screen_ai.dll`, model ScreenAI, hay bất kỳ binary/model nào của
  Google vào release assets, archive portable, hoặc kho khác. Người dùng cuối
  tự tải component từ Google CDN qua downloader khi chạy lần đầu (giống cơ chế
  component updater của Chrome).
- Bản portable build cục bộ (có sẵn ScreenAI) chỉ dùng **cá nhân/nội bộ**.

Dự án này được dùng cho **quy trình số hóa tài liệu nội bộ, không thương mại**.
Không dùng bản portable/release chứa ScreenAI để bán lại, cung cấp dịch vụ
thương mại, SaaS, hoặc phân phối như một sản phẩm công khai khi chưa có đánh
giá pháp lý và quyền sử dụng/phân phối phù hợp từ Google. Nếu cần triển khai
thương mại, hãy thay engine Google/Chrome bằng engine có license rõ ràng cho
mục đích đó, hoặc xin quyền sử dụng riêng.

Người vận hành cần tự bảo đảm việc sử dụng phù hợp với
[Google Terms of Service](https://policies.google.com/terms) và
[Google Chrome and ChromeOS Additional Terms of Service](https://www.google.com/chrome/terms/).

## Cài đặt từ source

```powershell
git clone https://github.com/welcomyou/scanindex.git
cd scanindex
python -m venv .venv_build
.venv_build\Scripts\activate
pip install -r requirements.txt
```

### Tải model (~2.1 GB) — có verify SHA256

```powershell
python scripts\download_offline_models.py
```

Script kéo từng repo HF về `models/`, tải bootstrap ***REMOVED*** từ GitHub
Release, sau đó **verify SHA256 từng file** theo bảng cứng trong
[scripts/download_offline_models.py](scripts/download_offline_models.py). Mỗi
repo HF cũng pin `revision=<commit_sha>`; bootstrap ***REMOVED*** pin URL asset,
SHA256 archive và SHA256 từng file. ***REMOVED*** là component Google/Chrome; phần
verify này chỉ xác nhận đúng artifact đã pin, không phải giấy phép phân phối hay
thương mại hóa. Hash mismatch → script raise `ModelIntegrityError` và dừng.

ScreenAI tải từ Google CDN qua [scanindex/core/ocr/screen_ai_downloader.py](scanindex/core/ocr/screen_ai_downloader.py). Downloader kiểm tra host Google + URL HTTPS, SHA256 64 ký tự do Google updater XML công bố hoặc fallback Google CDN được pin cứng, và chữ ký Authenticode của DLL Google.
Đây cũng chỉ là kiểm tra provenance/tamper-resistance; quyền sử dụng ScreenAI vẫn
phụ thuộc điều khoản Google/Chrome.

Kiểm tra manifest URL/hash mà không tải file:

```powershell
python scripts\download_offline_models.py --validate-config
```

Sau khi retrain + re-upload model nào đó, regen lại hash anchor:

```powershell
python scripts\refresh_model_hashes.py --apply
```

### Chạy

```powershell
python ocr_app.py
```

## Build portable EXE

```powershell
build_portable.bat
```

### Portable updates (version-per-file)

> Từ 1.1.4, ScanIndex dùng **version-per-file** để chống mất dữ liệu khi update
> bằng copy-đè folder. Mỗi release ghi config/DB vào tên file mang version
> (`settings-1.1.4.ini`, `repository/repository-1.1.4.db`), khác tên với bản cũ
> → copy đè folder không bao giờ ghi đè data thật. Xem chi tiết trong
> [BUILD_AND_VERSIONING.md](BUILD_AND_VERSIONING.md).

**Cách nâng cấp (cho người dùng cuối):**

1. Tải bản mới (`.7z`), giải nén.
2. Đóng app cũ nếu đang mở.
3. Copy **toàn bộ nội dung** folder bản mới đè vào folder bản cũ (Windows
   Explorer merge folder, không xóa file cũ).
4. Mở app — dữ liệu cũ (settings, kho lưu trữ, file PDF) được tự động chuyển
   sang tên file mới. Không cần làm gì thêm.

**An toàn ngay cả khi lỡ mở bản mới trước rồi mới copy đè:** app phát hiện cả
file mới (rỗng/default) và file cũ (có data), tự lấy data cũ. Khởi động lần đầu
trên thư mục sạch không tạo file nào, nên copy đi đâu cũng vô hại.

The dist payload contains config samples only (`.example` files). For GitHub
Releases, upload a 7z made from the generated `dist/ScanIndex-<version>/`
folder only, not from the source tree or an old dist.

Output ở `dist/ScanIndex-<version>/` (auto-derived từ `git describe`). Spec: [Lightweight_OCR.spec](Lightweight_OCR.spec).

Auto-versioning đi theo git tag SemVer (xem [scanindex/infra/version.py](scanindex/infra/version.py)):

```powershell
git tag v1.1.0          # → bundle dist\ScanIndex-1.1.0\
# 3 commits sau v1.1.0  → dist\ScanIndex-1.1.0\ + VERSION="1.1.0+3.<hash>"
```

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
| [scripts/](scripts/) | Download/upload models, refresh SHA256 anchor, benchmark, tooling |
| [train-convert/](train-convert/) | Decision records + scripts để retrain / re-export model (artifacts và data train không kèm) |

Tests đang gitignored — pytest chạy local từ working tree của developer, không ship lên repo public (test fixtures có thể chứa OCR văn bản hành chính thật).

## Models

Tổng hợp ở Collection [welcomyou/scanindex](https://huggingface.co/collections/welcomyou/scanindex). Pin SHA256 trong [scripts/download_offline_models.py](scripts/download_offline_models.py) cho các repo đang dùng:

| Repo | Vai trò | Trạng thái |
|---|---|---|
| [welcomyou/layoutlmv3-vn-admin-kie](https://huggingface.co/welcomyou/layoutlmv3-vn-admin-kie) | KIE LayoutLMv3 fine-tune | active |
| [welcomyou/distilled-protonx-vn-correction-ct2](https://huggingface.co/welcomyou/distilled-protonx-vn-correction-ct2) | Correction CTranslate2 | active |
| [welcomyou/lightgbm-vn-page-splitter](https://huggingface.co/welcomyou/lightgbm-vn-page-splitter) | Tách văn bản trong batch + chọn trang ký | active |
| [welcomyou/doclayout-yolo-onnx-dynamic](https://huggingface.co/welcomyou/doclayout-yolo-onnx-dynamic) | Layout YOLO (dynamic ONNX, DocStructBench + DocLayNet) | active |
| [welcomyou/gmft-tatr-onnx](https://huggingface.co/welcomyou/gmft-tatr-onnx) | Bảng — TATR detection + structure | active |
| [welcomyou/docling-tableformer-v1-onnx-stepcache](https://huggingface.co/welcomyou/docling-tableformer-v1-onnx-stepcache) | Bảng — Docling TableFormer (stepcache) | active |
| [welcomyou/scanindex-models](https://huggingface.co/welcomyou/scanindex-models) | Bundle nhỏ: PaddleOCR orientation classifier | active |
| [ScanIndex ***REMOVED***](https://github.com/welcomyou/scanindex/releases/tag/***REMOVED***) | ***REMOVED*** EN↔VI bootstrap, pin SHA256 archive + từng file | active |
| [welcomyou/e5-small-vn-archive-mix50](https://huggingface.co/welcomyou/e5-small-vn-archive-mix50) | Multilingual E5 embedder (semantic search) | **dormant** — code search đã chuyển sang Tantivy + SQLite, model giữ trên HF cho lần revive |
| `BAAI/bge-reranker-v2-m3` (upstream) | Cross-encoder rerank cho semantic search | **dormant** — không wire trong UI hiện tại |

Mọi model semantic vẫn còn trên HF + train-convert scripts ([train-convert/archive-embedder/](train-convert/archive-embedder/)) để dễ bật lại sau. Bộ "dormant" không nằm trong `MODELS_CONFIG` nên `download_offline_models.py` không tự kéo.

Chrome ScreenAI OCR DLL nằm ngoài HF — auto download từ Google CDN bởi [scanindex/core/ocr/screen_ai_downloader.py](scanindex/core/ocr/screen_ai_downloader.py), kèm SHA256 từ Google updater XML hoặc fallback CDN pin cứng và Authenticode verify để chắc DLL ký bởi Google LLC.

Upload model sau khi retrain:

```powershell
huggingface-cli login
python scripts\upload_models_to_hf.py            # tất cả
python scripts\upload_models_to_hf.py --only welcomyou/layoutlmv3-vn-admin-kie   # 1 repo
python scripts\upload_models_to_hf.py --dry-run  # xem trước
```

## Settings

`settings.ini` is created automatically from `settings.ini.example` on first
launch if it does not exist. Existing runtime config is never overwritten by
an update.

Edit `settings.ini` để chỉnh runtime config (ngôn ngữ, correction model, số
worker, v.v.). `settings.ini` được gitignored.

## Phụ thuộc chính

PySide6 · PyMuPDF · pikepdf · CTranslate2 · Transformers · ONNX Runtime ·
DocLayout-YOLO · GMFT · OpenCV · LightGBM · tantivy · pyHanko · pywin32

## License

Code: TBD; hiện chưa công bố open-source/commercial license cho việc dùng lại bên
ngoài. Mặc định repo và bản portable/release được dùng cho **nội bộ, không thương
mại**.

Model weights của ScanIndex: xem từng subdir/repo tương ứng trong
[welcomyou/scanindex-models](https://huggingface.co/welcomyou/scanindex-models#licenses)
và các repo Hugging Face được liệt kê ở trên; một số model upstream/base có thể
có giới hạn non-commercial riêng.

Google/Chrome ScreenAI: không nằm trong license của ScanIndex. Mọi quyền, nhãn
hiệu, binary/runtime/model và điều khoản sử dụng thuộc Google LLC hoặc chủ sở
hữu tương ứng. README này không cấp quyền phân phối, thương mại hóa,
sublicensing, hoặc sử dụng ngoài phạm vi nội bộ/không thương mại.
