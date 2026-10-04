# Kế hoạch: nhập thư mục nhiều hồ sơ trong Số hóa lưu trữ

Bản giao việc cho Coder. Đây là kế hoạch triển khai, chưa phải mô tả tính năng đã hoàn thành.

## 1. Mục tiêu và các yêu cầu đã chốt

- Mở rộng chức năng chọn thư mục tại **Số hóa lưu trữ – Bước 2**.
- Nhận diện cấu trúc chuẩn, cho chọn các hồ sơ cần xử lý bằng giao diện dùng chung với **Đổi tên cây thư mục**.
- Popup phải có đầy đủ khả năng chỉnh cây như công cụ hiện tại: đổi tên, kéo thả thứ tự, di chuyển hợp lệ, đánh số lại, xem PDF và hoàn tác. Không làm một bản sao chỉ để xem/chọn.
- Thay đổi hoặc sửa lỗi trong thành phần cây dùng chung phải có hiệu lực ở cả công cụ độc lập và popup chọn hồ sơ.
- Nếu không có cấu trúc chuẩn, đề nghị gom PDF vào một hồ sơ; người dùng tự quyết định hồ sơ tự do hay theo mã qua hộp thoại cấu hình hiện có.
- Bước 2 hỗ trợ nhiều hồ sơ, sửa thông tin hồ sơ và KIE riêng.
- Giữ luồng ba bước: nguồn tách từ Bước 1 hoặc nhập thư mục tại Bước 2 → OCR/KIE tại Bước 2 → Bước 3 ký số → đưa vào Kho hoặc xuất ZIP.
- Không thêm nút xuất Kho/ZIP trực tiếp tại Bước 2. Không buộc các PDF đã tách sẵn phải quay lại Bước 1.
- Không kiểm tra hoặc loại bỏ tài liệu trùng nội dung bằng hash. Cùng một file được phép thuộc nhiều hồ sơ.
- Khi đưa vào Kho, dùng bản sao do Kho quản lý, không phụ thuộc đường dẫn PDF nguồn.

## 2. Hiện trạng cần dựa vào

| Khu vực | File hiện có | Vai trò |
|---|---|---|
| Công cụ cây | `scanindex/ui/screens/rename_tree_screen.py` | `RenameTreeScreen`, `_ReorderTree`, xem PDF, worker thao tác cây, thứ tự thủ công, undo |
| Logic cây | `scanindex/core/rename_tree.py` | Parse tên, kiểm tra mã, lập/thực thi kế hoạch đổi tên, di chuyển, đánh số và hoàn tác |
| Chọn thư mục/chạy/xuất | `scanindex/ui/main_window.py` | `_arc_browse_input`, `_arc_start_process`, `_arc_export_external`, `_arc_import_to_kho`, `_run_kho_import` |
| Phiên số hóa | `scanindex/core/digitization/session.py` | `ArchiveSession`, `IdentityCodes`; hiện một `session.identity` cho cả phiên |
| Vỏ ba bước | `scanindex/ui/digitization/container.py` | Điều hướng, chuyển dữ liệu giữa các bước |
| Bước 2 | `scanindex/ui/digitization/extraction_step.py` | `ArchiveStep2Kie`, danh sách PDF, viewer và form KIE |
| Thông tin hồ sơ | `scanindex/ui/dialogs/archive_session_dialog.py` | `DossierInfoDialog`, lựa chọn tự do/theo mã |
| OCR/KIE | `scanindex/core/digitization/runner.py` | `ArchiveRunner`, `FileSpec`, kết quả và sự kiện theo file |
| Bước 3 | `scanindex/ui/digitization/signing_step.py` | Ký số, đặt tên bản ký, chọn xuất ZIP/Kho |
| Kho | `scanindex/core/repository/importer.py` | Đồng bộ hồ sơ theo bốn mã, lưu PDF/JSON, metadata và chỉ mục |

Luồng chọn thư mục hiện tại quét đệ quy và gom mọi PDF vào một identity. Không thể chỉ thay danh sách hiển thị: phải sửa việc gắn hồ sơ trong phiên, pipeline, ký số, metadata và đầu ra.

## 3. Nhận diện thư mục chuẩn

### 3.1. Pattern

```text
[CSDL_SOHOA/]
└── <Mã định danh>/
    └── <Mã phông>/
        └── <Mã mục lục>/
            └── <Mã định danh>-<Mã phông>-<Mã mục lục>-<Mã hồ sơ>/
                └── <Mã định danh>-<Mã phông>-<Mã mục lục>-<Mã hồ sơ>-<STT>.pdf
```

- Tên/lớp ngoài `CSDL_SOHOA` không bắt buộc. Xác định gốc logic từ nội dung cây, không chỉ nhìn tên thư mục.
- Hỗ trợ chọn gốc chứa trực tiếp các thư mục mã định danh, hoặc thư mục bao ngoài có `CSDL_SOHOA`. Không tạo/di chuyển thư mục để chuẩn hóa gốc.
- Đối chiếu đầy đủ các mã ở thư mục cha, tên thư mục hồ sơ và tên PDF. Đúng số đoạn nhưng sai mã cha vẫn là lỗi.
- Giữ số 0 đầu mã. Nhận cả `.pdf` và `.PDF`.
- Dùng parser/validator của `core/rename_tree.py`; kiểm tra những khác biệt quy ước hiện có trước khi mở rộng. Parser hiện có hỗ trợ hậu tố tùy chọn sau STT: giữ tương thích, không làm công cụ cũ mất khả năng đọc dữ liệu này.
- Quét metadata đường dẫn trước, chưa OCR, chưa mở toàn bộ PDF để render. Không để UI bị treo với cây lớn.
- Mỗi lỗi phải có đường dẫn và lý do. File không phải PDF không đưa vào danh sách OCR.

### 3.2. Kết quả quét

Trả về danh sách hồ sơ, PDF thuộc từng hồ sơ, các mã, STT, gốc logic, cảnh báo và lỗi. Không dùng một cờ đúng/sai duy nhất làm mất thông tin từng nhánh.

Quy tắc đề xuất cho cây hỗn hợp:

- Nếu có hồ sơ hợp lệ: mở popup cây, chỉ rõ nhánh/PDF sai. Người dùng có thể sửa bằng công cụ cây rồi rà lại, hoặc chỉ chọn hồ sơ hợp lệ.
- Hồ sơ còn lỗi cấu trúc bên trong không được xác nhận để xử lý một phần một cách âm thầm.
- Không tự gom các file sai vào hồ sơ khác.
- Nếu không nhận diện được hồ sơ chuẩn nào: chuyển sang đề nghị tạo một hồ sơ theo mục 5.
- Nếu thư mục không có PDF: thông báo và dừng, không tạo hồ sơ rỗng.

## 4. Popup dùng chung toàn bộ giao diện Đổi tên cây thư mục

### 4.1. Tổ chức code

Tách phần editor của `RenameTreeScreen` thành widget dùng chung, tên gợi ý `RenameTreeEditor`. Tên/file mới có thể điều chỉnh theo cấu trúc dự án.

```text
RenameTreeEditor + logic core/rename_tree.py
├── RenameTreeScreen: màn hình công cụ độc lập
└── DossierSelectionDialog: popup chọn hồ sơ cho Bước 2
```

- `RenameTreeScreen` là vỏ dùng editor chung; popup cũng nhúng chính editor đó và bổ sung lựa chọn hồ sơ/footer.
- Không sao chép `_ReorderTree`, menu, worker đổi tên, drag/drop, viewer, quản lý thứ tự hoặc undo sang một implementation thứ hai.
- Thành phần chung sở hữu hành vi chỉnh cây; khác biệt giữa hai nơi được cấu hình bằng option/API rõ ràng.
- Tách lifecycle và settings của màn hình khỏi editor. Popup không tự tải root cũ hoặc ghi đè trạng thái root đang mở của công cụ độc lập.
- Mỗi instance có worker, viewer và undo riêng; nếu hai nơi cùng sửa một root, phối hợp khóa thao tác và refresh để không dùng snapshot cũ.
- Giữ các kiểm tra lỗi, xử lý file đang mở và giới hạn di chuyển hợp lệ của công cụ hiện tại.

API gợi ý: `set_root`, `set_selection_mode`, `selected_dossiers`, `ordered_documents`, `validate_selection`, `is_busy`, `request_cancel`; signal báo cây thay đổi, selection thay đổi và thao tác hoàn tất. Không ép dùng đúng tên nếu dự án có convention khác.

### 4.2. Các thao tác phải có ở popup

| Thao tác | Yêu cầu |
|---|---|
| Xem cây và PDF | Giữ layout cây trái, PDF phải; chọn PDF để xem trước |
| Đổi tên | Giữ F2, double-click, menu và dialog theo hành vi hiện có |
| Đổi mã ở cấp cha | Tên con được cập nhật bằng cùng logic cascade của công cụ |
| Kéo thả thứ tự | Hồ sơ/PDF giữ đúng thứ tự người dùng đặt |
| Di chuyển giữa nhánh | Giữ các kiểu di chuyển hiện được công cụ cho phép, gồm chuyển hồ sơ/PDF |
| Đánh số lại | Giữ lệnh và quy tắc hiện có, không viết bộ đánh số thứ hai |
| Hoàn tác | Giữ Ctrl+Z, đồng thời khôi phục đúng đường dẫn, thứ tự và trạng thái chọn |
| Làm mới/mở thư mục | Giữ các thao tác liên quan hiện có |

**Tác động lên nguồn:** theo yêu cầu “y hệt công cụ”, đổi tên/di chuyển/đánh số thực hiện trên thư mục nguồn như hiện tại. Kéo đổi thứ tự trong cùng cha chỉ đổi thứ tự hiển thị cho đến khi người dùng chủ động đánh số lại, đúng cơ chế đang có. Popup phải có dòng nhắc ngắn về việc sửa trực tiếp nguồn.

Đóng/Hủy popup là hủy chọn để OCR, không tự hoàn tác các thao tác đã áp dụng lên nguồn. Người dùng dùng Ctrl+Z để hoàn tác trước khi đóng. OCR/KIE và ký số sau đó vẫn làm việc trên đầu ra tạm của phiên, không ghi đè PDF nguồn.

### 4.3. Phần chọn hồ sơ bổ sung

- Checkbox ở cấp hồ sơ. Chọn nhánh cha áp dụng cho các hồ sơ bên dưới, có trạng thái chọn một phần.
- Có “Chọn tất cả”, “Bỏ chọn tất cả”, bộ đếm hồ sơ/PDF đã chọn, “Hủy” và “Đưa vào Bước 2”.
- Chọn dòng để xem hoặc kéo thả độc lập với đánh dấu checkbox. Không để multi-select phục vụ di chuyển làm thay đổi danh sách cần OCR.
- Chọn tất cả phải bao gồm hồ sơ chưa bung trên cây lazy-load. Không duyệt riêng các item hiện có trên màn hình.
- Không yêu cầu chọn từng PDF ở phiên bản này: chọn hồ sơ bao gồm các PDF hợp lệ của hồ sơ đó.
- Khi đổi tên/di chuyển/undo, giữ lựa chọn theo node ổn định hoặc remap theo kết quả thao tác. Không lưu lựa chọn chỉ bằng chuỗi đường dẫn cũ.
- Khi thao tác cây đang chạy, khóa nút xác nhận và phối hợp hủy/đóng với worker.
- Trước xác nhận, rà lại dữ liệu hiện tại và lấy danh sách theo thứ tự đã chỉnh. Sau khi chốt, không chạy lại `os.walk` rồi sort để thay thế kết quả người dùng vừa chọn.
- Không tự đánh số lại nguồn lúc xác nhận. Ghi nhận thứ tự thủ công cho phiên; xử lý STT hiển thị/xuất theo chính sách sắp xếp hiện có của Bước 2, trong phạm vi từng hồ sơ.

## 5. Không nhận diện được cấu trúc: tạo một hồ sơ

Thông báo đề xuất:

> Không tìm thấy cấu trúc thư mục Mã định danh / Phông / Mục lục / Hồ sơ theo chuẩn. Bạn có muốn gom toàn bộ PDF trong thư mục này và các thư mục con vào một hồ sơ không?

- Đồng ý → mở `DossierInfoDialog` hiện có.
- Tên hồ sơ điền sẵn bằng tên thư mục người dùng chọn, cho phép sửa.
- Giữ nguyên lựa chọn hồ sơ tự do hoặc theo mã như hiện tại; không ép `UNSTRUCT`.
- Mã hồ sơ có thể tự tạo theo cơ chế phù hợp với chế độ đã chọn; tên thư mục chỉ là tên gợi ý, không ép thành mã hồ sơ.
- Giữ validation theo từng chế độ; không ghi một mã không hợp lệ chỉ để hoàn tất nhập.
- Toàn bộ PDF con, ở mọi độ sâu, thuộc cùng hồ sơ. Không tạo phông/mục lục/hồ sơ dựa theo từng cấp con.
- PDF trùng basename ở các thư mục khác nhau vẫn là các tài liệu độc lập; giữ đường dẫn gốc trong dữ liệu để phân biệt.
- Hủy dialog thì không bắt đầu OCR và không làm mất phiên hiện tại.

## 6. Mô hình dữ liệu phiên nhiều hồ sơ

Mở rộng `ArchiveSession` với danh sách hồ sơ có thứ tự. Mỗi hồ sơ có ID ổn định, `IdentityCodes`, nguồn và danh sách tài liệu. Mỗi tài liệu có ID ổn định, liên kết hồ sơ, đường dẫn nguồn, thứ tự, metadata, đường dẫn OCR/canonical JSON/bản ký và trạng thái xử lý.

```text
ArchiveSession
  dossiers: [DossierState]

DossierState
  id, identity, source_directory, documents

DocumentState
  id, dossier_id, source_path, source_name, order
  metadata, ocr_path, canonical_path, signed_path, status
```

- ID nội bộ không phải basename, chỉ số dòng, hash nội dung hay bốn mã có thể chỉnh sửa.
- Có cơ chế resolve identity từ `dossier_id` cho mọi thao tác. Không dùng identity của hồ sơ đang focus để ký/xuất toàn bộ phiên.
- Phân tách file tạm theo hồ sơ/ID tài liệu, hoặc tên tạm duy nhất; PDF, JSON, ảnh và bản ký không đè nhau khi trùng tên nguồn.
- Rà các map/event/callback theo basename trong runner và `main_window.py`. Callback theo ID vẫn đúng sau khi đổi thứ tự hoặc chọn hồ sơ khác.
- Giữ tương thích luồng Bước 1 → Bước 2, mở ZIP và xử lý một hồ sơ. Có adapter cho API cũ nếu cần; không để `session.identity` trở thành nguồn dữ liệu thứ hai bị lệch.
- Không sửa schema của Kho chỉ vì phiên UI chuyển sang nhiều hồ sơ nếu dữ liệu hiện tại đã đáp ứng.

## 7. Giao diện và xử lý Bước 2

Panel trái chỉ có hai cấp:

```text
<Mã định danh>-<Phông>-<Mục lục>-<Hồ sơ>
    PDF thứ nhất
    PDF thứ hai
Hồ sơ khác
    PDF tiếp theo
```

- Hồ sơ theo mã: hiển thị đủ bốn mã; tên hồ sơ có thể hiện phụ/tooltip. Hồ sơ tự do: hiển thị tên người dùng đặt.
- Chọn hồ sơ → hiển thị/cấu hình thông tin hồ sơ bằng form/logic đang dùng, gồm các trường bổ sung hiện có.
- Chọn PDF → giữ PDF viewer và chỉnh KIE/bbox hiện tại.
- Lưu đúng phần đang sửa trước khi đổi lựa chọn; giữ cảnh báo thay đổi chưa lưu và không để KIE của PDF này ghi vào PDF khác.
- Sửa thông tin một hồ sơ chỉ ảnh hưởng tài liệu và tên đầu ra của hồ sơ đó.
- STT, số tờ/trang bắt đầu và tổng trang tính riêng từng hồ sơ. Không tiếp tục bộ đếm từ hồ sơ trước.
- Giữ thứ tự từ popup, hỗ trợ hành vi sắp xếp tài liệu hiện có trong phạm vi hồ sơ. Thứ tự xử lý pipeline có thể khác nhưng không được làm đổi thứ tự nghiệp vụ.
- Hiển thị tiến độ từng PDF, tổng hợp theo hồ sơ và toàn phiên; trạng thái lỗi/hủy rõ ràng.
- Dùng lại pipeline OCR/KIE. Không OCR những hồ sơ không được chọn.
- Chuẩn bị phiên mới xong mới thay phiên cũ, sau cơ chế kiểm tra chưa lưu hiện có. Đóng popup hoặc hủy cấu hình không xóa dữ liệu phiên cũ.

## 8. Bước 3: ký số trước khi chọn đầu ra

- `ArchiveContainer` chuyển đầy đủ hồ sơ/tài liệu từ Bước 2 sang Bước 3.
- Bước 3 hiển thị rõ hồ sơ của từng PDF, cho chọn phạm vi ký; dùng lại cấu hình chữ ký hiện có.
- Resolve tên PDF và identity theo từng tài liệu, không theo `session.identity` chung.
- Dùng ID để đối chiếu kết quả ký. Tách đường dẫn bản ký để không đè file của hồ sơ khác.
- Giữ các bước ký và xử lý lỗi hiện có; không tạo đường tắt xuất ở Bước 2. Trong luồng mới, tài liệu chưa ký thành công không được âm thầm thay bằng bản OCR để xuất như bản đã ký.
- Ký lỗi một phần: báo rõ file lỗi, cho thử lại hoặc chọn phạm vi hồ sơ đủ điều kiện. Không âm thầm xuất hồ sơ thiếu văn bản.
- Quay lại sửa nội dung PDF sau ký phải làm bản ký tương ứng hết hiệu lực trong phiên và yêu cầu ký lại. Chỉ đổi tên/metadata nằm ngoài PDF thì không tự suy ra chữ ký PDF đã hỏng; phải phân biệt đúng loại thay đổi.
- Sau xử lý ký tại Bước 3, người dùng chọn xuất ZIP hoặc đưa vào Kho. Không tự xuất ngay khi ký xong.

### 8.1. Xuất ZIP

- Một ZIP cho mỗi hồ sơ; đặt tên theo quy ước hiện tại.
- Mỗi ZIP chỉ có PDF cuối cùng và metadata/canonical sidecar của đúng hồ sơ đó, giữ cơ chế đóng gói đang có.
- Excel metadata, số thứ tự, số tờ/trang và số lượng văn bản phải khớp giao diện đã duyệt.
- Không gộp nhiều hồ sơ thành một ZIP có một bộ identity.
- Khi tên ZIP ở đích đã có, dùng cơ chế xử lý xung đột rõ ràng; không tự ghi đè.

### 8.2. Đưa vào Kho

- Chạy import theo từng hồ sơ; dùng khóa `(ma_dinh_danh, fonds, catalog, dossier_code)` để dùng lại hoặc tạo mới hồ sơ đích.
- Dùng PDF cuối cùng sau ký và canonical/KIE đã sửa đúng tài liệu. Sao chép vào Kho cùng dữ liệu cần thiết, không tham chiếu nguồn.
- Không so hash để quyết định bỏ qua/chặn nhập. Không xóa cột hash hoặc đổi định danh dữ liệu cũ chỉ vì tắt kiểm trùng; nếu importer còn cần hash nội bộ, nó không phải điều kiện loại tài liệu.
- Rà việc `skip_duplicates=False` hiện có: bảo đảm cùng nội dung ở cùng/khác hồ sơ không bị loại, và mọi file vật lý có đường dẫn riêng.
- Cùng tên hoặc STT trong cùng hồ sơ là xung đột định danh, không phải bằng chứng trùng nội dung. Cho chọn bỏ qua hoặc nhập thêm với tên/STT mới. Không cần chức năng thay thế file cũ trong phạm vi đầu tiên.
- Hồ sơ đã tồn tại: không ghi rỗng lên tên phông/mục lục hoặc tự ghi đè thông tin đã chỉnh trong Kho. Nếu metadata mới khác, giữ dữ liệu Kho và báo khác biệt; cập nhật có chủ ý qua chức năng sửa hồ sơ.
- Không có hash dedup đồng nghĩa nhập lại có thể tạo thêm tài liệu nếu người dùng chọn nhập thêm. Không hứa hẹn tự phát hiện file đã thay đổi nhưng giữ nguyên tên.
- Ghi trạng thái nhập theo ID của phiên để bấm lại sau lỗi không tự nhập lại những mục đã hoàn tất trong chính lượt đó. Đây là phục hồi thao tác, không phải kiểm trùng nội dung.
- Dùng cơ chế chỉ mục/outbox hiện có; báo cáo số hồ sơ, PDF thành công, bỏ qua, lỗi. Không xóa nguồn khi dọn thư mục tạm.

## 9. Thứ tự triển khai

1. **Tách editor cây dùng chung.** Chuyển công cụ độc lập sang dùng editor; xác nhận đổi tên, kéo thả, đánh số và undo vẫn hoạt động trước khi thêm popup.
2. **Bộ quét và popup lựa chọn.** Chuẩn hóa root, báo lỗi, checkbox, giữ selection sau chỉnh cây và trả về snapshot hồ sơ/thứ tự cuối cùng.
3. **Phiên nhiều hồ sơ.** Thêm ID/link hồ sơ, adapter luồng một hồ sơ, đường dẫn tạm riêng, callback theo ID.
4. **Bước 2.** Cây Hồ sơ → PDF, cấu hình hồ sơ, KIE theo file, OCR các hồ sơ đã chọn và gom một hồ sơ khi không chuẩn.
5. **Bước 3 và đầu ra.** Ký theo đúng identity; xuất từng ZIP hoặc import từng hồ sơ vào Kho; xử lý xung đột tên mà không dedup hash.
6. **Kiểm thử và hoàn thiện.** Kiểm tra luồng cũ, cây lớn, hủy/lỗi; cập nhật bản dịch và hướng dẫn sử dụng liên quan.

Không thêm logic nghiệp vụ dài vào `main_window.py`; giữ vai trò điều phối, đặt scanner/session/controller ở module phù hợp. Không sửa các thay đổi không liên quan đang có trong workspace.

## 10. Tiêu chí nghiệm thu và kiểm thử

| Nhóm | Tình huống bắt buộc | Kết quả mong đợi |
|---|---|---|
| Root | Có/không lớp `CSDL_SOHOA`, tên gốc khác | Nhận diện đúng cùng một cấu trúc logic |
| Pattern | Tên đúng nhưng mã cha không khớp; thiếu mã; `.PDF`; mã có số 0 đầu | Nhận diện hoặc báo lỗi chính xác, không tự đoán |
| Cây hỗn hợp | Có hồ sơ hợp lệ và nhánh sai | Hiển thị lỗi, không nhập nhầm/âm thầm bỏ file |
| Giao diện chung | Sửa hành vi editor chung | Cả công cụ và popup hưởng thay đổi, không có bản copy riêng |
| Chỉnh cây | Đổi tên cha/con, chuyển hồ sơ/PDF, đổi thứ tự, đánh số, Ctrl+Z | Kết quả và quy tắc giống công cụ hiện tại |
| Selection | Check rồi đổi tên/di chuyển/undo/làm mới | Không mất chọn hoặc chọn nhầm hồ sơ |
| Lazy tree | Chọn tất cả khi nhiều nhánh chưa bung | Chọn đủ các hồ sơ hợp lệ |
| Popup | Đóng khi có thao tác nguồn đã hoàn tất | Không OCR; giải thích được thao tác nguồn vẫn giữ, undo hoạt động trước khi đóng |
| Không chuẩn | PDF ở gốc và nhiều cấp, tên trùng | Gom đúng một hồ sơ, đủ PDF, không đè file |
| Cấu hình | Chọn hồ sơ tự do hoặc theo mã | Đúng lựa chọn người dùng; tên gợi ý là tên thư mục |
| Bước 2 | Hai hồ sơ, nhiều PDF trùng basename, đổi focus lúc OCR | KIE/progress lưu đúng file và hồ sơ |
| Thứ tự | Kéo thứ tự trong popup, chuyển Bước 2/3, xuất | Không bị sort lại; STT/trang xử lý riêng từng hồ sơ |
| Sửa thông tin | Sửa identity của một hồ sơ | Không đổi identity các hồ sơ khác |
| Ký số | Nhiều hồ sơ, ký lỗi một PDF, thử lại | Gắn đúng kết quả; không xuất âm thầm bản chưa ký |
| ZIP | Xuất hai hồ sơ và mở lại ZIP | Mỗi ZIP đúng identity, PDF, KIE và metadata của mình |
| Kho | Hồ sơ đã có; cùng hash ở nhiều hồ sơ; cùng tên khác nội dung | Dùng lại đúng hồ sơ, không dedup hash, không ghi đè file |
| Metadata Kho | Metadata nguồn trống/khác dữ liệu Kho | Không xóa hoặc ghi đè dữ liệu Kho ngoài ý muốn |
| Hủy/lỗi | Hủy ở chọn thư mục, OCR, ký hoặc import | Không mất phiên cũ; trạng thái phần đã làm rõ ràng; nguồn không bị xóa |
| Tương thích | Bước 1 → 2 → 3; nhập một thư mục; mở ZIP; công cụ cây độc lập | Các luồng hiện tại vẫn dùng được |

Tái sử dụng và chạy các test liên quan, đặc biệt `tests/test_rename_tree.py`, `tests/test_archive_runner_ocr_reuse.py`, `tests/test_archive_step1_json_only.py`, `tests/test_kie_archive_viewer_editing.py` và các test Kho/ký/xuất hiện có. Bổ sung test cho hành vi mới; kiểm tra UI thực tế các thao tác checkbox, drag/drop, undo, chuyển hồ sơ và preview PDF.

## 11. Sản phẩm Coder cần bàn giao

- Code dùng chung editor cây và popup chọn hồ sơ, không nhân bản hành vi chỉnh cây.
- Luồng nhập chuẩn/không chuẩn hoàn chỉnh qua Bước 2 và Bước 3.
- Ký số, ZIP và Kho hỗ trợ nhiều hồ sơ, không lẫn dữ liệu hoặc ghi đè do tên trùng.
- Test mới, kết quả chạy test liên quan và ghi nhận kiểm tra UI thực tế.
- Ghi rõ mọi giới hạn chưa xử lý; không báo hoàn tất nếu mới làm UI mà ký/xuất vẫn dùng một identity chung.
