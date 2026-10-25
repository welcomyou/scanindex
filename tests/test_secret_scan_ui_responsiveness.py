"""Offscreen UI regressions using synthetic files only; no OCR/models."""
from __future__ import annotations

import os
import sys
import threading
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from scanindex.infra import translations
from scanindex.ui.screens import secret_file_scan_screen as ui


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def screen(qapp, tmp_path, monkeypatch):
    # Construction clears the preview cache: keep all state in tmp_path.
    monkeypatch.setattr(ui, "get_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ui.ssp, "progress_dir", lambda: str(tmp_path / "progress"))
    previous_lang = translations.current_locale.lang
    translations.current_locale.set_language("vi")
    widget = ui.SecretFileScanScreen()
    widget._pool_warm_started = True
    widget.resize(1280, 800)
    yield widget
    widget._feedback_timer.stop()
    widget.close()
    widget.deleteLater()
    qapp.processEvents()
    translations.current_locale.set_language(previous_lang)


def _match(tmp_path, name="a.pdf", page=1):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"synthetic document")
    return ui.SecretScanMatch(
        source_path=str(path),
        relative_path=name,
        keyword="Mật",
        page_number=page,
        mode="Trang đầu",
        ocr_pdf_path="",
        note="PDF gốc; trang đầu",
    )


def _send_from_worker(screen, qapp, *matches):
    def emit():
        for match in matches:
            screen._result_found.emit(match)

    worker = threading.Thread(target=emit)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    qapp.processEvents()


def _assert_counts(screen):
    matches = screen._current_matches()
    files = len({ui._norm(m.source_path) for m in matches if m.source_path})
    rows = screen.table.rowCount()
    visible = sum(not screen.table.isRowHidden(r) for r in range(rows))
    assert len(screen._result_file_keys) == files
    assert screen._visible_result_rows == visible
    assert f"Tổng: {files} văn bản mật" in screen.lbl_total.text()
    if visible != rows:
        assert f"hiện {visible}/{rows}" in screen.lbl_total.text()


def test_scroll_does_not_retranslate_existing_cells(
    screen, qapp, tmp_path, monkeypatch
):
    for i in range(300):
        screen._add_result(_match(tmp_path, page=i + 1), quiet=True)
    screen._refresh_totals()
    existing_filter = getattr(qapp, "_scanindex_i18n_filter", None)
    translations.install_event_filter(qapp)
    try:
        screen.show()
        qapp.processEvents()
        reads = []
        original_item = screen.table.item

        def item(row, col):
            reads.append((row, col))
            return original_item(row, col)

        monkeypatch.setattr(screen.table, "item", item)
        for pos in (10, 40, 5):
            screen.table.verticalScrollBar().setValue(pos)
            screen.table.viewport().repaint()
        # Qt paints visible cells itself. Python must not walk the model.
        assert reads == []
        assert screen.table.rowCount() == 300

        # Explicit language changes must still translate existing items.
        translations.current_locale.set_language("en")
        translations.retranslate_widget_tree(screen)
        assert screen.table.item(0, 7).text() == translations.localize_text("Trang đầu")
        assert screen.table.item(0, 7).text() != "Trang đầu"
        assert screen.table.item(0, 2).text() == "a.pdf"
        screen._add_result(_match(tmp_path, "b.pdf"))
        assert screen.table.item(300, 7).text() == screen.table.item(0, 7).text()
        translations.current_locale.set_language("vi")
        translations.retranslate_widget_tree(screen)
        assert screen.table.item(0, 7).text() == "Trang đầu"
    finally:
        if existing_filter is None:
            qapp.removeEventFilter(qapp._scanindex_i18n_filter)
            del qapp._scanindex_i18n_filter


def test_insert_does_not_read_old_rows_or_change_selection(
    screen, qapp, tmp_path, monkeypatch
):
    for i in range(300):
        screen._add_result(_match(tmp_path, page=i + 1), quiet=True)
    screen._refresh_totals()
    screen.show()
    qapp.processEvents()
    screen.table.setCurrentCell(40, 2)
    screen.table.verticalScrollBar().setValue(30)
    screen._on_select_all_toggled(True)
    previous_scroll = screen.table.verticalScrollBar().value()
    original_item = screen.table.item
    reads = []

    def item(row, col):
        reads.append(row)
        return original_item(row, col)

    monkeypatch.setattr(screen.table, "item", item)
    screen._busy = True
    screen._add_result(_match(tmp_path, "b.pdf"))
    assert all(row >= 300 for row in reads)
    assert screen.table.currentRow() == 40
    assert screen.table.verticalScrollBar().value() == previous_scroll
    assert ui._norm(str(tmp_path / "a.pdf")) in screen._checked_paths
    assert screen.btn_batch_delete.isEnabled()
    assert screen.table.horizontalHeader().check_state() == Qt.CheckState.PartiallyChecked
    _assert_counts(screen)


def test_counts_filter_replace_and_reset(screen, tmp_path):
    a = _match(tmp_path, "keep/a.pdf")
    b = _match(tmp_path, "other/b.pdf")
    screen._add_result(a)
    screen._add_result(_match(tmp_path, "keep/a.pdf", page=2))
    screen._add_result(b)
    _assert_counts(screen)
    screen.filter_edit.setText("keep")
    _assert_counts(screen)
    screen._add_result(_match(tmp_path, "other/c.pdf"))
    assert screen.table.isRowHidden(3)
    screen._add_result(_match(tmp_path, "keep/d.pdf"))
    _assert_counts(screen)
    screen._remove_file_rows(b.source_path)
    _assert_counts(screen)
    screen._on_results_replace({"path": a.source_path, "matches": [asdict(a)]})
    _assert_counts(screen)
    screen.filter_edit.clear()
    _assert_counts(screen)
    screen._reset_results()
    _assert_counts(screen)
    screen._load_matches_into_table([a, b], replace=True)
    _assert_counts(screen)


def test_actions_still_work_during_worker_results(
    screen, qapp, tmp_path, monkeypatch
):
    monkeypatch.setattr(screen, "_confirm_paths", lambda *args: True)
    screen._busy = True
    a, b, c = [_match(tmp_path, name) for name in ("a.pdf", "b.pdf", "c.pdf")]
    _send_from_worker(screen, qapp, a, _match(tmp_path, page=2), b)
    screen.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    screen._preview_current = a
    screen._refresh_action_buttons()
    assert screen.btn_batch_delete.isEnabled()
    assert screen.btn_preview_not_secret.isEnabled()
    screen._mark_not_secret([a.source_path])
    assert Path(a.source_path).exists()
    assert ui.ssp.NotSecretMarks.load().is_marked(a.source_path)
    # A late result for the marked file must still pass the existing gate.
    _send_from_worker(screen, qapp, a, c)
    assert {m.source_path for m in screen._current_matches()} == {b.source_path, c.source_path}
    screen.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    screen._batch_delete_clicked()
    assert not Path(b.source_path).exists()
    assert [m.source_path for m in screen._current_matches()] == [c.source_path]
    _assert_counts(screen)
    xlsx = str(tmp_path / "results.xlsx")
    ui.export_matches_to_excel(screen._current_matches(), xlsx)
    assert [m.source_path for m in ui.load_secret_matches_from_excel(xlsx)] == [c.source_path]


def test_failed_delete_preserves_rows_and_counts(screen, tmp_path, monkeypatch):
    a = _match(tmp_path)
    screen._busy = True
    screen._add_result(a)
    monkeypatch.setattr(screen, "_confirm_paths", lambda *args: True)
    monkeypatch.setattr(screen, "_show_path_errors", lambda *args: None)

    def locked(path):
        raise PermissionError("synthetic locked file")

    monkeypatch.setattr(ui, "_permanent_delete", locked)
    screen._delete_files([a.source_path])
    assert Path(a.source_path).exists()
    assert screen._current_matches() == [a]
    _assert_counts(screen)


def test_feedback_coalesces_without_overwriting_completion(screen, qapp):
    screen._busy = True
    screen._set_status("initial")
    for i in range(20):
        screen._status_changed.emit(f"scanning {i}")
        screen._progress_changed.emit(i + 1, 20)
    assert screen.status_label.text() == "initial"
    # Timers can be delayed under the full suite's native/model load.
    # Wait for the UI condition with a bound, retaining the no-immediate-update assertion.
    for _ in range(100):
        if screen.status_label.text() == "scanning 19":
            break
        QTest.qWait(10)
    assert screen.status_label.text() == "scanning 19"
    assert screen.progress.value() == 20
    screen._status_changed.emit("stale")
    screen._progress_changed.emit(20, 20)
    screen._on_finished({"total": 20, "scanned": 20})
    final_status = screen.status_label.text()
    assert "Hoàn tất" in final_status
    QTest.qWait(150)
    assert screen.status_label.text() == final_status
    assert not screen._feedback_timer.isActive()


def test_select_all_recounts_header_once(screen, tmp_path, monkeypatch):
    for i in range(100):
        screen._add_result(_match(tmp_path, page=i + 1), quiet=True)
    calls = []
    original = screen._update_select_all_state

    def update():
        calls.append(True)
        original()

    monkeypatch.setattr(screen, "_update_select_all_state", update)
    screen._on_select_all_toggled(True)
    assert len(calls) == 1
    assert all(screen.table.item(r, 0).checkState() == Qt.CheckState.Checked for r in range(100))
    assert screen.table.horizontalHeader().check_state() == Qt.CheckState.Checked
