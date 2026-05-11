"""Paragraph-level chunker for OCR'd PDF text (v2).

Goal: produce a small number of useful retrieval units per page instead of
one tiny chunk per OCR line.

Two retrieval-unit kinds the importer assembles per document:

* body chunks: adjacent OCR blocks merged into paragraphs, with very short
  noise blocks dropped.
* metadata chunk: one synthesized chunk per document built from the raw KIE
  fields and indexed by Tantivy.

Every emitted chunk carries chunk_type so downstream queries can filter or
boost; merge_reason is debug-only metadata.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import constants as C


# Vietnamese sentence boundary: . ? ! ; followed by whitespace.
_SENT_END_RE = re.compile(r"(?<=[\.\?\!;])\s+")
_PUNCT_STRIP_RE = re.compile(r"[^\w\s]+", flags=re.UNICODE)


@dataclass
class Block:
    page: int
    block_idx: int
    text: str
    bbox: Tuple[float, float, float, float]   # (x0, y0, x1, y1)
    font_size: Optional[float] = None
    region: Optional[str] = None              # 'header' | 'body' | 'footer' | 'signature'


@dataclass
class Chunk:
    page: int
    block_idx: int                            # block_idx of the first source block
    text: str
    bbox: Tuple[float, float, float, float]
    word_count: int
    chunk_type: str = "body"                  # 'metadata' | 'body'
    source_blocks: List[int] = field(default_factory=list)
    merge_reason: str = "body_single"


# ---------------------------------------------------------------- helpers


def _word_count(text: str) -> int:
    return len(text.split()) if text else 0


def _bbox_union(boxes: List[Tuple[float, float, float, float]]) -> Tuple[float, float, float, float]:
    if not boxes:
        return (0.0, 0.0, 0.0, 0.0)
    xs0, ys0, xs1, ys1 = zip(*boxes)
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def _line_height_estimate(blocks: List[Block]) -> float:
    sizes = [b.font_size for b in blocks if b.font_size]
    if not sizes:
        return 12.0
    sizes.sort()
    return sizes[len(sizes) // 2]


def _normalize_for_noise(text: str) -> str:
    """Lower + strip diacritics + collapse whitespace + drop punctuation —
    used only for blacklist comparison, not for storage."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    no_acc = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    no_acc = no_acc.replace("đ", "d").replace("Đ", "D").lower()
    no_punct = _PUNCT_STRIP_RE.sub(" ", no_acc)
    return " ".join(no_punct.split()).strip()


def is_noise_block(text: str) -> bool:
    """True for body blocks that should be dropped before chunking.

    Rules (any one matches):
      1. After normalisation the entire text is empty or pure digits / runs
         of single punctuation chars (page numbers, separators).
      2. Word count below ``CHUNK_NOISE_MIN_WORDS`` AND the normalised text
         matches a noise blacklist token.
    """
    raw = (text or "").strip()
    if not raw:
        return True
    norm = _normalize_for_noise(raw)
    if not norm:
        return True
    # Pure digits or single-character lines like "*" / "-" / "/"
    if norm.isdigit():
        return True
    if len(norm) <= 2 and not any(ch.isalnum() for ch in raw):
        return True
    words = norm.split()
    if len(words) < C.CHUNK_NOISE_MIN_WORDS:
        if norm in C.CHUNK_NOISE_TOKENS:
            return True
        # Generic short fragments like "Số:" / "Trang 5" — drop unless they
        # look like a sentence (have a verb-ish trailing word).
        if all(w in C.CHUNK_NOISE_TOKENS or w.isdigit() for w in words):
            return True
    return False


# ---------------------------------------------------------------- splitting


def _split_long_text(text: str) -> List[str]:
    """Split a paragraph above CHUNK_MAX_BODY_WORDS into sentence-aligned
    pieces with CHUNK_OVERLAP_WORDS overlap. Falls back to a sliding
    word window when no sentence boundary fits."""
    sentences = _SENT_END_RE.split(text)
    if len(sentences) <= 1:
        return _split_words(text.split())

    out: List[str] = []
    cur: List[str] = []
    for sent in sentences:
        sw = sent.split()
        if not sw:
            continue
        if cur and len(cur) + len(sw) > C.CHUNK_MAX_BODY_WORDS:
            out.append(" ".join(cur))
            cur = cur[-C.CHUNK_OVERLAP_WORDS:] + sw
        else:
            cur.extend(sw)
    if cur:
        out.append(" ".join(cur))
    return out


def _split_words(words: List[str]) -> List[str]:
    if not words:
        return []
    step = max(1, C.CHUNK_MAX_BODY_WORDS - C.CHUNK_OVERLAP_WORDS)
    out: List[str] = []
    i = 0
    while i < len(words):
        out.append(" ".join(words[i:i + C.CHUNK_MAX_BODY_WORDS]))
        i += step
    return out


# ---------------------------------------------------------------- merge


def _can_merge(a: Block, b: Block, line_height: float) -> bool:
    """Loose merge predicate compared to v1: we drop the font-size diff
    constraint (OCR fonts are unreliable) and widen the vertical gap to
    3× line-height so paragraph-internal blank lines still merge."""
    if a.page != b.page:
        return False
    if a.region and b.region and a.region != b.region:
        return False
    y_gap = b.bbox[1] - a.bbox[3]
    # Vertical reading: b sits below a and the gap is at most a few line heights.
    if y_gap < -0.2 * line_height:
        return False
    if y_gap > C.CHUNK_MERGE_Y_RATIO * line_height:
        return False
    return True


def chunk_blocks(blocks: List[Block]) -> List[Chunk]:
    """Merge consecutive non-noise blocks into paragraph chunks.

    Algorithm:
      1. Drop noise blocks up-front (page numbers, "STT", lone punctuation).
      2. Walk remaining blocks in reading order. Accumulate into ``pending``
         until either the next block can't merge (different page / region /
         vertical gap too large) OR the running word count would push the
         next merge beyond ``CHUNK_TARGET_BODY_WORDS``.
      3. Emit chunks. Below MIN: keep merging with the next paragraph if
         possible; otherwise emit as-is (truly isolated short paragraph).
      4. Above MAX: split via sentence boundary with overlap.

    The output ``chunk_type`` is always ``'body'``; the importer prepends
    its own metadata chunk."""
    if not blocks:
        return []

    # Step 1: drop noise.
    clean = [b for b in blocks if not is_noise_block(b.text)]
    if not clean:
        return []

    line_height = _line_height_estimate(clean)
    chunks: List[Chunk] = []
    pending: List[Block] = []

    def emit(reason_override: Optional[str] = None) -> None:
        if not pending:
            return
        joined = " ".join(b.text.strip() for b in pending if b.text and b.text.strip())
        joined = re.sub(r"\s+", " ", joined).strip()
        if not joined:
            pending.clear()
            return
        wc = _word_count(joined)
        boxes = [b.bbox for b in pending]
        sources = [b.block_idx for b in pending]
        first_page = pending[0].page

        # Tiny-chunk rescue: if the staged paragraph is below MIN and the
        # previous emitted chunk is on the same page and still has room,
        # fold this into it instead of emitting a standalone tiny chunk.
        # Eliminates the long tail of 3-15 word chunks that bloated v2.0.
        if (wc < C.CHUNK_MIN_BODY_WORDS
                and chunks
                and chunks[-1].chunk_type == "body"
                and chunks[-1].page == first_page
                and chunks[-1].word_count + wc <= C.CHUNK_MAX_BODY_WORDS):
            prev = chunks[-1]
            prev.text = (prev.text + " " + joined).strip()
            prev.word_count = _word_count(prev.text)
            prev.bbox = _bbox_union([prev.bbox, _bbox_union(boxes)])
            prev.source_blocks = list(prev.source_blocks) + list(sources)
            prev.merge_reason = "body_merged"
            pending.clear()
            return

        if wc > C.CHUNK_MAX_BODY_WORDS:
            parts = _split_long_text(joined)
            for i, part in enumerate(parts):
                chunks.append(Chunk(
                    page=first_page,
                    block_idx=pending[0].block_idx,
                    text=part,
                    bbox=_bbox_union(boxes),
                    word_count=_word_count(part),
                    source_blocks=list(sources),
                    chunk_type="body",
                    merge_reason=f"body_split_{i + 1}",
                ))
        else:
            reason = reason_override or ("body_merged" if len(pending) > 1 else "body_single")
            chunks.append(Chunk(
                page=first_page,
                block_idx=pending[0].block_idx,
                text=joined,
                bbox=_bbox_union(boxes),
                word_count=wc,
                source_blocks=list(sources),
                chunk_type="body",
                merge_reason=reason,
            ))
        pending.clear()

    for blk in clean:
        if not pending:
            pending.append(blk)
            continue
        last = pending[-1]
        running = sum(_word_count(b.text) for b in pending)
        next_wc = _word_count(blk.text)

        if not _can_merge(last, blk, line_height):
            # Hard boundary (different page / region / gap too large).
            emit()
            pending.append(blk)
            continue

        # Soft boundary: only merge while the running paragraph is below
        # TARGET. Once we cross it, emit and start a new paragraph.
        if running >= C.CHUNK_TARGET_BODY_WORDS and next_wc >= C.CHUNK_MIN_BODY_WORDS:
            emit()
            pending.append(blk)
            continue

        pending.append(blk)

    emit()

    # Step 3: rescue tiny tail chunks. If the last chunk is below MIN_BODY
    # words and the previous chunk is on the same page, fold it in.
    if (len(chunks) >= 2
            and chunks[-1].word_count < C.CHUNK_MIN_BODY_WORDS
            and chunks[-1].page == chunks[-2].page):
        tail = chunks.pop()
        prev = chunks[-1]
        merged_text = (prev.text + " " + tail.text).strip()
        merged_wc = _word_count(merged_text)
        if merged_wc <= C.CHUNK_MAX_BODY_WORDS:
            prev.text = merged_text
            prev.word_count = merged_wc
            prev.bbox = _bbox_union([prev.bbox, tail.bbox])
            prev.source_blocks = list(prev.source_blocks) + list(tail.source_blocks)
            prev.merge_reason = "body_merged"
        else:
            chunks.append(tail)

    return chunks


# ---------------------------------------------------------------- metadata chunk


# KIE label → display label in the synthesised metadata chunk. Order
# matters: header → identifier → date → subject → recipients → signer
# → marks. Order keeps prose readable and groups related fields so the
# Tantivy sees consistent context.
_METADATA_LABELS: List[Tuple[str, str]] = [
    ("DOC_TYPE",            "Loại văn bản"),
    ("DOC_NUMBER_SYMBOL",   "Số ký hiệu"),
    ("ISSUE_ORG_SUPERIOR",  "Cơ quan cấp trên"),
    ("ISSUE_ORG_NAME",      "Cơ quan ban hành"),
    ("PLACE_DATE",          "Ngày tháng"),
    ("DOC_SUBJECT",         "Trích yếu"),
    ("ADDRESSEE",           "Kính gửi"),
    ("RECIPIENTS",          "Nơi nhận"),
    ("SIGNER_ROLE",         "Chức vụ người ký"),
    ("SIGNER_NAME",         "Người ký"),
    ("URGENCY_MARK",        "Độ khẩn"),
    ("SECRECY_MARK",        "Độ mật"),
    ("CIRCULATION_MARK",    "Hình thức lưu hành"),
    # REGIME_HEADER deliberately omitted — it's the boilerplate
    # "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM" header that appears on every
    # state document and adds nothing to retrieval.
]


def synthesize_metadata_chunk(kie_fields: Dict[str, str]) -> Optional[Chunk]:
    """Build the per-document metadata chunk from cleaned KIE field values.

    ``kie_fields`` keys are the same ``kie_*`` column names the importer
    writes to ``documents`` (e.g. ``kie_doc_subject``); values are the
    text-normalised single-line strings.

    Returns ``None`` if no KIE field had a value — the document then has
    no metadata chunk and Kho relies on body chunks alone.
    """
    parts: List[str] = []
    for label, vi in _METADATA_LABELS:
        col = f"kie_{label.lower()}"
        text = (kie_fields.get(col) or "").strip()
        if not text:
            continue
        # Already single-line by importer normalisation, but be defensive:
        text = re.sub(r"\s+", " ", text).strip()
        parts.append(f"{vi}: {text}.")
    if not parts:
        return None
    body = " ".join(parts)
    return Chunk(
        page=1,
        block_idx=-1,                 # sentinel: synthesised, not from a PDF block
        text=body,
        bbox=(0.0, 0.0, 0.0, 0.0),
        word_count=_word_count(body),
        chunk_type="metadata",
        source_blocks=[],
        merge_reason="kie_metadata",
    )

