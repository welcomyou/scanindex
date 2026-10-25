"""Regression checks for digitization audit conclusions."""
from __future__ import annotations

import threading

import fitz
from PIL import Image

from scanindex.core import digitization_audit as audit


def test_jpx_check_can_reenter_pdf_lock():
    class Doc:
        def xref_stream_raw(self, _xref):
            return b"unknown codestream"

    image = audit.ImageInfo(1, 100, 100, 300, 300, 8, 3,
                            "DeviceRGB", ["JPXDecode"], 1)
    result = []

    def inspect():
        with audit.FITZ_LOCK:
            result.append(audit._compression_note(Doc(), image))

    worker = threading.Thread(target=inspect, daemon=True)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive(), "JPX inspection deadlocked"
    assert result == [None]


def test_text_only_page_does_not_mask_scan_without_ocr(tmp_path):
    document = fitz.open()
    scanned = document.new_page()
    pixels = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100))
    pixels.set_rect(pixels.irect, (200, 30, 30))
    scanned.insert_image(scanned.rect, stream=pixels.tobytes("png"))
    text_page = document.new_page()
    text_page.insert_text((72, 100),
                          "A text-only page with enough searchable text.")
    path = tmp_path / "mixed.pdf"
    document.save(path)

    result = audit.audit_pdf(path)
    assert result.pages_with_text == 1
    assert result.pages_with_image == 1
    assert result.scanned_pages_with_text == 0
    assert result.ocr_ok is False


def _save_tiff(path):
    Image.new("RGB", (40, 40), (200, 30, 30)).save(path, dpi=(300, 300))


def test_tiff_pages_are_numbered_across_the_dossier(tmp_path):
    # Four documents may span twelve pages; last segment is the dossier page.
    folder = tmp_path / "A38-011-07-0123"
    folder.mkdir()
    for page in range(1, 13):
        _save_tiff(folder / f"A38-011-07-0123-{page:03d}.tif")

    first = audit.audit_tiff(folder / "A38-011-07-0123-001.tif")
    last = audit.audit_tiff(folder / "A38-011-07-0123-012.tif")
    assert first.name_ok is True and last.name_ok is True
    assert first.sequence_warning == last.sequence_warning == ""
    parsed = audit.parse_tiff_name("A38-011-07-0123-012.tif")
    assert (parsed.ma_dinh_danh, parsed.ma_phong, parsed.muc_luc,
            parsed.don_vi_bao_quan, parsed.trang) == (
        "A38", "011", "07", "0123", "012")
    assert audit.parse_tiff_name("011-07-0123-001.tif") is None


def test_tiff_name_accepts_duplicate_storage_unit_suffix(tmp_path):
    folder = tmp_path / "H42-011-07-0123a"
    folder.mkdir()
    path = folder / "H42-011-07-0123a-001.tif"
    _save_tiff(path)
    assert audit.audit_tiff(path).name_ok is True


def test_tiff_name_requires_padded_numbers(tmp_path):
    folder = tmp_path / "A38-011-07-0123"
    folder.mkdir()
    for name in ("A38-011-7-0123-001.tif",
                 "A38-011-07-123-001.tif",
                 "A38-011-07-0123-1.tif"):
        path = folder / name
        _save_tiff(path)
        assert audit.audit_tiff(path).name_ok is False


def test_tiff_name_accepts_free_form_fonds(tmp_path):
    # Phông tùy ý (chỉ cấm "-") thống nhất với quy ước PDF/CSDL_SOHOA —
    # mã phông phân cấp vd. A29.244.01.002 đạt; lệch mã phông với thư
    # mục cha vẫn trượt.
    folder = tmp_path / "A29.244.01-A29.244.01.002-01-0001"
    folder.mkdir()
    path = folder / "A29.244.01-A29.244.01.002-01-0001-001.tif"
    _save_tiff(path)
    assert audit.audit_tiff(path).name_ok is True
    parsed = audit.parse_tiff_name(path.name)
    assert parsed.ma_phong == "A29.244.01.002"

    wrong = folder / "A29.244.01-002-01-0001-001.tif"
    _save_tiff(wrong)
    result = audit.audit_tiff(wrong)
    assert result.name_ok is False
    assert "không khớp thư mục hồ sơ chứa" in result.name_note


def test_tiff_sequence_checks_first_page_missing_and_duplicates(tmp_path):
    folder = tmp_path / "A38-011-07-0123"
    folder.mkdir()
    third = folder / "A38-011-07-0123-003.tif"
    _save_tiff(third)
    assert "001" in audit.audit_tiff(third).sequence_warning
    assert "002" in audit.audit_tiff(third).sequence_warning

    duplicate = folder / "A38-011-07-0123-003.tiff"
    _save_tiff(duplicate)
    warning = audit.audit_tiff(third).sequence_warning
    assert "số trang bị trùng: 003" in warning
    assert "thiếu trang: 001, 002" in warning

    wrong_dossier = folder / "A38-011-07-0456-001.tif"
    _save_tiff(wrong_dossier)
    result = audit.audit_tiff(wrong_dossier)
    assert result.name_ok is False
    assert "không khớp thư mục" in result.name_note
    assert result.sequence_warning == ""


def test_tiff_page_zero_is_invalid(tmp_path):
    folder = tmp_path / "A38-011-07-0123"
    folder.mkdir()
    path = folder / "A38-011-07-0123-000.tif"
    _save_tiff(path)
    result = audit.audit_tiff(path)
    assert result.name_ok is False
    assert "001" in result.name_note


def test_folder_stats_count_dossiers_and_pages_separately(tmp_path):
    for dossier, pages in (("A38-011-07-0123", 3),
                           ("A38-011-07-0123a", 2)):
        folder = tmp_path / dossier
        folder.mkdir()
        for page in range(1, pages + 1):
            _save_tiff(folder / f"{dossier}-{page:03d}.tif")

    stats = audit.scan_tree_stats(tmp_path)
    assert stats.total_docs == 5  # five image files, not five documents
    assert stats.total_pdf_documents == 0
    assert stats.document_count_of_rel("A38-011-07-0123") == 0
    assert stats.total_pages == 5
    assert stats.total_size == sum(path.stat().st_size
                                   for path in tmp_path.rglob("*.tif"))
    assert stats.has_subfolders("")
    assert stats.dossier_count_of_rel("") == 2
    assert not stats.has_subfolders("A38-011-07-0123")
    assert stats.of_rel("A38-011-07-0123") == (3, 3)
    assert stats.size_of_rel("A38-011-07-0123") == sum(
        path.stat().st_size
        for path in (tmp_path / "A38-011-07-0123").glob("*.tif"))


def test_pdf_documents_are_counted_separately_from_tiff_pages(tmp_path):
    pdf_dossier = tmp_path / "A38-011-07-0123"
    tiff_dossier = tmp_path / "A38-011-07-0124"
    pdf_dossier.mkdir()
    tiff_dossier.mkdir()
    for order in (1, 2):
        document = fitz.open()
        document.new_page()
        document.save(pdf_dossier / f"A38-011-07-0123-{order:03d}.pdf")
        document.close()
    _save_tiff(tiff_dossier / "A38-011-07-0124-001.tif")

    stats = audit.scan_tree_stats(tmp_path)
    assert stats.dossier_count_of_rel("") == 2
    assert stats.total_docs == 3  # two PDFs and one page image
    assert stats.total_pdf_documents == 2
    assert stats.document_count_of_rel(pdf_dossier.name) == 2
    assert stats.document_count_of_rel(tiff_dossier.name) == 0
    assert stats.total_pages == 3

    single_dossier = audit.scan_tree_stats(pdf_dossier)
    assert single_dossier.dossier_count_of_rel("") == 1
    assert single_dossier.document_count_of_rel("") == 2


# -------------------------------------------------------------------------- #
# Ảnh dấu/mộc chữ ký không được tính vào thống kê chất lượng scan
# -------------------------------------------------------------------------- #

def test_is_signature_stamp_image_geometry():
    page_rect = fitz.Rect(0, 0, 842, 595)
    sig = [fitz.Rect(739.9, 10.0, 832.1, 81.3)]
    # ảnh dấu nằm trọn trong ô chữ ký → loại
    assert audit._is_signature_stamp_image((740.0, 10.5, 831.5, 80.5),
                                           sig, page_rect)
    # ảnh phủ gần hết trang → là nội dung scan, giữ lại
    assert not audit._is_signature_stamp_image((0, 0, 842, 595),
                                               sig, page_rect)
    # ảnh thường ngoài ô chữ ký → giữ lại
    assert not audit._is_signature_stamp_image((100, 100, 300, 300),
                                               sig, page_rect)
    # không có ô chữ ký trên trang → giữ lại
    assert not audit._is_signature_stamp_image((740.0, 10.5, 831.5, 80.5),
                                               [], page_rect)
    assert not audit._is_signature_stamp_image(None, sig, page_rect)


def _self_signed_cert_info():
    import datetime as _dt

    from cryptography import x509 as cx509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = cx509.Name([
        cx509.NameAttribute(NameOID.COMMON_NAME, "TEST SIGNER"),
        cx509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test Org"),
    ])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (cx509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(cx509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=365))
            .sign(key, hashes.SHA256()))
    return {
        "der": cert.public_bytes(serialization.Encoding.DER),
        "cn": "TEST SIGNER", "org": "Test Org", "ou": "Test OU",
        "subject": "CN=TEST SIGNER", "issuer": "CN=TEST SIGNER",
        "serial": "0A1B2C",
        "not_after": (now + _dt.timedelta(days=365)).date(),
    }


def test_signed_pdf_keeps_scan_dpi_verdict(tmp_path, monkeypatch):
    """File scan 300 dpi sau khi ký vẫn phải 'Đạt' 300 dpi.

    pyHanko chèn ảnh bitmap dấu (~287 dpi) vào appearance của ô chữ ký;
    MuPDF liệt kê ảnh này trong get_image_info như ảnh thường nên trước
    đây min_dpi tụt từ 300 xuống 287 → file đã ký bị 'Không đạt' oan."""
    from scanindex.core.pdf import signer as signer_mod

    document = fitz.open()
    page = document.new_page(width=100, height=141.4)
    pixels = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 417, 590))
    pixels.set_rect(pixels.irect, (235, 233, 228))
    page.insert_image(page.rect, stream=pixels.tobytes("png"))
    src = tmp_path / "scan300.pdf"
    document.save(src)
    document.close()

    result = audit.audit_pdf(src)
    assert result.dpi_ok is True
    assert result.stamp_images_skipped == 0

    monkeypatch.setattr(
        signer_mod, "sign_data",
        lambda cert_info, data, hash_alg="sha256": b"\x5a" * 256)
    signed = tmp_path / "scan300_signed.pdf"
    signer_mod.sign_single_pdf(
        str(src), str(signed), _self_signed_cert_info(),
        sig_box=(0, 0, 220, 60), page=0,
        stamp_template="Xác nhận ... {datetime}",
        avoid_text_overlap=True,
    )

    result = audit.audit_pdf(signed)
    assert result.signed is True
    assert result.stamp_images_skipped == 1
    assert len(result.images) == 1          # chỉ còn ảnh scan
    assert result.min_dpi >= audit.DPI_MIN - 1.0
    assert result.dpi_ok is True
    assert "ảnh dấu chữ ký" in result.dpi_detail()
