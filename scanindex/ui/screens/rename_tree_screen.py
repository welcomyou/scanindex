"""Đổi tên theo cây thư mục — công cụ cho cấu trúc CSDL_SOHOA.

Tool độc lập dưới menu "Công cụ": chọn thư mục gốc CSDL_SOHOA, xem cây 4
cấp (Mã định danh → Phông → Mục lục → Hồ sơ → PDF) bên trái, xem trước PDF
bên phải, và đổi tên một thư mục ở cấp bất kỳ: tên PHÔNG và MỤC LỤC bên
dưới giữ nguyên, còn TÊN HỒ SƠ và TÊN FILE PDF bên dưới được DỰNG LẠI từ
bộ mã của chuỗi cha — kể cả khi đang lệch mã cha (được sửa luôn) — theo
quy ước compositional (xem ``scanindex.core.rename_tree``).

Điểm đáng chú ý:
  * Cây dùng lazy populate (chỉ scandir khi bung ra); khi mở chức năng,
    toàn bộ cây của thư mục CSDL được nạp + bung full mặc định (nạp đệ
    quy điều khiển thủ công, tắt repaint trong lúc quét).
  * Trước khi thực thi đổi tên, viewer nhả file handle đang mở
    (Windows chặn rename file đang được đọc).
  * Sau đổi tên, cây được dựng lại nhưng giữ nguyên trạng thái bung/chọn
    nhờ map path cũ → mới trong :class:`RenamePlan`.
  * Kéo-thả để di chuyển — thực thi NGAY không hỏi lại (Ctrl+Z hoàn tác):
    mục lục lên phông khác / phông lên mã định danh khác / hồ sơ lên mục
    lục khác (kể cả thả lên hàng hồ sơ khác). TÊN của chính mục được GIỮ
    NGUYÊN — không tự đánh số lại thứ tự; chỉ hồ sơ / PDF bên dưới được
    dựng lại theo mã của cha đích (``plan_folder_move``).
  * Hồ sơ LẪN PDF đều tuân theo quy tắc "thả ở đâu đứng ở đó": kéo thả
    giữa các hàng chỉ đặt VỊ TRÍ theo thứ tự thủ công (lưu trong settings
    của tool, không đụng cây file), KHÔNG đổi tên. PDF chuyển hồ sơ khác
    giữ nguyên số trang (``plan_pdf_move``); chuyển trong cùng hồ sơ chỉ
    đổi thứ tự hiển thị.
  * Chuột phải hồ sơ / mục lục / tài liệu PDF: "Đánh số từ ... này tự động
    đến hết ..." — đánh số tăng dần theo THỨ TỰ HIỂN THỊ từ mục đó (giữ số
    của nó, không reset về 1) đến hết (``plan_renumber_siblings_from``).
  * Double-click mở hộp thoại đổi tên (không bung/thụt cây); F2 như cũ.
  * Mũi tên lên/xuống lướt qua các PDF (xuyên qua hồ sơ) — viewer hiển thị
    file hiện hành.
  * Ctrl+Z hoàn tác thao tác gần nhất (đổi tên / di chuyển / chuyển PDF /
    sắp xếp / đánh số lại): hoàn tác = chạy ngược toàn bộ op của kế hoạch
    (``plan_invert``).
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QSplitter, QStackedWidget, QTreeWidget, QTreeWidgetItem,
    QTreeWidgetItemIterator, QVBoxLayout, QWidget,
)

from scanindex.core import rename_tree as rt
from scanindex.infra import translations
from scanindex.ui.screens.screen_base import ScreenContent
from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG, COLOR_BORDER, COLOR_PANEL,
    COLOR_SURFACE, COLOR_TEXT, COLOR_TEXT_SECONDARY, FONT_MONO, FONT_UI,
    RADIUS_MD, SP,
)
from scanindex.ui.widgets.pdf_viewer_widget import PdfViewerWidget

try:
    from scanindex.infra.paths import get_base_dir
except Exception:
    def get_base_dir():
        return os.getcwd()

_SETTINGS_FILE = os.path.join(get_base_dir(), "config", "rename_tree_settings.json")

_ROLE_REL = Qt.ItemDataRole.UserRole          # path posix tương đối root
_ROLE_LEVEL = Qt.ItemDataRole.UserRole + 1    # rt.Level | None
_ROLE_ISDIR = Qt.ItemDataRole.UserRole + 2
_ROLE_LOADED = Qt.ItemDataRole.UserRole + 3   # đã populate con chưa
_ROLE_REORDERABLE = Qt.ItemDataRole.UserRole + 4  # PDF hợp lệ được kéo thả
_ROLE_BADGE = Qt.ItemDataRole.UserRole + 5    # nhãn cột Loại chưa dịch
_ROLE_TIP = Qt.ItemDataRole.UserRole + 6      # tooltip chưa dịch

# Icon theo cấp: Mã định danh / Phông / Mục lục = icon thư mục (màu đỏ/xanh
# lá/xanh dương), Hồ sơ = icon hồ sơ tài liệu (trang tài liệu ló sau thư mục,
# màu vàng), PDF / file khác = icon trang giấy.
_LEVEL_COLOR = {
    rt.Level.MA_DINH_DANH: "#ef4444",
    rt.Level.PHONG: "#22c55e",
    rt.Level.MUC_LUC: "#3b82f6",
    rt.Level.HO_SO: "#f5b301",
}
_FOLDER_FALLBACK_COLOR = "#9aa0a6"  # thư mục con sâu/lạ
_DUMMY_TEXT = "\u2026"  # giữ chỗ để hiện mũi tên bung trước khi populate


_ICON_CACHE: dict[tuple[str, str], QIcon] = {}


def _folder_icon(color: str) -> QIcon:
    """Icon thư mục vẽ bằng QPainter, tô màu theo cấp (cache theo màu)."""
    key = ("folder", color)
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached
    dpr = 2.0
    size = 18
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    fill = QColor(color)
    border = QColor(color).darker(190)
    path = QPainterPath()
    path.moveTo(2.0, 13.8)
    path.lineTo(2.0, 4.6)
    path.quadTo(QPointF(2.0, 3.6), QPointF(3.0, 3.6))     # góc tab trái
    path.lineTo(6.8, 3.6)                                  # đỉnh tab
    path.lineTo(8.8, 5.6)                                  # dốc tab → thân
    path.lineTo(15.0, 5.6)
    path.quadTo(QPointF(16.0, 5.6), QPointF(16.0, 6.6))   # góc thân phải
    path.lineTo(16.0, 13.8)
    path.quadTo(QPointF(16.0, 14.8), QPointF(15.0, 14.8)) # góc dưới phải
    path.lineTo(3.0, 14.8)
    path.quadTo(QPointF(2.0, 14.8), QPointF(2.0, 13.8))   # góc dưới trái
    path.closeSubpath()
    painter.setPen(QPen(border, 1.1))
    painter.setBrush(fill)
    painter.drawPath(path)
    # Đường "mép trước" của thư mục ngay dưới tab.
    painter.setPen(QPen(border, 1.0))
    painter.drawLine(QPointF(2.0, 5.6), QPointF(16.0, 5.6))
    painter.end()
    icon = QIcon(pm)
    _ICON_CACHE[key] = icon
    return icon


def _file_icon(color: str = "#eef1f4") -> QIcon:
    """Icon trang giấy (gấp góc phải trên) cho file trong hồ sơ."""
    key = ("file", color)
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached
    dpr = 2.0
    size = 18
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    border = QColor(color).darker(210)
    path = QPainterPath()
    path.moveTo(5.0, 2.6)
    path.lineTo(11.2, 2.6)
    path.lineTo(13.2, 4.6)   # góc gấp
    path.lineTo(13.2, 15.4)
    path.lineTo(5.0, 15.4)
    path.closeSubpath()
    painter.setPen(QPen(border, 1.1))
    painter.setBrush(QColor(color))
    painter.drawPath(path)
    fold = QPainterPath()
    fold.moveTo(11.2, 2.6)
    fold.lineTo(11.2, 4.6)
    fold.lineTo(13.2, 4.6)
    fold.closeSubpath()
    painter.setBrush(QColor(color).darker(160))
    painter.drawPath(fold)
    painter.end()
    icon = QIcon(pm)
    _ICON_CACHE[key] = icon
    return icon


def _dossier_icon(color: str) -> QIcon:
    """Icon hồ sơ tài liệu: trang tài liệu ló sau thư mục hồ sơ (màu theo cấp)."""
    key = ("dossier", color)
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached
    dpr = 2.0
    size = 18
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    fill = QColor(color)
    border = QColor(color).darker(190)
    # Trang tài liệu ló phía sau (trên phải) với vài dòng chữ.
    page = QPainterPath()
    page.moveTo(8.8, 1.6)
    page.lineTo(14.0, 1.6)
    page.lineTo(16.2, 3.8)
    page.lineTo(16.2, 10.6)
    page.lineTo(8.8, 10.6)
    page.closeSubpath()
    painter.setPen(QPen(QColor("#8a93a0"), 1.0))
    painter.setBrush(QColor("#f7f9fb"))
    painter.drawPath(page)
    painter.setPen(QPen(QColor("#aab3bf"), 1.0))
    for y in (3.6, 5.6):
        painter.drawLine(QPointF(9.9, y), QPointF(15.1, y))
    # Thư mục hồ sơ đè phía trước (dưới trái).
    path = QPainterPath()
    path.moveTo(1.8, 14.4)
    path.lineTo(1.8, 6.8)
    path.quadTo(QPointF(1.8, 5.8), QPointF(2.8, 5.8))
    path.lineTo(5.4, 5.8)
    path.lineTo(7.0, 7.4)
    path.lineTo(12.6, 7.4)
    path.quadTo(QPointF(13.6, 7.4), QPointF(13.6, 8.4))
    path.lineTo(13.6, 14.4)
    path.quadTo(QPointF(13.6, 15.4), QPointF(12.6, 15.4))
    path.lineTo(2.8, 15.4)
    path.quadTo(QPointF(1.8, 15.4), QPointF(1.8, 14.4))
    path.closeSubpath()
    painter.setPen(QPen(border, 1.1))
    painter.setBrush(fill)
    painter.drawPath(path)
    painter.setPen(QPen(border, 1.0))
    painter.drawLine(QPointF(1.8, 7.4), QPointF(13.6, 7.4))
    painter.end()
    icon = QIcon(pm)
    _ICON_CACHE[key] = icon
    return icon


# Kéo-thả (đổi thứ tự PDF + di chuyển mục) được điều khiển hoàn toàn bằng
# chuột (xem ``_ReorderTree``), không dùng pipeline DnD nội bộ của QTreeWidget.

# Cấp có thể kéo-di chuyển lên đúng cấp cha trực tiếp trên nó.
_MOVABLE_LEVELS = {rt.Level.PHONG, rt.Level.MUC_LUC, rt.Level.HO_SO}


class _ReorderTree(QTreeWidget):
    """Cây kéo-thả bằng chuột: sắp xếp / chuyển PDF (đơn hay NHÓM) + di chuyển mục.

    Kéo-thả được điều khiển hoàn toàn bằng chuột (press → move vượt ngưỡng
    → release) thay vì pipeline DnD nội bộ của QTreeWidget: không phụ thuộc
    MIME/OS nên hoạt động xác định trên mọi nền tảng, đồng thời không nhận
    file kéo từ Explorer vào. Feedback khi kéo:

    - Chọn nhiều PDF (Ctrl/Shift + nhấp) rồi kéo: **thứ tự nhóm được bảo
      toàn** khi thả.
    - Thả nhóm PDF giữa các hàng PDF (một hồ sơ bất kỳ) → gạch chèn đúng
      vị trí: file ĐỨNG NGAY vị trí đó theo THỨ TỰ THỦ CÔNG, KHÔNG đổi tên
      (phát ``pdf_move_requested`` kèm vị trí; cùng hồ sơ = chỉ đổi thứ tự
      hiển thị, khác hồ sơ = chuyển sang rồi đặt vào chỗ). Tên chỉ đổi khi
      chủ động "đánh số từ tài liệu này" ở menu chuột phải.
    - Thả nhóm PDF lên hàng hồ sơ → khung sáng: chuyển sang rồi đặt xuống
      cuối hồ sơ đó (cũng không đổi số).
    - Hồ sơ kéo qua các hàng hồ sơ → gạch chèn ĐÚNG vị trí: hồ sơ ĐỨNG NGAY
      vị trí đó theo THỨ TỰ THỦ CÔNG (không sort theo tên, không tự đổi số)
      — phát ``ho_so_dropped``; thả lên hàng mục lục = đặt cuối (vị trí -1).
      Khác mục lục: chuyển sang rồi đặt vào vị trí; cùng mục lục: chỉ đổi
      thứ tự hiển thị. Tên chỉ đổi khi người dùng chủ động đổi hoặc dùng
      menu chuột phải "đánh số từ ... này tự động".
    - Phông / Mục lục kéo lên đúng cấp cha → khung sáng (phát
      ``folder_move_requested``). TÊN của mục được GIỮ NGUYÊN — không tự
      đánh số lại; chỉ hồ sơ / PDF bên dưới được dựng lại theo mã cha đích.

    Di chuyển item trong cây chỉ là thị giác; màn hình lập kế hoạch thật và
    thực thi ngay (có Ctrl+Z hoàn tác).
    """

    folder_move_requested = Signal(str, str)    # (rel mục, rel cha đích)
    ho_so_dropped = Signal(str, str, int)       # (rel hồ sơ, rel mục lục đích, vị trí; -1 = cuối)
    pdf_move_requested = Signal(list, str, int)  # (các rel PDF, rel hồ sơ đích, vị trí; -1 = cuối)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_pos: QPoint | None = None
        self._press_item: QTreeWidgetItem | None = None
        self._press_group: list[QTreeWidgetItem] = []
        self._dragging = False
        self._marker_y: int | None = None  # y của gạch chỉ báo vị trí chèn
        self._drop_item: QTreeWidgetItem | None = None  # item đích di chuyển

    # ------------------------------------------------------------ kéo chuột

    def _reorderable(self, item) -> bool:
        return bool(item is not None and item.data(0, _ROLE_REORDERABLE))

    @staticmethod
    def _level(item) -> rt.Level | None:
        return item.data(0, _ROLE_LEVEL) if item is not None else None

    @staticmethod
    def _rel(item) -> str | None:
        return item.data(0, _ROLE_REL) if item is not None else None

    def _draggable(self, item) -> bool:
        """PDF hợp lệ (sắp xếp / chuyển) hoặc thư mục ở cấp di chuyển được."""
        if item is None:
            return False
        if self._reorderable(item):
            return True
        return (bool(item.data(0, _ROLE_ISDIR))
                and self._level(item) in _MOVABLE_LEVELS)

    def _visual_key(self, item) -> tuple[int, ...]:
        """Vị trí duyệt theo chiều sâu — dùng bảo toàn thứ tự nhóm kéo."""
        keys: list[int] = []
        cur = item
        while cur is not None:
            parent = cur.parent()
            keys.append(self.indexOfTopLevelItem(cur) if parent is None
                        else parent.indexOfChild(cur))
            cur = parent
        return tuple(reversed(keys))

    def _drag_group(self, pressed) -> list:
        """Nhóm sẽ kéo: các PDF đang được chọn (press trên một trong chúng),
        ngược lại chỉ item được nhấn. Thứ tự = thứ tự hiển thị."""
        if self._reorderable(pressed):
            selected = [i for i in self.selectedItems()
                        if self._reorderable(i)]
            if pressed in selected and len(selected) > 1:
                return sorted(selected, key=self._visual_key)
        return [pressed]

    def _pdf_folder_drop(self, group, target):
        """Hồ sơ đích hợp lệ để chuyển nhóm PDF vào (None nếu không hợp lệ)."""
        if not bool(target.data(0, _ROLE_ISDIR)):
            return None
        if self._level(target) is not rt.Level.HO_SO:
            return None
        return target

    def _folder_drop(self, src, target):
        """Item cha đích hợp lệ để DI CHUYỂN thư mục src vào; None nếu không."""
        if target is None or target is src or not target.data(0, _ROLE_ISDIR):
            return None
        src_level = self._level(src)
        if self._level(target) is rt.Level.HO_SO:
            return None  # hàng hồ sơ xử lý bằng gạch chèn (marker)
        src_rel, tgt_rel = self._rel(src), self._rel(target)
        tgt_level = self._level(target)
        if not src_rel or not tgt_rel:
            return None
        if src_level not in _MOVABLE_LEVELS or tgt_level is None:
            return None
        if tgt_level.value != src_level.value - 1:
            return None
        if tgt_rel == Path(src_rel).parent.as_posix():
            # Hồ sơ thả lên chính mục lục của nó = đặt xuống CUỐI (thứ tự
            # thủ công); phông/mục lục thì không có gì để làm.
            if src_level is not rt.Level.HO_SO:
                return None
        if tgt_rel.startswith(src_rel + "/"):
            return None
        return target

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._press_pos = None
        self._press_item = None
        self._press_group = []
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            item = self.itemAt(pos)
            if self._draggable(item):
                self._press_pos = pos
                self._press_item = item
                self._press_group = self._drag_group(item)

    def mouseMoveEvent(self, event):
        if self._press_pos is not None and not self._dragging:
            delta = event.position().toPoint() - self._press_pos
            if delta.manhattanLength() >= QApplication.startDragDistance():
                self._dragging = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
        if self._dragging:
            self._auto_scroll(event.position().toPoint())
            self._update_drop_feedback(event.position().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._finish_drag(event.position().toPoint())
            event.accept()
            return
        super().mouseReleaseEvent(event)
        self._reset_drag_state()

    def _auto_scroll(self, vpos: QPoint):
        """Cuộn cây khi rê sát mép trên/dưới để kéo đi xa trong cây lớn."""
        margin, step = 20, 16
        sb = self.verticalScrollBar()
        if vpos.y() <= margin:
            sb.setValue(sb.value() - step)
        elif vpos.y() >= self.viewport().height() - margin:
            sb.setValue(sb.value() + step)

    def _update_drop_feedback(self, vpos: QPoint):
        group = self._press_group
        target = self.itemAt(vpos)
        self._marker_y = None
        self._drop_item = None
        if group and target is not None:
            if self._reorderable(group[0]):  # nhóm PDF
                if self._reorderable(target) and target not in group:
                    # Gạch chèn giữa các hàng PDF — vừa để sắp xếp trong hồ sơ,
                    # vừa để chèn vào vị trí của hồ sơ khác (dồn số phần sau).
                    rect = self.visualItemRect(target)
                    self._marker_y = (
                        rect.bottom() + 1 if vpos.y() > rect.center().y()
                        else rect.top() - 1
                    )
                else:
                    self._drop_item = self._pdf_folder_drop(group, target)
            else:  # phông / mục lục / hồ sơ
                src = group[0]
                if (self._level(src) is rt.Level.HO_SO
                        and self._level(target) is rt.Level.HO_SO
                        and target is not src
                        and target.parent() is not None
                        and self._level(target.parent()) is rt.Level.MUC_LUC):
                    # Gạch chèn: đặt hồ sơ đúng vị trí (thứ tự thủ công).
                    rect = self.visualItemRect(target)
                    self._marker_y = (
                        rect.bottom() + 1 if vpos.y() > rect.center().y()
                        else rect.top() - 1
                    )
                else:
                    self._drop_item = self._folder_drop(src, target)
        self.viewport().update()

    def _finish_drag(self, vpos: QPoint):
        group = self._press_group
        self._update_drop_feedback(vpos)
        if group and self._marker_y is not None:
            target = self.itemAt(vpos)
            if self._reorderable(group[0]):
                # Đặt nhóm PDF vào đúng vị trí thả (thứ tự thủ công, không
                # đổi tên) — cùng hồ sơ hay khác hồ sơ đều một luồng.
                parent = target.parent()
                ordinals = [
                    parent.child(i) for i in range(parent.childCount())
                    if self._reorderable(parent.child(i))
                ]
                rect = self.visualItemRect(target)
                insert_at = ordinals.index(target) + (
                    1 if vpos.y() > rect.center().y() else 0)
                insert_at -= sum(
                    1 for m in group if m in ordinals
                    and ordinals.index(m) < insert_at)
                insert_at = max(0, insert_at)
                self.pdf_move_requested.emit(
                    [self._rel(m) for m in group],
                    parent.data(0, _ROLE_REL), insert_at,
                )
            else:  # đặt hồ sơ vào đúng vị trí thả (thứ tự thủ công)
                src = group[0]
                parent = target.parent()
                ordinals = [
                    parent.child(i) for i in range(parent.childCount())
                    if parent.child(i).data(0, _ROLE_ISDIR)
                ]
                rect = self.visualItemRect(target)
                insert_at = ordinals.index(target) + (
                    1 if vpos.y() > rect.center().y() else 0)
                if parent is src.parent() and src in ordinals:
                    if ordinals.index(src) < insert_at:
                        insert_at -= 1  # hồ sơ nguồn rời vị trí cũ trước
                self.ho_so_dropped.emit(
                    self._rel(src), parent.data(0, _ROLE_REL), insert_at)
        elif group and self._drop_item is not None:
            if self._reorderable(group[0]):
                self.pdf_move_requested.emit(
                    [self._rel(m) for m in group],
                    self._rel(self._drop_item), -1,
                )
            elif self._level(group[0]) is rt.Level.HO_SO:
                # Thả lên hàng mục lục = đặt hồ sơ xuống cuối (thứ tự thủ công).
                self.ho_so_dropped.emit(
                    self._rel(group[0]), self._rel(self._drop_item), -1)
            else:
                self.folder_move_requested.emit(
                    self._rel(group[0]), self._rel(self._drop_item),
                )
        self._reset_drag_state()

    def _reset_drag_state(self):
        was_dragging = self._dragging
        self._press_pos = None
        self._press_item = None
        self._press_group = []
        self._dragging = False
        self._marker_y = None
        self._drop_item = None
        if was_dragging:
            self.unsetCursor()
            self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._dragging:
            return
        painter = QPainter(self.viewport())
        if self._marker_y is not None:
            painter.setPen(QPen(QColor(COLOR_ACCENT), 2))
            width = self.viewport().width()
            painter.drawLine(0, self._marker_y, width, self._marker_y)
        elif self._drop_item is not None:
            rect = self.visualItemRect(self._drop_item).adjusted(-4, 0, 4, 0)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(COLOR_ACCENT), 2))
            fill = QColor(COLOR_ACCENT)
            fill.setAlpha(38)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 4.0, 4.0)
        painter.end()


# --------------------------------------------------------------------------- #
# Hộp thoại đổi tên (có preview live dựng từ plan thật)
# --------------------------------------------------------------------------- #

class _RenameDialog(QDialog):
    """Nhập mã mới cho cấp đang đổi; preview tên đầy đủ + số mục ảnh hưởng.

    ``title`` / ``hint`` ghi đè tiêu đề và nhãn hướng dẫn mặc định theo cấp
    (dùng cho hộp thoại đổi số thứ tự tài liệu PDF).
    """

    def __init__(self, parent, level: rt.Level, old_name: str,
                 current_value: str, preview_fn, parent_chain: str = "",
                 title: str = "", hint: str = ""):
        super().__init__(parent)
        self.setWindowTitle(
            title
            or translations.localize_text(
                f"Đổi tên {rt.LEVEL_LABELS[level].lower()}"
            )
        )
        self.setMinimumWidth(580)
        self._preview_fn = preview_fn
        self.plan: rt.RenamePlan | None = None

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(SP[4], SP[4], SP[4], SP[4])
        vbox.setSpacing(SP[2])

        if parent_chain:
            chain = QLabel(translations.localize_text(parent_chain))
            chain.setStyleSheet(
                f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
            )
            chain.setWordWrap(True)
            vbox.addWidget(chain)

        lbl_old = QLabel(translations.localize_text(
            f"Tên hiện tại:  <span style='font-family:\"{FONT_MONO}\";'>{old_name}</span>"
        ))
        lbl_old.setStyleSheet(f"color: {COLOR_TEXT}; font: 13px '{FONT_UI}';")
        vbox.addWidget(lbl_old)

        hint = translations.localize_text(hint or {
            rt.Level.MA_DINH_DANH: (
                "Mã định danh mới (phông, mục lục giữ nguyên; "
                "hồ sơ, PDF đổi theo):"),
            rt.Level.PHONG: "Mã phông mới (hồ sơ, PDF đổi theo):",
            rt.Level.MUC_LUC: (
                "Số mục lục mới (bắt buộc đúng 2 chữ số; hồ sơ, PDF đổi theo):"),
            rt.Level.HO_SO: (
                "Số hồ sơ mới (bắt buộc đúng 4 chữ số; hoặc dán cả tên 4 đoạn):"),
        }[level])
        lbl_new = QLabel(hint)
        lbl_new.setStyleSheet(f"color: {COLOR_TEXT}; font: 600 13px '{FONT_UI}';")
        vbox.addWidget(lbl_new)

        self.edit = QLineEdit(current_value)
        self.edit.setStyleSheet(
            f"QLineEdit {{ background: {COLOR_SURFACE}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD}px;"
            f" padding: 6px 10px; font: 13px '{FONT_MONO}'; }}"
            f"QLineEdit:focus {{ border-color: {COLOR_ACCENT}; }}"
        )
        self.edit.selectAll()
        vbox.addWidget(self.edit)

        self.lbl_result = QLabel("")
        self.lbl_result.setWordWrap(True)
        self.lbl_result.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_result.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
        )
        vbox.addWidget(self.lbl_result)

        self.lbl_summary = QLabel("")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
        )
        vbox.addWidget(self.lbl_summary)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_cancel = QPushButton("Hủy")
        self.btn_ok = QPushButton("Đổi tên")
        for btn, primary in ((self.btn_cancel, False), (self.btn_ok, True)):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background: {COLOR_ACCENT if primary else 'transparent'};"
                f" color: {'white' if primary else COLOR_TEXT_SECONDARY};"
                f" border: {'none' if primary else f'1px solid {COLOR_BORDER}'};"
                f" padding: 7px 18px; border-radius: {RADIUS_MD}px;"
                f" font: 600 13px '{FONT_UI}'; }}"
                f"QPushButton:hover {{ background: {COLOR_ACCENT_HOVER if primary else COLOR_PANEL}; }}"
                f"QPushButton:disabled {{ background: #555; color: #aaa; }}"
            )
            buttons.addWidget(btn)
        vbox.addLayout(buttons)

        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok.clicked.connect(self._on_ok)
        self.edit.returnPressed.connect(self._on_ok)

        # Debounce preview để không scan lại cây quá dày sau mỗi phím gõ.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._refresh_preview)
        self.edit.textChanged.connect(lambda: self._debounce.start())
        self._refresh_preview()

    def _refresh_preview(self):
        self.plan = None
        try:
            plan = self._preview_fn(self.edit.text())
        except ValueError as exc:
            self.btn_ok.setEnabled(False)
            self.lbl_result.setText(
                "<span style='color:#f87171;'>✖ "
                f"{translations.localize_text(str(exc))}</span>"
            )
            self.lbl_summary.setText("")
            return
        self.plan = plan
        if not plan.ops:
            self.btn_ok.setEnabled(False)
            self.lbl_result.setText(translations.localize_text(
                f"Tên mới: <span style='font-family:\"{FONT_MONO}\";'>{plan.new_name}</span>"
            ))
            self.lbl_summary.setText(
                translations.localize_text("Tên không thay đổi.")
            )
            return
        self.btn_ok.setEnabled(True)
        self.lbl_result.setText(translations.localize_text(
            f"✔ Tên mới: <span style='font-family:\"{FONT_MONO}\";'>{plan.new_name}</span>"
        ))
        skipped = ""
        if plan.skipped:
            skipped = " · " + translations.localize_text(
                f"{len(plan.skipped)} mục bỏ qua (không khớp quy ước)")
        self.lbl_summary.setText(translations.localize_text(
            f"Sẽ đổi tên {plan.affected_dirs} thư mục và {plan.affected_pdfs} "
            f"file PDF bên trong{skipped}."
        ))

    def _on_ok(self):
        self._debounce.stop()
        self._refresh_preview()
        if self.plan is not None and self.plan.ops:
            self.accept()


# --------------------------------------------------------------------------- #
# Worker thực thi plan (rename hàng loạt có thể hàng nghìn op)
# --------------------------------------------------------------------------- #

class _RenameWorker(QThread):
    progress = Signal(int, int)
    done = Signal(object, object)  # (RenamePlan, ExecuteResult)

    def __init__(self, plan: rt.RenamePlan, parent=None):
        super().__init__(parent)
        self._plan = plan
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        result = rt.execute_plan(
            self._plan,
            progress_cb=lambda d, t: self.progress.emit(d, t),
            cancel_cb=lambda: self._cancel,
        )
        self.done.emit(self._plan, result)


# --------------------------------------------------------------------------- #
# Hoàn tác (Ctrl+Z)
# --------------------------------------------------------------------------- #

@dataclass
class _UndoEntry:
    """Một thao tác đã thực thi, dựng lại được kế hoạch hoàn tác.

    ``build`` chạy lúc hoàn tác (Ctrl+Z): dựng kế hoạch đảo ngược op của
    kế hoạch gốc — chạy ngược từ trạng thái cuối về trạng thái đầu. Nếu
    cây đã đổi khác khiến op hỏng thì thực thi tự rollback và báo lỗi.

    ``orders`` là ảnh WHOLE của thứ tự thủ công TRƯỚC thao tác — hoàn tác
    khôi phục lại cùng lúc với op trên đĩa (kéo thả đổi cả hai).

    ``apply`` (nếu có) là hoàn tác thuần hiển thị cho kéo-thả chỉ xếp lại
    thứ tự trong cùng cha (không đụng đĩa) — khôi phục ``orders`` và vẽ
    lại cây, không chạy kế hoạch nào.
    """
    title: str
    build: Callable[[], rt.RenamePlan] | None = None
    apply: Callable[[], None] | None = None
    orders: dict[str, list[str]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Màn hình chính
# --------------------------------------------------------------------------- #

class RenameTreeScreen(ScreenContent):
    """Công cụ đổi tên theo cây thư mục CSDL_SOHOA."""

    log_message = Signal(str, str)

    _UNDO_LIMIT = 100

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {COLOR_BG};")
        self._root: Path | None = None
        self._worker: _RenameWorker | None = None
        self._executing = False   # worker đã chạy xong nhưng done chưa về tới slot
        self._current_pdf_rel: str | None = None
        self._undo_stack: list[_UndoEntry] = []
        self._pending_undo: _UndoEntry | None = None
        # Thứ tự hồ sơ THỦ CÔNG theo từng mục lục: {ml_rel: [tên thư mục hồ
        # sơ theo thứ tự hiển thị]}. Cây KHÔNG sort hồ sơ theo tên khi mục
        # lục có mục này — người dùng kéo thả đặt vị trí, số chỉ đổi khi chủ
        # động đánh số lại (lưu kèm settings của tool, không đụng cây file).
        self._manual_order: dict[str, list[str]] = {}
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

        self.btn_pick = QPushButton("📂  Chọn thư mục CSDL_SOHOA…")
        self.btn_pick.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pick.setStyleSheet(
            f"QPushButton {{ background: {COLOR_ACCENT}; color: white;"
            f" border: none; padding: 8px 16px; border-radius: {RADIUS_MD}px;"
            f" font: 600 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}"
        )
        self.btn_pick.clicked.connect(self._pick_root)
        h.addWidget(self.btn_pick)

        self.edit_root = QLineEdit()
        self.edit_root.setReadOnly(True)
        self.edit_root.setPlaceholderText("Chưa chọn thư mục gốc")
        self.edit_root.setStyleSheet(
            f"QLineEdit {{ background: {COLOR_SURFACE}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD}px;"
            f" padding: 6px 10px; font: 12px '{FONT_MONO}'; }}"
        )
        h.addWidget(self.edit_root, 1)

        self.btn_refresh = QPushButton("🔄  Làm mới")
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f" border: 1px solid {COLOR_BORDER}; padding: 6px 14px;"
            f" border-radius: {RADIUS_MD}px; font: 500 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_PANEL}; color: {COLOR_TEXT}; }}"
        )
        self.btn_refresh.clicked.connect(self._refresh_tree)
        h.addWidget(self.btn_refresh)

        self.lbl_stats = QLabel("")
        self.lbl_stats.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
        )
        h.addWidget(self.lbl_stats)

        outer.addWidget(bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_tree_panel())
        splitter.addWidget(self._build_preview_panel())
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([520, 680])
        splitter.setHandleWidth(8)
        splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {COLOR_BORDER}; border-radius: 2px; }}"
            f"QSplitter::handle:hover {{ background: {COLOR_ACCENT}; }}"
        )
        outer.addWidget(splitter, 1)

        # Ctrl+Z đặt trên cả màn hình (cả cây lẫn viewer đang focus đều dùng
        # được) vì hoàn tác áp cho thao tác cây gần nhất, không theo focus.
        undo_sc = QShortcut(QKeySequence("Ctrl+Z"), self)
        undo_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        undo_sc.activated.connect(self._undo_last)

    def _build_tree_panel(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background: {COLOR_PANEL}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(SP[2], SP[2], SP[2], SP[2])
        vbox.setSpacing(SP[1])

        self.tree = _ReorderTree()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Tên", "Loại"])
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(18, 18))
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Double-click chỉ để đổi tên — không bung/thụt cây cho khỏi nhiễu.
        self.tree.setExpandsOnDoubleClick(False)
        # Cột Tên dàn đầy chiều rộng còn lại → cột Loại (ResizeToContents,
        # căn phải) luôn ôm sát lề phải khung cây.
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
            f"QTreeWidget::item {{ padding: 4px 2px; border-radius: 4px; }}"
            # :hover đặt TRƯỚC :selected và có cả :selected:hover để item đang
            # chọn luôn sáng accent ngay cả khi chuột còn đang rê trên nó.
            f"QTreeWidget::item:hover {{ background: {COLOR_SURFACE}; }}"
            f"QTreeWidget::item:selected {{ background: {COLOR_ACCENT}; color: white; }}"
            f"QTreeWidget::item:selected:hover {{ background: {COLOR_ACCENT}; color: white; }}"
            f"QHeaderView::section {{ background: {COLOR_SURFACE};"
            f" color: {COLOR_TEXT_SECONDARY}; border: none; padding: 4px 6px;"
            f" font: 600 11px '{FONT_UI}'; }}"
        )
        self.tree.itemExpanded.connect(lambda item: self._populate_children(item))
        self.tree.currentItemChanged.connect(self._on_current_changed)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.folder_move_requested.connect(self._on_folder_move_requested)
        self.tree.ho_so_dropped.connect(self._on_ho_so_dropped)
        self.tree.pdf_move_requested.connect(self._on_pdf_move_requested)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        vbox.addWidget(self.tree, 1)

        shortcut = QShortcut(QKeySequence("F2"), self.tree)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self._rename_selected)
        return panel

    def _build_preview_panel(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background: {COLOR_PANEL}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(SP[2], SP[2], SP[2], SP[2])
        vbox.setSpacing(SP[1])

        self.lbl_preview_path = QLabel("")
        self.lbl_preview_path.setWordWrap(True)
        self.lbl_preview_path.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_MONO}';"
        )
        vbox.addWidget(self.lbl_preview_path)

        self.preview_stack = QStackedWidget()
        empty = QLabel("Chọn một file PDF trong cây để xem trước.")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 13px '{FONT_UI}';"
        )
        self.preview_stack.addWidget(empty)  # trang 0
        self.pdf_viewer = PdfViewerWidget()
        self.preview_stack.addWidget(self.pdf_viewer)  # trang 1
        vbox.addWidget(self.preview_stack, 1)
        return panel

    # --------------------------------------------------------- chọn thư mục

    def _pick_root(self):
        if self.is_busy():
            return
        folder = QFileDialog.getExistingDirectory(
            self, translations.localize_text("Chọn thư mục gốc CSDL_SOHOA"),
            str(self._root) if self._root else "",
        )
        if not folder:
            return
        self._set_root(Path(folder))

    def _set_root(self, path: Path):
        self._root = Path(path)
        self.edit_root.setText(str(self._root))
        self._current_pdf_rel = None
        self._undo_stack.clear()
        self._pending_undo = None
        self._manual_order = {}  # thứ tự thủ công chỉ áp cho cây đang mở
        self.lbl_preview_path.setText("")
        self.preview_stack.setCurrentIndex(0)
        self.pdf_viewer.clear()
        self._reload_root()
        if self.isVisible():
            # Chọn/đổi thư mục gốc khi chức năng đang mở → về mặc định bung
            # full (lúc khởi động app trang còn ẩn, showEvent sẽ lo việc bung).
            QTimer.singleShot(0, self, self._expand_all_loaded)
        self._save_settings()
        self.log_message.emit(
            translations.localize_text(
                f"Đổi tên cây thư mục: đã mở {self._root}"
            ),
            "info",
        )

    def _refresh_tree(self):
        if self._root is None or self.is_busy():
            return
        self._reload_root()
        # Làm mới = quay về trạng thái mặc định: bung full toàn bộ cây.
        self._expand_all_loaded()

    def showEvent(self, event):
        """Mở chức năng: mặc định bung full toàn bộ cây thư mục CSDL.

        Nạp đệ quy chạy ở tick kế tiếp (singleShot) để trang kịp hiển thị
        trước khi quét cây; chỉ áp khi màn hình thấy được — lúc khởi động
        app (nạp lại settings khi trang còn ẩn) thì đợi tới lần mở thật.
        """
        super().showEvent(event)
        if self.isVisible() and self._root is not None and not self.is_busy():
            QTimer.singleShot(0, self, self._expand_all_loaded)

    def _expand_all_loaded(self):
        """Nạp đệ quy + bung toàn bộ cây (trạng thái mặc định khi mở chức năng).

        Đi qua lazy populate của từng item nhưng điều khiển thủ công — nạp
        hết cấp con rồi mới mở, tắt repaint trong lúc quét để cây hàng nghìn
        hồ sơ không nhấp nháy/treo từng nhánh.
        """
        if self._root is None or self.is_busy():
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

    def _reload_root(self, expanded: set[str] | None = None,
                     selected: str | None = None):
        """Nạp lại cây; ``expanded``/``selected`` là rel path (đã map) cần khôi phục."""
        self.tree.clear()
        if self._root is None:
            self._update_stats({})
            return
        try:
            entries = sorted(self._root.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            QMessageBox.warning(self, "Không đọc được thư mục", str(exc))
            return
        for entry in entries:
            if entry.is_dir():
                self.tree.addTopLevelItem(self._make_dir_item(entry))
        # Bung cha trước con (đi theo độ sâu tăng dần) để _item_for_rel tìm thấy.
        for rel in sorted(expanded or set(), key=lambda r: r.count("/")):
            item = self._item_for_rel(rel)
            if item is not None:
                self.tree.expandItem(item)
        if selected:
            item = self._item_for_rel(selected)
            if item is not None:
                self.tree.setCurrentItem(item)
        self._update_stats(rt.scan_stats(self._root))

    def _update_stats(self, stats: dict[str, int]):
        if not stats:
            self.lbl_stats.setText("")
            return
        self.lbl_stats.setText(translations.localize_text(
            f"{stats['mdd']} mã định danh · {stats['phong']} phông · "
            f"{stats['ho_so']} hồ sơ · {stats['pdf']} PDF"
        ))

    # ------------------------------------------------------------- cây item

    def _make_dir_item(self, path: Path) -> QTreeWidgetItem:
        rel = path.relative_to(self._root).as_posix()
        level = rt.level_of(Path(rel))
        badge = rt.LEVEL_LABELS.get(level, "Thư mục con")
        name = path.name
        bad = (
            (level is rt.Level.HO_SO
             and rt.parse_dossier_folder_name(path.name) is None)
            or (level in (rt.Level.MA_DINH_DANH, rt.Level.PHONG)
                and rt.SEGMENT_SEP in path.name)
        )
        item = QTreeWidgetItem(
            [f"{'⚠ ' if bad else ''}{name}", translations.localize_text(badge)]
        )
        item.setData(0, _ROLE_REL, rel)
        item.setData(0, _ROLE_LEVEL, level)
        item.setData(0, _ROLE_ISDIR, True)
        item.setData(0, _ROLE_LOADED, False)
        item.setData(0, _ROLE_BADGE, badge)
        if level is rt.Level.HO_SO:
            item.setIcon(0, _dossier_icon(
                _LEVEL_COLOR.get(level, _FOLDER_FALLBACK_COLOR)
            ))
        else:
            item.setIcon(0, _folder_icon(
                _LEVEL_COLOR.get(level, _FOLDER_FALLBACK_COLOR)
            ))
        if bad:
            tip = ("Tên không khớp quy ước — mục này sẽ bị bỏ qua "
                   "khi đổi tên cấp cha.")
            item.setData(0, _ROLE_TIP, tip)
            item.setToolTip(0, translations.localize_text(tip))
        item.setForeground(1, QColor(COLOR_TEXT_SECONDARY))
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
        # Dummy child để hiện mũi tên bung (lazy populate khi expand).
        item.addChild(QTreeWidgetItem([_DUMMY_TEXT]))
        return item

    def _make_file_item(self, path: Path) -> QTreeWidgetItem:
        rel = path.relative_to(self._root).as_posix()
        is_pdf = path.suffix.lower() == ".pdf"
        parsed = rt.parse_pdf_name(path.name)
        badge = "PDF" if parsed else ("PDF (lệch tên)" if is_pdf else "File khác")
        name = path.name
        bad = is_pdf and parsed is None
        item = QTreeWidgetItem(
            [f"{'⚠ ' if bad else ''}{name}", translations.localize_text(badge)]
        )
        item.setData(0, _ROLE_REL, rel)
        item.setData(0, _ROLE_LEVEL, None)
        item.setData(0, _ROLE_ISDIR, False)
        item.setData(0, _ROLE_REORDERABLE, parsed is not None)
        item.setData(0, _ROLE_BADGE, badge)
        item.setIcon(0, _file_icon("#eef1f4" if is_pdf else "#cdd3da"))
        if bad:
            tip = ("Tên PDF không khớp quy ước 5 đoạn — sẽ không "
                   "được đổi tên theo hồ sơ.")
            item.setData(0, _ROLE_TIP, tip)
            item.setToolTip(0, translations.localize_text(tip))
        item.setForeground(1, QColor(COLOR_TEXT_SECONDARY))
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _populate_children(self, item: QTreeWidgetItem):
        if item.data(0, _ROLE_LOADED) or not item.data(0, _ROLE_ISDIR):
            return
        item.setData(0, _ROLE_LOADED, True)
        # Xóa giữ chỗ "…" nếu có.
        for i in range(item.childCount()):
            if item.child(i).text(0) == _DUMMY_TEXT:
                item.removeChild(item.child(i))
                break
        rel = item.data(0, _ROLE_REL)
        if rel is None:
            return
        base = self._root / Path(rel)
        try:
            entries = sorted(
                base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except OSError:
            return
        # Mục lục / hồ sơ có thứ tự thủ công → dùng nó thay vì sort theo tên.
        if rt.level_of(Path(rel)) in (rt.Level.MUC_LUC, rt.Level.HO_SO) \
                and rel in self._manual_order:
            entries = self._apply_manual_order(rel, entries)
        for entry in entries:
            if entry.is_dir():
                item.addChild(self._make_dir_item(entry))
            else:
                item.addChild(self._make_file_item(entry))

    def _apply_manual_order(self, rel: str, entries: list) -> list:
        """Sắp con của item theo thứ tự thủ công; mục lạ xếp sau theo tên."""
        order = self._manual_order.get(rel) or []
        rank = {name: i for i, name in enumerate(order)}

        def key(p):
            if p.name in rank:  # hồ sơ hoặc PDF — đều rank theo tên
                return (0, f"{rank[p.name]:06d}", "")
            return (1 if p.is_dir() else 2, p.name.lower(), p.name)

        return sorted(entries, key=key)

    def _child_order(self, rel: str, *, dirs: bool = True) -> list[str]:
        """Thứ tự hiển thị hiện tại của các con của một item.

        ``dirs=True``: các thư mục hồ sơ con của mục lục; ``dirs=False``:
        các file PDF hợp lệ con của hồ sơ (cùng cơ sở với gạch chèn kéo-thả).
        """
        item = self._item_for_rel(rel)
        if item is not None:
            names = []
            for i in range(item.childCount()):
                child = item.child(i)
                if not child.data(0, _ROLE_REL):
                    continue
                if dirs:
                    if child.data(0, _ROLE_ISDIR):
                        names.append(Path(child.data(0, _ROLE_REL)).name)
                elif child.data(0, _ROLE_REORDERABLE):
                    names.append(Path(child.data(0, _ROLE_REL)).name)
            if names:
                return names
        try:
            if dirs:
                entries = [e for e in (self._root / Path(rel)).iterdir()
                           if e.is_dir()]
            else:
                entries = [e for e in (self._root / Path(rel)).iterdir()
                           if e.is_file()
                           and rt.parse_pdf_name(e.name) is not None]
            if rel in self._manual_order:
                entries = self._apply_manual_order(rel, entries)
            else:
                entries.sort(key=lambda e: e.name.lower())
            return [e.name for e in entries]
        except OSError:
            return []

    def _dir_child_order(self, rel: str) -> list[str]:
        return self._child_order(rel, dirs=True)

    def _set_manual_order(self, ml_rel: str, names: list[str]) -> None:
        self._manual_order[ml_rel] = list(names)
        self._save_settings()

    def _repopulate_item(self, rel: str) -> None:
        """Nạp lại con của một item (áp thứ tự thủ công nếu có)."""
        item = self._item_for_rel(rel)
        if item is None:
            return
        item.takeChildren()
        item.setData(0, _ROLE_LOADED, False)
        self._populate_children(item)

    def _on_current_changed(self, current: QTreeWidgetItem,
                             _prev: QTreeWidgetItem | None):
        """Mũi tên lên/xuống (và nhấp chuột) lướt qua PDF nào thì viewer
        hiển thị PDF đó — đi xuyên qua cả ranh giới hồ sơ."""
        if current is None or current.data(0, _ROLE_ISDIR):
            return
        rel = current.data(0, _ROLE_REL)
        if rel and rel.lower().endswith(".pdf"):
            self._show_pdf(rel)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _col: int):
        if item.data(0, _ROLE_ISDIR):
            if item.data(0, _ROLE_LEVEL) is not None:
                self._open_rename_dialog(item)
        elif item.data(0, _ROLE_REORDERABLE):
            # Double-click tài liệu PDF → đổi số thứ tự (NNN).
            self._open_pdf_stt_dialog(item)

    def _on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        is_dir = bool(item.data(0, _ROLE_ISDIR))
        level = item.data(0, _ROLE_LEVEL)
        if is_dir and level is not None:
            act = menu.addAction(translations.localize_text(
                f"Đổi tên {rt.LEVEL_LABELS[level].lower()} (F2)"))
            act.triggered.connect(lambda: self._open_rename_dialog(item))
        elif not is_dir and item.data(0, _ROLE_REORDERABLE):
            act = menu.addAction(translations.localize_text(
                "Đổi số thứ tự tài liệu (F2)"))
            act.triggered.connect(lambda: self._open_pdf_stt_dialog(item))
        if is_dir and level in (rt.Level.HO_SO, rt.Level.MUC_LUC):
            menu.addSeparator()
            end_of = "mục lục" if level is rt.Level.HO_SO else "phông"
            act = menu.addAction(translations.localize_text(
                f"Đánh số từ {rt.LEVEL_LABELS[level].lower()} này tự động "
                f"đến hết {end_of}"))
            act.triggered.connect(lambda: self._renumber_from(item))
        elif not is_dir and item.data(0, _ROLE_REORDERABLE):
            menu.addSeparator()
            act = menu.addAction(translations.localize_text(
                "Đánh số từ tài liệu này tự động đến hết hồ sơ"))
            act.triggered.connect(lambda: self._renumber_from(item))
        rel = item.data(0, _ROLE_REL)
        if rel:
            menu.addSeparator()
            act_open = menu.addAction(translations.localize_text(
                "Mở trong Explorer"))
            target = self._root / Path(rel)
            if not is_dir:
                target = target.parent
            act_open.triggered.connect(lambda: self._open_in_explorer(target))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _renumber_from(self, item: QTreeWidgetItem):
        """Đánh số tăng dần từ hồ sơ/mục lục được chọn đến hết (giữ số của nó).

        Với hồ sơ: đánh theo THỨ TỰ HIỂN THỊ hiện tại (thứ tự kéo-thả thủ
        công nếu có) — không reset về 1, số tiếp diễn từ hồ sơ được chọn.
        Sau khi đánh số, tên đã mang đúng thứ tự → bỏ thứ tự thủ công của
        mục lục này.
        """
        if self.is_busy() or self._root is None:
            return
        rel = item.data(0, _ROLE_REL)
        if not rel:
            return
        order = None
        container = None
        orders_before = None
        level = item.data(0, _ROLE_LEVEL)
        if not item.data(0, _ROLE_ISDIR):  # tài liệu PDF
            container = Path(rel).parent.as_posix()
            order = self._child_order(container, dirs=False)
        elif level is rt.Level.HO_SO:
            container = Path(rel).parent.as_posix()
            order = self._dir_child_order(container)
        try:
            plan = rt.plan_renumber_siblings_from(
                self._root, Path(rel), order)
        except ValueError as exc:
            QMessageBox.warning(self, "Đánh số tự động", str(exc))
            return
        if not plan.ops:
            QMessageBox.information(
                self, "Đánh số tự động",
                "Các mục phía sau đã đúng thứ tự — không cần đổi tên gì.",
            )
            return
        if container is not None:
            # Tên đã mang đúng thứ tự hiển thị → bỏ thứ tự thủ công của cha.
            orders_before = self._snapshot_orders()
            self._manual_order.pop(container, None)
            self._save_settings()
        self._execute_plan(plan, orders_before=orders_before)

    def _open_in_explorer(self, path: Path):
        try:
            os.startfile(str(path))
        except OSError as exc:
            QMessageBox.information(self, "Mở thư mục", f"Không mở được:\n{exc}")

    def _item_for_rel(self, rel: str) -> QTreeWidgetItem | None:
        """Tìm item theo rel path, tự populate các cấp trung gian nếu cần."""
        parts = [p for p in rel.split("/") if p]
        if not parts:
            return None
        item = None
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.data(0, _ROLE_REL) == parts[0]:
                item = top
                break
        for part in parts[1:]:
            if item is None:
                return None
            self._populate_children(item)
            expected = item.data(0, _ROLE_REL) + "/" + part
            item = next(
                (item.child(i) for i in range(item.childCount())
                 if item.child(i).data(0, _ROLE_REL) == expected),
                None,
            )
        return item

    # ------------------------------------------------------------- viewer

    def _show_pdf(self, rel: str):
        if self.is_busy() or self._root is None:
            return
        path = self._root / Path(rel)
        if not path.is_file():
            return
        self._current_pdf_rel = rel
        self.lbl_preview_path.setText(rel)
        self.preview_stack.setCurrentIndex(1)
        self.pdf_viewer.show_pdf(str(path))

    def _restore_pdf_preview(self, rel: str):
        # A delayed callback can outlive the selection or the next Ctrl+Z.
        if self._current_pdf_rel == rel:
            self._show_pdf(rel)

    # ------------------------------------------------------------- đổi tên

    def _rename_selected(self):
        item = self.tree.currentItem()
        if item is None:
            return
        if item.data(0, _ROLE_ISDIR):
            if item.data(0, _ROLE_LEVEL) is not None:
                self._open_rename_dialog(item)
        elif item.data(0, _ROLE_REORDERABLE):
            self._open_pdf_stt_dialog(item)  # F2 trên PDF = đổi số thứ tự

    def _open_pdf_stt_dialog(self, item: QTreeWidgetItem):
        """Double-click / F2 trên tài liệu PDF: đổi số thứ tự (đúng 3 chữ số)."""
        if self.is_busy() or self._root is None:
            return
        rel = item.data(0, _ROLE_REL)
        if not rel:
            return
        parsed = rt.parse_pdf_name(Path(rel).name)
        if parsed is None:
            return
        dlg = _RenameDialog(
            self, rt.Level.HO_SO, Path(rel).name, parsed.stt,
            preview_fn=lambda value, _rel=rel: rt.plan_pdf_rename_stt(
                self._root, Path(_rel), value
            ),
            parent_chain=translations.localize_text(
                "Thuộc: " + (self._root / Path(rel).parent).as_posix()),
            title=translations.localize_text("Đổi số thứ tự tài liệu"),
            hint=translations.localize_text(
                "Số thứ tự mới (bắt buộc đúng 3 chữ số, ví dụ 001):"),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.plan is None:
            return
        self._execute_plan(dlg.plan)

    def _open_rename_dialog(self, item: QTreeWidgetItem):
        if self.is_busy() or self._root is None:
            return
        rel = item.data(0, _ROLE_REL)
        level = item.data(0, _ROLE_LEVEL)
        old_name = Path(rel).name
        current = old_name
        if level is rt.Level.HO_SO:
            parsed = rt.parse_dossier_folder_name(old_name)
            if parsed is not None:
                current = parsed.ho_so
        parent_chain = ""
        if level is not rt.Level.MA_DINH_DANH:
            parent_chain = translations.localize_text(
                "Thuộc: " + (self._root / Path(rel).parent).as_posix())

        dlg = _RenameDialog(
            self, level, old_name, current,
            preview_fn=lambda value, _rel=rel: rt.plan_folder_rename(
                self._root, Path(_rel), value
            ),
            parent_chain=parent_chain,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.plan is None:
            return
        self._execute_plan(dlg.plan)

    def _execute_plan(self, plan: rt.RenamePlan, *, undoable: bool = True,
                      orders_before: dict[str, list[str]] | None = None,
                      undo_entry: _UndoEntry | None = None):
        # Windows chặn rename file đang mở và MuPDF không thích bị đóng lúc
        # render thread đang chạy → nhả viewer + xả hết slot treo trước khi
        # thực thi (cùng pattern với Kho lưu trữ trước khi relabel hồ sơ).
        self._pending_undo = (
            self._undo_entry_for(plan, orders_before) if undoable else None)
        orders_before = self._snapshot_orders() if orders_before is None else {
            k: list(v) for k, v in orders_before.items()
        }
        self._executing = True
        had_pdf = self._current_pdf_rel is not None
        self.pdf_viewer.clear()
        QApplication.processEvents()
        if had_pdf:
            self.pdf_viewer.release_file_handles(timeout=5.0)
            QApplication.processEvents()

        old_expanded = self._collect_expanded()
        cur = self.tree.currentItem()
        old_selected = cur.data(0, _ROLE_REL) if cur is not None else None
        old_pdf = self._current_pdf_rel

        self._set_busy(True)
        self._worker = _RenameWorker(plan)
        self._worker.progress.connect(self._on_rename_progress)
        self._worker.done.connect(
            lambda p, r: self._on_rename_done(
                p, r, old_expanded, old_selected, old_pdf,
                orders_before=orders_before, undo_entry=undo_entry,
            )
        )
        self._worker.start()

    def _on_rename_progress(self, done: int, total: int):
        self.lbl_stats.setText(translations.localize_text(
            f"Đang đổi tên… {done}/{total}"
        ))

    def _on_rename_done(self, plan: rt.RenamePlan, result: rt.ExecuteResult,
                        old_expanded: set[str], old_selected: str | None,
                        old_pdf: str | None, *,
                        orders_before: dict[str, list[str]] | None = None,
                        undo_entry: _UndoEntry | None = None):
        # KHÔNG set self._worker = None tại đây: slot này chạy ngay khi thread
        # vừa emit done xong — hủy wrapper QThread lúc C++ còn dọn dẹp sẽ
        # abort tiến trình. Giữ reference cho đến khi bị thay thế ở lần
        # đổi tên kế tiếp (is_busy đã chắn qua isRunning).

        if result.error:
            self.log_message.emit(
                translations.localize_text(
                    "Đổi tên cây thư mục: "
                    f"{translations.localize_text(result.error)}"
                ),
                "warning",
            )
            # Chỉ bật hộp thoại khi màn hình đang hiển thị: hộp thoại modal
            # trong slot này sẽ khoáy cứng mọi vòng chờ không có người bấm
            # (chạy headless / test offscreen) — khi đó lỗi vẫn hiện đủ ở log.
            if self.isVisible():
                QMessageBox.critical(self, "Thao tác thất bại", result.error)
        else:
            skipped = ""
            if plan.skipped:
                skipped = " · " + translations.localize_text(
                    f"{len(plan.skipped)} mục bỏ qua")
            if plan.kind in ("pdf_reorder", "pdf_move"):
                detail = translations.localize_text(
                    f"{plan.affected_pdfs} file PDF đổi tên")
            else:
                detail = translations.localize_text(
                    f"{plan.affected_dirs} thư mục, {plan.affected_pdfs} PDF")
            self.log_message.emit(
                translations.localize_text(
                    f"Đã hoàn tất — {translations.localize_text(plan.title)}: "
                    f"{detail}{skipped}."
                ),
                "success",
            )

        # Map trạng thái cây / viewer sang path mới CHỈ khi thao tác thành
        # công; nếu đã hoàn tác thì mọi path giữ nguyên như trước thao tác.
        success = not result.error
        if success and undo_entry is not None:
            # Consume history and restore display state only after disk undo
            # succeeds. A failed/cancelled undo stays available for retry.
            if self._undo_stack and self._undo_stack[-1] is undo_entry:
                self._undo_stack.pop()
            self._manual_order = {
                k: list(v) for k, v in undo_entry.orders.items()
            }
            self._save_settings()
        elif not success and orders_before is not None:
            self._manual_order = {k: list(v) for k, v in orders_before.items()}
            self._save_settings()
        if success and self._pending_undo is not None:
            self._push_undo(self._pending_undo)
        self._pending_undo = None
        if success:
            expanded = {rt.map_path(plan, r) for r in old_expanded}
            selected = rt.map_path(plan, old_selected) if old_selected else None
        else:
            expanded = set(old_expanded)
            selected = old_selected
        self._reload_root(expanded=expanded, selected=selected)

        if old_pdf:
            target_rel = rt.map_path(plan, old_pdf) if success else old_pdf
            if (self._root / target_rel).is_file():
                # Cập nhật state ngay; hoãn việc mở viewer sang tick kế tiếp
                # để các slot render cũ xả hết trước khi mở lại file (gắn
                # context=self để màn hình bị hủy thì timer tự hủy theo).
                self._current_pdf_rel = target_rel
                self.lbl_preview_path.setText(target_rel)
                self.preview_stack.setCurrentIndex(1)
                QTimer.singleShot(
                    30, self, lambda rel=target_rel: self._restore_pdf_preview(rel)
                )
            else:
                self._current_pdf_rel = None
                self.lbl_preview_path.setText("")
                self.preview_stack.setCurrentIndex(0)
        self._executing = False
        self._set_busy(False)

    # ------------------------------------------------------ di chuyển (DnD)

    def _on_folder_move_requested(self, rel: str, target_rel: str):
        if self.is_busy() or self._root is None:
            return
        self._handle_folder_move(rel, target_rel)

    def _handle_folder_move(self, rel: str, target_rel: str) -> None:
        """Kéo-thả phông/mục lục sang cha khác — thực thi ngay.

        TÊN của chính mục được GIỮ NGUYÊN (không tự đánh số lại); toàn bộ
        hồ sơ / PDF bên dưới được dựng lại theo mã của cha đích. Thứ tự /
        số thứ tự chỉ đổi khi người dùng chủ động đổi tên hoặc dùng menu
        chuột phải "đánh số từ ... này tự động" (xem ``rt.plan_folder_move``).
        """
        try:
            plan = rt.plan_folder_move(self._root, Path(rel), Path(target_rel))
        except ValueError as exc:
            QMessageBox.warning(self, "Di chuyển thư mục", str(exc))
            return
        self._execute_plan(plan)

    def _on_ho_so_dropped(self, rel: str, target_ml_rel: str,
                          insert_at: int = -1):
        if self.is_busy() or self._root is None:
            return
        self._handle_ho_so_drop(rel, target_ml_rel, insert_at)

    def _handle_ho_so_drop(self, rel: str, target_ml_rel: str,
                           insert_at: int = -1) -> None:
        """Thả hồ sơ vào vị trí: đặt ĐÚNG chỗ đó theo thứ tự thủ công.

        - Cùng mục lục: chỉ đổi thứ tự hiển thị (không đụng đĩa, không đổi
          tên).
        - Khác mục lục: chuyển sang (tên giữ nguyên, xem ``plan_folder_move``)
          rồi đặt vào đúng vị trí thả.
        Thả lên hàng mục lục (``insert_at`` -1) = đặt xuống cuối. Số thứ tự
        chỉ đổi khi người dùng dùng menu "đánh số từ ... này tự động".
        """
        src_ml = Path(rel).parent.as_posix()
        same_ml = src_ml == target_ml_rel
        if same_ml:
            names = self._dir_child_order(target_ml_rel)
            name = Path(rel).name
            if name in names:
                names.remove(name)
            if insert_at < 0:
                insert_at = len(names)
            names.insert(min(insert_at, len(names)), name)
            # Xếp lại thứ tự trong cùng cha là thuần hiển thị — vẫn phải
            # hoàn tác được (Ctrl+Z), nếu không Ctrl+Z sẽ lùi thao tác ĐĨA
            # trước đó thay vì thao tác người dùng vừa làm.
            snapshot = self._snapshot_orders()
            self._set_manual_order(target_ml_rel, names)
            self._repopulate_item(target_ml_rel)
            self._push_undo(_UndoEntry(
                title=(f'đặt hồ sơ "{name}" vào vị trí {insert_at + 1} '
                       f'của "{target_ml_rel}"'),
                apply=lambda s=snapshot, c=[target_ml_rel]:
                    self._apply_orders_undo(s, c),
            ))
            self.log_message.emit(
                translations.localize_text(
                    f'Đã đặt hồ sơ "{name}" vào vị trí {insert_at + 1} của '
                    f'"{target_ml_rel}"'
                ),
                "info")
            return
        orders_before = self._snapshot_orders()
        try:
            plan = rt.plan_folder_move(
                self._root, Path(rel), Path(target_ml_rel))
        except ValueError as exc:
            QMessageBox.warning(self, "Di chuyển hồ sơ", str(exc))
            return
        # Đặt hồ sơ vào đúng vị trí trong thứ tự thủ công của mục lục đích.
        names = self._dir_child_order(target_ml_rel)
        final_name = plan.new_name
        if insert_at < 0:
            insert_at = len(names)
        names.insert(min(insert_at, len(names)), final_name)
        self._set_manual_order(target_ml_rel, names)
        old_order = self._manual_order.get(src_ml)
        if old_order and Path(rel).name in old_order:
            old_order.remove(Path(rel).name)
            self._set_manual_order(src_ml, old_order)
        self._execute_plan(plan, orders_before=orders_before)

    def _on_pdf_move_requested(self, rels: list, target_rel: str,
                               insert_at: int = -1):
        if self.is_busy() or self._root is None:
            return
        self._handle_pdf_move(rels, target_rel, insert_at)

    def _handle_pdf_move(self, rels: list, target_rel: str,
                         insert_at: int = -1) -> None:
        """Thả (nhóm) PDF vào vị trí — ĐỨNG ĐÚNG chỗ đó, KHÔNG đổi tên.

        - Cùng hồ sơ: chỉ đổi thứ tự hiển thị (không đụng đĩa).
        - Khác hồ sơ: chuyển sang (giữ nguyên số trang, chỉ dựng lại mã cha
          — xem ``rt.plan_pdf_move``) rồi đặt vào đúng vị trí thả.
        Thả lên hàng hồ sơ (``-1``) = đặt xuống cuối. Tên chỉ đổi khi người
        dùng dùng menu "đánh số từ tài liệu này".
        """
        same = all(Path(r).parent.as_posix() == target_rel for r in rels)
        if same:
            names = self._child_order(target_rel, dirs=False)
            group_names = [Path(r).name for r in rels]
            for n in group_names:
                if n in names:
                    names.remove(n)
            if insert_at < 0:
                insert_at = len(names)
            for k, n in enumerate(group_names):
                names.insert(min(insert_at + k, len(names)), n)
            # Như hồ sơ cùng mục lục: thuần hiển thị nhưng phải hoàn tác được.
            snapshot = self._snapshot_orders()
            self._set_manual_order(target_rel, names)
            self._repopulate_item(target_rel)
            self._push_undo(_UndoEntry(
                title=(f"đặt {len(group_names)} tài liệu vào vị trí "
                       f'{insert_at + 1} của "{Path(target_rel).name}"'),
                apply=lambda s=snapshot, c=[target_rel]:
                    self._apply_orders_undo(s, c),
            ))
            return
        orders_before = self._snapshot_orders()
        try:
            plan = rt.plan_pdf_move(
                self._root, [Path(r) for r in rels], Path(target_rel))
        except ValueError as exc:
            QMessageBox.warning(self, "Chuyển tài liệu", str(exc))
            return
        # Cắm tên file (sau chuyển) vào đúng vị trí trong thứ tự đích.
        names = self._child_order(target_rel, dirs=False)
        for r in rels:
            old = Path(r).name
            if old in names:
                names.remove(old)
        finals = [
            Path(plan.path_map.get(Path(r).as_posix(), r)).name for r in rels
        ]
        if insert_at < 0:
            insert_at = len(names)
        for k, n in enumerate(finals):
            names.insert(min(insert_at + k, len(names)), n)
        self._set_manual_order(target_rel, names)
        # Dọn tên cũ khỏi thứ tự thủ công của hồ sơ nguồn (nếu có).
        for r in rels:
            src_hs = Path(r).parent.as_posix()
            if src_hs != target_rel and src_hs in self._manual_order:
                old_list = self._manual_order[src_hs]
                if Path(r).name in old_list:
                    old_list.remove(Path(r).name)
                    self._set_manual_order(src_hs, old_list)
        if plan.ops:
            self._execute_plan(plan, orders_before=orders_before)
        else:
            self._repopulate_item(target_rel)

    # ------------------------------------------------------------- hoàn tác

    def _undo_entry_for(self, plan: rt.RenamePlan,
                        orders_before: dict[str, list[str]] | None = None
                        ) -> _UndoEntry | None:
        """Mục hoàn tác cho plan sắp thực thi: đảo ngược toàn bộ op (xem
        ``rt.plan_invert`` — chạy ngược từ trạng thái cuối về đầu) kèm ảnh
        thứ tự thủ công TRƯỚC thao tác để khôi phục cùng lúc."""
        if not plan.ops:
            return None
        orders = self._snapshot_orders() if orders_before is None \
            else {k: list(v) for k, v in orders_before.items()}

        def build() -> rt.RenamePlan:
            return rt.plan_invert(
                plan,
                translations.localize_text(
                    f"hoàn tác {translations.localize_text(plan.title)}"
                ),
            )

        return _UndoEntry(title=plan.title, build=build, orders=orders)

    def _snapshot_orders(self) -> dict[str, list[str]]:
        """Ảnh WHOLE của thứ tự thủ công hiện tại (deep-copy từng danh sách)."""
        return {k: list(v) for k, v in self._manual_order.items()}

    def _push_undo(self, entry: _UndoEntry) -> None:
        self._undo_stack.append(entry)
        if len(self._undo_stack) > self._UNDO_LIMIT:
            self._undo_stack.pop(0)

    def _apply_orders_undo(self, orders: dict[str, list[str]],
                           containers: list[str]) -> None:
        """Hoàn tác thuần hiển thị (xếp lại thứ tự trong cùng cha): trả thứ
        tự thủ công về ảnh trước thao tác và vẽ lại các mục bị đụng đến."""
        self._manual_order = {k: list(v) for k, v in orders.items()}
        self._save_settings()
        for rel in containers:
            self._repopulate_item(rel)

    def _undo_last(self):
        """Ctrl+Z — hoàn tác thao tác đổi tên / di chuyển / sắp xếp gần nhất.

        Hoàn tác thuần hiển thị (``apply``) chạy ngay tại chỗ; hoàn tác đĩa
        chạy ngược toàn bộ op của kế hoạch gốc và chỉ khôi phục thứ tự thủ
        công / xóa lịch sử sau khi thành công.
        """
        if self.is_busy() or self._root is None or not self._undo_stack:
            return
        entry = self._undo_stack[-1]
        if entry.apply is not None:
            entry.apply()
            self._undo_stack.pop()
            self.log_message.emit(
                translations.localize_text(
                    f"Hoàn tác: {translations.localize_text(entry.title)}"
                ),
                "info",
            )
            return
        try:
            plan = entry.build()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(
                self, "Không hoàn tác được",
                translations.localize_text(
                    f"Thao tác \"{translations.localize_text(entry.title)}\" "
                    f"không hoàn tác được:\n\n{translations.localize_text(str(exc))}"
                ),
            )
            return
        if not plan.ops:
            self._undo_stack.pop()
            return  # cây đã về đúng trạng thái cũ
        self.log_message.emit(
            translations.localize_text(
                f"Hoàn tác: {translations.localize_text(entry.title)}"
            ),
            "info",
        )
        self._execute_plan(plan, undoable=False, undo_entry=entry)

    def _collect_expanded(self) -> set[str]:
        rels: set[str] = set()

        def walk(item: QTreeWidgetItem):
            for i in range(item.childCount()):
                child = item.child(i)
                if child.isExpanded() and child.data(0, _ROLE_REL):
                    rels.add(child.data(0, _ROLE_REL))
                walk(child)

        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.isExpanded() and top.data(0, _ROLE_REL):
                rels.add(top.data(0, _ROLE_REL))
            walk(top)
        return rels

    def _set_busy(self, busy: bool):
        self.btn_pick.setEnabled(not busy)
        self.btn_refresh.setEnabled(not busy)
        self.tree.setEnabled(not busy)

    # ------------------------------------------------------------- ScreenBase

    def is_busy(self) -> bool:
        # _executing che khoảng hở: thread đã return nhưng signal done còn
        # nằm trong hàng event — caller không được thao tác cây lúc đó.
        if self._executing:
            return True
        return self._worker is not None and self._worker.isRunning()

    def request_cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def update_texts(self) -> None:
        """Dịch lại nhãn cấp (cột Loại) và tooltip của cây sau khi đổi ngôn
        ngữ — item của QTreeWidget không nằm trong retranslate_widget_tree,
        nguồn tiếng Việt gốc được giữ trong _ROLE_BADGE / _ROLE_TIP."""
        it = QTreeWidgetItemIterator(self.tree)
        while it.value() is not None:
            item = it.value()
            badge = item.data(0, _ROLE_BADGE)
            if badge:
                item.setText(1, translations.localize_text(str(badge)))
            tip = item.data(0, _ROLE_TIP)
            if tip:
                item.setToolTip(0, translations.localize_text(str(tip)))
            it += 1

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
                order = data.get("order")
                if isinstance(order, dict):
                    # Chỉ nhận thứ tự thủ công của cây vừa mở lại.
                    self._manual_order = {
                        str(k): [str(n) for n in v]
                        for k, v in order.items() if isinstance(v, list)
                    }
                    # _set_root đã xả file settings với thứ tự rỗng (nó phải
                    # clear trước khi đọc được root) — ghi lại ngay, kẻo app
                    # đóng trước một lần _save_settings kế tiếp thì mất.
                    self._save_settings()
        except Exception:
            pass

    def _save_settings(self):
        try:
            os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {"root": str(self._root or ""),
                     "order": self._manual_order},
                    f, ensure_ascii=False, indent=2)
        except Exception:
            pass
