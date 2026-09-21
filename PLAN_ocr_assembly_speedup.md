# KẾ HOẠCH: Tăng tốc pipeline OCR — overlay text layer, layout analysis, JSON-only Step 1

**Trạng thái:** ĐÃ TRIỂN KHAI WI-1 + WI-3 + guard C1 + WI-4 (env-only) theo REVIEW_ocr_assembly_speedup.md (2026-09-21) — xem mục 9. **WI-2 không triển khai** (số đo review §6.3: process pool chỉ nhanh hơn ~3,6% vì ONNX intra-op đã dùng hết nhân CPU); đểparked chờ hướng thiết kế lại.

**Phạm vi:** `scanindex/core/ocr/direct_engine.py` (chủ yếu), `scanindex/ui/digitization/split_step.py`, `scanindex/core/digitization/runner.py` (không đổi logic, chỉ hưởng lợi).
**File kích phát:** `temp/TH-K.V[15-20]-11-09022026215320.pdf` — 252 trang, scan Toshiba 300 DPI (2473×3497 px/trang), 240.7 MB, không text layer.

---

## 1. Bối cảnh & mục tiêu

Bước 1 "Số hóa lưu trữ" với file 252 trang mất **~10 phút**. Đo từng giai đoạn (engine thật, 2 OCR worker theo `settings-1.1.8.ini`, máy 12 CPU logic / 32 GB RAM) cho thấy OCR **không** phải thủ phạm chính:

| Giai đoạn | 1 trang | 252 trang | Ghi chú |
|---|---|---|---|
| OCR (2 workers, 240 DPI) | 0.55s effective | ~139s | raw ~1.1s/trang |
| **Assemble — dựng text overlay tàng hình** | **1.67s** | **~420s** | **thủ phạm, 73% tổng** |
| Assemble — `doc_out.save` | 0.083s | ~21s | |
| Assemble — canonical JSON (zstd) | 0.013s | ~3s | |
| LightGBM predict_doc_starts | ~0.01s | ~3s | + load model 1.5–6.6s |

Luồng PDF→Word còn tệ hơn (bật thêm layout analysis, chạy **tuần tự** trong vòng assemble):

| Giai đoạn PDF→Word | 252 trang | Ghi chú |
|---|---|---|
| Preprocess geometry | ~88s | 0.35s/trang, 2 workers |
| OCR | ~139s | |
| **Layout analysis (DocLayout-YOLO + DocLayNet)** | **~459s** | **1.82s/trang, tuần tự** |
| Text overlay | ~420s | cùng hàm với trên |
| **Tổng** | **~18 phút** | |

**Mục tiêu:**
1. Bước 1 (Số hóa): ~10 phút → **~2.5 phút** (floor = OCR 139s với 2 worker — không phá sàn này).
2. PDF→Word / ảnh→Word: ~18 phút → **~4–5 phút**.
3. **Không đổi chất lượng đầu ra**: text layer trích xuất phải giống hệt (đã có prototype chứng minh parity 100%).
4. Không tăng yêu cầu phần cứng; mặc định 2 OCR worker giữ nguyên (máy yếu an toàn).

---

## 2. Hiện trạng code (đã xác minh, kèm tham chiếu)

### 2.1 `_build_text_page_words` — `direct_engine.py:705-755`
Overlay từ cấp word, dùng tại **4 call site**: `process_pdf_for_docx_export` (dòng 1418), `process_pdf` (1634), `assemble_pdf_from_page_results` (1787), screenshot/secret-scan (2209). Với **mỗi từ**:
1. `page.insert_text(...)` → mỗi lần tạo **một content stream mới** (đo: 477 từ → 478 stream).
2. Đọc lại toàn bộ stream vừa tạo (`xref_stream`) rồi ghi lại (`update_stream`) kèm wrapper `BDC/EMC ActualText` (đo: sau vòng lặp 1 trang có tới 1430 stream object).

Chi phí tăng siêu tuyến tính theo số từ/trang. Đo micro (trang 1, 477 từ): tổng 1.69s = insert 1.04s (2.3ms/từ) + rewrite 0.44s. Trang dày (121, 670 từ): 2.56s.

### 2.2 Layout analysis trong `process_pdf` — `direct_engine.py:1574`
`_analyze_combined_layout_regions` (dòng 142-192) chạy **tuần tự từng trang** trong vòng assemble của riêng luồng PDF→Word (`include_layout_analysis=True`, `main_window.py:129`; các luồng Số hóa truyền `False`). Mỗi trang: re-render 240 DPI (dòng 150-153) + 2 model YOLO. Đo: init 1.6s, **1.82s/trang**. Các trang độc lập nhau hoàn toàn.

### 2.3 Bước 1 ghi một PDF 240MB không ai đọc lại — `split_step.py:940-948`
`assemble_pdf_from_page_results(path, "_step1_source_ocr.pdf", ...)` dựng bản PDF đầy đủ có text layer. Đã truy toàn bộ người tiêu thụ (`grep step1_ocr_pdf_path`): chỉ `session.py` lưu path + `split_step.py` set — **Step 2 không dùng file này** (Step 2 assemble lại per-segment từ page cache qua `pre_ocr_cache`, `main_window.py:2629` → `runner.py:597-605, 673-677`; thậm chí runner *từ chối* re-OCR nếu cache rỗng). Chỉ file `.json.zst` đi kèm được dùng: `predict_doc_starts` (`split_step.py:1005`) + secrecy cache (`split_step.py:794`). Nhánh fallback reload viewer (`split_step.py:779-784`) chỉ chạy khi page count lệch (không xảy ra trong thực tế).

### 2.4 OCR pool — `direct_engine.py:282-319`
`multiprocessing.Pool(maxtasksperchild=100)` — 252 trang / 2 worker → worker restart 2-3 lần giữa chừng, mỗi lần load lại DLL ScreenAI ~2s (tổng phí ~5-8s/file dài).

### 2.5 Những gì đã đúng và GIỮ NGUYÊN
- Step 2 tái dụng OCR Step 1 qua `pre_ocr_cache` + `_PrebakedAsyncResult` (`direct_engine.py:336`, `runner.py:673`) — bỏ qua cả preprocess lẫn pool warmup. **Không đề xuất thay đổi.**
- Step 2 chạy assemble trong process con (`runner.py:427-486`) — GUI không đơ. Step 1 chạy trong background thread (chấp nhận được, không đổi).
- Viewer render lười (`pdf_split_viewer.py:222-255`) — không phải bottleneck.
- Tách vật lý dùng `insert_pdf` (`pdf/splitter.py:59`) — rẻ (~ms/trang).

---

## 3. Work items

### WI-1 (P0): Batch hóa `_build_text_page_words` — 1 Shape + 1 lần wrap ActualText

**Thiết kế** (đã prototype thành công, `temp/probe_shape_batch.py` + `temp/probe_assemble_fast.py`):

```python
def _build_text_page_words(page, words_data, font_path, *, fallback_lines=None):
    if not words_data:
        _build_text_page_lines(page, fallback_lines or [], font_path)  # giữ nguyên
        return
    font = fitz.Font(fontfile=font_path)
    shape = page.new_shape()
    metas = []                                  # ActualText hex, đúng thứ tự insert
    for wd in words_data:
        # ... lọc & tính font_size/baseline/hscale GIỮ NGUYÊN như hiện tại ...
        shape.insert_text(baseline, text, fontname="F0", fontfile=font_path,
                          fontsize=font_size, render_mode=3,
                          morph=(baseline, fitz.Matrix(hscale, 1.0)))
        metas.append(actual_hex)                # chỉ append khi insert thành công
    shape.commit()                              # → đúng 1 content stream mới
    # Wrap ActualText trong 1 pass duy nhất trên stream vừa commit:
    xref = page.get_contents()[-1]
    content = page.parent.xref_stream(xref)
    # split theo token BT/ET, wrap mỗi block q/BT/.../ET/Q bằng BDC...EMC
    page.parent.update_stream(xref, wrapped)
```

**Cơ sở an toàn của bộ split (đã xác minh bằng probe `temp/probe_ascii_encoding.py`):** với font nhúng qua `fontfile`, PyMuPDF embed font dạng **Type0/Identity-H** và encode *mọi* từ — kể cả ASCII thuần như `BT`, `(x)`, `back\slash` — thành **hex glyph-ID** `<...>TJ`. Stream không bao giờ chứa literal string `(BT) Tj`, nên token `BT`/`ET` trong stream chắc chắn là toán tử text object, split theo byte an toàn. Mỗi `insert_text` sinh đúng 1 block `q/BT/.../ET/Q` theo đúng thứ tự gọi (verified 7/7 stream trong probe).

**Kết quả đo prototype (cùng máy, cùng trang):**

| Chỉ số | Bản gốc | Bản batch |
|---|---|---|
| Trang 1 (477 từ) | 1.69s | **0.12s (14×)** |
| Trang 121 (670 từ) | 2.56s | **0.13s (~20×)** |
| Assemble 8 trang end-to-end | 14.1s | **0.8s** |
| `get_text("words")` — text & bbox | — | **giống hệt**, 477/477 từ, lệch bbox 0.00pt |
| `get_text("text")` full page | — | **bằng hệt** |
| `predict_doc_starts` | `[0, 7]` | `[0, 7]` |
| Dung lượng PDF ra (8 trang) | 10.3 MB | 9.4 MB (ít stream object hơn) |

**Edge cases xử lý:**
- E1: `shape.insert_text` raise → bỏ qua từ đó, **không** append meta → số meta luôn khớp số block BT đã commit.
- E2: trang không có từ hợp lệ nào sau lọc → `commit()` vẫn được gọi nhưng `metas` rỗng → **return sớm, không đụng stream** (tránh đọc/ghi stream rỗng).
- E3: defense-in-depth — nếu số token BT tìm được ≠ `len(metas)` (do version PyMuPDF đổi format): **log warning + bỏ qua bước wrap**, giữ stream nguyên vẹn (hành vi = text có tìm kiếm/selection nhưng thiếu ActualText khoảng trắng — tương đương path `_build_text_page_lines` hôm nay, không sai dữ liệu).
- E4: chữ không có glyph (`has_glyph == 0` → .notdef), từ dài bật clamp hscale 0.05/20, `has_space_after` thiếu → logic lọc giữ nguyên bản gốc từng dòng.
- E5: từ chứa "BT"/"ET"/paren/backslash — an toàn theo phân tích encoding ở trên; sẽ có **unit test đối kháng** riêng (mục 6).
- E6: giữ nguyên fallback `_build_text_page_lines` (dòng 676) cho trang không có word data.

**Rủi ro & giảm thiểu:** thấp — cùng APIs PyMuPDF đang dùng (`Shape.insert_text` cùng chữ ký morph/render_mode với `Page.insert_text`; prototype đã chạy thực tế). Ràng buộc version PyMuPDF hiện dùng — ghi thêm assert nhẹ về số block BT (E3) để phát hiện sớm khi nâng PyMuPDF.

**Lợi hưởng:** cả 4 call site — Số hóa Bước 1 & 2, PDF→Word, ảnh→Word, DOCX-export, screenshot/secret-scan.

---

### WI-2 (P1): Song song hóa layout analysis trong `process_pdf` (chỉ luồng PDF→Word/ảnh→Word)

**Thiết kế — phương án O1 (process pool độc lập, rủi ro thấp):**
1. Tách vòng `process_pdf` (dòng 1564-1611) thành 2 pha: (a) build page records + background specs **chưa có** layout; (b) chạy layout song song rồi gắn `layout_regions` vào `page_data` theo đúng thứ tự trang.
2. Pool: `ProcessPoolExecutor(max_workers=N)`, `N = env OCRTOOL_LAYOUT_WORKERS`, default `min(2, cpu_count-2)`; worker init load analyzers **một lần mỗi process** (mẫu giống `_worker_init` của OCR pool, dòng 201).
3. Mỗi task nhận `(source_path, page_idx)` và **tự render 240 DPI trong worker** (giống OCR worker) — không truyền pixmap qua IPC.
4. Tôn trọng `OCRTOOL_DISABLE_DOCLAYNET_LAYOUT=1` (dòng 130) và đường "analyzer không khả dụng → `[]`" như hiện nay; giữ progress callback từng trang; hỗ trợ cancel.

**Phương án O2 (ghi nhận, chưa làm):** gộp layout vào chính OCR worker để tái dùng pixmap 240 DPI (tiết kiệm thêm ~0.15s/trang render + tránh render 2 lần) — nhưng đổi schema payload worker, tăng RAM worker (thêm 2 model YOLO), blast radius lớn. Để sau khi O1 ổn định.

**Kỳ vọng:** 252 trang × 1.82s = 459s tuần tự → ~115-230s tùy N=4/2. Kết quả layout phải **đ byte-đidentical** với bản tuần tự (model xác định, cùng render, cùng version) — đây là tiêu chí nghiệm thu cứng.

---

### WI-3 (P1): Chế độ JSON-only cho assemble ở Bước 1

**Thiết kế:**
1. Thêm param `pdf_output: bool = True` vào `assemble_pdf_from_page_results` (dòng 1657). Khi `False`: bỏ font lookup, `_append_backgrounds_preserving_inline_images`, toàn bộ overlay, `doc_out.save` — chỉ build `ocr_data` + `save_canonical`.
2. `split_step.py:940` truyền `pdf_output=False`. `session.step1_ocr_pdf_path` = `""`.
3. Nhánh fallback viewer `split_step.py:779-784`: khi không có PDF OCR, giữ nguyên source viewer + log (nhánh này vốn chỉ chạy khi page count lệch — hiếm).
4. Escape hatch: env `OCRTOOL_STEP1_BUILD_OCR_PDF=1` ép build đầy đủ như cũ (debug/diagnose).

**Lợi ích:** assemble Bước 1 còn ~0.03s/trang (chỉ canonical build) ≈ ~8s cả file; tiết kiệm ~240 MB disk churn mỗi session. **Lưu ý cho reviewer:** WI-3 làm phần lợi ích của WI-1 *tại riêng Bước 1* bị che khuất (overlay không còn chạy ở Bước 1), nhưng WI-1 vẫn bắt buộc vì Bước 2 assemble per-segment (đường cache), PDF→Word, DOCX-export, screenshot vẫn cần overlay nhanh.

**Bước 1 sau WI-1+WI-3:** ~139s OCR + ~8s JSON + ~3s predict ≈ **~2.5 phút**.

---

### WI-4 (P2): Micro-optimizations

1. `maxtasksperchild` 100 → 500, đọc từ env `DIRECT_OCR_MAXTASKSPERCHILD` (dòng 311). Lý do tồn tại của giá trị nhỏ = chống leak của DLL third-party; 500 vẫn bounded, giảm 2-3 lần reload DLL mỗi file 252 trang (~5-8s).
2. Warm OCR pool khi user mở tab "Số hóa lưu trữ" (background thread gọi `_get_pool()`), bỏ qua nếu env `OCRTOOL_DISABLE_POOL_WARMUP=1` — tiết kiệm ~2-5s ở lần drop file đầu tiên.

---

## 4. Những gì KHÔNG làm (kèm số đo bác bỏ)

| Ý tưởng | Số đo | Kết luận |
|---|---|---|
| Hạ OCR DPI 240→200/180 | Nhanh hơn chỉ 16% (4.3→3.6s/4trang); header "ĐẢNG BỘ QUẬN 7"→"ĐÁNG", vài trang lệch tới 2.3% ký tự — đúng loại dòng (tên cơ quan, "Số: …") mà KIE & doc-start ăn vào | **Bác bỏ** — giữ 240 DPI |
| Tăng mặc định `MaxConcurrentOCR` | Là setting per-machine đã có | **Không đổi mặc định** (máy yếu an toàn); ai muốn tự nâng |
| Pipelining Step 2 chạy sớm trong lúc Bước 1 còn OCR | Tiết kiệm vài chục giây, đổi lại race với cut points user đang sửa | **Bác bỏ** ở giai đoạn này |
| Bật GPU cho engine | App chủ trương CPU-only portable (`ocr_app.py:73`) | Ngoài phạm vi |
| Bỏ/đổi cơ chế Step 2 tái dụng OCR Step 1 | Đã hoạt động đúng (`runner.py:597-605`) | **Giữ nguyên** |

Ghi nhận thêm bối cảnh đã thống nhất: Bước 2 chạy standalone (folder mode) là đường hợp lệ khi đầu vào đã tách sẵn — đường đó có preprocess + OCR riêng như thiết kế, không thay đổi trong plan này.

---

## 5. Ưu tiên & ước lượng công sức

| WI | Ưu tiên | Công sức | Rủi ro |
|---|---|---|---|
| WI-1 overlay batch | P0 | ~0.5-1 ngày (code + test) | Thấp (đã có prototype parity) |
| WI-3 JSON-only Step 1 | P1 | ~0.5 ngày | Thấp (flag bật tắt được) |
| WI-2 layout song song | P1 | ~1-1.5 ngày | Trung bình (process pool + determinism) |
| WI-4 micro-opts | P2 | ~1 giờ | Thấp |

Target release: 1.1.9 (hiện 1.1.8). Mỗi WI một commit riêng để revert độc lập.

---

## 6. Kế hoạch kiểm thử & tiêu chí nghiệm thu

**Unit/regression (pytest, thêm mới):**
1. Parity overlay: với tập trang đại diện (thưa/dày/VN/ASCII), so sánh bản cũ vs mới về (a) `get_text("words")` — text + 4 tọa độ bbox (tolerance 0); (b) `get_text("text")` nguyên trang; (c) số ActualText span.
2. **Test đối kháng E5:** words = `["BT", "ET", "(x)", "EMC", "back\\slash", "qu)ote", "Trường", "verständnis", emoji?]` + word không glyph + word w≤0 + từ dài bật clamp hscale — xác nhận wrap đúng từng span, extraction đúng.
3. E3 mismatch-fallback: giả lập stream lạ (monkeypatch) → hàm không crash, stream không bị sửa.
4. Layout parallel: kết quả `layout_regions` đ identical với đường tuần tự trên 20 trang; determinism qua 2 lần chạy.
5. JSON-only: canonical JSON từ `pdf_output=True` vs `False` phải đ identical (cùng page results).

**Benchmark nghiệm thu (file 252 trang, máy 12 CPU/32GB, 2 worker):**

| Luồng | Trước | Sau (chấp nhận) |
|---|---|---|
| Bước 1 Số hóa | ~10 phút | **≤ 3 phút** |
| Assemble 8 trang | 14.1s | **≤ 1.5s** |
| PDF→Word | ~18 phút | **≤ 6 phút** |
| Kích thước `_step1_source_ocr*` | ~240 MB | **không còn file PDF** (chỉ JSON ~3 MB) |

**Kiểm thủ công (gate trước khi merge):** mở PDF xuất ra bằng Adobe Reader + Chrome + Edge: bôi đen copy giữ khoảng trắng (ActualText), Ctrl+F tìm từ có dấu, text layer khớp vị trí ảnh.

**Chạy lại toàn bộ pytest hiện có** (`pytest.ini`) — không regression.

---

## 7. Triển khai & rollback

1. Thứ tự merge: WI-1 → WI-4 → WI-3 → WI-2 (WI-3 phụ thuộc hiểu WI-1; WI-2 độc lập).
2. Rollback từng WI = revert commit riêng; WI-3 có thêm env escape hatch không cần revert code.
3. Log thêm 1 dòng timing từng pha trong `assemble_pdf_from_page_results` (đã có log `[step1] assembled ... in Xs` — bổ sung tương tự cho layout phase) để giám sát sau release.

---

## 8. Phụ lục: môi trường & script đo

- Máy đo: 12 CPU logic, 32 GB RAM (~11.5 GB rảnh), Python 3.12, PyMuPDF bản hiện hành của repo, ScreenAI model dir 337 MB.
- Script đo (giữ lại trong `temp/` để reproduce):
  - `probe_step1_pdf.py` — cấu trúc PDF; `bench_step1_pipeline.py` — timing 4 giai đoạn; `probe_assemble_breakdown.py` — breakdown assemble; `probe_word_overlay_micro.py` — micro insert/rewrite; `probe_shape_batch.py` — prototype WI-1 + parity; `probe_assemble_fast.py` — end-to-end WI-1; `probe_ascii_encoding.py` — chứng minh encoding hex (cơ sở an toàn split BT); `probe_dpi_tradeoff.py` — bác bỏ hạ DPI; `probe_step2only_costs.py` — chi phí layout 1.82s/trang & preprocess 0.35s/trang.
- Các số liệu trong plan đều đo trên chính file của user, engine thật, cấu hình thật (2 worker).

---

## 9. Trạng thái triển khai (cập nhật sau review 2026-09-21)

Thứ tự triển khai theo review §8: **WI-1 → WI-3 → guard C1 (commit correctness riêng) → WI-4 (env-only)**. WI-2 parked. Không có `git.exe` trong môi trường triển khai — các thay đổi đã tách theo ranh giới commit trên đĩa, cần commit riêng khi có git:

1. **WI-1** (`scanindex/core/ocr/direct_engine.py`):
   - `_build_text_page_words` mới = batch (một Shape + một content stream) + wrap ActualText một pass; `_build_text_page_words_legacy` giữ nguyên code cũ làm fallback & reference.
   - Đáp ứng review §4.2: staging trên scratch page (validate số block BT/ET trước khi đụng trang thật); nếu staging/wrap fail → chạy legacy trên trang chưa bị đụng; nếu bytes sau commit lệch staging → **empty-stream rollback** (xóa sạch overlay vừa thêm, giữ nguyên nền) rồi chạy legacy — không bao giờ nhân đôi overlay hay mất spacing.
   - Đáp ứng §4.3: không đọc `get_contents()[-1]` khi không có từ hợp lệ; giữ nguyên bộ lọc cũ (không thêm lọc glyph); fallback line chỉ khi `words_data` rỗng.
   - Kiểm chứng: parity legacy-vs-batch trên file thật (trang 1/121/252): text + word tuples + rendered pixels bằng hệt; 8-11× trên trang dày (1.70s→0.22s).
2. **WI-3** (`direct_engine.py` + `split_step.py`):
   - `assemble_pdf_from_page_results(..., pdf_output=True)`; `pdf_output=False` chỉ build canonical JSON — bỏ font/background/overlay/save, giữ nguyên page map, normalize tọa độ, profile. Payload entry point default `True` (payload thiếu field vẫn build PDF).
   - Đáp ứng review §5.1: signal `_ocr_finished` mang riêng `canonical_json_path`; session set trực tiếp từ payload; prediction + secrecy cache dùng canonical path trực tiếp (bỏ resolve gián tiếp qua PDF).
   - Đáp ứng §5.3: viewer page-count mismatch → thử load lại source PDF trước, rồi OCR PDF (nếu có), log rõ khi không phục hồi được; đổi text "Đang dựng PDF OCR" → "Đang dựng dữ liệu OCR" ở cả 2 chỗ; escape hatch `OCRTOOL_STEP1_BUILD_OCR_PDF=1` đọc một lần mỗi run, dùng nhất quán cho assemble + payload + fallback viewer.
   - Kiểm chứng: canonical JSON-only == full build (bỏ `pipeline.ocr.completed_at`); `resolve_companion` resolve sidecar khi PDF không tồn tại; E2E cả 2 mode đều chạy `predict_doc_starts` được.
3. **Guard C1** (`direct_engine.find_failed_page_results` + gọi trong worker Step 1 trước assemble/predict): phân biệt trang trắng hợp lệ (có render dims) với trang lỗi (missing / worker_init_failed / error); có lỗi thì fail run với thông điệp "OCR thất bại trên N/T trang…" thay vì xuất canonical rỗng. Hành vi mutate-input của assembler (review C2) giữ nguyên như cũ; mọi test A/B dùng deepcopy.
4. **WI-4**: chỉ thêm env `DIRECT_OCR_MAXTASKSPERCHILD` (default 100 giữ nguyên theo review §7.2). Warmup bỏ (đã tồn tại qua GROUP_CORE_OCR — review §7.1).

**Tests:** 379 passed (`.venv_build`, gồm 16 test mới: parity dense/sparse/adversarial, ActualText spacing, all-filtered no-stream, 2 fallback/rollback monkeypatch, JSON-only == full build, sidecar resolve, payload default, guard classification). Log đủ: `tests/test_ocr_text_layer.py`, `tests/test_archive_step1_json_only.py`.
