"""So sánh độ chính xác OCR.

Flow:
  - Ground truth = text gốc trong groundtruth.docx (không qua OCR).
  - Phần mềm này: OCR trên groundtruth.pdf -> text (cached).
  - Phần mềm khác: user upload PDF đã OCR -> trích text từ text layer.
  - So sánh từng bên với GT bằng CER/WER (KHÔNG normalize text trước khi so).
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Callable


# Ngưỡng chênh lệch để cảnh báo: their_acc < ours_acc - WARN_DELTA
WARN_DELTA = 0.03  # 3% trên CHAR accuracy


def _edit_distance(a, b) -> int:
    # rapidfuzz là C extension, ~3000x nhanh hơn pure Python trên text 30K chars.
    # Pure Python fallback chỉ kích hoạt khi rapidfuzz thiếu — không kỳ vọng.
    try:
        from rapidfuzz.distance import Levenshtein
        return Levenshtein.distance(a, b)
    except ImportError:
        pass
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


@dataclass
class SideMetrics:
    char_accuracy: float
    word_accuracy: float
    cer: float
    wer: float
    pred_chars: int
    pred_words: int


@dataclass
class ComparisonResult:
    ours: SideMetrics
    theirs: SideMetrics
    gt_chars: int
    gt_words: int
    verdict: str         # "praise" | "warn" | "tie"
    delta_char_acc: float  # theirs - ours (theo char_accuracy, có thể âm)


def compute_side_metrics(pred: str, gt: str) -> SideMetrics:
    """Tính CER/WER/accuracy giữa pred và gt. KHÔNG normalize text."""
    pred_words = pred.split()
    gt_words = gt.split()

    char_dist = _edit_distance(pred, gt)
    cer = char_dist / max(len(gt), 1)
    word_dist = _edit_distance(pred_words, gt_words)
    wer = word_dist / max(len(gt_words), 1)

    return SideMetrics(
        char_accuracy=max(0.0, 1.0 - cer),
        word_accuracy=max(0.0, 1.0 - wer),
        cer=cer,
        wer=wer,
        pred_chars=len(pred),
        pred_words=len(pred_words),
    )


def extract_text_from_pdf_layer(pdf_path: str) -> str:
    """Trích text layer của PDF (giả định user đã OCR). Raise nếu rỗng."""
    import fitz  # PyMuPDF

    doc = fitz.open(pdf_path)
    try:
        parts: list[str] = []
        for page in doc:
            t = page.get_text("text") or ""
            if t.strip():
                parts.append(t)
        text = "\n".join(parts)
    finally:
        doc.close()

    if not text.strip():
        raise ValueError(
            "PDF bạn tải lên không có text layer. "
            "Có thể OCR đã thất bại hoặc file chưa được OCR."
        )
    return text


def compare_against_baseline(
    user_pdf_path: str,
    log_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> ComparisonResult:
    """Pipeline so sánh đầy đủ: load GT, lấy our OCR (cache), trích their text, tính."""
    log_cb = log_cb or (lambda m: None)

    if not os.path.exists(user_pdf_path):
        raise FileNotFoundError(f"Không tìm thấy {user_pdf_path}")

    from scanindex.core.ocr import accuracy_baseline

    log_cb("Đọc text gốc (groundtruth.txt nếu có, không thì docx)...")
    gt_text = accuracy_baseline.load_ground_truth_text()

    log_cb("Lấy text OCR của phần mềm này (sẽ chạy lần đầu, cache về sau)...")
    ours_text = accuracy_baseline.get_or_compute_our_ocr_text(log_cb, cancel_event)
    if cancel_event and cancel_event.is_set():
        raise RuntimeError("Đã hủy")

    log_cb("Trích text từ PDF bạn tải lên...")
    theirs_text = extract_text_from_pdf_layer(user_pdf_path)

    log_cb("Tính CER/WER cho cả hai phía...")
    ours = compute_side_metrics(ours_text, gt_text)
    theirs = compute_side_metrics(theirs_text, gt_text)

    delta = theirs.char_accuracy - ours.char_accuracy
    if delta >= -WARN_DELTA:
        verdict = "praise" if delta >= 0 else "tie"
    else:
        verdict = "warn"

    return ComparisonResult(
        ours=ours,
        theirs=theirs,
        gt_chars=len(gt_text),
        gt_words=len(gt_text.split()),
        verdict=verdict,
        delta_char_acc=delta,
    )


def format_report(result: ComparisonResult) -> str:
    """Render kết quả dạng bảng box-drawing cho QTextEdit (font monospace).

    Bảng chỉ gồm CER và WER (theo yêu cầu hiển thị tinh gọn).
    """
    o, t = result.ours, result.theirs

    LABEL_W = 18   # chiều rộng cột nhãn (giữa hai dấu │)
    NUM_W = 9      # chiều rộng cột số

    def cell_pct(x: float) -> str:
        # 9 chars: " " + "{:5.2f}" (5) + "%" + "  "
        return f" {x*100:5.2f}%  "

    def cell_label(s: str) -> str:
        # 18 chars: " " + "{:<16}" (16) + " "
        return f" {s:<16} "

    top    = "┌" + "─"*LABEL_W + "┬" + "─"*NUM_W + "┬" + "─"*NUM_W + "┐"
    sep    = "├" + "─"*LABEL_W + "┼" + "─"*NUM_W + "┼" + "─"*NUM_W + "┤"
    bottom = "└" + "─"*LABEL_W + "┴" + "─"*NUM_W + "┴" + "─"*NUM_W + "┘"

    header = (
        "│" + " "*LABEL_W
        + "│" + "CER".center(NUM_W)
        + "│" + "WER".center(NUM_W) + "│"
    )
    row1 = (
        "│" + cell_label("Phần mềm này")
        + "│" + cell_pct(o.cer) + "│" + cell_pct(o.wer) + "│"
    )
    row2 = (
        "│" + cell_label("Phần mềm của bạn")
        + "│" + cell_pct(t.cer) + "│" + cell_pct(t.wer) + "│"
    )

    lines = [
        "=== KẾT QUẢ SO SÁNH ĐỘ CHÍNH XÁC OCR ===",
        f"Văn bản gốc: {result.gt_chars:,} ký tự / {result.gt_words:,} từ",
        "",
        top, header, sep, row1, row2, bottom,
        "",
    ]

    delta_pct = result.delta_char_acc * 100
    if result.verdict == "praise":
        lines.append(
            f"✓ Tuyệt vời! OCR của bạn tốt hơn phần mềm này khoảng "
            f"{abs(delta_pct):.2f} điểm (theo CER)."
        )
    elif result.verdict == "tie":
        lines.append(
            f"✓ Tốt. Chênh lệch chỉ {abs(delta_pct):.2f} điểm — "
            f"hai bên ngang ngửa."
        )
    else:
        lines.append(
            f"⚠ Cảnh báo: CER của OCR bạn cao hơn {abs(delta_pct):.2f} điểm "
            f"so với phần mềm này. Nên kiểm tra lại cấu hình OCR."
        )

    lines.append("")
    lines.append(
        "Ghi chú: CER = tỉ lệ lỗi ký tự, WER = tỉ lệ lỗi từ. "
        "Càng thấp càng chính xác hơn."
    )
    return "\n".join(lines)
