from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from scanindex.core.repository.search_engine import SearchEngine, _exact_frequency
from scanindex.core.repository.store import ArchiveStore
from scanindex.core.repository.tokenizer import to_no_diacritic
from scanindex.infra import translations
from scanindex.ui.repository.screen import (
    DossierRow,
    FileHit,
    FileRow,
    _RightPanel,
    _SearchHitCard,
    _dossier_stats_text,
    _file_card_title,
    _file_summary_text,
    _format_issue_date,
)


def _file(**overrides) -> FileRow:
    values = {
        "doc_id": "doc-1",
        "dossier_id": 1,
        "file_name": "sample.pdf",
        "file_path": "pdf/sample.pdf",
        "subject": "Về việc số hóa tài liệu",
        "doc_number": "Số: 12/QĐ-UBND",
        "issue_org": "Ủy ban nhân dân",
        "issue_org_superior": "",
        "signer_name": "Nguyễn Văn A",
        "issue_date": "Hà Nội, ngày 03 tháng 02 năm 2026",
        "doc_type": "Quyết định",
        "secrecy_mark": "",
        "page_count": 4,
    }
    values.update(overrides)
    return FileRow(**values)


def test_keyword_exact_match_is_diacritic_insensitive_but_not_fuzzy() -> None:
    assert _exact_frequency("Kế hoạch số hóa hồ sơ", "so hoa") == 1
    assert _exact_frequency("Kế hoạch số hóa hồ sơ", "so hpa") == 0


def test_issue_date_summary_shows_only_the_canonical_date_value() -> None:
    assert _format_issue_date("Hà Nội, ngày 03 tháng 02 năm 2026") == "03/02/2026"
    assert _format_issue_date("Hà Nội") == ""

    summary = _file_summary_text(_file())
    assert "03/02/2026" in summary
    assert "Hà Nội" not in summary
    assert "ngày" not in summary.casefold()
    assert " · " in summary


def test_file_ordinal_is_prefixed_to_the_dossier_file_title() -> None:
    assert _file_card_title(_file(), 3) == "03. Về việc số hóa tài liệu"


def test_dossier_compact_stats_have_only_counts_and_stored_time() -> None:
    dossier = DossierRow(
        dossier_id=1,
        title="Hồ sơ mẫu",
        fonds="01",
        catalog="02",
        dossier_code="03",
        doc_count=9,
        page_count=30,
        start_date="01/01/2020",
        end_date="31/12/2025",
        stored_at=None,
    )
    assert _dossier_stats_text(dossier) == "9 tài liệu · 30 trang"
    assert "Lưu kho" not in _dossier_stats_text(dossier)
    assert "2020" not in _dossier_stats_text(dossier)
    assert "2025" not in _dossier_stats_text(dossier)


def test_all_scope_recovers_no_diacritic_exact_metadata_matches(tmp_path) -> None:
    class EmptyIndex:
        @staticmethod
        def search_lexical(*_args, **_kwargs):
            return []

        @staticmethod
        def search_fuzzy(*_args, **_kwargs):
            return []

        @staticmethod
        def search_substring(*_args, **_kwargs):
            return []

    store = ArchiveStore(tmp_path)
    store.connect()
    store.ensure_schema()
    now = int(time.time())
    conn = store.connect()
    cur = conn.execute(
        "INSERT INTO dossiers ("
        "ma_dinh_danh, fonds, catalog, dossier_code, title, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        ("DV", "P01", "M01", "H01", "Hồ sơ số hóa", now),
    )
    dossier_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO documents ("
        "doc_id, dossier_id, file_name, file_path, kie_doc_subject,"
        "kie_issue_org_name, kie_doc_type, sha256, indexed_status, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'indexed', ?)",
        (
            "doc-1", dossier_id, "sample.pdf", "pdf/sample.pdf",
            "Kế hoạch số hóa", "Ủy ban nhân dân", "Kế hoạch", "sha-1", now,
        ),
    )
    metadata = "Cơ quan ban hành: Ủy ban nhân dân. Trích yếu: Kế hoạch số hóa."
    conn.execute(
        "INSERT INTO chunks ("
        "doc_id, doc_version, chunk_type, page, block_idx, text_original,"
        "text_no_diacritic, bbox, word_count, indexed_status, created_at"
        ") VALUES (?, 1, 'metadata', 1, 0, ?, ?, '[]', 12, 'indexed', ?)",
        ("doc-1", metadata, to_no_diacritic(metadata), now),
    )

    try:
        results = SearchEngine(store, EmptyIndex()).search("uy ban", mode="all")
    finally:
        store.close()

    assert results
    assert all(result.match_kind == "exact" for result in results)
    assert results[0].fonds == "P01"
    assert results[0].catalog == "M01"
    assert results[0].dossier_code == "H01"


def test_content_query_and_document_number_filter_are_combined_with_and(
    tmp_path,
) -> None:
    class EmptyIndex:
        @staticmethod
        def search_lexical(*_args, **_kwargs):
            return []

        @staticmethod
        def search_fuzzy(*_args, **_kwargs):
            return []

        @staticmethod
        def search_substring(*_args, **_kwargs):
            return []

    store = ArchiveStore(tmp_path)
    store.connect()
    store.ensure_schema()
    now = int(time.time())
    conn = store.connect()
    dossier_id = int(conn.execute(
        "INSERT INTO dossiers ("
        "ma_dinh_danh, fonds, catalog, dossier_code, title, created_at"
        ") VALUES ('DV', 'P01', 'M01', 'H01', 'Hồ sơ dự toán', ?)",
        (now,),
    ).lastrowid)

    for number, doc_id in (("240/QĐ-UBND", "doc-240"), ("241/QĐ-UBND", "doc-241")):
        conn.execute(
            "INSERT INTO documents ("
            "doc_id, dossier_id, file_name, file_path, kie_doc_number_symbol,"
            "sha256, indexed_status, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, 'indexed', ?)",
            (
                doc_id, dossier_id, f"{doc_id}.pdf", f"pdf/{doc_id}.pdf",
                number, f"sha-{doc_id}", now,
            ),
        )
        body = "Nội dung về việc phân bổ dự toán ngân sách năm 2026."
        conn.execute(
            "INSERT INTO chunks ("
            "doc_id, doc_version, chunk_type, page, block_idx, text_original,"
            "text_no_diacritic, bbox, word_count, indexed_status, created_at"
            ") VALUES (?, 1, 'body', 1, 0, ?, ?, '[]', 12, 'indexed', ?)",
            (doc_id, body, to_no_diacritic(body), now),
        )

    try:
        results = SearchEngine(store, EmptyIndex()).search(
            "du toan", filters={"doc_number": "240"}, mode="content"
        )
    finally:
        store.close()

    assert {result.doc_id for result in results} == {"doc-240"}


def test_search_card_prefixes_the_visible_result_rank() -> None:
    app = QApplication.instance() or QApplication([])
    hit = FileHit(file_row=_file(), chunks=[], match_kind="exact")
    card = _SearchHitCard(hit, rank=7, rank_width=2)
    assert card._title_label.text().startswith("07. ")
    card.deleteLater()
    app.processEvents()


def test_dossier_panel_keeps_codes_and_names_on_separate_rows() -> None:
    app = QApplication.instance() or QApplication([])
    translations.set_lang("vi")
    panel = _RightPanel()
    panel.show_dossier(DossierRow(
        dossier_id=1,
        title="Hồ sơ dự toán",
        fonds="P01",
        fonds_name="Phông Văn phòng",
        catalog="02",
        catalog_name="Mục lục Tổng hợp",
        dossier_code="0034",
        doc_count=7,
        page_count=19,
        start_date="",
        end_date="",
    ))
    info = panel._info_box.text()
    for label in (
        "Mã phông", "Tên phông", "Số mục lục",
        "Tên mục lục", "Số hồ sơ", "Tên hồ sơ",
    ):
        assert label in info
    panel.deleteLater()
    app.processEvents()


def test_dossier_panel_hides_empty_rows_and_renames_stored_at() -> None:
    app = QApplication.instance() or QApplication([])
    translations.set_lang("vi")
    panel = _RightPanel()
    panel.show_dossier(DossierRow(
        dossier_id=1,
        title="Hồ sơ chỉ có tên",
        fonds="",
        fonds_name="",
        catalog="",
        catalog_name="",
        dossier_code="0001",
        doc_count=3,
        page_count=9,
        start_date="",
        end_date="",
        stored_at=1700000000,
    ))
    info = panel._info_box.text()
    assert "Tên hồ sơ" in info and "Số hồ sơ" in info
    # Fields without a value are hidden entirely (no "—" filler rows).
    for label in ("Mã phông", "Tên phông", "Số mục lục", "Tên mục lục"):
        assert label not in info
    assert "—" not in info
    assert "Thời điểm lưu" in info
    assert "Thời điểm:" not in info
    # Completely empty dossier → single placeholder line.
    panel.show_dossier(DossierRow(
        dossier_id=2,
        title="",
        fonds="",
        catalog="",
        dossier_code="",
        doc_count=0,
        page_count=0,
        start_date="",
        end_date="",
    ))
    assert "—" not in panel._info_box.text()
    panel.deleteLater()
    app.processEvents()
