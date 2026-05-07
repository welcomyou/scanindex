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
)
from .accuracy_screen import AccuracyScreen
from scanindex.ui.repository import RepositoryScreen, KhoLuuTruScreen

__all__ = [
    "ScreenContainer", "ScreenContent",
    "HomeScreen", "AccuracyScreen", "RepositoryScreen", "KhoLuuTruScreen",
    "FUNCTION_HOME", "FUNCTION_PDF_TO_WORD", "FUNCTION_DIGITIZATION",
    "FUNCTION_REPOSITORY", "FUNCTION_ARCHIVE", "FUNCTION_KHO_LUU_TRU",
    "FUNCTION_SETTINGS", "FUNCTION_ABOUT", "FUNCTION_ACCURACY",
]
