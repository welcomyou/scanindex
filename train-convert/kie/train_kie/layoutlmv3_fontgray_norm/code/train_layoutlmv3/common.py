from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


FIELDS: list[str] = [
    "REGIME_HEADER",
    "ISSUE_ORG_SUPERIOR",
    "ISSUE_ORG_NAME",
    "DOC_NUMBER_SYMBOL",
    "PLACE_DATE",
    "DOC_SUBJECT",
    "ADDRESSEE",
    "RECIPIENTS",
    "SIGNER_ROLE",
    "SIGNER_NAME",
]

SINGLE_FIELDS: set[str] = {
    "REGIME_HEADER",
    "ISSUE_ORG_SUPERIOR",
    "ISSUE_ORG_NAME",
    "DOC_NUMBER_SYMBOL",
    "PLACE_DATE",
    "DOC_SUBJECT",
    "ADDRESSEE",
    "RECIPIENTS",
}
MULTI_FIELDS: set[str] = {"SIGNER_ROLE", "SIGNER_NAME"}

LABEL_LIST: list[str] = ["O"] + [f"{prefix}-{field}" for field in FIELDS for prefix in ("B", "I")]
PROJECT_SCHEMA_VERSION = "layoutlmv3_kie_project_v1"
FONT_STYLE_SPECIAL_TOKENS: list[str] = [
    "<fs_missing>",
    "<fs_xs>",
    "<fs_s>",
    "<fs_m>",
    "<fs_l>",
    "<fs_xl>",
    "<fsr_missing>",
    "<fsr_smaller>",
    "<fsr_normal>",
    "<fsr_larger>",
    "<fg_missing>",
    "<fg_black>",
    "<fg_dark>",
    "<fg_mid>",
    "<fg_light>",
    "<wh_xs>",
    "<wh_s>",
    "<wh_m>",
    "<wh_l>",
    "<wh_xl>",
    "<style_plain>",
    "<style_emphasis>",
]
_PAGE_SPLITTER_MODULE: Any | None = None


@dataclass(frozen=True)
class SourceContext:
    source_root: Path
    source_project_root: Path
    json_input_root: Path
    manifest_path: Path | None
    manifest_by_doc_id: dict[str, dict[str, Any]]
    manifest_by_input_rel: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class DocMeta:
    doc_id: str
    split: str
    label_output_json: Path
    label_input_json: Path | None
    source_canonical_json: Path
    relative_pdf_path: str
    selected_pages: tuple[int, ...]
    manifest_doc: dict[str, Any] | None
    label_rel: str


@dataclass(frozen=True)
class OCRWord:
    id: str
    page_index: int
    line_id: str
    order: int
    text: str
    bbox: tuple[float, float, float, float]
    has_space_after: bool
    font_size: float
    fg_gray: float
    confidence: float
    content_type: int


@dataclass(frozen=True)
class OCRLine:
    id: str
    page_index: int
    order: int
    text: str
    bbox: tuple[float, float, float, float]
    word_ids: tuple[str, ...]
    font_size: float
    fg_gray: float
    confidence: float
    content_type: int


@dataclass(frozen=True)
class OCRPage:
    page_index: int
    page_id: str
    width: float
    height: float
    words: tuple[OCRWord, ...]
    lines: tuple[OCRLine, ...]


@dataclass(frozen=True)
class OCRDocument:
    pages: dict[int, OCRPage]
    word_lookup: dict[str, OCRWord]
    line_lookup: dict[str, OCRLine]
    raw: dict[str, Any]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_archive_page_splitter():
    global _PAGE_SPLITTER_MODULE
    if _PAGE_SPLITTER_MODULE is not None:
        return _PAGE_SPLITTER_MODULE

    errors: list[BaseException] = []
    try:
        from scanindex.core.digitization import page_splitter as archive_page_splitter

        _PAGE_SPLITTER_MODULE = archive_page_splitter
        return archive_page_splitter
    except Exception as exc:
        errors.append(exc)

    current = Path(__file__).resolve()
    for root in current.parents:
        if not (root / "scanindex" / "core" / "digitization" / "page_splitter.py").exists():
            continue
        root_text = str(root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        try:
            from scanindex.core.digitization import page_splitter as archive_page_splitter

            _PAGE_SPLITTER_MODULE = archive_page_splitter
            return archive_page_splitter
        except Exception as exc:
            errors.append(exc)

    detail = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors[-3:])
    raise RuntimeError(
        "Cannot import scanindex.core.digitization.page_splitter for the LightGBM signer-page guard. "
        f"Import errors: {detail}"
    ) from errors[-1]


def assert_page1_lightgbm_guard_available() -> None:
    page_splitter = _load_archive_page_splitter()
    signer_model_path = getattr(page_splitter, "signer_model_path", None)
    model_path = signer_model_path() if callable(signer_model_path) else None
    if not model_path:
        raise FileNotFoundError("LightGBM signer_page model not found under models/lightgbm_splitter/signer_page.")
    repo_root = Path(page_splitter._repo_root()).resolve() if hasattr(page_splitter, "_repo_root") else Path.cwd().resolve()
    expected_dir = (repo_root / "models" / "lightgbm_splitter" / "signer_page").resolve()
    actual_path = Path(model_path).resolve()
    if actual_path.parent != expected_dir:
        raise FileNotFoundError(
            "Refusing LightGBM signer_page fallback model. "
            f"Expected model under {expected_dir}, got {actual_path}."
        )
    load_signer_model = getattr(page_splitter, "load_signer_model", None)
    if not callable(load_signer_model):
        raise RuntimeError("LightGBM signer-page selector has no load_signer_model() function.")
    if load_signer_model() is None:
        raise FileNotFoundError(f"LightGBM signer_page model could not be loaded: {model_path}")


def _page1_lightgbm_signer_decision(canonical_json: str | Path) -> dict[str, Any]:
    assert_page1_lightgbm_guard_available()
    page_splitter = _load_archive_page_splitter()
    result = page_splitter.predict_signer_page(str(canonical_json))
    page_row: dict[str, Any] | None = None
    for row in result.get("pages") or []:
        try:
            row_page_index = int(row.get("page_index"))
        except Exception:
            continue
        if row_page_index == 1:
            page_row = row
            break
    if page_row is None:
        raise RuntimeError(f"LightGBM signer-page selector did not return page_index=1 for {canonical_json}")

    signer_page_raw = result.get("signer_page")
    signer_page = int(signer_page_raw) if signer_page_raw is not None else None
    is_top_signer = signer_page == 1
    return {
        "signer_page": signer_page,
        "signer_score": result.get("signer_score"),
        "page1_score": page_row.get("score"),
        "page1_passes_threshold": bool(page_row.get("passes_threshold")),
        "page1_is_top_signer": bool(page_row.get("is_signer_page")) or is_top_signer,
        "threshold": result.get("threshold"),
        "model_path": result.get("model_path"),
    }


def write_json(path: str | Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
            count += 1
    return count


def append_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
            count += 1
    return count


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    path = Path(path)
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def project_dirs(project_root: str | Path) -> dict[str, Path]:
    root = Path(project_root)
    return {
        "root": root,
        "dataset": root / "exports" / "dataset",
        "models": root / "models",
        "reports": root / "reports",
        "logs": root / "logs",
        "onnx": root / "onnx",
    }


def ensure_project_dirs(project_root: str | Path) -> dict[str, Path]:
    dirs = project_dirs(project_root)
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def stable_split(doc_id: str) -> str:
    digest = hashlib.md5(doc_id.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 1000
    if bucket < 800:
        return "train"
    if bucket < 900:
        return "val"
    return "test"


def load_source_context(source_root: str | Path) -> SourceContext:
    source_root = Path(source_root).resolve()
    source_project_root = source_root.parent
    json_input_root = source_project_root / "json_input"
    manifest_path = source_project_root / "manifest.json"
    manifest_by_doc_id: dict[str, dict[str, Any]] = {}
    manifest_by_input_rel: dict[str, dict[str, Any]] = {}
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        for doc in manifest.get("documents", []):
            doc_id = str(doc.get("doc_id") or "")
            if doc_id:
                manifest_by_doc_id[doc_id] = doc
            label_input = doc.get("artifacts", {}).get("label_input_json")
            if label_input:
                try:
                    rel = str(Path(label_input).resolve().relative_to(json_input_root.resolve()))
                    manifest_by_input_rel[rel.replace("\\", "/")] = doc
                except ValueError:
                    pass
    return SourceContext(
        source_root=source_root,
        source_project_root=source_project_root,
        json_input_root=json_input_root,
        manifest_path=manifest_path if manifest_path.exists() else None,
        manifest_by_doc_id=manifest_by_doc_id,
        manifest_by_input_rel=manifest_by_input_rel,
    )


def _doc_id_from_label_name(path: Path) -> str:
    stem = path.stem
    if "__" in stem:
        return stem.rsplit("__", 1)[-1]
    return stem


def resolve_doc_meta(label_path: Path, context: SourceContext) -> DocMeta | None:
    label_path = label_path.resolve()
    rel_path = label_path.relative_to(context.source_root)
    rel_key = str(rel_path).replace("\\", "/")
    label_input_json = context.json_input_root / rel_path
    input_payload: dict[str, Any] = {}
    if label_input_json.exists():
        input_payload = read_json(label_input_json)
    else:
        label_input_json = None

    doc_id = str(input_payload.get("doc_id") or _doc_id_from_label_name(label_path))
    manifest_doc = context.manifest_by_doc_id.get(doc_id) or context.manifest_by_input_rel.get(rel_key)

    source_canonical = input_payload.get("source_canonical_json")
    if not source_canonical and manifest_doc:
        artifacts = manifest_doc.get("artifacts", {})
        source_canonical = artifacts.get("canonical_json") or artifacts.get("corrected_canonical_json")
    if not source_canonical:
        return None

    split = input_payload.get("split")
    if not split and manifest_doc:
        split = manifest_doc.get("split")
    split = str(split or stable_split(doc_id)).lower()
    if split not in {"train", "val", "test"}:
        split = stable_split(doc_id)

    selected = input_payload.get("selected_pages")
    if selected is None and input_payload.get("page_selection"):
        selected = input_payload["page_selection"].get("selected_pages")
    if selected is None and manifest_doc:
        selected = manifest_doc.get("selected_pages")
    selected_pages = tuple(sorted({int(p) for p in (selected or []) if _is_int_like(p)}))

    relative_pdf_path = str(input_payload.get("relative_pdf_path") or "")
    if not relative_pdf_path and manifest_doc:
        relative_pdf_path = str(manifest_doc.get("relative_pdf_path") or "")

    return DocMeta(
        doc_id=doc_id,
        split=split,
        label_output_json=label_path,
        label_input_json=label_input_json,
        source_canonical_json=Path(source_canonical),
        relative_pdf_path=relative_pdf_path,
        selected_pages=selected_pages,
        manifest_doc=manifest_doc,
        label_rel=rel_key,
    )


def iter_label_files(source_root: str | Path, limit_docs: int | None = None) -> list[Path]:
    source_root = Path(source_root)
    files = sorted(
        p
        for p in source_root.rglob("*.json")
        if p.is_file() and p.name.lower() not in {"readme.json", "manifest.json"}
    )
    if limit_docs is not None:
        files = files[: max(0, limit_docs)]
    return files


def _is_int_like(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _to_bbox(raw: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return (x0, y0, x1, y1)


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_float(raw: dict[str, Any], keys: tuple[str, ...], default: float) -> float:
    for key in keys:
        if key in raw and raw.get(key) is not None:
            return _float_value(raw.get(key), default)
    return default


def bucket_float(value: float, low: float, high: float, bins: int = 16) -> int:
    if not math.isfinite(value):
        value = low
    if high <= low:
        return 0
    value = min(high, max(low, value))
    scaled = (value - low) / (high - low)
    return min(bins - 1, max(0, int(round(scaled * (bins - 1)))))


def _median_float(values: Iterable[float]) -> float:
    clean = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not clean:
        return 0.0
    mid = len(clean) // 2
    if len(clean) % 2:
        return float(clean[mid])
    return float((clean[mid - 1] + clean[mid]) / 2.0)


def style_emphasis_ids(font_size: list[float], fg_gray: list[float], word_height: list[float]) -> list[int]:
    """Page-relative visual prominence flag from OCR metadata, not pixels."""
    median_font = _median_float(v for v in font_size if float(v) > 0)
    median_gray = _median_float(v for v in fg_gray if 0 <= float(v) <= 255)
    median_height = _median_float(v for v in word_height if float(v) > 0)
    dark_threshold = max(18.0, median_gray - 14.0)
    out: list[int] = []
    for fs, gray, height in zip(font_size, fg_gray, word_height):
        fs = float(fs)
        gray = float(gray)
        height = float(height)
        is_large = fs >= median_font + 1.35 or height >= median_height + 2.25
        is_dark_prominent = gray <= dark_threshold and (fs >= median_font - 0.25 or height >= median_height - 0.25)
        out.append(1 if is_large or is_dark_prominent else 0)
    return out


def _font_size_token(value: float) -> str:
    if value <= 0:
        return "<fs_missing>"
    if value < 8.0:
        return "<fs_xs>"
    if value < 10.0:
        return "<fs_s>"
    if value < 12.5:
        return "<fs_m>"
    if value < 15.5:
        return "<fs_l>"
    return "<fs_xl>"


def _font_size_relative_token(value: float, median_font: float) -> str:
    if value <= 0 or median_font <= 0:
        return "<fsr_missing>"
    ratio = value / max(median_font, 1e-6)
    if ratio < 0.92:
        return "<fsr_smaller>"
    if ratio > 1.08:
        return "<fsr_larger>"
    return "<fsr_normal>"


def _fg_gray_token(value: float) -> str:
    if value < 0:
        return "<fg_missing>"
    if value <= 45:
        return "<fg_black>"
    if value <= 90:
        return "<fg_dark>"
    if value <= 150:
        return "<fg_mid>"
    return "<fg_light>"


def _word_height_token(value: float) -> str:
    if value <= 0:
        return "<wh_xs>"
    if value < 8.0:
        return "<wh_xs>"
    if value < 11.0:
        return "<wh_s>"
    if value < 15.0:
        return "<wh_m>"
    if value < 20.0:
        return "<wh_l>"
    return "<wh_xl>"


def add_style_features_to_row(row: dict[str, Any]) -> dict[str, Any]:
    tokens = list(row.get("tokens") or [])
    raw_bboxes = row.get("raw_bboxes") or []
    font_size = [float(v) for v in row.get("font_size", [])]
    fg_gray = [float(v) for v in row.get("fg_gray", [])]
    word_height = [float(v) for v in row.get("word_height", [])]
    if len(font_size) != len(tokens):
        font_size = [0.0] * len(tokens)
    if len(fg_gray) != len(tokens):
        fg_gray = [-1.0] * len(tokens)
    if len(word_height) != len(tokens):
        word_height = [
            float(box[3]) - float(box[1]) if isinstance(box, list) and len(box) >= 4 else 0.0
            for box in raw_bboxes
        ]
        if len(word_height) != len(tokens):
            word_height = [0.0] * len(tokens)
    median_font = _median_float(v for v in font_size if v > 0)
    style_ids = style_emphasis_ids(font_size, fg_gray, word_height)
    style_tokens: list[list[str]] = []
    tokens_with_style: list[str] = []
    font_size_bucket: list[int] = []
    fg_gray_bucket: list[int] = []
    word_height_bucket: list[int] = []
    for token, fs, gray, height, style_id in zip(tokens, font_size, fg_gray, word_height, style_ids):
        markers = [
            _font_size_token(fs),
            _font_size_relative_token(fs, median_font),
            _fg_gray_token(gray),
            _word_height_token(height),
            "<style_emphasis>" if style_id else "<style_plain>",
        ]
        style_tokens.append(markers)
        tokens_with_style.append(f"{token}{''.join(markers)}")
        font_size_bucket.append(bucket_float(fs, 6.0, 22.0, bins=16))
        fg_gray_bucket.append(bucket_float(gray, 0.0, 255.0, bins=16))
        word_height_bucket.append(bucket_float(height, 5.0, 28.0, bins=16))
    out = dict(row)
    out.update(
        {
            "font_size": [round(v, 3) for v in font_size],
            "fg_gray": [round(v, 3) for v in fg_gray],
            "word_height": [round(v, 3) for v in word_height],
            "font_size_bucket": font_size_bucket,
            "fg_gray_bucket": fg_gray_bucket,
            "word_height_bucket": word_height_bucket,
            "style_token_type_id": style_ids,
            "style_tokens": style_tokens,
            "tokens_with_style": tokens_with_style,
        }
    )
    return out


def model_tokens_for_row(row: dict[str, Any], word_feature_mode: str = "none") -> list[str]:
    if word_feature_mode == "style_tokens":
        tokens = row.get("tokens_with_style")
        if isinstance(tokens, list) and len(tokens) == len(row.get("tokens", [])):
            return [str(token) for token in tokens]
        return [str(token) for token in add_style_features_to_row(row).get("tokens_with_style", [])]
    return [str(token) for token in row.get("tokens", [])]


def is_valid_bbox(box: tuple[float, float, float, float] | None) -> bool:
    if not box:
        return False
    x0, y0, x1, y1 = box
    return x1 > x0 and y1 > y0 and all(math.isfinite(v) for v in box)


def bbox_union(boxes: Iterable[tuple[float, float, float, float]]) -> list[float]:
    boxes = [box for box in boxes if is_valid_bbox(box)]
    if not boxes:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def bbox_area(box: tuple[float, float, float, float] | list[float]) -> float:
    return max(0.0, float(box[2]) - float(box[0])) * max(0.0, float(box[3]) - float(box[1]))


def bbox_intersection(
    a: tuple[float, float, float, float] | list[float],
    b: tuple[float, float, float, float] | list[float],
) -> float:
    x0 = max(float(a[0]), float(b[0]))
    y0 = max(float(a[1]), float(b[1]))
    x1 = min(float(a[2]), float(b[2]))
    y1 = min(float(a[3]), float(b[3]))
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def bbox_iou(
    a: tuple[float, float, float, float] | list[float],
    b: tuple[float, float, float, float] | list[float],
) -> float:
    inter = bbox_intersection(a, b)
    denom = bbox_area(a) + bbox_area(b) - inter
    return inter / denom if denom > 0 else 0.0


def word_coverage_in_box(word_box: tuple[float, float, float, float], field_box: tuple[float, float, float, float]) -> float:
    area = bbox_area(word_box)
    if area <= 0:
        return 0.0
    return bbox_intersection(word_box, field_box) / area


def normalize_bbox(box: tuple[float, float, float, float], width: float, height: float) -> list[int]:
    width = max(float(width), 1.0)
    height = max(float(height), 1.0)
    x0, y0, x1, y1 = box
    values = [
        int(round(1000.0 * x0 / width)),
        int(round(1000.0 * y0 / height)),
        int(round(1000.0 * x1 / width)),
        int(round(1000.0 * y1 / height)),
    ]
    values = [min(1000, max(0, v)) for v in values]
    if values[2] < values[0]:
        values[0], values[2] = values[2], values[0]
    if values[3] < values[1]:
        values[1], values[3] = values[3], values[1]
    return values


def denormalize_bbox(box: list[int], width: float, height: float) -> list[float]:
    return [
        float(box[0]) * width / 1000.0,
        float(box[1]) * height / 1000.0,
        float(box[2]) * width / 1000.0,
        float(box[3]) * height / 1000.0,
    ]


def load_ocr_document(canonical_json: str | Path) -> OCRDocument:
    raw = read_json(canonical_json)
    pages: dict[int, OCRPage] = {}
    word_lookup: dict[str, OCRWord] = {}
    line_lookup: dict[str, OCRLine] = {}
    for raw_page in raw.get("pages", []):
        page_index = int(raw_page.get("page_index", len(pages)))
        width = float(raw_page.get("width") or raw_page.get("page_width") or raw_page.get("render_width") or 1.0)
        height = float(raw_page.get("height") or raw_page.get("page_height") or raw_page.get("render_height") or 1.0)
        lines: list[OCRLine] = []
        for raw_line in raw_page.get("lines", []):
            box = _to_bbox(raw_line.get("bbox"))
            if not box:
                box = (
                    float(raw_line.get("x", 0.0)),
                    float(raw_line.get("y", 0.0)),
                    float(raw_line.get("x", 0.0)) + float(raw_line.get("w", 0.0)),
                    float(raw_line.get("y", 0.0)) + float(raw_line.get("h", 0.0)),
                )
            line = OCRLine(
                id=str(raw_line.get("id") or f"p{page_index}_l{len(lines)}"),
                page_index=page_index,
                order=int(raw_line.get("order", len(lines))),
                text=str(raw_line.get("text") or raw_line.get("ocr_text") or ""),
                bbox=box,
                word_ids=tuple(str(w) for w in (raw_line.get("word_ids") or [])),
                font_size=_first_float(raw_line, ("font_size", "fontSize"), max(0.0, box[3] - box[1])),
                fg_gray=_first_float(raw_line, ("fg_gray", "fgGray", "gray"), 128.0),
                confidence=_first_float(raw_line, ("confidence", "conf"), 0.0),
                content_type=_int_value(raw_line.get("content_type"), 0),
            )
            lines.append(line)
            line_lookup[line.id] = line

        words: list[OCRWord] = []
        for raw_word in raw_page.get("words", []):
            box = _to_bbox(raw_word.get("bbox"))
            if not is_valid_bbox(box):
                continue
            line_id = str(raw_word.get("line_id") or raw_word.get("lineId") or "")
            line = line_lookup.get(line_id)
            word_height = max(0.0, float(box[3] - box[1]))
            word = OCRWord(
                id=str(raw_word.get("id") or f"p{page_index}_w{len(words)}"),
                page_index=page_index,
                line_id=line_id,
                order=int(raw_word.get("order", len(words))),
                text=str(raw_word.get("text") or raw_word.get("ocr_text") or ""),
                bbox=box,
                has_space_after=bool(raw_word.get("has_space_after", True)),
                font_size=_first_float(
                    raw_word,
                    ("font_size", "fontSize"),
                    float(line.font_size) if line else word_height,
                ),
                fg_gray=_first_float(
                    raw_word,
                    ("fg_gray", "fgGray", "gray"),
                    float(line.fg_gray) if line else 128.0,
                ),
                confidence=_first_float(
                    raw_word,
                    ("confidence", "conf"),
                    float(line.confidence) if line else 0.0,
                ),
                content_type=_int_value(
                    raw_word.get("content_type"),
                    int(line.content_type) if line else 0,
                ),
            )
            words.append(word)
            word_lookup[word.id] = word

        if not words and raw_page.get("kie_tokens"):
            for raw_token in raw_page.get("kie_tokens", []):
                box = _to_bbox(raw_token.get("bbox"))
                if not is_valid_bbox(box):
                    continue
                source_ids = raw_token.get("source_word_ids") or []
                token_id = str(source_ids[0] if source_ids else raw_token.get("id") or f"p{page_index}_t{len(words)}")
                word = OCRWord(
                    id=token_id,
                    page_index=page_index,
                    line_id=str(raw_token.get("line_id") or ""),
                    order=int(raw_token.get("order", len(words))),
                    text=str(raw_token.get("text") or raw_token.get("ocr_text") or ""),
                    bbox=box,
                    has_space_after=True,
                    font_size=_first_float(raw_token, ("font_size", "fontSize"), max(0.0, box[3] - box[1])),
                    fg_gray=_first_float(raw_token, ("fg_gray", "fgGray", "gray"), 128.0),
                    confidence=_first_float(raw_token, ("confidence", "conf"), 0.0),
                    content_type=_int_value(raw_token.get("content_type"), 0),
                )
                words.append(word)
                word_lookup[word.id] = word

        line_rank = {line.id: (line.order, idx) for idx, line in enumerate(lines)}
        word_rank: dict[str, int] = {}
        for line in lines:
            for idx, word_id in enumerate(line.word_ids):
                word_rank[word_id] = idx

        def word_sort_key(word: OCRWord) -> tuple[float, float, int, int, float, float, str]:
            if word.line_id in line_rank:
                line_order, line_idx = line_rank[word.line_id]
                return (0.0, float(line_order), line_idx, word_rank.get(word.id, word.order), word.bbox[1], word.bbox[0], word.id)
            return (1.0, word.bbox[1], 0, word.order, word.bbox[0], word.bbox[1], word.id)

        words.sort(key=word_sort_key)
        pages[page_index] = OCRPage(
            page_index=page_index,
            page_id=str(raw_page.get("id") or f"p{page_index}"),
            width=width,
            height=height,
            words=tuple(words),
            lines=tuple(sorted(lines, key=lambda l: (l.order, l.bbox[1], l.bbox[0], l.id))),
        )
    return OCRDocument(pages=pages, word_lookup=word_lookup, line_lookup=line_lookup, raw=raw)


def label_field(label: str) -> str:
    if label == "O" or not label:
        return "O"
    if "-" in label:
        return label.split("-", 1)[1]
    return label


def label_prefix(label: str) -> str:
    if label == "O" or "-" not in label:
        return "O"
    return label.split("-", 1)[0]


def get_annotation_payload(label_payload: dict[str, Any]) -> dict[str, Any]:
    return label_payload.get("annotation") or label_payload


def _field_bbox(raw_field: dict[str, Any]) -> tuple[float, float, float, float] | None:
    for key in ("bbox", "box", "bounding_box"):
        box = _to_bbox(raw_field.get(key))
        if is_valid_bbox(box):
            return box
    return None


def resolve_field_words(
    raw_field: dict[str, Any],
    ocr_doc: OCRDocument,
    stats: Counter,
) -> dict[str, Any] | None:
    field_label = str(raw_field.get("label") or "")
    if field_label not in FIELDS:
        stats["unknown_label_instances"] += 1
        return None
    raw_word_ids = [str(w) for w in (raw_field.get("word_ids") or []) if str(w)]
    raw_line_ids = [str(l) for l in (raw_field.get("line_ids") or []) if str(l)]
    page_index = int(raw_field.get("page_index", -1)) if _is_int_like(raw_field.get("page_index", None)) else -1

    resolved: list[str] = []
    source = "word_ids"
    expected = len(raw_word_ids)
    if raw_word_ids:
        for word_id in raw_word_ids:
            if word_id in ocr_doc.word_lookup and word_id not in resolved:
                resolved.append(word_id)
            else:
                stats["missing_labeled_word_ids"] += 1
    else:
        box = _field_bbox(raw_field)
        if box and page_index in ocr_doc.pages:
            source = "bbox_overlap"
            page = ocr_doc.pages[page_index]
            if max(box) > max(page.width, page.height) * 1.2:
                box = tuple(denormalize_bbox([int(v) for v in box], page.width, page.height))  # type: ignore[arg-type]
            for word in page.words:
                if word_coverage_in_box(word.bbox, box) >= 0.3 or bbox_iou(word.bbox, box) >= 0.1:
                    resolved.append(word.id)
            expected = len(resolved)
        elif raw_line_ids:
            source = "line_ids_fallback"
            stats["line_id_fallback_instances"] += 1
            for line_id in raw_line_ids:
                line = ocr_doc.line_lookup.get(line_id)
                if line:
                    for word_id in line.word_ids:
                        if word_id in ocr_doc.word_lookup and word_id not in resolved:
                            resolved.append(word_id)
            expected = len(resolved)

    if not resolved:
        stats["dropped_field_instances"] += 1
        return None

    if page_index < 0:
        page_index = ocr_doc.word_lookup[resolved[0]].page_index
    present = len(resolved)
    coverage = present / max(1, expected)
    if coverage < 1.0:
        stats["partial_field_instances"] += 1
    boxes = [ocr_doc.word_lookup[w].bbox for w in resolved if w in ocr_doc.word_lookup]
    return {
        "field_id": str(raw_field.get("field_id") or ""),
        "label": field_label,
        "page_index": page_index,
        "word_ids": resolved,
        "text": str(raw_field.get("text") or raw_field.get("normalized_value") or ""),
        "bbox": bbox_union(boxes),
        "coverage": coverage,
        "confidence": raw_field.get("confidence"),
        "source": source,
        "raw_word_count": len(raw_word_ids),
        "raw_line_count": len(raw_line_ids),
    }


def _assignment_score(field: dict[str, Any]) -> tuple[float, float, int]:
    conf = field.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else 0.0
    except (TypeError, ValueError):
        conf_f = 0.0
    return (float(field.get("coverage") or 0.0), conf_f, -len(field.get("word_ids") or []))


def make_page_row(
    meta: DocMeta,
    page: OCRPage,
    fields: list[dict[str, Any]],
    stats: Counter,
    conflict_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    tokens: list[str] = []
    bboxes: list[list[int]] = []
    raw_bboxes: list[list[float]] = []
    word_ids: list[str] = []
    line_ids: list[str] = []
    font_size: list[float] = []
    fg_gray: list[float] = []
    word_height: list[float] = []
    confidence: list[float] = []
    content_type: list[int] = []
    for word in page.words:
        if not word.text.strip():
            stats["dropped_blank_words"] += 1
            continue
        if not is_valid_bbox(word.bbox):
            stats["dropped_invalid_bbox_words"] += 1
            continue
        tokens.append(word.text)
        bboxes.append(normalize_bbox(word.bbox, page.width, page.height))
        raw_bboxes.append([float(v) for v in word.bbox])
        word_ids.append(word.id)
        line_ids.append(word.line_id)
        font_size.append(round(float(word.font_size), 3))
        fg_gray.append(round(float(word.fg_gray), 3))
        word_height.append(round(float(word.bbox[3] - word.bbox[1]), 3))
        confidence.append(round(float(word.confidence), 5))
        content_type.append(int(word.content_type))

    if not tokens:
        stats["dropped_empty_pages"] += 1
        return None

    fields_for_page = [field for field in fields if any(w in set(word_ids) for w in field.get("word_ids", []))]
    word_to_fields: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for field in fields_for_page:
        for word_id in field.get("word_ids", []):
            if word_id in word_ids:
                word_to_fields[word_id].append(field)

    owner: dict[str, dict[str, Any]] = {}
    for word_id, candidates in word_to_fields.items():
        if len(candidates) == 1:
            owner[word_id] = candidates[0]
            continue
        candidates = sorted(candidates, key=_assignment_score, reverse=True)
        owner[word_id] = candidates[0]
        stats["conflict_words"] += 1
        if len(conflict_rows) < 10000:
            conflict_rows.append(
                {
                    "doc_id": meta.doc_id,
                    "page_index": page.page_index,
                    "word_id": word_id,
                    "chosen": candidates[0]["label"],
                    "candidates": [
                        {
                            "field_id": c.get("field_id"),
                            "label": c.get("label"),
                            "coverage": c.get("coverage"),
                            "confidence": c.get("confidence"),
                        }
                        for c in candidates
                    ],
                }
            )

    labels = ["O"] * len(tokens)
    word_index = {word_id: i for i, word_id in enumerate(word_ids)}
    for field in fields_for_page:
        owned = [wid for wid in field.get("word_ids", []) if owner.get(wid) is field and wid in word_index]
        if not owned:
            continue
        owned.sort(key=lambda wid: word_index[wid])
        previous_index: int | None = None
        for word_id in owned:
            index = word_index[word_id]
            prefix = "I" if previous_index is not None and index == previous_index + 1 else "B"
            labels[index] = f"{prefix}-{field['label']}"
            previous_index = index

    row = {
        "doc_id": meta.doc_id,
        "page_id": page.page_id,
        "source_file": str(meta.source_canonical_json),
        "label_file": str(meta.label_output_json),
        "label_rel": meta.label_rel,
        "relative_pdf_path": meta.relative_pdf_path,
        "split": meta.split,
        "page_index": page.page_index,
        "tokens": tokens,
        "bboxes": bboxes,
        "raw_bboxes": raw_bboxes,
        "labels": labels,
        "word_ids": word_ids,
        "line_ids": line_ids,
        "font_size": font_size,
        "fg_gray": fg_gray,
        "word_height": word_height,
        "confidence": confidence,
        "content_type": content_type,
        "page_width": page.width,
        "page_height": page.height,
    }
    return add_style_features_to_row(row)


def build_rows_for_doc(
    meta: DocMeta,
    label_payload: dict[str, Any],
    ocr_doc: OCRDocument,
    stats: Counter,
    conflict_rows: list[dict[str, Any]],
    include_selected_negative_pages: bool = False,
    include_page1_clean_negative: bool = True,
    require_page1_not_lightgbm_signer: bool = True,
) -> list[dict[str, Any]]:
    annotation = get_annotation_payload(label_payload)
    fields: list[dict[str, Any]] = []
    for raw_field in annotation.get("field_instances", []):
        resolved = resolve_field_words(raw_field, ocr_doc, stats)
        if resolved:
            fields.append(resolved)
    label_pages = {int(field["page_index"]) for field in fields if _is_int_like(field.get("page_index"))}
    selected_pages = set(label_pages)

    page1_clean_negative_pages: set[int] = set()
    signer_pages = {
        int(field["page_index"])
        for field in fields
        if field.get("label") in {"SIGNER_ROLE", "SIGNER_NAME"} and _is_int_like(field.get("page_index"))
    }
    if include_page1_clean_negative and 0 in label_pages and 1 not in label_pages and any(page >= 2 for page in signer_pages):
        if 1 in ocr_doc.pages:
            include_page1 = True
            if require_page1_not_lightgbm_signer:
                decision = _page1_lightgbm_signer_decision(meta.source_canonical_json)
                stats["page1_clean_negative_lightgbm_guard_checked"] += 1
                if decision["page1_is_top_signer"]:
                    include_page1 = False
                    stats["page1_clean_negative_skipped_lightgbm_top_signer"] += 1
                elif decision["page1_passes_threshold"]:
                    include_page1 = False
                    stats["page1_clean_negative_skipped_lightgbm_threshold"] += 1
                else:
                    stats["page1_clean_negative_lightgbm_guard_passed"] += 1
            if include_page1:
                page1_clean_negative_pages.add(1)
                selected_pages.add(1)
                stats["page1_clean_negative_pages_included"] += 1
        else:
            stats["page1_clean_negative_pages_missing"] += 1

    selected_negative_pages = set(meta.selected_pages) - label_pages - page1_clean_negative_pages
    if include_selected_negative_pages:
        selected_pages.update(meta.selected_pages)
        stats["selected_negative_pages_included"] += len(selected_negative_pages)
    else:
        stats["selected_negative_pages_skipped"] += len(selected_negative_pages)
    if not selected_pages:
        stats["docs_without_labeled_kie_pages"] += 1
        return []
    rows: list[dict[str, Any]] = []
    serious_conflicts_before = stats["conflict_words"]
    for page_index in sorted(selected_pages):
        page = ocr_doc.pages.get(page_index)
        if not page:
            stats["missing_selected_pages"] += 1
            continue
        row = make_page_row(meta, page, fields, stats, conflict_rows)
        if row:
            rows.append(row)
    if stats["conflict_words"] - serious_conflicts_before > 10:
        stats["serious_conflict_docs"] += 1
    stats["field_instances"] += len(fields)
    return rows


def percentile(values: list[int | float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    if len(values) == 1:
        return float(values[0])
    pos = (len(values) - 1) * pct
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(values[lo])
    return float(values[lo] * (hi - pos) + values[hi] * (pos - lo))


def label_counts(rows: Iterable[dict[str, Any]]) -> Counter:
    counts: Counter = Counter()
    for row in rows:
        counts.update(row.get("labels", []))
    return counts


def dataset_paths(project_root: str | Path) -> dict[str, Path]:
    dataset = project_dirs(project_root)["dataset"]
    return {
        "dataset": dataset,
        "train": dataset / "train.jsonl",
        "val": dataset / "val.jsonl",
        "test": dataset / "test.jsonl",
        "label_list": dataset / "label_list.json",
        "manifest": dataset / "manifest.json",
    }


def load_dataset_split(project_root: str | Path, split: str, limit_docs: int | None = None) -> list[dict[str, Any]]:
    path = dataset_paths(project_root)[split]
    rows = read_jsonl(path)
    if limit_docs is None:
        return rows
    keep: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        doc_id = row.get("doc_id")
        if doc_id not in keep and len(keep) >= limit_docs:
            continue
        keep.add(doc_id)
        out.append(row)
    return out


BLOCK_FIELDS: set[str] = {"DOC_SUBJECT", "RECIPIENTS", "ADDRESSEE"}
GAP_REPAIR_LIMITS: dict[str, int] = {
    "DOC_SUBJECT": 1,
    "RECIPIENTS": 2,
    "ADDRESSEE": 1,
}
GAP_REPAIR_MAX_O_CONFIDENCE = 0.72


def _bbox_center_y(box: list[int | float] | tuple[int | float, ...]) -> float:
    return (float(box[1]) + float(box[3])) / 2.0


def _bbox_height(box: list[int | float] | tuple[int | float, ...]) -> float:
    return max(1.0, float(box[3]) - float(box[1]))


def _bbox_x_overlap_ratio(a: list[int | float] | tuple[int | float, ...], b: list[int | float] | tuple[int | float, ...]) -> float:
    left = max(float(a[0]), float(b[0]))
    right = min(float(a[2]), float(b[2]))
    overlap = max(0.0, right - left)
    width = max(1.0, min(float(a[2]) - float(a[0]), float(b[2]) - float(b[0])))
    return overlap / width


def _word_gap_is_layout_close(row: dict[str, Any], left_idx: int, right_idx: int, field: str) -> bool:
    bboxes = row.get("bboxes", [])
    if left_idx < 0 or right_idx >= len(bboxes) or left_idx >= len(bboxes) or right_idx < 0:
        return False
    left = bboxes[left_idx]
    right = bboxes[right_idx]
    y_delta = abs(_bbox_center_y(left) - _bbox_center_y(right))
    line_height = max(_bbox_height(left), _bbox_height(right), 1.0)
    if y_delta <= max(10.0, 0.85 * line_height):
        return True
    if field not in BLOCK_FIELDS:
        return False
    if _bbox_center_y(right) < _bbox_center_y(left):
        return False
    vertical_gap = float(right[1]) - float(left[3])
    if vertical_gap > max(42.0, 2.1 * line_height):
        return False
    return _bbox_x_overlap_ratio(left, right) >= 0.10 or float(right[0]) <= float(left[0]) + 220.0


def repair_bio_labels(row: dict[str, Any], labels: list[str], scores: list[float] | None = None) -> tuple[list[str], dict[str, Any]]:
    """Repair conservative BIO fragmentation without inventing long spans."""
    repaired = list(labels)
    stats = Counter()
    n = len(repaired)

    # Consecutive B-X predictions often mean the same visual block restarted at a line break.
    for idx in range(1, n):
        field = label_field(repaired[idx])
        if field == "O" or label_prefix(repaired[idx]) != "B":
            continue
        if field in MULTI_FIELDS:
            continue
        prev_field = label_field(repaired[idx - 1])
        if prev_field == field and _word_gap_is_layout_close(row, idx - 1, idx, field):
            repaired[idx] = f"I-{field}"
            stats["b_to_i_continuations"] += 1

    idx = 0
    while idx < n:
        if label_field(repaired[idx]) != "O":
            idx += 1
            continue
        gap_start = idx
        while idx < n and label_field(repaired[idx]) == "O":
            idx += 1
        gap_end = idx
        left_idx = gap_start - 1
        right_idx = gap_end
        if left_idx < 0 or right_idx >= n:
            continue
        left_field = label_field(repaired[left_idx])
        right_field = label_field(repaired[right_idx])
        if left_field == "O" or left_field != right_field:
            continue
        gap_len = gap_end - gap_start
        if gap_len > GAP_REPAIR_LIMITS.get(left_field, 1):
            continue
        if not scores:
            continue
        gap_scores = [float(scores[pos]) for pos in range(gap_start, gap_end) if pos < len(scores)]
        if gap_scores and max(gap_scores) > GAP_REPAIR_MAX_O_CONFIDENCE:
            continue
        if not _word_gap_is_layout_close(row, left_idx, right_idx, left_field):
            continue
        for pos in range(gap_start, gap_end):
            repaired[pos] = f"I-{left_field}"
        stats["bridged_o_gaps"] += 1
        stats[f"bridged_{left_field}"] += 1

    return repaired, dict(stats)


def decode_bio_spans(
    row: dict[str, Any],
    labels: list[str],
    scores: list[float] | None = None,
    repair: bool = True,
) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    tokens = row.get("tokens", [])
    word_ids = row.get("word_ids", [])
    bboxes = row.get("bboxes", [])
    if repair:
        labels, _repair_stats = repair_bio_labels(row, labels, scores)
    current_field: str | None = None
    current_indices: list[int] = []

    def close_span() -> None:
        nonlocal current_field, current_indices
        if not current_field or not current_indices:
            current_field = None
            current_indices = []
            return
        span_scores = [scores[i] for i in current_indices] if scores else []
        spans.append(
            {
                "field": current_field,
                "doc_id": row.get("doc_id"),
                "page_index": row.get("page_index"),
                "word_indices": list(current_indices),
                "word_ids": [word_ids[i] for i in current_indices],
                "text": " ".join(str(tokens[i]) for i in current_indices).strip(),
                "bbox": bbox_union([tuple(bboxes[i]) for i in current_indices]),
                "confidence": float(sum(span_scores) / len(span_scores)) if span_scores else None,
            }
        )
        current_field = None
        current_indices = []

    for i, label in enumerate(labels):
        field = label_field(label)
        prefix = label_prefix(label)
        if field == "O":
            close_span()
            continue
        if prefix == "B" or current_field != field:
            close_span()
            current_field = field
            current_indices = [i]
        else:
            current_indices.append(i)
    close_span()
    return spans


def _word_metric_counts(rows: list[dict[str, Any]], pred_labels: list[list[str]]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    overall = Counter()
    per_field: dict[str, Counter] = {field: Counter() for field in FIELDS}
    for row, preds in zip(rows, pred_labels):
        for gold, pred in zip(row.get("labels", []), preds):
            gold_field = label_field(gold)
            pred_field = label_field(pred)
            if gold_field != "O":
                overall["gold"] += 1
                per_field.setdefault(gold_field, Counter())["gold"] += 1
            if pred_field != "O":
                overall["pred"] += 1
                per_field.setdefault(pred_field, Counter())["pred"] += 1
            if gold_field != "O" and pred_field == gold_field:
                overall["tp"] += 1
                per_field.setdefault(gold_field, Counter())["tp"] += 1
            elif pred_field != "O":
                overall["fp"] += 1
                per_field.setdefault(pred_field, Counter())["fp"] += 1
                if gold_field != "O":
                    overall["fn"] += 1
                    per_field.setdefault(gold_field, Counter())["fn"] += 1
            elif gold_field != "O":
                overall["fn"] += 1
                per_field.setdefault(gold_field, Counter())["fn"] += 1
    return _prf(overall), {field: _prf(counts) for field, counts in per_field.items()}


def _prf(counts: Counter | dict[str, Any]) -> dict[str, Any]:
    tp = float(counts.get("tp", 0))
    fp = float(counts.get("fp", 0))
    fn = float(counts.get("fn", 0))
    precision = tp / (tp + fp) if tp + fp > 0 else 0.0
    recall = tp / (tp + fn) if tp + fn > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": int(tp),
        "fp": int(fp),
        "fn": int(fn),
        "gold": int(counts.get("gold", 0)),
        "pred": int(counts.get("pred", 0)),
    }


def _span_key(span: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    return (span["field"], tuple(span.get("word_ids") or []))


def compute_kie_metrics(rows: list[dict[str, Any]], pred_labels: list[list[str]], pred_scores: list[list[float]] | None = None) -> dict[str, Any]:
    word_overall, word_per_field = _word_metric_counts(rows, pred_labels)
    gold_spans: list[dict[str, Any]] = []
    pred_spans: list[dict[str, Any]] = []
    for idx, (row, preds) in enumerate(zip(rows, pred_labels)):
        gold_spans.extend(decode_bio_spans(row, row.get("labels", []), repair=False))
        scores = pred_scores[idx] if pred_scores and idx < len(pred_scores) else None
        pred_spans.extend(decode_bio_spans(row, preds, scores))

    gold_spans, gold_cardinality = apply_cardinality(gold_spans)
    pred_spans, pred_cardinality = apply_cardinality(pred_spans)

    gold_by_field: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pred_by_field: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for span in gold_spans:
        gold_by_field[span["field"]].append(span)
    for span in pred_spans:
        pred_by_field[span["field"]].append(span)

    span_counts = Counter()
    span_per_field: dict[str, Counter] = {field: Counter() for field in FIELDS}
    errors: list[dict[str, Any]] = []
    missing_words = 0
    extra_words = 0
    total_gold_words = 0
    total_pred_words = 0
    best_ious: list[float] = []

    for field in FIELDS:
        gold_items = gold_by_field.get(field, [])
        pred_items = pred_by_field.get(field, [])
        used_pred: set[int] = set()
        for gold in gold_items:
            total_gold_words += len(gold.get("word_ids") or [])
            exact_idx = None
            for pred_idx, pred in enumerate(pred_items):
                if pred_idx in used_pred:
                    continue
                if _span_key(pred) == _span_key(gold):
                    exact_idx = pred_idx
                    break
            if exact_idx is not None:
                used_pred.add(exact_idx)
                span_counts["tp"] += 1
                span_per_field[field]["tp"] += 1
                span_counts["gold"] += 1
                span_counts["pred"] += 1
                span_per_field[field]["gold"] += 1
                span_per_field[field]["pred"] += 1
                best_ious.append(1.0)
                continue

            span_counts["fn"] += 1
            span_counts["gold"] += 1
            span_per_field[field]["fn"] += 1
            span_per_field[field]["gold"] += 1
            best_idx = None
            best_overlap = -1
            gold_set = set(gold.get("word_ids") or [])
            for pred_idx, pred in enumerate(pred_items):
                if pred_idx in used_pred:
                    continue
                overlap = len(gold_set & set(pred.get("word_ids") or []))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_idx = pred_idx
            if best_idx is not None and best_overlap > 0:
                pred = pred_items[best_idx]
                used_pred.add(best_idx)
                pred_set = set(pred.get("word_ids") or [])
                missing_words += len(gold_set - pred_set)
                extra_words += len(pred_set - gold_set)
                best_ious.append(bbox_iou(gold.get("bbox", [0, 0, 0, 0]), pred.get("bbox", [0, 0, 0, 0])))
                errors.append({"type": "partial_or_wrong_span", "field": field, "gold": gold, "pred": pred})
            else:
                missing_words += len(gold_set)
                errors.append({"type": "missing_span", "field": field, "gold": gold, "pred": None})

        for pred_idx, pred in enumerate(pred_items):
            total_pred_words += len(pred.get("word_ids") or [])
            if pred_idx in used_pred:
                continue
            span_counts["fp"] += 1
            span_counts["pred"] += 1
            span_per_field[field]["fp"] += 1
            span_per_field[field]["pred"] += 1
            extra_words += len(pred.get("word_ids") or [])
            errors.append({"type": "extra_span", "field": field, "gold": None, "pred": pred})

    span_overall = _prf(span_counts)
    span_per_field_metrics = {field: _prf(counts) for field, counts in span_per_field.items()}
    exact_instance_accuracy = span_counts["tp"] / span_counts["gold"] if span_counts["gold"] else 0.0
    metrics = {
        "word": {"overall": word_overall, "per_field": word_per_field},
        "span": {"overall": span_overall, "per_field": span_per_field_metrics},
        "exact_instance_accuracy": exact_instance_accuracy,
        "missing_word_rate": missing_words / total_gold_words if total_gold_words else 0.0,
        "extra_word_rate": extra_words / total_pred_words if total_pred_words else 0.0,
        "bbox_iou_mean": float(sum(best_ious) / len(best_ious)) if best_ious else 0.0,
        "gold_instances": len(gold_spans),
        "pred_instances": len(pred_spans),
        "gold_cardinality": gold_cardinality,
        "pred_cardinality": pred_cardinality,
        "errors": errors[:1000],
    }
    return metrics


def _span_sort_key(span: dict[str, Any]) -> tuple[int, float, float, int]:
    bbox = span.get("bbox") or [0, 0, 0, 0]
    indices = span.get("word_indices") or []
    return (int(span.get("page_index") or 0), float(bbox[1]), float(bbox[0]), int(indices[0]) if indices else 0)


def _merge_span_items(items: list[dict[str, Any]], field: str) -> dict[str, Any]:
    items_sorted = sorted(items, key=_span_sort_key)
    merged_words: list[str] = []
    merged_indices: list[int] = []
    merged_text: list[str] = []
    merged_boxes: list[list[float]] = []
    scores: list[float] = []
    for item in items_sorted:
        for wid in item.get("word_ids") or []:
            if wid not in merged_words:
                merged_words.append(wid)
        for word_index in item.get("word_indices") or []:
            if word_index not in merged_indices:
                merged_indices.append(int(word_index))
        if item.get("text"):
            merged_text.append(item["text"])
        if item.get("bbox"):
            merged_boxes.append(item["bbox"])
        if item.get("confidence") is not None:
            scores.append(float(item["confidence"]))
    base = dict(items_sorted[0])
    base["field"] = field
    base["word_ids"] = merged_words
    base["word_indices"] = sorted(merged_indices)
    base["text"] = " ".join(merged_text).strip()
    base["bbox"] = bbox_union([tuple(b) for b in merged_boxes])
    base["confidence"] = sum(scores) / len(scores) if scores else base.get("confidence")
    base["merged_span_count"] = len(items_sorted)
    return base


def _span_page_score(span: dict[str, Any], field: str) -> tuple[float, float, int]:
    confidence = float(span.get("confidence") or 0.0)
    word_count = len(span.get("word_ids") or [])
    if field in {"RECIPIENTS", "DOC_SUBJECT", "ADDRESSEE"}:
        length_bonus = min(word_count, 80) * 0.002
    else:
        length_bonus = min(word_count, 20) * 0.001
    return (confidence + length_bonus, confidence, word_count)


def _spans_are_close(left: dict[str, Any], right: dict[str, Any], field: str) -> bool:
    left_indices = left.get("word_indices") or []
    right_indices = right.get("word_indices") or []
    if not left_indices or not right_indices:
        return False
    gap = int(min(right_indices)) - int(max(left_indices)) - 1
    if gap < 0:
        return True
    max_gap = 2 if field in BLOCK_FIELDS else 1
    if gap > max_gap:
        return False
    left_box = left.get("bbox") or [0, 0, 0, 0]
    right_box = right.get("bbox") or [0, 0, 0, 0]
    y_delta = abs(_bbox_center_y(left_box) - _bbox_center_y(right_box))
    line_height = max(_bbox_height(left_box), _bbox_height(right_box), 1.0)
    if y_delta <= max(12.0, 0.9 * line_height):
        return True
    if field in {"SIGNER_ROLE", "SIGNER_NAME"}:
        return False
    if field in BLOCK_FIELDS and float(right_box[1]) >= float(left_box[1]):
        vertical_gap = float(right_box[1]) - float(left_box[3])
        return vertical_gap <= max(45.0, 2.2 * line_height)
    return False


def _merge_close_multi_spans(items: list[dict[str, Any]], field: str) -> tuple[list[dict[str, Any]], int]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[(str(item.get("doc_id")), int(item.get("page_index") or 0))].append(item)
    merged: list[dict[str, Any]] = []
    merge_count = 0
    for group_items in grouped.values():
        current: list[dict[str, Any]] = []
        for item in sorted(group_items, key=_span_sort_key):
            if not current:
                current = [item]
                continue
            current_span = _merge_span_items(current, field)
            if _spans_are_close(current_span, item, field):
                current.append(item)
                merge_count += 1
            else:
                merged.append(current_span)
                current = [item]
        if current:
            merged.append(_merge_span_items(current, field))
    return merged, merge_count


def apply_cardinality(spans: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for span in spans:
        grouped[(str(span.get("doc_id")), span["field"])].append(span)
    out: list[dict[str, Any]] = []
    fragmentation = Counter()
    multi_merges = Counter()
    cross_page = Counter()
    for (_doc_id, field), items in grouped.items():
        if field in MULTI_FIELDS:
            out.extend(items)
            continue
        if len(items) <= 1:
            out.extend(items)
            continue

        fragmentation[field] += len(items) - 1
        by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            by_page[int(item.get("page_index") or 0)].append(item)
        page_candidates = [_merge_span_items(page_items, field) for page_items in by_page.values()]
        if len(page_candidates) > 1:
            cross_page[field] += len(page_candidates) - 1
        best = max(page_candidates, key=lambda span: _span_page_score(span, field))
        out.append(best)
    return out, {
        "fragmentation_count": dict(fragmentation),
        "fragmented_fields": sum(fragmentation.values()),
        "multi_merge_count": dict(multi_merges),
        "multi_merged_fields": sum(multi_merges.values()),
        "cross_page_candidate_count": dict(cross_page),
        "cross_page_candidates": sum(cross_page.values()),
    }


def summarize_dataset_rows(rows_by_split: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    split_stats: dict[str, Any] = {}
    all_counts = Counter()
    for split, rows in rows_by_split.items():
        doc_ids = {row["doc_id"] for row in rows}
        token_lengths = [len(row.get("tokens", [])) for row in rows]
        counts = label_counts(rows)
        all_counts.update(counts)
        total_labels = sum(counts.values())
        split_stats[split] = {
            "docs": len(doc_ids),
            "pages": len(rows),
            "words": sum(token_lengths),
            "token_length": {
                "min": min(token_lengths) if token_lengths else 0,
                "p50": percentile(token_lengths, 0.50),
                "p90": percentile(token_lengths, 0.90),
                "p95": percentile(token_lengths, 0.95),
                "p99": percentile(token_lengths, 0.99),
                "max": max(token_lengths) if token_lengths else 0,
                "mean": statistics.mean(token_lengths) if token_lengths else 0.0,
            },
            "label_counts": dict(counts),
            "o_label_rate": counts.get("O", 0) / total_labels if total_labels else 0.0,
            "pages_over_512_words": sum(1 for n in token_lengths if n > 512),
        }
    total = sum(all_counts.values())
    return {
        "splits": split_stats,
        "label_counts": dict(all_counts),
        "o_label_rate": all_counts.get("O", 0) / total if total else 0.0,
    }


def write_layout_report(project_root: str | Path) -> None:
    dirs = ensure_project_dirs(project_root)
    dataset_manifest = dataset_paths(project_root)["manifest"]
    manifest = read_json(dataset_manifest) if dataset_manifest.exists() else {}
    sanity_path = dirs["reports"] / "sanity_report.json"
    sanity = read_json(sanity_path) if sanity_path.exists() else None
    training_path = dirs["reports"] / "training_summary.json"
    training = read_json(training_path) if training_path.exists() else None
    eval_reports = sorted(dirs["reports"].glob("evaluation_*.json"))
    eval_payloads = {p.stem.replace("evaluation_", ""): read_json(p) for p in eval_reports}
    onnx_path = dirs["reports"] / "onnx_export_report.json"
    onnx = read_json(onnx_path) if onnx_path.exists() else None
    variant_paths = [
        dirs["reports"] / "onnx_variant_benchmark_threads8.json",
        dirs["reports"] / "onnx_variant_benchmark.json",
    ]
    onnx_variants = None
    for variant_path in variant_paths:
        if variant_path.exists():
            onnx_variants = read_json(variant_path)
            break

    lines: list[str] = []
    lines.append("# LayoutLMv3 KIE Experiment Report")
    lines.append("")
    lines.append(f"Generated: {now_iso()}")
    lines.append("")
    lines.append("## Dataset stats")
    if manifest:
        lines.append(f"- Source labels: `{manifest.get('source_root', '')}`")
        lines.append(f"- Dataset dir: `{manifest.get('dataset_dir', '')}`")
        for split, stats in (manifest.get("summary", {}).get("splits") or {}).items():
            lines.append(
                f"- {split}: docs={stats.get('docs', 0)}, pages={stats.get('pages', 0)}, "
                f"words={stats.get('words', 0)}, O={stats.get('o_label_rate', 0):.3f}, "
                f">512={stats.get('pages_over_512_words', 0)}"
            )
        build_stats = manifest.get("build_stats") or {}
        if build_stats:
            lines.append(
                f"- Dropped fields={build_stats.get('dropped_field_instances', 0)}, "
                f"conflict_words={build_stats.get('conflict_words', 0)}, "
                f"line_id_fallback={build_stats.get('line_id_fallback_instances', 0)}"
            )
    else:
        lines.append("- Pending dataset build.")
    lines.append("")
    lines.append("## Sanity check")
    if sanity:
        lines.append(f"- Pages over max length: {sanity.get('pages_over_max_length', 0)}")
        lines.append(f"- Field instances split by chunks: {sanity.get('field_instances_split_by_chunks', 0)}")
        lines.append(f"- Reconstructed samples: `{sanity.get('sample_output', '')}`")
    else:
        lines.append("- Pending sanity run.")
    lines.append("")
    lines.append("## Training config")
    if training:
        cfg = training.get("config", {})
        lines.append(f"- Model: `{cfg.get('model_name', '')}`")
        lines.append(f"- Epochs: {cfg.get('epochs')}, lr={cfg.get('learning_rate')}, max_length={cfg.get('max_length')}")
        lines.append(f"- Subword labels: {cfg.get('subword_label_strategy')}")
        best = training.get("best_metrics") or {}
        if best:
            lines.append(f"- Best val word F1: {best.get('eval_word_f1', best.get('word_f1', 0)):.4f}")
            lines.append(f"- Best val span F1: {best.get('eval_span_f1', best.get('span_f1', 0)):.4f}")
    else:
        lines.append("- Pending training.")
    lines.append("")
    lines.append("## Evaluation")
    if eval_payloads:
        for split, payload in eval_payloads.items():
            metrics = payload.get("metrics", payload)
            word = metrics.get("word", {}).get("overall", {})
            span = metrics.get("span", {}).get("overall", {})
            lines.append(
                f"- {split}: word_f1={word.get('f1', 0):.4f}, span_f1={span.get('f1', 0):.4f}, "
                f"exact={metrics.get('exact_instance_accuracy', 0):.4f}, "
                f"missing={metrics.get('missing_word_rate', 0):.4f}, extra={metrics.get('extra_word_rate', 0):.4f}"
            )
    else:
        lines.append("- Pending evaluation.")
    lines.append("")
    lines.append("## Fragmentation analysis")
    if eval_payloads:
        for split, payload in eval_payloads.items():
            frag = payload.get("fragmentation", {})
            lines.append(f"- {split}: {frag}")
    else:
        lines.append("- Pending model predictions.")
    lines.append("")
    lines.append("## CPU latency")
    if onnx:
        for key in ("pytorch", "onnx_fp32", "onnx_int8"):
            item = onnx.get("latency", {}).get(key)
            if item:
                lines.append(f"- {key}: {item.get('ms_per_page', 0):.2f} ms/page over {item.get('pages', 0)} pages")
        if onnx.get("accuracy_delta"):
            lines.append(f"- ONNX accuracy delta: {onnx['accuracy_delta']}")
        if onnx_variants:
            lines.append(
                f"- ONNX variant benchmark: split={onnx_variants.get('split')}, "
                f"docs={onnx_variants.get('docs')}, rows={onnx_variants.get('rows')}"
            )
            for name, payload in (onnx_variants.get("variants") or {}).items():
                latency = payload.get("latency", {})
                accuracy = payload.get("accuracy", {})
                lines.append(
                    f"- {name}: {latency.get('ms_per_page', 0):.2f} ms/page, "
                    f"word_f1={accuracy.get('word_f1', 0):.4f}, "
                    f"span_f1={accuracy.get('span_f1', 0):.4f}, "
                    f"exact={accuracy.get('exact_instance_accuracy', 0):.4f}"
                )
    else:
        lines.append("- Pending ONNX export/benchmark.")
    lines.append("")
    lines.append("## LightGBM comparison")
    lines.append("- LightGBM baseline test F1: ~0.935")
    lines.append("- LightGBM baseline exact: ~0.868")
    lines.append("- LightGBM CPU latency: very fast; LayoutLMv3 must beat accuracy enough to justify heavier CPU cost.")
    lines.append("")
    lines.append("## Conclusion")
    if eval_payloads and onnx:
        test = eval_payloads.get("test") or next(iter(eval_payloads.values()))
        span_f1 = test.get("metrics", test).get("span", {}).get("overall", {}).get("f1", 0.0)
        exact = test.get("metrics", test).get("exact_instance_accuracy", 0.0)
        if span_f1 >= 0.935 and exact >= 0.868:
            lines.append("- LayoutLMv3 is a candidate replacement if ONNX/INT8 latency is acceptable for production CPU.")
        else:
            lines.append("- Do not replace LightGBM yet; keep this as an experiment until test accuracy and CPU latency beat the baseline tradeoff.")
    else:
        lines.append("- Pending full train/evaluate/export results.")
    lines.append("")
    (dirs["reports"] / "layoutlmv3_report.md").write_text("\n".join(lines), encoding="utf-8")
