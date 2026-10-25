"""Thẩm định chất lượng số hóa PDF/TIFF — chỉ đọc cấu trúc file, không sửa.

PDF (qua PyMuPDF): DPI ảnh (quy định ≥ 300, trượt khi dưới 297 — dung sai
DPI_TOLERANCE; khổ ≤ ~70% A4 còn phải ≥ 600 dpi — chỉ
cảnh báo), chế độ màu (chỉ cần CÓ ảnh màu — RGB/CMYK — là đạt; 100% ảnh
đen-trắng/xám mới trượt), độ nén (không nén hoặc lossless — JPEG/DCT,
JBIG2, JPEG 2000 lossy 9/7 là trượt), lớp text OCR (trang scan phải có
text, cảnh báo khi text gần như không có dấu tiếng Việt) và metadata
PDF/A. Tên file kiểm tra quy ước hồ sơ ``<MãĐĐ>-<Phông>-<ML 2 số>-<HS
4 số>-<STT 3 số>.pdf`` khớp thư mục cha (chỉ cảnh báo — xem
``_pdf_name_check``).

TIFF (qua Pillow — ``audit_tiff``): bản bảo hiểm lưu trữ 1 tệp = 1 trang,
không nén (nén lossless chỉ cảnh báo, lossy trượt), DPI/màu như PDF và
quy ước tên trang ``<MãĐĐ>-<Phông>-<Mục lục>-<ĐVBC>-<Trang>.tif``
khớp thư mục hồ sơ + chuỗi trang
liên tục (chỉ cảnh báo). ``audit_file`` chọn hàm theo đuôi file.

``scan_tree_stats`` đếm tệp/trang (PDF + TIFF) theo từng thư mục
trong cây CSDL_SOHOA (cấp thư mục xem
``scanindex.core.rename_tree.level_of``).
"""
from __future__ import annotations

import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from scanindex.core import rename_tree as rt

# --------------------------------------------------------------------------- #
# Ngưỡng chuẩn
# --------------------------------------------------------------------------- #

DPI_MIN = 300
# Dung sai DPI: dưới 297 mới trượt — máy quét nạp giấy lệch ±1 dpi mỗi trục
# (ảnh 2486px trải trên khổ ~595pt ra 298,99…) nên giữ 3 để không trượt đỏ
# vì sai số thiết bị.
DPI_TOLERANCE = 3.0
# Quy định 600 dpi cho khổ nhỏ hơn A4: "khổ nhỏ" = tỉ lệ cạnh ≤ ~70% A4
# (chọn 0,72 để bao trọn A5 — A5/A4 = 70,7% — mà vẫn bỏ qua khổ chỉ "bé
# hơn A4 một xíu"). Chỉ CẢNH BÁO, không trượt: máy quét cắt lề làm khổ đo
# được lệch, còn "cỡ chữ dưới 8" thì file không cho biết.
DPI_SMALL_PAGE = 600
A4_SMALL_SCALE = 0.72
A4_MM = (210.0, 297.0)

# Số ký tự text tối thiểu trên một trang có ảnh scan để tính là "có lớp text".
OCR_MIN_CHARS = 10
# Tỉ lệ dấu tiếng Việt / chữ cái dưới ngưỡng này (khi text đủ dài) → cảnh báo.
VN_DIACRITIC_RATIO_WARN = 0.01

# Phạm vi thẩm định theo từng tiêu chí. Legacy xét mọi trang như trước;
# v1 (chính sách người dùng chốt): DPI chỉ trang 1, OCR chỉ cần MỘT trang
# có text trong tối đa OCR_SCOPE_PAGES trang đầu. Màu/nén luôn toàn tài
# liệu. Không phải tối ưu tương đương — đổi phạm vi là đổi kết luận.
POLICY_LEGACY = "legacy_all_pages"
POLICY_V1 = "dpi_first_ocr_any_first5_v1"
DEFAULT_AUDIT_POLICY = POLICY_LEGACY
OCR_SCOPE_PAGES = 5

# Filter stream mất dữ liệu — xuất hiện ở bất kỳ ảnh resource nào của trang
# thì trang đó phải rơi về đường decode (xrefs=True) để kết luận theo đúng
# placement, tránh oan/miss ảnh không được vẽ.
_LOSSY_FILTERS = {"DCTDecode", "JBIG2Decode", "JPXDecode"}

# Toán tử ảnh inline (BI…EI) trong content stream — inline không nằm trong
# resource nên đường fast-path phải nhường về decode.
_INLINE_IMAGE_RE = re.compile(rb"BI\s*/[A-Za-z]{1,3}[\s/>]")
# Ngàn đọc content stream khi tìm inline; to quá thì coi như nghi ngờ.
_INLINE_SCAN_LIMIT = 16_000_000

_VN_DIACRITIC_RE = re.compile(
    r"[ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợ"
    r"úùủũụứừửữựýỳỷỹỵĐĂÂÊÔƠƯ]", re.IGNORECASE)

FITZ_LOCK = threading.RLock()  # _compression_note may re-enter during audit_pdf


# --------------------------------------------------------------------------- #
# Kết quả thẩm định 1 file PDF
# --------------------------------------------------------------------------- #

@dataclass
class ImageInfo:
    """Một ảnh đặt trên một trang của PDF."""
    page_no: int          # 1-based
    width_px: int
    height_px: int
    dpi_x: float
    dpi_y: float
    bpc: int              # bits per component
    ncomp: int            # số kênh màu (3 = RGB, 1 = xám, 4 = CMYK)
    cs_name: str          # "DeviceRGB", "ICCBased(RGB,…)", …
    filters: list[str]    # tên filter stream, rỗng = không nén
    xref: int = 0         # 0 = ảnh inline (BI…EI) — không đọc được filter

    @property
    def dpi(self) -> float:
        return min(self.dpi_x, self.dpi_y)

    @property
    def color_mode(self) -> str:
        if self.ncomp >= 3:
            return "colour"
        if self.ncomp == 1:
            return "grey"
        return "other"


@dataclass
class PdfAuditResult:
    path: str
    pages: int = 0
    images: list[ImageInfo] = field(default_factory=list)
    # Kết luận từng tiêu chí (None = không xác định được / không áp dụng).
    color_ok: bool | None = None
    compression_ok: bool | None = None
    compression_note: str = ""      # mô tả vi phạm nén đầu tiên ('' = đạt)
    dpi_ok: bool | None = None
    ocr_ok: bool | None = None
    min_dpi: float | None = None
    max_dpi: float | None = None
    color_mode_file: str = "none"   # colour | grey | mixed | other | none
    filters: list[str] = field(default_factory=list)
    pages_with_text: int = 0
    pages_with_image: int = 0
    scanned_pages_with_text: int = 0
    blank_pages: int = 0
    text_chars: int = 0
    vn_letters: int = 0             # số chữ cái có dấu tiếng Việt
    letters: int = 0                # tổng chữ cái latin trong text
    pdfa: str = ""                  # "1B", "2B", "3U"… — rỗng nếu không có
    signed: bool | None = None      # có chữ ký số (None = không xác định)
    stamp_images_skipped: int = 0   # ảnh dấu/mộc chữ ký đã loại khỏi thống kê
    name_ok: bool | None = None     # tên khớp quy ước hồ sơ + thư mục cha
    name_note: str = ""             # mô tả vi phạm tên ('' = đạt)
    small_page_note: str = ""       # khổ ≤ ~70% A4 mà < 600 dpi ('' = không)
    file_size: int = 0
    error: str = ""                 # không rỗng = không thẩm định được
    quick: bool = False             # True = quét nhanh (chưa có nén/OCR)
    policy: str = POLICY_LEGACY     # phạm vi thẩm định đã dùng
    dpi_scope_page: int = 0         # 0 = mọi trang; 1 = chỉ trang 1 (v1)
    ocr_scope_pages: int = 0        # 0 = mọi trang; K = K trang đầu (v1)
    ocr_detected_page: int | None = None  # trang tìm thấy text (v1)
    ocr_text_pages_tried: int = 0   # số trang đã trích text (v1 — theo phạm vi)
    ocr_stop_reason: str = ""       # found|limit_reached|error|not_run
    pages_decoded: int = 0          # số trang phải decode ảnh (xrefs=True)
    fast_filters: list[str] = field(default_factory=list)  # filter an toàn
    # thu thập từ fast-path resource (đều không mất dữ liệu); _conclude
    # gộp vào result.filters.

    def dpi_scope_images(self) -> list[ImageInfo]:
        """Tập ảnh dùng cho kết luận DPI theo policy hiện hành."""
        if self.policy == POLICY_V1:
            return [im for im in self.images if im.page_no == 1]
        return self.images

    # ------------------------------------------------------------ mô tả

    def color_summary(self) -> str:
        """Mô tả chế độ màu thực tế, ví dụ 'Màu RGB 8 bit (24 bit)'."""
        if not self.images:
            return "Không có ảnh scan"
        if self.color_mode_file == "mixed":
            return "Trộn lẫn màu và thang xám"
        bpcs = sorted({im.bpc for im in self.images})
        bpc_txt = "/".join(str(b) for b in bpcs) + " bit"
        if self.color_mode_file == "colour":
            depth = " (24 bit)" if bpcs == [8] else ""
            return f"Màu {bpc_txt}{depth}"
        if self.color_mode_file == "grey":
            return f"Thang xám {bpc_txt}"
        css = sorted({im.cs_name for im in self.images})[:3]
        return "Chế độ màu lạ: " + ", ".join(css)

    def compression_summary(self) -> str:
        if not self.filters:
            return "Không nén"
        return "Bộ nén: " + ", ".join("/" + f for f in self.filters)

    def dpi_detail(self) -> str:
        if self.min_dpi is None:
            return ""
        if self.policy == POLICY_V1:
            n_first = sum(1 for im in self.images if im.page_no == 1)
            txt = (f"{self.min_dpi:.0f}–{self.max_dpi:.0f} dpi "
                   f"(trang 1: {n_first} ảnh)")
        else:
            txt = (f"{self.min_dpi:.0f}–{self.max_dpi:.0f} dpi "
                   f"({len(self.images)} ảnh)")
        if self.stamp_images_skipped:
            txt += (f" · đã bỏ qua {self.stamp_images_skipped} "
                    f"ảnh dấu chữ ký")
        return txt

    def ocr_detail(self) -> str:
        if self.policy == POLICY_V1 and not self.quick:
            if self.ocr_detected_page is not None:
                return (f"Có lớp text — phát hiện ở trang "
                        f"{self.ocr_detected_page} trong "
                        f"{self.ocr_scope_pages} trang đầu")
            if self.ocr_stop_reason == "error":
                return "Không xác định được lớp text (lỗi đọc trang)"
            return (f"Chưa phát hiện OCR trong {self.ocr_scope_pages} "
                    "trang đầu")
        parts = [f"{self.pages_with_text}/{self.pages} trang có text"]
        if self.pages_with_image:
            parts.append(f"{self.pages_with_image} trang scan")
        if self.blank_pages:
            parts.append(f"{self.blank_pages} trang trắng")
        return " · ".join(parts)

    def warnings(self) -> list[str]:
        """Cảnh báo phụ (không rơi vào 4 tiêu chí chính).

        Trạng thái PDF/A KHÔNG nằm đây — đó là mô tả trung tính (chuẩn
        nhận cả PDF thường), UI tự hiển thị qua ``self.pdfa``.
        """
        out: list[str] = []
        if self.letters >= 200 and self.vn_letters < 5:
            out.append("Text hầu như không có dấu tiếng Việt — "
                       "khả năng OCR chạy sai ngôn ngữ.")
        if self.small_page_note:
            out.append(self.small_page_note)
        if self.name_ok is False and self.name_note:
            out.append(f"Tên file: {self.name_note}.")
        return out

    def hard_fail(self) -> bool:
        """Trượt tiêu chí nặng mức quét nhanh (màu/DPI) — dùng gắn dấu ❗
        trên cây; OCR chốt ở bản thẩm định đầy đủ khi bấm chọn file."""
        return self.color_ok is False or self.dpi_ok is False


# --------------------------------------------------------------------------- #
# Filter stream ảnh / JPEG 2000 lossless
# --------------------------------------------------------------------------- #

def _stream_filters(doc: "fitz.Document", xref: int) -> list[str]:
    """Danh sách filter của stream ảnh (theo thứ tự áp dụng)."""
    try:
        kind, value = doc.xref_get_key(xref, "Filter")
    except Exception:
        return []
    if kind in ("null", "missing"):
        return []
    if kind == "array":
        return [s for s in re.findall(r"/([A-Za-z0-9]+)", value or "")]
    if kind == "name":
        return [value.strip("/ ")] if value.strip("/ ") else []
    return []


def _jpx_transform(data: bytes) -> int | None:
    """Byte 'transformation' trong marker COD của codestream JPEG 2000.

    0 = wavelet 9/7 không thuận nghịch (LOSSY), 1 = wavelet 5/3 thuận
    nghịch (lossless). None nếu không tìm thấy COD (dữ liệu lạ/hỏng).
    Cấu trúc payload COD sau marker FF52: Lcod(2) Scod(1) | SGcod:
    prog(1) layers(2) mct(1) | SPcod: levels(1) cbw(1) cbh(1) style(1)
    **transform(1)** → byte ở offset 11.
    """
    start = data.find(b"\xff\x4f\xff\x51")  # SOC + SIZ
    if start < 0:
        return None
    pos = start + 4
    end = min(len(data), start + 4_000_000)  # COD nằm gần đầu codestream
    while pos + 4 <= end:
        marker = data[pos:pos + 2]
        if marker == b"\xff\xd9":  # EOC
            break
        if marker[0] != 0xFF or 0x30 <= marker[1] <= 0x3F:
            pos += 1  # marker không kèm độ dài — bước 1 byte
            continue
        seg_len = int.from_bytes(data[pos + 2:pos + 4], "big")
        if marker == b"\xff\x52":  # COD
            payload = data[pos + 2:pos + 2 + seg_len]
            return payload[11] if len(payload) >= 12 else None
        if seg_len < 2:
            break
        pos += 2 + seg_len
    return None


def _compression_note(doc: "fitz.Document", im: ImageInfo,
                      note_cache: dict | None = None) -> str | None:
    """Mô tả vi phạm nén ('' = đạt, None = không xác định).

    Chuẩn cho phép: KHÔNG NÉN hoặc nén KHÔNG MẤT DỮ LIỆU — LZW, Flate
    (zlib), CCITT Fax G3/G4 (thang xám 1 bit), JPEG 2000 lossless (wavelet
    5/3), RunLength. Trượt: JPEG (DCTDecode), JBIG2, JPEG 2000 lossy (9/7).
    """
    if not im.filters:
        return ""
    if "DCTDecode" in im.filters:
        return f"trang {im.page_no}: JPEG (DCTDecode) nén mất dữ liệu"
    if "JBIG2Decode" in im.filters:
        return f"trang {im.page_no}: JBIG2 nén mất dữ liệu"
    if "JPXDecode" in im.filters:
        if note_cache is not None and im.xref in note_cache:
            transform = note_cache[im.xref]
        else:
            try:
                with FITZ_LOCK:
                    raw = doc.xref_stream_raw(im.xref)
            except Exception:
                return None
            transform = _jpx_transform(raw[:2_000_000])
            if note_cache is not None:
                note_cache[im.xref] = transform
        if transform == 0:
            return f"trang {im.page_no}: JPEG 2000 lossy (wavelet 9/7)"
        return "" if transform == 1 else None
    return ""


def _page_fast_filters(doc: "fitz.Document", page) -> tuple[bool, list[str], list[int]]:
    """Tập filter ảnh của trang qua resource list (KHÔNG decode ảnh).

    Trả ``(usable, filters, xrefs)``. ``usable=False`` khi không đọc được
    resource hoặc gặp filter mất dữ liệu — khi đó caller phải fallback về
    ``get_image_info(xrefs=True)`` để kết luận theo đúng placement (resource
    thừa/không được vẽ không được phép làm kết luận sai).
    """
    try:
        images = page.get_images(full=True) or []
    except Exception:
        return False, [], []
    xrefs = [int(im[0]) for im in images if im[0]]
    filters: list[str] = []
    for xref in xrefs:
        for f in _stream_filters(doc, xref):
            if f in _LOSSY_FILTERS:
                return False, [], []
            if f not in filters:
                filters.append(f)
    return True, filters, xrefs


def _page_has_inline_images(page) -> bool:
    """Trang có ảnh inline (BI…EI) không — inline không nằm trong resource.

    Đọc content stream tìm dấu hiệu ``BI`` kèm khóa dict ảnh. Không đọc
    được nội dung (hoặc stream quá lớn) thì coi như nghi ngờ → fallback.
    """
    try:
        content = page.read_contents()
    except Exception:
        return True
    if not content:
        return False            # trang rỗng — không có inline
    if len(content) > _INLINE_SCAN_LIMIT:
        return True             # quá lớn — coi như nghi ngờ, nhường decode
    return _INLINE_IMAGE_RE.search(content) is not None


def _pdf_name_check(path: str | os.PathLike) -> tuple[bool | None, str]:
    """Kiểm tra quy ước tên hồ sơ của một file PDF (chỉ CẢNH BÁO, không
    vào tiêu chí nặng).

    Tên file ≥ 5 đoạn ``<MãĐĐ>-<Phông>-<MụcLục>-<Hồ sơ>-<STT>[-gợi nhớ].
    pdf`` — mã định danh/phông tùy ý (chỉ cấm dấu "-"), mục lục 2 chữ số,
    hồ sơ 4 chữ số, số thứ tự 3 chữ số; phần sau STT giữ nguyên như
    ``PdfName.extra``. 4 mã đầu phải trùng khớp tên thư mục hồ sơ chứa nó.
    """
    p = Path(path)
    parsed = rt.parse_pdf_name(p.name)
    if parsed is None:
        return False, ("không khớp quy ước 5 đoạn <MãĐĐ>-<Phông>-<Mục lục>"
                       "-<Hồ sơ>-<STT>.pdf")
    bad: list[str] = []
    if not (parsed.muc_luc.isdigit() and len(parsed.muc_luc) == 2):
        bad.append("mục lục phải là đúng 2 chữ số")
    if not (parsed.ho_so.isdigit() and len(parsed.ho_so) == 4):
        bad.append("hồ sơ phải là đúng 4 chữ số")
    if not (parsed.stt.isdigit() and len(parsed.stt) == 3):
        bad.append("số thứ tự phải là đúng 3 chữ số")
    parent = rt.parse_dossier_folder_name(p.parent.name)
    if parent is None:
        bad.append("thư mục chứa không đúng quy ước hồ sơ 4 đoạn")
    elif (parent.ma_dinh_danh, parent.ma_phong, parent.muc_luc,
          parent.ho_so) != (parsed.ma_dinh_danh, parsed.ma_phong,
                            parsed.muc_luc, parsed.ho_so):
        bad.append("4 mã đầu của tên file không khớp thư mục hồ sơ chứa")
    if bad:
        return False, "; ".join(bad)
    return True, ""


# --------------------------------------------------------------------------- #
# Thẩm định 1 file
# --------------------------------------------------------------------------- #

def audit_pdf(path: str | os.PathLike, *, quick: bool = False,
              policy: str = DEFAULT_AUDIT_POLICY,
              progress_cb=None, cancel_cb=None) -> PdfAuditResult:
    """Thẩm định một file PDF theo chuẩn số hóa. Không ghi/sửa file.

    ``quick=True``: chỉ lấy những gì cần để gắn dấu ❗ trên cây (trang,
    ảnh, DPI, màu, PDF/A) — bỏ extract text và dò filter stream nên nhanh
    hơn hẳn; ``compression_ok``/``ocr_ok`` giữ None và ``quick=True``.

    ``policy``: ``POLICY_LEGACY`` xét DPI/OCR mọi trang (mặc định, giữ cho
    tham chiếu/test); ``POLICY_V1``: DPI chỉ trang 1, OCR chỉ cần một trang
    có text trong tối đa ``OCR_SCOPE_PAGES`` trang đầu (màu/nén luôn toàn
    tài liệu). Đổi policy là đổi phạm vi kết luận, không phải tối ưu tương
    đương.
    """
    v1 = policy == POLICY_V1
    result = PdfAuditResult(path=str(path), quick=quick, policy=policy)
    if v1:
        result.dpi_scope_page = 1
    try:
        result.file_size = os.path.getsize(path)
    except OSError:
        pass
    result.name_ok, result.name_note = _pdf_name_check(path)
    try:
        with FITZ_LOCK:
            doc = fitz.open(str(path))
    except Exception as exc:
        result.error = f"Không mở được PDF: {exc}"
        return result
    try:
        with FITZ_LOCK:
            result.pages = doc.page_count
            result.pdfa = _pdfa_label(doc)
            result.signed = _has_signature(doc)
            ocr_limit = doc.page_count
            if not quick and v1:
                ocr_limit = min(OCR_SCOPE_PAGES, doc.page_count)
                result.ocr_scope_pages = ocr_limit
            ocr_stop = False       # v1: tìm thấy text thì ngừng trích
            text_errors: list[int] = []
            note_cache: dict[int, str | None] = {}
            for i in range(doc.page_count):
                if cancel_cb is not None and cancel_cb():
                    result.error = "Đã hủy"
                    return result
                if progress_cb is not None:
                    progress_cb(i, doc.page_count)
                page = doc.load_page(i)
                # Ảnh dấu/mộc chữ ký (nằm trong appearance của SigField)
                # không phải nội dung scan — loại khỏi thống kê chất lượng.
                sig_rects = _signature_widget_rects(page)

                def _split_stamps(infos: list[dict]) -> list[dict]:
                    if not sig_rects:
                        return infos
                    kept = []
                    for im in infos:
                        if _is_signature_stamp_image(
                                im.get("bbox"), sig_rects, page.rect):
                            result.stamp_images_skipped += 1
                        else:
                            kept.append(im)
                    return kept

                if quick:
                    # xrefs=True ép MuPDF GIẢI MÃ toàn bộ ảnh + tính MD5
                    # (chiếm ~95% thời gian full!) chỉ để lấy xref — quét
                    # nhanh lấy metadata thuần.
                    infos = page.get_image_info(xrefs=False)
                else:
                    # Fast-path: đọc filter qua resource list (rẻ) — chỉ
                    # khi TOÀN BỘ filter an toàn, đủ resource cho mọi
                    # placement và không có ảnh inline mới né được decode.
                    # Gặp filter mất dữ liệu/mơ hồ thì decode đúng trang
                    # đó để kết luận theo placement như đường cũ.
                    usable, page_filters, res_xrefs = _page_fast_filters(
                        doc, page)
                    infos = None
                    if usable:
                        meta = page.get_image_info(xrefs=False)
                        kept_n = _count_kept_placeholders(
                            meta, sig_rects, page.rect)
                        # Fast-path chỉ khi đủ resource cho mọi placement
                        # (trang không ảnh cũng đạt — không cần decode) và
                        # không có ảnh inline. Trang có ảnh mà thiếu
                        # resource (inline/form lạ) → decode đúng trang.
                        if (len(res_xrefs) >= kept_n
                                and (res_xrefs or not kept_n)
                                and not _page_has_inline_images(page)):
                            infos = meta
                            for f in page_filters:
                                if f not in result.fast_filters:
                                    result.fast_filters.append(f)
                    if infos is None:
                        infos = page.get_image_info(xrefs=True)
                        result.pages_decoded += 1
                infos = _split_stamps(infos)
                has_image = bool(infos)
                page_entries = [_image_entry(i + 1, im, doc, quick=quick)
                                for im in infos]
                result.images.extend(page_entries)
                if page_entries and not result.small_page_note \
                        and (not v1 or i == 0):
                    # Quy định 600 dpi cho khổ nhỏ: cả hai cạnh trang ≤
                    # ~70% A4 (đã chuẩn hóa chiều) mà ảnh < 600 dpi → ghi
                    # chú cảnh báo (không trượt nặng — máy quét cắt lề có
                    # thể làm khổ đo được lệch). v1: chỉ cảnh báo trang 1.
                    w_mm = page.rect.width * 25.4 / 72.0
                    h_mm = page.rect.height * 25.4 / 72.0
                    scale = min(min(w_mm, h_mm) / A4_MM[0],
                                max(w_mm, h_mm) / A4_MM[1])
                    if scale <= A4_SMALL_SCALE:
                        worst = min(e.dpi for e in page_entries)
                        if worst < DPI_SMALL_PAGE - DPI_TOLERANCE:
                            result.small_page_note = (
                                f"Trang {i + 1} khổ nhỏ (≈{scale * 100:.0f}"
                                f"% A4) quét {worst:.0f} dpi — quy định yêu "
                                " cầu 600 dpi cho khổ nhỏ hơn A4, cân nhắc "
                                "quét lại")
                if not quick:
                    chars = 0
                    extracted = False
                    if i < ocr_limit and not ocr_stop:
                        extracted = True
                        result.ocr_text_pages_tried += 1
                        try:
                            text = page.get_text("text") or ""
                        except Exception:
                            text = ""
                            text_errors.append(i + 1)
                        chars = len("".join(text.split()))
                        if chars >= OCR_MIN_CHARS:
                            result.pages_with_text += 1
                            if has_image:
                                result.scanned_pages_with_text += 1
                            result.text_chars += chars
                            result.letters += sum(ch.isalpha()
                                                  for ch in text)
                            result.vn_letters += len(
                                _VN_DIACRITIC_RE.findall(text))
                            if v1:
                                result.ocr_detected_page = i + 1
                                result.ocr_stop_reason = "found"
                                ocr_stop = True
                    if has_image:
                        result.pages_with_image += 1
                    elif extracted and chars < OCR_MIN_CHARS:
                        result.blank_pages += 1
                elif has_image:
                    result.pages_with_image += 1
            if v1 and not quick and result.ocr_detected_page is None \
                    and not result.ocr_stop_reason:
                result.ocr_stop_reason = ("error" if text_errors
                                          else "limit_reached")
            _conclude(result, doc, note_cache)
    except Exception as exc:
        result.error = f"Lỗi đọc cấu trúc PDF: {exc}"
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return result


def _count_kept_placeholders(infos: list[dict], sig_rects: list,
                             page_rect) -> int:
    """Số placement còn lại sau khi loại ảnh dấu — KHÔNG đếm vào thống kê
    (dùng để so chuẩn resource ≥ placement của fast-path, không đè lên
    ``stamp_images_skipped`` của lượt _split_stamps thật)."""
    if not sig_rects:
        return len(infos)
    return sum(1 for im in infos
               if not _is_signature_stamp_image(im.get("bbox"), sig_rects,
                                                page_rect))


def _image_entry(page_no: int, im: dict, doc: "fitz.Document",
                 quick: bool = False) -> ImageInfo:
    """ImageInfo từ ``page.get_image_info(xrefs=True)``.

    DPI = pixel ÷ kích thước đặt trên trang (điểm/72); chiều dài chiếu lấy
    hypot của cột ma trận biến đổi nên đúng cả ảnh bị xoay/nghiêng.
    """
    width = int(im.get("width") or 0)
    height = int(im.get("height") or 0)
    a, b, c, d = (im.get("transform") or (1, 0, 0, 1))[:4]
    disp_w = (a * a + b * b) ** 0.5
    disp_h = (c * c + d * d) ** 0.5
    bbox = im.get("bbox")
    if disp_w < 1e-6 and bbox is not None:
        disp_w = float(bbox[2] - bbox[0])
    if disp_h < 1e-6 and bbox is not None:
        disp_h = float(bbox[3] - bbox[1])
    dpi_x = width * 72.0 / disp_w if disp_w > 1e-6 else 0.0
    dpi_y = height * 72.0 / disp_h if disp_h > 1e-6 else 0.0
    xref = int(im.get("xref") or 0)
    return ImageInfo(
        page_no=page_no, width_px=width, height_px=height,
        dpi_x=dpi_x, dpi_y=dpi_y,
        bpc=int(im.get("bpc") or 0),
        ncomp=int(im.get("colorspace") or 0),
        cs_name=str(im.get("cs-name") or ""),
        filters=(_stream_filters(doc, xref)
                 if xref and not quick else []),
        xref=xref,
    )


def _signature_widget_rects(page) -> list:
    """Rect các ô chữ ký số (SigField widget) trên trang.

    Ảnh dấu/mộc do tool ký số (pyHanko, Adobe…) chèn nằm trong appearance
    của widget chữ ký, nhưng MuPDF liệt kê chúng trong ``get_image_info``
    như ảnh thường — làm sai thống kê DPI/nén của ảnh scan (ví dụ bản ký
    của file scan 300 dpi bị tụt còn "287 dpi" vì dấu chỉ ~287 dpi).
    """
    rects = []
    try:
        widgets = page.widgets() or []
    except Exception:
        return rects
    for widget in widgets:
        try:
            if (getattr(widget, "field_type_string", "") == "Signature"
                    and widget.rect is not None):
                rects.append(fitz.Rect(widget.rect))
        except Exception:
            continue
    return rects


def _is_signature_stamp_image(bbox, sig_rects, page_rect) -> bool:
    """True khi ảnh là dấu/mộc chữ ký chèn bởi tool ký số.

    Ảnh nằm ≥90% diện tích trong rect một ô chữ ký, và không phủ gần hết
    trang (ảnh full-page là nội dung scan, dù hi hữu hiếm khi dấu tràn
    trang thì vẫn giữ trong thống kê — an toàn hơn là mất dữ liệu scan).
    """
    if not sig_rects or bbox is None:
        return False
    try:
        rect = fitz.Rect(bbox)
    except Exception:
        return False
    area = rect.width * rect.height
    page_area = page_rect.width * page_rect.height
    if area <= 1e-6 or page_area <= 1e-6:
        return False
    if area / page_area >= 0.9:
        return False
    for sr in sig_rects:
        ix0, iy0 = max(rect.x0, sr.x0), max(rect.y0, sr.y0)
        ix1, iy1 = min(rect.x1, sr.x1), min(rect.y1, sr.y1)
        inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        if inter / area >= 0.9:
            return True
    return False


def _has_signature(doc: "fitz.Document") -> bool:
    """PDF có chữ ký số không: đọc ``AcroForm/SigFlags`` — bit 1 (=1)
    "có ít nhất một chữ ký". AcroForm có thể là dict nhúng hoặc xref.
    Mọi công cụ ký số (Adobe, USB-token…) đều đặt cờ này theo spec."""
    try:
        cat = doc.pdf_catalog()
        kind, value = doc.xref_get_key(cat, "AcroForm")
        if kind == "xref":
            kind, value = doc.xref_get_key(
                int(value.split()[0]), "SigFlags")
        elif kind == "dict":
            kind, value = doc.xref_get_key(cat, "AcroForm/SigFlags")
        else:
            return False
        if kind == "int":
            try:
                return (int(value) & 1) == 1
            except ValueError:
                return False
    except Exception:
        pass
    return False


def _pdfa_label(doc: "fitz.Document") -> str:
    """Đọc pdfaid:part + pdfaid:conformance từ XMP ('' nếu không có)."""
    try:
        xmp = doc.get_xml_metadata() or ""
    except Exception:
        return ""
    if "pdfaid" not in xmp:
        return ""
    part = (re.search(r"<pdfaid:part>\s*(\d+)\s*<", xmp)
            or re.search(r'pdfaid:part="(\d+)"', xmp))
    if not part:
        return ""
    conf = (re.search(r"<pdfaid:conformance>\s*([ABab])\s*<", xmp)
            or re.search(r'pdfaid:conformance="([ABab])"', xmp))
    return part.group(1) + (conf.group(1).upper() if conf else "")


def _conclude(result: PdfAuditResult, doc: "fitz.Document",
              note_cache: dict | None = None):
    """Chốt kết luận 4 tiêu chí từ dữ liệu đã quét (theo policy của result)."""
    v1 = result.policy == POLICY_V1
    # --- chế độ màu: CÓ ảnh màu là đạt; 100% ảnh không màu mới trượt ------
    if result.images:
        modes = {im.color_mode for im in result.images}
        result.color_mode_file = ("mixed" if len(modes) > 1
                                  else next(iter(modes)))
        result.color_ok = "colour" in modes
    # --- độ nén (bỏ qua với quét nhanh — chưa đọc filter) -----------------
    # Fast-path đã chứng minh toàn bộ filter trang là an toàn (không có
    # DCT/JBIG2/JPX trong resource) nên không cần _compression_note; chỉ
    # ảnh đến từ trang decode mới được chấm note.
    if not result.quick:
        seen: list[str] = list(result.fast_filters)
        notes: list[str] = []
        compression_unknown = False
        for im in result.images:
            for f in im.filters:
                if f not in seen:
                    seen.append(f)
            if im.filters:
                note = _compression_note(doc, im, note_cache)
                if note is None:
                    compression_unknown = True
                if note and note not in notes:
                    notes.append(note)
        result.filters = sorted(seen)
        result.compression_ok = (False if notes else
                                 None if compression_unknown or not result.images
                                 else True)
        result.compression_note = (notes[0] if notes else
                                   "Không xác định được chế độ nén JPEG 2000"
                                   if compression_unknown else "")
    # --- DPI: theo phạm vi policy (legacy mọi ảnh; v1 chỉ trang 1) ---------
    scope_images = result.dpi_scope_images()
    if scope_images:
        result.min_dpi = min(im.dpi for im in scope_images)
        result.max_dpi = max(im.dpi for im in scope_images)
        # Trượt đỏ khi dưới 297 dpi (DPI_MIN trừ dung sai máy quét).
        result.dpi_ok = all(im.dpi >= DPI_MIN - DPI_TOLERANCE
                            for im in scope_images)
    # v1: trang 1 không có ảnh scan (trắng/text-only/chỉ ảnh dấu) → N/A,
    # KHÔNG tìm ảnh trang sau thay thế (phạm vi do người dùng chốt).
    # --- OCR (bỏ qua với quét nhanh — chưa extract text) --------------------
    if not result.quick:
        if v1:
            # Phép kiểm tra tồn tại: một trang trong phạm vi có text là đủ.
            if result.ocr_detected_page is not None:
                result.ocr_ok = True
            elif result.ocr_stop_reason == "error":
                result.ocr_ok = None      # lỗi đọc — không chốt chắc chắn
            elif result.pages == 0:
                result.ocr_ok = None      # PDF rỗng không tự pass
            else:
                result.ocr_ok = False     # hết phạm vi, không thấy text
        elif result.pages_with_image == 0:
            # File sinh từ Word/Excel… không có ảnh scan: có text là đạt.
            result.ocr_ok = result.pages_with_text > 0 or result.pages == 0
        else:
            # Text-only pages must not make up for scanned pages lacking OCR.
            result.ocr_ok = (result.scanned_pages_with_text
                             == result.pages_with_image)


# --------------------------------------------------------------------------- #
# Thẩm định 1 tệp TIFF (bản bảo hiểm lưu trữ — 1 tệp = 1 trang)
# --------------------------------------------------------------------------- #

# Compression tag 259: lossless (CCITT RLE/G3/G4, LZW, Deflate, PackBits,
# zlib) chỉ CẢNH BÁO ("văn bản quy định TIFF không nén"); lossy (JPEG cũ/
# mới, JPEG 2000) TRƯỢT nặng. Giá trị lạ → "unknown" (cảnh báo).
_TIFF_LOSSLESS = {2, 3, 4, 5, 8, 32773, 32946}
_TIFF_LOSSY = {6, 7, 34712}
_TIFF_COMPRESSION_NAMES = {
    1: "Không nén", 2: "CCITT RLE", 3: "CCITT G3", 4: "CCITT G4",
    5: "LZW", 6: "JPEG (cũ)", 7: "JPEG", 8: "Deflate",
    32773: "PackBits", 32946: "Deflate", 34712: "JPEG 2000",
}

# TIFF đánh số trang liên tục trong HỒ SƠ, độc lập với số văn bản PDF.
# Mã định danh/phông TÙ Ý (chỉ cấm dấu "-") thống nhất với quy ước
# CSDL_SOHOA/PDF — bề rộng chỉ ép cho mục lục/ĐVBC/trang; lệch mã với
# thư mục cha vẫn bị bắt ở phép so khớp riêng. ĐVBC gồm 4 chữ số, có thể
# thêm ký tự phân biệt số trùng (vd. 0123a).
_TIFF_NAME_RE = re.compile(
    r"^(?P<mdd>[^-]+)-(?P<phong>[^-]+)-(?P<ml>[0-9]{2})-"
    r"(?P<dvbc>[0-9]{4}[A-Za-z]?)-(?P<trang>[0-9]{3})$")


@dataclass(frozen=True)
class TiffName:
    """Tên trang TIFF ``<MãĐĐ>-<Phông>-<ML>-<ĐVBC>-<Trang>``."""
    ma_dinh_danh: str
    ma_phong: str
    muc_luc: str
    don_vi_bao_quan: str
    trang: str


def parse_tiff_name(name: str) -> TiffName | None:
    """Parse tên tệp trang TIFF; trả None nếu không khớp quy ước 5 đoạn.

    Đuôi ``.tif``/``.tiff`` do caller kiểm tra riêng — hai đuôi là CÙNG
    định dạng TIFF, nhận ngang hàng (không ép đuôi).
    """
    m = _TIFF_NAME_RE.match(Path(name).stem)
    if m is None:
        return None
    return TiffName(m["mdd"], m["phong"], m["ml"], m["dvbc"], m["trang"])


@dataclass
class TiffAuditResult:
    """Kết quả thẩm định một tệp TIFF trang quét (1 tệp = 1 trang)."""

    path: str
    pages: int = 1                    # n_frames — hợp lệ phải == 1
    width_px: int = 0
    height_px: int = 0
    dpi_x: float = 0.0                # 0 = thiếu thẻ DPI trong metadata
    dpi_y: float = 0.0
    bpc: int = 0                      # bits per sample (0 = thiếu tag)
    ncomp: int = 0                    # samples per pixel
    photometric: str = "unknown"      # RGB | grey | palette | CMYK | other
    compression_status: str = "unknown"  # none | lossless | lossy | unknown
    compression_name: str = ""
    has_icc: bool = False             # có ICC profile (tham khảo sRGB)
    color_ok: bool | None = None
    dpi_ok: bool | None = None
    single_page_ok: bool | None = None
    name_ok: bool | None = None
    name_note: str = ""
    sequence_warning: str = ""        # trang thiếu/trùng trong thư mục
    lowres_page_warning: str = ""     # khổ ≤ ~70% A4 mà < 600 dpi
    file_size: int = 0
    error: str = ""                   # không rỗng = không thẩm định được

    @property
    def dpi(self) -> float:
        return min(self.dpi_x, self.dpi_y)

    def hard_fail(self) -> bool:
        """Trượt tiêu chí nặng: màu, DPI, 1 trang/tệp, nén lossy."""
        return (self.color_ok is False or self.dpi_ok is False
                or self.single_page_ok is False
                or self.compression_status == "lossy")

    def color_summary(self) -> str:
        if self.photometric == "RGB":
            return f"Màu {self.bpc * 3} bit" if self.bpc else "Màu RGB"
        names = {"grey": "Thang xám/đen trắng", "palette": "Bảng màu",
                 "CMYK": "CMYK", "other": "Chế độ màu lạ",
                 "unknown": "Chế độ màu không rõ"}
        return names.get(self.photometric, self.photometric)

    def dpi_detail(self) -> str:
        if not self.dpi_x or not self.dpi_y:
            return "không có thẻ DPI trong metadata"
        return f"{self.dpi:.0f} dpi"

    def warnings(self) -> list[str]:
        """Cảnh báo phụ (không vào tiêu chí nặng): khổ nhỏ < 600 dpi,
        chuỗi trang thiếu/trùng, tên lệch quy ước."""
        out: list[str] = []
        if self.lowres_page_warning:
            out.append(self.lowres_page_warning)
        if self.sequence_warning:
            out.append(f"Chuỗi trang: {self.sequence_warning}.")
        if self.name_ok is False and self.name_note:
            out.append(f"Tên file: {self.name_note}.")
        return out


def _tiff_name_check(path: Path, sibling_cache: dict | None = None
                     ) -> tuple[bool | None, str, str]:
    """Kiểm tra tên + vị trí tệp TIFF. Trả ``(name_ok, name_note,
    sequence_warning)`` — vi phạm tên/chuỗi trang chỉ CẢNH BÁO.

    ``sibling_cache``: dict dùng chung cho cả lượt thẩm định (khóa là thư
    mục) để hồ sơ nhiều trang TIFF không phải liệt kê lại thư mục cho từng
    tệp (O(N²) khi quét cả cây).
    """
    parsed = parse_tiff_name(path.name)
    if parsed is None:
        return False, ("không khớp quy ước <MãĐĐ>-<Phông>-<Mục lục 2 số>"
                       "-<ĐVBC 4 số và ký tự trùng nếu có>-<Trang 3 số>.tif"), ""
    bad: list[str] = []
    parent = rt.parse_dossier_folder_name(path.parent.name)
    if parent is None:
        bad.append("thư mục chứa không đúng quy ước hồ sơ 4 đoạn")
    elif (parent.ma_dinh_danh, parent.ma_phong, parent.muc_luc,
          parent.ho_so) != (parsed.ma_dinh_danh, parsed.ma_phong,
                            parsed.muc_luc, parsed.don_vi_bao_quan):
        bad.append("4 mã đầu của tên file không khớp thư mục hồ sơ chứa")
    if int(parsed.trang) == 0:
        bad.append("số trang phải bắt đầu từ 001")
    key = str(path.parent)
    siblings = None
    if sibling_cache is not None:
        siblings = sibling_cache.get(key)
    if siblings is None:
        try:
            siblings = [e for e in path.parent.iterdir()
                        if e.suffix.lower() in (".tif", ".tiff")]
        except OSError:
            siblings = []
        if sibling_cache is not None:
            sibling_cache[key] = siblings
    nums: list[int] = []
    for sibling in siblings:
        other = parse_tiff_name(sibling.name)
        if (other is not None and int(other.trang) > 0
                and (other.ma_dinh_danh, other.ma_phong,
                     other.muc_luc, other.don_vi_bao_quan)
                == (parsed.ma_dinh_danh, parsed.ma_phong,
                    parsed.muc_luc, parsed.don_vi_bao_quan)):
            nums.append(int(other.trang))
    warnings: list[str] = []
    dupes = sorted(n for n, count in Counter(nums).items() if count > 1)
    if dupes:
        warnings.append("số trang bị trùng: " + ", ".join(
            str(n).zfill(3) for n in dupes))
    missing = sorted(set(range(1, max(nums, default=0) + 1)) - set(nums))
    if missing:
        warnings.append("thiếu trang: " + ", ".join(
            str(n).zfill(3) for n in missing[:10]))
    seq_warn = "; ".join(warnings)
    if bad:
        return False, "; ".join(bad), seq_warn
    return True, "", seq_warn


def audit_tiff(path: str | os.PathLike, *, quick: bool = False,
               progress_cb=None, cancel_cb=None,
               sibling_cache: dict | None = None) -> TiffAuditResult:
    """Thẩm định một tệp TIFF trang quét. Chỉ đọc header qua Pillow nên
    rẻ — mọi lượt quét cùng độ sâu; ``quick`` giữ để tương thích chữ ký
    ``audit_file``. Không ghi/sửa file. ``sibling_cache`` dùng chung cho cả
    lượt quét cây để tránh liệt kê lại thư mục theo từng tệp.

    Tiêu chí nặng: DPI ≥ 300 (trượt khi dưới 297 — dung sai máy quét), màu
    (RGB ≥ 3 kênh, 8 bit/kênh — xám/1-bit
    trượt như chính sách PDF), đúng 1 trang/tệp, nén không lossy. Cảnh
    báo: nén lossless ("văn bản quy định TIFF không nén"), khổ ≤ ~70% A4
    mà < 600 dpi, tên/chuỗi trang lệch quy ước.
    """
    result = TiffAuditResult(path=str(path))
    p = Path(path)
    try:
        result.file_size = os.path.getsize(path)
    except OSError:
        pass
    try:
        with Image.open(str(path)) as im:
            im.seek(0)
            n_frames = getattr(im, "n_frames", 1) or 1
            result.pages = n_frames
            result.single_page_ok = n_frames == 1
            result.width_px = int(im.width)
            result.height_px = int(im.height)
            tags = getattr(im, "tag_v2", {})

            def _num(tag):
                v = tags.get(tag)
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            unit = _num(296)
            factor = 2.54 if unit == 3 else 1.0   # 2 = inch (mặc định), 3 = cm
            xres, yres = _num(282), _num(283)
            if xres is not None:
                result.dpi_x = xres * factor
            if yres is not None:
                result.dpi_y = yres * factor
            bps = tags.get(258)
            if isinstance(bps, (tuple, list)):
                bpc_list = [int(b) for b in bps]
            elif bps is not None:
                bpc_list = [int(bps)]
            else:
                bpc_list = []
            spp = _num(277)
            if spp is None and not bpc_list and im.mode in ("1", "L"):
                # Bilevel/thang xám không ghi tag 258/277 (giá trị ngầm
                # định 1 bit / 8 bit, 1 kênh) — suy ra từ mode Pillow.
                bpc_list = [1 if im.mode == "1" else 8]
                spp = 1.0
            result.bpc = bpc_list[0] if bpc_list else 0
            result.ncomp = int(spp) if spp is not None else len(bpc_list)
            photo = _num(262)
            photo = int(photo) if photo is not None else None
            result.photometric = {
                0: "grey", 1: "grey", 2: "RGB", 3: "palette",
                5: "CMYK", 6: "RGB",          # YCbCr = JPEG RGB
            }.get(photo, "other")
            result.has_icc = 34675 in tags
            comp = _num(259)
            comp = int(comp) if comp is not None else None
            result.compression_name = _TIFF_COMPRESSION_NAMES.get(
                comp, f"bộ nén lạ ({comp})" if comp is not None else "không rõ")
            if comp == 1:
                result.compression_status = "none"
            elif comp in _TIFF_LOSSLESS:
                result.compression_status = "lossless"
            elif comp in _TIFF_LOSSY:
                result.compression_status = "lossy"
            else:
                result.compression_status = "unknown"

            if result.dpi_x > 0 and result.dpi_y > 0:
                dpi_eff = min(result.dpi_x, result.dpi_y)
                result.dpi_ok = dpi_eff >= DPI_MIN - DPI_TOLERANCE
                w_mm = result.width_px * 25.4 / result.dpi_x
                h_mm = result.height_px * 25.4 / result.dpi_y
                scale = min(min(w_mm, h_mm) / A4_MM[0],
                            max(w_mm, h_mm) / A4_MM[1])
                if result.dpi_ok and scale <= A4_SMALL_SCALE \
                        and dpi_eff < DPI_SMALL_PAGE - DPI_TOLERANCE:
                    result.lowres_page_warning = (
                        f"Khổ nhỏ (≈{scale * 100:.0f}% A4) quét "
                        f"{dpi_eff:.0f} dpi — quy định yêu cầu 600 dpi cho "
                        "khổ nhỏ hơn A4, cân nhắc quét lại")
            # Màu bắt buộc (chính sách như PDF): RGB/YCbCr/CMYK ≥ 3 kênh,
            # đủ 8 bit/kênh; xám, bảng màu, 1-bit, 16 bit đều trượt.
            if result.ncomp and bpc_list:
                result.color_ok = (
                    result.ncomp >= 3
                    and photo in (2, 5, 6)       # RGB / CMYK / YCbCr
                    and all(b == 8 for b in bpc_list))
    except Exception as exc:
        result.error = f"Không đọc được TIFF: {exc}"
        return result
    result.name_ok, result.name_note, result.sequence_warning = \
        _tiff_name_check(p, sibling_cache)
    return result


def audit_file(path: str | os.PathLike, *, quick: bool = False,
               policy: str = DEFAULT_AUDIT_POLICY,
               progress_cb=None, cancel_cb=None,
               sibling_cache: dict | None = None):
    """Thẩm định 1 file theo đuôi: ``.pdf`` → ``audit_pdf``, ``.tif``/
    ``.tiff`` → ``audit_tiff`` (hai đuôi cùng định dạng, nhận ngang hàng).
    Đuôi khác raise ValueError (UI không đưa vào cây). ``policy`` chỉ áp
    dụng cho PDF (TIFF luôn đủ độ sâu vì đọc header rẻ)."""
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return audit_pdf(path, quick=quick, policy=policy,
                         progress_cb=progress_cb, cancel_cb=cancel_cb)
    if suffix in (".tif", ".tiff"):
        return audit_tiff(path, progress_cb=progress_cb,
                          cancel_cb=cancel_cb, sibling_cache=sibling_cache)
    raise ValueError(
        f"Không thẩm định được định dạng \"{suffix or '(không có)'}\": {path}")


# --------------------------------------------------------------------------- #
# Thống kê cây thư mục (số tài liệu / số trang theo từng thư mục)
# --------------------------------------------------------------------------- #

@dataclass
class FolderStats:
    root: str
    total_docs: int = 0  # số tệp PDF/TIFF; TIFF không cho biết số văn bản
    total_pdf_documents: int = 0
    total_pages: int = 0
    # rel posix của thư mục ("": gốc) → (số tệp, số trang) toàn subtree.
    dir_stats: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Chỉ tệp PDF là tài liệu; tệp TIFF tương ứng với trang.
    dir_pdf_documents: dict[str, int] = field(default_factory=dict)
    # rel posix của thư mục → tổng dung lượng PDF/TIFF trong subtree (byte).
    dir_sizes: dict[str, int] = field(default_factory=dict)
    # Thư mục lá là hồ sơ; thống kê số hồ sơ trong từng nhánh.
    dir_dossiers: dict[str, int] = field(default_factory=dict)
    dirs_with_subfolders: set[str] = field(default_factory=set)
    total_size: int = 0
    level_counts: dict[str, int] = field(default_factory=lambda: {
        "mdd": 0, "phong": 0, "muc_luc": 0, "ho_so": 0})
    unreadable: list[str] = field(default_factory=list)  # PDF lỗi

    def of_rel(self, rel: str | None) -> tuple[int, int]:
        if not rel:
            return (self.total_docs, self.total_pages)
        return self.dir_stats.get(rel, (0, 0))

    def size_of_rel(self, rel: str | None) -> int:
        """Tổng dung lượng subtree của thư mục ``rel`` (byte)."""
        if not rel:
            return self.total_size
        return self.dir_sizes.get(rel, 0)

    def dossier_count_of_rel(self, rel: str | None) -> int:
        return self.dir_dossiers.get(rel or "", 0)

    def document_count_of_rel(self, rel: str | None) -> int:
        return self.total_pdf_documents if not rel else self.dir_pdf_documents.get(rel, 0)

    def has_subfolders(self, rel: str | None) -> bool:
        return (rel or "") in self.dirs_with_subfolders


def populate_dossier_folder_stats(stats: FolderStats) -> None:
    """Count leaf dossier folders once for every ancestor folder."""
    dirs = set(stats.dir_stats)
    parents = set()
    for rel in dirs:
        if rel:
            parents.add(rel.rsplit("/", 1)[0] if "/" in rel else "")
    stats.dirs_with_subfolders = parents
    counts: dict[str, int] = {}
    for rel in dirs - parents - {""}:
        current = rel
        while True:
            counts[current] = counts.get(current, 0) + 1
            if not current:
                break
            current = current.rsplit("/", 1)[0] if "/" in current else ""
    if not parents and stats.dir_stats.get("", (0, 0))[0] > 0:
        counts[""] = 1  # chính thư mục gốc là một hồ sơ
    stats.dir_dossiers = counts


def count_pages(path: Path, cache: dict | None = None) -> int:
    """Số trang PDF; đọc lại chỉ khi size/mtime đổi. Lỗi → -1."""
    try:
        st = path.stat()
    except OSError:
        return -1
    key = (str(path), st.st_size, st.st_mtime_ns)
    if cache is not None and key in cache:
        return cache[key]
    try:
        with FITZ_LOCK:
            with fitz.open(str(path)) as doc:
                pages = doc.page_count
    except Exception:
        pages = -1
    if cache is not None:
        cache[key] = pages
    return pages


def scan_tree_stats(root: Path, *, page_cache: dict | None = None,
                    progress_cb=None, cancel_cb=None) -> FolderStats:
    """Đếm tệp PDF/TIFF + trang theo subtree cho từng thư mục dưới root.

    ``progress_cb(docs_so_far, pdf_path)`` báo tiến độ; ``cancel_cb`` trả
    True để dừng giữa chừng.
    """
    stats = FolderStats(root=str(root))
    docs_seen = 0

    def walk(path: Path, rel: str) -> tuple[int, int]:
        nonlocal docs_seen
        docs = 0
        pages = 0
        pdf_documents = 0
        size = 0
        try:
            entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            stats.dir_stats[rel] = (0, 0)
            return 0, 0
        for entry in entries:
            if cancel_cb is not None and cancel_cb():
                break
            if entry.is_dir():
                child_rel = entry.relative_to(root).as_posix()
                sub_docs, sub_pages = walk(entry, child_rel)
                pdf_documents += stats.dir_pdf_documents.get(child_rel, 0)
                size += stats.dir_sizes.get(child_rel, 0)
                level = rt.level_of(Path(child_rel))
                if level is rt.Level.MA_DINH_DANH:
                    stats.level_counts["mdd"] += 1
                elif level is rt.Level.PHONG:
                    stats.level_counts["phong"] += 1
                elif level is rt.Level.MUC_LUC:
                    stats.level_counts["muc_luc"] += 1
                elif level is rt.Level.HO_SO:
                    stats.level_counts["ho_so"] += 1
                docs += sub_docs
                pages += sub_pages
            elif entry.suffix.lower() == ".pdf":
                pdf_documents += 1
                try:
                    size += entry.stat().st_size
                except OSError:
                    pass
                docs_seen += 1
                if progress_cb is not None:
                    progress_cb(docs_seen, entry)
                n = count_pages(entry, page_cache)
                if n < 0:
                    stats.unreadable.append(
                        entry.relative_to(root).as_posix())
                    n = 0
                docs += 1
                pages += n
            elif entry.suffix.lower() in (".tif", ".tiff"):
                try:
                    size += entry.stat().st_size
                except OSError:
                    pass
                # TIFF chuẩn là 1 tệp = 1 trang — không đọc file (.tif và
                # .tiff cùng định dạng, đếm ngang hàng).
                docs_seen += 1
                if progress_cb is not None:
                    progress_cb(docs_seen, entry)
                docs += 1
                pages += 1
        stats.dir_stats[rel] = (docs, pages)
        stats.dir_pdf_documents[rel] = pdf_documents
        stats.dir_sizes[rel] = size
        return docs, pages

    stats.total_docs, stats.total_pages = walk(root, "")
    stats.total_pdf_documents = stats.dir_pdf_documents.get("", 0)
    stats.total_size = stats.dir_sizes.get("", 0)
    populate_dossier_folder_stats(stats)
    return stats
