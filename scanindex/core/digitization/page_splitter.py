"""Page-level document-start detector for archive Step 1.

This is the runtime counterpart of the experimental
``train-convert/archive-page-splitter`` scripts. It scores each OCR page with the
doc-start LightGBM model and returns page indices that should become split
boundaries in a long scanned PDF.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import types
import unicodedata
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import Any


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

DEFAULT_THRESHOLD = 0.50
DEFAULT_SIGNER_THRESHOLD = 0.39

_MODEL_LOCK = threading.Lock()
_MODEL = None
_MODEL_PATH: str | None = None
_SIGNER_MODEL = None
_SIGNER_MODEL_PATH: str | None = None

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


def _repo_root() -> Path:
    try:
        from scanindex.infra.paths import get_base_dir

        return Path(get_base_dir())
    except Exception:
        return Path(__file__).resolve().parents[3]


def _candidate_model_paths() -> list[Path]:
    root = _repo_root()
    return [
        root / "models" / "lightgbm_splitter" / "doc_start" / "model.txt",
        root / "models" / "lightgbm_splitter" / "doc_start" / "model.joblib",
        Path(r"D:\tmp\Train_20260413_143844_LGBM_SPLITTER_DOCNUM_V2\models\doc_start\model.txt"),
        Path(r"D:\tmp\Train_20260413_143844_LGBM_SPLITTER_DOCNUM_V2\models\doc_start\model.joblib"),
    ]


def _candidate_signer_model_paths() -> list[Path]:
    root = _repo_root()
    return [
        root / "models" / "lightgbm_splitter" / "signer_page" / "model.txt",
        root / "models" / "lightgbm_splitter" / "signer_page" / "model.joblib",
        Path(r"D:\tmp\Train_20260413_143844_LGBM_SPLITTER_RELPOS\models\signer_page\model.txt"),
        Path(r"D:\tmp\Train_20260413_143844_LGBM_SPLITTER_RELPOS\models\signer_page\model.joblib"),
    ]


def model_path() -> str | None:
    for path in _candidate_model_paths():
        if path.exists():
            return str(path)
    return None


def signer_model_path() -> str | None:
    for path in _candidate_signer_model_paths():
        if path.exists():
            return str(path)
    return None


class _LightgbmPortableImports:
    """Let LightGBM Booster load in frozen builds without sklearn/scipy."""

    def __init__(self):
        self.active = bool(getattr(sys, "frozen", False))
        self._blocked_sklearn = False
        self._created_scipy = False
        self._created_scipy_sparse = False
        self._had_scipy_sparse_attr = False
        self._previous_scipy_sparse_attr = None

    def __enter__(self):
        if not self.active:
            return

        if "sklearn" not in sys.modules:
            sys.modules["sklearn"] = None
            self._blocked_sklearn = True

        if "scipy.sparse" not in sys.modules:
            scipy_mod = sys.modules.get("scipy")
            if scipy_mod is None:
                scipy_mod = types.ModuleType("scipy")
                scipy_mod.__path__ = []
                scipy_mod.__package__ = "scipy"
                scipy_mod.__spec__ = ModuleSpec("scipy", loader=None, is_package=True)
                sys.modules["scipy"] = scipy_mod
                self._created_scipy = True

            sparse_mod = types.ModuleType("scipy.sparse")
            sparse_mod.__package__ = "scipy"
            sparse_mod.__spec__ = ModuleSpec("scipy.sparse", loader=None)

            class spmatrix:
                pass

            class csr_matrix(spmatrix):
                def __init__(self, *args, **kwargs):
                    raise RuntimeError("scipy.sparse is not bundled in portable LightGBM inference")

            class csc_matrix(spmatrix):
                def __init__(self, *args, **kwargs):
                    raise RuntimeError("scipy.sparse is not bundled in portable LightGBM inference")

            def issparse(value):
                return isinstance(value, spmatrix)

            def hstack(*args, **kwargs):
                raise RuntimeError("scipy.sparse is not bundled in portable LightGBM inference")

            sparse_mod.spmatrix = spmatrix
            sparse_mod.csr_matrix = csr_matrix
            sparse_mod.csc_matrix = csc_matrix
            sparse_mod.issparse = issparse
            sparse_mod.hstack = hstack
            self._had_scipy_sparse_attr = hasattr(scipy_mod, "sparse")
            self._previous_scipy_sparse_attr = getattr(scipy_mod, "sparse", None)
            scipy_mod.sparse = sparse_mod
            sys.modules["scipy.sparse"] = sparse_mod
            self._created_scipy_sparse = True

    def __exit__(self, exc_type, exc, tb):
        if self._blocked_sklearn and sys.modules.get("sklearn") is None:
            sys.modules.pop("sklearn", None)
        if self._created_scipy_sparse:
            sys.modules.pop("scipy.sparse", None)
            if not self._created_scipy:
                scipy_mod = sys.modules.get("scipy")
                if scipy_mod is not None:
                    if self._had_scipy_sparse_attr:
                        scipy_mod.sparse = self._previous_scipy_sparse_attr
                    elif getattr(scipy_mod, "sparse", None) is not None:
                        try:
                            delattr(scipy_mod, "sparse")
                        except Exception:
                            pass
        if self._created_scipy:
            sys.modules.pop("scipy", None)


class _BoosterBinaryClassifier:
    def __init__(self, booster):
        self._booster = booster

    def predict_proba(self, data):
        import numpy as np

        if hasattr(data, "to_numpy"):
            arr = data.to_numpy(dtype="float64")
        else:
            arr = np.asarray(data, dtype="float64")
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)

        raw = np.asarray(self._booster.predict(arr), dtype="float64")
        if raw.ndim == 2:
            pos = raw[:, 1] if raw.shape[1] > 1 else raw[:, 0]
        else:
            pos = raw
        pos = np.clip(pos, 0.0, 1.0)
        return np.column_stack([1.0 - pos, pos])


def _load_model_from_path(path: str):
    if path.lower().endswith(".txt"):
        with _LightgbmPortableImports():
            import lightgbm as lgb

        return _BoosterBinaryClassifier(lgb.Booster(model_file=path))

    if getattr(sys, "frozen", False):
        raise RuntimeError(
            f"Portable build requires LightGBM booster text model, not joblib: {path}"
        )

    from joblib import load

    return load(path)


def load_model():
    global _MODEL, _MODEL_PATH
    with _MODEL_LOCK:
        path = model_path()
        if not path:
            return None
        if _MODEL is not None and _MODEL_PATH == path:
            return _MODEL

        _MODEL = _load_model_from_path(path)
        _MODEL_PATH = path
        return _MODEL


def load_signer_model():
    global _SIGNER_MODEL, _SIGNER_MODEL_PATH
    with _MODEL_LOCK:
        path = signer_model_path()
        if not path:
            return None
        if _SIGNER_MODEL is not None and _SIGNER_MODEL_PATH == path:
            return _SIGNER_MODEL

        _SIGNER_MODEL = _load_model_from_path(path)
        _SIGNER_MODEL_PATH = path
        return _SIGNER_MODEL


def strip_accents(text: str) -> str:
    text = (text or "").replace("\u0110", "D").replace("\u0111", "d")
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


def regime_score(page: dict[str, Any]) -> tuple[float, str]:
    top_lines = [line for line in page_lines(page) if line_pos(line, page)[1] < 0.34]
    has_regime = False
    has_motto = False
    right_bonus = 0.0
    evidence = []
    for line in top_lines:
        text = line.get("text") or ""
        norm = norm_text(text)
        x0, _, x1, _ = line_pos(line, page)
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
        keyword_hits = sum(1 for kw in _ORG_KEYWORDS if kw in norm)
        if keyword_hits == 0:
            continue
        cx = (x0 + x1) / 2.0
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
        candidates.append({
            "score": clamp01(score),
            "text": text,
            "norm": norm,
            "x0": x0,
            "y0": y0,
            "x1": x1,
            "width": width,
            "has_standalone_prefix": has_prefix,
            "has_doc_number": has_doc_number,
        })
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
        candidates.append({
            "score": clamp01(score),
            "text": text,
            "norm": norm,
            "x0": x0,
            "y0": y0,
            "x1": x1,
            "width": width,
        })
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
        if (
            _RE_NOI_NHAN.search(norm)
            or _RE_SIGNER_PREFIX.search(norm)
            or any(kw in norm for kw in _SIGNER_ROLE_KEYWORDS)
        ):
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


def relative_page_position(page_ordinal: int, page_count: int) -> float:
    if page_count <= 1:
        return 0.0
    return clamp01(float(page_ordinal) / float(page_count - 1))


def build_signer_features(
    page: dict[str, Any],
    page_ordinal: int,
    page_count: int,
) -> tuple[dict[str, float], dict[str, str]]:
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
        "relative_page_position": round(relative_page_position(page_ordinal, page_count), 6),
    }
    evidence = {
        "recipients_score_evidence": rec_evidence,
        "signer_role_score_evidence": role_evidence,
        "signer_name_score_evidence": name_evidence,
    }
    return features, evidence


def predict_doc_starts(canonical_json_path: str, threshold: float = DEFAULT_THRESHOLD) -> dict[str, Any]:
    model = load_model()
    if model is None:
        raise FileNotFoundError("doc_start LightGBM model not found")

    with open(canonical_json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    pages = payload.get("pages") or []
    rows = []
    feature_rows = []
    for page in pages:
        features, evidence = build_doc_start_features(page)
        page_index = int(page.get("page_index", len(rows)))
        rows.append({
            "page_index": page_index,
            "features": features,
            "evidence": evidence,
        })
        feature_rows.append([features[name] for name in DOC_START_FEATURES])

    if not rows:
        return {"threshold": threshold, "model_path": _MODEL_PATH, "pages": [], "start_pages": []}

    scores = model.predict_proba(feature_rows)[:, 1]

    start_pages = set()
    predictions = []
    for row, score in zip(rows, scores):
        page_index = int(row["page_index"])
        is_start = float(score) >= threshold
        if page_index == 0:
            is_start = True
        if is_start:
            start_pages.add(page_index)
        predictions.append({
            "page_index": page_index,
            "score": round(float(score), 6),
            "is_doc_start": bool(is_start),
            "features": row["features"],
            "evidence": row["evidence"],
        })

    return {
        "threshold": float(threshold),
        "model_path": _MODEL_PATH,
        "pages": predictions,
        "start_pages": sorted(start_pages),
    }


def predict_signer_page(
    canonical_json_path: str,
    threshold: float = DEFAULT_SIGNER_THRESHOLD,
) -> dict[str, Any]:
    model = load_signer_model()
    if model is None:
        raise FileNotFoundError("signer_page LightGBM model not found")

    with open(canonical_json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    pages = payload.get("pages") or []
    rows = []
    for ordinal, page in enumerate(pages):
        features, evidence = build_signer_features(page, ordinal, len(pages))
        page_index = int(page.get("page_index", ordinal))
        rows.append({
            "page_index": page_index,
            "features": features,
            "evidence": evidence,
        })

    if not rows:
        return {
            "threshold": threshold,
            "model_path": _SIGNER_MODEL_PATH,
            "pages": [],
            "signer_page": None,
            "selected_pages": [],
        }

    feature_rows = [[row["features"][name] for name in SIGNER_FEATURES] for row in rows]
    scores = model.predict_proba(feature_rows)[:, 1]

    best_idx = max(range(len(rows)), key=lambda i: float(scores[i]))
    signer_page = int(rows[best_idx]["page_index"])
    predictions = []
    for row, score in zip(rows, scores):
        page_index = int(row["page_index"])
        predictions.append({
            "page_index": page_index,
            "score": round(float(score), 6),
            "is_signer_page": page_index == signer_page,
            "passes_threshold": bool(float(score) >= threshold),
            "features": row["features"],
            "evidence": row["evidence"],
        })

    selected_pages = sorted({0, signer_page})
    return {
        "threshold": float(threshold),
        "model_path": _SIGNER_MODEL_PATH,
        "pages": predictions,
        "signer_page": signer_page,
        "signer_score": round(float(scores[best_idx]), 6),
        "selected_pages": selected_pages,
    }
