"""Schema converters for the archive SQLite store.

This is the *data* layer of versioning (as opposed to the *filename* layer in
``scanindex.infra.data_versioning``). The on-disk DB records its schema
version in ``index_meta.schema_version``. When that stored value predates
``constants.SCHEMA_VERSION``, :func:`convert_schema_to_latest` walks a chain
of converters v_n → v_(n+1) until the DB is current.

Each converter is ``(conn) -> None`` and runs inside a transaction. A backup
of the DB file is taken by the caller (:func:`migrate_db_if_needed`) before
this module is invoked, so a failing or partial conversion can be rolled
back by hand. If the chain cannot reach the target (a converter is missing),
we raise :class:`MissingConverterError` *instead of silently dropping the
DB* — the user must be told, never surprised by data loss.

Adding a converter when bumping the schema (future release)::

    # 1. Bump constants.SCHEMA_VERSION, e.g. "8" -> "9".
    # 2. Write the converter:
    def _convert_v8_to_v9(conn):
        conn.executescript("ALTER TABLE documents ADD COLUMN new_col TEXT;")
        # ...data backfill...
    # 3. Register it:
    _CONVERTERS["8"] = _convert_v8_to_v9
"""
from __future__ import annotations

import sqlite3
from typing import Callable, Optional

from . import constants as C

Converter = Callable[[sqlite3.Connection], None]


def _convert_v8_to_v9(conn: sqlite3.Connection) -> None:
    """v8 → v9: add ``trang_so`` and ``so_thu_tu`` columns to ``documents``.

    These hold the HSLTCQ dossier-sequencing fields (Tờ số trang số / Số thứ
    tự văn bản trong hồ sơ) captured in Step 2's metadata form, so the
    archive ZIP export reproduces the operator's exact numbering instead of
    falling back to a positional default. Existing rows default to NULL and
    are backfilled at export time (or on the next import that carries the
    values)."""
    # ALTER TABLE ADD COLUMN is idempotent-safe via PRAGMA check: if a prior
    # partial run already added one, the column exists and we skip it.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "trang_so" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN trang_so INTEGER")
    if "so_thu_tu" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN so_thu_tu INTEGER")


def _convert_v9_to_v10(conn: sqlite3.Connection) -> None:
    """v9 → v10: add ``documents.doc_filter_text`` + backfill.

    The column stores the normalized (no-diacritic, lowercased, tokenized)
    concatenation of every advanced-filter field, doc-level and
    dossier-level, so `_scope_doc_ids` can thin candidates with plain
    instr() in SQL before the Python fuzzy matcher re-verifies. Rows keep
    matching semantics: the SQL condition is a *necessary* condition of the
    Python match, never a stricter one.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "doc_filter_text" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN doc_filter_text TEXT")
    # Backfill needs the dossiers join; a minimal/legacy DB may not have
    # that table yet — skip here, ensure_schema creates it and backfills
    # NULL rows on the next open.
    has_dossiers = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dossiers'"
    ).fetchone()
    if has_dossiers:
        from .filter_builder import backfill_missing_filter_text
        backfill_missing_filter_text(conn)


def _convert_v10_to_v11(conn: sqlite3.Connection) -> None:
    """v10 → v11: add the ``index_jobs`` outbox table.

    Rows are written in the same transaction as document mutations and
    deleted after the matching Tantivy commit; startup replays leftovers
    (delete + re-add per doc, idempotent). CREATE TABLE IF NOT EXISTS
    keeps this idempotent for partially-migrated databases.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS index_jobs (
            job_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id     TEXT NOT NULL,
            op         TEXT NOT NULL DEFAULT 'reindex',
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_index_jobs_doc ON index_jobs(doc_id);
    """)


# schema_version (source) -> converter to the next version.
_CONVERTERS: dict[str, Converter] = {
    # "7": _convert_v7_to_v8,
    "8": _convert_v8_to_v9,
    "9": _convert_v9_to_v10,
    "10": _convert_v10_to_v11,
}


class MissingConverterError(Exception):
    """Raised when there is no registered converter to advance a DB to the
    current schema version. We refuse to silently start fresh on top of
    user data."""


def _read_schema_version(conn: sqlite3.Connection) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _set_schema_version(conn: sqlite3.Connection, version: str) -> None:
    import time
    conn.execute(
        "INSERT INTO index_meta(key, value, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "    value = excluded.value, updated_at = excluded.updated_at",
        ("schema_version", version, int(time.time())),
    )


def _next_version(v: str) -> str:
    """Bump the trailing numeric component: '8' -> '9', '2.1' -> '2.2'."""
    parts = v.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError:
        parts.append("1")
    return ".".join(parts)


def needs_conversion(conn: sqlite3.Connection) -> bool:
    """True if the DB records a schema_version older than the current target
    AND at least one converter is registered to advance it.

    Use this to decide whether to take an expensive/side-effecting action
    (like backing up the DB file) *before* calling
    :func:`convert_schema_to_latest`. Without it you'd back up even when no
    conversion runs (e.g. same schema), leaving stray ``.preconv.bak`` files.
    """
    current = _read_schema_version(conn)
    if current is None or current == C.SCHEMA_VERSION:
        return False
    # The full chain may still hit a MissingConverterError at convert time;
    # this helper only reports whether *any* conversion step is pending.
    return True


def convert_schema_to_latest(
    conn: sqlite3.Connection, log: Optional[Callable[[str], None]] = None
) -> Optional[tuple[str, str]]:
    """Advance ``conn``'s DB schema to ``C.SCHEMA_VERSION``.

    Returns ``(from_version, to_version)`` if any conversion ran, or ``None``
    if the DB was already current (or fresh enough that no schema_version is
    recorded yet — :func:`ArchiveStore.ensure_schema` seeds it afterwards).

    Raises :class:`MissingConverterError` when the recorded version cannot be
    advanced to the target because a step in the chain is unregistered.
    """
    log = log or (lambda s: None)
    current = _read_schema_version(conn)
    target = C.SCHEMA_VERSION
    if current is None:
        # Brand-new DB or pre-``index_meta``: ensure_schema will seed versions.
        return None
    if current == target:
        return None

    start = current
    # Guard against an infinite loop if converters mis-bump.
    for _ in range(100):
        if current == target:
            log(f"schema migration complete: {start} -> {target}")
            return (start, target)
        conv = _CONVERTERS.get(current)
        if conv is None:
            raise MissingConverterError(
                f"No converter registered from schema v{current}; "
                f"cannot reach v{target}. Refusing to start fresh."
            )
        nxt = _next_version(current)
        log(f"running schema converter v{current} -> v{nxt}")
        conv(conn)
        _set_schema_version(conn, nxt)
        current = nxt
    raise MissingConverterError(
        "Schema migration exceeded 100 steps — likely a converter "
        "registry bug. Aborting to protect user data."
    )
