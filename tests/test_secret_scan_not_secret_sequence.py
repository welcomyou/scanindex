"""Repro: xác nhận "không phải mật" liên tiếp hai file.

Mô phỏng đúng thao tác người dùng: chọn file A → bấm "Không phải mật"
(file A bị loại khỏi bảng) → chọn file B → bấm "Không phải mật" lần nữa.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from scanindex.core import secret_scan_progress as ssp  # noqa: E402
from scanindex.ui.screens.secret_file_scan_screen import (  # noqa: E402
    SecretFileScanScreen,
    SecretScanMatch,
    _norm,
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


def _mkfile(tmp_path, name: str, size: int = 8) -> str:
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return str(p)


@pytest.fixture()
def screen(qapp):
    return SecretFileScanScreen()


@pytest.fixture()
def auto_confirm(monkeypatch):
    monkeypatch.setattr(
        SecretFileScanScreen, "_confirm_paths", lambda *a, **k: True
    )


def _click_not_secret(screen: SecretFileScanScreen) -> None:
    screen._preview_not_secret_clicked()


def test_confirm_not_secret_twice_in_a_row(qapp, screen, auto_confirm, tmp_path) -> None:
    p1 = _mkfile(tmp_path, "a.pdf")
    p2 = _mkfile(tmp_path, "b.pdf")
    screen._add_result(_match(p1))
    screen._add_result(_match(p2))

    # Lượt 1: chọn file A rồi xác nhận.
    screen._show_preview(0)
    assert screen._preview_current is not None
    assert screen._preview_current.source_path == p1
    _click_not_secret(screen)

    assert screen.table.rowCount() == 1, "file A phải bị bỏ khỏi bảng"
    assert screen._row_path(0) == p2, "dòng còn lại phải là file B"
    # Sau khi loại A, chương trình tự chuyển preview sang file B.
    assert screen._preview_current is not None, (
        "preview phải tự chuyển sang file kế tiếp để xác nhận tiếp"
    )
    assert screen._preview_current.source_path == p2

    # Lượt 2: xác nhận luôn file B (không cần chọn lại).
    _click_not_secret(screen)
    assert screen.table.rowCount() == 0, "file B phải bị bỏ khỏi bảng"
    assert ssp.NotSecretMarks.load().is_marked(p1)
    assert ssp.NotSecretMarks.load().is_marked(p2)


def test_confirm_not_secret_select_next_row_manually(
    qapp, screen, auto_confirm, tmp_path
) -> None:
    p1 = _mkfile(tmp_path, "a.pdf")
    p2 = _mkfile(tmp_path, "b.pdf")
    p3 = _mkfile(tmp_path, "c.pdf")
    screen._add_result(_match(p1))
    screen._add_result(_match(p2))
    screen._add_result(_match(p3))

    # Lượt 1: chọn dòng 0 (file A) và xác nhận.
    screen._show_preview(0)
    _click_not_secret(screen)

    assert screen.table.rowCount() == 2
    assert screen._row_path(0) == p2
    assert screen._row_path(1) == p3

    # Lượt 2: người dùng bấm chọn file B trong bảng rồi xác nhận.
    screen._show_preview(0)
    assert screen._preview_current.source_path == p2
    _click_not_secret(screen)

    assert screen.table.rowCount() == 1
    assert screen._row_path(0) == p3
    assert ssp.NotSecretMarks.load().is_marked(p2)


def test_confirm_not_secret_multirow_file_then_next(
    qapp, screen, auto_confirm, tmp_path
) -> None:
    """File A có 2 dòng (2 trang) đứng trước file B 1 dòng."""
    p1 = _mkfile(tmp_path, "a.pdf")
    p2 = _mkfile(tmp_path, "b.pdf")
    screen._add_result(_match(p1, page=1))
    screen._add_result(_match(p1, page=2))
    screen._add_result(_match(p2))

    screen._show_preview(0)
    _click_not_secret(screen)

    assert screen.table.rowCount() == 1
    assert screen._row_path(0) == p2
    assert screen._preview_current is not None
    assert screen._preview_current.source_path == p2

    _click_not_secret(screen)
    assert screen.table.rowCount() == 0
    assert ssp.NotSecretMarks.load().is_marked(p2)


def test_confirm_not_secret_last_row(qapp, screen, auto_confirm, tmp_path) -> None:
    """Xác nhận file CUỐI bảng → tự chọn dòng kế (file bên trên)."""
    p1 = _mkfile(tmp_path, "a.pdf")
    p2 = _mkfile(tmp_path, "b.pdf")
    screen._add_result(_match(p1))
    screen._add_result(_match(p2))

    screen._show_preview(1)
    assert screen._preview_current.source_path == p2
    _click_not_secret(screen)

    assert screen.table.rowCount() == 1
    assert screen._row_path(0) == p1
    assert screen._preview_current is not None
    assert screen._preview_current.source_path == p1

    _click_not_secret(screen)
    assert screen.table.rowCount() == 0
    assert ssp.NotSecretMarks.load().is_marked(p1)


def test_confirm_not_secret_survives_row_shift_during_dialog(
    qapp, screen, auto_confirm, tmp_path, monkeypatch
) -> None:
    """Lỗi "sai index" khi xác nhận liên tiếp: hộp thoại xác nhận bơm event
    loop, signal worker (_results_replace / rút dòng) chen vào TRONG lúc
    dialog mở làm các dòng phía dưới dồn lên. Target tự nhảy sau xác nhận
    phải suy từ đường dẫn file kế (danh tính), không phải từ index chụp
    trước đó.

    Bố cục: C(0) D(1) A(2) B(3) E(4). Xem A rồi xác nhận; trong lúc dialog
    mở, file C bị rút khỏi bảng → sau khi A bị loại, file kế của A là B
    (không phải E như code dùng index cũ làm).
    """
    pc = _mkfile(tmp_path, "c.pdf")
    pd = _mkfile(tmp_path, "d.pdf")
    pa = _mkfile(tmp_path, "a.pdf")
    pb = _mkfile(tmp_path, "b.pdf")
    pe = _mkfile(tmp_path, "e.pdf")
    for p in (pc, pd, pa, pb, pe):
        screen._add_result(_match(p))
    assert screen._row_path(2) == pa

    def confirm_and_race(self, *a, **k):
        # Giả lập signal worker chen trong lúc box.exec(): file C (phía
        # trên A) bị rút khỏi bảng → A dồn từ dòng 2 xuống dòng 1.
        self._remove_file_rows(pc)
        return True

    monkeypatch.setattr(
        SecretFileScanScreen, "_confirm_paths", confirm_and_race
    )

    screen._show_preview(2)  # file A
    assert screen._preview_current.source_path == pa
    _click_not_secret(screen)

    assert screen.table.rowCount() == 3
    assert ssp.NotSecretMarks.load().is_marked(pa)
    # Sau khi loại A, preview phải nhảy sang B (file kế của A theo danh
    # tính), không nhảy nhầm sang E vì index cũ đã lỗi.
    assert screen._preview_current is not None
    assert screen._preview_current.source_path == pb, (
        f"preview sau xác nhận phải là file kế tiếp (B), không phải file khác"
    )
    assert screen._row_path(screen.table.currentRow()) == pb


def test_remove_rows_sweeps_table_when_map_desyncs(
    qapp, screen, auto_confirm, tmp_path
) -> None:
    """Lưới an toàn: map path→dòng lỡ mất đồng bộ (entry biến mất nhưng
    dòng còn trên bảng) thì xác nhận "không phải mật" vẫn phải gỡ sạch
    dòng của file — tuyệt đối không để lại dòng "đã xác nhận rồi mà
    vẫn hiện trong danh sách"."""
    pa = _mkfile(tmp_path, "a.pdf")
    pb = _mkfile(tmp_path, "b.pdf")
    screen._add_result(_match(pa))
    screen._add_result(_match(pb))

    # Mô phỏng lệch map: entry của A biến mất, dòng của A còn nguyên.
    screen._result_items_by_file.pop(_norm(pa), None)
    assert screen.table.rowCount() == 2

    screen._show_preview(0)
    _click_not_secret(screen)

    assert screen.table.rowCount() == 1, (
        "dòng của file đã xác nhận phải bị gỡ dù map bị lệch"
    )
    assert screen._row_path(0) == pb
    assert ssp.NotSecretMarks.load().is_marked(pa)


def test_confirm_many_files_while_table_keeps_shifting(
    qapp, screen, tmp_path, monkeypatch
) -> None:
    """Đảm bảo chính của luồng: xác nhận LIÊN TIẾP nhiều file bằng nút
    "Không phải mật" (đi theo auto-jump) trong khi bảng liên tục bị xê
    dịch giữa chừng hộp thoại (giả lập _results_replace của lượt quét
    đang chạy) — mỗi lần xác nhận phải đúng file đang xem trước, không
    bao giờ xác nhận nhầm file khác.

    Bố cục: A(0) B(1) C(2) D(3) E(4).
    - Lượt 1: xem A; trong lúc dialog mở, file D bị rút → [A B C E];
      xác nhận A → auto-jump phải sang B.
    - Lượt 2: xem B; trong lúc dialog mở, file E bị rút → [B C];
      xác nhận B → auto-jump phải sang C.
    - Lượt 3: xem C; không có xê dịch; xác nhận C → bảng rỗng.
    D và E bị rút khỏi bảng (không phải do xác nhận) → KHÔNG được dính dấu.
    """
    pa = _mkfile(tmp_path, "a.pdf")
    pb = _mkfile(tmp_path, "b.pdf")
    pc = _mkfile(tmp_path, "c.pdf")
    pd = _mkfile(tmp_path, "d.pdf")
    pe = _mkfile(tmp_path, "e.pdf")
    for p in (pa, pb, pc, pd, pe):
        screen._add_result(_match(p))

    def make_dialog_race(victim):
        def confirm_and_race(self, *a, **k):
            if victim:
                self._remove_file_rows(victim)
            return True
        return confirm_and_race

    # --- Lượt 1: xác nhận A, dialog làm rút file D ---
    monkeypatch.setattr(
        SecretFileScanScreen, "_confirm_paths", make_dialog_race(pd)
    )
    screen._show_preview(0)
    assert screen._preview_current.source_path == pa
    _click_not_secret(screen)
    assert screen.table.rowCount() == 3
    assert [screen._row_path(r) for r in range(3)] == [pb, pc, pe]
    marks = ssp.NotSecretMarks.load()
    assert marks.is_marked(pa) and not marks.is_marked(pd)
    assert screen._preview_current.source_path == pb, (
        "auto-jump sau lượt 1 phải sang đúng file B"
    )

    # --- Lượt 2: xác nhận B, dialog làm rút file E ---
    monkeypatch.setattr(
        SecretFileScanScreen, "_confirm_paths", make_dialog_race(pe)
    )
    _click_not_secret(screen)
    assert screen.table.rowCount() == 1
    assert screen._row_path(0) == pc
    marks = ssp.NotSecretMarks.load()
    assert marks.is_marked(pb) and not marks.is_marked(pe)
    assert screen._preview_current.source_path == pc, (
        "auto-jump sau lượt 2 phải sang đúng file C"
    )

    # --- Lượt 3: xác nhận C, không xê dịch ---
    monkeypatch.setattr(
        SecretFileScanScreen, "_confirm_paths", make_dialog_race("")
    )
    _click_not_secret(screen)
    assert screen.table.rowCount() == 0
    assert ssp.NotSecretMarks.load().is_marked(pc)
    assert screen._preview_current is None


def test_cancel_dialog_keeps_current_preview(
    qapp, screen, tmp_path, monkeypatch
) -> None:
    """Bấm Hủy trong hộp thoại xác nhận → không gỡ gì và preview GIỮ
    nguyên file đang xem (không tự nhảy sang file kế)."""
    pa = _mkfile(tmp_path, "a.pdf")
    pb = _mkfile(tmp_path, "b.pdf")
    screen._add_result(_match(pa))
    screen._add_result(_match(pb))

    monkeypatch.setattr(
        SecretFileScanScreen, "_confirm_paths", lambda *a, **k: False
    )
    screen._show_preview(0)
    _click_not_secret(screen)

    assert screen.table.rowCount() == 2
    assert not ssp.NotSecretMarks.load().is_marked(pa)
    assert screen._preview_current.source_path == pa, (
        "Hủy thì phải ở lại file đang xem, không nhảy sang file kế"
    )


def test_delete_failed_keeps_current_preview(
    qapp, screen, tmp_path, monkeypatch
) -> None:
    """Xóa lỗi (file bị khóa trên đĩa) → dòng được giữ lại và preview
    không nhảy sang file khác để người dùng thử lại."""
    pa = _mkfile(tmp_path, "a.pdf")
    pb = _mkfile(tmp_path, "b.pdf")
    screen._add_result(_match(pa))
    screen._add_result(_match(pb))

    monkeypatch.setattr(
        SecretFileScanScreen, "_confirm_paths", lambda *a, **k: True
    )
    monkeypatch.setattr(
        SecretFileScanScreen, "_show_path_errors", lambda *a, **k: None
    )

    def locked(path):
        raise OSError("file đang bị khóa bởi chương trình khác")

    import scanindex.ui.screens.secret_file_scan_screen as mod

    original_delete = mod._permanent_delete
    monkeypatch.setattr(mod, "_permanent_delete", locked)
    screen._show_preview(0)
    screen._preview_delete_clicked()

    assert screen.table.rowCount() == 2, "xóa lỗi → dòng phải được giữ lại"
    assert screen._preview_current.source_path == pa, (
        "xóa lỗi → ở lại file đang xem để thử lại, không nhảy dòng"
    )

    # Xóa thành công thì mới tự nhảy sang dòng kế.
    monkeypatch.setattr(mod, "_permanent_delete", original_delete)
    screen._preview_delete_clicked()
    assert screen.table.rowCount() == 1
    assert screen._row_path(0) == pb
    assert screen._preview_current.source_path == pb
