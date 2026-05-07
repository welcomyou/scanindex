"""ArchiveStep2Kie — Bước 2: trích xuất KIE.

Source modes:
  - "step1"  : segments handed off from Step 1, all rows start in spinner
               state (OCR + KIE running) and only become clickable once the
               full pipeline completes.
  - "folder" : user picks an input folder. Filenames matching the canonical
               pattern <MãĐD>-<MãPhông>-<MụcLục>-<HồSơ>-<STT>.pdf are normal,
               others get a soft warning marker.

The body layout (file list left | viewer center | metadata panel right) is
ported from the original `ArchiveTab` essentially unchanged."""
import os
import json
import re
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QComboBox,
    QLabel, QLineEdit, QTextEdit, QPushButton,
    QListWidget, QListWidgetItem, QScrollArea, QFrame,
    QMessageBox, QSizePolicy,
)
from PySide6.QtCore import Qt, QTimer, Signal, QDate
from PySide6.QtGui import QBrush, QColor, QTextOption

from scanindex.ui.widgets.fuzzy_combobox import FuzzyComboBox

from scanindex.ui.theme import (
    COLOR_BG, COLOR_SURFACE, COLOR_ELEVATED, COLOR_HOVER,
    COLOR_BORDER, COLOR_BORDER_DEFAULT, COLOR_INPUT,
    COLOR_TEXT, COLOR_TEXT_SECONDARY, COLOR_TEXT_MUTED,
    COLOR_ACCENT, COLOR_ACCENT_HOVER,
    COLOR_GREEN, COLOR_GREEN_HOVER, COLOR_RED, COLOR_RED_HOVER,
    SP, RADIUS_MD, RADIUS_SM, FONT_UI,
)
from scanindex.ui.widgets.kie_archive_viewer import KieArchiveViewer
from scanindex.infra import translations


# ---------- Design tokens ----------
_H = 26
_FONT = 12
_FONT_SM = 11
_RAD = 4
_META_W = 280
_FLIST_W = 220


_INPUT_FOCUS = f"border-color: {COLOR_ACCENT};"

_TEXTAREA = f"""
    background: {COLOR_INPUT};
    border: 1px solid {COLOR_BORDER};
    border-radius: {_RAD}px;
    color: {COLOR_TEXT};
    font-size: {_FONT}px;
    font-family: {FONT_UI};
    padding: 4px 6px;
    selection-background-color: {COLOR_ACCENT};
"""


# Canonical name pattern — see CLAUDE.md / Step 1 naming rules.
# Example: H42-001-01-0123a-001.pdf
NAME_PATTERN = re.compile(r"^[\w]+-[\w]+-\d{2}-\d{4}[a-zA-Z]?-\d{3}\.pdf$",
                          re.IGNORECASE)


# Section 1 form: derived/projected metadata the user sees and edits.
_FIELDS = [
    ("co_quan_ban_hanh", "arc_field_co_quan",   True),
    ("loai_van_ban",     "arc_field_loai_vb",   True),
    ("so_van_ban",       "arc_field_so",        True),
    ("ky_hieu",          "arc_field_ky_hieu",   True),
    ("ngay_ban_hanh",    "arc_field_ngay",      False),
    ("trich_yeu",        "arc_field_trich_yeu", True),
    ("ngon_ngu",         "arc_field_ngon_ngu",  True),
    ("nguoi_ky",         "arc_field_nguoi_ky",  True),
    ("do_mat",           "arc_field_do_mat",    False),
]


# Form key → KIE label used for: bbox highlight on label click, field-active
# sync, fuzzy-match scope.
_FORM_TO_KIE_LABEL = {
    "co_quan_ban_hanh": "ISSUE_ORG_NAME",
    "loai_van_ban":     "DOC_TYPE",
    "so_van_ban":       "DOC_NUMBER_SYMBOL",
    "ky_hieu":          "DOC_NUMBER_SYMBOL",
    "ngay_ban_hanh":    "PLACE_DATE",
    "trich_yeu":        "DOC_SUBJECT",
    "nguoi_ky":         "SIGNER_NAME",
    "do_mat":           "SECRECY_MARK",
}


# Section 2 panel — fixed list of all 14 raw KIE labels, in the canonical
# display order. Display name for the row label; PDF colour pulled from
# `LABEL_COLORS` and the on-PDF badge number from `FIELD_NUMBER_MAP` (where
# present — the 4 mark-style labels at the end have no badge).
_RAW_KIE_LABELS = [
    ("REGIME_HEADER",      "Tiêu ngữ"),
    ("ISSUE_ORG_SUPERIOR", "Cơ quan cấp trên"),
    ("ISSUE_ORG_NAME",     "Cơ quan ban hành"),
    ("DOC_NUMBER_SYMBOL",  "Số - Ký hiệu"),
    ("PLACE_DATE",         "Địa điểm, ngày tháng"),
    ("DOC_SUBJECT",        "Trích yếu"),
    ("ADDRESSEE",          "Người nhận"),
    ("RECIPIENTS",         "Nơi nhận"),
    ("SIGNER_ROLE",        "Chức vụ người ký"),
    ("SIGNER_NAME",        "Người ký"),
    ("URGENCY_MARK",       "Mức độ khẩn"),
    ("SECRECY_MARK",       "Độ mật"),
    ("CIRCULATION_MARK",   "Chế độ sử dụng"),
    ("DOC_TYPE",           "Loại văn bản"),
]


# UI option order for the Section 1 "Độ mật" dropdown — Vietnamese display
# name first (matches HSLTCQ), and the same strings flow through to the
# canonical Văn bản sheet column "Độ mật".
_DO_MAT_OPTIONS = ["Thường", "Mật", "Tối mật", "Tuyệt mật"]

_DATE_EMPTY = QDate(1900, 1, 1)
_DATE_RE = re.compile(r"\b(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2,4})\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")

# Strict whole-string match for the user-facing date input. Loose enough
# to accept "14/5/26" while still flagging garbage like "ngày mai".
_DATE_INPUT_RE = re.compile(r"^\s*(\d{1,2})[\s/.\-](\d{1,2})[\s/.\-](\d{2,4})\s*$")
# Accept compact 8-digit (DDMMYYYY) and 6-digit (DDMMYY) input — typing
# "12121988" should normalise to "12/12/1988".
_DATE_INPUT_DIGITS8_RE = re.compile(r"^\s*(\d{2})(\d{2})(\d{4})\s*$")
_DATE_INPUT_DIGITS6_RE = re.compile(r"^\s*(\d{2})(\d{2})(\d{2})\s*$")
# "Số của văn bản" allows digits with an optional single trailing letter
# (e.g. "245a"). Anything else is treated as junk and won't propagate
# to the workbook.
_NUMBER_INPUT_RE = re.compile(r"^\s*\d+[A-Za-z]?\s*$")


def _normalize_date_input(text: str) -> str:
    """Return ``text`` normalised to "DD/MM/YYYY" or empty when it isn't
    a valid date. Two-digit years use a 50-year split (≥50 → 19xx, <50
    → 20xx) so OCR'd "14/05/26" lands in 2026 rather than year 26.
    Compact "12121988" or "121288" (no separators) parse the same way."""
    text = (text or "").strip()
    if not text:
        return ""
    for re_ in (_DATE_INPUT_RE, _DATE_INPUT_DIGITS8_RE, _DATE_INPUT_DIGITS6_RE):
        m = re_.match(text)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            break
    else:
        return ""
    if y < 100:
        y = 2000 + y if y < 50 else 1900 + y
    qd = QDate(y, mo, d)
    if not qd.isValid():
        return ""
    return qd.toString("dd/MM/yyyy")

# Subject + org normalization helpers — shared with archive_store.importer
# so Kho columns get the same cleaned text Step 2 displays.
from scanindex.core.kie.text_normalize import (  # noqa: E402
    normalize_subject_type_prefix as _normalize_subject_type_prefix,
    single_line_text as _single_line_text,
)


def _load_annotation(json_path):
    if not json_path or not os.path.exists(json_path):
        return None
    try:
        import json
        with open(json_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        ann = doc.get("annotations") or None
        if ann and "field_instances" in ann:
            try:
                from scanindex.core.kie.postprocess import apply_layoutlmv3_schema_postprocess
                ann = apply_layoutlmv3_schema_postprocess(doc, ann)
            except Exception:
                pass
            return ann
    except Exception:
        return None
    return None


def _field_text(by_label: dict, label: str) -> str:
    field = by_label.get(label) or {}
    return str(field.get("text") or "").strip()


def _parse_qdate(value: str | None) -> QDate:
    text = str(value or "").strip()
    if not text:
        return QDate()
    m = _DATE_RE.search(text)
    if m:
        d, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year = 2000 + year if year < 50 else 1900 + year
        qd = QDate(year, month, d)
        return qd if qd.isValid() else QDate()
    m = _ISO_DATE_RE.search(text)
    if m:
        year, month, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        qd = QDate(year, month, d)
        return qd if qd.isValid() else QDate()
    try:
        from scanindex.core.digitization.metadata_export import _parse_date_from_place_date
        parsed = _parse_date_from_place_date(text)
        if parsed and parsed != text:
            return _parse_qdate(parsed)
    except Exception:
        pass
    return QDate()


def _annotation_to_metadata_form(annotation):
    """Project the canonical KIE annotation onto the Section 1 form
    (8 derived fields + Độ mật)."""
    fields = annotation.get("field_instances", []) or []
    by_label = {}
    for f in fields:
        by_label.setdefault(f.get("label", ""), f)

    meta = {}
    raw_subject = _field_text(by_label, "DOC_SUBJECT")
    raw_doc_number = _field_text(by_label, "DOC_NUMBER_SYMBOL")
    detected_doc_type = ""
    try:
        from scanindex.core.digitization.doctype import detect_doc_type
        detected_doc_type = detect_doc_type(raw_subject, raw_doc_number)
    except Exception:
        detected_doc_type = _field_text(by_label, "DOC_TYPE")
    if detected_doc_type:
        meta["loai_van_ban"] = detected_doc_type

    direct_map = {
        "DOC_SUBJECT":  "trich_yeu",
        "SIGNER_NAME":  "nguoi_ky",
    }
    for kie_label, form_key in direct_map.items():
        f = by_label.get(kie_label)
        if f and f.get("text"):
            meta[form_key] = f["text"].strip()

    # "Cơ quan ban hành" — issuing org first, then its superior as a
    # single-line value. The raw KIE panel keeps the original two fields;
    # the final form/Excel cell needs one readable string.
    org_name = _field_text(by_label, "ISSUE_ORG_NAME")
    org_superior = _field_text(by_label, "ISSUE_ORG_SUPERIOR")
    sup1 = _single_line_text(org_superior)
    name1 = _single_line_text(org_name)
    if name1 and sup1:
        meta["co_quan_ban_hanh"] = _single_line_text(f"{name1} {sup1}")
    elif name1 or sup1:
        meta["co_quan_ban_hanh"] = name1 or sup1

    # Subject normalisation: preserve the type prefix, only collapse spacing.
    if meta.get("trich_yeu"):
        meta["trich_yeu"] = _single_line_text(
            _normalize_subject_type_prefix(
                meta["trich_yeu"],
                detected_doc_type or _field_text(by_label, "DOC_TYPE"),
            )
        )

    # Split DOC_NUMBER_SYMBOL → "Số" + "Ký hiệu". Only commit the "Số"
    # portion when it parses as a number (digits + optional letter
    # suffix); anything else leaves the form field empty so the
    # red-border validator flags it for review.
    doc_num = by_label.get("DOC_NUMBER_SYMBOL")
    if doc_num and doc_num.get("text"):
        try:
            from scanindex.core.kie.ontology import split_doc_number_symbol_text
            num, sym = split_doc_number_symbol_text(doc_num["text"])
            if num and _NUMBER_INPUT_RE.match(num.strip()):
                meta["so_van_ban"] = num.strip()
            if sym:
                meta["ky_hieu"] = sym
        except Exception:
            pass

    # Parse PLACE_DATE → DD/MM/YYYY
    pd_field = by_label.get("PLACE_DATE")
    if pd_field and pd_field.get("text"):
        try:
            from scanindex.core.digitization.metadata_export import _parse_date_from_place_date
            dt = _parse_date_from_place_date(pd_field["text"])
            if dt:
                meta["ngay_ban_hanh"] = dt
        except Exception:
            pass

    # SECRECY_MARK → "Độ mật". The mark text from KIE is one of
    # {Mật, Tối mật, Tuyệt mật}; absence of the mark = "Thường".
    secrecy_field = by_label.get("SECRECY_MARK")
    secrecy_text = (secrecy_field or {}).get("text", "").strip() if secrecy_field else ""
    matched = ""
    for opt in ("Tuyệt mật", "Tối mật", "Mật"):
        if opt.lower() in secrecy_text.lower():
            matched = opt
            break
    meta["do_mat"] = matched or "Thường"

    meta.setdefault("ngon_ngu", "Tiếng Việt")
    return meta


def _annotation_to_zone_map(annotation):
    """Form key → bbox so clicking a Section 1 label highlights its source
    word region on the PDF."""
    fields = annotation.get("field_instances", []) or []
    by_label = {}
    for f in fields:
        by_label.setdefault(f.get("label", ""), f)
    label_to_keys = {
        "ISSUE_ORG_NAME":    ["co_quan_ban_hanh"],
        "DOC_TYPE":          ["loai_van_ban"],
        "DOC_NUMBER_SYMBOL": ["so_van_ban", "ky_hieu"],
        "PLACE_DATE":        ["ngay_ban_hanh"],
        "DOC_SUBJECT":       ["trich_yeu"],
        "SIGNER_NAME":       ["nguoi_ky"],
        "SECRECY_MARK":      ["do_mat"],
    }
    zones = {}
    for kie_label, form_keys in label_to_keys.items():
        f = by_label.get(kie_label)
        if not f or not f.get("bbox"):
            continue
        z = {"page": int(f.get("page_index", 0)), "bbox_pdf": f["bbox"]}
        for fk in form_keys:
            zones[fk] = z
    return zones


def _kie_label_for_field_id(annotation: dict, field_id: str | None) -> str:
    if not field_id:
        return ""
    fid = str(field_id)
    for f in (annotation or {}).get("field_instances") or []:
        if str(f.get("field_id") or "") == fid:
            return str(f.get("label") or "")
    return ""


def _metadata_keys_impacted_by_kie_label(kie_label: str | None) -> set[str]:
    """Final-form fields that must refresh when a raw KIE label changes."""
    label = str(kie_label or "")
    if label in {"ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME"}:
        return {"co_quan_ban_hanh"}
    if label == "DOC_NUMBER_SYMBOL":
        return {"so_van_ban", "ky_hieu", "loai_van_ban"}
    if label == "PLACE_DATE":
        return {"ngay_ban_hanh"}
    if label == "DOC_SUBJECT":
        return {"trich_yeu", "loai_van_ban"}
    if label == "SIGNER_NAME":
        return {"nguoi_ky"}
    if label == "SECRECY_MARK":
        return {"do_mat"}
    if label == "DOC_TYPE":
        return {"loai_van_ban"}
    return set()


class _FieldLabel(QLabel):
    clicked = Signal(str)

    def __init__(self, key, text, parent=None):
        super().__init__(text, parent)
        self._key = key
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QLabel {{
                color: {COLOR_TEXT_MUTED};
                font-size: {_FONT_SM}px;
                font-family: {FONT_UI};
                padding: 0;
                margin-top: 2px;
            }}
            QLabel:hover {{ color: {COLOR_ACCENT}; }}
        """)

    def mousePressEvent(self, ev):
        self.clicked.emit(self._key)
        super().mousePressEvent(ev)


class ArchiveStep2Kie(QWidget):
    """Bước 2 — danh sách + viewer + metadata KIE form."""

    browse_input_clicked = Signal()
    process_clicked = Signal()
    stop_clicked = Signal()
    field_label_clicked = Signal(str)
    log_message = Signal(str)

    def __init__(self, icons=None, parent=None):
        super().__init__(parent)
        self._icons = icons or {}
        self._documents = []
        self._current_doc_idx = -1
        self._field_widgets = {}
        self._field_labels = {}
        self._flist_visible = True
        self._source_mode = "folder"   # "folder" | "step1"
        self._is_processing = False
        # Tracks whether the section-1 form has user edits the
        # operator hasn't saved into doc["metadata"] yet. Switching
        # rows while dirty triggers a Save / Discard / Cancel prompt.
        self._form_dirty = False
        # Output folder is no longer surfaced in the toolbar; the pipeline
        # always writes intermediate _ocr.pdf / _ocr.pdf.json into
        # <session_temp>/_step2_kie/ and only Step 3's "Xuất hồ sơ nén"
        # button picks the real destination. We keep the value here so
        # existing set_output_folder/get_output_folder callers still work.
        self._output_folder = ""
        self._fuzzy_active_field = None
        self._fuzzy_timer = QTimer(self)
        self._fuzzy_timer.setSingleShot(True)
        self._fuzzy_timer.setInterval(300)
        self._fuzzy_timer.timeout.connect(self._run_fuzzy_match)
        self._save_notice_timer = QTimer(self)
        self._save_notice_timer.setSingleShot(True)
        self._save_notice_timer.setInterval(1000)
        self._save_notice_timer.timeout.connect(self._hide_saved_notice)
        self._setup_ui()

    # ── ui construction ────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_toolbar(root)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._flist_panel = QWidget()
        self._flist_panel.setFixedWidth(_FLIST_W)
        self._flist_panel.setStyleSheet(f"background: {COLOR_SURFACE};")
        self._build_file_list_panel()
        body.addWidget(self._flist_panel)

        self._btn_toggle = QPushButton("❯")
        self._btn_toggle.setFixedWidth(14)
        self._btn_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_toggle.setStyleSheet(f"""
            QPushButton {{
                background: {COLOR_SURFACE};
                border: none;
                border-left: 1px solid {COLOR_BORDER};
                border-right: 1px solid {COLOR_BORDER};
                color: {COLOR_TEXT_MUTED};
                font-size: 10px; padding: 0;
            }}
            QPushButton:hover {{ background: {COLOR_ELEVATED}; color: {COLOR_TEXT}; }}
        """)
        self._btn_toggle.clicked.connect(self._toggle_file_list)
        body.addWidget(self._btn_toggle)

        self.pdf_viewer = KieArchiveViewer()
        self.pdf_viewer.prev_file_requested.connect(self._go_prev_file)
        self.pdf_viewer.next_file_requested.connect(self._go_next_file)
        self.pdf_viewer.dirty_changed.connect(self._on_viewer_dirty_changed)
        self.pdf_viewer.field_words_changed.connect(self._on_viewer_field_changed)
        self.pdf_viewer.field_clicked.connect(self._on_viewer_field_clicked)
        body.addWidget(self.pdf_viewer, 1)

        self._meta_panel = QWidget()
        self._meta_panel.setFixedWidth(_META_W)
        self._meta_panel.setStyleSheet(f"QWidget {{ background: {COLOR_SURFACE}; }}")
        self._build_metadata_panel()
        body.addWidget(self._meta_panel)

        root.addLayout(body, 1)

        self._spinner_chars = ["⠋", "⠙", "⠹", "⠸",
                                "⠼", "⠴", "⠦", "⠧",
                                "⠇", "⠏"]
        self._spinner_idx = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(80)
        self._spinner_timer.timeout.connect(self._tick_spinner)
        self._spinner_timer.start()

    def _build_toolbar(self, parent_layout):
        bar = QFrame()
        bar.setFixedHeight(34)
        bar.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE}; "
            f"border-bottom: 1px solid {COLOR_BORDER}; }}"
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 0, 8, 0)
        h.setSpacing(4)

        # Source mode label
        self._lbl_source = QLabel(translations.get_text("arc_step2_source_folder"))
        self._lbl_source.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI};"
        )
        h.addWidget(self._lbl_source)

        self.entry_input = self._make_path_input()
        h.addWidget(self.entry_input, 1)

        self._btn_browse_in = self._make_browse_btn()
        self._btn_browse_in.clicked.connect(self.browse_input_clicked.emit)
        h.addWidget(self._btn_browse_in)

        h.addSpacing(6)

        self.btn_process = QPushButton(translations.get_text("arc_btn_process"))
        self.btn_process.setFixedHeight(_H)
        self.btn_process.setStyleSheet(f"""
            QPushButton {{
                background: {COLOR_GREEN}; border: none; border-radius: {_RAD}px;
                color: #fff; font-size: {_FONT}px; font-family: {FONT_UI};
                font-weight: 600; padding: 0 14px;
            }}
            QPushButton:hover {{ background: {COLOR_GREEN_HOVER}; }}
        """)
        self.btn_process.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_process.clicked.connect(self.process_clicked.emit)
        h.addWidget(self.btn_process)

        self.btn_stop = QPushButton(translations.get_text("arc_btn_stop"))
        self.btn_stop.setFixedHeight(_H)
        self.btn_stop.setStyleSheet(f"""
            QPushButton {{
                background: {COLOR_RED}; border: none; border-radius: {_RAD}px;
                color: #fff; font-size: {_FONT}px; font-family: {FONT_UI};
                font-weight: 600; padding: 0 14px;
            }}
            QPushButton:hover {{ background: {COLOR_RED_HOVER}; }}
        """)
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop.clicked.connect(self.stop_clicked.emit)
        self.btn_stop.setVisible(False)
        h.addWidget(self.btn_stop)

        parent_layout.addWidget(bar)

    def _build_metadata_panel(self):
        layout = QVBoxLayout(self._meta_panel)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(0)

        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        self._lbl_meta_title = QLabel(translations.get_text("arc_metadata_title"))
        self._lbl_meta_title.setStyleSheet(
            f"font-size: {_FONT_SM}px; font-weight: 600; "
            f"color: {COLOR_TEXT_SECONDARY}; font-family: {FONT_UI}; "
            f"text-transform: uppercase; letter-spacing: 0.5px;"
        )
        hdr.addWidget(self._lbl_meta_title)
        hdr.addStretch()

        self._lbl_saved_notice = QLabel(translations.get_text("arc_saved_notice"))
        self._lbl_saved_notice.setVisible(False)
        self._lbl_saved_notice.setStyleSheet(
            f"color: {COLOR_GREEN}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI}; font-weight: 600;"
        )
        hdr.addWidget(self._lbl_saved_notice)

        self._btn_save_meta = QPushButton(translations.get_text("arc_btn_save"))
        self._btn_save_meta.setFixedHeight(_H - 2)
        self._btn_save_meta.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_save_meta.setStyleSheet(f"""
            QPushButton {{
                background: {COLOR_ACCENT}; border: none; border-radius: {_RAD}px;
                color: #fff; font-size: {_FONT_SM}px; font-family: {FONT_UI};
                font-weight: 600; padding: 0 10px;
            }}
            QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}
        """)
        self._btn_save_meta.clicked.connect(self._on_save_meta_clicked)
        hdr.addWidget(self._btn_save_meta)
        layout.addLayout(hdr)

        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_scroll.setStyleSheet(f"""
            QScrollArea {{ background: {COLOR_SURFACE}; border: none; }}
            QScrollBar:vertical {{ background: transparent; width: 6px; }}
            QScrollBar::handle:vertical {{
                background: {COLOR_BORDER_DEFAULT}; border-radius: 3px; min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

        form_w = QWidget()
        form_l = QVBoxLayout(form_w)
        form_l.setContentsMargins(0, 4, 0, 4)
        form_l.setSpacing(2)

        for key, tr_key, multiline in _FIELDS:
            lbl = _FieldLabel(key, translations.get_text(tr_key))
            lbl.clicked.connect(self._on_field_label_clicked)
            self._field_labels[key] = lbl
            form_l.addWidget(lbl)

            if key == "ngay_ban_hanh":
                # Plain QLineEdit, not QDateEdit — user wants to type
                # dd/mm/yyyy directly without dealing with a calendar
                # popup. Validation lives on textChanged; invalid /
                # empty input gets a red border via _refresh_validity.
                # editingFinished auto-normalises so "12121988" snaps to
                # "12/12/1988" on Tab / Enter / blur.
                w = QLineEdit()
                w.setPlaceholderText("dd/mm/yyyy")
                w.setFixedHeight(_H)
                w.setStyleSheet(self._field_qss("QLineEdit", invalid=False))
                w.textChanged.connect(lambda _t, k=key: self._on_field_text_changed(k))
                w.textChanged.connect(lambda _t, k=key: self._refresh_validity(k))
                w.editingFinished.connect(
                    lambda widget=w, k=key: self._auto_normalize_date(k)
                )
            elif key == "loai_van_ban":
                # ComboBox driven by archive_doctype taxonomy. KIE
                # postprocess auto-fills DOC_TYPE from subject prefix +
                # doc_number suffix, but the user can still override
                # here when the heuristic is wrong. FuzzyComboBox adds
                # type-to-filter (diacritic-insensitive) on top.
                from scanindex.core.digitization.doctype import all_display_names
                w = FuzzyComboBox()
                w.addItems(all_display_names())
                w.setCurrentIndex(-1)             # blank by default
                w.setFixedHeight(_H)
                w.setStyleSheet(self._field_qss("QComboBox", invalid=True))
                w.currentTextChanged.connect(
                    lambda _t, k=key: self._on_field_text_changed(k)
                )
                w.currentTextChanged.connect(
                    lambda _t, k=key: self._refresh_validity(k)
                )
            elif key == "do_mat":
                # 4-option fuzzy combo. Anything outside this set is
                # treated as "Thường" by `_annotation_to_metadata_form`.
                # `sort=False` keeps the severity order (Thường → Tuyệt
                # mật) instead of A-Z which would put Mật first.
                w = FuzzyComboBox(sort=False)
                w.addItems(_DO_MAT_OPTIONS)
                w.setCurrentIndex(0)              # default "Thường"
                w.setFixedHeight(_H)
                w.currentTextChanged.connect(
                    lambda _t, k=key: self._on_field_text_changed(k)
                )
            else:
                w = QTextEdit()
                w.setMinimumHeight(_H)
                w.setMaximumHeight(16777215)
                w.setSizePolicy(
                    QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.Fixed,
                )
                w.document().setDocumentMargin(0)
                w.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
                w.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
                w.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                w.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                w.setStyleSheet(self._field_qss("QTextEdit", invalid=False))
                w.textChanged.connect(
                    lambda widget=w: QTimer.singleShot(0, lambda: self._auto_resize_textedit(widget))
                )
                w.textChanged.connect(lambda k=key: self._on_field_text_changed(k))
                # so_van_ban gets the same red-border treatment as the
                # date — bad OCR (e.g. extracting "Số 245" or just dots)
                # is dropped at export, but the user still sees the
                # original input flagged in the form.
                if key == "so_van_ban":
                    w.textChanged.connect(lambda k=key: self._refresh_validity(k))
            self._field_widgets[key] = w
            form_l.addWidget(w)
            w.installEventFilter(self)

        # ── Section 2: raw KIE viewer ───────────────────────────────────
        # All 14 raw KIE labels with their badge colour + on-PDF number.
        # Row order is fixed (`_RAW_KIE_LABELS`); only text + visibility
        # change as the active document switches.
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {COLOR_BORDER_DEFAULT}; margin: 8px 0 4px 0;")
        form_l.addWidget(sep)

        raw_title = QLabel(translations.get_text("arc_raw_kie_title"))
        raw_title.setStyleSheet(
            f"font-size: {_FONT_SM}px; font-weight: 600; "
            f"color: {COLOR_TEXT_SECONDARY}; font-family: {FONT_UI}; "
            f"text-transform: uppercase; letter-spacing: 0.5px; padding: 4px 0;"
        )
        form_l.addWidget(raw_title)

        self._raw_kie_rows = {}
        for label, display in _RAW_KIE_LABELS:
            row = self._make_raw_kie_row(label, display)
            self._raw_kie_rows[label] = row
            form_l.addWidget(row)

        form_l.addStretch()
        form_scroll.setWidget(form_w)
        layout.addWidget(form_scroll, 1)

        self.pdf_viewer.fuzzy_match_picked.connect(self._on_fuzzy_match_picked)

    def _make_raw_kie_row(self, label: str, display: str) -> QFrame:
        """One row in the raw KIE panel. Layout:

            [#N] <colored display name>
                 <extracted text>      (read-only, multi-line wrapped)

        Click anywhere on the row → highlight the bbox on the PDF.
        """
        from scanindex.ui.widgets.kie_archive_viewer import (
            FIELD_NUMBER_MAP, LABEL_COLORS,
        )
        dark, light = LABEL_COLORS.get(label, ("#6b7280", "#9ca3af"))
        number = FIELD_NUMBER_MAP.get(label)

        row = QFrame()
        row.setObjectName("RawKieRow")
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setStyleSheet(
            f"QFrame#RawKieRow {{ background: transparent; border: none; "
            f"border-left: 3px solid {dark}; padding: 2px 0 2px 6px; }}"
            f"QFrame#RawKieRow:hover {{ background: {COLOR_ELEVATED}; }}"
        )

        v = QVBoxLayout(row)
        v.setContentsMargins(0, 2, 0, 2)
        v.setSpacing(1)

        # Header: badge + label name
        h = QHBoxLayout()
        h.setSpacing(6)
        h.setContentsMargins(0, 0, 0, 0)

        badge = QLabel(str(number) if number is not None else "·")
        badge.setFixedWidth(18)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            f"background: {dark}; color: white; "
            f"border-radius: 9px; font: 600 10px '{FONT_UI}';"
        )
        h.addWidget(badge)

        name_lbl = QLabel(display)
        name_lbl.setStyleSheet(
            f"color: {light}; font: 600 {_FONT_SM}px '{FONT_UI}'; "
            f"background: transparent;"
        )
        h.addWidget(name_lbl)
        h.addStretch()
        v.addLayout(h)

        # Body: extracted text (set later by _refresh_raw_kie_panel)
        text_lbl = QLabel("—")
        text_lbl.setObjectName("RawKieText")
        text_lbl.setWordWrap(True)
        text_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        text_lbl.setStyleSheet(
            f"color: {COLOR_TEXT}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI}; padding: 0 0 0 24px; "
            f"background: transparent;"
        )
        v.addWidget(text_lbl)

        # Whole-row click → highlight the bbox on the PDF.
        row.mousePressEvent = lambda _ev, lbl=label: self._on_raw_kie_clicked(lbl)
        # The body text label has TextSelectableByMouse and therefore
        # consumes mouse press events before they bubble up to the row's
        # handler. Forward presses to the same jump handler while still
        # invoking QLabel's own press logic so click-drag selection
        # keeps working for copying text.
        text_lbl.mousePressEvent = lambda ev, lbl=label, _w=text_lbl: (
            self._on_raw_kie_clicked(lbl),
            QLabel.mousePressEvent(_w, ev),
        )
        return row

    def _refresh_raw_kie_panel(self, annotation):
        """Update the body text of every raw KIE row from the active doc's
        annotation. Per-label fallbacks fill rows that the model didn't
        emit so the user gets something meaningful instead of '—':
          - SECRECY_MARK : absence ≡ "Thường" (per HSLTCQ convention)
          - DOC_TYPE     : derive from DOC_SUBJECT prefix + DOC_NUMBER suffix
                           via archive_doctype.detect_doc_type (returns
                           "Khác" when nothing matches — still better than
                           leaving the row blank).
        """
        rows = getattr(self, "_raw_kie_rows", None)
        if not rows:
            return
        by_label = {}
        for f in (annotation or {}).get("field_instances") or []:
            existing = by_label.get(f.get("label", ""))
            text = (f.get("text") or "").strip()
            if not text:
                continue
            if existing is None:
                by_label[f.get("label", "")] = text
            else:
                by_label[f.get("label", "")] = existing + " | " + text

        if not by_label.get("SECRECY_MARK"):
            by_label["SECRECY_MARK"] = "Thường"

        try:
            from scanindex.core.digitization.doctype import detect_doc_type
            detected = detect_doc_type(
                by_label.get("DOC_SUBJECT", ""),
                by_label.get("DOC_NUMBER_SYMBOL", ""),
            )
            if detected:
                by_label["DOC_TYPE"] = detected
        except Exception:
            pass

        for label, row in rows.items():
            text_lbl = row.findChild(QLabel, "RawKieText")
            if text_lbl is None:
                continue
            text_lbl.setText(by_label.get(label, "—"))

    def _on_raw_kie_clicked(self, kie_label: str):
        """Click on a raw KIE row → activate that field in the PDF viewer
        and scroll to its bbox. Mirrors the Section 1 label-click path."""
        idx = self._current_doc_idx
        if idx < 0 or idx >= len(self._documents):
            return
        annotation = self._documents[idx].get("annotation") or {}
        for f in annotation.get("field_instances") or []:
            if f.get("label") == kie_label:
                self.pdf_viewer.set_active_field(f.get("field_id", ""))
                bbox = f.get("bbox") or []
                if bbox:
                    self.pdf_viewer.highlight_zone(int(f.get("page_index", 0)), bbox)
                return
        self.pdf_viewer.clear_highlight()

    def _build_file_list_panel(self):
        layout = QVBoxLayout(self._flist_panel)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        self._lbl_docs = QLabel(translations.get_text("arc_doc_list"))
        self._lbl_docs.setStyleSheet(
            f"font-size: {_FONT_SM}px; font-weight: 600; "
            f"color: {COLOR_TEXT_SECONDARY}; font-family: {FONT_UI}; "
            f"text-transform: uppercase; letter-spacing: 0.5px;"
        )
        hdr.addWidget(self._lbl_docs)
        hdr.addStretch()
        self._lbl_count = QLabel()
        self._lbl_count.setStyleSheet(
            f"font-size: {_FONT_SM}px; color: {COLOR_TEXT_MUTED}; "
            f"font-family: {FONT_UI};"
        )
        hdr.addWidget(self._lbl_count)
        layout.addLayout(hdr)

        self.doc_list = QListWidget()
        self.doc_list.setStyleSheet(f"""
            QListWidget {{
                background: {COLOR_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: {_RAD}px;
                outline: none;
                font-size: {_FONT_SM}px;
                font-family: {FONT_UI};
            }}
            QListWidget::item {{ padding: 3px 6px; }}
            QListWidget::item:selected {{ background: {COLOR_ACCENT}; color: #fff; }}
            QListWidget::item:hover:!selected {{ background: {COLOR_ELEVATED}; }}
        """)
        self.doc_list.currentRowChanged.connect(self._on_doc_selected)
        layout.addWidget(self.doc_list, 1)

    # ── helpers ─────────────────────────────────────────────────────

    def _make_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI};"
        )
        return lbl

    def _make_path_input(self):
        w = QLineEdit()
        w.setReadOnly(True)
        w.setPlaceholderText(translations.get_text("arc_step2_source_folder_hint"))
        w.setFixedHeight(_H)
        w.setStyleSheet(f"""
            QLineEdit {{
                background: {COLOR_INPUT};
                border: 1px solid {COLOR_BORDER};
                border-radius: {_RAD}px;
                color: {COLOR_TEXT};
                font-size: {_FONT_SM}px;
                font-family: {FONT_UI};
                padding: 0 6px;
            }}
        """)
        return w

    def _make_browse_btn(self):
        b = QPushButton("Chọn")
        b.setFixedHeight(_H)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet(f"""
            QPushButton {{
                background: {COLOR_ELEVATED};
                border: 1px solid {COLOR_BORDER_DEFAULT};
                border-radius: {_RAD}px;
                color: {COLOR_TEXT_SECONDARY};
                font-size: {_FONT}px;
                font-family: {FONT_UI};
                padding: 0 12px;
            }}
            QPushButton:hover {{ background: {COLOR_HOVER}; color: {COLOR_TEXT}; }}
        """)
        return b

    def _toggle_file_list(self):
        self._flist_visible = not self._flist_visible
        self._flist_panel.setVisible(self._flist_visible)
        self._btn_toggle.setText("❯" if self._flist_visible else "❮")

    # ── source mode ─────────────────────────────────────────────────

    def set_source_mode(self, mode: str):
        """`mode` is "folder" or "step1". In step1 mode the input folder
        controls are hidden and the source label flips to "Bước 1"."""
        self._source_mode = mode
        if mode == "step1":
            self._lbl_source.setText(translations.get_text("arc_step2_source_step1"))
            self.entry_input.setReadOnly(True)
            self.entry_input.setPlaceholderText(
                translations.get_text("arc_step2_source_step1_hint"))
            self._btn_browse_in.setVisible(False)
            # Pipeline auto-starts on the Step 1 handoff — manual process
            # button would only confuse users (and crash _arc_start_process,
            # which would try to scan the "From Step 1 (N files)" string).
            self.btn_process.setVisible(False)
        else:
            self._lbl_source.setText(translations.get_text("arc_step2_source_folder"))
            self.entry_input.setReadOnly(True)
            self.entry_input.setPlaceholderText(
                translations.get_text("arc_step2_source_folder_hint"))
            self._btn_browse_in.setVisible(True)
            self.btn_process.setVisible(not self._is_processing)

    def get_source_mode(self) -> str:
        return self._source_mode

    # ── file navigation ─────────────────────────────────────────────

    def _go_prev_file(self):
        if self._current_doc_idx > 0:
            self.doc_list.setCurrentRow(self._current_doc_idx - 1)

    def _go_next_file(self):
        if self._current_doc_idx < len(self._documents) - 1:
            self.doc_list.setCurrentRow(self._current_doc_idx + 1)

    def _update_file_nav(self):
        total = len(self._documents)
        idx = self._current_doc_idx
        self.pdf_viewer.set_file_label(idx, total)
        self.pdf_viewer.set_file_nav_enabled(idx > 0, idx < total - 1)

    # ── public API ──────────────────────────────────────────────────

    def set_input_folder(self, path):
        self.entry_input.setText(path)

    def set_output_folder(self, path):
        self._output_folder = path or ""

    def get_input_folder(self):
        return self.entry_input.text().strip()

    def get_output_folder(self):
        return self._output_folder

    def set_processing_state(self, is_running):
        self._is_processing = bool(is_running)
        self.btn_process.setVisible((not is_running) and self._source_mode != "step1")
        self.btn_stop.setVisible(is_running)
        self._btn_browse_in.setEnabled(not is_running)

    def reset(self):
        """Wipe every piece of Step 2 state for the "↻ Bắt đầu lại" flow:
        viewer (PDF + canonical), document list, right-side form fields,
        fuzzy state, file-nav label. `set_documents([])` alone leaves the
        viewer + form populated from the previous run."""
        self.pdf_viewer.clear()
        self.set_source_mode("folder")
        self.set_input_folder("")
        self.set_output_folder("")
        self.set_documents([])
        self.set_processing_state(False)
        self._clear_fields()
        self._form_dirty = False
        self._hide_saved_notice()
        self._fuzzy_active_field = None
        self.pdf_viewer.set_file_label(-1, 0)
        self.pdf_viewer.set_file_nav_enabled(False, False)

    def set_progress(self, current, total):
        return  # progress reflected per-row

    def set_documents(self, documents, default_status: str = "Pending"):
        """Populate the list. With `default_status='OCR...'` (used when
        coming from Step 1) every row starts active — load spinner shown
        until the pipeline marks it Done."""
        self._documents = documents
        self.doc_list.blockSignals(True)
        self.doc_list.clear()
        for doc in documents:
            name = os.path.basename(doc.get("pdf_path", ""))
            item = QListWidgetItem(name)
            # Soft-warn malformed names from folder mode
            if (self._source_mode == "folder"
                    and not NAME_PATTERN.match(name)):
                item.setToolTip(translations.get_text("arc_step2_name_warn"))
                item.setText(f"⚠ {name}")
            secrecy = doc.get("_secrecy") if isinstance(doc, dict) else None
            if secrecy:
                item.setToolTip(f"Văn bản mật: {secrecy}")
            self.doc_list.addItem(item)
            doc.setdefault("status", default_status)
            self._apply_row_state(item, doc)
        self.doc_list.blockSignals(False)
        self._lbl_count.setText(str(len(documents)))
        self._current_doc_idx = -1
        target_row = -1
        for i, d in enumerate(documents):
            if self._is_preview_ready(d):
                target_row = i; break
        if target_row >= 0:
            self.doc_list.setCurrentRow(target_row)
        if not self._flist_visible and documents:
            self._toggle_file_list()

    def update_doc_status(self, idx: int, status: str):
        if not (0 <= idx < self.doc_list.count()):
            return
        if not (0 <= idx < len(self._documents)):
            return
        self._documents[idx]["status"] = status
        item = self.doc_list.item(idx)
        if item is not None:
            self._apply_row_state(item, self._documents[idx])
        if self._current_doc_idx < 0 and self._is_preview_ready(self._documents[idx]):
            self.doc_list.setCurrentRow(idx)

    def _apply_row_state(self, item: QListWidgetItem, doc: dict):
        from PySide6.QtCore import Qt as _Qt
        status = doc.get("status", "") if isinstance(doc, dict) else ""
        # KIE_DONE populates json_path on the doc *before* FILE_COMPLETE
        # flips status to "Done". Treat the row as preview-ready as soon as
        # the data lands, so the brief "Pending" window between events
        # (or a pipeline that never gets to FILE_COMPLETE) doesn't lock
        # the user out of an already-finished file.
        has_output = bool(isinstance(doc, dict) and doc.get("json_path"))
        # Secrecy mark detected in Step 1 (mật / tối mật / tuyệt mật) —
        # paint the row red across all states so the user can spot
        # classified docs at a glance, even before KIE runs.
        has_secrecy = bool(isinstance(doc, dict) and doc.get("_secrecy"))

        is_failed = status in ("Failed", "Done (Export Failed)")
        is_done_status = status == "Done"
        is_complete = is_done_status or has_output
        is_active = (
            not is_complete and not is_failed
            and status not in ("Pending", "", "Corrected", "OCR Done")
        )

        name = self._strip_state_prefix(item.text())
        selectable = item.flags() | _Qt.ItemFlag.ItemIsSelectable | _Qt.ItemFlag.ItemIsEnabled
        not_selectable = item.flags() & ~_Qt.ItemFlag.ItemIsSelectable & ~_Qt.ItemFlag.ItemIsEnabled
        if is_complete:
            # KIE produced annotation output → clickable.
            item.setText(name)
            item.setForeground(QBrush(QColor(COLOR_RED if has_secrecy else COLOR_TEXT)))
            item.setFlags(selectable)
        elif is_failed:
            item.setText(name)
            item.setForeground(QBrush(QColor(COLOR_RED)))
            item.setFlags(not_selectable)
        elif is_active:
            char = self._spinner_chars[self._spinner_idx % len(self._spinner_chars)]
            item.setText(f"{char} {name}")
            item.setForeground(QBrush(QColor(COLOR_RED if has_secrecy else COLOR_TEXT_MUTED)))
            item.setFlags(not_selectable)
        else:
            # "Pending" before KIE has produced data for this file.
            item.setText(name)
            item.setForeground(QBrush(QColor(COLOR_RED if has_secrecy else COLOR_TEXT_MUTED)))
            item.setFlags(not_selectable)

    @staticmethod
    def _is_preview_ready(doc: dict) -> bool:
        if not isinstance(doc, dict):
            return False
        status = doc.get("status", "")
        return status == "Done" or bool(doc.get("json_path"))

    def _strip_state_prefix(self, text):
        if not text:
            return text
        if len(text) >= 2 and text[0] in self._spinner_chars and text[1] == " ":
            return text[2:]
        # Strip any warning prefix too (won't recur because we apply it once)
        if text.startswith("⚠ "):
            return text[2:]
        return text

    def _tick_spinner(self):
        self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_chars)
        char = self._spinner_chars[self._spinner_idx]
        for i in range(self.doc_list.count()):
            item = self.doc_list.item(i)
            if not item or not (0 <= i < len(self._documents)):
                continue
            status = self._documents[i].get("status", "Pending")
            if status in ("Pending", "Done", "Corrected", "OCR Done",
                          "Failed", "Done (Export Failed)"):
                continue
            txt = item.text()
            if len(txt) >= 2 and txt[0] in self._spinner_chars:
                item.setText(char + txt[1:])

    def get_documents(self):
        self._save_current_fields()
        return self._documents

    def has_unsaved_changes(self) -> bool:
        viewer_dirty = False
        try:
            viewer_dirty = bool(self.pdf_viewer.is_dirty())
        except Exception:
            viewer_dirty = False
        return bool(self._form_dirty or viewer_dirty)

    def confirm_unsaved_before_leave(self) -> bool:
        """Prompt before leaving Step 2 when bbox or metadata edits are dirty."""
        form_values = self._current_form_values() if self._form_dirty else None
        try:
            if not self.pdf_viewer.check_unsaved():
                return False
        except Exception as e:
            QMessageBox.warning(
                self,
                "Không thể kiểm tra thay đổi",
                f"Không kiểm tra được thay đổi KIE chưa lưu:\n{e}",
            )
            return False
        try:
            if self.pdf_viewer.dirty_resolution() == "discard":
                self._restore_current_doc_after_viewer_discard()
                if form_values is not None:
                    self._restore_form_values(form_values, dirty=True)
        except Exception as e:
            QMessageBox.warning(
                self,
                "Không thể bỏ thay đổi",
                f"Không khôi phục được KIE trước khi sửa:\n{e}",
            )
            return False
        return self._confirm_form_unsaved()

    def _confirm_form_unsaved(self) -> bool:
        if not self._form_dirty:
            return True
        reply = QMessageBox.question(
            self,
            "Có thay đổi chưa lưu",
            "Bạn đã chỉnh thông tin của văn bản hiện tại.\n"
            "Lưu lại trước khi chuyển?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if reply == QMessageBox.StandardButton.Save:
            self._save_current_fields()
            return True
        if reply == QMessageBox.StandardButton.Discard:
            self._discard_current_form_edits()
            return True
        return False

    def _discard_current_form_edits(self) -> None:
        idx = self._current_doc_idx
        if idx < 0 or idx >= len(self._documents):
            self._form_dirty = False
            return
        meta = self._documents[idx].get("metadata") or {}
        for key, _, multiline in _FIELDS:
            self._set_field_value(key, meta.get(key, "") or "", block_signals=True)
        self._refresh_validity("ngay_ban_hanh")
        self._refresh_validity("so_van_ban")
        self._form_dirty = False
        self._resize_fields_soon()

    def _current_form_values(self) -> dict:
        return {key: self._field_value(key) for key, _, _ in _FIELDS}

    def _restore_form_values(self, values: dict, *, dirty: bool) -> None:
        for key, _, _ in _FIELDS:
            self._set_field_value(key, values.get(key, "") or "", block_signals=True)
        self._refresh_validity("ngay_ban_hanh")
        self._refresh_validity("so_van_ban")
        self._form_dirty = bool(dirty)
        self._resize_fields_soon()

    def _restore_current_doc_after_viewer_discard(self) -> None:
        idx = self._current_doc_idx
        if idx < 0 or idx >= len(self._documents):
            return
        doc = self._documents[idx]
        saved_meta = doc.pop("_metadata_before_viewer_dirty", None)
        json_path = doc.get("json_path")
        if not json_path:
            out_pdf = doc.get("output_path") or ""
            if out_pdf:
                json_path = out_pdf + ".json"

        canonical = None
        if json_path and os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                canonical = json.load(f)
        ann = (canonical or {}).get("annotations") or {}
        doc["annotation"] = ann
        doc["metadata"] = dict(saved_meta) if isinstance(saved_meta, dict) else _annotation_to_metadata_form(ann)
        doc["zones"] = _annotation_to_zone_map(ann)
        if canonical is not None:
            doc["_canonical_cache"] = canonical
            self.pdf_viewer.load_canonical(json_path)
        self._refresh_raw_kie_panel(ann)
        for key, _, _ in _FIELDS:
            self._set_field_value(key, doc["metadata"].get(key, "") or "", block_signals=True)
        self._form_dirty = False
        self._resize_fields_soon()

    def refresh_current_doc(self):
        idx = self._current_doc_idx
        if 0 <= idx < len(self._documents):
            self._current_doc_idx = -1
            self._on_doc_selected(idx)

    def update_texts(self):
        self._lbl_docs.setText(translations.get_text("arc_doc_list"))
        self.btn_process.setText(translations.get_text("arc_btn_process"))
        self.btn_stop.setText(translations.get_text("arc_btn_stop"))
        self.set_source_mode(self._source_mode)
        self._lbl_saved_notice.setText(translations.get_text("arc_saved_notice"))
        for key, tr_key, _ in _FIELDS:
            if key in self._field_labels:
                self._field_labels[key].setText(translations.get_text(tr_key))
        self.pdf_viewer.update_texts()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_fields_soon()

    # ── selection / metadata (port of original ArchiveTab) ─────────

    def _debug_log(self, msg: str):
        """Surface debug trace to the visible log panel (so user sees it
        without opening a terminal) AND append to a file in %TEMP% as a
        durable record. Best-effort, never raises."""
        try:
            self.log_message.emit(f"[STEP2-SELECT] {msg}")
        except Exception:
            pass
        try:
            import tempfile, time as _time
            path = os.path.join(tempfile.gettempdir(), "ocrtool_step2_select.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{_time.strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    def _field_qss(self, widget_type: str, *, invalid: bool) -> str:
        """Build the per-field stylesheet. When ``invalid`` is True, the
        border swaps to ``COLOR_RED`` even on focus so the alert is
        visible until the user fixes the value."""
        if invalid:
            return (
                f"{widget_type} {{ {_TEXTAREA} border: 1px solid {COLOR_RED}; }}"
                f"{widget_type}:focus {{ border: 1px solid {COLOR_RED}; }}"
            )
        return (
            f"{widget_type} {{ {_TEXTAREA} }}"
            f"{widget_type}:focus {{ {_INPUT_FOCUS} }}"
        )

    def _auto_normalize_date(self, key: str) -> None:
        """On editingFinished, snap parseable text to DD/MM/YYYY in the
        widget. Lets the user type "12121988" and have it shown as
        "12/12/1988" on Tab / Enter / focus-out without losing the
        cursor mid-typing (we only fire on commit, not on every key)."""
        widget = self._field_widgets.get(key)
        if not isinstance(widget, QLineEdit):
            return
        text = widget.text().strip()
        normalized = _normalize_date_input(text)
        if normalized and normalized != text:
            widget.blockSignals(True)
            try:
                widget.setText(normalized)
            finally:
                widget.blockSignals(False)
            self._refresh_validity(key)

    def _refresh_validity(self, key: str) -> None:
        """Toggle the red-border style for date / number fields based on
        their current text. Empty *or* unparseable counts as invalid —
        empty means KIE failed to extract the value, both deserve the
        attention prompt."""
        widget = self._field_widgets.get(key)
        if widget is None:
            return
        if key == "ngay_ban_hanh":
            text = widget.text().strip() if isinstance(widget, QLineEdit) else ""
            valid = bool(text) and bool(_normalize_date_input(text))
            widget.setStyleSheet(self._field_qss("QLineEdit", invalid=not valid))
        elif key == "so_van_ban":
            if isinstance(widget, QTextEdit):
                text = widget.toPlainText().strip()
            elif isinstance(widget, QLineEdit):
                text = widget.text().strip()
            else:
                return
            valid = bool(text) and bool(_NUMBER_INPUT_RE.match(text))
            widget_type = "QTextEdit" if isinstance(widget, QTextEdit) else "QLineEdit"
            widget.setStyleSheet(self._field_qss(widget_type, invalid=not valid))
        elif key == "loai_van_ban":
            if not isinstance(widget, QComboBox):
                return
            text = widget.currentText().strip()
            try:
                from scanindex.core.digitization.doctype import all_display_names
                valid = bool(text) and text in set(all_display_names())
            except Exception:
                valid = bool(text)
            widget.setStyleSheet(self._field_qss("QComboBox", invalid=not valid))
            if valid:
                widget.setToolTip("")
            else:
                widget.setToolTip(
                    "Chưa xác định được tên loại văn bản. Hãy chọn lại trước khi xuất."
                )

    def _set_field_value(self, key: str, value: str, block_signals: bool = False):
        # NOTE: this method is called both on KIE-projection load *and*
        # on row-switch reload. Filtering invalid input here would wipe
        # user edits when they switch rows and back, so per-field
        # strictness lives in `_annotation_to_metadata_form` (KIE side)
        # and in `_apply_form_overrides` (export side) instead.
        widget = self._field_widgets.get(key)
        if widget is None:
            return
        if key == "co_quan_ban_hanh":
            value = _single_line_text(str(value or ""))
        old_block = widget.blockSignals(block_signals)
        try:
            if isinstance(widget, QComboBox):
                widget.setCurrentText(str(value or ""))
            elif isinstance(widget, QTextEdit):
                widget.setPlainText(str(value or ""))
            else:
                widget.setText(str(value or ""))
        finally:
            widget.blockSignals(old_block)
        if isinstance(widget, QTextEdit):
            self._resize_fields_soon()
        if key in ("ngay_ban_hanh", "so_van_ban", "loai_van_ban"):
            self._refresh_validity(key)

    def _field_value(self, key: str) -> str:
        widget = self._field_widgets.get(key)
        if widget is None:
            return ""
        if isinstance(widget, QComboBox):
            return widget.currentText().strip()
        if isinstance(widget, QTextEdit):
            value = widget.toPlainText().strip()
            if key == "co_quan_ban_hanh":
                return _single_line_text(value)
            return value
        text = widget.text().strip()
        # Normalise the date here too — `editingFinished` fires on
        # focus-loss, but if the user clicks "Xuất hồ sơ nén" without
        # tabbing out of the date field, the auto-normalize hasn't run
        # yet. Normalising on save guarantees meta always carries a
        # canonical "DD/MM/YYYY" form (or the user's raw text when it
        # doesn't parse, which the export-side validator then drops).
        if key == "ngay_ban_hanh":
            normalized = _normalize_date_input(text)
            if normalized:
                return normalized
        return text

    def _clear_field_value(self, key: str):
        widget = self._field_widgets.get(key)
        if widget is None:
            return
        if isinstance(widget, QComboBox):
            widget.setCurrentIndex(-1)
        elif isinstance(widget, QTextEdit):
            widget.setPlainText("")
        else:
            widget.setText("")
        if key in ("ngay_ban_hanh", "so_van_ban", "loai_van_ban"):
            self._refresh_validity(key)

    def _on_doc_selected(self, row):
        # Item flags already gate selection (only "Done" rows are
        # ItemIsSelectable; pending / in-progress / failed rows are
        # disabled at the model level), so this slot only fires for rows
        # whose KIE has finished. The handler can therefore assume
        # annotation/output paths are present.

        prev_row = self._current_doc_idx
        if prev_row != row and prev_row >= 0:
            if not self.confirm_unsaved_before_leave():
                self.doc_list.blockSignals(True)
                self.doc_list.setCurrentRow(prev_row)
                self.doc_list.blockSignals(False)
                return
        if self._form_dirty:
            self._save_current_fields()
        else:
            # _save_current_fields() also resets dirty; if we're not
            # calling it (Discard branch) clear the flag manually
            # so the next row starts clean.
            self._form_dirty = False
        self._current_doc_idx = row
        self._update_file_nav()
        if row < 0 or row >= len(self._documents):
            self._clear_fields()
            self.pdf_viewer.clear()
            return
        doc = self._documents[row]

        json_path = doc.get("json_path")
        if not json_path:
            out_pdf = doc.get("output_path") or ""
            if out_pdf:
                json_path = out_pdf + ".json"
        self._debug_log(
            f"row={row} status={doc.get('status')!r} "
            f"output_path={doc.get('output_path')!r} "
            f"json_path={json_path!r} "
            f"json_exists={bool(json_path) and os.path.exists(json_path)}"
        )
        annotation = _load_annotation(json_path) if json_path else None
        if annotation:
            doc["annotation"] = annotation
            # Derive form metadata from the annotation only when the
            # doc doesn't already carry user-edited values. Re-running
            # the projection on every selection clobbers anything the
            # operator typed (e.g. a manually-entered date for a doc
            # whose OCR garbled the PLACE_DATE token), which would
            # silently revert their work between row clicks.
            existing_meta = doc.get("metadata") or {}
            derived = _annotation_to_metadata_form(annotation)
            if existing_meta:
                # Merge: user's existing non-empty values win, derived
                # values fill the rest.
                merged = dict(derived)
                for k, v in existing_meta.items():
                    if isinstance(v, str) and v.strip():
                        merged[k] = v
                doc["metadata"] = merged
            else:
                doc["metadata"] = derived
            doc["zones"] = _annotation_to_zone_map(annotation)
            self._debug_log(
                f"  annotation OK: "
                f"{len(annotation.get('field_instances', []))} field_instances; "
                f"mapped meta keys={list(doc['metadata'].keys())}"
            )
        else:
            self._debug_log(
                f"  NO annotation (json_path missing OR schema lacks "
                f"'annotations.field_instances')"
            )

        meta = doc.get("metadata", {})
        for key, _, multiline in _FIELDS:
            val = meta.get(key, "") or ""
            self._set_field_value(key, val, block_signals=True)
        self._refresh_raw_kie_panel(doc.get("annotation"))
        self._resize_fields_soon()

        for candidate in [doc.get("output_path"), doc.get("ocr_path"), doc.get("pdf_path")]:
            if candidate and os.path.exists(candidate):
                self.pdf_viewer.load_pdf(candidate)
                if json_path:
                    self.pdf_viewer.load_canonical(json_path)
                else:
                    self.pdf_viewer.clear_field_overlays()
                return
        self.pdf_viewer.clear()

    def _on_viewer_dirty_changed(self, dirty: bool):
        idx = self._current_doc_idx
        if not (0 <= idx < len(self._documents)):
            return
        doc = self._documents[idx]
        if dirty:
            doc.setdefault("_metadata_before_viewer_dirty", dict(doc.get("metadata") or {}))
            return
        try:
            resolution = self.pdf_viewer.dirty_resolution()
        except Exception:
            resolution = None
        if resolution == "save":
            doc.pop("_metadata_before_viewer_dirty", None)

    def _on_viewer_field_changed(self, field_id, op):
        idx = self._current_doc_idx
        if not (0 <= idx < len(self._documents)):
            return
        doc = self._documents[idx]
        doc.setdefault("_metadata_before_viewer_dirty", dict(doc.get("metadata") or {}))
        previous_ann = doc.get("annotation") or {}
        # Save current top-panel edits first. Then overwrite only the final
        # fields impacted by the raw KIE bbox that just changed. This gives
        # true last-action-wins behavior: direct edit after bbox wins; bbox
        # edit after direct edit wins for the affected final fields.
        self._save_current_fields()
        canonical = self.pdf_viewer.canonical() or {}
        ann = canonical.get("annotations") or {}
        changed_label = (
            _kie_label_for_field_id(ann, field_id)
            or _kie_label_for_field_id(previous_ann, field_id)
        )
        doc["annotation"] = ann
        derived = _annotation_to_metadata_form(ann)
        existing = doc.get("metadata") or {}
        merged = dict(existing)
        impacted = _metadata_keys_impacted_by_kie_label(changed_label)
        if not impacted and not changed_label:
            impacted = set(derived.keys())
        for k in impacted:
            if k in derived:
                merged[k] = derived[k]
            else:
                merged.pop(k, None)
        doc["metadata"] = merged
        doc["zones"] = _annotation_to_zone_map(ann)
        doc["_canonical_cache"] = canonical
        for k, _, multiline in _FIELDS:
            val = merged.get(k, "") or ""
            self._set_field_value(k, val, block_signals=True)
        self._refresh_raw_kie_panel(ann)
        self._resize_fields_soon()

    def _on_save_meta_clicked(self):
        if self._save_current_fields():
            self._show_saved_notice()

    def _on_viewer_field_clicked(self, field_id):
        self.pdf_viewer.set_active_field(field_id)

    def _save_current_fields(self):
        idx = self._current_doc_idx
        if idx < 0 or idx >= len(self._documents):
            return False
        meta = self._documents[idx].setdefault("metadata", {})
        for key, _, multiline in _FIELDS:
            meta[key] = self._field_value(key)
        self._form_dirty = False
        return True

    def _show_saved_notice(self):
        self._lbl_saved_notice.setText(translations.get_text("arc_saved_notice"))
        self._lbl_saved_notice.setVisible(True)
        self._save_notice_timer.start()

    def _hide_saved_notice(self):
        notice = getattr(self, "_lbl_saved_notice", None)
        if notice is not None:
            notice.setVisible(False)

    def _clear_fields(self):
        for key, _, multiline in _FIELDS:
            self._clear_field_value(key)
        self._refresh_raw_kie_panel(None)
        self._resize_fields_soon()

    def _resize_fields_soon(self):
        # Programmatic KIE updates block textChanged signals and can happen
        # before Qt has finalised the sidebar width. A few cheap passes keep
        # long fields expanded and short fields compact after every update.
        QTimer.singleShot(0, self._sweep_resize_all_fields)
        QTimer.singleShot(40, self._sweep_resize_all_fields)
        QTimer.singleShot(120, self._sweep_resize_all_fields)

    def _sweep_resize_all_fields(self):
        for w in self._field_widgets.values():
            if isinstance(w, QTextEdit):
                self._auto_resize_textedit(w)

    @staticmethod
    def _auto_resize_textedit(widget):
        doc = widget.document()
        viewport_w = widget.viewport().width()
        if viewport_w <= 0:
            viewport_w = max(120, widget.width() - 20)
        doc.setTextWidth(viewport_w)
        try:
            laid_h = doc.documentLayout().documentSize().height()
        except Exception:
            laid_h = doc.size().height()
        margins = widget.contentsMargins()
        # contentsMargins already include stylesheet padding/border. Adding
        # frameWidth again made short fields too tall and long fields prone
        # to stale clipped heights after programmatic KIE updates.
        new_h = int(laid_h + 0.999) + margins.top() + margins.bottom() + 2
        # setFixedHeight() updates the widget min/max internally; using
        # those values here would lock a field at its previous height.
        cap = 16777215
        floor = _H
        target = max(floor, min(cap, new_h))
        if widget.height() != target:
            widget.setFixedHeight(target)
            widget.updateGeometry()

    def _on_field_label_clicked(self, field_key):
        self.field_label_clicked.emit(field_key)
        idx = self._current_doc_idx
        if idx < 0 or idx >= len(self._documents):
            return
        doc = self._documents[idx]
        annotation = doc.get("annotation") or {}
        kie_label = _FORM_TO_KIE_LABEL.get(field_key)

        target = None
        for f in annotation.get("field_instances") or []:
            if f.get("label") == kie_label:
                target = f; break
        if target is not None:
            self.pdf_viewer.set_active_field(target.get("field_id", ""))
            page_idx = int(target.get("page_index", 0))
            bbox = target.get("bbox") or []
            if bbox:
                self.pdf_viewer.highlight_zone(page_idx, bbox)
            return
        zone_info = doc.get("zones", {}).get(field_key)
        if zone_info:
            bbox = zone_info.get("bbox_pdf")
            if bbox:
                self.pdf_viewer.highlight_zone(zone_info.get("page", 0), bbox)
                return
        self.pdf_viewer.clear_highlight()

    # ── fuzzy ───────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent as _QE
        if event.type() == _QE.Type.FocusIn:
            for key, w in self._field_widgets.items():
                if obj is w:
                    self._fuzzy_active_field = key
                    self._sync_viewer_active_field(key)
                    break
        elif event.type() == _QE.Type.FocusOut:
            self.pdf_viewer.clear_fuzzy_matches()
        return super().eventFilter(obj, event)

    def _sync_viewer_active_field(self, form_key):
        idx = self._current_doc_idx
        if idx < 0 or idx >= len(self._documents):
            return
        annotation = self._documents[idx].get("annotation") or {}
        kie_label = _FORM_TO_KIE_LABEL.get(form_key)
        for f in annotation.get("field_instances") or []:
            if f.get("label") == kie_label:
                self.pdf_viewer.set_active_field(f.get("field_id", ""))
                bbox = f.get("bbox")
                if bbox:
                    self.pdf_viewer.highlight_zone(int(f.get("page_index", 0)), bbox)
                return

    def _on_field_text_changed(self, field_key):
        # Real user edit (signals are blocked during programmatic
        # `_set_field_value`, so this only fires on actual typing /
        # combo selection).
        self._form_dirty = True
        self._fuzzy_active_field = field_key
        self._fuzzy_timer.start()

    def _run_fuzzy_match(self):
        idx = self._current_doc_idx
        if idx < 0 or idx >= len(self._documents):
            return
        field_key = self._fuzzy_active_field
        if not field_key:
            return
        widget = self._field_widgets.get(field_key)
        if widget is None:
            return
        text = self._field_value(field_key)
        if len(text) < 3:
            self.pdf_viewer.clear_fuzzy_matches()
            return
        doc = self._documents[idx]
        annotation = doc.get("annotation")
        if not annotation:
            return
        kie_label = _FORM_TO_KIE_LABEL.get(field_key)
        if kie_label is None:
            return
        target_field = None
        for f in annotation.get("field_instances", []) or []:
            if f.get("label") == kie_label:
                target_field = f; break
        if target_field is None:
            return
        canonical = doc.get("_canonical_cache")
        if canonical is None:
            json_path = doc.get("json_path")
            if json_path and os.path.exists(json_path):
                try:
                    import json as _json
                    with open(json_path, "r", encoding="utf-8") as f:
                        canonical = _json.load(f)
                    doc["_canonical_cache"] = canonical
                except Exception:
                    return
        if canonical is None:
            return
        try:
            from scanindex.core.digitization.fuzzy import build_candidates_for_field, fuzzy_rank
        except Exception:
            return
        candidates = build_candidates_for_field(canonical, target_field)
        matches = fuzzy_rank(candidates, text, top_k=5, min_score=55.0)
        self.pdf_viewer.set_fuzzy_matches(matches)

    def _on_fuzzy_match_picked(self, text, bbox_pdf):
        if not self._fuzzy_active_field:
            return
        widget = self._field_widgets.get(self._fuzzy_active_field)
        if widget is None:
            return
        self._set_field_value(self._fuzzy_active_field, text, block_signals=True)
        self._form_dirty = True
        if isinstance(widget, QTextEdit):
            self._resize_fields_soon()
        self.pdf_viewer.clear_fuzzy_matches()
