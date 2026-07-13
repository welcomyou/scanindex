"""Tests for the runtime-config bootstrap.

Behavior changed in 1.1.4: ``ensure_runtime_config_files`` no longer seeds the
live config files (settings.ini, sign json, ignored_words). First-launch
seeding and upgrade migration moved to :mod:`scanindex.infra.data_versioning`
(version-per-file scheme). These tests assert the new contract:

* ``ensure_runtime_config_files`` only creates the stamp-image directory.
* The old seeding responsibilities are covered by ``data_versioning``.
"""
from pathlib import Path

from scanindex.infra import paths


def test_ensure_runtime_files_no_longer_seeds_config(tmp_path, monkeypatch):
    """ensure_runtime_config_files must NOT create settings.ini / sign json /
    ignored_words anymore — that is data_versioning's job now."""
    monkeypatch.setattr(paths, "get_base_dir", lambda: str(tmp_path))
    created = paths.ensure_runtime_config_files()
    assert created == []
    # Only the stamp-image dir is guaranteed to exist.
    assert (tmp_path / "config" / "sign_stamp_images").is_dir()
    # No live config files created.
    assert not (tmp_path / "settings.ini").exists()
    assert not (tmp_path / "ignored_words.txt").exists()
    assert not (tmp_path / "config" / "sign_settings.json").exists()


def test_copy_failure_does_not_remove_an_existing_runtime_file(tmp_path):
    target_path = tmp_path / "settings.ini"
    target_path.write_text("keep-settings\n", encoding="utf-8")
    # _copy_file_if_missing is still used internally by data_versioning.
    with __import__("pytest").raises(FileNotFoundError):
        paths._copy_file_if_missing(tmp_path / "missing.example", target_path)
    assert target_path.read_text(encoding="utf-8") == "keep-settings\n"
