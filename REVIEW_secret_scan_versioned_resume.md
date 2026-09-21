# Review kế hoạch resume khi khác version

Review `PLAN_secret_scan_versioned_resume.md` và code hiện có ngày 2026-09-21. Đây là review thiết kế; chưa triển khai tính năng.

**Kết luận: đúng mục tiêu, nhưng cần sửa thiết kế lưu trạng thái trước khi giao coder triển khai.** Ba lựa chọn là yêu cầu đã được người dùng xác nhận. Việc chấp nhận kết quả cũ ở lựa chọn 2/3 không còn là câu hỏi xin phê duyệt.

## 1. Hành vi cần chốt

Giả định 500.000 file hỗ trợ, 300.000 file đã xử lý thành công, 200 file mật duy nhất nằm trong nhóm đã xử lý, dữ liệu đầu vào không thay đổi:

| Lựa chọn | Khối lượng phải xử lý bằng bộ quét | Kết quả cũ | Lưu lịch sử |
|---|---|---|---|
| 1. Quét lại từ đầu | 500.000 file | Không dùng làm kết quả của lượt mới | Ghi kết quả mới vào journal/registry |
| 2. Quét tiếp | 200.000 file còn lại | Giữ kết luận của 300.000 file, gồm các dòng của 200 file mật | Chấp nhận dùng ở version mới và lưu bền vững vào cả journal/registry |
| 3. Quét tiếp + quét lại 200 file mật cũ | 200 file mật cũ + 200.000 file còn lại | Giữ nhóm đã xử lý; thay toàn bộ dòng của từng file mật sau khi quét lại thành công | Như lựa chọn 2, cộng trạng thái quét lại còn dở và kết quả thay thế |

Không tính 200 file mật thành 200 dòng: một file có thể có nhiều trang/dấu mật. Những file không thể xử lý hoặc phát sinh lỗi phải có trạng thái riêng, không tính là quét lại thành công.

Hủy, Esc hoặc đóng hộp thoại: không chạy worker, không reset bảng, không tạo/ghi đè journal. Đây là thao tác thoát, không phải lựa chọn nghiệp vụ thứ tư.

## 2. Các phát hiện cần sửa

### R1 — [P1] Hàng đợi quét lại còn dở phải được lưu bền vững

**Vị trí kế hoạch:** mục 3.3 và mục 4, đặc biệt dòng 113.

Kế hoạch đã chỉ ra đúng lỗi nhưng để phương án xử lý mở. Nếu stamp version mới rồi dừng sau 50/200 file mật, lần sau version khớp và `_done` chứa cả 150 file còn lại. Các file này không được quét lại nữa; mục tiêu hoàn thiện dữ liệu Excel thất bại.

**Sửa bắt buộc:** chọn phương án lưu `rescan_pending` trong journal. Ghi và flush ý định quét lại trước khi có thể coi việc nâng version là hoàn tất. Mỗi file chỉ được bỏ khỏi tập này khi kết quả thay thế được commit thành công. `load()` và `_compact()` phải giữ tập còn dở. Resume cùng version vẫn tiếp tục tập này; không phụ thuộc việc có hiện hộp thoại ba lựa chọn hay không.

File lỗi/mất trên đĩa giữ kết quả cũ và trạng thái chưa cập nhật; không biến thành kết quả mới thành công. Luồng kết thúc phải kiểm tra cả migration chưa xong và `rescan_pending`, không chỉ biến đếm lỗi/hủy của lượt hiện tại. Code hiện tại ở `_run_worker` xóa journal khi không có lỗi/hủy trong lượt, nên chỉ thêm event `rs` là chưa đủ.

### R2 — [P1] `mc` rồi nhiều `m` không bảo đảm thay thế nguyên vẹn khi crash

**Vị trí kế hoạch:** mục 3.4–3.5, dòng 80–94.

`state_lock` chỉ ngăn các thread ghi xen nhau. Nếu mất điện sau dòng `mc` nhưng trước khi ghi đủ các dòng `m`, replay sẽ xóa kết quả cũ và chỉ giữ một phần hoặc không có kết quả mới. `record_file(..., "ok")` ghi trước đó càng dễ khiến file bị bỏ qua khi mở lại.

**Sửa bắt buộc:** dùng một bản ghi commit đầy đủ theo file, ví dụ `{"t":"replace_file","p":rel,"s":"ok","matches":[...]}`, hoặc transaction có begin/commit mà replay bỏ qua transaction chưa hoàn chỉnh. Replay cùng một commit phải đồng thời cập nhật kết quả, trạng thái file và hoàn tất phần việc quét lại của file đó. Dòng cuối bị cắt dở phải giữ được kết quả cũ và nhiệm vụ còn chờ. Áp dụng cùng quy tắc khi compact.

Chỉ commit sau khi bộ quét trả kết quả thành công. Danh sách rỗng hợp lệ có nghĩa là không còn phát hiện mật, khi đó mới xóa các dòng cũ. Hủy, lỗi và `_ScanSkip` không được giả làm kết quả rỗng thành công.

### R3 — [P1] `bump_versions()` theo mô tả sẽ không lưu version mới

**Vị trí kế hoạch:** mục 3.3, dòng 72–73.

`FileRegistry.save()` hiện chỉ flush/fsync các dòng đã append; chỉ compact khi số dòng mới vượt ngưỡng. Sửa `_entries` rồi gọi `save()` không tạo dòng mới, không ép compact, vì vậy version mới chỉ tồn tại trong RAM. Đã tái hiện: record version `old` → reload → đổi tuple thành `new` → save/close → reload vẫn là `old`.

**Sửa bắt buộc:** `bump_versions` phải append bản ghi cập nhật, hoặc gọi một API rewrite snapshot tường minh. Nếu rewrite, ghi file tạm, flush/fsync trước khi replace và chỉ công bố thành công sau khi lưu thành công. Test phải đóng rồi mở registry mới để kiểm tra, không chỉ đọc lại object vừa sửa.

Hai file journal/registry không phải một transaction chung. Cần lưu ý định migration và khả năng chạy lại an toàn: ghi ý định → cập nhật registry bền vững → xác nhận migration trong journal. Crash giữa các bước phải tiếp tục được. Không để `av` đã khớp khiến lần sau bỏ qua một registry chưa nâng xong. Registry ghi thất bại phải được báo và giữ việc sửa lịch sử còn dở.

### R4 — [P1] Entry registry bị thiếu và checkbox lịch sử đang tắt chưa được xử lý

**Vị trí kế hoạch:** mục 3.3, dòng 72; đối chiếu `_run_worker` dòng 2629–2630.

Code hiện tại không load/ghi registry khi `history_checkbox` tắt. Một lượt quét cũ vẫn có journal đủ `_done` nhưng không có registry tương ứng. Registry cũng có thể bị mất/hỏng. Do đó thiếu entry không chỉ xảy ra với file rác. Quy tắc “entry không tồn tại → no-op” không đáp ứng yêu cầu lưu lại 300.000 file vào cả hai nơi: sau khi journal bị xóa lúc hoàn tất, lần quét sau sẽ xử lý lại các file thiếu entry.

**Sửa bắt buộc:** tách việc đọc cache để bỏ qua OCR khỏi việc lưu lịch sử được người dùng yêu cầu ở lựa chọn 2/3. Chốt cách tạo entry kế thừa cho file `ok` thiếu registry, dùng kết quả theo file từ journal; file `skip` cần trạng thái skip riêng, không giả thành “đã quét và không mật”.

Journal cũ không có size/mtime từng file. Nếu lấy stat hiện tại để lập entry kế thừa, phải hiểu đây là mốc người dùng chấp nhận kế thừa, không phải bằng chứng file chưa đổi từ lần quét cũ. Lưu nguồn gốc kế thừa/thiếu fingerprint cũ; lỗi stat không được bỏ qua im lặng rồi báo migration đầy đủ. Không cần thêm hộp thoại xin phép vì người dùng đã chọn kế thừa, nhưng hợp đồng dữ liệu phải rõ.

### R5 — [P1] Quét từ đầu phải bỏ qua cache; nhánh quét lại phải đi trước cache lookup

**Vị trí kế hoạch:** dòng 16, mục 3.2 và mục 3.4.

Trả `None` chỉ tạo journal mới. Worker hiện vẫn lookup registry nếu checkbox lịch sử bật. Sau một lần chọn 2 đã đóng dấu mới, người dùng chọn 1 có thể nhận lại chính kết quả cũ được chấp nhận, thay vì chạy bộ quét mới trên toàn bộ file. Ngay lần nâng version đầu tiên cũng có thể có cache cùng version từ một thư mục con đã quét.

**Sửa bắt buộc:** truyền chế độ rõ ràng như `RESTART / RESUME / RESUME_RESCAN_SECRET / CANCEL`, cùng chính sách đọc cache. `RESTART` bắt buộc bỏ qua cache lookup cho toàn lượt nhưng vẫn có thể ghi cache mới; không cần xóa registry toàn cục. `replace=True` cũng bắt buộc bỏ qua lookup, kể cả entry vừa được stamp version mới. Không trả `None` cho cả hủy và quét lại từ đầu.

### R6 — [P1] Lấy file quét lại bằng `relative_path` cũ có thể nhắm sai file

**Vị trí kế hoạch:** mục 3.4–3.5, dòng 77 và 93.

Registry dùng chung giữa thư mục cha/con, nhưng nhánh cache hit hiện lấy nguyên match dict. Ví dụ quét `D:\a\b`, registry lưu `source_path=D:\a\b\x.pdf`, `relative_path=x.pdf`. Sau đó quét `D:\a` và dùng cache: journal có done key `b\x.pdf`, nhưng match vẫn là `x.pdf`. Nối root mới với relative path cũ sẽ quét `D:\a\x.pdf`: sai file hoặc file không tồn tại. Xóa matches bằng rel mới cũng có thể không xóa các dòng cũ.

**Sửa bắt buộc:** định danh file bằng đường dẫn tuyệt đối chuẩn hóa từ `source_path`, kiểm tra nằm trong root của phiên, rồi tính lại relative path theo root hiện tại. Áp dụng khi đọc journal cũ, lấy cache, đếm N, lập hàng đợi, thay dòng và ghi journal mới. Nhóm theo file chuẩn hóa, không theo chuỗi relative path kế thừa.

Đảm bảo mỗi file chỉ có một task trong lượt: một file mật đang có trạng thái lỗi có thể nằm cả trong danh sách quét lại lẫn `pending`. Không nối hai danh sách mà không loại trùng. Nếu yêu cầu là hoàn tất nhóm quét lại trước, cần hai pha có điểm chờ; chỉ đặt trước danh sách trong cửa sổ 32 future không bảo đảm mọi task quét lại đã kết thúc trước khi task thường bắt đầu.

### R7 — [P2] “Bản cũ bỏ qua event mới” không phải tương thích an toàn

**Vị trí kế hoạch:** mục 3.1, 3.5 và 5, dòng 57, 95, 117.

Bản cũ bỏ qua `mc` nên có thể khôi phục kết quả đã được kết luận không mật; bỏ qua `rs` nên mất công việc còn dở. Khi compact, bản cũ viết lại chỉ header/f/m và loại bỏ luôn các event mới. Khi chạy xong, bản cũ còn có thể xóa journal. Đây không chỉ là trùng hàng tạm thời. Đã kiểm chứng bản đọc hiện tại compact làm mất `rs`.

**Sửa:** yêu cầu bản mới đọc được dữ liệu cũ; không tuyên bố bản cũ được phép sửa journal mới an toàn. Chốt chính sách downgrade và bảo vệ dữ liệu mới bằng định dạng/đường dẫn riêng nếu vẫn hỗ trợ chạy bản cũ. Chỉ bump `STATE_VERSION` chưa đủ để bảo vệ: loader cũ trả `None` cho version lạ, và caller có thể tạo lượt mới ghi đè cùng đường dẫn.

### R8 — [P2] Chưa phân biệt dữ liệu kế thừa, cập nhật thành công và cập nhật thất bại

**Vị trí kế hoạch:** mục 3.3–3.4 và 3.6.

Đổi app version không tạo ra `issue_year/declass_years/declass_due`. Với lựa chọn 2, thiếu dữ liệu mới là kết quả được chấp nhận. Với lựa chọn 3, file lỗi/mất vẫn giữ dòng cũ. Nếu chỉ để ô trống và thông báo hoàn tất, người dùng không biết đâu là “đã quét mới nhưng không tìm được năm” và đâu là “chưa cập nhật”.

**Sửa:** lưu trạng thái kế thừa/cập nhật và hiển thị thống kê rõ, ví dụ “Đã cập nhật 198/200; 2 file chưa cập nhật, đang giữ kết quả cũ”. Excel cần giữ được thông tin chưa cập nhật, qua Ghi chú hoặc trường trạng thái phù hợp. Không gán số 0/False mặc định thành kết luận nghiệp vụ chắc chắn. Version được phép tái dùng và version thật sự tạo kết quả nên được phân biệt bằng metadata nguồn gốc.

Không cần bắt buộc thêm cột trên bảng app để hoàn thành yêu cầu resume. Việc thêm cột Excel là phần mở rộng có thể làm riêng; ưu tiên cấu trúc kết quả và khả năng phục hồi trước. Nếu làm cột Excel, kiểm tra cả Excel cũ → nạp → xuất mới để các dòng nhận biết qua ghi chú không bị biểu diễn mâu thuẫn.

## 3. Các chỉnh sửa nhỏ và giới hạn cần nói đúng

- Tên journal thực tế là `secret_scan_{hash}.jsonl`; hash gồm folder chuẩn hóa và mode. Mục 2.1 đang ghi sai tên `secret_scan_{fast|thorough}.jsonl`.
- `done_files()` chỉ chứa đường dẫn, `_split_pending()` không kiểm tra size/mtime. Vì vậy câu “file đổi nội dung thì tự quét lại” chưa đúng với file đã done khi resume lựa chọn 2. Hoặc bổ sung kiểm tra fingerprint khi có dữ liệu, hoặc mô tả rõ việc phát hiện thay đổi chỉ áp dụng ở đường đi qua registry; không cam kết cho journal cũ thiếu fingerprint.
- `bump_versions` nhận tập key chính xác từ phiên, không duyệt cả prefix thư mục. Cách này tránh đụng file chưa quét, mode khác và thư mục anh em.
- Giữ cả trạng thái quét lại thất bại cho `_ScanSkip`, `_ScanCancelled`, lỗi OCR và OSError. Hiện worker kiểm tra file mất bằng `_ScanSkip`, nên chỉ xử lý OSError sẽ không bao phủ trường hợp đã mô tả.
- Tiến độ lượt hiện tại nên bắt đầu từ 0 với tổng 200.000 hoặc 200.200; số 300.000 kế thừa hiển thị riêng. Nếu giữ tiến độ toàn thư mục thì phải định nghĩa riêng tiến độ quét lại để không đếm một file hai lần hoặc vượt 100%.
- `_offer_resume()` đang gọi `prune_stale()` trước khi load và có thể xóa journal sau 30 ngày. Với nhu cầu giữ tiến độ thư mục rất lớn, nên bảo vệ journal đang được chọn/đang có việc còn dở; nếu giữ thời hạn này thì phải đưa vào điều kiện sử dụng, không hứa resume vô thời hạn.
- Không có cơ sở đo đạc cho “rewrite vài giây” với 300.000–500.000 entry. Đo thời gian/RAM bằng dữ liệu giả lập, giữ UI phản hồi và có trạng thái đang nâng lịch sử. Không chạy thử OCR thật 500.000 file để kiểm chứng migration.
- Tổng các dòng effort trong bảng là khoảng 15,5 giờ, chưa tính đầy đủ phục hồi crash/migration và kiểm thử quy mô lớn. “~1 ngày công” không khớp chính bảng ước lượng.

## 4. Trình tự lưu trạng thái đề nghị

Đây là hợp đồng hành vi; coder có thể chọn tên event/API khác:

1. Đọc lựa chọn và snapshot cấu hình trên UI thread. Hủy thì return ngay.
2. Chuẩn hóa định danh file, dựng tập kế thừa và tập quét lại duy nhất. Khôi phục cả phần việc migration/quét lại còn dở từ phiên trước.
3. Lưu bền vững ý định migration, version đích, lựa chọn và tập quét lại. Không dựa vào biến RAM để khôi phục sau crash.
4. Cập nhật registry bằng thao tác có thể chạy lại; giữ nguồn gốc kết quả cũ. Xác nhận migration journal sau khi registry đã lưu thành công. Đóng dấu mới không đồng nghĩa tập quét lại đã xong.
5. Nếu chọn 3, quét lại nhóm đã chỉ định bằng OCR/bộ quét thật. Commit từng file nguyên vẹn vào journal; cập nhật registry và theo dõi sửa đồng bộ nếu ghi registry chưa thành công. Phát signal thay dòng sau khi kết quả đã được chấp nhận, không phát thêm dòng ở cả nhánh thường và nhánh replace.
6. Xử lý phần pending còn lại, mỗi file tối đa một task. Hủy giữa chừng giữ nguyên phần chưa commit.
7. Khi kết thúc, chỉ xóa journal khi mọi việc cần phục hồi đã hoàn tất và kết quả cần giữ đã có trong registry. Nếu còn lỗi/quét lại/migration dở, giữ journal và báo đúng trạng thái.

## 5. Tiêu chí nghiệm thu bổ sung

1. Mock bộ quét và kiểm tra lựa chọn 1/2/3 lần lượt xử lý 500.000 / 200.000 / 200.200 file theo kịch bản không thay đổi đầu vào. Có cache cùng version cũng không giảm số lần gọi bộ quét ở lựa chọn 1 hoặc nhóm forced rescan.
2. Đóng/mở object và mô phỏng khởi động app lại: mọi entry kế thừa cần tái dùng thật sự mang version được chấp nhận mới; matches không bị mất. Bao phủ cả registry thiếu entry và checkbox lịch sử tắt.
3. Dừng lựa chọn 3 sau 50/200 file mật: resume cùng version chỉ quét lại 150 file còn dở, tiếp tục pending; không quét lại 50 file đã commit, không bỏ sót 150 file.
4. Fault injection trước/sau từng mốc lưu ý định, nâng registry, xác nhận version và commit kết quả file. Bao gồm đuôi JSONL bị cắt giữa chừng, ghi file tạm lỗi và compact. Luôn giữ kết quả cũ hoặc kết quả mới đầy đủ, không giữ một nửa kết quả.
5. Sau quét lại một file: kết quả có nhiều dòng thì thay đủ; kết quả rỗng thì xóa toàn bộ dòng cũ; lỗi/hủy/skip thì giữ kết quả cũ và trạng thái chưa cập nhật. Resume/compact không làm hồi sinh false positive hoặc thêm trùng.
6. Một file vừa có match cũ vừa đang `err`: chỉ nhận một task quét lại. Task thường không chạy song song lần hai trên cùng file.
7. Quét thư mục con → dùng cache khi quét thư mục cha → dừng → nâng version → chọn 3: quét đúng source path, loại đúng dòng cũ, tính N theo file duy nhất.
8. Hủy/Esc/đóng hộp thoại không thay đổi dữ liệu, bảng hoặc khởi động worker. Version khớp nhưng có `rescan_pending` vẫn tiếp tục được.
9. File mất, không stat được, file đổi nội dung, không nhận diện được năm và file đã đánh dấu “Không phải mật”: thống kê/trạng thái/Excel nhất quán; không báo dữ liệu đã cập nhật đầy đủ nếu còn file lỗi.
10. Registry khác folder/mode giữ nguyên. Nhóm `skip` không biến thành kết quả OCR không mật. Hành vi khi thiếu fingerprint của journal cũ được test đúng hợp đồng kế thừa.
11. Benchmark migration và replay trên 300.000–500.000 entry giả lập; không OCR; kiểm tra bộ nhớ, thời gian và cửa sổ submit hữu hạn.
12. Chạy các test progress/registry, worker/resume, thao tác danh sách và Excel liên quan; sau đó regression suite hiện có. Không khóa kế hoạch vào con số 227 test vì code hiện tại có thể đã thay đổi.

## 6. Phạm vi đã xác minh trong review

Đã đọc kế hoạch, `secret_scan_progress.py`, các nhánh resume/worker/cache/Excel/thay dòng trong `secret_file_scan_screen.py` và cách lấy app version. Đã chạy hai probe độc lập trên thư mục tạm: registry không lưu thay đổi chỉ ở RAM khi gọi `save()`, và reader cũ compact làm mất event `rs`/không áp dụng `mc`.

Chưa triển khai API/event đề xuất, chưa chạy OCR hoặc toàn bộ test suite. Không sửa source code hay kế hoạch gốc trong lần review này.
