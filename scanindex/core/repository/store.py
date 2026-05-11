"""SQLite store for Kho lưu trữ.

`ArchiveStore` is the single source of truth for archive metadata. Tantivy
is the derived full-text index that gets reconciled against the SQLite
`indexed_status` field on startup (see archive_store/repair.py — Sprint 2).

Typical usage:

    store = ArchiveStore(Path("./repository"))
    with store:
        mismatches = store.version_mismatches()
        if mismatches:
            ...  # prompt user to rebuild
        store.set_meta("last_check_at", str(int(time.time())))
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional

from . import constants as C


_SCHEMA_FILE = Path(__file__).parent / "schema.sql"


class ArchiveStore:
    """SQLite-backed archive metadata store."""

    def __init__(self, archive_path: Path | str):
        self.archive_path = Path(archive_path).resolve()
        self.db_path = self.archive_path / C.SQLITE_FILE
        self._conn: Optional[sqlite3.Connection] = None

    # ---------- Folder + connection ----------

    def ensure_folders(self) -> None:
        self.archive_path.mkdir(parents=True, exist_ok=True)
        (self.archive_path / C.PDF_SUBDIR).mkdir(exist_ok=True)
        (self.archive_path / C.TANTIVY_SUBDIR).mkdir(exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.ensure_folders()
        # autocommit mode — we manage tx explicitly via `with self.transaction()`.
        # check_same_thread=False is needed because SearchEngine runs Tantivy
        # queries on worker threads; both call back into the connection for
        # chunks lookups. WAL + autocommit
        # makes concurrent readers safe.
        conn = sqlite3.connect(str(self.db_path), isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL: concurrent reader during writer (UI search while indexing)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                # Make a closed Kho folder easier to copy/backup: merge WAL
                # pages back into archive.db so archive.db-wal is normally
                # empty/truncated after the app exits cleanly.
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()
            self._conn = None

    # ---------- Schema ----------

    def ensure_schema(self) -> None:
        """Run schema.sql; idempotent thanks to IF NOT EXISTS clauses.
        Never delete an existing archive automatically. Version mismatches
        must be handled by an explicit migration/rebuild path because this
        folder is now user data and is expected to be portable across
        machines."""
        conn = self.connect()
        # Read currently-stored versions BEFORE running DDL: if the user
        # opened an older DB, the new schema's columns won't be added by
        # CREATE TABLE IF NOT EXISTS, leaving us with a half-broken table.
        stored_schema: Optional[str] = None
        stored_chunker: Optional[str] = None
        try:
            rows = conn.execute(
                "SELECT key, value FROM index_meta "
                "WHERE key IN ('schema_version', 'chunker_version')"
            ).fetchall()
            for r in rows:
                if r["key"] == "schema_version":
                    stored_schema = r["value"]
                elif r["key"] == "chunker_version":
                    stored_chunker = r["value"]
        except sqlite3.OperationalError:
            pass
        has_version_mismatch = (
            (stored_schema is not None and stored_schema != C.SCHEMA_VERSION)
            or (stored_chunker is not None and stored_chunker != C.CHUNKER_VERSION)
        )
        ddl = _SCHEMA_FILE.read_text(encoding="utf-8")
        conn.executescript(ddl)
        self._seed_meta_if_empty()
        self.set_meta(
            "needs_migration",
            "1" if (has_version_mismatch or self.version_mismatches()) else "0",
        )

    def _wipe_archive_folder(self) -> None:
        """Close the connection, then delete every derived store + the DB
        itself, then re-create the empty folder layout. Caller re-opens."""
        self.close()
        for child in (
            self.archive_path / C.SQLITE_FILE,
            self.archive_path / f"{C.SQLITE_FILE}-wal",
            self.archive_path / f"{C.SQLITE_FILE}-shm",
            self.archive_path / C.IMPORT_LOG_FILE,
        ):
            try:
                if child.is_file():
                    child.unlink()
            except OSError:
                pass
        for sub in (
            self.archive_path / C.TANTIVY_SUBDIR,
            self.archive_path / C.PDF_SUBDIR,
        ):
            try:
                if sub.is_dir():
                    shutil.rmtree(sub, ignore_errors=True)
            except OSError:
                pass
        self.ensure_folders()

    def reset_archive_data(self) -> None:
        """Explicit destructive reset, used only by the Settings UI after
        typed confirmation. This is the only supported path that wipes user
        archive data."""
        self._wipe_archive_folder()
        self.connect()
        ddl = _SCHEMA_FILE.read_text(encoding="utf-8")
        self.connect().executescript(ddl)
        self._seed_meta_if_empty()

    def _seed_meta_if_empty(self) -> None:
        if self.get_meta("schema_version") is not None:
            return
        self.set_meta_batch({
            "schema_version":      C.SCHEMA_VERSION,
            "chunker_version":     C.CHUNKER_VERSION,
            "tokenizer_name":      C.TOKENIZER_NAME,
            "tokenizer_version":   C.TOKENIZER_VERSION,
            "total_documents":     "0",
            "total_chunks":        "0",
            "created_at":          str(int(time.time())),
        })

    # ---------- Index meta accessors ----------

    def get_meta(self, key: str) -> Optional[str]:
        row = self.connect().execute(
            "SELECT value FROM index_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        now = int(time.time())
        self.connect().execute(
            "INSERT INTO index_meta(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "    value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now),
        )

    def set_meta_batch(self, items: dict) -> None:
        now = int(time.time())
        rows = [(k, str(v), now) for k, v in items.items()]
        self.connect().executemany(
            "INSERT INTO index_meta(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "    value = excluded.value, updated_at = excluded.updated_at",
            rows,
        )

    # ---------- Version checks ----------

    def version_mismatches(self) -> dict[str, tuple[str, str]]:
        """Return {key: (stored, current)} for keys that diverge from
        constants.py. Empty dict means no rebuild needed."""
        checks = {
            "schema_version":    C.SCHEMA_VERSION,
            "chunker_version":   C.CHUNKER_VERSION,
        }
        out: dict[str, tuple[str, str]] = {}
        for key, current in checks.items():
            stored = self.get_meta(key)
            if stored is not None and stored != current:
                out[key] = (stored, current)
        return out

    # ---------- Counter helpers ----------

    def refresh_counters(self) -> None:
        """Recompute total_documents / total_chunks into
        index_meta. Cheap (uses indexes); call after bulk ops."""
        conn = self.connect()
        n_docs = conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE indexed_status != 'deleted'"
        ).fetchone()["n"]
        n_chunks = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE indexed_status != 'deleted'"
        ).fetchone()["n"]
        self.set_meta_batch({
            "total_documents":  str(n_docs),
            "total_chunks":     str(n_chunks),
        })

    # ---------- Context manager ----------

    def __enter__(self) -> "ArchiveStore":
        self.connect()
        self.ensure_schema()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False
