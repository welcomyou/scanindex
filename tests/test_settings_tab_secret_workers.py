"""Smoke: trường mới 'Số tài liệu quét song song' chạy đủ vòng set/get."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from scanindex.ui.tabs.settings_tab import SettingsTab  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_settings_tab_secret_file_workers_roundtrip(qapp):
    tab = SettingsTab(current_language="vi")
    tab.set_values(
        wait_page="1",
        compare_int="1",
        concurrency="4",
        export_workers="1",
        model="",
        verbose=True,
        secret_file_workers="3",
    )
    vals = tab.get_values()
    assert vals["secret_file_workers"] == "3"
    assert vals["concurrency"] == "4"
    tab.set_values(
        wait_page="1",
        compare_int="1",
        concurrency="4",
        export_workers="1",
        model="",
        verbose=True,
        secret_file_workers="7",
    )
    assert tab.get_values()["secret_file_workers"] == "7"
    # Mặc định khi không truyền: giữ "2".
    tab2 = SettingsTab(current_language="vi")
    assert tab2.get_values()["secret_file_workers"] == "2"
