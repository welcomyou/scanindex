# WKP: kết quả sửa và kiểm thử mã nguồn

Chốt ngày **23/10/2026**, theo yêu cầu sửa trực tiếp sau review `FIX_REPORT_wkp_review3_2026-10-22.md`. Môi trường nghiệm thu lần cuối: **`D:\App\ocrtool\.venv_build\Scripts\python.exe`**. Phạm vi chốt là mã nguồn; **không nghiệm thu EXE/dist**.

**Kết luận: đủ điều kiện đưa nhánh mã nguồn hiện tại lên máy 64 CPU/128 GB để chạy pilot scan mật có giám sát và benchmark chọn cấu hình.** Các lỗi tích hợp chặn pilot phát hiện trong review đã được sửa và có kiểm thử hành vi đúng. Chưa có cơ sở khẳng định đã đạt tốc độ tối đa trên máy đích, hoặc cho phép triển khai toàn bộ ứng dụng vào vận hành chính thức: máy kiểm tra có 12 CPU logic/31,86 GiB RAM; bộ kiểm thử Kho lưu trữ vẫn còn lỗi Tantivy riêng.

## 1. Những lỗi đã sửa

| Vấn đề review | Thay đổi thực tế | Cách xác nhận |
|---|---|---|
| Callback spool tự gọi nhầm chính nó, KIE không được submit | Đổi tên adapter cục bộ; gọi đúng helper tạo snapshot; nối lại đường submit/commit | Chạy `_run_worker` với producer spool thật; smoke model thật qua harness |
| Producer và resume dùng khóa spool khác nhau | Codec chung có `schema_version`, `canonical`, `hash`; đọc tương thích `spool_path`/`sha256`; định dạng không hợp lệ chuyển về rescan có lý do | Quét tạo spool rồi resume trong **interpreter khác**; nguồn không đổi không gọi lại OCR |
| GC xóa snapshot khi còn retry/lease hoặc chưa persist | Xét cả nghĩa vụ metadata/rescan; bắt buộc xác nhận durability trước GC; cleanup dùng sổ lease; chỉ bỏ record sau khi xác nhận xóa | Test retry, input lease đang giữ, persist chưa được xác nhận, tombstone/replay |
| Job Object không hoạt động đúng trên Windows x64/spawn | Khai báo đầy đủ ABI Win32; named Job riêng mỗi phiên; parent giữ handle; child mở theo tên, attach và xác nhận membership rồi đóng handle tạm; attach thất bại thì không chạy native | Test W/K/P qua file import được; child ACK và còn sống **trước** parent crash; sau crash child phải chết |
| Soft shutdown làm mất process handles; hard-stop/deadline báo sai | Giữ tham chiếu process trước khi executor xóa `_processes`; theo dõi cả pool bị thay; deadline tuyệt đối; kết quả shutdown chỉ thành công khi xác nhận worker thoát | ProcessPool thật có worker bị chặn; soft → hard phải hết process; test deadline, concurrent caller và cached failure |
| Đóng cửa sổ chặn Qt thread | Chạy runtime shutdown ở thread riêng, QTimer nhận kết quả; chưa xác nhận thoát thì giữ cửa sổ và báo lỗi | Fixture Qt gọi `MainWindow.closeEvent`, kiểm tra event loop tiếp tục hoạt động |
| KIE deferred của file chưa hoàn tất bị đưa vào đường OCR lại | Cho cả `pending` và `rescan_tasks` dùng KIE-only recovery khi snapshot còn hợp lệ; giữ đúng replace semantics | Test restart qua hai process riêng; test rescan/commit/persistence hiện có |
| Harness lỗi entry/bootstrap, đường dẫn model, đo CPU/RAM và đếm file hoàn tất | Bootstrap chung; QApplication/event loop thật; tách app/model root khỏi data root; sampler Win32 có PID + creation time; journal namespace riêng; reload journal/registry để đếm kết quả bền vững | CLI từ script và app entry; so RSS với psutil; 5 lượt smoke native thành công |

Các thay đổi hỗ trợ: ghi lỗi worker riêng theo PID; lỗi OCR được trả về parent bằng sentinel có loại lỗi/nội dung thay vì `None`; xử lý import-order `six`; lệnh kiểm tra chữ ký ScreenAI nạp rõ module Security của Windows PowerShell. Không tắt xác minh chữ ký, không thay thư viện trong `.venv_build`.

Các tệp chính: `scanindex/infra/windows_job.py`, `scanindex/core/runtime/shutdown.py`, `scanindex/infra/bootstrap.py`, `scanindex/core/secret_scan_spool.py`, `scanindex/core/secret_scan_progress.py`, `scanindex/core/secret_scan_kie.py`, `scanindex/core/preprocessing/prep_worker.py`, `scanindex/core/ocr/direct_engine.py`, `scanindex/ui/screens/secret_file_scan_screen.py`, `scanindex/ui/main_window.py`, `scripts/bench_secret_scan.py`.

Workspace đã có nhiều sửa đổi trước lượt này; không quy toàn bộ `git diff` thành thay đổi của báo cáo này. Snapshot trước sửa và manifest ban đầu nằm trong `temp/secret_scan_perf_review/implementation_2026_10_22/before/`. Không reset/commit hoặc ghi đè báo cáo gốc.

## 2. Kết quả kiểm thử dùng đúng `.venv_build`

| Lượt kiểm tra | Kết quả |
|---|---|
| Toàn bộ nhóm hồi quy liên quan: KIE, prep, admission, resume, journal/writer, lifecycle, UI, i18n, paths/config, tái sử dụng OCR ở số hóa | **271 passed**, 0 failed/error/skip; 81,35 giây |
| Nhóm lifecycle/warmup/UI và 13 test đóng lỗi tích hợp chạy riêng | **42 passed**, 0 failed/error/skip; 34,86 giây. Nhóm này nằm trong 271 test trên, **không cộng thành 313 test độc lập** |
| Model thật, Tìm kỹ, W2/F2/chunk4/K1/P1/warmup1, ép quét lại, OCR worker recycle sau 3 task | **3/3 lượt đạt**, mỗi lượt 2 PDF ảnh × 3 trang; đủ 2 kết quả bền vững; failures/rescan/metadata/persisted pending đều 0 |
| Model thật, Trang đầu, cùng corpus và W2/F2/chunk4/K1/P1/warmup1 | **2/2 lượt đạt**, mỗi lượt đủ 2 kết quả bền vững; không pending/lỗi |
| Toàn bộ `tests/` | Lượt chạy qua đêm chưa hoàn tất, đã dừng process test được xác định bằng PID + command line. **Không tính là pass**, không có kết quả full-suite sạch |
| Hai test Kho lưu trữ được kiểm riêng | `test_recall_8000_docs`: fail; fixture của `test_fuzzy_two_edits_on_short_tokens_is_not_near`: error. Cùng báo `Tantivy index writer was killed` |
| Đối chứng hai test Kho lưu trữ với module paths trước sửa trong cùng `.venv_build` | `test_recall_8000_docs` vẫn fail với cùng lỗi writer; test còn lại pass trong lượt đối chứng. Đây là đối chứng riêng module paths, **không phải toàn bộ checkout trước sửa** |

Hai lỗi Kho lưu trữ chưa được sửa trong gói này. Bằng chứng hiện tại cho thấy lỗi writer cũng tái hiện khi dùng paths trước sửa; chưa xác định được nguyên nhân gốc I/O/native. Vì vậy không tuyên bố toàn bộ ứng dụng đạt nghiệm thu. Scan mật dùng journal/registry của nó, không đi qua đường rebuild Tantivy bị lỗi trong hai test này.

Môi trường: Python 3.12.0, NumPy 1.26.4, PySide6 6.10.2, ONNX Runtime 1.24.4, LightGBM 4.6.0, Tantivy 0.26.0, PyMuPDF 1.26.7, pytest 9.0.3. Manifest lưu cả phiên bản distribution và `cv2.__version__` để tránh nhầm khi môi trường có nhiều wheel OpenCV.

Không dùng số file/phút của corpus 2 file để kết luận nhanh hơn bản cũ. Các lượt smoke chỉ chứng minh kết nối pipeline, metadata/persist và vòng đời process hoạt động; không thay thế A/B hoặc soak trên corpus thực.

## 3. Bằng chứng kiểm tra lại được

Tất cả đường dẫn dưới đây tính từ `D:\App\ocrtool\`:

- Tổng hợp kết quả, phiên bản thư viện và SHA-256 toàn bộ mã nguồn được kiểm: [final_source_evidence_2026_10_23.json](temp/secret_scan_perf_review/implementation_2026_10_22/final_source_evidence_2026_10_23.json).
- JUnit 271 test: [requested_venv_relevant.xml](temp/secret_scan_perf_review/implementation_2026_10_22/requested_venv_relevant.xml); [log đầy đủ tên test](temp/secret_scan_perf_review/implementation_2026_10_22/requested_venv_relevant.log).
- Smoke Tìm kỹ: [results.json](temp/secret_scan_perf_review/implementation_2026_10_22/native_bcc37b8ad6/state/bench_results/c2c7dd77d722456992c835415bd83aef/results.json).
- Smoke Trang đầu: [results.json](temp/secret_scan_perf_review/implementation_2026_10_22/requested_venv_firstpage_state/bench_results/c1ff135a88444f6db61af65f43f248d2/results.json).
- Mỗi thư mục smoke có `manifest.json` ghi requested/effective, model hash và source hash; `timeline_run*.jsonl` ghi CPU/RAM/process/queue/lease. Đã đối chiếu source hash của cả hai smoke với mã hiện tại: **khớp**.
- Lỗi Kho lưu trữ: [requested_venv_repository.log](temp/secret_scan_perf_review/implementation_2026_10_22/requested_venv_repository.log); [đối chứng](temp/secret_scan_perf_review/implementation_2026_10_22/requested_venv_repository_baseline.log).
- Test bổ sung: `temp/secret_scan_perf_review/test_wkp_closure_2026_10_22.py`; fixture restart và Qt close nằm cùng thư mục. Theo quy định dự án, các probe/test bổ sung của lượt này được giữ dưới `temp/`; cần mang theo thư mục bằng chứng nếu sao chép source sang máy khác.

Sau các lượt kiểm cuối, không còn process Python của `.venv_build` do các lượt kiểm này để lại. Không dừng các process Python công việc khác.

## 4. Chạy pilot mã nguồn trên máy 64 CPU/128 GB

Chạy từ thư mục source có đầy đủ models. Corpus nên là **bản sao** tài liệu đại diện, có file ngắn/dài, PDF ảnh, dấu ở trang sau, trang xoay và mẫu cần KIE. `data-dir` phải tách khỏi corpus và dữ liệu ứng dụng đang dùng; lần đầu dùng thư mục mới/rỗng. Harness tự giữ marker và OS lock để tránh hai lượt cùng ghi.

```powershell
Set-Location 'D:\App\ocrtool'
& 'D:\App\ocrtool\.venv_build\Scripts\python.exe' scripts/bench_secret_scan.py `
  --corpus 'D:\corpus_bench' `
  --data-dir 'D:\bench_wkp_64_128_W16' `
  --mode thorough --runs 3 --force-rescan `
  --profile 'W=16,F=8,CHUNK=8,KIE_PROC=2,PREP_PROC=2,WARMUP_K=4'
```

Đây là **điểm bắt đầu**, không phải cấu hình tối ưu đã đo. W là số OCR worker toàn app, F là số file đang chạy, CHUNK là số trang mỗi đợt của một file ở Tìm kỹ; KIE_PROC/PREP_PROC là worker hai giai đoạn riêng. W8 và F8 không tạo 64 OCR worker. Không cần người dùng phổ thông tự nhập cả sáu biến; các biến bổ sung giúp đo và điều chỉnh các nút nghẽn độc lập.

Trình tự chọn cấu hình cụ thể:

1. Chạy W8 → W16 → W24 → W32, giữ **F8/chunk8/K2/P2/warmup4**, cùng corpus/mode/model. Mỗi cấu hình có data-dir riêng. Chỉ thử W48/W64 nếu tăng W vẫn làm tăng số file hoàn tất/phút và còn headroom RAM/commit.
2. Với W tốt nhất, thử F4/F8/F16, giữ chunk8. Sau đó mới thử chunk4/8/16, giữ W/F. Không thay nhiều yếu tố cùng lúc rồi quy kết lợi ích cho worker.
3. Nếu OCR thường nhàn trong khi KIE outstanding/deliveries chưa giảm, thử K2 → K4 → K8, giữ ONNX threads=2 và kiểm tra RAM. Nếu prep bão hòa trong khi OCR thiếu việc, thử P2 → P4. Tăng W khi stage phía trước/sau đang nghẽn thường không giúp.
4. Chỉ lấy lượt có `benchmark_pass=true`, `durability_confirmed=true`, không failure/pending/sampling_error. So median file mới hoàn tất/phút của các lượt quét lại; xem riêng lượt đầu có chi phí load model. CPU thấp/RAM thấp tự nó không phải tiêu chí thất bại; mục tiêu là throughput hoàn chỉnh và tính đúng.
5. Với cấu hình thắng, chạy corpus lớn nhiều lượt/soak, kiểm hủy → tiếp tục, đóng app giữa KIE, mở lại và resume, không còn process/lease tồn sau kết thúc. Đối chiếu kết quả dấu, năm và giải mật với tập kỳ vọng trước khi áp vào dữ liệu vận hành.

Lệnh benchmark chỉ áp profile cho process chạy lệnh, **không tự lưu cấu hình giao diện**. Các env tương ứng được ghi trong `scripts/bench_secret_scan.py::apply_profile` và `settings.ini.example`; khi chạy GUI phải đặt chúng trước khi khởi động `ocr_app.py`, hoặc dùng các mục UI tương ứng.

## 5. Những giới hạn còn phải đo/đối chiếu ở máy đích

Các mục này không được dùng để trì hoãn pilot thêm một vòng viết plan; đây là tiêu chí nghiệm thu pilot:

- **Tốc độ 64/128:** chưa đo, cần thực hiện sweep ở mục 4. Không có con số tăng tốc hoặc cấu hình “tận dụng hết” đã được chứng minh.
- **KIE tên cơ quan:** test tích hợp có kiểm tra truyền/commit metadata; smoke model thật xác nhận không còn pending, nhưng mẫu tổng hợp không chứng minh nhận đúng một tên cơ quan khác rỗng. Smoke parity trước đó trả org rỗng ở cả sync/deferred, không được tính là đạt org-positive. Dùng mẫu được gán nhãn có cơ quan rõ ràng để so sync/deferred và ground truth. Nếu cả hai cùng rỗng, kiểm OCR đầu vào, model/token mapping và confidence; nếu sync có mà deferred mất, đó là lỗi đường truyền cần chặn nghiệm thu. Không tắt KIE để làm đẹp benchmark.
- **Readiness sau worker recycle:** telemetry `ready_live_workers` chỉ đếm ACK warmup còn sống; worker thay thế tự load qua load semaphore, nhưng chưa có sổ READY theo từng incarnation đầy đủ. Smoke đã chạy qua recycle thành công; không được diễn giải `ready_live_workers=0` sau recycle là toàn bộ worker chết hoặc dùng chỉ số đó một mình để kết luận hiệu năng. Theo dõi PID/CPU, hoàn tất task, queue và số load đang chạy. Nếu pilot gặp replacement không phục hồi hoặc chu kỳ load làm tụt throughput, cần bổ sung READY/FAILED theo incarnation và deadline vào supervisor trước nghiệm thu soak; không nâng giả số ready bằng số process sống.
- **Kho lưu trữ/Tantivy:** lỗi rebuild index vẫn mở, có bằng chứng riêng ở mục 2. Không coi kết quả scan mật là giấy phép đưa chức năng Kho lưu trữ đang lỗi vào vận hành.
- **Đóng gói:** ngoài phạm vi yêu cầu cuối; mọi artifact thử đóng gói trước khi giới hạn phạm vi không phải bản bàn giao và không dùng để triển khai.

Không bắt buộc đổi journal sang SQLite hoặc viết lại toàn bộ pool để bắt đầu pilot. Điều kiện mở pilot là tính đúng của đường submit/resume/persist, ownership và phép đo đã được kiểm; điều kiện vận hành và cấu hình tối ưu là kết quả corpus/soak trên máy đích.
