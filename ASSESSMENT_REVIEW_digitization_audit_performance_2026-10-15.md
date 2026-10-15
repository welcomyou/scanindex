# Đánh giá bản REVIEW về tăng tốc rà soát số hóa

Ngày: 15/10/2026. Đối tượng: `REVIEW_PLAN_digitization_audit_performance_2026-10-15.md`.

**Kết luận:** tiếp thu hướng ưu tiên giảm chi phí tìm xref trong full, giữ pool giữa các lượt và tối ưu UI/cache. Tuy nhiên, bản review có lỗi số liệu, một ví dụ DPI sai, diễn giải benchmark quá rộng và prompt phủ nhận quy tắc người dùng đã yêu cầu. **Không nên giao AI triển khai nguyên văn mục 6 của review.**

Đã đọc cả ba script benchmark và chạy kiểm chứng độc lập. Không sửa bản review, kế hoạch chính hoặc mã ứng dụng trong lượt đánh giá này.

## 1. Những điểm nên tiếp thu

- Hạn chế DPI/trích text không giải quyết chi phí tìm xref ảnh vẫn phải chạy toàn file cho màu/nén. Điều này phù hợp cảnh báo đã có trong plan; đo lại trên một phần corpus của review xác nhận gần như không có cải thiện wall time ở kịch bản scan này.
- Nên nâng **thử nghiệm fast-path tìm filter không cần hash ảnh** lên sớm cho đường full, thay vì để cuối. Ưu tiên là xây đường xử lý đúng có fallback rồi đo, không đưa thẳng prototype vào production.
- Pool tồn tại lâu, payload nhỏ, queue giới hạn, UI lazy, debounce và reuse kết quả đều là hướng phù hợp.
- Nên tách **thay đổi nghiệp vụ** và **tối ưu cùng nghiệp vụ** thành hai phép so sánh. Không hứa giới hạn DPI/OCR sẽ làm nhanh nhiều lần.

## 2. Các điểm phải sửa trong review

### 2.1. Không được bỏ yêu cầu mới nhất của người dùng

Review mục 4.1/6 yêu cầu chưa làm DPI trang 1, OCR một trong 5 trang đầu cho đến khi xác nhận riêng. Nhưng người dùng đã trực tiếp yêu cầu hai quy tắc đó trong cuộc trao đổi này. Không cần yêu cầu xác nhận lại chỉ vì tác dụng tăng tốc nhỏ.

Cách chỉnh: giữ policy đã yêu cầu, ghi rõ nó thay đổi phạm vi kết luận, tách commit/benchmark nghiệp vụ khỏi tối ưu kỹ thuật. Có thể khuyến nghị người dùng cân nhắc lại phạm vi nếu họ muốn, nhưng không dùng prompt để tự phủ quyết yêu cầu.

### 2.2. Ví dụ DPI tại mục 4.1 bị đảo

Review nói trang 1 là 250 DPI, các trang sau 300 DPI thì policy mới đạt. Điều này sai: policy mới xét trang 1 nên vẫn không đạt.

Đã tạo PDF kiểm chứng có trang 1 250 DPI và trang 2 300 DPI. Cả `audit_file(..., quick=False)` hiện tại lẫn `audit_pdf_policy_new()` của chính reviewer đều trả `dpi_ok=False`.

Ví dụ đúng để thể hiện nới phạm vi: trang 1 300 DPI, trang sau 250 DPI. Bản cũ không đạt; policy trang 1 đạt.

### 2.3. Bảng hiệu năng trộn baseline hoặc tính sai

Trong mục 2.2:

- Quick serial 48 file = 1,74 s; pool = 0,42–0,49 s. Nếu cùng 48 file thì tăng tốc là **3,55–4,14×**, không phải 0,8–0,9×.
- Nếu 0,42–0,49 s thuộc phép đo 12 file ở script khác, phải ghi đúng số file và baseline 12 file; không đặt cạnh baseline 48 file mà không phân biệt.
- 48 file / 1,74 s = **27,6 file/s**, không phải 94 file/s. 48 / 0,40 = 120 file/s.
- Không tăng tốc là xấp xỉ **1×**, không phải “0×” như bảng lộ trình mục 5.

Các sai khác này không bác khả năng process pool giúp nhanh hơn; chúng khiến bảng hiện tại không đủ nhất quán để dùng làm yêu cầu nghiệm thu.

### 2.4. “Bắt buộc chunk 3–6, không được 1 file/task” chưa được chứng minh

Các phép đo review thay đổi đồng thời: số file, số worker, startup pool, chunk size và payload trả về. `bench_audit.py` trả toàn `PdfAuditResult`, `bench_more.py` trả số trang, còn `bench_big.py` trả path/bad flag. Không thể quy toàn bộ cải thiện cho chunk.

Các điểm phương pháp khác:

- `bench_more.py` đặt tiêu đề pool tồn tại lâu nhưng `audit_chunked()` tạo pool mới mỗi lần gọi.
- `bench_big.py` chỉ submit một warmup job; như vậy chưa xác nhận mọi worker đã khởi tạo. Các lượt sau có thể đã warm, nhưng cần đo/ghi nhận rõ.
- Script thường báo lượt cuối, không báo median và độ phân tán.
- Không có đối chứng warm pool, cùng corpus/worker/payload, chunk=1 so với chunk=4.

Đã bổ sung đối chứng ở mục 3: **1 file/task vẫn đạt khoảng 3,38×** khi pool đã warm. Chunk=4 đạt khoảng 3,90×. Chunk có lợi trong phép thử này nhưng không phải điều kiện bắt buộc để có tăng tốc.

Đề xuất sửa: tune chunk=1/2/4/8 trên corpus thật. File dài hoặc tác vụ người dùng đang chọn nên giữ đơn lẻ để giảm chờ/hủy; gom nhóm nhỏ cho nhiều file ngắn nếu đo được lợi ích. Không ép full một file đang xem vào nhóm 3–6 file.

### 2.5. Prototype fast-path có thể kết luận sai nén

`temp/bench_audit/bench_more.py:51–69` lấy resource theo tên rồi ghi filter liên tiếp vào `result.images[-1]`. Nó không ánh xạ từng resource với từng placement, không gắn xref thực vào entry. Filter cuối có thể ghi đè filter vi phạm trước đó.

**Đã tái hiện:** một trang có ảnh JPEG trước, ảnh Flate sau:

| Kết quả | Bộ kiểm tra hiện tại | Prototype không decode |
|---|---|---|
| Filter theo ảnh | `[DCTDecode]`, `[FlateDecode]` | `[]`, `[FlateDecode]` |
| `compression_ok` | `False` | `True` |

Review đã thừa nhận corpus đơn giản và đề xuất fallback; vì vậy lỗi này không bác bỏ hướng fast-path. Nó cho thấy số 24,7× là **tiềm năng của prototype trên corpus thuận lợi**, không phải hiệu năng của một implementation đã bảo toàn đúng đắn.

Ngoài ra, script chỉ đối chiếu một file đầu tiên và một số trường kết luận, không toàn bộ corpus/evidence. Chưa có chứng cứ thực nghiệm cho guard/fallback đề xuất trong mục 4.3.

Khi triển khai:

- Chỉ chốt bằng resource/filter nếu chứng minh tập resource được xét bao phủ tất cả ảnh phù hợp và không bỏ inline/nguồn ảnh khác.
- Số resource bằng số placement tự nó không chứng minh mapping/coverage; resource thừa và inline hoặc resource trùng tên trong Form có thể làm phép ghép đơn giản sai.
- Nhánh mọi filter an toàn có thể dùng lập luận bảo thủ nếu đủ coverage; gặp không rõ thì fallback đúng trang.
- Không dùng dict chỉ theo tên resource toàn trang nếu namespace của Form có thể khác nhau.
- Gắn xref/filter/evidence đúng hoặc thiết kế phép tổng hợp độc lập không giả vờ có mapping. Đặc biệt không gọi kiểm tra JPX với xref=0 từ entry chưa ánh xạ.
- Test đa ảnh, JPEG+Flate, mask/SMask, inline, nested Form, resource không vẽ, ảnh dấu và JPX; sau đó đo lại tốc độ có fallback thật.

### 2.6. Không khái quát “95%” thành đặc tính cố định trên dữ liệu thật

Chi phí của `xrefs=True` là hướng nghi ngờ có cơ sở. Nhưng 95,6% là tỉ trọng trong một phép đo tổng hợp cụ thể, không phải hằng số của thư viện.

`breakdown_one()` gọi cả xrefs=False rồi xrefs=True trên cùng page và cộng chúng vào mẫu số; không bao trọn thời gian load page, ImageInfo, conclude, UI hoặc chi phí mạng. Nó giúp tìm thao tác nặng, không phải profiler chính xác của full pipeline.

Kho khác có ảnh/nén/kích thước/storage khác sẽ có cơ cấu chi phí khác. Tương tự, “cache gần tức thời” chưa có benchmark triển khai cache trong các script; enumeration/stat vẫn tốn thời gian, nhất là nhiều file trên NAS.

## 3. Kết quả kiểm chứng độc lập

Môi trường chạy: Python 3.12.0, PyMuPDF 1.26.7, `.venv_build` của dự án. Corpus tổng hợp đã có của reviewer, 12 PDF khoảng 296 MB. Không sinh thêm bản sao corpus 1,2 GB.

Script: `temp/bench_audit_review/verify_review.py`.
Kết quả đầy đủ và từng lượt: `temp/bench_audit_review/results.json`.

### 3.1. Full: 3 file đầu corpus, median 3 lượt

| Đường chạy | Thời gian |
|---|---:|
| Full hiện tại | 2,767 s |
| Mô phỏng policy DPI trang 1 / OCR tối đa 5 | 2,793 s |
| Prototype không decode của reviewer | 0,103 s |

Policy mới gần như không cải thiện trong mẫu này; chênh lệch nhỏ không đủ kết luận nó chậm hơn một cách ổn định. Prototype nhanh khoảng 27× trên mẫu thuận lợi, nhưng mắc lỗi JPEG+Flate đã nêu nên chưa thể dùng làm bản thay thế đúng.

Đây là chạy lại các hàm prototype của reviewer, không phải implementation đầy đủ policy mới. Hàm policy vẫn gọi `_image_entry()` cho mọi ảnh, tức vẫn tính DPI trung gian toàn file rồi mới lọc kết luận; cũng chưa bao phủ hết error/counter semantics trong plan. Vì vậy đây là kiểm tra xu hướng chi phí, không phải chứng nhận đúng/tối ưu tuyệt đối của policy mới.

### 3.2. Quick: đối chứng cùng payload và pool đã warm

12 đường dẫn PDF lặp 4 lần = **48 jobs trên 12 file riêng biệt**, không phải 48 file khác nhau. OS/file cache đã được làm ấm bằng chạy trước. 6 worker; warmup kiểm tra thực sự nhận được 6 PID. Cả chunk=1 và chunk=4 trả cùng dạng `(path, hard_fail)`, luân phiên thứ tự đo, median 3 lượt.

| Đường chạy | Median | Tăng tốc so với serial |
|---|---:|---:|
| Serial | 1,508 s | 1× |
| Warm pool, chunk=1 | 0,447 s | 3,38× |
| Warm pool, chunk=4 | 0,386 s | 3,90× |

Phép thử này chứng minh chunk không phải điều kiện bắt buộc để pool nhanh hơn trên mẫu này. Nó không xác định cấu hình tối ưu cho kho thực, không bao gồm thời gian startup vào throughput warm và không đánh giá latency UI.

## 4. Lộ trình nên sử dụng sau review

1. Giữ policy DPI trang 1 / OCR có text trong tối đa 5 trang đầu vì đó là yêu cầu hiện tại. Tách nó khỏi tuyên bố tăng tốc nhiều lần.
2. Đo riêng ba vấn đề: mở cây/quét nền quick; mở chi tiết full; mở lại kho.
3. Làm reuse trong phiên, bỏ auto-expand và giảm công việc click cũ; thử pool tồn tại lâu với chunk điều chỉnh theo workload nếu quick còn nghẽn.
4. Nâng thử nghiệm full không hash ảnh lên sớm, **kèm guard/fallback đúng và kiểm thử phản ví dụ**, rồi mới chọn làm mặc định.
5. Thêm cache lưu giữa phiên nếu workflow thường mở lại kho. Không gọi “tức thời” trước khi đo enumeration/stat/storage thực.
6. Viewer và TIFF context ưu tiên theo dữ liệu và profile; không mặc định mọi thay đổi đó là “rủi ro thấp”.

Không nhập nguyên văn đoạn “KHÔNG làm policy…” và “không 1 file/task” từ review vào prompt. Nên thay bằng:

> Giữ policy người dùng đã yêu cầu; đo lợi ích đổi phạm vi riêng với tối ưu kỹ thuật. Giữ process pool giữa các lượt, xác nhận worker đã warm, tune chunk theo corpus và yêu cầu tương tác. Xây fast-path filter có chứng minh coverage và fallback trước khi dùng mặc định; benchmark lại implementation an toàn, không dùng số đo prototype như cam kết.
