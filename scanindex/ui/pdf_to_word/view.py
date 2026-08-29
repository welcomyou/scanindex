"""
DnD Tab — Drag & Drop file processing tab.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton
)
from PySide6.QtCore import Qt, Signal

from scanindex.ui.theme import SP, BUTTON_PRIMARY_QSS, BUTTON_SUCCESS_QSS, BUTTON_DANGER_QSS
from scanindex.ui.widgets.file_list_widget import FileListWidget
from scanindex.infra import translations


class DnDTab(QWidget):
    """Drag & Drop tab with toolbar and file list."""

    add_files_clicked = Signal()
    process_clicked = Signal()
    stop_clicked = Signal()
    clear_clicked = Signal()

    def __init__(self, icons: dict = None, parent=None):
        super().__init__(parent)
        self._icons = icons or {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP[1], SP[1], SP[1], SP[1])
        layout.setSpacing(SP[1])

        toolbar = QHBoxLayout()
        toolbar.setSpacing(SP[1])

        self.btn_add = QPushButton(translations.get_text("btn_add_files"))
        self.btn_add.setProperty("cssClass", "primary")
        self.btn_add.setStyleSheet(BUTTON_PRIMARY_QSS)
        self.btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add.clicked.connect(self.add_files_clicked.emit)
        toolbar.addWidget(self.btn_add)

        self.btn_process = QPushButton(translations.get_text("btn_process_all"))
        self.btn_process.setProperty("cssClass", "success")
        self.btn_process.setStyleSheet(BUTTON_SUCCESS_QSS)
        self.btn_process.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_process.clicked.connect(self.process_clicked.emit)
        toolbar.addWidget(self.btn_process)

        self.btn_stop = QPushButton(translations.get_text("btn_stop"))
        self.btn_stop.setProperty("cssClass", "danger")
        self.btn_stop.setStyleSheet(BUTTON_DANGER_QSS)
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop.clicked.connect(self.stop_clicked.emit)
        self.btn_stop.setVisible(False)
        toolbar.addWidget(self.btn_stop)

        toolbar.addStretch()

        self.btn_clear = QPushButton(translations.get_text("btn_clear"))
        self.btn_clear.setProperty("cssClass", "ghost")
        self.btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear.clicked.connect(self.clear_clicked.emit)
        toolbar.addWidget(self.btn_clear)

        layout.addLayout(toolbar)

        self.file_list = FileListWidget(list_type="dnd", icons=self._icons)
        layout.addWidget(self.file_list)

    def set_processing_state(self, is_running: bool):
        self.btn_process.setVisible(not is_running)
        self.btn_stop.setVisible(is_running)
        self.btn_add.setEnabled(not is_running)
        self.btn_clear.setEnabled(not is_running)

    def update_texts(self):
        self.btn_add.setText(translations.get_text("btn_add_files"))
        self.btn_process.setText(translations.get_text("btn_process_all"))
        self.btn_stop.setText(translations.get_text("btn_stop"))
        self.btn_clear.setText(translations.get_text("btn_clear"))
