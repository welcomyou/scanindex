"""Rebuild the derived Tantivy index from SQLite (the source of truth).

Why a dedicated module
----------------------
SQLite holds every chunk + KIE projection needed to rebuild Tantivy from
scratch, so an index-schema upgrade never touches user data: the new index
is staged in ``<tantivy_dir>.building``, and only a complete build is
swapped into place. A crash or power loss mid-build leaves the previous
generation untouched; the next startup simply retries.

Downgrade safety follows the same version-per-file idea as
``scanindex.infra.data_versioning``: each indexer generation lives in its
own folder (``tantivy_index`` → ``tantivy_index-v2`` → ...). An older app
release keeps reading its own folder, at worst with stale search results.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

from . import constants as C
from .indexer import HybridIndex
from .store import ArchiveStore
from .tokenizer import norm_tokens, search_norm, to_no_diacritic

_log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]

_BATCH_SIZE = 2000


# ---------------------------------------------------------------------------
# Document-level record payload (indexer v3)
# ---------------------------------------------------------------------------

_DOC_RECORD_META_SQL = (
    "SELECT kie_regime_header, kie_issue_org_superior, kie_issue_org_name,"
    " kie_doc_number_symbol, kie_place_date, kie_doc_subject,"
    " kie_addressee, kie_recipients, kie_signer_role, kie_signer_name,"
    " kie_urgency_mark, kie_secrecy_mark, kie_circulation_mark,"
    " kie_doc_type FROM documents WHERE doc_id = ?"
)

# Long chunks are split with CHUNK_OVERLAP_WORDS of repeated context, so
# naively concatenating a page's chunks duplicates the overlap and creates
# ARTIFICIAL phrases at the join ("delta" + "gamma" from two chunks
# matching a query that never existed in the document). De-overlap before
# building the page stream: strip an exact suffix==prefix repeat (the
# chunker always re-emits the overlap verbatim).
_DEOVERLAP_MAX_TOKENS = 40   # chunker overlap is 30; slack for safety
_DEOVERLAP_MIN_TOKENS = 2


def _deoverlap_page_tokens(token_lists: list) -> list:
    out: list = []
    for toks in token_lists:
        if out and len(toks) >= _DEOVERLAP_MIN_TOKENS:
            max_n = min(len(out), len(toks), _DEOVERLAP_MAX_TOKENS)
            for n in range(max_n, _DEOVERLAP_MIN_TOKENS - 1, -1):
                if out[-n:] == toks[:n]:
                    toks = toks[n:]
                    break
        out.extend(toks)
    return out


def document_norm_payload(conn, doc_id: str):
    """Canonical-stream payload for one document's Tantivy record.

    Returns (dossier_id, meta_norm, body_pages_norm) or None when the doc
    no longer exists. `meta_norm` folds ALL 14 KIE fields, each separated
    by a FIELD sentinel so a phrase can never match across two fields;
    each body page's chunks are concatenated and normalized with the SAME
    tokenizer.search_norm the phrase verifier uses — single source of
    truth on both sides of the query.
    """
    from .tokenizer import FIELD_SENTINEL_TOKEN

    d = conn.execute(_DOC_RECORD_META_SQL, (doc_id,)).fetchone()
    if d is None:
        return None
    field_norms = [
        search_norm(d[key] or "")
        for key in d.keys()
    ]
    meta_norm = f" {FIELD_SENTINEL_TOKEN} ".join(
        fn for fn in field_norms if fn
    )
    did_row = conn.execute(
        "SELECT dossier_id FROM documents WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    dossier_id = int(did_row["dossier_id"]) if did_row and did_row["dossier_id"] is not None else None
    rows = conn.execute(
        "SELECT page, text_original FROM chunks "
        "WHERE doc_id = ? AND chunk_type = 'body' "
        "AND indexed_status = 'indexed' ORDER BY page, chunk_id",
        (doc_id,),
    ).fetchall()
    pages: dict[int, list[list]] = {}
    for r in rows:
        pages.setdefault(int(r["page"] or 1), []).append(
            norm_tokens(r["text_original"] or "")
        )
    body_pages = [
        " ".join(_deoverlap_page_tokens(token_lists))
        for _page, token_lists in sorted(pages.items())
        if any(token_lists)
    ]
    return dossier_id, meta_norm, body_pages


def _add_document_record_from_sql(index: HybridIndex, conn, doc_id: str) -> bool:
    payload = document_norm_payload(conn, doc_id)
    if payload is None:
        return False
    dossier_id, meta_norm, body_pages = payload
    index.add_document_record(
        doc_id=doc_id,
        dossier_id=dossier_id,
        meta_norm=meta_norm,
        body_pages_norm=body_pages,
    )
    return True


# ---------------------------------------------------------------------------
# Outbox: crash-safe SQLite → Tantivy synchronisation (schema v11)
# ---------------------------------------------------------------------------

def enqueue_index_job(conn, doc_id: str, op: str = "reindex", *,
                      store: "ArchiveStore | None" = None):
    """Record that `doc_id`'s Tantivy state must be rebuilt.

    Called BEFORE the SQLite mutation: the connection is autocommit, so
    the job may outlive a partially-applied mutation — which is fine,
    because replay rebuilds from the FINAL SQLite truth (idempotent).
    The job is acknowledged later, per document, via
    ``store.note_job_applied(doc_id)`` once that doc's Tantivy writes
    succeeded — never wholesale at session level. Returns the job id (or
    None on a pre-v11 schema, where replay is unavailable by design).
    """
    try:
        cur = conn.execute(
            "INSERT INTO index_jobs (doc_id, op, created_at) VALUES (?, ?, ?)",
            (doc_id, op, int(time.time())),
        )
    except Exception as exc:
        if "no such table" in str(exc):
            return None  # pre-v11 schema — outbox hardening unavailable
        raise  # real failure: surface it, never silently lose the job
    return int(cur.lastrowid)


def replay_pending_index_jobs(store: ArchiveStore, index: HybridIndex) -> dict:
    """Converge Tantivy onto SQLite for every leftover outbox job.

    Runs at startup AFTER run_startup_repair. For each pending doc:
    doc still in SQLite → delete + re-add its chunks + document record;
    doc gone → tantivy delete only. Both paths are idempotent.
    """
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT doc_id FROM index_jobs"
        ).fetchall()
    except Exception:
        return {"replayed": 0, "deleted": 0}
    if not rows:
        return {"replayed": 0, "deleted": 0}
    replayed = deleted = 0
    index.begin_writer()
    for r in rows:
        doc_id = str(r["doc_id"])
        alive = conn.execute(
            "SELECT 1 FROM documents WHERE doc_id = ? "
            "AND indexed_status != 'deleted'", (doc_id,),
        ).fetchone()
        if alive is None:
            index.delete_tantivy_by_doc(doc_id)
            deleted += 1
            continue
        index.delete_tantivy_by_doc(doc_id)
        _add_chunks_for_doc(store, index, doc_id)
        _add_document_record_from_sql(index, conn, doc_id)
        replayed += 1
    index.commit()
    conn.execute("DELETE FROM index_jobs")
    _log.info("index_jobs replayed: %d reindexed, %d purged", replayed, deleted)
    return {"replayed": replayed, "deleted": deleted}


def _add_chunks_for_doc(store: ArchiveStore, index: HybridIndex,
                        doc_id: str) -> int:
    """Re-add one document's chunk records from SQLite (no doc record)."""
    conn = store.connect()
    rows = conn.execute(
        "SELECT c.chunk_id, c.chunk_type, c.text_original, "
        "       c.text_no_diacritic, c.text_segmented, "
        "       d.kie_doc_number_symbol, d.kie_issue_org_name, "
        "       d.kie_issue_org_superior, d.kie_signer_name, "
        "       d.kie_doc_subject, d.kie_recipients, d.dossier_id "
        "FROM chunks c JOIN documents d ON c.doc_id = d.doc_id "
        "WHERE c.doc_id = ? AND c.indexed_status = 'indexed' "
        "AND d.indexed_status = 'indexed' ORDER BY c.chunk_id",
        (doc_id,),
    ).fetchall()
    for r in rows:
        dossier_id = int(r["dossier_id"]) if r["dossier_id"] is not None else None
        if r["chunk_type"] == "metadata":
            index.add_metadata_chunk(
                doc_id=doc_id,
                dossier_id=dossier_id,
                chunk_id=int(r["chunk_id"]),
                doc_number=r["kie_doc_number_symbol"] or "",
                signer_name=r["kie_signer_name"] or "",
                issue_org=" ".join(filter(None, [
                    r["kie_issue_org_name"] or "",
                    r["kie_issue_org_superior"] or "",
                ])).strip(),
                subject=r["kie_doc_subject"] or "",
                recipients=r["kie_recipients"] or "",
                metadata_text=r["text_original"] or "",
            )
        else:
            index.add_body_text_chunk(
                doc_id=doc_id,
                dossier_id=dossier_id,
                chunk_id=int(r["chunk_id"]),
                body_original=r["text_original"] or "",
                body_no_diacritic=r["text_no_diacritic"]
                or to_no_diacritic(r["text_original"] or ""),
                body_segmented=r["text_segmented"] or "",
            )
    return len(rows)


def index_needs_rebuild(store: ArchiveStore,
                        archive_path: Path) -> Optional[str]:
    """Return a short reason string when the on-disk index must be rebuilt,
    or None when it is current.

    Reasons:
      - "indexer_version": meta predates the current schema generation
        (fresh installs seed it; older repos have no key at all).
      - "missing_dir": the versioned folder vanished (manual copy/partial
        uninstall).
      - "stale": the folder exists and claims to be current, but SQLite has
        a different number of indexable chunks than were indexed at build
        time — e.g. an older app release added documents in the meantime.
    """
    stored = store.get_meta("indexer_version")
    if stored != C.INDEXER_VERSION:
        return "indexer_version"
    tantivy_dir = Path(archive_path) / C.TANTIVY_SUBDIR
    if not (tantivy_dir / "meta.json").exists():
        return "missing_dir"
    built = store.get_meta("indexer_built_chunks")
    if built is None:
        return "stale"
    current = _count_indexable_chunks(store)
    try:
        if int(built) != current:
            return "stale"
    except ValueError:
        return "stale"
    return None


def _rename_with_retry(src: Path, dst: Path, attempts: int = 4) -> None:
    """Directory rename that tolerates Windows' lingering-mmap window.

    Even after tantivy's writer/lock is released and the Python-side Index
    object dropped, Windows can deny the rename for a moment while segment
    mmaps close. Force GC, then retry briefly before giving up.
    """
    import gc

    last_exc: Exception | None = None
    for attempt in range(attempts):
        gc.collect()
        try:
            src.rename(dst)
            return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.25 * (attempt + 1))
    raise last_exc if last_exc else OSError(f"rename failed: {src} -> {dst}")


def _count_indexable_chunks(store: ArchiveStore) -> int:
    row = store.connect().execute(
        "SELECT COUNT(*) AS n FROM chunks c "
        "JOIN documents d ON c.doc_id = d.doc_id "
        "WHERE c.indexed_status = 'indexed' "
        "  AND d.indexed_status = 'indexed'"
    ).fetchone()
    return int(row["n"]) if row else 0


def note_index_write(store: ArchiveStore) -> None:
    """Refresh the built-chunks watermark after any index-writing session
    (import / admin edit / repair) so staleness detection stays accurate.
    Acknowledges the outbox for EXACTLY the documents whose Tantivy writes
    this session completed (store.note_job_applied) — a document that
    failed mid-import keeps its job for startup replay, and another
    concurrent worker's pending jobs are never touched."""
    try:
        store.set_meta("indexer_built_chunks", str(_count_indexable_chunks(store)))
        store.set_meta("indexer_version", C.INDEXER_VERSION)
    except Exception:
        pass  # bookkeeping only; never fail the caller's commit path
    docs = list(getattr(store, "_applied_job_docs", set()) or ())
    if not docs:
        return
    conn = store.connect()
    for start in range(0, len(docs), 4000):
        batch = docs[start:start + 4000]
        try:
            ph = ",".join("?" * len(batch))
            conn.execute(
                f"DELETE FROM index_jobs WHERE doc_id IN ({ph})", batch)
        except Exception:
            break  # never fail the caller's commit path
    store._applied_job_docs.clear()


def rebuild_search_index(store: ArchiveStore,
                         archive_path: Path,
                         progress_cb: Optional[ProgressCallback] = None,
                         cancel_check: Optional[CancelCheck] = None) -> dict:
    """Build a fresh index for this indexer generation from SQLite.

    Streams every indexed chunk, mirrors the importer's field projections,
    commits in batches (calling progress_cb(done, total) along the way), then
    swaps the staged folder into place. Never mutates SQLite user data.

    Returns {"chunks": n, "elapsed": seconds} on success or
    {"cancelled": True, "chunks": n} when cancel_check fired.
    """
    archive_path = Path(archive_path)
    target_dir = archive_path / C.TANTIVY_SUBDIR
    build_dir = archive_path / (C.TANTIVY_SUBDIR + ".building")
    t0 = time.time()
    total = _count_indexable_chunks(store)
    _log.info("rebuild start: %s chunks to index (target=%s)", total, target_dir.name)

    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)

    staging = HybridIndex(archive_path, tantivy_dir=build_dir)
    staging.open()
    done = 0
    doc_record_count = 0
    try:
        rows_iter = store.connect().execute(
            "SELECT c.chunk_id, c.doc_id, c.chunk_type, "
            "       c.text_original, c.text_no_diacritic, c.text_segmented, "
            "       d.dossier_id, "
            "       d.kie_doc_number_symbol, d.kie_issue_org_name, "
            "       d.kie_issue_org_superior, d.kie_signer_name, "
            "       d.kie_doc_subject, d.kie_recipients "
            "FROM chunks c JOIN documents d ON c.doc_id = d.doc_id "
            "WHERE c.indexed_status = 'indexed' "
            "  AND d.indexed_status = 'indexed' "
            "ORDER BY c.chunk_id"
        )
        batch = 0
        for r in rows_iter:
            if cancel_check and cancel_check():
                staging.close()
                shutil.rmtree(build_dir, ignore_errors=True)
                _log.info("rebuild cancelled at %d/%d chunks", done, total)
                return {"cancelled": True, "chunks": done}
            dossier_id = int(r["dossier_id"]) if r["dossier_id"] is not None else None
            if r["chunk_type"] == "metadata":
                staging.add_metadata_chunk(
                    doc_id=r["doc_id"],
                    dossier_id=dossier_id,
                    chunk_id=int(r["chunk_id"]),
                    doc_number=r["kie_doc_number_symbol"] or "",
                    signer_name=r["kie_signer_name"] or "",
                    issue_org=" ".join(filter(None, [
                        r["kie_issue_org_name"] or "",
                        r["kie_issue_org_superior"] or "",
                    ])).strip(),
                    subject=r["kie_doc_subject"] or "",
                    recipients=r["kie_recipients"] or "",
                    metadata_text=r["text_original"] or "",
                )
            else:
                staging.add_body_text_chunk(
                    doc_id=r["doc_id"],
                    dossier_id=dossier_id,
                    chunk_id=int(r["chunk_id"]),
                    body_original=r["text_original"] or "",
                    body_no_diacritic=r["text_no_diacritic"]
                    or to_no_diacritic(r["text_original"] or ""),
                    body_segmented=r["text_segmented"] or "",
                )
            done += 1
            batch += 1
            if batch >= _BATCH_SIZE:
                staging.commit()
                staging.begin_writer()
                batch = 0
                if progress_cb:
                    progress_cb(done, total)
        staging.commit()

        # Indexer v3: emit one DOCUMENT-level record per document so phrase
        # queries are complete without any linear scan. One small query per
        # doc; total stays bounded by the document count.
        doc_ids = [
            r["doc_id"] for r in store.connect().execute(
                "SELECT DISTINCT c.doc_id AS doc_id FROM chunks c "
                "JOIN documents d ON c.doc_id = d.doc_id "
                "WHERE c.indexed_status = 'indexed' "
                "AND d.indexed_status = 'indexed'"
            ).fetchall()
        ]
        staging.begin_writer()
        for n, did in enumerate(doc_ids):
            if cancel_check and cancel_check():
                staging.close()
                shutil.rmtree(build_dir, ignore_errors=True)
                _log.info("rebuild cancelled during doc records (%d/%d)",
                          n, len(doc_ids))
                return {"cancelled": True, "chunks": done}
            _add_document_record_from_sql(staging, store.connect(), did)
        doc_record_count = len(doc_ids)
        staging.commit()
    finally:
        staging.close()
        staging = None  # drop the last reference so mmaps can close (Windows)

    # Swap only after a fully-built index exists. Replacing an existing
    # generation is safe: it is derived data, and old generations used by
    # older app releases keep their own folder names.
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    _rename_with_retry(build_dir, target_dir)

    store.set_meta("indexer_version", C.INDEXER_VERSION)
    store.set_meta("indexer_built_chunks", str(done))
    try:
        store.connect().execute("DELETE FROM index_jobs")  # full rebuild ⇒ outbox void
    except Exception:
        pass
    if progress_cb:
        progress_cb(done, total)
    _log.info("rebuild done: %d chunks + %d doc records in %.1fs -> %s",
              done, doc_record_count, time.time() - t0, target_dir.name)
    return {"chunks": done, "elapsed": time.time() - t0}
