"""CRUD operations for Kho lÆ°u trá»¯ documents + dossiers.

Imports run via `Importer.import_dossier` are the canonical write path
(big workflow). This module is for *targeted* edits the user performs
inside the Kho UI:

  - delete_document         remove 1 file
  - delete_documents_bulk   remove many files in one Tantivy commit
  - delete_dossier          remove 1 dossier (cascades documents)
  - delete_dossiers_bulk    remove many dossiers
  - update_document_metadata edit the 14 KIE fields of a doc
  - update_dossier_metadata  edit dossier title / codes / retention / term
  - add_document            append a new file to an existing dossier
                              (caller supplies pre-extracted body chunks
                              + KIE fields; the orchestrator runs OCR/
                              correction/KIE before calling this)

Every mutating call ends with `index.commit()` so Tantivy
on-disk state matches the SQLite truth.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from . import constants as C
from .chunker import Chunk, synthesize_metadata_chunk
from .importer import (
    DossierCodes, KIE_COLUMNS, _kie_col, _file_sha256, _strip_ocr_suffix,
    _replace_pdf_file,
)
from .indexer import HybridIndex
from .store import ArchiveStore
from .tokenizer import segment, to_no_diacritic


@dataclass
class DeleteStats:
    deleted_docs: int = 0
    deleted_chunks: int = 0
    freed_bytes: int = 0
    errors: List[str] = None


@dataclass
class RelabelStats:
    code_changed: bool = False
    renamed_docs: int = 0
    old_key: str = ""
    new_key: str = ""
    target_dir: str = ""


# ---------------------------------------------------------------- helpers


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _abs_pdf_path(store: ArchiveStore, file_path: str) -> Path:
    return (store.archive_path / file_path).resolve()


def _allocate_document_instance_id(conn, sha256: str, dossier_id: int) -> str:
    """Return a document-instance id.

    The first copy keeps the historical id (= sha256). If the same PDF bytes
    are later stored in another dossier, create a distinct id while keeping
    sha256 in its own column for duplicate/audit checks.
    """
    if conn.execute(
        "SELECT 1 FROM documents WHERE doc_id = ? LIMIT 1", (sha256,)
    ).fetchone() is None:
        return sha256
    suffix = 1
    while True:
        candidate = f"{sha256}-{int(dossier_id)}-{suffix:03d}"
        if conn.execute(
            "SELECT 1 FROM documents WHERE doc_id = ? LIMIT 1", (candidate,)
        ).fetchone() is None:
            return candidate
        suffix += 1


_INVALID_PATH_PART_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _validate_code_part(label: str, value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Thiáº¿u {label}")
    if _INVALID_PATH_PART_RE.search(text) or text in {".", ".."}:
        raise ValueError(f"{label} chá»©a kÃ½ tá»± khÃ´ng há»£p lá»‡: {text!r}")
    return text


def _dossier_key(ma_dinh_danh: str, fonds: str, catalog: str,
                 dossier_code: str) -> str:
    return f"{ma_dinh_danh}-{fonds}-{catalog}-{dossier_code}"


def _dossier_pdf_dir(store: ArchiveStore, *, ma_dinh_danh: str,
                     fonds: str, catalog: str, dossier_code: str) -> Path:
    return (
        store.archive_path / C.PDF_SUBDIR
        / ma_dinh_danh / fonds / catalog / dossier_code
    )


def _canonical_doc_name(ma_dinh_danh: str, fonds: str, catalog: str,
                        dossier_code: str, index: int) -> str:
    return f"{ma_dinh_danh}-{fonds}-{catalog}-{dossier_code}-{index:03d}.pdf"


def _cleanup_empty_pdf_parents(store: ArchiveStore, start: Path) -> None:
    """Best-effort removal of empty folders up to, but not including, pdf/."""
    pdf_root = (store.archive_path / C.PDF_SUBDIR).resolve()
    cur = Path(start).resolve()
    while cur != pdf_root and pdf_root in cur.parents:
        try:
            cur.rmdir()
        except OSError:
            break
        cur = cur.parent


# ---------------------------------------------------------------- delete


def delete_document(store: ArchiveStore, index: HybridIndex,
                    doc_id: str, *, commit: bool = True) -> DeleteStats:
    """Hard-delete a single document. CASCADE in SQL takes care of
    chunks; we delete from Tantivy + rm the physical PDF + commit the index."""
    stats = DeleteStats(errors=[])
    conn = store.connect()
    row = conn.execute(
        "SELECT file_path FROM documents WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    if row is None:
        return stats
    file_path = row["file_path"] or ""

    # Count chunks before delete (CASCADE wipes them, lose count).
    n_chunks = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE doc_id = ?", (doc_id,)
    ).fetchone()["n"]

    # Tantivy delete-by-doc (removes BOTH metadata + body chunks).
    index.delete_tantivy_by_doc(doc_id)

    # Hard-delete SQL row: CASCADE wipes chunks.
    conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))

    # Remove the physical PDF (best-effort).
    if file_path:
        pdf = _abs_pdf_path(store, file_path)
        if pdf.exists() and pdf.is_file():
            stats.freed_bytes += _file_size(pdf)
            try:
                pdf.unlink()
            except OSError as e:
                stats.errors.append(f"unlink {pdf.name}: {e}")

    stats.deleted_docs = 1
    stats.deleted_chunks = int(n_chunks or 0)
    if commit:
        index.begin_writer()  # no-op if already open
        index.commit()
        store.refresh_counters()
    return stats


def delete_documents_bulk(store: ArchiveStore, index: HybridIndex,
                          doc_ids: Iterable[str]) -> DeleteStats:
    """Batch the per-doc deletes into one Tantivy commit at the end."""
    total = DeleteStats(errors=[])
    index.begin_writer()
    for did in doc_ids:
        s = delete_document(store, index, did, commit=False)
        total.deleted_docs += s.deleted_docs
        total.deleted_chunks += s.deleted_chunks
        total.freed_bytes += s.freed_bytes
        if s.errors:
            total.errors.extend(s.errors)
    index.commit()
    store.refresh_counters()
    return total


def delete_dossier(store: ArchiveStore, index: HybridIndex,
                   dossier_id: int) -> DeleteStats:
    """Cascade-delete: every document in the dossier, then the dossier
    folder + the dossier row itself."""
    stats = DeleteStats(errors=[])
    conn = store.connect()
    row = conn.execute(
        "SELECT ma_dinh_danh, fonds, catalog, dossier_code "
        "FROM dossiers WHERE dossier_id = ?",
        (dossier_id,),
    ).fetchone()
    if row is None:
        return stats

    doc_ids = [r["doc_id"] for r in conn.execute(
        "SELECT doc_id FROM documents WHERE dossier_id = ?", (dossier_id,)
    ).fetchall()]
    sub = delete_documents_bulk(store, index, doc_ids)
    stats.deleted_docs = sub.deleted_docs
    stats.deleted_chunks = sub.deleted_chunks
    stats.freed_bytes = sub.freed_bytes
    if sub.errors:
        stats.errors.extend(sub.errors)

    # Drop the dossier row (FK ON DELETE CASCADE already wiped documents).
    conn.execute("DELETE FROM dossiers WHERE dossier_id = ?", (dossier_id,))

    # Best-effort: remove empty parent folders left by the deleted PDFs.
    # Do not rmtree by dossier code: older archives used 3-level folders
    # that can contain PDFs from multiple ma_dinh_danh values.
    try:
        _cleanup_empty_pdf_parents(
            store,
            store.archive_path / C.PDF_SUBDIR
            / (row["ma_dinh_danh"] or "") / (row["fonds"] or "")
            / (row["catalog"] or "") / (row["dossier_code"] or ""),
        )
        _cleanup_empty_pdf_parents(
            store,
            store.archive_path / C.PDF_SUBDIR
            / (row["fonds"] or "") / (row["catalog"] or "")
            / (row["dossier_code"] or ""),
        )
    except Exception:
        pass
    store.refresh_counters()
    return stats


def delete_dossiers_bulk(store: ArchiveStore, index: HybridIndex,
                         dossier_ids: Iterable[int]) -> DeleteStats:
    total = DeleteStats(errors=[])
    for did in dossier_ids:
        s = delete_dossier(store, index, did)
        total.deleted_docs += s.deleted_docs
        total.deleted_chunks += s.deleted_chunks
        total.freed_bytes += s.freed_bytes
        if s.errors:
            total.errors.extend(s.errors)
    return total


# ---------------------------------------------------------------- update


def update_document_metadata(store: ArchiveStore, index: HybridIndex,
                              doc_id: str, kie_fields: dict) -> None:
    """Persist edited 14 raw KIE columns + rebuild the per-doc metadata
    chunk.

    `kie_fields` keys are the same `kie_*` column names; values are
    cleaned strings (caller normalises via kie_text_normalize).

    Tantivy doesn't support per-row UPDATE; we delete every chunk for
    this doc from Tantivy and re-add metadata + body chunks reading
    from SQL."""
    from scanindex.core.kie.text_normalize import normalize_subject_for_storage, single_line_text
    conn = store.connect()
    row = conn.execute(
        "SELECT dossier_id, kie_annotation_json FROM documents WHERE doc_id = ?",
        (doc_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"doc_id not found: {doc_id}")
    dossier_id = row["dossier_id"]

    # Normalise the same way Importer does for fresh imports.
    cleaned = dict(kie_fields)
    if cleaned.get("kie_doc_subject"):
        cleaned["kie_doc_subject"] = normalize_subject_for_storage(
            cleaned["kie_doc_subject"],
            cleaned.get("kie_doc_type", ""),
        )
    for col in ("kie_issue_org_name", "kie_issue_org_superior"):
        if cleaned.get(col):
            cleaned[col] = single_line_text(cleaned[col])

    # 1. Update documents row.
    sets = ", ".join(f"{c} = :{c}" for c in KIE_COLUMNS)
    params = {c: cleaned.get(c, "") for c in KIE_COLUMNS}
    params["doc_id"] = doc_id
    params["now"] = int(time.time())
    conn.execute(
        f"UPDATE documents SET {sets}, updated_at = :now WHERE doc_id = :doc_id",
        params,
    )

    # 2. Update kie_annotation_json â€” keep bbox/page/score/source from the
    # original annotation, only override `text` for the 14 labels we know.
    try:
        ann = json.loads(row["kie_annotation_json"] or "{}")
    except Exception:
        ann = {}
    fis = ann.setdefault("field_instances", [])
    by_label = {f.get("label"): f for f in fis if f.get("label")}
    for col in KIE_COLUMNS:
        label = col[len("kie_"):].upper()
        new_text = cleaned.get(col, "")
        if label in by_label:
            by_label[label]["text"] = new_text
        elif new_text:
            fis.append({
                "label": label,
                "text": new_text,
                "page_index": 0,
                "bbox": [0, 0, 0, 0],
                "field_id": f"{label.lower()}_user_edit",
                "score": 1.0,
                "source": "user_edit",
            })
    conn.execute(
        "UPDATE documents SET kie_annotation_json = ? WHERE doc_id = ?",
        (json.dumps(ann, ensure_ascii=False), doc_id),
    )

    # 3. Re-synthesise metadata chunk text + UPDATE the existing chunk row.
    new_meta = synthesize_metadata_chunk(cleaned)
    if new_meta is not None:
        meta_text = new_meta.text
        meta_tno = to_no_diacritic(meta_text)
        meta_tseg = segment(meta_text)
        # If a metadata chunk already exists, UPDATE; else INSERT.
        existing = conn.execute(
            "SELECT chunk_id FROM chunks "
            "WHERE doc_id = ? AND chunk_type = 'metadata' LIMIT 1",
            (doc_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE chunks SET text_original = ?, text_no_diacritic = ?,"
                " text_segmented = ?, word_count = ? "
                "WHERE chunk_id = ?",
                (meta_text, meta_tno, meta_tseg,
                 new_meta.word_count, int(existing["chunk_id"])),
            )
        else:
            conn.execute(
                "INSERT INTO chunks ("
                "  doc_id, doc_version, chunk_type, page, block_idx,"
                "  text_original, text_no_diacritic, text_segmented,"
                "  bbox, word_count, source_blocks, merge_reason,"
                "  indexed_status, created_at"
                ") VALUES (?, 1, 'metadata', 1, -1, ?, ?, ?, ?, ?, '[]',"
                "  'kie_metadata', 'pending', ?)",
                (doc_id, meta_text, meta_tno, meta_tseg,
                 json.dumps([0, 0, 0, 0]), new_meta.word_count,
                 int(time.time())),
            )

    # 4. Tantivy: delete-by-doc, then re-add ALL chunks (metadata + body)
    # reading from SQL.
    index.begin_writer()
    index.delete_tantivy_by_doc(doc_id)
    chunks_rows = conn.execute(
        "SELECT chunk_id, chunk_type, text_original FROM chunks "
        "WHERE doc_id = ?", (doc_id,),
    ).fetchall()
    doc_number_proj = cleaned.get("kie_doc_number_symbol", "")
    signer_proj     = cleaned.get("kie_signer_name", "")
    issue_org_proj  = " ".join(filter(None, [
        cleaned.get("kie_issue_org_name", ""),
        cleaned.get("kie_issue_org_superior", ""),
    ])).strip()
    subject_proj    = cleaned.get("kie_doc_subject", "")
    recipients_proj = cleaned.get("kie_recipients", "")
    for r in chunks_rows:
        if r["chunk_type"] == "metadata":
            index.add_metadata_chunk(
                doc_id=doc_id,
                dossier_id=dossier_id,
                chunk_id=int(r["chunk_id"]),
                doc_number=doc_number_proj,
                signer_name=signer_proj,
                issue_org=issue_org_proj,
                subject=subject_proj,
                recipients=recipients_proj,
                metadata_text=r["text_original"] or "",
            )
        else:
            text = r["text_original"] or ""
            tno = to_no_diacritic(text)
            tseg = segment(text)
            # Body chunks: re-add Tantivy text.
            index.add_body_text_chunk(
                doc_id=doc_id, dossier_id=dossier_id,
                chunk_id=int(r["chunk_id"]),
                body_original=text,
                body_no_diacritic=tno,
                body_segmented=tseg or "",
            )
    index.commit()


def update_dossier_metadata(store: ArchiveStore, dossier_id: int,
                             title: str = "", retention: str = "",
                             term: str = "", storage_unit: str = "",
                             physical_state: str = "", topic: str = "",
                             note: str = "", fonds_name: str = "",
                             catalog_name: str = "") -> None:
    """In-place update of dossier title + soft fields. The 4 identity
    codes are NOT changed here â€” those form the natural unique key, and
    changing them would mean re-keying every child document. Add a
    separate `relabel_dossier` if needed later."""
    conn = store.connect()
    conn.execute(
        "UPDATE dossiers SET title = ?, fonds_name = ?, catalog_name = ?,"
        " retention = ?, term = ?,"
        " storage_unit = ?, physical_state = ?, topic = ?, note = ?,"
        " updated_at = ? WHERE dossier_id = ?",
        (
            (title or "").strip()[:1000] or None,
            (fonds_name or "").strip()[:1000] or None,
            (catalog_name or "").strip()[:1000] or None,
            (retention or "").strip() or None,
            (term or "").strip() or None,
            (storage_unit or "").strip() or None,
            (physical_state or "").strip() or None,
            (topic or "").strip()[:1000] or None,
            (note or "").strip()[:1000] or None,
            int(time.time()), dossier_id,
        ),
    )


def relabel_dossier(store: ArchiveStore, dossier_id: int, *,
                    ma_dinh_danh: str, fonds: str, catalog: str,
                    dossier_code: str, title: str = "",
                    is_unstructured: bool = False,
                    retention: str = "", term: str = "",
                    storage_unit: str = "", physical_state: str = "",
                    topic: str = "", note: str = "",
                    fonds_name: str = "", catalog_name: str = "") -> RelabelStats:
    """Update dossier metadata and, when any of the 4 identity codes changes,
    rename every child PDF + update documents.file_name/file_path.

    This is intentionally a separate operation from update_dossier_metadata:
    changing codes changes the dossier's natural key and physical storage
    layout. The move is guarded against duplicate dossier keys and target
    filename collisions; on failure it rolls moved files back best-effort.
    """
    ma_dinh_danh = _validate_code_part("mÃ£ Ä‘á»‹nh danh", ma_dinh_danh)
    fonds = _validate_code_part("mÃ£ phÃ´ng", fonds)
    catalog = _validate_code_part("má»¥c lá»¥c", catalog)
    dossier_code = _validate_code_part("sá»‘ há»“ sÆ¡", dossier_code)

    conn = store.connect()
    row = conn.execute(
        "SELECT ma_dinh_danh, fonds, catalog, dossier_code "
        "FROM dossiers WHERE dossier_id = ?",
        (dossier_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"dossier_id not found: {dossier_id}")

    old_key = _dossier_key(
        row["ma_dinh_danh"] or "", row["fonds"] or "",
        row["catalog"] or "", row["dossier_code"] or "",
    )
    new_key = _dossier_key(ma_dinh_danh, fonds, catalog, dossier_code)
    code_changed = old_key != new_key

    if code_changed:
        conflict = conn.execute(
            "SELECT dossier_id FROM dossiers "
            "WHERE ma_dinh_danh = ? AND fonds = ? "
            "  AND catalog = ? AND dossier_code = ? "
            "  AND dossier_id != ?",
            (ma_dinh_danh, fonds, catalog, dossier_code, dossier_id),
        ).fetchone()
        if conflict:
            raise ValueError(
                "ÄÃ£ tá»“n táº¡i há»“ sÆ¡ cÃ³ cÃ¹ng mÃ£ Ä‘á»‹nh danh/phÃ´ng/má»¥c lá»¥c/sá»‘ há»“ sÆ¡"
            )

    docs = conn.execute(
        "SELECT doc_id, file_name, file_path FROM documents "
        "WHERE dossier_id = ? AND indexed_status != 'deleted' "
        "ORDER BY file_name, created_at, doc_id",
        (dossier_id,),
    ).fetchall()
    target_dir = _dossier_pdf_dir(
        store,
        ma_dinh_danh=ma_dinh_danh,
        fonds=fonds,
        catalog=catalog,
        dossier_code=dossier_code,
    )

    move_ops = []
    if code_changed:
        for idx, doc in enumerate(docs, start=1):
            src = _abs_pdf_path(store, doc["file_path"] or "")
            if not src.exists() or not src.is_file():
                raise FileNotFoundError(f"KhÃ´ng tÃ¬m tháº¥y PDF: {src}")
            new_name = _canonical_doc_name(
                ma_dinh_danh, fonds, catalog, dossier_code, idx
            )
            dst = target_dir / new_name
            if dst.exists() and dst.resolve() != src.resolve():
                raise FileExistsError(f"File Ä‘Ã­ch Ä‘Ã£ tá»“n táº¡i: {dst}")
            rel = str(dst.relative_to(store.archive_path)).replace("\\", "/")
            move_ops.append((doc["doc_id"], src, dst, new_name, rel))

    now = int(time.time())
    moved: list[tuple[Path, Path]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        if code_changed:
            target_dir.mkdir(parents=True, exist_ok=True)
            for _doc_id, src, dst, _new_name, _rel in move_ops:
                if src.resolve() == dst.resolve():
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved.append((dst, src))

        conn.execute(
            "UPDATE dossiers SET ma_dinh_danh = ?, fonds = ?, fonds_name = ?,"
            " catalog = ?, catalog_name = ?, dossier_code = ?,"
            " title = ?, is_unstructured = ?, retention = ?, term = ?,"
            " storage_unit = ?, physical_state = ?, topic = ?, note = ?,"
            " updated_at = ? WHERE dossier_id = ?",
            (
                ma_dinh_danh, fonds, (fonds_name or "").strip()[:1000] or None,
                catalog, (catalog_name or "").strip()[:1000] or None,
                dossier_code, (title or "").strip()[:1000] or None,
                1 if is_unstructured else 0,
                (retention or "").strip() or None,
                (term or "").strip()[:10] or None,
                (storage_unit or "").strip() or None,
                (physical_state or "").strip() or None,
                (topic or "").strip()[:1000] or None,
                (note or "").strip()[:1000] or None,
                now, dossier_id,
            ),
        )
        for doc_id, _src, _dst, new_name, rel in move_ops:
            conn.execute(
                "UPDATE documents SET file_name = ?, file_path = ?,"
                " updated_at = ? WHERE doc_id = ?",
                (new_name, rel, now, doc_id),
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        for dst, src in reversed(moved):
            try:
                if dst.exists() and not src.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst), str(src))
            except Exception:
                pass
        raise

    if code_changed:
        for _doc_id, _src, _dst, _new_name, _rel in move_ops:
            try:
                _cleanup_empty_pdf_parents(store, _src.parent)
            except Exception:
                pass

    return RelabelStats(
        code_changed=code_changed,
        renamed_docs=len(move_ops) if code_changed else 0,
        old_key=old_key,
        new_key=new_key,
        target_dir=str(target_dir),
    )


# ---------------------------------------------------------------- add


def add_document(store: ArchiveStore, index: HybridIndex, *,
                  dossier_id: int, pdf_path: Path,
                  kie_fields: dict,
                  body_chunks: Optional[List[Chunk]] = None,
                  kie_annotation_json: str = "") -> str:
    """Append a new document to an existing dossier.

    Caller must have already produced `body_chunks` (via the chunker on
    OCR'd PDF text) and the cleaned `kie_fields` dict; this function
    persists rows + Tantivy text. Returns the new doc_id.

    Raises ValueError if a document with the same sha256 already exists
    in this dossier. The same PDF bytes may still be stored in another
    dossier as a separate document instance."""
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    sha = _file_sha256(pdf_path)
    conn = store.connect()
    existing_same_dossier = conn.execute(
        "SELECT doc_id FROM documents "
        "WHERE sha256 = ? AND dossier_id = ? AND indexed_status != 'deleted' "
        "LIMIT 1",
        (sha, dossier_id),
    ).fetchone()
    if existing_same_dossier is not None:
        raise ValueError(
            f"VÄƒn báº£n nÃ y Ä‘Ã£ cÃ³ trong há»“ sÆ¡ hiá»‡n táº¡i (sha256={sha[:16]}â€¦)"
        )
    doc_id = _allocate_document_instance_id(conn, sha, dossier_id)

    dr = conn.execute(
        "SELECT ma_dinh_danh, fonds, catalog, dossier_code "
        "FROM dossiers WHERE dossier_id = ?",
        (dossier_id,),
    ).fetchone()
    if dr is None:
        raise KeyError(f"dossier_id not found: {dossier_id}")

    target_subdir = _dossier_pdf_dir(
        store,
        ma_dinh_danh=dr["ma_dinh_danh"] or "",
        fonds=dr["fonds"] or "",
        catalog=dr["catalog"] or "",
        dossier_code=dr["dossier_code"] or "",
    )
    target_name = _strip_ocr_suffix(pdf_path.name)
    target_pdf = target_subdir / target_name
    _replace_pdf_file(pdf_path, target_pdf)

    # documents row
    rel = str(target_pdf.relative_to(store.archive_path)).replace("\\", "/")
    import fitz
    with fitz.open(str(target_pdf)) as f:
        page_count = f.page_count
    now = int(time.time())
    cols_kie = {col: (kie_fields.get(col) or "") for col in KIE_COLUMNS}
    params = {
        "doc_id": doc_id, "dossier_id": dossier_id,
        "file_name": target_name, "file_path": rel,
        **cols_kie,
        "kie_annotation_json": kie_annotation_json or "{}",
        "page_count": page_count, "sha256": sha,
        "indexed_status": "pending", "created_at": now,
    }
    cols_all = list(params.keys())
    placeholders = ", ".join(f":{c}" for c in cols_all)
    conn.execute(
        f"INSERT INTO documents ({', '.join(cols_all)}) VALUES ({placeholders})",
        params,
    )

    # Metadata chunk
    meta_chunk = synthesize_metadata_chunk(cols_kie)
    index.begin_writer()
    if meta_chunk is not None:
        cur = conn.execute(
            "INSERT INTO chunks ("
            "  doc_id, doc_version, chunk_type, page, block_idx,"
            "  text_original, text_no_diacritic, text_segmented,"
            "  bbox, word_count, source_blocks, merge_reason,"
            "  indexed_status, created_at"
            ") VALUES (?, 1, 'metadata', 1, -1, ?, ?, ?, ?, ?, '[]',"
            "  'kie_metadata', 'pending', ?)",
            (doc_id, meta_chunk.text, to_no_diacritic(meta_chunk.text),
             segment(meta_chunk.text), json.dumps([0, 0, 0, 0]),
             meta_chunk.word_count, now),
        )
        meta_chunk_id = int(cur.lastrowid)
        doc_number_proj = cols_kie.get("kie_doc_number_symbol", "")
        signer_proj     = cols_kie.get("kie_signer_name", "")
        issue_org_proj  = " ".join(filter(None, [
            cols_kie.get("kie_issue_org_name", ""),
            cols_kie.get("kie_issue_org_superior", ""),
        ])).strip()
        subject_proj    = cols_kie.get("kie_doc_subject", "")
        recipients_proj = cols_kie.get("kie_recipients", "")
        index.add_metadata_chunk(
            doc_id=doc_id, dossier_id=dossier_id,
            chunk_id=meta_chunk_id,
            doc_number=doc_number_proj, signer_name=signer_proj,
            issue_org=issue_org_proj, subject=subject_proj,
            recipients=recipients_proj,
            metadata_text=meta_chunk.text,
        )

    # Body chunks
    if body_chunks:
        for ch in body_chunks:
            tno = to_no_diacritic(ch.text)
            tseg = segment(ch.text)
            cur = conn.execute(
                "INSERT INTO chunks ("
                "  doc_id, doc_version, chunk_type, page, block_idx,"
                "  text_original, text_no_diacritic, text_segmented,"
                "  bbox, word_count, source_blocks, merge_reason,"
                "  indexed_status, created_at"
                ") VALUES (?, 1, 'body', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (doc_id, ch.page, ch.block_idx, ch.text, tno, tseg,
                 json.dumps(list(ch.bbox)), ch.word_count,
                 json.dumps(ch.source_blocks),
                 ch.merge_reason, now),
            )
            chunk_id = int(cur.lastrowid)
            index.add_body_text_chunk(
                doc_id=doc_id, dossier_id=dossier_id,
                chunk_id=chunk_id,
                body_original=ch.text,
                body_no_diacritic=tno,
                body_segmented=tseg or "",
            )

    conn.execute(
        "UPDATE documents SET indexed_status = 'indexed', "
        " indexed_at = ?, updated_at = ? WHERE doc_id = ?",
        (now, now, doc_id),
    )
    conn.execute(
        "UPDATE chunks SET indexed_status = 'indexed' WHERE doc_id = ?",
        (doc_id,),
    )
    index.commit()
    store.refresh_counters()
    return doc_id
