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
from .tokenizer import to_no_diacritic

_log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]

_BATCH_SIZE = 2000


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
    (import / admin edit / repair) so staleness detection stays accurate."""
    try:
        store.set_meta("indexer_built_chunks", str(_count_indexable_chunks(store)))
        store.set_meta("indexer_version", C.INDEXER_VERSION)
    except Exception:
        pass  # bookkeeping only; never fail the caller's commit path


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
    if progress_cb:
        progress_cb(done, total)
    _log.info("rebuild done: %d chunks in %.1fs -> %s",
              done, time.time() - t0, target_dir.name)
    return {"chunks": done, "elapsed": time.time() - t0}
