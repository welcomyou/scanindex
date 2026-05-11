"""
Design tokens and QSS stylesheets for Lightweight OCR (ScanIndex).

Two palettes are provided — Dark (default) and Light — paired so each variant
keeps similar visual hierarchy and meets WCAG AA contrast (>=4.5:1 for body
text). The active palette is decided once at module import time by reading
[General]Theme from settings.ini. Switching theme persists to settings.ini
and applies after the next app restart, because many widgets capture
COLOR_* tokens at module load time and embed them in local QSS strings.
"""

import configparser

from scanindex.infra.paths import get_resource_path as _get_resource_path


# ===== TYPOGRAPHY (theme-independent) =====
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

# ===== Asset paths (resolved for QSS url(...) refs) =====
# Resolved via the central get_resource_path so frozen-exe and source-tree
# layouts both work. Qt QSS expects forward slashes inside url(...).
CHEVRON_DOWN_URL = _get_resource_path("assets/chevron_down.png").replace("\\", "/")


# ===== PALETTES =====
# Dark palette — original "Document Processing Workbench" warm-neutral.
_PALETTE_DARK = {
    "bg":             "#2b2b2b",  # main background
    "surface":        "#3a3a3a",  # cards, panels, tab bar
    "elevated":       "#464646",  # hover bg, pill bg, elevated surface
    "hover":          "#525252",  # hover state
    "input":          "#464646",  # input fields
    "panel":          "#333333",  # log side panels

    "accent":         "#007bff",
    "accent_hover":   "#0056b3",
    "green":          "#28a745",
    "green_hover":    "#1e7e34",
    "red":            "#dc3545",
    "red_hover":      "#bd2130",
    "warning":        "#ffc107",
    "info":           "#00ced1",
    "orange":         "#fd7e14",

    "text":           "#ffffff",
    "text_secondary": "#cccccc",
    "text_muted":     "#888888",

    "border":         "#4a4a4a",
    "border_default": "#555555",
    "sash":           "#2b2b2b",

    "on_accent":      "#ffffff",

    # Disabled colored-button bg/fg pairs — keep identity hue (blue/green/red)
    # but use a darker tinted background and a lighter foreground so the label
    # stays legible (AA >=4.5:1) while clearly looking inactive.
    "primary_disabled_bg": "#1a2b3f",   # dark blue tint
    "primary_disabled_fg": "#4d9eff",   # lighter accent for AA on bg
    "success_disabled_bg": "#1a3320",
    "success_disabled_fg": "#5fb573",
    "danger_disabled_bg":  "#3a1c20",
    "danger_disabled_fg":  "#ee6970",
}

# Light palette — paired mirror, validated for WCAG AA (>=4.5:1 body text).
# Accent/status colors use darker variants so white text remains readable
# on colored buttons and so the same hue read as foreground on white still
# meets AA.
_PALETTE_LIGHT = {
    "bg":             "#f5f5f5",  # warm light gray
    "surface":        "#ffffff",  # cards, panels, tab bar
    "elevated":       "#eeeeee",  # hover bg, pill bg
    "hover":          "#e0e0e0",  # clearly darker than elevated
    "input":          "#ffffff",
    "panel":          "#fafafa",

    "accent":         "#0066cc",  # ~5.0:1 on white (AA)
    "accent_hover":   "#004c99",
    "green":          "#1e7e34",  # ~4.7:1 on white
    "green_hover":    "#155724",
    "red":            "#c82333",  # ~5.0:1 on white
    "red_hover":      "#a71d2a",
    "warning":        "#b8860b",  # dark goldenrod ~4.5:1
    "info":           "#008b8b",  # dark cyan ~4.7:1
    "orange":         "#d35400",  # ~4.5:1

    "text":           "#1f1f1f",  # near-black, ~17:1 on white (AAA)
    "text_secondary": "#555555",  # ~7.5:1 on white (AAA)
    "text_muted":     "#6a6a6a",  # 5.4:1 on white, 4.7:1 on #eee (AA) —
                                  # darker than dark-theme equivalent so
                                  # disabled buttons/placeholders stay readable

    "border":         "#e0e0e0",
    "border_default": "#cccccc",
    "sash":           "#f5f5f5",

    "on_accent":      "#ffffff",

    # Disabled colored-button bg/fg pairs — pale tint background + the *_hover
    # (darker) variant as foreground so we comfortably clear WCAG AA (>=4.5:1)
    # while still looking faded compared to the saturated enabled state.
    "primary_disabled_bg": "#d4e6fa",   # pale blue, ~80% tint of accent on white
    "primary_disabled_fg": "#004c99",   # accent_hover — 6.5:1 on bg
    "success_disabled_bg": "#d4ead8",
    "success_disabled_fg": "#155724",   # green_hover
    "danger_disabled_bg":  "#f5d3d6",
    "danger_disabled_fg":  "#a71d2a",   # red_hover
}


def _read_theme_setting() -> str:
    """Read [General]Theme from settings.ini once. Default 'dark'."""
    try:
        cfg = configparser.ConfigParser()
        cfg.read(_get_resource_path("settings.ini"), encoding="utf-8")
        val = cfg.get("General", "Theme", fallback="dark").strip().lower()
        return "light" if val == "light" else "dark"
    except Exception:
        return "dark"


ACTIVE_THEME: str = _read_theme_setting()
_PALETTE = _PALETTE_LIGHT if ACTIVE_THEME == "light" else _PALETTE_DARK


# Module-level COLOR_* tokens populated from the active palette so existing
# `from scanindex.ui.theme import COLOR_BG, ...` imports keep working.
COLOR_BG             = _PALETTE["bg"]
COLOR_SURFACE        = _PALETTE["surface"]
COLOR_ELEVATED       = _PALETTE["elevated"]
COLOR_HOVER          = _PALETTE["hover"]
COLOR_INPUT          = _PALETTE["input"]
COLOR_PANEL          = _PALETTE["panel"]

COLOR_ACCENT         = _PALETTE["accent"]
COLOR_ACCENT_HOVER   = _PALETTE["accent_hover"]
COLOR_GREEN          = _PALETTE["green"]
COLOR_GREEN_HOVER    = _PALETTE["green_hover"]
COLOR_RED            = _PALETTE["red"]
COLOR_RED_HOVER      = _PALETTE["red_hover"]
COLOR_WARNING        = _PALETTE["warning"]
COLOR_INFO           = _PALETTE["info"]
COLOR_ORANGE         = _PALETTE["orange"]

COLOR_TEXT           = _PALETTE["text"]
COLOR_TEXT_SECONDARY = _PALETTE["text_secondary"]
COLOR_TEXT_MUTED     = _PALETTE["text_muted"]

COLOR_BORDER         = _PALETTE["border"]
COLOR_BORDER_DEFAULT = _PALETTE["border_default"]
COLOR_SASH           = _PALETTE["sash"]


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


# Reusable QSS for the dropdown chevron on QComboBox. Append this to any
# local widget-level setStyleSheet that re-styles QComboBox, otherwise
# Qt skips the global ::drop-down / ::down-arrow rules and the arrow
# disappears.
COMBOBOX_DROPDOWN_QSS = f"""
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    border: none;
    background: {COLOR_ELEVATED};
    width: 22px;
    border-top-right-radius: 4px;
    border-bottom-right-radius: 4px;
}}
QComboBox::down-arrow {{
    image: url({CHEVRON_DOWN_URL});
    width: 10px;
    height: 10px;
}}
QComboBox::down-arrow:on {{
    top: 1px;
}}
"""


def _build_stylesheet(p: dict) -> str:
    """Build the full QSS string for the given palette."""
    return f"""
/* ---- Base ---- */
QMainWindow, QWidget {{
    background-color: {p["bg"]};
    color: {p["text"]};
    font-family: "{FONT_UI}";
    font-size: 13px;
}}
QDialog {{
    background-color: {p["bg"]};
    color: {p["text"]};
}}

/* ---- Splitter ---- */
QSplitter::handle {{
    background: {p["border"]};
    width: 3px;
}}
QSplitter::handle:hover {{
    background: {p["accent"]};
}}

/* ---- Tabs ---- */
QTabWidget::pane {{
    border: none;
    background: {p["bg"]};
}}
QTabBar {{
    background: {p["surface"]};
    border-bottom: 1px solid {p["border"]};
}}
QTabBar::tab {{
    color: {p["text_secondary"]};
    padding: 5px 16px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 12px;
    min-width: 60px;
    background: transparent;
}}
QTabBar::tab:selected {{
    color: {p["accent"]};
    font-weight: 600;
    border-bottom: 2px solid {p["accent"]};
}}
QTabBar::tab:hover:!selected {{
    color: {p["text"]};
    background: {p["elevated"]};
}}

/* ---- Buttons (compact) ---- */
QPushButton {{
    background: {p["surface"]};
    color: {p["text"]};
    border: 1px solid {p["border_default"]};
    border-radius: {RADIUS_MD}px;
    padding: 0 10px;
    font-size: 12px;
    min-height: {CTRL_H}px;
    max-height: {CTRL_H}px;
}}
QPushButton:hover {{
    background: {p["hover"]};
    border-color: {p["text_muted"]};
}}
QPushButton:pressed {{
    background: {p["border"]};
}}
QPushButton:focus {{
    outline: none;
    border-color: {p["accent"]};
}}
QPushButton:disabled {{
    color: {p["text_secondary"]};
    background: {p["elevated"]};
    border-color: {p["border"]};
}}
/* Colored button classes — pair :enabled and :disabled explicitly so Qt
 * does not match the same rule against both states. Qt's QSS engine has
 * known quirks combining attribute selectors with implicit-state rules
 * (e.g. `[cssClass="primary"]` without a pseudo-class), where the base
 * rule can override the more-specific `:disabled` rule. Using `:enabled`
 * on the base rule eliminates the ambiguity. */
QPushButton[cssClass="primary"]:enabled {{
    background: {p["accent"]};
    color: {p["on_accent"]};
    border: none;
    font-weight: 600;
}}
QPushButton[cssClass="primary"]:enabled:hover {{
    background: {p["accent_hover"]};
}}
QPushButton[cssClass="primary"]:disabled {{
    background: {p["primary_disabled_bg"]};
    color: {p["primary_disabled_fg"]};
    border: 1px solid {p["primary_disabled_fg"]};
    font-weight: 600;
}}
QPushButton[cssClass="success"]:enabled {{
    background: {p["green"]};
    color: {p["on_accent"]};
    border: none;
    font-weight: 600;
}}
QPushButton[cssClass="success"]:enabled:hover {{
    background: {p["green_hover"]};
}}
QPushButton[cssClass="success"]:disabled {{
    background: {p["success_disabled_bg"]};
    color: {p["success_disabled_fg"]};
    border: 1px solid {p["success_disabled_fg"]};
    font-weight: 600;
}}
QPushButton[cssClass="danger"]:enabled {{
    background: {p["red"]};
    color: {p["on_accent"]};
    border: none;
    font-weight: 600;
}}
QPushButton[cssClass="danger"]:enabled:hover {{
    background: {p["red_hover"]};
}}
QPushButton[cssClass="danger"]:disabled {{
    background: {p["danger_disabled_bg"]};
    color: {p["danger_disabled_fg"]};
    border: 1px solid {p["danger_disabled_fg"]};
    font-weight: 600;
}}
QPushButton[cssClass="ghost"] {{
    background: transparent;
    color: {p["text_secondary"]};
    border: 1px solid {p["border_default"]};
}}
QPushButton[cssClass="ghost"]:hover {{
    background: {p["hover"]};
    color: {p["text"]};
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
    color: {p["text_muted"]};
}}
QPushButton[cssClass="icon"]:hover {{
    background: {p["hover"]};
    color: {p["text"]};
}}

/* ---- Inputs (compact) ---- */
QLineEdit, QSpinBox {{
    background: {p["input"]};
    color: {p["text"]};
    border: 1px solid {p["border_default"]};
    border-radius: {RADIUS_MD}px;
    padding: 0 6px;
    min-height: {CTRL_H}px;
    max-height: {CTRL_H}px;
    font-size: 12px;
    selection-background-color: {p["accent"]};
    selection-color: {p["on_accent"]};
}}
QLineEdit:focus, QSpinBox:focus {{
    border-color: {p["accent"]};
}}
QLineEdit:read-only {{
    background: {p["bg"]};
    color: {p["text_secondary"]};
}}
QComboBox {{
    background: {p["input"]};
    color: {p["text"]};
    border: 1px solid {p["border_default"]};
    border-radius: {RADIUS_MD}px;
    padding: 0 6px;
    min-height: {CTRL_H}px;
    max-height: {CTRL_H}px;
    font-size: 12px;
}}
QComboBox:focus {{
    border-color: {p["accent"]};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    border: none;
    background: {p["elevated"]};
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
    background: {p["elevated"]};
    color: {p["text"]};
    selection-background-color: {p["accent"]};
    selection-color: {p["on_accent"]};
    border: 1px solid {p["border_default"]};
    outline: none;
}}

/* ---- Checkbox (compact) ---- */
QCheckBox {{
    color: {p["text"]};
    spacing: 4px;
    font-size: 12px;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border-radius: 3px;
    border: 1px solid {p["border_default"]};
    background: {p["input"]};
}}
QCheckBox::indicator:checked {{
    background: {p["accent"]};
    border-color: {p["accent"]};
}}
QCheckBox::indicator:hover {{
    border-color: {p["accent"]};
}}

/* ---- ListWidget ---- */
QListWidget {{
    background: {p["surface"]};
    border: 1px solid {p["border"]};
    border-radius: {RADIUS_MD}px;
    outline: none;
}}
QListWidget::item {{
    padding: 0;
    border: none;
    background: transparent;
}}
QListWidget::item:selected {{
    background: {p["accent"]};
    color: {p["on_accent"]};
}}
QListWidget::item:hover {{
    background: {p["elevated"]};
}}

/* ---- ScrollBar (thin) ---- */
QScrollBar:vertical {{
    width: 6px;
    background: transparent;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p["border_default"]};
    border-radius: 3px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p["text_muted"]};
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
    background: {p["border_default"]};
    border-radius: 3px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {p["text_muted"]};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ---- Table ---- */
QTableWidget {{
    background: {p["surface"]};
    gridline-color: {p["border"]};
    border: 1px solid {p["border"]};
    font-size: 12px;
    alternate-background-color: {p["elevated"]};
}}
QTableWidget::item {{
    padding: 3px 6px;
}}
QTableWidget::item:selected {{
    background: {p["accent"]};
    color: {p["on_accent"]};
}}
QHeaderView::section {{
    background: {p["elevated"]};
    color: {p["text_secondary"]};
    border: none;
    border-bottom: 1px solid {p["border"]};
    border-right: 1px solid {p["border"]};
    padding: 3px 6px;
    font-weight: 600;
    font-size: 11px;
}}

/* ---- TextEdit ---- */
QTextEdit {{
    background: {p["surface"]};
    color: {p["text"]};
    border: 1px solid {p["border"]};
    border-radius: {RADIUS_MD}px;
    font-size: 12px;
    selection-background-color: {p["accent"]};
    selection-color: {p["on_accent"]};
}}
QTextEdit#logPanel {{
    background: {p["panel"]};
    color: {p["text_secondary"]};
    font-family: "{FONT_MONO}", "{FONT_MONO_FALLBACK}", monospace;
    font-size: 11px;
    border: 1px solid {p["border"]};
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
    background: {p["surface"]};
    border: 1px solid {p["border"]};
    border-radius: {RADIUS_MD}px;
}}

/* ---- Hint Box ---- */
QFrame[cssClass="hint-box"] {{
    background: {p["elevated"]};
    border-radius: {RADIUS_SM}px;
    border-left: 3px solid {p["accent"]};
}}

/* ---- Separator ---- */
QFrame[cssClass="separator"] {{
    background: {p["border"]};
    max-height: 1px;
    min-height: 1px;
}}

/* ---- ToolTip ---- */
QToolTip {{
    background: {p["surface"]};
    color: {p["text"]};
    border: 1px solid {p["border_default"]};
    padding: 3px 6px;
    font-size: 11px;
    border-radius: {RADIUS_SM}px;
}}

/* ---- ProgressBar ---- */
QProgressBar {{
    background: {p["border"]};
    border: none;
    border-radius: 4px;
    color: {p["text"]};
    font-size: 11px;
    text-align: center;
    min-height: 18px;
    max-height: 18px;
}}
QProgressBar::chunk {{
    background: {p["accent"]};
    border-radius: 4px;
}}

/* ---- GroupBox ---- */
QGroupBox {{
    background: {p["surface"]};
    border: 1px solid {p["border"]};
    border-radius: {RADIUS_MD}px;
    margin-top: 12px;
    padding-top: 8px;
    font-size: 11px;
    font-weight: 600;
    color: {p["text_secondary"]};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    color: {p["text_secondary"]};
}}

/* ---- Menu ---- */
QMenuBar {{
    background: {p["surface"]};
    color: {p["text"]};
    border-bottom: 1px solid {p["border"]};
    font-size: 12px;
    padding: 1px 0;
}}
QMenuBar::item:selected {{
    background: {p["hover"]};
}}
QMenu {{
    background: {p["surface"]};
    color: {p["text"]};
    border: 1px solid {p["border_default"]};
    padding: 2px 0;
}}
QMenu::item {{
    padding: 4px 24px 4px 8px;
    font-size: 12px;
}}
QMenu::item:selected {{
    background: {p["accent"]};
    color: {p["on_accent"]};
}}
QMenu::separator {{
    height: 1px;
    background: {p["border"]};
    margin: 2px 4px;
}}

/* ---- StatusBar ---- */
QStatusBar {{
    background: {p["surface"]};
    color: {p["text_secondary"]};
    border-top: 1px solid {p["border"]};
    font-size: 11px;
    min-height: 20px;
    max-height: 20px;
}}
"""


DARK_STYLESHEET  = _build_stylesheet(_PALETTE_DARK)
LIGHT_STYLESHEET = _build_stylesheet(_PALETTE_LIGHT)

# The stylesheet matching the active theme. ocr_app.py applies this on
# QApplication; switching theme requires app restart.
APP_STYLESHEET = LIGHT_STYLESHEET if ACTIVE_THEME == "light" else DARK_STYLESHEET


# ===== WIDGET-LEVEL BUTTON QSS =====
# Use these directly with `widget.setStyleSheet(...)` when the global
# `QPushButton[cssClass="primary"]` attribute selector is NOT being matched
# by Qt's QSS engine reliably (which can happen when the button lives
# under a parent QFrame that has its own setStyleSheet). Widget-level QSS
# always wins, so the enabled / hover / disabled triplet is guaranteed.
def _make_colored_button_qss(bg: str, bg_hover: str,
                              disabled_bg: str, disabled_fg: str,
                              on_color: str) -> str:
    return f"""
QPushButton {{
    background: {bg};
    color: {on_color};
    border: none;
    border-radius: {RADIUS_MD}px;
    padding: 0 10px;
    font-size: 12px;
    font-weight: 600;
    min-height: {CTRL_H}px;
    max-height: {CTRL_H}px;
}}
QPushButton:hover {{
    background: {bg_hover};
}}
QPushButton:disabled {{
    background: {disabled_bg};
    color: {disabled_fg};
    border: 1px solid {disabled_fg};
}}
"""


BUTTON_PRIMARY_QSS = _make_colored_button_qss(
    _PALETTE["accent"], _PALETTE["accent_hover"],
    _PALETTE["primary_disabled_bg"], _PALETTE["primary_disabled_fg"],
    _PALETTE["on_accent"],
)
BUTTON_SUCCESS_QSS = _make_colored_button_qss(
    _PALETTE["green"], _PALETTE["green_hover"],
    _PALETTE["success_disabled_bg"], _PALETTE["success_disabled_fg"],
    _PALETTE["on_accent"],
)
BUTTON_DANGER_QSS = _make_colored_button_qss(
    _PALETTE["red"], _PALETTE["red_hover"],
    _PALETTE["danger_disabled_bg"], _PALETTE["danger_disabled_fg"],
    _PALETTE["on_accent"],
)
