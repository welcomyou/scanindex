"""Tests cho 2 nâng cấp màn hình quét file mật:

- Ghi chú "Đáp ứng thời gian giải mật" theo năm ban hành (regex dòng
  ngày-tháng ở trang đầu canonical) và tô xanh ở bảng/Excel.
- Phím mũi tên đổi dòng chọn → panel xem trước phải theo file đó.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

pytest.importorskip("openpyxl")
pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from scanindex.ui.screens import secret_file_scan_screen as sfss  # noqa: E402
from scanindex.ui.screens.secret_file_scan_screen import (  # noqa: E402
    DECLASS_NOTE_LABEL,
    SecretScanMatch,
    _apply_declassification_notes,
    _extract_issue_year,
    export_matches_to_excel,
)


def _canonical(lines: list[str]) -> dict:
    return {
        "pages": [
            {"page_index": 0, "lines": [{"text": t} for t in lines]},
        ]
    }


def _match(path: str = "D:/kho/a.pdf", keyword: str = "MẬT") -> SecretScanMatch:
    return SecretScanMatch(
        source_path=path,
        relative_path=os.path.basename(path),
        keyword=keyword,
        page_number=1,
        mode="Trang đầu",
        ocr_pdf_path="",
        note="PDF gốc; trang đầu",
    )


# ---------------------------------------------------------------------------
# Năm ban hành
# ---------------------------------------------------------------------------

def test_extract_issue_year_standard_line() -> None:
    canon = _canonical([
        "THÀNH PHỐ HỒ CHÍ MINH, ngày 04 tháng 4 năm 2016",
        "Số: 114-KH/TU",
    ])
    assert _extract_issue_year(canon) == 2016


def test_extract_issue_year_survives_ocr_junk() -> None:
    assert _extract_issue_year(_canonical(["ngay 15. thang 06 nam 2014"])) == 2014
    assert _extract_issue_year(_canonical(["NGÀY:9/THÁNG:1/NĂM:2020"])) == 2020


def test_extract_issue_year_missing_or_invalid() -> None:
    assert _extract_issue_year(_canonical(["Trích yếu thông báo"])) == 0
    # Ngày/tháng vô lý → bỏ qua, không lấy năm.
    assert _extract_issue_year(_canonical(["ngày 40 tháng 13 năm 2020"])) == 0


def test_extract_issue_year_uses_page_zero() -> None:
    canon = {
        "pages": [
            {
                "page_index": 0,
                "lines": [{"text": "ngày 01 tháng 1 năm 2010"}],
            },
            {
                "page_index": 1,
                "lines": [{"text": "ngày 01 tháng 1 năm 1990"}],
            },
        ]
    }
    assert _extract_issue_year(canon) == 2010


def test_extract_issue_year_prefers_topmost_short_line() -> None:
    """Trích yếu chứa ngày văn bản dẫn đứng TRƯỚC trong thứ tự dòng nhưng
    nằm giữa trang — phải lấy dòng ngày ban hành ngắn gần đầu trang."""
    canon = {
        "pages": [
            {
                "page_index": 0,
                "width": 595.0,
                "height": 842.0,
                "lines": [
                    {
                        "text": (
                            "V/v thực hiện Thông tư số 12/2020/TT-BNV ngày 30 "
                            "tháng 12 năm 2020 của Bộ Nội vụ về công tác văn thư"
                        ),
                        "bbox": [60, 300, 540, 340],
                    },
                    {
                        "text": "Hà Nội, ngày 04 tháng 4 năm 2016",
                        "bbox": [330, 95, 540, 118],
                    },
                ],
            }
        ]
    }
    assert _extract_issue_year(canon) == 2016


def test_extract_issue_year_long_line_only_returns_zero() -> None:
    canon = {
        "pages": [
            {
                "page_index": 0,
                "lines": [
                {
                    "text": (
                        "Kế hoạch ngày 30 tháng 12 năm 2020 về việc triển khai "
                        "nhiệm vụ phát triển kinh tế - xã hội và đảm bảo an ninh "
                        "trật tự năm 2021"
                    ),
                    "bbox": [60, 200, 540, 240],
                }
                ],
            }
        ]
    }
    assert _extract_issue_year(canon) == 0


# ---------------------------------------------------------------------------
# Ghi chú giải mật
# ---------------------------------------------------------------------------

def _freeze_year(monkeypatch, year: int) -> None:
    import time as time_mod

    fake = time_mod.struct_time((year, 6, 1, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(time_mod, "localtime", lambda: fake)


def test_declass_mat_after_10_years(monkeypatch) -> None:
    _freeze_year(monkeypatch, 2026)
    match = _match(keyword="MẬT")
    _apply_declassification_notes([match], _canonical(["ngày 4 tháng 4 năm 2016"]))
    assert match.issue_year == 2016
    assert match.declass_years == 10
    assert match.declass_due is True
    assert DECLASS_NOTE_LABEL in match.note
    assert match.note.startswith("PDF gốc; trang đầu; ")


def test_declass_toi_mat_not_due_after_10_years(monkeypatch) -> None:
    _freeze_year(monkeypatch, 2026)
    match = _match(keyword="TỐI MẬT")
    _apply_declassification_notes([match], _canonical(["ngày 4 tháng 4 năm 2016"]))
    # 10 năm < 20 năm của TỐI MẬT → chưa giải mật, ghi chú giữ nguyên.
    assert match.declass_due is False
    assert DECLASS_NOTE_LABEL not in match.note
    assert match.issue_year == 2016
    assert match.declass_years == 20


def test_declass_tuyet_mat_after_30_years(monkeypatch) -> None:
    _freeze_year(monkeypatch, 2026)
    match = _match(keyword="TUYỆT MẬT")
    _apply_declassification_notes([match], _canonical(["ngày 1 tháng 1 năm 1996"]))
    assert match.declass_due is True
    assert match.declass_years == 30


def test_declass_skipped_without_issue_year(monkeypatch) -> None:
    _freeze_year(monkeypatch, 2026)
    match = _match(keyword="MẬT")
    _apply_declassification_notes([match], _canonical(["không có ngày tháng"]))
    assert match.issue_year == 0
    assert match.declass_due is False
    assert match.note == "PDF gốc; trang đầu"


def test_declass_future_year_ignored(monkeypatch) -> None:
    _freeze_year(monkeypatch, 2026)
    match = _match(keyword="MẬT")
    _apply_declassification_notes([match], _canonical(["ngày 1 tháng 1 năm 2030"]))
    assert match.issue_year == 0


def test_declass_multi_doc_file_uses_match_page(monkeypatch) -> None:
    """File ghép nhiều văn bản: dấu mật ở trang 2 phải lấy ngày trang 2,
    không phải ngày của văn bản đầu ở trang 1."""
    _freeze_year(monkeypatch, 2026)
    canon = {
        "pages": [
            {
                "page_index": 0,
                "width": 595.0,
                "height": 842.0,
                "lines": [
                    {"text": "ngày 01 tháng 1 năm 2010", "bbox": [330, 95, 540, 115]}
                ],
            },
            {
                "page_index": 1,
                "width": 595.0,
                "height": 842.0,
                "lines": [
                    {"text": "ngày 01 tháng 1 năm 1996", "bbox": [330, 95, 540, 115]}
                ],
            },
        ]
    }
    match = _match(keyword="TUYỆT MẬT")
    match.page_number = 2
    _apply_declassification_notes([match], canon)
    assert match.issue_year == 1996
    assert match.declass_due is True  # 2026 - 1996 = 30 năm TUYỆT MẬT


# ---------------------------------------------------------------------------
# Cascade KIE lấy năm (regex trước, >2 ứng viên mới gọi LayoutLM PLACE_DATE)
# ---------------------------------------------------------------------------

def _page_with_dates(years_y: list[tuple[int, int]]) -> dict:
    """Trang với các dòng ngày: (y, năm)."""
    return {
        "page_index": 0,
        "width": 595.0,
        "height": 842.0,
        "lines": [
            {"text": f"Hà Nội, ngày 0{1 + i} tháng 1 năm {year}", "bbox": [330, y, 540, y + 20]}
            for i, (y, year) in enumerate(years_y)
        ],
    }


def test_declass_kie_called_when_over_2_candidates(monkeypatch, tmp_path) -> None:
    _freeze_year(monkeypatch, 2026)
    canon = {"pages": [_page_with_dates([(95, 2016), (300, 2010), (500, 2004)])]}
    json_path = str(tmp_path / "ocr.json.zst")
    open(json_path, "wb").close()  # chỉ cần file tồn tại; KIE bị mock
    calls = []

    def fake_kie(path, page_index):
        calls.append((path, page_index))
        return 2004  # KIE chọn dòng "biên bản họp..." thay dòng trên cùng

    monkeypatch.setattr(sfss, "_kie_issue_year", fake_kie)
    match = _match(keyword="MẬT")
    _apply_declassification_notes([match], canon, canonical_json_path=json_path)

    assert calls == [(json_path, 0)]
    assert match.issue_year == 2004  # năm theo KIE
    assert match.declass_due is True  # 2026 - 2004 ≥ 10 năm MẬT


def test_declass_kie_not_called_with_2_or_fewer_candidates(monkeypatch, tmp_path) -> None:
    _freeze_year(monkeypatch, 2026)
    canon = {"pages": [_page_with_dates([(95, 2016), (500, 2004)])]}
    json_path = str(tmp_path / "ocr.json.zst")
    open(json_path, "wb").close()

    def fake_kie(path, page_index):
        raise AssertionError("KIE phải không được gọi khi ≤ 2 ứng viên")

    monkeypatch.setattr(sfss, "_kie_issue_year", fake_kie)
    match = _match(keyword="MẬT")
    _apply_declassification_notes([match], canon, canonical_json_path=json_path)
    assert match.issue_year == 2016  # regex dòng trên cùng, không cần KIE


def test_declass_kie_failure_falls_back_to_regex(monkeypatch, tmp_path) -> None:
    _freeze_year(monkeypatch, 2026)
    canon = {"pages": [_page_with_dates([(95, 2016), (300, 2010), (500, 2004)])]}
    json_path = str(tmp_path / "ocr.json.zst")
    open(json_path, "wb").close()
    monkeypatch.setattr(sfss, "_kie_issue_year", lambda p, i: None)

    match = _match(keyword="TỐI MẬT")
    _apply_declassification_notes([match], canon, canonical_json_path=json_path)
    # Model lỗi/không có nhãn → giữ năm regex (dòng trên cùng).
    assert match.issue_year == 2016
    assert match.declass_due is False


def test_declass_kie_disabled_by_env(monkeypatch, tmp_path) -> None:
    _freeze_year(monkeypatch, 2026)
    monkeypatch.setenv("SECRET_SCAN_DISABLE_KIE_YEAR", "1")
    canon = {"pages": [_page_with_dates([(95, 2016), (300, 2010), (500, 2004)])]}
    json_path = str(tmp_path / "ocr.json.zst")
    open(json_path, "wb").close()

    def fake_kie(path, page_index):
        raise AssertionError("KIE đã bị tắt qua SECRET_SCAN_DISABLE_KIE_YEAR")

    monkeypatch.setattr(sfss, "_kie_issue_year", fake_kie)
    match = _match(keyword="MẬT")
    _apply_declassification_notes([match], canon, canonical_json_path=json_path)
    assert match.issue_year == 2016


def test_declass_excel_note_cell_is_green(tmp_path) -> None:
    import openpyxl

    match = _match()
    match.declass_due = True
    match.note = f"{DECLASS_NOTE_LABEL} (văn bản 2016, MẬT 10 năm)"
    dest = str(tmp_path / "danh_sach.xlsx")
    export_matches_to_excel([match], dest)

    wb = openpyxl.load_workbook(dest)
    cell = wb.active.cell(row=2, column=8)
    assert str(cell.value).startswith(DECLASS_NOTE_LABEL)
    assert str(cell.font.color.rgb).endswith("1D7A34")


# ---------------------------------------------------------------------------
# Điều hướng bàn phím → preview
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def screen(qapp):
    from scanindex.ui.screens.secret_file_scan_screen import SecretFileScanScreen

    return SecretFileScanScreen()


def test_arrow_key_moves_preview_with_selection(qapp, screen, tmp_path) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    p1 = tmp_path / "a.pdf"
    p2 = tmp_path / "b.pdf"
    for p in (p1, p2):
        p.write_bytes(b"x")
    screen._add_result(_match(str(p1)))
    screen._add_result(_match(str(p2)))

    screen.table.setCurrentCell(0, 2)
    assert screen._preview_current is None  # chưa click/chọn gì trước đó

    QTest.keyPress(screen.table, Qt.Key.Key_Down)
    assert screen._preview_current is not None
    assert screen._preview_current.source_path == str(p2)

    QTest.keyPress(screen.table, Qt.Key.Key_Up)
    assert screen._preview_current.source_path == str(p1)


def test_add_result_colors_declass_note_green(qapp, screen) -> None:
    from PySide6.QtGui import QColor

    from scanindex.ui.theme import COLOR_GREEN

    match = _match()
    match.declass_due = True
    match.note = f"{DECLASS_NOTE_LABEL} (văn bản 2016, MẬT 10 năm)"
    screen._add_result(match)

    note_item = screen.table.item(0, 5)
    assert note_item is not None
    assert note_item.foreground().color() == QColor(COLOR_GREEN)
    # Cột khác giữ màu bình thường.
    file_item = screen.table.item(0, 2)
    assert file_item.foreground().color() != QColor(COLOR_GREEN)


def test_add_result_loaded_note_without_flag_still_green(qapp, screen) -> None:
    """Dòng load từ Excel không có cờ → nhận diện qua chuỗi ghi chú."""
    from PySide6.QtGui import QColor

    from scanindex.ui.theme import COLOR_GREEN

    match = _match()
    match.note = f"x; {DECLASS_NOTE_LABEL} (văn bản 2015, MẬT 10 năm)"
    screen._add_result(match)
    assert screen.table.item(0, 5).foreground().color() == QColor(COLOR_GREEN)
