"""Tests for version-per-file migration (scanindex.infra.data_versioning).

Covers the core upgrade-safety scenarios:
  * Fresh install writes nothing.
  * Legacy bare-name files are merged into versioned names.
  * Copy-overwrite: a real legacy file beats an auto-seeded default own file.
  * Version ladder 1.1.3 -> 1.1.4 -> 1.1.5 chains cleanly.
  * SQLite DB migration preserves documents and the pdf/ folder.
  * Schema converter framework runs registered converters and refuses when
    a converter is missing.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scanindex.infra import data_versioning as dv


# --------------------------------------------------------------------- helpers

@pytest.fixture
def isolated_base(tmp_path, monkeypatch):
    """Pin both _app_version and _base_dir so tests are hermetic."""
    monkeypatch.setattr(dv, "_app_version", lambda: "1.1.4")
    monkeypatch.setattr(dv, "_base_dir", lambda: tmp_path)
    # Also patch the module-level lazy imports' source so get_active_* resolve
    # to the same isolated base.
    from scanindex.infra import paths
    monkeypatch.setattr(paths, "get_base_dir", lambda: str(tmp_path))
    return tmp_path


def _wipe(base: Path) -> None:
    for child in list(base.iterdir()):
        if child.is_dir():
            import shutil
            shutil.rmtree(child)
        else:
            child.unlink()


def _make_legacy_db(path: Path, n_docs: int = 0, schema_version: str = "8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE documents (doc_id TEXT PRIMARY KEY, indexed_status TEXT);
        CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER);
        """
    )
    for i in range(n_docs):
        conn.execute(
            "INSERT INTO documents VALUES (?, 'indexed')", (f"d{i}",)
        )
    conn.execute(
        "INSERT INTO index_meta(key,value,updated_at) VALUES ('schema_version', ?, ?)",
        (schema_version, int(time.time())),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------- text-file tests

def test_fresh_install_writes_nothing(isolated_base):
    _wipe(isolated_base)
    dv.run_startup_migration()
    files = sorted(p.name for p in isolated_base.rglob("*") if p.is_file())
    assert files == [], f"fresh install must write no config files, got {files}"


def test_legacy_bare_settings_merged_to_versioned(isolated_base):
    _wipe(isolated_base)
    (isolated_base / "settings.ini").write_text(
        "[General]\nlanguage = vi\ntheme = light\n[KIE]\nmode = layoutlmv3\n",
        encoding="utf-8",
    )
    dv.run_startup_migration()
    own = isolated_base / "settings-1.1.4.ini"
    assert own.exists()
    assert not (isolated_base / "settings.ini").exists()
    content = own.read_text(encoding="utf-8")
    assert "theme = light" in content
    assert "layoutlmv3" in content


def test_copy_overwrite_legacy_real_data_beats_auto_seeded_default(isolated_base):
    """The headline scenario: user opens 1.1.4 fresh (auto-seeds a default
    settings file), then copies the 1.1.4 folder over their 1.1.3 folder.
    The 1.1.3 real settings must win over the 1.1.4 auto-seeded default."""
    _wipe(isolated_base)
    # Own file: auto-seeded default (carries the marker).
    (isolated_base / "settings-1.1.4.ini").write_text(
        "[Meta]\nauto_seeded = 123\n[General]\ntheme = dark\n",
        encoding="utf-8",
    )
    # Legacy bare file: real user config.
    (isolated_base / "settings.ini").write_text(
        "[General]\ntheme = light\n[KIE]\nmode = layoutlmv3\n",
        encoding="utf-8",
    )
    dv.run_startup_migration()
    own = isolated_base / "settings-1.1.4.ini"
    content = own.read_text(encoding="utf-8")
    assert "layoutlmv3" in content, "real legacy data must win"
    assert not (isolated_base / "settings.ini").exists(), "legacy removed"


def test_own_kept_when_no_legacy(isolated_base):
    _wipe(isolated_base)
    (isolated_base / "settings-1.1.4.ini").write_text(
        "[General]\ntheme = light\n", encoding="utf-8"
    )
    dv.run_startup_migration()
    own = isolated_base / "settings-1.1.4.ini"
    # No [Meta] auto_seeded injected — own is untouched.
    assert own.read_text(encoding="utf-8").strip() == "[General]\ntheme = light"


def test_legacy_sign_json_migrated(isolated_base):
    _wipe(isolated_base)
    (isolated_base / "config").mkdir()
    (isolated_base / "config" / "sign_settings.json").write_text(
        json.dumps({"tsa_enabled": True}), encoding="utf-8"
    )
    dv.run_startup_migration()
    assert (isolated_base / "config" / "sign_settings-1.1.4.json").exists()
    assert not (isolated_base / "config" / "sign_settings.json").exists()


def test_version_ladder_settings_1_1_3_to_1_1_4_to_1_1_5(tmp_path, monkeypatch):
    from scanindex.infra import paths

    monkeypatch.setattr(paths, "get_base_dir", lambda: str(tmp_path))
    _wipe(tmp_path)

    # 1.1.3: bare settings.ini with real data.
    (tmp_path / "settings.ini").write_text(
        "[General]\ntheme = light\n", encoding="utf-8"
    )
    monkeypatch.setattr(dv, "_app_version", lambda: "1.1.4")
    dv.run_startup_migration()
    assert (tmp_path / "settings-1.1.4.ini").exists()
    assert not (tmp_path / "settings.ini").exists()

    monkeypatch.setattr(dv, "_app_version", lambda: "1.1.5")
    dv.run_startup_migration()
    assert (tmp_path / "settings-1.1.5.ini").exists()
    assert not (tmp_path / "settings-1.1.4.ini").exists()
    assert "theme = light" in (tmp_path / "settings-1.1.5.ini").read_text(
        encoding="utf-8"
    )


# -------------------------------------------------------------------- DB tests

@pytest.fixture
def isolated_repo(isolated_base):
    repo = isolated_base / "repository"
    return repo


def test_db_fresh_writes_nothing(isolated_repo):
    _wipe(isolated_repo.parent)
    r = dv.migrate_db_if_needed(isolated_repo)
    assert r["action"] == "fresh"
    assert not (isolated_repo / "repository-1.1.4.db").exists()


def test_db_legacy_renamed_data_preserved(isolated_repo):
    _make_legacy_db(isolated_repo / "repository.db", n_docs=5)
    r = dv.migrate_db_if_needed(isolated_repo)
    assert r["action"] == "migrated"
    own = isolated_repo / "repository-1.1.4.db"
    assert own.exists()
    assert not (isolated_repo / "repository.db").exists()
    conn = sqlite3.connect(str(own))
    n = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE indexed_status != 'deleted'"
    ).fetchone()[0]
    conn.close()
    assert n == 5


def test_db_copy_overwrite_real_data_wins(isolated_repo):
    """Own DB empty (fresh) + legacy DB with real docs -> legacy wins."""
    _make_legacy_db(isolated_repo / "repository-1.1.4.db", n_docs=0)
    _make_legacy_db(isolated_repo / "repository.db", n_docs=10)
    r = dv.migrate_db_if_needed(isolated_repo)
    assert r["action"] == "replaced_own"
    own = isolated_repo / "repository-1.1.4.db"
    conn = sqlite3.connect(str(own))
    n = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE indexed_status != 'deleted'"
    ).fetchone()[0]
    conn.close()
    assert n == 10
    assert not (isolated_repo / "repository.db").exists()


def test_db_noop_when_own_present_no_legacy(isolated_repo):
    _make_legacy_db(isolated_repo / "repository-1.1.4.db", n_docs=3)
    r = dv.migrate_db_if_needed(isolated_repo)
    assert r["action"] in ("noop", "kept_own")


def test_db_pdf_folder_preserved(isolated_repo):
    _make_legacy_db(isolated_repo / "repository.db", n_docs=2)
    (isolated_repo / "pdf").mkdir(exist_ok=True)
    (isolated_repo / "pdf" / "doc1.pdf").write_bytes(b"%PDF-1.4 fake1")
    (isolated_repo / "pdf" / "doc2.pdf").write_bytes(b"%PDF-1.4 fake2")
    dv.migrate_db_if_needed(isolated_repo)
    assert (isolated_repo / "pdf" / "doc1.pdf").exists()
    assert (isolated_repo / "pdf" / "doc2.pdf").exists()


def test_db_ladder_1_1_3_to_1_1_4_to_1_1_5(tmp_path, monkeypatch):
    from scanindex.infra import paths
    monkeypatch.setattr(paths, "get_base_dir", lambda: str(tmp_path))
    repo = tmp_path / "repository"
    _make_legacy_db(repo / "repository.db", n_docs=3)

    monkeypatch.setattr(dv, "_app_version", lambda: "1.1.4")
    dv.migrate_db_if_needed(repo)
    assert (repo / "repository-1.1.4.db").exists()

    monkeypatch.setattr(dv, "_app_version", lambda: "1.1.5")
    dv.migrate_db_if_needed(repo)
    assert (repo / "repository-1.1.5.db").exists()
    assert not (repo / "repository-1.1.4.db").exists()
    conn = sqlite3.connect(str(repo / "repository-1.1.5.db"))
    n = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE indexed_status != 'deleted'"
    ).fetchone()[0]
    conn.close()
    assert n == 3


# ----------------------------------------------------- schema converter tests

def test_converter_same_schema_is_noop(tmp_path):
    from scanindex.core.repository import schema_converters as sc
    from scanindex.core.repository import constants as C

    p = tmp_path / "t.db"
    _make_legacy_db(p, schema_version=C.SCHEMA_VERSION)
    conn = sqlite3.connect(str(p))
    assert sc.convert_schema_to_latest(conn) is None
    conn.close()


def test_try_convert_same_schema_leaves_no_preconv_bak(tmp_path):
    """Regression: _try_convert used to create a .preconv.bak backup BEFORE
    checking whether conversion was even needed. On same-schema DBs (the common
    1.1.3→1.1.4 case, both schema v8) this left a stray ~MB-sized .preconv.bak
    in the repository folder. The backup must only be taken when a conversion
    is actually pending."""
    from scanindex.core.repository import constants as C

    db = tmp_path / "repository-1.1.4.db"
    _make_legacy_db(db, schema_version=C.SCHEMA_VERSION)
    result = dv._try_convert(db)
    assert result is None
    assert not Path(str(db) + ".preconv.bak").exists(), (
        "same-schema DB must not produce a leftover .preconv.bak"
    )


def test_try_convert_older_schema_creates_preconv_bak(tmp_path):
    """When a real conversion is pending, the backup must be taken so a failed
    converter never destroys the original DB."""
    from scanindex.core.repository import schema_converters as sc

    def _fake_v7_v8(conn):
        conn.execute("ALTER TABLE documents ADD COLUMN new_col TEXT")

    sc._CONVERTERS["7"] = _fake_v7_v8
    try:
        db = tmp_path / "repository-1.1.4.db"
        _make_legacy_db(db, schema_version="7")
        result = dv._try_convert(db)
        assert result == ("7", "8")
        assert Path(str(db) + ".preconv.bak").exists(), (
            "pre-conversion backup must exist when a converter ran"
        )
    finally:
        del sc._CONVERTERS["7"]


def test_converter_missing_raises(tmp_path):
    from scanindex.core.repository import schema_converters as sc

    p = tmp_path / "t.db"
    _make_legacy_db(p, schema_version="6")  # no converter from 6
    conn = sqlite3.connect(str(p))
    with pytest.raises(sc.MissingConverterError):
        sc.convert_schema_to_latest(conn)
    conn.close()


def test_converter_registered_runs_and_bumps_version(tmp_path):
    from scanindex.core.repository import schema_converters as sc
    from scanindex.core.repository import constants as C

    p = tmp_path / "t.db"
    _make_legacy_db(p, schema_version="7")

    def _fake_v7_v8(conn):
        conn.execute("ALTER TABLE documents ADD COLUMN new_col TEXT")

    sc._CONVERTERS["7"] = _fake_v7_v8
    try:
        conn = sqlite3.connect(str(p))
        result = sc.convert_schema_to_latest(conn)
        assert result == ("7", "8")
        cols = [row[1] for row in conn.execute("PRAGMA table_info(documents)")]
        assert "new_col" in cols
        sv = conn.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert sv == "8"
        conn.close()
    finally:
        del sc._CONVERTERS["7"]


# --------------------------------------------------------- ArchiveStore tests

def test_archive_store_versioned_db_filename(tmp_path):
    from scanindex.core.repository.store import ArchiveStore

    store = ArchiveStore(tmp_path, db_filename="repository-1.1.4.db")
    store.ensure_folders()
    with store:  # context manager runs ensure_schema
        store.set_meta("k", "v")
    store.close()
    assert (tmp_path / "repository-1.1.4.db").exists()


def test_archive_store_reset_wipes_versioned_db(tmp_path):
    from scanindex.core.repository.store import ArchiveStore

    store = ArchiveStore(tmp_path, db_filename="repository-1.1.4.db")
    store.ensure_folders()
    with store:
        store.connect().execute(
            "INSERT INTO documents(doc_id, file_name, file_path, sha256, "
            "indexed_status, created_at) "
            "VALUES('d1','f.pdf','pdf/f.pdf','h','indexed',1)"
        )
    store.close()

    store2 = ArchiveStore(tmp_path, db_filename="repository-1.1.4.db")
    store2.reset_archive_data()
    store2.close()

    store3 = ArchiveStore(tmp_path, db_filename="repository-1.1.4.db")
    with store3:
        n = store3.connect().execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0]
    store3.close()
    assert n == 0, "reset must wipe documents"


def test_archive_store_backward_compat_bare_name(tmp_path):
    from scanindex.core.repository.store import ArchiveStore

    store = ArchiveStore(tmp_path)  # no db_filename -> bare repository.db
    store.ensure_folders()
    with store:
        pass
    store.close()
    assert (tmp_path / "repository.db").exists()
