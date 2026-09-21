# KẾ HOẠCH: Resume quét dở theo version + cột "Đáp ứng giải mật" trong Excel

**Trạng thái:** bản thảo chờ chuyên gia review — CHƯA code.
**Phạm vi:** màn hình "Phát hiện file mật trong thư mục" (`scanindex/ui/screens/secret_file_scan_screen.py`) + journal/registry (`scanindex/core/secret_scan_progress.py`).

---

## 1. Bối cảnh & mục tiêu

Bản mới đã thay đổi cách dò dấu mật (xoay hướng trang trước → dò vùng góc trên-trái cx≤0.6/cy≤0.6 kèm guard Mặt trận) và thêm dữ liệu giải mật (năm ban hành, thời hạn 10/20/30 năm, ghi chú xanh). Dữ liệu này nằm trong `SecretScanMatch` ở các field `issue_year` / `declass_years` / `declass_due`.

**Vấn đề cần giải quyết:** phiên quét dở của bản cũ (journal) khôi phục các hàng mật với dict cũ → thiếu field giải mật → Excel format mới thiếu dữ liệu cho đúng các hàng đó. Đồng thời cache registry của bản cũ tự vô hiệu theo version → nếu chọn "quét lại từ đầu" thì mất công OCR lại toàn thư mục lớn (ví dụ 500.000 file).

**Mục tiêu — kịch bản chuẩn (500.000 file, đã quét 300.000, tìm 200 file mật, journal bản cũ):** khi mở lại thư mục này bằng bản mới, hộp thoại resume hiển thị **3 lựa chọn**:

1. **Quét lại từ đầu** — toàn bộ 500k file chạy bằng code mới (đã có sẵn hôm nay, giữ nguyên).
2. **Quét tiếp** — 200k file còn lại quét bằng code mới; 300k file đã quét **không quét lại** nhưng được **đóng dấu version mới** vào journal + registry để các lượt sau không phải quét lại.
3. **Quét tiếp như (2) + quét lại đúng N file mật cũ** — thay thế hàng cũ bằng hàng mới (đủ năm + ghi chú giải mật, loại được false positive Mặt trận mà bản cũ nhầm), rồi xuất Excel đúng & đủ.

Journal cùng version → hộp thoại 2 nút như hiện nay (không hiện lựa chọn đặc biệt).

**Đi kèm:** cột riêng "Đáp ứng giải mật" trong Excel xuất ra (hiện chỉ có chuỗi trong cột Ghi chú).

---

## 2. Hiện trạng code (đã xác minh, kèm tham chiếu)

### 2.1 Journal quét dở — `SecretScanProgress` (`secret_scan_progress.py`)
- File: `scan_progress/secret_scan_{fast|thorough}.jsonl` (dòng 61).
- Header: `{"t":"h","v":STATE_VERSION,"folder":...,"mode":...,"started_at":...}` (dòng 134-146). **`STATE_VERSION = 1` là version định dạng file journal, KHÔNG phải version app — journal hiện không lưu version app.**
- Event per-file: `{"t":"f","p":rel,"s":"ok"|"err"|"skip"}` (dòng 255-258); event match: `{"t":"m","d":match_dict}` (dòng 263) — **append-only, không có cơ chế thay thế/xóa matches theo file**.
- Replay `load()` (dòng 150-221): chỉ xử lý `t == "h"|"f"|"m"` qua if/elif — **các event type lạ bị bỏ qua im lặng** (fall-through, không crash). Đuôi file hỏng (mất điện giữa chừng) được cắt và tự compact.
- `done_files()` (dòng 276-280) = `_done` (ok) ∪ `_skipped` (rác vĩnh viễn). File `err` được thử lại. **→ tập "đã quét" cần đóng dấu = `done_files()`.**
- `_compact()` (dòng 306-355): ghi lại toàn bộ journal từ bộ nhớ (header + f + m). Compact phải giữ được mọi field mới thêm.

### 2.2 Registry cache — `FileRegistry` (`secret_scan_progress.py`)
- File: `scan_progress/file_registry.jsonl` (dòng 67). Entry: key = `normcase(abspath(path)) + "\x00" + mode` (dòng 384-385) → `(size, mtime, app_version, matches)` (dòng 377-378).
- `lookup()` (dòng 433-445) yêu cầu **khớp tuyệt đối size + mtime + app_version** → nâng cấp app tự vô hiệu entry cũ. **Version đã tồn tại sẵn ở registry** (không cần thêm).
- Chỉ ghi entry khi quét thành công; file lỗi không ghi (dòng 371-373). Load toàn bộ vào bộ nhớ + compact định kỳ.

### 2.3 Màn hình quét — `secret_file_scan_screen.py`
- `_offer_resume()` (≈ dòng 2310-2347): load journal, thống kê, `QMessageBox.question` Yes/No → trả `prog | None`.
- `_run_clicked()`: nếu resume → khôi phục hàng từ `resume_prog.matches` qua `SecretScanMatch(**dict)` (field mới thiếu → nhận default 0/False → **hàng cũ không có dữ liệu giải mật, im lặng**).
- `_run_worker(folder, first_page_only, resume_prog)`: `_split_pending()` bỏ qua `done_files()`; cửa sổ trượt ThreadPoolExecutor (`_SCAN_MAX_INFLIGHT_FILES=32`, 2 file-worker); `process_one` lookup registry (có version) → cache hit thì phát hàng từ dict, miss thì quét + `prog.record_file/record_matches` + `registry.record`.
- Hàng: `_add_result` (gate `NotSecretMarks.is_marked`), `_remove_file_rows(path)` xóa mọi hàng của 1 file (dùng cho Xóa file / Không phải mật — tái sử dụng cho thay thế hàng).
- Excel: `EXCEL_HEADERS` 8 cột, `export_matches_to_excel` (ghi chú giải mật tô xanh `1D7A34`), `load_secret_matches_from_excel` (bắt buộc cột "STT"+"Đường dẫn đầy đủ", các cột khác tùy chọn qua `col(name, default)`), `_match_meets_declassification` nhận diện qua chuỗi ghi chú (fallback cho file load).
- Version app: `scanindex/infra/version.py::get_version()` — git describe → file VERSION → fallback.

---

## 3. Thiết kế chi tiết

### 3.1 Journal lưu app version
- `create()`: thêm `"av": get_version()` vào header.
- `load()`: đọc `header.get("av") or ""` vào `prog.app_version`. Journal cũ không có field → `""`.
- `_compact()`: giữ nguyên `av` khi ghi lại header.
- **Tương thích ngược:** bản cũ đọc journal mới — header vẫn `v=1` nên pass kiểm tra (dòng 185); field lạ bị bỏ qua. Không cần bump `STATE_VERSION`.

### 3.2 Hộp thoại resume 3 lựa chọn
- Trích logic quyết định ra hàm thuần để test được: `_resume_options(prog, current_version) -> ("legacy_3opt" | "normal_2opt")`.
- `_offer_resume()` thay `QMessageBox.question` bằng `QMessageBox` + 3 `addButton` tùy chọn (chỉ khi `prog.app_version != current_version`):
  - **A "Quét lại từ đầu"** → trả `None` (giữ hành vi hiện tại; lượt quét mới `create()` ghi đè journal với `av` mới).
  - **B "Quét tiếp"** → trả `(prog, upgrade_stamps=True, rescan_paths=[])`.
  - **C "Quét tiếp + quét lại N file mật cũ"** → trả `(prog, True, rescan_paths)`. Label nút ghi rõ N (vd "Quét tiếp + quét lại 200 file mật").
  - Nút thứ 4 "Hủy" thoát không chạy.
- Version khớp → hộp thoại 2 nút Yes/No hiện hành.
- Kiểu trả về mới: dataclass nhỏ `ResumeDecision(prog, upgrade_stamps, rescan_rels)` — `None` khi quét từ đầu. `_run_clicked` truyền xuống worker.

### 3.3 Option 2 — đóng dấu version mới (journal + registry)
- **Thời điểm áp dụng:** đầu `_run_worker`, sau khi load registry, TRƯỚC khi bơm hàng đợi. Lý do: ngữ nghĩa của lựa chọn là "bless" kết quả cũ như kết quả của bản mới — áp một lần, dừng ngang chừng giữa quét không làm mất ngữ nghĩa (các file còn lại quét bằng bản mới và được ghi bản mới như thường).
- **Journal:** method `prog.stamp_app_version(version)` — append event `{"t":"av","av":version}`; replay `load()` xử lý `t=="av"` → ghi đè `prog.app_version` (event sau header, append-safe, không phải sửa dòng đầu).
- **Registry:** method `FileRegistry.bump_versions(abs_paths, mode, version)` — với mỗi key tồn tại, thay field version giữ nguyên `(size, mtime, matches)`; gọi `save()` (một lần compact). Chỉ đóng dấu cho `done_files()` của journal (ok + skip) thuộc đúng `folder + mode` của phiên. Entry không tồn tại (file skip/rác không có entry) → no-op.
- **Chi phí:** 1 lần rewrite `file_registry.jsonl` (đã load toàn bộ trong bộ nhớ sẵn) — I/O thuần, không OCR. Với ~300k entry ước tính vài giây.
- **Ngữ cảnh cần ghi rõ trong tooltip/hộp thoại:** lựa chọn này giữ nguyên kết luận "không mật" của bản cũ cho nhóm file đã quét — mọi sai sót của bản cũ trên nhóm này sẽ không được soát lại trừ khi file đổi nội dung hoặc chọn option A.

### 3.4 Option 3 — quét lại N file mật cũ
- `rescan_rels` = danh sách rel path duy nhất suy từ `prog.matches` (group theo `relative_path`).
- **Hàng đợi:** đưa `rescan_rels` lên TRƯỚC `pending` (xử lý xong nhóm này mới quét tiếp phần còn lại — người dùng thấy danh sách "sửa" sớm). Task mang cờ `replace=True`; progress tổng = `len(pending) + len(rescan_rels)`.
- **Trong `process_one` nhánh replace:** gọi `scan_one_file_for_secret(...)` như thường (cùng `first_page_only` của phiên — giữ nguyên ngữ nghĩa dò); sau đó:
  - `prog.record_file(rel, "ok")`;
  - `prog.replace_matches(rel, new_dicts)` — xem 3.5;
  - `registry.record(path, size, mtime, mode, version_mới, new_dicts)` (tự đóng dấu version mới cho đúng file này);
  - phát signal mới `_results_replace = Signal(object)` chứa `(source_path, [SecretScanMatch...])` → slot chính thread: `_remove_file_rows(path)` rồi `_add_result(...)` từng match (gate NotSecretMarks hoạt động như mọi nguồn hàng).
- **File特殊情况:**
  - File đã bị xóa khỏi đĩa: bắt OSError → giữ hàng cũ, log "[rel] Quét lại thất bại: ...", KHÔNG gọi `replace_matches` (hàng cũ còn trong journal).
  - File đổi nội dung (size/mtime khác): quét lại với nội dung hiện tại — tự nhiên đúng.
  - File giờ bị xác nhận "không phải mật" (vd Mặt trận FP được rule mới loại): `replace_matches` với danh sách rỗng → hàng cũ biến mất khỏi bảng và journal. **Đây là hành vi mong muốn.**
- **Khôi phục ban đầu:** `_run_clicked` vẫn khôi phục toàn bộ hàng cũ từ `prog.matches` (kể cả nhóm chờ quét lại) — hàng được thay thế dần khi có kết quả; người dùng thấy tiến trình sửa chữa diễn ra.

### 3.5 Cơ chế thay thế matches trong journal (chống trùng khi resume lần nữa)
- Event mới: `{"t":"mc","p":rel}` = "clear mọi matches của rel phát sinh trước đây".
- `replace_matches(rel, match_dicts)`: append 1 event `mc` + các event `m` mới (dưới `state_lock` như hiện tại).
- `load()`: nhánh `t == "mc"` → lọc bỏ các dict trong `prog.matches` có `relative_path == rel`. Các event `m` của rel phát sinh SAU `mc` vẫn được append bình thường → replay cho đúng trạng thái cuối.
- `_compact()`: `prog.matches` trong bộ nhớ đã là trạng thái sau lọc → ghi lại tự nhiên đúng.
- **Tương thích ngược (đã xác minh):** bản cũ đọc journal có `mc`/`av` → `load()` if/elif chỉ match `h|f|m`, event lạ rơi ra ngoài, bị bỏ qua im lặng (dòng 192-208). Trường hợp biên: bản cũ resume journal đã có `mc` sẽ thấy trùng hàng tạm thời trong phiên chuyển tiếp — chấp nhận được (chỉ xảy ra khi hạ cấp version).

### 3.6 Cột "Đáp ứng giải mật" trong Excel
- `EXCEL_HEADERS` chèn `"Đáp ứng giải mật"` vào vị trí 8 (giữa "Chế độ" và "Ghi chú"); `_EXCEL_COL_WIDTHS` chèn 24; `auto_filter.ref` mở rộng `A1:H…` → `A1:I…`.
- Giá trị ô:
  - `declass_due=True` → `Đáp ứng (2016, 10 năm)` — font xanh đậm `1D7A34` + bold (nhất quán màu ghi chú hiện nay).
  - Có năm nhưng chưa đến hạn → `Chưa đủ (2025, 20 năm)` — màu chữ thường.
  - Không xác định được năm → để trống.
  - Chuỗi ghi chú trong "Ghi chú" GIỮ NGUYÊN (tương thích loader cũ + fallback nhận diện).
- **Loader** `load_secret_matches_from_excel`:
  - Cột mới tùy chọn (cơ chế `col(name, default)` sẵn có) → file cũ 8 cột vẫn nạp được.
  - Khi có cột: parse value để dựng lại `issue_year` / `declass_years` / `declass_due`.
  - Khi không có cột: fallback `_match_meets_declassification` (quét chuỗi ghi chú) — đã tồn tại, giữ màu xanh Ghi chú đúng.
- **Bảng trong app (điểm mở chờ chốt):** đề xuất thêm cột "Giải mật" tương ứng (giữa "Chế độ" và "Ghi chú", kéo rộng được nhờ Interactive mode) để đồng bộ bảng ↔ Excel. Phương án b: giữ nguyên chỉ tô xanh Ghi chú. **Khuyến nghị: phương án a.**

---

## 4. Ngắt giữa chừng & idempotency
- Option 3 dừng ngang: các file đã quét lại có `mc`+`m` mới (không trùng khi resume); các file chưa quét lại giữ hàng cũ trong journal — resume lần sau với journal `av` đã stamp (khớp version) → hộp thoại 2 nút; nhóm còn lại của `rescan_rels` chưa xong sẽ KHÔNG tự tiếp tục (chúng thuộc `_done`) — cần ghi nhận trong kế hoạch test; **phương án xử lý đề xuất:** khi stamp, chỉ stamp NHỮNG file không thuộc `rescan_rels` chưa xong… → đơn giản hơn: stamp toàn bộ ngay đầu lượt (ngữ nghĩa "bless"), và ghi `rescan_rels` còn dở vào journal (event `{"t":"rs","p":[...]}`) để lần resume sau detect và đề nghị tiếp tục nhóm này. **Điểm này xin chuyên gia phán quyết:** (i) event `rs` + tự đề nghị tiếp tục, hay (ii) chấp nhận nhóm dở phải dùng "Quét lại từ đầu"/thủ công.

## 5. Tương thích & triển khai
- Bản mới đọc journal/registry cũ: hoạt động (thiếu `av` → hộp thoại 3 nút; registry miss → quét lại).
- Bản cũ đọc journal mới: bỏ qua `av`/`mc`/`rs` (đã xác minh fall-through); degrade chấp nhận được khi hạ cấp.
- **Yêu cầu phát hành:** build phải bump version (git tag / VERSION) — quy trình build_portable hiện hành đã làm; nếu version không đổi, registry cũ bị tái dùng và mọi tính năng mới im lặng không chạy trên file cũ.
- Ảnh hưởng các tính năng khác: không thay đổi `detect_secrecy_mark`, luồng OCR, NotSecretMarks, "Xóa lịch sử quét" (xoá cả journal lẫn registry — mở rộng xoá luôn các event mới vì nằm cùng file).

## 6. Kế hoạch test
**Unit (mới):**
1. `create()` ghi `av`; `load()` đọc lại; journal cũ không `av` → `""`; `_compact` giữ `av`.
2. `stamp_app_version` + replay; `mc` replay loại đúng matches của rel, giữ rel khác; compact sau `mc` không hồi sinh hàng cũ.
3. `bump_versions`: chỉ đổi version của key thuộc (paths, mode); size/mtime/matches giữ nguyên; key không tồn tại no-op.
4. `_resume_options`: journal cũ → 3 nút; khớp version → 2 nút.
5. Excel: cột mới giá trị/định dạng 3 trạng thái; nạp file 8 cột cũ OK; nạp file 9 cột dựng lại đủ field; roundtrip.
**Integration (offscreen, qapp):**
6. Option 3 end-to-end với thư mục giả lập: 2 file mật cũ (dict thiếu field) + 1 file pending → hàng bị thay thế đúng, không trùng, tổng đếm đúng; file bị xóa giữ hàng; FP giả lập rớt khỏi danh sách.
7. Resume lần nữa sau option 3 dừng chừng: không trùng hàng (replay `mc`).
8. Regression: toàn suite hiện có (227 test) không hỏng.

## 7. Rủi ro & câu hỏi mở cho chuyên gia
1. **Đóng dấu version cho 300k kết luận "không mật" của bản cũ** — đây là đánh đổi được người dùng chọn (option 2/3 nhanh), nhưng cần_chuyên_gia xác nhận chấp nhận: sai sót recall của bản cũ (vd trang xoay bị sót ở fast mode cũ) sẽ không bao giờ được soát cho nhóm này. Option A là lối thoát.
2. Mục 4 — nhóm `rescan_rels` dở khi dừng ngang: phương án (i) event `rs` hay (ii) chấp nhận?
3. Vị trí cột Excel (đề xuất vị trí 8) và có thêm cột "Giải mật" trong bảng app không (khuyến nghị CÓ)?
4. `bump_versions` theo prefix thư mục: chuẩn hóa `normcase(abspath)` + tránh khớp tiền tố "nhầm đường dẫn anh em" (vd `D:\a` vs `D:\ab`) — phải nối `os.sep` vào prefix khi lọc key.
5. I/O rewrite registry lớn (500k+ entry toàn cục): đo trên máy thật trước khi chốt (ước tính vài giây; `_compact` đã có sẵn).

## 8. Ước lượng effort
| Hạng mục | Ước tính |
|---|---|
| 3.1 journal `av` + 3.5 `mc`/`replace_matches` | ~2h |
| 3.2 hộp thoại + ResumeDecision | ~1.5h |
| 3.3 stamp + `bump_versions` | ~2h |
| 3.4 hàng đợi quét lại + signal thay thế hàng | ~3h |
| 3.6 Excel + loader + (cột bảng app) | ~2h |
| Test (mục 6) | ~3h |
| Buffer review/sửa | ~2h |
| **Tổng** | **~1 ngày công** |

## 9. Thứ tự triển khai đề xuất
1. Journal: `av`, `mc`, `replace_matches`, compact giữ field (+ test 1, 2).
2. Registry: `bump_versions` (+ test 3).
3. `_resume_options` + hộp thoại 3 nút (+ test 4).
4. Worker: stamp + hàng đợi rescan + signal thay thế hàng (+ test 6, 7).
5. Excel cột mới + loader (+ test 5).
6. Chạy toàn suite + xác minh bằng bộ sample `temp/secret_scan_samples`.
