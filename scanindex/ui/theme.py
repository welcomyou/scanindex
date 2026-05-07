"""
Design tokens and QSS stylesheet for Lightweight OCR.
Dark-gray "Document Processing Workbench" theme — warm neutral, compact, professional.
Palette adapted from asr-vn project.
"""

# ===== SURFACE COLORS (warm dark gray — asr-vn style) =====
COLOR_BG        = "#2b2b2b"   # main background
COLOR_SURFACE   = "#3a3a3a"   # cards, panels, tab bar
COLOR_ELEVATED  = "#464646"   # hover bg, pill bg, elevated surface
COLOR_HOVER     = "#525252"   # hover state
COLOR_INPUT     = "#464646"   # input fields
COLOR_PANEL     = "#333333"   # side panels (log)

# ===== ACCENT =====
COLOR_ACCENT       = "#007bff"   # primary blue (asr-vn)
COLOR_ACCENT_HOVER = "#0056b3"
COLOR_GREEN        = "#28a745"
COLOR_GREEN_HOVER  = "#1e7e34"
COLOR_RED          = "#dc3545"
COLOR_RED_HOVER    = "#bd2130"
COLOR_WARNING      = "#ffc107"
COLOR_INFO         = "#00ced1"
COLOR_ORANGE       = "#fd7e14"

# ===== TEXT =====
COLOR_TEXT           = "#ffffff"   # primary text
COLOR_TEXT_SECONDARY = "#cccccc"   # secondary text
COLOR_TEXT_MUTED     = "#888888"   # muted / placeholder

# ===== BORDERS =====
COLOR_BORDER         = "#4a4a4a"   # subtle border
COLOR_BORDER_DEFAULT = "#555555"   # standard border
COLOR_SASH           = "#2b2b2b"

# ===== TYPOGRAPHY =====
FONT_UI   = "Segoe UI"
FONT_MONO = "Cascadia Code"
FONT_MONO_FALLBACK = "Consolas"

# ===== SPACING (4px base) =====
SP = {1: 4, 2: 8, 3: 12, 4: 16, 5: 20, 6: 24, 8: 32, 10: 40}

# ===== SIZES (compact) =====
CTRL_H     = 26    # compact control height
RADIUS_SM  = 3
RADIUS_MD  = 4
RADIUS_LG  = 6

# ===== LOG LEVELS =====
LOG_INFO    = "info"
LOG_ERROR   = "err"
LOG_DEBUG   = "debug"
LOG_SUCCESS = "success"

# ===== STATUS MAPPINGS =====
STATUS_KEY_MAP = {
    "Pending":        "status_pending",
    "Processing":     "status_processing",
    "OCR Processing": "status_ocr_processing",
    "OCR Done":       "status_ocr_done",
    "Correcting...":  "status_correcting",
    "Corrected":      "status_corrected",
    "Done":           "status_done",
    "Failed":         "status_failed",
    "Exporting...":   "status_exporting",
}

STATUS_COLOR_MAP = {
    "Pending":        COLOR_TEXT_MUTED,
    "Processing":     COLOR_WARNING,
    "OCR Processing": COLOR_WARNING,
    "OCR Done":       COLOR_ORANGE,
    "Correcting...":  COLOR_INFO,
    "Corrected":      COLOR_GREEN,
    "Done":           COLOR_GREEN,
    "Failed":         COLOR_RED,
    "Exporting...":   COLOR_WARNING,
}

# ===== Asset paths (resolved for QSS url(...) refs) =====
import os as _os
# Resolve assets/ next to gui/ — works both from source and from the
# PyInstaller-frozen exe (the spec bundles assets/ at the same level).
# Qt QSS expects forward slashes inside url(...) on Windows.
_ASSETS_DIR = _os.path.normpath(
    _os.path.join(_os.path.dirname(__file__), "..", "assets")
).replace("\\", "/")
CHEVRON_DOWN_URL = f"{_ASSETS_DIR}/chevron_down.png"


# ===== QSS =====
DARK_STYLESHEET = f"""
/* ---- Base ---- */
QMainWindow, QWidget {{
    background-color: {COLOR_BG};
    color: {COLOR_TEXT};
    font-family: "{FONT_UI}";
    font-size: 13px;
}}
QDialog {{
    background-color: {COLOR_BG};
    color: {COLOR_TEXT};
}}

/* ---- Splitter ---- */
QSplitter::handle {{
    background: {COLOR_BORDER};
    width: 3px;
}}
QSplitter::handle:hover {{
    background: {COLOR_ACCENT};
}}

/* ---- Tabs ---- */
QTabWidget::pane {{
    border: none;
    background: {COLOR_BG};
}}
QTabBar {{
    background: {COLOR_SURFACE};
    border-bottom: 1px solid {COLOR_BORDER};
}}
QTabBar::tab {{
    color: {COLOR_TEXT_SECONDARY};
    padding: 5px 16px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 12px;
    min-width: 60px;
    background: transparent;
}}
QTabBar::tab:selected {{
    color: {COLOR_ACCENT};
    font-weight: 600;
    border-bottom: 2px solid {COLOR_ACCENT};
}}
QTabBar::tab:hover:!selected {{
    color: {COLOR_TEXT};
    background: {COLOR_ELEVATED};
}}

/* ---- Buttons (compact) ---- */
QPushButton {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER_DEFAULT};
    border-radius: {RADIUS_MD}px;
    padding: 0 10px;
    font-size: 12px;
    min-height: {CTRL_H}px;
    max-height: {CTRL_H}px;
}}
QPushButton:hover {{
    background: {COLOR_HOVER};
    border-color: {COLOR_TEXT_MUTED};
}}
QPushButton:pressed {{
    background: {COLOR_BORDER};
}}
QPushButton:focus {{
    outline: none;
    border-color: {COLOR_ACCENT};
}}
QPushButton:disabled {{
    color: {COLOR_TEXT_MUTED};
    background: {COLOR_ELEVATED};
    border-color: {COLOR_BORDER};
}}
QPushButton[cssClass="primary"] {{
    background: {COLOR_ACCENT};
    color: #FFFFFF;
    border: none;
    font-weight: 600;
}}
QPushButton[cssClass="primary"]:hover {{
    background: {COLOR_ACCENT_HOVER};
}}
QPushButton[cssClass="success"] {{
    background: {COLOR_GREEN};
    color: #FFFFFF;
    border: none;
    font-weight: 600;
}}
QPushButton[cssClass="success"]:hover {{
    background: {COLOR_GREEN_HOVER};
}}
QPushButton[cssClass="danger"] {{
    background: {COLOR_RED};
    color: #FFFFFF;
    border: none;
    font-weight: 600;
}}
QPushButton[cssClass="danger"]:hover {{
    background: {COLOR_RED_HOVER};
}}
QPushButton[cssClass="ghost"] {{
    background: transparent;
    color: {COLOR_TEXT_SECONDARY};
    border: 1px solid {COLOR_BORDER_DEFAULT};
}}
QPushButton[cssClass="ghost"]:hover {{
    background: {COLOR_HOVER};
    color: {COLOR_TEXT};
}}
QPushButton[cssClass="icon"] {{
    background: transparent;
    border: none;
    padding: 0;
    min-width: 24px;
    max-width: 24px;
    min-height: 24px;
    max-height: 24px;
    border-radius: {RADIUS_SM}px;
    color: {COLOR_TEXT_MUTED};
}}
QPushButton[cssClass="icon"]:hover {{
    background: {COLOR_HOVER};
    color: {COLOR_TEXT};
}}

/* ---- Inputs (compact) ---- */
QLineEdit, QSpinBox {{
    background: {COLOR_INPUT};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER_DEFAULT};
    border-radius: {RADIUS_MD}px;
    padding: 0 6px;
    min-height: {CTRL_H}px;
    max-height: {CTRL_H}px;
    font-size: 12px;
    selection-background-color: {COLOR_ACCENT};
}}
QLineEdit:focus, QSpinBox:focus {{
    border-color: {COLOR_ACCENT};
}}
QLineEdit:read-only {{
    background: {COLOR_BG};
    color: {COLOR_TEXT_SECONDARY};
}}
QComboBox {{
    background: {COLOR_INPUT};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER_DEFAULT};
    border-radius: {RADIUS_MD}px;
    padding: 0 6px;
    min-height: {CTRL_H}px;
    max-height: {CTRL_H}px;
    font-size: 12px;
}}
QComboBox:focus {{
    border-color: {COLOR_ACCENT};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    border: none;
    background: {COLOR_ELEVATED};
    width: 22px;
    border-top-right-radius: {RADIUS_MD}px;
    border-bottom-right-radius: {RADIUS_MD}px;
}}
QComboBox::down-arrow {{
    image: url({CHEVRON_DOWN_URL});
    width: 10px;
    height: 10px;
}}
QComboBox::down-arrow:on {{
    /* Slight nudge while the popup is open so the arrow looks pressed. */
    top: 1px;
}}
QComboBox QAbstractItemView {{
    background: {COLOR_ELEVATED};
    color: {COLOR_TEXT};
    selection-background-color: {COLOR_ACCENT};
    selection-color: #FFFFFF;
    border: 1px solid {COLOR_BORDER_DEFAULT};
    outline: none;
}}

/* ---- Checkbox (compact) ---- */
QCheckBox {{
    color: {COLOR_TEXT};
    spacing: 4px;
    font-size: 12px;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border-radius: 3px;
    border: 1px solid {COLOR_BORDER_DEFAULT};
    background: {COLOR_INPUT};
}}
QCheckBox::indicator:checked {{
    background: {COLOR_ACCENT};
    border-color: {COLOR_ACCENT};
}}
QCheckBox::indicator:hover {{
    border-color: {COLOR_ACCENT};
}}

/* ---- ListWidget ---- */
QListWidget {{
    background: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_MD}px;
    outline: none;
}}
QListWidget::item {{
    padding: 0;
    border: none;
    background: transparent;
}}
QListWidget::item:selected {{
    background: transparent;
}}
QListWidget::item:hover {{
    background: {COLOR_ELEVATED};
}}

/* ---- ScrollBar (thin) ---- */
QScrollBar:vertical {{
    width: 6px;
    background: transparent;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {COLOR_BORDER_DEFAULT};
    border-radius: 3px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {COLOR_TEXT_MUTED};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    height: 6px;
    background: transparent;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {COLOR_BORDER_DEFAULT};
    border-radius: 3px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {COLOR_TEXT_MUTED};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ---- Table ---- */
QTableWidget {{
    background: {COLOR_SURFACE};
    gridline-color: {COLOR_BORDER};
    border: 1px solid {COLOR_BORDER};
    font-size: 12px;
    alternate-background-color: {COLOR_ELEVATED};
}}
QTableWidget::item {{
    padding: 3px 6px;
}}
QTableWidget::item:selected {{
    background: {COLOR_ACCENT};
    color: #FFFFFF;
}}
QHeaderView::section {{
    background: {COLOR_ELEVATED};
    color: {COLOR_TEXT_SECONDARY};
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
    border-right: 1px solid {COLOR_BORDER};
    padding: 3px 6px;
    font-weight: 600;
    font-size: 11px;
}}

/* ---- TextEdit ---- */
QTextEdit {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_MD}px;
    font-size: 12px;
    selection-background-color: {COLOR_ACCENT};
}}
QTextEdit#logPanel {{
    background: {COLOR_PANEL};
    color: {COLOR_TEXT_SECONDARY};
    font-family: "{FONT_MONO}", "{FONT_MONO_FALLBACK}", monospace;
    font-size: 11px;
    border: 1px solid {COLOR_BORDER};
}}

/* ---- ScrollArea ---- */
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

/* ---- Section Card ---- */
QFrame[cssClass="section-card"] {{
    background: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_MD}px;
}}

/* ---- Hint Box ---- */
QFrame[cssClass="hint-box"] {{
    background: {COLOR_ELEVATED};
    border-radius: {RADIUS_SM}px;
    border-left: 3px solid {COLOR_ACCENT};
}}

/* ---- Separator ---- */
QFrame[cssClass="separator"] {{
    background: {COLOR_BORDER};
    max-height: 1px;
    min-height: 1px;
}}

/* ---- ToolTip ---- */
QToolTip {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER_DEFAULT};
    padding: 3px 6px;
    font-size: 11px;
    border-radius: {RADIUS_SM}px;
}}

/* ---- ProgressBar ---- */
QProgressBar {{
    background: {COLOR_BORDER};
    border: none;
    border-radius: 4px;
    color: {COLOR_TEXT};
    font-size: 11px;
    text-align: center;
    min-height: 18px;
    max-height: 18px;
}}
QProgressBar::chunk {{
    background: {COLOR_ACCENT};
    border-radius: 4px;
}}

/* ---- GroupBox ---- */
QGroupBox {{
    background: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_MD}px;
    margin-top: 12px;
    padding-top: 8px;
    font-size: 11px;
    font-weight: 600;
    color: {COLOR_TEXT_SECONDARY};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    color: {COLOR_TEXT_SECONDARY};
}}

/* ---- Menu ---- */
QMenuBar {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border-bottom: 1px solid {COLOR_BORDER};
    font-size: 12px;
    padding: 1px 0;
}}
QMenuBar::item:selected {{
    background: {COLOR_HOVER};
}}
QMenu {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER_DEFAULT};
    padding: 2px 0;
}}
QMenu::item {{
    padding: 4px 24px 4px 8px;
    font-size: 12px;
}}
QMenu::item:selected {{
    background: {COLOR_ACCENT};
    color: #FFFFFF;
}}
QMenu::separator {{
    height: 1px;
    background: {COLOR_BORDER};
    margin: 2px 4px;
}}

/* ---- StatusBar ---- */
QStatusBar {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT_SECONDARY};
    border-top: 1px solid {COLOR_BORDER};
    font-size: 11px;
    min-height: 20px;
    max-height: 20px;
}}
"""
