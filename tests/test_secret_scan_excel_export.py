from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scanindex.ui.screens.secret_file_scan_screen import (
    EXCEL_HEADERS,
    SecretScanMatch,
    export_matches_to_excel,
)


def _match(keyword: str = "Mật", page: int = 1) -> SecretScanMatch:
    return SecretScanMatch(
        source_path=r"D:\tailieu\hocbao\congvan_01.pdf",
        relative_path=os.path.join("hocbao", "congvan_01.pdf"),
        keyword=keyword,
        page_number=page,
        mode="Trang đầu",
        ocr_pdf_path="",
        note="PDF gốc; trang đầu",
    )


def test_export_matches_to_excel_writes_all_fields(tmp_path) -> None:
    import openpyxl

    dest = str(tmp_path / "danh_sach.xlsx")
    matches = [_match("Mật", 1), _match("Tuyệt mật", 2)]
    export_matches_to_excel(matches, dest)

    wb = openpyxl.load_workbook(dest)
    ws = wb.active
    assert ws.title == "Van ban mat"
    headers = [ws.cell(row=1, column=c).value for c in range(1, len(EXCEL_HEADERS) + 1)]
    assert headers == EXCEL_HEADERS

    assert ws.cell(row=2, column=1).value == 1
    assert ws.cell(row=2, column=2).value == "Mật"
    assert ws.cell(row=2, column=3).value == "congvan_01.pdf"
    assert os.path.basename(ws.cell(row=2, column=4).value) == "congvan_01.pdf"
    assert ws.cell(row=2, column=5).value == r"D:\tailieu\hocbao\congvan_01.pdf"
    assert ws.cell(row=2, column=6).value == 1
    assert ws.cell(row=2, column=7).value == "Trang đầu"
    assert ws.cell(row=2, column=8).value == "PDF gốc; trang đầu"

    assert ws.cell(row=3, column=1).value == 2
    assert ws.cell(row=3, column=2).value == "Tuyệt mật"
    assert ws.cell(row=3, column=6).value == 2


def test_export_creates_parent_dirs(tmp_path) -> None:
    dest = str(tmp_path / "sub" / "dir" / "out.xlsx")
    export_matches_to_excel([_match()], dest)
    assert os.path.isfile(dest)


def test_export_empty_list_still_writes_header(tmp_path) -> None:
    import openpyxl

    dest = str(tmp_path / "empty.xlsx")
    export_matches_to_excel([], dest)
    ws = openpyxl.load_workbook(dest).active
    assert ws.max_row == 1
    assert ws.cell(row=1, column=1).value == "STT"
