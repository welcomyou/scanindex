from __future__ import annotations

import ast
import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QMessageBox,
)

from scanindex.infra import translations
from scanindex.infra.ui_log_text_catalog import UI_LOG_TEXT_PAIRS
from scanindex.infra.ui_text_catalog import UI_TEXT_PAIRS


ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")


def _normalise(value: str) -> str:
    return " ".join(PLACEHOLDER_RE.sub("{}", value).split())


def _covered_texts() -> set[str]:
    covered: set[str] = set()
    for en, vi in (*UI_TEXT_PAIRS, *UI_LOG_TEXT_PAIRS):
        covered.update((_normalise(str(en)), _normalise(str(vi))))
    for values in translations.TRANSLATIONS.values():
        if not isinstance(values, dict):
            continue
        for lang in ("en", "vi"):
            value = values.get(lang)
            if value:
                covered.add(_normalise(str(value)))
    return covered


def _literal_strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            "".join(
                part.value
                if isinstance(part, ast.Constant)
                and isinstance(part.value, str)
                else "{}"
                for part in node.values
            )
        ]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [value for item in node.elts for value in _literal_strings(item)]
    return []


def _runtime_literal(node: ast.AST) -> str | None:
    values = _literal_strings(node)
    if values:
        return values[0]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _runtime_literal(node.left)
        right = _runtime_literal(node.right)
        return None if left is None or right is None else left + right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return _runtime_literal(node.func.value)
    return None


def test_every_translation_has_both_languages_and_matching_placeholders():
    pairs = [*UI_TEXT_PAIRS, *UI_LOG_TEXT_PAIRS]
    pairs.extend(
        (values.get("en", ""), values.get("vi", ""))
        for values in translations.TRANSLATIONS.values()
        if isinstance(values, dict)
    )
    assert pairs
    for en, vi in pairs:
        assert str(en).strip()
        assert str(vi).strip()
        assert len(PLACEHOLDER_RE.findall(str(en))) == len(
            PLACEHOLDER_RE.findall(str(vi))
        )


def test_dynamic_translation_keeps_runtime_values_and_line_breaks():
    assert translations.localize_text(
        "2 hồ sơ · 3 tài liệu · 4 trang · 5 đoạn", "en"
    ) == "2 dossiers · 3 documents · 4 pages · 5 passages"
    assert translations.localize_text(
        "OCR Success: sample.pdf (1.5s)", "vi"
    ) == "OCR thành công: sample.pdf (1.5 giây)"
    source = "Could not write file:\nC:\\work\\first.pdf\nsecond.pdf"
    result = translations.localize_text(source, "vi")
    assert result == "Không thể ghi file:\nC:\\work\\first.pdf\nsecond.pdf"
    translations.set_lang("en")
    assert translations.format_import_summary(
        dossiers=2,
        imported=7,
        duplicates=3,
        failed=1,
        duplicate_skipped=1,
        duplicate_kept=2,
    ) == (
        "Dossiers: 2\nDocuments imported: 7\nDuplicates: 3 "
        "(skipped 1, kept 2)\nErrors: 1\n\nYou can search immediately."
    )


def test_widget_retranslation_does_not_mutate_business_or_document_data():
    app = QApplication.instance() or QApplication([])
    root = QWidget()
    layout = QVBoxLayout(root)
    label = QLabel("Chọn chức năng để bắt đầu")
    button = QPushButton("Xóa")
    layout.addWidget(label)
    layout.addWidget(button)

    business_combo = QComboBox()
    translations.set_lang("en")
    translations.add_localized_combo_items(
        business_combo, ["Thường", "Mật", "Tối mật", "Tuyệt mật"]
    )
    business_combo.setCurrentIndex(3)
    layout.addWidget(business_combo)

    document_type_combo = QComboBox()
    translations.add_localized_combo_items(
        document_type_combo,
        ["Thông báo", "Quyết định"],
        context="document_type",
    )
    document_type_combo.setCurrentIndex(0)
    layout.addWidget(document_type_combo)

    data_combo = QComboBox()
    data_combo.addItem("Normal")
    layout.addWidget(data_combo)

    table = QTableWidget(1, 1)
    table.setHorizontalHeaderItem(0, QTableWidgetItem("Trạng thái"))
    table.setItem(0, 0, QTableWidgetItem("Thông báo"))
    table.insertRow(1)
    status_item = QTableWidgetItem()
    translations.set_translatable_item_text(status_item, "Chờ ký")
    table.setItem(1, 0, status_item)
    layout.addWidget(table)

    file_list = QListWidget()
    file_list.addItem(QListWidgetItem("Thông báo"))
    layout.addWidget(file_list)

    tree = QTreeWidget()
    tree.setHeaderLabels(["Nhãn / Văn bản"])
    QTreeWidgetItem(tree, ["Thông báo"])
    layout.addWidget(tree)

    translations.retranslate_widget_tree(root)
    assert label.text() == "Choose a function to begin"
    assert button.text() == "Delete"
    assert business_combo.currentText() == "Top secret"
    assert translations.combo_value(business_combo) == "Tuyệt mật"
    assert document_type_combo.currentText() == "Notice"
    assert translations.combo_value(document_type_combo) == "Thông báo"
    assert data_combo.currentText() == "Normal"
    assert table.horizontalHeaderItem(0).text() == "Status"
    assert table.item(0, 0).text() == "Thông báo"
    assert table.item(1, 0).text() == "Waiting to sign"
    assert file_list.item(0).text() == "Thông báo"
    assert tree.topLevelItem(0).text(0) == "Thông báo"

    translations.set_lang("vi")
    translations.retranslate_widget_tree(root)
    assert label.text() == "Chọn chức năng để bắt đầu"
    assert button.text() == "Xóa"
    assert business_combo.currentText() == "Tuyệt mật"
    assert translations.combo_value(business_combo) == "Tuyệt mật"
    assert document_type_combo.currentText() == "Thông báo"
    assert translations.combo_value(document_type_combo) == "Thông báo"
    root.deleteLater()
    app.processEvents()


def test_dossier_dialog_localizes_defaults_but_returns_canonical_values():
    from scanindex.ui.dialogs.archive_session_dialog import DossierInfoDialog

    app = QApplication.instance() or QApplication([])
    translations.set_lang("en")
    dialog = DossierInfoDialog(actual_page_count=3)
    translations.retranslate_widget_tree(dialog)
    assert dialog.windowTitle() == "Dossier information"
    assert dialog._cb_retention.currentText() == "Permanent"
    assert translations.combo_value(dialog._cb_retention) == "Vĩnh viễn"
    assert dialog._cb_physical.currentText() == "Normal"
    assert translations.combo_value(dialog._cb_physical) == "Bình thường"

    translations.set_lang("vi")
    translations.retranslate_widget_tree(dialog)
    assert dialog._cb_retention.currentText() == "Vĩnh viễn"
    assert translations.combo_value(dialog._cb_retention) == "Vĩnh viễn"
    dialog.deleteLater()
    app.processEvents()


def test_existing_activity_log_entries_retranslate():
    from scanindex.ui.widgets.log_panel import LogPanel

    app = QApplication.instance() or QApplication([])
    translations.set_lang("vi")
    panel = LogPanel()
    panel.append_log("OCR Success: sample.pdf (1.5s)")
    panel._flush_pending_logs()
    assert "OCR thành công: sample.pdf (1.5 giây)" in panel.text_edit.toPlainText()

    translations.set_lang("en")
    panel.update_texts()
    assert "OCR Success: sample.pdf (1.5s)" in panel.text_edit.toPlainText()
    panel.deleteLater()
    app.processEvents()


def test_standard_dialog_buttons_and_secondary_text_retranslate():
    app = QApplication.instance() or QApplication([])
    translations.set_lang("vi")

    message = QMessageBox()
    message.setStandardButtons(
        QMessageBox.StandardButton.Save
        | QMessageBox.StandardButton.Discard
        | QMessageBox.StandardButton.Cancel
    )
    message.setInformativeText("The current task will be asked to cancel.")
    translations.retranslate_widget_tree(message)
    assert message.button(QMessageBox.StandardButton.Save).text() == "Lưu"
    assert message.button(QMessageBox.StandardButton.Discard).text() == "Không lưu"
    assert message.button(QMessageBox.StandardButton.Cancel).text() == "Hủy"
    assert message.informativeText() == "Tác vụ hiện tại sẽ được yêu cầu hủy."

    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok
        | QDialogButtonBox.StandardButton.Cancel
    )
    translations.retranslate_widget_tree(buttons)
    assert buttons.button(QDialogButtonBox.StandardButton.Ok).text() == "Đồng ý"
    assert buttons.button(QDialogButtonBox.StandardButton.Cancel).text() == "Hủy"

    message.deleteLater()
    buttons.deleteLater()
    app.processEvents()


def test_accuracy_report_has_complete_english_and_vietnamese_versions():
    from scanindex.core.ocr.accuracy_metrics import (
        ComparisonResult,
        SideMetrics,
        format_report,
    )

    side = SideMetrics(0.98, 0.97, 0.02, 0.03, 100, 20)
    result = ComparisonResult(side, side, 100, 20, "tie", 0.0)

    translations.set_lang("en")
    english = format_report(result)
    assert "OCR ACCURACY COMPARISON RESULTS" in english
    assert "Ground truth: 100 characters / 20 words" in english
    assert "This software" in english

    translations.set_lang("vi")
    vietnamese = format_report(result)
    assert "KẾT QUẢ SO SÁNH ĐỘ CHÍNH XÁC OCR" in vietnamese
    assert "Văn bản gốc: 100 ký tự / 20 từ" in vietnamese
    assert "Phần mềm này" in vietnamese


def test_backend_activity_log_literals_have_a_bilingual_catalog_entry():
    methods = {
        "showMessage",
        "set_status",
        "setStatus",
        "_set_status",
        "set_detail",
        "_set_detail",
        "log",
        "_log",
        "add_log",
        "set_error",
        "_show_error",
        "set_progress_text",
        "log_cb",
        "_log_optional",
    }
    covered = _covered_texts()
    missing: list[str] = []
    for path in (ROOT / "scanindex" / "core").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if isinstance(call.func, ast.Name):
                name = call.func.id
            elif isinstance(call.func, ast.Attribute):
                name = call.func.attr
            else:
                continue
            if name not in methods or not call.args:
                continue
            value = _runtime_literal(call.args[0])
            if not value or not any(char.isalpha() for char in value):
                continue
            if _normalise(value) not in covered:
                missing.append(
                    f"{path.relative_to(ROOT)}:{call.lineno}: {value}"
                )
    assert not missing, "Backend UI logs missing EN/VI pairs:\n" + "\n".join(missing)


def test_direct_ui_literals_have_a_bilingual_catalog_entry():
    constructors = {
        "QLabel": (0,),
        "QPushButton": (0,),
        "QCheckBox": (0,),
        "QRadioButton": (0,),
        "QGroupBox": (0,),
        "QAction": (0,),
        "QListWidgetItem": (0,),
        "QTableWidgetItem": (0,),
    }
    setters = {
        "setText": (0,),
        "setWindowTitle": (0,),
        "setTitle": (0,),
        "setToolTip": (0,),
        "setStatusTip": (0,),
        "setWhatsThis": (0,),
        "setPlaceholderText": (0,),
        "setLabelText": (0,),
        "setInformativeText": (0,),
        "setDetailedText": (0,),
        "setIconText": (0,),
        "setSuffix": (0,),
        "setPrefix": (0,),
        "setTabText": (1,),
        "addTab": (1,),
        "insertTab": (2,),
        "setHorizontalHeaderLabels": (0,),
        "setVerticalHeaderLabels": (0,),
        "addItems": (0,),
        "addItem": (0,),
        "addButton": (0,),
        "addAction": (0,),
        "addRow": (0,),
    }
    dialogs = {
        "information": (1, 2),
        "warning": (1, 2),
        "critical": (1, 2),
        "question": (1, 2),
        "getText": (1, 2),
        "getItem": (1, 2),
        "getExistingDirectory": (1,),
        "getOpenFileName": (1, 3),
        "getOpenFileNames": (1, 3),
        "getSaveFileName": (1, 3),
    }
    allowed_product_or_markup = {
        "English",
        "LayoutLMv3",
        "<b style='color:{}'>{}</b>{}",
        "<span style='color:{}'>{}</span>{}",
    }
    covered = _covered_texts()
    missing: list[str] = []
    paths = [ROOT / "ocr_app.py", *(ROOT / "scanindex" / "ui").rglob("*.py")]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if isinstance(call.func, ast.Name):
                name = call.func.id
            elif isinstance(call.func, ast.Attribute):
                name = call.func.attr
            else:
                continue
            indices = constructors.get(name) or setters.get(name) or dialogs.get(name)
            if not indices:
                continue
            for index in indices:
                if index >= len(call.args):
                    continue
                for value in _literal_strings(call.args[index]):
                    if (
                        not value.strip()
                        or not any(char.isalpha() for char in value)
                        or value in allowed_product_or_markup
                    ):
                        continue
                    if _normalise(value) not in covered:
                        missing.append(f"{path.relative_to(ROOT)}:{call.lineno}: {value}")
    assert not missing, "UI strings missing EN/VI pairs:\n" + "\n".join(missing)


def test_native_file_dialog_captions_and_filters_are_localized():
    dialog_args = {
        "getExistingDirectory": (1,),
        "getOpenFileName": (1, 3),
        "getOpenFileNames": (1, 3),
        "getSaveFileName": (1, 3),
    }
    missing: list[str] = []
    for path in (ROOT / "scanindex" / "ui").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if not isinstance(call.func, ast.Attribute):
                continue
            indices = dialog_args.get(call.func.attr)
            if not indices:
                continue
            for index in indices:
                if index >= len(call.args):
                    continue
                arg = call.args[index]
                if isinstance(arg, ast.Constant) and arg.value == "":
                    continue
                localized = (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr in {"get_text", "localize_text"}
                )
                if not localized:
                    missing.append(
                        f"{path.relative_to(ROOT)}:{call.lineno}: arg {index}"
                    )
    assert not missing, "Native dialog text is not localized:\n" + "\n".join(missing)
