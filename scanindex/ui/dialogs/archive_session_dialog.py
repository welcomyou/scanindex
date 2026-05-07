"""DossierInfoDialog — collect dossier identity info (Bước 1 / Kho edit).

Two modes the user can pick via the "Không lưu theo cấu trúc này" checkbox:

* **Structured** (default): the 4 archive codes (Mã định danh, Mã phông,
  Mục lục, Hồ sơ) are user-filled. Output filenames follow the standard
  ``<Mã định danh>-<Mã phông>-<Mục lục>-<Hồ sơ>-<STT>.pdf`` convention.

* **Unstructured**: user only types a free-text "Tên hồ sơ"; the 4 codes
  are auto-generated deterministically from a session-scoped seed so the
  dossier still has a stable composite key in Kho.

Width-limited fields:
  - Mục lục: 2 chars (digits typical but not enforced)
  - Hồ sơ:   5 chars (digits + optional letter — letter NOT required)
  - Ten phong / Ten muc luc / Title: 1000 chars hard-cap
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QTextEdit,
    QPushButton, QLabel, QFrame, QCheckBox, QComboBox, QScrollArea, QWidget,
)

from scanindex.ui.widgets.fuzzy_combobox import FuzzyComboBox

from scanindex.ui.theme import (
    COLOR_BG, COLOR_SURFACE, COLOR_BORDER, COLOR_INPUT,
    COLOR_TEXT, COLOR_TEXT_SECONDARY, COLOR_TEXT_MUTED,
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_RED,
    SP, FONT_UI,
)
from scanindex.core.digitization.session import IdentityCodes
from scanindex.infra import translations


_TITLE_MAX = 1000
_TERM_MAX = 10

# Dropdown options for the new dossier-level fields.
_RETENTION_OPTIONS = [
    "Vĩnh viễn", "1 năm", "5 năm", "10 năm", "20 năm",
    "30 năm", "50 năm", "60 năm", "70 năm",
]
_PHYSICAL_STATE_OPTIONS = ["Tốt", "Bình thường", "Hỏng"]
_DEFAULT_RETENTION = "Vĩnh viễn"
_DEFAULT_PHYSICAL_STATE = "Bình thường"


class DossierInfoDialog(QDialog):
    """Dialog gathering dossier metadata: 4 codes + optional title, OR
    unstructured (title-only with auto-generated codes)."""

    def __init__(self, initial: IdentityCodes | None = None,
                 seed_for_unstructured: str = "default",
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(translations.get_text("arc_session_dialog_title"))
        self.setModal(True)
        self.setMinimumWidth(520)
        self.resize(760, 640)
        self.setStyleSheet(f"QDialog {{ background: {COLOR_BG}; }}")

        # Seed used to derive the 4 placeholder codes when the user picks
        # unstructured mode. Caller passes session_id so the same dossier
        # stays stable across re-opens within the same session.
        self._seed = seed_for_unstructured
        self._result: IdentityCodes | None = None

        self._setup_ui()
        if initial is not None:
            self._load_initial(initial)
        self._on_unstructured_toggled(self._cb_unstructured.isChecked())

    # ── UI build ────────────────────────────────────────────────────

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP[3], SP[3], SP[3], SP[2])
        outer.setSpacing(SP[2])

        title = QLabel(translations.get_text("arc_session_dialog_heading"))
        title.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {COLOR_TEXT}; "
            f"font-family: {FONT_UI};")
        outer.addWidget(title)

        hint = QLabel(translations.get_text("arc_session_dialog_hint"))
        hint.setStyleSheet(
            f"font-size: 11px; color: {COLOR_TEXT_MUTED}; font-family: {FONT_UI};")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {COLOR_BORDER};")
        outer.addWidget(sep)

        # Unstructured toggle
        self._cb_unstructured = QCheckBox(
            translations.get_text("arc_unstructured_label"))
        self._cb_unstructured.setStyleSheet(
            f"QCheckBox {{ color: {COLOR_TEXT_SECONDARY}; "
            f"font: 12px '{FONT_UI}'; }}")
        self._cb_unstructured.toggled.connect(self._on_unstructured_toggled)
        outer.addWidget(self._cb_unstructured)
        cb_hint = QLabel(translations.get_text("arc_unstructured_hint"))
        cb_hint.setStyleSheet(
            f"font-size: 11px; color: {COLOR_TEXT_MUTED}; "
            f"font-family: {FONT_UI};")
        cb_hint.setWordWrap(True)
        outer.addWidget(cb_hint)

        # Form
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setSpacing(SP[1])
        form.setVerticalSpacing(SP[1])

        self._ed_ma_dd = self._mk_input(
            placeholder=translations.get_text("arc_ph_ma_dd"))
        self._ed_ma_phong = self._mk_input(
            placeholder=translations.get_text("arc_ph_ma_phong"))
        self._ed_ten_phong = self._mk_input(
            placeholder="Tên phông (tối đa 1000 ký tự) - đảm bảo giống hệ thống kho lưu trữ",
            max_len=_TITLE_MAX,
        )
        self._ed_muc_luc = self._mk_input(
            placeholder=translations.get_text("arc_ph_muc_luc"), max_len=2)
        self._ed_ten_muc_luc = self._mk_input(
            placeholder="Tên mục lục (tối đa 1000 ký tự) - đảm bảo giống hệ thống kho lưu trữ",
            max_len=_TITLE_MAX,
        )
        self._ed_ho_so = self._mk_input(
            placeholder=translations.get_text("arc_ph_ho_so"), max_len=5)
        self._ed_title = self._mk_textarea(
            placeholder=translations.get_text("arc_ph_title"))

        # Three HSLTCQ Hồ-sơ-sheet fields the dialog now collects.
        self._cb_retention = self._mk_combobox(_RETENTION_OPTIONS, allow_blank=True)
        self._cb_physical = self._mk_combobox(_PHYSICAL_STATE_OPTIONS, allow_blank=True)
        self._cb_retention.setCurrentText(_DEFAULT_RETENTION)
        self._cb_physical.setCurrentText(_DEFAULT_PHYSICAL_STATE)
        self._ed_term = self._mk_input(placeholder="VD: 2021–2026")
        self._ed_term.setToolTip(f"Nhiệm kỳ tối đa {_TERM_MAX} ký tự.")
        self._term_hint = QLabel(f"Nhiệm kỳ tối đa {_TERM_MAX} ký tự.")
        self._term_hint.setStyleSheet(
            f"font-size: 10px; color: {COLOR_RED}; font-family: {FONT_UI};"
        )
        self._term_hint.setVisible(False)
        term_box = QWidget()
        term_layout = QVBoxLayout(term_box)
        term_layout.setContentsMargins(0, 0, 0, 0)
        term_layout.setSpacing(2)
        term_layout.addWidget(self._ed_term)
        term_layout.addWidget(self._term_hint)

        # Free-text 1000-char fields stored in IdentityCodes only (not in
        # HSLTCQ 13-col Hồ sơ schema).
        self._ed_topic = self._mk_textarea(placeholder="Chuyên đề (tùy chọn)")
        self._ed_note = self._mk_textarea(placeholder="Chú thích (tùy chọn)")

        form.addRow(self._mk_label(translations.get_text("arc_field_ma_dd")), self._ed_ma_dd)
        form.addRow(self._mk_label(translations.get_text("arc_field_ma_phong")), self._ed_ma_phong)
        form.addRow(self._mk_label("Tên phông"), self._ed_ten_phong)
        form.addRow(self._mk_label(translations.get_text("arc_field_muc_luc")), self._ed_muc_luc)
        form.addRow(self._mk_label("Tên mục lục"), self._ed_ten_muc_luc)
        form.addRow(self._mk_label(translations.get_text("arc_field_ho_so")), self._ed_ho_so)
        form.addRow(self._mk_label(translations.get_text("arc_field_title")), self._ed_title)
        form.addRow(self._mk_label("Thời hạn bảo quản"), self._cb_retention)
        form.addRow(self._mk_label("Tình trạng vật lý"), self._cb_physical)
        form.addRow(self._mk_label("Nhiệm kỳ"), term_box)
        form.addRow(self._mk_label("Chuyên đề"), self._ed_topic)
        form.addRow(self._mk_label("Chú thích"), self._ed_note)

        # Wrap the form in a scroll area so the dialog stays compact even
        # with the extra fields.
        form_host = QWidget()
        form_host.setLayout(form)
        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_scroll.setWidget(form_host)
        form_scroll.setMinimumHeight(300)
        outer.addWidget(form_scroll, 1)

        self._ed_title.textChanged.connect(self._enforce_title_cap)
        self._ed_term.textChanged.connect(self._on_term_changed)

        # Hard-cap topic + note at 1000 chars too.
        self._ed_topic.textChanged.connect(
            lambda w=self._ed_topic: self._enforce_textarea_cap(w)
        )
        self._ed_note.textChanged.connect(
            lambda w=self._ed_note: self._enforce_textarea_cap(w)
        )

        # Error label
        self._err = QLabel("")
        self._err.setStyleSheet(
            f"font-size: 11px; color: {COLOR_RED}; font-family: {FONT_UI};")
        self._err.setWordWrap(True)
        outer.addWidget(self._err)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(SP[1])
        btn_row.addStretch()
        btn_cancel = QPushButton(translations.get_text("btn_cancel"))
        btn_cancel.setStyleSheet(self._secondary_btn_style())
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        btn_ok = QPushButton(translations.get_text("btn_ok"))
        btn_ok.setStyleSheet(self._primary_btn_style())
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self._on_ok)
        btn_row.addWidget(btn_ok)
        outer.addLayout(btn_row)

    # ── widget factories ────────────────────────────────────────────

    def _mk_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: 12px; font-family: {FONT_UI};")
        return lbl

    def _mk_input(self, placeholder: str = "", max_len: int = 0) -> QLineEdit:
        w = QLineEdit()
        if placeholder:
            w.setPlaceholderText(placeholder)
        if max_len > 0:
            w.setMaxLength(max_len)
        w.setFixedHeight(32)
        w.setStyleSheet(self._input_qss())
        return w

    def _mk_textarea(self, placeholder: str = "") -> QTextEdit:
        w = QTextEdit()
        if placeholder:
            w.setPlaceholderText(placeholder)
        w.setTabChangesFocus(True)
        w.setFixedHeight(52)
        w.setStyleSheet(self._input_qss(textarea=True))
        return w

    def _mk_combobox(self, options: list[str], allow_blank: bool = True) -> QComboBox:
        # FuzzyComboBox handles wheel-ignore + diacritic-insensitive
        # fuzzy filter + A-Z sort + the global theme's chevron. Local
        # stylesheets caused inconsistent dropdown styling, so leave it
        # bare here.
        w = FuzzyComboBox()
        if allow_blank:
            w.addItem("")               # blank = field unset (kept first)
        w.addItems(options)
        w.setFixedHeight(32)
        return w

    def _enforce_textarea_cap(self, widget: QTextEdit) -> None:
        text = widget.toPlainText()
        if len(text) > _TITLE_MAX:
            cur = widget.textCursor()
            widget.blockSignals(True)
            widget.setPlainText(text[:_TITLE_MAX])
            cur.setPosition(_TITLE_MAX)
            widget.setTextCursor(cur)
            widget.blockSignals(False)

    def _input_qss(self, *, textarea: bool = False, invalid: bool = False) -> str:
        widget = "QTextEdit" if textarea else "QLineEdit"
        border = COLOR_RED if invalid else COLOR_BORDER
        return f"""
            {widget} {{
                background: {COLOR_INPUT};
                border: 1px solid {border};
                border-radius: 4px;
                color: {COLOR_TEXT};
                font-size: 12px;
                font-family: {FONT_UI};
                padding: 3px 8px;
            }}
            {widget}:focus {{ border-color: {COLOR_ACCENT}; }}
            {widget}:disabled {{
                background: {COLOR_SURFACE};
                color: {COLOR_TEXT_MUTED};
            }}
        """

    def _primary_btn_style(self) -> str:
        return f"""
            QPushButton {{
                background: {COLOR_ACCENT}; border: none; border-radius: 4px;
                color: white; font-size: 12px; font-family: {FONT_UI};
                font-weight: 600; padding: 4px 16px; min-height: 20px;
                max-height: 30px;
            }}
            QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}
        """

    def _secondary_btn_style(self) -> str:
        return f"""
            QPushButton {{
                background: transparent; border: 1px solid {COLOR_BORDER};
                border-radius: 4px; color: {COLOR_TEXT_SECONDARY};
                font-size: 12px; font-family: {FONT_UI};
                padding: 4px 14px; min-height: 20px;
                max-height: 30px;
            }}
            QPushButton:hover {{ background: {COLOR_SURFACE}; color: {COLOR_TEXT}; }}
        """

    # ── state helpers ───────────────────────────────────────────────

    def _load_initial(self, initial: IdentityCodes) -> None:
        self._cb_unstructured.setChecked(bool(initial.is_unstructured))
        self._ed_ma_dd.setText(initial.ma_dinh_danh)
        self._ed_ma_phong.setText(initial.ma_phong)
        self._ed_ten_phong.setText(getattr(initial, "ten_phong", "") or "")
        self._ed_muc_luc.setText(initial.muc_luc)
        self._ed_ten_muc_luc.setText(getattr(initial, "ten_muc_luc", "") or "")
        self._ed_ho_so.setText(initial.ho_so)
        self._ed_title.setPlainText(initial.title)
        # Combobox widgets fall through to the blank entry when the stored
        # value isn't in the option list.
        ret = (initial.thoi_han_bao_quan or "").strip() or _DEFAULT_RETENTION
        idx = self._cb_retention.findText(ret)
        self._cb_retention.setCurrentIndex(max(0, idx))
        ph = (initial.tinh_trang_vat_ly or "").strip() or _DEFAULT_PHYSICAL_STATE
        idx2 = self._cb_physical.findText(ph)
        self._cb_physical.setCurrentIndex(max(0, idx2))
        self._ed_term.setText(initial.nhiem_ky or "")
        self._ed_topic.setPlainText(initial.chuyen_de or "")
        self._ed_note.setPlainText(initial.chu_thich or "")

    def _on_unstructured_toggled(self, checked: bool) -> None:
        # Disable the 4 codes when unstructured; the title becomes the
        # primary input. Codes stay readable (so user sees the auto-gen
        # values) but not editable.
        for w in (self._ed_ma_dd, self._ed_ma_phong,
                  self._ed_ten_phong, self._ed_muc_luc,
                  self._ed_ten_muc_luc, self._ed_ho_so):
            w.setEnabled(not checked)
        if checked:
            preview = IdentityCodes.auto_unstructured(
                title=self._ed_title.toPlainText().strip() or "(chưa đặt tên)",
                seed=self._seed,
            )
            self._ed_ma_dd.setText(preview.ma_dinh_danh)
            self._ed_ma_phong.setText(preview.ma_phong)
            self._ed_ten_phong.clear()
            self._ed_muc_luc.setText(preview.muc_luc)
            self._ed_ten_muc_luc.clear()
            self._ed_ho_so.setText(preview.ho_so)
        self._ed_title.setFocus()

    def _enforce_title_cap(self) -> None:
        text = self._ed_title.toPlainText()
        if len(text) > _TITLE_MAX:
            cur = self._ed_title.textCursor()
            self._ed_title.blockSignals(True)
            self._ed_title.setPlainText(text[:_TITLE_MAX])
            cur.setPosition(_TITLE_MAX)
            self._ed_title.setTextCursor(cur)
            self._ed_title.blockSignals(False)

    def _on_term_changed(self, text: str) -> None:
        too_long = len((text or "").strip()) > _TERM_MAX
        self._term_hint.setVisible(too_long)
        self._ed_term.setStyleSheet(self._input_qss(invalid=too_long))

    # ── validation ──────────────────────────────────────────────────

    def _on_ok(self):
        unstructured = self._cb_unstructured.isChecked()
        title = self._ed_title.toPlainText().strip()
        retention = self._cb_retention.currentText().strip()
        physical = self._cb_physical.currentText().strip()
        term = self._ed_term.text().strip()
        topic = self._ed_topic.toPlainText().strip()[:_TITLE_MAX]
        note = self._ed_note.toPlainText().strip()[:_TITLE_MAX]
        errs = []
        if len(term) > _TERM_MAX:
            errs.append(f"Nhiệm kỳ không được vượt quá {_TERM_MAX} ký tự")
        if unstructured:
            if not title:
                errs.append(translations.get_text("arc_err_title_required"))
            if errs:
                self._err.setText(" • ".join(errs))
                return
            self._result = IdentityCodes.auto_unstructured(
                title=title, seed=self._seed,
            )
            self._result.thoi_han_bao_quan = retention
            self._result.tinh_trang_vat_ly = physical
            self._result.nhiem_ky = term
            self._result.chuyen_de = topic
            self._result.chu_thich = note
            self.accept()
            return

        ma_dd = self._ed_ma_dd.text().strip()
        ma_phong = self._ed_ma_phong.text().strip()
        ten_phong = self._ed_ten_phong.text().strip()[:_TITLE_MAX]
        muc_luc = self._ed_muc_luc.text().strip()
        ten_muc_luc = self._ed_ten_muc_luc.text().strip()[:_TITLE_MAX]
        ho_so = self._ed_ho_so.text().strip()

        if not ma_dd:
            errs.append(translations.get_text("arc_err_ma_dd_empty"))
        if not ma_phong:
            errs.append(translations.get_text("arc_err_ma_phong_empty"))
        if not muc_luc or len(muc_luc) > 2:
            errs.append(translations.get_text("arc_err_muc_luc_format"))
        if not ho_so or len(ho_so) > 5:
            errs.append(translations.get_text("arc_err_ho_so_format"))

        if errs:
            self._err.setText(" • ".join(errs))
            return

        self._result = IdentityCodes(
            ma_dinh_danh=ma_dd, ma_phong=ma_phong,
            muc_luc=muc_luc, ho_so=ho_so,
            ten_phong=ten_phong,
            ten_muc_luc=ten_muc_luc,
            title=title, is_unstructured=False,
            thoi_han_bao_quan=retention,
            tinh_trang_vat_ly=physical,
            nhiem_ky=term,
            chuyen_de=topic,
            chu_thich=note,
        )
        self.accept()

    def result_codes(self) -> IdentityCodes | None:
        return self._result


# Legacy export name so existing callers still resolve.
ArchiveSessionDialog = DossierInfoDialog
