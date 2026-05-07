from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable


RERANK_FIELDS = [
    "ISSUE_ORG_SUPERIOR",
    "ISSUE_ORG_NAME",
    "DOC_SUBJECT",
    "ADDRESSEE",
    "RECIPIENTS",
    "SIGNER_ROLE",
    "SIGNER_NAME",
]

FIELD_QUERIES = {
    "ISSUE_ORG_SUPERIOR": "Chon ung vien la co quan cap tren cua co quan ban hanh van ban hanh chinh Viet Nam.",
    "ISSUE_ORG_NAME": "Chon ung vien la co quan ban hanh van ban, khong phai co quan cap tren.",
    "DOC_SUBJECT": "Chon ung vien la trich yeu hoac tieu de noi dung cua van ban, khong lay phan than bai.",
    "ADDRESSEE": "Chon ung vien la muc Kinh gui hoac noi duoc gui den o dau van ban.",
    "RECIPIENTS": "Chon ung vien la muc Noi nhan o cuoi van ban.",
    "SIGNER_ROLE": "Chon ung vien la chuc vu, vai tro hoac tham quyen cua nguoi ky van ban.",
    "SIGNER_NAME": "Chon ung vien la ho ten nguoi ky van ban.",
}


def ascii_text(text: object) -> str:
    raw = "" if text is None else str(text)
    raw = raw.replace("Ä", "D").replace("Ä‘", "d")
    decomposed = unicodedata.normalize("NFKD", raw)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def compact_text(text: object, limit: int = 1600) -> str:
    cleaned = re.sub(r"[ \t]+", " ", ascii_text(text)).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if len(cleaned) > limit:
        return cleaned[:limit] + " ..."
    return cleaned or "[EMPTY]"


def source_kind(candidate_id: str) -> str:
    parts = (candidate_id or "").split(":")
    return parts[2] if len(parts) >= 4 else "unknown"


def query_for(field: str) -> str:
    return FIELD_QUERIES.get(field, f"Chon ung vien dung cho truong {field} trong van ban hanh chinh Viet Nam.")


def candidate_payload(
    *,
    field: str,
    text: object,
    candidate_id: str,
    page_index: int | float | None,
    bbox: list[float] | tuple[float, ...] | None,
    line_ids: list[str] | tuple[str, ...] | None,
    word_ids: list[str] | tuple[str, ...] | None,
    lgbm_score: float | None = None,
    page_role: str | None = None,
    features: dict | None = None,
) -> str:
    box = [float(value) for value in (bbox or [0, 0, 0, 0])]
    if len(box) != 4:
        box = [0.0, 0.0, 0.0, 0.0]
    width = max(0.0, box[2] - box[0])
    height = max(0.0, box[3] - box[1])
    cx = box[0] + width / 2.0
    cy = box[1] + height / 2.0
    src = source_kind(candidate_id)
    lines = len(line_ids or [])
    words = len(word_ids or [])
    score_part = "" if lgbm_score is None else f" lgbm_score={float(lgbm_score):.6f}"
    role_part = "" if not page_role else f" page_role={page_role}"
    feature_bits = []
    for key in (
        "is_top_band",
        "is_bottom_band",
        "is_left_half",
        "is_right_half",
        "starts_kinh_gui",
        "starts_noi_nhan",
        "starts_signer_prefix",
        "looks_like_name",
        "rx_doc_number",
        "rx_place_date",
        "rx_signer_role_word",
    ):
        if features and key in features:
            feature_bits.append(f"{key}={float(features[key]):.0f}")
    feature_part = "" if not feature_bits else " hints=" + ",".join(feature_bits)
    return (
        f"FIELD={field}\n"
        f"TEXT:\n{compact_text(text)}\n"
        f"LAYOUT: page={page_index}{role_part} source={src} lines={lines} words={words} "
        f"x0={box[0]:.1f} y0={box[1]:.1f} w={width:.1f} h={height:.1f} cx={cx:.1f} cy={cy:.1f}"
        f"{score_part}{feature_part}"
    )


def read_jsonl(path: str | Path) -> list[dict]:
    rows = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: str | Path, rows: Iterable[dict]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
