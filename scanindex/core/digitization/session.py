"""ArchiveSession — shared state for the 3-step archive workflow.

Holds:
  - Identity codes (Mã định danh, Mã phông, Mục lục, Hồ sơ) entered once
    when the user enters Step 1 in a fresh app session.
  - The dropped source PDF + cut points + derived segments for Step 1.
  - The OCR page cache so segments fed into Step 2 can skip re-OCR.

Lifetime: one instance per app run, owned by `ArchiveContainer`. A new
`session_id` is minted each time the user re-enters Step 1 with a different
source PDF so on-disk temp folders never collide between runs.
"""
from __future__ import annotations

import os
import secrets
import shutil
import threading
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional


def _new_session_id() -> str:
    """`YYYYMMDD_HHMMSS_xxxx` — local timestamp + 4 hex chars to avoid
    collision when the user re-picks within the same second."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{secrets.token_hex(2)}"


def cleanup_stale_temp_dirs() -> int:
    """Remove every `./temp/archive_*` left behind by previous app runs.
    Best-effort: directories still locked by another process are skipped.
    Returns the number of dirs successfully removed."""
    base = os.path.join(os.getcwd(), "temp")
    if not os.path.isdir(base):
        return 0
    removed = 0
    try:
        entries = os.listdir(base)
    except OSError:
        return 0
    for name in entries:
        if not name.startswith("archive_"):
            continue
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path, ignore_errors=False)
            removed += 1
        except OSError:
            shutil.rmtree(path, ignore_errors=True)
    return removed


@dataclass
class IdentityCodes:
    """Information collected per dossier when entering Step 1.

    Two modes:
      - **Structured** (default): user fills the 4 archive codes; the
        output filename follows the `<MãĐD>-<MãPhông>-<MụcLục>-<HồSơ>-<STT>.pdf`
        convention required by the government archive standard.
      - **Unstructured** (`is_unstructured=True`): user only supplies a
        free-text `title` (≤1000 chars). The 4 codes are auto-generated
        deterministically from a session-scoped seed so the dossier still
        has a stable composite key in Kho but doesn't pretend to follow
        the archive code system.

    The trailing five fields cover the dossier-level metadata required by
    the official "Hồ sơ" sheet — three of them (retention / physical
    state / term) flow into the exported MetaDuLieu.xlsx, while
    `chuyen_de` and `chu_thich` are kept for in-app display + Kho
    persistence only (not in the 13-col HSLTCQ schema).
    """
    ma_dinh_danh: str = ""      # Mã định danh đơn vị lưu trữ
    ma_phong: str = ""          # Mã phông
    muc_luc: str = ""           # Số Mục lục (2 chars)
    ho_so: str = ""             # Số Hồ sơ (≤5 chars)
    ten_phong: str = ""         # Tên phông (≤1000 chars; for Excel display)
    ten_muc_luc: str = ""       # Tên mục lục (≤1000 chars; for Excel display)
    title: str = ""             # Tên hồ sơ (free text, ≤1000 chars)
    is_unstructured: bool = False
    thoi_han_bao_quan: str = "" # Thời hạn bảo quản (Vĩnh viễn / N năm)
    tinh_trang_vat_ly: str = "" # Tình trạng vật lý (Tốt / Bình thường / Hỏng)
    nhiem_ky: str = ""          # Nhiệm kỳ
    chuyen_de: str = ""         # Chuyên đề (≤1000 chars; not in xlsx)
    chu_thich: str = ""         # Chú thích (≤1000 chars; not in xlsx)

    def is_complete(self) -> bool:
        if self.is_unstructured:
            # In unstructured mode the codes are auto-filled; only `title`
            # is user-mandatory (validation happens in the dialog).
            return bool(self.title and self.ma_dinh_danh and self.ma_phong
                        and self.muc_luc and self.ho_so)
        return bool(self.ma_dinh_danh and self.ma_phong
                    and self.muc_luc and self.ho_so)

    def make_segment_name(self, stt: int) -> str:
        """Build `<MãĐD>-<MãPhông>-<MụcLục>-<HồSơ>-<STT>.pdf`."""
        return (f"{self.ma_dinh_danh}-{self.ma_phong}-"
                f"{self.muc_luc}-{self.ho_so}-{stt:03d}.pdf")

    @classmethod
    def auto_unstructured(cls, title: str, seed: str) -> "IdentityCodes":
        """Build a IdentityCodes for unstructured mode where the 4 codes
        are derived from `seed` (typically the session_id). Same seed →
        same codes, so re-importing is idempotent."""
        import hashlib
        h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        return cls(
            ma_dinh_danh="UNSTRUCT",
            ma_phong=h[:8].upper(),
            muc_luc="00",
            ho_so=h[8:13].upper(),
            ten_phong="",
            ten_muc_luc="",
            title=title.strip()[:1000],
            is_unstructured=True,
        )


@dataclass
class Segment:
    """One contiguous range of source pages destined to become one output PDF."""
    start_page: int          # inclusive, 0-based
    end_page: int            # inclusive, 0-based
    name: str = ""           # generated via IdentityCodes.make_segment_name

    def page_count(self) -> int:
        return self.end_page - self.start_page + 1

    def page_indices(self) -> list[int]:
        return list(range(self.start_page, self.end_page + 1))


class ArchiveSession:
    """Shared mutable state across the 3 archive steps."""

    def __init__(self):
        self.identity: Optional[IdentityCodes] = None
        self.session_id: str = _new_session_id()
        self._temp_root: Optional[str] = None

        # Step 1 source state
        self.source_pdf: Optional[str] = None
        self.source_page_count: int = 0
        # Cut points: a sorted set of page indices i meaning "split BEFORE page i".
        # Always implicitly contains 0 and source_page_count as bounds.
        self.cut_points: set[int] = set()

        # OCR cache populated by Step 1 background runner: page_idx -> page_dict
        self.ocr_cache: dict[int, dict] = {}
        self._cache_lock = threading.Lock()

        # Step 1 → Step 2 handoff: segments after physical split (paths absolute)
        self.segments: list[Segment] = []
        self.step1_ocr_pdf_path: Optional[str] = None
        self.step1_ocr_json_path: Optional[str] = None
        self.doc_start_predictions: list[dict] = []

    # ── temp dir ────────────────────────────────────────────────────

    def temp_dir(self) -> str:
        """Lazily-allocated session-scoped temp dir under ./temp/archive_<sid>/."""
        if self._temp_root is None:
            base = os.path.join(os.getcwd(), "temp", f"archive_{self.session_id}")
            os.makedirs(base, exist_ok=True)
            self._temp_root = base
        return self._temp_root

    # Sub-directory layout — all 3 steps' intermediate artefacts live under
    # the per-session temp root so a single `cleanup_temp()` wipes everything.
    #
    #   <temp_dir>/
    #   ├── _step1_source_ocr.pdf{,.json}   # full-source OCR (uncorrected)
    #   ├── _step1_split/*.pdf              # segment PDFs after Step 1 cut
    #   ├── _step2_kie/*_ocr.pdf{,.json}    # KIE outputs (text-corrected JSON)
    #   └── _step3_signed/*.pdf             # PDFs after digital signing

    def step1_split_dir(self) -> str:
        d = os.path.join(self.temp_dir(), "_step1_split")
        os.makedirs(d, exist_ok=True)
        return d

    def step2_kie_dir(self) -> str:
        d = os.path.join(self.temp_dir(), "_step2_kie")
        os.makedirs(d, exist_ok=True)
        return d

    def step3_signed_dir(self) -> str:
        d = os.path.join(self.temp_dir(), "_step3_signed")
        os.makedirs(d, exist_ok=True)
        return d

    def cleanup_temp(self) -> None:
        """Remove the per-session temp dir. Safe to call repeatedly."""
        if self._temp_root and os.path.isdir(self._temp_root):
            try:
                shutil.rmtree(self._temp_root, ignore_errors=True)
            except Exception:
                pass
        self._temp_root = None

    # ── new run ─────────────────────────────────────────────────────

    def reset_for_new_source(self, pdf_path: str, page_count: int) -> None:
        """Wipe per-source state and start fresh under a new session_id so
        any in-flight runner that captured the old id can't clobber new files."""
        self.cleanup_temp()
        self.session_id = _new_session_id()
        self.source_pdf = pdf_path
        self.source_page_count = page_count
        self.cut_points = set()
        with self._cache_lock:
            self.ocr_cache = {}
        self.segments = []
        self.step1_ocr_pdf_path = None
        self.step1_ocr_json_path = None
        self.doc_start_predictions = []

    # ── cut point manipulation ──────────────────────────────────────

    def toggle_cut(self, page_idx: int) -> bool:
        """Toggle a cut between page `page_idx-1` and `page_idx` (i.e. "split
        BEFORE this page"). Returns True if the cut is now ON."""
        if page_idx <= 0 or page_idx >= self.source_page_count:
            return False
        if page_idx in self.cut_points:
            self.cut_points.discard(page_idx)
            return False
        self.cut_points.add(page_idx)
        return True

    def has_cut(self, page_idx: int) -> bool:
        return page_idx in self.cut_points

    def compute_segments(self) -> list[Segment]:
        """Materialise segments from current cut_points. STT starts at 1."""
        if self.source_page_count <= 0:
            return []
        boundaries = sorted({0, self.source_page_count, *self.cut_points})
        segs: list[Segment] = []
        for i in range(len(boundaries) - 1):
            start = boundaries[i]
            end = boundaries[i + 1] - 1
            stt = i + 1
            name = self.identity.make_segment_name(stt) if self.identity else f"segment_{stt:03d}.pdf"
            segs.append(Segment(start_page=start, end_page=end, name=name))
        return segs

    # ── OCR cache ───────────────────────────────────────────────────

    def cache_page(self, page_idx: int, page_result: dict) -> None:
        with self._cache_lock:
            self.ocr_cache[page_idx] = page_result

    def get_cached_page(self, page_idx: int) -> Optional[dict]:
        with self._cache_lock:
            return self.ocr_cache.get(page_idx)

    def cached_page_count(self) -> int:
        with self._cache_lock:
            return len(self.ocr_cache)

    def all_pages_cached(self) -> bool:
        return self.cached_page_count() >= self.source_page_count > 0
