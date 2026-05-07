"""Fuzzy search to help users correct KIE field text by finding the OCR
word combinations that best match what they're typing.

Search is *region-bounded*: only words that fall inside (or partially overlap)
the field's existing detect bbox are considered. We never fuzzy-match across
other pages or other field regions, so the user can't accidentally pull text
from an unrelated part of the document.

Public API:
    candidates = build_candidates_for_field(canonical_doc, field_instance,
                                             margin_ratio=0.10)
    matches = fuzzy_rank(candidates, query, top_k=5, min_score=55)
"""
from __future__ import annotations

from typing import Iterable

try:
    from rapidfuzz import fuzz as _fuzz
    _HAVE_RAPIDFUZZ = True
except Exception:
    from difflib import SequenceMatcher
    _HAVE_RAPIDFUZZ = False


# Maximum number of consecutive words to combine for one candidate. Limiting
# prevents O(N^2) explosion on dense pages — KIE fields are typically <12
# words anyway.
MAX_WINDOW = 12


def _score(query: str, candidate_text: str) -> float:
    """Return 0..100 similarity score (token-aware partial match)."""
    if not query or not candidate_text:
        return 0.0
    q = query.strip()
    c = candidate_text.strip()
    if not q or not c:
        return 0.0
    if _HAVE_RAPIDFUZZ:
        # token_set_ratio handles word-order differences better than
        # partial_ratio for KIE fields where users may reorder while typing.
        a = _fuzz.token_set_ratio(q, c)
        b = _fuzz.partial_ratio(q, c)
        return float(max(a, b))
    return SequenceMatcher(None, q, c).ratio() * 100.0


def _bbox_intersects(bbox_a: list[float], bbox_b: list[float],
                      margin_ratio: float = 0.10) -> bool:
    """True if `bbox_a` (a single word) overlaps the inflated `bbox_b` (the
    field's detect region)."""
    if not bbox_a or not bbox_b or len(bbox_a) < 4 or len(bbox_b) < 4:
        return False
    bx0, by0, bx1, by1 = bbox_b[:4]
    bw = max(bx1 - bx0, 1.0)
    bh = max(by1 - by0, 1.0)
    mx, my = bw * margin_ratio, bh * margin_ratio
    bx0 -= mx; bx1 += mx; by0 -= my; by1 += my
    ax0, ay0, ax1, ay1 = bbox_a[:4]
    return not (ax1 < bx0 or ax0 > bx1 or ay1 < by0 or ay0 > by1)


def _word_bbox(word: dict) -> list[float]:
    if "bbox" in word and word["bbox"]:
        return list(word["bbox"])
    if all(k in word for k in ("x", "y", "w", "h")):
        return [float(word["x"]), float(word["y"]),
                float(word["x"]) + float(word["w"]),
                float(word["y"]) + float(word["h"])]
    return [0.0, 0.0, 0.0, 0.0]


def _merge_bboxes(bboxes: list[list[float]]) -> list[float]:
    if not bboxes:
        return [0.0, 0.0, 0.0, 0.0]
    xs0 = [b[0] for b in bboxes]; ys0 = [b[1] for b in bboxes]
    xs1 = [b[2] for b in bboxes]; ys1 = [b[3] for b in bboxes]
    return [min(xs0), min(ys0), max(xs1), max(ys1)]


def _words_in_region(canonical_doc: dict, page_index: int,
                       region_bbox: list[float], margin_ratio: float
                       ) -> list[dict]:
    """Return the OCR words on `page_index` whose bbox overlaps the inflated
    region_bbox, sorted in reading order (top-to-bottom, then left-to-right)."""
    pages = canonical_doc.get("pages") or []
    if not (0 <= page_index < len(pages)):
        return []
    page = pages[page_index]
    raw_words = list(page.get("words") or [])
    if not raw_words:
        # Fallback: pull words out of lines
        for line in page.get("lines") or []:
            for w in line.get("words") or []:
                raw_words.append(w)

    selected = []
    for w in raw_words:
        bb = _word_bbox(w)
        if _bbox_intersects(bb, region_bbox, margin_ratio=margin_ratio):
            selected.append(w)
    # Reading-order sort (group by line via y-center, then by x)
    def _sort_key(w):
        b = _word_bbox(w)
        cy = (b[1] + b[3]) / 2.0
        cx = (b[0] + b[2]) / 2.0
        # Bucket y to ~10 px so words on the same line cluster
        return (round(cy / 10.0), cx)
    selected.sort(key=_sort_key)
    return selected


def build_candidates_for_field(canonical_doc: dict,
                                field_instance: dict,
                                margin_ratio: float = 0.10,
                                max_window: int = MAX_WINDOW
                                ) -> list[dict]:
    """Generate every consecutive-word window (1..max_window) inside the
    field's region. Each candidate has: text, bbox, page_index, word_ids."""
    page_index = int(field_instance.get("page_index", 0))
    region = field_instance.get("bbox") or []
    if not region:
        return []
    words = _words_in_region(canonical_doc, page_index, region, margin_ratio)
    if not words:
        return []

    candidates: list[dict] = []
    for start in range(len(words)):
        for length in range(1, max_window + 1):
            end = start + length
            if end > len(words):
                break
            window = words[start:end]
            text = " ".join((w.get("text") or "").strip() for w in window
                             if (w.get("text") or "").strip())
            if not text:
                continue
            bboxes = [_word_bbox(w) for w in window]
            candidates.append({
                "text": text,
                "bbox": _merge_bboxes(bboxes),
                "page_index": page_index,
                "word_ids": [w.get("id") or w.get("word_id") for w in window],
            })
    return candidates


def fuzzy_rank(candidates: list[dict], query: str,
                top_k: int = 5, min_score: float = 55.0) -> list[dict]:
    """Score candidates against the query, return top_k by descending score."""
    q = (query or "").strip()
    if not q or not candidates:
        return []
    scored = []
    for cand in candidates:
        score = _score(q, cand["text"])
        if score >= min_score:
            scored.append((score, cand))
    scored.sort(key=lambda t: t[0], reverse=True)
    out: list[dict] = []
    seen_keys: set[tuple] = set()
    for score, cand in scored[: top_k * 3]:
        # Dedupe by (text, bbox)
        key = (cand["text"], tuple(cand["bbox"]))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        cand_with_score = dict(cand)
        cand_with_score["score"] = score
        out.append(cand_with_score)
        if len(out) >= top_k:
            break
    return out
