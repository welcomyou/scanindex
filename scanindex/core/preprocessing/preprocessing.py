
import os
from functools import lru_cache
from typing import Tuple, Optional

import fitz  # PyMuPDF
import cv2
import numpy as np
import shutil

try:
    import onnxruntime as ort
except Exception:
    ort = None

from scanindex.infra.paths import get_base_dir

APP_BASE_DIR = get_base_dir()

def has_text_content(input_path: str) -> bool:
    """Check if PDF has searchable text."""
    try:
        doc = fitz.open(input_path)
        has_text = False
        for page in doc:
            if len(page.get_text().strip()) > 0:
                has_text = True
                break
        doc.close()
        return has_text
    except:
        return True # Assume yes to be safe


# ── PDF type classification ──────────────────────────────────────────

# Keywords in producer/creator that indicate digital (non-scan) origin
_DIGITAL_PRODUCERS = {
    "microsoft", "word", "libreoffice", "openoffice", "wps",
    "google", "acrobat", "pdflatex", "xelatex", "lualatex",
    "quartz", "cairo", "skia", "chrome", "firefox", "webkit",
    "fpdf", "reportlab", "typst", "prince", "weasyprint",
    "docx", "xlsx", "pptx",
}

# Producers / creators that strongly indicate scan workflows or OCR overlays.
_SCAN_PRODUCERS = {
    "kodak", "smart touch", "scanner", "scan", "scandall", "fujitsu",
    "canon", "ricoh", "xerox", "epson", "konica", "minolta", "brother",
    "abbyy", "tesseract", "gdpicture", "camscanner", "ocr",
}

# Font name substrings that indicate OCR-generated text (not real text)
_OCR_FONT_MARKERS = {"ocr", "gdpicture", "tesseract", "abbyy", "ocrb"}
_FULL_PAGE_IMAGE_THRESHOLD = 0.7
_MEANINGFUL_TEXT_WORDS = 20
_ORIENTATION_ANALYSIS_MAX_SIDE = 1280
_ORIENTATION_CLASSIFIER_PATH = os.path.join(
    APP_BASE_DIR, "models", "orientation", "PP-LCNet_x1_0_doc_ori.onnx"
)
_ORIENTATION_CLASSIFIER_LABELS = ("0", "90", "180", "270")
_ORIENTATION_CLASSIFIER_RESIZE_SHORT = 256
_ORIENTATION_CLASSIFIER_CROP_SIZE = 224
_ORIENTATION_CLASSIFIER_MIN_SCORE = 0.70
_ORIENTATION_CLASSIFIER_MIN_MARGIN = 0.35
_ORIENTATION_CLASSIFIER_THREADS = min(8, max(1, os.cpu_count() or 1))
_ORIENTATION_UPRIGHT_MIN_SCORE = 0.30
_ORIENTATION_UPRIGHT_STRONG_SCORE = 0.65
_ORIENTATION_UPRIGHT_MIN_IMPROVEMENT = 0.15
_ORIENTATION_UPRIGHT_MIN_MARGIN = 0.05
_ORIENTATION_GUARD_MAX_SIDE = 1200
_ORIENTATION_GUARD_SPARSE_FG_RATIO = 0.025
_ORIENTATION_GUARD_SPARSE_BBOX_COVERAGE = 0.25
_ORIENTATION_GUARD_RULED_HORIZONTAL_SHARE = 0.25

# ── Quality-preserving preprocessing (v2) ─────────────────────────────
_DESKEW_THRESHOLD_DEG = 0.5         # below this magnitude → no deskew applied
_DESKEW_KEEP_CANVAS_MAX_DEG = 3.0   # |skew| <= this → Option B keep-canvas
_DOMINANT_IMAGE_COVERAGE = 0.90     # displayed bbox area / page area
_FALLBACK_RASTER_DPI = 300          # vector / multi-image pages without dominant image
_ANALYSIS_DPI = 200                 # low-res preview for orientation/deskew detection


def classify_pdf(input_path: str, sample_pages: int = 3) -> str:
    """
    Classify a PDF into one of:
      'digital'       - text-native (exported from Word/Writer/etc.), skip OCR
      'scan_no_text'  - pure image scan, needs OCR
      'scan_ocr_low'  - image scan with existing OCR text, still re-OCR
      'scan_ocr_ok'   - reserved for future use

    Effective policy:
      1. Near full-page raster image -> scan-family.
         - no meaningful text  -> scan_no_text
         - meaningful text     -> scan_ocr_low
         Both cases should re-OCR downstream.
      2. Only pages without a dominant full-page image are eligible to be
         classified as digital.
      3. "Has text" alone is not enough; the text layer must be meaningful
         native text rather than OCR overlay noise.
    """
    try:
        doc = fitz.open(input_path)
        if doc.is_encrypted:
            doc.close()
            return "scan_no_text"
    except Exception:
        return "scan_no_text"  # can't open → assume worst case

    n_pages = len(doc)
    if n_pages == 0:
        doc.close()
        return "scan_no_text"

    # ── Step 1: Metadata check ───────────────────────────────────────
    meta = doc.metadata or {}
    producer = (meta.get("producer") or "").lower()
    creator = (meta.get("creator") or "").lower()
    meta_text = f"{producer} {creator}"
    meta_is_digital = any(kw in meta_text for kw in _DIGITAL_PRODUCERS)
    meta_is_scan = any(kw in meta_text for kw in _SCAN_PRODUCERS)

    # ── Step 2: Sample pages ─────────────────────────────────────────
    pages_to_check = min(sample_pages, n_pages)
    digital_pages = 0
    scan_with_text_pages = 0
    scan_no_text_pages = 0

    for pi in range(pages_to_check):
        page = doc[pi]
        page_area = page.rect.width * page.rect.height
        if page_area <= 0:
            continue

        # 2a. Image coverage
        max_coverage = 0.0
        for info in page.get_image_info():
            bbox = fitz.Rect(info["bbox"])
            coverage = (bbox.width * bbox.height) / page_area
            if coverage > max_coverage:
                max_coverage = coverage

        has_fullpage_image = max_coverage > _FULL_PAGE_IMAGE_THRESHOLD

        # 2b. Text content
        text = page.get_text().strip()
        word_count = len(text.split()) if text else 0
        has_meaningful_text = word_count > _MEANINGFUL_TEXT_WORDS

        # 2c. Font analysis
        fonts = page.get_fonts()
        has_ocr_font = False
        has_real_font = False
        for f in fonts:
            basefont = (f[3] or "").lower()
            if any(m in basefont for m in _OCR_FONT_MARKERS):
                has_ocr_font = True
            elif basefont:
                has_real_font = True

        # 2d. Content stream: real text operations?
        has_text_ops = False
        try:
            contents = page.get_contents()
            if contents:
                raw = doc.xref_stream(contents[0])
                if raw:
                    stream = raw.decode("latin-1", errors="replace")
                    # BT ... Tj/TJ ... ET = real text rendering
                    has_text_ops = (
                        stream.count("BT") > 0
                        and (stream.count("Tj") + stream.count("TJ")) > 0
                    )
        except Exception:
            pass

        # ── Classify this page ───────────────────────────────────────
        # Primary scan signal: a near full-page raster image.
        # If meaningful text also exists, treat it as OCR overlay / existing text layer.
        # This must beat BT/Tj because many scanner workflows embed OCR text directly
        # in the page content stream while still being scan PDFs.
        if has_fullpage_image:
            if has_meaningful_text:
                scan_with_text_pages += 1
            else:
                scan_no_text_pages += 1
        else:
            # No dominant raster background.
            if has_text_ops or has_meaningful_text:
                if meta_is_scan and (has_ocr_font or not has_real_font):
                    scan_with_text_pages += 1
                elif has_ocr_font and not meta_is_digital:
                    scan_with_text_pages += 1
                else:
                    digital_pages += 1
            else:
                scan_no_text_pages += 1

    doc.close()

    # ── Final verdict (majority vote) ────────────────────────────────
    if digital_pages > 0 and digital_pages >= scan_with_text_pages + scan_no_text_pages:
        return "digital"

    # If metadata says digital but pages look like scans, trust the pages
    # (e.g. "Print to PDF" creates image-based PDFs from digital sources)

    if scan_no_text_pages >= scan_with_text_pages:
        return "scan_no_text"

    # Has OCR text from previous engine — check quality
    return "scan_ocr_low"

def _to_rgb(image: np.ndarray) -> np.ndarray:
    if len(image.shape) == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)


def _resize_for_orientation_analysis(
    image: np.ndarray, max_side: int = _ORIENTATION_ANALYSIS_MAX_SIDE
) -> np.ndarray:
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return image
    scale = max_side / float(longest)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)


def _rotate_right_angle(image: np.ndarray, angle: int) -> np.ndarray:
    angle = int(angle) % 360
    if angle == 0:
        return image
    if angle == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if angle == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if angle == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return rotate_image(image, -angle)


@lru_cache(maxsize=8)
def _get_orientation_classifier_session(num_threads: int = _ORIENTATION_CLASSIFIER_THREADS):
    if ort is None:
        return None
    if not os.path.exists(_ORIENTATION_CLASSIFIER_PATH):
        return None
    try:
        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = max(1, int(num_threads))
        session_options.inter_op_num_threads = 1
        session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return ort.InferenceSession(
            _ORIENTATION_CLASSIFIER_PATH,
            sess_options=session_options,
            providers=["CPUExecutionProvider"],
        )
    except Exception:
        return None


def _prepare_orientation_classifier_input(image: np.ndarray) -> np.ndarray:
    rgb = _to_rgb(image)
    h, w = rgb.shape[:2]
    short = _ORIENTATION_CLASSIFIER_RESIZE_SHORT
    if h < w:
        new_h = short
        new_w = int(round(w * short / float(h)))
    else:
        new_w = short
        new_h = int(round(h * short / float(w)))
    resized = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    crop = _ORIENTATION_CLASSIFIER_CROP_SIZE
    top = max((new_h - crop) // 2, 0)
    left = max((new_w - crop) // 2, 0)
    cropped = resized[top : top + crop, left : left + crop]
    normalized = cropped.astype("float32") / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype="float32")
    std = np.array([0.229, 0.224, 0.225], dtype="float32")
    normalized = (normalized - mean) / std
    return np.transpose(normalized, (2, 0, 1))[None, ...].astype("float32")


def _normalize_orientation_classifier_scores(raw_scores: np.ndarray) -> np.ndarray:
    scores = np.asarray(raw_scores, dtype="float32").reshape(-1)
    if (
        scores.size > 0
        and np.all(scores >= 0.0)
        and np.all(scores <= 1.0)
        and 0.95 <= float(scores.sum()) <= 1.05
    ):
        return scores
    scores = scores - np.max(scores)
    exp_scores = np.exp(scores)
    return exp_scores / np.sum(exp_scores)


def _resize_for_orientation_guard(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    max_side = max(h, w)
    if max_side <= _ORIENTATION_GUARD_MAX_SIDE:
        return image
    scale = _ORIENTATION_GUARD_MAX_SIDE / float(max_side)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)


def _compute_orientation_guard_features(image: np.ndarray) -> dict:
    sample = _resize_for_orientation_guard(image)
    gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY) if len(sample.shape) == 3 else sample
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary_inv = cv2.threshold(
        blur,
        0,
        255,
        cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
    )
    foreground = binary_inv > 0
    foreground_ratio = float(np.mean(foreground))

    ys, xs = np.where(foreground)
    bbox_coverage = 0.0
    if len(xs) > 0:
        bbox_area = (xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1)
        bbox_coverage = float(bbox_area) / float(sample.shape[0] * sample.shape[1])

    kernel_len = max(25, sample.shape[1] // 5)
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_len, 1))
    horizontal_lines = cv2.morphologyEx(binary_inv, cv2.MORPH_OPEN, horizontal_kernel)
    horizontal_line_share = float(np.count_nonzero(horizontal_lines)) / max(1, int(np.count_nonzero(binary_inv)))

    is_sparse = (
        foreground_ratio <= _ORIENTATION_GUARD_SPARSE_FG_RATIO
        or bbox_coverage <= _ORIENTATION_GUARD_SPARSE_BBOX_COVERAGE
    )
    is_ruled = horizontal_line_share >= _ORIENTATION_GUARD_RULED_HORIZONTAL_SHARE
    return {
        "foreground_ratio": foreground_ratio,
        "bbox_coverage": bbox_coverage,
        "horizontal_line_share": horizontal_line_share,
        "is_sparse": is_sparse,
        "is_ruled": is_ruled,
        "is_sparse_or_ruled": bool(is_sparse or is_ruled),
    }


def _predict_orientation_with_classifier(
    image: np.ndarray, num_threads: int = _ORIENTATION_CLASSIFIER_THREADS
) -> Optional[dict]:
    session = _get_orientation_classifier_session(num_threads)
    if session is None:
        return None
    try:
        tensor = _prepare_orientation_classifier_input(image)
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name
        raw_scores = session.run([output_name], {input_name: tensor})[0]
        probabilities = _normalize_orientation_classifier_scores(raw_scores[0])
        best_idx = int(np.argmax(probabilities))
        best_score = float(probabilities[best_idx])
        runner_up_score = float(np.partition(probabilities, -2)[-2]) if probabilities.size > 1 else 0.0
        label = int(_ORIENTATION_CLASSIFIER_LABELS[best_idx])
        rotate_angle = (360 - label) % 360
        return {
            "label_angle": label,
            "rotate_angle": rotate_angle,
            "score": best_score,
            "runner_up_score": runner_up_score,
            "margin": best_score - runner_up_score,
            "probabilities": {
                _ORIENTATION_CLASSIFIER_LABELS[i]: float(probabilities[i])
                for i in range(len(_ORIENTATION_CLASSIFIER_LABELS))
            },
        }
    except Exception:
        return None


def _make_orientation_decision(
    rotate_angle: int,
    method: str,
    classifier_result: Optional[dict] = None,
    verifier_result: Optional[dict] = None,
    guard: Optional[dict] = None,
) -> dict:
    decision = {
        "rotate_angle": int(rotate_angle),
        "method": method,
        "osd_rotate": None,
        "osd_confidence": None,
        "verify_original_score": None,
        "verify_rotated_score": None,
        "verification_passed": bool(verifier_result.get("verification_passed")) if verifier_result else False,
        "best_angle": int(verifier_result.get("best_angle", rotate_angle) or rotate_angle) if verifier_result else int(rotate_angle),
        "best_score": verifier_result.get("best_score") if verifier_result else None,
        "runner_up_score": verifier_result.get("runner_up_score") if verifier_result else None,
        "angle_scores": verifier_result.get("angle_scores") if verifier_result else None,
        "best_metrics": verifier_result.get("best_metrics") if verifier_result else None,
        "angle_metrics": verifier_result.get("angle_metrics") if verifier_result else None,
        "classifier_angle": None,
        "classifier_score": None,
        "classifier_runner_up_score": None,
        "classifier_margin": None,
        "classifier_probabilities": None,
        "guard": guard,
    }
    if verifier_result:
        decision["verify_original_score"] = verifier_result.get("verify_original_score")
        decision["verify_rotated_score"] = verifier_result.get("verify_rotated_score")
    if classifier_result is not None:
        decision["classifier_angle"] = int(classifier_result.get("label_angle", 0) or 0)
        decision["classifier_score"] = float(classifier_result.get("score", 0.0) or 0.0)
        decision["classifier_runner_up_score"] = float(classifier_result.get("runner_up_score", 0.0) or 0.0)
        decision["classifier_margin"] = float(classifier_result.get("margin", 0.0) or 0.0)
        decision["classifier_probabilities"] = classifier_result.get("probabilities")
    return decision


def _detect_orientation_by_classifier_upright_scoring(
    image: np.ndarray,
    classifier_threads: int = _ORIENTATION_CLASSIFIER_THREADS,
) -> dict:
    """
    Score 0/90/180/270 by asking the classifier which rotated view looks most
    upright (`label 0`) after the candidate rotation is applied.
    """
    decision = {
        "rotate_angle": 0,
        "method": "onnx_4angle",
        "osd_rotate": None,
        "osd_confidence": None,
        "verify_original_score": None,
        "verify_rotated_score": None,
        "verification_passed": False,
        "best_angle": 0,
        "best_score": None,
        "runner_up_score": None,
        "angle_scores": None,
        "best_metrics": None,
        "angle_metrics": None,
        "classifier_angle": None,
        "classifier_score": None,
        "classifier_runner_up_score": None,
        "classifier_margin": None,
    }

    angle_scores = {}
    angle_metrics = {}
    for angle in (0, 90, 180, 270):
        sample = image if angle == 0 else _rotate_right_angle(image, angle)
        classifier_result = _predict_orientation_with_classifier(sample, num_threads=classifier_threads)
        if classifier_result is None:
            angle_metrics[angle] = None
            angle_scores[angle] = -1.0
            continue
        upright_probability = float(classifier_result["probabilities"].get("0", 0.0) or 0.0)
        angle_metrics[angle] = classifier_result
        angle_scores[angle] = upright_probability

    decision["angle_scores"] = angle_scores
    decision["angle_metrics"] = angle_metrics
    decision["verify_original_score"] = angle_scores.get(0)

    ranked = sorted(
        ((angle, score) for angle, score in angle_scores.items() if score is not None and score >= 0.0),
        key=lambda item: item[1],
        reverse=True,
    )
    if not ranked:
        return decision

    best_angle, best_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else -1.0
    decision["best_angle"] = int(best_angle)
    decision["best_score"] = float(best_score)
    decision["runner_up_score"] = float(runner_up_score)
    decision["best_metrics"] = angle_metrics.get(best_angle)
    decision["verify_rotated_score"] = best_score if best_angle != 0 else runner_up_score
    return decision


def detect_orientation_correction(
    image: np.ndarray, classifier_threads: int = _ORIENTATION_CLASSIFIER_THREADS
) -> dict:
    """
    Use the ONNX 4-way document orientation classifier only.

    Fast path:
    - run the classifier once on a downscaled analysis image
    - accept confident upright pages immediately
    - otherwise verify only one candidate rotation:
      - the classifier's predicted non-zero angle, or
      - for uncertain upright pages, the strongest non-zero alternative

    This keeps the decision path close to 1-2 classifier passes per page
    instead of brute-forcing all 4 angles.
    """
    analysis_image = _resize_for_orientation_analysis(image)
    guard = _compute_orientation_guard_features(analysis_image)
    classifier_result = _predict_orientation_with_classifier(
        analysis_image,
        num_threads=classifier_threads,
    )
    if classifier_result is None:
        return _make_orientation_decision(
            0,
            "onnx_unavailable",
            guard=guard,
        )

    score = float(classifier_result["score"])
    margin = float(classifier_result["margin"])
    classifier_confident = (
        score >= _ORIENTATION_CLASSIFIER_MIN_SCORE
        and margin >= _ORIENTATION_CLASSIFIER_MIN_MARGIN
    )
    predicted_rotate = int(classifier_result["rotate_angle"])

    if predicted_rotate == 0 and classifier_confident:
        return _make_orientation_decision(
            0,
            "onnx_classifier",
            classifier_result=classifier_result,
            guard=guard,
        )

    # Guard: sparse/ruled pages (signature-only last pages, forms with horizontal
    # rules) fool the classifier. Block any non-zero rotation on them — the model
    # is unreliable in this regime, so staying at 0° is the safer default.
    if guard.get("is_sparse_or_ruled") and predicted_rotate != 0:
        return _make_orientation_decision(
            0,
            "onnx_guard_blocked",
            classifier_result=classifier_result,
            guard=guard,
        )

    current_upright_score = float(classifier_result["probabilities"].get("0", 0.0) or 0.0)

    candidate_rotate = 0
    method = "onnx_candidate_rejected"
    if predicted_rotate != 0:
        candidate_rotate = predicted_rotate
        method = "onnx_verified_candidate"
    else:
        probs = classifier_result.get("probabilities") or {}
        alt_label = max(
            (90, 180, 270),
            key=lambda a: float(probs.get(str(a), 0.0) or 0.0),
        )
        alt_prob = float(probs.get(str(alt_label), 0.0) or 0.0)
        if alt_prob >= _ORIENTATION_UPRIGHT_MIN_SCORE:
            candidate_rotate = (360 - int(alt_label)) % 360
            method = "onnx_zero_alt_candidate"

    if candidate_rotate == 0:
        return _make_orientation_decision(
            0,
            "onnx_zero_kept",
            classifier_result=classifier_result,
            guard=guard,
        )

    candidate_result = _predict_orientation_with_classifier(
        _rotate_right_angle(analysis_image, candidate_rotate),
        num_threads=classifier_threads,
    )
    verifier_result = {
        "verify_original_score": current_upright_score,
        "verify_rotated_score": None,
        "verification_passed": False,
        "best_angle": candidate_rotate,
        "best_score": None,
        "runner_up_score": None,
        "angle_scores": {
            0: current_upright_score,
            candidate_rotate: None,
        },
        "best_metrics": candidate_result,
        "angle_metrics": {
            0: classifier_result,
            candidate_rotate: candidate_result,
        },
    }

    if candidate_result is None:
        return _make_orientation_decision(
            0,
            method,
            classifier_result=classifier_result,
            verifier_result=verifier_result,
            guard=guard,
        )

    candidate_upright_score = float(candidate_result["probabilities"].get("0", 0.0) or 0.0)
    candidate_margin = float(candidate_result.get("margin", 0.0) or 0.0)
    candidate_label = int(candidate_result.get("label_angle", 0) or 0)
    upright_improvement = candidate_upright_score - current_upright_score

    verifier_result["verify_rotated_score"] = candidate_upright_score
    verifier_result["best_score"] = candidate_upright_score
    verifier_result["runner_up_score"] = current_upright_score
    verifier_result["angle_scores"][candidate_rotate] = candidate_upright_score

    if (
        candidate_label == 0
        and candidate_upright_score >= _ORIENTATION_UPRIGHT_MIN_SCORE
        and candidate_margin >= _ORIENTATION_UPRIGHT_MIN_MARGIN
        and upright_improvement >= _ORIENTATION_UPRIGHT_MIN_IMPROVEMENT
        and (
            candidate_upright_score >= _ORIENTATION_UPRIGHT_STRONG_SCORE
            or predicted_rotate != 0
        )
    ):
        verifier_result["verification_passed"] = True
        return _make_orientation_decision(
            candidate_rotate,
            method,
            classifier_result=classifier_result,
            verifier_result=verifier_result,
            guard=guard,
        )

    return _make_orientation_decision(
        0,
        method,
        classifier_result=classifier_result,
        verifier_result=verifier_result,
        guard=guard,
    )

_PROJ_SWEEP_DEG = 8.0
_PROJ_COARSE_STEP = 0.5
_PROJ_FINE_STEP = 0.05
_PROJ_ANALYSIS_LONG_EDGE = 1500
_PROJ_INNER_MARGIN_RATIO = 0.05
_PROJ_CONFIDENCE_MIN = 1.15


def _projection_profile_score(binary: np.ndarray, angle: float) -> float:
    if angle == 0.0:
        rotated = binary
    else:
        h, w = binary.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
        rotated = cv2.warpAffine(
            binary, M, (w, h),
            borderMode=cv2.BORDER_CONSTANT, borderValue=0,
            flags=cv2.INTER_NEAREST,
        )
    h, w = rotated.shape[:2]
    my = int(h * _PROJ_INNER_MARGIN_RATIO)
    mx = int(w * _PROJ_INNER_MARGIN_RATIO)
    inner = rotated[my:h - my, mx:w - mx]
    row_sums = inner.sum(axis=1).astype(np.float32)
    diffs = np.diff(row_sums)
    return float(np.dot(diffs, diffs))


def get_fine_deskew_angle(image: np.ndarray) -> float:
    """Detect small-angle page skew via projection-profile variance (Postl / Leptonica-style).

    For each trial angle, score = Σ(row_sum[i+1] − row_sum[i])². Aligned text baselines
    create strong row-to-row contrast at the correct angle. Stamps, signatures, and
    diagonal handwriting contribute uniformly across rows and do not bias the peak,
    which fixes the Hough-median failure mode on pages with heavy non-text content.
    """
    try:
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image.copy()

        h, w = gray.shape
        scale = min(1.0, _PROJ_ANALYSIS_LONG_EDGE / max(h, w))
        if scale < 1.0:
            gray = cv2.resize(
                gray, (int(w * scale), int(h * scale)),
                interpolation=cv2.INTER_AREA,
            )

        _, binary = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)

        coarse_angles = np.arange(-_PROJ_SWEEP_DEG, _PROJ_SWEEP_DEG + 1e-6, _PROJ_COARSE_STEP)
        coarse_scores = np.array([_projection_profile_score(binary, a) for a in coarse_angles])
        coarse_best = float(coarse_angles[int(np.argmax(coarse_scores))])

        fine_angles = np.arange(
            coarse_best - _PROJ_COARSE_STEP,
            coarse_best + _PROJ_COARSE_STEP + 1e-6,
            _PROJ_FINE_STEP,
        )
        fine_scores = np.array([_projection_profile_score(binary, a) for a in fine_angles])
        fine_best = float(fine_angles[int(np.argmax(fine_scores))])

        best_score = float(fine_scores.max())
        mean_score = float(fine_scores.mean())
        confidence = best_score / mean_score if mean_score > 0 else 0.0
        if confidence < _PROJ_CONFIDENCE_MIN:
            return 0.0

        if abs(fine_best) > _PROJ_SWEEP_DEG:
            return 0.0

        return fine_best
    except Exception:
        return 0.0

def rotate_image(image: np.ndarray, angle: float) -> np.ndarray:
    """Rotate image by angle (degrees)."""
    if angle == 0: return image
    
    (h, w) = image.shape[:2]
    (cX, cY) = (w // 2, h // 2)

    M = cv2.getRotationMatrix2D((cX, cY), angle, 1.0)
    cos = np.abs(M[0, 0])
    sin = np.abs(M[0, 1])

    nW = int((h * sin) + (w * cos))
    nH = int((h * cos) + (w * sin))

    M[0, 2] += (nW / 2) - cX
    M[1, 2] += (nH / 2) - cY

    return cv2.warpAffine(image, M, (nW, nH), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))


def _rotate_image_keep_canvas(image: np.ndarray, angle: float) -> np.ndarray:
    """Rotate image by angle (deg) keeping the ORIGINAL canvas dimensions.

    Pixels rotated outside the original canvas are clipped; uncovered areas
    are padded white. Pixel density is preserved exactly. Suitable for small
    deskew angles (<= ~3°). For larger angles, use rotate_image() which
    expands the canvas to avoid clipping content (at the cost of a larger page).
    """
    if angle == 0:
        return image
    h, w = image.shape[:2]
    center = (w / 2.0, h / 2.0)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(
        image, M, (w, h),
        borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255),
    )


def _find_dominant_embedded_image(page) -> Optional[dict]:
    """Return metadata of the SINGLE dominant raster image on this page.

    A dominant image must (a) be displayed at >= _DOMINANT_IMAGE_COVERAGE of
    the page area and (b) be the only such image on the page. Used to derive
    the effective source DPI without re-rasterizing.
    Returns: {"pixel_w","pixel_h","displayed_rect": fitz.Rect} or None.
    """
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return None
    candidates = []
    try:
        infos = page.get_image_info(xrefs=True)
    except TypeError:
        infos = page.get_image_info()
    for info in infos:
        try:
            bbox = fitz.Rect(info["bbox"])
        except Exception:
            continue
        coverage = (bbox.width * bbox.height) / page_area
        if coverage < _DOMINANT_IMAGE_COVERAGE:
            continue
        pixel_w = int(info.get("width", 0) or 0)
        pixel_h = int(info.get("height", 0) or 0)
        if pixel_w <= 0 or pixel_h <= 0:
            continue
        candidates.append({
            "displayed_rect": bbox,
            "pixel_w": pixel_w,
            "pixel_h": pixel_h,
        })
    if len(candidates) != 1:
        return None
    return candidates[0]


def _estimate_embedded_dpi(displayed_rect, pixel_w: int, pixel_h: int) -> Optional[float]:
    """Effective rendered DPI: pixel_dim / displayed_inch.

    Mean of width and height DPIs (typically equal for non-stretched images).
    Returns None if rect or pixel dims are invalid.
    """
    if displayed_rect.width <= 0 or displayed_rect.height <= 0:
        return None
    if pixel_w <= 0 or pixel_h <= 0:
        return None
    dpi_w = (pixel_w * 72.0) / displayed_rect.width
    dpi_h = (pixel_h * 72.0) / displayed_rect.height
    return (dpi_w + dpi_h) / 2.0


def pre_process_pdf(input_path: str, output_path: str, update_callback=None,
                    debug_mode=False, max_workers: Optional[int] = None,
                    return_metadata: bool = False):
    """
    Smart Preprocessing (parallel):
    Phase 1 (serial): Extract/rasterize images from PDF (fitz not thread-safe)
    Phase 2 (parallel): OSD + deskew on all images using all CPU threads
    Phase 3 (serial): Assemble output PDF

    Return:
      - default: Tuple[bool, str]  (backward-compatible).
      - if ``return_metadata=True``: Tuple[bool, str, dict] where the dict has
        ``{"page_rotations": [int, ...]}`` -- one entry per source page
        (0/90/180/270) indicating the cardinal rotation applied during
        preprocessing. A digital-passthrough PDF reports all zeros.
    """
    try:
        pdf_type = classify_pdf(input_path)
        if pdf_type == "digital":
            if debug_mode and update_callback:
                update_callback(
                    f"[Debug] File '{os.path.basename(input_path)}': Digital PDF detected, preprocessing passthrough.",
                    "debug",
                )
            if os.path.abspath(input_path) != os.path.abspath(output_path):
                output_dir = os.path.dirname(output_path)
                if output_dir:
                    os.makedirs(output_dir, exist_ok=True)
                shutil.copy2(input_path, output_path)
            if return_metadata:
                try:
                    with fitz.open(input_path) as _tmp_doc:
                        _n = len(_tmp_doc)
                except Exception:
                    _n = 0
                return True, "Digital passthrough", {"page_rotations": [0] * _n}
            return True, "Digital passthrough"

        # Debug: Check for text layer
        if debug_mode and update_callback:
            has_text = has_text_content(input_path)
            update_callback(f"[Debug] File '{os.path.basename(input_path)}': Has Text Layer = {has_text}")

        src_doc = fitz.open(input_path)
        total_pages = len(src_doc)

        # =================================================================
        # PHASE 1 (serial): per-page native-DPI metadata + analysis preview
        # =================================================================
        page_data_list = []
        for i in range(total_pages):
            page = src_doc[i]
            if update_callback:
                update_callback(f"Extracting Page {i+1}/{total_pages}...", "debug")

            dominant = _find_dominant_embedded_image(page)
            estimated_dpi = None
            if dominant is not None:
                estimated_dpi = _estimate_embedded_dpi(
                    dominant["displayed_rect"], dominant["pixel_w"], dominant["pixel_h"]
                )

            zoom = _ANALYSIS_DPI / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), annots=True)
            if pix.n >= 3:
                arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
                if pix.n == 4:
                    analysis_img = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
                else:
                    analysis_img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            else:
                arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w)
                analysis_img = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)

            page_data_list.append({
                "index": i,
                "analysis_img": analysis_img,
                "estimated_dpi": estimated_dpi,
                "has_dominant_image": dominant is not None,
            })

        # =================================================================
        # PHASE 2 (parallel): orientation + deskew detection on previews
        # =================================================================
        import concurrent.futures

        if max_workers is None:
            n_workers = max(2, (os.cpu_count() or 4) - 1)
        else:
            n_workers = max(1, int(max_workers))
        n_workers = max(1, min(total_pages or 1, n_workers))
        classifier_threads = max(
            1,
            min(
                _ORIENTATION_CLASSIFIER_THREADS,
                max(1, (os.cpu_count() or _ORIENTATION_CLASSIFIER_THREADS) // n_workers),
            ),
        )
        if update_callback:
            update_callback(f"Analyzing orientation ({n_workers} threads)...", "debug")

        def analyze_page(pd):
            """Detect cardinal rotation + fine skew on the preview image. Thread-safe."""
            img = pd["analysis_img"]
            rotate_angle = 0
            orientation_meta = {
                "method": None, "osd_rotate": None, "osd_confidence": None,
                "verify_original_score": None, "verify_rotated_score": None,
                "verification_passed": False, "best_angle": 0,
                "best_score": None, "runner_up_score": None,
                "classifier_angle": None, "classifier_score": None,
                "classifier_runner_up_score": None, "classifier_margin": None,
            }
            try:
                orientation_meta = detect_orientation_correction(
                    img, classifier_threads=classifier_threads
                )
                rotate_angle = int(orientation_meta.get("rotate_angle", 0) or 0)
            except Exception:
                pass

            # Skew detection on cardinally-corrected preview (in-memory only)
            test_img = img if rotate_angle == 0 else _rotate_right_angle(img, rotate_angle)
            skew_angle = get_fine_deskew_angle(test_img)

            return {
                "index": pd["index"],
                "rotate_angle": rotate_angle,
                "skew_angle": skew_angle,
                "orientation_meta": orientation_meta,
                "estimated_dpi": pd["estimated_dpi"],
                "has_dominant_image": pd["has_dominant_image"],
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as executor:
            results = list(executor.map(analyze_page, page_data_list))
        results.sort(key=lambda r: r["index"])

        # =================================================================
        # PHASE 3 (serial): per-branch assembly
        #   A: cardinal=0, |skew|<0.5°  → insert_pdf passthrough
        #   B: cardinal!=0, |skew|<0.5° → insert_pdf + set_rotation()
        #   C: cardinal=0, |skew|>=0.5° → re-raster + keep-canvas deskew
        #   D: cardinal!=0, |skew|>=0.5°→ re-raster + combined expand-canvas
        # =================================================================
        out_doc = fitz.open()
        processed_count = 0
        branch_counts = {"A": 0, "B": 0, "C": 0, "D": 0}

        for r in results:
            i = r["index"]
            rotate_angle = r["rotate_angle"]
            skew_angle = r["skew_angle"]
            estimated_dpi = r["estimated_dpi"]

            needs_cardinal = rotate_angle != 0
            needs_deskew = abs(skew_angle) > _DESKEW_THRESHOLD_DEG

            if not needs_cardinal and not needs_deskew:
                branch = "A"
            elif needs_cardinal and not needs_deskew:
                branch = "B"
            elif not needs_cardinal and needs_deskew:
                branch = "C"
            else:
                branch = "D"

            try:
                if branch == "A":
                    out_doc.insert_pdf(src_doc, from_page=i, to_page=i)

                elif branch == "B":
                    out_doc.insert_pdf(src_doc, from_page=i, to_page=i)
                    new_page = out_doc[-1]
                    composed_rotation = (new_page.rotation + rotate_angle) % 360
                    new_page.set_rotation(composed_rotation)

                else:  # C or D — rasterize at native DPI then warp
                    if estimated_dpi is None:
                        native_dpi = float(_FALLBACK_RASTER_DPI)
                        if update_callback:
                            update_callback(
                                f"  Page {i+1}: No dominant embedded image — using fallback {native_dpi:.0f} DPI",
                                "warning",
                            )
                    else:
                        native_dpi = float(estimated_dpi)

                    src_page = src_doc[i]
                    zoom = native_dpi / 72.0

                    pix = src_page.get_pixmap(
                        matrix=fitz.Matrix(zoom, zoom),
                        annots=True,
                    )
                    if pix.n >= 3:
                        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
                        if pix.n == 4:
                            native_img = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
                        else:
                            native_img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
                    else:
                        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w)
                        native_img = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)

                    if branch == "C":
                        if abs(skew_angle) <= _DESKEW_KEEP_CANVAS_MAX_DEG:
                            rotated = _rotate_image_keep_canvas(native_img, skew_angle)
                        else:
                            rotated = rotate_image(native_img, skew_angle)
                            if update_callback:
                                update_callback(
                                    f"  Page {i+1}: Skew {skew_angle:.2f}° > {_DESKEW_KEEP_CANVAS_MAX_DEG}° — using expand-canvas (page rect will change)",
                                    "warning",
                                )
                    else:  # D — combined cardinal + skew, single warpAffine
                        # rotate_angle: CW degrees to make image upright.
                        # cv2 angle convention: positive = CCW, hence negate.
                        # skew_angle from get_fine_deskew_angle is in cv2 convention.
                        combined_angle = -rotate_angle + skew_angle
                        rotated = rotate_image(native_img, combined_angle)

                    h_px_out, w_px_out = rotated.shape[:2]
                    w_pts_out = w_px_out / zoom
                    h_pts_out = h_px_out / zoom

                    # Keep the native pixel dimensions/DPI, but do not store
                    # warped full-page scans as raw/PNG RGB streams in the PDF.
                    # PyMuPDF expands inserted PNGs for these pages to ~25 MB
                    # uncompressed streams, while high-quality JPEG preserves
                    # OCR-visible detail and matches the source scan encoding.
                    success, enc = cv2.imencode(
                        ".jpg",
                        rotated,
                        [
                            int(cv2.IMWRITE_JPEG_QUALITY), 97,
                            int(cv2.IMWRITE_JPEG_OPTIMIZE), 1,
                        ],
                    )
                    if not success:
                        continue
                    final_bytes = enc.tobytes()

                    new_page = out_doc.new_page(width=w_pts_out, height=h_pts_out)
                    new_page.insert_image(new_page.rect, stream=final_bytes)

                branch_counts[branch] += 1
                processed_count += 1

                if debug_mode and update_callback:
                    dpi_str = f"{estimated_dpi:.0f}" if estimated_dpi else f"{_FALLBACK_RASTER_DPI} (fallback)"
                    update_callback(
                        f"[Debug] Page {i+1}: branch={branch} rotate={rotate_angle}° skew={skew_angle:.2f}° native_dpi={dpi_str}",
                        "debug",
                    )
            except Exception as e:
                if update_callback:
                    update_callback(f"  Page {i+1}: ERROR — {e}", "warning")
                continue

        src_doc.close()
        out_doc.save(output_path, garbage=4, deflate=True, deflate_images=True)
        out_doc.close()

        summary = (
            f"Processed {processed_count}/{total_pages} pages "
            f"(A={branch_counts['A']}, B={branch_counts['B']}, "
            f"C={branch_counts['C']}, D={branch_counts['D']})"
        )
        if return_metadata:
            rotations = [0] * total_pages
            for r in results:
                idx = r.get("index")
                if isinstance(idx, int) and 0 <= idx < total_pages:
                    rotations[idx] = int(r.get("rotate_angle", 0) or 0) % 360
            return True, summary, {"page_rotations": rotations}
        return True, summary

    except Exception as e:
        import traceback
        traceback.print_exc()
        if return_metadata:
            return False, str(e), {"page_rotations": []}
        return False, str(e)
