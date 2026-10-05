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
    for name in ("A38-11-07-0123-001.tif",
                 "A38-011-7-0123-001.tif",
                 "A38-011-07-123-001.tif",
                 "A38-011-07-0123-1.tif"):
        path = folder / name
        _save_tiff(path)
        assert audit.audit_tiff(path).name_ok is False


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
