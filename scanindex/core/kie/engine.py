"""Unified KIE inference interface.

Production KIE supports LayoutLMv3 text-only mode.
Full-field LightGBM KIE is disabled; LightGBM remains only in archive_page_splitter.

The public entry point is `extract_metadata_kie(canonical_json_path, mode)`
which returns an annotation dict in the same shape as
`scanindex.core.kie.inference_pipeline.inject_annotation_into_canonical` expects:

  {
    "schema": "kie_vi_official_v3",
    "field_instances": [{field_id, label, page_index, line_ids, word_ids,
                          text, normalized_value, confidence, bbox}, ...],
    "relations": [{relation_id, type, from_field_id, to_field_id}, ...],
  }
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

KIE_MODE_LAYOUTLMV3 = "layoutlmv3"
KIE_MODE_LAYOUTLMV3_VISUAL = "layoutlmv3_visual"

def normalize_kie_mode(mode: str | None) -> str:
    key = (mode or "").strip().lower().replace("-", "_")
    if key == KIE_MODE_LAYOUTLMV3:
        return key
    raise ValueError(
        f"Unsupported KIE mode {mode!r}; expected {KIE_MODE_LAYOUTLMV3!r}."
    )


# ────────────────────────────────────────────────────────────────────
# Path resolution
# ────────────────────────────────────────────────────────────────────

def _resolve_repo_root() -> str:
    try:
        from scanindex.infra.paths import get_base_dir
        return get_base_dir()
    except Exception:
        return str(Path(__file__).resolve().parents[3])


def _layoutlmv3_dir() -> str:
    return os.path.join(_resolve_repo_root(), "models", "layoutlmv3_fontgray_norm_final_epoch25")


def _layoutlmv3_visual_root() -> str:
    raise RuntimeError("LayoutLMv3 visual KIE is disabled.")


def _layoutlmv3_visual_model_dir() -> str:
    raise RuntimeError("LayoutLMv3 visual KIE is disabled.")


def _layoutlmv3_visual_onnx_path() -> str:
    raise RuntimeError("LayoutLMv3 visual KIE is disabled.")


# ────────────────────────────────────────────────────────────────────
# LayoutLMv3 backend
# ────────────────────────────────────────────────────────────────────

class _LayoutLMv3State:
    loaded: bool = False
    session: Any = None              # ort InferenceSession
    tokenizer: Any = None
    label_list: list[str] | None = None
    cfg: dict | None = None          # layoutlmv3_fontgray_config
    lock: threading.Lock = threading.Lock()


_llmv3 = _LayoutLMv3State()


class _LayoutLMv3VisualState:
    loaded: bool = False
    session: Any = None              # ort InferenceSession
    tokenizer: Any = None
    image_processor: Any = None
    label_list: list[str] | None = None
    id2label: dict[int, str] | None = None
    cfg: dict | None = None
    lock: threading.Lock = threading.Lock()


_llmv3_visual = _LayoutLMv3VisualState()


class _NumpyLayoutLMv3ImageProcessor:
    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        size = cfg.get("size") if isinstance(cfg.get("size"), dict) else {}
        self.width = int(size.get("width") or 224)
        self.height = int(size.get("height") or 224)
        self.do_resize = bool(cfg.get("do_resize", True))
        self.do_rescale = bool(cfg.get("do_rescale", True))
        self.do_normalize = bool(cfg.get("do_normalize", True))
        self.rescale_factor = float(cfg.get("rescale_factor", 1.0 / 255.0))
        self.mean = cfg.get("image_mean") or [0.5, 0.5, 0.5]
        self.std = cfg.get("image_std") or [0.5, 0.5, 0.5]
        try:
            self.resample = int(cfg.get("resample", 2))
        except (TypeError, ValueError):
            self.resample = 2

    @classmethod
    def from_pretrained(cls, model_dir: str) -> "_NumpyLayoutLMv3ImageProcessor":
        cfg_path = os.path.join(model_dir, "preprocessor_config.json")
        cfg = {}
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        return cls(cfg)

    def __call__(self, images: list[Any], return_tensors: str = "np") -> dict[str, Any]:
        import numpy as np
        from PIL import Image

        resampling = getattr(Image.Resampling, "BILINEAR", Image.BILINEAR)
        if self.resample == 0:
            resampling = getattr(Image.Resampling, "NEAREST", Image.NEAREST)
        elif self.resample == 1:
            resampling = getattr(Image.Resampling, "LANCZOS", Image.LANCZOS)
        elif self.resample == 3:
            resampling = getattr(Image.Resampling, "BICUBIC", Image.BICUBIC)

        mean = np.asarray(self.mean, dtype="float32").reshape(1, 1, 3)
        std = np.asarray(self.std, dtype="float32").reshape(1, 1, 3)
        pixel_values = []
        for image in images:
            if not isinstance(image, Image.Image):
                image = Image.fromarray(np.asarray(image))
            image = image.convert("RGB")
            if self.do_resize:
                image = image.resize((self.width, self.height), resampling)
            arr = np.asarray(image, dtype="float32")
            if self.do_rescale:
                arr *= self.rescale_factor
            if self.do_normalize:
                arr = (arr - mean) / np.clip(std, 1e-12, None)
            pixel_values.append(arr.transpose(2, 0, 1))
        return {"pixel_values": np.stack(pixel_values, axis=0).astype("float32")}


_LINE_NUM_RE = re.compile(r"(?:^|[_\-.])(?:l|line)[_\-.]?(\d+)(?=$|[_\-.])", re.IGNORECASE)
_LINE_ID_EXACT_RE = re.compile(r"^p(?P<page>\d+)_l(?P<line>\d+)$", re.IGNORECASE)
_STYLE_BASE_TYPE_VOCAB_SIZE = 64
_LINE_POSITION_BUCKET_COUNT = 16
_LINE_EXACT_PREFIX_BUCKETS = 12


def warmup_layoutlmv3(log_cb: Optional[Callable[[str], None]] = None) -> bool:
    log_cb = log_cb or (lambda m: None)
    with _llmv3.lock:
        if _llmv3.loaded:
            return True
        model_dir = _layoutlmv3_dir()
        onnx_path = os.path.join(model_dir, "layoutlmv3_fontgray_norm_final_epoch25.int8.onnx")
        if not os.path.exists(onnx_path):
            log_cb(f"LayoutLMv3 ONNX missing: {onnx_path}")
            return False
        try:
            t_total = time.perf_counter()
            t0 = time.perf_counter()
            log_cb("Loading LayoutLMv3 runtime imports...")
            import onnxruntime as ort
            from transformers.models.layoutlmv3.tokenization_layoutlmv3_fast import (
                LayoutLMv3TokenizerFast,
            )
            log_cb(f"LayoutLMv3 runtime imports ready ({time.perf_counter() - t0:.1f}s)")
            log_cb(f"Loading LayoutLMv3 ONNX int8 ({os.path.basename(onnx_path)})...")
            sess_opts = ort.SessionOptions()
            # EXTENDED keeps the graph portable across CPUs while avoiding the
            # oversubscription that ORT defaults can trigger on hybrid cores.
            default_threads = max(1, (os.cpu_count() or 4) // 2)
            try:
                threads = int(os.environ.get("LAYOUTLMV3_ONNX_THREADS", default_threads))
            except (TypeError, ValueError):
                threads = default_threads
            sess_opts.intra_op_num_threads = max(1, threads)
            sess_opts.inter_op_num_threads = 1
            sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
            t0 = time.perf_counter()
            session = ort.InferenceSession(
                onnx_path, sess_options=sess_opts,
                providers=["CPUExecutionProvider"],
            )
            log_cb(f"LayoutLMv3 ONNX session ready ({time.perf_counter() - t0:.1f}s)")
            t0 = time.perf_counter()
            tokenizer = LayoutLMv3TokenizerFast.from_pretrained(
                model_dir,
                use_fast=True,
                local_files_only=True,
            )
            log_cb(f"LayoutLMv3 tokenizer ready ({time.perf_counter() - t0:.1f}s)")
            with open(os.path.join(model_dir, "label_list.json"), "r", encoding="utf-8") as f:
                label_list = json.load(f)
            cfg = {}
            cfg_path = os.path.join(model_dir, "layoutlmv3_fontgray_config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            _llmv3.session = session
            _llmv3.tokenizer = tokenizer
            _llmv3.label_list = label_list
            _llmv3.cfg = cfg
            _llmv3.loaded = True
            log_cb(
                f"LayoutLMv3 ONNX ready ({len(label_list)} labels, "
                f"total={time.perf_counter() - t_total:.1f}s)"
            )
            return True
        except Exception as e:
            log_cb(f"LayoutLMv3 load failed: {e}")
            logger.exception("layoutlmv3 load")
            return False


def warmup_layoutlmv3_visual(log_cb: Optional[Callable[[str], None]] = None) -> bool:
    log_cb = log_cb or (lambda m: None)
    with _llmv3_visual.lock:
        if _llmv3_visual.loaded:
            return True
        model_dir = _layoutlmv3_visual_model_dir()
        onnx_path = _layoutlmv3_visual_onnx_path()
        if not os.path.isdir(model_dir):
            log_cb(f"LayoutLMv3 visual model dir missing: {model_dir}")
            return False
        if not os.path.exists(onnx_path):
            log_cb(f"LayoutLMv3 visual ONNX missing: {onnx_path}")
            return False
        try:
            t_total = time.perf_counter()
            t0 = time.perf_counter()
            log_cb("Loading LayoutLMv3 visual runtime imports...")
            import onnxruntime as ort
            from transformers.models.layoutlmv3.tokenization_layoutlmv3_fast import (
                LayoutLMv3TokenizerFast,
            )
            log_cb(f"LayoutLMv3 visual runtime imports ready ({time.perf_counter() - t0:.1f}s)")

            log_cb(f"Loading LayoutLMv3 visual ONNX int8 ({os.path.basename(onnx_path)})...")
            sess_opts = ort.SessionOptions()
            default_threads = max(1, min(10, (os.cpu_count() or 4) // 2))
            raw_threads = (
                os.environ.get("LAYOUTLMV3_VISUAL_ONNX_THREADS")
                or os.environ.get("LAYOUTLMV3_ONNX_THREADS")
                or default_threads
            )
            try:
                threads = int(raw_threads)
            except (TypeError, ValueError):
                threads = default_threads
            sess_opts.intra_op_num_threads = max(1, threads)
            sess_opts.inter_op_num_threads = 1
            sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
            t0 = time.perf_counter()
            session = ort.InferenceSession(
                onnx_path, sess_options=sess_opts,
                providers=["CPUExecutionProvider"],
            )
            log_cb(f"LayoutLMv3 visual ONNX session ready ({time.perf_counter() - t0:.1f}s)")
            t0 = time.perf_counter()
            tokenizer = LayoutLMv3TokenizerFast.from_pretrained(
                model_dir,
                use_fast=True,
                local_files_only=True,
            )
            image_processor = _NumpyLayoutLMv3ImageProcessor.from_pretrained(model_dir)
            log_cb(f"LayoutLMv3 visual tokenizer/processor ready ({time.perf_counter() - t0:.1f}s)")
            with open(os.path.join(model_dir, "label_list.json"), "r", encoding="utf-8") as f:
                label_list = json.load(f)
            cfg = {}
            cfg_path = os.path.join(model_dir, "layoutlmv3_fontgray_visual_config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            _llmv3_visual.session = session
            _llmv3_visual.tokenizer = tokenizer
            _llmv3_visual.image_processor = image_processor
            _llmv3_visual.label_list = label_list
            _llmv3_visual.id2label = {idx: label for idx, label in enumerate(label_list)}
            _llmv3_visual.cfg = cfg
            _llmv3_visual.loaded = True
            log_cb(
                f"LayoutLMv3 visual ONNX ready ({len(label_list)} labels, "
                f"threads={max(1, threads)}, total={time.perf_counter() - t_total:.1f}s)"
            )
            return True
        except Exception as e:
            log_cb(f"LayoutLMv3 visual load failed: {e}")
            logger.exception("layoutlmv3 visual load")
            return False


def _bio_tags_to_field_instances(tags_per_word: list[str], words_per_page: list[list[dict]],
                                   page_index_offset: int = 0) -> list[dict]:
    """Group consecutive B-/I- tags into field instances per page."""
    instances: list[dict] = []
    field_counter = 0
    for page_idx, (word_tags, page_words) in enumerate(zip(tags_per_word, words_per_page)):
        current_label: str | None = None
        current_words: list[dict] = []
        for tag, w in zip(word_tags, page_words):
            if tag == "O" or not tag:
                if current_label and current_words:
                    field_counter += 1
                    instances.append(_build_instance(field_counter, current_label,
                                                     page_idx + page_index_offset, current_words))
                current_label = None
                current_words = []
                continue
            if tag.startswith("B-"):
                if current_label and current_words:
                    field_counter += 1
                    instances.append(_build_instance(field_counter, current_label,
                                                     page_idx + page_index_offset, current_words))
                current_label = tag[2:]
                current_words = [w]
            elif tag.startswith("I-"):
                lbl = tag[2:]
                if current_label == lbl:
                    current_words.append(w)
                else:
                    # Treat orphan I- as B-
                    if current_label and current_words:
                        field_counter += 1
                        instances.append(_build_instance(field_counter, current_label,
                                                         page_idx + page_index_offset, current_words))
                    current_label = lbl
                    current_words = [w]
        if current_label and current_words:
            field_counter += 1
            instances.append(_build_instance(field_counter, current_label,
                                             page_idx + page_index_offset, current_words))
    return instances


def _build_instance(field_idx: int, label: str, page_index: int, words: list[dict]) -> dict:
    words = _ordered_words_for_reading(words)
    word_ids = [w.get("id") or w.get("word_id") for w in words]
    line_ids = []
    seen_lines: set[str] = set()
    for w in words:
        line_id = w.get("line_id")
        if line_id is None:
            continue
        line_id = str(line_id)
        if line_id in seen_lines:
            continue
        seen_lines.add(line_id)
        line_ids.append(line_id)
    bbox = _merge_bboxes([_word_bbox(w) for w in words])
    text = _text_from_ordered_words(words)
    return {
        "field_id": f"f{field_idx}",
        "label": label,
        "page_index": page_index,
        "line_ids": line_ids,
        "word_ids": word_ids,
        "bbox": bbox,
        "text": text,
    }


def _line_number_from_id(line_id: object) -> int | None:
    if line_id is None:
        return None
    match = _LINE_NUM_RE.search(str(line_id))
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _ordered_words_for_reading(words: list[dict]) -> list[dict]:
    def key(item):
        fallback_order, word = item
        bbox = _word_bbox(word)
        line_no = _line_number_from_id(word.get("line_id"))
        if line_no is None:
            cy = (float(bbox[1]) + float(bbox[3])) / 2.0
            line_key = (1, round(cy / 10.0))
        else:
            line_key = (0, line_no)
        try:
            word_order = int(word.get("order", fallback_order) or fallback_order)
        except (TypeError, ValueError):
            word_order = fallback_order
        return (line_key[0], line_key[1], float(bbox[0]), word_order, fallback_order)

    return [word for _idx, word in sorted(enumerate(words or []), key=key)]


def _text_from_ordered_words(words: list[dict]) -> str:
    parts: list[str] = []
    last_line_id = None
    for word in words:
        text = str(word.get("text") or word.get("ocr_text") or "").strip()
        if not text:
            continue
        line_id = word.get("line_id")
        if parts and line_id is not None and last_line_id is not None and str(line_id) != str(last_line_id):
            parts.append("\n")
        elif parts and parts[-1] != "\n":
            parts.append(" ")
        parts.append(text)
        last_line_id = line_id
    return "".join(parts).strip()


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


def _resolve_selected_pages(canonical: dict, override: list[int] | None) -> set[int]:
    """Decide which page indices to feed to the model.

    Priority order:
      1. Caller-supplied `override` list (used when user manually picks pages)
      2. `scanindex.core.kie.labeling_workspace.analyze_page_selection` heuristic
         (typically first page + last non-appendix page)
    """
    if override is not None:
        return set(int(p) for p in override)
    from scanindex.core.kie.labeling_workspace import analyze_page_selection
    sel = analyze_page_selection(canonical) or {}
    sp = sel.get("selected_pages") or []
    if not sp:
        raise RuntimeError("KIE page selection returned no selected_pages.")
    return set(int(p) for p in sp)


def _run_layoutlmv3(canonical_json_path: str,
                     selected_pages: list[int] | None = None) -> dict:
    """LayoutLMv3 inference on the pages chosen by `selected_pages` (or, when
    None, the heuristic in `scanindex.core.kie.labeling_workspace.analyze_page_selection`).

    Skipping irrelevant pages (e.g. body content in long reports) avoids paying
    inference cost for pages that don't contain header/signer fields and also
    cuts down false-positive predictions on body text."""
    if not _llmv3.loaded:
        warmup_layoutlmv3()
    if not _llmv3.loaded:
        raise RuntimeError("LayoutLMv3 text-only model is not loaded.")

    with open(canonical_json_path, "r", encoding="utf-8") as f:
        canonical = json.load(f)

    pages = canonical.get("pages", [])
    page_filter = _resolve_selected_pages(canonical, selected_pages)
    tags_per_page: list[list[str]] = []
    words_per_page: list[list[dict]] = []

    for pi, page in enumerate(pages):
        words = page.get("words") or []
        words_per_page.append(words)
        if pi not in page_filter:
            tags_per_page.append(["O"] * len(words))
            continue
        if not words:
            raise RuntimeError(f"LayoutLMv3 selected page {pi} has no OCR words.")
        line_lookup = {}
        for ln in page.get("lines") or []:
            lid = ln.get("id") or ln.get("line_id")
            if lid:
                line_lookup[str(lid)] = ln
        tags = _layoutlmv3_predict_page(page, words, line_lookup)
        if len(tags) != len(words):
            tags = (tags + ["O"] * len(words))[:len(words)]
        tags_per_page.append(tags)

    instances = _bio_tags_to_field_instances(tags_per_page, words_per_page)
    payload = {
        "schema": "kie_vi_official_v3",
        "source": "layoutlmv3",
        "selected_pages": sorted(page_filter),
        "field_instances": instances,
        "relations": [],
    }
    from scanindex.core.kie.postprocess import apply_layoutlmv3_schema_postprocess
    return apply_layoutlmv3_schema_postprocess(canonical, payload)


def _layoutlmv3_predict_page(page: dict, words: list[dict],
                              line_lookup: dict | None = None) -> list[str]:
    """Run LayoutLMv3 token classification on one page; return per-word BIO tag.

    The fontgray-trained variant requires `token_type_ids` (style emphasis
    flag per token, 0 or 1, derived from page-relative font_size, fg_gray,
    and word_height — see `train_layoutlmv3.common.style_emphasis_ids`)."""
    import numpy as np
    tokenizer = _llmv3.tokenizer
    session = _llmv3.session
    label_list = _llmv3.label_list or []
    cfg = _llmv3.cfg or {}
    max_length = int(cfg.get("max_length", 512))

    # Normalize against PDF-point width (training uses `page.width`, not
    # `render_width`). Word bboxes in canonical JSON are in PDF points too.
    page_w = float(page.get("width") or page.get("page_width")
                    or page.get("render_width") or 1.0)
    page_h = float(page.get("height") or page.get("page_height")
                    or page.get("render_height") or 1.0)

    word_texts = [(w.get("text") or "").strip() or " " for w in words]
    word_bboxes_norm = []
    for w in words:
        x0, y0, x1, y1 = _word_bbox(w)
        word_bboxes_norm.append([
            int(min(1000, max(0, round(x0 * 1000.0 / max(page_w, 1))))),
            int(min(1000, max(0, round(y0 * 1000.0 / max(page_h, 1))))),
            int(min(1000, max(0, round(x1 * 1000.0 / max(page_w, 1))))),
            int(min(1000, max(0, round(y1 * 1000.0 / max(page_h, 1))))),
        ])

    # Pre-compute per-word style/line ids used for token_type_ids per chunk.
    word_style = _word_style_type_ids(words, line_lookup or {})

    # Tokenize with chunking — long pages produce multiple overlapping chunks
    # (matches training-time stride=128 behaviour). Without this, pages with
    # >~500 words get truncated and signers/footers at the end are missed.
    try:
        stride = int(os.environ.get("LAYOUTLMV3_STRIDE", cfg.get("stride", 128)))
    except (TypeError, ValueError):
        stride = int(cfg.get("stride", 128))
    encoding = tokenizer(
        text=word_texts,
        boxes=word_bboxes_norm,
        truncation=True,
        max_length=max_length,
        stride=stride,
        padding="max_length",
        return_overflowing_tokens=True,
        return_tensors="np",
    )

    expected_inputs = {ip.name for ip in session.get_inputs()}
    n_chunks = encoding["input_ids"].shape[0]

    # Per-word vote tracking: pick the FIRST non-O tag seen for a word across
    # chunks (training code uses similar "first-seen" logic).
    per_word_tag: list[str] = ["O"] * len(words)
    word_seen: set[int] = set()

    for chunk_idx in range(n_chunks):
        word_ids = encoding.word_ids(batch_index=chunk_idx)
        feeds = {
            "input_ids": encoding["input_ids"][chunk_idx:chunk_idx + 1].astype("int64"),
            "attention_mask": encoding["attention_mask"][chunk_idx:chunk_idx + 1].astype("int64"),
            "bbox": encoding["bbox"][chunk_idx:chunk_idx + 1].astype("int64"),
        }
        if "pixel_values" in expected_inputs:
            feeds["pixel_values"] = np.zeros((1, 3, 224, 224), dtype="float32")
        if "token_type_ids" in expected_inputs:
            seq_len = encoding["input_ids"].shape[1]
            token_type_ids = np.zeros((1, seq_len), dtype="int64")
            for tok_idx, w_idx in enumerate(word_ids):
                if w_idx is not None and 0 <= w_idx < len(word_style):
                    token_type_ids[0, tok_idx] = int(word_style[w_idx])
            feeds["token_type_ids"] = token_type_ids

        outputs = session.run(None, feeds)
        pred_ids = outputs[0][0].argmax(axis=-1).tolist()

        # First non-O wins per word across chunks; if all O, last write keeps O
        for tok_idx, w_idx in enumerate(word_ids):
            if w_idx is None:
                continue
            if w_idx in word_seen and per_word_tag[w_idx] != "O":
                continue  # already have a meaningful tag for this word
            label_idx = pred_ids[tok_idx]
            if 0 <= label_idx < len(label_list) and 0 <= w_idx < len(per_word_tag):
                per_word_tag[w_idx] = label_list[label_idx]
                word_seen.add(w_idx)
    return per_word_tag


def _word_style_type_ids(words: list[dict], line_lookup: dict) -> list[int]:
    """Compute the same combined font/gray/height + line-position type id
    used by the LayoutLMv3 style training pipeline."""
    font_sizes: list[float] = []
    fg_grays: list[float] = []
    heights: list[float] = []
    for w in words:
        line_id = w.get("line_id")
        line = line_lookup.get(str(line_id)) if line_id else None
        fs = 0.0
        if line:
            try:
                fs = float(line.get("font_size", 0.0) or 0.0)
            except (TypeError, ValueError):
                fs = 0.0
        if fs <= 0:
            try:
                fs = float(w.get("font_size", 0.0) or 0.0)
            except (TypeError, ValueError):
                fs = 0.0
        font_sizes.append(fs)
        try:
            fg_grays.append(float(w.get("fg_gray", -1.0)))
        except (TypeError, ValueError):
            fg_grays.append(-1.0)
        # word_height ≈ word's bbox height
        try:
            h = float(w.get("h", 0.0) or 0.0)
        except (TypeError, ValueError):
            h = 0.0
        if h <= 0:
            bb = _word_bbox(w)
            if bb and len(bb) >= 4:
                h = max(0.0, bb[3] - bb[1])
        heights.append(h)

    line_ids = [w.get("line_id") for w in words]
    style_ids = _layoutlmv3_style_type_ids(font_sizes, fg_grays, heights)
    line_bucket_ids = _layoutlmv3_line_position_bucket_ids(line_ids)
    return _layoutlmv3_combine_style_line_type_ids(style_ids, line_bucket_ids)


def _median_float(values) -> float:
    clean: list[float] = []
    for value in values:
        try:
            clean.append(float(value))
        except (TypeError, ValueError):
            continue
    if not clean:
        return 0.0
    clean.sort()
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def _font_relative_group(font_size: float, median_font: float) -> int:
    if font_size <= 0 or median_font <= 0:
        return 0
    ratio = font_size / max(median_font, 1e-6)
    if ratio < 0.92:
        return 1
    if ratio > 1.08:
        return 3
    return 2


def _gray_relative_group(gray: float, median_gray: float) -> int:
    if gray < 0 or median_gray < 0:
        return 0
    delta = gray - median_gray
    if delta <= -14.0:
        return 1
    if delta >= 14.0:
        return 3
    return 2


def _height_group(height: float, median_height: float) -> int:
    if height <= 0 or median_height <= 0:
        return 0
    ratio = height / max(median_height, 1e-6)
    if ratio < 0.88:
        return 1
    if ratio > 1.12:
        return 3
    return 2


def _layoutlmv3_style_type_ids(
    font_size: list[float],
    fg_gray: list[float],
    word_height: list[float],
) -> list[int]:
    median_font = _median_float(v for v in font_size if float(v) > 0)
    median_gray = _median_float(v for v in fg_gray if 0 <= float(v) <= 255)
    median_height = _median_float(v for v in word_height if float(v) > 0)
    ids: list[int] = []
    for fs, gray, height in zip(font_size, fg_gray, word_height):
        rel = _font_relative_group(float(fs), median_font)
        g = _gray_relative_group(float(gray), median_gray)
        h = _height_group(float(height), median_height)
        ids.append(min(_STYLE_BASE_TYPE_VOCAB_SIZE - 1, rel * 16 + g * 4 + h))
    return ids


def _parse_line_index(line_id: Any) -> int | None:
    if line_id is None:
        return None
    match = _LINE_ID_EXACT_RE.match(str(line_id).strip())
    if not match:
        return None
    return int(match.group("line"))


def _line_bucket_from_rank(rank: int, line_count: int) -> int:
    if rank < 0:
        return 0
    if rank < _LINE_EXACT_PREFIX_BUCKETS:
        return 1 + rank
    tail_buckets = max(1, _LINE_POSITION_BUCKET_COUNT - 1 - _LINE_EXACT_PREFIX_BUCKETS)
    tail_count = max(1, line_count - _LINE_EXACT_PREFIX_BUCKETS)
    tail_rank = max(0, rank - _LINE_EXACT_PREFIX_BUCKETS)
    return 1 + _LINE_EXACT_PREFIX_BUCKETS + min(tail_buckets - 1, int(tail_rank * tail_buckets / tail_count))


def _layoutlmv3_line_position_bucket_ids(line_ids: list[Any]) -> list[int]:
    parsed = [_parse_line_index(line_id) for line_id in line_ids]
    known_indices = sorted({idx for idx in parsed if idx is not None})
    if known_indices:
        rank_by_index = {idx: rank for rank, idx in enumerate(known_indices)}
        line_count = len(known_indices)
        return [
            _line_bucket_from_rank(rank_by_index[idx], line_count) if idx is not None else 0
            for idx in parsed
        ]

    order_by_line_id: dict[str, int] = {}
    for line_id in line_ids:
        key = str(line_id or "").strip()
        if not key:
            continue
        if key not in order_by_line_id:
            order_by_line_id[key] = len(order_by_line_id)
    line_count = len(order_by_line_id)
    return [
        _line_bucket_from_rank(order_by_line_id[str(line_id).strip()], line_count)
        if str(line_id or "").strip() in order_by_line_id
        else 0
        for line_id in line_ids
    ]


def _layoutlmv3_combine_style_line_type_ids(style_ids: list[int], line_bucket_ids: list[int]) -> list[int]:
    out: list[int] = []
    for style_id, line_bucket_id in zip(style_ids, line_bucket_ids):
        style = min(_STYLE_BASE_TYPE_VOCAB_SIZE - 1, max(0, int(style_id)))
        line_bucket = min(_LINE_POSITION_BUCKET_COUNT - 1, max(0, int(line_bucket_id)))
        out.append(line_bucket * _STYLE_BASE_TYPE_VOCAB_SIZE + style)
    return out


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────

def _visual_page_image_from_payload(raw_page: dict[str, Any]) -> Path | None:
    for key in ("image_path", "render_path", "page_image", "image"):
        value = raw_page.get(key)
        if isinstance(value, str) and value.strip():
            path = Path(value)
            if path.exists():
                return path
    return None


def _visual_source_pdf_candidates(canonical_json: str | Path, canonical_payload: dict[str, Any]) -> list[Path]:
    path = Path(canonical_json)
    candidates: list[Path] = []
    for raw in (
        canonical_payload.get("input_path"),
        canonical_payload.get("pipeline", {}).get("ocr", {}).get("input_path"),
        canonical_payload.get("document", {}).get("source_path"),
    ):
        if raw:
            candidates.append(Path(raw))
    if path.name.endswith(".pdf.json"):
        candidates.append(Path(str(path)[:-5]))
    candidates.extend([
        path.with_suffix(""),
        path.with_name(path.stem + ".pdf"),
        path.with_name(path.stem.replace("_ocr", "") + ".pdf"),
    ])
    out: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def _render_or_load_visual_page_images(
    canonical_json_path: str | Path,
    canonical: dict,
    rows: list[dict[str, Any]],
    image_height: int,
) -> list[Any]:
    from PIL import Image
    import fitz  # type: ignore

    raw_page_by_index: dict[int, dict] = {}
    for fallback_index, page in enumerate(canonical.get("pages") or []):
        if isinstance(page, dict):
            try:
                page_index = int(page.get("page_index", fallback_index))
            except (TypeError, ValueError):
                page_index = fallback_index
            raw_page_by_index[page_index] = page

    pdf_path = None
    for candidate in _visual_source_pdf_candidates(canonical_json_path, canonical):
        if candidate.exists():
            pdf_path = candidate
            break

    pdf_doc = fitz.open(str(pdf_path)) if pdf_path else None
    images: list[Any] = []
    try:
        for row in rows:
            page_index = int(row["page_index"])
            raw_page = raw_page_by_index.get(page_index) or {}
            render_annots = bool(raw_page.get("kie_render_annots", True))
            source_image = _visual_page_image_from_payload(raw_page)
            if not render_annots and pdf_doc is not None:
                source_image = None
            if source_image is not None:
                with Image.open(source_image) as image:
                    image = image.convert("RGB")
                    if image_height > 0 and image.height != image_height:
                        width = max(1, int(round(image.width * image_height / image.height)))
                        image = image.resize((width, image_height), Image.Resampling.LANCZOS)
                    images.append(image.copy())
                continue
            if pdf_doc is None:
                raise RuntimeError(f"No source PDF/image found for {canonical_json_path} page={page_index}")
            if page_index < 0 or page_index >= pdf_doc.page_count:
                raise IndexError(f"page_index={page_index} outside PDF page_count={pdf_doc.page_count}: {pdf_path}")
            page = pdf_doc.load_page(page_index)
            scale = (float(image_height) / float(page.rect.height)) if image_height > 0 else 1.0
            pix = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                alpha=False,
                annots=render_annots,
            )
            images.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    finally:
        if pdf_doc is not None:
            pdf_doc.close()
    return images


def _visual_make_features(
    rows: list[dict[str, Any]],
    tokenizer: Any,
    max_length: int,
    stride: int,
    subword_label_strategy: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    features: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        style_ids = row.get("layoutlmv3_style_type_id") or [0] * len(row["tokens"])
        if len(style_ids) != len(row["tokens"]):
            style_ids = [0] * len(row["tokens"])
        enc = tokenizer(
            row["tokens"],
            boxes=row["bboxes"],
            truncation=True,
            max_length=max_length,
            stride=stride,
            return_overflowing_tokens=True,
            padding=False,
        )
        for chunk_index in range(len(enc["input_ids"])):
            word_ids = enc.word_ids(batch_index=chunk_index)
            token_type_ids: list[int] = []
            seen_words: set[int] = set()
            for word_id in word_ids:
                if word_id is None:
                    token_type_ids.append(0)
                    continue
                wid = int(word_id)
                token_type_ids.append(max(0, int(style_ids[wid])))
                if subword_label_strategy == "first" and wid in seen_words:
                    token_type_ids[-1] = 0
                seen_words.add(wid)
            features.append({
                "input_ids": enc["input_ids"][chunk_index],
                "attention_mask": enc["attention_mask"][chunk_index],
                "bbox": enc["bbox"][chunk_index],
                "token_type_ids": token_type_ids,
                "row_index": row_index,
            })
            metadata.append({
                "row_index": row_index,
                "chunk_index": chunk_index,
                "word_ids": word_ids,
                "doc_id": row["doc_id"],
                "page_index": row["page_index"],
            })
    return features, metadata


def _valid_bbox(bbox: list[float]) -> bool:
    return len(bbox) >= 4 and float(bbox[2]) > float(bbox[0]) and float(bbox[3]) > float(bbox[1])


def _normalize_visual_bbox(bbox: list[float], width: float, height: float) -> list[int]:
    width = max(float(width or 1.0), 1.0)
    height = max(float(height or 1.0), 1.0)
    return [
        int(min(1000, max(0, round(float(bbox[0]) * 1000.0 / width)))),
        int(min(1000, max(0, round(float(bbox[1]) * 1000.0 / height)))),
        int(min(1000, max(0, round(float(bbox[2]) * 1000.0 / width)))),
        int(min(1000, max(0, round(float(bbox[3]) * 1000.0 / height)))),
    ]


def _visual_rows_from_canonical(
    canonical_json_path: str | Path,
    canonical: dict[str, Any],
    selected_pages: set[int],
    doc_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fallback_index, page in enumerate(canonical.get("pages") or []):
        if not isinstance(page, dict):
            continue
        try:
            page_index = int(page.get("page_index", fallback_index))
        except (TypeError, ValueError):
            page_index = fallback_index
        if page_index not in selected_pages:
            continue

        page_w = float(page.get("width") or page.get("page_width") or page.get("render_width") or 1.0)
        page_h = float(page.get("height") or page.get("page_height") or page.get("render_height") or 1.0)
        line_lookup = {}
        for line in page.get("lines") or []:
            if not isinstance(line, dict):
                continue
            line_id = line.get("id") or line.get("line_id")
            if line_id:
                line_lookup[str(line_id)] = line

        row_words: list[dict] = []
        tokens: list[str] = []
        bboxes: list[list[int]] = []
        raw_bboxes: list[list[float]] = []
        word_ids: list[str] = []
        line_ids: list[str] = []
        for word_index, word in enumerate(page.get("words") or []):
            if not isinstance(word, dict):
                continue
            text = str(word.get("text") or word.get("ocr_text") or "").strip()
            bbox = [float(v) for v in _word_bbox(word)]
            if not text or not _valid_bbox(bbox):
                continue
            word_id = str(word.get("id") or word.get("word_id") or f"p{page_index}_w{word_index}")
            line_id = str(word.get("line_id") or "")
            row_word = dict(word)
            row_word["id"] = word_id
            if line_id:
                row_word["line_id"] = line_id
            row_words.append(row_word)
            tokens.append(text)
            bboxes.append(_normalize_visual_bbox(bbox, page_w, page_h))
            raw_bboxes.append(bbox)
            word_ids.append(word_id)
            line_ids.append(line_id)

        if not tokens:
            continue
        rows.append({
            "doc_id": doc_id,
            "page_id": page.get("page_id") or f"p{page_index}",
            "source_file": str(canonical_json_path),
            "split": "inference",
            "page_index": page_index,
            "tokens": tokens,
            "bboxes": bboxes,
            "raw_bboxes": raw_bboxes,
            "labels": ["O"] * len(tokens),
            "word_ids": word_ids,
            "line_ids": line_ids,
            "layoutlmv3_style_type_id": _word_style_type_ids(row_words, line_lookup),
            "page_width": page_w,
            "page_height": page_h,
            "_words": row_words,
        })
    return rows


def _visual_aggregate_logits_to_words(
    rows: list[dict[str, Any]],
    metadata: list[dict[str, Any]],
    logits: Any,
    id2label: dict[int, str],
) -> tuple[list[list[str]], list[list[float]]]:
    import numpy as np
    from collections import defaultdict

    raw = np.asarray(logits)
    raw = raw - np.max(raw, axis=-1, keepdims=True)
    probs = np.exp(raw)
    probs = probs / np.sum(probs, axis=-1, keepdims=True)

    row_word_probs: dict[tuple[int, int], list[Any]] = defaultdict(list)
    for chunk_index, meta in enumerate(metadata):
        for token_index, word_id in enumerate(meta.get("word_ids") or []):
            if word_id is None:
                continue
            row_word_probs[(int(meta["row_index"]), int(word_id))].append(probs[chunk_index, token_index])

    pred_labels: list[list[str]] = []
    pred_scores: list[list[float]] = []
    for row_index, row in enumerate(rows):
        labels: list[str] = []
        scores: list[float] = []
        for word_index in range(len(row["tokens"])):
            parts = row_word_probs.get((row_index, word_index))
            if not parts:
                labels.append("O")
                scores.append(0.0)
                continue
            mean_prob = np.mean(np.stack(parts, axis=0), axis=0)
            pred_id = int(np.argmax(mean_prob))
            labels.append(id2label.get(pred_id, "O"))
            scores.append(float(mean_prob[pred_id]))
        pred_labels.append(labels)
        pred_scores.append(scores)
    return pred_labels, pred_scores


def _visual_fields_from_predictions(
    rows: list[dict[str, Any]],
    labels: list[list[str]],
    scores: list[list[float]],
) -> dict[str, Any]:
    from collections import defaultdict

    fields: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def flush(row: dict[str, Any], label: str | None, indices: list[int], row_scores: list[float]) -> None:
        if not label or not indices:
            return
        words = [row["_words"][idx] for idx in indices]
        word_ids = [row["word_ids"][idx] for idx in indices]
        raw_boxes = [row["raw_bboxes"][idx] for idx in indices]
        conf_values = [float(row_scores[idx]) for idx in indices if idx < len(row_scores)]
        confidence = sum(conf_values) / len(conf_values) if conf_values else 0.0
        fields[label].append({
            "text": _text_from_ordered_words(words),
            "word_ids": word_ids,
            "bbox": _merge_bboxes(raw_boxes),
            "page_index": int(row["page_index"]),
            "confidence": confidence,
        })

    for row, row_labels, row_scores in zip(rows, labels, scores):
        current_label: str | None = None
        current_indices: list[int] = []
        for idx, tag in enumerate(row_labels):
            tag = str(tag or "O")
            if tag == "O":
                flush(row, current_label, current_indices, row_scores)
                current_label = None
                current_indices = []
                continue
            if tag.startswith("B-"):
                flush(row, current_label, current_indices, row_scores)
                current_label = tag[2:]
                current_indices = [idx]
                continue
            if tag.startswith("I-"):
                label = tag[2:]
                if current_label == label:
                    current_indices.append(idx)
                else:
                    flush(row, current_label, current_indices, row_scores)
                    current_label = label
                    current_indices = [idx]
        flush(row, current_label, current_indices, row_scores)

    return {"fields": dict(fields), "fragmentation": 0}


def _pad_1d(values: list[int], length: int, pad: int):
    import numpy as np

    out = np.full((length,), pad, dtype=np.int64)
    take = min(length, len(values))
    if take:
        out[:take] = np.asarray(values[:take], dtype=np.int64)
    return out


def _pad_bbox(values: list[list[int]], length: int):
    import numpy as np

    out = np.zeros((length, 4), dtype=np.int64)
    take = min(length, len(values))
    if take:
        out[:take] = np.asarray(values[:take], dtype=np.int64)
    return out


def _layoutlmv3_visual_payload_from_fields(
    decoded: dict[str, Any],
    rows: list[dict[str, Any]],
    selected_pages: set[int],
) -> dict:
    line_by_word: dict[str, str] = {}
    for row in rows:
        for word_id, line_id in zip(row.get("word_ids", []), row.get("line_ids", [])):
            if word_id and line_id:
                line_by_word[str(word_id)] = str(line_id)

    instances: list[dict] = []
    field_idx = 0
    for label, field_items in (decoded.get("fields") or {}).items():
        for item in field_items or []:
            field_idx += 1
            word_ids = [str(wid) for wid in item.get("word_ids", []) if wid]
            line_ids: list[str] = []
            seen_lines: set[str] = set()
            for word_id in word_ids:
                line_id = line_by_word.get(word_id)
                if line_id and line_id not in seen_lines:
                    seen_lines.add(line_id)
                    line_ids.append(line_id)
            try:
                confidence_value = float(item.get("confidence"))
            except (TypeError, ValueError):
                confidence_value = 0.0
            instances.append({
                "field_id": f"v{field_idx}",
                "label": label,
                "page_index": int(item.get("page_index", 0) or 0),
                "line_ids": line_ids,
                "word_ids": word_ids,
                "bbox": list(item.get("bbox") or [0.0, 0.0, 0.0, 0.0]),
                "text": item.get("text", ""),
                "confidence": confidence_value,
            })
    return {
        "schema": "kie_vi_official_v3",
        "source": "layoutlmv3_visual",
        "selected_pages": sorted(selected_pages),
        "field_instances": instances,
        "relations": [],
        "fragmentation": decoded.get("fragmentation", 0),
    }


def _run_layoutlmv3_visual(canonical_json_path: str,
                           selected_pages: list[int] | None = None) -> dict:
    if not _llmv3_visual.loaded:
        warmup_layoutlmv3_visual()
    if not _llmv3_visual.loaded:
        raise RuntimeError("LayoutLMv3 visual model is not loaded.")

    import numpy as np

    with open(canonical_json_path, "r", encoding="utf-8") as f:
        canonical = json.load(f)

    page_filter = _resolve_selected_pages(canonical, selected_pages)
    stem = os.path.splitext(os.path.basename(canonical_json_path))[0]
    rows = _visual_rows_from_canonical(canonical_json_path, canonical, page_filter, stem)
    if not rows:
        raise RuntimeError(
            f"LayoutLMv3 visual generated no input rows for {canonical_json_path}; "
            f"selected_pages={sorted(page_filter)}."
        )

    cfg = _llmv3_visual.cfg or {}
    try:
        image_height = int(os.environ.get("LAYOUTLMV3_VISUAL_IMAGE_HEIGHT", cfg.get("image_height", 896)))
    except (TypeError, ValueError):
        image_height = 896
    try:
        stride = int(os.environ.get("LAYOUTLMV3_STRIDE", cfg.get("stride", 128)))
    except (TypeError, ValueError):
        stride = int(cfg.get("stride", 128))
    max_length = int(cfg.get("max_length", 512))
    subword_strategy = str(cfg.get("subword_label_strategy", "same"))

    images = _render_or_load_visual_page_images(canonical_json_path, canonical, rows, image_height)
    pixel_values = _llmv3_visual.image_processor(images=images, return_tensors="np")["pixel_values"].astype(np.float32)
    features, metadata = _visual_make_features(rows, _llmv3_visual.tokenizer, max_length, stride, subword_strategy)
    if not features:
        raise RuntimeError(
            f"LayoutLMv3 visual generated no token features for {canonical_json_path}; "
            f"selected_pages={sorted(page_filter)}."
        )

    input_names = {item.name for item in _llmv3_visual.session.get_inputs()}
    pad_id = int(_llmv3_visual.tokenizer.pad_token_id or 1)
    try:
        batch_size = int(os.environ.get("LAYOUTLMV3_VISUAL_BATCH_SIZE", "1"))
    except (TypeError, ValueError):
        batch_size = 1
    logits_parts = []
    for start in range(0, len(features), max(1, batch_size)):
        chunk = features[start:start + max(1, batch_size)]
        row_indices = [int(item["row_index"]) for item in chunk]
        candidate = {
            "input_ids": np.stack([_pad_1d(item["input_ids"], max_length, pad_id) for item in chunk], axis=0),
            "attention_mask": np.stack([_pad_1d(item["attention_mask"], max_length, 0) for item in chunk], axis=0),
            "bbox": np.stack([_pad_bbox(item["bbox"], max_length) for item in chunk], axis=0),
            "token_type_ids": np.stack([_pad_1d(item["token_type_ids"], max_length, 0) for item in chunk], axis=0),
            "pixel_values": np.stack([pixel_values[row_index] for row_index in row_indices], axis=0),
        }
        feeds = {key: value for key, value in candidate.items() if key in input_names}
        logits_parts.append(_llmv3_visual.session.run(None, feeds)[0])

    logits = np.concatenate(logits_parts, axis=0)
    pred_labels, pred_scores = _visual_aggregate_logits_to_words(
        rows,
        metadata,
        logits,
        _llmv3_visual.id2label or {},
    )
    decoded = _visual_fields_from_predictions(rows, pred_labels, pred_scores)
    payload = _layoutlmv3_visual_payload_from_fields(decoded, rows, page_filter)
    from scanindex.core.kie.postprocess import apply_layoutlmv3_schema_postprocess
    return apply_layoutlmv3_schema_postprocess(canonical, payload)


def warmup_kie(mode: str, log_cb: Optional[Callable[[str], None]] = None) -> bool:
    """Pre-load the chosen KIE backend. Idempotent and thread-safe."""
    mode = normalize_kie_mode(mode)
    if mode == KIE_MODE_LAYOUTLMV3:
        return warmup_layoutlmv3(log_cb)
    if mode == KIE_MODE_LAYOUTLMV3_VISUAL:
        return warmup_layoutlmv3_visual(log_cb)
    raise AssertionError(f"Unhandled KIE mode: {mode}")


def is_kie_ready(mode: str) -> bool:
    mode = normalize_kie_mode(mode)
    if mode == KIE_MODE_LAYOUTLMV3:
        return _llmv3.loaded
    if mode == KIE_MODE_LAYOUTLMV3_VISUAL:
        return _llmv3_visual.loaded
    raise AssertionError(f"Unhandled KIE mode: {mode}")


def release_kie(mode: str | None = None) -> None:
    """Drop loaded model state to free RAM. mode=None releases both."""
    import gc
    normalized = normalize_kie_mode(mode) if mode is not None else None
    if normalized in (None, KIE_MODE_LAYOUTLMV3):
        with _llmv3.lock:
            _llmv3.session = None
            _llmv3.tokenizer = None
            _llmv3.label_list = None
            _llmv3.cfg = None
            _llmv3.loaded = False
    if normalized in (None, KIE_MODE_LAYOUTLMV3_VISUAL):
        with _llmv3_visual.lock:
            _llmv3_visual.session = None
            _llmv3_visual.tokenizer = None
            _llmv3_visual.image_processor = None
            _llmv3_visual.label_list = None
            _llmv3_visual.id2label = None
            _llmv3_visual.cfg = None
            _llmv3_visual.loaded = False
    gc.collect()


def extract_metadata_kie(canonical_json_path: str,
                           mode: str,
                           selected_pages: list[int] | None = None) -> dict:
    """Run KIE on a canonical OCR JSON. When `selected_pages` is omitted the
    page-selection heuristic chooses the first page + last non-appendix page."""
    mode = normalize_kie_mode(mode)
    if mode == KIE_MODE_LAYOUTLMV3:
        return _run_layoutlmv3(canonical_json_path, selected_pages=selected_pages)
    if mode == KIE_MODE_LAYOUTLMV3_VISUAL:
        return _run_layoutlmv3_visual(canonical_json_path, selected_pages=selected_pages)
    raise AssertionError(f"Unhandled KIE mode: {mode}")
