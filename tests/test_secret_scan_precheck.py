"""Precheck file hỏng trong secret scan: phân loại đúng SKIP (vĩnh viễn)
vs ERR (tạm thời, thử lại) — xem _precheck_source_readable."""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scanindex.ui.screens.secret_file_scan_screen import (  # noqa: E402
    _precheck_source_readable,
    _ScanSkip,
)


def _make_pdf(tmp_path, name: str, pages: int = 1) -> str:
    import fitz

    p = str(tmp_path / name)
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(p)
    doc.close()
    return p


def _make_zero_page_pdf(tmp_path, name: str) -> str:
    p = str(tmp_path / name)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [] /Count 0 >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    with open(p, "wb") as f:
        f.write(bytes(out))
    return p


def _backdate(path: str, seconds: int = 600) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


# ── Nhánh SKIP: rác vĩnh viễn, file cũ ────────────────────────────────────

def test_old_empty_file_is_skip(tmp_path):
    p = str(tmp_path / "empty.pdf")
    open(p, "wb").close()
    _backdate(p)
    with pytest.raises(_ScanSkip, match="File rỗng"):
        _precheck_source_readable(p)


def test_old_corrupt_content_is_skip(tmp_path):
    # Mô phỏng file rác AppleDouble của macOS — nội dung không phải PDF.
    p = str(tmp_path / "uuid_._rac.pdf")
    with open(p, "wb") as f:
        f.write(b"\x00\x05\x16\x07\x00\x02\x00\x00junk" * 10)
    _backdate(p)
    with pytest.raises(_ScanSkip, match="Không phải PDF hợp lệ"):
        _precheck_source_readable(p)


def test_old_zero_page_pdf_is_skip(tmp_path):
    p = _make_zero_page_pdf(tmp_path, "nopages.pdf")
    _backdate(p)
    with pytest.raises(_ScanSkip, match="PDF không có trang"):
        _precheck_source_readable(p)


def test_valid_old_pdf_passes(tmp_path):
    p = _make_pdf(tmp_path, "ok.pdf")
    _backdate(p)
    _precheck_source_readable(p)  # không raise


# ── Nhánh ERR: tạm thời — phải để ngoại lệ gốc bọt lên, KHÔNG skip ────────

def test_oserror_is_retryable_not_skip(tmp_path, monkeypatch):
    """Mất quyền/mạng (OSError) là tạm thời → propagate cho process_one ghi
    'err' và resume thử lại; không được chuyển thành skip vĩnh viễn."""
    p = str(tmp_path / "locked.pdf")

    def denied(*args, **kwargs):
        raise PermissionError(32, "Quyền truy cập bị từ chối")

    monkeypatch.setattr(os, "stat", denied)
    with pytest.raises(PermissionError):
        _precheck_source_readable(p)
    # Đặc biệt KHÔNG được raise _ScanSkip (kiểm tra bằng cách đảo mệnh đề:
    # nếu hàm đổi thành _ScanSkip thì pytest.raises(PermissionError) đã fail).


def test_missing_file_is_retryable_not_skip(tmp_path):
    """File biến mất giữa chừng (đang dời/đổi tên) → tạm thời, không skip."""
    p = str(tmp_path / "vanishing.pdf")
    with pytest.raises(OSError):
        _precheck_source_readable(p)


def test_fresh_empty_file_is_retryable_not_skip(tmp_path):
    """File 0 byte nhưng vừa xuất hiện → có thể đang copy dở → RuntimeError
    (ghi 'err', thử lại) thay vì skip vĩnh viễn."""
    p = str(tmp_path / "copying.pdf")
    open(p, "wb").close()  # mtime = bây giờ
    with pytest.raises(RuntimeError, match="chưa ổn định"):
        _precheck_source_readable(p)


def test_fresh_corrupt_content_is_retryable_not_skip(tmp_path):
    p = str(tmp_path / "fresh_rac.pdf")
    with open(p, "wb") as f:
        f.write(b"\x00\x05\x16\x07\x00\x02\x00\x00junk" * 10)
    with pytest.raises(RuntimeError, match="chưa ổn định"):
        _precheck_source_readable(p)


# ── Van điều chỉnh số file-worker (nghi vấn PyMuPDF đa luồng) ─────────────

def test_file_worker_count_default_and_clamp(monkeypatch):
    from scanindex.ui.screens.secret_file_scan_screen import _file_worker_count

    monkeypatch.delenv("SECRET_SCAN_MAX_FILE_WORKERS", raising=False)
    assert _file_worker_count(10) == 2
    assert _file_worker_count(1) == 1
    monkeypatch.setenv("SECRET_SCAN_MAX_FILE_WORKERS", "1")
    assert _file_worker_count(10) == 1
    monkeypatch.setenv("SECRET_SCAN_MAX_FILE_WORKERS", "9")
    assert _file_worker_count(10) == 9
    monkeypatch.setenv("SECRET_SCAN_MAX_FILE_WORKERS", "abc")
    assert _file_worker_count(10) == 2
    monkeypatch.setenv("SECRET_SCAN_MAX_FILE_WORKERS", "0")
    assert _file_worker_count(10) == 1
