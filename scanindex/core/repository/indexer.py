"""Tantivy index manager for Kho lexical/fuzzy search.

Both stores are *derived* from SQLite (the source of truth). Repair logic
(repair.py) reconciles Tantivy against SQLite on startup.

Design notes:
- Tantivy schema indexes 9 text fields with per-field weights applied at
  query time via boost syntax: `field:(query)^weight`.
- Writer is held only during a batch; released after commit so concurrent
  searches (which use a fresh searcher) are not blocked by the lock.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

import tantivy

from . import constants as C
from .tokenizer import segment_query, to_no_diacritic


# Fields that are searchable text (default tokenizer) — order matters for query build.
_TEXT_FIELDS: Tuple[str, ...] = (
    "doc_number", "signer_name", "issue_org",
    "subject", "recipients", "metadata_text",
    "body_original", "body_no_diacritic", "body_segmented",
)
_METADATA_TEXT_FIELDS: Tuple[str, ...] = (
    "doc_number", "signer_name", "issue_org",
    "subject", "recipients", "metadata_text",
)
_BODY_TEXT_FIELDS: Tuple[str, ...] = (
    "body_original", "body_no_diacritic", "body_segmented",
)

# ---- Indexer v2 derived fields -------------------------------------------
# Every base field gets recall twins, fed with `to_no_diacritic(text).lower()`:
#   <f>_nodiac — default tokenizer; recovers "nguyen van a" ↔ "Nguyễn Văn A"
#                typed without diacritics (metadata previously had no twin).
#   <f>_tri    — 3-gram tokenizer; substring + typo recall served by the
#                index instead of the linear SQLite fallbacks.
# body_segmented gets no twins: its underscore-joined tokens would pollute
# the grams, and body_no_diacritic already covers the stripped body text.
_METADATA_NODIAC_FIELDS: Tuple[str, ...] = tuple(
    f + "_nodiac" for f in _METADATA_TEXT_FIELDS
)
_TRI_FIELDS: Tuple[str, ...] = tuple(
    f + "_tri" for f in _METADATA_TEXT_FIELDS
) + ("body_tri",)
_TRI_SOURCE_FIELD = {
    # trigram field -> base field whose (normalized) text feeds it
    **{f + "_tri": f for f in _METADATA_TEXT_FIELDS},
    "body_tri": "body_original",
}


def _normalized_index_text(text: str) -> str:
    """Canonical text for the _nodiac/_tri twins: diacritic-stripped + folded."""
    return to_no_diacritic(text or "").lower()


def metadata_text_fields() -> Tuple[str, ...]:
    return _METADATA_TEXT_FIELDS


def body_text_fields() -> Tuple[str, ...]:
    return _BODY_TEXT_FIELDS


def _normalize_fields(fields: Optional[Iterable[str]]) -> Tuple[str, ...]:
    if fields is None:
        return _TEXT_FIELDS
    allowed = set(_TEXT_FIELDS)
    out = tuple(f for f in fields if f in allowed)
    return out or _TEXT_FIELDS


def _trigram_analyzer() -> "tantivy.TextAnalyzer":
    return tantivy.TextAnalyzerBuilder(
        tantivy.Tokenizer.ngram(
            C.TRIGRAM_MIN_GRAM, C.TRIGRAM_MAX_GRAM, prefix_only=False
        )
    ).build()


def _build_schema() -> tantivy.Schema:
    sb = tantivy.SchemaBuilder()
    # Stored identifier fields — used for filtering and result lookup.
    sb.add_text_field("doc_id",     stored=True, tokenizer_name="raw")
    sb.add_text_field("chunk_id",   stored=True, tokenizer_name="raw")
    sb.add_text_field("dossier_id", stored=True, tokenizer_name="raw")
    # Searchable text fields. tokenizer="default" lowercases and splits
    # on Unicode word boundaries — good enough for VN once we pre-segment
    # multi-syllable terms with underscore in body_segmented.
    for fname in _TEXT_FIELDS:
        sb.add_text_field(fname, stored=False, tokenizer_name="default")
    # Indexer v2 recall twins.
    for fname in _METADATA_NODIAC_FIELDS:
        sb.add_text_field(fname, stored=False, tokenizer_name="default")
    for fname in _TRI_FIELDS:
        sb.add_text_field(
            fname, stored=False, tokenizer_name=C.TRIGRAM_TOKENIZER_NAME
        )
    return sb.build()


# Characters that break Tantivy's query parser when used as literal terms.
_QUERY_ESCAPE = ('"', ":", "(", ")", "[", "]", "{", "}", "^", "~", "!", "?", "+", "-", "*", "\\")


def _escape_query(q: str) -> str:
    s = q
    for ch in _QUERY_ESCAPE:
        s = s.replace(ch, " ")
    return " ".join(s.split())  # collapse whitespace


def _segment_query_if_enabled(query: str) -> str:
    if os.environ.get("ARCHIVE_SEGMENT_QUERY") != "1":
        return query
    return segment_query(query) or query


def _build_boosted_query(query: str,
                         fields: Optional[Iterable[str]] = None) -> str:
    """Build Tantivy query string with per-field boost.

    Returns syntax like:
      doc_number:(query)^5.0 OR signer_name:(query)^3.0 OR ...

    Indexer v2 also queries the _nodiac/_tri twins with a diacritic-stripped
    query, so "nguyen van a" typed without accents still hits metadata, and
    substring matches surface without the linear SQL fallback.
    """
    safe = _escape_query(query)
    if not safe:
        return ""
    segmented = _segment_query_if_enabled(query)
    safe_segmented = _escape_query(segmented)
    # One canonical normalized form feeds every _nodiac/_tri twin.
    safe_norm = _escape_query(_normalized_index_text(query))
    parts = []
    for fname in _normalize_fields(fields):
        weight = C.TANTIVY_FIELD_WEIGHTS.get(fname, 1.0)
        field_query = safe_segmented if fname == "body_segmented" and safe_segmented else safe
        parts.append(f"{fname}:({field_query})^{weight}")
        if fname == "body_segmented" and safe_segmented and safe_segmented != safe:
            parts.append(f"{fname}:({safe})^{weight * 0.4}")
        if fname in _METADATA_TEXT_FIELDS:
            if safe_norm:
                parts.append(
                    f"{fname}_nodiac:({safe_norm})^{weight * C.NODIAC_WEIGHT_SCALE}"
                )
                parts.append(
                    f"{fname}_tri:({safe_norm})^{weight * C.TRIGRAM_WEIGHT_SCALE}"
                )
        elif fname == "body_original":
            # body_no_diacritic already IS the body's normalized twin.
            if safe_norm:
                parts.append(f"body_tri:({safe_norm})^{weight * C.TRIGRAM_WEIGHT_SCALE}")
    return " OR ".join(parts)


def _build_fuzzy_query(query: str,
                       fields: Optional[Iterable[str]] = None) -> str:
    """Build a Tantivy fuzzy-term query for near matches.

    Fuzzy search is token-oriented, so it works for OCR slips like a missing
    accent or one wrong character, while exact lexical search remains the
    higher-confidence group.
    """
    safe = _escape_query(query)
    if not safe:
        return ""
    segmented = _segment_query_if_enabled(query)
    safe_segmented = _escape_query(segmented)

    def _fuzzy_terms(s: str) -> str:
        terms = []
        for tok in s.split():
            if len(tok) <= 2 or any(ch.isdigit() for ch in tok):
                continue
            distance = 1 if len(tok) <= 5 else 2
            terms.append(f"{tok}~{distance}")
        return " ".join(terms)

    fuzzy = _fuzzy_terms(safe)
    fuzzy_segmented = _fuzzy_terms(safe_segmented)
    if not fuzzy and not fuzzy_segmented:
        return ""
    parts = []
    for fname in _normalize_fields(fields):
        weight = C.TANTIVY_FIELD_WEIGHTS.get(fname, 1.0) * 0.7
        field_query = fuzzy_segmented if fname == "body_segmented" and fuzzy_segmented else fuzzy
        if field_query:
            parts.append(f"{fname}:({field_query})^{weight}")
        if fname == "body_segmented" and fuzzy and fuzzy_segmented and fuzzy_segmented != fuzzy:
            parts.append(f"{fname}:({fuzzy})^{weight * 0.4}")
    return " OR ".join(parts)


def _fuzzy_tokens(query: str, *, allow_numeric: bool = False) -> List[str]:
    safe = _escape_query(to_no_diacritic(query or "").lower())
    out: List[str] = []
    for tok in safe.split():
        has_digit = any(ch.isdigit() for ch in tok)
        if len(tok) >= 3 and (allow_numeric or not has_digit):
            out.append(tok)
    return out


def _base_field_of(field: str) -> str:
    """Map a derived field (doc_number_nodiac / body_tri / ...) back to its
    base field so weight lookups hit TANTIVY_FIELD_WEIGHTS correctly."""
    if field.endswith("_nodiac"):
        return field[: -len("_nodiac")]
    return _TRI_SOURCE_FIELD.get(field, field)


def _build_structured_fuzzy_query(index: tantivy.Index,
                                  query: str,
                                  fields: Optional[Iterable[str]] = None):
    """Build real Tantivy FuzzyTermQuery objects.

    The query-parser string syntax is unreliable in tantivy-py for fuzzy terms;
    this path uses the native API and requires most query tokens to match.
    """
    norm_fields = _normalize_fields(fields)
    allow_numeric = bool(set(norm_fields) & set(_BODY_TEXT_FIELDS))
    tokens = _fuzzy_tokens(query, allow_numeric=allow_numeric)
    if not tokens:
        return None
    schema = index.schema
    # Fuzzy over each base field plus its _nodiac twin (the trigram twins
    # are the fuzzy mechanism for substrings, so they are not fuzzy-queried).
    fuzzy_fields = []
    for fname in norm_fields:
        fuzzy_fields.append((fname, 1.0))
        if fname in _METADATA_TEXT_FIELDS:
            fuzzy_fields.append(
                (fname + "_nodiac", C.NODIAC_WEIGHT_SCALE)
            )
        elif fname == "body_original":
            fuzzy_fields.append(("body_no_diacritic", C.NODIAC_WEIGHT_SCALE))
    token_queries = []
    for tok in tokens:
        distance = 1 if len(tok) <= 5 else 2
        field_queries = []
        for fname, scale in fuzzy_fields:
            weight = C.TANTIVY_FIELD_WEIGHTS.get(
                _base_field_of(fname), 1.0
            ) * 0.7 * scale
            try:
                q = tantivy.Query.fuzzy_term_query(
                    schema,
                    fname,
                    tok,
                    distance=distance,
                    transposition_cost_one=True,
                    prefix=False,
                )
            except Exception:
                continue
            field_queries.append(tantivy.Query.boost_query(q, weight))
        if field_queries:
            try:
                token_queries.append(tantivy.Query.disjunction_max_query(field_queries))
            except Exception:
                token_queries.append(tantivy.Query.boolean_query(
                    [(tantivy.Occur.Should, q) for q in field_queries],
                    minimum_number_should_match=1,
                ))
    if not token_queries:
        return None
    min_should = 1
    if len(token_queries) >= 4:
        min_should = max(2, int(round(len(token_queries) * 0.6)))
    elif len(token_queries) >= 2:
        min_should = 2
    return tantivy.Query.boolean_query(
        [(tantivy.Occur.Should, q) for q in token_queries],
        minimum_number_should_match=min_should,
    )


class IndexSchemaMismatch(RuntimeError):
    """Raised when the on-disk Tantivy index predates the current indexer
    schema (e.g. an index written by a newer release). Callers should offer
    a rebuild; SQLite remains the source of truth, so rebuilding is safe."""


class HybridIndex:
    def __init__(self, archive_path: Path,
                 tantivy_dir: Optional[Path] = None):
        self.archive_path = Path(archive_path)
        # `tantivy_dir` override lets the rebuild routine stage a fresh index
        # in a sibling folder before swapping it into place.
        self.tantivy_dir = (
            Path(tantivy_dir) if tantivy_dir is not None
            else self.archive_path / C.TANTIVY_SUBDIR
        )
        self._tan_index: Optional[tantivy.Index] = None
        self._tan_writer: Optional[tantivy.IndexWriter] = None

    # ---------- Lifecycle ----------

    def open(self) -> None:
        self._open_tantivy()

    def close(self) -> None:
        # Drop writer so the lockfile is released; Tantivy closes implicitly.
        self._tan_writer = None
        self._tan_index = None

    def _open_tantivy(self) -> None:
        self.tantivy_dir.mkdir(parents=True, exist_ok=True)
        schema = _build_schema()
        # If the directory has no Tantivy meta yet, create_in initializes it;
        # otherwise we attach to the existing index. tantivy-py auto-detects.
        meta = self.tantivy_dir / "meta.json"
        try:
            if meta.exists():
                self._tan_index = tantivy.Index.open(str(self.tantivy_dir))
            else:
                self._tan_index = tantivy.Index(schema, path=str(self.tantivy_dir))
        except Exception as exc:
            raise IndexSchemaMismatch(
                f"Chỉ mục Tantivy tại {self.tantivy_dir} không mở được "
                f"(có thể do schema phiên bản khác): {exc}"
            ) from exc
        # Custom analyzers are not persisted across sessions — the trigram
        # tokenizer must be re-registered on every open (name first, then
        # the analyzer), or writers/queries against the *_tri fields fail.
        self._tan_index.register_tokenizer(
            C.TRIGRAM_TOKENIZER_NAME, _trigram_analyzer()
        )

    # ---------- Writer (batch) ----------

    def begin_writer(self, heap_bytes: int = 64_000_000) -> None:
        if self._tan_writer is None:
            self._tan_writer = self._tan_index.writer(heap_size=heap_bytes)

    def add_metadata_chunk(self, *,
                            doc_id: str,
                            dossier_id: Optional[int],
                            chunk_id: int,
                            doc_number: str,
                            signer_name: str,
                            issue_org: str,
                            subject: str,
                            recipients: str,
                            metadata_text: str) -> None:
        """Index a metadata chunk in Tantivy.
        Per-field metadata fields are populated so per-field boosts in
        TANTIVY_FIELD_WEIGHTS hit *just this chunk* for queries like
        "số 218" or "Nguyễn Văn A". The body_* fields stay blank.
        Indexer v2 also fills the _nodiac/_tri twins for accent-free and
        substring recall; all writers (importer/admin/rebuild) go through
        here, so the normalization can never drift from the schema."""
        if self._tan_writer is None:
            self.begin_writer()
        per_field = {
            "doc_number": doc_number,
            "signer_name": signer_name,
            "issue_org": issue_org,
            "subject": subject,
            "recipients": recipients,
            "metadata_text": metadata_text,
        }
        doc: dict = {
            "doc_id": doc_id,
            "chunk_id": str(chunk_id),
            "dossier_id": str(dossier_id) if dossier_id is not None else "",
            "body_original": "",
            "body_no_diacritic": "",
            "body_segmented": "",
        }
        for fname, raw in per_field.items():
            text = raw or ""
            doc[fname] = text
            norm = _normalized_index_text(text)
            doc[fname + "_nodiac"] = norm
            doc[fname + "_tri"] = norm
        doc["body_tri"] = ""
        self._tan_writer.add_document(tantivy.Document(**doc))

    def add_body_text_chunk(self, *,
                             doc_id: str,
                             dossier_id: Optional[int],
                             chunk_id: int,
                             body_original: str,
                             body_no_diacritic: str,
                             body_segmented: Optional[str]) -> None:
        """Index a body chunk in Tantivy only."""
        if self._tan_writer is None:
            self.begin_writer()
        norm = _normalized_index_text(body_original)
        self._tan_writer.add_document(tantivy.Document(
            doc_id=doc_id,
            chunk_id=str(chunk_id),
            dossier_id=str(dossier_id) if dossier_id is not None else "",
            doc_number="",
            signer_name="",
            issue_org="",
            subject="",
            recipients="",
            metadata_text="",
            doc_number_nodiac="",
            signer_name_nodiac="",
            issue_org_nodiac="",
            subject_nodiac="",
            recipients_nodiac="",
            metadata_text_nodiac="",
            doc_number_tri="",
            signer_name_tri="",
            issue_org_tri="",
            subject_tri="",
            recipients_tri="",
            metadata_text_tri="",
            body_original=body_original or "",
            body_no_diacritic=body_no_diacritic or "",
            body_segmented=body_segmented or "",
            body_tri=norm,
        ))

    def delete_tantivy_by_doc(self, doc_id: str) -> None:
        if self._tan_writer is None:
            self.begin_writer()
        self._tan_writer.delete_documents("doc_id", doc_id)

    def commit(self) -> None:
        if self._tan_writer is not None:
            self._tan_writer.commit()
            # Background merge threads keep file handles open after commit
            # (large indexes especially); without waiting, Windows denies
            # later renames/deletes of the index directory (e.g. during a
            # staged rebuild or an archive reset).
            try:
                self._tan_writer.wait_merging_threads()
            except Exception:
                pass
            self._tan_writer = None  # release lock
        self._tan_index.reload()

    # ---------- Search ----------

    def search_lexical(self, query: str,
                       top_k: int = C.TANTIVY_TOP_K,
                       filter_doc_ids: Optional[Set[str]] = None,
                       fields: Optional[Iterable[str]] = None,
                       ) -> List[Tuple[int, str, float]]:
        """Return list of (chunk_id, doc_id, score)."""
        search_fields = _normalize_fields(fields)
        boosted = _build_boosted_query(query, search_fields)
        if not boosted:
            return []
        searcher = self._tan_index.searcher()
        try:
            tan_query = self._tan_index.parse_query(boosted, list(search_fields))
        except Exception:
            # Last-resort fallback: parse the plain user query against all fields.
            safe = _escape_query(query)
            if not safe:
                return []
            tan_query = self._tan_index.parse_query(safe, list(search_fields))

        # Over-fetch when filtering, since some hits will be discarded.
        fetch_k = top_k * 3 if filter_doc_ids else top_k
        results = searcher.search(tan_query, limit=fetch_k)
        out: List[Tuple[int, str, float]] = []
        for score, addr in results.hits:
            doc = searcher.doc(addr)
            doc_id_list = doc["doc_id"]
            chunk_id_list = doc["chunk_id"]
            if not doc_id_list or not chunk_id_list:
                continue
            doc_id = doc_id_list[0]
            if filter_doc_ids is not None and doc_id not in filter_doc_ids:
                continue
            out.append((int(chunk_id_list[0]), doc_id, float(score)))
            if len(out) >= top_k:
                break
        return out

    def search_fuzzy(self, query: str,
                     top_k: int = C.TANTIVY_TOP_K,
                     filter_doc_ids: Optional[Set[str]] = None,
                     fields: Optional[Iterable[str]] = None,
                     ) -> List[Tuple[int, str, float]]:
        """Return fuzzy lexical hits as (chunk_id, doc_id, score)."""
        fuzzy_query = _build_structured_fuzzy_query(self._tan_index, query, fields)
        if fuzzy_query is None:
            return []
        searcher = self._tan_index.searcher()

        fetch_k = top_k * 3 if filter_doc_ids else top_k
        results = searcher.search(fuzzy_query, limit=fetch_k)
        out: List[Tuple[int, str, float]] = []
        for score, addr in results.hits:
            doc = searcher.doc(addr)
            doc_id_list = doc["doc_id"]
            chunk_id_list = doc["chunk_id"]
            if not doc_id_list or not chunk_id_list:
                continue
            doc_id = doc_id_list[0]
            if filter_doc_ids is not None and doc_id not in filter_doc_ids:
                continue
            out.append((int(chunk_id_list[0]), doc_id, float(score)))
            if len(out) >= top_k:
                break
        return out
