# Rà soát hiệu năng quét file mật — 20/10/2026

**Kết luận:** cấu hình 8 trang OCR / 8 tài liệu không tương đương 64 tác vụ OCR. Toàn ứng dụng chỉ có một pool 8 tiến trình OCR. Chưa có cơ sở coi đây là cấu hình tối ưu cho máy 64 CPU, RAM 128 GB; mã hiện tại còn giới hạn ở khâu cấp việc, tiền xử lý và hậu xử lý.

Đã đọc luồng thực thi hiện tại, chạy OCR thật trên dữ liệu giả lập và kiểm chứng riêng đường xử lý timeout. Không sửa mã ứng dụng hoặc cấu hình người dùng. Workspace đang có thay đổi chưa commit của công việc khác; các thay đổi đó được giữ nguyên.

## 1. Phạm vi và giới hạn bằng chứng

- Máy truy cập được: Intel i5-12400F, 6 nhân / 12 luồng, RAM 31,86 GiB; Python 3.12.0, PyMuPDF 1.26.7.
- Máy 64 CPU / 128 GB do người dùng mô tả chưa được đo trực tiếp. Chưa biết 64 là số nhân vật lý hay số luồng logic, loại ổ đĩa, chế độ quét và phiên bản đang chạy.
- Benchmark dùng 16 PDF tự sinh, mỗi PDF 2 trang ảnh JPEG, mỗi trang 28 dòng tiếng Việt vô hại. Không sử dụng hoặc gửi tài liệu thật ra ngoài.
- Đo hai lần sau warmup ở mỗi cấu hình. Đây là phép đo chẩn đoán nhỏ trên trang sạch, lặp nội dung; không đại diện cho mọi PDF, trang nghiêng, ảnh lớn hoặc kho dữ liệu thật.
- Bài đo per-file gọi `scan_one_file_for_secret_artifact`, gồm tiền xử lý, OCR, tìm dấu và ghi canonical. Không gồm Qt UI, duyệt cả thư mục, journal/registry và xóa thư mục tạm của `_run_worker`.
- PDF giả lập không có dấu mật nên không phát sinh LightGBM xác nhận trang đầu hoặc KIE lấy tên cơ quan/năm. Những nút thắt này được xác định bằng mã, chưa định lượng trong benchmark.

## 2. Hai mức song song thực sự hoạt động thế nào

| Cấu hình | Thực thi |
|---|---|
| Số trang OCR song song = 8 | Một `multiprocessing.Pool` dùng chung toàn app, 8 tiến trình, mỗi tiến trình một ScreenAI DLL |
| Số tài liệu song song = 8 | 8 thread điều phối tài liệu trong tiến trình chính |
| Tìm nhanh | Mỗi tài liệu gửi tối đa 1 trang; với 8 file-thread có tối đa 8 trang được yêu cầu cùng lúc, và thường ít hơn khi thread đang tiền xử lý/hậu xử lý |
| Tìm kỹ | Mỗi tài liệu gửi một nhóm trang; mặc định kích thước nhóm bằng số OCR worker, tối thiểu 2 |

Nguồn: [pool OCR](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:287), [file workers](D:/App/ocrtool/scanindex/ui/screens/secret_file_scan_screen.py:1827), [streaming](D:/App/ocrtool/scanindex/ui/screens/secret_file_scan_screen.py:1309).

Với Tìm kỹ, 8 tài liệu × nhóm 8 trang có thể đưa tổng cộng khoảng 64 trang vào hàng đợi khi đủ trang scan. Chỉ 8 trang thực thi OCR đồng thời. Với Tìm nhanh, tăng riêng OCR worker lên 16/32 trong khi vẫn chỉ có 8 file-thread không thể tạo 16/32 trang đang xử lý từ màn quét này.

Số CPU một DLL dùng không được điều chỉnh qua hai ô trên. Trong bài đo OCR riêng, tổng CPU time / tổng wall time của các job ở cấu hình 4 worker khoảng 1,06–1,09, tương đương khoảng một luồng CPU bận cho mỗi job ở mẫu này. Không nên mặc định mỗi DLL dùng đủ 8 nhân.

Nếu máy đích có **64 luồng logic**, 8 tác vụ mà mỗi tác vụ chỉ dùng khoảng một luồng tương ứng khoảng 12,5% tổng năng lực logic trong mô hình đơn giản. Đây là minh họa cách đọc tải CPU, không phải mức trần chính xác của toàn ứng dụng.

## 3. Các giới hạn và vấn đề đã xác nhận

### 3.1. Đổi số trang OCR cần khởi động lại

`ocr_app._apply_ocr_page_worker_setting()` đọc cấu hình trước khi import engine. Engine tính `_NUM_PAGE_WORKERS` đúng một lần khi import.

`MainWindow.save_settings()` lưu `MaxConcurrentOCR` và cập nhật thuộc tính UI, nhưng không cập nhật các biến module của engine, không dựng lại pool và không thông báo yêu cầu khởi động lại cho thay đổi này.

**Hệ quả:** giao diện có thể đã lưu 8 trong khi pool vẫn chạy ở số cũ cho đến khi thoát và mở lại ứng dụng. Số file-worker được đọc lại khi bắt đầu lượt quét, nên hai cấu hình có thể áp dụng khác thời điểm.

Đối chiếu sau khi mở lại bằng log `Page-level OCR pool ready: 8 worker processes (1 DLL each)`. Biến môi trường `DIRECT_OCR_NUM_PAGE_WORKERS`, các biến legacy của engine và `SECRET_SCAN_MAX_FILE_WORKERS` có thể ghi đè thiết lập tương ứng.

Nguồn: [khởi động](D:/App/ocrtool/ocr_app.py:84), [lưu cài đặt](D:/App/ocrtool/scanindex/ui/main_window.py:1255), [biến pool](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:95).

### 3.2. UI chặn số tài liệu ở 8

Khi lưu, `MaxFileWorkers` bị ép vào khoảng 1–8. Vì vậy chế độ Tìm nhanh đang có giới hạn kiến trúc ngay cả khi tăng số OCR worker.

Không nên chỉ nới con số này lên 32/64. File-worker là thread và có thao tác PyMuPDF trong process chính. Tài liệu chính thức PyMuPDF không hỗ trợ sử dụng đa luồng, kể cả mỗi thread mở một Document riêng; hướng được khuyến nghị là multiprocessing.

Nguồn: [giới hạn 8](D:/App/ocrtool/scanindex/ui/main_window.py:1278), [PyMuPDF multiprocessing](https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html).

### 3.3. Khâu chuẩn bị trang đáng kể; chưa có pipeline cấp việc độc lập

Luồng PDF scan hiện tại có thể trải qua:

1. Mở/kiểm tra PDF, cắt trang đầu hoặc cửa sổ trang.
2. Phân loại PDF để quyết định xoay.
3. Tiền xử lý lại phân loại PDF, render preview 200 DPI, nhận hướng, tìm góc nghiêng.
4. Nếu cần sửa nghiêng, raster lại ở DPI gốc hoặc fallback 300 DPI, xoay ảnh và nén JPEG.
5. Ghi PDF tạm; phân loại từng trang thêm lần nữa.
6. OCR worker mở PDF tạm và render lại ở 200 DPI.

`_preprocess_pdf_for_ocr` mặc định tối đa 4 thread phân tích trang cho mỗi lần gọi, độc lập với ô số trang OCR. `_run_worker` không truyền `ocr_workers`, nên mặc định này được dùng. Với Tìm nhanh một trang, số thread tiền xử lý thực tế bị giới hạn về 1.

Các bước fitz như cắt/ghi PDF và phân loại trang trong scanner giữ `_FITZ_LOCK`, vì vậy có phần chạy tuần tự giữa các file. Tuy nhiên `_pdf_file_kind` và `pre_process_pdf` gọi fitz ngoài khóa này: **không thể nói toàn bộ tiền xử lý đã được bảo vệ bởi một khóa chung**, cũng không nên bỏ khóa để tăng tốc.

`get_fine_deskew_angle` quét 33 góc thô và 21 góc tinh, có thể tốn thời gian ngay cả khi trang cuối cùng không cần sửa nghiêng. Trong mẫu 8/8, tổng thời gian gọi deskew khoảng 9,37–10,76 giây cho 16 file; tổng thời gian gọi tiền xử lý khoảng 17,28–18,83 giây. Đây là thời gian của các lời gọi chồng lấp, **không cộng trực tiếp vào wall time của lượt chạy**.

Nguồn: [tiền xử lý scanner](D:/App/ocrtool/scanindex/ui/screens/secret_file_scan_screen.py:799), [deskew](D:/App/ocrtool/scanindex/core/preprocessing/preprocessing.py:665), [pipeline tiền xử lý](D:/App/ocrtool/scanindex/core/preprocessing/preprocessing.py:806).

### 3.4. Tìm kỹ chờ hết nhóm trang rồi mới tìm dấu

`_process_and_detect_streaming` gọi `_process_pdf_per_page` cho cả nhóm; hàm này thu đủ OCR rồi mới trả canonical cho bước tìm dấu.

Do đó, với nhóm 8 trang, dấu ở trang 1 vẫn có thể phải chờ OCR xong cả 8 trang. Dừng sớm diễn ra giữa các nhóm, không phải ngay khi một trang OCR xong. Tăng OCR worker cũng tăng nhóm mặc định, có thể tăng số trang làm thừa trên tài liệu có dấu ở đầu.

Hàng đợi `_collect_page_ocr_bounded` mặc định tối đa 16 trang **cho mỗi lời gọi**, không phải giới hạn toàn cục. Một file không thể tự cấp quá 16 trang đang chờ cho pool 32 worker; nhiều file thì có thể cộng dồn và làm hàng đợi dài.

Nguồn: [streaming](D:/App/ocrtool/scanindex/ui/screens/secret_file_scan_screen.py:1332), [thu kết quả OCR](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:2212).

### 3.5. KIE có thể làm các file chờ, dù OCR pool rảnh

Sau khi phát hiện dấu, nếu tên file không tra được cơ quan, scanner gọi LayoutLMv3 lấy tên cơ quan dự phòng. Một số trường hợp năm ban hành nhiễu cũng dùng KIE.

Toàn bộ `_run_layoutlmv3` được bọc trong `_KIE_INFER_LOCK`: chỉ một file chạy phần này tại một thời điểm. Model ONNX mặc định dùng 2 intra-op threads. Vì file-worker đợi KIE xong mới nhận file tiếp theo, tập dữ liệu nhiều tài liệu mật cần KIE có thể làm giảm tốc độ cấp trang cho OCR.

Đây là nút thắt có điều kiện; không giải thích được tập dữ liệu hoàn toàn không có dấu mật.

Nguồn: [khóa KIE](D:/App/ocrtool/scanindex/ui/screens/secret_file_scan_screen.py:1562), [gọi KIE](D:/App/ocrtool/scanindex/ui/screens/secret_file_scan_screen.py:1607), [ONNX threads](D:/App/ocrtool/scanindex/core/kie/engine.py:186).

### 3.6. Có chi phí I/O và khởi tạo lại worker

- Mỗi file kết thúc có `prog.save()` và có thể thêm `registry.save()` trong `state_lock`; cả hai gọi `fsync`. Trên ổ chậm, đây là điểm chờ tuần tự. File journal nằm dưới thư mục ứng dụng, không nhất thiết cùng ổ với thư mục tài liệu.
- Cắt, ghi, mở lại và xóa PDF tạm có thêm chi phí I/O. Nếu ứng dụng hoặc thư mục nguồn ở ổ mạng, cần đo riêng.
- OCR worker được thay mới sau 100 task, phải load DLL/model lại. Đây là cơ chế kiểm soát rò bộ nhớ; chưa đo chi phí dài hạn trong lượt này, không đề xuất bỏ giới hạn ngay.
- RAM thấp là phù hợp với thiết kế giữ lượng công việc chờ có giới hạn. Dùng thêm RAM không tự làm tăng thông lượng.

Nguồn: [lưu cuối file](D:/App/ocrtool/scanindex/ui/screens/secret_file_scan_screen.py:4329), [journal fsync](D:/App/ocrtool/scanindex/core/secret_scan_progress.py:437), [registry fsync](D:/App/ocrtool/scanindex/core/secret_scan_progress.py:720), [worker recycling](D:/App/ocrtool/scanindex/core/ocr/direct_engine.py:116).

### 3.7. Lỗi cần sửa trước khi tăng mạnh hàng đợi: timeout thành trang rỗng

Timeout mặc định 120 giây tính từ lúc submit, gồm cả thời gian chờ hàng đợi. Khi lấy kết quả lỗi/timeout, `_collect_page_ocr_bounded` ghi log nhưng thay kết quả bằng `_empty_page_ocr_result()`.

`_process_pdf_per_page` chỉ từ chối kết quả `None`; dict rỗng hợp lệ về hình thức vẫn được lắp thành trang canonical không có chữ. Caller vì thế có thể xem file là đã quét xong nhưng không thấy dấu.

**Đã tái hiện bằng fault injection:** ép một AsyncResult timeout; collector trả trang không chữ, kích thước render bằng 0; scanner lắp canonical mà không raise. Đây là chứng cứ đường lỗi trong mã, không phải bằng chứng máy người dùng đang timeout.

Khi pool lỗi nặng, engine còn có đường fallback OCR tuần tự qua `_ocr_lock`. Cần quan sát log `falling back to serial`, `single-worker`, `OCR error` để phân biệt với pool thực sự hoạt động đủ worker.

## 4. Kết quả benchmark

| Cấu hình trang / file | OCR riêng, 16 trang | Tìm nhanh per-file, 16 PDF | CPU bận tương đương khi Tìm nhanh | RSS đỉnh cây tiến trình |
|---|---:|---:|---:|---:|
| 4 / 4 | 2,89–3,11 s | 6,91–7,07 s | 5,87–6,24 luồng | 2,40–2,82 GiB |
| 8 / 8 | 2,12–2,19 s | 5,55–5,80 s | 6,43–7,08 luồng | 4,00–4,46 GiB |

Trung bình Tìm nhanh: 6,99 → 5,67 giây, tăng thông lượng khoảng **1,23 lần**, chưa phải gấp đôi khi tăng từ 4/4 lên 8/8. Không suy con số này thành mức tăng tốc trên máy 64 CPU.

Các worker warmup đều được xác nhận đã nhận việc: đủ 4 hoặc 8 PID. Không có lỗi OCR trong phần OCR riêng. Benchmark per-file không phát hiện dấu trên fixture vô hại.

Script và số liệu:

- [Benchmark](D:/App/ocrtool/temp/secret_scan_perf_review/benchmark.py).
- [Kết quả 4/4](D:/App/ocrtool/temp/secret_scan_perf_review/result_4_4.json).
- [Kết quả 8/8](D:/App/ocrtool/temp/secret_scan_perf_review/result_8_8.json).
- [Tái hiện timeout](D:/App/ocrtool/temp/secret_scan_perf_review/verify_timeout.py), [kết quả](D:/App/ocrtool/temp/secret_scan_perf_review/timeout_result.json).

Đã chạy nhóm test cấu hình file-worker, precheck, giải mật và điều hướng: **34 passed**. Các test này không xác nhận hiệu năng máy đích hoặc thay thế kiểm thử OCR lỗi.

## 5. Cấu hình thử và hướng sửa

### Với mã hiện tại

1. Lưu cài đặt, thoát hẳn và mở lại; kiểm tra số worker thực trong log.
2. Nếu dùng **Tìm nhanh**: 8/8 là mức thử hợp lý trong giới hạn UI hiện tại. Tăng riêng số trang lên 32/64 không vượt được giới hạn 8 file đang hoạt động.
3. Nếu dùng **Tìm kỹ**: thử 16 trang / 4 tài liệu, rồi 24 trang / 4 tài liệu trên cùng tập PDF đại diện, sau mỗi lần đổi phải mở lại. Đây là các điểm thử, chưa phải cấu hình tối ưu đã đo cho máy đích. Cần theo dõi số trang thực sự OCR, vì nhóm lớn làm tăng công việc thừa trước dừng sớm.
4. So sánh file/phút, trang OCR/giây, lỗi và độ đầy đủ kết quả. Không lấy mức RAM cao hay CPU 100% làm tiêu chí duy nhất.
5. Đo riêng tập không có dấu, tập có dấu cần KIE và tập nhiều trang; kiểm tra hiệu ứng cache/resume để các lượt so sánh làm cùng lượng công việc.

### Thứ tự cải thiện đề xuất

1. Sửa trạng thái lỗi OCR/timeout để không ghi thành kết quả quét thành công; hiển thị số worker thực, cảnh báo cần mở lại khi cấu hình thay đổi.
2. Đo thời gian chuẩn bị, chờ queue, render, OCR, KIE và ghi journal; đo độ bận của pool. Từ đó xác định nút thắt trên máy đích.
3. Chuyển phần xử lý PDF/chuẩn bị trang sang process có Document riêng; thiết kế pipeline cấp việc liên tục cho OCR. Sau đó mới nâng giới hạn tài liệu an toàn trên máy lớn.
4. Giảm vòng cắt → ghi PDF tạm → mở/render lại; tái dùng phân loại và hình học trang. Giữ đúng metadata xoay và tọa độ khi thay đường xử lý.
5. Tách hàng đợi KIE khỏi file-worker OCR; cân nhắc batch hoặc một số ít KIE worker theo đo đạc, tránh nhiều bản model tranh CPU.
6. Tách kích thước nhóm Tìm kỹ khỏi số OCR worker, thu kết quả dần và giữ đúng thứ tự trang khi quyết định dừng sớm. Giới hạn queue toàn cục và tính timeout hợp lý.
7. Đánh giá checkpoint journal theo nhóm/thời gian nếu `fsync` là nút thắt, với tiêu chí rõ về khả năng quét tiếp sau mất điện. Không bỏ đảm bảo lưu bền chỉ để tăng số đo tốc độ.

Chỉ sau các bước trên mới có cơ sở chọn 16/24/32 OCR worker và mức song song tài liệu cao hơn cho máy 64 CPU, 128 GB.
