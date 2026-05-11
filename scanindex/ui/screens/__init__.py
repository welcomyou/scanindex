"""Screen widgets for the QStackedWidget-based main window."""
from .screen_base import ScreenContainer, ScreenContent
from .home_screen import (
    HomeScreen,
    FUNCTION_HOME,
    FUNCTION_PDF_TO_WORD,
    FUNCTION_DIGITIZATION,
    FUNCTION_REPOSITORY,
    FUNCTION_ARCHIVE,
    FUNCTION_KHO_LUU_TRU,
    FUNCTION_SETTINGS,
    FUNCTION_ABOUT,
    FUNCTION_ACCURACY,
    FUNCTION_SUPPORT_TOOLS,
)
from .accuracy_screen import AccuracyScreen
from .secret_file_scan_screen import SecretFileScanScreen
from .support_tools_screen import SupportToolsScreen


def __getattr__(name):
    if name in {"RepositoryScreen", "KhoLuuTruScreen"}:
        from scanindex.ui.repository import RepositoryScreen, KhoLuuTruScreen
        return {
            "RepositoryScreen": RepositoryScreen,
            "KhoLuuTruScreen": KhoLuuTruScreen,
        }[name]
    raise AttributeError(name)

__all__ = [
    "ScreenContainer", "ScreenContent",
    "HomeScreen", "AccuracyScreen", "SecretFileScanScreen",
    "SupportToolsScreen", "RepositoryScreen", "KhoLuuTruScreen",
    "FUNCTION_HOME", "FUNCTION_PDF_TO_WORD", "FUNCTION_DIGITIZATION",
    "FUNCTION_REPOSITORY", "FUNCTION_ARCHIVE", "FUNCTION_KHO_LUU_TRU",
    "FUNCTION_SETTINGS", "FUNCTION_ABOUT", "FUNCTION_ACCURACY",
    "FUNCTION_SUPPORT_TOOLS",
]
