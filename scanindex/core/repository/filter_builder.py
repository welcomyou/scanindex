"""Build SQL WHERE clause from the advance-filter UI dict.

UI passes a dict of structured filters (number/text fields, date range,
multi-select). We translate to a parametrized WHERE clause that joins
documents (alias `d`) ↔ dossiers (alias `ds`).

Empty fields are dropped. The clause always restricts to indexed docs.
"""
from __future__ import annotations
from typing import Any, List, Optional, Tuple

from .tokenizer import build_filter_text

# Fields folded into documents.doc_filter_text (schema v10): every field
# the advanced filters match against, doc-level + dossier-level, so the
# SQL prefilter can thin candidates with plain instr() on normalized text.
DOC_FILTER_DOC_FIELDS: Tuple[str, ...] = (
    "kie_doc_number_symbol", "kie_issue_org_superior", "kie_issue_org_name",
    "kie_signer_name", "kie_doc_subject", "kie_doc_type", "kie_secrecy_mark",
)
DOC_FILTER_DOSSIER_FIELDS: Tuple[str, ...] = (
    "fonds", "fonds_name", "catalog", "catalog_name",
    "term", "retention", "confidentiality",
)


def _row_get(row, field: str) -> str:
    """Field access that works for both sqlite3.Row and mapping."""
    try:
        keys = row.keys()
    except AttributeError:
        return str(row.get(field) or "")
    return str(row[field] or "") if field in keys else ""


def doc_filter_text_from_row(doc_row, dossier_row) -> str:
    """Compute doc_filter_text from one documents row + its dossiers row.

    `dossier_row` may be None (LEFT JOIN miss) — dossier fields fold to "".
    """
    values = [_row_get(doc_row, f) for f in DOC_FILTER_DOC_FIELDS]
    if dossier_row is not None:
        values += [_row_get(dossier_row, f) for f in DOC_FILTER_DOSSIER_FIELDS]
    return build_filter_text(*values)


_BACKFILL_SQL = (
    "SELECT d.doc_id, "
    + ", ".join(f"d.{f}" for f in DOC_FILTER_DOC_FIELDS)
    + ", " + ", ".join(f"ds.{f}" for f in DOC_FILTER_DOSSIER_FIELDS)
    + " FROM documents d "
    "LEFT JOIN dossiers ds ON d.dossier_id = ds.dossier_id "
    "WHERE d.doc_filter_text IS NULL"
)


def backfill_missing_filter_text(conn) -> int:
    """Fill doc_filter_text for rows lacking it (pre-v10 data, or rows an
    older app release inserted without maintaining the column). Idempotent.
    Returns the number of rows updated."""
    rows = conn.execute(_BACKFILL_SQL).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE documents SET doc_filter_text = ? WHERE doc_id = ?",
            (doc_filter_text_from_row(r, r), r["doc_id"]),
        )
    return len(rows)


def refresh_doc_filter_text(conn, doc_ids) -> int:
    """Recompute doc_filter_text for specific documents (after KIE edits or
    dossier-level changes). Returns the number of rows updated."""
    ids = [d for d in (doc_ids or []) if d]
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT d.doc_id, "
        + ", ".join(f"d.{f}" for f in DOC_FILTER_DOC_FIELDS)
        + ", " + ", ".join(f"ds.{f}" for f in DOC_FILTER_DOSSIER_FIELDS)
        + " FROM documents d "
        "LEFT JOIN dossiers ds ON d.dossier_id = ds.dossier_id "
        f"WHERE d.doc_id IN ({ph})",
        ids,
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE documents SET doc_filter_text = ? WHERE doc_id = ?",
            (doc_filter_text_from_row(r, r), r["doc_id"]),
        )
    return len(rows)


def build_where(filters: dict) -> Tuple[str, List[Any]]:
    parts: List[str] = []
    params: List[Any] = []

    def add_like(field: str, val, table: str = "d") -> None:
        if val:
            parts.append(f"{table}.{field} LIKE ?")
            params.append(f"%{val}%")

    def add_in(field: str, vals, table: str = "d") -> None:
        if vals:
            if isinstance(vals, str):
                parts.append(f"{table}.{field} LIKE ?")
                params.append(f"%{vals}%")
                return
            ph = ",".join("?" * len(vals))
            parts.append(f"{table}.{field} IN ({ph})")
            params.extend(vals)

    f = filters or {}
    # Schema v2: documents columns are raw KIE fields. Filter terms map to
    # the corresponding kie_* columns; keywords / language / access_mode no
    # longer have dedicated columns (Tantivy full-text covers freeform).
    add_like("kie_doc_number_symbol", f.get("doc_number"))
    add_like("kie_issue_org_name",    f.get("issue_org"))
    add_like("kie_signer_name",       f.get("signer_name"))
    add_like("kie_doc_subject",       f.get("subject"))

    add_in("kie_doc_type",     f.get("doc_type"))
    add_in("kie_secrecy_mark", f.get("confidentiality"))

    if f.get("issue_date_from"):
        parts.append("d.kie_place_date >= ?")
        params.append(f["issue_date_from"])
    if f.get("issue_date_to"):
        parts.append("d.kie_place_date <= ?")
        params.append(f["issue_date_to"])

    add_like("fonds",       f.get("fonds"),    table="ds")
    add_like("catalog",     f.get("catalog"),  table="ds")
    add_like("term",        f.get("term"),     table="ds")
    add_in("retention",     f.get("retention"), table="ds")

    parts.append("d.indexed_status = 'indexed'")
    return " AND ".join(parts), params


def is_active(filters: dict) -> bool:
    """True when the UI has any user-set filter beyond the implicit indexed=true."""
    if not filters:
        return False
    for k, v in filters.items():
        if v in (None, "", [], ()):
            continue
        return True
    return False
