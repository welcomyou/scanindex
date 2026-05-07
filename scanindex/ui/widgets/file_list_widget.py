"""
FileListWidget — QListWidget managing a list of FileItemWidgets.
Supports drag-and-drop of PDF files and empty state display.
"""
import os
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QLabel, QVBoxLayout, QWidget
from PySide6.QtCore import Qt, Signal, QSize, QMimeData
from PySide6.QtGui import QDragEnterEvent, QDropEvent

from scanindex.ui.theme import COLOR_TEXT_MUTED, COLOR_ACCENT, FONT_UI, SP
from scanindex.ui.widgets.file_item_widget import FileItemWidget


class FileListWidget(QListWidget):
    """File list with drag-and-drop support and custom item widgets."""

    files_dropped = Signal(list)  # List of file paths

    def __init__(self, list_type: str = "dnd", icons: dict = None, parent=None):
        super().__init__(parent)
        self._list_type = list_type
        self._icons = icons or {}
        self._item_widgets = {}  # index -> FileItemWidget
        self._show_relative = None

        self.setAcceptDrops(list_type == "dnd")
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly if list_type == "dnd"
                             else QListWidget.DragDropMode.NoDragDrop)
        self.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSpacing(0)

    def set_show_relative(self, base_dir: str):
        """Set base directory for showing relative file paths (batch mode)."""
        self._show_relative = base_dir

    def populate(self, files: list):
        """Rebuild the list from a list of file dicts."""
        self.clear()
        self._item_widgets.clear()

        for i, item in enumerate(files):
            self._add_item_widget(i, item)

    def _add_item_widget(self, index: int, item: dict):
        widget = FileItemWidget(
            index=index,
            item=item,
            list_type=self._list_type,
            icons=self._icons,
            show_relative=self._show_relative,
        )

        list_item = QListWidgetItem(self)
        list_item.setSizeHint(QSize(0, 32))
        list_item.setFlags(list_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.setItemWidget(list_item, widget)
        self._item_widgets[index] = widget

    def get_widget(self, index: int) -> FileItemWidget:
        return self._item_widgets.get(index)

    def update_item_status(self, index: int, status: str):
        widget = self._item_widgets.get(index)
        if widget:
            widget.update_status(status)

    def update_item_data(self, index: int, item: dict):
        widget = self._item_widgets.get(index)
        if widget:
            widget.update_item(item)

    # --- Drag & Drop ---
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            files = []
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if path and path.lower().endswith(".pdf"):
                    files.append(path)
            if files:
                self.files_dropped.emit(files)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)
