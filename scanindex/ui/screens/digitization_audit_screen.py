"""Thẩm định số hóa — công cụ CHỈ ĐỌC kiểm tra chất lượng file PDF số hóa.

Giao diện giống "Đổi tên theo cây thư mục": chọn thư mục gốc, cây 4 cấp
CSDL_SOHOA với icon màu tương tự; KHÔNG đổi tên / kéo thả / sửa gì.

  * Bấm thư mục → bên phải hiện Số tài liệu / Số trang / Dung lượng
    (chỉ tính file PDF; đếm nền, xem
    ``scanindex.core.digitization_audit.scan_tree_stats``).
  * Bấm PDF → viewer + 5 thẻ chỉ tiêu 2 dòng (Scan màu, DPI, Độ nén, Đã
    OCR, Ký số) kèm chip tổng kết; màu thống nhất: ĐẠT = xanh, KHÔNG ĐẠT
    quan trọng (DPI, Scan màu) = đỏ, ít quan trọng (Độ nén, Đã OCR, Ký số
    chưa ký) = vàng, chưa xác định = xám. Ký số là tùy chọn — không tính
    vào chip tổng kết / dấu ❗.
  * Dưới thẻ: một hàng mô tả dài (chế độ màu, bộ nén, số trang, dung lượng,
    PDF/A, ghi chú kết luận, cảnh báo ⚠) — chỉ mô tả file.
  * Chạy nền: thẩm định tất cả PDF dưới thư mục gốc — file trượt tiêu chí
    nặng (Scan màu HOẶC DPI) được gắn dấu " !" sau tên + tô đỏ trên cây.
  * Không cache: mỗi lần quét / chọn file đều đọc lại để phản ánh hiện
    trạng (file thêm / sửa / xóa luôn được đánh giá lại).
"""
from __future__ import annotations

import html
import json
import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QProgressBar,
    QPushButton, QSplitter, QStackedWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from scanindex.core import digitization_audit as da
from scanindex.core import rename_tree as rt
from scanindex.infra import translations
from scanindex.ui.screens.rename_tree_screen import (
    _FOLDER_FALLBACK_COLOR, _LEVEL_COLOR, _dossier_icon, _file_icon,
    _folder_icon,
)
from scanindex.ui.screens.screen_base import ScreenContent
from scanindex.ui.theme import (
    ACTIVE_THEME, COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG, COLOR_BORDER,
    COLOR_PANEL, COLOR_SURFACE, COLOR_TEXT, COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY, FONT_MONO, FONT_UI, RADIUS_LG, RADIUS_MD, SP,
)
from scanindex.ui.widgets.pdf_viewer_widget import PdfViewerWidget

try:
    from scanindex.infra.paths import get_base_dir
except Exception:
    def get_base_dir():
        return os.getcwd()

_SETTINGS_FILE = os.path.join(
    get_base_dir(), "config", "digitization_audit_settings.json")

_ROLE_REL = Qt.ItemDataRole.UserRole          # path posix tương đối root
_ROLE_LEVEL = Qt.ItemDataRole.UserRole + 1    # rt.Level | None
_ROLE_ISDIR = Qt.ItemDataRole.UserRole + 2
_ROLE_LOADED = Qt.ItemDataRole.UserRole + 3
_ROLE_BADGE = Qt.ItemDataRole.UserRole + 4    # nhãn cột Loại chưa dịch
_ROLE_TIP = Qt.ItemDataRole.UserRole + 5
_ROLE_BASENAME = Qt.ItemDataRole.UserRole + 6  # tên file không kèm dấu ❗

_DUMMY_TEXT = "\u2026"  # giữ chỗ hiện mũi tên bung trước khi lazy populate

# Màu kết luận (theo theme): fg = chữ giá trị + chấm trạng thái, accent =
# viền trái thẻ. Nền thẻ luôn thống nhất — chỉ màu trạng thái thay đổi:
# ĐẠT = xanh; KHÔNG ĐẠT quan trọng (DPI, scan màu) = đỏ; KHÔNG ĐẠT ít quan
# trọng (độ nén, OCR) = vàng; Ký số chưa ký = xám trung tính (tùy chọn,
# không phải lỗi); chưa xác định = xám.
if ACTIVE_THEME == "light":
    _STATUS_LOOK = {
        "pass": ("#15803d", "#16a34a"),
        "warn": ("#92400e", "#d97706"),
        "fail": ("#b91c1c", "#dc2626"),
        "na":   (COLOR_TEXT_SECONDARY, COLOR_BORDER),
    }
else:
    _STATUS_LOOK = {
        "pass": ("#4ade80", "#4ade80"),
        "warn": ("#fbbf24", "#fbbf24"),
        "fail": ("#f87171", "#f87171"),
        "na":   (COLOR_TEXT_SECONDARY, COLOR_BORDER),
    }
_COLOR_WARN_FG = _STATUS_LOOK["warn"][0]
_COLOR_FAIL_FG = _STATUS_LOOK["fail"][0]


# --------------------------------------------------------------------------- #
# Worker nền
# --------------------------------------------------------------------------- #

class _TreeAuditWorker(QThread):
    """Một lượt đi duy nhất trên cây: quét nhanh từng PDF (trang, ảnh,
    DPI, màu, chữ ký — bỏ text/filter) để gắn dấu ❗, đồng thời tích lũy
    Số tài liệu / Số trang / Dung lượng theo subtree (chỉ tính file PDF,
    bỏ qua file khác). Mỗi lần quét đều đọc lại file (không cache) để
    luôn phản ánh hiện trạng mới nhất."""

    progress = Signal(int, int)          # (đã xét, tổng PDF) → progress bar
    stats_step = Signal(int, int)        # (tài liệu, trang) đang đếm
    marked = Signal(str, object, bool)   # (rel, PdfAuditResult, không đạt?)
    stats_done = Signal(object)          # FolderStats
    done = Signal(int)                   # tổng số file không đạt

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self._root = root
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        # Lượt 1 (rẻ, không mở PDF): scandir liệt kê PDF + cộng dung lượng
        # PDF (bỏ qua file khác). entry.stat() lấy size từ dir-entry nên
        # gần như miễn phí.
        pdfs: list[Path] = []
        dir_rels: set[str] = set()
        direct_size: dict[str, int] = {}   # rel → dung lượng file trực tiếp

        def walk(dirpath: Path, rel: str):
            dir_rels.add(rel)
            size = 0
            try:
                entries = sorted(os.scandir(dirpath),
                                 key=lambda e: e.name.lower())
            except OSError:
                entries = []
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        child_rel = f"{rel}/{e.name}" if rel else e.name
                        walk(Path(e.path), child_rel)
                    elif e.name.lower().endswith(".pdf"):
                        # Chỉ thống kê file PDF — dung lượng cũng chỉ tính PDF.
                        pdfs.append(Path(e.path))
                        try:
                            size += e.stat().st_size
                        except OSError:
                            pass
                except OSError:
                    continue
            direct_size[rel] = size

        walk(self._root, "")
        total = len(pdfs)

        bad = 0
        docs_done = 0
        pages_done = 0
        unreadable: list[str] = []
        pages_by_rel: dict[str, int] = {}
        for i, path in enumerate(pdfs, 1):
            if self._cancel:
                return
            rel = path.relative_to(self._root).as_posix()
            result = da.audit_pdf(
                str(path), quick=True, cancel_cb=lambda: self._cancel)
            if self._cancel:
                return
            docs_done += 1
            if result.error:
                unreadable.append(rel)
                pages_by_rel[rel] = 0
            else:
                pages_by_rel[rel] = max(0, result.pages)
                pages_done += max(0, result.pages)
                if result.color_ok is False or result.dpi_ok is False:
                    bad += 1
                    bad_flag = True
                else:
                    bad_flag = False
                self.marked.emit(rel, result, bad_flag)
            self.progress.emit(i, total)
            self.stats_step.emit(docs_done, pages_done)
        if self._cancel:
            return

        # Tích lũy Số tài liệu / Số trang theo subtree (giữ semantics của
        # scan_tree_stats: file lỗi đếm là tài liệu, 0 trang).
        stats = da.FolderStats(root=str(self._root))
        stats.unreadable = unreadable
        dir_docs: dict[str, int] = {}
        dir_pages: dict[str, int] = {}
        for rel, n in pages_by_rel.items():
            d = rel.rsplit("/", 1)[0] if "/" in rel else ""
            while True:
                dir_docs[d] = dir_docs.get(d, 0) + 1
                dir_pages[d] = dir_pages.get(d, 0) + n
                if d == "":
                    break
                d = d.rsplit("/", 1)[0] if "/" in d else ""
        stats.total_docs = dir_docs.get("", 0)
        stats.total_pages = dir_pages.get("", 0)
        stats.dir_stats = {d: (dir_docs.get(d, 0), dir_pages.get(d, 0))
                           for d in dir_rels}
        # Tổng dung lượng PDF theo subtree: dung lượng trực tiếp của từng
        # thư mục cộng lên tổ tiên.
        dir_sizes: dict[str, int] = {}
        for d, s in direct_size.items():
            if not s:
                continue
            cur = d
            while True:
                dir_sizes[cur] = dir_sizes.get(cur, 0) + s
                if cur == "":
                    break
                cur = cur.rsplit("/", 1)[0] if "/" in cur else ""
        stats.dir_sizes = dir_sizes
        stats.total_size = dir_sizes.get("", 0)
        for d in dir_rels:
            if d == "":
                continue
            level = rt.level_of(Path(d))
            if level is rt.Level.MA_DINH_DANH:
                stats.level_counts["mdd"] += 1
            elif level is rt.Level.PHONG:
                stats.level_counts["phong"] += 1
            elif level is rt.Level.MUC_LUC:
                stats.level_counts["muc_luc"] += 1
            elif level is rt.Level.HO_SO:
                stats.level_counts["ho_so"] += 1
        self.stats_done.emit(stats)
        self.done.emit(bad)


class _AuditWorker(QThread):
    """Thẩm định đầy đủ một file PDF khi được bấm chọn (đọc cấu trúc,
    không sửa file). Mỗi lần chọn đều đọc lại — không cache."""

    done = Signal(str, object)  # (path, PdfAuditResult)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path

    def run(self):
        self.done.emit(self._path, da.audit_pdf(self._path))


class _CheckCard(QFrame):
    """Thẻ một tiêu chí thẩm định, đúng 2 dòng: nhãn nhỏ + chấm trạng
    thái, giá trị đậm màu theo kết luận. Nền/viền thống nhất, chỉ màu
    trạng thái thay đổi (xanh đạt, đỏ lỗi nặng, vàng lỗi nhẹ / chưa ký)."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("CheckCard")
        v = QVBoxLayout(self)
        v.setContentsMargins(SP[3], SP[2] + 1, SP[3], SP[2] + 1)
        v.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(5)
        self.dot = QLabel("●")
        self.title = QLabel(title)
        head.addWidget(self.dot)
        head.addWidget(self.title)
        head.addStretch(1)
        v.addLayout(head)

        self.value = QLabel("—")
        v.addWidget(self.value)
        self.set_status("na", "—")

    def set_title(self, text: str):
        self.title.setText(text.upper())

    def set_status(self, status: str, value: str):
        fg, accent = _STATUS_LOOK.get(status, _STATUS_LOOK["na"])
        self.setStyleSheet(
            f"QFrame#CheckCard {{ background: {COLOR_SURFACE};"
            f" border: 1px solid {COLOR_BORDER};"
            f" border-left: 3px solid {accent};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        self.dot.setStyleSheet(
            f"color: {accent}; font-size: 9px;"
            " background: transparent; border: none;"
        )
        self.title.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font: 600 10px '{FONT_UI}';"
            " background: transparent; border: none;"
        )
        self.value.setStyleSheet(
            f"color: {fg}; font: 700 15px '{FONT_UI}';"
            " background: transparent; border: none;"
        )
        self.value.setText(value)


# --------------------------------------------------------------------------- #
# Tiện ích
# --------------------------------------------------------------------------- #

def _fmt_size(n: int) -> str:
    """Đọc được: '845 B', '12,3 MB', '1,4 GB' (thập phân kiểu VN)."""
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(v):,}".replace(",", ".") + " B"
            return f"{v:.1f}".replace(".", ",") + f" {unit}"
        v /= 1024
    return f"{n} B"


# --------------------------------------------------------------------------- #
# Màn hình chính
# --------------------------------------------------------------------------- #

class DigitizationAuditScreen(ScreenContent):
    """Công cụ thẩm định số hóa (view-only)."""

    log_message = Signal(str, str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {COLOR_BG};")
        self._root: Path | None = None
        self._stats = None                    # FolderStats | None
        self._tree_worker: _TreeAuditWorker | None = None
        self._audit_worker: _AuditWorker | None = None
        self._audit_seq = 0                   # kết quả cũ đến sau thì bỏ
        self._tree_mark_seq = 0               # vô hiệu mark của worker cũ
        self._bad_rels: set[str] = set()      # rel các file trượt nặng
        self._item_by_rel: dict = {}          # rel → item file trên cây
        self._current_pdf_abs: str | None = None
        self._last_audit = None               # PdfAuditResult đang hiển thị
        self._build_ui()
        self._load_settings()

    # ------------------------------------------------------------- layout

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP[4], SP[4], SP[4], SP[4])
        outer.setSpacing(SP[2])

        bar = QFrame()
        bar.setStyleSheet("QFrame { background: transparent; }")
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(SP[2])

        self.btn_pick = QPushButton("📂  Chọn thư mục gốc…")
        self.btn_pick.setFixedHeight(32)
        self.btn_pick.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pick.setStyleSheet(
            f"QPushButton {{ background: {COLOR_ACCENT}; color: white;"
            f" border: none; padding: 0 16px; border-radius: {RADIUS_LG}px;"
            f" font: 600 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}"
        )
        self.btn_pick.clicked.connect(self._pick_root)
        h.addWidget(self.btn_pick)

        self.edit_root = QLineEdit()
        self.edit_root.setReadOnly(True)
        self.edit_root.setFixedHeight(32)
        self.edit_root.setPlaceholderText("Chưa chọn thư mục gốc")
        self.edit_root.setStyleSheet(
            f"QLineEdit {{ background: {COLOR_SURFACE}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_LG}px;"
            f" padding: 0 10px; font: 12px '{FONT_MONO}'; }}"
        )
        h.addWidget(self.edit_root, 1)

        self.btn_refresh = QPushButton("🔄  Làm mới")
        self.btn_refresh.setFixedHeight(32)
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f" border: 1px solid {COLOR_BORDER}; padding: 0 14px;"
            f" border-radius: {RADIUS_LG}px; font: 500 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_PANEL}; color: {COLOR_TEXT}; }}"
        )
        self.btn_refresh.clicked.connect(self._refresh_tree)
        h.addWidget(self.btn_refresh)

        self.lbl_stats = QLabel("")
        self.lbl_stats.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
        )
        h.addWidget(self.lbl_stats)

        self.lbl_audit_stats = QLabel("")
        self.lbl_audit_stats.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
        )
        h.addWidget(self.lbl_audit_stats)

        # Thanh tiến độ quét nền + nút dừng (ẩn khi không chạy).
        self.progress_scan = QProgressBar()
        self.progress_scan.setFixedWidth(180)
        self.progress_scan.setTextVisible(False)
        self.progress_scan.setVisible(False)
        h.addWidget(self.progress_scan)

        self.btn_stop_scan = QPushButton("⏹  Dừng")
        self.btn_stop_scan.setFixedHeight(26)
        self.btn_stop_scan.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop_scan.setStyleSheet(
            f"QPushButton {{ background: transparent;"
            f" color: {COLOR_TEXT_SECONDARY};"
            f" border: 1px solid {COLOR_BORDER}; padding: 0 10px;"
            f" border-radius: {RADIUS_MD}px; font: 500 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_PANEL};"
            f" color: {COLOR_TEXT}; }}"
        )
        self.btn_stop_scan.clicked.connect(self._stop_tree_scan)
        self.btn_stop_scan.setVisible(False)
        h.addWidget(self.btn_stop_scan)

        outer.addWidget(bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_tree_panel())
        splitter.addWidget(self._build_info_panel())
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([600, 600])
        splitter.setHandleWidth(8)
        splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {COLOR_BORDER}; border-radius: 2px; }}"
            f"QSplitter::handle:hover {{ background: {COLOR_ACCENT}; }}"
        )
        outer.addWidget(splitter, 1)

    def _build_tree_panel(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background: {COLOR_PANEL}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(SP[2], SP[2], SP[2], SP[2])
        vbox.setSpacing(SP[1])

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Tên", "Loại"])
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(18, 18))
        # Elide GIỮA: tên dài cắt ở giữa, giữ lại đuôi tên + dấu " !" phía
        # sau (elide phải sẽ che mất dấu cảnh báo).
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        # CHỈ ĐỌC: không sửa tên, không kéo thả, chọn đơn để xem thông tin.
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setDragEnabled(False)
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        self.tree.headerItem().setTextAlignment(
            1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.tree.setStyleSheet(
            f"QTreeWidget {{ background: transparent; border: none;"
            f" font: 13px '{FONT_UI}'; outline: none; }}"
            f"QTreeWidget::item {{ padding: 5px 3px; border-radius: 4px; }}"
            f"QTreeWidget::item:hover {{ background: {COLOR_SURFACE}; }}"
            f"QTreeWidget::item:selected {{ background: {COLOR_ACCENT}; color: white; }}"
            f"QTreeWidget::item:selected:hover {{ background: {COLOR_ACCENT}; color: white; }}"
            f"QHeaderView::section {{ background: {COLOR_SURFACE};"
            f" color: {COLOR_TEXT_MUTED}; border: none; padding: 5px 8px;"
            f" font: 600 10px '{FONT_UI}'; }}"
        )
        self.tree.itemExpanded.connect(lambda item: self._populate_children(item))
        self.tree.currentItemChanged.connect(self._on_current_changed)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        vbox.addWidget(self.tree, 1)
        return panel

    def _build_info_panel(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background: {COLOR_PANEL}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(SP[2], SP[2], SP[2], SP[2])
        vbox.setSpacing(SP[1])

        # Header gọn một hàng: tên file/thư mục đậm + chip tổng kết bên
        # phải (chip chỉ hiện khi đang xem PDF).
        head_row = QHBoxLayout()
        head_row.setContentsMargins(SP[1], 0, SP[1], 0)
        head_row.setSpacing(SP[2])
        self.lbl_info_name = QLabel("")
        self.lbl_info_name.setStyleSheet(
            f"color: {COLOR_TEXT}; font: 600 14px '{FONT_UI}';"
            " background: transparent; border: none;"
        )
        self.lbl_info_name.setVisible(False)
        self.chip_verdict = QLabel("")
        self.chip_verdict.setObjectName("VerdictChip")
        self.chip_verdict.setVisible(False)
        head_row.addWidget(self.lbl_info_name)
        head_row.addStretch(1)
        head_row.addWidget(self.chip_verdict)
        vbox.addLayout(head_row)

        self.info_stack = QStackedWidget()

        # Trang 0: chưa chọn gì / file không phải PDF.
        empty = QLabel(
            "Chọn một thư mục để xem Số tài liệu / Số trang,\n"
            "hoặc chọn file PDF để thẩm định chất lượng số hóa.")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 13px '{FONT_UI}';"
        )
        self.info_stack.addWidget(empty)               # trang 0

        # Trang 1: thư mục — Số tài liệu / Số trang.
        folder_page = QWidget()
        fv = QVBoxLayout(folder_page)
        fv.setContentsMargins(0, 0, 0, 0)
        fv.setSpacing(SP[2])
        cards = QHBoxLayout()
        cards.setSpacing(SP[2])
        self.lbl_doc_count = self._stat_card(cards, "Số tài liệu")
        self.lbl_page_count = self._stat_card(cards, "Số trang")
        self.lbl_dir_size = self._stat_card(cards, "Dung lượng")
        fv.addLayout(cards)
        fv.addStretch(1)
        self.lbl_folder_extra = QLabel("")
        self.lbl_folder_extra.setWordWrap(True)
        self.lbl_folder_extra.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
        )
        fv.addWidget(self.lbl_folder_extra)
        fv.addStretch(2)
        self.info_stack.addWidget(folder_page)          # trang 1

        # Trang 2: PDF — 5 thẻ chỉ tiêu + hàng mô tả + viewer. Chip tổng
        # kết nằm ở hàng tên file phía trên (xem head_row).
        pdf_page = QWidget()
        pv = QVBoxLayout(pdf_page)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(SP[2])

        cards_row = QHBoxLayout()
        cards_row.setSpacing(SP[2])
        self.card_color = _CheckCard("Scan màu")
        self.card_dpi = _CheckCard("DPI")
        self.card_compression = _CheckCard("Độ nén")
        self.card_ocr = _CheckCard("Đã OCR")
        self.card_sign = _CheckCard("Ký số")
        self._check_cards = (self.card_color, self.card_dpi,
                             self.card_compression, self.card_ocr,
                             self.card_sign)
        # Cân đều 4 cột: min width theo giá trị dài nhất ("Không đảm bảo")
        # để các thẻ không bị lệch kích thước khi chia chỗ trong layout.
        fm = QFontMetrics(self._card_value_font())
        min_w = max(fm.horizontalAdvance(txt) for txt in
                    ("Đúng", "300 dpi", "Không đảm bảo", "—")) \
            + SP[3] * 2 + 4
        for card in self._check_cards:
            card.setMinimumWidth(min_w)
            cards_row.addWidget(card, 1)
        pv.addLayout(cards_row)

        # Một hàng mô tả dài: thông tin file + ghi chú kết luận + cảnh báo
        # (chỉ mô tả, không góp phần quyết định kết quả thẩm định).
        self.lbl_audit_detail = QLabel("")
        self.lbl_audit_detail.setWordWrap(True)
        self.lbl_audit_detail.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_audit_detail.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}';"
            " background: transparent; border: none;"
        )
        pv.addWidget(self.lbl_audit_detail)

        self.pdf_viewer = PdfViewerWidget()
        pv.addWidget(self.pdf_viewer, 1)
        self.info_stack.addWidget(pdf_page)             # trang 2

        self.info_stack.setCurrentIndex(0)
        vbox.addWidget(self.info_stack, 1)
        return panel

    def _stat_card(self, layout: QHBoxLayout, title: str) -> QLabel:
        """Một thẻ số liệu lớn (Số tài liệu / Số trang)."""
        card = QFrame()
        card.setObjectName("StatCard")
        card.setStyleSheet(
            f"QFrame#StatCard {{ background: {COLOR_SURFACE};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_LG}px; }}"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(SP[3], SP[3], SP[3], SP[3])
        v.setSpacing(SP[1])
        lbl_title = QLabel(translations.localize_text(title).upper())
        lbl_title.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font: 600 10px '{FONT_UI}';"
            " background: transparent; border: none;"
        )
        lbl_value = QLabel("—")
        lbl_value.setStyleSheet(
            f"color: {COLOR_TEXT}; font: 700 24px '{FONT_UI}';"
            " background: transparent; border: none;"
        )
        v.addWidget(lbl_title)
        v.addWidget(lbl_value)
        layout.addWidget(card, 1)
        return lbl_value

    # --------------------------------------------------------- chọn thư mục

    def _pick_root(self):
        folder = QFileDialog.getExistingDirectory(
            self, translations.localize_text("Chọn thư mục gốc"),
            str(self._root) if self._root else "",
        )
        if not folder:
            return
        self._set_root(Path(folder))

    def _set_root(self, path: Path):
        self._root = Path(path)
        self.edit_root.setText(str(self._root))
        self._stop_workers()
        self._stats = None
        self._current_pdf_abs = None
        self._last_audit = None
        self._clear_info_header()
        self.lbl_stats.setText("")
        self.lbl_audit_stats.setText("")
        self.info_stack.setCurrentIndex(0)
        self.pdf_viewer.clear()
        self._reload_root()
        if self.isVisible():
            QTimer.singleShot(0, self, self._expand_all_loaded)
        self._start_tree_audit()
        self._save_settings()
        self.log_message.emit(
            translations.localize_text(f"Thẩm định số hóa: đã mở {self._root}"),
            "info",
        )

    def _refresh_tree(self):
        if self._root is None:
            return
        self._stop_workers()
        self._stats = None
        self.pdf_viewer.clear()
        self._reload_root()
        self._expand_all_loaded()
        self._start_tree_audit()

    def _stop_workers(self):
        self._stop_tree_scan()
        self._audit_seq += 1      # vô hiệu kết quả thẩm định đang chạy
        self._tree_mark_seq += 1  # vô hiệu dấu ❗ của vòng quét cũ

    def showEvent(self, event):
        """Mở chức năng: bung full cây (nạp lazy điều khiển thủ công)."""
        super().showEvent(event)
        if self.isVisible() and self._root is not None:
            QTimer.singleShot(0, self, self._expand_all_loaded)

    def _expand_all_loaded(self):
        if self._root is None:
            return
        self.tree.setUpdatesEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            stack = [self.tree.topLevelItem(i)
                     for i in range(self.tree.topLevelItemCount())]
            while stack:
                item = stack.pop()
                self._populate_children(item)
                if item.childCount():
                    item.setExpanded(True)
                    stack.extend(item.child(i)
                                 for i in range(item.childCount()))
        finally:
            QApplication.restoreOverrideCursor()
            self.tree.setUpdatesEnabled(True)

    def _reload_root(self):
        self.tree.clear()
        self._item_by_rel.clear()
        self._bad_rels.clear()
        if self._root is None:
            self.lbl_stats.setText("")
            return
        try:
            # Chỉ kiểm tra đọc được; con thật sự do _populate_children nạp.
            sorted(self._root.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            self.lbl_stats.setText(
                translations.localize_text(f"Không đọc được thư mục: {exc}"))
            return
        root_item = self._make_root_item()
        self.tree.addTopLevelItem(root_item)
        root_item.setExpanded(True)
        # Con của gốc nạp đúng 1 lần qua _populate_children (tự bỏ dummy) —
        # thêm thủ công ở đây sẽ nhân đôi từng thư mục cấp gốc.
        self._populate_children(root_item)

    # ------------------------------------------------- quét + thẩm định cây

    def _start_tree_audit(self):
        """Một lượt đi duy nhất trên cây: quét nhanh từng PDF (trang, ảnh,
        DPI, màu, chữ ký — bỏ text/filter nên nhanh) để gắn dấu ❗, đồng
        thời đếm Số tài liệu / Số trang / Dung lượng. Luôn đọc lại file —
        không cache — để phản ánh đúng hiện trạng (thêm/sửa/xóa)."""
        if self._root is None:
            return
        # parent=self: Qt đợi thread chạy xong khi dọn dẹp (thoát app) thay
        # vì hủy QThread đang chạy → tránh crash lúc shutdown.
        self._tree_worker = _TreeAuditWorker(self._root, parent=self)
        self._tree_mark_seq += 1
        seq = self._tree_mark_seq
        w = self._tree_worker
        w.progress.connect(
            lambda done, total, s=seq: self._on_tree_progress(done, total, s))
        w.stats_step.connect(
            lambda d, p, s=seq: self._on_tree_stats_step(d, p, s))
        w.marked.connect(
            lambda rel, r, bad, s=seq: self._on_tree_marked(rel, r, bad, s))
        w.stats_done.connect(
            lambda stats, s=seq: self._on_tree_stats_done(stats, s))
        w.done.connect(lambda bad, s=seq: self._on_tree_done(bad, s))
        w.start()
        self.progress_scan.setRange(0, 0)
        self.progress_scan.setValue(0)
        self.progress_scan.setVisible(True)
        self.btn_stop_scan.setVisible(True)
        self.lbl_stats.setText(translations.localize_text("Đang quét…"))
        self.lbl_audit_stats.setText("")

    def _stop_tree_scan(self):
        """Ngưng quét nền (nút Dừng / rời màn / chọn gốc mới / thoát app)."""
        if self._tree_worker is not None:
            self._tree_worker.cancel()
            self._tree_worker = None
        self.progress_scan.setVisible(False)
        self.btn_stop_scan.setVisible(False)

    def _on_tree_progress(self, done: int, total: int, seq: int):
        if seq != self._tree_mark_seq:
            return
        self.progress_scan.setRange(0, max(1, total))
        self.progress_scan.setValue(done)

    def _on_tree_stats_step(self, docs: int, pages: int, seq: int):
        if seq != self._tree_mark_seq:
            return
        self.lbl_stats.setText(translations.localize_text(
            f"Đang quét… {docs} tài liệu · {pages} trang"))

    def _on_tree_marked(self, rel: str, result, bad: bool, seq: int):
        if seq != self._tree_mark_seq:
            return
        if bad:
            self._bad_rels.add(rel)
        item = self._item_by_rel.get(rel)
        if item is not None:
            self._apply_bad_mark(item, bad)

    def _on_tree_stats_done(self, stats, seq: int):
        if seq != self._tree_mark_seq:
            return
        self._stats = stats
        extra = ""
        if stats.unreadable:
            extra = (f" · {len(stats.unreadable)} PDF không đọc được")
        self.lbl_stats.setText(translations.localize_text(
            f"{stats.total_docs} tài liệu · "
            f"{stats.total_pages} trang{extra}")
            + f" · {_fmt_size(stats.total_size)}")
        # Cập nhật ngay card thư mục nếu đang chọn một thư mục.
        item = self.tree.currentItem()
        if item is not None and item.data(0, _ROLE_ISDIR):
            self._show_folder_info(item.data(0, _ROLE_REL))

    def _on_tree_done(self, bad: int, seq: int):
        if seq != self._tree_mark_seq:
            return
        self.progress_scan.setVisible(False)
        self.btn_stop_scan.setVisible(False)
        if bad:
            self.lbl_audit_stats.setText(
                translations.localize_text(f"⚠ {bad} PDF không đạt"))
            self.lbl_audit_stats.setStyleSheet(
                f"color: {_COLOR_FAIL_FG}; font: 600 12px '{FONT_UI}';"
            )
        else:
            self.lbl_audit_stats.setText(
                translations.localize_text("Tất cả PDF đạt"))
            self.lbl_audit_stats.setStyleSheet(
                f"color: {_STATUS_LOOK['pass'][0]}; font: 12px '{FONT_UI}';"
            )

    def cleanup(self) -> None:
        """Ngưng quét nền khi thoát app (được gọi qua SupportToolsScreen.
        cleanup trong closeEvent). Không có cache gì cần lưu — mỗi lần quét
        đều đọc lại file để phản ánh hiện trạng."""
        self._stop_workers()

    def _apply_bad_mark(self, item: QTreeWidgetItem, bad: bool):
        """Gắn/bỏ dấu ! SAU tên file + tô đỏ tên cho file trượt Scan màu /
        DPI (dấu ở sau để không che đầu tên file dài)."""
        base = item.data(0, _ROLE_BASENAME) or item.text(0)
        item.setText(0, f"{base} !" if bad else str(base))
        item.setForeground(
            0, QColor(_COLOR_FAIL_FG if bad else COLOR_TEXT))

    # ------------------------------------------------------------- cây item

    def _make_root_item(self) -> QTreeWidgetItem:
        name = self._root.name or str(self._root)
        badge = "Thư mục gốc"
        item = QTreeWidgetItem([name, translations.localize_text(badge)])
        item.setData(0, _ROLE_REL, "")
        item.setData(0, _ROLE_LEVEL, None)
        item.setData(0, _ROLE_ISDIR, True)
        item.setData(0, _ROLE_LOADED, False)
        item.setData(0, _ROLE_BADGE, badge)
        item.setIcon(0, _folder_icon(_FOLDER_FALLBACK_COLOR))
        item.setForeground(1, QColor(COLOR_TEXT_SECONDARY))
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
        item.addChild(QTreeWidgetItem([_DUMMY_TEXT]))
        return item

    def _make_dir_item(self, path: Path) -> QTreeWidgetItem:
        rel = path.relative_to(self._root).as_posix()
        level = rt.level_of(Path(rel))
        badge = rt.LEVEL_LABELS.get(level, "Thư mục con")
        item = QTreeWidgetItem(
            [path.name, translations.localize_text(badge)]
        )
        item.setData(0, _ROLE_REL, rel)
        item.setData(0, _ROLE_LEVEL, level)
        item.setData(0, _ROLE_ISDIR, True)
        item.setData(0, _ROLE_LOADED, False)
        item.setData(0, _ROLE_BADGE, badge)
        if level is rt.Level.HO_SO:
            item.setIcon(0, _dossier_icon(
                _LEVEL_COLOR.get(level, _FOLDER_FALLBACK_COLOR)))
        else:
            item.setIcon(0, _folder_icon(
                _LEVEL_COLOR.get(level, _FOLDER_FALLBACK_COLOR)))
        item.setForeground(1, QColor(COLOR_TEXT_SECONDARY))
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
        item.addChild(QTreeWidgetItem([_DUMMY_TEXT]))
        return item

    def _make_file_item(self, path: Path) -> QTreeWidgetItem:
        rel = path.relative_to(self._root).as_posix()
        is_pdf = path.suffix.lower() == ".pdf"
        badge = "PDF" if is_pdf else "File khác"
        item = QTreeWidgetItem([path.name, translations.localize_text(badge)])
        item.setData(0, _ROLE_REL, rel)
        item.setData(0, _ROLE_LEVEL, None)
        item.setData(0, _ROLE_ISDIR, False)
        item.setData(0, _ROLE_BADGE, badge)
        item.setData(0, _ROLE_BASENAME, path.name)
        item.setIcon(0, _file_icon("#eef1f4" if is_pdf else "#cdd3da"))
        item.setForeground(1, QColor(COLOR_TEXT_SECONDARY))
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
        self._item_by_rel[rel] = item
        if rel in self._bad_rels:
            self._apply_bad_mark(item, True)
        return item

    def _populate_children(self, item: QTreeWidgetItem):
        if item.data(0, _ROLE_LOADED) or not item.data(0, _ROLE_ISDIR):
            return
        item.setData(0, _ROLE_LOADED, True)
        for i in range(item.childCount()):
            if item.child(i).text(0) == _DUMMY_TEXT:
                item.removeChild(item.child(i))
                break
        rel = item.data(0, _ROLE_REL)
        if rel is None or self._root is None:
            return
        base = self._root if rel == "" else self._root / Path(rel)
        try:
            entries = sorted(
                base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                item.addChild(self._make_dir_item(entry))
            else:
                item.addChild(self._make_file_item(entry))

    # ------------------------------------------------------- chọn / hiển thị

    def _on_current_changed(self, current: QTreeWidgetItem,
                            _prev: QTreeWidgetItem | None):
        if current is None:
            self.info_stack.setCurrentIndex(0)
            self._clear_info_header()
            return
        rel = current.data(0, _ROLE_REL)
        if current.data(0, _ROLE_ISDIR):
            self._show_folder(rel)
        elif rel and rel.lower().endswith(".pdf"):
            self._show_pdf(rel)
        else:
            self._set_info_header(Path(rel).name if rel else "")
            self.info_stack.setCurrentIndex(0)

    # --------------------------------------------------------- header tên

    def _clear_info_header(self):
        self.lbl_info_name.setText("")
        self.lbl_info_name.setVisible(False)
        self.chip_verdict.setVisible(False)

    def _set_info_header(self, name: str):
        self.lbl_info_name.setText(name)
        self.lbl_info_name.setVisible(bool(name))

    def _show_folder(self, rel: str):
        self._current_pdf_abs = None
        path = self._root if rel == "" else self._root / Path(rel)
        self._set_info_header(path.name or str(path))
        self.chip_verdict.setVisible(False)
        self.info_stack.setCurrentIndex(1)
        self._show_folder_info(rel)

    def _show_folder_info(self, rel: str):
        """Đổ Số tài liệu / Số trang / Dung lượng của thư mục ``rel``."""
        if self._stats is None:
            self.lbl_doc_count.setText("…")
            self.lbl_page_count.setText("…")
            self.lbl_dir_size.setText("…")
            self.lbl_folder_extra.setText(
                translations.localize_text("Đang quét…"))
            return
        docs, pages = self._stats.of_rel(rel)
        self.lbl_doc_count.setText(f"{docs:,}".replace(",", "."))
        self.lbl_page_count.setText(f"{pages:,}".replace(",", "."))
        self.lbl_dir_size.setText(
            _fmt_size(self._stats.size_of_rel(rel)))
        lc = self._stats.level_counts
        parts = []
        if lc.get("mdd"):
            parts.append(f"{lc['mdd']} mã định danh")
        if lc.get("phong"):
            parts.append(f"{lc['phong']} phông")
        if lc.get("muc_luc"):
            parts.append(f"{lc['muc_luc']} mục lục")
        if lc.get("ho_so"):
            parts.append(f"{lc['ho_so']} hồ sơ")
        if self._stats.unreadable:
            parts.append(
                f"{len(self._stats.unreadable)} PDF không đọc được")
        self.lbl_folder_extra.setText(
            translations.localize_text(" · ".join(parts)))

    def _show_pdf(self, rel: str):
        if self._root is None:
            return
        path = self._root / Path(rel)
        if not path.is_file():
            return
        self._current_pdf_abs = str(path)
        self._set_info_header(path.name)
        self.chip_verdict.setVisible(True)
        self.info_stack.setCurrentIndex(2)
        self._render_audit(None)  # trạng thái chờ
        self._start_audit(str(path))
        self.pdf_viewer.show_pdf(str(path))

    # ------------------------------------------------------------ thẩm định

    def _start_audit(self, path: str):
        """Thẩm định đầy đủ file vừa chọn — luôn đọc lại, không cache."""
        self._audit_seq += 1
        seq = self._audit_seq
        self._audit_worker = _AuditWorker(path, parent=self)
        self._audit_worker.done.connect(
            lambda p, r, s=seq: self._on_audit_done(p, r, s))
        self._audit_worker.start()

    def _on_audit_done(self, path: str, result, seq: int):
        if seq != self._audit_seq or path != self._current_pdf_abs:
            return  # người dùng đã chọn file khác — bỏ kết quả cũ
        self._render_audit(result)
        if result.error:
            self.log_message.emit(
                translations.localize_text(
                    f"Thẩm định số hóa: {result.error} ({path})"),
                "warning",
            )

    @staticmethod
    def _card_value_font() -> QFont:
        """Font của chữ giá trị trên thẻ chỉ tiêu (khớp _CheckCard)."""
        f = QFont(FONT_UI)
        f.setPixelSize(15)
        f.setBold(True)
        return f

    def _render_audit(self, result):
        """Đổ 5 chỉ tiêu vào thẻ (Ký số là tùy chọn); ``result=None`` =
        đang phân tích."""
        self._last_audit = result
        for card, name in zip(self._check_cards,
                              ("Scan màu", "DPI", "Độ nén", "Đã OCR",
                               "Ký số")):
            card.set_title(translations.localize_text(name))
        if result is None:
            for card in self._check_cards:
                card.set_status("na", "…")
            self.lbl_audit_detail.setText("")
            self._set_verdict([])
            return
        if result.error:
            msg = html.escape(translations.localize_text(result.error))
            for card in self._check_cards:
                card.set_status("na", "—")
            self.lbl_audit_detail.setText(
                f"<span style='color:{_COLOR_FAIL_FG};'>{msg}</span>")
            self._set_verdict([], error=True)
            return

        statuses: list[str] = []
        notes: list[str] = []      # ghi chú kết luận → hàng mô tả dài

        # Scan màu — lỗi nặng.
        if result.color_ok is None:
            self.card_color.set_status("na", "—")
            statuses.append("na")
        else:
            ok = bool(result.color_ok)
            self.card_color.set_status(
                "pass" if ok else "fail",
                translations.localize_text("Đúng" if ok else "Không"))
            statuses.append("pass" if ok else "fail")

        # DPI — lỗi nặng.
        if result.dpi_ok is None:
            self.card_dpi.set_status("na", "—")
            statuses.append("na")
        elif result.dpi_ok:
            self.card_dpi.set_status("pass", f"{result.min_dpi:.0f} dpi")
            statuses.append("pass")
        else:
            worst = min(result.images, key=lambda im: im.dpi)
            self.card_dpi.set_status("fail", f"{worst.dpi:.0f} dpi")
            statuses.append("fail")
            notes.append(translations.localize_text(
                f"DPI dưới ngưỡng {da.DPI_MIN} dpi"
                f" — thấp nhất ở trang {worst.page_no}"))

        # Độ nén — lỗi nhẹ.
        if result.compression_ok is None:
            self.card_compression.set_status("na", "—")
        else:
            ok = bool(result.compression_ok)
            self.card_compression.set_status(
                "pass" if ok else "warn",
                translations.localize_text(
                    "Đảm bảo" if ok else "Không đảm bảo"))
            if result.compression_note:
                notes.append(translations.localize_text(
                    result.compression_note))

        # Đã OCR — lỗi nhẹ.
        if result.ocr_ok is None:
            self.card_ocr.set_status("na", "—")
        else:
            ok = bool(result.ocr_ok)
            self.card_ocr.set_status(
                "pass" if ok else "warn",
                translations.localize_text("Đúng" if ok else "Không"))

        # Ký số — tùy chọn, KHÔNG tính vào chip tổng kết / dấu ❗.
        # Chưa ký = vàng (ít quan trọng), không ảnh hưởng kết luận.
        if result.signed is None:
            self.card_sign.set_status("na", "—")
        elif result.signed:
            self.card_sign.set_status("pass", "Có")
        else:
            self.card_sign.set_status(
                "warn", translations.localize_text("Không"))
            notes.append(translations.localize_text(
                "chưa ký số (tùy chọn)"))

        # Một hàng mô tả dài: thông tin file + ghi chú + cảnh báo ⚠.
        # Trạng thái PDF/A là mô tả trung tính (thiếu PDF/A không phải lỗi —
        # chuẩn nhận cả PDF thường) → chữ xám như mô tả, KHÔNG ⚠ vàng.
        # Bộ nén ẩn khi ghi chú vi phạm đã nêu rõ.
        size_mb = result.file_size / (1024 * 1024)
        bits = [result.color_summary()]
        if not result.compression_note:
            bits.append(result.compression_summary())
        bits += [f"{result.pages} trang", f"{size_mb:.1f} MB"]
        if result.pdfa:
            bits.append(f"PDF/A-{result.pdfa}")
        else:
            bits.append("không khai báo PDF/A (không bắt buộc)")
        bits += notes
        warn_segs = [
            f"<span style='color:{_COLOR_WARN_FG};'>⚠ "
            f"{html.escape(translations.localize_text(w))}</span>"
            for w in result.warnings()]
        html_bits = [
            html.escape(translations.localize_text(b)) for b in bits]
        self.lbl_audit_detail.setText(
            " · ".join(html_bits + warn_segs))
        self._set_verdict(statuses)

    def _set_verdict(self, statuses: list[str], error: bool = False):
        """Chip tổng kết cạnh tên file: CHỈ 2 tiêu chí quan trọng quyết
        định — Scan màu + DPI → 'Đạt' / 'Không đạt' (không đếm x/4; các
        tiêu chí vàng chỉ mang tính tham khảo, không vào chip)."""
        known = [s for s in statuses if s != "na"]
        if error:
            key, text = "fail", "Lỗi thẩm định"
        elif not known:
            key, text = "na", "Đang phân tích…"
        elif "fail" in known:
            key, text = "fail", "Không đạt"
        else:
            key, text = "pass", "Đạt"
        fg, accent = _STATUS_LOOK[key]
        self.chip_verdict.setText(translations.localize_text(text))
        self.chip_verdict.setStyleSheet(
            f"QLabel#VerdictChip {{ background: {COLOR_SURFACE};"
            f" color: {fg};"
            f" border: 1px solid {accent}; border-radius: 10px;"
            f" padding: 2px 10px; font: 600 11px '{FONT_UI}'; }}"
        )

    # ------------------------------------------------------------- khác

    def _on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        rel = item.data(0, _ROLE_REL)
        if rel is None:
            return
        menu = QMenu(self)
        act_open = menu.addAction(translations.localize_text(
            "Mở trong Explorer"))
        target = self._root if rel == "" else self._root / Path(rel)
        if not item.data(0, _ROLE_ISDIR):
            target = target.parent
        act_open.triggered.connect(
            lambda: self._open_in_explorer(target))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _open_in_explorer(self, path: Path):
        try:
            os.startfile(str(path))
        except OSError as exc:
            self.log_message.emit(
                translations.localize_text(
                    f"Thẩm định số hóa: không mở được thư mục ({exc})"),
                "warning",
            )

    # ------------------------------------------------------------- ScreenBase

    def is_busy(self) -> bool:
        # Công cụ chỉ đọc: quét/thẩm định chạy nền, không chặn điều hướng.
        return False

    def request_cancel(self) -> None:
        self._stop_workers()

    def update_texts(self) -> None:
        """Dịch lại nhãn cột Loại sau khi đổi ngôn ngữ (nguồn giữ trong
        _ROLE_BADGE); card thẩm định vẽ lại từ kết quả gần nhất."""
        def walk(item: QTreeWidgetItem):
            badge = item.data(0, _ROLE_BADGE)
            if badge:
                item.setText(1, translations.localize_text(str(badge)))
            for i in range(item.childCount()):
                walk(item.child(i))
        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        if self._last_audit is not None:
            self._render_audit(self._last_audit)

    # ------------------------------------------------------------- settings

    def _load_settings(self):
        try:
            if not os.path.exists(_SETTINGS_FILE):
                return
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            root = str(data.get("root") or "").strip()
            if root and os.path.isdir(root):
                self._set_root(Path(root))
        except Exception:
            pass

    def _save_settings(self):
        try:
            os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({"root": str(self._root or "")},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass
