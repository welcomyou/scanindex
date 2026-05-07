from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


START_LABELS = {
    "REGIME_HEADER",
    "ISSUE_ORG_SUPERIOR",
    "ISSUE_ORG_NAME",
    "DOC_NUMBER_SYMBOL",
    "PLACE_DATE",
    "DOC_SUBJECT",
}
SIGNER_LABELS = {"SIGNER_ROLE", "SIGNER_NAME"}

DOC_START_FEATURES = [
    "regime_score",
    "org_header_score",
    "doc_number_standalone_score",
    "place_date_standalone_score",
    "subject_score",
    "doc_number_date_alignment_score",
    "header_completeness_score",
    "reference_line_penalty",
]
SIGNER_FEATURES = [
    "recipients_score",
    "signer_role_score",
    "signer_name_score",
    "has_noi_nhan_regex",
    "has_tm_kt_tl_tuq_regex",
    "relative_page_position",
]

_RE_WS = re.compile(r"\s+")
_RE_DOC_NUMBER = re.compile(r"\bso\s*[:.]?\s*\d", re.IGNORECASE)
_RE_DOC_SYMBOL_VALUE = re.compile(r"\b\d{1,5}\s*(?:[/.-]\s*[a-z0-9]+)+", re.IGNORECASE)
_RE_DOC_NUMBER_PREFIX = re.compile(r"^\W*so\s*[:.]?\s*\d", re.IGNORECASE)
_RE_PLACE_DATE_STRICT = re.compile(
    r"\bngay\s+\d{1,2}\s+thang\s+\d{1,2}\s+nam\s+\d{4}\b",
    re.IGNORECASE,
)
_RE_NOI_NHAN = re.compile(r"\bnoi\s*nhan\b", re.IGNORECASE)
_RE_SIGNER_PREFIX = re.compile(r"\b(?:t/?m|k/?t|t/?l|tuq|q\.)\b", re.IGNORECASE)
_RE_DIGIT = re.compile(r"\d")

_ORG_KEYWORDS = (
    "dang uy",
    "ban chap hanh",
    "ban thuong vu",
    "uy ban",
    "ubnd",
    "hdnd",
    "hoi dong",
    "mat tran",
    "bo ",
    "so ",
    "phong",
    "ban ",
    "cong an",
    "vien kiem sat",
    "toa an",
    "doan ",
    "hoi ",
    "truong",
    "cuc ",
    "huyen uy",
    "tinh uy",
)
_SUBJECT_KEYWORDS = (
    "ve viec",
    "quyet dinh",
    "ke hoach",
    "bao cao",
    "thong bao",
    "to trinh",
    "cong van",
    "chuong trinh",
    "huong dan",
    "quy che",
    "nghi quyet",
    "de an",
    "ket luan",
    "bien ban",
    "phuong an",
)
_SIGNER_ROLE_KEYWORDS = (
    "chu tich",
    "pho chu tich",
    "bi thu",
    "pho bi thu",
    "giam doc",
    "pho giam doc",
    "tong giam doc",
    "chanh van phong",
    "pho chanh",
    "truong ban",
    "pho truong",
    "truong phong",
    "pho phong",
    "thu truong",
    "bo truong",
    "cuc truong",
    "nguoi ky",
)
_SURNAME_HINTS = {
    "nguyen",
    "tran",
    "le",
    "pham",
    "hoang",
    "huynh",
    "phan",
    "vu",
    "vo",
    "dang",
    "bui",
    "do",
    "ho",
    "ngo",
    "duong",
    "ly",
    "trinh",
    "dinh",
    "mai",
    "cao",
    "dao",
    "luu",
    "truong",
}


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def strip_accents(text: str) -> str:
    text = (text or "").replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def norm_text(text: str) -> str:
    return _RE_WS.sub(" ", strip_accents(text).lower()).strip()


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def bbox(line: dict[str, Any]) -> tuple[float, float, float, float]:
    raw = line.get("bbox") or [0.0, 0.0, 0.0, 0.0]
    try:
        return tuple(float(v) for v in raw[:4])  # type: ignore[return-value]
    except Exception:
        return (0.0, 0.0, 0.0, 0.0)


def line_pos(line: dict[str, Any], page: dict[str, Any]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox(line)
    width = max(float(page.get("width") or 1.0), 1.0)
    height = max(float(page.get("height") or 1.0), 1.0)
    return x0 / width, y0 / height, x1 / width, y1 / height


def page_lines(page: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        line
        for line in (page.get("lines") or [])
        if (line.get("text") or "").strip()
    ]


def uppercase_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch.upper() == ch and ch.lower() != ch.upper()) / len(letters)


def token_list(text: str) -> list[str]:
    return [tok for tok in re.split(r"[^A-Za-zÀ-ỹĐđ]+", text or "") if tok]


def doc_id_from_path(path: Path, payload: dict[str, Any]) -> str:
    if payload.get("doc_id"):
        return str(payload["doc_id"])
    match = re.search(r"__([0-9a-fA-F]+)\.json$", path.name)
    return match.group(1) if match else path.stem


def label_instances(payload: dict[str, Any]) -> list[dict[str, Any]]:
    annotation = payload.get("annotation", payload)
    instances = annotation.get("field_instances") or []
    return instances if isinstance(instances, list) else []


def canonical_for_label(path: Path, payload: dict[str, Any], ocr_root: Path) -> tuple[Path | None, dict[str, Any] | None]:
    if payload.get("pages"):
        return path, payload
    source = payload.get("source_canonical_json")
    if source and Path(source).exists():
        canonical_path = Path(source)
        return canonical_path, read_json(canonical_path)
    stem = path.name.split("__", 1)[0]
    digital_match = re.search(r"DIGITAL_(\d+)", path.name, re.IGNORECASE)
    candidates = [
        ocr_root / f"{stem}_ocr.pdf.json",
        ocr_root / f"{stem}_ocr.json",
        ocr_root / f"{stem}.json",
    ]
    if digital_match:
        idx = int(digital_match.group(1))
        candidates.extend(
            [
                ocr_root / f"DIGITAL ({idx})_ocr.pdf.json",
                ocr_root / f"DIGITAL ({idx})_ocr.json",
                ocr_root / f"DIGITAL ({idx}).json",
            ]
        )
    for candidate in candidates:
        if candidate.exists():
            return candidate, read_json(candidate)
    matches = sorted(ocr_root.rglob(f"{stem}_ocr*.json"))
    if matches:
        return matches[0], read_json(matches[0])
    return None, None


def deterministic_split(doc_id: str, val_ratio: float, test_ratio: float) -> str:
    bucket = int(hashlib.sha1(doc_id.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    if bucket < test_ratio:
        return "test"
    if bucket < test_ratio + val_ratio:
        return "val"
    return "train"


def gold_pages(instances: list[dict[str, Any]]) -> tuple[set[int], set[int]]:
    start_pages: set[int] = set()
    signer_pages: set[int] = set()
    for inst in instances:
        label = inst.get("label")
        try:
            page_index = int(inst.get("page_index"))
        except Exception:
            continue
        if label in START_LABELS:
            start_pages.add(page_index)
        if label in SIGNER_LABELS:
            signer_pages.add(page_index)
    return start_pages, signer_pages


def regime_score(page: dict[str, Any]) -> tuple[float, str]:
    top_lines = [line for line in page_lines(page) if line_pos(line, page)[1] < 0.34]
    has_regime = False
    has_motto = False
    right_bonus = 0.0
    evidence = []
    for line in top_lines:
        text = line.get("text") or ""
        norm = norm_text(text)
        x0, y0, x1, _ = line_pos(line, page)
        cx = (x0 + x1) / 2.0
        if "cong hoa xa hoi chu nghia" in norm or "dang cong san viet nam" in norm:
            has_regime = True
            evidence.append(text)
            if cx > 0.42:
                right_bonus = max(right_bonus, 0.10)
        if "doc lap" in norm and "tu do" in norm and "hanh phuc" in norm:
            has_motto = True
            evidence.append(text)
            if cx > 0.42:
                right_bonus = max(right_bonus, 0.10)
    return clamp01((0.70 if has_regime else 0.0) + (0.30 if has_motto else 0.0) + right_bonus), " | ".join(evidence[:3])


def issue_org_score(page: dict[str, Any]) -> tuple[float, str]:
    best = 0.0
    evidence = ""
    for line in page_lines(page):
        text = line.get("text") or ""
        norm = norm_text(text)
        x0, y0, x1, _ = line_pos(line, page)
        if y0 > 0.38:
            continue
        cx = (x0 + x1) / 2.0
        keyword_hits = sum(1 for kw in _ORG_KEYWORDS if kw in norm)
        if keyword_hits == 0:
            continue
        score = 0.35 + min(0.30, keyword_hits * 0.12)
        if cx < 0.45:
            score += 0.20
        if y0 < 0.22:
            score += 0.10
        if uppercase_ratio(text) >= 0.55:
            score += 0.10
        score = clamp01(score)
        if score > best:
            best = score
            evidence = text
    return best, evidence


def org_header_score(page: dict[str, Any]) -> tuple[float, str]:
    return issue_org_score(page)


def _line_shape_penalty(norm: str, x0: float, x1: float) -> float:
    penalty = 0.0
    if norm.startswith("(") or norm.endswith(")"):
        penalty += 0.35
    if len(norm) > 70:
        penalty += 0.25
    if (x1 - x0) > 0.62:
        penalty += 0.20
    return penalty


def _doc_number_line_candidates(page: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    for line in page_lines(page):
        text = line.get("text") or ""
        norm = norm_text(text)
        x0, y0, x1, _ = line_pos(line, page)
        if y0 > 0.48:
            continue
        has_doc_number = bool(_RE_DOC_NUMBER.search(norm) or _RE_DOC_SYMBOL_VALUE.search(norm))
        has_prefix = bool(_RE_DOC_NUMBER_PREFIX.search(norm))
        if not has_doc_number and not re.search(r"^\W*so\b", norm):
            continue
        width = max(0.0, x1 - x0)
        cx = (x0 + x1) / 2.0
        score = 0.0
        if has_prefix:
            score += 0.62
        elif re.search(r"^\W*so\b", norm):
            score += 0.35
        elif has_doc_number:
            score += 0.12
        if _RE_DOC_SYMBOL_VALUE.search(norm):
            score += 0.22
        if x0 < 0.34:
            score += 0.10
        if cx < 0.52:
            score += 0.08
        if width <= 0.42:
            score += 0.10
        elif width <= 0.56:
            score += 0.04
        if 0.12 <= y0 <= 0.44:
            score += 0.06
        score -= _line_shape_penalty(norm, x0, x1)
        candidates.append(
            {
                "score": clamp01(score),
                "text": text,
                "norm": norm,
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "width": width,
                "has_standalone_prefix": has_prefix,
                "has_doc_number": has_doc_number,
            }
        )
    return sorted(candidates, key=lambda item: float(item["score"]), reverse=True)


def _place_date_line_candidates(page: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    for line in page_lines(page):
        text = line.get("text") or ""
        norm = norm_text(text)
        x0, y0, x1, _ = line_pos(line, page)
        if y0 > 0.50:
            continue
        tokens = sum(1 for kw in ("ngay", "thang", "nam") if re.search(rf"\b{kw}\b", norm))
        if tokens < 2:
            continue
        width = max(0.0, x1 - x0)
        cx = (x0 + x1) / 2.0
        score = 0.0
        if _RE_PLACE_DATE_STRICT.search(norm):
            score += 0.72
        elif tokens >= 3 and re.search(r"\b\d{4}\b", norm):
            score += 0.55
        elif tokens >= 2:
            score += 0.32
        if cx > 0.45:
            score += 0.12
        if x0 > 0.32:
            score += 0.08
        if width <= 0.55:
            score += 0.08
        if 0.16 <= y0 <= 0.48:
            score += 0.05
        score -= _line_shape_penalty(norm, x0, x1)
        candidates.append(
            {
                "score": clamp01(score),
                "text": text,
                "norm": norm,
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "width": width,
            }
        )
    return sorted(candidates, key=lambda item: float(item["score"]), reverse=True)


def doc_number_standalone_score(page: dict[str, Any]) -> tuple[float, str]:
    candidates = _doc_number_line_candidates(page)
    if not candidates:
        return 0.0, ""
    best = candidates[0]
    return float(best["score"]), str(best["text"])


def place_date_standalone_score(page: dict[str, Any]) -> tuple[float, str]:
    candidates = _place_date_line_candidates(page)
    if not candidates:
        return 0.0, ""
    best = candidates[0]
    return float(best["score"]), str(best["text"])


def doc_number_date_alignment_score(page: dict[str, Any]) -> tuple[float, str]:
    doc_candidates = [item for item in _doc_number_line_candidates(page) if float(item["score"]) >= 0.45]
    date_candidates = [item for item in _place_date_line_candidates(page) if float(item["score"]) >= 0.45]
    if not doc_candidates or not date_candidates:
        return 0.0, ""
    best_score = 0.0
    best_evidence = ""
    for doc_item in doc_candidates[:3]:
        for date_item in date_candidates[:3]:
            y_gap = abs(float(doc_item["y0"]) - float(date_item["y0"]))
            if y_gap > 0.11:
                continue
            score = 0.55
            if float(doc_item["x0"]) < 0.38 and float(date_item["x0"]) > 0.30:
                score += 0.25
            if y_gap <= 0.045:
                score += 0.15
            score += 0.05 * min(float(doc_item["score"]), float(date_item["score"]))
            score = clamp01(score)
            if score > best_score:
                best_score = score
                best_evidence = f"{doc_item['text']} | {date_item['text']}"
    return best_score, best_evidence


def reference_line_penalty(page: dict[str, Any]) -> tuple[float, str]:
    best = 0.0
    evidence = ""
    for line in page_lines(page):
        text = line.get("text") or ""
        norm = norm_text(text)
        x0, y0, x1, _ = line_pos(line, page)
        if y0 > 0.56:
            continue
        has_doc_or_date = bool(
            _RE_DOC_NUMBER.search(norm)
            or _RE_DOC_SYMBOL_VALUE.search(norm)
            or _RE_PLACE_DATE_STRICT.search(norm)
        )
        if not has_doc_or_date:
            continue
        standalone_prefix = bool(_RE_DOC_NUMBER_PREFIX.search(norm))
        shape_penalty = _line_shape_penalty(norm, x0, x1)
        score = 0.0
        if shape_penalty and not standalone_prefix:
            score = min(1.0, 0.45 + shape_penalty)
        if len(norm) > 85 and not standalone_prefix:
            score = max(score, 0.65)
        if score > best:
            best = score
            evidence = text
    return best, evidence


def header_completeness_score(page: dict[str, Any]) -> tuple[float, str]:
    regime, regime_text = regime_score(page)
    org, org_text = org_header_score(page)
    doc, doc_text = doc_number_standalone_score(page)
    date, date_text = place_date_standalone_score(page)
    subject, subject_text = subject_score(page)
    score = 0.0
    score += 0.28 * doc
    score += 0.20 * org
    score += 0.18 * regime
    score += 0.18 * date
    score += 0.12 * subject
    if doc >= 0.65 and date >= 0.55:
        score += 0.08
    if org >= 0.55 and regime >= 0.55:
        score += 0.04
    evidence = " | ".join(text for text in (org_text, regime_text, doc_text, date_text, subject_text) if text)
    return clamp01(score), evidence[:300]


def doc_number_score(page: dict[str, Any]) -> tuple[float, str]:
    best = 0.0
    evidence = ""
    for line in page_lines(page):
        text = line.get("text") or ""
        norm = norm_text(text)
        if line_pos(line, page)[1] > 0.46:
            continue
        score = 0.0
        if _RE_DOC_NUMBER.search(norm):
            score = 0.80
            if _RE_DOC_SYMBOL_VALUE.search(norm):
                score = 1.0
        elif re.search(r"\bso\b", norm):
            score = 0.35
        if score > best:
            best = score
            evidence = text
    return best, evidence


def place_date_score(page: dict[str, Any]) -> tuple[float, str]:
    best = 0.0
    evidence = ""
    for line in page_lines(page):
        text = line.get("text") or ""
        norm = norm_text(text)
        x0, y0, x1, _ = line_pos(line, page)
        if y0 > 0.46:
            continue
        tokens = sum(1 for kw in ("ngay", "thang", "nam") if re.search(rf"\b{kw}\b", norm))
        score = 0.0
        if _RE_PLACE_DATE_STRICT.search(norm):
            score = 1.0
        elif tokens >= 3 and re.search(r"\b\d{4}\b", norm):
            score = 0.85
        elif tokens >= 2:
            score = 0.55
        if score and (x0 + x1) / 2.0 > 0.45:
            score += 0.10
        score = clamp01(score)
        if score > best:
            best = score
            evidence = text
    return best, evidence


def subject_score(page: dict[str, Any]) -> tuple[float, str]:
    best = 0.0
    evidence = ""
    for line in page_lines(page):
        text = line.get("text") or ""
        norm = norm_text(text)
        _, y0, _, _ = line_pos(line, page)
        if y0 < 0.16 or y0 > 0.68:
            continue
        score = 0.0
        if "ve viec" in norm:
            score = 1.0
        elif any(kw in norm for kw in _SUBJECT_KEYWORDS):
            score = 0.75
        if score and uppercase_ratio(text) >= 0.55:
            score += 0.10
        score = clamp01(score)
        if score > best:
            best = score
            evidence = text
    return best, evidence


def recipients_score(page: dict[str, Any]) -> tuple[float, str]:
    best = 0.0
    evidence = ""
    for line in page_lines(page):
        text = line.get("text") or ""
        norm = norm_text(text)
        if not _RE_NOI_NHAN.search(norm):
            continue
        _, y0, _, _ = line_pos(line, page)
        score = 0.90 if y0 > 0.35 else 0.70
        if y0 > 0.55:
            score = 1.0
        if score > best:
            best = score
            evidence = text
    return best, evidence


def signer_role_score(page: dict[str, Any]) -> tuple[float, str]:
    best = 0.0
    evidence = ""
    for line in page_lines(page):
        text = line.get("text") or ""
        norm = norm_text(text)
        x0, y0, x1, _ = line_pos(line, page)
        if y0 < 0.30:
            continue
        has_prefix = bool(_RE_SIGNER_PREFIX.search(norm))
        has_role = any(kw in norm for kw in _SIGNER_ROLE_KEYWORDS)
        if not has_prefix and not has_role:
            continue
        score = (0.55 if has_prefix else 0.0) + (0.35 if has_role else 0.0)
        cx = (x0 + x1) / 2.0
        if y0 > 0.45:
            score += 0.08
        if cx > 0.40:
            score += 0.05
        score = clamp01(score)
        if score > best:
            best = score
            evidence = text
    return best, evidence


def signer_name_score(page: dict[str, Any]) -> tuple[float, str]:
    lines = page_lines(page)
    best = 0.0
    evidence = ""
    role_line_indices = set()
    for idx, line in enumerate(lines):
        norm = norm_text(line.get("text") or "")
        if _RE_SIGNER_PREFIX.search(norm) or any(kw in norm for kw in _SIGNER_ROLE_KEYWORDS):
            role_line_indices.add(idx)
    for idx, line in enumerate(lines):
        text = (line.get("text") or "").strip()
        norm = norm_text(text)
        x0, y0, x1, _ = line_pos(line, page)
        if y0 < 0.34 or _RE_DIGIT.search(norm):
            continue
        if _RE_NOI_NHAN.search(norm) or _RE_SIGNER_PREFIX.search(norm) or any(kw in norm for kw in _SIGNER_ROLE_KEYWORDS):
            continue
        tokens = token_list(text)
        norm_tokens = [norm_text(tok) for tok in tokens]
        if not (2 <= len(norm_tokens) <= 5):
            continue
        if any(len(tok) <= 1 for tok in norm_tokens):
            continue
        score = 0.0
        upper = uppercase_ratio(text)
        if upper >= 0.55:
            score += 0.40
        elif sum(1 for tok in tokens if tok[:1].isupper()) >= max(2, len(tokens) - 1):
            score += 0.28
        if norm_tokens[0] in _SURNAME_HINTS:
            score += 0.30
        if any(0 < idx - role_idx <= 5 for role_idx in role_line_indices):
            score += 0.25
        cx = (x0 + x1) / 2.0
        if y0 > 0.45:
            score += 0.05
        if cx > 0.35:
            score += 0.05
        score = clamp01(score)
        if score > best:
            best = score
            evidence = text
    return best, evidence


def build_doc_start_features(page: dict[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    funcs = {
        "regime_score": regime_score,
        "org_header_score": org_header_score,
        "doc_number_standalone_score": doc_number_standalone_score,
        "place_date_standalone_score": place_date_standalone_score,
        "subject_score": subject_score,
        "doc_number_date_alignment_score": doc_number_date_alignment_score,
        "header_completeness_score": header_completeness_score,
        "reference_line_penalty": reference_line_penalty,
    }
    features: dict[str, float] = {}
    evidence: dict[str, str] = {}
    for name, func in funcs.items():
        score, text = func(page)
        features[name] = round(float(score), 6)
        evidence[f"{name}_evidence"] = text
    return features, evidence


def build_signer_features(page: dict[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    rec_score, rec_evidence = recipients_score(page)
    role_score, role_evidence = signer_role_score(page)
    name_score, name_evidence = signer_name_score(page)
    page_norm = norm_text("\n".join(line.get("text") or "" for line in page_lines(page)))
    features = {
        "recipients_score": round(float(rec_score), 6),
        "signer_role_score": round(float(role_score), 6),
        "signer_name_score": round(float(name_score), 6),
        "has_noi_nhan_regex": 1.0 if _RE_NOI_NHAN.search(page_norm) else 0.0,
        "has_tm_kt_tl_tuq_regex": 1.0 if _RE_SIGNER_PREFIX.search(page_norm) else 0.0,
    }
    evidence = {
        "recipients_score_evidence": rec_evidence,
        "signer_role_score_evidence": role_evidence,
        "signer_name_score_evidence": name_evidence,
    }
    return features, evidence


def relative_page_position(page_ordinal: int, page_count: int) -> float:
    if page_count <= 1:
        return 0.0
    return clamp01(float(page_ordinal) / float(page_count - 1))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def summarize_rows(rows: list[dict[str, Any]], target_col: str) -> dict[str, Any]:
    by_split: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        by_split[str(row["split"])][int(row[target_col])] += 1
    return {
        split: {"negative": counts.get(0, 0), "positive": counts.get(1, 0), "total": sum(counts.values())}
        for split, counts in sorted(by_split.items())
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build page-level LightGBM splitter datasets from labeled KIE JSON.")
    parser.add_argument("--label-root", default=r"D:\tmp\Train_20260413_143844_kie\json_output_labeled")
    parser.add_argument("--ocr-root", default=r"D:\tmp\Train_20260413_143844_kie\ocr")
    parser.add_argument("--output-root", default=r"D:\tmp\Train_20260413_143844_LGBM_SPLITTER")
    parser.add_argument("--val-ratio", type=float, default=0.10)
    parser.add_argument("--test-ratio", type=float, default=0.10)
    parser.add_argument("--limit-docs", type=int, default=0)
    args = parser.parse_args()

    label_root = Path(args.label_root)
    ocr_root = Path(args.ocr_root)
    output_root = Path(args.output_root)
    dataset_root = output_root / "dataset"
    files = sorted(p for p in label_root.rglob("*.json") if not p.name.startswith("_sample"))
    if args.limit_docs:
        files = files[: args.limit_docs]

    doc_start_rows: list[dict[str, Any]] = []
    signer_rows: list[dict[str, Any]] = []
    manifest_docs: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    stats = Counter()

    for path in files:
        try:
            payload = read_json(path)
            instances = label_instances(payload)
            canonical_path, canonical = canonical_for_label(path, payload, ocr_root)
            if canonical_path is None or canonical is None:
                skipped.append({"file": str(path), "reason": "canonical OCR JSON not found"})
                continue
            pages = canonical.get("pages") or []
            if not pages or not instances:
                skipped.append({"file": str(path), "reason": "missing pages or field_instances"})
                continue
            doc_id = doc_id_from_path(path, payload)
            split = deterministic_split(doc_id, args.val_ratio, args.test_ratio)
            start_pages, signer_pages = gold_pages(instances)
            if not start_pages:
                start_pages = {int(pages[0].get("page_index", 0))}
            first_start_page = min(start_pages)
            first_signer_page = min(signer_pages) if signer_pages else None
            page_count = len(pages)
            batch = path.parent.name

            for idx, page in enumerate(pages):
                page_index = int(page.get("page_index", idx))
                common = {
                    "doc_id": doc_id,
                    "batch": batch,
                    "split": split,
                    "label_json": str(path),
                    "ocr_json": str(canonical_path),
                    "page_index": page_index,
                    "page_ordinal": idx,
                    "page_count": page_count,
                    "first_start_page": first_start_page,
                    "first_signer_page": "" if first_signer_page is None else first_signer_page,
                }
                start_features, start_evidence = build_doc_start_features(page)
                doc_start_rows.append(
                    {
                        **common,
                        "target_doc_start": 1 if page_index in start_pages else 0,
                        **start_features,
                        **start_evidence,
                    }
                )
                if first_signer_page is None or page_index <= first_signer_page:
                    signer_features, signer_evidence = build_signer_features(page)
                    signer_features["relative_page_position"] = round(relative_page_position(idx, page_count), 6)
                    signer_rows.append(
                        {
                            **common,
                            "target_signer_page": 1 if first_signer_page is not None and page_index == first_signer_page else 0,
                            "signer_scope": "no_gold_signer" if first_signer_page is None else "up_to_first_gold_signer",
                            **signer_features,
                            **signer_evidence,
                        }
                    )
            manifest_docs.append(
                {
                    "doc_id": doc_id,
                    "batch": batch,
                    "split": split,
                    "label_json": str(path),
                    "ocr_json": str(canonical_path),
                    "page_count": page_count,
                    "start_pages": sorted(start_pages),
                    "first_signer_page": first_signer_page,
                    "signer_pages_labeled": sorted(signer_pages),
                }
            )
            stats["docs"] += 1
            stats["pages"] += page_count
            stats[f"docs_{split}"] += 1
            stats[f"pages_{split}"] += page_count
            if first_signer_page is None:
                stats["docs_without_signer_label"] += 1
            else:
                stats["docs_with_signer_label"] += 1
                stats["signer_pages_ignored_after_first"] += sum(
                    1
                    for page in pages
                    if int(page.get("page_index", 0)) > first_signer_page
                )
        except Exception as exc:
            skipped.append({"file": str(path), "reason": repr(exc)})

    doc_start_fields = [
        "doc_id",
        "batch",
        "split",
        "label_json",
        "ocr_json",
        "page_index",
        "page_ordinal",
        "page_count",
        "first_start_page",
        "first_signer_page",
        "target_doc_start",
        *DOC_START_FEATURES,
        *[f"{name}_evidence" for name in DOC_START_FEATURES],
    ]
    signer_fields = [
        "doc_id",
        "batch",
        "split",
        "label_json",
        "ocr_json",
        "page_index",
        "page_ordinal",
        "page_count",
        "first_start_page",
        "first_signer_page",
        "target_signer_page",
        "signer_scope",
        *SIGNER_FEATURES,
        "recipients_score_evidence",
        "signer_role_score_evidence",
        "signer_name_score_evidence",
    ]
    write_csv(dataset_root / "doc_start_pages.csv", doc_start_rows, doc_start_fields)
    write_csv(dataset_root / "signer_pages.csv", signer_rows, signer_fields)
    write_json(dataset_root / "manifest.json", {"documents": manifest_docs})
    write_json(
        dataset_root / "feature_definitions.json",
        {
            "doc_start": {
                "target": "target_doc_start",
                "features": DOC_START_FEATURES,
                "positive": "page containing labeled REGIME_HEADER/ISSUE_ORG/DOC_NUMBER_SYMBOL/PLACE_DATE/DOC_SUBJECT, fallback first page",
                "negative": "other pages in the same labeled document",
            },
            "signer_page": {
                "target": "target_signer_page",
                "features": SIGNER_FEATURES,
                "positive": "first page containing labeled SIGNER_NAME or SIGNER_ROLE",
                "negative": "pages before that first labeled signer page",
                "outside_scope": "pages after first labeled signer page are not included",
            },
        },
    )
    summary = {
        "label_root": str(label_root),
        "ocr_root": str(ocr_root),
        "output_root": str(output_root),
        "stats": dict(stats),
        "doc_start_rows": len(doc_start_rows),
        "signer_rows": len(signer_rows),
        "doc_start_target_by_split": summarize_rows(doc_start_rows, "target_doc_start"),
        "signer_target_by_split": summarize_rows(signer_rows, "target_signer_page"),
        "skipped_count": len(skipped),
        "skipped_examples": skipped[:50],
    }
    write_json(output_root / "reports" / "dataset_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
