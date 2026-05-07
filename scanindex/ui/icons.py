"""
Icon loader for Lightweight OCR.
Loads PNG icons from assets/ directory, returns QIcon/QPixmap.
"""
import os
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtCore import QSize


_icons_cache = {}


def _asset_dir():
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")


def load_icon(name: str, size: tuple = None) -> QIcon:
    """Load a PNG icon from assets/ by name (without extension)."""
    key = (name, size)
    if key in _icons_cache:
        return _icons_cache[key]

    path = os.path.join(_asset_dir(), f"{name}.png")
    if not os.path.exists(path):
        return QIcon()

    icon = QIcon(path)
    _icons_cache[key] = icon
    return icon


def load_pixmap(name: str, width: int = 20, height: int = 16) -> QPixmap:
    """Load a PNG icon as QPixmap with specified size."""
    path = os.path.join(_asset_dir(), f"{name}.png")
    if not os.path.exists(path):
        return QPixmap()

    pm = QPixmap(path)
    return pm.scaled(QSize(width, height), mode=1)  # SmoothTransformation


def load_all_icons() -> dict:
    """Pre-load all standard icons used in the app."""
    icons = {}
    icon_defs = {
        "eye_yellow": (20, 16),
        "eye_green": (20, 16),
        "eye_gray": (20, 16),
        "refresh": (14, 14),
    }
    for name, (w, h) in icon_defs.items():
        try:
            icons[name] = load_pixmap(name, w, h)
        except Exception:
            icons[name] = QPixmap()
    return icons
