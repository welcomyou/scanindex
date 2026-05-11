"""
KIE Viewer — Xem + Sửa kết quả KIE (Key Information Extraction).

- Chọn batch → danh sách file bên trái → click hoặc ← → chuyển file
- Click trường KIE → highlight bbox trên PDF
- Bấm "Sửa" → chế độ edit: click word trên PDF để gán/bỏ → Save

Chạy:  python temp/kie_viewer.py
"""

import json
import os
import sys
import threading
import time
from collections import OrderedDict, deque
from functools import partial
from pathlib import Path

# orjson: ~3-5× faster JSON parse for big canonical files. Fallback to stdlib
# if unavailable so the viewer remains runnable without the optional dep.
try:
    import orjson  # type: ignore
    def _fast_json_load(path: str):
        with open(path, "rb") as f:
            return orjson.loads(f.read())
except ImportError:
    orjson = None
    def _fast_json_load(path: str):
        with open(path, encoding="utf-8") as f:
            return json.load(f)


class PixmapCache:
    """LRU cache for rendered page QPixmaps keyed by (stem, page_idx, zoom, rot).

    Capped by total raw-pixel byte budget (default ~300 MB). Evicts oldest
    entries when the budget is exceeded. Reloading a file or revisiting a
    previously-rendered page becomes an instant dict lookup (~0 ms) instead
    of paying ~60-100 ms for fitz + pixmap conversion.
    """

    def __init__(self, max_bytes: int = 300 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.total_bytes = 0
        self.entries: "OrderedDict[tuple, tuple]" = OrderedDict()

    def get(self, key):
        entry = self.entries.get(key)
        if entry is None:
            return None
        self.entries.move_to_end(key)
        return entry[0]

    def put(self, key, pixmap):
        # Raw pixel footprint — independent of internal Qt compression
        size = max(1, pixmap.width() * pixmap.height() * 4)
        if key in self.entries:
            self.total_bytes -= self.entries[key][1]
            self.entries.move_to_end(key)
        self.entries[key] = (pixmap, size)
        self.total_bytes += size
        while self.total_bytes > self.max_bytes and len(self.entries) > 1:
            _, (_, evicted_size) = self.entries.popitem(last=False)
            self.total_bytes -= evicted_size

    def invalidate_stem(self, stem: str):
        """Drop all entries for a file. Called on manual rotation override etc."""
        to_drop = [k for k in self.entries if k[0] == stem]
        for k in to_drop:
            self.total_bytes -= self.entries.pop(k)[1]

    def clear(self):
        self.entries.clear()
        self.total_bytes = 0


import fitz  # PyMuPDF
from PySide6.QtCore import Qt, QRectF, QPointF, QTimer, QObject, Signal
from PySide6.QtGui import (
    QPixmap, QImage, QPainter, QColor, QPen, QBrush, QFont,
    QKeySequence, QShortcut, QTransform, QAction,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QSplitter, QScrollArea,
    QTreeWidget, QTreeWidgetItem, QSizePolicy, QFrame,
    QHeaderView, QAbstractItemView, QListWidget, QListWidgetItem,
    QMessageBox, QCheckBox, QDialog, QLineEdit, QFileDialog,
    QDialogButtonBox, QInputDialog, QTextEdit, QMenu,
)


class PrefetchEmitter(QObject):
    """QObject host for the background-to-main prefetch-done signal.

    Worker threads run fitz + QImage conversion (both release the GIL and are
    thread-safe to that extent), then emit this signal with a QImage. A slot
    on the main thread receives it, converts to QPixmap (which requires the
    GUI thread), and seeds the PixmapCache.

    Payload: (stem, page_idx, zoom, rotation, QImage)
    """
    prefetch_done = Signal(str, int, float, int, object)


class SecretScanEmitter(QObject):
    """Signal host for background SECRECY_MARK scans. Payload:
    (stem, keyword_or_empty_string). Empty string conveys "not classified"
    safely across PySide6's signal marshaller (None is not round-tripped
    cleanly when typed as str)."""
    secret_detected = Signal(str, str)

# Make repo root importable so the shared KIE validator can be used even when
# this file is launched directly instead of via `python -m kie_viewer`.
def _find_repo_root(start: Path) -> Path:
    for candidate in start.resolve().parents:
        if (candidate / "scanindex" / "__init__.py").exists():
            return candidate
    return start.resolve().parents[3]


_REPO_ROOT = _find_repo_root(Path(__file__))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
try:
    from scanindex.core.kie.labeling_workspace import validate_label_output_detailed
except Exception as _exc:  # pragma: no cover - viewer should still open
    validate_label_output_detailed = None
    _VALIDATOR_IMPORT_ERROR = _exc
else:
    _VALIDATOR_IMPORT_ERROR = None

try:
    from scanindex.core.kie.inference_pipeline import detect_secrecy_mark
except Exception:  # pragma: no cover - viewer still opens without secrecy detection
    detect_secrecy_mark = None

# ═══════════════════════════════════════════════════════
# DEFAULT PATHS
# ═══════════════════════════════════════════════════════
DEFAULT_CONFIG = {
    "input_dir": r"D:\tmp\Train_20260413_143844_kie\json_input",
    "output_dir": r"D:\tmp\Train_20260413_143844_kie\json_output_labeled",
    "ocr_dir": r"D:\tmp\Train_20260413_143844_kie\ocr",
}

CONFIG_PATH = Path(
    os.environ.get(
        "KIE_VIEWER_CONFIG",
        str(Path(__file__).resolve().with_name("kie_viewer_config.json")),
    )
)


def _load_path_config():
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.is_file():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for key in DEFAULT_CONFIG:
                    value = loaded.get(key)
                    if isinstance(value, str) and value.strip():
                        config[key] = value.strip()
        except Exception as exc:
            print(f"Error reading viewer config {CONFIG_PATH}: {exc}")

    for key in DEFAULT_CONFIG:
        env_key = f"KIE_VIEWER_{key.upper()}"
        value = os.environ.get(env_key)
        if value and value.strip():
            config[key] = value.strip()

    return config


def _apply_path_config(config):
    global PATH_CONFIG, DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR, DEFAULT_OCR_DIR
    PATH_CONFIG = dict(config)
    DEFAULT_INPUT_DIR = PATH_CONFIG["input_dir"]
    DEFAULT_OUTPUT_DIR = PATH_CONFIG["output_dir"]
    DEFAULT_OCR_DIR = PATH_CONFIG["ocr_dir"]


def _save_path_config(config):
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_apply_path_config(_load_path_config())

# ═══════════════════════════════════════════════════════
# Theme
# ═══════════════════════════════════════════════════════
BG        = "#2b2b2b"
SURFACE   = "#3a3a3a"
ELEVATED  = "#464646"
HOVER     = "#525252"
INPUT_BG  = "#464646"
ACCENT    = "#007bff"
ACCENT2   = "#0056b3"
GREEN     = "#28a745"
RED       = "#dc3545"
TEXT      = "#ffffff"
TEXT2     = "#cccccc"
MUTED     = "#888888"
BORDER    = "#4a4a4a"
FONT_UI   = "Segoe UI"
FONT_MONO = "Cascadia Code"

# Reuse the global asset resolver so QComboBox::down-arrow works in both
# source and frozen-exe builds.
from scanindex.ui.theme import CHEVRON_DOWN_URL as _CHEVRON_DOWN_URL

# 16 colored KIE fields + 2 neutral, distributed across 360° hue wheel
# Ontology v3: 10 train labels + 3 rule-based marks + DOC_TYPE (output-only).
# Colors chosen from Tailwind palette (600/300 bright, 700-800/400 deep) so
# adjacent labels look clearly different. Keeping hue gap >= 30° between
# commonly-adjacent fields on official documents.
LABEL_COLORS = {
    # ── 10 train labels (bright + deep tiers mixed by page region) ──
    "REGIME_HEADER":         ("#dc2626", "#fca5a5"),   # red-600 / red-300 — top header, very visible
    "ISSUE_ORG_SUPERIOR":    ("#0284c7", "#7dd3fc"),   # sky-600 / sky-300
    "ISSUE_ORG_NAME":        ("#a21caf", "#e879f9"),   # fuchsia-700 / fuchsia-400
    "DOC_NUMBER_SYMBOL":     ("#ea580c", "#fdba74"),   # orange-600 / orange-300
    "PLACE_DATE":            ("#4b5563", "#d1d5db"),   # gray-600 / gray-300 — date line
    "DOC_SUBJECT":           ("#65a30d", "#bef264"),   # lime-600 / lime-300 — title block
    "ADDRESSEE":             ("#a16207", "#facc15"),   # yellow-700 / yellow-400
    "RECIPIENTS":            ("#1e40af", "#60a5fa"),   # blue-800 / blue-400 — navy
    "SIGNER_ROLE":           ("#0d9488", "#5eead4"),   # teal-600 / teal-300
    "SIGNER_NAME":           ("#9333ea", "#d8b4fe"),   # purple-600 / purple-300

    # ── Rule-based marks (shown only if present, not creatable in UI) ──
    "URGENCY_MARK":          ("#9f1239", "#fb7185"),   # rose-800 / rose-400 — alarming
    "SECRECY_MARK":          ("#3f3f46", "#a1a1aa"),   # charcoal
    "CIRCULATION_MARK":      ("#155e75", "#22d3ee"),   # cyan-800 / cyan-400

    # ── Output-only, deterministic post-process ──
    "DOC_TYPE":              ("#737373", "#d4d4d4"),   # gray
}

# V3 train labels: what the human/LLM can create in the viewer
V3_TRAIN_LABELS = [
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
# V3 read-only labels: shown if already present in file, but NOT listed
# in create-field dropdowns (rule-based marks + deterministic DOC_TYPE).
V3_READONLY_LABELS = [
    "URGENCY_MARK",
    "SECRECY_MARK",
    "CIRCULATION_MARK",
    "DOC_TYPE",
]
# ALL_LABELS is kept for backward compat with code that iterates labels.
ALL_LABELS = V3_TRAIN_LABELS + V3_READONLY_LABELS

# Compact numeric badges drawn next to each field bbox.
FIELD_NUMBER_MAP = {
    "REGIME_HEADER":      0,
    "ISSUE_ORG_SUPERIOR": 1,
    "ISSUE_ORG_NAME":     2,
    "DOC_NUMBER_SYMBOL":  3,
    "PLACE_DATE":         4,
    "DOC_SUBJECT":        5,
    "ADDRESSEE":          6,
    "RECIPIENTS":         7,
    "SIGNER_ROLE":        8,
    "SIGNER_NAME":        9,
}


def get_label_color(label):
    return LABEL_COLORS.get(label, ("#6b7280", "#9ca3af"))


# Soft-delete folder name. Files excluded from a batch are moved into a
# sibling directory with this name (one per original file location, so the
# batch structure is preserved). _scan_directories skips any subtree under
# this name, so the files stop appearing in the UI immediately.
_EXCLUDED_DIR_NAME = "_excluded"


# Vietnamese display names for each KIE label. Shown wherever a label
# appears in the UI (fields tree, add/relation dialogs, status messages)
# alongside the English code in parentheses so labels stay self-documenting
# for labelers while preserving the canonical code for downstream code.
LABEL_VI = {
    # Train labels (V3)
    "REGIME_HEADER":      "Quốc hiệu",
    "ISSUE_ORG_SUPERIOR": "Cơ quan cấp trên",
    "ISSUE_ORG_NAME":     "Cơ quan ban hành",
    "DOC_NUMBER_SYMBOL":  "Số / ký hiệu văn bản",
    "PLACE_DATE":         "Địa danh & ngày ban hành",
    "DOC_SUBJECT":        "Trích yếu nội dung",
    "ADDRESSEE":          "Nơi nhận (Kính gửi)",
    "RECIPIENTS":         "Nơi nhận (danh sách)",
    "SIGNER_ROLE":        "Chức vụ người ký",
    "SIGNER_NAME":        "Tên người ký",
    # Rule-based marks
    "URGENCY_MARK":       "Mức độ khẩn",
    "SECRECY_MARK":       "Độ mật",
    "CIRCULATION_MARK":   "Chỉ dẫn lưu hành",
    # Deterministic post-process
    "DOC_TYPE":           "Loại văn bản",
}


def label_display(label: str) -> str:
    """Return the label formatted for UI display: "Tiếng Việt (CODE)".

    Falls back to just the code if no Vietnamese translation exists.
    """
    vi = LABEL_VI.get(label)
    return f"{vi} ({label})" if vi else label

def extract_stem(filename):
    name = Path(filename).stem
    if "__" in name:
        # Keep the full logical doc id and only strip the trailing hash suffix.
        # Example:
        # - digitalpdf__DIGITAL_10__3061... -> digitalpdf__DIGITAL_10
        # - A06.44...__599e... -> A06.44...
        name = name.rsplit("__", 1)[0]
    return name

# ═══════════════════════════════════════════════════════
# STYLESHEET
# ═══════════════════════════════════════════════════════
STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {BG}; color: {TEXT};
    font-family: "{FONT_UI}"; font-size: 13px;
}}
QSplitter::handle {{ background-color: {BORDER}; }}
QSplitter::handle:hover {{ background-color: {ACCENT}; }}
QPushButton {{
    background-color: {ELEVATED}; border: 1px solid {BORDER};
    border-radius: 4px; padding: 4px 12px; color: {TEXT};
    font-size: 12px; min-height: 26px;
}}
QPushButton:hover {{ background-color: {HOVER}; border-color: {ACCENT}; }}
QPushButton:pressed {{
    background-color: {ACCENT2}; border-color: {ACCENT2};
    padding-top: 5px; padding-bottom: 3px;
}}
QPushButton#btn_primary {{
    background-color: {ACCENT}; border-color: {ACCENT}; font-weight: bold;
}}
QPushButton#btn_primary:hover {{ background-color: {ACCENT2}; }}
QPushButton#btn_save {{
    background-color: {GREEN}; border-color: {GREEN}; font-weight: bold; color: white;
}}
QPushButton#btn_save:hover {{ background-color: #1e7e34; }}
QPushButton#btn_danger {{
    background-color: {RED}; border-color: {RED}; font-weight: bold; color: white;
}}
QPushButton#btn_danger:hover {{ background-color: #b02a37; }}
/* Edit-mode active state — amber is a "you're in an action, click to leave"
   warning cue without the alarm connotation of red (red is reserved for
   destructive Delete). */
QPushButton#btn_edit_active {{
    background-color: #d97706; border-color: #d97706;
    color: white; font-weight: bold;
}}
QPushButton#btn_edit_active:hover {{ background-color: #b45309; }}
QPushButton#btn_nav {{
    background-color: {ELEVATED}; border: 1px solid {BORDER};
    border-radius: 4px; padding: 4px 8px; font-size: 16px;
    font-weight: bold; min-width: 32px; min-height: 28px;
}}
QPushButton#btn_nav:hover {{ background-color: {ACCENT}; }}
QComboBox {{
    background-color: {INPUT_BG}; border: 1px solid {BORDER};
    border-radius: 4px; padding: 3px 8px; color: {TEXT};
    font-size: 12px; min-height: 26px; min-width: 140px;
}}
QComboBox:hover {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: top right;
    border: none; background: {ELEVATED}; width: 22px;
    border-top-right-radius: 4px; border-bottom-right-radius: 4px;
}}
QComboBox::down-arrow {{
    image: url({_CHEVRON_DOWN_URL}); width: 10px; height: 10px;
}}
QComboBox::down-arrow:on {{ top: 1px; }}
QComboBox QAbstractItemView {{
    background-color: {SURFACE}; border: 1px solid {BORDER};
    color: {TEXT}; selection-background-color: {ACCENT};
}}
QLabel#section_label {{ color: {MUTED}; font-size: 11px; font-weight: bold; }}
QLabel#file_indicator {{ color: {TEXT}; font-size: 13px; font-weight: bold; padding: 0 8px; }}
QLabel#file_counter {{ color: {MUTED}; font-family: "{FONT_MONO}"; font-size: 11px; padding: 0 4px; }}
QScrollArea {{ border: none; background-color: {BG}; }}
QTreeWidget {{
    background-color: {SURFACE}; border: 1px solid {BORDER};
    border-radius: 4px; outline: none; font-size: 12px;
}}
QTreeWidget::item {{ padding: 3px 4px; border-radius: 3px; }}
QTreeWidget::item:hover {{ background-color: {ELEVATED}; }}
QTreeWidget::item:selected {{
    background-color: rgba(0, 123, 255, 0.25); border: 1px solid {ACCENT};
}}
QTreeWidget::branch {{ background-color: {SURFACE}; }}
QHeaderView::section {{
    background-color: {SURFACE}; border: none;
    border-bottom: 1px solid {BORDER}; padding: 4px 8px;
    color: {MUTED}; font-size: 11px; font-weight: bold;
}}
QListWidget {{
    background-color: {SURFACE}; border: 1px solid {BORDER};
    border-radius: 4px; outline: none; font-size: 12px;
}}
QListWidget::item {{ padding: 4px 8px; border-radius: 3px; }}
QListWidget::item:hover {{ background-color: {ELEVATED}; }}
QListWidget::item:selected {{
    background-color: rgba(0, 123, 255, 0.3); color: {TEXT};
}}
QLabel#detail_label {{ color: {MUTED}; font-size: 11px; min-width: 80px; }}
QLabel#detail_value {{ color: {TEXT}; font-size: 12px; }}
QLabel#detail_value_mono {{
    color: {TEXT2}; font-family: "{FONT_MONO}", Consolas; font-size: 11px;
}}
QLabel#edit_banner {{
    background-color: rgba(0, 123, 255, 0.15); border: 1px solid {ACCENT};
    border-radius: 4px; padding: 6px 12px; color: {TEXT};
    font-size: 12px;
}}
"""


# ═══════════════════════════════════════════════════════
# PDF PAGE WIDGET
# ═══════════════════════════════════════════════════════
class PdfPageWidget(QLabel):
    # Unified signal for all word-selection edits.
    # operation ∈ {"set", "add", "remove"}:
    #   set    — drag without modifier: replace field's words with these
    #   add    — Ctrl+drag or Ctrl+click: add these to field
    #   remove — Shift+drag or Shift+click: remove these from field
    word_selection_changed = Signal(int, list, str)  # page_index, word_ids, op
    empty_clicked = Signal()  # clicked on empty area (no bbox hit, no drag)
    # Direct-manipulation signals for the PDF canvas:
    # - right-click anywhere pops a "+ Trường…" menu (non-edit mode only)
    # - left-click on an existing bbox in non-edit mode enters edit for
    #   the owning field
    context_menu_requested = Signal(int, object)     # page_index, global_pos QPoint
    bbox_clicked_non_edit = Signal(int, str)         # page_index, word_id

    def __init__(self, page_index, parent=None):
        super().__init__(parent)
        self.page_index = page_index
        self.base_pixmap = None
        self.pdf_width = 0.0
        self.pdf_height = 0.0
        self.render_scale = 1.0
        self.bbox_origin_bottom_left = False
        self.highlight_bboxes = []   # [(bbox, label, is_selected)]
        self.field_icons = []        # [(anchor_bbox, number, label, is_selected)]
        self.word_rects = []         # [(word_id, bbox)] for hit testing
        self.word_ownership = {}     # word_id -> label (which field owns it)
        self.selected_word_ids = set()  # word_ids of the currently selected field
        self.word_to_line = {}       # word_id -> line_id (for line-mode selection)
        self.line_to_words = {}      # line_id -> [word_id] (for line-mode selection)
        self.line_mode = False       # when True, single click selects whole line
        self.edit_mode = False
        self._drag_origin = None
        self._drag_current = None
        self._is_dragging = False
        self._drag_modifiers = Qt.NoModifier
        self._overlaid_pixmap = None
        self.setAlignment(Qt.AlignCenter)

    def _pixmap_offset(self):
        """Return (ox, oy) offset from widget top-left to pixmap top-left
        caused by QLabel AlignCenter."""
        pm = self.pixmap()
        if not pm:
            return QPointF(0, 0)
        ox = (self.width() - pm.width()) / 2.0
        oy = (self.height() - pm.height()) / 2.0
        return QPointF(max(0, ox), max(0, oy))

    def set_page(self, pixmap, pdf_w, pdf_h, scale):
        self.base_pixmap = pixmap
        self.pdf_width = pdf_w
        self.pdf_height = pdf_h
        self.render_scale = scale
        self.highlight_bboxes = []
        self.field_icons = []
        self.word_rects = []
        self.word_ownership = {}
        self.selected_word_ids = set()
        self._overlaid_pixmap = None
        self.setPixmap(pixmap)

    def set_highlights(self, bboxes_with_labels, field_icons=None):
        self.highlight_bboxes = bboxes_with_labels
        self.field_icons = list(field_icons or [])
        self._repaint()

    def _bbox_to_pixmap_rect(self, bbox):
        """Convert OCR bbox to pixmap rect.

        ``bbox_origin_bottom_left=True`` indicates the canonical JSON encodes
        bbox coordinates in the frame of the UPSIDE-DOWN source image (legacy
        pipeline output). That is a full 180° rotation of the reading frame,
        not merely a Y-axis flip -- we must mirror BOTH X and Y to map back
        to the visual top-left frame of the rendered page.
        """
        x0, y0, x1, y1 = bbox
        if self.bbox_origin_bottom_left and self.pdf_height and self.pdf_width:
            top_y = self.pdf_height - y1
            left_x = self.pdf_width - x1
        else:
            top_y = y0
            left_x = x0
        return QRectF(
            left_x * self.render_scale,
            top_y * self.render_scale,
            (x1 - x0) * self.render_scale,
            (y1 - y0) * self.render_scale,
        )

    def _repaint(self):
        if not self.base_pixmap:
            return
        pm = self.base_pixmap.copy()
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        s = self.render_scale

        # In edit mode: draw ALL words with clear visual states
        if self.edit_mode and self.word_rects:
            for wid, bbox in self.word_rects:
                rect = self._bbox_to_pixmap_rect(bbox)

                if wid in self.selected_word_ids:
                    # Words in the SELECTED field — drawn by highlight_bboxes below
                    continue
                elif wid in self.word_ownership:
                    # Words owned by ANOTHER field — dim with that field's color
                    label = self.word_ownership[wid]
                    dark, light = get_label_color(label)
                    fill = QColor(dark)
                    fill.setAlpha(20)
                    painter.setBrush(QBrush(fill))
                    painter.setPen(QPen(QColor(light).darker(150), 0.8))
                    painter.drawRect(rect)
                else:
                    # UNASSIGNED words — cyan outline, clearly clickable
                    fill = QColor(0, 200, 255, 15)
                    painter.setBrush(QBrush(fill))
                    painter.setPen(QPen(QColor(0, 200, 255, 120), 1.2))
                    painter.drawRect(rect)

        # Draw highlighted bboxes (selected field's words)
        for bbox, label, is_selected in self.highlight_bboxes:
            dark, light = get_label_color(label)
            fill_color = QColor(dark)
            fill_color.setAlpha(55 if is_selected else 35)
            painter.setBrush(QBrush(fill_color))
            pen_color = QColor(light)
            pen_color.setAlpha(220)
            painter.setPen(QPen(pen_color, 2.5 if is_selected else 1.5))
            painter.drawRect(self._bbox_to_pixmap_rect(bbox))

        # Draw numeric field-icons (one per field, anchored to the right of
        # its union-bbox with a short connector line).
        for anchor_bbox, number, label, is_selected in self.field_icons:
            self._draw_field_icon(painter, anchor_bbox, number, label, is_selected)

        painter.end()
        self._overlaid_pixmap = pm
        self.setPixmap(pm)

    def _draw_field_icon(self, painter, anchor_bbox, number, label, is_selected):
        """Pill (ellipse) + text + connector line, placed to the right of anchor.

        Width grows with text length so multi-instance badges like "8a", "9b"
        or "8.27" fit cleanly inside.
        """
        rect = self._bbox_to_pixmap_rect(anchor_bbox)
        if rect.isEmpty():
            return
        dark, light = get_label_color(label)
        text = str(number)

        # Pill geometry (pixmap coords) — height fixed, width grows with text
        ay = rect.center().y()
        ry = 12 if is_selected else 10  # vertical radius
        # Each extra char ~4px wider (covers up to 4 chars cleanly)
        rx = ry + max(0, len(text) - 1) * 4
        line_len = 14

        # Place on the right of bbox by default
        cx = rect.right() + line_len + rx
        # Fallback to left side if right would clip the pixmap
        pm_w = self.base_pixmap.width() if self.base_pixmap else 0
        if cx + rx > pm_w:
            cx = rect.left() - line_len - rx
            line_start_x = rect.left()
            line_end_x = cx + rx
        else:
            line_start_x = rect.right()
            line_end_x = cx - rx
        cy = ay

        # Connector line
        pen = QPen(QColor(dark), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(QPointF(line_start_x, ay), QPointF(line_end_x, cy))

        # Pill (filled dark, light border)
        painter.setBrush(QBrush(QColor(dark)))
        painter.setPen(QPen(QColor(light), 2.0 if is_selected else 1.2))
        painter.drawEllipse(QPointF(cx, cy), rx, ry)

        # Text (white, bold) — slightly smaller for multi-char badges
        painter.setPen(QColor("white"))
        base_font_size = 9 if is_selected else 8
        if len(text) >= 3:
            base_font_size -= 1
        font = QFont("Segoe UI", base_font_size)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            QRectF(cx - rx, cy - ry, 2 * rx, 2 * ry),
            Qt.AlignCenter, text,
        )

    def _draw_rubber_band(self):
        if not self._overlaid_pixmap or not self._drag_origin or not self._drag_current:
            return
        pm = self._overlaid_pixmap.copy()
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        # _drag_origin/_drag_current are already in pixmap coords
        rect = QRectF(self._drag_origin, self._drag_current).normalized()
        painter.setBrush(QBrush(QColor(0, 200, 255, 30)))
        painter.setPen(QPen(QColor(0, 200, 255, 180), 1.5, Qt.DashLine))
        painter.drawRect(rect)
        painter.end()
        self.setPixmap(pm)

    def mousePressEvent(self, event):
        # Right-click anywhere on the page → ask the parent to show a label
        # picker menu. The parent handles "pick label → enter edit → create
        # empty field ready for the next drag".
        if event.button() == Qt.RightButton:
            global_pos = event.globalPosition().toPoint()
            self.context_menu_requested.emit(self.page_index, global_pos)
            return
        # Left-click in non-edit mode:
        # - hit an existing bbox belonging to a field → emit a signal so the
        #   parent can enter edit mode on that field
        # - miss bboxes → do nothing (so ordinary clicks outside don't
        #   accidentally toggle UI state)
        if not self.edit_mode and event.button() == Qt.LeftButton:
            off = self._pixmap_offset()
            pos = event.position() - off
            for wid, bbox in self.word_rects:
                if self._bbox_to_pixmap_rect(bbox).contains(pos):
                    self.bbox_clicked_non_edit.emit(self.page_index, wid)
                    return
            return
        if not self.edit_mode or event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        off = self._pixmap_offset()
        self._drag_origin = event.position() - off
        self._drag_current = self._drag_origin
        self._is_dragging = False
        # Snapshot modifiers at press time (release time may differ if user
        # releases the modifier mid-drag).
        self._drag_modifiers = event.modifiers()

    def mouseMoveEvent(self, event):
        if not self.edit_mode or self._drag_origin is None:
            return super().mouseMoveEvent(event)
        off = self._pixmap_offset()
        pos = event.position() - off
        delta = pos - self._drag_origin
        if not self._is_dragging and (abs(delta.x()) > 4 or abs(delta.y()) > 4):
            self._is_dragging = True
        if self._is_dragging:
            self._drag_current = pos
            self._draw_rubber_band()

    def mouseReleaseEvent(self, event):
        if not self.edit_mode or event.button() != Qt.LeftButton:
            self._reset_drag()
            return super().mouseReleaseEvent(event)
        off = self._pixmap_offset()
        pos = event.position() - off
        modifiers = self._drag_modifiers
        was_dragging = self._is_dragging

        if was_dragging:
            # Rubber-band selection: collect every word whose bbox INTERSECTS
            # the dragged rectangle (forgiving — any overlap counts).
            rect = QRectF(self._drag_origin, self._drag_current).normalized()
            hits = []
            for wid, bbox in self.word_rects:
                word_rect = self._bbox_to_pixmap_rect(bbox)
                if rect.intersects(word_rect):
                    hits.append(wid)
            self._reset_drag()
            if not hits:
                self._repaint()  # clear rubber band visual
                return
            op = self._operation_from_modifiers(modifiers, default="set")
            self.word_selection_changed.emit(self.page_index, hits, op)
        else:
            # Single click on bbox: TOGGLE by default (intuitive — click a
            # word to add it; click again to remove). Modifiers force a
            # specific direction. In line_mode a click selects every word
            # on the same OCR line as the clicked word.
            self._reset_drag()
            for wid, bbox in self.word_rects:
                if self._bbox_to_pixmap_rect(bbox).contains(pos):
                    op = self._operation_from_modifiers(modifiers, default="toggle")
                    if self.line_mode:
                        line_id = self.word_to_line.get(wid)
                        sibling_ids = list(self.line_to_words.get(line_id, []) or [wid])
                        self.word_selection_changed.emit(self.page_index, sibling_ids, op)
                    else:
                        self.word_selection_changed.emit(self.page_index, [wid], op)
                    return
            # No bbox hit — empty area click → deselect field
            self.empty_clicked.emit()

    def _reset_drag(self):
        self._drag_origin = None
        self._drag_current = None
        self._is_dragging = False
        self._drag_modifiers = Qt.NoModifier

    @staticmethod
    def _operation_from_modifiers(modifiers, default):
        """Map Ctrl → 'add', Shift → 'remove', neither → default."""
        if modifiers & Qt.ControlModifier:
            return "add"
        if modifiers & Qt.ShiftModifier:
            return "remove"
        return default


# ═══════════════════════════════════════════════════════
# MAIN WINDOW
# ═══════════════════════════════════════════════════════
class KieViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("KIE Viewer")
        self.setMinimumSize(1200, 700)

        # Data
        self.input_files = {}
        self.output_files = {}
        self.batches = {}
        self.current_batch_stems = []
        self.current_index = -1
        self.current_stem = ""
        self.input_json = None
        self.output_json = None
        self.canonical_json = None  # Full OCR with all pages (bbox source)
        self.fitz_doc = None
        self.page_widgets = []
        # IMPORTANT: don't store the field DICT directly — Qt's QVariantMap
        # conversion makes a copy on access via QTreeWidgetItem.setData/data.
        # Track only the field_id (string) and look up the live dict on
        # every access via the `selected_field` property.
        self._selected_field_id = None
        self.zoom = 1.5

        # Edit mode
        self.edit_mode = False
        self.line_mode = False  # when True, single click selects the whole OCR line
        self._dirty = False  # backing store for `dirty` property below
        self.undo_stack = []
        self.redo_stack = []

        # Re-entry guard for _render_pdf (processEvents inside the render loop
        # can trigger another navigation → another _render_pdf call, which
        # would swap self.fitz_doc mid-iteration → IndexError).
        self._render_gen = 0
        self._is_loading = False

        # Viewport-based render state (Tier 1 — lazy render only pages the
        # user can actually see, plus a 1-viewport buffer above/below).
        self._render_queue: "deque[int]" = deque()
        self._render_queued: set = set()         # pages already in queue or rendered
        self._render_active = False              # single-slot render guard
        self._scroll_debounce_pending = False    # coalesce rapid scroll events

        # Pixmap cache — reloading a file or revisiting a rendered page hits
        # this cache and skips fitz entirely.
        self._pixmap_cache = PixmapCache(max_bytes=300 * 1024 * 1024)

        # Classified-document detection cache. Maps stem -> matched keyword
        # ("TUYỆT MẬT" / "TỐI MẬT" / "MẬT") or None. Invalidated on save.
        self._secret_cache: dict = {}
        self._secret_emitter = SecretScanEmitter()
        self._secret_emitter.secret_detected.connect(
            self._on_secret_detected, Qt.QueuedConnection
        )
        self._secret_scan_queue: "deque[str]" = deque()
        self._secret_scan_submitted: set = set()
        self._secret_scan_lock = threading.Lock()
        self._secret_scan_thread = None

        # Prefetch (Tier 2) — after a file's VIEWPORT_READY, warm the cache
        # for adjacent files (±1, ±2) in the batch so clicking next/prev is
        # an instant cache hit. Worker threads render the priority page off
        # the UI thread and post the result back via PrefetchEmitter.
        self._prefetch_emitter = PrefetchEmitter()
        self._prefetch_emitter.prefetch_done.connect(
            self._on_prefetch_done, Qt.QueuedConnection
        )
        self._prefetch_submitted: set = set()
        self._prefetch_lock = threading.Lock()

        self._build_ui()
        self.setStyleSheet(STYLESHEET)
        self._scan_directories()

        # Warm the ONNX orientation classifier session in a BACKGROUND thread
        # so the first file's per-page rotation detection doesn't pay a
        # 300-400ms cold-start cost on the UI thread. Session creation is
        # cached via lru_cache; once it completes, subsequent inferences are
        # ~20ms each. onnxruntime releases the GIL during session init, so
        # this is truly non-blocking for the Qt event loop.
        def _warm_orientation_session():
            try:
                from scanindex.core.preprocessing.preprocessing import _get_orientation_classifier_session
                t0 = time.perf_counter()
                _get_orientation_classifier_session()
                dt_ms = (time.perf_counter() - t0) * 1000.0
                print(f"[warm] orientation classifier session ready ({dt_ms:.0f}ms)")
            except Exception as e:
                print(f"[warm] orientation classifier warm-up failed: {e}")
        threading.Thread(target=_warm_orientation_session, daemon=True).start()

    # ═══════════════════════════════════════════════════
    # SELECTED FIELD — always look up the LIVE dict in output_json
    # (storing the dict directly in QTreeWidgetItem yields a copy because
    # of QVariantMap conversion, so mutations would never propagate.)
    # ═══════════════════════════════════════════════════
    @property
    def selected_field(self):
        if not self._selected_field_id:
            return None
        for f in self._get_fields():
            if f.get("field_id") == self._selected_field_id:
                return f
        # ID exists but field gone (e.g. deleted) → clear stale ID
        self._selected_field_id = None
        return None

    @selected_field.setter
    def selected_field(self, value):
        if value is None:
            self._selected_field_id = None
        elif isinstance(value, str):
            self._selected_field_id = value
        else:
            self._selected_field_id = value.get("field_id") if value else None
        # Selection change drives the destructive Delete button's visibility
        self._update_delete_visibility()

    # ═══════════════════════════════════════════════════
    # DIRTY STATE — auto-toggles the Save button when changes exist so the
    # button reflects exactly one invariant: "there is something to save".
    # ═══════════════════════════════════════════════════
    @property
    def dirty(self):
        return self._dirty

    @dirty.setter
    def dirty(self, value):
        self._dirty = bool(value)
        self._update_save_visibility()

    def _update_save_visibility(self):
        """Show btn_save ⟺ edit_mode AND unsaved changes exist.

        Relies on the button's size policy having ``setRetainSizeWhenHidden``
        enabled (configured in _build_ui) so toggling visibility never shifts
        neighbouring buttons.
        """
        btn = getattr(self, "btn_save", None)
        if btn is None:
            return
        btn.setVisible(bool(self._dirty) and self.edit_mode)

    # ═══════════════════════════════════════════════════
    # SCAN
    # ═══════════════════════════════════════════════════
    def _scan_directories(self):
        self.input_files = {}
        self.output_files = {}
        self.batches = {}

        for d, store, ext in [
            (DEFAULT_INPUT_DIR, self.input_files, ".json"),
        ]:
            if not os.path.isdir(d):
                continue
            for root_dir, dirs, files in os.walk(d):
                # Hide soft-deleted files: files under any "_excluded" subtree
                # should not reappear in the batch list.
                dirs[:] = [x for x in dirs if x != _EXCLUDED_DIR_NAME]
                for f in files:
                    if f.lower().endswith(ext):
                        store[extract_stem(f)] = os.path.join(root_dir, f)

        if os.path.isdir(DEFAULT_OUTPUT_DIR):
            for root_dir, dirs, files in os.walk(DEFAULT_OUTPUT_DIR):
                dirs[:] = [x for x in dirs if x != _EXCLUDED_DIR_NAME]
                for f in files:
                    if f.lower().endswith(".json"):
                        stem = extract_stem(f)
                        self.output_files[stem] = os.path.join(root_dir, f)
                        rel = os.path.relpath(root_dir, DEFAULT_OUTPUT_DIR)
                        batch = rel if rel != "." else "(root)"
                        self.batches.setdefault(batch, []).append(stem)
        for b in self.batches:
            self.batches[b].sort()
        self._populate_batches()

    def _browse_for_directory(self, line_edit: QLineEdit):
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Chọn thư mục",
            line_edit.text().strip() or str(Path.home()),
        )
        if chosen:
            line_edit.setText(chosen)

    def _reload_sources(self, preserve_batch=None, preserve_stem=None):
        self._scan_directories()

        if not self.batches:
            if self.fitz_doc:
                self.fitz_doc.close()
                self.fitz_doc = None
            self.output_json = None
            self.input_json = None
            self.canonical_json = None
            self.current_batch_stems = []
            self.current_index = -1
            self.current_stem = ""
            for pw in self.page_widgets:
                pw.hide()
                pw.deleteLater()
            self.page_widgets = []
            self.file_list.clear()
            self.file_list_count.setText("0")
            self._update_nav_display()
            self.pdf_empty.setText("Không tìm thấy batch output")
            self.pdf_empty.show()
            self.fields_tree.clear()
            self.fields_count.setText("0")
            self._clear_details()
            self._update_status()
            return

        batch_to_select = preserve_batch if preserve_batch in self.batches else sorted(self.batches.keys())[0]
        batch_idx = self.batch_combo.findData(batch_to_select)
        if batch_idx < 0:
            return

        self.batch_combo.blockSignals(True)
        self.batch_combo.setCurrentIndex(batch_idx)
        self.batch_combo.blockSignals(False)
        self._on_batch_changed()

        if preserve_stem and preserve_stem in self.current_batch_stems:
            row = self.current_batch_stems.index(preserve_stem)
            self.current_index = row
            self.file_list.blockSignals(True)
            self.file_list.setCurrentRow(row)
            self.file_list.blockSignals(False)
            self._load_current_index()

    def _open_config_dialog(self):
        if not self._check_unsaved():
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Cấu hình đường dẫn")
        dialog.setModal(True)
        dialog.resize(860, 260)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        intro = QLabel("Sửa 4 đường dẫn mặc định của KIE Viewer. Thay đổi sẽ được lưu vào kie_viewer_config.json.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {TEXT2};")
        layout.addWidget(intro)

        field_specs = [
            ("input_dir", "json_input"),
            ("output_dir", "json_output_labeled"),
            ("ocr_dir", "ocr"),
        ]
        edits = {}
        for key, label in field_specs:
            row = QHBoxLayout()
            row.setSpacing(8)
            lbl = QLabel(label)
            lbl.setFixedWidth(140)
            edit = QLineEdit(PATH_CONFIG.get(key, ""))
            edit.setPlaceholderText(label)
            browse = QPushButton("...")
            browse.setFixedWidth(34)
            browse.clicked.connect(partial(self._browse_for_directory, edit))
            row.addWidget(lbl)
            row.addWidget(edit, 1)
            row.addWidget(browse)
            layout.addLayout(row)
            edits[key] = edit

        config_hint = QLabel(f"File cấu hình: {CONFIG_PATH}")
        config_hint.setWordWrap(True)
        config_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        layout.addWidget(config_hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return

        new_config = {}
        for key, _ in field_specs:
            value = edits[key].text().strip()
            if not value:
                QMessageBox.warning(self, "Lỗi", "Không được để trống đường dẫn cấu hình.")
                return
            new_config[key] = value

        try:
            _save_path_config(new_config)
            _apply_path_config(new_config)
            self._reload_sources(
                preserve_batch=self.batch_combo.currentData(),
                preserve_stem=self.current_stem,
            )
            self._flash_button(self.btn_config, "✓ Đã lưu", 1500)
        except Exception as exc:
            QMessageBox.warning(self, "Lỗi", f"Không thể lưu cấu hình:\n{exc}")

    def _resolve_pdf_path(self, stem, input_json=None):
        if not stem:
            return None

        source_canonical_json = None
        if isinstance(input_json, dict):
            source_canonical_json = input_json.get("source_canonical_json")
        elif stem in self.input_files:
            raw_input = self._read_json(self.input_files.get(stem))
            if isinstance(raw_input, dict):
                source_canonical_json = raw_input.get("source_canonical_json")

        # Prefer the OCR-rendered PDF that lives next to the canonical JSON.
        # This keeps page geometry aligned with OCR boxes and avoids depending
        # on the original PDF tree layout.
        if source_canonical_json:
            canonical_path = Path(source_canonical_json)
            if canonical_path.exists():
                ocr_pdf_path = canonical_path.with_suffix("")
                if ocr_pdf_path.exists():
                    return str(ocr_pdf_path.resolve())

        guessed = os.path.join(DEFAULT_OCR_DIR, f"{stem}_ocr.pdf")
        if os.path.isfile(guessed):
            return guessed

        return None

    # ═══════════════════════════════════════════════════
    # BUILD UI
    # ═══════════════════════════════════════════════════
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── TOP BAR ──
        topbar = QWidget()
        topbar.setStyleSheet(f"background-color: {SURFACE}; border-bottom: 1px solid {BORDER};")
        tbl = QHBoxLayout(topbar)
        tbl.setContentsMargins(12, 6, 12, 6)
        tbl.setSpacing(12)

        lbl_batch = QLabel("BATCH")
        lbl_batch.setObjectName("section_label")
        self.batch_combo = QComboBox()
        self.batch_combo.addItem("-- Chọn batch --", "")
        # ClickFocus: combo only accepts focus when clicked, never from Tab.
        # Keeps Up/Down arrows from accidentally landing on the combo and
        # flipping the batch while the user is navigating files.
        self.batch_combo.setFocusPolicy(Qt.ClickFocus)
        self.batch_combo.currentIndexChanged.connect(self._on_batch_changed)

        self.file_label = QLabel("--")
        self.file_label.setObjectName("file_indicator")
        self.file_label.setAlignment(Qt.AlignCenter)
        self.file_counter = QLabel("")
        self.file_counter.setObjectName("file_counter")

        self.btn_refresh = QPushButton("Làm mới (F5)")
        self.btn_refresh.setObjectName("btn_primary")
        self.btn_refresh.clicked.connect(self._refresh)

        self.btn_config = QPushButton("Cấu hình")
        self.btn_config.clicked.connect(self._open_config_dialog)

        self.btn_edit = QPushButton("Sửa")
        self.btn_edit.setCheckable(True)
        self.btn_edit.clicked.connect(self._toggle_edit_mode)

        self.btn_save = QPushButton("Lưu")
        self.btn_save.setObjectName("btn_save")
        self.btn_save.clicked.connect(self._save)
        self.btn_save.hide()

        self.btn_add_field = QPushButton("+ Field")
        self.btn_add_field.setToolTip("Thêm một field KIE mới")
        self.btn_add_field.clicked.connect(self._add_field)
        self.btn_add_field.hide()

        self.btn_add_relation = QPushButton("+ Relation")
        self.btn_add_relation.setToolTip("Tạo relation signed_by (SIGNER_ROLE → SIGNER_NAME)")
        self.btn_add_relation.clicked.connect(self._add_relation)
        self.btn_add_relation.hide()

        self.btn_delete_field = QPushButton("Xoá field")
        self.btn_delete_field.setObjectName("btn_danger")
        self.btn_delete_field.setToolTip("Xoá field KIE đang được chọn")
        self.btn_delete_field.clicked.connect(self._delete_field)
        self.btn_delete_field.hide()

        self.chk_line_mode = QCheckBox("Line mode")
        self.chk_line_mode.setToolTip(
            "Bật: click 1 từ → chọn cả dòng OCR. Tắt: click 1 từ → chọn 1 từ."
        )
        self.chk_line_mode.toggled.connect(self._on_line_mode_toggled)
        self.chk_line_mode.hide()

        # Retain layout slot when these buttons are hidden so toggling their
        # visibility (edit mode, dirty state, selection) never shifts the
        # surrounding toolbar items around.
        for _btn in (self.btn_save, self.btn_add_field, self.btn_add_relation,
                     self.btn_delete_field, self.chk_line_mode):
            _policy = _btn.sizePolicy()
            _policy.setRetainSizeWhenHidden(True)
            _btn.setSizePolicy(_policy)

        self.btn_rotate = QPushButton("⟲ Xoay")
        self.btn_rotate.setToolTip(
            "Xoay PDF thêm 90° (dùng khi chữ hiển thị bị ngược so với bbox OCR)."
        )
        self.btn_rotate.clicked.connect(self._rotate_current_file)

        tbl.addWidget(lbl_batch)
        tbl.addWidget(self.batch_combo)
        tbl.addSpacing(4)
        tbl.addWidget(self.file_label, 1)
        tbl.addWidget(self.file_counter)
        tbl.addSpacing(4)
        tbl.addWidget(self.btn_config)
        tbl.addWidget(self.btn_refresh)
        tbl.addWidget(self.btn_rotate)
        tbl.addWidget(self.btn_edit)
        tbl.addWidget(self.chk_line_mode)
        tbl.addWidget(self.btn_add_field)
        tbl.addWidget(self.btn_add_relation)
        tbl.addWidget(self.btn_delete_field)
        tbl.addWidget(self.btn_save)
        root.addWidget(topbar)

        # ── MAIN AREA ──
        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setHandleWidth(4)

        # Left: File list
        file_list_widget = QWidget()
        fl_layout = QVBoxLayout(file_list_widget)
        fl_layout.setContentsMargins(0, 0, 0, 0)
        fl_layout.setSpacing(0)

        fl_header = QWidget()
        fl_header.setStyleSheet(f"background-color: {SURFACE}; border-bottom: 1px solid {BORDER};")
        flh = QHBoxLayout(fl_header)
        flh.setContentsMargins(8, 4, 8, 4)
        flh.addWidget(self._make_label("DANH SÁCH FILE"))
        self.file_list_count = QLabel("0")
        self.file_list_count.setStyleSheet(f"color: {MUTED}; font-family: '{FONT_MONO}'; font-size: 11px;")
        self.btn_copy_checked = QPushButton("Copy checked")
        self.btn_copy_checked.setFixedHeight(22)
        self.btn_copy_checked.setStyleSheet(f"font-size: 10px; padding: 0 6px;")
        self.btn_copy_checked.clicked.connect(self._copy_checked_files)
        self.btn_delete_checked = QPushButton("Xoá đã chọn")
        self.btn_delete_checked.setObjectName("btn_danger")
        self.btn_delete_checked.setFixedHeight(22)
        self.btn_delete_checked.setStyleSheet(
            f"QPushButton#btn_danger {{ background-color: {RED}; border-color: {RED}; "
            f"color: white; font-size: 10px; padding: 0 8px; font-weight: bold; }}"
            f"QPushButton#btn_danger:hover {{ background-color: #b02a37; }}"
        )
        self.btn_delete_checked.setToolTip(
            "Xoá HẲN các file đã check khỏi disk (input JSON, output JSON, "
            "canonical JSON, _ocr.pdf). Không thể khôi phục."
        )
        self.btn_delete_checked.clicked.connect(self._delete_checked_files)
        flh.addStretch()
        flh.addWidget(self.btn_delete_checked)
        flh.addWidget(self.btn_copy_checked)
        flh.addWidget(self.file_list_count)
        fl_layout.addWidget(fl_header)

        self.file_list = QListWidget()
        self.file_list.currentRowChanged.connect(self._on_file_list_row_changed)
        fl_layout.addWidget(self.file_list)
        self.main_splitter.addWidget(file_list_widget)

        # Center: PDF
        pdf_widget = QWidget()
        pdf_layout = QVBoxLayout(pdf_widget)
        pdf_layout.setContentsMargins(0, 0, 0, 0)
        pdf_layout.setSpacing(0)

        pdf_header = QWidget()
        pdf_header.setStyleSheet(f"background-color: {SURFACE}; border-bottom: 1px solid {BORDER};")
        ph = QHBoxLayout(pdf_header)
        ph.setContentsMargins(12, 4, 12, 4)
        ph.addWidget(self._make_label("PDF VIEWER"))
        ph.addSpacing(8)
        self.chk_last_page = QCheckBox("Xem người ký")
        self.chk_last_page.setStyleSheet(f"color: {TEXT2}; font-size: 11px;")
        ph.addWidget(self.chk_last_page)
        ph.addStretch()
        for text, slot in [("-", lambda: self._set_zoom(-0.25)),
                           (None, None),
                           ("+", lambda: self._set_zoom(0.25)),
                           ("Fit", self._zoom_fit)]:
            if text is None:
                self.zoom_label = QLabel(f"{int(self.zoom * 100)}%")
                self.zoom_label.setStyleSheet(f"color: {MUTED}; font-family: '{FONT_MONO}'; font-size: 11px; min-width: 40px;")
                self.zoom_label.setAlignment(Qt.AlignCenter)
                ph.addWidget(self.zoom_label)
            else:
                b = QPushButton(text)
                b.setFixedSize(36 if text == "Fit" else 26, 26)
                b.clicked.connect(slot)
                ph.addWidget(b)
        pdf_layout.addWidget(pdf_header)

        self.pdf_scroll = QScrollArea()
        self.pdf_scroll.setWidgetResizable(True)
        # Pin vertical scrollbar so viewport width stays constant whether the
        # current file has 1 page or many — otherwise fit zoom changes per
        # file and tall pages overflow right edge.
        self.pdf_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        # Remove the QScrollArea frame (default ~2px per side) so viewport()
        # width matches the visible inner area exactly.
        self.pdf_scroll.setFrameShape(QFrame.NoFrame)
        self.pdf_scroll.setStyleSheet("background-color: #1e1e1e;")
        # Forbid the scroll area from grabbing keyboard focus. Otherwise
        # Up/Down/Left/Right would scroll the page instead of falling
        # through to the file-navigation shortcuts (the user expects arrow
        # keys to ALWAYS step prev/next file). Mouse wheel still scrolls.
        self.pdf_scroll.setFocusPolicy(Qt.NoFocus)
        self.pdf_pages_widget = QWidget()
        self.pdf_pages_widget.setFocusPolicy(Qt.NoFocus)
        self.pdf_pages_layout = QVBoxLayout(self.pdf_pages_widget)
        self.pdf_pages_layout.setContentsMargins(16, 16, 16, 16)
        self.pdf_pages_layout.setSpacing(12)
        self.pdf_pages_layout.setAlignment(Qt.AlignHCenter)
        self.pdf_empty = QLabel("Chọn batch để bắt đầu")
        self.pdf_empty.setAlignment(Qt.AlignCenter)
        self.pdf_empty.setStyleSheet(f"color: {MUTED}; font-size: 14px; padding: 60px;")
        self.pdf_pages_layout.addWidget(self.pdf_empty)
        self.pdf_scroll.setWidget(self.pdf_pages_widget)
        # Hook viewport-based lazy render: on scroll, debounced handler queues
        # any newly-visible pages for render.
        self.pdf_scroll.verticalScrollBar().valueChanged.connect(self._schedule_viewport_update)
        pdf_layout.addWidget(self.pdf_scroll)

        # Render progress overlay — compact badge pinned to the bottom of
        # the pdf_scroll viewport during progressive rendering.
        self.render_overlay = QLabel(self.pdf_scroll.viewport())
        self.render_overlay.setAlignment(Qt.AlignCenter)
        self.render_overlay.setStyleSheet(
            f"background-color: rgba(0,0,0,180); color: {TEXT}; "
            f"font-size: 11px; font-weight: bold; padding: 4px 10px; "
            f"border-radius: 10px; border: 1px solid {ACCENT};"
        )
        self.render_overlay.hide()

        self.main_splitter.addWidget(pdf_widget)

        # Right: Fields + Details
        self.right_splitter = QSplitter(Qt.Vertical)
        self.right_splitter.setHandleWidth(4)

        # Fields panel
        fields_w = QWidget()
        fields_l = QVBoxLayout(fields_w)
        fields_l.setContentsMargins(0, 0, 0, 0)
        fields_l.setSpacing(0)

        fh_w = QWidget()
        fh_w.setStyleSheet(f"background-color: {SURFACE}; border-bottom: 1px solid {BORDER};")
        fh = QHBoxLayout(fh_w)
        fh.setContentsMargins(12, 4, 12, 4)
        fh.addWidget(self._make_label("TRƯỜNG KIE"))
        self.fields_count = QLabel("0")
        self.fields_count.setStyleSheet(f"color: {MUTED}; font-family: '{FONT_MONO}'; font-size: 11px;")
        fh.addStretch()
        fh.addWidget(self.fields_count)
        fields_l.addWidget(fh_w)

        # Edit banner (hidden by default; text/style updated by _update_edit_banner)
        self.edit_banner = QLabel("")
        self.edit_banner.setObjectName("edit_banner")
        self.edit_banner.setAlignment(Qt.AlignCenter)
        self.edit_banner.setWordWrap(True)
        self.edit_banner.hide()
        fields_l.addWidget(self.edit_banner)

        self.fields_tree = QTreeWidget()
        self.fields_tree.setHeaderLabels(["Nhãn / Văn bản", "Conf"])
        self.fields_tree.setColumnCount(2)
        self.fields_tree.header().setStretchLastSection(False)
        self.fields_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.fields_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.fields_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.fields_tree.setRootIsDecorated(True)
        self.fields_tree.setAnimated(True)
        self.fields_tree.itemClicked.connect(self._on_field_clicked)
        fields_l.addWidget(self.fields_tree)
        self.right_splitter.addWidget(fields_w)

        # Details panel
        det_w = QWidget()
        det_l = QVBoxLayout(det_w)
        det_l.setContentsMargins(0, 0, 0, 0)
        det_l.setSpacing(0)
        dh_w = QWidget()
        dh_w.setStyleSheet(f"background-color: {SURFACE}; border-bottom: 1px solid {BORDER};")
        dh = QHBoxLayout(dh_w)
        dh.setContentsMargins(12, 4, 12, 4)
        dh.addWidget(self._make_label("CHI TIẾT VÙNG KIE"))
        det_l.addWidget(dh_w)

        self.details_scroll = QScrollArea()
        self.details_scroll.setWidgetResizable(True)
        self.details_content = QWidget()
        self.details_layout = QVBoxLayout(self.details_content)
        self.details_layout.setContentsMargins(12, 8, 12, 8)
        self.details_layout.setSpacing(4)
        self.details_layout.setAlignment(Qt.AlignTop)
        self._show_details_empty()
        self.details_scroll.setWidget(self.details_content)
        det_l.addWidget(self.details_scroll)
        self.right_splitter.addWidget(det_w)

        self.right_splitter.setSizes([400, 300])
        self.main_splitter.addWidget(self.right_splitter)
        self.main_splitter.setSizes([180, 650, 400])

        root.addWidget(self.main_splitter, 1)

        # Status bar
        sb_w = QWidget()
        sb_w.setFixedHeight(24)
        sb_w.setStyleSheet(f"background-color: {SURFACE}; border-top: 1px solid {BORDER};")
        sb = QHBoxLayout(sb_w)
        sb.setContentsMargins(12, 0, 12, 0)
        self.status_left = QLabel("--")
        self.status_left.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.status_right = QLabel("← → chuyển file  |  F5 làm mới  |  Esc bỏ chọn  |  Ctrl+Z/Y hoàn tác")
        self.status_right.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        sb.addWidget(self.status_left)
        sb.addStretch()
        sb.addWidget(self.status_right)
        root.addWidget(sb_w)

        # Shortcuts
        QShortcut(QKeySequence("F5"), self, self._refresh)
        QShortcut(QKeySequence("Escape"), self, self._deselect)
        QShortcut(QKeySequence("Ctrl+Z"), self, self._undo,
                  context=Qt.ApplicationShortcut)
        QShortcut(QKeySequence("Ctrl+Y"), self, self._redo,
                  context=Qt.ApplicationShortcut)
        # All four arrow keys step to the prev/next file — users expect
        # arrow keys to navigate documents, never the PDF viewport.
        # ApplicationShortcut lets QLineEdit / QTextEdit still consume the
        # key (text widgets call accept() first), preserving text editing
        # in dialogs. The scroll area has NoFocus so it never wins the key.
        QShortcut(QKeySequence(Qt.Key_Left), self, self._prev_file,
                  context=Qt.ApplicationShortcut)
        QShortcut(QKeySequence(Qt.Key_Up), self, self._prev_file,
                  context=Qt.ApplicationShortcut)
        QShortcut(QKeySequence(Qt.Key_Right), self, self._next_file,
                  context=Qt.ApplicationShortcut)
        QShortcut(QKeySequence(Qt.Key_Down), self, self._next_file,
                  context=Qt.ApplicationShortcut)

    def _make_label(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("section_label")
        return lbl

    # ═══════════════════════════════════════════════════
    # BATCH & FILE NAVIGATION
    # ═══════════════════════════════════════════════════
    def _populate_batches(self):
        self.batch_combo.blockSignals(True)
        self.batch_combo.clear()
        self.batch_combo.addItem("-- Chọn batch --", "")
        for b in sorted(self.batches.keys()):
            self.batch_combo.addItem(f"{b} ({len(self.batches[b])})", b)
        self.batch_combo.blockSignals(False)

    def _on_batch_changed(self):
        if self._is_loading:
            return
        if not self._check_unsaved():
            return
        batch = self.batch_combo.currentData()
        if not batch:
            self.current_batch_stems = []
            self.current_index = -1
            self.file_list.clear()
            self._update_nav_display()
            return
        stems = self.batches.get(batch, [])
        # Keep every labeled/input stem in the batch, even when a matching PDF file
        # is missing on disk. The file list UI already marks "Không có PDF" items,
        # and dropping them here hides valid JSON pairs such as digitalpdf__DIGITAL_*.
        self.current_batch_stems = stems[:]
        self._populate_file_list()
        self.current_index = 0 if self.current_batch_stems else -1
        if self.current_index >= 0:
            self.file_list.blockSignals(True)
            self.file_list.setCurrentRow(0)
            self.file_list.blockSignals(False)
            self._load_current_index()
            # Move focus off the combo so Up/Down arrows navigate files,
            # not flip the batch back and forth.
            self.file_list.setFocus()

    def _populate_file_list(self):
        self.file_list.clear()
        for stem in self.current_batch_stems:
            item = QListWidgetItem(stem)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._apply_secret_styling(item, stem)
            self.file_list.addItem(item)
        self.file_list_count.setText(str(len(self.current_batch_stems)))
        # Kick off background canonical-JSON secrecy scan. Items whose
        # status is unknown start with default styling and recolor when the
        # worker returns a match.
        self._schedule_secret_scan(self.current_batch_stems)

    def _on_file_list_row_changed(self, row):
        if self._is_loading:
            return
        if row < 0 or row >= len(self.current_batch_stems):
            return
        if not self._check_unsaved():
            # Revert selection
            self.file_list.blockSignals(True)
            self.file_list.setCurrentRow(self.current_index)
            self.file_list.blockSignals(False)
            return
        self.current_index = row
        self._load_current_index()

    # ──────────────────────────────────────────────────────────────────
    # Hard delete: "Xoá đã chọn" — tick boxes in the file list then click
    # the red "Xoá đã chọn" button.
    # ──────────────────────────────────────────────────────────────────
    # ──────────────────────────────────────────────────────────────────
    # Classified-document detection (Văn bản MẬT)
    # ──────────────────────────────────────────────────────────────────
    def _find_canonical_path(self, stem: str):
        """Return absolute path to canonical OCR JSON for ``stem`` or None."""
        inp = self.input_files.get(stem)
        if inp and os.path.isfile(inp):
            try:
                raw = _fast_json_load(inp)
                if isinstance(raw, dict):
                    hint = raw.get("source_canonical_json")
                    if hint and os.path.isfile(hint):
                        return os.path.abspath(hint)
            except Exception:
                pass
        guessed = os.path.join(DEFAULT_OCR_DIR, f"{stem}_ocr.pdf.json")
        if os.path.isfile(guessed):
            return guessed
        return None

    def _detect_secret(self, stem: str):
        """Return the secrecy keyword for ``stem`` or None.

        ALWAYS scans the canonical OCR JSON with the pipeline's rule
        (:func:`scanindex.core.kie.inference_pipeline.detect_secrecy_mark`). Output
        JSON is intentionally NOT used as a primary source — the labeler's
        workflow leaves SECRECY_MARK missing from most output files, so
        relying on it would silently under-report.

        Sync path (used by :meth:`_refresh_current_file_item_styling`).
        The batch-load path prefers :meth:`_schedule_secret_scan` which
        runs the same detection in a background thread so opening a
        100-file batch doesn't block the UI for seconds.

        Cached per stem; invalidate via :meth:`_invalidate_secret_cache`.
        """
        if stem in self._secret_cache:
            return self._secret_cache[stem]
        result = None
        if detect_secrecy_mark is not None:
            canonical = self._find_canonical_path(stem)
            if canonical:
                try:
                    doc = _fast_json_load(canonical)
                    result = detect_secrecy_mark(doc)
                except Exception:
                    pass
        self._secret_cache[stem] = result
        return result

    def _invalidate_secret_cache(self, stem: str = None):
        """Drop cached secret-detection entries. No arg = clear all."""
        if stem is None:
            self._secret_cache.clear()
        else:
            self._secret_cache.pop(stem, None)

    def _schedule_secret_scan(self, stems):
        """Queue ``stems`` for background canonical-JSON secrecy detection.

        Items whose result is already cached are applied to the UI
        immediately. The rest are enqueued for a single worker daemon
        thread which streams results back via ``SecretScanEmitter`` so the
        list colors in progressively from top to bottom without ever
        blocking the main thread.
        """
        if detect_secrecy_mark is None:
            return
        new_enqueued = 0
        for stem in stems:
            if stem in self._secret_cache:
                continue  # default styling path already chose the right color
            with self._secret_scan_lock:
                if stem in self._secret_scan_submitted:
                    continue
                self._secret_scan_submitted.add(stem)
                self._secret_scan_queue.append(stem)
                new_enqueued += 1
        if new_enqueued > 0:
            self._ensure_secret_scan_thread()

    def _ensure_secret_scan_thread(self):
        with self._secret_scan_lock:
            t = self._secret_scan_thread
            if t is not None and t.is_alive():
                return
            self._secret_scan_thread = threading.Thread(
                target=self._secret_scan_worker,
                daemon=True,
                name="kie-secret-scan",
            )
            self._secret_scan_thread.start()

    def _secret_scan_worker(self):
        """Drain ``_secret_scan_queue`` one canonical at a time. Emits
        ``secret_detected(stem, keyword_or_empty)`` for each stem.
        """
        emitter = self._secret_emitter
        while True:
            with self._secret_scan_lock:
                if not self._secret_scan_queue:
                    return
                stem = self._secret_scan_queue.popleft()
            canonical = self._find_canonical_path(stem)
            kw = ""
            if canonical:
                try:
                    doc = _fast_json_load(canonical)
                    matched = detect_secrecy_mark(doc) if detect_secrecy_mark else None
                    kw = matched or ""
                except Exception:
                    kw = ""
            emitter.secret_detected.emit(stem, kw)

    def _on_secret_detected(self, stem: str, keyword: str):
        """Main-thread slot: cache the scan result and re-paint the matching
        list item. ``keyword == ""`` means "not classified"."""
        result = keyword if keyword else None
        self._secret_cache[stem] = result
        with self._secret_scan_lock:
            self._secret_scan_submitted.discard(stem)
        if not result:
            return  # non-secret — default styling already applied
        for i, s in enumerate(self.current_batch_stems):
            if s == stem:
                item = self.file_list.item(i)
                if item is not None:
                    self._apply_secret_styling(item, stem)
                return

    def _refresh_current_file_item_styling(self):
        """Re-run secret detection for the current file and update only that
        list item in place (preserves checkbox state for every other row)."""
        if not self.current_stem:
            return
        self._invalidate_secret_cache(self.current_stem)
        for i, s in enumerate(self.current_batch_stems):
            if s == self.current_stem:
                item = self.file_list.item(i)
                if item is not None:
                    self._apply_secret_styling(item, s)
                return

    def _apply_secret_styling(self, item, stem: str):
        """Update a QListWidgetItem's label/foreground/tooltip to reflect
        whether its stem is a classified document."""
        secret = self._detect_secret(stem)
        if secret:
            item.setText(f"🔒 {stem}")
            item.setForeground(QColor("#dc2626"))   # red-600
            item.setToolTip(f"Văn bản mật: {secret}")
        else:
            item.setText(stem)
            if not self._resolve_pdf_path(stem):
                item.setForeground(QColor(MUTED))
                item.setToolTip("Không có PDF")
            else:
                item.setForeground(QColor(TEXT))
                item.setToolTip("")

    def _collect_stem_paths(self, stem: str) -> list:
        """Return every file path on disk that belongs to ``stem`` (input
        JSON, output labeled JSON, canonical JSON, _ocr.pdf). Missing paths
        are skipped so a partial set still deletes correctly."""
        paths = []
        inp = self.input_files.get(stem)
        if inp and os.path.isfile(inp):
            paths.append(inp)
        out = self.output_files.get(stem)
        if out and os.path.isfile(out):
            paths.append(out)
        # Canonical JSON + OCR PDF are resolved from the input JSON's
        # ``source_canonical_json`` hint, with a DEFAULT_OCR_DIR fallback.
        try:
            raw = self._read_json(inp) if inp else None
        except Exception:
            raw = None
        canonical_hint = None
        if isinstance(raw, dict):
            canonical_hint = raw.get("source_canonical_json")
        if canonical_hint and os.path.isfile(canonical_hint):
            paths.append(os.path.abspath(canonical_hint))
            ocr_pdf = Path(canonical_hint).with_suffix("")
            if ocr_pdf.is_file():
                paths.append(str(ocr_pdf.resolve()))
        else:
            guessed_pdf = Path(DEFAULT_OCR_DIR) / f"{stem}_ocr.pdf"
            if guessed_pdf.is_file():
                paths.append(str(guessed_pdf.resolve()))
            guessed_json = Path(DEFAULT_OCR_DIR) / f"{stem}_ocr.pdf.json"
            if guessed_json.is_file():
                paths.append(str(guessed_json.resolve()))
        return paths

    def _checked_stems(self) -> list:
        """Return the stems whose checkbox is ticked in the file list, in
        display order."""
        stems = []
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item is not None and item.checkState() == Qt.Checked:
                if 0 <= i < len(self.current_batch_stems):
                    stems.append(self.current_batch_stems[i])
        return stems

    def _rebuild_file_list(self):
        """Re-populate the QListWidget from ``self.current_batch_stems``."""
        self.file_list.blockSignals(True)
        self.file_list.clear()
        for stem in self.current_batch_stems:
            item = QListWidgetItem(stem)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._apply_secret_styling(item, stem)
            self.file_list.addItem(item)
        self.file_list_count.setText(str(len(self.current_batch_stems)))
        self.file_list.blockSignals(False)
        self._schedule_secret_scan(self.current_batch_stems)

    def _delete_checked_files(self):
        """Hard-delete all on-disk artefacts for each ticked stem.

        Destructive; no undo. Confirmation dialog shows the exact list so
        the user can sanity-check before committing.
        """
        stems = self._checked_stems()
        if not stems:
            self._toast("Chưa tick file nào để xoá", kind="info")
            return

        # Collect every path that will be deleted (across all ticked stems)
        per_stem_paths = {s: self._collect_stem_paths(s) for s in stems}
        total_paths = sum(len(v) for v in per_stem_paths.values())
        if total_paths == 0:
            QMessageBox.information(
                self, "Không tìm thấy file",
                "Không có file nào trên disk cho các stem đã chọn.",
            )
            return

        # Summary bullet list capped so long selections don't overflow
        preview_lines = []
        for s in stems[:10]:
            preview_lines.append(f"  • {s}  ({len(per_stem_paths[s])} file)")
        if len(stems) > 10:
            preview_lines.append(f"  … và {len(stems) - 10} stem khác")
        preview = "\n".join(preview_lines)

        reply = QMessageBox.question(
            self, "Xoá HẲN các file đã chọn",
            f"Sẽ xoá VĨNH VIỄN {total_paths} file thuộc {len(stems)} stem:\n\n"
            f"{preview}\n\n"
            f"Xoá mỗi stem: input JSON, output JSON, canonical JSON, _ocr.pdf.\n"
            f"KHÔNG THỂ KHÔI PHỤC.\n\nTiếp tục?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # Close fitz if the currently loaded file is being deleted (Windows
        # will refuse to unlink an open file otherwise).
        current_being_deleted = self.current_stem in stems
        if current_being_deleted and self.fitz_doc:
            try:
                self.fitz_doc.close()
            except Exception:
                pass
            self.fitz_doc = None

        deleted = []
        failed = []
        for stem, paths in per_stem_paths.items():
            for p in paths:
                try:
                    os.remove(p)
                    deleted.append(p)
                except Exception as e:
                    failed.append((p, str(e)))
            # Drop from in-memory indexes regardless of per-file errors,
            # so the UI doesn't try to re-open a deleted file.
            self.input_files.pop(stem, None)
            self.output_files.pop(stem, None)
            for b in list(self.batches.keys()):
                if stem in self.batches[b]:
                    self.batches[b].remove(stem)
                    if not self.batches[b]:
                        del self.batches[b]
            if stem in self.current_batch_stems:
                self.current_batch_stems.remove(stem)
            self._pixmap_cache.invalidate_stem(stem)

        self._rebuild_file_list()

        if failed:
            detail = "\n".join(f"  • {p}: {err}" for p, err in failed[:20])
            if len(failed) > 20:
                detail += f"\n  … và {len(failed) - 20} lỗi khác"
            QMessageBox.warning(
                self, "Một số file không xoá được",
                f"Đã xoá {len(deleted)} file. {len(failed)} file lỗi:\n\n{detail}",
            )
            self._toast(f"Xoá 1 phần: {len(failed)} file lỗi", kind="error")
        else:
            self._toast(
                f"Đã xoá {len(deleted)} file của {len(stems)} stem",
                kind="success",
            )

        # Advance selection if the currently loaded file just vanished
        if current_being_deleted:
            if self.current_batch_stems:
                new_row = min(self.current_index, len(self.current_batch_stems) - 1)
                self.current_index = new_row
                self.file_list.blockSignals(True)
                self.file_list.setCurrentRow(new_row)
                self.file_list.blockSignals(False)
                self._load_current_index()
            else:
                self.current_stem = ""
                self.current_index = -1
                self.output_json = None
                self.input_json = None
                self.canonical_json = None
                self._render_pdf()
                self._render_fields()
                self._render_details()
                self._update_status()

    def _prev_file(self):
        if self._is_loading:
            return  # drop keypresses arriving during an in-progress load
        if not self.current_batch_stems or self.current_index <= 0:
            return
        if not self._check_unsaved():
            return
        self.current_index -= 1
        self.file_list.blockSignals(True)
        self.file_list.setCurrentRow(self.current_index)
        self.file_list.blockSignals(False)
        self._load_current_index()

    def _next_file(self):
        if self._is_loading:
            return
        if not self.current_batch_stems or self.current_index >= len(self.current_batch_stems) - 1:
            return
        if not self._check_unsaved():
            return
        self.current_index += 1
        self.file_list.blockSignals(True)
        self.file_list.setCurrentRow(self.current_index)
        self.file_list.blockSignals(False)
        self._load_current_index()

    def _load_current_index(self):
        self.current_stem = self.current_batch_stems[self.current_index]
        self.selected_field = None
        self.dirty = False
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._update_nav_display()
        self._load_data()

    def _update_nav_display(self):
        total = len(self.current_batch_stems)
        idx = self.current_index
        self.file_label.setText(self.current_stem if self.current_stem else "--")
        self.file_counter.setText(f"{idx + 1} / {total}" if total > 0 else "")

    # ═══════════════════════════════════════════════════
    # DATA LOADING
    # ═══════════════════════════════════════════════════
    def _load_data(self):
        # Re-entry guard: while loading, any nested _load_data triggered via
        # processEvents (e.g. a queued arrow-key press) must be dropped so it
        # can't close fitz_doc or overwrite state mid-render.
        if self._is_loading:
            return
        self._is_loading = True
        # Stamp click-to-load start so _render_pdf can report elapsed timings
        # from the user's perspective (file switch → first page visible).
        self._load_click_t0 = time.perf_counter()
        t_click = self._load_click_t0
        try:
            stem = self.current_stem
            if not stem:
                return
            self.output_json = self._read_json(self.output_files.get(stem))
            self.input_json = self._read_json(self.input_files.get(stem))
            self.canonical_json = self._load_canonical()
            t_json = time.perf_counter()

            if self.fitz_doc:
                self.fitz_doc.close()
                self.fitz_doc = None
            pdf_path = self._resolve_pdf_path(stem, self.input_json)
            if pdf_path:
                try:
                    self.fitz_doc = fitz.open(pdf_path)
                except Exception as e:
                    print(f"Error loading PDF: {e}")
            t_fitz = time.perf_counter()

            # Don't clear old page widgets here — _render_pdf clears them
            # INSIDE its setUpdatesEnabled(False) batch so the transition
            # (old content → new content) happens in one paint with no
            # intermediate blank viewport.
            self._zoom_fit_silent()
            t_prep = time.perf_counter()
            print(
                f"[load]   stem={stem} json_read={((t_json-t_click)*1000):.0f}ms "
                f"fitz_open={((t_fitz-t_json)*1000):.0f}ms "
                f"fit={((t_prep-t_fitz)*1000):.0f}ms"
            )

            self._render_pdf()
            self._render_fields()
            self._render_details()
            self._update_status()
            # NOTE: _render_pdf already calls _draw_overlays inside its
            # updates-disabled batch so the priority page paints with bboxes
            # in one frame. Calling it again here was the source of the
            # residual flash (priority page repainted twice).
            # Scroll SYNC (not queued) so the viewport position is final
            # before _render_pdf's singleShot(0, _enqueue_viewport_pages) fires
            # — otherwise the enqueue would queue the wrong pages.
            if self.chk_last_page.isChecked():
                self._scroll_to_signer()
            else:
                # Non-signer mode: always land at the top of the document.
                # QScrollArea can retain the previous file's scroll value
                # across widget swaps; an explicit setValue(0) guarantees the
                # user sees page 1 on every file switch.
                self.pdf_scroll.verticalScrollBar().setValue(0)
        finally:
            self._is_loading = False

    def _read_json(self, path):
        if not path:
            return None
        try:
            return _fast_json_load(path)
        except Exception as e:
            print(f"Error reading {path}: {e}")
            return None

    def _refresh(self):
        """F5 / "Làm mới" — rescan the three configured directories and
        reload the currently-viewed file.

        Rescanning is needed because new auto-labelled batches, deleted
        files, or files that were added externally won't show up until
        we call :meth:`_scan_directories` again. The batch combo's
        per-batch counts and the file list widget are rebuilt from the
        rescan result.
        """
        prev_batch = self.batch_combo.currentData() or None
        prev_stem = self.current_stem

        # 1) Re-read input/output/batches from disk. _scan_directories
        #    also refreshes the batch combo (with updated counts).
        self._scan_directories()

        # 2) Re-select the previously-selected batch if it still exists.
        #    If it's gone (e.g. fully deleted), fall back to the first batch.
        if prev_batch and prev_batch in self.batches:
            idx = self.batch_combo.findData(prev_batch)
            if idx >= 0:
                self.batch_combo.blockSignals(True)
                self.batch_combo.setCurrentIndex(idx)
                self.batch_combo.blockSignals(False)
            self.current_batch_stems = self.batches[prev_batch][:]
        elif self.batches:
            first = sorted(self.batches.keys())[0]
            idx = self.batch_combo.findData(first)
            if idx >= 0:
                self.batch_combo.blockSignals(True)
                self.batch_combo.setCurrentIndex(idx)
                self.batch_combo.blockSignals(False)
            self.current_batch_stems = self.batches[first][:]
        else:
            self.current_batch_stems = []

        # 3) Rebuild the file list widget (items + count + secret-scan kickoff)
        self._populate_file_list()

        # 4) Re-select the previously-viewed file if it's still in the list,
        #    then reload it from disk. Otherwise reset the viewer to empty.
        if prev_stem and prev_stem in self.current_batch_stems:
            row = self.current_batch_stems.index(prev_stem)
            self.current_index = row
            self.file_list.blockSignals(True)
            self.file_list.setCurrentRow(row)
            self.file_list.blockSignals(False)
            self.output_json = self._read_json(self.output_files.get(prev_stem))
            self.dirty = False
            self.undo_stack.clear()
            self.redo_stack.clear()
            self._render_fields()
            self._render_details()
            self._draw_overlays()
            self._update_status()
            self._refresh_current_file_item_styling()
        else:
            # Previous file disappeared — clear the right-hand panels
            self.current_index = -1
            self.current_stem = ""
            self.output_json = None
            self.input_json = None
            self.canonical_json = None
            if self.fitz_doc:
                self.fitz_doc.close()
                self.fitz_doc = None
            self._render_pdf()
            self._render_fields()
            self._render_details()
            self._update_status()

        self._flash_button(self.btn_refresh, "✓ Đã làm mới", 1200)

    # ═══════════════════════════════════════════════════
    # HELPERS: get fields/relations from either format
    # ═══════════════════════════════════════════════════
    def _get_fields(self):
        if not self.output_json:
            return []
        fi = self.output_json.get("field_instances")
        if fi is not None:
            return fi
        ann = self.output_json.get("annotations")
        if isinstance(ann, dict):
            fi = ann.get("field_instances")
            if fi is not None:
                return fi
        return []

    def _get_relations(self):
        if not self.output_json:
            return []
        rels = self.output_json.get("relations")
        if rels is not None:
            return rels
        ann = self.output_json.get("annotations")
        if isinstance(ann, dict):
            rels = ann.get("relations")
            if rels is not None:
                return rels
        return []

    def _get_page_rotation(self, page_index):
        """Return the cardinal rotation (0/90/180/270) that preprocessing
        applied to this page before OCR, so the viewer can render the source
        PDF in the same orientation as the OCR bboxes.

        Priority:
          0. user manual override (per file, cumulative).
          1. canonical_json.pages[i].applied_rotation (metadata written by
             the current pipeline).
          2. input_json.pages[i].applied_rotation (fallback).
          3. On-the-fly ONNX orientation classifier cached per page.
          4. 0 (no rotation).
        """
        # 0: manual override applies to all pages of the current file
        overrides = getattr(self, "_rotation_overrides", None) or {}
        if self.current_stem and self.current_stem in overrides:
            return int(overrides[self.current_stem]) % 360

        # 1+2: metadata in canonical / input
        for src in (self.canonical_json, self.input_json):
            if not src:
                continue
            for p in src.get("pages", []):
                if p.get("page_index") == page_index:
                    rot = p.get("applied_rotation")
                    if rot is not None:
                        try:
                            return int(rot) % 360
                        except (TypeError, ValueError):
                            pass
                    break

        # 3: on-the-fly classifier, cached per (stem, page_index)
        cache = getattr(self, "_rotation_cache", None)
        if cache is None:
            cache = {}
            self._rotation_cache = cache
        cache_key = (self.current_stem, page_index)
        if cache_key in cache:
            return cache[cache_key]

        rot = 0
        try:
            if self.fitz_doc is not None and page_index < len(self.fitz_doc):
                from scanindex.core.preprocessing.preprocessing import detect_orientation_correction
                import numpy as np
                page = self.fitz_doc[page_index]
                # Low-res rasterization is enough for the classifier.
                pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
                if pix.n >= 3:
                    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
                    if pix.n == 4:
                        img = arr[:, :, :3][:, :, ::-1]  # RGBA -> BGR
                    else:
                        img = arr[:, :, ::-1]            # RGB  -> BGR
                else:
                    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w)
                    img = np.stack([arr, arr, arr], axis=-1)
                meta = detect_orientation_correction(img)
                rot = int(meta.get("rotate_angle", 0) or 0) % 360
        except Exception:
            rot = 0

        cache[cache_key] = rot
        return rot

    def _get_page_rotation_fast(self, page_index):
        """Like :meth:`_get_page_rotation` but never runs the ONNX classifier.

        Returns the rotation if it is already known cheaply (manual override,
        JSON metadata, or a cache hit from a prior full resolution); otherwise
        returns 0. Used on the cold-load placeholder path where paying a
        classifier inference per page (~30ms × N) blocks the UI for hundreds
        of milliseconds. The full :meth:`_get_page_rotation` still runs from
        the render tick, so the true rotation is applied before the pixmap is
        composited — at the cost of (at most) one re-layout per page.
        """
        overrides = getattr(self, "_rotation_overrides", None) or {}
        if self.current_stem and self.current_stem in overrides:
            return int(overrides[self.current_stem]) % 360
        for src in (self.canonical_json, self.input_json):
            if not src:
                continue
            for p in src.get("pages", []):
                if p.get("page_index") == page_index:
                    rot = p.get("applied_rotation")
                    if rot is not None:
                        try:
                            return int(rot) % 360
                        except (TypeError, ValueError):
                            pass
                    break
        cache = getattr(self, "_rotation_cache", None) or {}
        return cache.get((self.current_stem, page_index), 0)

    def _load_canonical(self):
        """Load the canonical OCR JSON referenced by input_json.source_canonical_json.
        Fallback: derive path from pdf filename in default ocr/ directory."""
        if not self.input_json:
            return None
        path = self.input_json.get("source_canonical_json")
        if path and os.path.isfile(path):
            return self._read_json(path)
        # Fallback: try DEFAULT_OCR_DIR/{stem}_ocr.pdf.json
        stem = self.current_stem
        if stem:
            guessed = os.path.join(DEFAULT_OCR_DIR, f"{stem}_ocr.pdf.json")
            if os.path.isfile(guessed):
                return self._read_json(guessed)
        return None

    def _find_page_data(self, page_index):
        """Find the page entry by page_index.
        Prefer canonical JSON (all pages); fallback to input JSON (selected pages)."""
        for src in (self.canonical_json, self.input_json):
            if not src:
                continue
            for p in src.get("pages", []):
                if p.get("page_index") == page_index:
                    return p
        return None

    @staticmethod
    def _iter_words(page):
        """Yield words from a page, supporting both layouts:
        - canonical: page.words[] (flat)
        - input: page.lines[].words[] (nested)"""
        if not page:
            return
        words = page.get("words")
        if isinstance(words, list) and words:
            for w in words:
                yield w
            return
        for line in page.get("lines", []):
            for w in line.get("words", []):
                yield w

    def _get_word_map(self, page_index):
        """Return {word_id: {id, text, bbox}} for a page."""
        page = self._find_page_data(page_index)
        if not page:
            return {}
        wm = {}
        for w in self._iter_words(page):
            wid = w.get("word_id") or w.get("id", "")
            if wid:
                wm[wid] = w
        return wm

    def _get_line_map(self, page_index):
        page = self._find_page_data(page_index)
        if not page:
            return {}
        return {(l.get("line_id") or l.get("id", "")): l
                for l in page.get("lines", [])}

    def _get_word_to_line_map(self, page_index):
        """Return {word_id: line_id} for a page."""
        page = self._find_page_data(page_index)
        if not page:
            return {}
        wl = {}
        # Canonical: word has line_id field directly
        words = page.get("words")
        if isinstance(words, list) and words and "line_id" in words[0]:
            for w in words:
                wid = w.get("word_id") or w.get("id", "")
                lid = w.get("line_id", "")
                if wid:
                    wl[wid] = lid
            return wl
        # Input: nested inside lines
        for line in page.get("lines", []):
            lid = line.get("line_id") or line.get("id", "")
            for w in line.get("words", []):
                wid = w.get("word_id") or w.get("id", "")
                if wid:
                    wl[wid] = lid
        return wl

    def _page_uses_bottom_left_coords(self, page_index):
        """Whether bbox y-axis uses PDF origin (y=0 at bottom).

        Priority:
          1. Explicit metadata on the page (``coord_origin``/``y_origin``).
          2. Heuristic: compare the average y of the first 3 lines (in reading
             order) to the last 3. If the first group is significantly HIGHER
             (y large), the page uses bottom-left coords, because reading order
             goes top -> bottom which in bottom-left means y descending.

        The heuristic uses the extremes (first/last 3) so that isolated
        page-number boxes appended to the line list cannot flip the whole
        decision. A 50-unit margin avoids noise.
        """
        page = self._find_page_data(page_index)
        if not isinstance(page, dict):
            return False
        origin = (page.get("coord_origin") or page.get("y_origin") or "").lower()
        if origin in {"bottom-left", "bottom_left", "bottom"}:
            return True
        if origin in {"top-left", "top_left", "top"}:
            return False
        # Heuristic fallback
        lines = page.get("lines") or []
        if len(lines) < 6:
            return False
        try:
            by_order = sorted(lines, key=lambda L: L.get("order", 0))
            first_ys = [float(L.get("y", 0) or 0) for L in by_order[:3]]
            last_ys = [float(L.get("y", 0) or 0) for L in by_order[-3:]]
            first_avg = sum(first_ys) / len(first_ys)
            last_avg = sum(last_ys) / len(last_ys)
        except Exception:
            return False
        return first_avg > last_avg + 50.0

    # ═══════════════════════════════════════════════════
    # PDF RENDERING
    # ═══════════════════════════════════════════════════
    def _show_render_overlay(self, total):
        self.render_overlay.setText(f"Render 0 / {total}")
        self._position_render_overlay()
        self.render_overlay.show()
        self.render_overlay.raise_()

    def _update_render_overlay(self, done, total):
        self.render_overlay.setText(f"Render {done} / {total}")
        self._position_render_overlay()

    def _hide_render_overlay(self):
        self.render_overlay.hide()

    def _position_render_overlay(self):
        """Pin overlay to the bottom-center of the scroll viewport, compact."""
        vp = self.pdf_scroll.viewport()
        self.render_overlay.adjustSize()
        w = self.render_overlay.width()
        h = self.render_overlay.height()
        x = (vp.width() - w) // 2
        y = vp.height() - h - 12
        self.render_overlay.move(max(0, x), max(0, y))

    def _render_pdf(self):
        # Viewport-based lazy render (Tier 1):
        # - Only render pages the user can actually see (viewport + 1-page
        #   buffer above/below). Other pages stay as placeholders until the
        #   user scrolls toward them. Saves 70-90% of fitz work on long docs.
        # - setUpdatesEnabled(False) wraps clear-old + placeholders + priority
        #   SYNC render + overlays so the transition paints ONCE.
        # - Priority page = last page if "Xem người ký" is on, else page 0.
        # - Scroll events trigger _schedule_viewport_update (debounced 50 ms).
        # - Pixmap cache means revisiting a file/page is ~0 ms.
        self._render_gen += 1
        my_gen = self._render_gen

        # Reset render queue state for the new file
        self._render_queue.clear()
        self._render_queued.clear()
        self._render_active = False

        if not self.fitz_doc:
            for pw in self.page_widgets:
                pw.hide()
                pw.deleteLater()
            self.page_widgets = []
            self.pdf_empty.setText("Không tìm thấy PDF")
            self.pdf_empty.show()
            self._hide_render_overlay()
            return
        self.pdf_empty.hide()

        doc = self.fitz_doc
        n_pages = len(doc)
        if n_pages == 0:
            for pw in self.page_widgets:
                pw.hide()
                pw.deleteLater()
            self.page_widgets = []
            self._hide_render_overlay()
            return

        priority_idx = (n_pages - 1) if self.chk_last_page.isChecked() else 0

        t_loop0 = time.perf_counter()
        cached_style = (
            "QLabel { "
            "  background: #1f1f1f; "
            "  border: 1px solid #3a3a3a; "
            "  color: #666; "
            "  font-size: 12px; "
            "}"
        )

        # ONE transition paint: drop old widgets → build new placeholders →
        # render priority page, all under a single updates-disabled window.
        self.pdf_pages_widget.setUpdatesEnabled(False)
        try:
            for pw in self.page_widgets:
                pw.hide()
                pw.deleteLater()
            self.page_widgets = []

            for i in range(n_pages):
                page = doc[i]
                # Fast path: never triggers ONNX classifier. Tick render uses
                # the full _get_page_rotation() which may re-layout the page
                # if the true rotation disagrees with this placeholder estimate.
                rotation = self._get_page_rotation_fast(i)
                w_pts, h_pts = page.rect.width, page.rect.height
                if rotation in (90, 270):
                    w_pts, h_pts = h_pts, w_pts
                pw = PdfPageWidget(i, parent=self.pdf_pages_widget)
                pw.setFocusPolicy(Qt.NoFocus)  # arrow keys stay on nav, not scroll
                pw.pdf_width = w_pts
                pw.pdf_height = h_pts
                pw.render_scale = self.zoom
                pw.setFixedSize(max(1, int(w_pts * self.zoom)), max(1, int(h_pts * self.zoom)))
                pw.setStyleSheet(cached_style)
                pw.setAlignment(Qt.AlignCenter)
                pw.setText(f"Trang {i + 1}")
                pw.bbox_origin_bottom_left = self._page_uses_bottom_left_coords(i)
                pw.edit_mode = self.edit_mode
                pw.line_mode = self.line_mode
                pw.word_selection_changed.connect(self._on_word_selection_changed)
                pw.empty_clicked.connect(self._on_pdf_empty_clicked)
                pw.context_menu_requested.connect(self._on_pdf_context_menu)
                pw.bbox_clicked_non_edit.connect(self._on_pdf_bbox_clicked_non_edit)
                # Defer word-map build to render tick (see _render_single_page_into_placeholder)
                self.pdf_pages_layout.addWidget(pw)
                self.page_widgets.append(pw)

            # Render priority page SYNC so the transition paint already shows
            # content (no placeholder flash on the target page).
            t_prio0 = time.perf_counter()
            try:
                self._render_single_page_into_placeholder(priority_idx)
            except Exception as e:
                print(f"Render priority page {priority_idx} failed: {e}")
            t_prio1 = time.perf_counter()
            # Draw bbox overlays INSIDE the same updates-disabled batch so the
            # priority page appears WITH its fields highlighted in one paint,
            # not "pixmap first → bboxes popping in a frame later" (which was
            # the source of the remaining flash/jerky feel).
            # Per-page helper — only priority has a pixmap at this point, so
            # full _draw_overlays would do N-1 no-op iterations for placeholders.
            try:
                self._apply_overlays_to_page(priority_idx)
            except Exception as e:
                print(f"Apply overlays to priority page failed: {e}")
        finally:
            self.pdf_pages_widget.setUpdatesEnabled(True)
        t_loop1 = time.perf_counter()
        print(
            f"[render] placeholders+priority={((t_loop1-t_loop0)*1000):.0f}ms "
            f"(priority_render={((t_prio1-t_prio0)*1000):.0f}ms)"
        )

        # Priority page is rendered. Mark it in the queued set so viewport
        # scanning won't re-enqueue it.
        self._render_queued.add(priority_idx)
        self._render_total = n_pages

        # Telemetry state captured in closure so supersede/drain events can
        # report elapsed time even after a new file swap overrides gen.
        t0 = getattr(self, "_load_click_t0", None) or time.perf_counter()
        stem = self.current_stem or "?"
        first_ms = (time.perf_counter() - t0) * 1000.0
        print(
            f"[render] gen={my_gen} stem={stem} pages={n_pages} "
            f"zoom={self.zoom:.2f} first_page_visible_at={first_ms:.0f}ms "
            f"(page_idx={priority_idx})"
        )
        state = {
            "gen": my_gen,
            "stem": stem,
            "n_pages": n_pages,
            "t_start": t0,
            "viewport_done_logged": False,
        }
        self._render_state = state

        # Enqueue viewport pages on the NEXT event-loop tick so the scroll
        # position (e.g. from _scroll_to_signer) settles first. Without this
        # we'd compute the viewport before any scroll happens and queue the
        # wrong set of pages.
        self._show_render_overlay(n_pages)
        self._update_render_overlay(1, n_pages)
        QTimer.singleShot(0, lambda: self._enqueue_viewport_pages(my_gen, state))

    # ──────────────────────────────────────────────────────────────────
    # Viewport-based render queue (Tier 1)
    # ──────────────────────────────────────────────────────────────────
    def _viewport_page_indices(self) -> list:
        """Return page indices whose geometry intersects the viewport + a
        one-viewport-height buffer above and below. Buffer lets pages come
        into view the instant the user scrolls, without waiting for a tick.
        """
        if not self.page_widgets:
            return []
        vp = self.pdf_scroll.viewport()
        vsb = self.pdf_scroll.verticalScrollBar()
        scroll_y = vsb.value()
        vp_h = vp.height()
        buffer = vp_h
        top = scroll_y - buffer
        bot = scroll_y + vp_h + buffer
        hits = []
        for pw in self.page_widgets:
            y = pw.y()
            h = pw.height()
            if y + h >= top and y <= bot:
                hits.append(pw.page_index)
        return hits

    def _enqueue_viewport_pages(self, my_gen: int, state: dict):
        """Queue pages in the viewport-range that are not yet rendered.

        ``state`` is the per-render telemetry dict captured in the closure so
        supersede/done events log info for the render they actually belong to,
        not the current one.
        """
        if my_gen != self._render_gen:
            return
        newly_queued = 0
        for idx in self._viewport_page_indices():
            if idx in self._render_queued:
                continue
            self._render_queue.append(idx)
            self._render_queued.add(idx)
            newly_queued += 1
        if newly_queued and not self._render_active:
            QTimer.singleShot(0, lambda: self._drain_render_queue(my_gen, state))
        elif not self._render_queue and not self._render_active:
            self._log_viewport_done(state)
            self._hide_render_overlay()

    def _drain_render_queue(self, my_gen: int, state: dict):
        """Render one queued page per tick; keep scheduling while queue lasts."""
        if my_gen != self._render_gen:
            elapsed = (time.perf_counter() - state["t_start"]) * 1000.0
            print(
                f"[render] gen={state['gen']} stem={state['stem']} SUPERSEDED "
                f"with {state.get('rendered_at_supersede', '?')} pages complete "
                f"({elapsed:.0f}ms)"
            )
            return
        if not self._render_queue:
            self._render_active = False
            self._log_viewport_done(state)
            self._hide_render_overlay()
            return

        self._render_active = True
        page_idx = self._render_queue.popleft()
        try:
            self._render_single_page_into_placeholder(page_idx)
            self._apply_overlays_to_page(page_idx)
        except Exception as e:
            print(f"Render page {page_idx} failed: {e}")
        self._render_active = False

        rendered = len(self._render_queued) - len(self._render_queue)
        state["rendered_at_supersede"] = rendered
        self._update_render_overlay(rendered, self._render_total)

        if self._render_queue:
            QTimer.singleShot(0, lambda: self._drain_render_queue(my_gen, state))
        else:
            self._log_viewport_done(state)
            self._hide_render_overlay()

    def _log_viewport_done(self, state: dict):
        if state is None or state.get("viewport_done_logged"):
            return
        elapsed = (time.perf_counter() - state["t_start"]) * 1000.0
        rendered = state.get("rendered_at_supersede", 1)
        print(
            f"[render] gen={state['gen']} stem={state['stem']} VIEWPORT_READY "
            f"rendered={rendered}/{state['n_pages']} pages ({elapsed:.0f}ms)"
        )
        state["viewport_done_logged"] = True
        # Warm the cache for neighbouring files once the current viewport is
        # fully rendered. Deferred via singleShot so the foreground render
        # paints to the screen BEFORE the prefetch thread starts competing
        # for disk bandwidth.
        QTimer.singleShot(100, self._schedule_prefetch_adjacent)

    def _schedule_viewport_update(self):
        """Scroll handler — debounced so rapid wheel events coalesce into
        one viewport-page enqueue pass."""
        if self._scroll_debounce_pending:
            return
        self._scroll_debounce_pending = True
        QTimer.singleShot(50, self._do_viewport_update)

    def _do_viewport_update(self):
        self._scroll_debounce_pending = False
        if not self.fitz_doc:
            return
        state = getattr(self, "_render_state", None)
        if state is None:
            return
        # Reopen the overlay if new pages need rendering after a scroll
        before = len(self._render_queued)
        self._enqueue_viewport_pages(self._render_gen, state)
        after = len(self._render_queued)
        if after > before:
            # Viewport expanded — reset the "done" log so it fires again when
            # this new batch completes.
            state["viewport_done_logged"] = False
            self._show_render_overlay(self._render_total)

    # ──────────────────────────────────────────────────────────────────
    # Adjacent-file prefetch (Tier 2)
    # ──────────────────────────────────────────────────────────────────
    def _schedule_prefetch_adjacent(self):
        """Warm the pixmap cache for the files adjacent to the current one.

        Called once per file load, AFTER the current file's VIEWPORT_READY so
        the prefetch doesn't compete with the foreground render for disk/CPU.
        """
        if not self.current_batch_stems:
            return
        idx = self.current_index
        signer_mode = self.chk_last_page.isChecked()
        zoom = self.zoom
        # Prefetch ±1 first (most likely next click), then ±2
        for offset in (1, -1, 2, -2):
            target = idx + offset
            if not (0 <= target < len(self.current_batch_stems)):
                continue
            stem = self.current_batch_stems[target]
            with self._prefetch_lock:
                if stem in self._prefetch_submitted:
                    continue
                self._prefetch_submitted.add(stem)
            self._submit_prefetch(stem, signer_mode, zoom)

    def _submit_prefetch(self, stem: str, signer_mode: bool, zoom: float):
        """Kick off one daemon worker thread to warm the cache for ``stem``.

        Resolves the PDF path on the main thread (cheap, no disk I/O beyond
        what's already mapped) and hands off only primitive arguments to the
        worker — avoids shared mutable state between threads.
        """
        input_json_path = self.input_files.get(stem)
        if not input_json_path or not os.path.exists(input_json_path):
            self._prefetch_submitted.discard(stem)
            return
        try:
            input_json = _fast_json_load(input_json_path)
        except Exception as e:
            print(f"[prefetch] {stem}: read input_json failed: {e}")
            self._prefetch_submitted.discard(stem)
            return
        pdf_path = self._resolve_pdf_path(stem, input_json)
        if not pdf_path:
            self._prefetch_submitted.discard(stem)
            return

        emitter = self._prefetch_emitter

        def worker():
            t0 = time.perf_counter()
            try:
                doc = fitz.open(pdf_path)
                n = len(doc)
                if n == 0:
                    doc.close()
                    return
                priority_idx = (n - 1) if signer_mode else 0
                page = doc[priority_idx]
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat)
                # Independent buffer so QImage survives after `doc` closes
                img = QImage(
                    pix.samples, pix.width, pix.height, pix.stride,
                    QImage.Format_RGB888,
                ).copy()
                doc.close()
                emitter.prefetch_done.emit(stem, priority_idx, zoom, 0, img)
                dt_ms = (time.perf_counter() - t0) * 1000.0
                print(
                    f"[prefetch] stem={stem} page={priority_idx} "
                    f"rendered in {dt_ms:.0f}ms (worker)"
                )
            except Exception as e:
                print(f"[prefetch] {stem}: render failed: {e}")
            finally:
                # Always clear the in-flight marker, even on failure
                pass  # cleared in _on_prefetch_done / or below on exception

        t = threading.Thread(
            target=worker,
            daemon=True,
            name=f"kie-prefetch-{stem[-12:]}",
        )
        t.start()

    def _on_prefetch_done(self, stem: str, page_idx: int, zoom: float,
                          rotation: int, qimage):
        """Main-thread slot: convert QImage → QPixmap and seed the cache."""
        try:
            pm = QPixmap.fromImage(qimage)
            cache_key = (stem, int(page_idx), round(float(zoom), 4),
                         int(rotation) % 360)
            self._pixmap_cache.put(cache_key, pm)
        except Exception as e:
            print(f"[prefetch] {stem}: cache seed failed: {e}")
        finally:
            with self._prefetch_lock:
                self._prefetch_submitted.discard(stem)

    def _render_single_page_into_placeholder(self, i):
        """Render page ``i``'s pixmap into the existing placeholder widget.

        Uses the shared :class:`PixmapCache` so that revisiting a file/page at
        the same zoom/rotation is an O(1) dict lookup — no fitz render, no
        Qt conversion.
        """
        doc = self.fitz_doc
        if not doc or i >= len(doc) or i >= len(self.page_widgets):
            return
        rotation = self._get_page_rotation(i)
        zoom_key = round(float(self.zoom), 4)
        cache_key = (self.current_stem, i, zoom_key, int(rotation) % 360)
        pm = self._pixmap_cache.get(cache_key)
        page = doc[i]
        w_pts = page.rect.width
        h_pts = page.rect.height
        if rotation and rotation in (90, 270):
            w_pts, h_pts = h_pts, w_pts

        if pm is None:
            mat = fitz.Matrix(self.zoom, self.zoom)
            pix = page.get_pixmap(matrix=mat)
            # QImage wraps pix.samples; QPixmap.fromImage() copies to its own
            # buffer, so the intermediate .copy() on QImage is redundant
            # (~30-100 ms saved on large scan pages).
            img = QImage(pix.samples, pix.width, pix.height, pix.stride,
                         QImage.Format_RGB888)
            pm = QPixmap.fromImage(img)
            if rotation:
                pm = pm.transformed(QTransform().rotate(rotation), Qt.SmoothTransformation)
            self._pixmap_cache.put(cache_key, pm)

        pw = self.page_widgets[i]
        # Only resize if the placeholder size differs from the actual pixmap
        # (skips a redundant layout pass on the common case).
        if pw.width() != pm.width() or pw.height() != pm.height():
            pw.setFixedSize(pm.width(), pm.height())
        pw.setStyleSheet("border: 1px solid #555; background: white;")
        pw.set_page(pm, w_pts, h_pts, self.zoom)
        # Populate word maps AFTER set_page — set_page() resets word_rects to
        # [] as part of its state reset, so populating before it would wipe
        # everything and edit-mode hit-testing would find no words.
        wm = self._get_word_map(i)
        pw.word_rects = [(wid, w["bbox"]) for wid, w in wm.items() if "bbox" in w]
        pw.word_to_line = self._get_word_to_line_map(i)
        line_to_words: dict = {}
        for wid, lid in pw.word_to_line.items():
            line_to_words.setdefault(lid, []).append(wid)
        pw.line_to_words = line_to_words

    def _set_zoom(self, delta):
        self.zoom = max(0.5, min(5.0, self.zoom + delta))
        self.zoom_label.setText(f"{int(self.zoom * 100)}%")
        self._render_pdf()
        self._draw_overlays()

    def _zoom_fit_silent(self):
        """Calculate fit zoom without re-rendering (used before _render_pdf).

        Default: fit by width so normal A4 docs render at readable size.
        For large or oddly-shaped pages (scanned full-resolution images
        where the page rect itself is huge), fit the WHOLE page in viewport
        instead — otherwise the user has to scroll just to see one page.

        We apply a safety multiplier (0.95) on top of the math because Qt
        viewport sizing has subtle rounding (frame, scrollbar reserve,
        widget border, sub-pixel layout) that's hard to enumerate exactly.
        The cost is ~5% wasted horizontal space; the benefit is the page
        ALWAYS fits without horizontal scrollbar.
        """
        if not self.fitz_doc:
            return
        page = self.fitz_doc[0]
        vp = self.pdf_scroll.viewport()
        # 16 + 16 layout margins, 1+1 page border = 34. Round up to 36.
        layout_pad = 36
        avail_w = vp.width() - layout_pad
        avail_h = vp.height() - layout_pad
        if avail_w <= 0 or avail_h <= 0:
            return
        SAFETY = 0.95  # absorb Qt sub-pixel/frame rounding
        zoom_w = (avail_w / page.rect.width) * SAFETY
        zoom_h = (avail_h / page.rect.height) * SAFETY
        # Heuristic: a real A4 PDF page is ~595×842 pt. Larger than that
        # almost always means a scanned image embedded as a single PDF page.
        is_large_image = page.rect.width > 1000 or page.rect.height > 1500
        self.zoom = min(zoom_w, zoom_h) if is_large_image else zoom_w
        self.zoom_label.setText(f"{int(self.zoom * 100)}%")

    def _zoom_fit(self):
        if not self.fitz_doc:
            return
        self._zoom_fit_silent()
        self._render_pdf()
        self._draw_overlays()

    def _scroll_to_signer(self):
        """Scroll to the signer field, centered in viewport."""
        fields = self._get_fields()
        # Find signer field by priority
        signer = None
        for pref in ("SIGNER_NAME", "SIGNER_ROLE"):
            signer = next((f for f in fields if f.get("label") == pref), None)
            if signer:
                break
        if signer:
            self._scroll_to_field_bbox(signer)
        elif self.page_widgets:
            # Fallback: bottom of last page
            QApplication.processEvents()
            vsb = self.pdf_scroll.verticalScrollBar()
            vsb.setValue(vsb.maximum())

    # ═══════════════════════════════════════════════════
    # OVERLAY DRAWING
    # ═══════════════════════════════════════════════════
    def _draw_overlays(self):
        for pw in self.page_widgets:
            pw.word_ownership = {}
            pw.selected_word_ids = set()
            pw.set_highlights([])

        if not self.input_json and not self.canonical_json:
            return

        fields = self._get_fields()

        # In edit mode, populate word ownership + selected word ids
        if self.edit_mode:
            selected_fid = self.selected_field.get("field_id") if self.selected_field else None
            for field in fields:
                pi = field.get("page_index", 0)
                pw = next((p for p in self.page_widgets if p.page_index == pi), None)
                if not pw:
                    continue
                fid = field.get("field_id")
                label = field.get("label", "")
                for wid in field.get("word_ids", []):
                    if fid == selected_fid:
                        pw.selected_word_ids.add(wid)
                    else:
                        pw.word_ownership[wid] = label

        # Determine what to show:
        # - No field selected → show ALL fields (default overview)
        # - Field selected → show only that field
        if self.selected_field:
            to_show = [self.selected_field]
        elif not self.edit_mode:
            to_show = fields  # show all by default
        else:
            to_show = []

        if not to_show:
            # In edit mode with no field selected, still repaint for word outlines
            if self.edit_mode:
                for pw in self.page_widgets:
                    pw._repaint()
            return

        # Phase 1 — group by (page, label) and order each group by reading
        # position (top→down, left→right) so multi-instance fields like
        # SIGNER_ROLE / SIGNER_NAME get stable a/b/c suffixes that align
        # with their visual order on the page.
        groups_pl = {}  # (pi, label) -> [field, ...]
        for field in to_show:
            pi = field.get("page_index", 0)
            label = field.get("label", "")
            groups_pl.setdefault((pi, label), []).append(field)

        def _field_anchor_xy(field, pi):
            wm = self._get_word_map(pi)
            ys, xs = [], []
            for wid in field.get("word_ids", []):
                w = wm.get(wid)
                if w and "bbox" in w:
                    ys.append(w["bbox"][1])
                    xs.append(w["bbox"][0])
            return (min(ys) if ys else 0.0, min(xs) if xs else 0.0)

        for (pi, _label), fields_in_group in groups_pl.items():
            if len(fields_in_group) > 1:
                fields_in_group.sort(key=lambda f: _field_anchor_xy(f, pi))

        # Phase 2 — assign badge text per field. Single instance → "8".
        # Multiple instances → "8a", "8b", "8c"... (or "8.27" beyond z).
        field_badge = {}  # field_id -> "8" / "8a" / ...
        for (_pi, label), fields_in_group in groups_pl.items():
            base = FIELD_NUMBER_MAP.get(label)
            if base is None:
                continue
            n = len(fields_in_group)
            for idx, f in enumerate(fields_in_group):
                fid = f.get("field_id")
                if not fid:
                    continue
                if n == 1:
                    field_badge[fid] = str(base)
                elif idx < 26:
                    field_badge[fid] = f"{base}{chr(ord('a') + idx)}"
                else:
                    field_badge[fid] = f"{base}.{idx + 1}"

        # Phase 3 — collect bboxes + icons for each page widget.
        page_bboxes = {}  # page_index -> [(bbox, label, is_sel)]
        page_icons = {}   # page_index -> [(anchor_bbox, badge_text, label, is_sel)]
        for field in to_show:
            pi = field.get("page_index", 0)
            is_sel = (self.selected_field and
                      field.get("field_id") == self.selected_field.get("field_id"))
            label = field.get("label", "")
            wm = self._get_word_map(pi)
            lm = self._get_line_map(pi)
            bboxes = [wm[wid]["bbox"] for wid in field.get("word_ids", [])
                       if wid in wm and "bbox" in wm[wid]]
            if not bboxes:
                bboxes = [lm[lid]["bbox"] for lid in field.get("line_ids", [])
                           if lid in lm and "bbox" in lm[lid]]
            for bbox in bboxes:
                page_bboxes.setdefault(pi, []).append((bbox, label, is_sel))

            badge = field_badge.get(field.get("field_id"))
            if badge is not None and bboxes:
                x0 = min(b[0] for b in bboxes)
                y0 = min(b[1] for b in bboxes)
                x1 = max(b[2] for b in bboxes)
                y1 = max(b[3] for b in bboxes)
                page_icons.setdefault(pi, []).append(
                    ((x0, y0, x1, y1), badge, label, is_sel))

        for pw in self.page_widgets:
            pw.set_highlights(
                page_bboxes.get(pw.page_index, []),
                field_icons=page_icons.get(pw.page_index, []),
            )

    def _apply_overlays_to_page(self, page_idx):
        """Lightweight per-page version of :meth:`_draw_overlays`.

        Computes highlight bboxes + field icons for a SINGLE page and repaints
        only that page. Used by the render tick so adding page K doesn't
        trigger an O(N) repaint across all previously rendered pages — the
        main source of the per-tick jerk.

        Semantics mirror :meth:`_draw_overlays` for the target page:
          - edit mode: populate word_ownership/selected_word_ids from fields
          - selected field: show only that field's bboxes (if on this page)
          - no selection: show all fields on this page
        """
        pw = next((p for p in self.page_widgets if p.page_index == page_idx), None)
        if not pw:
            return

        pw.word_ownership = {}
        pw.selected_word_ids = set()
        pw.highlight_bboxes = []
        pw.field_icons = []

        if not self.input_json and not self.canonical_json:
            pw._repaint()
            return

        page_fields = [f for f in self._get_fields() if f.get("page_index", 0) == page_idx]

        if self.edit_mode:
            selected_fid = self.selected_field.get("field_id") if self.selected_field else None
            for field in page_fields:
                fid = field.get("field_id")
                label = field.get("label", "")
                for wid in field.get("word_ids", []):
                    if fid == selected_fid:
                        pw.selected_word_ids.add(wid)
                    else:
                        pw.word_ownership[wid] = label

        if self.selected_field:
            sel_fid = self.selected_field.get("field_id")
            to_show = [f for f in page_fields if f.get("field_id") == sel_fid]
        elif not self.edit_mode:
            to_show = page_fields
        else:
            to_show = []

        if not to_show:
            pw._repaint()
            return

        # Group same-label fields on this page for stable "a/b/c" badge suffix
        groups = {}
        for field in to_show:
            groups.setdefault(field.get("label", ""), []).append(field)

        wm = self._get_word_map(page_idx)
        lm = self._get_line_map(page_idx)

        def _anchor_xy(field):
            ys, xs = [], []
            for wid in field.get("word_ids", []):
                w = wm.get(wid)
                if w and "bbox" in w:
                    ys.append(w["bbox"][1])
                    xs.append(w["bbox"][0])
            return (min(ys) if ys else 0.0, min(xs) if xs else 0.0)

        field_badge = {}
        for label, group in groups.items():
            if len(group) > 1:
                group.sort(key=_anchor_xy)
            base = FIELD_NUMBER_MAP.get(label)
            if base is None:
                continue
            n = len(group)
            for idx, f in enumerate(group):
                fid = f.get("field_id")
                if not fid:
                    continue
                if n == 1:
                    field_badge[fid] = str(base)
                elif idx < 26:
                    field_badge[fid] = f"{base}{chr(ord('a') + idx)}"
                else:
                    field_badge[fid] = f"{base}.{idx + 1}"

        highlight_bboxes = []
        field_icons = []
        sel_fid = self.selected_field.get("field_id") if self.selected_field else None
        for field in to_show:
            is_sel = (sel_fid and field.get("field_id") == sel_fid)
            label = field.get("label", "")
            bboxes = [wm[wid]["bbox"] for wid in field.get("word_ids", [])
                      if wid in wm and "bbox" in wm[wid]]
            if not bboxes:
                bboxes = [lm[lid]["bbox"] for lid in field.get("line_ids", [])
                          if lid in lm and "bbox" in lm[lid]]
            for bbox in bboxes:
                highlight_bboxes.append((bbox, label, bool(is_sel)))
            badge = field_badge.get(field.get("field_id"))
            if badge is not None and bboxes:
                x0 = min(b[0] for b in bboxes)
                y0 = min(b[1] for b in bboxes)
                x1 = max(b[2] for b in bboxes)
                y1 = max(b[3] for b in bboxes)
                field_icons.append(((x0, y0, x1, y1), badge, label, bool(is_sel)))

        pw.highlight_bboxes = highlight_bboxes
        pw.field_icons = field_icons
        pw._repaint()

    # ═══════════════════════════════════════════════════
    # FIELDS TREE
    # ═══════════════════════════════════════════════════
    def _render_fields(self):
        self.fields_tree.clear()
        fields = self._get_fields()
        self.fields_count.setText(str(len(fields)))

        groups = {}
        for f in fields:
            groups.setdefault(f.get("label", "?"), []).append(f)

        for label in sorted(groups.keys()):
            items = groups[label]
            _, light = get_label_color(label)
            gi = QTreeWidgetItem(self.fields_tree)
            gi.setText(0, f"{label_display(label)} — {len(items)}")
            gi.setForeground(0, QColor(light))
            gi.setFlags(gi.flags() & ~Qt.ItemIsSelectable)
            gi.setExpanded(True)
            for field in items:
                ci = QTreeWidgetItem(gi)
                ci.setText(0, field.get("text", "(trống)"))
                conf = field.get("confidence")
                ci.setText(1, f"{conf*100:.1f}%" if conf is not None else "--")
                ci.setForeground(1, QColor(MUTED))
                # Store field_id (string), not the dict — Qt copies dicts
                # via QVariantMap, breaking mutation propagation.
                ci.setData(0, Qt.UserRole, field.get("field_id", ""))

    # ═══════════════════════════════════════════════════
    # FIELD CLICK + SCROLL
    # ═══════════════════════════════════════════════════
    def _on_field_clicked(self, item, column):
        # Tree items hold a field_id STRING (not the dict — see selected_field
        # property comment above). Group headers have no string ID.
        field_id = item.data(0, Qt.UserRole)
        if not isinstance(field_id, str) or not field_id:
            return
        # Toggle: clicking the already-selected field deselects it.
        if self._selected_field_id == field_id:
            self.selected_field = None
            self.fields_tree.clearSelection()
        else:
            self.selected_field = field_id
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.btn_delete_field.setVisible(
            self.edit_mode and self.selected_field is not None)
        self._update_edit_banner()
        self._render_details()
        self._draw_overlays()
        if self.selected_field:
            QTimer.singleShot(0, partial(self._scroll_to_field_bbox, self.selected_field))

    def _scroll_to_field_bbox(self, field):
        pi = field.get("page_index", 0)
        pw = next((p for p in self.page_widgets if p.page_index == pi), None)
        if not pw:
            return
        QApplication.processEvents()

        min_y = None
        use_bottom_left = pw.bbox_origin_bottom_left
        wm = self._get_word_map(pi)
        for wid in field.get("word_ids", []):
            w = wm.get(wid)
            if w and "bbox" in w:
                y = (pw.pdf_height - w["bbox"][3]) if use_bottom_left else w["bbox"][1]
                if min_y is None or y < min_y:
                    min_y = y
        if min_y is None:
            lm = self._get_line_map(pi)
            for lid in field.get("line_ids", []):
                l = lm.get(lid)
                if l and "bbox" in l:
                    y = (pw.pdf_height - l["bbox"][3]) if use_bottom_left else l["bbox"][1]
                    if min_y is None or y < min_y:
                        min_y = y

        page_y = pw.mapTo(self.pdf_pages_widget, pw.rect().topLeft()).y()
        vh = self.pdf_scroll.viewport().height()
        if min_y is not None:
            target = int(page_y + min_y * self.zoom - vh / 2)
        else:
            target = int(page_y + pw.height() / 2 - vh / 2)
        vsb = self.pdf_scroll.verticalScrollBar()
        vsb.setValue(max(0, min(target, vsb.maximum())))

    def _deselect(self):
        self.selected_field = None
        self.fields_tree.clearSelection()
        self.btn_delete_field.setVisible(False)
        self._update_edit_banner()
        self._render_details()
        self._draw_overlays()

    # ═══════════════════════════════════════════════════
    # DIRECT-MANIPULATION ON THE PDF CANVAS
    # ═══════════════════════════════════════════════════
    def _on_pdf_context_menu(self, page_index: int, global_pos):
        """Right-click on a PDF page → popup label menu. Picking a label
        enters edit mode, creates an empty field bound to ``page_index``,
        and selects it so the user's next drag fills its bbox."""
        if not self.output_json or not self.current_stem:
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background-color: {SURFACE}; color: {TEXT}; "
            f"border: 1px solid {BORDER}; padding: 4px; }}"
            f"QMenu::item {{ padding: 6px 16px; border-radius: 3px; }}"
            f"QMenu::item:selected {{ background-color: {ACCENT}; color: white; }}"
        )
        header = QAction(f"+ Trường mới trên trang {page_index + 1}", self)
        header.setEnabled(False)
        menu.addAction(header)
        menu.addSeparator()
        for label in V3_TRAIN_LABELS:
            act = QAction(label_display(label), self)
            act.triggered.connect(
                partial(self._create_field_from_context_menu, label, page_index)
            )
            menu.addAction(act)
        menu.exec(global_pos)

    def _create_field_from_context_menu(self, label: str, page_index: int):
        """Enter edit mode (if not already) and create an empty field on the
        right-clicked page. The user then drag-selects words to fill it."""
        if not self.edit_mode:
            self.btn_edit.setChecked(True)
            self._toggle_edit_mode()
        self._create_field_with_label(label, page_index=page_index)

    def _on_pdf_bbox_clicked_non_edit(self, page_index: int, word_id: str):
        """Left-click on an existing bbox while NOT in edit mode → enter
        edit mode on that bbox's owning field."""
        if self.edit_mode:
            return  # normal edit-mode handling takes over
        target = None
        for f in self._get_fields():
            if f.get("page_index", 0) != page_index:
                continue
            if word_id in (f.get("word_ids") or []):
                target = f
                break
        if target is None:
            return
        # Flip the edit button state so _toggle_edit_mode sees the right
        # isChecked() value.
        self.btn_edit.setChecked(True)
        self._toggle_edit_mode()
        self.selected_field = target
        self._reselect_field_in_tree(target.get("field_id"))
        self._render_details()
        self._apply_overlays_to_page(page_index)

    def _on_pdf_empty_clicked(self):
        """Left-click on an empty area of the PDF canvas.

        - Non-edit mode: clear any field selection.
        - Edit mode with unsaved changes: auto-save and exit. No confirmation
          dialog — the click is the commit gesture. If the save fails
          (validator rejects), stay in edit mode so the user can fix errors.
        - Edit mode with no changes: just exit silently.
        """
        if not self.edit_mode:
            self._deselect()
            return
        if self.dirty:
            self._save()
            if self.dirty:
                # Save failed (validator rejection etc.) — keep user in edit
                # mode so they can address the errors.
                return
        # Exit edit mode: deselect then flip the toggle
        self._deselect()
        self.btn_edit.setChecked(False)
        self._toggle_edit_mode()

    def _copy_checked_files(self):
        """Copy checked file names to clipboard as a text list."""
        checked = []
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.checkState() == Qt.Checked:
                checked.append(item.text())
        if not checked:
            self._flash_button(self.btn_copy_checked, "Chưa check file nào", 1200)
            return
        text = "\n".join(checked)
        QApplication.clipboard().setText(text)
        self._flash_button(self.btn_copy_checked, f"✓ Đã copy {len(checked)}", 1500)
        self.status_left.setText(f"Đã copy {len(checked)} file vào clipboard")

    def _flash_button(self, button, temp_text, duration_ms=1500):
        """Briefly change button text to give visual feedback after click."""
        original = button.text()
        button.setText(temp_text)
        button.setEnabled(False)
        def restore():
            button.setText(original)
            button.setEnabled(True)
        QTimer.singleShot(duration_ms, restore)

    # ═══════════════════════════════════════════════════
    # TOAST — bottom-right success/info/error notifications. Float above the
    # UI (no layout reflow) and auto-dismiss so the user stays focused on
    # their task. Green for success, red for error, neutral for info.
    # ═══════════════════════════════════════════════════
    def _toast(self, message: str, kind: str = "success", duration_ms: int = 3000):
        palette = {
            "success": ("#28a745", "white"),
            "error":   ("#dc3545", "white"),
            "info":    ("#374151", "white"),
        }
        bg, fg = palette.get(kind, palette["info"])
        if not hasattr(self, "_toast_label") or self._toast_label is None:
            self._toast_label = QLabel(self)
            self._toast_label.setAlignment(Qt.AlignCenter)
            self._toast_label.hide()
        lbl = self._toast_label
        lbl.setStyleSheet(
            f"background-color: {bg}; color: {fg}; "
            f"font-size: 13px; font-weight: 600; "
            f"padding: 10px 16px; border-radius: 6px;"
        )
        lbl.setText(message)
        lbl.adjustSize()
        # Pin to bottom-right with a small margin. Parent is the main window
        # so we anchor against its client area; overlay never reflows siblings.
        margin = 20
        x = self.width() - lbl.width() - margin
        y = self.height() - lbl.height() - margin
        lbl.move(max(0, x), max(0, y))
        lbl.show()
        lbl.raise_()
        # Reuse one QTimer: restart by cancelling any pending hide
        if not hasattr(self, "_toast_timer") or self._toast_timer is None:
            self._toast_timer = QTimer(self)
            self._toast_timer.setSingleShot(True)
            self._toast_timer.timeout.connect(self._toast_label.hide)
        self._toast_timer.stop()
        self._toast_timer.start(duration_ms)


    # ═══════════════════════════════════════════════════
    # EDIT MODE
    # ═══════════════════════════════════════════════════
    def _toggle_edit_mode(self):
        self.edit_mode = self.btn_edit.isChecked()
        self.btn_edit.setText("Thoát sửa" if self.edit_mode else "Sửa")
        # Swap objectName so the stylesheet applies the amber "active" look
        # (see QPushButton#btn_edit_active in STYLESHEET). Qt does not
        # re-evaluate selectors after setObjectName, so we force a repolish.
        self.btn_edit.setObjectName("btn_edit_active" if self.edit_mode else "")
        self.btn_edit.style().unpolish(self.btn_edit)
        self.btn_edit.style().polish(self.btn_edit)
        self._update_save_visibility()
        self.btn_add_field.setVisible(self.edit_mode)
        self.btn_add_relation.setVisible(self.edit_mode)
        self.chk_line_mode.setVisible(self.edit_mode)
        self._update_delete_visibility()
        self.edit_banner.setVisible(self.edit_mode)
        if not self.edit_mode:
            self.undo_stack.clear()
            self.redo_stack.clear()
        for pw in self.page_widgets:
            pw.edit_mode = self.edit_mode
            pw.line_mode = self.line_mode
            pw.setCursor(Qt.CrossCursor if self.edit_mode else Qt.ArrowCursor)
        self._update_edit_banner()
        self._draw_overlays()
        self._render_details()

    def _update_delete_visibility(self):
        """Show btn_delete_field ⟺ edit_mode AND a field is selected.

        Called from both _toggle_edit_mode and wherever self.selected_field
        changes, so the red destructive button only appears when there's
        actually something to delete.
        """
        btn = getattr(self, "btn_delete_field", None)
        if btn is None:
            return
        btn.setVisible(self.edit_mode and self.selected_field is not None)

    def _on_line_mode_toggled(self, checked):
        self.line_mode = bool(checked)
        for pw in self.page_widgets:
            pw.line_mode = self.line_mode
        self._update_edit_banner()

    def _rotate_current_file(self):
        """Cycle the manual rotation override for the current file by +90°.

        Used when preprocessing metadata is missing and the ONNX classifier
        cannot detect the correct orientation (e.g. handwritten scans). The
        override is in-memory only -- it is NOT written to any file.
        """
        if not self.current_stem:
            return
        overrides = getattr(self, "_rotation_overrides", None)
        if overrides is None:
            overrides = {}
            self._rotation_overrides = overrides
        # If there is no override yet, start from the detected rotation so the
        # first click adds 90° on top of whatever metadata said.
        current = overrides.get(self.current_stem)
        if current is None:
            current = 0
            for src in (self.canonical_json, self.input_json):
                if not src:
                    continue
                for p in src.get("pages", []):
                    if p.get("page_index") == 0:
                        current = int(p.get("applied_rotation") or 0) % 360
                        break
                if current:
                    break
        new_rot = (current + 90) % 360
        overrides[self.current_stem] = new_rot
        # Force re-render to apply the new rotation.
        self._render_pdf()
        self._draw_overlays()
        self.status_left.setText(f"Đã xoay PDF → {new_rot}° (ghi tạm, không lưu vào file)")

    def _on_word_selection_changed(self, page_index, word_ids, op):
        """Apply set/add/remove on the selected field's word list.

        op = "set":    field.word_ids = word_ids (drag without modifier)
        op = "add":    field.word_ids ∪= word_ids (Ctrl)
        op = "remove": field.word_ids -= word_ids (Shift)
        """
        if not self.edit_mode:
            return
        field = self.selected_field
        if field is None:
            self.status_left.setText(
                "Chưa chọn field — bấm 1 field bên phải để bắt đầu sửa")
            return
        # If the field already has words on a different page, don't silently
        # mix pages; warn and bail.
        existing = list(field.get("word_ids", []))
        field_pi = field.get("page_index")
        if existing and field_pi is not None and field_pi != page_index:
            QMessageBox.warning(
                self, "Khác trang",
                f"Field '{field.get('label')}' hiện ở trang {field_pi + 1}. "
                f"Không thể thêm bbox từ trang {page_index + 1}.\n"
                f"Hãy xoá hết bbox cũ trước, hoặc tạo field mới.")
            return

        self._push_undo()
        new_set = list(existing)
        if op == "set":
            new_set = list(word_ids)
        elif op == "add":
            seen = set(new_set)
            for wid in word_ids:
                if wid not in seen:
                    new_set.append(wid)
                    seen.add(wid)
        elif op == "remove":
            drop = set(word_ids)
            new_set = [wid for wid in new_set if wid not in drop]
        elif op == "toggle":
            current_set = set(new_set)
            for wid in word_ids:
                if wid in current_set:
                    new_set.remove(wid)
                    current_set.discard(wid)
                else:
                    new_set.append(wid)
                    current_set.add(wid)
        else:
            return  # unknown op

        field["word_ids"] = new_set
        # If field had no page yet (newly created) or now has words, anchor it
        # to this page.
        if new_set:
            field["page_index"] = page_index
        self._rebuild_field_from_words(field, field.get("page_index", page_index))
        self.dirty = True
        self._refresh_after_edit()

    def _add_field(self):
        """Popup a label menu right under the "+ Field" button.

        Picking a label commits immediately — no separate OK/Cancel step —
        and drops the user straight into bbox-selection mode for the new
        (empty) field. This matches the labeler's workflow: "pick label,
        drag region, done".
        """
        if not self.output_json or not self.current_stem:
            QMessageBox.warning(self, "Chưa có file", "Hãy chọn một file trước.")
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background-color: {SURFACE}; color: {TEXT}; "
            f"border: 1px solid {BORDER}; padding: 4px; }}"
            f"QMenu::item {{ padding: 6px 16px; border-radius: 3px; }}"
            f"QMenu::item:selected {{ background-color: {ACCENT}; color: white; }}"
        )
        for label in V3_TRAIN_LABELS:
            _, light = get_label_color(label)
            act = QAction(label_display(label), self)
            act.triggered.connect(partial(self._create_field_with_label, label))
            # Foreground color matches the bbox color so labelers pair them visually
            act.setIconText(label_display(label))
            menu.addAction(act)
            # Colorize menu item text per label via QAction data is not trivial
            # in Qt6/PySide6 stylesheets; rely on bbox + tree colors for cueing.
        # Anchor the menu directly under the button so the click feels local.
        btn = self.btn_add_field
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _create_field_with_label(self, label: str, page_index: int | None = None):
        """Create an empty field with the chosen label and select it so the
        user can immediately drag-select words on the PDF.

        ``page_index`` defaults to the first rendered page. The context-menu
        flow (right-click on a PDF page) passes the page the user actually
        clicked so the new field attaches to that page rather than page 0.
        """
        if not self.output_json or not self.current_stem:
            return
        fields = self._get_fields()
        existing_ids = {f.get("field_id", "") for f in fields}
        n = 1
        while f"f{n}" in existing_ids:
            n += 1
        if page_index is None:
            page_index = self.page_widgets[0].page_index if self.page_widgets else 0
        new_field = {
            "field_id": f"f{n}",
            "label": label,
            "page_index": int(page_index),
            "line_ids": [],
            "word_ids": [],
            "text": "",
        }
        if "field_instances" in self.output_json:
            self.output_json["field_instances"].append(new_field)
        elif isinstance(self.output_json.get("annotations"), dict):
            self.output_json["annotations"].setdefault("field_instances", []).append(new_field)
        else:
            self.output_json["field_instances"] = [new_field]
        self.dirty = True
        self.selected_field = new_field
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._render_fields()
        self._reselect_field_in_tree(new_field["field_id"])
        self._render_details()
        self._draw_overlays()
        self._update_edit_banner()
        self.status_left.setText(
            f"Tạo field {label_display(label)} — kéo vùng trên PDF để gán bbox")

    def _add_relation(self):
        """Create a signed_by relation between a SIGNER_ROLE and a SIGNER_NAME."""
        if not self.output_json or not self.current_stem:
            QMessageBox.warning(self, "Chưa có file", "Hãy chọn một file trước.")
            return
        fields = self._get_fields()
        roles = [f for f in fields if f.get("label") == "SIGNER_ROLE"]
        names = [f for f in fields if f.get("label") == "SIGNER_NAME"]
        if not roles or not names:
            QMessageBox.information(
                self, "Thiếu field",
                f"Cần ít nhất 1 field {label_display('SIGNER_ROLE')} "
                f"và 1 field {label_display('SIGNER_NAME')} để tạo relation signed_by.",
            )
            return

        def _label_for(f):
            text = (f.get("text") or "").split("\n", 1)[0][:60]
            return f"{f.get('field_id', '?')} — {text}"

        role_labels = [_label_for(f) for f in roles]
        name_labels = [_label_for(f) for f in names]

        role_choice, ok = QInputDialog.getItem(
            self, "Tạo relation signed_by",
            f"Chọn {label_display('SIGNER_ROLE')} (từ):",
            role_labels, 0, False,
        )
        if not ok:
            return
        name_choice, ok = QInputDialog.getItem(
            self, "Tạo relation signed_by",
            f"Chọn {label_display('SIGNER_NAME')} (đến):",
            name_labels, 0, False,
        )
        if not ok:
            return

        from_field = roles[role_labels.index(role_choice)]
        to_field = names[name_labels.index(name_choice)]

        relations = self._get_relations()
        # Prevent exact duplicates
        from_id = from_field.get("field_id")
        to_id = to_field.get("field_id")
        for r in relations:
            if (r.get("type") == "signed_by"
                    and r.get("from_field_id") == from_id
                    and r.get("to_field_id") == to_id):
                QMessageBox.information(
                    self, "Đã có", "Relation này đã tồn tại.",
                )
                return

        existing_ids = {r.get("relation_id", "") for r in relations}
        n = 1
        while f"r{n}" in existing_ids:
            n += 1
        new_rel = {
            "relation_id": f"r{n}",
            "type": "signed_by",
            "from_field_id": from_id,
            "to_field_id": to_id,
        }
        # Insert into live list (flat or nested)
        if "relations" in self.output_json:
            self.output_json["relations"].append(new_rel)
        elif isinstance(self.output_json.get("annotations"), dict):
            self.output_json["annotations"].setdefault("relations", []).append(new_rel)
        else:
            self.output_json["relations"] = [new_rel]

        self.dirty = True
        self._render_details()
        self._update_status()
        self.status_left.setText(f"Đã tạo relation signed_by: {from_id} → {to_id}")

    def _delete_relation(self, relation_id):
        """Remove a relation by id (with confirmation)."""
        r = QMessageBox.question(
            self, "Xoá relation", f"Xoá relation {relation_id}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if r != QMessageBox.Yes:
            return
        for container in (
            self.output_json,
            self.output_json.get("annotations") if isinstance(self.output_json, dict) else None,
        ):
            if isinstance(container, dict) and "relations" in container:
                container["relations"] = [
                    rel for rel in container["relations"]
                    if rel.get("relation_id") != relation_id
                ]
        self.dirty = True
        self._render_details()
        self._update_status()
        self.status_left.setText(f"Đã xoá relation {relation_id}")

    def _delete_field(self):
        """Remove the currently selected field (after confirmation)."""
        if not self.selected_field:
            return
        field = self.selected_field
        label = field.get("label", "?")
        text_preview = (field.get("text", "") or "(trống)")[:60]
        r = QMessageBox.question(
            self, "Xoá field",
            f"Xoá field {label}?\n\n\"{text_preview}\"",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        fid = field.get("field_id")
        # Remove from field_instances (handle both flat & nested formats)
        for container in (
            self.output_json,
            self.output_json.get("annotations") if isinstance(self.output_json, dict) else None,
        ):
            if isinstance(container, dict) and "field_instances" in container:
                container["field_instances"] = [
                    f for f in container["field_instances"]
                    if f.get("field_id") != fid
                ]
        # Drop relations referencing this field
        for container in (
            self.output_json,
            self.output_json.get("annotations") if isinstance(self.output_json, dict) else None,
        ):
            if isinstance(container, dict) and "relations" in container:
                container["relations"] = [
                    r for r in container["relations"]
                    if r.get("from_field_id") != fid and r.get("to_field_id") != fid
                ]
        self.selected_field = None
        self.dirty = True
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.btn_delete_field.setVisible(False)
        self._render_fields()
        self._render_details()
        self._draw_overlays()
        self._update_edit_banner()
        self.status_left.setText(f"Đã xoá field {label}")

    def _update_edit_banner(self):
        """Banner shows the current edit-mode state and key shortcuts."""
        if not self.edit_mode:
            self.edit_banner.setText("")
            return
        if self.selected_field is None:
            self.edit_banner.setText(
                "CHẾ ĐỘ SỬA — chọn 1 field bên phải để bắt đầu, hoặc bấm '+ Field' để tạo mới")
            self.edit_banner.setStyleSheet(
                f"background-color: rgba(0, 123, 255, 0.15); "
                f"border: 1px solid {ACCENT}; border-radius: 4px; "
                f"padding: 6px 12px; color: {TEXT}; font-size: 12px;")
            return
        label = self.selected_field.get("label", "?")
        n_words = len(self.selected_field.get("word_ids", []))
        dark, light = get_label_color(label)
        self.edit_banner.setText(
            f"Đang sửa: {label}  ({n_words} word)\n"
            f"Click: chọn/bỏ • Ctrl+click: thêm • Shift+click: bỏ\n"
            f"Kéo: thay vùng • Ctrl+kéo: thêm vùng • Shift+kéo: bỏ vùng • Esc: thoát"
        )
        self.edit_banner.setStyleSheet(
            f"background-color: {dark}; color: white; font-weight: bold; "
            f"border: 1px solid {light}; border-radius: 4px; "
            f"padding: 6px 12px; font-size: 12px;")

    def _rebuild_field_from_words(self, field, page_index):
        """Rebuild line_ids and text from word_ids.

        Text rule:
          - If the selected word_ids cover every word of a line -> use
            line.ocr_text (falling back to line.text).
          - Partial line -> join word.ocr_text (falling back to word.text)
            using word.has_space_after when available; default to inserting
            a single space between words.
          - Multiple lines joined with "\\n" in canonical line order.
        """
        word_ids = list(field.get("word_ids", []))
        selected_set = set(word_ids)

        lm = self._get_line_map(page_index)
        wm = self._get_word_map(page_index)
        w2l = self._get_word_to_line_map(page_index)

        def _line_sort_key(line_id: str):
            line_data = lm.get(line_id) or {}
            order = line_data.get("order")
            bbox = line_data.get("bbox") or [0, 0, 0, 0]
            y = bbox[1] if len(bbox) > 1 else 0
            x = bbox[0] if len(bbox) > 0 else 0
            if isinstance(order, (int, float)):
                return (0, int(order), y, x, line_id)
            return (1, y, x, line_id)

        def _line_orders_are_contiguous(line_ids: list[str]) -> bool:
            if len(line_ids) <= 1:
                return True
            orders = []
            for line_id in line_ids:
                order = (lm.get(line_id) or {}).get("order")
                if not isinstance(order, (int, float)):
                    return False
                orders.append(int(order))
            start = orders[0]
            return orders == list(range(start, start + len(orders)))

        # Line-id membership
        lines_with_selection = set()
        for wid in word_ids:
            lid = w2l.get(wid)
            if lid:
                lines_with_selection.add(lid)
        ordered_line_ids = sorted(lines_with_selection, key=_line_sort_key)

        # DOC_NUMBER_SYMBOL occasionally suffers from OCR line-order glitches:
        # words that are visually on the same row get split into non-contiguous
        # line ids (for example "Số: 10" and "-BB/ĐU"). In that case keep the
        # precise word_ids, but drop line_ids so validator/export do not treat
        # it as a multi-line block span.
        if (
            field.get("label") == "DOC_NUMBER_SYMBOL"
            and len(ordered_line_ids) > 1
            and not _line_orders_are_contiguous(ordered_line_ids)
        ):
            field["line_ids"] = []
        else:
            field["line_ids"] = ordered_line_ids

        # Group selected words by line, preserving per-line x order.
        by_line: dict[str, list[dict]] = {}
        for wid in word_ids:
            w = wm.get(wid)
            if not w:
                continue
            lid = w2l.get(wid, "")
            by_line.setdefault(lid, []).append(w)
        for lid, words in by_line.items():
            words.sort(key=lambda w: (w.get("bbox", [0])[0] if w.get("bbox") else 0))

        def _line_ocr_surface(line_data):
            return (line_data.get("ocr_text") or line_data.get("text") or "").strip()

        def _line_all_word_ids(line_data):
            ids = []
            for w in line_data.get("words", []):
                ids.append(w.get("word_id") or w.get("id", ""))
            # Some canonical shapes store only an id list on the line
            if not ids:
                ids = list(line_data.get("word_ids") or [])
            return [i for i in ids if i]

        def _rebuild_partial(words):
            parts = []
            for w in words:
                text = w.get("ocr_text") or w.get("text", "")
                parts.append(text)
                if w.get("has_space_after", True):
                    parts.append(" ")
            return "".join(parts).strip()

        # Build per-line text. Full-line -> line.ocr_text; partial -> word join.
        line_chunks = []
        for lid in ordered_line_ids:
            words = by_line.get(lid) or []
            if not words:
                continue
            line_data = lm.get(lid) or {}
            all_ids = set(_line_all_word_ids(line_data))
            if all_ids and all_ids.issubset(selected_set):
                chunk = _line_ocr_surface(line_data) or _rebuild_partial(words)
            else:
                chunk = _rebuild_partial(words)
            line_chunks.append((lid, chunk))

        new_text = "\n".join(chunk for _, chunk in line_chunks if chunk)
        field["text"] = new_text
        if "normalized_value" in field:
            field["normalized_value"] = new_text

    def _push_undo(self):
        if not self.selected_field:
            return
        state = {
            "field_id": self.selected_field.get("field_id"),
            "word_ids": list(self.selected_field.get("word_ids", [])),
            "line_ids": list(self.selected_field.get("line_ids", [])),
            "text": self.selected_field.get("text", ""),
        }
        self.undo_stack.append(state)
        self.redo_stack.clear()

    def _undo(self):
        if not self.undo_stack or not self.edit_mode or not self.selected_field:
            return
        state = self.undo_stack[-1]
        if state["field_id"] != self.selected_field.get("field_id"):
            return
        self.undo_stack.pop()
        current = {
            "field_id": self.selected_field.get("field_id"),
            "word_ids": list(self.selected_field.get("word_ids", [])),
            "line_ids": list(self.selected_field.get("line_ids", [])),
            "text": self.selected_field.get("text", ""),
        }
        self.redo_stack.append(current)
        self.selected_field["word_ids"] = state["word_ids"]
        self.selected_field["line_ids"] = state["line_ids"]
        self.selected_field["text"] = state["text"]
        self.dirty = True
        self._refresh_after_edit()

    def _redo(self):
        if not self.redo_stack or not self.edit_mode or not self.selected_field:
            return
        state = self.redo_stack[-1]
        if state["field_id"] != self.selected_field.get("field_id"):
            return
        self.redo_stack.pop()
        current = {
            "field_id": self.selected_field.get("field_id"),
            "word_ids": list(self.selected_field.get("word_ids", [])),
            "line_ids": list(self.selected_field.get("line_ids", [])),
            "text": self.selected_field.get("text", ""),
        }
        self.undo_stack.append(current)
        self.selected_field["word_ids"] = state["word_ids"]
        self.selected_field["line_ids"] = state["line_ids"]
        self.selected_field["text"] = state["text"]
        self.dirty = True
        self._refresh_after_edit()

    def _refresh_after_edit(self):
        self._render_fields()
        self._render_details()
        self._draw_overlays()
        self._update_edit_banner()
        if self.selected_field:
            self._reselect_field_in_tree(self.selected_field.get("field_id"))
        self._update_status()

    def _reselect_field_in_tree(self, field_id):
        """Find and select the field item in the tree by field_id."""
        root = self.fields_tree.invisibleRootItem()
        for gi in range(root.childCount()):
            group = root.child(gi)
            for ci in range(group.childCount()):
                child = group.child(ci)
                if child.data(0, Qt.UserRole) == field_id:
                    self.fields_tree.setCurrentItem(child)
                    return

    # ═══════════════════════════════════════════════════
    # SAVE
    # ═══════════════════════════════════════════════════
    def _save(self):
        if not self.output_json or not self.current_stem:
            QMessageBox.warning(self, "Lỗi", "Không có dữ liệu để lưu.")
            return
        path = self.output_files.get(self.current_stem)
        if not path:
            QMessageBox.warning(self, "Lỗi",
                                f"Không tìm thấy file output cho '{self.current_stem}'.")
            return
        try:
            # Drop fields the user created but never filled (no word/line ids).
            # Saving a placeholder hurts training (model sees a labelled-but-
            # empty class) and is almost always an oversight from clicking
            # "+ Field" or the right-click menu without follow-through.
            # Relations dangling on those field_ids get cleaned up too so the
            # validator doesn't complain about orphan endpoints.
            raw_fields = self._get_fields()
            kept_fields = []
            dropped_field_ids = set()
            for f in raw_fields:
                wids = f.get("word_ids") or []
                lids = f.get("line_ids") or []
                if not wids and not lids:
                    dropped_field_ids.add(f.get("field_id"))
                else:
                    kept_fields.append(f)
            relations = [
                r for r in self._get_relations()
                if r.get("from_field_id") not in dropped_field_ids
                and r.get("to_field_id") not in dropped_field_ids
            ]
            n_dropped = len(dropped_field_ids)
            fields = kept_fields
            save_data = {
                "field_instances": fields,
                "relations": relations,
            }

            if not self._run_validator_before_save(save_data):
                return

            with open(path, "w", encoding="utf-8") as f:
                json.dump(save_data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            self.output_json = save_data
            self.dirty = False
            # If the just-selected field was one of the empty drops, clear it
            # so the right pane doesn't dangle on a deleted field_id.
            if (self._selected_field_id
                    and self._selected_field_id in dropped_field_ids):
                self.selected_field = None
            self._render_fields()
            self._render_details()
            self._update_status()
            # Secrecy status may have changed (user added/removed SECRECY_MARK)
            self._refresh_current_file_item_styling()

            # Post-save: reload file from disk and validate again to confirm
            # what was actually written is still consistent with canonical.
            post = self._run_validator_after_save(path)
            toast_kind = "success"
            empty_suffix = (
                f" (đã bỏ {n_dropped} field trống)" if n_dropped else ""
            )
            if post is None:
                msg = f"✓ Đã lưu {Path(path).name}{empty_suffix}"
            else:
                n_err, n_warn = post
                if n_err:
                    msg = f"⚠ Đã lưu nhưng reload validate còn {n_err} lỗi{empty_suffix}"
                    toast_kind = "error"
                elif n_warn:
                    msg = f"✓ Đã lưu ({n_warn} warning){empty_suffix}"
                else:
                    msg = f"✓ Đã lưu + validate OK{empty_suffix}"
            # After save: dirty = False already hides btn_save via the
            # property setter (retainSizeWhenHidden keeps neighbours put).
            # Feedback is delivered via a toast so nothing in the toolbar
            # has to morph or steal attention from the field panel.
            self._toast(msg, kind=toast_kind, duration_ms=3000)
            self.status_left.setText(f"{msg} — {path}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.warning(self, "Lỗi", f"Không thể lưu:\n{e}")

    def _run_validator_before_save(self, save_data):
        """Run the shared KIE validator. Return True if save may proceed.

        Hard errors -> block (show list, return False).
        Warnings    -> ask the user whether to save anyway.
        """
        if validate_label_output_detailed is None:
            # Validator unavailable (import failed). Warn once, allow save.
            if _VALIDATOR_IMPORT_ERROR is not None:
                QMessageBox.warning(
                    self, "Validator không khả dụng",
                    f"Không thể import scanindex.core.kie.labeling_workspace:\n{_VALIDATOR_IMPORT_ERROR}\n"
                    "Save vẫn tiếp tục nhưng KHÔNG được validate.",
                )
            return True
        if not self.canonical_json:
            QMessageBox.warning(
                self, "Thiếu canonical OCR",
                "Không có canonical JSON để validate. Save vẫn tiếp tục nhưng KHÔNG được validate.",
            )
            return True

        try:
            result = validate_label_output_detailed(
                save_data, self.canonical_json, llm_name="viewer_manual",
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "Validator lỗi", f"Validator crash:\n{exc}\n\nSave bị huỷ.",
            )
            return False

        errors = result.get("errors") or []
        warnings = result.get("warnings") or []

        if errors:
            self._show_validation_dialog(
                title="Có lỗi — KHÔNG thể lưu",
                header=f"Validator phát hiện {len(errors)} lỗi bắt buộc phải sửa:",
                errors=errors,
                warnings=warnings,
                blocking=True,
            )
            return False

        if warnings:
            return self._show_validation_dialog(
                title="Có cảnh báo",
                header=(
                    f"Validator không thấy lỗi nhưng có {len(warnings)} cảnh báo. "
                    "Bạn muốn vẫn lưu?"
                ),
                errors=[],
                warnings=warnings,
                blocking=False,
            )
        return True

    def _run_validator_after_save(self, saved_path):
        """Reload the file just written and run the validator again.

        Returns (n_errors, n_warnings) tuple, or None if skipped.
        On unexpected post-save errors, pops a dialog so the user knows.
        """
        if validate_label_output_detailed is None or not self.canonical_json:
            return None
        try:
            with open(saved_path, "r", encoding="utf-8") as f:
                on_disk = json.load(f)
        except Exception as exc:
            QMessageBox.warning(
                self, "Post-save load lỗi",
                f"Đã ghi file nhưng đọc lại không được:\n{exc}",
            )
            return None
        try:
            result = validate_label_output_detailed(
                on_disk, self.canonical_json, llm_name="viewer_manual",
            )
        except Exception as exc:
            QMessageBox.warning(
                self, "Post-save validate lỗi",
                f"Validator crash khi reload file:\n{exc}",
            )
            return None
        errors = result.get("errors") or []
        warnings = result.get("warnings") or []
        if errors:
            # Ghi file đã không thể có errors vì pre-save đã pass. Nếu xuất hiện
            # là bug nghiêm trọng của viewer - báo user ngay.
            self._show_validation_dialog(
                title="POST-SAVE: file đã ghi có lỗi bất thường",
                header=(
                    "File đã được ghi, nhưng khi đọc lại và validate thì còn lỗi. "
                    "Đây là bug — hãy báo kỹ thuật. Nội dung lỗi:"
                ),
                errors=errors,
                warnings=warnings,
                blocking=True,
            )
        return len(errors), len(warnings)

    def _show_validation_dialog(self, title, header, errors, warnings, blocking):
        """Show errors + warnings. Return True if user chooses to save anyway.

        blocking=True  -> only one button (Close), always returns False.
        blocking=False -> Save Anyway / Cancel.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(640, 420)
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel(header))

        txt = QTextEdit()
        txt.setReadOnly(True)
        lines = []
        if errors:
            lines.append("ERRORS:")
            for e in errors:
                lines.append(f"  • {e}")
            lines.append("")
        if warnings:
            lines.append("WARNINGS:")
            for w in warnings:
                if isinstance(w, dict):
                    msg = w.get("message") or w.get("code", "")
                    code = w.get("code", "")
                    lines.append(f"  • [{code}] {msg}")
                else:
                    lines.append(f"  • {w}")
        txt.setPlainText("\n".join(lines))
        layout.addWidget(txt, 1)

        btns = QDialogButtonBox()
        if blocking:
            btns.addButton(QDialogButtonBox.Close)
            btns.rejected.connect(dlg.reject)
            btns.accepted.connect(dlg.accept)
        else:
            save_btn = btns.addButton("Vẫn lưu", QDialogButtonBox.AcceptRole)
            btns.addButton("Hủy", QDialogButtonBox.RejectRole)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)

        result = dlg.exec()
        return (not blocking) and (result == QDialog.Accepted)

    def _check_unsaved(self):
        """Return True if ok to proceed, False to cancel."""
        if not self.dirty:
            return True
        r = QMessageBox.question(
            self, "Chưa lưu",
            f"File '{self.current_stem}' có thay đổi chưa lưu.\nBạn muốn lưu không?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if r == QMessageBox.Save:
            self._save()
            return True
        if r == QMessageBox.Discard:
            self.dirty = False
            return True
        return False  # Cancel

    # ═══════════════════════════════════════════════════
    # DETAILS PANEL
    # ═══════════════════════════════════════════════════
    def _clear_details(self):
        while self.details_layout.count():
            item = self.details_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _show_details_empty(self):
        msg = ("Chọn trường KIE → click từ trên PDF để gán/bỏ"
               if self.edit_mode else
               "Chọn một trường KIE để xem chi tiết")
        e = QLabel(msg)
        e.setAlignment(Qt.AlignCenter)
        e.setStyleSheet(f"color: {MUTED}; font-size: 12px; padding: 24px;")
        self.details_layout.addWidget(e)

    def _render_details(self):
        self._clear_details()
        if not self.selected_field:
            self._show_details_empty()
            return

        field = self.selected_field
        dark, light = get_label_color(field.get("label", ""))

        # Basic info
        self._add_section("Thông tin cơ bản")

        badge = QLabel(field.get("label", ""))
        badge.setStyleSheet(
            f"background-color: {dark}; color: {light}; "
            f"border-radius: 3px; padding: 1px 6px; font-size: 10px; font-weight: bold;"
        )
        badge.setFixedHeight(18)
        row = QHBoxLayout()
        lbl = QLabel("Nhãn")
        lbl.setObjectName("detail_label")
        row.addWidget(lbl)
        row.addWidget(badge)
        row.addStretch()
        w = QWidget()
        w.setLayout(row)
        self.details_layout.addWidget(w)

        self._add_detail("Văn bản", field.get("text", ""))
        nv = field.get("normalized_value")
        self._add_detail("Giá trị chuẩn", nv if nv else "--")
        conf = field.get("confidence")
        self._add_detail("Độ tin cậy", f"{conf*100:.2f}%" if conf is not None else "--", mono=True)
        self._add_detail("Trang", str((field.get("page_index", 0)) + 1), mono=True)

        # Words
        word_ids = field.get("word_ids", [])
        pi = field.get("page_index", 0)
        if word_ids and (self.input_json or self.canonical_json):
            self._add_section(f"Từ ({len(word_ids)})")
            wm = self._get_word_map(pi)
            flow = QWidget()
            fl = QHBoxLayout(flow)
            fl.setContentsMargins(0, 0, 0, 0)
            fl.setSpacing(3)
            for wid in word_ids:
                w = wm.get(wid, {})
                text = w.get("text", wid)
                bbox = w.get("bbox", [])
                tooltip = f"{wid}: [{', '.join(f'{v:.1f}' for v in bbox)}]" if bbox else wid
                chip = QLabel(text)
                chip.setToolTip(tooltip)
                chip.setStyleSheet(
                    f"background-color: {ELEVATED}; border: 1px solid {BORDER}; "
                    f"border-radius: 3px; padding: 1px 5px; font-size: 10px; "
                    f"font-family: '{FONT_MONO}'; color: {TEXT2};"
                )
                fl.addWidget(chip)
            fl.addStretch()
            self.details_layout.addWidget(flow)

        # Relations
        rels = self._get_relations()
        fid = field.get("field_id", "")
        related = [r for r in rels
                    if r.get("from_field_id") == fid or r.get("to_field_id") == fid]
        if related:
            self._add_section(f"Quan hệ ({len(related)})")
            all_f = self._get_fields()
            for rel in related:
                is_from = rel.get("from_field_id") == fid
                oid = rel.get("to_field_id") if is_from else rel.get("from_field_id")
                other = next((f for f in all_f if f.get("field_id") == oid), None)
                arrow = "\u2192" if is_from else "\u2190"
                rid = rel.get("relation_id", "")
                desc = f"{rel.get('type', '')} ({rid})"
                target = f"{arrow} {oid}"
                if other:
                    target += f": {other.get('text', '')}"
                if self.edit_mode and rid:
                    self._add_detail_with_action(
                        desc, target, action_text="Xoá",
                        on_click=partial(self._delete_relation, rid),
                    )
                else:
                    self._add_detail(desc, target)

        self.details_layout.addStretch()

    def _add_section(self, title):
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"background-color: {BORDER}; max-height: 1px; margin-top: 8px;")
        self.details_layout.addWidget(sep)
        lbl = QLabel(title)
        lbl.setStyleSheet(f"color: {TEXT2}; font-size: 11px; font-weight: bold; padding: 4px 0 2px 0;")
        self.details_layout.addWidget(lbl)

    def _add_detail(self, label_text, value_text, mono=False):
        row = QHBoxLayout()
        row.setSpacing(8)
        if label_text:
            lbl = QLabel(label_text)
            lbl.setObjectName("detail_label")
            lbl.setAlignment(Qt.AlignTop)
            lbl.setMinimumWidth(80)
            lbl.setMaximumWidth(120)
            row.addWidget(lbl)
        else:
            row.addSpacing(80)
        val = QLabel(value_text)
        val.setObjectName("detail_value_mono" if mono else "detail_value")
        val.setWordWrap(True)
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(val, 1)
        w = QWidget()
        w.setLayout(row)
        self.details_layout.addWidget(w)

    def _add_detail_with_action(self, label_text, value_text, action_text, on_click):
        """Like _add_detail, but with a small action button on the right."""
        row = QHBoxLayout()
        row.setSpacing(8)
        if label_text:
            lbl = QLabel(label_text)
            lbl.setObjectName("detail_label")
            lbl.setAlignment(Qt.AlignTop)
            lbl.setMinimumWidth(80)
            lbl.setMaximumWidth(120)
            row.addWidget(lbl)
        else:
            row.addSpacing(80)
        val = QLabel(value_text)
        val.setObjectName("detail_value")
        val.setWordWrap(True)
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(val, 1)
        btn = QPushButton(action_text)
        btn.setFixedWidth(50)
        btn.clicked.connect(lambda: on_click())
        row.addWidget(btn)
        w = QWidget()
        w.setLayout(row)
        self.details_layout.addWidget(w)

    # ═══════════════════════════════════════════════════
    # STATUS BAR
    # ═══════════════════════════════════════════════════
    def _update_status(self):
        parts = []
        if self.current_stem:
            parts.append(self.current_stem)
        if self.fitz_doc:
            parts.append(f"{len(self.fitz_doc)} trang")
        fields = self._get_fields()
        if fields:
            parts.append(f"{len(fields)} trường KIE")
        if self.dirty:
            parts.append("* chưa lưu")
        self.status_left.setText(" | ".join(parts) if parts else "--")

    def closeEvent(self, event):
        if not self._check_unsaved():
            event.ignore()
            return
        if self.fitz_doc:
            self.fitz_doc.close()
        super().closeEvent(event)


# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════
def main():
    """Launch the KIE Viewer. Used by both ``python -m kie_viewer`` (via
    :mod:`kie_viewer.__main__`) and direct script invocation below."""
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    viewer = KieViewer()
    viewer.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
