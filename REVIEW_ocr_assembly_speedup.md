# Phản hồi kế hoạch tăng tốc OCR, dựa trên review code và kiểm chứng thực tế

Ngày review: 2026-09-21. Code tại commit `d8ea955dc13932c710ff1670f369be04049b6f3e`.

Tài liệu được trả lời: [PLAN_ocr_assembly_speedup.md](D:/App/ocrtool/PLAN_ocr_assembly_speedup.md). Đường dẫn `PLAN/_ocr/_assembly/_speedup.md` trong yêu cầu ban đầu không tồn tại; bản plan thực tế nằm ở thư mục gốc repository.

Review này đi từ code thực thi, dữ liệu thử và kết quả chạy, rồi mới đối chiếu các WI. Không thay đổi code production hoặc plan gốc. Probe và dữ liệu kiểm chứng nằm trong `temp/`.

## 1. Quyết định đối với từng WI

| Hạng mục | Kết luận từ code | Phản hồi |
|---|---|---|
| WI-1: batch word overlay | Bottleneck có thật; prototype đạt parity trên các mẫu đã kiểm chứng và tăng tốc mạnh trên trang dày | Chấp thuận hướng thiết kế, phải thay fallback E3 và hoàn thiện xử lý stream/exception trước khi merge |
| WI-3: JSON-only Step 1 | Step 2 không cần PDF OCR toàn nguồn; JSON và page cache mới là dữ liệu tiêu thụ | Chấp thuận, cần cập nhật hợp đồng kết quả với UI và giữ nguyên page mapping/canonical |
| WI-2: layout process pool | Plan chỉ nhầm nhánh thực thi; phép chia thời gian cho số worker chưa được thực nghiệm hỗ trợ | Thiết kế lại phạm vi, giới hạn tài nguyên, lifecycle và benchmark trước khi triển khai |
| WI-4: warmup/recycle | Warmup khi vào Số hóa đã tồn tại; lợi ích và bộ nhớ của recycle 500 chưa được đo | Không thêm warmup trùng; chưa đổi default recycle |

Không chấp thuận cam kết PDF→Word 4–5 phút hoặc gate ≤6 phút với thiết kế và bằng chứng hiện có. Step 1 ≤3 phút là mục tiêu có cơ sở hơn, nhưng chưa được lần review này xác nhận bằng một lần chạy UI đủ 252 trang.

## 2. Bản đồ thực thi đã truy từ code

### 2.1. Số hóa Step 1

```text
ArchiveStep1Split._start_background_ocr
  -> submit_page(source_pdf, source_page_index), queue tối đa 16 mặc định
  -> ArchiveSession.cache_page: ghi từng page result xuống .json.zst
  -> session.cache_slice(range(page_count)): view đọc cache theo nhu cầu
  -> assemble_pdf_from_page_results(..., profile=layoutlmv3_runtime,
                                     include_layout_analysis=False)
  -> _step1_source_ocr.pdf + _step1_source_ocr.pdf.json.zst
  -> predict_doc_starts đọc JSON
  -> _on_ocr_finished: resolve JSON từ tên PDF, cập nhật cut points/secrecy cache
```

Bằng chứng: [submit/cache](D:/App/ocrtool/scanindex/ui/digitization/split_step.py:863), [assemble](D:/App/ocrtool/scanindex/ui/digitization/split_step.py:937), [prediction](D:/App/ocrtool/scanindex/ui/digitization/split_step.py:1005), [completion](D:/App/ocrtool/scanindex/ui/digitization/split_step.py:742), [cache trên disk](D:/App/ocrtool/scanindex/core/digitization/session.py:275).

Page cache hiện không phải một dict đầy đủ nằm sẵn trong RAM. `_OcrCacheSlice.get(local_idx)` ánh xạ sang source page, rồi đọc page cache. JSON-only vẫn phải đọc cache, metadata trang, tính source SHA-256 và chạy canonical finishing/compression; không có nghĩa là bỏ hết I/O hoặc toàn bộ bộ nhớ theo số trang.

### 2.2. Step 1 chuyển sang Step 2

```text
_physically_split: tách các segment từ session.source_pdf
  -> FileSpec(source_document_path=source_pdf,
              source_page_indices=source_pages,
              pre_ocr_cache=session.cache_slice(source_pages), from_step1=True)
  -> runner bỏ preprocess và OCR-pool warmup
  -> _PrebakedAsyncResult cho từng trang có cache
  -> assemble per-segment trong process con
  -> PDF/JSON riêng của segment -> correction/KIE/output
```

Bằng chứng: [tách từ source](D:/App/ocrtool/scanindex/ui/digitization/split_step.py:666), [FileSpec](D:/App/ocrtool/scanindex/ui/main_window.py:2619), [bỏ warmup](D:/App/ocrtool/scanindex/core/digitization/runner.py:549), [cache adapter](D:/App/ocrtool/scanindex/core/digitization/runner.py:670), [assemble process](D:/App/ocrtool/scanindex/core/digitization/runner.py:427).

Đã truy tên `step1_ocr_pdf_path` và `step1_ocr_json_path`: hiện là state được set/reset; Step 2 không đọc PDF OCR toàn nguồn qua các field này. Vì vậy nhận định cốt lõi của WI-3 là đúng.

Hợp đồng cần giữ: page result dùng **index cục bộ của segment**, `source_page_indices` chọn **trang nguồn**. Ví dụ segment lấy source `[120,121]` thì cache/record đầu ra vẫn có key/index `[0,1]`. Không thay bằng source index trong canonical segment.

### 2.3. PDF/ảnh → Word

```text
_run_source_to_ocr_pdf -> preprocess
  -> process_pdf(canonical_profile="docx_export")
    -> return process_pdf_for_docx_export(...)
      -> build_page_source_manifest(original_pdf, visual_pdf sau preprocess)
      -> OCR riêng các trang scan/mixed; giữ native text cho digital
      -> layout tuần tự trên visual_doc[page_idx]
      -> background + overlay + canonical docx_export
  -> correction nếu bật
  -> table extraction / DOCX export ở các stage tiếp theo
```

Bằng chứng: [profile ở UI](D:/App/ocrtool/scanindex/ui/main_window.py:120), [dispatch return sớm](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:1478), [manifest](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:1251), [layout DOCX](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:1380).

Layout tại `process_pdf:1574` không phải vòng lặp mà luồng Word này đi qua. Ngoài ra manifest có render 96 DPI để fingerprint từng visual page: [page_visual_sha256](D:/App/ocrtool/scanindex/core/pdf/docx_page_manifest.py:101), [vòng manifest](D:/App/ocrtool/scanindex/core/pdf/docx_page_manifest.py:156). Chi phí này không có trong bảng dự phóng của plan.

### 2.4. Phạm vi overlay thực tế

Bốn call site trực tiếp của `_build_text_page_words` là:

1. `process_pdf_for_docx_export:1418`.
2. `process_pdf:1634`.
3. `assemble_pdf_from_page_results:1787`.
4. `rebuild_pdf_with_text:2209`, phục vụ dựng lại PDF sau thay text.

Call site thứ tư không phải screenshot/secret-scan như plan ghi. Secret-scan có [đường dựng canonical riêng](D:/App/ocrtool/scanindex/ui/screens/secret_file_scan_screen.py:862), sử dụng OCR rồi trả JSON, không gọi word-overlay trong hàm đó. Không tính lợi ích WI-1 cho những tác vụ chỉ OCR/text/JSON mà không đi qua bốn call site trên.

## 3. Phát hiện trong code hiện tại cần tính vào thiết kế

### C1 — [P1] Step 1 và assembler có thể coi trang OCR lỗi là trang rỗng hợp lệ

**Đây là hành vi hiện có, không phải regression đã xảy ra do plan.**

Tại [split_step.py:906](D:/App/ocrtool/scanindex/ui/digitization/split_step.py:906), exception của `ar.get()` được chuyển thành `None`, nhưng `done` vẫn tăng. Payload `{worker_init_failed: True, error: ...}` khác `None` còn được cache như kết quả thường. Sau vòng OCR không có validation tương đương guard của Step 2 trước khi assemble/predict.

Tại [direct_engine.py:1730](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:1730), cache thiếu hoặc result rỗng được thay bằng lists rỗng và render dimensions bằng 0. Payload init-failed cũng đi tới record rỗng qua `.get(..., default)`. Assembler tiếp tục ghi PDF/canonical rồi trả `(True, None)`.

Probe một trang trên code thật cho kết quả:

| Page results | Assembler hiện tại | Canonical |
|---|---|---|
| `{}` | Thành công | 1 trang, 0 từ |
| `{0: {worker_init_failed: True, error: "probe"}}` | Thành công | 1 trang, 0 từ |
| OCR hợp lệ của trang trắng, có render dimensions | Thành công | 1 trang, 0 từ |

Như vậy không thể dùng số trang JSON hay file đã ghi để khẳng định OCR đầy đủ. Prediction có thể chạy trên nội dung thiếu; ở nhánh thiếu cache, UI hoàn tất và dòng đếm cache còn có thể không nhất quán. Step 2 đã có [guard cache thiếu/lỗi](D:/App/ocrtool/scanindex/core/digitization/runner.py:804), nên lỗi bị phát hiện muộn hơn.

**Phản hồi WI-3:** thêm guard ở Step 1 trước khi xuất canonical và prediction, hoặc một validator dùng chung có phạm vi rõ ràng. Phân biệt trang trắng hợp lệ với OCR thất bại; không dùng `words == []` làm tiêu chí duy nhất. Trả lỗi có số trang và không công bố completion thành công. Việc thêm guard nên thành commit correctness riêng, không trộn vào số đo tăng tốc.

### C2 — [P2] Assembly sửa page results tại chỗ; retry/parity test có thể mất metadata xoay

[Assembler lấy trực tiếp lists từ result](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:1736), rồi gọi [_normalize_page_coord_to_top_left](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:958), vốn sửa `y` và `bbox` tại chỗ.

Probe gọi assembler hai lần với cùng object chứa 6 dòng bottom-left cho kết quả:

| Lần gọi | `coord_origin_source` | `first_y` trong input sau gọi |
|---|---|---:|
| 1 | `normalized_from_bottom_left` | 240 |
| 2 | không còn field | 240 |

Lần 2 không phát hiện flip nữa, nên mất cả quyết định `bake_angle=180` khi dựng background. Điều này quan trọng với API reuse/retry trên dict và với test parity gọi hai chế độ liên tiếp.

**Giới hạn kết luận:** Step 1 bình thường đọc lại cache đã ghi disk qua `_OcrCacheSlice`, nên probe này không chứng minh mọi lần chuyển Step 1→2 đều lỗi. Nhánh DOCX còn có [deepcopy sẵn](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:1195), không mắc đúng kiểu aliasing này.

**Phản hồi WI-3:** luôn dùng input copies độc lập trong A/B tests. Khi tách helper build canonical, cân nhắc làm helper không sửa input hoặc quy định rõ ownership; nếu sửa hành vi production, thêm regression test riêng cho reuse và ảnh/background xoay.

### C3 — Giữ nguyên logic bảo toàn background và native text

[_append_backgrounds_preserving_inline_images](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:897) batch `insert_pdf` theo dải liên tiếp để giữ inline/transparency image layers. Các trang có scan image + text layer hoặc cần bake rotation đi nhánh khác. Đây không phải boilerplate có thể đơn giản hóa tùy ý khi refactor.

WI-1 chỉ đổi overlay. WI-3 chỉ bỏ toàn bộ nhánh PDF khi `pdf_output=False`. Những luồng còn tạo PDF phải giữ batching background, rotation, annotation/stamp và filter OCR/native của DOCX.

Runner mặc định isolate assemble, nhưng có [fallback về process hiện tại](D:/App/ocrtool/scanindex/core/digitization/runner.py:473). Vì vậy mô tả “Step 2 chạy process con nên GUI không đơ” chỉ đúng cho đường bình thường, không phải bảo đảm tuyệt đối cho mọi failure path.

## 4. Phản hồi WI-1: giữ hướng batch, sửa điều kiện an toàn

### 4.1. Đã xác nhận lợi ích bằng code thật và file thật

Runtime probe: Python 3.12.0, PyMuPDF 1.26.7, Arial Windows, 12 logical / 6 physical CPU, khoảng 31,86 GiB RAM. PyMuPDF khớp pin trong `requirements.txt`.

Dữ liệu OCR được lấy bằng ScreenAI thật ở 240 DPI từ file 252 trang nêu trong plan. Mỗi bản cũ/batch nhận deepcopy cùng page result; đo overlay riêng, lưu PDF `deflate=True, garbage=4`, mở lại rồi so sánh text, toàn bộ tuples `get_text("words")` và pixel render. Timing dưới đây là một lần chạy mỗi trường hợp, không phải median hoặc SLA.

| Trang | OCR input words | Extracted words, cũ = batch | Overlay cũ | Batch prototype | Streams cũ / batch |
|---:|---:|---:|---:|---:|---:|
| 1 | 477 | 477 | 1,839 s | 0,134 s | 1430 / 2 |
| 121 | 670 | 673 | 2,773 s | 0,144 s | 2009 / 2 |
| 252 | 74 | 74 | 0,112 s | 0,062 s | 221 / 2 |

Cả ba mẫu: text bằng nhau, tuples word/bbox bằng nhau, pixel render không đổi. Ở trang 121, 670 input records thành 673 extracted words ở cả hai bản: tiêu chí đúng là parity giữa implementations, không phải bắt số extracted words luôn bằng số OCR records.

Sáu case tổng hợp cũng đạt parity sau save/reopen: bbox sát nhau; tokens `BT/ET/EMC`, ngoặc/backslash, Việt/Đức, emoji/CJK/combining mark/newline; rotation 90/180/270; background đã có native text. Ảnh render không đổi ở cả hai bản. Đây là parity với hành vi cũ, không phải chứng minh mọi glyph hoặc mọi bbox của hành vi cũ đều đúng tuyệt đối.

Trong PyMuPDF cài tại máy, `Page.insert_text()` tạo `Shape`, gọi `Shape.insert_text()`, rồi commit từng lần. `Shape.commit()` gọi `page.wrap_contents()` trước khi gắn stream. Với code hiện tại, số streams quan sát lớn hơn “một stream mỗi từ”; batch giảm cả số lần commit/wrap lẫn rewrite stream. Cần phân biệt `len(page.get_contents())` với tổng xref objects trong document khi báo metric.

### 4.2. [P1] E3 hiện đề xuất làm mất nội dung spacing

Probe dùng chính bbox của test spacing hiện có:

```text
Code cũ:                   "Vũng Tàu\n" -> 2 extracted words
Batch có ActualText:       "Vũng Tàu\n" -> 2 extracted words
Batch không có ActualText: "VũngTàu\n"  -> 1 extracted word
```

Vì vậy “warning rồi bỏ wrap nhưng vẫn thành công” không đạt yêu cầu không đổi đầu ra. So sánh với line fallback không phù hợp: line fallback chứa khoảng trắng trong nguyên chuỗi dòng, còn E3 vẫn là các text object từng từ.

**Yêu cầu sửa:** giữ implementation cũ làm fallback; nếu batch không validate được, bỏ batch overlay và dựng lại bằng đường cũ. Không được fallback bằng cách chèn thêm text lên batch đã commit. Thiết kế cụ thể phải bảo đảm rollback chỉ loại phần overlay vừa thêm và không mất nội dung nền. Có thể chuẩn bị/validate trên staging trước khi gắn kết quả vào trang đích.

### 4.3. Những điều cần ghi rõ trong implementation contract

- Chỉ sửa stream mới của batch; không parse/sửa toàn bộ content streams nền.
- Validate số lượng, thứ tự và biên block hoàn chỉnh trước khi update. Count `BT` là cần thiết nhưng không chứng minh một mình rằng mapping metadata đúng.
- Metas rỗng: không đọc `get_contents()[-1]`; trang nền có thể có stream nhưng không phải stream mới của overlay.
- Không giả định “không raise” luôn đồng nghĩa đã thêm một block; kiểm tra return/đầu ra phù hợp với version đã pin, và xử lý exception trước/sau phần thêm text nhất quán.
- Tính/validate ActualText trước khi thêm operation để tránh text đã thêm nhưng metadata encode thất bại. E4 trong plan nhắc `has_glyph` như một phần logic cũ, nhưng `_build_text_page_words` hiện không gọi `has_glyph`; không thêm bộ lọc glyph nếu muốn parity.
- Các probe token đã pass hỗ trợ encoder hiện tại, không phải cam kết mọi chuỗi đều được encode. `getTJstr` của PyMuPDF 1.26.7 có nhánh trả nguyên chuỗi bắt đầu `[<` và kết thúc `>]`. Test adversarial nên có dạng này; raw byte split không được coi là PDF parser tổng quát.
- Giữ fallback line cho `words_data` rỗng; không tự đổi thành fallback line cho mọi trường hợp “words tồn tại nhưng bị lọc hết” nếu mục tiêu là giữ semantics cũ.

Prototype trong `temp/probe_shape_batch.py` chưa có đầy đủ E2/E3 và không phải code sẵn sàng merge. Probe này chỉ được dùng để đánh giá hướng batch.

## 5. Phản hồi WI-3: JSON-only phù hợp nhưng phải tách đường dẫn PDF/JSON

### 5.1. [P1] Completion hiện lấy JSON path gián tiếp từ PDF path

[split_step.py:762](D:/App/ocrtool/scanindex/ui/digitization/split_step.py:762) chỉ resolve JSON nếu `ocr_pdf_path` có giá trị; sau đó ghi session và dùng JSON để xây secrecy cache. Signal hoàn tất hiện mang `(run_id, ocr_pdf_path, split_result, error)`.

Nếu đổi payload PDF thành chuỗi rỗng, JSON path của UI cũng rỗng dù file JSON đã được tạo. Nếu chỉ set session rỗng trước đó nhưng vẫn emit đường dẫn PDF cũ, callback lại ghi đè session thành đường dẫn không tồn tại. Đây là lỗ hổng trong thiết kế migration của WI-3, không phải lỗi JSON-only đã được triển khai.

**Hợp đồng đề xuất:** completion mang riêng `canonical_json_path` và `ocr_pdf_path` tùy chọn, qua signal đã cập nhật hoặc một result object. Prediction, session và secrecy cache lấy JSON path trực tiếp. Giữ kiểm tra `run_id` để kết quả nguồn cũ không áp dụng lên nguồn mới.

Probe xác nhận [resolve_companion](D:/App/ocrtool/scanindex/core/canonical_io.py:136) vẫn resolve được `json_only.pdf.json.zst` khi `json_only.pdf` không tồn tại. Do đó có thể giữ quy ước tên sidecar để giảm thay đổi; không cần tạo file PDF giả.

### 5.2. Những gì được bỏ và những gì phải giữ

Khi `pdf_output=False`, bỏ font lookup, tạo `doc_out`, background specs/overlay queue không cần thiết, copy/rasterize background, overlay và save PDF. Giữ mở source để đọc geometry/page map, validate indices, chuẩn hóa tọa độ, tạo record và `save_canonical(..., profile="layoutlmv3_runtime")`.

Không đổi profile, text normalization, raw text, word IDs/order, rotation metadata, source identity. `save_canonical` có bước finishing/pruning và ghi atomic; không thay bằng dump thẳng JSON thô. Không dùng “canonical byte-identical” làm gate nếu chưa cố định metadata thời gian: [completed_at](D:/App/ocrtool/scanindex/core/kie/json_utils.py:104) khác giữa hai lần build.

API hiện được Step 2 và payload worker sử dụng: giữ default `pdf_output=True`, tương thích caller cũ. Nếu đưa flag vào payload entry point thì dùng default `True` cho payload không có field, tránh làm mất PDF Step 2.

### 5.3. UI và phục hồi

Khi viewer page count lệch, thử load lại source PDF hiện tại rồi xác minh; đừng chỉ log và tiếp tục với viewer thiếu trang. Nếu vẫn lỗi, hiển thị trạng thái phù hợp. Không có bằng chứng đủ để loại fallback với lập luận “không xảy ra trong thực tế”.

Cập nhật lời nhắc “Đang dựng PDF OCR” tại cả stage và timer/status, cùng log completion, để phản ánh JSON-only. Escape hatch phải được đọc một lần theo run và dùng nhất quán cho assemble, completion path và viewer fallback.

## 6. Phản hồi WI-2: sửa đúng nhánh, đo trước khi cam kết tốc độ

### 6.1. [P1] Phải tích hợp vào DOCX-export branch

Helper song song phải được gọi từ vòng layout của `process_pdf_for_docx_export`, không chỉ `process_pdf:1564`. Có thể dùng chung helper ở các nhánh khác, nhưng scope đầu tiên là call graph thật ở mục 2.3.

Task cần phân biệt rõ `visual_pdf_path` sau preprocess, source page index dùng để render, và output page index dùng để decorate ID/bbox. Render giữ 240 DPI, `annots=True`, geometry của visual page. Không truyền source PDF gốc thay visual PDF chỉ vì plan đặt tên task argument là `source_path`.

Giữ đủ layout cho scan, mixed và digital; nhánh DOCX hiện phân tích cả trang digital không cần OCR. Không gộp layout vào OCR worker như O2 rồi vô tình bỏ các trang digital.

### 6.2. [P1] Giới hạn process phải đi cùng thread budget và ngân sách chung

[LayoutAnalyzer](D:/App/ocrtool/scanindex/core/tables/layout_analyzer.py:195) chỉ đặt `intra_op_num_threads` khi `DOCLAYOUT_ONNX_THREADS > 0`. Mặc định 0 cho ONNX Runtime tự chọn số thread theo lõi vật lý. `ORT_SEQUENTIAL` là thứ tự operators, không biến inference thành single-thread. Xem [tài liệu ONNX Runtime](https://onnxruntime.ai/docs/performance/tune-performance/threading.html).

Mỗi worker load hai sessions/model. Nếu mỗi file tạo pool riêng trong file executor, tổng số worker/model nhân theo số file đang chạy. Code UI đang có [ThreadPoolExecutor theo file](D:/App/ocrtool/scanindex/ui/main_window.py:3568), còn OCR dùng pool toàn ứng dụng.

Thiết kế phải quy định: pool shared hay per-file; giới hạn concurrency toàn app; threads/session; số task in flight; ngưỡng chọn serial; thời điểm giải phóng model; không load hai model thừa trong process cha. Đo tổng RAM process tree, không chỉ RSS của GUI.

Default `min(2, cpu_count-2)` còn trả 0 hoặc âm trên máy 1–2 logical CPU. Cần clamp tối thiểu 1, xử lý `cpu_count() is None`, cap theo số trang và validate env. Có đường tuần tự explicit để benchmark và rollback.

### 6.3. Thí nghiệm bằng hai model thật

Probe render sáu trang 1, 8, 121, 122, 201, 252 từ source PDF; cả DocStructBench và DocLayNet phải load thành công. Callback bắt lỗi để không có kết quả parity giả do hai bên cùng trả `[]`. So sánh toàn bộ lists/dicts regions theo thứ tự trang; các cấu hình thử đều có cùng số regions `[16,25,10,13,18,5]` trong lần chạy đã ghi nhận.

**Kết quả trên ONNX Runtime 1.26.0 của Python hệ thống, một lượt/cấu hình, thời gian gồm spawn/init/shutdown:**

| Workers | Threads/session | Wall time 6 trang |
|---:|---:|---:|
| 1 | 0, mặc định | 11,164 s |
| 1 | 2 | 15,999 s |
| 2 | 2 | 10,965 s |
| 2 | 0, mặc định | 10,660 s |

Các output regions bằng nhau trong thử nghiệm này. RSS sau task cao nhất quan sát của một worker khoảng 888–966 MiB, tùy cấu hình; đây không phải peak memory toàn app. Hai workers có nghĩa là có hai bản model/runtime, không phải vẫn một bản chia sẻ.

Runtime hệ thống khác pin repo (`onnxruntime==1.24.4`), nên đã chạy thêm đối chứng trong `.venv_build`, lưu riêng ở `layout_results_build.json`:

| ONNX Runtime đúng pin | Workers | Threads/session | Wall time 6 trang |
|---|---:|---:|---:|
| 1.24.4 | 1 | 0, mặc định | 11,342 s |
| 1.24.4 | 2 | 0, mặc định | 10,937 s |

Trong runtime build, regions cũng bằng nhau giữa hai cấu hình. Hai process nhanh hơn khoảng 3,6% trong mẫu này, chưa gần mức 2×. RSS sau task cao nhất quan sát của một worker lần lượt khoảng 1080 MiB và 1003 MiB; số thứ hai không phải tổng RAM của cả hai workers. Kết quả không chỉ là hiện tượng của runtime hệ thống khác pin.

Mẫu nhỏ, một lượt và khác cold/warm filesystem không đủ để chọn cấu hình production. Nhưng kết quả hiện có không hỗ trợ phép suy diễn “thêm hai worker ⇒ thời gian còn một nửa”. Phải benchmark đủ dài trên runtime release trước khi biến O1 thành mặc định. Cũng không kết luận rằng song song layout không bao giờ có ích.

### 6.4. Cancellation và lỗi worker cần thiết kế cụ thể

Các public OCR functions hiện nhận log callback, chưa có hợp đồng cancel xuyên suốt layout pool. “Hỗ trợ cancel” không thể chỉ là gọi `Future.cancel()` cho mọi task rồi thoát.

Cần quy định: dừng submit ngay khi cancel; cancel task chưa chạy; xử lý task đang chạy và shutdown; không attach partial layout vào canonical thành công; không xóa visual PDF tạm khi worker còn giữ; một file hủy không làm hỏng file khác nếu pool shared. Initializer phải dùng các guard Windows spawn/stdio hiện có trong `scanindex.infra.mp_safety`.

“Model không cài → không layout” khác “model đã chọn bị crash giữa run”. Hiện helper có thể trả `[]` khi exception; test mới phải nhận diện lỗi thật thay vì coi output rỗng là parity. Không tự bỏ layout khi pool lỗi rồi vẫn báo cùng chất lượng.

## 7. Phản hồi WI-4 và dự phóng hiệu năng

### 7.1. Warmup đã chạy khi mở chức năng Số hóa

`FUNCTION_DIGITIZATION` yêu cầu `GROUP_CORE_OCR` tại [main_window.py:528](D:/App/ocrtool/scanindex/ui/main_window.py:528). Reconcile chạy trong thread tại [dòng 682](D:/App/ocrtool/scanindex/ui/main_window.py:682); loader gọi `_get_pool()` tại [dòng 1475](D:/App/ocrtool/scanindex/ui/main_window.py:1475).

Vì vậy WI-4.2 không phải tối ưu mới. Nếu cần env disable warmup, thêm vào loader hiện có. `_get_pool()` trả pool sau khi tạo process, không có barrier chứng minh mọi initializer DLL đã xong; metric “pool creation” không bằng “engine ready”. Script benchmark cũ đặt nhãn DLL load quanh `_get_pool()` có thể tính phần initializer vào OCR stage tiếp theo.

### 7.2. Recycle 100 → 500 cần bài đo dài

Giữ default 100 trước; có thể cho env override phục vụ đo. Không khẳng định 500 an toàn chỉ vì vẫn bounded. Chưa đo leak/RSS theo 100/200/500 tasks, worker crash hay thời gian bị chặn do recycle trên pool sống qua nhiều file.

Theo phân phối đều từ pool mới, 252 task / 2 workers khiến mỗi worker vượt mốc 100 một lần. Số lần thay worker thực tế phụ thuộc phân phối và lịch sử pool; cần log PID/task count, không suy ra “5–8 giây tiết kiệm” bằng cộng thời gian init độc lập. Hai initializer có thể chồng nhau và chạy cùng worker còn lại.

### 7.3. Mục tiêu Word chưa khớp phép tính

Dùng số của plan và giả sử layout scale lý tưởng:

```text
T trước DOCX ≈ 88 preprocess + 139 OCR + 459/N layout
             + 30 overlay mới + 21 save PDF + 3 canonical (giây)
N=2: 510,5 giây ≈ 8,5 phút
N=4: 395,75 giây ≈ 6,6 phút
```

Đây chưa cộng manifest, pool overhead, correction, nhận dạng cấu trúc bảng và xuất DOCX. WI-3 chỉ áp dụng Step 1, không xóa overlay/PDF ở đường Word. Mục tiêu 4–5 phút không suy ra từ thiết kế hiện tại; gate ≤6 phút cũng chưa có cơ sở ngay cả theo phép chia lý tưởng N=4.

Đối với Step 1, 139 OCR + 8 canonical + 3 prediction ≈150 giây là dự phóng hợp lý về số học. Cần đo thêm cache I/O, source hashing, UI completion/secrecy cache và warmup thực tế. Không gọi 139 giây là “sàn” đã chứng minh cho mọi file; đó là throughput mẫu trên một cấu hình.

Các script gốc đo pipeline/assemble trên 8 trang, layout trên 3 trang. Phân biệt số đo mẫu với ngoại suy 252 trang. Trang 252 thưa trong probe mới cũng cho thấy hiệu quả batch thay đổi lớn theo mật độ từ.

## 8. Gate đề xuất cho bản plan sửa đổi

| Phạm vi | Kiểm chứng bắt buộc |
|---|---|
| WI-1 output | Cùng input copies; save/reopen; text + tuples word/bbox + ActualText + rendered pixels; giữ native text/annotations/background |
| WI-1 exceptions | Empty words, words bị lọc hết, insert error, metadata encode error, mismatch stream; fallback phải giữ nội dung và không nhân đôi overlay |
| WI-1 geometry | Trang thưa/dày, crop/rotation, scale clamp, dấu Việt/combining marks, unsupported glyph, sát bbox, spacing true/false, newline |
| WI-3 canonical | So sánh decoded canonical với clock cố định hoặc chỉ bỏ field thời gian đã định nghĩa; profile/source/page map/rotation giữ nguyên |
| WI-3 data completeness | Trang trắng hợp lệ, missing page, init-failed, cache file mất/hỏng; lỗi không được biến thành successful blank OCR |
| WI-3 UI integration | JSON path riêng, secrecy cache, prediction, viewer reload, cancel/đổi nguồn, escape hatch bật/tắt |
| Step 1→2 | Segment có offset nguồn khác 0; đủ PDF/JSON, cache còn nguyên, không preprocess/re-OCR; cả isolated và fallback path |
| WI-2 routing | Test gọi qua `process_pdf(profile=docx_export)` tới helper mới; đủ scan/mixed/digital; đúng visual PDF sau preprocess |
| WI-2 parity | Hai model thực sự chạy, regions/IDs/order/bbox/confidence bằng baseline trong runtime pin; lặp lại, không pass giả với `[]` |
| WI-2 lifecycle | 1/2 CPU giả lập, env lỗi, single-page, nhiều file đồng thời, worker crash/init failure, cancel, portable Windows |
| Performance | Đo từng phase và end-to-end UI đủ 252 trang; tách cold/warm, nhiều lượt, median/biến thiên, peak RAM/process count; xác nhận file Word thật hoàn tất nếu ghi PDF→Word |

Không cần hạ gate parity chỉ để implementation pass. Nếu đổi thread budget gây khác floating point ở runtime khác, phải điều tra và quyết định tolerance có căn cứ; không bỏ qua khác biệt type/count/reading order hoặc detection gần threshold.

Thứ tự đề xuất: **WI-1 → WI-3 → thử nghiệm/thiết kế lại WI-2 → WI-4 nếu số đo chứng minh lợi ích**. Guard dữ liệu thiếu và thay đổi ownership input nên có commit correctness riêng. WI-3 không có dependency kỹ thuật bắt buộc vào WI-1; đặt sau WI-1 giúp giữ nhánh xuất PDF/escape hatch nhanh và dễ đối chứng.

## 9. Bằng chứng, cách tái hiện và giới hạn review

- [Probe overlay/cache/normalization](D:/App/ocrtool/temp/review_ocr_assembly_probe.py).
- [Kết quả JSON của probe](D:/App/ocrtool/temp/review_ocr_assembly_evidence/results.json).
- [Probe layout process/thread](D:/App/ocrtool/temp/review_layout_pool_probe.py).
- [Layout Python hệ thống](D:/App/ocrtool/temp/review_ocr_assembly_evidence/layout_results.json).
- [Layout runtime build 1.24.4](D:/App/ocrtool/temp/review_ocr_assembly_evidence/layout_results_build.json).
- [Log pytest](D:/App/ocrtool/temp/review_ocr_assembly_evidence/pytest.log).

Các probe/data nằm trong `temp/` đang gitignored; muốn giữ bằng chứng theo release cần chuyển script đã làm sạch vào `scripts/` và lưu log kèm thông số môi trường. Probe overlay phụ thuộc prototype hiện có `temp/probe_shape_batch.py`; không sửa module production trên disk.

Lệnh đã chạy cho overlay và model layout:

```powershell
python temp/review_ocr_assembly_probe.py --real
python temp/review_layout_pool_probe.py
python temp/review_layout_pool_probe.py --default-parallel
& .venv_build/Scripts/python.exe temp/review_layout_pool_probe.py --build-pair
```

Tests liên quan đã chạy trong `.venv_build`: **43 passed in 8.18s**, gồm `test_ocr_text_layer`, `test_archive_runner_ocr_reuse`, `test_mp_safety`, `test_canonical_profiles`, `test_text_layer_import`, `test_pdf_splitter_preserves_appearances`, `test_pdf_viewer_text_selection`, `test_page_splitter_portable_imports`.

Lần chạy đầu bằng Python hệ thống có 35 pass / 8 fail do thiếu `tantivy`, chưa phải lỗi logic được xác nhận. Đã chuyển sang môi trường build có dependency này và cả 43 test pass. Không cài thêm package hoặc sửa dependency repo để làm test pass.

Chưa chạy toàn bộ pytest, chưa benchmark full 252 trang end-to-end, chưa đo peak RAM toàn ứng dụng hoặc leak DLL dài hạn, chưa kiểm tra thao tác Adobe/Chrome/Edge và chưa build/chạy portable executable mới. Kết quả nêu trên không thay thế các gate release đó.
