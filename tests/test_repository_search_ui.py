import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from scanindex.core.repository.importer import _apply_step2_metadata_overrides
from scanindex.ui.repository.screen import (
    DossierRow,
    FileRow,
    _AddFileMetadataDialog,
    _DateFilterInput,
    _RightPanel,
    _dossier_code_line,
    _dossier_status_html,
    _dossier_stats_text,
    _file_summary_text,
    _format_repo_stats,
    _format_issue_date,
    _highlight_query_html,
    _is_unstructured_dossier,
    _secrecy_mark_color,
    _snippet_context_text,
)
from scanindex.core.repository.search_engine import (
    SearchResult,
    _advanced_text_match,
    _date_key,
    _fuzzy_frequency,
)
from scanindex.ui.widgets.kie_archive_viewer import _field_menu_text
from scanindex.ui.widgets.pdf_viewer_widget import PdfViewerWidget


def _qapp():
    return QApplication.instance() or QApplication([])


def test_kie_create_menu_uses_vietnamese_label_with_raw_code():
    assert _field_menu_text("SIGNER_NAME") == "Người ký (SIGNER_NAME)"
    assert _field_menu_text("ISSUE_ORG_SUPERIOR") == "Cơ quan cấp trên (ISSUE_ORG_SUPERIOR)"


def test_snippet_highlight_matches_without_diacritics():
    html = _highlight_query_html("Văn bản về Chợ Lớn hôm nay", "cho lon")

    assert "background-color:#facc15" in html
    assert "Chợ Lớn" in html


def test_multi_word_snippet_highlight_does_not_mark_single_token_noise():
    html = _highlight_query_html(
        "Văn phòng Thành ủy mời đồng chí Phạm Văn Hiền dự họp",
        "PHẠM VĂN HIỀN",
    )

    assert html.count("background-color:#facc15") == 1
    assert "Văn phòng" in html
    assert "Phạm Văn Hiền" in html


def test_snippet_context_centers_query_inside_long_chunk():
    text = ("dau doan " * 60) + "Nguoi ky Pham Van Hien tai cuoi trang" + (" cuoi doan" * 60)

    snippet = _snippet_context_text(text, "pham van hien", max_chars=100)

    assert "Pham Van Hien" in snippet
    assert snippet.startswith("...")
    assert snippet.endswith("...")
    pos = snippet.index("Pham Van Hien")
    assert 20 <= pos <= 70


def test_fuzzy_snippet_context_centers_and_highlights_near_numeric_match():
    text = ("dau doan " * 60) + "Kinh phi Quy tien thuong theo ND73 1.609" + (" cuoi doan" * 60)

    snippet = _snippet_context_text(text, "600", max_chars=100, fuzzy=True)
    html = _highlight_query_html(snippet, "600", fuzzy=True)

    assert "609" in snippet
    assert snippet.startswith("...")
    assert snippet.endswith("...")
    assert "background-color:#facc15" in html
    assert "609</span>" in html


def test_fuzzy_metadata_requires_every_query_token():
    text = "Ve de an sap xep, Ho Chi Minh Thanh pho va cac don vi"

    snippet = _snippet_context_text(text, "vo minh thanh", max_chars=120, fuzzy=True)
    html = _highlight_query_html(snippet, "vo minh thanh", fuzzy=True)

    assert "background-color:#facc15" not in html


def test_fuzzy_metadata_highlights_full_short_name_match():
    text = "Nguoi ky: Vo Minh Thanh"

    snippet = _snippet_context_text(text, "vo minh thanh", max_chars=120, fuzzy=True)
    html = _highlight_query_html(snippet, "vo minh thanh", fuzzy=True)

    assert "Vo Minh Thanh" in snippet
    assert "background-color:#facc15" in html
    assert "Vo Minh Thanh</span>" in html


def test_fuzzy_metadata_keeps_two_letter_tokens_exact():
    text = "Nguoi ky: Vu Minh Thanh"

    snippet = _snippet_context_text(text, "vo minh thanh", max_chars=120, fuzzy=True)
    html = _highlight_query_html(snippet, "vo minh thanh", fuzzy=True)

    assert "Vu Minh Thanh" in snippet
    assert "background-color:#facc15" not in html


def test_fuzzy_short_phrase_highlight_does_not_mark_scattered_tokens():
    text = "Cac co quan, khao sat vuong mac ve ket luan"

    html = _highlight_query_html(text, "vuong xuan", fuzzy=True)

    assert "background-color:#facc15" not in html
    assert "quan</span>" not in html
    assert "luan</span>" not in html


def test_fuzzy_short_phrase_highlight_rejects_excessive_total_distance():
    html = _highlight_query_html("chi thuong xuyen de bo sung nguon", "vuong xuan", fuzzy=True)

    assert "background-color:#facc15" not in html
    assert "xuyen de bo sung</span>" not in html


def test_fuzzy_phrase_requires_one_exact_lexical_anchor():
    assert _fuzzy_frequency("quan so luong", "vuong xuan") == 0
    assert _fuzzy_frequency("va bien phap", "pham van hien") == 0


def test_fuzzy_short_phrase_highlight_marks_compact_phrase():
    html = _highlight_query_html("Nguoi du hop: Vuong Luan", "vuong xuan", fuzzy=True)

    assert html.count("background-color:#facc15") == 1
    assert "Vuong Luan</span>" in html


def test_fuzzy_two_token_phrase_allows_only_one_inserted_word():
    accepted = _highlight_query_html("Nguoi du hop: Vuong mac Luan", "vuong xuan", fuzzy=True)
    rejected = _highlight_query_html("Nguoi du hop: Vuong mac ket Luan", "vuong xuan", fuzzy=True)

    assert "Vuong mac Luan</span>" in accepted
    assert "background-color:#facc15" not in rejected


def test_fuzzy_short_phrase_highlight_allows_inserted_words_inside_span():
    html = _highlight_query_html(
        "Nguoi du hop: Phan Dung Van Hien",
        "pham van hien",
        fuzzy=True,
    )

    assert html.count("background-color:#facc15") == 1
    assert "Phan Dung Van Hien</span>" in html


def test_fuzzy_short_phrase_highlight_allows_token_reordering():
    html = _highlight_query_html("Nguoi du hop: Vuong Xuan Viet", "vuong viet xuan", fuzzy=True)

    assert html.count("background-color:#facc15") == 1
    assert "Vuong Xuan Viet</span>" in html

    reversed_html = _highlight_query_html(
        "Nguoi du hop: Viet Xuan Vuong",
        "vuong xuan viet",
        fuzzy=True,
    )
    assert reversed_html.count("background-color:#facc15") == 1
    assert "Viet Xuan Vuong</span>" in reversed_html


def test_numeric_fuzzy_frequency_requires_tight_same_length_match():
    assert _fuzzy_frequency("1.609", "600") == 1
    assert _fuzzy_frequency("1.600", "600") == 1
    assert _fuzzy_frequency("1.500", "600") == 0
    assert _fuzzy_frequency("1600", "600") == 0


def test_repository_pdf_highlights_all_match_boxes_for_file():
    from scanindex.ui.repository.screen import RepositoryScreen

    chunks = [
        SearchResult(
            chunk_id=1,
            doc_id="d1",
            score=1,
            page=1,
            text="",
            bbox=[],
            match_bboxes=[[10, 10, 20, 20]],
            match_kind="exact",
            query="abc",
        ),
        SearchResult(
            chunk_id=2,
            doc_id="d1",
            score=1,
            page=2,
            text="",
            bbox=[],
            match_bboxes=[[30, 30, 40, 40]],
            match_kind="exact",
            query="abc",
        ),
    ]

    assert RepositoryScreen._match_page_boxes(chunks) == [
        (0, [10.0, 10.0, 20.0, 20.0]),
        (1, [30.0, 30.0, 40.0, 40.0]),
    ]


def test_text_fuzzy_frequency_does_not_shadow_levenshtein_import():
    assert _fuzzy_frequency("Phạm Văn Hiền", "pham van hien") == 3


def test_text_fuzzy_frequency_requires_every_query_token():
    assert _fuzzy_frequency("Ho Chi Minh Thanh pho", "vo minh thanh") == 0
    assert _fuzzy_frequency("Vo Minh Thanh", "vo minh thanh") == 3
    assert _fuzzy_frequency("Vu Minh Thanh", "vo minh thanh") == 0
    assert _fuzzy_frequency("Vu Minh Thanh", "vo") == 0


def test_text_fuzzy_frequency_uses_compact_span_for_short_names():
    assert _fuzzy_frequency("Tham Van Hien", "pham van hien") == 3
    assert _fuzzy_frequency("Phan Van Hien", "pham van hien") == 3
    assert _fuzzy_frequency("Phu Van Hau", "pham van hien") == 0
    assert _fuzzy_frequency("Ham Van Tuyen", "pham van hien") == 0
    assert _fuzzy_frequency("Pham Van Vu Hien", "pham van hien") == 3
    assert _fuzzy_frequency("Phan Dung Van Hien", "pham van hien") == 3
    assert _fuzzy_frequency("Vuong Xuan Viet", "vuong viet xuan") == 3
    assert _fuzzy_frequency("Viet Xuan Vuong", "vuong xuan viet") == 3
    assert _fuzzy_frequency("tham muu Ban To chuc thuc hien kien nghi", "pham van hien") == 0
    assert _fuzzy_frequency("Pham Van Hieu", "pham van hien") == 3


def test_text_fuzzy_frequency_requires_compact_ordered_short_phrase():
    assert _fuzzy_frequency("Cac co quan khao sat vuong mac ve ket luan", "vuong xuan") == 0
    assert _fuzzy_frequency("chi thuong xuyen de bo sung nguon", "vuong xuan") == 0
    assert _fuzzy_frequency("Vuong mac ket Luan", "vuong xuan") == 0
    assert _fuzzy_frequency("Vuong mac Luan", "vuong xuan") == 2
    assert _fuzzy_frequency("Vuong Luan", "vuong xuan") == 2


def test_text_fuzzy_frequency_allows_standard_one_edit_typo():
    assert _fuzzy_frequency("Vuong Xuan Viet", "vuong xuan biet") == 3
    assert _fuzzy_frequency("Viet", "biet") == 1


def test_regular_secrecy_mark_stays_neutral_but_classified_is_red():
    assert _secrecy_mark_color("Thường") != "#dc2626"
    assert _secrecy_mark_color("Mật") == "#dc2626"


def test_repository_file_summary_matches_step2_final_metadata_contract():
    row = FileRow(
        doc_id="d1",
        dossier_id=1,
        file_name="A.pdf",
        file_path="pdf/A.pdf",
        subject="Quyết định về giao dự toán chi ngân sách năm 2026",
        doc_number="Số: 119/QĐ/VPTU",
        issue_org="VĂN PHÒNG THÀNH ỦY",
        issue_org_superior="TP HỒ CHÍ MINH",
        signer_name="Lê Ngọc Khánh",
        issue_date="2026-02-05",
        doc_type="Quyết định",
        secrecy_mark="",
        page_count=1,
        dossier_title="",
    )

    assert _format_issue_date(row.issue_date) == "05/02/2026"
    assert (
        _file_summary_text(row)
        == "119-QĐ/VPTU · 05/02/2026 · VĂN PHÒNG THÀNH ỦY TP HỒ CHÍ MINH · Lê Ngọc Khánh"
    )


def test_step2_metadata_overrides_raw_kie_fields_for_repository_import():
    raw = {
        "kie_doc_subject": "raw subject",
        "kie_doc_number_symbol": "Số: 1/RAW",
        "kie_issue_org_name": "RAW ORG",
        "kie_issue_org_superior": "RAW SUPERIOR",
        "kie_place_date": "TP.HCM, ngày 1 tháng 1 năm 2026",
        "kie_signer_name": "Raw Signer",
    }
    metadata = {
        "trich_yeu": "Quyết định về giao dự toán chi ngân sách năm 2026",
        "so_van_ban": "119",
        "ky_hieu": "QĐ/VPTU",
        "ngay_ban_hanh": "05/02/2026",
        "co_quan_ban_hanh": "VĂN PHÒNG THÀNH ỦY TP HỒ CHÍ MINH",
        "nguoi_ky": "Lê Ngọc Khánh",
    }

    out = _apply_step2_metadata_overrides(raw, metadata)

    assert out["kie_doc_subject"] == metadata["trich_yeu"]
    assert out["kie_doc_number_symbol"] == "119-QĐ/VPTU"
    assert out["kie_place_date"] == "05/02/2026"
    assert out["kie_issue_org_name"] == "VĂN PHÒNG THÀNH ỦY TP HỒ CHÍ MINH"
    assert out["kie_issue_org_superior"] == ""
    assert out["kie_signer_name"] == "Lê Ngọc Khánh"


def test_unstructured_dossier_hides_synthetic_code_line():
    dossier = DossierRow(
        dossier_id=1,
        title="dfg dfg dfg dgf",
        ma_dinh_danh="UNSTRUCT",
        fonds="1A69EC4A",
        catalog="00",
        dossier_code="A892B",
        doc_count=9,
        page_count=30,
        start_date="",
        end_date="",
        is_unstructured=True,
    )

    assert _is_unstructured_dossier(dossier)
    assert _dossier_code_line(dossier) == ""
    assert _dossier_stats_text(dossier) == "9 tài liệu · 30 trang"


def test_structured_dossier_keeps_code_line():
    dossier = DossierRow(
        dossier_id=1,
        title="",
        ma_dinh_danh="A99",
        fonds="005",
        catalog="01",
        dossier_code="7777",
        doc_count=1,
        page_count=2,
        start_date="",
        end_date="",
    )

    assert not _is_unstructured_dossier(dossier)
    assert _dossier_code_line(dossier) == "A99-005-01-7777"


def test_repository_stats_include_dossiers_docs_pages_and_chunks():
    assert _format_repo_stats(2, 45, 205, 788) == "2 hồ sơ · 45 tài liệu · 205 trang · 788 đoạn"


def test_open_dossier_status_puts_title_before_gray_stats():
    dossier = DossierRow(
        dossier_id=1,
        title="Hồ sơ thứ nhất",
        ma_dinh_danh="A99",
        fonds="01",
        catalog="A00",
        dossier_code="0008",
        doc_count=36,
        page_count=175,
        start_date="",
        end_date="",
    )

    html = _dossier_status_html(dossier)

    assert "Hồ sơ thứ nhất" in html
    assert "36 tài liệu · 175 trang" in html
    assert html.index("Hồ sơ thứ nhất") < html.index("36 tài liệu")
    assert "font-weight:600" in html


def test_pdf_viewer_defers_search_jump_until_async_render_finishes(monkeypatch, tmp_path):
    _qapp()
    viewer = PdfViewerWidget(fit_on_load=False)

    def fake_load(path):
        viewer._pdf_path = str(path)
        viewer._raw_pixmaps = []
        viewer._pages_widget.clear_pages()

    monkeypatch.setattr(viewer, "load_pdf", fake_load)

    viewer.show_pdf(
        tmp_path / "doc.pdf",
        page=3,
        bboxes=[[10, 20, 50, 40]],
        highlight_style="highlight",
    )

    assert viewer._pending_view_scroll == (2, [10, 20, 50, 40])
    assert viewer._current_search_highlight == (2, [[10, 20, 50, 40]], "highlight")
    viewer.deleteLater()


def test_hydrated_bboxes_paint_onto_the_currently_open_pdf_pane(tmp_path):
    """Round-6: mở văn bản tìm thấy LẦN ĐẦU đua với hydration bbox ngoài
    luồng — khi hydration xong, các khung phải được vẽ NGAY lên pane đang
    mở (không đợi bấm sang văn bản khác rồi quay lại), và không vẽ khi
    pane đã chuyển sang văn bản khác."""
    from types import SimpleNamespace

    from scanindex.ui.repository.screen import RepositoryScreen

    archive = tmp_path / "repo"
    (archive / "pdf").mkdir(parents=True)
    row = SimpleNamespace(doc_id="d1", file_path="pdf/one.pdf")
    pdf_abs = str((archive / "pdf/one.pdf").resolve())

    class FakePane:
        def __init__(self, path):
            self._pdf_path = path
            self.paints = []

        def highlight_page_regions(self, page_boxes, style="box"):
            self.paints.append((list(page_boxes), style))

    pane = FakePane(pdf_abs)
    scr = SimpleNamespace(
        _current_file=row, _archive_path=archive, _pdf_pane=pane,
        _match_page_boxes=RepositoryScreen._match_page_boxes,
    )
    hydrated = [SimpleNamespace(
        chunk_type="body", page=2, chunk_id=7,
        match_bboxes=[[10.0, 20.0, 30.0, 40.0]],
    )]

    RepositoryScreen._apply_match_highlight_to_pane(scr, hydrated)
    assert len(pane.paints) == 1, "bbox hydrate xong phải vẽ lên pane đang mở"
    page_boxes, style = pane.paints[0]
    assert style == "highlight"
    assert page_boxes == [(1, [10.0, 20.0, 30.0, 40.0])]  # page 2 → index 0-based

    # Pane đã chuyển sang văn bản khác → không được vẽ đè.
    other_pane = FakePane(str((archive / "pdf/two.pdf").resolve()))
    scr_moved = SimpleNamespace(
        _current_file=row, _archive_path=archive, _pdf_pane=other_pane,
    )
    RepositoryScreen._apply_match_highlight_to_pane(scr_moved, hydrated)
    assert other_pane.paints == []

    # Không có khung nào để vẽ → pane giữ nguyên.
    RepositoryScreen._apply_match_highlight_to_pane(
        scr, [SimpleNamespace(chunk_type="body", page=2, chunk_id=8,
                              match_bboxes=[])])
    assert len(pane.paints) == 1


def test_right_panel_enables_metadata_edit_for_selected_file(tmp_path):
    _qapp()
    panel = _RightPanel()
    file_row = FileRow(
        doc_id="doc-1",
        dossier_id=1,
        file_name="A.pdf",
        file_path="A.pdf",
        subject="",
        doc_number="",
        issue_org="",
        issue_org_superior="",
        signer_name="",
        issue_date="",
        doc_type="",
        secrecy_mark="",
        page_count=1,
    )

    assert not panel.btn_edit_metadata.isEnabled()
    panel.show_file(file_row, tmp_path)

    assert panel.btn_edit_metadata.isEnabled()
    panel.deleteLater()


def test_metadata_dialog_factory_keeps_builder_reference(tmp_path):
    _qapp()
    pdf = tmp_path / "A.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    dlg = _AddFileMetadataDialog(
        pdf_path=pdf,
        body_chunk_count=1,
        initial_doc_type="",
        parent=None,
    )

    assert hasattr(dlg, "get_fields")
    dlg.deleteLater()


def test_date_filter_input_keeps_typed_ddmmyyyy_value():
    _qapp()
    widget = _DateFilterInput()

    widget.setText("10/03/2026")

    assert widget.text() == "10/03/2026"
    widget.clear()
    assert widget.text() == ""
    widget.deleteLater()


def test_metadata_date_key_accepts_user_and_kie_date_formats():
    assert _date_key("10/03/2026") == "20260310"
    assert _date_key("2026-03-10") == "20260310"
    assert _date_key("TP.HCM, ngày 10 tháng 3 năm 2026") == "20260310"


def test_metadata_advanced_fuzzy_allows_ordered_token_gap():
    assert _advanced_text_match("pham van hien", "Tham Van Hien")
    assert _advanced_text_match("pham hien", "Phạm Văn Hiền")
    assert _advanced_text_match("van phong thanh uy", "VĂN PHÒNG THÀNH ỦY")
