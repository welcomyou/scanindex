"""Interactive flows of the missing-Kho startup guard (offscreen, stubbed
dialogs — no real user interaction).

Scenarios:
1. Configured non-default path missing + user cancels -> no store, nothing
   silently created at the stale path, banner visible.
2. Same, user picks "Tạo kho mới tại vị trí này" -> empty store created.
3. Same, user picks another folder that HAS a repository DB -> adopted,
   setting persisted, store opened.
4. Default path + fresh install (auto-seeded settings) + data-bearing Kho in
   a sibling folder -> adoption offered, "Yes" reuses the old Kho.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from scanindex.ui.repository import screen as screen_mod
from scanindex.ui.repository.screen import RepositoryScreen


class _Btn:
    def __init__(self, text: str):
        self._text = text

    def text(self) -> str:
        return self._text


class _StubMessageBox:
    """Replaces screen_mod.QMessageBox so no real dialog can block tests."""

    StandardButton = SimpleNamespace(Yes=1, No=0, Ok=1, Cancel=0)
    Icon = SimpleNamespace(Warning=1, Question=2, Critical=3, Information=4)
    ButtonRole = SimpleNamespace(YesRole=0, NoRole=1, RejectRole=2)

    # Per-test knobs.
    click_text: str | None = None       # button label to "click" on exec()
    click_queue: list[str] = []         # sequential answers across exec()s
    question_answer: int = 0            # Yes(1)/No(0) for QMessageBox.question
    seen_texts: list[str] = []          # every dialog text shown

    def __init__(self, *args, **kwargs):
        self._buttons: list[_Btn] = []
        self._clicked: _Btn | None = None

    def setWindowTitle(self, *_a):
        pass

    def setIcon(self, *_a):
        pass

    def setText(self, text):
        type(self).seen_texts.append(text)

    def addButton(self, text, *_a):
        btn = _Btn(text)
        self._buttons.append(btn)
        return btn

    def exec(self):
        queue = type(self).click_queue
        want = queue.pop(0) if queue else type(self).click_text
        for btn in self._buttons:
            if btn.text() == want:
                self._clicked = btn
                return 0
        self._clicked = self._buttons[-1] if self._buttons else None
        return 0

    def clickedButton(self):
        return self._clicked

    @classmethod
    def question(cls, *a, **k):
        cls.seen_texts.append(a[2] if len(a) > 2 else "")
        return cls.question_answer

    @classmethod
    def critical(cls, *a, **k):
        cls.seen_texts.append(a[2] if len(a) > 2 else "")
        return 0

    @classmethod
    def warning(cls, *a, **k):
        cls.seen_texts.append(a[2] if len(a) > 2 else "")
        return 0


class _StubFileDialog:
    pick_dir: str | None = None

    @staticmethod
    def getExistingDirectory(*_a, **_k):
        return _StubFileDialog.pick_dir or ""


@pytest.fixture()
def qtapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _install_stubs(monkeypatch, tmp_path, *, raw_setting: str,
                   configured_path: Path):
    monkeypatch.setattr(screen_mod, "QMessageBox", _StubMessageBox)
    monkeypatch.setattr(screen_mod, "QFileDialog", _StubFileDialog)
    monkeypatch.setattr(
        screen_mod, "_read_repository_path_setting",
        lambda: configured_path,
    )
    monkeypatch.setattr(
        screen_mod, "_read_repository_path_setting_raw",
        lambda: raw_setting,
    )
    monkeypatch.setattr(
        screen_mod, "_read_repository_declined_sibling", lambda: "",
    )
    written: list[Path] = []
    monkeypatch.setattr(
        screen_mod, "_write_repository_path_setting",
        lambda p: written.append(Path(p)),
    )
    return written


def _reset_stubs():
    _StubMessageBox.click_text = None
    _StubMessageBox.click_queue = []
    _StubMessageBox.question_answer = 0
    _StubMessageBox.seen_texts = []
    _StubFileDialog.pick_dir = None


def _make_kho(folder: Path) -> Path:
    repo = folder / "repository"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "repository.db").touch()
    return repo


def test_missing_configured_kho_cancel_keeps_store_closed(qtapp, monkeypatch,
                                                          tmp_path):
    _reset_stubs()
    configured = tmp_path / "gone" / "kho"
    _install_stubs(monkeypatch, tmp_path, raw_setting=str(configured),
                   configured_path=configured)
    _StubMessageBox.click_text = "Hủy"

    screen = RepositoryScreen()

    assert screen._store is None
    assert screen._repo_open_declined is True
    # Nothing was silently recreated at the stale location.
    assert not configured.exists()
    # Banner explains the state and offers a way out.
    assert "chưa được mở" in screen._repo_banner.text().lower()
    assert "Chọn lại vị trí kho" in screen._repo_banner.text()


def test_missing_configured_kho_create_new(qtapp, monkeypatch, tmp_path):
    _reset_stubs()
    configured = tmp_path / "kho_moi"
    _install_stubs(monkeypatch, tmp_path, raw_setting=str(configured),
                   configured_path=configured)
    _StubMessageBox.click_text = "Tạo kho mới tại vị trí này"

    screen = RepositoryScreen()

    assert screen._store is not None
    assert screen._repo_open_declined is False
    assert screen_mod._repository_has_db(configured)


def test_missing_configured_kho_pick_existing(qtapp, monkeypatch, tmp_path):
    _reset_stubs()
    configured = tmp_path / "gone" / "kho"
    written = _install_stubs(monkeypatch, tmp_path,
                             raw_setting=str(configured),
                             configured_path=configured)
    other_repo = _make_kho(tmp_path / "old" / "ScanIndex-1.1.13")
    _StubMessageBox.click_text = "Chọn vị trí kho khác"
    # The kho itself (the folder holding repository*.db) is what the user
    # picks — same convention as the settings' [Repository] path.
    _StubFileDialog.pick_dir = str(other_repo)

    screen = RepositoryScreen()

    assert screen._archive_path == other_repo
    assert screen._store is not None
    assert written == [other_repo]  # persisted


def test_missing_configured_kho_pick_empty_folder_needs_confirm(
        qtapp, monkeypatch, tmp_path):
    _reset_stubs()
    configured = tmp_path / "gone" / "kho"
    written = _install_stubs(monkeypatch, tmp_path,
                             raw_setting=str(configured),
                             configured_path=configured)
    empty_pick = tmp_path / "empty_pick"
    empty_pick.mkdir()
    _StubFileDialog.pick_dir = str(empty_pick)
    # Dialog 1: "Chọn vị trí kho khác"; dialog 2 (confirm new store): bail
    # out with "Chọn lại".
    _StubMessageBox.click_queue = ["Chọn vị trí kho khác", "Chọn lại"]

    screen = RepositoryScreen()

    # User bailed out at the confirm step: no store, nothing written, and
    # crucially the empty picked folder was NOT turned into a Kho.
    assert screen._store is None
    assert written == []
    assert list(empty_pick.iterdir()) == []


def test_fresh_install_adopts_sibling_kho(qtapp, monkeypatch, tmp_path):
    _reset_stubs()
    app_dir = tmp_path / "ScanIndex-1.2.0"
    app_dir.mkdir()
    sibling_repo = _make_kho(tmp_path / "ScanIndex-1.1.13")
    # Default (never-configured) setting points inside the app folder.
    configured = app_dir / "repository"
    written = _install_stubs(monkeypatch, tmp_path, raw_setting="repository",
                             configured_path=configured)
    monkeypatch.setattr(screen_mod, "get_base_dir", lambda: str(app_dir))
    import scanindex.infra.data_versioning as dv
    monkeypatch.setattr(dv, "is_settings_auto_seeded", lambda: True)
    _StubMessageBox.question_answer = _StubMessageBox.StandardButton.Yes

    screen = RepositoryScreen()

    assert screen._archive_path == sibling_repo
    assert screen._store is not None
    assert written == [sibling_repo]


def test_fresh_install_declines_sibling_and_remembered(qtapp, monkeypatch,
                                                       tmp_path):
    _reset_stubs()
    app_dir = tmp_path / "ScanIndex-1.2.0"
    app_dir.mkdir()
    sibling_repo = _make_kho(tmp_path / "ScanIndex-1.1.13")
    configured = app_dir / "repository"
    _install_stubs(monkeypatch, tmp_path, raw_setting="repository",
                   configured_path=configured)
    monkeypatch.setattr(screen_mod, "get_base_dir", lambda: str(app_dir))
    import scanindex.infra.data_versioning as dv
    monkeypatch.setattr(dv, "is_settings_auto_seeded", lambda: True)
    _StubMessageBox.question_answer = _StubMessageBox.StandardButton.No
    declined: list[Path] = []
    monkeypatch.setattr(
        screen_mod, "_write_repository_declined_sibling",
        lambda p: declined.append(Path(p)),
    )

    screen = RepositoryScreen()

    # Declined: a fresh empty Kho is created at the default location and the
    # decline is remembered so the question is not asked every launch.
    assert declined == [sibling_repo]
    assert screen._archive_path == configured
    assert screen._store is not None
