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


def test_fallback_accepts_stamp_in_top_left_quadrant() -> None:
    """Trang đã xoay đúng chiều: dấu đóng dưới ngưỡng ROI nhưng vẫn trong
    góc tư trên-trái vẫn được nhận qua fallback."""
    assert detect_secrecy_mark(_doc(MAT, [40, 300, 90, 325])) == MAT


def test_fallback_accepts_stamp_in_relaxed_quadrant() -> None:
    """Vùng nhận nới rộng tới cx≤0.6, cy≤0.6 (chịu bbox OCR trôi nhẹ)."""
    # cx = 325/595 ≈ 0.55, cy = 442/842 ≈ 0.53
    assert detect_secrecy_mark(_doc(MAT, [300, 430, 350, 455])) == MAT
    # Vượt quá 0.6 theo chiều dọc → từ chối.
    assert detect_secrecy_mark(_doc(MAT, [300, 560, 350, 585])) is None


def test_fallback_rejects_stamp_below_top_half() -> None:
    """Sau khi xoay đúng chiều, "MẬT" đứng một mình ở nửa dưới không phải
    dấu mật (dấu thật chỉ ở góc trên-trái) — thường là mảnh mộc/nhiễu."""
    assert detect_secrecy_mark(_doc(MAT, [450, 450, 500, 475])) is None


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
            (MAT, [220, 300, 270, 325]),
        ]
    )
    assert detect_secrecy_mark(doc) == MAT


def test_mattran_page_right_edge_mat_not_flagged() -> None:
    """Dấu mộc Mặt trận giáp la / mép phải trang là dấu tròn, không phải dấu mật."""
    doc = _multi_doc(
        [
            (MTTQ_HEADER, [40, 40, 280, 65]),
            (MAT, [440, 300, 470, 325]),
        ]
    )
    assert detect_secrecy_mark(doc) is None


def test_mattran_shredded_header_right_edge_mat_not_flagged() -> None:
    doc = _multi_doc(
        [
            ("ỦY BAN", [40, 40, 120, 60]),
            ("MẶT TRẬN TỔ QUỐC", [40, 62, 260, 82]),
            ("VIỆT NAM", [40, 84, 160, 104]),
            (MAT, [450, 260, 480, 285]),
        ]
    )
    assert detect_secrecy_mark(doc) is None


def test_non_front_page_right_edge_mat_with_tran_ring_neighbour_not_flagged() -> None:
    """Trang không nhận ra Mặt trận nhưng "MAT" sát mảnh vòng cung "TRAN" là dấu tròn."""
    doc = _multi_doc(
        [
            ("ỦY BAN NHÂN DÂN XÃ A", [40, 40, 280, 65]),
            (MAT, [450, 300, 480, 325]),
            ("TRAN", [430, 350, 470, 370]),
        ]
    )
    assert detect_secrecy_mark(doc) is None


def test_non_front_page_right_edge_mat_without_tran_neighbour_not_flagged() -> None:
    """Trang đã xoay đúng chiều: dấu đóng ngoài góc tư trên-trái (mép phải)
    không được nhận là dấu mật, kể cả khi không có mảnh vòng cung kề bên."""
    doc = _multi_doc(
        [
            ("ỦY BAN NHÂN DÂN XÃ A", [40, 40, 280, 65]),
            (MAT, [450, 300, 480, 325]),
        ]
    )
    assert detect_secrecy_mark(doc) is None


def test_lowercase_tran_line_is_not_a_ring_neighbour() -> None:
    """"Trân trọng" in hoa thấp — không phải mảnh vòng cung dấu tròn."""
    doc = _multi_doc(
        [
            ("ỦY BAN NHÂN DÂN XÃ A", [40, 40, 280, 65]),
            (MAT, [200, 300, 250, 325]),
            ("Trân trọng", [180, 350, 260, 370]),
        ]
    )
    assert detect_secrecy_mark(doc) == MAT


def test_tran_line_far_from_mat_is_not_a_ring_neighbour() -> None:
    doc = _multi_doc(
        [
            ("ỦY BAN NHÂN DÂN XÃ A", [40, 40, 280, 65]),
            (MAT, [200, 300, 250, 325]),
            ("TRAN", [40, 760, 80, 780]),
        ]
    )
    assert detect_secrecy_mark(doc) == MAT


def test_non_mattran_page_bottom_mat_not_flagged() -> None:
    """Nửa dưới trang (sau xoay đúng chiều) không có dấu mật thật — vùng
    chữ ký/con dấu; "MAT" ở đây là mảnh mộc, không phải dấu độ mật."""
    doc = _multi_doc(
        [
            ("ỦY BAN NHÂN DÂN XÃ A", [40, 40, 280, 65]),
            (MAT, [450, 450, 500, 475]),
        ]
    )
    assert detect_secrecy_mark(doc) is None
