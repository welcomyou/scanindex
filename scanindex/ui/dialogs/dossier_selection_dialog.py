"""Popup chọn hồ sơ cho Bước 2 Số hóa lưu trữ (nhập thư mục nhiều hồ sơ).

Nhúng NGUYÊN :class:`RenameTreeEditor` (widget dùng chung với công cụ "Đổi
tên cây thư mục" — không bản sao) ở chế độ chọn hồ sơ, bổ sung footer chọn:
Chọn tất cả / Bỏ chọn tất cả / bộ đếm / Rà lại / Hủy / Đưa vào Bước 2.

Nguyên tắc (mục 4 của kế hoạch):
  * Đổi tên / di chuyển / đánh số / hoàn tác chạy TRỰC TIẾP trên thư mục
    nguồn như công cụ độc lập — popup chỉ nhắc ngắn ở đầu.
  * Đóng/Hủy là hủy chọn để OCR; các thao tác đã áp dụng lên nguồn KHÔNG tự
    hoàn tác (Ctrl+Z trong popup trước khi đóng).
  * "Chọn tất cả" dựa trên model quét — phủ cả hồ sơ chưa bung trên cây.
  * Trước khi chốt ("Đưa vào Bước 2") tự rà lại lần cuối; lấy danh sách theo
    THỨ TỰ người dùng đã chỉnh; không chạy lại os.walk để thay kết quả chọn.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from scanindex.core.digitization.dossier_model import (
    DossierPick,
    DossierSelection,
)
from scanindex.core.digitization.folder_scan import (
    FolderScanResult, scan_archive_folder,
)
from scanindex.infra import translations
from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG, COLOR_BORDER, COLOR_PANEL,
    COLOR_SURFACE, COLOR_TEXT, COLOR_TEXT_SECONDARY, FONT_MONO, FONT_UI,
    RADIUS_MD, SP,
)
from scanindex.ui.widgets.rename_tree_editor import RenameTreeEditor


class FolderScanWorker(QThread):
    """Quét cây thư mục ở thread nền — không treo UI với cây lớn (mục 3.1)."""

    done = Signal(object)   # FolderScanResult

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self._root = Path(root)

    def run(self):
        self.done.emit(scan_archive_folder(self._root))


class DossierSelectionDialog(QDialog):
    """Chọn hồ sơ cần số hóa từ thư mục có cấu trúc chuẩn CSDL_SOHOA."""

    def __init__(self, root: Path, scan: FolderScanResult | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(translations.localize_text(
            "Chọn hồ sơ để số hóa"))
        self.resize(1120, 760)
        self.source_root = Path(root)
        self.scan = scan
        self.selection_result: DossierSelection | None = None
        self._scan_worker: FolderScanWorker | None = None

        self.editor = RenameTreeEditor(
            self, log_prefix="Nhập thư mục Số hóa")
        self.editor.set_selection_mode(True)
        self.editor.log_message.connect(self._on_editor_log)
        self.editor.busy_changed.connect(self._refresh_footer)
        self.editor.selection_changed.connect(self._refresh_footer)

        self._build_ui()
        if self.scan is not None:
            self._apply_scan(self.scan)
        else:
            self._start_scan()

    # ------------------------------------------------------------- layout

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP[4], SP[3], SP[4], SP[3])
        outer.setSpacing(SP[2])

        head = QFrame()
        head.setStyleSheet("QFrame { background: transparent; }")
        hv = QHBoxLayout(head)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(SP[2])
        self.lbl_path = QLabel("")
        self.lbl_path.setStyleSheet(
            f"color: {COLOR_TEXT}; font: 600 13px '{FONT_MONO}';")
        hv.addWidget(self.lbl_path, 1)
        self.lbl_issues = QLabel("")
        self.lbl_issues.setStyleSheet(
            f"color: #f87171; font: 600 12px '{FONT_UI}';")
        self.lbl_issues.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_issues.setVisible(False)
        self.lbl_issues.mousePressEvent = lambda _e: self._show_issue_details()
        hv.addWidget(self.lbl_issues)
        outer.addWidget(head)

        warn = QLabel(translations.localize_text(
            "⚠ Đổi tên / di chuyển / đánh số trong cửa sổ này sẽ sửa TRỰC "
            "TIẾP thư mục nguồn (Ctrl+Z hoàn tác được trước khi đóng)."))
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';")
        outer.addWidget(warn)

        outer.addWidget(self.editor, 1)

        self.lbl_log = QLabel("")
        self.lbl_log.setWordWrap(True)
        self.lbl_log.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}';")
        self.lbl_log.setVisible(False)
        outer.addWidget(self.lbl_log)

        foot = QFrame()
        foot.setStyleSheet("QFrame { background: transparent; }")
        h = QHBoxLayout(foot)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(SP[2])

        self.btn_all = self._foot_button("☑ Chọn tất cả", self.editor.select_all)
        self.btn_none = self._foot_button("☐ Bỏ chọn tất cả",
                                          self.editor.clear_selection)
        h.addWidget(self.btn_all)
        h.addWidget(self.btn_none)

        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet(
            f"color: {COLOR_TEXT}; font: 600 12px '{FONT_UI}';")
        h.addWidget(self.lbl_count, 1)

        self.btn_rescan = self._foot_button("🔄 Rà lại", self._start_scan)
        h.addWidget(self.btn_rescan)

        self.btn_cancel = QPushButton(translations.localize_text("Hủy"))
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f" border: 1px solid {COLOR_BORDER}; padding: 7px 18px;"
            f" border-radius: {RADIUS_MD}px; font: 600 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_PANEL}; color: {COLOR_TEXT}; }}"
        )
        self.btn_cancel.clicked.connect(self.reject)
        h.addWidget(self.btn_cancel)

        self.btn_ok = QPushButton(translations.localize_text("Đưa vào Bước 2"))
        self.btn_ok.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ok.setStyleSheet(
            f"QPushButton {{ background: {COLOR_ACCENT}; color: white;"
            f" border: none; padding: 7px 22px; border-radius: {RADIUS_MD}px;"
            f" font: 600 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}"
            f"QPushButton:disabled {{ background: #555; color: #aaa; }}"
        )
        self.btn_ok.clicked.connect(self._on_confirm)
        h.addWidget(self.btn_ok)
        outer.addWidget(foot)

    def _foot_button(self, text: str, on_click) -> QPushButton:
        btn = QPushButton(translations.localize_text(text))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f" border: 1px solid {COLOR_BORDER}; padding: 6px 14px;"
            f" border-radius: {RADIUS_MD}px; font: 500 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_PANEL}; color: {COLOR_TEXT}; }}"
        )
        btn.clicked.connect(on_click)
        return btn

    # -------------------------------------------------------------- quét

    def _start_scan(self):
        """Rà lại cây (thread nền); giữ trạng thái editor khi đang bận."""
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        self.lbl_path.setText(translations.localize_text("Đang quét cây…"))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._scan_worker = FolderScanWorker(self.source_root, self)
        self._scan_worker.done.connect(self._on_scan_done)
        self._scan_worker.start()

    def _on_scan_done(self, scan: FolderScanResult):
        try:
            QApplication.restoreOverrideCursor()
        except Exception:
            pass
        self.scan = scan
        self._apply_scan(scan)

    def _apply_scan(self, scan: FolderScanResult):
        """Nạp scan vào editor: gốc logic = nơi chứa các thư mục mã định danh
        (có/không lớp CSDL_SOHOA — xác định theo nội dung cây, mục 3.1)."""
        root = Path(scan.logical_root) if scan.logical_root else self.source_root
        if self.editor.root() != root:
            self.editor.set_root(root)
        self.editor.set_scan(scan)
        self.lbl_path.setText(str(self.source_root))
        n_issues = len(scan.issues)
        self.lbl_issues.setVisible(n_issues > 0)
        if n_issues:
            self.lbl_issues.setText(translations.localize_text(
                f"⚠ {n_issues} PDF/thư mục không thuộc hồ sơ chuẩn — bấm xem"))

    def _show_issue_details(self):
        if not self.scan or not self.scan.issues:
            return
        lines = [f"• {issue.rel}\n    {issue.reason}"
                 for issue in self.scan.issues[:200]]
        if len(self.scan.issues) > 200:
            lines.append(translations.localize_text(
                f"… và {len(self.scan.issues) - 200} mục khác"))
        dlg = QDialog(self)
        dlg.setWindowTitle(translations.localize_text(
            "Mục không thuộc hồ sơ chuẩn"))
        dlg.resize(720, 480)
        v = QVBoxLayout(dlg)
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setText("\n".join(lines))
        txt.setStyleSheet(
            f"QTextEdit {{ background: {COLOR_SURFACE}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; font: 12px '{FONT_MONO}'; }}")
        v.addWidget(txt, 1)
        close = QPushButton(translations.localize_text("Đóng"))
        close.clicked.connect(dlg.accept)
        v.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    def _on_editor_log(self, message: str, level: str):
        """Log của editor (đổi tên, hoàn tác…) hiện dòng mỏng dưới cây."""
        prefix = {"success": "✓ ", "warning": "⚠ "}.get(level, "")
        self.lbl_log.setText(prefix + message)
        self.lbl_log.setVisible(True)

    # ------------------------------------------------------------ footer

    def _refresh_footer(self, *_):
        n, docs = self.editor.selection_stats()
        self.lbl_count.setText(translations.localize_text(
            f"Đã chọn: {n} hồ sơ · {docs} tài liệu PDF"))
        self.btn_ok.setEnabled(n > 0 and not self.editor.is_busy())
        self.btn_all.setEnabled(not self.editor.is_busy())
        self.btn_none.setEnabled(not self.editor.is_busy())
        self.btn_rescan.setEnabled(not self.editor.is_busy())

    # ------------------------------------------------------------ chốt

    def _on_confirm(self):
        """Chốt danh sách: rà lại lần cuối (dữ liệu hiện tại), giữ phần chọn
        vẫn hợp lệ, lấy theo THỨ TỰ người dùng đã chỉnh trên cây."""
        if self.editor.is_busy() or self.editor.selected_rels() == set():
            return
        pre_check = self.editor.selected_rels()  # trước khi lọc theo scan mới
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            final_scan = scan_archive_folder(self.source_root)
        finally:
            QApplication.restoreOverrideCursor()
        self.scan = final_scan
        self.editor.set_scan(final_scan)   # lọc checked phần còn hợp lệ
        picked = self.editor.ordered_selected()
        before = len(pre_check)
        if not picked:
            QMessageBox.warning(
                self,
                translations.localize_text("Không còn hồ sơ hợp lệ"),
                translations.localize_text(
                    "Lần rà lại cuối không còn hồ sơ hợp lệ nào (cấu trúc "
                    "đã đổi). Hãy kiểm tra cây rồi thử lại."),
            )
            self._apply_scan(final_scan)
            return
        if len(picked) != before:
            QMessageBox.warning(
                self,
                translations.localize_text("Có hồ sơ mất chọn"),
                translations.localize_text(
                    f"{before - len(picked)} hồ sơ vừa mất chọn do cấu trúc "
                    "thay đổi. Danh sách chốt sẽ không gồm các hồ sơ đó."),
            )
        self.selection_result = DossierSelection(
            source_root=str(self.source_root),
            logical_root=str(final_scan.logical_root or self.source_root),
            dossiers=tuple(
                DossierPick(
                    dossier_dir=str(final_scan.logical_root / d.rel)
                    if final_scan.logical_root else str(self.source_root / d.rel),
                    documents=tuple(
                        str((final_scan.logical_root or self.source_root)
                            / doc.rel)
                        for doc in d.documents),
                    codes=d.codes,
                )
                for d in picked
            ),
        )
        self.accept()

    # ------------------------------------------------------------ đóng

    def reject(self):
        """Hủy = hủy chọn để OCR. Thao tác đã áp dụng lên nguồn KHÔNG hoàn
        tác; nếu editor đang chạy kế hoạch thì hỏi trước khi rời."""
        if self.editor.is_busy():
            confirm = QMessageBox(self)
            confirm.setWindowTitle(translations.localize_text("Đang bận"))
            confirm.setIcon(QMessageBox.Icon.Question)
            confirm.setText(translations.localize_text(
                "Đang thực hiện thao tác trên cây. Dừng và đóng?"))
            confirm.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            confirm.setDefaultButton(QMessageBox.StandardButton.No)
            if confirm.exec() != QMessageBox.StandardButton.Yes:
                return
            self.editor.request_cancel()
        super().reject()

    def is_busy(self) -> bool:
        return self.editor.is_busy()

    def sizeHint(self) -> QSize:
        return QSize(1120, 760)
