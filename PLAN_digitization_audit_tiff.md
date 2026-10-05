# Kế hoạch bổ sung Thẩm định số hóa cho file TIFF (+ rà soát thẩm định PDF)

> **Đính chính 05/10/2026:** Tên TIFF có 5 đoạn:
> `<MãĐĐ>-<Phông 3 số>-<Mục lục 2 số>-<ĐVBC 4 số, kèm ký tự số trùng nếu có>-<Trang 3 số>.tif`.
> Ví dụ `A38-011-07-0123-001.tif`, `A38-011-07-0123-002.tif`.
> Một tệp là một trang; số trang chạy liên tục trong hồ sơ, không theo số văn bản.
> Các đoạn cũ nói phông TIFF tùy ý hoặc ĐVBC chỉ có đúng 4 ký tự đã được thay thế.

> Trạng thái: **ĐÃ TRIỂN KHAI 03/10/2026** — core + UI + tests xong
> (`test_digitization_audit.py` 28/28, `test_digitization_audit_screen.py`
> 4/4 pass). Căn cứ: Hướng dẫn 40/HD-VPTW (07/11/2018) và
> Công văn 14748/CV-VPTW (06/5/2025) sửa đổi, bổ sung Hướng dẫn 40.
>
> Nguồn pháp lý đã đối chiếu: thuvienphapluat.vn (bị chặn bot, tra qua luatvietnam.vn
> bản full text Công văn 14748 và các nguồn dẫn chiếu). Đoạn trích nội dung CV 14748
> trong kế hoạch này lấy từ bản full text luatvietnam — trước khi code nên đối chiếu
> lại 1 lần với bản scan gốc TVPL người dùng đang có.
>
> **Cập nhật 03/10/2026 — người dùng đã chốt 2 yếu tố bổ sung cho thẩm định PDF:**
> (a) kiểm tra **"Đặt tên đúng"** mức **cảnh báo vàng**, dùng đúng quy ước cây
> CSDL_SOHOA (mục 5.3); (b) quy tắc **600 dpi cho khổ ≤ 70% A4**, mức cảnh báo
> (mục 5.2 — dùng hằng số 0,72 để bao trọn A5, chỉnh 1 số nếu muốn đúng 0,70).
>
> **Cập nhật 03/10/2026 (2) — chốt đủ 4 câu còn lại (mục 6):** màu bắt buộc —
> giữ nguyên hành vi PDF, áp dụng cả TIFF; nén lossless TIFF = cảnh báo vàng,
> chỉ nén lossy mới trượt; chỉ quét đuôi `.tif` chính xác (bỏ `.tiff`); tên TIFF
> "phông tùy ý" thống nhất với PDF. Kế hoạch sẵn sàng triển khai.
>
> **Cập nhật 04/10/2026 (3) — đảo 2 quyết định trước:**
> (a) `.tif` và `.tiff` là CÙNG định dạng TIFF → thẩm định nhận ngang hàng
> cả hai, không ép đuôi, không cảnh báo (mục 4.2, mục 6.3);
> (b) OCR hạ về **cảnh báo vàng**, chưa OCR không trượt (mục 5.1).

---

## 1. Yêu cầu kỹ thuật hiện hành (HD 40 sau khi sửa bởi CV 14748)

### 1.1. Lưu trữ — chế độ ảnh (Điểm b, Bước 2, Mục 2)

| Tiêu chí | Quy định |
|---|---|
| Định dạng | Quét đồng thời **2 định dạng: TIFF (.tif, bản 6.0+)** và **PDF hoặc PDF/A** |
| Số trang/tệp | **TIFF: đúng 1 trang/tệp** (multi-page TIFF không được chấp nhận). PDF: 1 hoặc nhiều trang |
| Độ phân giải | **≥ 300 dpi**; **600 dpi** khi khổ giấy nhỏ hơn A4 hoặc cỡ chữ dưới 8 |
| Màu sắc | Có màu/nền màu → ảnh màu **24 bit (8 bit/kênh), sRGB**; không màu → **8 bit thang xám** |
| Độ nén | **TIFF: không nén**; PDF/PDF/A: không nén hoặc nén không mất dữ liệu (LZW / JPEG 2000 lossless) |
| OCR | Tệp .pdf phải **có khả năng tìm kiếm bằng OCR tiếng Việt** (bắt buộc) |
| Tỉ lệ | 100% (kích thước gốc), 1:1 |
| Vai trò TIFF | .tif là bản "bảo hiểm" cho lưu trữ lâu dài |

### 1.2. Quy ước đặt tên (Điểm c, d — Bước 2, Mục 2)

CV 14748 **bổ sung mã định danh vào tên thư mục** (trước khi sửa, tên thư mục hồ sơ
chỉ gồm phông–mục lục–đơn vị bảo quản):

```
Thư mục hồ sơ:   <MãĐĐ>-<Phông 3 số>-<MụcLục 2 số>-<ĐVBC 4 số + ký tự trùng nếu có>  VD: A38-011-07-0123
Thư mục tài liệu:<MãĐĐ>-<Phông>-<MụcLục>-<ĐVBC>-<STT 3 số>             VD: A38-011-07-0123-001
Tệp .pdf:        <MãĐĐ>-<Phông>-<MụcLục>-<ĐVBC>-<STT 3 số>.pdf         VD: A38-011-07-0123-001.pdf
                 (có thể kèm siêu dữ liệu: …-001-BC-0001-1998.pdf)
Tệp .tif:        <MãĐĐ>-<Phông 3 số>-<MụcLục 2 số>-<ĐVBC 4 số + ký tự trùng nếu có>-<Trang 3 số>.tif
                 VD: A38-011-07-0123-001.tif, A38-011-07-0123-002.tif…
```

- Số thiếu ký tự phải pad 0 phía trước; dấu nối là `-` không có khoảng trắng.
- Khuyến khích (không bắt buộc) lưu tệp .pdf **cùng thư mục** với tệp .tif.
- Thư mục gốc ("thư mục bất kỳ") không bị ràng buộc — chỉ quy định cấu trúc bên trong.

---

## 2. Đánh giá thẩm định PDF hiện tại so với yêu cầu

Đối chiếu `scanindex/core/digitization_audit.py` + màn
`scanindex/ui/screens/digitization_audit_screen.py` với mục 1.

| # | Tiêu chí hiện hành | Hiện trạng tool | Kết luận |
|---|---|---|---|
| 1 | PDF/PDF/A, ≥300 dpi | `dpi_ok`, DPI_MIN=300, dung sai 1 dpi, tính đúng cả ảnh xoay/nghiêng | **ĐẠT** |
| 2 | 600 dpi khi khổ < A4 / chữ < 8 | Không kiểm tra | **THIẾU (P2)** — **đã chốt** phương án bổ sung: cảnh báo vàng khi khổ ≤ 70% A4 mà < 600 dpi (mục 5.2) |
| 3 | Màu 24bit sRGB / xám 8bit khi không màu | `color_ok` = có ≥1 ảnh màu; **100% xám = TRƯỢT**; không kiểm tra bit depth/sRGB | **CHỐT GIỮ NGUYÊN** — đơn vị yêu cầu bắt buộc số hóa màu (chặt hơn phần cho phép xám của CV 14748): 100% xám/đen-trắng tiếp tục TRƯỢT; chỉ bổ sung mô tả bit depth vào ghi chú |
| 4 | Nén không mất dữ liệu | Cho: không nén, LZW, Flate, CCITT G3/G4, RunLength, JPX lossless; chặn JPEG/DCT, JBIG2, JPX lossy | **ĐẠT** (danh sách cho phép rộng hơn văn bản 1 chút nhưng đều lossless — đúng tinh thần) |
| 5 | PDF phải có OCR tiếng Việt (bắt buộc theo CV 14748) | Có check `ocr_ok` + cảnh báo thiếu dấu tiếng Việt, thẻ vàng không vào verdict | **CHỐT GIỮ CẢNH BÁO VÀNG** — người dùng quyết định chưa OCR không trượt, dù văn bản viết là bắt buộc |
| 6 | Thống nhất dùng PDF/A | Chỉ mô tả trung tính "PDF/A-2B / không khai báo" | **ĐỦ (P3)** — PDF thường vẫn được chấp nhận; có thể thêm cảnh báo nhẹ khi chưa PDF/A |
| 7 | Quy ước tên file/thư mục | Màn thẩm định **không kiểm tra** tên; parse tên chỉ có ở `rename_tree.py` (màn thẩm định không gọi) | **THIẾU (P1)** — **đã chốt** phương án bổ sung: thẻ cảnh báo vàng "Đặt tên đúng" theo quy ước cây CSDL_SOHOA (mục 5.3) |
| 8 | TIFF | **Không có gì** — audit chỉ nhận `.pdf` (bộ lọc cứng ở screen :140, :869, :921) | **LÀM MỚI** — mục 4 |

Kết luận ngắn: **core thẩm định PDF đạt ~80%** — trụ chuẩn ở DPI + nén; 3 lỗ hổng
chính: (a) chính sách màu/xám lệch phần cho phép xám của CV 14748, (b) OCR chưa
là tiêu chí bắt buộc, (c) chưa kiểm tra quy ước tên file/thư mục. Toàn bộ đã có
quyết định chốt tại mục 5 và mục 6: (a) giữ nguyên theo chính sách "phải là số
hóa màu" của đơn vị, (b) (c) bổ sung theo mục 5.

---

## 3. Quy ước tên TIFF do người dùng đề xuất — đối chiếu

Đề xuất: `Thư mục bất kỳ/Phông-MụcLục-ĐVBC/Phông-MụcLục-ĐVBC-Trang.TIFF`

- ✅ Đúng **hình dạng** cấu trúc: 1 thư mục/hồ sơ (đơn vị bảo quản), 1 tệp/trang.
- ❌ **Thiếu MÃ ĐỊNH DANH** ở đầu: CV 14748 bổ sung mã định danh vào tên thư mục và
  tệp. Mẫu "Phông-MụcLục-ĐVBC" (không có mã định danh) là quy ước **trước khi sửa
  đổi** — hiện không còn đúng chuẩn.
- ❌ Đuôi theo văn bản là **`.tif`** (3 chữ cái, "TIFF bản 6.0+"), không phải `.TIFF`.
- ❌ Thiếu bề rộng số bắt buộc: Phông **3** số, Mục lục **2** số, ĐVBC **4** số,
  Trang **3** số.

**Mẫu đúng hiện hành:**

```
<Thư mục gốc bất kỳ>/A38-011-07-0123/A38-011-07-0123-001.tif
                                         A38-011-07-0123-002.tif
                                         A38-011-07-0123-003.tif …
```

(tùy chọn: kèm `A38-011-07-0123-001.pdf` cùng thư mục)

---

## 4. Thiết kế chức năng Thẩm định TIFF

### 4.1. Core — `scanindex/core/digitization_audit.py` (chỉ đọc, không sửa file)

**Dataclass mới `TiffAuditResult`** (giữ shape gần `PdfAuditResult` để UI tái dùng):

```python
@dataclass
class TiffAuditResult:
    path: str
    pages: int            # n_frames — TIFF hợp lệ phải == 1
    width_px: int
    height_px: int
    dpi_x: float          # tag 282/283 × ResolutionUnit (inch=1, cm=×2.54); 0 nếu thiếu tag
    dpi_y: float
    bpc: int              # BitsPerSample (tag 258)
    ncomp: int            # SamplesPerPixel (tag 277)
    photometric: str      # "RGB" | "grey" | "palette" | "CMYK" | "other" (tag 262)
    compression: str      # tên bộ nén từ tag 259 ("none", "LZW", "JPEG", "PackBits", …)
    has_icc: bool         # tag 34675 (tham khảo sRGB)
    color_ok: bool | None     # RGB 24 bit (8 bit/kênh) — xám/1-bit = trượt (màu bắt buộc, như PDF)
    compression_status: str       # "none" | "lossless" | "lossy" | "unknown" (tag 259)
    compression_note: str = ""
    dpi_ok: bool | None
    single_page_ok: bool | None   # n_frames == 1
    name_ok: bool | None          # khớp quy ước tên file + thư mục cha (cảnh báo vàng)
    name_note: str = ""
    sequence_warning: str = ""    # trang thiếu/trùng/không liên tiếp trong thư mục
    lowres_page_warning: str = "" # khổ ≤ ~70% A4 mà < 600 dpi → cảnh báo vàng (mục 5.2)
    file_size: int = 0
    error: str = ""
    quick: bool = False
```

**Hàm `audit_tiff(path, *, quick=False, …)`** — dùng **Pillow 12.1** (đã có sẵn
trong `requirements.txt`, không thêm phụ thuộc):

1. Mở `PIL.Image.open`; `getattr(im, "n_frames", 1)` → `pages`.
   `pages > 1` → `single_page_ok=False` (các tiêu chí còn lại vẫn đọc từ frame 0).
2. Đọc tag: `im.tag_v2` — 259 Compression, 282/283 X/YResolution, 296 ResolutionUnit,
   258 BitsPerSample, 277 SamplesPerPixel, 262 Photometric, 34675 ICC.
   Thiếu DPI tag → `dpi_ok=None` + ghi chú "thiếu thẻ DPI trong metadata".
3. Chốt tiêu chí:
   - `dpi_ok` = `min(dpi_x, dpi_y) >= DPI_MIN - 1.0` (tái dùng `DPI_MIN`, cùng
     dung sai với PDF); kèm cảnh báo vàng khổ nhỏ ≤ ~70% A4 mà < 600 dpi (mục 5.2).
   - `color_ok` = photometric RGB và bps == 8/kênh — **màu bắt buộc** (chốt mục 6,
     áp dụng cùng chính sách PDF): xám 8 bit và 1-bit đen trắng đều TRƯỢT;
     16-bit/CMYK/palette → "other" + ghi chú.
   - Nén (`compression_status`, chốt mục 6): "none" → xanh đạt; lossless
     (LZW/PackBits/Deflate/CCITT) → **cảnh báo vàng** "văn bản quy định TIFF
     không nén" — không trượt; lossy (JPEG-in-TIFF, JPEG 2000 9/7) → **trượt nặng**.
4. **Kiểm tra tên file + vị trí** (mục 4.2) và **tuần tự trang** trong thư mục.
5. **Dispatcher `audit_file(path, …)`**: `.pdf → audit_pdf`, `.tif/.tiff → audit_tiff`
   để worker chỉ gọi 1 chỗ. Text layer/OCR, ký số, PDF/A: **không áp dụng** với TIFF.

### 4.2. Validator tên file — `parse_tiff_name()` trong cùng module

```python
TIFF_NAME_RE   = r"^(?P<mdd>[^-]+)-(?P<phong>[0-9]{3})-(?P<ml>[0-9]{2})-(?P<dvbc>[0-9]{4}[A-Za-z]?)-(?P<trang>[0-9]{3})$"
FOLDER_NAME_RE = r"^(?P<mdd>[^-]+)-(?P<phong>[0-9]{3})-(?P<ml>[0-9]{2})-(?P<dvbc>[0-9]{4}[A-Za-z]?)$"
```

- `<mdd>` không chứa dấu "-"; `<phong>` 3 số, `<ml>` 2 số, `<dvbc>`
  4 số với một ký tự phân biệt số trùng nếu có (ví dụ `0123a`), `<trang>`
  3 số và bắt đầu từ 001. Số thiếu ký tự phải thêm 0 phía trước.
- Kiểm tra: (1) tên file khớp mẫu; (2) **tên thư mục cha khớp mẫu và 4 mã đầu
  trùng với 4 mã của file**.
- **Đuôi `.tif` / `.tiff`** (chốt lại 04/10/2026): hai đuôi là CÙNG định
  dạng TIFF — quét ngang hàng cả hai, badge "TIFF", chuỗi trang tính cả
  hai đuôi; không ép đuôi, không cảnh báo. `.TIF` hoa vẫn nhận (Windows
  không phân biệt hoa/thường trên đuôi).
- **Chính sách:** tên lệch quy ước → `name_ok=False`, **chỉ cảnh báo vàng** như
  PDF (mục 5.3) — không trượt, không setting ép.
- **Đoạn cuối:** khác PDF ở đoạn cuối là *trang* trong hồ sơ
  thay vì *STT tài liệu*.
- Tuần tự trang: nhóm file .tif theo thư mục cha, sort theo `trang`, báo
  "thiếu trang 005" / "trùng trang 003" / "không bắt đầu từ 001".

### 4.3. UI — `scanindex/ui/screens/digitization_audit_screen.py`

4 điểm cứng `.pdf`-only cần mở cho `.tif/.tiff`:

| Vị trí | Sửa |
|---|---|
| `_TreeAuditWorker` :140 — `endswith(".pdf")` | thêm `.tif/.tiff`, gọi `audit_file()`; quick-audit TIFF rẻ (chỉ header) |
| `_AuditWorker` :233 | gọi `audit_file()` thay `audit_pdf()` |
| `_make_file_item` :869 — badge "PDF"/"File khác" | thêm badge "TIFF" |
| `_show_pdf` :921 + viewer :988 | nhánh `.tif/.tiff` → preview ảnh qua `QImage` (Qt đọc TIFF sẵn) thay `PdfViewerWidget`; hiển thị full-res scale theo DPI |

Nội dung thẻ chỉ tiêu TIFF (thay thẻ OCR/ký số không áp dụng):

```
[Độ phân giải]  ≥ 300 dpi (metadata X/YResolution)   — nặng; khổ ≤ ~70% A4 mà < 600 dpi → thêm vàng
[Chế độ màu]    RGB 24 bit — xám/1-bit trượt (màu bắt buộc)   — nặng
[Độ nén]        Không nén = xanh · lossless = vàng · lossy = đỏ — lossy mới trượt
[Số trang/tệp]  Đúng 1 trang (n_frames == 1)          — nặng
[Tên quy ước]   <MãĐĐ>-<Phông 3 số>-<ML 2 số>-<ĐVBC 4 số + ký tự trùng>-<Trang 3 số>.tif, khớp thư mục cha, trang liên tiếp — cảnh báo vàng
```

Verdict nặng của TIFF = `dpi_ok AND color_ok AND single_page_ok AND nén không
lossy` (nén lossless và tên lệch quy ước chỉ tô vàng).

Cây thống kê `scan_tree_stats` (core :461, `entry.suffix == ".pdf"` :497): cộng thêm
đếm `.tif` (mỗi tệp = 1 trang) vào `total_docs`/`total_pages` và `dir_sizes` để số
trang hồ sơ phản ánh cả bản bảo hiểm TIFF — hoặc tách cột "trang TIFF" nếu muốn rõ
ràng hơn (đề xuất: đếm chung + chú thích).

### 4.4. i18n / settings / tests

- `scanindex/infra/ui_text_catalog.py` + `translations.py`: nhãn thẻ, badge, các
  chuỗi ghi chú TIFF.
- `config/digitization_audit_settings.json`: **không thêm setting mới** — sau khi
  chốt mục 6, mọi chính sách quyết theo văn bản/quyết định (nén, tên, đuôi, màu),
  giữ nguyên loader hiện có (chỉ `root`).
- `tests/test_digitization_audit.py`: dựng TIFF mẫu bằng Pillow —
  RGB 300dpi không nén (đạt); 150dpi trượt; multi-frame trượt; JPEG-in-TIFF
  trượt nặng; 1-bit và xám 8bit trượt (màu bắt buộc); LZW/PackBits chỉ vàng;
  tên đúng/sai/thiếu pad-0; thư mục lệch mã cha; chuỗi trang thiếu 005;
  khổ ≤ ~70% A4 mà 300dpi → vàng; `.tiff` quét ngang hàng `.tif`.
- `tests/test_digitization_audit_screen.py`: thêm 1 hồ sơ có TIFF đạt + 1 TIFF trượt
  vào cây smoke test.

### 4.5. Không đụng tới

`digitization/folder_scan.py` (pipeline số hóa chỉ nhận PDF) và `rename_tree.py`
(TIFF là "file khác đi theo thư mục cha") — **giữ nguyên**; chức năng thẩm định là
chỉ đọc, không luân chuyển TIFF trong pipeline OCR.

---

## 5. Sửa thẩm định PDF kèm theo (đóng gap mục 2)

> **Màu/xám (đã chốt, mục 6):** giữ nguyên logic `color_ok` hiện tại — phải có
> ảnh màu; 100% xám/đen-trắng = TRƯỢT nặng ("phải là số hóa màu"). Chỉ bổ sung
> mô tả độ sâu bit (`bpc` đã có trong `ImageInfo`) vào `color_summary` để thấy
> rõ "Thang xám 8 bit / 1 bit đen trắng". Không thêm setting.

### 5.1. OCR — giữ mức cảnh báo vàng (chốt lại 03/10/2026)

Dù CV 14748 viết "tệp .pdf phải có khả năng tìm kiếm bằng OCR tiếng Việt",
người dùng chốt: **chưa OCR KHÔNG làm trượt** — thẻ "Đã OCR" vàng như trước,
không vào chip tổng kết, không gắn dấu ❗ trên cây. (Đã hạ lại sau khi từng
nâng thử thành tiêu chí nặng — quyết định đơn vị overrides văn bản.)

### 5.2. Quy tắc 600 dpi cho khổ nhỏ — ĐÃ CHỐT (cảnh báo vàng, không trượt)

Yêu cầu người dùng: khổ giấy **nhỏ hơn từ 70% A4 trở xuống** mà quét dưới 600 dpi
thì phải nhắc; các khổ chỉ "bé hơn A4 một xíu" thì KHÔNG nhắc.

- **Đo khổ:** PDF — `page.rect` (point → mm nhân 25,4/72) của trang chứa ảnh;
  TIFF — pixel ÷ DPI metadata. Sau đó chuẩn hóa chiều (so cạnh-ngắn với
  cạnh-ngắn, cạnh-dài với cạnh-dài của A4 210×297 mm) lấy tỉ lệ
  `s = min(cạnh_ngắn/210, cạnh_dài/297)`.
- **Ngưỡng "khổ nhỏ":** yêu cầu gốc là ≤ 0,70 — nhưng **A5 = 70,7% của A4**
  (148/210 và 210/297), chốt đúng 0,70 sẽ **sót đúng khổ A5**, khổ nhỏ phổ biến
  nhất. Đề xuất hằng số `A4_SMALL_SCALE = 0.72` (đặt cạnh `DPI_MIN` trong
  `digitization_audit.py`): bao trọn A5, vẫn bỏ qua mọi khổ ≥ ~76% A4
  (19×28 cm, A4 cắt lề, Letter…) — đúng tinh thần "không bắt lỗi vì bé hơn một
  xíu". Hằng số để chỉnh nhanh nếu người dùng muốn đúng 0,70.
- **Hành vi:** trang có khổ `s ≤ A4_SMALL_SCALE` chứa ảnh có DPI < 600 (−1 dung
  sai, cùng cơ chế với DPI_MIN) → thêm cảnh báo vàng vào `warnings()`:
  "Trang khổ nhỏ (≈≤70% A4) quét dưới 600 dpi — quy định yêu cầu 600 dpi,
  cân nhắc quét lại". Không đổi chip Đạt/Không đạt, không gắn ❗ trên cây.
- **Giới hạn ghi rõ trong UI:** "cỡ chữ dưới 8" không đo được từ file — nhắc
  người kiểm tra đối chiếu bản gốc; máy scan cắt lề tự động có thể làm khổ đo
  được lệch nhỏ.
- Chỗ gắn PDF: trong vòng lặp trang của `audit_pdf()` (đã có `page.rect` + DPI
  từng ảnh); chạy cả quick lẫn full (rẻ). TIFF: field `lowres_page_warning` đã
  có sẵn trong `TiffAuditResult` (mục 4.1) — dùng cùng hằng số.

### 5.3. Yếu tố "Đặt tên đúng" — ĐÃ CHỐT (cảnh báo vàng, không trượt)

Quy ước **dùng đúng quy ước cây CSDL_SOHOA của app** (giống màn Đổi tên theo cây —
`rename_tree.py`), không ép mẫu thuần số của ví dụ CV 14748:

- **Tên file ≥ 5 đoạn:** `<MãĐĐ>-<MãPhông>-<MụcLục>-<Hồ sơ>-<STT>[-<gợi nhớ>].pdf`
  - MãĐĐ, MãPhông: **tùy ý** (chỉ cấm dấu "-"); MụcLục **2 chữ số**; Hồ sơ/đơn vị
    bảo quản **4 chữ số**; STT **3 chữ số** (thiếu thì pad 0); phần gợi nhớ sau
    STT tùy ý, giữ nguyên verbatim (`PdfName.extra` — ví dụ `…-001-BC-0001-1998.pdf`).
  - Tái dùng `rt.parse_pdf_name` (rename_tree.py:153) và thêm 3 phép kiểm bề rộng
    theo `LEVEL_WIDTH` / `PDF_STT_WIDTH` — parse cho phép độ rộng bất kỳ, audit
    kiểm thêm: MụcLục đúng 2 số, Hồ sơ đúng 4 số, STT đúng 3 số toàn chữ số.
- **Khớp thư mục cha:** 4 mã đầu của tên file phải trùng khớp tên thư mục hồ sơ
  chứa nó — `rt.parse_dossier_folder_name(tên thư mục cha)` rồi so 4 trường.
  Thư mục cha không parse được quy ước 4 đoạn → cảnh báo luôn ("thư mục chứa
  không đúng quy ước hồ sơ").
- **Dữ liệu kết quả:** thêm `name_ok: bool | None` + `name_note: str` vào
  `PdfAuditResult`; tính trong `audit_pdf()` (chỉ đọc path — rẻ, chạy cả quick).
- **UI:** thẻ vàng mới "Đặt tên đúng" (style thẻ nén/OCR hiện tại — warning,
  không vào verdict); chi tiết ghi cụ thể lỗi: không parse được 5 đoạn / đoạn nào
  sai bề rộng (kèm mẫu đúng `<MãĐĐ>-<Phông>-<ML 2 số>-<HS 4 số>-<STT 3 số>.pdf`)
  / lệch mã với thư mục cha. Chip "Đạt/Không đạt" và dấu ❗ trên cây KHÔNG đổi.
- **Ghi chú:** "phông tùy ý" vẫn bao quát cả mẫu CV 14748 (phông 3 số chỉ là
  trường hợp riêng); nếu sau này tổ chức ép thuần CV 14748 thì siết lại regex ở
  một chỗ duy nhất.

### 5.4. PDF/A — giữ nguyên mô tả trung tính

Không thêm cảnh báo ⚠ khi thiếu PDF/A: hành vi hiện tại (dòng "không khai báo
PDF/A" chữ xám trong hàng mô tả) là chủ đích của app — test màn hình chốt rõ
"thiếu PDF/A chỉ là mô tả trung tính, KHÔNG phải cảnh báo vàng". Khác với đề
xuất P3 ban đầu; giữ nguyên như cũ.

### 5.5. Dọn tàn dư

`config/digitization_audit_cache.json` (không còn code tham chiếu) — để lại không
gây hại, chỉ xóa khi tiện.

---

## 6. Các quyết định đã chốt (03/10/2026)

1. **Màu bắt buộc (PDF + TIFF):** "phải là số hóa màu" — giữ nguyên hành vi hiện
   tại của PDF (có ≥1 ảnh màu là đạt; 100% xám/đen-trắng = TRƯỢT nặng); TIFF áp
   cùng chính sách: photometric RGB 8 bit/kênh mới đạt, xám 8 bit và 1-bit đều
   trượt. Chặt hơn phần cho phép xám của CV 14748 — theo yêu cầu của đơn vị.
2. **Nén TIFF:** không nén = xanh; lossless (LZW/PackBits/Deflate/CCITT) = **vàng**
   "văn bản quy định TIFF không nén" — không trượt; lossy (JPEG-in-TIFF, JP2 9/7)
   = trượt nặng. Không cần setting.
3. **Đuôi `.tif`/`.tiff`** (chốt lại 04/10/2026): hai đuôi là CÙNG định dạng
   TIFF — nhận ngang hàng cả hai, không ép đuôi, không cảnh báo. Việc thống
   nhất một đuôi (nếu cần cho kho đồng bộ mẫu `*.tif`) là việc của công cụ
   đổi tên, không phải của thẩm định.
4. **Tên TIFF:** MãĐĐ, phông 3 số, mục lục 2 số, ĐVBC 4 số với ký tự số
   trùng nếu có, trang 3 số; lệch quy ước = cảnh báo vàng, không trượt.
5. **Trước đó đã chốt:** tên PDF cảnh báo vàng theo quy ước CSDL_SOHOA (5.3);
   600 dpi cảnh báo vàng cho khổ ≤ ~70% A4, hằng số `A4_SMALL_SCALE = 0.72` để
   bao trọn A5 (5.2 — chỉnh 1 số nếu muốn đúng 0,70); OCR giữ cảnh báo vàng,
   không vào chip (5.1 — hạ lại); PDF/A giữ trung tính (5.4).

---

## 7. Thứ tự thực hiện đề xuất

1. **Core thẩm định PDF** (`digitization_audit.py`): thẻ "Đặt tên đúng" cảnh báo
   vàng tái dùng `rename_tree` (mục 5.3) + cảnh báo 600 dpi khổ nhỏ (mục 5.2) +
   nâng OCR tiêu chí nặng (mục 5.1) + ghi chú PDF/A (mục 5.4) + tests tương ứng
   trong `tests/test_digitization_audit.py`.
2. **Core TIFF:** `audit_tiff` + `TiffAuditResult` + `parse_tiff_name` +
   `audit_file` dispatcher + tests core.
3. **UI** (`digitization_audit_screen.py`): mở 4 điểm lọc `.pdf`-only, thẻ chỉ
   tiêu TIFF, thẻ vàng "Đặt tên đúng" cho PDF, preview QImage, i18n.
4. Dọn tàn dư `config/digitization_audit_cache.json` (mục 5.5) — không cần
   setting mới.
5. Chạy toàn bộ `tests/test_digitization_audit*.py`, smoke UI, build portable.

**Trạng thái triển khai (03/10/2026):** mục 1–3, 5 và test đã xong —

- `scanindex/core/digitization_audit.py`: `_pdf_name_check` (tái dùng
  `rename_tree`), cảnh báo 600 dpi khổ ≤ ~70% A4 (`A4_SMALL_SCALE = 0.72`,
  `DPI_SMALL_PAGE = 600`) cho cả PDF lẫn TIFF, `TiffAuditResult` +
  `audit_tiff` (Pillow, suy bpc/spp cho bilevel từ `im.mode`) +
  `parse_tiff_name` + `_tiff_name_check` (khớp thư mục cha + chuỗi trang) +
  `audit_file`, `hard_fail()` trên 2 lớp kết quả, `scan_tree_stats` đếm .tif
  (1 tệp = 1 trang).
- `scanindex/ui/screens/digitization_audit_screen.py`: 6 thẻ (thêm "Đặt tên
  đúng" cho cả PDF/TIFF), nhánh TIFF
  (badge "TIFF", preview QImage, thẻ "Số trang/tệp", ẩn thẻ Ký số), worker
  quét `.pdf` + `.tif` qua `audit_file`, nhãn tổng "file không đạt"; OCR và
  tên lệch quy ước chỉ vàng, không vào chip.
- Tests: `test_digitization_audit.py` 28 pass (thêm 11: quy ước tên PDF,
  cảnh báo 600 dpi, 9 case TIFF); `test_digitization_audit_screen.py` 4 pass
  (thêm 1 smoke TIFF).
- Chưa làm: build portable (mục 5), dọn `config/digitization_audit_cache.json`
  (5.5 — vô hại).
