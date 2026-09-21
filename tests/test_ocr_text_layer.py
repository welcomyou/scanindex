import pytest

fitz = pytest.importorskip("fitz")


def test_word_level_text_layer_preserves_vietnamese_word_bboxes(tmp_path):
    from scanindex.core.ocr.direct_engine import (
        _build_text_page_words,
        _find_unicode_font,
    )

    font_path = _find_unicode_font()
    if not font_path:
        pytest.skip("No Unicode font available")

    words = [
        {"text": "V\u0103n", "bbox": [242.29, 241.42, 262.26, 253.95]},
        {"text": "ph\u00f2ng", "bbox": [265.37, 241.42, 296.59, 253.95]},
        {"text": "\u0110\u1ea3ng", "bbox": [299.70, 241.42, 326.54, 253.95]},
        {"text": "\u1ee7y", "bbox": [329.65, 241.42, 341.51, 253.95]},
        {"text": "x\u00e3", "bbox": [344.62, 241.42, 356.47, 253.95]},
        {"text": "Ngh\u0129a", "bbox": [359.59, 241.42, 389.54, 253.95]},
        {"text": "Th\u00e0nh", "bbox": [392.65, 241.42, 424.47, 253.95]},
        {"text": "b\u00e1o", "bbox": [427.58, 241.42, 446.31, 253.95]},
        {"text": "c\u00e1o", "bbox": [449.43, 241.42, 467.53, 253.95]},
        {"text": "t\u00ecnh", "bbox": [470.64, 241.42, 489.35, 253.95]},
    ]

    doc = fitz.open()
    page = doc.new_page(width=600, height=850)
    _build_text_page_words(page, words, font_path)
    out_path = tmp_path / "word_text_layer.pdf"
    doc.save(out_path)
    doc.close()

    doc = fitz.open(out_path)
    page = doc[0]
    assert page.get_text().strip() == "V\u0103n ph\u00f2ng \u0110\u1ea3ng \u1ee7y x\u00e3 Ngh\u0129a Th\u00e0nh b\u00e1o c\u00e1o t\u00ecnh"

    extracted = page.get_text("words")
    assert [w[4] for w in extracted] == [w["text"] for w in words]
    for expected, got in zip(words, extracted):
        bbox = expected["bbox"]
        assert abs(got[0] - bbox[0]) < 0.1
        assert abs(got[1] - bbox[1]) < 0.15
        assert abs(got[2] - bbox[2]) < 0.1
        assert abs(got[3] - bbox[3]) < 0.15
    doc.close()


def test_word_level_text_layer_preserves_explicit_space_for_tight_bboxes(tmp_path):
    from scanindex.core.ocr.direct_engine import (
        _build_text_page_words,
        _find_unicode_font,
    )

    font_path = _find_unicode_font()
    if not font_path:
        pytest.skip("No Unicode font available")

    words = [
        {
            "text": "V\u0169ng",
            "bbox": [163.76, 318.26, 193.75, 329.96],
            "has_space_after": True,
        },
        {
            "text": "T\u00e0u",
            "bbox": [194.95, 318.26, 215.35, 328.76],
            "has_space_after": False,
        },
    ]

    doc = fitz.open()
    page = doc.new_page(width=600, height=850)
    _build_text_page_words(page, words, font_path)
    out_path = tmp_path / "tight_word_text_layer.pdf"
    doc.save(out_path)
    doc.close()

    doc = fitz.open(out_path)
    page = doc[0]
    assert page.get_text().strip() == "V\u0169ng T\u00e0u"
    assert [word[4] for word in page.get_text("words")] == ["V\u0169ng", "T\u00e0u"]
    doc.close()


def test_native_spacing_damage_rejects_glued_native_when_ocr_has_more_words():
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


# ---------------------------------------------------------------------------
# Batched word-overlay (`_build_text_page_words` fast path) — parity vs the
# verbatim legacy implementation `_build_text_page_words_legacy`, adversarial
# stream tokens, and the guaranteed fallback/rollback contract from
# REVIEW_ocr_assembly_speedup.md §4.
# ---------------------------------------------------------------------------

import copy


def _dense_vietnamese_words(rows=10, cols=12):
    tokens = [
        "V\u0103n", "ph\u00f2ng", "\u0110\u1ea3ng", "\u1ee7y", "b\u00e1o",
        "c\u00e1o", "t\u00ecnh", "h\u1ecdp", "ngh\u1ecb", "qu\u1eadn",
        "7", "ch\u1ec9", "\u0111\u1ea1o", "s\u1ed1", "35-BC/\u0110U",
    ]
    words = []
    for r in range(rows):
        x = 40.0
        for c in range(cols):
            text = tokens[(r * cols + c) % len(tokens)]
            w = 4.2 * len(text) + 3.0
            words.append({
                "text": text,
                "bbox": [x, 60.0 + r * 18.0, x + w, 72.0 + r * 18.0],
                "has_space_after": (c < cols - 1),
            })
            x += w + 6.0
    return words


def _adversarial_words():
    texts = [
        "BT", "ET", "(x)", "EMC", "back\\slash", "qu)ote",
        "[<0042>]", "Tr\u01b0\u1eddng", "verst\u00e4ndnis",
        "\U0001f680", "\u5317\u4eac", "caf\u00e9",
        "a" * 240,
    ]
    words = []
    for i, text in enumerate(texts):
        words.append({
            "text": text,
            "bbox": [40.0, 60.0 + i * 20.0, 40.0 + 6.0 * len(text) + 5.0, 72.0 + i * 20.0],
            "has_space_after": i != len(texts) - 1,
        })
    return words


def _build_overlay_reference(words, font_path, with_background=False):
    """Legacy overlay on a fresh doc; returns (doc, page)."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=850)
    if with_background:
        page.insert_text(fitz.Point(300, 820), "BACKGROUND", fontsize=8)
    from scanindex.core.ocr.direct_engine import _build_text_page_words_legacy
    _build_text_page_words_legacy(page, copy.deepcopy(words), font_path)
    return doc, page


def _snapshots(page):
    return {
        "text": page.get_text("text"),
        "words": page.get_text("words"),
        "pixels": page.get_pixmap(dpi=72).samples,
    }


def _assert_overlay_parity(words):
    from scanindex.core.ocr.direct_engine import (
        _build_text_page_words,
        _find_unicode_font,
    )
    font_path = _find_unicode_font()
    if not font_path:
        pytest.skip("No Unicode font available")

    ref_doc, ref_page = _build_overlay_reference(words, font_path)
    ref = _snapshots(ref_page)

    doc = fitz.open()
    page = doc.new_page(width=600, height=850)
    assert _build_text_page_words(page, copy.deepcopy(words), font_path) is None
    got = _snapshots(page)

    assert got["text"] == ref["text"]
    assert got["words"] == ref["words"]
    assert got["pixels"] == ref["pixels"]


def test_batch_overlay_matches_legacy_dense_page():
    _assert_overlay_parity(_dense_vietnamese_words())


def test_batch_overlay_matches_legacy_sparse_page():
    words = _dense_vietnamese_words(rows=1, cols=5)
    _assert_overlay_parity(words)


def test_batch_overlay_matches_legacy_adversarial_tokens():
    _assert_overlay_parity(_adversarial_words())


def test_batch_overlay_preserves_actualtext_spacing():
    from scanindex.core.ocr.direct_engine import (
        _build_text_page_words,
        _find_unicode_font,
    )
    font_path = _find_unicode_font()
    if not font_path:
        pytest.skip("No Unicode font available")

    words = [
        {"text": "V\u0169ng", "bbox": [163.76, 318.26, 193.75, 329.96],
         "has_space_after": True},
        {"text": "T\u00e0u", "bbox": [194.95, 318.26, 215.35, 328.76],
         "has_space_after": False},
    ]
    doc = fitz.open()
    page = doc.new_page(width=600, height=850)
    _build_text_page_words(page, copy.deepcopy(words), font_path)
    extracted = page.get_text("words")
    assert [w[4] for w in extracted] == ["V\u0169ng", "T\u00e0u"]


def test_batch_overlay_all_words_filtered_touches_no_stream():
    from scanindex.core.ocr.direct_engine import (
        _build_text_page_words,
        _find_unicode_font,
    )
    font_path = _find_unicode_font()
    if not font_path:
        pytest.skip("No Unicode font available")

    words = [
        {"text": "   ", "bbox": [10, 10, 60, 22]},
        {"text": "zero-width", "bbox": [10, 30, 10, 42]},
        {"text": "zero-height", "bbox": [10, 50, 60, 50]},
        {"text": "", "bbox": [10, 70, 60, 82]},
    ]
    doc = fitz.open()
    page = doc.new_page(width=600, height=850)
    before = len(page.get_contents())
    _build_text_page_words(page, copy.deepcopy(words), font_path)
    assert len(page.get_contents()) == before
    assert page.get_text().strip() == ""


def test_batch_overlay_wrap_invalid_falls_back_to_legacy(monkeypatch):
    import scanindex.core.ocr.direct_engine as de
    from scanindex.core.ocr.direct_engine import _find_unicode_font
    font_path = _find_unicode_font()
    if not font_path:
        pytest.skip("No Unicode font available")

    words = _dense_vietnamese_words(rows=4, cols=6)
    monkeypatch.setattr(de, "_wrap_actual_text_spans", lambda raw, hexes: None)

    ref_doc, ref_page = _build_overlay_reference(words, font_path, with_background=True)
    ref_text = ref_page.get_text("text")
    ref_words = ref_page.get_text("words")

    doc = fitz.open()
    page = doc.new_page(width=600, height=850)
    page.insert_text(fitz.Point(300, 820), "BACKGROUND", fontsize=8)
    de._build_text_page_words(page, copy.deepcopy(words), font_path)

    assert page.get_text("text") == ref_text
    assert page.get_text("words") == ref_words
    # Spacing must survive the fallback (no glued words).
    assert any(w[4] == "V\u0103n" for w in page.get_text("words"))


def test_batch_overlay_staging_mismatch_rolls_back_and_falls_back(monkeypatch):
    import scanindex.core.ocr.direct_engine as de
    from scanindex.core.ocr.direct_engine import _find_unicode_font
    font_path = _find_unicode_font()
    if not font_path:
        pytest.skip("No Unicode font available")

    real_stage = de._stage_word_overlay_stream

    def tampered_stage(page_w, page_h, fill):
        raw = real_stage(page_w, page_h, fill)
        if raw is None:
            return None
        return raw + b"% tampered"  # force phase-3 byte mismatch

    monkeypatch.setattr(de, "_stage_word_overlay_stream", tampered_stage)

    words = _dense_vietnamese_words(rows=3, cols=6)
    ref_doc, ref_page = _build_overlay_reference(words, font_path, with_background=True)

    doc = fitz.open()
    page = doc.new_page(width=600, height=850)
    page.insert_text(fitz.Point(300, 820), "BACKGROUND", fontsize=8)
    de._build_text_page_words(page, copy.deepcopy(words), font_path)

    # Rollback must leave exactly the legacy result: overlay present once,
    # background intact — no doubled text.
    assert page.get_text("text") == ref_page.get_text("text")
    assert page.get_text("words") == ref_page.get_text("words")
    assert page.get_pixmap(dpi=72).samples == ref_page.get_pixmap(dpi=72).samples
