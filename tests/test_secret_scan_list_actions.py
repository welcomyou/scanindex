"""Tests cho phần nâng cấp danh sách file mật: dấu "không phải mật"
(NotSecretMarks), xóa vĩnh viễn, load/xuất Excel round-trip và hành vi
bảng (checkbox, tổng đếm, bỏ dòng theo file)."""
from __future__ import annotations

import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

pytest.importorskip("openpyxl")
pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from scanindex.core import secret_scan_progress as ssp  # noqa: E402
from scanindex.ui.screens.secret_file_scan_screen import (  # noqa: E402
    SecretFileScanScreen,
    SecretScanMatch,
    _permanent_delete,
    export_matches_to_excel,
    load_secret_matches_from_excel,
)


@pytest.fixture(autouse=True)
def _progress_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ssp, "progress_dir", lambda: str(tmp_path / "scan_progress")
    )
    return tmp_path / "scan_progress"


@pytest.fixture(autouse=True)
def _vietnamese_ui():
    from scanindex.infra import translations

    translations.current_locale.set_language("vi")
    yield
    translations.current_locale.set_language("en")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _match(path: str, keyword: str = "Mật", page: int = 1) -> SecretScanMatch:
    return SecretScanMatch(
        source_path=str(path),
        relative_path=os.path.basename(str(path)),
        keyword=keyword,
        page_number=page,
        mode="fast",
        ocr_pdf_path="",
        note="trang đầu",
    )


def _mkfile(tmp_path, name: str = "a.pdf", size: int = 8) -> str:
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return str(p)


# ---------------------------------------------------------------------------
# NotSecretMarks
# ---------------------------------------------------------------------------

def test_mark_blocks_row_and_survives_reload(tmp_path) -> None:
    path = _mkfile(tmp_path)
    marks = ssp.NotSecretMarks.load()
    assert not marks.is_marked(path)
    assert marks.mark(path)

    reloaded = ssp.NotSecretMarks.load()
    assert reloaded.is_marked(path)


def test_mark_invalid_when_file_changes(tmp_path) -> None:
    path = _mkfile(tmp_path)
    marks = ssp.NotSecretMarks.load()
    marks.mark(path)
    assert marks.is_marked(path)

    # File đổi nội dung (size) → dấu hết hiệu lực, dòng sẽ hiện lại.
    other = _mkfile(tmp_path, "a.pdf", size=99)
    assert other == path
    assert not marks.is_marked(path)


def test_mark_missing_file_returns_false(tmp_path) -> None:
    marks = ssp.NotSecretMarks.load()
    assert not marks.mark(str(tmp_path / "khong-ton-tai.pdf"))


def test_marks_survive_clear_all_and_prune(tmp_path) -> None:
    path = _mkfile(tmp_path)
    marks = ssp.NotSecretMarks.load()
    marks.mark(path)

    ssp.clear_all()  # nút "Xóa lịch sử quét"
    ssp.prune_stale(max_age_days=0)  # quét dọn journal cũ nhất

    assert ssp.NotSecretMarks.load().is_marked(path)


def test_marks_bad_json_is_ignored(tmp_path) -> None:
    os.makedirs(str(tmp_path / "scan_progress"), exist_ok=True)
    (tmp_path / "scan_progress" / "not_secret_marks.json").write_text(
        "{khong phai json", encoding="utf-8"
    )
    marks = ssp.NotSecretMarks.load()
    assert not marks.is_marked(str(tmp_path / "a.pdf"))


# ---------------------------------------------------------------------------
# Xóa vĩnh viễn
# ---------------------------------------------------------------------------

def test_permanent_delete_plain_file(tmp_path) -> None:
    path = _mkfile(tmp_path)
    _permanent_delete(path)
    assert not os.path.exists(path)


def test_permanent_delete_read_only_file(tmp_path) -> None:
    path = _mkfile(tmp_path)
    os.chmod(path, stat.S_IREAD)
    _permanent_delete(path)
    assert not os.path.exists(path)


def test_permanent_delete_missing_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        _permanent_delete(str(tmp_path / "khong-ton-tai.pdf"))


# ---------------------------------------------------------------------------
# Excel round-trip
# ---------------------------------------------------------------------------

def test_excel_export_load_roundtrip(tmp_path) -> None:
    m1 = SecretScanMatch(
        source_path=r"D:\kho\hop1\bi-mat-1.pdf",
        relative_path=r"hop1\bi-mat-1.pdf",
        keyword="TỐI MẬT",
        page_number=3,
        mode="thorough",
        ocr_pdf_path="",
        note="dấu đóng góc phải",
    )
    m2 = SecretScanMatch(
        source_path=r"D:\kho\hop2\bi-mat-2.docx",
        relative_path=r"hop2\bi-mat-2.docx",
        keyword="MẬT",
        page_number=1,
        mode="fast",
        ocr_pdf_path="",
        note="",
    )
    dest = str(tmp_path / "danhsach.xlsx")
    export_matches_to_excel([m1, m2], dest)

    loaded = load_secret_matches_from_excel(dest)
    assert len(loaded) == 2
    assert [m.source_path for m in loaded] == [m1.source_path, m2.source_path]
    assert [m.keyword for m in loaded] == ["TỐI MẬT", "MẬT"]
    assert [m.page_number for m in loaded] == [3, 1]
    assert [m.mode for m in loaded] == ["thorough", "fast"]
    assert [m.note for m in loaded] == ["dấu đóng góc phải", ""]


def test_load_rejects_foreign_excel(tmp_path) -> None:
    import openpyxl

    dest = str(tmp_path / "sai.xlsx")
    wb = openpyxl.Workbook()
    wb.active["A1"] = "Báo cáo gì đó"
    wb.save(dest)
    with pytest.raises(ValueError):
        load_secret_matches_from_excel(dest)


# ---------------------------------------------------------------------------
# Hành vi bảng trên screen (offscreen)
# ---------------------------------------------------------------------------

def _screen(qapp) -> SecretFileScanScreen:
    return SecretFileScanScreen()


def test_add_result_marks_filter_and_totals(qapp, tmp_path) -> None:
    screen = _screen(qapp)
    path = _mkfile(tmp_path)

    screen._add_result(_match(path))
    screen._add_result(_match(path, keyword="TỐI MẬT", page=2))
    assert screen.table.rowCount() == 2
    # 1 file, 2 dòng → nhãn tổng phải nêu đủ hai con số.
    assert screen.lbl_total.text() == "Tổng: 1 văn bản mật (2 dòng)"

    # Xác nhận "không phải mật" cho file → mọi dòng của file biến mất.
    screen._not_secret.mark(path)
    screen._remove_file_rows(path)
    assert screen.table.rowCount() == 0
    assert screen.lbl_total.text() == "Tổng: 0 văn bản mật"

    # Cả 3 nguồn đều chảy qua _add_result: dòng của file đã mark bị chặn.
    screen._add_result(_match(path, page=5))
    assert screen.table.rowCount() == 0

    # File khác thì vẫn hiện bình thường.
    other = _mkfile(tmp_path, "b.pdf")
    screen._add_result(_match(other))
    assert screen.table.rowCount() == 1

    # Chiều Anh của cặp mục song ngữ cũng phải render đúng số.
    from scanindex.infra import translations

    translations.current_locale.set_language("en")
    screen._refresh_totals()
    assert screen.lbl_total.text() == "Total: 1 classified documents"
    translations.current_locale.set_language("vi")


def test_checkbox_updates_checked_paths_and_buttons(qapp, tmp_path) -> None:
    screen = _screen(qapp)
    path = _mkfile(tmp_path)
    screen._add_result(_match(path))

    assert not screen.btn_batch_delete.isEnabled()
    item = screen.table.item(0, 0)
    item.setCheckState(__import__("PySide6").QtCore.Qt.CheckState.Checked)
    assert screen._checked_paths == {os.path.normpath(os.path.abspath(path))}
    assert screen.btn_batch_delete.isEnabled()
    assert screen.btn_batch_not_secret.isEnabled()

    item.setCheckState(__import__("PySide6").QtCore.Qt.CheckState.Unchecked)
    assert not screen._checked_paths
    assert not screen.btn_batch_delete.isEnabled()


def test_busy_disables_action_buttons(qapp, tmp_path) -> None:
    screen = _screen(qapp)
    path = _mkfile(tmp_path)
    screen._add_result(_match(path))
    screen._checked_paths.add(os.path.normpath(os.path.abspath(path)))

    screen._busy = True
    screen._refresh_action_buttons()
    assert not screen.btn_batch_delete.isEnabled()
    assert not screen.btn_batch_not_secret.isEnabled()

    screen._busy = False
    screen._refresh_action_buttons()
    assert screen.btn_batch_delete.isEnabled()


def test_delete_files_removes_rows_and_disk_file(qapp, tmp_path, monkeypatch) -> None:
    screen = _screen(qapp)
    p1 = _mkfile(tmp_path, "a.pdf")
    p2 = _mkfile(tmp_path, "b.pdf")
    screen._add_result(_match(p1))
    screen._add_result(_match(p2))

    monkeypatch.setattr(
        SecretFileScanScreen, "_confirm_paths", lambda *a, **k: True
    )
    screen._delete_files([p1])

    assert not os.path.exists(p1)
    assert os.path.exists(p2)
    assert screen.table.rowCount() == 1
    assert screen._row_path(0) == p2


def test_delete_files_keeps_rows_of_failed_files(qapp, tmp_path, monkeypatch) -> None:
    screen = _screen(qapp)
    p1 = _mkfile(tmp_path, "a.pdf")
    missing = str(tmp_path / "da-mat.pdf")
    screen._add_result(_match(p1))
    screen._add_result(_match(missing))

    monkeypatch.setattr(
        SecretFileScanScreen, "_confirm_paths", lambda *a, **k: True
    )
    monkeypatch.setattr(
        SecretFileScanScreen, "_show_path_errors", lambda *a, **k: None
    )
    # File "missing" không tồn tại → lỗi FileNotFoundError, dòng giữ nguyên.
    screen._delete_files([p1, missing])

    assert not os.path.exists(p1)
    assert screen.table.rowCount() == 1
    assert screen._row_path(0) == missing


def test_mark_not_secret_via_batch(qapp, tmp_path, monkeypatch) -> None:
    screen = _screen(qapp)
    p1 = _mkfile(tmp_path, "a.pdf")
    screen._add_result(_match(p1))
    screen._add_result(_match(p1, page=2))

    monkeypatch.setattr(
        SecretFileScanScreen, "_confirm_paths", lambda *a, **k: True
    )
    screen._mark_not_secret([p1])

    # File trên đĩa giữ nguyên; mọi dòng của file bị bỏ; mark được ghi.
    assert os.path.exists(p1)
    assert screen.table.rowCount() == 0
    assert ssp.NotSecretMarks.load().is_marked(p1)


def test_load_from_excel_into_table(qapp, tmp_path, monkeypatch) -> None:
    m = _match(_mkfile(tmp_path, "a.pdf"), keyword="MẬT", page=4)
    xlsx = str(tmp_path / "danhsach.xlsx")
    export_matches_to_excel([m], xlsx)

    matches = load_secret_matches_from_excel(xlsx)
    screen = _screen(qapp)
    for x in matches:
        screen._add_result(x)
    assert screen.table.rowCount() == 1
    assert screen._row_path(0) == m.source_path


def test_derive_scan_root() -> None:
    from scanindex.ui.screens.secret_file_scan_screen import _derive_scan_root

    m = SecretScanMatch(
        source_path=r"D:\kho\hop1\bi-mat.pdf",
        relative_path=r"hop1\bi-mat.pdf",
        keyword="MẬT", page_number=1, mode="fast", ocr_pdf_path="", note="",
    )
    assert _derive_scan_root([m]) == r"D:\kho"

    # File nằm ngay ở gốc thư mục quét.
    m2 = SecretScanMatch(
        source_path=r"D:\kho\bi-mat.pdf",
        relative_path="bi-mat.pdf",
        keyword="MẬT", page_number=1, mode="fast", ocr_pdf_path="", note="",
    )
    assert _derive_scan_root([m2]) == r"D:\kho"

    # Thiếu đường dẫn quan hệ (rel == full) → không suy được.
    m3 = SecretScanMatch(
        source_path=r"D:\kho\bi-mat.pdf",
        relative_path=r"D:\kho\bi-mat.pdf",
        keyword="MẬT", page_number=1, mode="fast", ocr_pdf_path="", note="",
    )
    assert _derive_scan_root([m3]) == ""
