from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scanindex.ui.screens.secret_file_scan_screen import (
    EXCEL_HEADERS,
    SecretScanMatch,
    _declass_cell_text,
    _org_code_from_filename,
    _org_name_lookup,
    export_matches_to_excel,
)
from scanindex.infra.version import get_version


def _match(
    keyword: str = "Mật",
    page: int = 1,
    issue_year: int = 0,
    declass_years: int = 0,
    declass_due: bool = False,
    source_version: str = "",
    source_path: str = r"D:\tailieu\hocbao\congvan_01.pdf",
) -> SecretScanMatch:
    return SecretScanMatch(
        source_path=source_path,
        relative_path=os.path.join("hocbao", os.path.basename(source_path)),
        keyword=keyword,
        page_number=page,
        mode="Trang đầu",
        ocr_pdf_path="",
        note="PDF gốc; trang đầu",
        issue_year=issue_year,
        declass_years=declass_years,
        declass_due=declass_due,
        source_version=source_version,
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
    assert EXCEL_HEADERS[3] == "Mã cơ quan"
    assert EXCEL_HEADERS[4] == "Tên cơ quan"
    assert EXCEL_HEADERS[9] == "Đáp ứng giải mật"
    assert EXCEL_HEADERS[10] == "Ghi chú"

    assert ws.cell(row=2, column=1).value == 1
    assert ws.cell(row=2, column=2).value == "Mật"
    assert ws.cell(row=2, column=3).value == "congvan_01.pdf"
    # Tên file không dạng <uuid>_<mã>-… → 2 cột mã/tên cơ quan trống.
    assert ws.cell(row=2, column=4).value in ("", None)
    assert ws.cell(row=2, column=5).value in ("", None)
    assert os.path.basename(ws.cell(row=2, column=6).value) == "congvan_01.pdf"
    assert ws.cell(row=2, column=7).value == r"D:\tailieu\hocbao\congvan_01.pdf"
    assert ws.cell(row=2, column=8).value == 1
    assert ws.cell(row=2, column=9).value == "Trang đầu"
    # Chưa đạt giải mật → ô trống.
    assert ws.cell(row=2, column=10).value in ("", None)
    assert ws.cell(row=2, column=11).value == "PDF gốc; trang đầu"

    assert ws.cell(row=3, column=1).value == 2
    assert ws.cell(row=3, column=2).value == "Tuyệt mật"
    assert ws.cell(row=3, column=8).value == 2


def test_declass_cell_text_two_states_only() -> None:
    current = get_version()
    due = _match(issue_year=2016, declass_years=10, declass_due=True,
                 source_version=current)
    assert _declass_cell_text(due, current) == "Đáp ứng (2016, 10 năm)"
    # Mọi trường hợp khác → trống (chưa đạt).
    not_due = _match(issue_year=2025, declass_years=20, source_version=current)
    assert _declass_cell_text(not_due, current) == ""
    inherited = _match()  # không năm, không source_version
    assert _declass_cell_text(inherited, current) == ""
    fresh_no_year = _match(source_version=current)
    assert _declass_cell_text(fresh_no_year, current) == ""


# Ví dụ của người dùng: mã P9 (không có tên) và A29.183 (Đảng ủy Xã Hưng Long).
_UUID_A29 = (
    r"D:\kho\39cfea63-af46-460f-8c1c-340ab5e3c6af_"
    r"A29.183-A29.37.28.001-04-0010-082.pdf"
)
_UUID_P9 = (
    r"D:\kho\4bded843-8b83-4d68-a3de-32973498177e_P9-16-04-10.pdf"
)


def test_org_code_from_filename() -> None:
    assert _org_code_from_filename(_UUID_A29) == "A29.183"
    assert _org_code_from_filename(_UUID_P9) == "P9"
    # Không dạng uuid → trống.
    assert _org_code_from_filename(r"D:\kho\congvan_01.pdf") == ""
    assert _org_code_from_filename("") == ""


def test_org_name_lookup_examples() -> None:
    lookup = _org_name_lookup()
    assert lookup.get("A29.183") == "Đảng ủy Xã Hưng Long"
    # P9 không có trong bảng → tra không ra (cột Tên cơ quan trống).
    assert "P9" not in lookup


def test_export_writes_org_columns(tmp_path) -> None:
    import openpyxl

    dest = str(tmp_path / "danh_sach.xlsx")
    m1 = _match(source_path=_UUID_A29)
    m2 = _match(keyword="Tuyệt mật", source_path=_UUID_P9)
    export_matches_to_excel([m1, m2], dest)

    ws = openpyxl.load_workbook(dest).active
    assert ws.cell(row=2, column=4).value == "A29.183"
    assert ws.cell(row=2, column=5).value == "Đảng ủy Xã Hưng Long"
    assert ws.cell(row=3, column=4).value == "P9"
    assert ws.cell(row=3, column=5).value in ("", None)


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
