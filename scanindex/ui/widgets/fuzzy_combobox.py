"""FuzzyComboBox — editable QComboBox with diacritic-insensitive
fuzzy filtering on its dropdown popup.

UX rules consolidated here so every combo across the app behaves the
same:

  * Wheel events are ignored (the operator regularly scrolls long forms
    and shouldn't accidentally cycle item values).
  * The combo is editable so the user can type to narrow the popup; new
    text is *not* inserted as a permanent item (NoInsert policy).
  * Typing matches the popup against a normalised form of each item —
    diacritics and case are stripped first, so "quyet" finds "Quyết
    định" and "qd" matches both "Quyết định" and "Quy định".

Drop-in for `QComboBox`. Set `addItems(...)` / `setCurrentText(...)`
exactly as before.
"""
from __future__ import annotations

import re
import unicodedata

from PySide6.QtCore import Qt, QSortFilterProxyModel, QModelIndex
from PySide6.QtWidgets import QComboBox, QCompleter


def _normalize(text: str) -> str:
    """Vietnamese-friendly normalisation: strip diacritics + lowercase.
    Maps đ/Đ to d/D before NFD decomposition."""
    if not text:
        return ""
    pre = text.replace("đ", "d").replace("Đ", "D")
    nfd = unicodedata.normalize("NFD", pre)
    no_acc = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return no_acc.lower()


def _compact(text: str) -> str:
    return "".join(ch for ch in _normalize(text) if ch.isalnum())


def _acronym(text: str) -> str:
    return "".join(part[:1] for part in re.findall(r"\w+", _normalize(text)))


class _FuzzyFilterProxy(QSortFilterProxyModel):
    """Substring-match proxy in the diacritic-stripped namespace. Empty
    needle accepts every row so the popup behaves like a normal
    dropdown when the user just clicks the arrow."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._needle = ""
        self._needle_compact = ""

    def set_needle(self, text: str) -> None:
        self._needle = _normalize(text)
        self._needle_compact = _compact(text)
        self.invalidateFilter()

    def filterAcceptsRow(  # type: ignore[override]
        self, source_row: int, source_parent: QModelIndex
    ) -> bool:
        if not self._needle:
            return True
        src = self.sourceModel()
        if src is None:
            return True
        idx = src.index(source_row, 0, source_parent)
        text = src.data(idx) or ""
        item = str(text)
        norm = _normalize(item)
        compact = _compact(item)
        acronym = _acronym(item)
        return (
            self._needle in norm
            or bool(self._needle_compact and self._needle_compact in compact)
            or bool(self._needle_compact and acronym.startswith(self._needle_compact))
        )


class FuzzyComboBox(QComboBox):
    """Editable combo with fuzzy popup filtering. See module docstring."""

    def __init__(self, parent=None, *, sort: bool = True):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._sort = sort

        # Completer source = filtering proxy over our own model. This
        # narrows the autocompletion list as the user types; the combo's
        # own currentIndex stays untouched until the user picks a row.
        self._proxy = _FuzzyFilterProxy(self)
        self._proxy.setSourceModel(self.model())

        completer = QCompleter(self._proxy, self)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        # The proxy already does fuzzy matching, so the completer's own
        # filter must be permissive — Match{Contains|StartsWith} would
        # double-filter and hide proxy hits.
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.setCompleter(completer)

        line = self.lineEdit()
        if line is not None:
            line.textEdited.connect(self._proxy.set_needle)

    def wheelEvent(self, event):  # type: ignore[override]
        event.ignore()

    # Re-bind the proxy whenever the underlying model changes (callers
    # using `addItems` after construction don't trigger setModel, but
    # explicit `setModel` calls do).
    def setModel(self, model):  # type: ignore[override]
        super().setModel(model)
        if hasattr(self, "_proxy"):
            self._proxy.setSourceModel(model)

    # ── default A-Z sort on the option list ─────────────────────────────
    # Items go through `_normalize` for the sort key so Vietnamese
    # diacritics order naturally next to their unaccented forms (e.g.
    # "Đề án" sorts with "De an"). Disable per instance with sort=False.

    def addItems(self, texts):  # type: ignore[override]
        if self._sort:
            texts = sorted(texts, key=lambda t: _normalize(str(t)))
        super().addItems(list(texts))

    def addItem(self, *args, **kwargs):  # type: ignore[override]
        super().addItem(*args, **kwargs)
        if self._sort:
            self._resort_items()

    def _resort_items(self) -> None:
        if self.count() <= 1:
            return
        current = self.currentText()
        items = [(self.itemText(i), self.itemData(i)) for i in range(self.count())]
        items.sort(key=lambda pair: _normalize(str(pair[0])))
        self.blockSignals(True)
        try:
            self.clear()
            for text, data in items:
                if data is None:
                    super().addItem(text)
                else:
                    super().addItem(text, data)
            if current:
                idx = self.findText(current)
                if idx >= 0:
                    self.setCurrentIndex(idx)
        finally:
            self.blockSignals(False)
