from scanindex.core.kie.inference_pipeline import detect_secrecy_mark


MAT = "M\u1eacT"
TOI_MAT = "T\u1ed0I M\u1eacT"
FATHERLAND_FRONT = "M\u1eb6T TR\u1eacN T\u1ed4 QU\u1ed0C VI\u1ec6T NAM"
BODY_SECRET_TEXT = (
    "PH\u00c2N LO\u1ea0I T\u00c0I LI\u1ec6U B\u00cd M\u1eacT "
    "NH\u00c0 N\u01af\u1edaC"
)
DOC_NUMBER_SECRET_SUFFIX = "S\u1ed0 12/M\u1eacT"


def _doc(text: str, bbox: list[float]) -> dict:
    return {
        "pages": [
            {
                "page_index": 0,
                "width": 595.0,
                "height": 842.0,
                "lines": [{"id": "l1", "text": text, "bbox": bbox}],
            }
        ]
    }


def test_detects_secrecy_stamp_in_roi() -> None:
    assert detect_secrecy_mark(_doc(TOI_MAT, [40, 40, 140, 65])) == TOI_MAT


def test_default_detector_uses_text_fallback_outside_roi() -> None:
    assert detect_secrecy_mark(_doc(MAT, [450, 450, 500, 475])) == MAT


def test_strict_detector_preserves_roi_rule() -> None:
    assert detect_secrecy_mark(
        _doc(MAT, [450, 450, 500, 475]),
        text_fallback=False,
    ) is None


def test_rejects_common_false_positive_lines() -> None:
    assert detect_secrecy_mark(_doc(FATHERLAND_FRONT, [40, 40, 280, 65])) is None
    assert detect_secrecy_mark(_doc(BODY_SECRET_TEXT, [40, 450, 420, 475])) is None


def test_detects_secret_marker_in_document_number_line() -> None:
    assert detect_secrecy_mark(_doc(DOC_NUMBER_SECRET_SUFFIX, [40, 450, 160, 475])) == MAT


MTTQ_HEADER = "ỦY BAN MTTQ VIỆT NAM"
SPELLT_FRONT_HEADER = "ỦY BAN MẶT TRẬN TỔ QUỐC VIỆT NAM"
SEAL_FRAGMENT = "MAT"


def _multi_doc(lines: list[tuple[str, list[float]]]) -> dict:
    return {
        "pages": [
            {
                "page_index": 0,
                "width": 595.0,
                "height": 842.0,
                "lines": [
                    {"id": f"l{i}", "text": text, "bbox": bbox}
                    for i, (text, bbox) in enumerate(lines)
                ],
            }
        ]
    }


def test_mattran_page_bottom_seal_fragment_not_flagged() -> None:
    doc = _multi_doc(
        [
            (MTTQ_HEADER, [40, 40, 280, 65]),
            (SEAL_FRAGMENT, [404, 739, 416, 746]),
        ]
    )
    assert detect_secrecy_mark(doc) is None


def test_mattran_spelled_out_header_bottom_fragment_not_flagged() -> None:
    doc = _multi_doc(
        [
            (SPELLT_FRONT_HEADER, [40, 40, 320, 65]),
            (SEAL_FRAGMENT, [355, 590, 368, 599]),
        ]
    )
    assert detect_secrecy_mark(doc) is None


def test_mattran_page_top_mat_stamp_still_flagged() -> None:
    doc = _multi_doc(
        [
            (MTTQ_HEADER, [40, 40, 280, 65]),
            (MAT, [40, 80, 90, 105]),
        ]
    )
    assert detect_secrecy_mark(doc) == MAT


def test_mattran_page_upper_half_mat_outside_roi_still_flagged() -> None:
    doc = _multi_doc(
        [
            (MTTQ_HEADER, [40, 40, 280, 65]),
            (MAT, [450, 300, 500, 325]),
        ]
    )
    assert detect_secrecy_mark(doc) == MAT


def test_non_mattran_page_bottom_mat_still_flagged() -> None:
    doc = _multi_doc(
        [
            ("ỦY BAN NHÂN DÂN XÃ A", [40, 40, 280, 65]),
            (MAT, [450, 450, 500, 475]),
        ]
    )
    assert detect_secrecy_mark(doc) == MAT
