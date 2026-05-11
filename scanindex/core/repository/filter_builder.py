"""Build SQL WHERE clause from the advance-filter UI dict.

UI passes a dict of structured filters (number/text fields, date range,
multi-select). We translate to a parametrized WHERE clause that joins
documents (alias `d`) ↔ dossiers (alias `ds`).

Empty fields are dropped. The clause always restricts to indexed docs.
"""
from __future__ import annotations
from typing import Any, List, Tuple


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
