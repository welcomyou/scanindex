"""Lexical search engine for the repository.

SQLite stores document metadata. Tantivy stores the derived full-text index
for metadata chunks and body chunks. Dense vectors are intentionally absent
from this path.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from . import constants as C
from .filter_builder import is_active
from .indexer import HybridIndex, body_text_fields, metadata_text_fields
from .store import ArchiveStore
from .tokenizer import to_no_diacritic

_log = logging.getLogger(__name__)

_MIN_CHUNK_TEXT_LEN = 12
_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass
class SearchResult:
    chunk_id: int
    doc_id: str
    score: float
    page: int
    text: str
    bbox: list
    doc_number: Optional[str] = None
    subject: Optional[str] = None
    issue_org: Optional[str] = None
    issue_org_superior: Optional[str] = None
    signer_name: Optional[str] = None
    issue_date: Optional[str] = None
    file_name: Optional[str] = None
    file_path: Optional[str] = None
    dossier_title: Optional[str] = None
    chunk_type: str = "body"   # metadata | body
    match_kind: str = ""       # exact | fuzzy | filter
    match_count: int = 0
    match_bboxes: Optional[List[List[float]]] = None
    query: str = ""


def _tokens(text: str) -> List[str]:
    norm = to_no_diacritic(text or "").lower()
    return _WORD_RE.findall(norm)


def _exact_frequency(text: str, query: str) -> int:
    q = " ".join(_tokens(query))
    if not q:
        return 0
    t = " ".join(_tokens(text))
    if not t:
        return 0
    return len(re.findall(rf"(?<!\w){re.escape(q)}(?!\w)", t))


def _bbox_intersects(a: list[float], b: list[float]) -> bool:
    if len(a) != 4 or len(b) != 4:
        return False
    ax0, ay0, ax1, ay1 = (float(v) for v in a)
    bx0, by0, bx1, by1 = (float(v) for v in b)
    return min(ax1, bx1) > max(ax0, bx0) and min(ay1, by1) > max(ay0, by0)


def _bbox_union(boxes: List[List[float]]) -> List[float]:
    if not boxes:
        return []
    xs0, ys0, xs1, ys1 = zip(*boxes)
    return [min(xs0), min(ys0), max(xs1), max(ys1)]


def _is_near_same_match(a: List[float], b: List[float]) -> bool:
    if len(a) != 4 or len(b) != 4:
        return False
    ax0, ay0, ax1, ay1 = (float(v) for v in a)
    bx0, by0, bx1, by1 = (float(v) for v in b)
    aw, ah = max(1.0, ax1 - ax0), max(1.0, ay1 - ay0)
    bw, bh = max(1.0, bx1 - bx0), max(1.0, by1 - by0)
    acx, acy = (ax0 + ax1) / 2.0, (ay0 + ay1) / 2.0
    bcx, bcy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
    return (
        abs(acx - bcx) <= max(aw, bw) * 0.35
        and abs(acy - bcy) <= max(ah, bh) * 0.60
    )


def _dedupe_match_bboxes(boxes: List[List[float]]) -> List[List[float]]:
    out: List[List[float]] = []
    for bb in boxes or []:
        if len(bb) != 4:
            continue
        clean = [float(v) for v in bb]
        if any(_is_near_same_match(clean, kept) for kept in out):
            continue
        out.append(clean)
    return out


def _auto_fuzzy_max_edits(token: str) -> int:
    """Elasticsearch/Lucene-style AUTO fuzziness: 0 edits for <=2 chars,
    1 edit for 3..5 chars, 2 edits for >5 chars."""
    n = len(token or "")
    if n <= 2:
        return 0
    if n <= 5:
        return 1
    return 2


def _edit_distance(a: str, b: str) -> int | None:
    try:
        from rapidfuzz.distance import DamerauLevenshtein
        return int(DamerauLevenshtein.distance(a, b))
    except Exception:
        try:
            from rapidfuzz.distance import Levenshtein
            return int(Levenshtein.distance(a, b))
        except Exception:
            return None


def _fuzzy_token_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if any(ch.isdigit() for ch in a + b):
        if not (a.isdigit() and b.isdigit()):
            return a == b
        if a[:1] != b[:1]:
            return False
    max_dist = _auto_fuzzy_max_edits(a)
    if max_dist <= 0:
        return a == b
    if abs(len(a) - len(b)) > max_dist:
        return False
    dist = _edit_distance(a, b)
    if dist is None:
        return False
    return dist <= max_dist


def _fuzzy_frequency(text: str, query: str) -> int:
    qtokens = [
        t for t in _tokens(query)
        if len(t) >= 2 or any(ch.isdigit() for ch in t)
    ]
    ttokens = [t for t in _tokens(text) if len(t) >= 2]
    if not qtokens or not ttokens:
        return 0
    if len(qtokens) >= 8:
        return 0
    allow_short_fuzzy = len(qtokens) > 1
    if len(qtokens) == 1:
        qt = qtokens[0]
        if len(qt) < 3 and not allow_short_fuzzy:
            return sum(1 for tt in ttokens if tt == qt)
        return sum(1 for tt in ttokens if _fuzzy_token_match(qt, tt))

    n = len(qtokens)
    hits = 0
    for i in range(0, len(ttokens) - n + 1):
        window = ttokens[i:i + n]
        if all(_fuzzy_token_match(qt, tt) for qt, tt in zip(qtokens, window)):
            hits += 1
    return hits * n


def _filter_tokens(text: str) -> List[str]:
    return [
        t for t in _tokens(text)
        if len(t) >= 2 or any(ch.isdigit() for ch in t)
    ]


def _same_number_token(a: str, b: str) -> bool:
    if not a.isdigit() or not b.isdigit():
        return a == b
    aa = a.lstrip("0") or "0"
    bb = b.lstrip("0") or "0"
    return aa == bb


def _doc_number_match(needle: Any, haystack: str) -> bool:
    if needle in (None, "", [], ()):
        return True
    if isinstance(needle, (list, tuple, set)):
        return any(_doc_number_match(x, haystack) for x in needle)

    qtokens = _filter_tokens(str(needle).strip())
    htokens = _filter_tokens(haystack or "")
    if not qtokens or not htokens:
        return False
    for qt in qtokens:
        if any(ch.isdigit() for ch in qt):
            if not any(_same_number_token(qt, ht) for ht in htokens):
                return False
            continue
        if qt not in htokens:
            return False
    return True


def _advanced_text_match(needle: Any, haystack: str, *, fuzzy: bool = True) -> bool:
    if needle in (None, "", [], ()):
        return True
    if isinstance(needle, (list, tuple, set)):
        return any(_advanced_text_match(x, haystack, fuzzy=fuzzy) for x in needle)

    q = " ".join(_filter_tokens(str(needle).strip()))
    h = " ".join(_filter_tokens(haystack or ""))
    if not q or not h:
        return False
    if q in h:
        return True
    if not fuzzy:
        return False

    qtokens = q.split()
    htokens = h.split()
    if len(qtokens) == 1:
        return any(_fuzzy_token_match(qtokens[0], t) for t in htokens)
    if len(qtokens) > 8 or len(htokens) < len(qtokens):
        return False
    n = len(qtokens)
    for i in range(0, len(htokens) - n + 1):
        if all(_fuzzy_token_match(qt, ht) for qt, ht in zip(qtokens, htokens[i:i + n])):
            return True
    pos = 0
    for ht in htokens:
        if _fuzzy_token_match(qtokens[pos], ht):
            pos += 1
            if pos >= len(qtokens):
                return True
    return False


_ADVANCED_FILTER_FIELDS: dict[str, Tuple[str, ...]] = {
    "doc_number": ("kie_doc_number_symbol",),
    "issue_org": ("kie_issue_org_superior", "kie_issue_org_name"),
    "signer_name": ("kie_signer_name",),
    "subject": ("kie_doc_subject",),
    "doc_type": ("kie_doc_type",),
    "confidentiality": ("kie_secrecy_mark", "confidentiality"),
    "fonds": ("fonds", "fonds_name"),
    "catalog": ("catalog", "catalog_name"),
    "term": ("term",),
    "retention": ("retention",),
}

_FUZZY_METADATA_FILTER_KEYS = {"issue_org", "signer_name", "subject"}


def _date_key(value: Any) -> Optional[str]:
    text = to_no_diacritic(str(value or "").strip()).lower()
    if not text:
        return None

    def build(day: str, month: str, year: str) -> Optional[str]:
        try:
            d = int(day)
            m = int(month)
            y = int(year)
            if y < 100:
                y = 2000 + y if y < 50 else 1900 + y
            if not (1 <= d <= 31 and 1 <= m <= 12 and 1900 <= y <= 2200):
                return None
            return f"{y:04d}{m:02d}{d:02d}"
        except Exception:
            return None

    for pat in (
        r"\b(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2,4})\b",
        r"\bngay\s+(\d{1,2})\s+thang\s+(\d{1,2})\s+nam\s+(\d{2,4})\b",
        r"\b(\d{1,2})\s+thang\s+(\d{1,2})\s+nam\s+(\d{2,4})\b",
    ):
        m = re.search(pat, text)
        if m:
            key = build(m.group(1), m.group(2), m.group(3))
            if key:
                return key
    m = re.search(r"\b(\d{4})[\/.\-](\d{1,2})[\/.\-](\d{1,2})\b", text)
    if m:
        return build(m.group(3), m.group(2), m.group(1))
    return None


def _dedupe_chunk_scores(
    chunk_id_score_pairs: List[Tuple[int, float]]
) -> List[Tuple[int, float]]:
    best: dict[int, float] = {}
    order: List[int] = []
    for cid, score in chunk_id_score_pairs or []:
        cid = int(cid)
        score = float(score or 0.0)
        if cid not in best:
            order.append(cid)
            best[cid] = score
        elif score > best[cid]:
            best[cid] = score
    return [(cid, best[cid]) for cid in order]


class SearchEngine:
    def __init__(self, store: ArchiveStore, index: HybridIndex):
        self.store = store
        self.index = index

    def search(self,
               query: str = "",
               filters: Optional[dict] = None,
               mode: str = "content",
               final_k: Optional[int] = None) -> List[SearchResult]:
        filters = filters or {}
        candidate_doc_ids = self._scope_doc_ids(filters)

        if not query or not query.strip():
            return self._sql_only(candidate_doc_ids, limit=final_k or 50)

        mode = (mode or "content").strip().lower()
        if mode in {"metadata", "meta", "info", "document_info", "title", "subject"}:
            mode = "metadata"
        else:
            mode = "content"

        t0 = time.time()
        fields = metadata_text_fields() if mode == "metadata" else body_text_fields()
        chunk_type_filter = "metadata" if mode == "metadata" else "body"
        cset = set(candidate_doc_ids) if candidate_doc_ids is not None else None

        lex_hits = self.index.search_lexical(
            query,
            C.TANTIVY_TOP_K,
            cset,
            fields=fields,
        )
        fuzzy_hits = self.index.search_fuzzy(
            query,
            C.TANTIVY_TOP_K,
            cset,
            fields=fields,
        )

        exact_results = self._build_results(
            [(cid, score) for cid, _did, score in lex_hits],
            match_kind="exact",
            query=query,
            chunk_type_filter=chunk_type_filter,
            build_match_bboxes=False,
        )
        exact_doc_ids = {r.doc_id for r in exact_results}
        fuzzy_hits = [
            (cid, did, score)
            for cid, did, score in fuzzy_hits
            if did not in exact_doc_ids
        ]
        fuzzy_results = self._build_results(
            [(cid, score) for cid, _did, score in fuzzy_hits],
            match_kind="fuzzy",
            query=query,
            chunk_type_filter=chunk_type_filter,
            build_match_bboxes=False,
        )
        results = exact_results + fuzzy_results
        _log.info(
            "[search] '%s' mode=%s exact=%d fuzzy=%d elapsed=%.0fms results=%d",
            query,
            mode,
            len(lex_hits),
            len(fuzzy_hits),
            (time.time() - t0) * 1000,
            len(results),
        )
        return results

    def _scope_doc_ids(self, filters: dict) -> Optional[List[str]]:
        if not is_active(filters):
            return None
        rows = self.store.connect().execute(
            "SELECT d.doc_id,"
            "       d.kie_doc_number_symbol, d.kie_issue_org_superior,"
            "       d.kie_issue_org_name, d.kie_signer_name,"
            "       d.kie_doc_subject, d.kie_doc_type, d.kie_secrecy_mark,"
            "       d.kie_place_date,"
            "       ds.fonds, ds.fonds_name, ds.catalog, ds.catalog_name,"
            "       ds.term, ds.retention, ds.confidentiality "
            "FROM documents d "
            "LEFT JOIN dossiers ds ON d.dossier_id = ds.dossier_id "
            "WHERE d.indexed_status = 'indexed'"
        ).fetchall()

        def row_matches(row) -> bool:
            for key, fields in _ADVANCED_FILTER_FIELDS.items():
                val = (filters or {}).get(key)
                if val in (None, "", [], ()):
                    continue
                haystack = " ".join(str(row[f] or "") for f in fields)
                if key == "doc_number":
                    matched = _doc_number_match(val, haystack)
                else:
                    matched = _advanced_text_match(
                        val,
                        haystack,
                        fuzzy=key in _FUZZY_METADATA_FILTER_KEYS,
                    )
                if not matched:
                    return False

            issue_date = str(row["kie_place_date"] or "")
            issue_key = _date_key(issue_date)
            date_from = _date_key((filters or {}).get("issue_date_from"))
            date_to = _date_key((filters or {}).get("issue_date_to"))
            if date_from and (not issue_key or issue_key < date_from):
                return False
            if date_to and (not issue_key or issue_key > date_to):
                return False
            return True

        return [r["doc_id"] for r in rows if row_matches(r)]

    def _fetch_chunks(self, chunk_ids: List[int]) -> dict:
        if not chunk_ids:
            return {}
        ph = ",".join("?" * len(chunk_ids))
        rows = self.store.connect().execute(
            f"SELECT chunk_id, text_original, page, bbox, doc_id "
            f"FROM chunks WHERE chunk_id IN ({ph})",
            chunk_ids,
        ).fetchall()
        return {int(r["chunk_id"]): dict(r) for r in rows}

    def _page_word_items(self, pdf_rel_path: str, page: int, cache: dict) -> List[dict]:
        if not pdf_rel_path or page <= 0:
            return []
        key = (pdf_rel_path, int(page))
        if key in cache:
            return cache[key]
        pdf_path = (Path(self.store.archive_path) / pdf_rel_path).resolve()
        items: List[dict] = []
        try:
            import fitz
            with fitz.open(str(pdf_path)) as doc:
                page_idx = int(page) - 1
                if page_idx < 0 or page_idx >= doc.page_count:
                    cache[key] = []
                    return []
                words = list(doc[page_idx].get_text("words") or [])
        except Exception:
            cache[key] = []
            return []
        words.sort(key=lambda w: (
            int(w[5]) if len(w) > 5 else 0,
            int(w[6]) if len(w) > 6 else 0,
            int(w[7]) if len(w) > 7 else 0,
            float(w[1]) if len(w) > 1 else 0.0,
            float(w[0]) if len(w) > 0 else 0.0,
        ))
        for w in words:
            if len(w) < 5:
                continue
            bbox = [float(w[0]), float(w[1]), float(w[2]), float(w[3])]
            for tok in _tokens(str(w[4] or "")):
                items.append({"token": tok, "bbox": bbox})
        cache[key] = items
        return items

    def _exact_match_bboxes(self,
                            *,
                            query: str,
                            pdf_rel_path: str,
                            page: int,
                            chunk_bbox: List[float],
                            word_cache: dict) -> List[List[float]]:
        qtokens = _tokens(query)
        if not qtokens or not chunk_bbox or len(chunk_bbox) != 4:
            return []
        words = [
            item for item in self._page_word_items(pdf_rel_path, page, word_cache)
            if _bbox_intersects(item.get("bbox") or [], chunk_bbox)
        ]
        if len(words) < len(qtokens):
            return []
        n = len(qtokens)
        boxes: List[List[float]] = []
        for i in range(0, len(words) - n + 1):
            if [w["token"] for w in words[i:i + n]] != qtokens:
                continue
            boxes.append(_bbox_union([w["bbox"] for w in words[i:i + n]]))
        return _dedupe_match_bboxes(boxes)

    def _fuzzy_match_bboxes(self,
                            *,
                            query: str,
                            pdf_rel_path: str,
                            page: int,
                            chunk_bbox: List[float],
                            word_cache: dict) -> List[List[float]]:
        qtokens = [
            t for t in _tokens(query)
            if len(t) >= 3 or (len(t) >= 2 and any(ch.isdigit() for ch in t))
        ]
        if not qtokens or len(qtokens) >= 8 or not chunk_bbox or len(chunk_bbox) != 4:
            return []
        words = [
            item for item in self._page_word_items(pdf_rel_path, page, word_cache)
            if _bbox_intersects(item.get("bbox") or [], chunk_bbox)
        ]
        if len(words) < len(qtokens):
            return []
        n = len(qtokens)
        boxes: List[List[float]] = []
        for i in range(0, len(words) - n + 1):
            window = words[i:i + n]
            if not all(_fuzzy_token_match(qt, w["token"]) for qt, w in zip(qtokens, window)):
                continue
            boxes.append(_bbox_union([w["bbox"] for w in window]))
        return _dedupe_match_bboxes(boxes)

    def hydrate_match_bboxes(self,
                             results: List[SearchResult],
                             *,
                             limit: int = 24) -> None:
        if not results or limit <= 0:
            return
        word_cache: dict = {}
        hydrated = 0
        for result in results:
            if hydrated >= limit:
                return
            if result.match_bboxes:
                continue
            if (result.chunk_type or "body") == "metadata":
                continue
            if result.match_kind not in {"exact", "fuzzy"}:
                continue
            if not result.query or not result.file_path or not result.bbox:
                continue
            if result.match_kind == "exact":
                boxes = self._exact_match_bboxes(
                    query=result.query,
                    pdf_rel_path=result.file_path,
                    page=int(result.page or 0),
                    chunk_bbox=result.bbox,
                    word_cache=word_cache,
                )
            else:
                boxes = self._fuzzy_match_bboxes(
                    query=result.query,
                    pdf_rel_path=result.file_path,
                    page=int(result.page or 0),
                    chunk_bbox=result.bbox,
                    word_cache=word_cache,
                )
            hydrated += 1
            if not boxes:
                continue
            result.match_bboxes = boxes
            result.match_count = len(boxes)
            result.score = float(len(boxes))

    def _build_results(self,
                       chunk_id_score_pairs: List[Tuple[int, float]],
                       *,
                       match_kind: str = "",
                       query: str = "",
                       chunk_type_filter: Optional[str] = None,
                       build_match_bboxes: bool = True,
                       ) -> List[SearchResult]:
        if not chunk_id_score_pairs:
            return []
        deduped_pairs = _dedupe_chunk_scores(chunk_id_score_pairs)
        if not deduped_pairs:
            return []
        cids = [c for c, _ in deduped_pairs]
        score_map = dict(deduped_pairs)
        ph = ",".join("?" * len(cids))
        rows = self.store.connect().execute(
            f"SELECT c.chunk_id, c.doc_id, c.page, c.text_original, c.bbox,"
            f"       c.chunk_type,"
            f"       d.kie_doc_number_symbol AS doc_number,"
            f"       d.kie_doc_subject       AS subject,"
            f"       d.kie_issue_org_name    AS issue_org,"
            f"       d.kie_issue_org_superior AS issue_org_superior,"
            f"       d.kie_signer_name       AS signer_name,"
            f"       d.kie_place_date        AS issue_date,"
            f"       d.file_name, d.file_path,"
            f"       ds.title AS dossier_title "
            f"FROM chunks c "
            f"JOIN documents d ON c.doc_id = d.doc_id "
            f"LEFT JOIN dossiers ds ON d.dossier_id = ds.dossier_id "
            f"WHERE c.chunk_id IN ({ph})",
            cids,
        ).fetchall()
        by_id = {int(r["chunk_id"]): r for r in rows}

        out: List[SearchResult] = []
        word_cache: dict = {}
        for cid in cids:
            r = by_id.get(cid)
            if r is None:
                continue
            if chunk_type_filter and (r["chunk_type"] or "body") != chunk_type_filter:
                continue
            text = (r["text_original"] or "").strip()
            if len(text) < _MIN_CHUNK_TEXT_LEN:
                continue
            try:
                bbox = json.loads(r["bbox"]) if r["bbox"] else []
            except Exception:
                bbox = []
            match_count = 0
            score = float(score_map.get(cid, 0.0))
            match_bboxes = None
            if query and match_kind == "exact":
                match_count = _exact_frequency(text, query)
                if match_count <= 0:
                    continue
                if build_match_bboxes:
                    match_bboxes = self._exact_match_bboxes(
                        query=query,
                        pdf_rel_path=r["file_path"] or "",
                        page=int(r["page"]) if r["page"] is not None else 0,
                        chunk_bbox=bbox,
                        word_cache=word_cache,
                    )
                    if match_bboxes:
                        match_count = len(match_bboxes)
                score = float(match_count)
            elif query and match_kind == "fuzzy":
                match_count = _fuzzy_frequency(text, query)
                if match_count <= 0:
                    continue
                if build_match_bboxes:
                    match_bboxes = self._fuzzy_match_bboxes(
                        query=query,
                        pdf_rel_path=r["file_path"] or "",
                        page=int(r["page"]) if r["page"] is not None else 0,
                        chunk_bbox=bbox,
                        word_cache=word_cache,
                    )
                    if match_bboxes:
                        match_count = len(match_bboxes)
                score = float(match_count)
            out.append(SearchResult(
                chunk_id=cid,
                doc_id=r["doc_id"],
                score=score,
                page=int(r["page"]) if r["page"] is not None else 0,
                text=text,
                bbox=bbox,
                doc_number=r["doc_number"],
                subject=r["subject"],
                issue_org=r["issue_org"],
                issue_org_superior=r["issue_org_superior"],
                signer_name=r["signer_name"],
                issue_date=r["issue_date"],
                file_name=r["file_name"],
                file_path=r["file_path"],
                dossier_title=r["dossier_title"],
                chunk_type=r["chunk_type"] or "body",
                match_kind=match_kind,
                match_count=match_count,
                match_bboxes=match_bboxes,
                query=query or "",
            ))
        return out

    def _sql_only(self, candidate_doc_ids: Optional[List[str]], limit: int) -> List[SearchResult]:
        conn = self.store.connect()
        base = (
            "SELECT d.doc_id, d.file_name, d.file_path,"
            "       d.kie_doc_subject       AS subject,"
            "       d.kie_doc_number_symbol AS doc_number,"
            "       d.kie_issue_org_name    AS issue_org,"
            "       d.kie_issue_org_superior AS issue_org_superior,"
            "       d.kie_signer_name       AS signer_name,"
            "       d.kie_place_date        AS issue_date,"
            "       ds.title AS dossier_title "
            "FROM documents d "
            "LEFT JOIN dossiers ds ON d.dossier_id = ds.dossier_id "
        )
        if candidate_doc_ids is None:
            rows = conn.execute(
                base + "WHERE d.indexed_status = 'indexed' "
                       "ORDER BY COALESCE(d.indexed_at, 0) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            if not candidate_doc_ids:
                return []
            ph = ",".join("?" * len(candidate_doc_ids))
            rows = conn.execute(
                base + f"WHERE d.doc_id IN ({ph}) "
                       f"ORDER BY COALESCE(d.indexed_at, 0) DESC LIMIT ?",
                [*candidate_doc_ids, limit],
            ).fetchall()
        return [SearchResult(
            chunk_id=0,
            doc_id=r["doc_id"],
            score=0.0,
            page=0,
            text="",
            bbox=[],
            doc_number=r["doc_number"],
            subject=r["subject"],
            issue_org=r["issue_org"],
            issue_org_superior=r["issue_org_superior"],
            signer_name=r["signer_name"],
            issue_date=r["issue_date"],
            file_name=r["file_name"],
            file_path=r["file_path"],
            dossier_title=r["dossier_title"],
            chunk_type="metadata",
            match_kind="filter",
        ) for r in rows]
