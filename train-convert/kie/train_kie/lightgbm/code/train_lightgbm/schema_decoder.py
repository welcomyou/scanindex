from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from .config import MULTI_INSTANCE_FIELDS, SINGLE_INSTANCE_FIELDS


@dataclass
class CandidatePrediction:
    field: str
    score: float
    page_index: int
    line_ids: list[str]
    word_ids: list[str]
    bbox: list[float]
    text: str
    candidate_id: str


MERGEABLE_SINGLE_INSTANCE_FIELDS = {"REGIME_HEADER"}
INITIAL_SCHEMA_RERANK_FIELDS = {"DOC_SUBJECT"}
DISALLOWED_SOURCE_KIND_BY_FIELD = {
    # For org name, same-column blocks often absorb superior-org words.
    # Keep the source for training/oracle analysis, but do not allow it as final output.
    "ISSUE_ORG_NAME": {"same_column_block"},
}
_RE_WS = re.compile(r"\s+")
_RE_DOC_NUMBER_PREFIX = re.compile(r"^\s*so\s*[:.]?\s*\d[\w/-]*", re.IGNORECASE)
_RE_DOC_NUMBER_ANY = re.compile(r"\bso\s*[:.]?\s*\d[\w/-]*", re.IGNORECASE)
_RE_DOC_NUMBER_HAS_LATE_NUMBER = re.compile(r"\bso\s*[:.]?\s*(?:[-/]\s*)+\d", re.IGNORECASE)
_RE_VN_DATE_COMPLETE = re.compile(r"\bngay\s+\d{1,2}\s+thang\s+\d{1,2}\s+nam\s+\d{4}\b", re.IGNORECASE)
_RE_VN_DATE_BAD_ORDER = re.compile(r"\bngay\s+thang\b|\bngay\s+nam\b|\bthang\s+nam\b", re.IGNORECASE)
_RE_DOC_SUBJECT_START = re.compile(
    r"\b(?:ve|ke\s+hoach|bao\s+cao|to\s+trinh|quyet\s+dinh|cong\s+van|thong\s+bao|giay\s+moi|thu\s+moi|chuong\s+trinh)\b",
    re.IGNORECASE,
)
_RE_LEADING_NUMBER_SUBJECT = re.compile(r"^\s*\d+\s+(?:ve|ke\s+hoach|bao\s+cao|to\s+trinh|giay\s+moi|thu\s+moi|chuong\s+trinh)\b", re.IGNORECASE)
_RE_SUBJECT_SEPARATOR = re.compile(r"(?:^|\s)-{3,}(?:\s|$)")
_RE_TOP_FIELD_NOISE = re.compile(r"\b(?:ngay|thang|nam|kinh\s+gui|noi\s+nhan)\b", re.IGNORECASE)
_RE_REGIME_TEXT = re.compile(r"\b(?:dang\s+cong\s+san|cong\s+hoa|doc\s+lap|tu\s+do|hanh\s+phuc)\b", re.IGNORECASE)
_RE_ORG_NAME_HINT = re.compile(
    r"\b(?:ban|van\s+phong|uy\s+ban|dang\s+uy|hoi\s+dong|mat\s+tran|trung\s+tam|phong|so)\b",
    re.IGNORECASE,
)
_RE_SUPERIOR_HINT = re.compile(r"\b(?:thanh\s+uy|tinh\s+uy|huyen\s+uy|quan\s+uy|thi\s+uy|dang\s+bo)\b", re.IGNORECASE)
_RE_NAME_PARTICLE_NOISE = re.compile(r"^(?:ban|dang|cong|san|van|phong|thanh|uy|co|chi|bo)$", re.IGNORECASE)
_RE_SIGNER_CONTEXT = re.compile(
    r"\b(?:t/?m|k/?t|t/?l|tuq|q\.|bi\s+thu|chu\s+tich|pho\s+chu\s+tich|chanh\s+van\s+phong|pho\s+chanh|truong\s+ban|pho\s+truong|giam\s+doc|cuc\s+truong)\b",
    re.IGNORECASE,
)


def _source_kind(candidate: CandidatePrediction) -> str:
    parts = candidate.candidate_id.split(":")
    return parts[2] if len(parts) >= 4 else "unknown"


def _normalize_text(text: str) -> str:
    text = (text or "").replace("Đ", "D").replace("đ", "d").replace("Ä", "D").replace("Ä‘", "d")
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    stripped = stripped.replace("Đ", "D").replace("đ", "d")
    return _RE_WS.sub(" ", stripped.lower()).strip()


def _tokenize_text(text: str) -> list[str]:
    return [token.strip(".,;:()[]{}<>-*") for token in (text or "").replace("\n", " ").split() if token.strip(".,;:()[]{}<>-*")]


def _word_overlap_ratio(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _word_containment_ratio(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(min(len(a), len(b)), 1)


def _has_blocking_word_overlap(a: CandidatePrediction, b: CandidatePrediction, overlap_threshold: float) -> bool:
    a_words = set(a.word_ids)
    b_words = set(b.word_ids)
    return (
        _word_overlap_ratio(a_words, b_words) >= overlap_threshold
        or _word_containment_ratio(a_words, b_words) >= max(0.75, overlap_threshold)
    )


def _greedy_non_overlap(candidates: Iterable[CandidatePrediction], overlap_threshold: float = 0.5) -> list[CandidatePrediction]:
    chosen: list[CandidatePrediction] = []
    for cand in sorted(candidates, key=lambda item: item.score, reverse=True):
        if any(_has_blocking_word_overlap(cand, prev, overlap_threshold) for prev in chosen):
            continue
        chosen.append(cand)
    return chosen


def _bbox_union(boxes: Iterable[list[float]]) -> list[float]:
    boxes = list(boxes)
    if not boxes:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _x_overlap_ratio(a: list[float], b: list[float]) -> float:
    left = max(a[0], b[0])
    right = min(a[2], b[2])
    inter = max(0.0, right - left)
    denom = max(min(a[2] - a[0], b[2] - b[0]), 1e-6)
    return inter / denom


def _vertical_gap(a: list[float], b: list[float]) -> float:
    if a[1] <= b[1]:
        return max(0.0, b[1] - a[3])
    return max(0.0, a[1] - b[3])


def _center_x(box: list[float]) -> float:
    return (box[0] + box[2]) / 2.0


def _top_band(candidate: CandidatePrediction) -> bool:
    # PDF points vary, but admin headers are consistently in the first ~280 pt.
    return candidate.bbox[1] <= 280.0


def _same_column(a: CandidatePrediction, b: CandidatePrediction) -> bool:
    if a.page_index != b.page_index:
        return False
    left_delta = abs(a.bbox[0] - b.bbox[0])
    center_delta = abs(_center_x(a.bbox) - _center_x(b.bbox))
    return _x_overlap_ratio(a.bbox, b.bbox) >= 0.20 or left_delta <= 72.0 or center_delta <= 64.0


def _contains_candidate(candidate: CandidatePrediction, base: CandidatePrediction) -> bool:
    cand_words = set(candidate.word_ids)
    base_words = set(base.word_ids)
    return bool(cand_words and base_words and base_words.issubset(cand_words))


def _signer_pair_score(role: CandidatePrediction, name: CandidatePrediction) -> float | None:
    if role.page_index != name.page_index:
        return None
    if role.bbox[1] > name.bbox[1] + 8.0:
        return None
    vertical_gap = max(0.0, name.bbox[1] - role.bbox[3])
    if vertical_gap > 220.0:
        return None
    role_w = max(role.bbox[2] - role.bbox[0], 1.0)
    name_w = max(name.bbox[2] - name.bbox[0], 1.0)
    center_delta = abs(_center_x(role.bbox) - _center_x(name.bbox))
    x_overlap = _x_overlap_ratio(role.bbox, name.bbox)
    if x_overlap < 0.10 and center_delta > max(role_w, name_w) * 1.4:
        return None
    geometry_score = 1.0 / (1.0 + vertical_gap / 80.0 + center_delta / 220.0)
    return role.score + 0.25 * name.score + geometry_score


def _compatible_merge(field: str, base: CandidatePrediction, other: CandidatePrediction) -> bool:
    if base.page_index != other.page_index:
        return False
    if _word_overlap_ratio(set(base.word_ids), set(other.word_ids)) >= 0.5:
        return False
    base_h = max(base.bbox[3] - base.bbox[1], 1.0)
    other_h = max(other.bbox[3] - other.bbox[1], 1.0)
    gap = _vertical_gap(base.bbox, other.bbox)
    x_overlap = _x_overlap_ratio(base.bbox, other.bbox)
    left_delta = abs(base.bbox[0] - other.bbox[0])
    if field in {"REGIME_HEADER", "ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME"}:
        return gap <= max(36.0, max(base_h, other_h) * 2.2) and (x_overlap >= 0.20 or left_delta <= 96.0)
    return gap <= max(48.0, max(base_h, other_h) * 2.6) and (x_overlap >= 0.25 or left_delta <= 120.0)


def _merge_predictions(preds: list[CandidatePrediction]) -> CandidatePrediction:
    preds = sorted(preds, key=lambda item: (item.page_index, item.bbox[1], item.bbox[0], -item.score))
    line_ids = list(dict.fromkeys(line_id for pred in preds for line_id in pred.line_ids))
    word_ids = list(dict.fromkeys(word_id for pred in preds for word_id in pred.word_ids))
    text = "\n".join(dict.fromkeys(pred.text for pred in preds if pred.text))
    return CandidatePrediction(
        field=preds[0].field,
        score=max(pred.score for pred in preds),
        page_index=preds[0].page_index,
        line_ids=line_ids,
        word_ids=word_ids,
        bbox=_bbox_union(pred.bbox for pred in preds),
        text=text,
        candidate_id="+".join(pred.candidate_id for pred in preds),
    )


def _schema_adjusted_score(field: str, candidate: CandidatePrediction, decoded: dict[str, list[CandidatePrediction]]) -> float:
    score = candidate.score
    normalized = _normalize_text(candidate.text)
    source = _source_kind(candidate)
    num_lines = max(1, len(candidate.line_ids))

    if field in {"ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME"} and _top_band(candidate):
        if source == "same_column_block":
            score += 0.75
        if num_lines > 1:
            score += min(1.25, 0.50 * (num_lines - 1))
        if field == "ISSUE_ORG_SUPERIOR" and num_lines > 3:
            score -= 2.00
        if _RE_TOP_FIELD_NOISE.search(normalized) or _RE_REGIME_TEXT.search(normalized):
            score -= 1.30
        if _RE_DOC_NUMBER_ANY.search(normalized):
            score -= 2.50
        for blocker_field in ("DOC_NUMBER_SYMBOL", "PLACE_DATE"):
            for blocker in decoded.get(blocker_field, []):
                if _has_blocking_word_overlap(candidate, blocker, 0.10):
                    score -= 2.50

    if field == "ISSUE_ORG_NAME":
        superior = decoded.get("ISSUE_ORG_SUPERIOR", [])
        if normalized.startswith("cua "):
            score -= 1.20
        if _RE_ORG_NAME_HINT.search(normalized):
            score += 1.10
        if _RE_SUPERIOR_HINT.search(normalized):
            score -= 1.10
        for sup in superior:
            if _has_blocking_word_overlap(candidate, sup, 0.20):
                score -= 2.50
            elif candidate.page_index == sup.page_index and _same_column(candidate, sup) and candidate.bbox[1] >= sup.bbox[1]:
                score += 0.80
    elif field == "ISSUE_ORG_SUPERIOR":
        for org_name in decoded.get("ISSUE_ORG_NAME", []):
            if _has_blocking_word_overlap(candidate, org_name, 0.10):
                score -= 2.50

    if field == "DOC_SUBJECT":
        if _RE_DOC_SUBJECT_START.search(normalized):
            score += 0.40
            if candidate.bbox[1] <= 290.0:
                score += 0.35
        if normalized.startswith("khan ") and _RE_DOC_SUBJECT_START.search(normalized):
            score -= 1.20
        if _RE_LEADING_NUMBER_SUBJECT.search(normalized):
            score -= 0.70
        if candidate.bbox[1] > 380.0:
            score -= 0.75
        if _RE_DOC_NUMBER_PREFIX.search(normalized):
            score -= 0.55
        for blocker_field in ("DOC_NUMBER_SYMBOL", "PLACE_DATE", "ADDRESSEE", "RECIPIENTS"):
            for blocker in decoded.get(blocker_field, []):
                if _has_blocking_word_overlap(candidate, blocker, 0.10):
                    score -= 2.00

    if field in {"ADDRESSEE", "RECIPIENTS"}:
        anchor = "kinh gui" if field == "ADDRESSEE" else "noi nhan"
        if anchor in normalized:
            score += 0.50
        if source in {"anchor_block", "same_column_block"}:
            score += 0.20

    if field == "SIGNER_NAME":
        if source == "word_window":
            score += 0.35
        if _looks_like_clean_name(candidate.text):
            score += 0.35
        if _name_noise_prefix_len(candidate.text) > 0:
            score -= 0.35

    return score


def _choose_schema_candidate(
    field: str,
    candidates: list[CandidatePrediction],
    threshold: float,
    decoded: dict[str, list[CandidatePrediction]],
    *,
    allow_below_threshold: float = 0.0,
) -> CandidatePrediction | None:
    if not candidates:
        return None
    floor = threshold - allow_below_threshold
    eligible = [cand for cand in candidates if cand.score >= floor]
    if not eligible:
        return None
    return max(eligible, key=lambda cand: (_schema_adjusted_score(field, cand, decoded), cand.score))


def _repair_doc_subject(
    decoded: dict[str, list[CandidatePrediction]],
    field_candidates: dict[str, list[CandidatePrediction]],
    thresholds: dict[str, float],
) -> None:
    current = decoded.get("DOC_SUBJECT", [])
    if not current:
        return
    subject = current[0]
    normalized = _normalize_text(subject.text)
    blockers = decoded.get("DOC_NUMBER_SYMBOL", []) + decoded.get("PLACE_DATE", [])
    overlaps_blocker = any(_has_blocking_word_overlap(subject, blocker, 0.10) for blocker in blockers)
    starts_with_doc_number = bool(_RE_DOC_NUMBER_PREFIX.search(normalized))
    starts_with_leading_number = bool(_RE_LEADING_NUMBER_SUBJECT.search(normalized))
    prefer_title_above = subject.bbox[1] > 160.0 and not _RE_DOC_SUBJECT_START.search(normalized)
    looks_like_body = subject.bbox[1] > 360.0 and not _RE_DOC_SUBJECT_START.search(normalized)
    if not overlaps_blocker and not starts_with_doc_number and not starts_with_leading_number and not looks_like_body and not prefer_title_above:
        return
    threshold = thresholds.get("DOC_SUBJECT", 0.5)
    alternatives = [
        cand
        for cand in field_candidates.get("DOC_SUBJECT", [])
        if cand.page_index == subject.page_index
        and (
            cand.score >= subject.score - (0.75 if starts_with_leading_number else 0.45)
            or ((looks_like_body or prefer_title_above) and cand.score >= threshold - 1.50)
        )
        and not any(_has_blocking_word_overlap(cand, blocker, 0.10) for blocker in blockers)
        and not _RE_DOC_NUMBER_PREFIX.search(_normalize_text(cand.text))
        and not _RE_LEADING_NUMBER_SUBJECT.search(_normalize_text(cand.text))
        and (
            not (looks_like_body or prefer_title_above)
            or (_RE_DOC_SUBJECT_START.search(_normalize_text(cand.text)) and cand.bbox[1] < subject.bbox[1])
        )
    ]
    replacement = _choose_schema_candidate(
        "DOC_SUBJECT",
        alternatives,
        threshold,
        decoded,
        allow_below_threshold=1.60 if (looks_like_body or prefer_title_above) else 0.40,
    )
    if replacement is not None:
        decoded["DOC_SUBJECT"] = [replacement]


def _repair_issue_org_hierarchy(
    decoded: dict[str, list[CandidatePrediction]],
    field_candidates: dict[str, list[CandidatePrediction]],
    thresholds: dict[str, float],
) -> None:
    # Superior/name are a hierarchy in the same top-left header, so choose them jointly.
    for field in ("ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME"):
        current = decoded.get(field, [])
        threshold = thresholds.get(field, 0.5)
        if field == "ISSUE_ORG_SUPERIOR" and current and not _looks_incomplete_header(current[0].text):
            continue
        if field == "ISSUE_ORG_NAME" and current:
            current_text = _normalize_text(current[0].text)
            overlaps_superior = any(_has_blocking_word_overlap(current[0], sup, 0.10) for sup in decoded.get("ISSUE_ORG_SUPERIOR", []))
            has_blocker = any(
                _has_blocking_word_overlap(current[0], blocker, 0.10)
                for blocker_field in ("DOC_NUMBER_SYMBOL", "PLACE_DATE")
                for blocker in decoded.get(blocker_field, [])
            )
            looks_bad = (
                current[0].score < threshold
                or overlaps_superior
                or has_blocker
                or current_text.startswith("cua ")
                or bool(_RE_TOP_FIELD_NOISE.search(current_text))
                or bool(_RE_REGIME_TEXT.search(current_text))
            )
            if not looks_bad:
                continue
        candidates = [
            cand
            for cand in field_candidates.get(field, [])
            if _top_band(cand) and not _RE_TOP_FIELD_NOISE.search(_normalize_text(cand.text))
        ]
        if field == "ISSUE_ORG_SUPERIOR":
            candidates = [cand for cand in candidates if _source_kind(cand) == "same_column_block"]
        if field == "ISSUE_ORG_NAME":
            candidates = [cand for cand in candidates if not _RE_REGIME_TEXT.search(_normalize_text(cand.text))]
        if not candidates:
            continue
        allow_below = 0.40 if field == "ISSUE_ORG_SUPERIOR" else 1.80
        replacement = _choose_schema_candidate(field, candidates, threshold, decoded, allow_below_threshold=allow_below)
        if replacement is None:
            continue
        if not current or _schema_adjusted_score(field, replacement, decoded) > _schema_adjusted_score(field, current[0], decoded) + 0.05:
            decoded[field] = [replacement]


def _repair_top_header_splits(
    decoded: dict[str, list[CandidatePrediction]],
    field_candidates: dict[str, list[CandidatePrediction]],
    thresholds: dict[str, float],
) -> None:
    regime = decoded.get("REGIME_HEADER", [])
    if regime:
        current = regime[0]
        normalized = _normalize_text(current.text)
        if _RE_REGIME_TEXT.search(normalized) and _RE_SUPERIOR_HINT.search(normalized):
            candidates = [
                cand
                for cand in field_candidates.get("REGIME_HEADER", [])
                if cand.page_index == current.page_index
                and _source_kind(cand) == "top_word_cluster"
                and _RE_REGIME_TEXT.search(_normalize_text(cand.text))
                and not _RE_SUPERIOR_HINT.search(_normalize_text(cand.text))
            ]
            replacement = _choose_schema_candidate(
                "REGIME_HEADER",
                candidates,
                thresholds.get("REGIME_HEADER", 0.5),
                decoded,
                allow_below_threshold=1.0,
            )
            if replacement is not None:
                decoded["REGIME_HEADER"] = [replacement]

    superior = decoded.get("ISSUE_ORG_SUPERIOR", [])
    if superior:
        current = superior[0]
        normalized = _normalize_text(current.text)
        if _RE_REGIME_TEXT.search(normalized):
            candidates = [
                cand
                for cand in field_candidates.get("ISSUE_ORG_SUPERIOR", [])
                if cand.page_index == current.page_index
                and _source_kind(cand) == "top_word_cluster"
                and not _RE_REGIME_TEXT.search(_normalize_text(cand.text))
                and (_RE_SUPERIOR_HINT.search(_normalize_text(cand.text)) or cand.bbox[0] < current.bbox[0] + 40.0)
            ]
            replacement = _choose_schema_candidate(
                "ISSUE_ORG_SUPERIOR",
                candidates,
                thresholds.get("ISSUE_ORG_SUPERIOR", 0.5),
                decoded,
                allow_below_threshold=2.5,
            )
            if replacement is not None:
                decoded["ISSUE_ORG_SUPERIOR"] = [replacement]


def _repair_anchored_fields(
    decoded: dict[str, list[CandidatePrediction]],
    field_candidates: dict[str, list[CandidatePrediction]],
    thresholds: dict[str, float],
) -> None:
    for field, anchor in (("ADDRESSEE", "kinh gui"), ("RECIPIENTS", "noi nhan")):
        if decoded.get(field):
            continue
        threshold = thresholds.get(field, 0.5)
        candidates = [cand for cand in field_candidates.get(field, []) if anchor in _normalize_text(cand.text)]
        replacement = _choose_schema_candidate(field, candidates, threshold, decoded, allow_below_threshold=0.35)
        if replacement is not None:
            decoded[field] = [replacement]


def _looks_like_clean_name(text: str) -> bool:
    tokens = _tokenize_text(text)
    if not (2 <= len(tokens) <= 5):
        return False
    if any(any(ch.isdigit() for ch in token) for token in tokens):
        return False
    if any(token.isupper() and len(token) > 1 for token in tokens):
        return False
    return sum(1 for token in tokens if token and token[0].isupper()) >= 2


def _name_noise_prefix_len(text: str) -> int:
    count = 0
    for token in _tokenize_text(text):
        normalized = _normalize_text(token)
        if _RE_NAME_PARTICLE_NOISE.match(normalized) or (token.isupper() and len(token) > 1):
            count += 1
            continue
        break
    return count


def _looks_incomplete_header(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    if normalized.endswith((",", ";", "-", "–")):
        return True
    tail = " ".join(normalized.split()[-2:])
    return tail in {"va", "cua", "ve", "khoa hoc"} or normalized.endswith("khoa")


def _top_header_expansion_score(
    field: str,
    current: CandidatePrediction,
    candidate: CandidatePrediction,
    decoded: dict[str, list[CandidatePrediction]],
) -> float | None:
    if field not in {"ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME"}:
        return None
    if candidate.page_index != current.page_index or not _top_band(candidate):
        return None
    if not _contains_candidate(candidate, current):
        return None
    if len(candidate.line_ids) <= len(current.line_ids):
        return None
    if field == "ISSUE_ORG_SUPERIOR" and not _looks_incomplete_header(current.text):
        return None
    if field == "ISSUE_ORG_NAME":
        current_lines = [_normalize_text(line) for line in (current.text or "").splitlines() if _normalize_text(line)]
        candidate_lines = [_normalize_text(line) for line in (candidate.text or "").splitlines() if _normalize_text(line)]
        extra_lines = candidate_lines[len(current_lines) :] if candidate_lines[: len(current_lines)] == current_lines else []
        if not extra_lines or not any(line.startswith("cua ") for line in extra_lines):
            return None
    if not _same_column(candidate, current):
        return None
    normalized = _normalize_text(candidate.text)
    if _RE_TOP_FIELD_NOISE.search(normalized) or _RE_REGIME_TEXT.search(normalized) or _RE_DOC_NUMBER_ANY.search(normalized):
        return None
    blockers = decoded.get("DOC_NUMBER_SYMBOL", []) + decoded.get("PLACE_DATE", []) + decoded.get("REGIME_HEADER", [])
    if any(_has_blocking_word_overlap(candidate, blocker, 0.10) for blocker in blockers):
        return None
    if field == "ISSUE_ORG_SUPERIOR":
        if len(candidate.line_ids) > 3:
            return None
        if any(_has_blocking_word_overlap(candidate, org_name, 0.10) for org_name in decoded.get("ISSUE_ORG_NAME", [])):
            return None
    if field == "ISSUE_ORG_NAME":
        if len(candidate.line_ids) > 3:
            return None
        if any(_has_blocking_word_overlap(candidate, superior, 0.10) for superior in decoded.get("ISSUE_ORG_SUPERIOR", [])):
            return None
        if current.bbox[1] < 70.0 and candidate.bbox[1] < 70.0:
            # Avoid pulling superior-org header lines into org-name candidates.
            return None
    extra_lines = max(0, len(candidate.line_ids) - len(current.line_ids))
    expansion_bonus = min(2.25, 0.95 * extra_lines)
    source = _source_kind(candidate)
    if source in {"same_column_block", "line_span"}:
        expansion_bonus += 0.35
    if field == "ISSUE_ORG_NAME" and extra_lines:
        expansion_bonus += 0.45
    return _schema_adjusted_score(field, candidate, decoded) + expansion_bonus


def _repair_top_header_expansion(
    decoded: dict[str, list[CandidatePrediction]],
    field_candidates: dict[str, list[CandidatePrediction]],
    thresholds: dict[str, float],
) -> None:
    for field in ("ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME"):
        current_list = decoded.get(field, [])
        if not current_list:
            continue
        current = current_list[0]
        threshold = thresholds.get(field, 0.5)
        current_score = _schema_adjusted_score(field, current, decoded)
        best = current
        best_score = current_score
        for cand in field_candidates.get(field, []):
            if cand.score < threshold - 2.25 and cand.score < current.score - 2.35:
                continue
            score = _top_header_expansion_score(field, current, cand, decoded)
            if score is None:
                continue
            if score > best_score + 0.02:
                best = cand
                best_score = score
        if best is not current:
            decoded[field] = [best]


def _repair_signer_names(
    decoded: dict[str, list[CandidatePrediction]],
    field_candidates: dict[str, list[CandidatePrediction]],
    thresholds: dict[str, float],
) -> None:
    names = decoded.get("SIGNER_NAME", [])
    if not names:
        return
    threshold = thresholds.get("SIGNER_NAME", 0.5)
    repaired = []
    all_candidates = field_candidates.get("SIGNER_NAME", [])
    for name in names:
        if _name_noise_prefix_len(name.text) == 0:
            repaired.append(name)
            continue
        name_words = set(name.word_ids)
        alternatives = [
            cand
            for cand in all_candidates
            if cand.page_index == name.page_index
            and set(cand.word_ids).issubset(name_words)
            and cand.score >= threshold - 0.15
            and _source_kind(cand) == "word_window"
        ]
        replacement = _choose_schema_candidate("SIGNER_NAME", alternatives, threshold, decoded, allow_below_threshold=0.15)
        repaired.append(replacement or name)
    decoded["SIGNER_NAME"] = _greedy_non_overlap(repaired, overlap_threshold=0.5)


def _decode_single_instance(
    field: str,
    candidates: list[CandidatePrediction],
    threshold: float,
    decoded: dict[str, list[CandidatePrediction]],
) -> list[CandidatePrediction]:
    disallowed_sources = DISALLOWED_SOURCE_KIND_BY_FIELD.get(field, set())
    if disallowed_sources:
        candidates = [cand for cand in candidates if _source_kind(cand) not in disallowed_sources]
    candidates = sorted(candidates, key=lambda item: item.score, reverse=True)
    if not candidates:
        return []
    if field in INITIAL_SCHEMA_RERANK_FIELDS:
        base = _choose_schema_candidate(field, candidates, threshold, decoded)
        if base is None:
            return []
    else:
        if candidates[0].score < threshold:
            return []
        base = candidates[0]
    chosen = [base]
    if field not in MERGEABLE_SINGLE_INSTANCE_FIELDS:
        return chosen
    support_floor = max(0.35, threshold * 0.70, base.score * 0.55)
    for cand in candidates:
        if cand.candidate_id == base.candidate_id:
            continue
        if cand.score < support_floor:
            continue
        if _compatible_merge(field, chosen[0], cand):
            chosen.append(cand)
            chosen = [_merge_predictions(chosen)]
    return chosen


def decode_document_predictions(
    field_candidates: dict[str, list[CandidatePrediction]],
    thresholds: dict[str, float],
) -> dict[str, list[CandidatePrediction]]:
    decoded: dict[str, list[CandidatePrediction]] = {}
    for field in SINGLE_INSTANCE_FIELDS:
        threshold = thresholds.get(field, 0.5)
        decoded[field] = _decode_single_instance(field, field_candidates.get(field, []), threshold, decoded)
    for field in MULTI_INSTANCE_FIELDS:
        threshold = thresholds.get(field, 0.5)
        kept = [cand for cand in field_candidates.get(field, []) if cand.score >= threshold]
        decoded[field] = _greedy_non_overlap(kept, overlap_threshold=0.5)
    if "SIGNER_ROLE" in decoded and "SIGNER_NAME" in decoded:
        role_threshold = thresholds.get("SIGNER_ROLE", 0.5)
        role_pair_floor = max(-0.25, role_threshold - 0.90) if role_threshold >= 0 else role_threshold * 1.45
        initial_roles = list(decoded.get("SIGNER_ROLE", []))
        paired_roles = []
        role_candidates = field_candidates.get("SIGNER_ROLE", [])
        paired_name_ids: set[str] = set()
        paired_items: list[tuple[CandidatePrediction, CandidatePrediction, float]] = []
        for name in decoded.get("SIGNER_NAME", []):
            best_role = None
            best_pair_score = None
            for role in role_candidates:
                if role.score < role_pair_floor:
                    continue
                pair_score = _signer_pair_score(role, name)
                if pair_score is None:
                    continue
                if best_pair_score is None or pair_score > best_pair_score:
                    best_pair_score = pair_score
                    best_role = role
            if best_role is not None:
                paired_items.append((best_role, name, float(best_pair_score or best_role.score)))
        if paired_items:
            best_page = max(paired_items, key=lambda item: item[2])[0].page_index
            paired_items = [item for item in paired_items if item[0].page_index == best_page and item[1].page_index == best_page]
            paired_roles = [item[0] for item in paired_items]
            paired_name_ids = {item[1].candidate_id for item in paired_items}
        if decoded.get("SIGNER_NAME"):
            strong_prefixed_roles = [
                role
                for role in initial_roles
                if role.score >= max(role_threshold, role_threshold * 1.25)
                and re.search(r"\b(?:t/?m|k/?t|t/?l|tuq)\b", _normalize_text(role.text), re.IGNORECASE)
            ]
            decoded["SIGNER_ROLE"] = _greedy_non_overlap(paired_roles + strong_prefixed_roles, overlap_threshold=0.5)
            if paired_name_ids:
                decoded["SIGNER_NAME"] = [
                    name for name in decoded.get("SIGNER_NAME", []) if name.candidate_id in paired_name_ids
                ]
            else:
                decoded["SIGNER_NAME"] = []
        else:
            decoded["SIGNER_ROLE"] = _greedy_non_overlap(initial_roles, overlap_threshold=0.5)
    _repair_top_header_splits(decoded, field_candidates, thresholds)
    _repair_issue_org_hierarchy(decoded, field_candidates, thresholds)
    _repair_top_header_expansion(decoded, field_candidates, thresholds)
    _repair_doc_subject(decoded, field_candidates, thresholds)
    _repair_anchored_fields(decoded, field_candidates, thresholds)
    _repair_signer_names(decoded, field_candidates, thresholds)
    for field in MULTI_INSTANCE_FIELDS:
        decoded[field] = _greedy_non_overlap(decoded.get(field, []), overlap_threshold=0.5)
    return decoded


def link_signers(decoded: dict[str, list[CandidatePrediction]]) -> list[dict]:
    roles = sorted(decoded.get("SIGNER_ROLE", []), key=lambda item: (item.page_index, item.bbox[1], item.bbox[0]))
    names = sorted(decoded.get("SIGNER_NAME", []), key=lambda item: (item.page_index, item.bbox[1], item.bbox[0]))
    links: list[dict] = []
    used_names: set[str] = set()
    for role in roles:
        best_name = None
        best_distance = None
        for name in names:
            if name.candidate_id in used_names or name.page_index != role.page_index:
                continue
            vertical_gap = abs(name.bbox[1] - role.bbox[3])
            horizontal_gap = abs((name.bbox[0] + name.bbox[2]) / 2.0 - (role.bbox[0] + role.bbox[2]) / 2.0)
            distance = vertical_gap + 0.25 * horizontal_gap
            if best_distance is None or distance < best_distance:
                best_name = name
                best_distance = distance
        if best_name is not None:
            used_names.add(best_name.candidate_id)
            links.append(
                {
                    "type": "signed_by",
                    "from_candidate_id": role.candidate_id,
                    "to_candidate_id": best_name.candidate_id,
                }
            )
    return links
