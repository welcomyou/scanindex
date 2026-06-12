from __future__ import annotations

import pytest

from scanindex.core.digitization.runner import ArchiveRunner
from scanindex.core.pipeline.batch_pipeline import BatchPipeline, FileTask


def _task(tmp_path, *, num_pages: int, page_results: dict[int, dict]) -> FileTask:
    return FileTask(
        file_id="sample.pdf",
        input_path=str(tmp_path / "sample.pdf"),
        output_pdf_path=str(tmp_path / "sample_ocr.pdf"),
        output_json_path=str(tmp_path / "sample_ocr.pdf.json.zst"),
        source_document_path=str(tmp_path / "sample.pdf"),
        num_pages=num_pages,
        page_results=page_results,
    )


def _page_result() -> dict:
    return {
        "lines_data": [{"text": "sample"}],
        "words_data": [{"text": "sample"}],
        "render_width": 100,
        "render_height": 100,
    }


def test_assemble_outputs_reuses_complete_page_cache(monkeypatch, tmp_path):
    runner = ArchiveRunner(output_dir=str(tmp_path))
    task = _task(tmp_path, num_pages=2, page_results={0: _page_result(), 1: _page_result()})
    calls = []

    def assemble(*args, **kwargs):
        calls.append((args, kwargs))
        return True, None

    monkeypatch.setattr(
        "scanindex.core.digitization.runner.direct_ocr_engine.assemble_pdf_from_page_results",
        assemble,
    )
    monkeypatch.setattr(
        "scanindex.core.digitization.runner.direct_ocr_engine.process_pdf",
        lambda *args, **kwargs: pytest.fail("KIE preparation must not OCR the full PDF again"),
    )

    runner._assemble_outputs(task)

    assert len(calls) == 1
    assert calls[0][0][2] is task.page_results


def test_assemble_outputs_can_run_in_isolated_process(monkeypatch, tmp_path):
    fitz = pytest.importorskip("fitz")
    input_pdf = tmp_path / "sample.pdf"
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((40, 80), "sample")
    doc.save(str(input_pdf))
    doc.close()

    runner = ArchiveRunner(output_dir=str(tmp_path))
    task = FileTask(
        file_id="sample.pdf",
        input_path=str(input_pdf),
        output_pdf_path=str(tmp_path / "sample_ocr.pdf"),
        output_json_path=str(tmp_path / "sample_ocr.pdf.json.zst"),
        source_document_path=str(input_pdf),
        num_pages=1,
        page_results={
            0: {
                "lines_data": [{
                    "id": "p0_l0",
                    "text": "sample",
                    "bbox": [40, 68, 80, 84],
                    "word_ids": ["p0_l0_w0"],
                }],
                "words_data": [{
                    "id": "p0_l0_w0",
                    "line_id": "p0_l0",
                    "text": "sample",
                    "bbox": [40, 68, 80, 84],
                    "has_space_after": False,
                }],
                "render_width": 300,
                "render_height": 200,
            }
        },
    )
    monkeypatch.setenv("OCRTOOL_STEP2_ASSEMBLE_PROCESS", "1")

    runner._assemble_outputs(task)

    assert (tmp_path / "sample_ocr.pdf").exists()
    assert (tmp_path / "sample_ocr.pdf.json.zst").exists()


def test_assemble_outputs_rejects_incomplete_page_cache_without_reocr(monkeypatch, tmp_path):
    runner = ArchiveRunner(output_dir=str(tmp_path))
    task = _task(tmp_path, num_pages=2, page_results={0: _page_result()})

    monkeypatch.setattr(
        "scanindex.core.digitization.runner.direct_ocr_engine.assemble_pdf_from_page_results",
        lambda *args, **kwargs: pytest.fail("Incomplete OCR cache must not be assembled"),
    )
    monkeypatch.setattr(
        "scanindex.core.digitization.runner.direct_ocr_engine.process_pdf",
        lambda *args, **kwargs: pytest.fail("KIE preparation must not OCR the full PDF again"),
    )

    with pytest.raises(RuntimeError, match=r"missing page\(s\): 2"):
        runner._assemble_outputs(task)


def test_batch_pipeline_submits_each_page_exactly_once(tmp_path):
    submitted = []

    class ReadyResult:
        def __init__(self, page_idx):
            self.page_idx = page_idx

        def ready(self):
            return True

        def get(self, timeout=None):
            return self.page_idx, _page_result()

    def submit(input_path, page_idx):
        submitted.append((input_path, page_idx))
        return ReadyResult(page_idx)

    tasks = [
        _task(tmp_path, num_pages=3, page_results={}),
        FileTask(
            file_id="second.pdf",
            input_path=str(tmp_path / "second.pdf"),
            output_pdf_path=str(tmp_path / "second_ocr.pdf"),
            output_json_path=str(tmp_path / "second_ocr.pdf.json.zst"),
            num_pages=2,
        ),
    ]
    pipeline = BatchPipeline(
        ocr_submit=submit,
        run_correction=lambda task: "",
        run_kie=lambda task: {},
    )
    for task in tasks:
        pipeline.add_file(task)
    pipeline.mark_no_more_files()
    pipeline.start()

    assert pipeline.wait(timeout=5)
    assert submitted == [
        (tasks[0].input_path, 0),
        (tasks[0].input_path, 1),
        (tasks[0].input_path, 2),
        (tasks[1].input_path, 0),
        (tasks[1].input_path, 1),
    ]


def test_clean_signer_page_skips_second_ocr_without_annotations(monkeypatch, tmp_path):
    runner = ArchiveRunner(output_dir=str(tmp_path))
    task = _task(tmp_path, num_pages=3, page_results={})
    task.signature_page = 2

    monkeypatch.setattr(
        "scanindex.core.digitization.runner.direct_ocr_engine.page_has_render_annotations",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "scanindex.core.digitization.runner.direct_ocr_engine.ocr_one_page",
        lambda *args, **kwargs: pytest.fail("Signer page without annotations must reuse OCR"),
    )

    assert runner._prepare_signature_page_clean_ocr_for_kie(task) is False


def test_clean_signer_page_reocrs_only_selected_annotated_page(monkeypatch, tmp_path):
    runner = ArchiveRunner(output_dir=str(tmp_path))
    task = _task(tmp_path, num_pages=4, page_results={})
    task.signature_page = 3
    ocr_calls = []

    monkeypatch.setattr(
        "scanindex.core.digitization.runner.direct_ocr_engine.page_has_render_annotations",
        lambda input_path, page_idx: page_idx == 3,
    )

    def ocr_one_page(input_path, page_idx, timeout, render_annots):
        ocr_calls.append((input_path, page_idx, render_annots))
        return _page_result()

    monkeypatch.setattr(
        "scanindex.core.digitization.runner.direct_ocr_engine.ocr_one_page",
        ocr_one_page,
    )
    monkeypatch.setattr(
        "scanindex.core.digitization.runner.direct_ocr_engine.replace_canonical_page_with_page_result",
        lambda *args, **kwargs: {"page_index": 3, "line_count": 1, "word_count": 1},
    )

    assert runner._prepare_signature_page_clean_ocr_for_kie(task) is True
    assert ocr_calls == [(task.input_path, 3, False)]


def test_native_spacing_damage_rejects_glued_native_without_reocr():
    from scanindex.core.pdf.text_extractor import _native_rejection_stats

    glued_tokens = [
        "\u0110\u1ea2NG",
        "B\u1ed8TH\u00c0NH",
        "PH\u1ed0H\u1ed2CH\u00cdMINH",
        "V\u1ec1t\u00ecnh",
        "h\u1ec7th\u1ed1ng",
        "h\u1ea1t\u1ea7ng",
        "b\u1ecbc\u00f4ng",
        "ngh\u1ec7th\u00f4ng",
        "s\u1ed105-TB/BC\u0110",
        "s\u1ed1Th\u00e0nh",
    ]
    native_items = [{"text": token} for token in glued_tokens]
    native_items.extend({"text": "sample"} for _ in range(60))
    ocr_items = [{"text": "ocr"} for _ in range(85)]

    stats = _native_rejection_stats(native_items, ocr_items)

    assert stats["reject_native"] is True
    assert stats["native_spacing_rejected"] is True
    assert stats["native_mojibake_rejected"] is False


def test_native_spacing_damage_keeps_native_without_ocr_word_gain():
    from scanindex.core.pdf.text_extractor import _native_rejection_stats

    native_items = [{"text": "sample"} for _ in range(70)]
    ocr_items = [{"text": "ocr"} for _ in range(72)]

    stats = _native_rejection_stats(native_items, ocr_items)

    assert stats["reject_native"] is False
    assert stats["native_spacing_rejected"] is False
