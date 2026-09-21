"""Archive Step 1 JSON-only assembly (`pdf_output=False`) and the failed-page
guard — gates from REVIEW_ocr_assembly_speedup.md §5 and C1."""
import copy
import os
import pickle

import pytest

fitz = pytest.importorskip("fitz")


def _make_source_pdf(path, pages=2):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.draw_rect(fitz.Rect(50, 50, 300, 120 + 30 * i))
    doc.save(path)
    doc.close()


def _make_page_result(seed=0, n_lines=8):
    lines = []
    words = []
    tokens = ["V\u0103n", "ph\u00f2ng", "\u0110\u1ea3ng", "\u1ee7y", "b\u00e1o",
              "c\u00e1o", "t\u00ecnh", "h\u1ecdp", "ngh\u1ecb", "qu\u1eadn"]
    for k in range(n_lines):
        text = tokens[(seed + k) % len(tokens)]
        x = 40.0 + (k % 3) * 8.0
        y = 80.0 + k * 24.0
        lines.append({
            "text": text, "order": k,
            "x": x, "y": y, "w": 120.0, "h": 12.0,
        })
        words.append({
            "text": text,
            "x": x, "y": y, "w": 60.0, "h": 12.0,
            "has_space_after": k < n_lines - 1,
        })
    return {
        "lines_data": lines,
        "words_data": words,
        "render_width": 1984,
        "render_height": 2805,
    }


def _assemble_shared_source(tmp_path, src, name, page_results, pdf_output):
    from scanindex.core.ocr import direct_engine as de

    out = str(tmp_path / f"out_{name}.pdf")
    ok, msg = de.assemble_pdf_from_page_results(
        src,
        out,
        page_results,
        source_document_path=src,
        canonical_profile="layoutlmv3_runtime",
        include_layout_analysis=False,
        pdf_output=pdf_output,
    )
    assert ok, msg
    return out


def _load_stripped(json_path):
    from scanindex.core.canonical_io import load_canonical

    doc = load_canonical(json_path)
    try:
        doc["pipeline"]["ocr"].pop("completed_at", None)
    except (KeyError, TypeError):
        pass
    return doc


def test_json_only_assembly_matches_full_build(tmp_path):
    results = {0: _make_page_result(seed=0), 1: _make_page_result(seed=3)}
    src = str(tmp_path / "src_shared.pdf")
    _make_source_pdf(src, pages=len(results))

    out_full = _assemble_shared_source(
        tmp_path, src, "full", copy.deepcopy(results), pdf_output=True)
    out_json = _assemble_shared_source(
        tmp_path, src, "json", copy.deepcopy(results), pdf_output=False)

    assert os.path.exists(out_full)
    assert not os.path.exists(out_json)
    assert os.path.exists(out_full + ".json.zst")
    assert os.path.exists(out_json + ".json.zst")

    assert _load_stripped(out_full + ".json.zst") == _load_stripped(out_json + ".json.zst")

    # The full build's PDF carries the invisible word overlay.
    doc = fitz.open(out_full)
    assert "V\u0103n" in doc[0].get_text()
    doc.close()


def test_json_sidecar_resolves_without_pdf(tmp_path):
    from scanindex.core.canonical_io import resolve_companion

    src = str(tmp_path / "src_resolve.pdf")
    _make_source_pdf(src, pages=1)
    out_json = _assemble_shared_source(
        tmp_path, src, "resolve", {0: _make_page_result()}, pdf_output=False)
    resolved = resolve_companion(out_json)
    assert str(resolved) == out_json + ".json.zst"
    assert os.path.exists(str(resolved))


def test_assemble_payload_pdf_output_default_true(tmp_path):
    from scanindex.core.ocr.direct_engine import assemble_pdf_from_page_results_payload

    src = str(tmp_path / "src_payload.pdf")
    _make_source_pdf(src, pages=1)
    out_default = str(tmp_path / "out_payload_default.pdf")
    payload_default = {
        "input_path": src,
        "output_path": out_default,
        "page_results": {0: _make_page_result()},
        "canonical_profile": "layoutlmv3_runtime",
        "include_layout_analysis": False,
        # no pdf_output key -> must default to building the PDF
    }
    pkl = str(tmp_path / "payload_default.pkl")
    with open(pkl, "wb") as fh:
        pickle.dump(payload_default, fh)
    result = assemble_pdf_from_page_results_payload(pkl)
    assert result["ok"], result["msg"]
    assert os.path.exists(out_default)
    assert os.path.exists(out_default + ".json.zst")


def test_assemble_payload_pdf_output_false(tmp_path):
    from scanindex.core.ocr.direct_engine import assemble_pdf_from_page_results_payload

    src = str(tmp_path / "src_payload2.pdf")
    _make_source_pdf(src, pages=1)
    out_json = str(tmp_path / "out_payload_json.pdf")
    payload = {
        "input_path": src,
        "output_path": out_json,
        "page_results": {0: _make_page_result()},
        "canonical_profile": "layoutlmv3_runtime",
        "include_layout_analysis": False,
        "pdf_output": False,
    }
    pkl = str(tmp_path / "payload_json.pkl")
    with open(pkl, "wb") as fh:
        pickle.dump(payload, fh)
    result = assemble_pdf_from_page_results_payload(pkl)
    assert result["ok"], result["msg"]
    assert not os.path.exists(out_json)
    assert os.path.exists(out_json + ".json.zst")


def test_find_failed_page_results_classification():
    from scanindex.core.ocr.direct_engine import find_failed_page_results

    blank_valid = {
        "lines_data": [], "words_data": [],
        "render_width": 1984, "render_height": 2805,
    }
    page_results = {
        0: blank_valid,                                   # valid blank page
        1: {"worker_init_failed": True, "error": "dll"},  # engine failure
        2: {"error": "boom"},                             # engine failure
        3: _make_page_result(),                           # valid content page
        # pages 4..5 missing entirely
    }
    failures = find_failed_page_results(page_results, total_pages=6)
    assert failures == [
        (1, "engine-error"),
        (2, "engine-error"),
        (4, "missing"),
        (5, "missing"),
    ]

    # All-good document reports nothing.
    good = {i: _make_page_result(seed=i) for i in range(3)}
    assert find_failed_page_results(good, total_pages=3) == []
