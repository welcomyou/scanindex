# Sửa OCR pool và kiểm tra hồi quy hiệu năng — 23/10/2026

**Đã sửa hai P1 và nhánh load permit của review kiến trúc, đồng thời rà các tối ưu số hóa lưu trữ, audit và danh sách file mật. Tổng hợp 444 test không trùng pass; 5 lượt smoke model thật trên source cuối đều đạt.** Không thay đổi mô hình OCR/KIE, ngưỡng nhận dấu, chính sách DPI/OCR audit hoặc chất lượng ảnh xuất. Chưa xác nhận cấu hình tối ưu trên máy 64 CPU / 128 GB; kiểm tra ở máy 12 CPU logic / 31,86 GiB RAM với `.venv_build`.

**1. Các lỗi đã sửa**

| Vấn đề | Thay đổi và hợp đồng giữ lại |
|---|---|
| Snapshot lỗi/retry rời execution budget nhưng còn trên đĩa | Thêm `SpoolLedger`: reserve trước copy, tính cả snapshot giữ lại, đang tạo, orphan/partial và mọi namespace trong cùng data root. Khôi phục byte từ đĩa một lần lúc khởi động, cập nhật theo sự kiện xóa; không quét filesystem theo từng file. Có OS lock để instance khác không cùng sở hữu và vượt ngân sách. |
| Copy snapshot xong nhưng lỗi/hủy có thể mất bộ đếm hoặc đè snapshot cũ | UUID riêng mỗi snapshot; ghi staging, fsync payload, rename; hash SHA-256. Lỗi cleanup vẫn tính reservation cho đến khi xác nhận đã xóa. Snapshot đang được worker đọc vẫn giữ input lease. |
| Lượt dài giữ snapshot thành công đến cuối | GC theo đúng file sau persist bền vững. Nếu KIE vẫn giữ lease, đưa vào cleanup muộn; chỉ trả storage khi thư mục thực sự bị xóa. Metadata applied đã fsync trước GC; tombstone có thể dùng chung lần save kế tiếp mà crash không mở lại nghĩa vụ metadata. |
| Model KIE không nạp được vẫn tiếp tục nhận cả kho | Coordinator ngừng nhận job của run bị lỗi; callback ngừng cấp file mới. Giữ pending/spool để resume. Run khác vẫn có thể thử lại sau khi sửa model. |
| Hết storage hoặc prep-process lỗi vẫn tiếp tục cấp file | Dừng producer, lưu lý do và trạng thái chưa hoàn tất. File chưa xét không bị đánh dấu xong; file lỗi giữ cho lần tiếp tục. Chờ storage có deadline và hủy được, không xóa payload retry để lấy chỗ. |
| Prep timeout/queue đầy chạy thêm native trong parent | Đường prep-process trả lỗi resumable, không fallback inline. Job cũ giữ staging/lease/window đến khi native thật sự xong. Nhánh inline chỉ dùng khi chọn mode inline. Thêm cap số analyzer worker bên trong mỗi prep process, độc lập số worker OCR. |
| Semaphore acquire ném exception nhưng vẫn cho load model | Mọi exception hoặc kết quả không nhận được permit đều fail-closed. OCR test mode đi qua self-load/limiter như worker thay thế, không trả thành công giả trước bước load. |
| ACK telemetry đọc dict trong lúc callback thay đổi | Chụp tập ACK dưới khóa trước khi tính ready-live. |
| Bước 1 và ký số hàng loạt có thể chờ admission mà không phản ứng Stop | Truyền `cancel_check` vào `submit_page`; hủy được lúc gate đang đầy/warmup. Bước 1 phát đúng tín hiệu cancelled, không báo lỗi OCR giả. |
| Ký số dùng tên biến engine không tồn tại sau khi OCR | Dùng đúng `direct_engine.page_result_state`; sửa biến `done` trong closure của nhánh submit lỗi. Kiểm tra đủ trang trước assembly vẫn giữ nguyên. |

Các điểm chính: [spool ledger](D:/App/ocrtool/scanindex/core/secret_scan_spool.py:33), [load permit](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:473), [GC có tham số file](D:/App/ocrtool/scanindex/core/secret_scan_progress.py:745), [dọn sau commit](D:/App/ocrtool/scanindex/ui/screens/secret_file_scan_screen.py:5012), [hủy Bước 1](D:/App/ocrtool/scanindex/ui/digitization/split_step.py:906), [kiểm tra OCR ký số](D:/App/ocrtool/scanindex/ui/screens/batch_signing_screen.py:287).

**2. Rà các tối ưu cũ và ảnh hưởng của pool**

| Đường xử lý | Kết quả rà và kiểm chứng |
|---|---|
| Bước 1 → Bước 2 số hóa lưu trữ | Giữ cache trang, bỏ preprocess/OCR/warmup ở Bước 2 khi handoff hợp lệ. Test mới chạy `ArchiveRunner` + `BatchPipeline` thật, dùng cache cho segment từ trang nguồn 120/121; cấm `_get_pool`, `submit_page`, `process_pdf`, preprocess. Pipeline vẫn Done, output đúng hai trang, canonical dùng index cục bộ `[0,1]`. KIE được thay bằng kết quả tổng hợp trong test này để chỉ kiểm hợp đồng handoff. |
| Assembly từ kết quả OCR | Giữ assembly từ cache, lỗi rõ khi cache thiếu, không tự OCR lại toàn PDF; giữ assembly process riêng ở Bước 2 và JSON-only ở Bước 1. Các test assembly isolated/JSON equivalence pass. |
| Chất lượng ảnh và lớp text | Nhóm `test_ocr_assembly_preserves_scan`, `test_ocr_text_layer`, canonical profile pass. Không thêm rasterize/resize để tăng tốc giả, không thay mapping trang nguồn/segment. |
| Làm sạch trang ký | Giữ quy tắc không OCR lần hai khi trang không có annotation; có annotation chỉ OCR lại trang cần làm sạch. |
| Audit PDF/TIFF | Audit dùng `AuditPool` riêng, không gọi model ScreenAI chỉ để thẩm định. Giữ pool sống lâu, scope DPI trang 1/OCR tối đa 5 trang đầu, fast-path nén không decode khi chứng minh an toàn, cache, debounce và batch UI. Các test policy, image/filter, cache trang, đổi file, tree và i18n đều pass. Không sửa mã audit sản phẩm. |
| List file mật: thêm dòng/cuộn | Giữ bộ đếm tổng theo sự kiện; thêm dòng không đọc lại dòng cũ; giữ cờ i18n explicit-items để cuộn không dịch lại toàn bảng; status/progress tiếp tục coalesce bằng timer. |
| List file mật: thay kết quả/xóa/chọn | Bổ sung chỉ mục path → item Qt. Thay một file không dò toàn bộ bảng; header chọn tất cả dùng bộ đếm visible/checked. Filter/select-all duyệt một lần khi người dùng yêu cầu. Thao tác xóa, đánh dấu không mật, sửa tay và xuất Excel vẫn pass. Override cơ quan theo trang được giữ khi thay metadata. |
| Trần trang toàn app | Giữ một pool OCR chung, không nhân số OCR process theo số file-worker. Giữ gate, lease, retry và callback handle hai phần tử cho caller cũ. Nhóm admission/generation/cancel cùng pipeline mỗi trang nộp một lần pass. |

Tối ưu list giảm phần Python duyệt lại bảng; `QTableWidget` vẫn phải dịch hàng khi xóa ở giữa. Không tuyên bố toàn bộ thao tác Qt là O(1) hoặc list không giới hạn kích thước. Các vòng duyệt phục vụ filter/export/select-all vẫn có mục đích và không nằm trong callback thêm từng dòng.

**3. Đo riêng chi phí cập nhật list**

Phép đo tổng hợp: bảng 2.000 dòng, thay kết quả 100 file. Giữ cùng widget Qt hiện tại, chỉ gắn lại ba method cũ remove/header/replace từ snapshot trước sửa để cô lập thuật toán.

| Biến thể | Lần đọc ô qua Python | Wall time một lượt |
|---|---:|---:|
| Ba method cũ | 400.100 | 0,886 s |
| Mã hiện tại | 200 | 0,026 s |

Số lần đọc là bằng chứng loại vòng duyệt toàn bảng. Thời gian là microbenchmark một lượt trên máy dev, không phải hệ số tăng tốc toàn ứng dụng hoặc số đo 64 CPU. Mã đo và JSON: [bench_list_updates.py](D:/App/ocrtool/temp/fix_64_128_2026_10_23/bench_list_updates.py), [ui_benchmark.json](D:/App/ocrtool/temp/fix_64_128_2026_10_23/ui_benchmark.json).

**4. Kết quả kiểm thử và cách diễn giải**

| Lượt cuối | Kết quả |
|---|---|
| Nhóm hồi quy rộng: OCR/KIE/prep, resume/writer/runtime, số hóa, audit, PDF, list, Excel, i18n, ký số và test bổ sung | **442 passed**, 93,43 s, không fail/error/skip |
| Sau thay đổi cuối ở nhánh dừng producer khi prep lỗi: chạy lại nhóm core cùng toàn bộ 30 test bổ sung | **242 passed**, 65,93 s, không fail/error/skip |
| Tổng hợp tên testcase của hai JUnit | **444 test không trùng**, không cộng 442+242 thành 684 test độc lập |
| Model thật Tìm kỹ, recycle OCR sau 3 task | **3/3 lượt đạt**, mỗi lượt đủ 2 kết quả bền vững |
| Model thật Trang đầu, cùng profile, ép quét lại | **2/2 lượt đạt**, mỗi lượt đủ 2 kết quả bền vững |
| Source hash của cả hai manifest smoke | **154 tệp/manifest, 0 mismatch** với source cuối |
| Snapshot sau các lượt thành công | **0 snapshot còn lại**, failure/pending/sampling_error đều 0 |
| Compile và whitespace | Các module thay đổi compile được; `git diff --check` không báo lỗi whitespace |

Nhóm hồi quy rộng dùng wrapper **chỉ cho fixture test**: PyMuPDF 1.26.7 tạo ảnh đặc bằng `set_rect` gọi Python theo từng byte, làm một ảnh A4 mất hàng chục giây trước khi chạm engine audit. Wrapper thay thao tác tô toàn khung trong đúng ba module test audit bằng ghi mẫu màu qua memoryview. Đã đối chiếu pixel RGB/GRAY/CMYK/alpha và bytes PNG/JPEG với hàm gốc; 70 lượt tô fixture được tăng tốc. Không thay `fitz` hoặc hàm audit trong module sản phẩm, không bỏ test/assertion. Wrapper còn thiết lập locale `vi` cho test audit-screen vốn assert chuỗi Việt; một fixture của test trước để lại `en` làm lượt ghép ban đầu có 1 failure. Các test i18n riêng vẫn chạy và pass cả hai ngôn ngữ.

Hai lượt trước đã dừng khi đang tạo fixture ảnh rất chậm, không tính là pass. Lượt ghép trước khi cô lập locale đạt 439 pass/1 fail; không dùng lượt đó làm nghiệm thu. Log giữ lại để đối chiếu. Nhóm core cuối chạy trực tiếp `python -m pytest`, không dùng wrapper tạo ảnh. Không chạy toàn bộ `tests/` hoặc nghiệm thu EXE trong gói này.

Các test bổ sung kiểm cả storage cạnh tranh nhiều thread, nhiều namespace, instance khác, khôi phục ledger khi restart, fsync lỗi, snapshot quá lớn, cancel lúc chờ budget, GC sau durability/native-stop, crash trước tombstone GC flush, model-missing chỉ khóa đúng run, prep không sinh native parent, inner worker cap, load permit lỗi, hủy admission, ký số và handoff cache. Fixture lỗi kiểm hành vi đúng sau sửa; không dùng lại assertion xác nhận defect của review cũ.

Bằng chứng chính:

- [evidence.json — hash source/test, JUnit, native manifest và kết quả](D:/App/ocrtool/temp/fix_64_128_2026_10_23/evidence.json).
- [JUnit rộng](D:/App/ocrtool/temp/fix_64_128_2026_10_23/verified_regression.xml), [log](D:/App/ocrtool/temp/fix_64_128_2026_10_23/verified_regression.log).
- [JUnit core cuối](D:/App/ocrtool/temp/fix_64_128_2026_10_23/final_core.xml), [log](D:/App/ocrtool/temp/fix_64_128_2026_10_23/final_core.log).
- [30 test bổ sung](D:/App/ocrtool/temp/fix_64_128_2026_10_23/test_resource_fixes.py), [wrapper fixture và locale](D:/App/ocrtool/temp/fix_64_128_2026_10_23/run_regression.py), [xác nhận fixture](D:/App/ocrtool/temp/fix_64_128_2026_10_23/fixture_generation.json).
- [Native Tìm kỹ](D:/App/ocrtool/temp/fix_64_128_2026_10_23/handoff_final_thorough.log), [native Trang đầu](D:/App/ocrtool/temp/fix_64_128_2026_10_23/handoff_final_firstpage.log). Mỗi cây `handoff_final_*/bench_results/` giữ results, manifest, timeline đầy đủ, gồm metric storage mới `kie_spool_storage`.

**5. Cấu hình và phạm vi bàn giao**

Tham chiếu đã cập nhật trong `settings.ini.example`:

| Biến | Mặc định | Ý nghĩa |
|---|---:|---|
| `OCRTOOL_KIE_PAYLOAD_BUDGET_MB` | 512 | Byte của các job KIE đang xử lý |
| `OCRTOOL_KIE_SPOOL_BUDGET_MB` | 512 | Toàn bộ snapshot giữ lại + reservation trong cùng data root |
| `OCRTOOL_KIE_SPOOL_WAIT_S` | 30 | Chờ storage có thể hủy; hết thì checkpoint, dừng cấp file |
| `OCRTOOL_PREP_INNER_WORKERS` | 4 | Trần analyzer worker bên trong mỗi process prep, độc lập W |

Không tăng mặc định W/F/K/P hoặc tự bật feature-flag để suy đoán cấu hình tối ưu. Harness vẫn áp profile trước khi import model; GUI cần khởi động với profile tương ứng. Profile khởi điểm máy đích giữ W16/F8/chunk8/K2/P2/warmup4, KIE intra-op=2; chọn cấu hình cuối bằng benchmark cùng corpus/metadata, rồi soak.

Các giới hạn chưa được gói này chứng nhận: throughput và soak trên máy 64/128; nhận đúng org khác rỗng trên ground truth; READY theo incarnation/deadline từng pha đầy đủ; artifact frozen. Smoke tổng hợp chỉ hai PDF ảnh, tên cơ quan vẫn rỗng và không được tính là org-positive. Các đường legacy ngoài scan mật có cơ chế fallback riêng không được gói này biến thành một hệ thống native-stop thống nhất toàn ứng dụng.

Mã sửa trực tiếp trong working tree; không reset thay đổi có sẵn và không commit/build/publish. `temp/fix_64_128_2026_10_23/before/` giữ snapshot bảy tệp chính trước sửa. Nhiều module/test của nhánh hiện tại vốn untracked/ignored; khi chuyển máy phải mang đầy đủ source/model và test/evidence cần thiết, không chỉ Git HEAD. Các process của các lượt kiểm này đã thoát; không dừng các process Python công việc khác có sẵn.
