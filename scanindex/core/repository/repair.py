"""Startup reconcile between SQLite and Tantivy.

SQLite is the source of truth. Tantivy is the derived search index. If the
application stops mid-import, this cleanup marks incomplete rows consistently
and removes their Tantivy entries.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .indexer import HybridIndex
from .store import ArchiveStore


def run_startup_repair(store: ArchiveStore,
                       index: HybridIndex,
                       log_cb: Optional[Callable[[str], None]] = None) -> dict:
    log = log_cb or (lambda s: None)
    conn = store.connect()
    summary = {
        "failed_docs": 0,
        "deleted_chunks": 0,
    }

    rows = conn.execute(
        "SELECT doc_id FROM documents "
        " WHERE indexed_status = 'pending' "
        "   AND doc_id NOT IN (SELECT DISTINCT doc_id FROM chunks)"
    ).fetchall()
    now = int(time.time())
    for r in rows:
        conn.execute(
            "UPDATE documents SET indexed_status = 'failed', updated_at = ? "
            "WHERE doc_id = ?",
            (now, r["doc_id"]),
        )
        summary["failed_docs"] += 1

    pending = conn.execute(
        "SELECT chunk_id, doc_id FROM chunks WHERE indexed_status = 'pending'"
    ).fetchall()
    if pending:
        index.begin_writer()
        affected_docs = {r["doc_id"] for r in pending}
        for did in affected_docs:
            index.delete_tantivy_by_doc(did)
        conn.execute(
            "UPDATE chunks SET indexed_status = 'deleted' "
            "WHERE indexed_status = 'pending'"
        )
        summary["deleted_chunks"] = len(pending)
        index.commit()

    store.refresh_counters()
    log(f"Startup repair done: {summary}")
    return summary
