"""
Settings Tab — Application configuration with section cards.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QScrollArea,
    QLabel, QLineEdit, QComboBox, QCheckBox, QPushButton, QFrame,
    QTabWidget, QListWidget, QListWidgetItem, QSplitter
)
from PySide6.QtCore import Qt, Signal

from scanindex.ui.theme import (
    COLOR_TEXT, COLOR_TEXT_MUTED, SP
)
from scanindex.ui.widgets.section_card import SectionCard
from scanindex.ui.widgets.fuzzy_combobox import FuzzyComboBox
from scanindex.infra import translations


class SettingsTab(QWidget):
    """Settings tab with scrollable section cards."""

    save_clicked = Signal()
    language_changed = Signal(str)
    model_changed = Signal(str)
    log_panel_toggled = Signal(bool)
    reset_archive_requested = Signal()

    def __init__(self, current_language: str = "en", parent=None):
        super().__init__(parent)
        self._current_language = current_language
        self._kie_mode_values = ["layoutlmv3"]
        self._catalogs: dict[str, dict] = {}
        self._current_catalog_key = ""
        self._setup_ui()

    def _setup_ui(self):
        self._outer_layout = QVBoxLayout(self)
        self._outer_layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()
        self._outer_layout.addWidget(self._tabs, 1)

        self._technical_layout = self._make_tab("Thông số kỹ thuật")
        self._catalog_layout = self._make_tab("Danh mục")
        self._language_layout = self._make_tab("Ngôn ngữ")

        self._layout = self._technical_layout
        self._build_ocr_section()
        self._build_correction_section()
        self._build_logging_section()
        self._build_archive_data_section()
        self._layout.addStretch()

        self._layout = self._catalog_layout
        self._build_catalog_section()
        self._layout.addStretch()

        self._layout = self._language_layout
        self._build_general_section()
        self._layout.addStretch()

        self._build_save_button()

    def _make_tab(self, title: str) -> QVBoxLayout:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(SP[3], SP[2], SP[3], SP[3])
        layout.setSpacing(SP[2])

        scroll.setWidget(container)
        self._tabs.addTab(scroll, title)
        return layout

    def _row_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 12px; background: transparent; border: none;")
        return lbl

    def _build_general_section(self):
        card = SectionCard(translations.get_text("sec_general"))
        form = QFormLayout()
        form.setSpacing(SP[1])
        form.setContentsMargins(0, 0, 0, 0)

        # FuzzyComboBox(sort=False): only two options, native order is fine.
        self.combo_lang = FuzzyComboBox(sort=False)
        self.combo_lang.addItems(["English", "Tiếng Việt"])
        self.combo_lang.setCurrentText(
            "Tiếng Việt" if self._current_language == "vi" else "English")
        self.combo_lang.setFixedWidth(160)
        self.combo_lang.currentTextChanged.connect(self._on_lang_change)
        form.addRow(self._row_label(translations.get_text("lbl_language")), self.combo_lang)

        card.content_layout().addLayout(form)
        self._layout.addWidget(card)

    def _build_ocr_section(self):
        card = SectionCard(translations.get_text("sec_ocr_processing"))
        form = QFormLayout()
        form.setSpacing(SP[1])
        form.setContentsMargins(0, 0, 0, 0)

        # Legacy compatibility values. Still forwarded to the backend, but no
        # longer shown because ScreenAI now runs direct parallel OCR.
        self._wait_page_value = "1.0"
        self._compare_value = "1.0"

        # Renamed semantics: now = number of OCR PAGES processed concurrently
        # across the whole app (not number of files). Default 4.
        self.entry_concurrency = QLineEdit("4")
        self.entry_concurrency.setFixedWidth(100)
        self.lbl_concurrency = self._row_label("Số trang OCR song song")
        form.addRow(self.lbl_concurrency, self.entry_concurrency)

        # KIE inference mode (used by Số hóa lưu trữ).
        # Keep the control for compatibility with saved settings/exported config.
        self.combo_kie_mode = FuzzyComboBox(sort=False)
        self.combo_kie_mode.addItems([
            "LayoutLMv3",
        ])
        self.combo_kie_mode.setFixedWidth(320)
        self.lbl_kie_mode = self._row_label("Chế độ KIE")
        form.addRow(self.lbl_kie_mode, self.combo_kie_mode)

        card.content_layout().addLayout(form)

        # Hint box
        hint_frame = QFrame()
        hint_frame.setProperty("cssClass", "hint-box")
        hint_layout = QHBoxLayout(hint_frame)
        hint_layout.setContentsMargins(SP[2], SP[1], SP[2], SP[1])

        self.lbl_desc = QLabel(translations.get_text("lbl_settings_desc"))
        self.lbl_desc.setWordWrap(True)
        self.lbl_desc.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px; background: transparent; border: none;")
        hint_layout.addWidget(self.lbl_desc)

        card.content_layout().addWidget(hint_frame)
        self._layout.addWidget(card)
        self._ocr_card = card

    def _build_correction_section(self):
        card = SectionCard(translations.get_text("sec_correction"))

        self.chk_correct_enabled = QCheckBox(translations.get_text("chk_correct_enabled"))
        self.chk_correct_enabled.setChecked(True)
        card.content_layout().addWidget(self.chk_correct_enabled)

        form = QFormLayout()
        form.setSpacing(SP[1])
        form.setContentsMargins(0, 0, 0, 0)

        # Correction model picker — fuzzy filter helps narrow the list
        # when the user has many local model dirs.
        self.combo_model = FuzzyComboBox()
        self.combo_model.setMinimumWidth(320)
        self.combo_model.currentTextChanged.connect(self.model_changed.emit)
        form.addRow(self._row_label(translations.get_text("lbl_correction_model")), self.combo_model)

        card.content_layout().addLayout(form)
        self._layout.addWidget(card)

    def _build_logging_section(self):
        card = SectionCard(translations.get_text("sec_logging"))

        self.chk_show_log_panel = QCheckBox(translations.get_text("chk_show_log_panel"))
        self.chk_show_log_panel.setChecked(True)
        self.chk_show_log_panel.toggled.connect(self.log_panel_toggled.emit)
        card.content_layout().addWidget(self.chk_show_log_panel)

        self.chk_verbose = QCheckBox(translations.get_text("chk_verbose_log"))
        self.chk_verbose.setChecked(True)
        card.content_layout().addWidget(self.chk_verbose)

        self._layout.addWidget(card)

    def _build_archive_data_section(self):
        card = SectionCard("Dữ liệu Kho lưu trữ")

        desc = QLabel(
            "Reset dữ liệu kho sẽ xóa toàn bộ hồ sơ, PDF, chỉ mục tìm kiếm và cơ sở dữ liệu trong Kho hiện tại."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px; background: transparent; border: none;"
        )
        card.content_layout().addWidget(desc)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_reset_archive = QPushButton("Reset dữ liệu kho")
        self.btn_reset_archive.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reset_archive.setStyleSheet(
            "QPushButton { background: #7f1d1d; color: white; "
            "border: none; border-radius: 4px; padding: 6px 12px; }"
            "QPushButton:hover { background: #991b1b; }"
        )
        self.btn_reset_archive.clicked.connect(self.reset_archive_requested.emit)
        row.addWidget(self.btn_reset_archive)
        card.content_layout().addLayout(row)

        self._layout.addWidget(card)

    def _build_catalog_section(self):
        card = SectionCard("Danh mục")

        desc = QLabel(
            "Chọn một danh mục ở bên trái, sau đó thêm hoặc xóa giá trị ở bên phải."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px; background: transparent; border: none;"
        )
        card.content_layout().addWidget(desc)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, SP[2], 0)
        left_layout.setSpacing(SP[1])
        left_layout.addWidget(self._row_label("Tên danh mục"))

        self.list_catalogs = QListWidget()
        self.list_catalogs.setMinimumWidth(180)
        self.list_catalogs.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list_catalogs.currentRowChanged.connect(self._on_catalog_row_changed)
        left_layout.addWidget(self.list_catalogs, 1)

        cat_row = QHBoxLayout()
        self.entry_catalog_name = QLineEdit()
        self.entry_catalog_name.setPlaceholderText("Danh mục mới")
        cat_row.addWidget(self.entry_catalog_name, 1)
        btn_add_catalog = QPushButton("Thêm")
        btn_add_catalog.clicked.connect(self._add_catalog)
        cat_row.addWidget(btn_add_catalog)
        btn_remove_catalog = QPushButton("Xóa")
        btn_remove_catalog.clicked.connect(self._remove_catalog)
        cat_row.addWidget(btn_remove_catalog)
        left_layout.addLayout(cat_row)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(SP[2], 0, 0, 0)
        right_layout.setSpacing(SP[1])

        self.lbl_catalog_values = self._row_label("Giá trị")
        right_layout.addWidget(self.lbl_catalog_values)

        self.list_catalog_values = QListWidget()
        self.list_catalog_values.setMinimumHeight(260)
        self.list_catalog_values.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        right_layout.addWidget(self.list_catalog_values, 1)

        value_row = QHBoxLayout()
        self.entry_catalog_value = QLineEdit()
        self.entry_catalog_value.setPlaceholderText("Giá trị mới")
        value_row.addWidget(self.entry_catalog_value, 1)
        btn_add_value = QPushButton("Thêm")
        btn_add_value.clicked.connect(self._add_catalog_value)
        value_row.addWidget(btn_add_value)
        btn_remove_value = QPushButton("Xóa")
        btn_remove_value.clicked.connect(self._remove_catalog_value)
        value_row.addWidget(btn_remove_value)
        btn_reset_values = QPushButton("Khôi phục mặc định")
        btn_reset_values.clicked.connect(self._reset_current_catalog_to_default)
        value_row.addWidget(btn_reset_values)
        right_layout.addLayout(value_row)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 620])
        card.content_layout().addWidget(splitter, 1)

        self._layout.addWidget(card)
        try:
            from scanindex.core.digitization.doctype import all_display_names
            self.set_doc_type_choices(all_display_names())
        except Exception:
            self.set_doc_type_choices([])

    def _build_save_button(self):
        row = QHBoxLayout()
        row.addStretch()
        self.btn_save = QPushButton(translations.get_text("btn_save_settings"))
        self.btn_save.setProperty("cssClass", "primary")
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save.clicked.connect(self.save_clicked.emit)
        row.addWidget(self.btn_save)
        row.addStretch()
        self._outer_layout.addLayout(row)

    def _catalog_sort_key(self, text: str) -> str:
        try:
            from scanindex.core.digitization.doctype import _norm
            return _norm(text)
        except Exception:
            return str(text or "").casefold()

    def _normalize_catalog_values(self, key: str, values: list[str]) -> list[str]:
        if key == "document_types":
            try:
                from scanindex.core.digitization.doctype import normalize_display_names
                return normalize_display_names(values)
            except Exception:
                pass
        out = []
        seen = set()
        for raw in values or []:
            text = " ".join(str(raw or "").split()).strip()
            if not text:
                continue
            norm = self._catalog_sort_key(text)
            if norm in seen:
                continue
            seen.add(norm)
            out.append(text)
        out.sort(key=self._catalog_sort_key)
        return out

    def _catalog_key_from_name(self, name: str) -> str:
        base = self._catalog_sort_key(name)
        slug = "".join(ch if ch.isalnum() else "_" for ch in base).strip("_")
        slug = slug or "catalog"
        key = slug
        n = 2
        while key in self._catalogs:
            key = f"{slug}_{n}"
            n += 1
        return key

    def _ensure_catalogs(self):
        if "document_types" not in self._catalogs:
            try:
                from scanindex.core.digitization.doctype import default_display_names
                values = default_display_names()
            except Exception:
                values = []
            self._catalogs["document_types"] = {
                "label": "Thể loại văn bản",
                "values": values,
                "system": True,
            }
        if not self._current_catalog_key:
            self._current_catalog_key = "document_types"

    def _refresh_catalog_list(self, select_key: str | None = None):
        self._ensure_catalogs()
        select_key = select_key or self._current_catalog_key or "document_types"
        self.list_catalogs.blockSignals(True)
        self.list_catalogs.clear()
        rows = sorted(
            self._catalogs.items(),
            key=lambda item: self._catalog_sort_key(item[1].get("label", item[0])),
        )
        selected_row = 0
        for idx, (key, data) in enumerate(rows):
            item = QListWidgetItem(data.get("label", key))
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.list_catalogs.addItem(item)
            if key == select_key:
                selected_row = idx
        self.list_catalogs.setCurrentRow(selected_row)
        self.list_catalogs.blockSignals(False)
        self._on_catalog_row_changed(selected_row)

    def _selected_catalog_key(self) -> str:
        item = self.list_catalogs.currentItem()
        if item is None:
            return self._current_catalog_key or "document_types"
        return item.data(Qt.ItemDataRole.UserRole) or "document_types"

    def _on_catalog_row_changed(self, row: int):
        if row < 0:
            return
        key = self._selected_catalog_key()
        self._current_catalog_key = key
        data = self._catalogs.get(key, {})
        label = data.get("label", key)
        self.lbl_catalog_values.setText(f"Giá trị của: {label}")
        self._set_catalog_value_items(data.get("values", []))

    def _set_catalog_value_items(self, values: list[str]) -> None:
        self.list_catalog_values.clear()
        for value in values:
            self.list_catalog_values.addItem(value)

    def _add_catalog(self):
        name = " ".join(self.entry_catalog_name.text().split()).strip()
        if not name:
            return
        key = self._catalog_key_from_name(name)
        self._catalogs[key] = {"label": name, "values": [], "system": False}
        self.entry_catalog_name.clear()
        self._refresh_catalog_list(select_key=key)

    def _remove_catalog(self):
        key = self._selected_catalog_key()
        data = self._catalogs.get(key)
        if not data or data.get("system"):
            return
        self._catalogs.pop(key, None)
        self._refresh_catalog_list(select_key="document_types")

    def _add_catalog_value(self):
        key = self._selected_catalog_key()
        text = " ".join(self.entry_catalog_value.text().split()).strip()
        if not text:
            return
        data = self._catalogs.setdefault(
            key, {"label": key, "values": [], "system": False}
        )
        data["values"] = self._normalize_catalog_values(
            key, list(data.get("values", [])) + [text]
        )
        self.entry_catalog_value.clear()
        self._set_catalog_value_items(data["values"])

    def _remove_catalog_value(self):
        key = self._selected_catalog_key()
        row = self.list_catalog_values.currentRow()
        if row < 0:
            return
        item = self.list_catalog_values.item(row)
        if key == "document_types" and item and item.text().strip() == "Khác":
            return
        data = self._catalogs.get(key)
        if not data:
            return
        values = list(data.get("values", []))
        if 0 <= row < len(values):
            values.pop(row)
        data["values"] = self._normalize_catalog_values(key, values)
        self._set_catalog_value_items(data["values"])

    def _reset_current_catalog_to_default(self):
        key = self._selected_catalog_key()
        data = self._catalogs.get(key)
        if not data:
            return
        if key == "document_types":
            try:
                from scanindex.core.digitization.doctype import default_display_names
                data["values"] = default_display_names()
            except Exception:
                data["values"] = []
        else:
            data["values"] = []
        self._set_catalog_value_items(data["values"])

    def _catalog_payload(self) -> dict:
        self._ensure_catalogs()
        payload = {}
        for key, data in self._catalogs.items():
            payload[key] = {
                "label": data.get("label", key),
                "values": self._normalize_catalog_values(
                    key, list(data.get("values", []))
                ),
                "system": bool(data.get("system")),
            }
        return payload

    def set_catalogs(self, catalogs: dict | None):
        self._catalogs = {}
        if isinstance(catalogs, dict):
            for key, data in catalogs.items():
                if not isinstance(data, dict):
                    continue
                label = " ".join(str(data.get("label") or key).split()).strip()
                values = data.get("values", [])
                self._catalogs[str(key)] = {
                    "label": label or str(key),
                    "values": self._normalize_catalog_values(str(key), list(values or [])),
                    "system": bool(data.get("system")) or str(key) == "document_types",
                }
        self._ensure_catalogs()
        self._refresh_catalog_list(select_key=self._current_catalog_key or "document_types")

    def _doc_type_items(self) -> list[str]:
        self._ensure_catalogs()
        return list(self._catalogs["document_types"].get("values", []))

    def _set_doc_type_items(self, items: list[str]) -> None:
        self._ensure_catalogs()
        self._catalogs["document_types"]["values"] = self._normalize_catalog_values(
            "document_types", items
        )
        self._refresh_catalog_list(select_key="document_types")

    def _on_lang_change(self, text):
        code = "vi" if text == "Tiếng Việt" else "en"
        if code != self._current_language:
            self._current_language = code
            self.language_changed.emit(code)

    # --- Getters / Setters ---

    def set_values(self, wait_page: str, compare_int: str, concurrency: str,
                   export_workers: str, model: str, verbose: bool,
                   correct: bool = True, export: bool = True,
                   show_log_panel: bool = True,
                   kie_mode: str = "layoutlmv3",
                   doc_types: list[str] | None = None,
                   catalogs: dict | None = None):
        self._wait_page_value = wait_page
        self._compare_value = compare_int
        self.entry_concurrency.setText(concurrency)
        self.combo_model.blockSignals(True)
        if model and self.combo_model.findText(model) < 0:
            self.combo_model.addItem(model)
        if model:
            self.combo_model.setCurrentText(model)
        self.combo_model.blockSignals(False)
        self.chk_correct_enabled.setChecked(bool(correct))
        self.chk_verbose.setChecked(verbose)
        self.chk_show_log_panel.setChecked(show_log_panel)
        # Map config key → display index
        if kie_mode not in self._kie_mode_values:
            kie_mode = "layoutlmv3"
        self.combo_kie_mode.setCurrentIndex(self._kie_mode_values.index(kie_mode))
        if catalogs is not None:
            self.set_catalogs(catalogs)
        elif doc_types is not None:
            self.set_doc_type_choices(doc_types)

    def get_values(self) -> dict:
        kie_idx = self.combo_kie_mode.currentIndex()
        if 0 <= kie_idx < len(self._kie_mode_values):
            kie_mode = self._kie_mode_values[kie_idx]
        else:
            kie_mode = "layoutlmv3"
        return {
            "wait_page": self._wait_page_value,
            "compare_int": self._compare_value,
            "concurrency": self.entry_concurrency.text(),
            "export_workers": "1",
            "model": self.combo_model.currentText(),
            "correct": self.chk_correct_enabled.isChecked(),
            "verbose": self.chk_verbose.isChecked(),
            "show_log_panel": self.chk_show_log_panel.isChecked(),
            "kie_mode": kie_mode,
            "doc_types": self._doc_type_items(),
            "catalogs": self._catalog_payload(),
        }

    def set_model_choices(self, models: list):
        self.combo_model.blockSignals(True)
        self.combo_model.clear()
        self.combo_model.addItems(models)
        self.combo_model.blockSignals(False)

    def set_doc_type_choices(self, doc_types: list[str]):
        self._set_doc_type_items(doc_types)

    def update_texts(self):
        self.btn_save.setText(translations.get_text("btn_save_settings"))
        self.chk_correct_enabled.setText(translations.get_text("chk_correct_enabled"))
        self.chk_show_log_panel.setText(translations.get_text("chk_show_log_panel"))
        self.chk_verbose.setText(translations.get_text("chk_verbose_log"))
        self.lbl_desc.setText(translations.get_text("lbl_settings_desc"))
        self.lbl_concurrency.setText(translations.get_text("lbl_concurrency_ocr"))
