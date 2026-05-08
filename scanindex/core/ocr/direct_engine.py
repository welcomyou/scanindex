"""
direct_ocr_engine.py - Drop-in replacement for chrome_ocr_engine.py
====================================================================
Uses chrome_screen_ai.dll directly (via screen_ai_ocr.py) instead of
launching Chrome browser + Selenium.

Produces _ocr.pdf: original page as background + invisible text overlay.
Text is rendered at LINE level (not word level) for proper alignment,
matching Chrome's "Save as PDF" output quality.
"""

import copy
import os
import re
import json
import threading
import logging
import atexit
import unicodedata

from scanindex.core.ocr.screen_ai import ScreenAIOCR
from scanindex.core.kie.json_utils import (
    decorate_layout_regions,
    make_document_stub,
    make_line_record,
    make_page_record,
    make_word_record,
    slim_canonical_for_layoutlmv3_runtime_in_place,
    upgrade_ocr_data_in_place,
)
from scanindex.core.ocr.text_normalizer import (
    OCR_TEXT_NORMALIZATION,
    sanitize_ocr_surface_text as _shared_sanitize_ocr_surface_text,
)
from scanindex.infra.paths import get_base_dir

logger = logging.getLogger(__name__)

_LATIN_VIET_RE = re.compile(r"[A-Za-zÀ-ỹĐđ]")
_DISALLOWED_SCRIPT_PREFIXES = {"CYRILLIC", "GREEK", "ARABIC"}
_CONFUSABLE_LATIN_MAP = {
    # Cyrillic → Latin (visually identical glyphs)
    "\u0410": "A", "\u0430": "a",  # А/а
    "\u0412": "B", "\u0432": "b",  # В/в
    "\u0421": "C", "\u0441": "c",  # С/с
    "\u0415": "E", "\u0435": "e",  # Е/е
    "\u041D": "H", "\u043D": "h",  # Н/н  ← was missing, caused word blanking
    "\u0406": "I", "\u0456": "i",  # І/і (Ukrainian)
    "\u0408": "J", "\u0458": "j",  # Ј/ј (Serbian)
    "\u041A": "K", "\u043A": "k",  # К/к
    "\u041C": "M", "\u043C": "m",  # М/м
    "\u041E": "O", "\u043E": "o",  # О/о
    "\u0420": "P", "\u0440": "p",  # Р/р
    "\u0405": "S", "\u0455": "s",  # Ѕ/ѕ (Macedonian DZE)
    "\u0422": "T", "\u0442": "t",  # Т/т
    "\u0423": "Y", "\u0443": "y",  # У/у
    "\u0425": "X", "\u0445": "x",  # Х/х
    # Greek → Latin (visually identical glyphs)
    "\u0391": "A", "\u03B1": "a",  # Α/α
    "\u0392": "B",                  # Β
    "\u0395": "E", "\u03B5": "e",  # Ε/ε
    "\u0397": "H", "\u03B7": "n",  # Η/η (Eta)
    "\u0399": "I", "\u03B9": "i",  # Ι/ι
    "\u039A": "K", "\u03BA": "k",  # Κ/κ
    "\u039C": "M",                  # Μ (Mu)
    "\u039D": "N",                  # Ν (Nu)
    "\u039F": "O", "\u03BF": "o",  # Ο/ο
    "\u03A1": "P", "\u03C1": "p",  # Ρ/ρ
    "\u03A4": "T",                  # Τ (Tau)
    "\u03A5": "Y", "\u03C5": "y",  # Υ/υ (Upsilon)
    "\u03A7": "X", "\u03C7": "x",  # Χ/χ
    "\u0396": "Z",                  # Ζ (Zeta)
}


def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default

# ===========================================================================
# Module-level singleton & hybrid OCR pool
# ===========================================================================
_ocr_instance = None
_ocr_lock = threading.Lock()
_init_lock = threading.Lock()

# Page-level pool: N processes, each loads ONE ScreenAI DLL. Pool size
# = number of OCR pages that can run concurrently across the whole app
# (regardless of how many input files are being processed).
_NUM_PAGE_WORKERS = _env_int("DIRECT_OCR_NUM_PAGE_WORKERS", 4)
# Legacy env vars kept for backwards compat — they map to the new model:
#   total_workers = NUM_PROCESSES * NUM_DLL_PER_PROCESS
_LEGACY_NUM_PROCESSES = os.environ.get("DIRECT_OCR_NUM_PROCESSES")
_LEGACY_NUM_DLL_PER_PROCESS = os.environ.get("DIRECT_OCR_NUM_DLL_PER_PROCESS")
if _LEGACY_NUM_PROCESSES and _LEGACY_NUM_DLL_PER_PROCESS:
    try:
        _NUM_PAGE_WORKERS = max(1, int(_LEGACY_NUM_PROCESSES) * int(_LEGACY_NUM_DLL_PER_PROCESS))
    except ValueError:
        pass

# Kept as references for older call sites that still inspect them
_NUM_PROCESSES = _NUM_PAGE_WORKERS
_NUM_DLL_PER_PROCESS = 1

_mp_pool = None  # multiprocessing.Pool with NUM_PAGE_WORKERS processes
_mp_pool_lock = threading.Lock()

OCR_DPI = 240
OCR_TEXT_NORMALIZATION = "latin_vi_canonical_v2"


def _init_layout_analyzers(log=None):
    """Initialize primary table layout plus auxiliary DocLayNet semantics."""
    primary = None
    auxiliary = None
    layout_module = None
    try:
        from scanindex.core.tables import layout_analyzer as la
        layout_module = la
        if la.is_available():
            primary = la.get_analyzer()
            if primary and log:
                log("DocLayout-YOLO DocStructBench layout analysis enabled", "info")
        if (
            os.environ.get("OCRTOOL_DISABLE_DOCLAYNET_LAYOUT") != "1"
            and hasattr(la, "is_doclaynet_available")
            and la.is_doclaynet_available()
        ):
            auxiliary = la.get_doclaynet_analyzer()
            if auxiliary and log:
                log("DocLayout-YOLO DocLayNet semantic analysis enabled", "info")
    except Exception:
        pass
    return primary, auxiliary, layout_module


def _analyze_combined_layout_regions(page, page_idx, page_w, page_h, analyzers, log=None):
    primary, auxiliary, layout_module = analyzers
    if not primary and not auxiliary:
        return []
    try:
        import fitz
        from PIL import Image

        dpi = OCR_DPI
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, annots=True)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        scale_x = page_w / pix.width
        scale_y = page_h / pix.height

        primary_regions = []
        auxiliary_regions = []
        if primary:
            primary_regions = decorate_layout_regions(
                primary.analyze_page(img),
                page_index=page_idx,
                scale_x=scale_x,
                scale_y=scale_y,
            )
        if auxiliary:
            auxiliary_regions = decorate_layout_regions(
                auxiliary.analyze_page(img),
                page_index=page_idx,
                scale_x=scale_x,
                scale_y=scale_y,
            )

        if layout_module and hasattr(layout_module, "merge_auxiliary_layout_regions"):
            layout_regions = layout_module.merge_auxiliary_layout_regions(
                primary_regions,
                auxiliary_regions,
            )
        else:
            layout_regions = list(primary_regions or [])
        if log:
            aux_count = max(0, len(layout_regions) - len(primary_regions))
            log(
                f"  Page {page_idx + 1}: {len(layout_regions)} layout regions "
                f"({len(primary_regions)} primary + {aux_count} semantic)",
                "debug",
            )
        return layout_regions
    except Exception as e:
        if log:
            log(f"  Layout analysis failed: {e}", "debug")
        return []


# --- Worker process state (1 ScreenAI instance per worker process) ---
_worker_ocr = None       # the worker's ScreenAIOCR instance
_worker_dll_path = None  # loaded DLL path, kept for diagnostics


def _worker_init(dll_path, model_dir):
    """Called once per worker process to load ScreenAI from the model dir.

    The DLL must stay next to its ScreenAI model/runtime files so Windows can
    resolve native dependencies from the same directory in frozen builds."""
    global _worker_ocr, _worker_dll_path

    ocr = ScreenAIOCR(dll_path=dll_path, model_dir=model_dir)
    ocr.initialize()
    _worker_ocr = ocr
    _worker_dll_path = dll_path


def _worker_ocr_single(input_path, page_idx, render_annots=True):
    """OCR a single page using this worker's DLL instance."""
    try:
        import fitz
        from PIL import Image

        doc = fitz.open(input_path)
        page = doc[page_idx]
        page_w = page.rect.width
        page_h = page.rect.height

        dpi = OCR_DPI
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, annots=bool(render_annots))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        render_w, render_h = pix.width, pix.height
        doc.close()

        scale_x = page_w / render_w
        scale_y = page_h / render_h

        if _worker_ocr is None:
            return (page_idx, None)
        result = _worker_ocr.perform_ocr(img)
        ocr_lines = result.get("lines", [])

        lines_data, words_data = _ocr_result_to_page_data(
            page_idx, ocr_lines, scale_x, scale_y
        )

        return (page_idx, {
            "lines_data": lines_data,
            "words_data": words_data,
            "render_width": render_w,
            "render_height": render_h,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return (page_idx, None)


def _worker_ocr_batch(args):
    """Worker function: OCR a list of pages sequentially.

    Used by the legacy file-level path; in the new pipeline the orchestrator
    submits one page per `apply_async` call instead of batching."""
    input_path, page_indices = args
    return [_worker_ocr_single(input_path, pi) for pi in page_indices]


def _get_pool():
    """Get or create the page-level OCR pool (lazy init, thread-safe).

    Returns a SINGLE `multiprocessing.Pool` with `_NUM_PAGE_WORKERS` worker
    processes. Each worker is fully independent (one DLL each, no shared
    state)."""
    global _mp_pool
    if _mp_pool is not None:
        return _mp_pool
    with _mp_pool_lock:
        if _mp_pool is not None:
            return _mp_pool

        import multiprocessing

        dll_path, model_dir = _find_screen_ai_paths()
        if not dll_path:
            raise FileNotFoundError("OCR library not installed")

        pool = multiprocessing.Pool(
            processes=_NUM_PAGE_WORKERS,
            initializer=_worker_init,
            initargs=(dll_path, model_dir),
            maxtasksperchild=100,
        )

        _mp_pool = pool
        logger.info(
            "Page-level OCR pool ready: %d worker processes (1 DLL each)",
            _NUM_PAGE_WORKERS,
        )
        return _mp_pool


def get_page_pool():
    """Public accessor returning the lazily-initialized page-level pool."""
    return _get_pool()


def submit_page(input_path: str, page_idx: int, render_annots: bool = True):
    """Submit one page to the OCR pool. Returns a `multiprocessing.AsyncResult`.

    Caller can `.get(timeout=...)` to block, or `.ready()` to poll. Result is
    a tuple `(page_idx, page_dict_or_None)`."""
    pool = _get_pool()
    return pool.apply_async(_worker_ocr_single, (input_path, page_idx, bool(render_annots)))


class _PrebakedAsyncResult:
    """Stub `AsyncResult` for pages whose OCR result was computed previously.

    Lets the archive pipeline reuse cached page_results from Step 1 without
    re-running OCR in Step 2, while presenting the same `.ready()` / `.get()`
    interface that pipeline_engine expects."""
    __slots__ = ("_page_idx", "_result")

    def __init__(self, page_idx: int, page_result: dict):
        self._page_idx = page_idx
        self._result = page_result

    def ready(self) -> bool:
        return True

    def successful(self) -> bool:
        return True

    def get(self, timeout=None):
        return (self._page_idx, self._result)

    def wait(self, timeout=None):
        return None


def make_prebaked_async_result(page_idx: int, page_result: dict) -> _PrebakedAsyncResult:
    """Wrap an already-OCRed page_result so it can be returned in lieu of a
    real `pool.apply_async()` call."""
    return _PrebakedAsyncResult(page_idx, page_result)


def ocr_one_page(input_path: str, page_idx: int, timeout: float = 120.0,
                 render_annots: bool = True):
    """Synchronous helper: submit one page and wait for the result.

    Returns the page dict (lines/words/render dims) or None on failure."""
    ar = submit_page(input_path, page_idx, render_annots=render_annots)
    try:
        _, result = ar.get(timeout=timeout)
        return result
    except Exception:
        return None


def page_has_render_annotations(input_path: str, page_idx: int) -> bool:
    """Return True when a page has PDF annotations/widgets that pixmap rendering
    can hide with ``annots=False``."""
    import fitz  # PyMuPDF - lazy import

    doc = fitz.open(input_path)
    try:
        if page_idx < 0 or page_idx >= len(doc):
            return False
        page = doc[page_idx]
        try:
            annots = page.annots()
            if annots is not None:
                for _annot in annots:
                    return True
        except Exception:
            pass
        try:
            widgets = page.widgets()
            if widgets is not None:
                for _widget in widgets:
                    return True
        except Exception:
            pass
        return False
    finally:
        doc.close()


def replace_canonical_page_with_page_result(
    canonical_json_path: str,
    source_pdf_path: str,
    page_idx: int,
    page_result: dict,
    *,
    render_annots: bool,
    canonical_profile: str | None = None,
) -> dict:
    """Replace one page's OCR payload in a canonical JSON file.

    Used by archive KIE to swap the signer page to a clean OCR pass where PDF
    annotations are hidden. This updates only the JSON consumed by KIE; it does
    not rewrite the visible PDF.
    """
    import fitz  # PyMuPDF - lazy import

    with open(canonical_json_path, "r", encoding="utf-8") as f:
        ocr_data = json.load(f)

    doc = fitz.open(source_pdf_path)
    try:
        if page_idx < 0 or page_idx >= len(doc):
            raise IndexError(f"page_idx={page_idx} outside PDF page_count={len(doc)}")
        page = doc[page_idx]
        page_w = page.rect.width
        page_h = page.rect.height
    finally:
        doc.close()

    lines_data = copy.deepcopy(page_result.get("lines_data") or [])
    words_data = copy.deepcopy(page_result.get("words_data") or [])
    coord_flipped = _normalize_page_coord_to_top_left(lines_data, words_data, page_h)

    existing = None
    pages = ocr_data.setdefault("pages", [])
    for idx, page in enumerate(pages):
        try:
            existing_page_idx = int(page.get("page_index", idx))
        except (TypeError, ValueError):
            existing_page_idx = idx
        if existing_page_idx == int(page_idx):
            existing = page
            break

    applied_rotation = 0
    if isinstance(existing, dict):
        try:
            applied_rotation = int(existing.get("applied_rotation", 0) or 0) % 360
        except (TypeError, ValueError):
            applied_rotation = 0

    page_data = make_page_record(
        page_index=page_idx,
        width=page_w,
        height=page_h,
        render_width=page_result.get("render_width", 0),
        render_height=page_result.get("render_height", 0),
        applied_rotation=applied_rotation,
    )
    page_data["coord_origin"] = "top-left"
    if coord_flipped:
        page_data["coord_origin_source"] = "normalized_from_bottom_left"
    page_data["lines"] = lines_data
    page_data["words"] = words_data
    page_data["ocr_render_annots"] = bool(render_annots)
    page_data["kie_render_annots"] = bool(render_annots)
    if not render_annots:
        page_data["kie_ocr_override"] = {
            "reason": "signature_page_without_pdf_annotations",
            "source_page_index": int(page_idx),
        }

    replaced = False
    for idx, page in enumerate(pages):
        try:
            existing_page_idx = int(page.get("page_index", idx))
        except (TypeError, ValueError):
            existing_page_idx = idx
        if existing_page_idx == int(page_idx):
            pages[idx] = page_data
            replaced = True
            break
    if not replaced:
        pages.append(page_data)
        pages.sort(key=lambda item: int(item.get("page_index", 0)))

    kie_pipeline = ocr_data.setdefault("pipeline", {}).setdefault("kie", {})
    kie_pipeline["signature_page_clean_ocr"] = {
        "page_index": int(page_idx),
        "render_annots": bool(render_annots),
        "line_count": len(lines_data),
        "word_count": len(words_data),
    }

    upgrade_ocr_data_in_place(ocr_data)
    profile = canonical_profile or ocr_data.get("pipeline", {}).get("ocr", {}).get("canonical_profile")
    if profile in {"layoutlmv3_runtime", "layoutlmv3_runtime_v1"}:
        slim_canonical_for_layoutlmv3_runtime_in_place(ocr_data)

    tmp_path = canonical_json_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(ocr_data, f, ensure_ascii=False)
    os.replace(tmp_path, canonical_json_path)

    return {
        "page_index": int(page_idx),
        "line_count": len(lines_data),
        "word_count": len(words_data),
        "coord_flipped": bool(coord_flipped),
    }


def shutdown_pool():
    """Shutdown the OCR pool. Call on app exit (or to release RAM)."""
    global _mp_pool
    with _mp_pool_lock:
        if _mp_pool is not None:
            try:
                _mp_pool.terminate()
                _mp_pool.join()
            except Exception:
                pass
            _mp_pool = None
            logger.info("OCR pool shut down")

atexit.register(shutdown_pool)


def _find_unicode_font():
    """Find a TTF font that supports Vietnamese characters."""
    candidates = [
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arial.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "times.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "calibri.ttf"),
        os.path.join(get_base_dir(), "models", "fonts", "NotoSans-Regular.ttf"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _get_model_dir():
    """Get the screen_ai model base directory."""
    return os.path.join(get_base_dir(), "models", "screen_ai")


def check_ocr_status():
    """
    Check ScreenAI availability without downloading.
    Returns ScreenAIStatus for UI to decide next action.
    """
    from scanindex.core.ocr.screen_ai_downloader import check_screen_ai
    return check_screen_ai(_get_model_dir())


def install_ocr(status, progress_callback=None, log_callback=None):
    """
    Install ScreenAI (copy from Chrome or download).
    Call ONLY after user consents.
    Returns (lib_path, model_path, color_type).
    """
    from scanindex.core.ocr.screen_ai_downloader import install_screen_ai
    return install_screen_ai(
        _get_model_dir(), status,
        progress_callback=progress_callback,
        log_callback=log_callback
    )


def _find_screen_ai_paths():
    """Auto-detect DLL and model directory using downloader."""
    from scanindex.core.ocr.screen_ai_downloader import check_screen_ai, ScreenAIStatus
    status = check_screen_ai(_get_model_dir())
    if status.status == ScreenAIStatus.FOUND_LOCAL:
        return status.lib_path, status.model_path
    return None, None


def _get_ocr():
    """Get or create the singleton ScreenAIOCR instance (thread-safe)."""
    global _ocr_instance
    if _ocr_instance is not None:
        return _ocr_instance
    with _init_lock:
        if _ocr_instance is not None:
            return _ocr_instance
        dll_path, model_dir = _find_screen_ai_paths()
        if not dll_path:
            raise FileNotFoundError(
                "Thư viện OCR chưa được cài đặt. "
                "Vui lòng cài đặt trong Settings hoặc khởi động lại app.")
        logger.info(f"Initializing ScreenAI OCR from: {model_dir}")
        ocr = ScreenAIOCR(dll_path=dll_path, model_dir=model_dir)
        ocr.initialize()
        logger.info("ScreenAI OCR initialized successfully")
        _ocr_instance = ocr
        return _ocr_instance


# ===========================================================================
# Line-level text rendering
# ===========================================================================

def _normalize_ocr_lookalikes(text):
    if not text:
        return text
    normalized = unicodedata.normalize("NFKC", text)
    if not _LATIN_VIET_RE.search(normalized):
        return normalized
    if not any(ch in _CONFUSABLE_LATIN_MAP for ch in normalized):
        return normalized
    return "".join(_CONFUSABLE_LATIN_MAP.get(ch, ch) for ch in normalized)


def _has_disallowed_script_letters(text):
    for ch in text or "":
        if not ch.isalpha():
            continue
        script_prefix = unicodedata.name(ch, "").split(" ")[0]
        if script_prefix in _DISALLOWED_SCRIPT_PREFIXES:
            return True
    return False


def _sanitize_ocr_surface_text(text):
    return _shared_sanitize_ocr_surface_text(text)


def get_parallel_capacity():
    """Return the maximum per-document OCR worker count for the current config."""
    return max(1, _NUM_PROCESSES * _NUM_DLL_PER_PROCESS)


def _line_text_from_words(words, text_key="text"):
    """Reconstruct line text from word list, respecting has_space_after."""
    parts = []
    for w in words:
        parts.append(w.get(text_key, ""))
        if w.get("has_space_after", True):
            parts.append(" ")
    return "".join(parts).rstrip()


def _line_xywh(ln):
    """Return (x, y, w, h) for a line dict.

    Newer canonical JSONs are slimmed by
    `kie_json_utils.slim_canonical_for_layoutlmv3_runtime_in_place` which
    drops the legacy `x/y/w/h` keys but keeps `bbox`. Older JSONs still have
    both. Read whichever is present so callers don't KeyError on slim files.
    """
    if all(key in ln and ln.get(key) is not None for key in ("x", "y", "w", "h")):
        try:
            return (
                float(ln.get("x", 0.0)),
                float(ln.get("y", 0.0)),
                float(ln.get("w", 0.0)),
                float(ln.get("h", 0.0)),
            )
        except (TypeError, ValueError):
            pass
    bbox = ln.get("bbox") or [0.0, 0.0, 0.0, 0.0]
    if len(bbox) < 4:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


def _build_text_page_lines(page, lines_data, font_path):
    """
    Add invisible text overlay at LINE level (not word level).
    Each line is inserted as a single text operation at the line's
    starting position with consistent font size.
    This matches Chrome's PDF output behavior.
    """
    import fitz  # PyMuPDF - lazy import (not needed by screenshot tool)
    font = fitz.Font(fontfile=font_path)
    tw = fitz.TextWriter(page.rect)

    for ln in lines_data:
        text = ln["text"]
        if not text.strip():
            continue
        x, y, _, h = _line_xywh(ln)
        try:
            tw.append(
                pos=(x, y + h * 0.85),  # baseline
                text=text,
                font=font,
                fontsize=ln.get("font_size", max(h * 0.78, 4.0)),
            )
        except Exception:
            continue

    tw.write_text(page, render_mode=3)  # invisible


def _copy_source_page_background(doc_out, doc_in, page_idx, page_w, page_h, bake_angle=0):
    """
    Copy one source page into the output PDF.

    Preserve the original page object whenever no baked transform is needed so
    page-level rotation metadata such as /Rotate=270 survives intact. Rebuilding
    via new_page()+show_pdf_page() can otherwise flatten the page into a new
    portrait canvas and visually clip landscape scan content.
    """
    if not bake_angle:
        doc_out.insert_pdf(doc_in, from_page=page_idx, to_page=page_idx)
        return doc_out[-1]

    new_page = doc_out.new_page(width=page_w, height=page_h)
    new_page.show_pdf_page(new_page.rect, doc_in, page_idx, rotate=bake_angle)
    return new_page


def _append_backgrounds_preserving_inline_images(doc_out, doc_in, background_specs):
    """
    Append source-page backgrounds in source order.

    Important PyMuPDF quirk:
    - Repeated ``insert_pdf(... from_page=i, to_page=i)`` on a multi-page source
      can drop later inline / transparency-backed image layers (for example a
      red circular stamp PNG placed above the main scan image).
    - Inserting a contiguous range in ONE ``insert_pdf`` call preserves those
      layers.

    Strategy:
    - batch consecutive pages whose ``bake_angle == 0`` into a single range
      insert_pdf call
    - only use show_pdf_page/new_page for pages that truly require a baked
      rotation transform.
    """
    range_start = None
    range_end = None

    def flush_copy_range():
        nonlocal range_start, range_end
        if range_start is not None and range_end is not None:
            doc_out.insert_pdf(doc_in, from_page=range_start, to_page=range_end)
        range_start = None
        range_end = None

    for spec in background_specs:
        page_idx = spec["page_idx"]
        bake_angle = int(spec.get("bake_angle", 0) or 0)
        if bake_angle == 0:
            if range_start is None:
                range_start = range_end = page_idx
            elif page_idx == range_end + 1:
                range_end = page_idx
            else:
                flush_copy_range()
                range_start = range_end = page_idx
            continue

        flush_copy_range()
        _copy_source_page_background(
            doc_out,
            doc_in,
            page_idx,
            spec["page_w"],
            spec["page_h"],
            bake_angle=bake_angle,
        )

    flush_copy_range()


def _normalize_page_coord_to_top_left(lines_data, words_data, page_h):
    """Flip bbox Y to top-left if OCR emitted bottom-left coords.

    ScreenAI occasionally auto-rotates an upside-down image internally to
    read the text, but reports bbox coordinates in the ORIGINAL (upside-down)
    pixel frame. The result is a page whose OCR text is correct but whose
    bbox Y direction is reversed -- i.e. the logically "top" line of the
    document has a LARGE Y value instead of a small one. That convention
    mismatch is a silent train/inference hazard.

    This function detects the case by comparing the average Y of the first 3
    lines (in reading order) to the last 3, and if first > last + 50 it
    flips Y in place for every line and word so the whole page is in
    top-left coords. Returns True if a flip was applied.

    The check uses line order extremes so isolated page-number boxes
    appended at the end of the list cannot fool the decision.
    """
    if not lines_data or len(lines_data) < 6:
        return False
    try:
        by_order = sorted(lines_data, key=lambda L: L.get("order", 0))
        first_ys = [float(L.get("y", 0)) for L in by_order[:3]]
        last_ys = [float(L.get("y", 0)) for L in by_order[-3:]]
        if sum(first_ys) / len(first_ys) <= sum(last_ys) / len(last_ys) + 50.0:
            return False
    except Exception:
        return False
    for rec in list(lines_data) + list(words_data):
        y_new = float(page_h) - (float(rec.get("y", 0)) + float(rec.get("h", 0)))
        rec["y"] = round(y_new, 2)
        bbox = rec.get("bbox")
        if bbox and len(bbox) == 4:
            x0, y0, x1, y1 = bbox
            rec["bbox"] = [x0, round(float(page_h) - y1, 2), x1, round(float(page_h) - y0, 2)]
    return True


def _ocr_result_to_page_data(page_idx, ocr_lines, scale_x, scale_y):
    """
    Convert OCR result lines to structured page data.
    Returns (lines_data, words_data) where:
      - lines_data: for PDF rendering (line-level text + position)
      - words_data: for JSON companion (word-level for correction)
    """
    lines_data = []
    words_data = []

    for line in ocr_lines:
        line_bbox = line.get("bounding_box")
        raw_line_text = (line.get("utf8_string", "") or "").strip()
        line_text = _sanitize_ocr_surface_text(raw_line_text)
        line_words = line.get("words", [])

        if not line_words and not line_text:
            continue

        # Collect word-level data
        page_line_words = []
        line_index = len(lines_data)
        for word in line_words:
            bbox = word.get("bounding_box")
            raw_text = (word.get("utf8_string", "") or "").strip()
            text = _sanitize_ocr_surface_text(raw_text)
            if not bbox or not text:
                continue
            word_index = len(page_line_words)
            page_line_words.append(make_word_record(
                page_index=page_idx,
                line_index=line_index,
                word_index=word_index,
                text=text,
                x=bbox["x"] * scale_x,
                y=bbox["y"] * scale_y,
                w=bbox["width"] * scale_x,
                h=bbox["height"] * scale_y,
                has_space_after=word.get("has_space_after", True),
                confidence=word.get("confidence", 0),
                fg_gray=word.get("foreground_gray", 128),
                content_type=word.get("content_type", 0),
                ocr_text=raw_text,
            ))

        # Preserve line-level OCR even when word boxes are missing.
        if not page_line_words and line_text and line_bbox:
            page_line_words.append(make_word_record(
                page_index=page_idx,
                line_index=line_index,
                word_index=0,
                text=line_text,
                x=line_bbox["x"] * scale_x,
                y=line_bbox["y"] * scale_y,
                w=line_bbox["width"] * scale_x,
                h=line_bbox["height"] * scale_y,
                has_space_after=False,
                confidence=line.get("confidence", 0),
                fg_gray=line.get("foreground_gray", 128),
                content_type=line.get("content_type", 0),
                ocr_text=raw_line_text or line_text,
            ))

        if not page_line_words:
            continue

        words_data.extend(page_line_words)

        # Line-level data: use line bbox for position, reconstruct text from words
        if line_bbox:
            lx = line_bbox["x"] * scale_x
            ly = line_bbox["y"] * scale_y
            lw = line_bbox["width"] * scale_x
            lh = line_bbox["height"] * scale_y
        else:
            # Fallback: derive from first/last word
            lx = page_line_words[0]["x"]
            ly = min(w["y"] for w in page_line_words)
            lw = max(w["x"] + w["w"] for w in page_line_words) - lx
            lh = max(w["h"] for w in page_line_words)

        # Always rebuild from words so line.text stays consistent with word.text.
        display_text = _line_text_from_words(page_line_words, text_key="text")
        display_ocr_text = raw_line_text or _line_text_from_words(page_line_words, text_key="ocr_text")

        # Font size: consistent per line, derived from line height
        font_size = max(lh * 0.78, 4.0)

        # Compute average fg_gray for this line from its words
        word_grays = [w["fg_gray"] for w in page_line_words if w.get("fg_gray", 128) != 128]
        avg_fg_gray = round(sum(word_grays) / len(word_grays)) if word_grays else 128

        lines_data.append(make_line_record(
            page_index=page_idx,
            line_index=line_index,
            text=display_text,
            x=lx,
            y=ly,
            w=lw,
            h=lh,
            font_size=font_size,
            block_id=line.get("block_id", 0),
            paragraph_id=line.get("paragraph_id", 0),
            confidence=line.get("confidence", 0),
            content_type=line.get("content_type", 0),
            fg_gray=avg_fg_gray,
            word_ids=[w["id"] for w in page_line_words],
            ocr_text=display_ocr_text,
        ))

    return lines_data, words_data


# ===========================================================================
# Public API
# ===========================================================================

def check_dependencies():
    return setup_library()


def setup_library():
    dll_path, model_dir = _find_screen_ai_paths()
    if dll_path and os.path.exists(dll_path):
        return True, None
    return False, "chrome_screen_ai.dll not found in models/screen_ai/"


def process_pdf(input_path, output_path, num_pages=None, update_callback=None,
                wait_per_page=None, comparison_interval=None,
                source_document_path=None,
                allow_page_parallel=True,
                preprocess_rotations=None,
                canonical_profile=None,
                include_layout_analysis=True):
    """
    OCR a PDF and produce output with positioned text.
    Drop-in replacement for chrome_ocr_engine.process_pdf().

    Uses page-level multiprocessing when processing multi-page PDFs
    to utilize multiple CPU cores (each worker loads its own DLL instance).

    ``preprocess_rotations`` is an optional list of cardinal rotations
    (0/90/180/270) that upstream preprocessing applied per page; when
    provided it is stored on each canonical page record as
    ``applied_rotation`` so downstream viewers can render the ORIGINAL
    source PDF in the same orientation as the OCR bboxes.
    """
    def log(msg, level="info"):
        if update_callback:
            try:
                update_callback(msg, level)
            except:
                try:
                    update_callback(msg)
                except:
                    pass
        logger.info(msg)

    try:
        import fitz  # PyMuPDF - lazy import
        import time
        from scanindex.core.preprocessing.preprocessing import classify_pdf

        pdf_type = classify_pdf(input_path)
        if pdf_type == "digital":
            from scanindex.core.pdf.text_extractor import extract_digital_pdf_as_ocr

            log("Digital PDF detected: extracting native text layer to OCR-compatible JSON...", "info")
            return extract_digital_pdf_as_ocr(
                input_path,
                output_path,
                source_document_path=source_document_path or input_path,
                update_callback=update_callback,
                canonical_profile=canonical_profile,
            )

        doc_in = fitz.open(input_path)
        total_pages = len(doc_in)

        log(f"Processing {total_pages} pages...", "info")

        font_path = _find_unicode_font()
        if not font_path:
            doc_in.close()
            return False, "No Unicode font found (need arial.ttf or similar)"

        # Determine parallelism: page-level pool with NUM_PAGE_WORKERS workers
        use_parallel = bool(
            allow_page_parallel and
            total_pages >= 2 and
            _NUM_PAGE_WORKERS > 1
        )

        if use_parallel:
            try:
                _get_pool()  # warm pool (lazy init)
                log(f"Parallel OCR: {_NUM_PAGE_WORKERS} page workers for {total_pages} pages", "info")
                all_page_results = _process_pages_parallel(
                    input_path, total_pages, log
                )
            except Exception as e:
                log(f"Parallel init failed, using serial: {e}", "info")
                all_page_results = _process_pages_serial(
                    input_path, doc_in, total_pages, log
                )
        else:
            all_page_results = _process_pages_serial(
                input_path, doc_in, total_pages, log
            )

        if all_page_results is None:
            doc_in.close()
            return False, "OCR processing failed"

        # Try to initialize layout analyzers only for flows that need
        # page-level semantic/table regions, such as PDF-to-Word export.
        _layout_analyzers = (
            _init_layout_analyzers(log)
            if include_layout_analysis
            else (None, None, None)
        )

        # Assemble output PDF and canonical JSON
        ocr_data = make_document_stub(
            input_path=input_path,
            engine="direct_screen_ai",
            ocr_dpi=OCR_DPI,
            source_path=source_document_path or input_path,
            text_normalization=OCR_TEXT_NORMALIZATION,
            raw_text_preserved=True,
        )

        doc_out = fitz.open()
        pending_line_overlays = []
        background_specs = []
        for page_idx in range(total_pages):
            page = doc_in[page_idx]
            page_w = page.rect.width
            page_h = page.rect.height

            pr = all_page_results[page_idx]
            lines_data = pr["lines_data"]
            words_data = pr["words_data"]

            # Run layout analysis on same page (if available)
            layout_regions = _analyze_combined_layout_regions(
                page,
                page_idx,
                page_w,
                page_h,
                _layout_analyzers,
                log,
            )

            # Normalize bbox Y direction to top-left BEFORE building the
            # canonical page record, so downstream train/inference always sees
            # the same coord convention regardless of whether ScreenAI
            # auto-rotated internally.
            coord_flipped = _normalize_page_coord_to_top_left(lines_data, words_data, page_h)

            # Store in canonical JSON
            applied_rotation = 0
            if preprocess_rotations and page_idx < len(preprocess_rotations):
                try:
                    applied_rotation = int(preprocess_rotations[page_idx] or 0) % 360
                except (TypeError, ValueError):
                    applied_rotation = 0
            page_data = make_page_record(
                page_index=page_idx,
                width=page_w,
                height=page_h,
                render_width=pr.get("render_width", 0),
                render_height=pr.get("render_height", 0),
                applied_rotation=applied_rotation,
            )
            page_data["coord_origin"] = "top-left"
            if coord_flipped:
                page_data["coord_origin_source"] = "normalized_from_bottom_left"
            page_data["lines"] = lines_data
            page_data["words"] = words_data
            if layout_regions:
                page_data["layout_regions"] = layout_regions
            ocr_data["pages"].append(page_data)

            # Create output page. When the coord normalizer detected that
            # ScreenAI had auto-rotated internally (source page is visually
            # upside-down), BAKE the 180° rotation into the content stream
            # via show_pdf_page(rotate=180). This rewrites the content at
            # the vector level -- no rasterization, no quality loss -- so the
            # output renders correctly in EVERY PDF viewer, not just those
            # that honor the /Rotate metadata flag.
            bake_angle = 180 if coord_flipped else 0
            pending_line_overlays.append(lines_data)
            background_specs.append({
                "page_idx": page_idx,
                "page_w": page_w,
                "page_h": page_h,
                "bake_angle": bake_angle,
            })

        _append_backgrounds_preserving_inline_images(doc_out, doc_in, background_specs)

        for page_idx, lines_data in enumerate(pending_line_overlays):
            if page_idx >= len(doc_out):
                break
            _build_text_page_lines(doc_out[page_idx], lines_data, font_path)

        doc_out.save(output_path, deflate=True, garbage=4)
        doc_out.close()
        doc_in.close()

        json_path = output_path + ".json"
        upgrade_ocr_data_in_place(ocr_data)
        if canonical_profile == "layoutlmv3_runtime":
            slim_canonical_for_layoutlmv3_runtime_in_place(ocr_data)
        json_tmp_path = json_path + ".tmp"
        with open(json_tmp_path, "w", encoding="utf-8") as f:
            json.dump(ocr_data, f, ensure_ascii=False)
        os.replace(json_tmp_path, json_path)

        log(f"OCR completed: {output_path}", "success")
        return True, None

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, str(e)


def assemble_pdf_from_page_results(input_path, output_path, all_page_results,
                                   source_document_path=None,
                                   preprocess_rotations=None,
                                   update_callback=None,
                                   canonical_profile=None,
                                   include_layout_analysis=True):
    """Assemble `_ocr.pdf` + canonical JSON from already-OCRed page_results.

    Skips the OCR phase entirely — used when Step 1 of the archive screen has
    already pre-OCR'd every page and the cache hits. Mirrors the assembly
    half of `process_pdf` (lines 727-850)."""
    def log(msg, level="info"):
        if update_callback:
            try:
                update_callback(msg, level)
            except Exception:
                try:
                    update_callback(msg)
                except Exception:
                    pass
        logger.info(msg)

    try:
        import fitz

        font_path = _find_unicode_font()
        if not font_path:
            return False, "No Unicode font found (need arial.ttf or similar)"

        doc_in = fitz.open(input_path)
        total_pages = len(doc_in)

        _layout_analyzers = (
            _init_layout_analyzers(log)
            if include_layout_analysis
            else (None, None, None)
        )

        ocr_data = make_document_stub(
            input_path=input_path,
            engine="direct_screen_ai",
            ocr_dpi=OCR_DPI,
            source_path=source_document_path or input_path,
            text_normalization=OCR_TEXT_NORMALIZATION,
            raw_text_preserved=True,
        )

        doc_out = fitz.open()
        pending_line_overlays = []
        background_specs = []
        for page_idx in range(total_pages):
            page = doc_in[page_idx]
            page_w = page.rect.width
            page_h = page.rect.height

            pr = all_page_results.get(page_idx) or {
                "lines_data": [],
                "words_data": [],
                "render_width": 0,
                "render_height": 0,
            }
            lines_data = pr.get("lines_data", [])
            words_data = pr.get("words_data", [])

            layout_regions = _analyze_combined_layout_regions(
                page,
                page_idx,
                page_w,
                page_h,
                _layout_analyzers,
                log,
            )

            coord_flipped = _normalize_page_coord_to_top_left(lines_data, words_data, page_h)

            applied_rotation = 0
            if preprocess_rotations and page_idx < len(preprocess_rotations):
                try:
                    applied_rotation = int(preprocess_rotations[page_idx] or 0) % 360
                except (TypeError, ValueError):
                    applied_rotation = 0
            page_data = make_page_record(
                page_index=page_idx,
                width=page_w,
                height=page_h,
                render_width=pr.get("render_width", 0),
                render_height=pr.get("render_height", 0),
                applied_rotation=applied_rotation,
            )
            page_data["coord_origin"] = "top-left"
            if coord_flipped:
                page_data["coord_origin_source"] = "normalized_from_bottom_left"
            page_data["lines"] = lines_data
            page_data["words"] = words_data
            if layout_regions:
                page_data["layout_regions"] = layout_regions
            ocr_data["pages"].append(page_data)

            bake_angle = 180 if coord_flipped else 0
            pending_line_overlays.append(lines_data)
            background_specs.append({
                "page_idx": page_idx,
                "page_w": page_w,
                "page_h": page_h,
                "bake_angle": bake_angle,
            })

        _append_backgrounds_preserving_inline_images(doc_out, doc_in, background_specs)

        for page_idx, lines_data in enumerate(pending_line_overlays):
            if page_idx >= len(doc_out):
                break
            _build_text_page_lines(doc_out[page_idx], lines_data, font_path)

        doc_out.save(output_path, deflate=True, garbage=4)
        doc_out.close()
        doc_in.close()

        json_path = output_path + ".json"
        upgrade_ocr_data_in_place(ocr_data)
        if canonical_profile == "layoutlmv3_runtime":
            slim_canonical_for_layoutlmv3_runtime_in_place(ocr_data)
        json_tmp_path = json_path + ".tmp"
        with open(json_tmp_path, "w", encoding="utf-8") as f:
            json.dump(ocr_data, f, ensure_ascii=False)
        os.replace(json_tmp_path, json_path)

        log(f"Assembled (cached OCR): {output_path}", "success")
        return True, None

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, str(e)


def _process_pages_serial(input_path, doc_in, total_pages, log):
    """Serial OCR processing (original single-threaded path)."""
    from PIL import Image

    ocr = _get_ocr()
    results = {}

    for page_idx in range(total_pages):
        page = doc_in[page_idx]
        page_w = page.rect.width
        page_h = page.rect.height

        log(f"OCR page {page_idx + 1}/{total_pages}...", "debug")

        import fitz
        dpi = OCR_DPI
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, annots=True)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        scale_x = page_w / pix.width
        scale_y = page_h / pix.height

        with _ocr_lock:
            result = ocr.perform_ocr(img)

        ocr_lines = result.get("lines", [])
        log(f"  Page {page_idx + 1}: {len(ocr_lines)} lines detected", "debug")

        lines_data, words_data = _ocr_result_to_page_data(
            page_idx, ocr_lines, scale_x, scale_y
        )

        results[page_idx] = {
            "lines_data": lines_data,
            "words_data": words_data,
            "render_width": pix.width,
            "render_height": pix.height,
        }

    return results


def _process_pages_parallel(input_path, total_pages, log,
                              page_timeout: float = 120.0):
    """Submit each page to the page-level pool concurrently and collect results.

    The pool has `_NUM_PAGE_WORKERS` worker processes (1 DLL each). All pages
    of the input file are submitted as individual `apply_async` tasks; the
    pool load-balances them across workers. Pages from concurrent files share
    the same pool, so true page-level parallelism is preserved across files."""
    import time

    pool = _get_pool()
    t0 = time.perf_counter()
    results = {}

    try:
        async_results = [
            (page_idx, pool.apply_async(_worker_ocr_single, (input_path, page_idx)))
            for page_idx in range(total_pages)
        ]
        for page_idx, ar in async_results:
            try:
                _, page_result = ar.get(timeout=page_timeout)
            except Exception as e:
                log(f"  Page {page_idx + 1}/{total_pages}: OCR error: {e}", "error")
                page_result = None
            if page_result is not None:
                results[page_idx] = page_result
                n_lines = len(page_result["lines_data"])
                log(f"  Page {page_idx + 1}/{total_pages}: {n_lines} lines detected", "debug")
            else:
                log(f"  Page {page_idx + 1}/{total_pages}: OCR failed", "error")
                results[page_idx] = {
                    "lines_data": [],
                    "words_data": [],
                    "render_width": 0,
                    "render_height": 0,
                }

        dt = time.perf_counter() - t0
        log(f"Parallel OCR completed: {total_pages} pages in {dt:.1f}s "
            f"({_NUM_PAGE_WORKERS} workers, {dt/max(total_pages,1):.2f}s/page avg)",
            "info")
        return results

    except Exception as e:
        log(f"Parallel OCR failed, falling back to serial: {e}", "error")
        import fitz
        doc_in = fitz.open(input_path)
        result = _process_pages_serial(input_path, doc_in, total_pages, log)
        doc_in.close()
        return result


def rebuild_pdf_with_text(original_input_path, output_path, ocr_json_path,
                          replacements=None, log_callback=None):
    """
    Rebuild PDF with corrected text using stored OCR positions.
    Applies word-level replacements, then re-renders at line level.
    """
    def log(msg, level="info"):
        if log_callback:
            try:
                log_callback(msg, level)
            except:
                pass

    try:
        with open(ocr_json_path, "r", encoding="utf-8") as f:
            ocr_data = json.load(f)

        font_path = _find_unicode_font()
        if not font_path:
            return False, "No Unicode font found"

        # Find the source PDF for page backgrounds
        # Prefer the original input (no text) over _ocr.pdf (has text overlay)
        input_pdf = original_input_path
        if not os.path.exists(input_pdf):
            stored = (
                ocr_data.get("input_path", "")
                or ocr_data.get("document", {}).get("source_path", "")
            )
            if os.path.exists(stored):
                input_pdf = stored
            else:
                return False, f"Original input PDF not found: {input_pdf}"

        # Detect if source PDF already has text (i.e., it's _ocr.pdf not original)
        # If so, we must render pages as images to avoid copying old text layer
        import fitz  # PyMuPDF - lazy import

        doc_in = fitz.open(input_pdf)
        source_has_text = len(doc_in[0].get_text().strip()) > 0

        replacements = replacements or {}
        doc_out = fitz.open()
        pending_corrected_lines = []
        background_specs = []
        total_replaced = 0

        for page_idx, page_data in enumerate(ocr_data["pages"]):
            if page_idx >= len(doc_in):
                break

            page_w = page_data["width"]
            page_h = page_data["height"]

            # Bake rotation when copying from raw source and the canonical
            # JSON says coords were normalized from a bottom-left OCR
            # (source page is visually upside-down).
            need_bake = (
                not source_has_text
                and page_data.get("coord_origin_source") == "normalized_from_bottom_left"
            )
            bake_angle = 180 if need_bake else 0

            if source_has_text:
                new_page = doc_out.new_page(width=page_w, height=page_h)
                # Source is _ocr.pdf (already has text) - render to image to strip text
                pix = doc_in[page_idx].get_pixmap(dpi=200, annots=True)
                new_page.insert_image(new_page.rect, pixmap=pix)
            else:
                background_specs.append({
                    "page_idx": page_idx,
                    "page_w": page_w,
                    "page_h": page_h,
                    "bake_angle": bake_angle,
                })

            # Apply word-level replacements
            corrected_words = []
            for wd in page_data.get("words", []):
                text = wd["text"]
                new_text = replacements.get(text, text)
                if new_text != text:
                    total_replaced += 1
                corrected_words.append({**wd, "text": new_text})

            # Handle multi-word phrase replacements
            for old_phrase, new_phrase in replacements.items():
                if " " not in old_phrase:
                    continue
                old_words = old_phrase.split()
                new_words = new_phrase.split()
                n = len(old_words)
                i = 0
                while i <= len(corrected_words) - n:
                    if all(corrected_words[i + j]["text"] == old_words[j] for j in range(n)):
                        if len(new_words) == n:
                            for j in range(n):
                                corrected_words[i + j]["text"] = new_words[j]
                                total_replaced += 1
                        i += n
                    else:
                        i += 1

            # Rebuild lines from corrected words using original line structure
            corrected_lines = _rebuild_lines_from_words(
                page_data.get("lines", []), corrected_words
            )
            pending_corrected_lines.append(corrected_lines)

        if not source_has_text:
            _append_backgrounds_preserving_inline_images(doc_out, doc_in, background_specs)

        for page_idx, corrected_lines in enumerate(pending_corrected_lines):
            if page_idx >= len(doc_out):
                break
            _build_text_page_lines(doc_out[page_idx], corrected_lines, font_path)

        doc_out.save(output_path, deflate=True, garbage=4)
        doc_out.close()
        doc_in.close()

        log(f"Rebuilt PDF with {total_replaced} corrections", "debug")
        return True, f"Rebuilt with {total_replaced} corrections"

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, str(e)


def _rebuild_lines_from_words(original_lines, corrected_words):
    """
    Rebuild line-level text from corrected words, preserving original
    line positions and font sizes.
    """
    if not original_lines or not corrected_words:
        return original_lines or []

    result_lines = []
    word_idx = 0

    for ln in original_lines:
        # Count how many words belong to this line
        # (by matching original line text against sequential words)
        orig_text = ln["text"]
        orig_word_list = orig_text.split()
        n_words = len(orig_word_list)

        # Take next n_words from corrected_words
        if word_idx + n_words <= len(corrected_words):
            line_words = corrected_words[word_idx:word_idx + n_words]
            new_text = _line_text_from_words(line_words)
            word_idx += n_words
        else:
            # Fallback: use remaining words
            remaining = corrected_words[word_idx:]
            new_text = _line_text_from_words(remaining) if remaining else orig_text
            word_idx = len(corrected_words)

        x, y, w, h = _line_xywh(ln)
        result_lines.append({
            "text": new_text,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "font_size": ln.get("font_size", max(h * 0.78, 4.0)),
        })

    return result_lines
