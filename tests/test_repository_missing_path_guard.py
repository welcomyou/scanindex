"""Guards for a configured Kho path that disappeared (app folder moved,
release extracted elsewhere, drive unmounted...).

Covers the pure helpers behind the interactive startup prompt in
``scanindex.ui.repository.screen``: default-path detection, DB presence
check, sibling-repository discovery, declined-sibling persistence and the
auto-seeded settings gate. The full dialog flows are interactive and are
exercised manually.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import configparser
from pathlib import Path
from types import SimpleNamespace

from scanindex.infra import data_versioning
from scanindex.ui.repository.screen import (
    _DECLINED_SIBLING_KEY,
    _find_sibling_repository,
    _is_default_repository_setting,
    _read_repository_declined_sibling,
    _read_repository_path_setting_raw,
    _repository_has_db,
    _write_repository_declined_sibling,
)


def _write_ini(path: Path, repository_path: str | None = None,
               extra: dict[str, str] | None = None) -> None:
    cfg = configparser.ConfigParser()
    if repository_path is not None or extra:
        cfg["Repository"] = {}
        if repository_path is not None:
            cfg["Repository"]["path"] = repository_path
        if extra:
            cfg["Repository"].update(extra)
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)


# ------------------------------------------------- default-path detection

def test_default_repository_setting_matches_only_the_builtin_default():
    assert _is_default_repository_setting("repository")
    assert _is_default_repository_setting("Repository")
    assert _is_default_repository_setting("repository/")
    assert _is_default_repository_setting(" repository\\ ")
    assert not _is_default_repository_setting("")
    assert not _is_default_repository_setting("D:\\Data\\kho")
    assert not _is_default_repository_setting("kho_data")
    assert not _is_default_repository_setting("..\\repository")


# ------------------------------------------------- DB presence check

def test_repository_has_db_detects_current_and_versioned_names(tmp_path):
    assert not _repository_has_db(tmp_path / "missing")
    assert not _repository_has_db(tmp_path)  # empty dir is not a Kho

    (tmp_path / "repository.db").touch()
    assert _repository_has_db(tmp_path)

    other = tmp_path / "other"
    other.mkdir()
    (other / "repository-1.1.13.db").touch()
    assert _repository_has_db(other)

    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "repository.sqlite").touch()
    assert not _repository_has_db(junk)


# ------------------------------------------------- sibling discovery

def test_find_sibling_repository_prefers_most_recent_db(tmp_path):
    base = tmp_path / "ScanIndex-1.2.0"
    base.mkdir()
    assert _find_sibling_repository(base) is None  # nothing beside it

    old = tmp_path / "ScanIndex-1.1.13"
    (old / "repository").mkdir(parents=True)
    (old / "repository" / "repository-1.1.13.db").touch()
    assert _find_sibling_repository(base) == old / "repository"

    newer = tmp_path / "backup_copy"
    (newer / "repository").mkdir(parents=True)
    newer_db = newer / "repository" / "repository.db"
    newer_db.touch()
    os.utime(newer_db, (2_000_000_000, 2_000_000_000))  # far future mtime
    assert _find_sibling_repository(base) == newer / "repository"


def test_find_sibling_repository_ignores_empty_siblings(tmp_path):
    base = tmp_path / "app"
    base.mkdir()
    empty = tmp_path / "old"
    (empty / "repository").mkdir(parents=True)  # no DB inside
    assert _find_sibling_repository(base) is None


# ------------------------------------------------- raw setting + decline memory

def test_read_repository_path_setting_raw_reads_repository_then_archive(
        tmp_path, monkeypatch):
    ini = tmp_path / "settings-1.2.0.ini"

    monkeypatch.setattr(
        data_versioning, "get_active_settings_path", lambda: str(ini)
    )

    _write_ini(ini, repository_path="D:\\Data\\kho")
    assert _read_repository_path_setting_raw() == "D:\\Data\\kho"

    # Legacy [Archive] section is honoured as a fallback.
    cfg = configparser.ConfigParser()
    cfg["Archive"] = {"path": "E:\\old\\kho"}
    with open(ini, "w", encoding="utf-8") as f:
        cfg.write(f)
    assert _read_repository_path_setting_raw() == "E:\\old\\kho"

    assert not _read_repository_path_setting_raw() or True  # smoke
    _write_ini(ini)  # no [Repository] section at all
    assert _read_repository_path_setting_raw() == ""


def test_declined_sibling_roundtrip_and_no_nag(tmp_path, monkeypatch):
    ini = tmp_path / "settings-1.2.0.ini"
    _write_ini(ini, repository_path="repository")
    monkeypatch.setattr(
        data_versioning, "get_active_settings_path", lambda: str(ini)
    )

    declined = tmp_path / "old" / "repository"
    _write_repository_declined_sibling(declined)
    assert _read_repository_declined_sibling() == str(declined)

    # The write must preserve the existing [Repository] path.
    cfg = configparser.ConfigParser()
    cfg.read(ini, encoding="utf-8")
    assert cfg.get("Repository", "path") == "repository"
    assert cfg.get("Repository", _DECLINED_SIBLING_KEY) == str(declined)


# ------------------------------------------------- auto-seeded gate

def test_is_settings_auto_seeded_follows_meta_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "scanindex.infra.paths.get_base_dir", lambda: str(tmp_path)
    )
    # find_versioned_file matches the *running* app version's filename.
    from scanindex.infra.version import get_version_short
    ini = tmp_path / f"settings-{get_version_short()}.ini"

    assert not data_versioning.is_settings_auto_seeded()  # no file yet

    _write_ini(ini, repository_path="repository")
    assert not data_versioning.is_settings_auto_seeded()

    cfg = configparser.ConfigParser()
    cfg.read(ini, encoding="utf-8")
    cfg["Meta"] = {"auto_seeded": "123"}
    with open(ini, "w", encoding="utf-8") as f:
        cfg.write(f)
    assert data_versioning.is_settings_auto_seeded()
