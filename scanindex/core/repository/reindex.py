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

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Callable, NamedTuple, Optional

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
# building the page stream — but ONLY between two chunks whose provenance
# proves they are consecutive parts of the SAME chunker split. Two
# INDEPENDENT chunks that merely happen to end and start with the same
# words keep their honest duplication: stripping there would DELETE a
# phrase that truly exists and CREATE a fake join phrase instead
# (expert round-4 repro: two body_single chunks sharing "repeat phrase").
#
# The overlap length is measured in RAW WHITESPACE WORDS — the exact unit
# the chunker slices with (``cur[-CHUNK_OVERLAP_WORDS:]`` / 220-word
# steps) — and only THEN is the stitched stream normalized. Measuring in
# normalized tokens with an estimated cap is wrong: one raw word like
# "x0-y0" splits into 2+ tokens, so a 30-word overlap can exceed any
# token cap and silently defeat the strip (expert round-5 repro).
_DEOVERLAP_MIN_WORDS = 2


class _PageChunk(NamedTuple):
    """One body chunk's RAW whitespace words plus the provenance needed
    to tell a chunker split continuation apart from an independent
    neighbour. Words (not normalized tokens) because that is the unit
    the chunker's overlap is defined in."""
    words: list
    block_idx: int
    merge_reason: str
    source_blocks: tuple


def _split_part_no(merge_reason: Optional[str]) -> Optional[int]:
    """'body_split_3' → 3; anything else (incl. NULL / other reasons) →
    None."""
    if not merge_reason or not merge_reason.startswith("body_split_"):
        return None
    try:
        return int(merge_reason[len("body_split_"):])
    except ValueError:
        return None


def _is_split_continuation(prev: _PageChunk, cur: _PageChunk) -> bool:
    """Provenance check: `cur` is the next part of the SAME chunker split
    that produced `prev`. Split parts share their parent paragraph's
    block_idx and source_blocks and carry consecutive body_split_N
    numbers; chunks from different paragraphs never satisfy all three
    conditions at once."""
    a = _split_part_no(prev.merge_reason)
    b = _split_part_no(cur.merge_reason)
    if a is None or b is None or b != a + 1:
        return False
    return (prev.block_idx == cur.block_idx
            and prev.source_blocks == cur.source_blocks)


def _deoverlap_page_words(chunks: list) -> list:
    """Concatenate one page's _PageChunk items into one RAW-word stream,
    dropping the chunker's deliberate overlap at split joins only. The
    chunker re-emits the overlap verbatim as whole words, so an exact
    suffix==prefix match of up to CHUNK_OVERLAP_WORDS raw words is safe
    to strip exactly there — and nowhere else. The caller normalizes the
    stitched stream afterwards."""
    out: list = []
    prev: Optional[_PageChunk] = None
    for ch in chunks:
        words = ch.words
        if (out and prev is not None
                and _is_split_continuation(prev, ch)
                and len(words) >= _DEOVERLAP_MIN_WORDS):
            # n0 is the guaranteed overlap size: min(30, words the
            # previous part can offer, words this part starts with).
            # Shorter matches below n0 only cover degenerate tails.
            n0 = min(C.CHUNK_OVERLAP_WORDS, len(out), len(words))
            for n in range(n0, _DEOVERLAP_MIN_WORDS - 1, -1):
                if out[-n:] == words[:n]:
                    words = words[n:]
                    break
        out.extend(words)
        prev = ch
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
        "SELECT page, block_idx, merge_reason, source_blocks, text_original "
        "FROM chunks "
        "WHERE doc_id = ? AND chunk_type = 'body' "
        "AND indexed_status = 'indexed' ORDER BY page, chunk_id",
        (doc_id,),
    ).fetchall()
    pages: dict[int, list[_PageChunk]] = {}
    for r in rows:
        try:
            sources = tuple(json.loads(r["source_blocks"] or "[]"))
        except Exception:
            sources = ()
        pages.setdefault(int(r["page"] or 1), []).append(_PageChunk(
            words=(r["text_original"] or "").split(),
            block_idx=int(r["block_idx"] or 0),
            merge_reason=r["merge_reason"] or "",
            source_blocks=sources,
        ))
    # Stitch each page in RAW words (dropping split overlaps), and only
    # then normalize the whole stream — same token boundary the phrase
    # verifier uses, applied once to the final text.
    body_pages = [
        " ".join(norm_tokens(" ".join(_deoverlap_page_words(page_chunks))))
        for _page, page_chunks in sorted(pages.items())
        if any(ch.words for ch in page_chunks)
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
    The job is acknowledged later, BY JOB ID, via
    ``store.note_job_applied(job_id)`` once that doc's Tantivy writes
    succeeded — never wholesale at session level, and never by doc_id
    (a doc_id sweep would delete a NEWER job another worker just
    enqueued for the same document). Callers MUST keep the returned id
    and pass it to note_job_applied. Returns None on a pre-v11 schema,
    where replay is unavailable by design.
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
    doc gone → tantivy delete only. Both paths are idempotent. Only the
    job ids that existed when replay started are acknowledged — a job
    enqueued by another worker DURING replay survives for the next run.
    """
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT job_id, doc_id FROM index_jobs"
        ).fetchall()
    except Exception:
        return {"replayed": 0, "deleted": 0}
    if not rows:
        return {"replayed": 0, "deleted": 0}
    job_ids = [int(r["job_id"]) for r in rows]
    replayed = deleted = 0
    index.begin_writer()
    for doc_id in dict.fromkeys(str(r["doc_id"]) for r in rows):
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
    for start in range(0, len(job_ids), 4000):
        batch = job_ids[start:start + 4000]
        try:
            ph = ",".join("?" * len(batch))
            conn.execute(
                f"DELETE FROM index_jobs WHERE job_id IN ({ph})", batch)
        except Exception:
            break
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
    Acknowledges the outbox for EXACTLY the job ids whose Tantivy writes
    this session completed (store.note_job_applied(job_id)) — a document
    that failed mid-import keeps its job for startup replay, and jobs
    enqueued by other workers, INCLUDING a newer job for the same doc_id,
    are never touched (DELETE ... WHERE job_id IN, never by doc_id)."""
    try:
        store.set_meta("indexer_built_chunks", str(_count_indexable_chunks(store)))
        store.set_meta("indexer_version", C.INDEXER_VERSION)
    except Exception:
        pass  # bookkeeping only; never fail the caller's commit path
    jobs = list(getattr(store, "_acked_job_ids", set()) or ())
    if not jobs:
        return
    conn = store.connect()
    for start in range(0, len(jobs), 4000):
        batch = jobs[start:start + 4000]
        try:
            ph = ",".join("?" * len(batch))
            conn.execute(
                f"DELETE FROM index_jobs WHERE job_id IN ({ph})", batch)
        except Exception:
            break  # never fail the caller's commit path
    store._acked_job_ids.clear()


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
