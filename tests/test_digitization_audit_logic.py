"""Regression checks for digitization audit conclusions."""
from __future__ import annotations

import threading

import fitz

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
