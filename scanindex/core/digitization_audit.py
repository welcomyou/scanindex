"""Thẩm định chất lượng số hóa PDF — chỉ đọc cấu trúc file, không sửa.

Kiểm tra qua PyMuPDF: DPI ảnh (≥ 300), chế độ màu (chỉ cần CÓ ảnh màu —
RGB/CMYK — là đạt; 100% ảnh đen-trắng/xám mới trượt), độ nén (không nén
hoặc lossless — JPEG/DCT, JBIG2, JPEG 2000 lossy 9/7 là trượt), lớp text
OCR (trang scan phải có text, cảnh báo khi text gần như không có dấu
tiếng Việt) và metadata PDF/A.

``scan_tree_stats`` đếm tài liệu/trang theo từng thư mục trong cây
CSDL_SOHOA (cấp thư mục xem ``scanindex.core.rename_tree.level_of``).
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF

from scanindex.core import rename_tree as rt

# --------------------------------------------------------------------------- #
# Ngưỡng chuẩn
# --------------------------------------------------------------------------- #

DPI_MIN = 300

# Số ký tự text tối thiểu trên một trang có ảnh scan để tính là "có lớp text".
OCR_MIN_CHARS = 10
# Tỉ lệ dấu tiếng Việt / chữ cái dưới ngưỡng này (khi text đủ dài) → cảnh báo.
VN_DIACRITIC_RATIO_WARN = 0.01

_VN_DIACRITIC_RE = re.compile(
    r"[ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợ"
    r"úùủũụứừửữựýỳỷỹỵĐĂÂÊÔƠƯ]", re.IGNORECASE)

FITZ_LOCK = threading.Lock()  # tuần tự hóa fitz giữa các worker + viewer


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
    blank_pages: int = 0
    text_chars: int = 0
    vn_letters: int = 0             # số chữ cái có dấu tiếng Việt
    letters: int = 0                # tổng chữ cái latin trong text
    pdfa: str = ""                  # "1B", "2B", "3U"… — rỗng nếu không có
    signed: bool | None = None      # có chữ ký số (None = không xác định)
    file_size: int = 0
    error: str = ""                 # không rỗng = không thẩm định được
    quick: bool = False             # True = quét nhanh (chưa có nén/OCR)

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
        return (f"{self.min_dpi:.0f}–{self.max_dpi:.0f} dpi "
                f"({len(self.images)} ảnh)")

    def ocr_detail(self) -> str:
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
        return out


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


def _compression_note(doc: "fitz.Document", im: ImageInfo) -> str:
    """Mô tả vi phạm nén của một ảnh ('' = không vi phạm).

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
        try:
            with FITZ_LOCK:
                raw = doc.xref_stream_raw(im.xref)
        except Exception:
            return ""
        if _jpx_transform(raw[:2_000_000]) == 0:
            return f"trang {im.page_no}: JPEG 2000 lossy (wavelet 9/7)"
        return ""
    return ""


# --------------------------------------------------------------------------- #
# Thẩm định 1 file
# --------------------------------------------------------------------------- #

def audit_pdf(path: str | os.PathLike, *, quick: bool = False,
              progress_cb=None, cancel_cb=None) -> PdfAuditResult:
    """Thẩm định một file PDF theo chuẩn số hóa. Không ghi/sửa file.

    ``quick=True``: chỉ lấy những gì cần để gắn dấu ❗ trên cây (trang,
    ảnh, DPI, màu, PDF/A) — bỏ extract text và dò filter stream nên nhanh
    hơn hẳn; ``compression_ok``/``ocr_ok`` giữ None và ``quick=True``.
    """
    result = PdfAuditResult(path=str(path), quick=quick)
    try:
        result.file_size = os.path.getsize(path)
    except OSError:
        pass
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
            for i in range(doc.page_count):
                if cancel_cb is not None and cancel_cb():
                    result.error = "Đã hủy"
                    return result
                if progress_cb is not None:
                    progress_cb(i, doc.page_count)
                page = doc.load_page(i)
                # xrefs=True ép MuPDF GIẢI MÃ toàn bộ ảnh + tính MD5
                # (~110ms/trang!) chỉ để lấy xref — chỉ bản đầy đủ mới cần
                # (đọc filter stream). Quét nhanh lấy metadata thuần.
                try:
                    infos = page.get_image_info(xrefs=not quick)
                except Exception:
                    infos = []
                has_image = bool(infos)
                for im in infos:
                    result.images.append(
                        _image_entry(i + 1, im, doc, quick=quick))
                if not quick:
                    try:
                        text = page.get_text("text") or ""
                    except Exception:
                        text = ""
                    chars = len("".join(text.split()))
                    if chars >= OCR_MIN_CHARS:
                        result.pages_with_text += 1
                        result.text_chars += chars
                        result.letters += sum(ch.isalpha() for ch in text)
                        result.vn_letters += len(
                            _VN_DIACRITIC_RE.findall(text))
                    if has_image:
                        result.pages_with_image += 1
                    elif chars < OCR_MIN_CHARS:
                        result.blank_pages += 1
                elif has_image:
                    result.pages_with_image += 1
            _conclude(result, doc)
    except Exception as exc:
        result.error = f"Lỗi đọc cấu trúc PDF: {exc}"
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return result


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


def _conclude(result: PdfAuditResult, doc: "fitz.Document"):
    """Chốt kết luận 4 tiêu chí từ dữ liệu đã quét."""
    # --- chế độ màu: CÓ ảnh màu là đạt; 100% ảnh không màu mới trượt ------
    if result.images:
        modes = {im.color_mode for im in result.images}
        result.color_mode_file = ("mixed" if len(modes) > 1
                                  else next(iter(modes)))
        result.color_ok = "colour" in modes
    # --- độ nén (bỏ qua với quét nhanh — chưa đọc filter) -----------------
    if not result.quick:
        seen: list[str] = []
        notes: list[str] = []
        for im in result.images:
            for f in im.filters:
                if f not in seen:
                    seen.append(f)
            note = _compression_note(doc, im)
            if note and note not in notes:
                notes.append(note)
        result.filters = sorted(seen)
        result.compression_ok = (not notes) if result.images else None
        result.compression_note = notes[0] if notes else ""
    # --- DPI ----------------------------------------------------------------
    if result.images:
        result.min_dpi = min(im.dpi for im in result.images)
        result.max_dpi = max(im.dpi for im in result.images)
        # Dung sai 1 dpi cho tròn số (2480px/595pt = 300.07…).
        result.dpi_ok = all(im.dpi >= DPI_MIN - 1.0 for im in result.images)
    # --- OCR (bỏ qua với quét nhanh — chưa extract text) --------------------
    if not result.quick:
        if result.pages_with_image == 0:
            # File sinh từ Word/Excel… không có ảnh scan: có text là đạt.
            result.ocr_ok = result.pages_with_text > 0 or result.pages == 0
        else:
            result.ocr_ok = (result.pages_with_text
                             >= result.pages - result.blank_pages)


# --------------------------------------------------------------------------- #
# Thống kê cây thư mục (số tài liệu / số trang theo từng thư mục)
# --------------------------------------------------------------------------- #

@dataclass
class FolderStats:
    root: str
    total_docs: int = 0
    total_pages: int = 0
    # rel posix của thư mục ("": gốc) → (số tài liệu, số trang) toàn subtree.
    dir_stats: dict[str, tuple[int, int]] = field(default_factory=dict)
    # rel posix của thư mục → tổng dung lượng subtree (byte, chỉ file PDF).
    dir_sizes: dict[str, int] = field(default_factory=dict)
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
    """Đếm tài liệu (PDF) + trang theo subtree cho từng thư mục dưới root.

    ``progress_cb(docs_so_far, pdf_path)`` báo tiến độ; ``cancel_cb`` trả
    True để dừng giữa chừng.
    """
    stats = FolderStats(root=str(root))
    docs_seen = 0

    def walk(path: Path, rel: str) -> tuple[int, int]:
        nonlocal docs_seen
        docs = 0
        pages = 0
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
        stats.dir_stats[rel] = (docs, pages)
        return docs, pages

    stats.total_docs, stats.total_pages = walk(root, "")
    return stats
