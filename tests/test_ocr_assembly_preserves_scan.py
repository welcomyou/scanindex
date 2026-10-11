# -*- coding: utf-8 -*-
"""Ghép PDF OCR (kèm ký số) phải giữ nguyên chất lượng ảnh scan gốc.

Regression cho yêu cầu "ký số kèm OCR không được làm thay đổi DPI/màu/nén
của file gốc":
- trang scan kèm text layer (scan_ocr_low) phải được dựng lại bằng CHÍNH
  stream ảnh gốc (JPEG giữ nguyên byte) thay vì rasterize re-encode;
- rasterize fallback (trang nhiều ảnh) phải render đúng DPI gốc, không kẹp
  300, và giữ thang xám cho scan grayscale.
"""
from __future__ import annotations

import hashlib
import io

import fitz
from PIL import Image

from scanindex.core.ocr.direct_engine import (
    _estimate_page_native_dpi,
    _page_has_text_layer,
    assemble_pdf_from_page_results,
)


def _jpeg_bytes(w_px: int, h_px: int, gray: bool = False,
                quality: int = 90) -> bytes:
    arr = Image.new("L" if gray else "RGB", (w_px, h_px), 235)
    buf = io.BytesIO()
    arr.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _scan_page_with_text(doc, w_px: int, h_px: int, gray: bool = False):
    """Trang A4-ly (200×283pt) chứa 1 ảnh JPEG + text layer ẩn."""
    page = doc.new_page(width=200, height=283)
    jpg = _jpeg_bytes(w_px, h_px, gray=gray)
    md5 = hashlib.md5(jpg).hexdigest()
    page.insert_image(page.rect, stream=jpg)
    page.insert_text((10, 20), "Trang scan thu nghiem text layer an",
                     fontsize=8, render_mode=3)
    return md5


def _first_image(path):
    doc = fitz.open(path)
    page = doc[0]
    imgs = page.get_images(full=True)
    assert imgs, "output page must keep the scan image"
    xref = imgs[0][0]
    raw = doc.xref_stream_raw(xref)
    rects = page.get_image_rects(xref)
    eff = ((imgs[0][2] / rects[0].width) + (imgs[0][3] / rects[0].height)) / 2 * 72
    filt = str(imgs[0][8]).split()            # "DCTDecode" hoặc "FlateDecode ..."
    info = (imgs[0][1], imgs[0][4], imgs[0][5], filt)  # smask, bpc, cs, filter
    doc.close()
    return raw, eff, info


def _assemble(src, tmp_path, name):
    out = str(tmp_path / name)
    ok, err = assemble_pdf_from_page_results(
        str(src), out, {}, source_document_path=str(src),
        include_layout_analysis=False)
    assert ok, f"assemble failed: {err}"
    return out


def test_scan_ocr_low_page_keeps_original_jpeg_verbatim(tmp_path):
    """600 dpi scan + text layer: ảnh JPEG gốc phải giữ nguyên byte + DPI."""
    doc = fitz.open()
    src_md5 = _scan_page_with_text(doc, 1667, 2360)  # 1667/200pt*72 = 600 dpi
    src = tmp_path / "scan600_ocrtext.pdf"
    doc.save(src)
    doc.close()

    check = fitz.open(src)
    assert _page_has_text_layer(check[0])          # fixture đúng dạng scan_ocr_low
    assert _estimate_page_native_dpi(check[0]) == 600  # không còn kẹp 300
    src_cs = check[0].get_images(full=True)[0][5]  # tên colorspace gốc
    check.close()

    out = _assemble(src, tmp_path, "out600.pdf")
    raw, eff_dpi, (smask, _bpc, cs, filt) = _first_image(out)
    assert hashlib.md5(raw).hexdigest() == src_md5, (
        "JPEG stream phải giữ nguyên byte (không re-encode)")
    assert abs(eff_dpi - 600) < 1.0, "DPI gốc 600 phải được giữ nguyên"
    assert smask == 0
    assert cs == src_cs, "colorspace phải giữ nguyên như bản gốc"
    assert filt == ["DCTDecode"]


def test_grayscale_scan_page_stays_grayscale(tmp_path):
    """Scan gray + text layer: ảnh đầu ra phải còn là thang xám."""
    doc = fitz.open()
    src_md5 = _scan_page_with_text(doc, 1667, 2358, gray=True)  # ~600 dpi
    src = tmp_path / "scan_gray_ocrtext.pdf"
    doc.save(src)
    doc.close()
    check = fitz.open(src)
    src_cs = check[0].get_images(full=True)[0][5]
    check.close()

    out = _assemble(src, tmp_path, "out_gray.pdf")
    raw, eff_dpi, (_smask, _bpc, cs, _filt) = _first_image(out)
    assert hashlib.md5(raw).hexdigest() == src_md5
    assert cs == src_cs, "colorspace phải giữ nguyên như bản gốc"
    # Kiểm chứng độc lập với tên colorspace: decode ảnh — scan gray phải
    # còn 1 kênh (n==1); nếu bị đổi RGB sẽ là 3.
    d = fitz.open(out)
    pix = fitz.Pixmap(d, d[0].get_images(full=True)[0][0])
    assert pix.n == 1, f"scan gray bị đổi màu: {pix.n} kênh"
    d.close()
    assert abs(eff_dpi - 600) < 2.0


def test_multi_image_text_page_rasterizes_at_native_dpi(tmp_path):
    """Trang nhiều ảnh (scan + logo) không dựng verbatim được → rasterize
    fallback nhưng vẫn render đúng DPI gốc (600), không kẹp 300."""
    doc = fitz.open()
    page = doc.new_page(width=200, height=283)
    page.insert_image(page.rect, stream=_jpeg_bytes(1667, 2360))
    logo = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40))
    logo.set_rect(logo.irect, (200, 30, 30))
    page.insert_image(fitz.Rect(10, 250, 40, 280), stream=logo.tobytes("png"))
    page.insert_text((10, 20), "scan nhieu anh text layer an",
                     fontsize=8, render_mode=3)
    src = tmp_path / "scan_multi.pdf"
    doc.save(src)
    doc.close()

    out = _assemble(src, tmp_path, "out_multi.pdf")
    _raw, eff_dpi, _info = _first_image(out)
    assert abs(eff_dpi - 600) < 6.0, f"rasterize phải đúng DPI gốc, thấy {eff_dpi:.0f}"
    # scan MÀU qua rasterize vẫn phải là RGB 3 kênh (không bị xám hóa)
    d = fitz.open(out)
    pix = fitz.Pixmap(d, d[0].get_images(full=True)[0][0])
    assert pix.n == 3, f"scan màu bị mất kênh: n={pix.n}"
    assert "RGB" in (pix.colorspace.name or ""), pix.colorspace.name
    d.close()


def test_mixed_document_each_page_preserved(tmp_path):
    """Hồ sơ trộn: trang scan sạch + trang scan kèm text — cả hai giữ DPI."""
    doc = fitz.open()
    _scan_page_with_text(doc, 1667, 2360)                    # trang 1: có text
    p2 = doc.new_page(width=200, height=283)                 # trang 2: scan sạch
    p2.insert_image(p2.rect, stream=_jpeg_bytes(833, 1180))  # 300 dpi
    src = tmp_path / "mixed.pdf"
    doc.save(src)
    doc.close()

    out = _assemble(src, tmp_path, "out_mixed.pdf")
    check = fitz.open(out)
    assert check.page_count == 2
    for i, expect_dpi in ((0, 600), (1, 300)):
        imgs = check[i].get_images(full=True)
        assert imgs, f"page {i} mất ảnh scan"
        rects = check[i].get_image_rects(imgs[0][0])
        eff = ((imgs[0][2] / rects[0].width) + (imgs[0][3] / rects[0].height)) / 2 * 72
        assert abs(eff - expect_dpi) < 2.0, (
            f"page {i}: DPI {eff:.0f} != {expect_dpi}")
    check.close()
