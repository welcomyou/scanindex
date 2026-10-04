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
import threading
import time
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QComboBox,
    QLabel, QLineEdit, QTextEdit, QPushButton,
    QListWidget, QListWidgetItem, QScrollArea, QFrame,
    QMessageBox, QSizePolicy, QSplitter, QProgressBar,
    QAbstractItemView, QHeaderView, QStackedWidget, QTreeWidget,
    QTreeWidgetItem, QDialog, QToolButton,
)
from PySide6.QtCore import Qt, QTimer, Signal, QDate, QSize, QPointF
from PySide6.QtGui import (
    QBrush, QColor, QTextOption, QIcon, QPainter, QPainterPath, QPolygonF,
    QPixmap,
)

from scanindex.ui.widgets.fuzzy_combobox import FuzzyComboBox

from scanindex.ui.theme import (
    COLOR_BG, COLOR_SURFACE, COLOR_ELEVATED, COLOR_HOVER,
    COLOR_BORDER, COLOR_BORDER_DEFAULT, COLOR_INPUT,
    COLOR_TEXT, COLOR_TEXT_SECONDARY, COLOR_TEXT_MUTED,
    COLOR_ACCENT, COLOR_ACCENT_HOVER,
    COLOR_GREEN, COLOR_GREEN_HOVER, COLOR_RED, COLOR_RED_HOVER,
    COMBOBOX_DROPDOWN_QSS,
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
# File names from the input folder are intentionally NOT validated here:
# the canonical archive name is generated at ZIP-export time from the
# dossier's identity codes (see ArchiveStep / `_export_pdf_name_for_dossier`),
# so whatever the operator's source files happen to be named is fine.


class _ReorderableDocList(QListWidget):
    """QListWidget with internal-move drag-and-drop reordering.

    Qt's built-in ``InternalMove`` mode shuffles the visible items but does
    not tell the host about the new order, so we emit ``order_changed`` after
    a drop completes. The host then mirrors the move into ``self._documents``
    and re-stamps the ordinals / ``so_thu_tu`` fields."""

    order_changed = Signal(int, int)  # (from_row, to_row)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # The vertical scroll bar would otherwise intercept drag moves near
        # the list edges; InternalMove handles scrolling itself.
        self._drop_from_row = -1

    def dropEvent(self, event):
        if self.model() is None:
            super().dropEvent(event)
            return
        # Remember the selected row before the move so we can report it.
        selected = self.selectedItems()
        from_row = self.row(selected[0]) if selected else -1
        self._drop_from_row = from_row
        # Track count: after the drop the model will have the same row count
        # (InternalMove moves, not copies), but we need the destination row.
        super().dropEvent(event)
        if from_row < 0:
            return
        # The moved item is now the current selected row.
        cur = self.currentRow()
        if cur >= 0 and cur != from_row:
            self.order_changed.emit(from_row, cur)
        self._drop_from_row = -1


# Section 1 form: derived/projected metadata the user sees and edits.
# Order matters: the two operator-controlled dossier-sequencing fields
# (Số thứ tự, Trang số) sit on top so they're visible without scrolling,
# then come the KIE-derived content fields.
_FIELDS = [
    # "Số thứ tự" = SoThuTuVanBanTrongHoSo (1-based doc ordinal). Editable so
    # the operator can reserve a slot for a doc that wasn't scanned; editing
    # re-anchors the +1 sequence for the docs that follow.
    ("so_thu_tu",        "arc_field_so_thu_tu", False),
    # "Trang số" = PMKhoSohoa ToSoTrangSo (starting page of this doc). Not a
    # KIE field — running page numbering is computed locally and edited by
    # the operator; the recompute logic cascades changes to later docs.
    ("trang_so",         "arc_field_trang_so",  False),
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

# Fields that are plain single-line numeric QLineEdit (no KIE label, no
# multiline). Rendered specially in `_build_metadata_panel`.
_NUMERIC_LINE_FIELDS = {"trang_so", "so_thu_tu"}


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


def _load_canonical_document(json_path):
    from scanindex.core.canonical_io import load_canonical, resolve_companion
    resolved = resolve_companion(json_path) if json_path else None
    if resolved is None:
        return None, None
    try:
        doc = load_canonical(resolved)
        ann = doc.get("annotations") or None
        if ann and "field_instances" in ann:
            try:
                from scanindex.core.kie.postprocess import apply_layoutlmv3_schema_postprocess
                doc["annotations"] = apply_layoutlmv3_schema_postprocess(doc, ann)
            except Exception:
                pass
        return doc, str(resolved)
    except Exception:
        return None, str(resolved)


def _load_annotation(json_path):
    doc, _resolved = _load_canonical_document(json_path)
    if not isinstance(doc, dict):
        return None
    ann = doc.get("annotations") or None
    if ann and "field_instances" in ann:
        return ann
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


class _DossierDocTree(QTreeWidget):
    """Panel trái 2 cấp cho luồng NHIỀU HỒ SƠ (mục 7 của kế hoạch):

        <MãĐD>-<Phông>-<MụcLục>-<Hồ sơ>      ← nhóm hồ sơ (header + counter)
            PDF thứ nhất                     ← tài liệu (map chỉ số phẳng)
            PDF thứ hai

    Kéo-thả CHỈ xếp lại thứ tự tài liệu TRONG CÙNG hồ sơ — phát
    ``order_changed(dossier_id, [chỉ số phẳng theo thứ tự mới])``; host phản
    chiếu vào ``self._documents`` rồi dựng lại cây (không dùng native move
    của QTreeWidget để cây luôn khớp danh sách nguồn). Double-click nhóm hồ
    sơ → ``dossier_edit_requested`` (sửa thông tin hồ sơ)."""

    current_doc_changed = Signal(int)      # chỉ số phẳng của tài liệu (-1 nếu nhóm)
    order_changed = Signal(str, list)      # (dossier_id, thứ tự mới các chỉ số phẳng)
    dossier_edit_requested = Signal(str)   # dossier_id

    _IDX_ROLE = Qt.ItemDataRole.UserRole + 2        # leaf: chỉ số phẳng
    _GROUP_ROLE = Qt.ItemDataRole.UserRole + 3      # group: dossier_id

    def __init__(self, parent=None):
        super().__init__(parent)
        # Cột 0: tên hồ sơ (+ cảnh thiếu thông tin), cột 1: done/total,
        # cột 2: nút sửa thông tin sát lề phải (yêu cầu phản hồi).
        self.setColumnCount(3)
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setExpandsOnDoubleClick(False)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        self.itemSelectionChanged.connect(self._emit_current)
        self.itemDoubleClicked.connect(self._on_double_clicked)

    # ── helpers ─────────────────────────────────────────────────────

    def leaf_index(self, item):
        if item is None or item.parent() is None:
            return -1
        return int(item.data(0, self._IDX_ROLE) or -1)

    def _emit_current(self):
        sel = self.selectedItems()
        idx = self.leaf_index(sel[0]) if sel else -1
        if idx >= 0:
            self.current_doc_changed.emit(idx)

    def _on_double_clicked(self, item, _col):
        if item.parent() is None:
            did = item.data(0, self._GROUP_ROLE)
            if did:
                self.dossier_edit_requested.emit(str(did))

    def dropEvent(self, event):
        """Kéo tài liệu trong CÙNG hồ sơ để đổi thứ tự — chặn native move,
        báo host rebuilt từ danh sách nguồn."""
        sel = self.selectedItems()
        src = sel[0] if sel else None
        src_idx = self.leaf_index(src)
        if src_idx < 0 or src is None or src.parent() is None:
            event.ignore()
            return
        group = src.parent()
        target = self.itemAt(event.position().toPoint())
        if target is None:
            event.ignore()
            return
        if target is group:
            insert_at = group.childCount()      # thả lên nhóm = cuối
        elif target.parent() is group:
            insert_at = group.indexOfChild(target)
            if (self.dropIndicatorPosition()
                    == QAbstractItemView.DropIndicatorPosition.BelowItem):
                insert_at += 1
        else:
            event.ignore()
            return  # cross-dossier drag không hỗ trợ ở phiên bản này
        orig = [self.leaf_index(group.child(i))
                for i in range(group.childCount())]
        if len(orig) <= 1:
            event.ignore()
            return
        order = list(orig)
        order.remove(src_idx)
        src_pos = orig.index(src_idx)
        pos = insert_at if insert_at <= src_pos else insert_at - 1
        pos = min(max(pos, 0), len(order))
        order.insert(pos, src_idx)
        if order == orig:
            event.ignore()  # không đổi thứ tự gì
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self.order_changed.emit(
            str(group.data(0, self._GROUP_ROLE)), order)


# Leaf ordinal trong hồ sơ (1..n) — lưu trên item để _apply_row_state dùng
# đánh số TRONG PHẠM VI hồ sơ (mục 7: không tiếp bộ đếm từ hồ sơ khác).
_DTL_ORD_ROLE = Qt.ItemDataRole.UserRole + 4


class _TreeLeafAdapter:
    """Trình bày leaf của ``_DossierDocTree`` theo API QListWidgetItem để
    ``_apply_row_state`` (và các logic hàng khác) dùng chung cho cả list
    phẳng lẫn cây đa hồ sơ — không nhân bản logic trạng thái hàng."""

    def __init__(self, item: QTreeWidgetItem):
        self._item = item

    def data(self, role):
        return self._item.data(0, role)

    def text(self):
        return self._item.text(0)

    def setText(self, text):
        self._item.setText(0, text)

    def setForeground(self, brush):
        self._item.setForeground(0, brush)

    def setFlags(self, flags):
        self._item.setFlags(flags)

    def flags(self):
        return self._item.flags()

    def setToolTip(self, *_a):
        pass  # tooltip (báo mật) gán một lần lúc dựng cây


# Icon bút chì vẽ sẵn theo màu (cache) — không dùng glyph "✎" vì glyph
# font chỉ có NÉT VIỀN, yêu cầu là icon ĐẶT phủ full màu.
_PENCIL_ICON_CACHE: dict[str, QIcon] = {}


def _pencil_icon(color: str) -> QIcon:
    """Icon bút chì ĐẶC, ngòi quay về dưới-trái (bản lật ngang của "✎").
    Vẽ bằng QPainter trong hệ toạ độ 24×24 (chuẩn Material "edit") để
    không phụ thuộc font hệ thống hay asset PNG, và sắc nét ở mọi DPI."""
    if color in _PENCIL_ICON_CACHE:
        return _PENCIL_ICON_CACHE[color]
    # 64px vật lý @ DPR 4 ⇒ mặt logical 16×16; painter vẽ theo toạ độ
    # LOGICAL sau khi setDevicePixelRatio nên scale 24→16 (không phải 24→64).
    pm = QPixmap(64, 64)
    pm.setDevicePixelRatio(4.0)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = 16 / 24
    ux, uy = 0.7071, -0.7071   # trục bút: dưới-trái → trên-phải
    nx, ny = 0.7071, 0.7071    # pháp tuyến của trục bút
    tx, ty = 3.4, 20.6          # chóp ngòi
    body_len, half_w = 17.4, 2.25
    bx, by = tx + 6.2 * ux, ty + 6.2 * uy          # chân ngòi / đầu thân
    ex, ey = tx + body_len * ux, ty + body_len * uy  # đuôi bút

    def pt(px, py):
        return QPointF(px * s, py * s)

    # Quanh bút chì: chóp ngòi → mép trên thân → đuôi → mép dưới → về chóp.
    path = QPainterPath()
    path.addPolygon(QPolygonF([
        pt(tx, ty),
        pt(bx - half_w * nx, by - half_w * ny),
        pt(ex - half_w * nx, ey - half_w * ny),
        pt(ex + half_w * nx, ey + half_w * ny),
        pt(bx + half_w * nx, by + half_w * ny),
    ]))
    path.closeSubpath()
    p.fillPath(path, QColor(color))
    p.end()
    icon = QIcon(pm)
    _PENCIL_ICON_CACHE[color] = icon
    return icon


class _IconHoverButton(QToolButton):
    """QToolButton đổi icon khi hover — QSS không đặt được màu icon, còn
    QIcon Active mode không được style toolbutton dùng lúc hover nên phải
    tự swap icon (bản xám mờ ↔ bản trắng)."""

    def __init__(self, icon_normal: QIcon, icon_hover: QIcon, parent=None):
        super().__init__(parent)
        self._icon_normal = icon_normal
        self._icon_hover = icon_hover
        self.setIcon(icon_normal)
        self.setIconSize(QSize(14, 14))

    def enterEvent(self, event):
        self.setIcon(self._icon_hover)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setIcon(self._icon_normal)
        super().leaveEvent(event)


class ArchiveStep2Kie(QWidget):
    """Bước 2 — danh sách + viewer + metadata KIE form."""

    browse_input_clicked = Signal()
    process_clicked = Signal()
    stop_clicked = Signal()
    field_label_clicked = Signal(str)
    log_message = Signal(str)
    _canonical_ready = Signal(int, int, str, object)
    # Emitted when the user opens an exported archive ZIP (via the toolbar
    # button or drag&drop) to reopen it for editing in Step 2.
    zip_dropped = Signal(str)

    def __init__(self, icons=None, parent=None, session=None):
        super().__init__(parent)
        self._icons = icons or {}
        # Session (ArchiveSession) — chỉ dùng ở chế độ NHIỀU HỒ SƠ (đọc/
        # sửa thông tin hồ sơ qua DossierInfoDialog). Luồng đơn (Bước 1/ZIP)
        # không đụng session.identity.
        self._session = session
        self._documents = []
        self._current_doc_idx = -1
        # Chế độ nhiều hồ sơ: panel trái chuyển thành cây Hồ sơ → PDF
        # (docs mang "_dossier_id"/"_dossier_label" từ phiên nhiều hồ sơ).
        self._multi_mode = False
        self._tree_items_by_idx: dict[int, object] = {}
        self._field_widgets = {}
        self._field_labels = {}
        self._lbl_so_trang_value = None  # read-only "Số trang" widget (built in _build_metadata_panel)
        self._flist_visible = True
        self._review_mode = False
        self._source_mode = "folder"   # "folder" | "step1"
        self._is_processing = False
        self._preprocess_busy = False
        self._preprocess_started_at = 0.0
        self._preprocess_total = 0
        self._preprocess_done = 0
        self._canonical_cache: dict[str, tuple[float, dict]] = {}
        self._canonical_request_gen = 0
        # Tracks whether the section-1 form has user edits the
        # operator hasn't saved into doc["metadata"] yet. Switching
        # rows while dirty triggers a Save / Discard / Cancel prompt.
        self._form_dirty = False
        # Output folder is no longer surfaced in the toolbar; the pipeline
        # always writes intermediate _ocr.pdf / _ocr.pdf.json.zst into
        # <session_temp>/_step2_kie/ and only Step 3's "Xuất hồ sơ nén"
        # button picks the real destination. We keep the value here so
        # existing set_output_folder/get_output_folder callers still work.
        self._output_folder = ""
        self._fuzzy_active_field = None
        # Form field whose KIE label is the viewer's active field. Its
        # accent border must persist even when the input loses focus to
        # the PDF viewer (lasso mode grabs focus), so we drive that border
        # manually instead of relying on the :focus pseudo-state alone.
        self._active_form_field = None
        self._fuzzy_timer = QTimer(self)
        self._fuzzy_timer.setSingleShot(True)
        self._fuzzy_timer.setInterval(300)
        self._fuzzy_timer.timeout.connect(self._run_fuzzy_match)
        self._save_notice_timer = QTimer(self)
        self._save_notice_timer.setSingleShot(True)
        self._save_notice_timer.setInterval(1000)
        self._save_notice_timer.timeout.connect(self._hide_saved_notice)
        self._canonical_ready.connect(self._on_canonical_ready)
        self._setup_ui()
        # Accept dropped exported ZIPs (reopen-for-edit round-trip).
        self.setAcceptDrops(True)

    # ── ui construction ────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_toolbar(root)

        # Same pattern as Step 1: one horizontal splitter owns the body.
        # Left and right panels are bounded but resizable; the PDF viewer
        # gets the stretch space in the middle.
        body = QSplitter(Qt.Orientation.Horizontal)
        self._body_splitter = body
        body.setChildrenCollapsible(False)
        body.setHandleWidth(4)
        body.setStyleSheet(
            f"QSplitter::handle {{ background: {COLOR_BORDER}; }}"
            f"QSplitter::handle:hover {{ background: {COLOR_ACCENT}; }}"
        )

        self._flist_panel = QWidget()
        self._flist_panel.setMinimumWidth(140)
        self._flist_panel.setMaximumWidth(560)
        self._flist_panel.setStyleSheet(f"background: {COLOR_SURFACE};")
        self._build_file_list_panel()
        body.addWidget(self._flist_panel)

        center = QWidget()
        center_l = QHBoxLayout(center)
        center_l.setContentsMargins(0, 0, 0, 0)
        center_l.setSpacing(0)

        self.pdf_viewer = KieArchiveViewer()
        self.pdf_viewer.prev_file_requested.connect(self._go_prev_file)
        self.pdf_viewer.next_file_requested.connect(self._go_next_file)
        self.pdf_viewer.dirty_changed.connect(self._on_viewer_dirty_changed)
        self.pdf_viewer.field_words_changed.connect(self._on_viewer_field_changed)
        self.pdf_viewer.field_clicked.connect(self._on_viewer_field_clicked)
        center_l.addWidget(self.pdf_viewer, 1)
        center.setMinimumWidth(360)
        body.addWidget(center)

        self._meta_panel = QWidget()
        self._meta_panel.setMinimumWidth(220)
        self._meta_panel.setMaximumWidth(620)
        self._meta_panel.setStyleSheet(f"QWidget {{ background: {COLOR_SURFACE}; }}")
        self._build_metadata_panel()
        body.addWidget(self._meta_panel)

        body.setCollapsible(0, False)
        body.setCollapsible(1, False)
        body.setCollapsible(2, False)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setStretchFactor(2, 0)
        body.setSizes([_FLIST_W, 9999, _META_W])
        root.addWidget(body, 1)
        self._build_preprocess_overlay()

        self._spinner_chars = ["⠋", "⠙", "⠹", "⠸",
                                "⠼", "⠴", "⠦", "⠧",
                                "⠇", "⠏"]
        self._spinner_idx = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(80)
        self._spinner_timer.timeout.connect(self._tick_spinner)
        self._spinner_timer.start()

    def _build_preprocess_overlay(self):
        self._preprocess_overlay = QFrame(self)
        self._preprocess_overlay.setObjectName("archiveStep2PreprocessOverlay")
        self._preprocess_overlay.setGeometry(self.rect())
        self._preprocess_overlay.setStyleSheet(f"""
            QFrame#archiveStep2PreprocessOverlay {{
                background: rgba(20, 20, 20, 205);
                border: none;
            }}
        """)
        layout = QVBoxLayout(self._preprocess_overlay)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch(1)

        card = QFrame()
        card.setFixedWidth(520)
        card.setStyleSheet(f"""
            QFrame {{
                background: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER_DEFAULT};
                border-radius: 6px;
            }}
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(10)

        self._preprocess_overlay_title = QLabel("Đang preprocess bước 2")
        self._preprocess_overlay_title.setStyleSheet(
            f"color: {COLOR_TEXT}; font-size: 14px; font-weight: 700; "
            f"font-family: {FONT_UI}; border: none;"
        )
        card_layout.addWidget(self._preprocess_overlay_title)

        self._preprocess_progress = QProgressBar()
        self._preprocess_progress.setRange(0, 100)
        self._preprocess_progress.setValue(0)
        card_layout.addWidget(self._preprocess_progress)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(5)
        self._preprocess_lbl_files = self._make_overlay_value("0/0")
        self._preprocess_lbl_current = self._make_overlay_value("-")
        self._preprocess_lbl_elapsed = self._make_overlay_value("0.0s")
        self._preprocess_lbl_stage = self._make_overlay_value("Preprocess")
        grid.addWidget(self._make_overlay_key("Preprocess"), 0, 0)
        grid.addWidget(self._preprocess_lbl_files, 0, 1)
        grid.addWidget(self._make_overlay_key("File hiện tại"), 1, 0)
        grid.addWidget(self._preprocess_lbl_current, 1, 1)
        grid.addWidget(self._make_overlay_key("Thời gian"), 2, 0)
        grid.addWidget(self._preprocess_lbl_elapsed, 2, 1)
        grid.addWidget(self._make_overlay_key("Giai đoạn"), 3, 0)
        grid.addWidget(self._preprocess_lbl_stage, 3, 1)
        card_layout.addLayout(grid)

        self._preprocess_overlay_status = QLabel("Đang chuẩn bị preprocess...")
        self._preprocess_overlay_status.setWordWrap(True)
        self._preprocess_overlay_status.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI}; border: none;"
        )
        card_layout.addWidget(self._preprocess_overlay_status)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self._btn_cancel_preprocess = QPushButton("Hủy")
        self._btn_cancel_preprocess.setFixedHeight(_H)
        self._btn_cancel_preprocess.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_cancel_preprocess.setStyleSheet(
            f"QPushButton {{ background: {COLOR_RED}; border: none; "
            f"border-radius: {_RAD}px; color: white; font-size: {_FONT}px; "
            f"font-family: {FONT_UI}; font-weight: 600; padding: 0 18px; }} "
            f"QPushButton:hover {{ background: {COLOR_RED_HOVER}; }}"
        )
        self._btn_cancel_preprocess.clicked.connect(self.stop_clicked.emit)
        buttons.addWidget(self._btn_cancel_preprocess)
        card_layout.addLayout(buttons)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(2)

        self._preprocess_overlay.hide()

    def _make_overlay_key(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI}; border: none;"
        )
        return label

    def _make_overlay_value(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {COLOR_TEXT}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI}; font-weight: 600; border: none;"
        )
        return label

    def _build_toolbar(self, parent_layout):
        bar = QFrame()
        self._toolbar = bar
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

        # "Mở ZIP" — reopen an exported archive ZIP to edit its metadata in
        # Step 2, then re-export. Mirrors the folder picker so the same
        # operator workflow covers both input shapes.
        self._btn_open_zip = self._make_browse_btn(
            translations.get_text("arc_step2_open_zip"))
        self._btn_open_zip.clicked.connect(self._on_open_zip_clicked)
        h.addWidget(self._btn_open_zip)

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

        # Nhiều hồ sơ: sửa thông tin hồ sơ đang chọn (ẩn ở luồng đơn).
        self._btn_edit_dossier = QPushButton(
            translations.get_text("arc_step2_edit_dossier"))
        self._btn_edit_dossier.setFixedHeight(_H)
        self._btn_edit_dossier.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: 1px solid {COLOR_BORDER};
                border-radius: {_RAD}px; color: {COLOR_TEXT_SECONDARY};
                font-size: {_FONT_SM}px; font-family: {FONT_UI};
                font-weight: 600; padding: 0 12px;
            }}
            QPushButton:hover {{
                background: {COLOR_ELEVATED}; color: {COLOR_TEXT};
            }}
        """)
        self._btn_edit_dossier.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_edit_dossier.clicked.connect(
            lambda: self._edit_dossier_info(None))
        self._btn_edit_dossier.setVisible(False)
        h.addWidget(self._btn_edit_dossier)

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
                translations.add_localized_combo_items(
                    w, all_display_names(), context="document_type"
                )
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
                translations.add_localized_combo_items(w, _DO_MAT_OPTIONS)
                w.setCurrentIndex(0)              # default "Thường"
                w.setFixedHeight(_H)
                w.currentTextChanged.connect(
                    lambda _t, k=key: self._on_field_text_changed(k)
                )
            elif key in _NUMERIC_LINE_FIELDS:
                # Single-line integer input (e.g. Trang số / ToSoTrangSo).
                # No KIE label mapping, no multiline. editingFinished
                # triggers the running-number recompute that cascades any
                # edit to the docs *after* this one.
                from PySide6.QtGui import QIntValidator
                w = QLineEdit()
                w.setPlaceholderText("VD: 1")
                w.setValidator(QIntValidator(0, 9999999))
                w.setFixedHeight(_H)
                w.setStyleSheet(self._field_qss("QLineEdit", invalid=False))
                w.textChanged.connect(lambda _t, k=key: self._on_field_text_changed(k))
                w.editingFinished.connect(
                    lambda k=key: self._on_numeric_field_edited(k)
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
            # QTextEdit dispatches mouse events to its viewport, not to the
            # QTextEdit itself. A field that already has focus (e.g. it was
            # the last one populated / clicked) does NOT re-emit FocusIn on
            # the next click, so we'd miss the click entirely. Installing
            # the filter on the viewport as well lets MouseButtonPress fire
            # reliably on every click into the field — which is what turns
            # on "Khoanh vùng" mode.
            if hasattr(w, "viewport"):
                w.viewport().installEventFilter(self)

            # Read-only "Số trang" (total page count of this doc's final PDF)
            # sits directly under "Trang số". Not part of `_FIELDS` so it is
            # never saved/exported; derived from the file via fitz.
            if key == "trang_so":
                so_lbl = _FieldLabel("so_trang", translations.get_text("arc_field_so_trang"))
                form_l.addWidget(so_lbl)
                self._lbl_so_trang_value = QLineEdit()
                self._lbl_so_trang_value.setReadOnly(True)
                self._lbl_so_trang_value.setFixedHeight(_H)
                self._lbl_so_trang_value.setStyleSheet(
                    self._field_qss("QLineEdit", invalid=False)
                    + f"QLineEdit {{ color: {COLOR_TEXT_SECONDARY}; background: {COLOR_SURFACE}; }}"
                )
                form_l.addWidget(self._lbl_so_trang_value)

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
        text_lbl.setProperty("_scanindex_i18n_skip", True)
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
        """Click on a raw KIE row → activate that field in the PDF viewer,
        turn on lasso mode, and scroll to its bbox.

        When the field doesn't exist yet (e.g. a ZIP-reopened PDF with no
        KIE annotation), create an empty field for this label, select it,
        and turn on edit mode so the operator can immediately drag a
        rectangle to capture its words."""
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
                # Always enter lasso mode on field pick so the operator can
                # start refining the bbox right away.
                if not self.pdf_viewer._edit_mode:
                    self.pdf_viewer._btn_edit.setChecked(True)
                return
        # No existing instance for this label — create an empty one so the
        # operator can lasso its words. Falls back to page 0.
        self.pdf_viewer._create_empty_field(kie_label, 0)
        self._on_viewer_field_changed(self.pdf_viewer._active_field_id, "created")

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

        self.doc_list = _ReorderableDocList()
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
        self.doc_list.order_changed.connect(self._on_doc_list_reordered)

        # Chế độ nhiều hồ sơ dùng cây 2 cấp Hồ sơ → PDF (cùng panel trái).
        # Cây và list KHÔNG chạy đồng thời — QStackedWidget chuyển qua lại.
        self.doc_tree = _DossierDocTree()
        self.doc_tree.setStyleSheet(f"""
            QTreeWidget {{
                background: {COLOR_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: {_RAD}px;
                outline: none;
                font-size: {_FONT_SM}px;
                font-family: {FONT_UI};
            }}
            QTreeWidget::item {{ padding: 3px 6px; }}
            QTreeWidget::item:selected {{ background: {COLOR_ACCENT}; color: #fff; }}
            QTreeWidget::item:hover:!selected {{ background: {COLOR_ELEVATED}; }}
        """)
        self.doc_tree.current_doc_changed.connect(self._on_doc_selected)
        self.doc_tree.order_changed.connect(self._on_doc_tree_reordered)
        self.doc_tree.dossier_edit_requested.connect(self._edit_dossier_info)

        self._flist_stack = QStackedWidget()
        self._flist_stack.addWidget(self.doc_list)   # trang 0: luồng đơn
        self._flist_stack.addWidget(self.doc_tree)   # trang 1: nhiều hồ sơ
        layout.addWidget(self._flist_stack, 1)

    # ── doc-list ordinal helpers ────────────────────────────────────

    @staticmethod
    def _format_doc_list_text(ordinal: int, name: str) -> str:
        """Prefix a file name with its 1-based ordinal so the operator can
        see each document's position (1, 2, 3…) and use it as a reference
        when dragging to reorder."""
        return f"{ordinal}.  {name}" if name else f"{ordinal}."

    def _refresh_doc_list_ordinals(self) -> None:
        """Re-stamp every list row's leading ordinal after a drag reorder.

        The display name is read from the item's UserRole data (the raw file
        name), never from ``item.text()`` — so re-stamping never accumulates
        duplicate "N.  " prefixes regardless of how many times it runs."""
        self.doc_list.blockSignals(True)
        try:
            for i in range(self.doc_list.count()):
                item = self.doc_list.item(i)
                if item is None:
                    continue
                name = (item.data(Qt.ItemDataRole.UserRole + 1)
                        or item.data(Qt.ItemDataRole.UserRole)
                        or "")
                doc = self._documents[i] if 0 <= i < len(self._documents) else None
                if doc is not None:
                    self._apply_row_state(item, doc)
                else:
                    item.setText(self._format_doc_list_text(i + 1, name))
        finally:
            self.doc_list.blockSignals(False)

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

    def _make_browse_btn(self, label: str = "Chọn"):
        b = QPushButton(label)
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

    # ── source mode ─────────────────────────────────────────────────

    def _on_open_zip_clicked(self):
        """Toolbar "Mở ZIP": pick an exported archive ZIP to reopen for
        editing. The actual parsing + document rebuild happens in the host
        (`main_window._on_zip_dropped`) — this widget just forwards the path."""
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, translations.get_text("arc_step2_open_zip_title"),
            "", translations.localize_text("ZIP Files (*.zip)"),
        )
        if path:
            self.zip_dropped.emit(path)

    def dragEnterEvent(self, event):
        """Accept a dropped .zip so the operator can reopen an exported
        archive ZIP straight into Step 2. Mirrors Step 1's PDF drop but for
        the round-trip artifact."""
        if self._is_processing:
            event.ignore()
            return
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                p = url.toLocalFile()
                if p and p.lower().endswith(".zip"):
                    event.acceptProposedAction()
                    return
        super().dragEnterEvent(event)

    def dropEvent(self, event):
        if self._is_processing:
            event.ignore()
            return
        if not event.mimeData().hasUrls():
            return super().dropEvent(event)
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and p.lower().endswith(".zip"):
                self.zip_dropped.emit(p)
                event.acceptProposedAction()
                return

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
        # "Mở ZIP" stays available in both modes — reopening an exported
        # ZIP is a separate entry point that resets into folder mode.

    def set_review_mode(self, enabled: bool = True, *, show_file_list: bool = False):
        """Reuse the Step 2 editor inside another workflow.

        Review mode keeps the shared PDF/KIE editor and metadata panel, but
        hides the source toolbar and optional file list because the caller
        already prepared the OCR/KIE result.
        """
        self._review_mode = bool(enabled)
        toolbar = getattr(self, "_toolbar", None)
        if toolbar is not None:
            toolbar.setVisible(not self._review_mode)
        if self._review_mode and not show_file_list:
            self._flist_panel.setVisible(False)
            self._flist_visible = False
        else:
            self._flist_panel.setVisible(True)
            self._flist_visible = True

    def get_source_mode(self) -> str:
        return self._source_mode

    # ── file navigation ─────────────────────────────────────────────

    def _go_prev_file(self):
        if self._current_doc_idx > 0:
            self._set_current_doc_view(self._current_doc_idx - 1)

    def _go_next_file(self):
        if self._current_doc_idx < len(self._documents) - 1:
            self._set_current_doc_view(self._current_doc_idx + 1)

    def _update_doc_counter(self):
        total = len(self._documents)
        idx = self._current_doc_idx
        if total <= 0:
            self._lbl_count.setText("")
        elif 0 <= idx < total:
            self._lbl_count.setText(f"{idx + 1} / {total}")
        else:
            self._lbl_count.setText(f"0 / {total}")

    def _update_file_nav(self):
        total = len(self._documents)
        idx = self._current_doc_idx
        self._update_doc_counter()
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
        self._btn_open_zip.setEnabled(not is_running)
        # Nút "Thông tin hồ sơ" vẫn enable khi đang chạy — sửa thông tin
        # hồ sơ không phụ thuộc kết quả OCR/KIE (identity đọc lúc xuất).
        self._btn_edit_dossier.setVisible(self._multi_mode)
        if not is_running:
            self.hide_preprocess_progress()

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
        self.pdf_viewer.set_file_nav_enabled(False, False)

    def set_progress(self, current, total):
        return  # progress reflected per-row

    def show_preprocess_progress(self, total: int):
        try:
            total = max(0, int(total or 0))
        except Exception:
            total = 0
        self._preprocess_busy = True
        self._preprocess_started_at = time.monotonic()
        self._preprocess_total = total
        self._preprocess_done = 0
        self._preprocess_progress.setRange(0, 100)
        self._preprocess_progress.setValue(0)
        self._preprocess_lbl_files.setText(f"0/{total}")
        self._preprocess_lbl_current.setText("-")
        self._preprocess_lbl_elapsed.setText("0.0s")
        self._preprocess_lbl_stage.setText("Preprocess")
        self._preprocess_overlay_title.setText("Đang preprocess bước 2")
        self._preprocess_overlay_status.setText(
            "Đang tiền xử lý PDF trước OCR/KIE..."
        )
        self._preprocess_overlay.setGeometry(self.rect())
        self._preprocess_overlay.raise_()
        self._preprocess_overlay.show()

    def update_preprocess_progress(
        self,
        done: int | None = None,
        total: int | None = None,
        current_file: str | None = None,
        status: str | None = None,
    ):
        if not self._preprocess_busy:
            self.show_preprocess_progress(total or len(self._documents))
        if total is not None:
            try:
                self._preprocess_total = max(0, int(total or 0))
            except Exception:
                pass
        if done is not None:
            try:
                self._preprocess_done = max(0, int(done or 0))
            except Exception:
                pass
        if self._preprocess_total:
            self._preprocess_done = min(self._preprocess_done, self._preprocess_total)
            self._preprocess_progress.setValue(
                round((self._preprocess_done / self._preprocess_total) * 100)
            )
        else:
            self._preprocess_progress.setValue(0)
        self._preprocess_lbl_files.setText(
            f"{self._preprocess_done}/{self._preprocess_total}"
        )
        self._preprocess_lbl_elapsed.setText(
            f"{time.monotonic() - self._preprocess_started_at:.1f}s"
        )
        if current_file is not None:
            base = os.path.basename(str(current_file)) or "-"
            self._preprocess_lbl_current.setText(base)
            self._preprocess_lbl_current.setToolTip(str(current_file))
        if status:
            self._preprocess_overlay_status.setText(str(status))
        self._preprocess_overlay.setGeometry(self.rect())
        self._preprocess_overlay.raise_()
        self._preprocess_overlay.show()

    def hide_preprocess_progress(self):
        self._preprocess_busy = False
        overlay = getattr(self, "_preprocess_overlay", None)
        if overlay is not None:
            overlay.hide()

    def set_documents(self, documents, default_status: str = "Pending"):
        """Populate the list. With `default_status='OCR...'` (used when
        coming from Step 1) every row starts active — load spinner shown
        until the pipeline marks it Done. Docs mang "_dossier_id" (phiên
        nhiều hồ sơ) hiển thị thành cây Hồ sơ → PDF thay vì list phẳng."""
        self.hide_preprocess_progress()
        self._documents = documents
        self._multi_mode = bool(documents) and all(
            isinstance(d, dict) and d.get("_dossier_id") for d in documents
        )
        self._flist_stack.setCurrentIndex(1 if self._multi_mode else 0)
        self._btn_edit_dossier.setVisible(self._multi_mode)
        if self._multi_mode:
            self.doc_tree.blockSignals(True)
            self.doc_tree.clear()
            self._tree_items_by_idx = {}
            self.doc_tree.blockSignals(False)
            self._rebuild_doc_tree(default_status=default_status)
            self._current_doc_idx = -1
            self._update_doc_counter()
            target_row = -1
            for i, d in enumerate(documents):
                if self._is_preview_ready(d):
                    target_row = i; break
            if target_row >= 0 and not self._is_processing:
                self._set_current_doc_view(target_row)
            if documents:
                try:
                    self._ensure_trang_so_initialised()
                except Exception:
                    pass
            return
        self.doc_list.blockSignals(True)
        self.doc_list.clear()
        for idx, doc in enumerate(documents, start=1):
            name = os.path.basename(doc.get("pdf_path", ""))
            item = QListWidgetItem()
            # Store the raw file name in UserRole so state/ordinal refreshes
            # can rebuild the visible text from a clean source instead of
            # parsing the already-displayed (prefixed) text — which used to
            # accumulate duplicate "N.  " prefixes on every refresh.
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setData(Qt.ItemDataRole.UserRole + 1, name)
            secrecy = doc.get("_secrecy") if isinstance(doc, dict) else None
            if secrecy:
                item.setToolTip(f"Văn bản mật: {secrecy}")
            self.doc_list.addItem(item)
            doc.setdefault("status", default_status)
            self._apply_row_state(item, doc)
        self.doc_list.blockSignals(False)
        self._current_doc_idx = -1
        self._update_doc_counter()
        target_row = -1
        for i, d in enumerate(documents):
            if self._is_preview_ready(d):
                target_row = i; break
        if target_row >= 0 and not self._is_processing:
            self.doc_list.setCurrentRow(target_row)
        if not self._review_mode and documents:
            self._flist_panel.setVisible(True)
            self._flist_visible = True
        # Back-compat safety net: archives from 1.1.3 (and earlier) and any
        # path that hands us docs without trang_so/so_thu_tu get the running
        # numbering seeded now, so exporting without clicking a row still
        # produces a complete workbook. (zip_roundtrip also does this, but
        # other loaders — e.g. a future CLI — may not.)
        if documents:
            try:
                self._ensure_trang_so_initialised()
            except Exception:
                pass

    def update_doc_status(self, idx: int, status: str):
        if not (0 <= idx < len(self._documents)):
            return
        self._documents[idx]["status"] = status
        if self._multi_mode:
            item = self._tree_items_by_idx.get(idx)
            if item is not None:
                self._apply_row_state(
                    _TreeLeafAdapter(item), self._documents[idx],
                    ordinal=item.data(0, _DTL_ORD_ROLE))
            self._update_group_counters()
        else:
            if not (0 <= idx < self.doc_list.count()):
                return
            item = self.doc_list.item(idx)
            if item is not None:
                self._apply_row_state(item, self._documents[idx])
        if (
            not self._is_processing
            and self._current_doc_idx < 0
            and self._is_preview_ready(self._documents[idx])
        ):
            self._set_current_doc_view(idx)

    # ── nhiều hồ sơ: cây Hồ sơ → PDF ────────────────────────────────

    def _doc_groups(self) -> list[tuple[str, list[int]]]:
        """Nhóm chỉ số phẳng theo hồ sơ, GIỮ thứ tự trong `self._documents`.
        Luồng đơn = một nhóm ẩn (toàn list) để tái dùng logic đánh số."""
        if not self._multi_mode:
            return [("", list(range(len(self._documents))))]
        groups: list[tuple[str, list[int]]] = []
        pos: dict[str, int] = {}
        for i, d in enumerate(self._documents):
            key = str(d.get("_dossier_id") or "")
            if key not in pos:
                pos[key] = len(groups)
                groups.append((key, []))
            groups[pos[key]][1].append(i)
        return groups

    def _rebuild_doc_tree(self, default_status: str = "Pending") -> None:
        """Dựng lại cây Hồ sơ → PDF từ `self._documents` (nguồn sự thật)."""
        from PySide6.QtGui import QFont
        self.doc_tree.blockSignals(True)
        try:
            self.doc_tree.clear()
            self._tree_items_by_idx = {}
            for dossier_id, idxs in self._doc_groups():
                label = str(
                    self._documents[idxs[0]].get("_dossier_label")
                    or dossier_id or "Hồ sơ")
                group = QTreeWidgetItem(["", ""])
                group.setData(0, _DossierDocTree._GROUP_ROLE, dossier_id)
                font = QFont()
                font.setBold(True)
                group.setFont(0, font)
                group.setFlags(group.flags()
                               & ~Qt.ItemFlag.ItemIsSelectable)
                self.doc_tree.addTopLevelItem(group)
                # Nút "✎" sửa thông tin sát lề phải panel + cảnh "thiếu
                # thông tin" (cam, in nghiêng) khi còn thiếu tên phông/tên
                # mục lục/tên hồ sơ — nhập đủ thì về bình thường.
                dossier = (self._session.dossier_by_id(dossier_id)
                           if self._session is not None else None)
                identity = getattr(dossier, "identity", None)
                incomplete = identity is not None and (
                    not str(getattr(identity, "ten_phong", "") or "").strip()
                    or not str(getattr(identity, "ten_muc_luc", "") or "").strip()
                    or not str(getattr(identity, "title", "") or "").strip())
                if incomplete:
                    hint = QLabel(
                        translations.get_text(
                            "arc_step2_missing_info_markup").format(
                                label,
                                translations.get_text(
                                    "arc_step2_missing_info")))
                    hint.setStyleSheet("background: transparent;")
                    # setItemWidget KHÔNG ẩn text của item — QTreeWidget vẫn
                    # tự vẽ text cột 0 bên dưới widget, nên phải để cột 0
                    # trống (nhãn nằm trong hint) nếu không sẽ bị chữ đè chữ.
                    self.doc_tree.setItemWidget(group, 0, hint)
                else:
                    group.setText(0, label)
                btn_edit = _IconHoverButton(
                    _pencil_icon("#9aa0a6"), _pencil_icon("#ffffff"))
                btn_edit.setToolTip(translations.get_text(
                    "arc_step2_edit_dossier_btn_tip"))
                btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
                btn_edit.setStyleSheet(
                    "QToolButton { border: none; background: transparent;"
                    " padding: 2px 6px; }"
                    "QToolButton:hover {"
                    " background: rgba(255,255,255,0.08);"
                    " border-radius: 4px; }")
                btn_edit.clicked.connect(
                    lambda _=False, did=dossier_id:
                    self.doc_tree.dossier_edit_requested.emit(did))
                self.doc_tree.setItemWidget(group, 2, btn_edit)
                for k, idx in enumerate(idxs, start=1):
                    doc = self._documents[idx]
                    if not doc.get("status"):
                        doc["status"] = default_status
                    name = os.path.basename(doc.get("pdf_path", ""))
                    it = QTreeWidgetItem()
                    it.setData(0, Qt.ItemDataRole.UserRole + 1, name)
                    it.setData(0, Qt.ItemDataRole.UserRole, name)
                    it.setData(0, _DossierDocTree._IDX_ROLE, idx)
                    it.setData(0, _DTL_ORD_ROLE, k)
                    secrecy = doc.get("_secrecy") if isinstance(doc, dict) else None
                    if secrecy:
                        it.setToolTip(0, f"Văn bản mật: {secrecy}")
                    group.addChild(it)
                    self._tree_items_by_idx[idx] = it
                    self._apply_row_state(_TreeLeafAdapter(it), doc,
                                          ordinal=k)
            self.doc_tree.expandAll()
        finally:
            self.doc_tree.blockSignals(False)
        self._update_group_counters()

    def _update_group_counters(self) -> None:
        """Header mỗi hồ sơ: 'done/total' theo trạng thái Done/Corrected."""
        for dossier_id, idxs in self._doc_groups():
            item = self._group_item(dossier_id)
            if item is None:
                continue
            done = sum(
                1 for i in idxs
                if self._is_preview_ready(self._documents[i])
            )
            item.setText(1, f"{done}/{len(idxs)}")

    def _group_item(self, dossier_id: str):
        for i in range(self.doc_tree.topLevelItemCount()):
            top = self.doc_tree.topLevelItem(i)
            if str(top.data(0, _DossierDocTree._GROUP_ROLE) or "") == dossier_id:
                return top
        return None

    def _tree_ordinal(self, idx: int):
        item = self._tree_items_by_idx.get(idx)
        return None if item is None else item.data(0, _DTL_ORD_ROLE)

    def _set_current_doc_view(self, idx: int) -> None:
        """Chọn tài liệu theo chỉ số phẳng trên widget đang dùng (signals
        block) rồi gọi slot tải viewer trực tiếp — không chạy 2 lần."""
        if self._multi_mode:
            it = self._tree_items_by_idx.get(idx)
            if it is not None:
                self.doc_tree.blockSignals(True)
                self.doc_tree.setCurrentItem(it)
                self.doc_tree.blockSignals(False)
        else:
            self.doc_list.blockSignals(True)
            self.doc_list.setCurrentRow(idx)
            self.doc_list.blockSignals(False)
        self._on_doc_selected(idx)

    def _select_view_only(self, idx: int) -> None:
        """Khôi phục selection thị giác khi người dùng HỦY đổi dòng (không
        tải lại viewer)."""
        if self._multi_mode:
            it = self._tree_items_by_idx.get(idx)
            if it is not None:
                self.doc_tree.blockSignals(True)
                self.doc_tree.setCurrentItem(it)
                self.doc_tree.blockSignals(False)
        else:
            self.doc_list.blockSignals(True)
            self.doc_list.setCurrentRow(idx)
            self.doc_list.blockSignals(False)

    def _on_doc_tree_reordered(self, dossier_id: str, new_order: list) -> None:
        """Kéo-thả tài liệu trong cùng hồ sơ: phản chiếu vào `self._documents`
        và đánh lại số trong phạm vi hồ sơ (mục 7: không tiếp bộ đếm từ hồ
        sơ khác)."""
        _, idxs = next(
            ((g, ii) for g, ii in self._doc_groups() if g == dossier_id),
            ("", []))
        if not idxs or sorted(new_order) != sorted(idxs):
            return
        if not self._so_thu_tu_is_contiguous(idxs):
            reply = QMessageBox.warning(
                self,
                translations.get_text("arc_step2_reorder_warn_title"),
                translations.get_text("arc_step2_reorder_warn_body"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self._rebuild_doc_tree()   # nguồn chưa đổi — vẽ lại là xong
                return
        if self._form_dirty:
            self._save_current_fields()
        cur_idx = self._current_doc_idx
        slice_docs = {i: self._documents[i] for i in idxs}
        for pos, i in enumerate(new_order):
            self._documents[idxs[pos]] = slice_docs[i]
        self._resequence_so_thu_tu()
        self._resequence_trang_so()
        self._rebuild_doc_tree()
        # Giữ selection trên tài liệu vừa kéo (nó đổi chỗ — theo dõi qua
        # identity của dict, không phải chỉ số cũ).
        if cur_idx in slice_docs:
            moved = slice_docs[cur_idx]
            for pos, i in enumerate(new_order):
                if self._documents[idxs[pos]] is moved:
                    self._current_doc_idx = idxs[pos]
                    break
        self._set_current_doc_view(self._current_doc_idx)

    def _edit_dossier_info(self, dossier_id=None) -> None:
        """Sửa thông tin MỘT hồ sơ (mục 7) — DossierInfoDialog hiện có,
        chỉ ảnh hưởng hồ sơ đó; không đụng hồ sơ khác. Cho phép sửa ngay
        cả khi OCR/KIE đang chạy: identity chỉ được đọc lúc xuất/ký ở
        Bước 3, không nằm trong đường ồn worker."""
        if not self._multi_mode:
            return
        if dossier_id is None:
            cur = self._current_doc_idx
            if 0 <= cur < len(self._documents):
                dossier_id = str(
                    self._documents[cur].get("_dossier_id") or "")
        if not dossier_id or self._session is None:
            return
        dossier = self._session.dossier_by_id(dossier_id)
        if dossier is None:
            return
        from scanindex.ui.dialogs.archive_session_dialog import (
            DossierInfoDialog,
        )
        _, idxs = next(
            ((g, ii) for g, ii in self._doc_groups() if g == dossier_id),
            ("", []))
        pages = sum(self._count_doc_pages(self._documents[i]) for i in idxs)
        dlg = DossierInfoDialog(
            initial=dossier.identity,
            seed_for_unstructured=self._session.session_id,
            parent=self,
            actual_page_count=pages or None,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        ident = dlg.result_codes()
        if ident is None:
            return
        dossier.identity = ident
        label = dossier.composite_label()
        for i in idxs:
            self._documents[i]["_dossier_label"] = label
        self._rebuild_doc_tree()
        self.log_message.emit(
            translations.get_text("arc_step2_dossier_updated").format(
                label=label))

    def _row_item_data(self, item, role):
        """Đọc data role của item ở CẢ hai loại widget: QListWidgetItem.data
        (1 tham số) và QTreeWidgetItem.data (2 tham số)."""
        try:
            return item.data(role)
        except TypeError:
            return item.data(0, role)

    def _apply_row_state(self, item: QListWidgetItem, doc: dict,
                         ordinal=None):
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
        # "Corrected" = reopened from an exported ZIP (zip_roundtrip) — the
        # doc already has a final PDF + metadata, so it behaves like "Done":
        # clickable and editable, no spinner.
        is_complete = is_done_status or status == "Corrected" or has_output
        is_active = (
            not is_complete and not is_failed
            and status not in ("Pending", "", "OCR Done")
        )

        name = (self._row_item_data(item, Qt.ItemDataRole.UserRole + 1)
                or self._row_item_data(item, Qt.ItemDataRole.UserRole)
                or self._strip_state_prefix(item.text()))
        # Ordinal prefix is derived from the row position so the displayed
        # number always matches the document's place in the list. Ở chế độ
        # nhiều hồ sơ, thứ tự tính TRONG PHẠM VI hồ sơ (ordinal truyền vào).
        if ordinal is None:
            row = self.doc_list.row(item)
            ordinal = row + 1 if row >= 0 else 0
        selectable = item.flags() | _Qt.ItemFlag.ItemIsSelectable | _Qt.ItemFlag.ItemIsEnabled
        not_selectable = item.flags() & ~_Qt.ItemFlag.ItemIsSelectable & ~_Qt.ItemFlag.ItemIsEnabled
        if is_complete:
            # KIE produced annotation output → clickable.
            item.setText(self._format_doc_list_text(ordinal, name))
            item.setForeground(QBrush(QColor(COLOR_RED if has_secrecy else COLOR_TEXT)))
            item.setFlags(selectable)
        elif is_failed:
            item.setText(self._format_doc_list_text(ordinal, name))
            item.setForeground(QBrush(QColor(COLOR_RED)))
            item.setFlags(not_selectable)
        elif is_active:
            char = self._spinner_chars[self._spinner_idx % len(self._spinner_chars)]
            item.setText(self._format_doc_list_text(ordinal, f"{char} {name}"))
            item.setForeground(QBrush(QColor(COLOR_RED if has_secrecy else COLOR_TEXT_MUTED)))
            item.setFlags(not_selectable)
        else:
            # "Pending" before KIE has produced data for this file.
            item.setText(self._format_doc_list_text(ordinal, name))
            item.setForeground(QBrush(QColor(COLOR_RED if has_secrecy else COLOR_TEXT_MUTED)))
            item.setFlags(not_selectable)

    @staticmethod
    def _is_preview_ready(doc: dict) -> bool:
        if not isinstance(doc, dict):
            return False
        status = doc.get("status", "")
        return status in ("Done", "Corrected") or bool(doc.get("json_path"))

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
        if self._multi_mode:
            for item in self._tree_items_by_idx.values():
                idx = int(item.data(0, _DossierDocTree._IDX_ROLE) or -1)
                if not (0 <= idx < len(self._documents)):
                    continue
                status = self._documents[idx].get("status", "Pending")
                if status in ("Pending", "Done", "Corrected", "OCR Done",
                              "Failed", "Done (Export Failed)"):
                    continue
                txt = item.text(0)
                for pos in range(len(txt)):
                    if txt[pos] in self._spinner_chars:
                        item.setText(0, txt[:pos] + char + txt[pos + 1:])
                        break
            return
        for i in range(self.doc_list.count()):
            item = self.doc_list.item(i)
            if not item or not (0 <= i < len(self._documents)):
                continue
            status = self._documents[i].get("status", "Pending")
            if status in ("Pending", "Done", "Corrected", "OCR Done",
                          "Failed", "Done (Export Failed)"):
                continue
            txt = item.text()
            # The active row's text is "<ordinal>.  <spinner> <name>", so the
            # spinner glyph is no longer at index 0 — scan for the first char
            # that is a spinner glyph and swap only it, preserving the ordinal
            # prefix and the file name. Without this the spinner froze once
            # ordinal prefixes were added to the displayed text.
            for pos in range(len(txt)):
                if txt[pos] in self._spinner_chars:
                    item.setText(txt[:pos] + char + txt[pos + 1:])
                    break

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
        from scanindex.core.canonical_io import companion_for_pdf, load_canonical, resolve_companion
        json_path = doc.get("json_path")
        if not json_path:
            out_pdf = doc.get("output_path") or ""
            if out_pdf:
                json_path = str(companion_for_pdf(out_pdf))

        canonical = None
        resolved_json = resolve_companion(json_path) if json_path else None
        if resolved_json is not None:
            canonical = load_canonical(resolved_json)
        ann = (canonical or {}).get("annotations") or {}
        doc["annotation"] = ann
        doc["metadata"] = dict(saved_meta) if isinstance(saved_meta, dict) else _annotation_to_metadata_form(ann)
        doc["zones"] = _annotation_to_zone_map(ann)
        if canonical is not None:
            doc["_canonical_cache"] = canonical
            self.pdf_viewer.load_canonical(str(resolved_json or json_path))
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
        self._btn_edit_dossier.setText(
            translations.get_text("arc_step2_edit_dossier"))
        self.set_source_mode(self._source_mode)
        self._lbl_saved_notice.setText(translations.get_text("arc_saved_notice"))
        for key, tr_key, _ in _FIELDS:
            if key in self._field_labels:
                self._field_labels[key].setText(translations.get_text(tr_key))
        self.pdf_viewer.update_texts()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        overlay = getattr(self, "_preprocess_overlay", None)
        if overlay is not None:
            overlay.setGeometry(self.rect())
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

    def _field_qss(self, widget_type: str, *, invalid: bool,
                    selected: bool = False) -> str:
        """Build the per-field stylesheet. When ``invalid`` is True, the
        border swaps to ``COLOR_RED`` even on focus so the alert is
        visible until the user fixes the value.

        When ``selected`` is True the accent border is forced on (not just
        via :focus). This keeps the blue outline on the active field while
        the operator clicks into the PDF viewer to lasso words — the input
        loses keyboard focus there, so :focus alone would drop the border.

        For QComboBox, append the shared ::drop-down/::down-arrow rules
        so the chevron stays visible — Qt isolates a widget's QSS from
        the global theme as soon as setStyleSheet is called.
        """
        if invalid:
            qss = (
                f"{widget_type} {{ {_TEXTAREA} border: 1px solid {COLOR_RED}; }}"
                f"{widget_type}:focus {{ border: 1px solid {COLOR_RED}; }}"
            )
        else:
            qss = (
                f"{widget_type} {{ {_TEXTAREA}"
                f" border: 1px solid {COLOR_ACCENT if selected else COLOR_BORDER}; }}"
                f"{widget_type}:focus {{ {_INPUT_FOCUS} }}"
            )
        if widget_type == "QComboBox":
            qss += COMBOBOX_DROPDOWN_QSS
        return qss

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
        attention prompt. Preserves the active-selection accent border."""
        widget = self._field_widgets.get(key)
        if widget is None:
            return
        selected = (key == self._active_form_field)
        if key == "ngay_ban_hanh":
            text = widget.text().strip() if isinstance(widget, QLineEdit) else ""
            valid = bool(text) and bool(_normalize_date_input(text))
            widget.setStyleSheet(
                self._field_qss("QLineEdit", invalid=not valid, selected=selected)
            )
        elif key == "so_van_ban":
            if isinstance(widget, QTextEdit):
                text = widget.toPlainText().strip()
            elif isinstance(widget, QLineEdit):
                text = widget.text().strip()
            else:
                return
            valid = bool(text) and bool(_NUMBER_INPUT_RE.match(text))
            widget_type = "QTextEdit" if isinstance(widget, QTextEdit) else "QLineEdit"
            widget.setStyleSheet(
                self._field_qss(widget_type, invalid=not valid, selected=selected)
            )
        elif key == "loai_van_ban":
            if not isinstance(widget, QComboBox):
                return
            text = translations.combo_value(widget).strip()
            try:
                from scanindex.core.digitization.doctype import all_display_names
                valid = bool(text) and text in set(all_display_names())
            except Exception:
                valid = bool(text)
            widget.setStyleSheet(
                self._field_qss("QComboBox", invalid=not valid, selected=selected)
            )
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
                translations.set_combo_value(widget, value)
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
            return translations.combo_value(widget).strip()
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

        # Seed Trang số (ToSoTrangSo) running numbering on first contact —
        # no-op once any doc already has a value (preserves user edits).
        self._ensure_trang_so_initialised()

        prev_row = self._current_doc_idx
        if prev_row != row and prev_row >= 0:
            if not self.confirm_unsaved_before_leave():
                self._select_view_only(prev_row)
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
                from scanindex.core.canonical_io import companion_for_pdf

                json_path = str(companion_for_pdf(out_pdf))
        from scanindex.core.canonical_io import resolve_companion
        resolved_json = resolve_companion(json_path) if json_path else None
        self._debug_log(
            f"row={row} status={doc.get('status')!r} "
            f"output_path={doc.get('output_path')!r} "
            f"json_path={json_path!r} "
            f"json_exists={resolved_json is not None}"
        )
        pdf_candidate = None
        for candidate in [doc.get("output_path"), doc.get("ocr_path"), doc.get("pdf_path")]:
            if candidate and os.path.exists(candidate):
                pdf_candidate = candidate
                break
        if pdf_candidate:
            self.pdf_viewer.load_pdf(pdf_candidate)
            QTimer.singleShot(120, lambda r=row: self._prefetch_adjacent_pdfs(r))
        else:
            self.pdf_viewer.clear()

        # Read-only "Số trang" reflects the final PDF's actual page count.
        self._update_so_trang(doc)

        selected_json = str(resolved_json or json_path) if json_path else ""
        if resolved_json is not None:
            # Companion JSON exists on disk — load it normally.
            self._request_canonical_for_selection(row, selected_json)
        else:
            # No canonical JSON yet (e.g. a ZIP-reopened PDF before KIE has
            # run). The PDF already carries a text layer, so extract it on
            # the fly into a companion JSON — that gives the viewer word/line
            # bboxes to draw in edit mode without needing a full KIE pass.
            self.pdf_viewer.clear_field_overlays()
            self._extract_text_layer_for_selection(row, pdf_candidate)
            QTimer.singleShot(30, lambda r=row: self._apply_doc_metadata(r, None, ""))

    def _prefetch_adjacent_pdfs(self, row: int):
        if row != self._current_doc_idx:
            return
        for offset in (1, -1, 2, -2):
            idx = row + offset
            if not (0 <= idx < len(self._documents)):
                continue
            doc = self._documents[idx]
            if not self._is_preview_ready(doc):
                continue
            for candidate in [doc.get("output_path"), doc.get("ocr_path"), doc.get("pdf_path")]:
                if candidate and os.path.exists(candidate):
                    try:
                        self.pdf_viewer.prefetch_pdf_first_page(candidate)
                    except Exception:
                        pass
                    break

    def _extract_text_layer_for_selection(self, row: int, pdf_path: str):
        """Build a companion canonical JSON from the PDF's text layer so the
        viewer can draw word/line bboxes in edit mode *before* KIE runs.

        This is the lightweight path for ZIP-reopened PDFs (which already
        carry a text layer but have no `.json.zst` companion until the user
        clicks "Xử lý"). It reuses `extract_digital_pdf_as_ocr` to copy the
        PDF verbatim (signature preserved) and emit a canonical JSON with
        pages[].words[].bbox. Runs on a daemon thread; the result is fed
        back through `_canonical_ready` exactly like a normal companion
        load so the viewer + bbox-edit machinery work unchanged."""
        if not pdf_path or not os.path.isfile(pdf_path):
            return
        self._canonical_request_gen += 1
        gen = self._canonical_request_gen
        json_path = pdf_path + ".json.zst"
        # Already cached from a previous selection of this row? Skip the
        # extraction.
        if json_path in self._canonical_cache:
            cached = self._canonical_cache[json_path][1]
            QTimer.singleShot(
                30,
                lambda r=row, g=gen, p=json_path, c=cached:
                    self._on_canonical_ready(r, g, p, c),
            )
            return

        def _worker():
            try:
                from scanindex.core.pdf.text_extractor import (
                    extract_digital_pdf_as_ocr,
                )
                # extract_digital_pdf_as_ocr copies the input PDF to
                # output_path and writes the companion JSON next to it. We
                # only need the JSON, so point output at a throwaway copy and
                # move its `.json.zst` companion next to the real PDF.
                tmp_out = pdf_path + ".__native_extract__.pdf"
                ok, err = extract_digital_pdf_as_ocr(
                    pdf_path, tmp_out,
                    source_document_path=pdf_path,
                    canonical_profile="layoutlmv3_runtime",
                )
                tmp_json = tmp_out + ".json.zst"
                if ok and os.path.isfile(tmp_json):
                    try:
                        if os.path.exists(json_path):
                            os.remove(json_path)
                        os.replace(tmp_json, json_path)
                    except OSError:
                        pass
                try:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                except OSError:
                    pass
                if not ok:
                    self._canonical_ready.emit(row, gen, json_path, None)
                    return
                canonical, resolved = _load_canonical_document(json_path)
                self._canonical_ready.emit(
                    row, gen, str(resolved or json_path), canonical
                )
            except Exception:
                self._canonical_ready.emit(row, gen, json_path, None)

        threading.Thread(
            target=_worker, daemon=True,
            name=f"step2-native-extract-{row}",
        ).start()

    def _request_canonical_for_selection(self, row: int, json_path: str):
        self._canonical_request_gen += 1
        gen = self._canonical_request_gen
        try:
            mtime = os.path.getmtime(json_path)
        except OSError:
            mtime = -1.0
        cached = self._canonical_cache.get(json_path)
        if cached is not None and cached[0] == mtime:
            canonical = cached[1]
            QTimer.singleShot(
                30,
                lambda r=row, g=gen, p=json_path, c=canonical:
                    self._on_canonical_ready(r, g, p, c),
            )
            return

        doc = self._documents[row] if 0 <= row < len(self._documents) else {}
        cached_doc = doc.get("_canonical_cache") if isinstance(doc, dict) else None
        cached_path = doc.get("_canonical_cache_path") if isinstance(doc, dict) else None
        if isinstance(cached_doc, dict) and cached_path == json_path:
            QTimer.singleShot(
                30,
                lambda r=row, g=gen, p=json_path, c=cached_doc:
                    self._on_canonical_ready(r, g, p, c),
            )
            return

        def _worker():
            canonical, resolved = _load_canonical_document(json_path)
            self._canonical_ready.emit(row, gen, str(resolved or json_path), canonical)

        threading.Thread(
            target=_worker,
            daemon=True,
            name=f"step2-canonical-load-{row}",
        ).start()

    def _on_canonical_ready(self, row: int, gen: int, json_path: str, canonical):
        if gen != self._canonical_request_gen or row != self._current_doc_idx:
            return
        if isinstance(canonical, dict):
            try:
                mtime = os.path.getmtime(json_path)
            except OSError:
                mtime = -1.0
            self._canonical_cache[json_path] = (mtime, canonical)
            while len(self._canonical_cache) > 8:
                self._canonical_cache.pop(next(iter(self._canonical_cache)))
            try:
                self.pdf_viewer.load_canonical_data(json_path, canonical)
            except AttributeError:
                self.pdf_viewer.load_canonical(json_path)
        else:
            self.pdf_viewer.clear_field_overlays()
        self._apply_doc_metadata(row, canonical, json_path)

    def _apply_doc_metadata(self, row: int, canonical, json_path: str):
        if row != self._current_doc_idx or not (0 <= row < len(self._documents)):
            return
        doc = self._documents[row]
        annotation = doc.get("annotation") if isinstance(doc, dict) else None
        if not annotation and isinstance(canonical, dict):
            annotation = canonical.get("annotations") or None
        if annotation and "field_instances" in annotation:
            doc["annotation"] = annotation
            if isinstance(canonical, dict):
                doc["_canonical_cache"] = canonical
                doc["_canonical_cache_path"] = json_path
            # Derive form metadata from the annotation only when the
            # doc doesn't already carry user-edited values. Re-running
            # the projection on every selection clobbers anything the
            # operator typed between row clicks.
            existing_meta = doc.get("metadata") or {}
            derived = _annotation_to_metadata_form(annotation)
            if existing_meta:
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
        for key, _, _multiline in _FIELDS:
            val = meta.get(key, "") or ""
            self._set_field_value(key, val, block_signals=True)
        self._refresh_raw_kie_panel(doc.get("annotation"))
        self._resize_fields_soon()

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
        if not changed_label and isinstance(op, str) and op.startswith("deleted:"):
            changed_label = op.split(":", 1)[1].strip()
        doc["annotation"] = ann
        derived = _annotation_to_metadata_form(ann)
        existing = doc.get("metadata") or {}
        merged = dict(existing)
        impacted = _metadata_keys_impacted_by_kie_label(changed_label)
        if not impacted and not changed_label:
            impacted = set(derived.keys())
        # Preserve user / metadata-loaded values until the operator actually
        # captures words for the field:
        #  - op == "created" → a freshly-created EMPTY field (e.g. clicking
        #    a raw-KIE row on a reopened ZIP that has metadata but no
        #    annotation yet). It carries no content, so it must NOT wipe
        #    the metadata that came from the file or from prior typing.
        #  - otherwise, when the field now resolves to empty text, keep the
        #    existing value rather than blanking it — last-action-wins only
        #    applies to real content changes, not to a stray empty result.
        skip_metadata_overwrite = (op == "created")
        for k in impacted:
            new_val = derived.get(k)
            if new_val:
                merged[k] = new_val
            elif not skip_metadata_overwrite and k not in derived:
                # Label legitimately dropped from the annotation (e.g.
                # field deleted) → remove its derived key.
                merged.pop(k, None)
            # else: empty value from a freshly-created field, or a label
            # that simply has no text yet → keep whatever the user had.
        doc["metadata"] = merged
        doc["zones"] = _annotation_to_zone_map(ann)
        doc["_canonical_cache"] = canonical
        for k, _, multiline in _FIELDS:
            val = merged.get(k, "") or ""
            self._set_field_value(k, val, block_signals=True)
        # Re-assert the active-field accent border — _set_field_value runs
        # _refresh_validity for date/number/doctype which rebuilds the QSS
        # and would otherwise drop the "selected" outline.
        if self._active_form_field:
            self._refresh_field_border(self._active_form_field)
        # If the active field was just deleted, clear the outline.
        if isinstance(op, str) and op.startswith("deleted:") and self._active_form_field:
            impacted_deleted = _metadata_keys_impacted_by_kie_label(
                op.split(":", 1)[1].strip()
            )
            if self._active_form_field in impacted_deleted:
                prev_active = self._active_form_field
                self._active_form_field = None
                self._refresh_field_border(prev_active)
        self._refresh_raw_kie_panel(ann)
        self._resize_fields_soon()

    def _on_save_meta_clicked(self):
        if self._save_current_fields():
            self._show_saved_notice()

    def _on_viewer_field_clicked(self, field_id):
        self.pdf_viewer.set_active_field(field_id)
        # User picked the field by clicking its bbox directly on the PDF.
        # Mirror the active KIE label back onto the matching form input so
        # that input keeps its accent border even though the PDF now holds
        # focus.
        idx = self._current_doc_idx
        if 0 <= idx < len(self._documents):
            ann = (self._documents[idx].get("annotation") or {})
            kie_label = _kie_label_for_field_id(ann, field_id)
            if kie_label:
                form_keys = _metadata_keys_impacted_by_kie_label(kie_label)
                # Pick the primary (first) form key for the outline.
                if form_keys:
                    self._set_active_form_field(next(iter(form_keys)))

    def _save_current_fields(self):
        idx = self._current_doc_idx
        if idx < 0 or idx >= len(self._documents):
            return False
        meta = self._documents[idx].setdefault("metadata", {})
        for key, _, multiline in _FIELDS:
            meta[key] = self._field_value(key)
        self._form_dirty = False
        return True

    # ── Trang số (ToSoTrangSo) running-number recompute ───────────────
    #
    # "Trang số" is the page each document starts on. The operator can
    # edit any doc's value; the edit re-anchors the running sequence so
    # docs *after* it shift automatically (doc[i+1].start = doc[i].start
    # + doc[i].page_count). Logic mirrors metadata_export.compute_trang_so
    # but is applied live to the GUI metadata + form widget.

    def _count_doc_pages(self, doc: dict) -> int:
        """Return the page count of one doc's final PDF, 0 if unknown."""
        for k in ("output_path", "ocr_path", "pdf_path"):
            p = doc.get(k) or ""
            if p and os.path.isfile(p):
                try:
                    import fitz
                    with fitz.open(str(p)) as f:
                        return int(f.page_count)
                except Exception:
                    return 0
        return 0

    def _update_so_trang(self, doc) -> None:
        """Refresh the read-only 'Số trang' widget from the selected doc's file."""
        if self._lbl_so_trang_value is None:
            return
        n = self._count_doc_pages(doc) if doc else 0
        self._lbl_so_trang_value.setText(str(n) if n else "")


    def _on_numeric_field_edited(self, key: str) -> None:
        """editingFinished slot for numeric single-line fields. Saves the
        typed value then cascades the change to the docs that follow."""
        if key == "trang_so":
            idx = self._current_doc_idx
            if idx < 0 or idx >= len(self._documents):
                return
            meta = self._documents[idx].setdefault("metadata", {})
            meta[key] = self._field_value(key)
            self._recompute_trang_so_from(idx)
        elif key == "so_thu_tu":
            idx = self._current_doc_idx
            if idx < 0 or idx >= len(self._documents):
                return
            meta = self._documents[idx].setdefault("metadata", {})
            meta[key] = self._field_value(key)
            self._recompute_so_thu_tu_from(idx)

    def _group_end(self, anchor_idx: int) -> int:
        """Chỉ số NGẮT của nhóm hồ sơ chứa anchor (cascade đánh số không
        tràn sang hồ sơ khác — mục 7)."""
        for _gid, idxs in self._doc_groups():
            if anchor_idx in idxs:
                return idxs[-1] + 1
        return len(self._documents)

    def _recompute_trang_so_from(self, anchor_idx: int) -> None:
        """Recompute trang_so for docs after `anchor_idx` using each doc's
        page count. Docs before/including the anchor keep their values.
        Cascade dừng ở cuối CÙNG hồ sơ."""
        n = self._group_end(anchor_idx)
        if anchor_idx < 0 or anchor_idx >= n:
            return
        try:
            cur = int(
                (self._documents[anchor_idx].get("metadata", {}) or {})
                .get("trang_so", "") or 0
            )
        except (ValueError, TypeError):
            return
        for i in range(anchor_idx + 1, n):
            prev_pages = self._count_doc_pages(self._documents[i - 1])
            cur = cur + max(1, prev_pages)
            meta = self._documents[i].setdefault("metadata", {})
            meta["trang_so"] = str(cur)
            # If that doc is currently displayed, refresh its field widget
            # without disturbing the operator's focus on other rows.
            if i == self._current_doc_idx:
                self._set_field_value("trang_so", str(cur), block_signals=True)

    def _recompute_so_thu_tu_from(self, anchor_idx: int) -> None:
        """Recompute so_thu_tu for docs after `anchor_idx` as a +1
        sequence. Docs before/including the anchor keep their values.
        Cascade dừng ở cuối CÙNG hồ sơ."""
        n = self._group_end(anchor_idx)
        if anchor_idx < 0 or anchor_idx >= n:
            return
        try:
            cur = int(
                (self._documents[anchor_idx].get("metadata", {}) or {})
                .get("so_thu_tu", "") or 0
            )
        except (ValueError, TypeError):
            return
        for i in range(anchor_idx + 1, n):
            cur = cur + 1
            meta = self._documents[i].setdefault("metadata", {})
            meta["so_thu_tu"] = str(cur)
            if i == self._current_doc_idx:
                self._set_field_value("so_thu_tu", str(cur), block_signals=True)

    def _so_thu_tu_values(self, indices=None) -> list[int]:
        """Collect each doc's ``so_thu_tu`` as ints; blanks become 0 so they
        are clearly distinguishable from a valid 1. ``indices`` giới hạn
        phạm vi (trong cùng hồ sơ ở chế độ nhiều hồ sơ)."""
        out: list[int] = []
        for i in (indices if indices is not None
                  else range(len(self._documents))):
            d = self._documents[i]
            raw = str((d.get("metadata", {}) or {}).get("so_thu_tu", "")).strip()
            try:
                out.append(int(raw))
            except (ValueError, TypeError):
                out.append(0)
        return out

    def _so_thu_tu_is_contiguous(self, indices=None) -> bool:
        """True when the documents' so_thu_tu values form a complete
        1, 2, 3 … N sequence with no gaps and no blanks. A contiguous
        sequence means the operator can drag-reorder freely: the new order
        just re-stamps 1..N. A gap (e.g. 1,2,4,5,6,7 because slot 3 was
        skipped for a secret doc) means dragging would clobber that reserved
        slot, so we warn first. Ở chế độ nhiều hồ sơ kiểm tra TRONG PHẠM VI
        hồ sơ (mỗi hồ sơ có bộ 1..N riêng)."""
        vals = self._so_thu_tu_values(indices)
        if not vals:
            return True
        if any(v <= 0 for v in vals):
            return False
        return sorted(vals) == list(range(1, len(vals) + 1))

    def _resequence_so_thu_tu(self) -> None:
        """Stamp every doc's so_thu_tu with its 1-based list position. Used
        after a drag reorder that the operator confirmed. Nhiều hồ sơ: đánh
        số riêng từng hồ sơ (1..n trong hồ sơ)."""
        for _gid, idxs in self._doc_groups():
            for k, i in enumerate(idxs, start=1):
                meta = self._documents[i].setdefault("metadata", {})
                meta["so_thu_tu"] = str(k)
                if i == self._current_doc_idx:
                    self._set_field_value("so_thu_tu", str(k),
                                          block_signals=True)

    def _resequence_trang_so(self) -> None:
        """Recompute trang_so for every doc as a cumulative running page
        numbering based on the NEW physical order. Doc 0 starts at 1; each
        subsequent doc starts at the previous doc's trang_so + that doc's
        page count. Used after a drag reorder so the starting-page column
        stays consistent with the new document sequence. Nhiều hồ sơ: bộ đếm
        trang TÍNH RIÊNG từng hồ sơ, bắt đầu từ 1 trong hồ sơ (mục 7)."""
        if not self._documents:
            return
        for _gid, idxs in self._doc_groups():
            cur = 1
            for i in idxs:
                meta = self._documents[i].setdefault("metadata", {})
                meta["trang_so"] = str(cur)
                if i == self._current_doc_idx:
                    self._set_field_value("trang_so", str(cur),
                                          block_signals=True)
                pages = self._count_doc_pages(self._documents[i])
                cur = cur + max(1, pages)

    def _on_doc_list_reordered(self, from_row: int, to_row: int) -> None:
        """Handle a drag-and-drop reorder inside the document list.

        If the current so_thu_tu sequence is contiguous (1..N) the move is
        silent: we mirror it into ``self._documents`` and re-stamp the
        ordinals. If it has gaps (a slot was skipped, e.g. for a secret doc
        not scanned), we warn the operator that dragging will reset the
        numbering to a flat 1..N and ask for confirmation."""
        n = len(self._documents)
        if not (0 <= from_row < n and 0 <= to_row < n) or from_row == to_row:
            return
        if not self._so_thu_tu_is_contiguous():
            reply = QMessageBox.warning(
                self,
                translations.get_text("arc_step2_reorder_warn_title"),
                translations.get_text("arc_step2_reorder_warn_body"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                # Restore the previous order by reloading the list from the
                # untouched ``self._documents`` (the QListWidget moved items,
                # but ``self._documents`` is the source of truth).
                self.set_documents(self._documents)
                return
        # Mirror the move into the underlying document list.
        # Persist any pending field edits first so the moved doc's metadata
        # (subject, signer, …) is not lost when we re-stamp numbering below.
        if self._form_dirty:
            self._save_current_fields()
        doc = self._documents.pop(from_row)
        self._documents.insert(to_row, doc)
        # Re-stamp ordinals on the list rows and re-sequence the two
        # dossier-sequencing fields: so_thu_tu (1, 2, 3…) and trang_so
        # (cumulative starting page based on each doc's page count).
        self._refresh_doc_list_ordinals()
        self._resequence_so_thu_tu()
        self._resequence_trang_so()
        # Keep the selection on the moved row so the operator sees it land.
        # Setting _current_doc_idx first makes _on_doc_selected see the move
        # as a no-op (prev == new), avoiding a confirm-unsaved prompt.
        self._current_doc_idx = to_row
        self.doc_list.blockSignals(True)
        self.doc_list.setCurrentRow(to_row)
        self.doc_list.blockSignals(False)
        self._on_doc_selected(to_row)

    def _ensure_trang_so_initialised(self) -> None:
        """First-run seeding: if no doc has a trang_so yet, assign the
        running numbering starting at 1. Also seeds so_thu_tu (1, 2, 3…).
        Called after KIE finishes so the export never ships empty Trang số
        / Số thứ tự cells for VB that exist. Nhiều hồ sơ: seed RIÊNG từng
        hồ sơ — STT/trang số không tiếp bộ đếm từ hồ sơ trước (mục 7)."""
        n = len(self._documents)
        if n == 0:
            return
        from scanindex.core.digitization.metadata_export import (
            compute_trang_so, compute_so_thu_tu,
        )
        changed = False
        for _gid, idxs in self._doc_groups():
            need_trang = not any(
                str((self._documents[i].get("metadata", {}) or {})
                    .get("trang_so", "")).strip()
                for i in idxs
            )
            need_stt = not any(
                str((self._documents[i].get("metadata", {}) or {})
                    .get("so_thu_tu", "")).strip()
                for i in idxs
            )
            if not (need_trang or need_stt):
                continue  # both already populated — don't clobber user edits
            page_counts = [self._count_doc_pages(self._documents[i])
                           for i in idxs]
            trang = compute_trang_so(page_counts, first_default=1)
            stt = compute_so_thu_tu(len(idxs), first_default=1)
            for k, i in enumerate(idxs):
                meta = self._documents[i].setdefault("metadata", {})
                if need_trang:
                    meta["trang_so"] = str(trang[k])
                if need_stt:
                    meta["so_thu_tu"] = str(stt[k])
            changed = True
        # Refresh the visible row's widgets if their metadata was just seeded.
        if changed and 0 <= self._current_doc_idx < n:
            cur_meta = self._documents[self._current_doc_idx].get("metadata", {}) or {}
            self._set_field_value(
                "trang_so", str(cur_meta.get("trang_so", "")),
                block_signals=True)
            self._set_field_value(
                "so_thu_tu", str(cur_meta.get("so_thu_tu", "")),
                block_signals=True)

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
        self._update_so_trang(None)
        self._refresh_raw_kie_panel(None)
        self._resize_fields_soon()
        # Row switch — drop the active-field outline from the previous doc.
        self._set_active_form_field(None)

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
        """Click on a Section-1 field label → mirror the raw-KIE row behaviour:
        activate the matching field in the PDF viewer, turn on "Khoanh vùng"
        mode, scroll to its bbox, and if no field_instance exists yet for this
        label create an empty one so the operator can lasso its words.

        Form keys with no KIE mapping (so_thu_tu, trang_so, ngon_ngu) are
        ignored — they carry no bbox to edit."""
        self.field_label_clicked.emit(field_key)
        idx = self._current_doc_idx
        if idx < 0 or idx >= len(self._documents):
            return
        kie_label = _FORM_TO_KIE_LABEL.get(field_key)
        if not kie_label:
            # No bbox-bound KIE label for this form field — nothing to lasso.
            self.pdf_viewer.clear_highlight()
            return
        # Delegate to the same code path used by the raw-KIE panel so both
        # entry points stay in lockstep (activate-or-create + lasso on).
        self._on_raw_kie_clicked(kie_label)

    # ── fuzzy ───────────────────────────────────────────────────────

    def _kick_lasso_for_field(self, key: str):
        """Turn on "Khoanh vùng" (lasso) mode and make sure a field_instance
        exists for the form field `key`, so the operator's next drag assigns
        words to the right KIE label. No-op when `key` has no KIE mapping
        (so_thu_tu / trang_so / ngon_ngu) — those carry no bbox to edit."""
        kie_label = _FORM_TO_KIE_LABEL.get(key)
        if not kie_label:
            return
        self._set_active_form_field(key)
        self._on_raw_kie_clicked(kie_label)

    def _set_active_form_field(self, key: str | None):
        """Mark `key` as the form field bound to the viewer's active KIE
        field, forcing its accent border to stay visible even after the
        input loses keyboard focus to the PDF lasso. Restores the previous
        field's normal styling first so only one field shows the active
        outline at a time."""
        prev = self._active_form_field
        if prev == key:
            return
        self._active_form_field = key
        if prev and prev in self._field_widgets and prev != key:
            self._refresh_field_border(prev)
        if key and key in self._field_widgets:
            self._refresh_field_border(key)

    def _refresh_field_border(self, key: str):
        """Re-apply the per-field stylesheet, honouring both validity and
        the active-selection outline."""
        widget = self._field_widgets.get(key)
        if widget is None:
            return
        if isinstance(widget, QComboBox):
            wt = "QComboBox"
        elif isinstance(widget, QTextEdit):
            wt = "QTextEdit"
        else:
            wt = "QLineEdit"
        # Re-derive invalid from current content for the fields that use it;
        # other fields stay valid (their red-border logic is self-contained).
        invalid = False
        if key == "ngay_ban_hanh" and isinstance(widget, QLineEdit):
            invalid = not (widget.text().strip()
                           and _normalize_date_input(widget.text()))
        elif key == "so_van_ban":
            txt = (widget.toPlainText() if isinstance(widget, QTextEdit)
                   else widget.text())
            invalid = not (txt.strip() and _NUMBER_INPUT_RE.match(txt))
        elif key == "loai_van_ban" and isinstance(widget, QComboBox):
            try:
                from scanindex.core.digitization.doctype import all_display_names
                invalid = translations.combo_value(widget).strip() not in set(all_display_names())
            except Exception:
                invalid = False
        selected = (key == self._active_form_field)
        widget.setStyleSheet(self._field_qss(wt, invalid=invalid, selected=selected))

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent as _QE
        if event.type() == _QE.Type.FocusIn:
            # Catches focus gained via Tab / popup / programmatic setFocus.
            # A click into an *already-focused* QTextEdit does NOT emit
            # FocusIn again — that case is handled by MouseButtonPress below.
            for key, w in self._field_widgets.items():
                if obj is w:
                    self._fuzzy_active_field = key
                    if event.reason() == Qt.FocusReason.MouseFocusReason:
                        self._kick_lasso_for_field(key)
                    else:
                        self._sync_viewer_active_field(key)
                    break
        elif event.type() == _QE.Type.MouseButtonPress:
            # QTextEdit routes mouse events to its viewport. Map the
            # viewport back to its owning field widget so we can trigger
            # lasso mode on every click — even when the field already had
            # focus (no FocusIn would fire in that case).
            for key, w in self._field_widgets.items():
                vp = w.viewport() if hasattr(w, "viewport") else None
                if obj is w or (vp is not None and obj is vp):
                    self._fuzzy_active_field = key
                    self._kick_lasso_for_field(key)
                    break
        elif event.type() == _QE.Type.FocusOut:
            self.pdf_viewer.clear_fuzzy_matches()
        return super().eventFilter(obj, event)

    def _sync_viewer_active_field(self, form_key):
        """Activate the viewer's field for `form_key`. Returns True when a
        matching field_instance was found and activated; False when there is
        no field to activate (caller should not turn on lasso mode, since the
        viewer's _on_edit_toggled would otherwise auto-pick the first field
        of the document — wrong target for a form field with no bbox yet)."""
        idx = self._current_doc_idx
        if idx < 0 or idx >= len(self._documents):
            return False
        annotation = self._documents[idx].get("annotation") or {}
        kie_label = _FORM_TO_KIE_LABEL.get(form_key)
        if not kie_label:
            return False
        for f in annotation.get("field_instances") or []:
            if f.get("label") == kie_label:
                self.pdf_viewer.set_active_field(f.get("field_id", ""))
                bbox = f.get("bbox")
                if bbox:
                    self.pdf_viewer.highlight_zone(int(f.get("page_index", 0)), bbox)
                return True
        return False

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
            from scanindex.core.canonical_io import load_canonical, resolve_companion
            json_path = doc.get("json_path")
            resolved = resolve_companion(json_path) if json_path else None
            if resolved is not None:
                try:
                    canonical = load_canonical(resolved)
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
