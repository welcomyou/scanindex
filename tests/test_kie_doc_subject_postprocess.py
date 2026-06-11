from __future__ import annotations

from scanindex.core.kie.postprocess import (
    apply_doc_subject_line_block_constraints,
    apply_issue_org_header_constraints,
    apply_single_line_block_constraints,
)


def _line(line_id: str, text: str, y0: float, y1: float, x0: float = 70.0, x1: float = 300.0) -> dict:
    word_id = f"{line_id}_w0"
    return {
        "id": line_id,
        "line_id": line_id,
        "text": text,
        "bbox": [x0, y0, x1, y1],
        "word_ids": [word_id],
    }


def _word(line_id: str, text: str, y0: float, y1: float, x0: float = 70.0, x1: float = 300.0) -> dict:
    return {
        "id": f"{line_id}_w0",
        "line_id": line_id,
        "text": text,
        "bbox": [x0, y0, x1, y1],
    }


def test_doc_subject_expansion_stops_at_doc_number_line():
    lines = [
        _line("p0_l4", "THANH PHO HO CHI MINH", 120.0, 134.0),
        _line("p0_l5", "CO QUAN THUONG TRUC", 136.0, 151.0),
        _line("p0_l6", "So 08 -CV/CQTTBCD", 169.0, 184.0),
        _line("p0_l7", "Ve ket qua ra soat, gop y va cung cap thong tin,", 185.0, 199.0),
        _line("p0_l8", "so lieu phuc vu theo doi, danh gia hieu qua viec", 201.0, 213.0),
        _line("p0_l9", "trien khai thuc hien Nghi quyet so 57-NQ/TW", 214.0, 226.0),
    ]
    words = [_word(line["line_id"], line["text"], line["bbox"][1], line["bbox"][3]) for line in lines]
    canonical = {
        "pages": [
            {
                "page_index": 0,
                "width": 600.0,
                "height": 800.0,
                "lines": lines,
                "words": words,
            }
        ]
    }
    annotation = {
        "field_instances": [
            {
                "field_id": "subject",
                "label": "DOC_SUBJECT",
                "page_index": 0,
                "text": "\n".join(line["text"] for line in lines[3:]),
                "line_ids": [line["line_id"] for line in lines[3:]],
                "word_ids": [f"{line['line_id']}_w0" for line in lines[3:]],
                "bbox": [70.0, 185.0, 300.0, 226.0],
            },
            {
                "field_id": "number",
                "label": "DOC_NUMBER_SYMBOL",
                "page_index": 0,
                "text": lines[2]["text"],
                "line_ids": [lines[2]["line_id"]],
                "word_ids": [f"{lines[2]['line_id']}_w0"],
                "bbox": lines[2]["bbox"],
            },
        ]
    }

    out = apply_doc_subject_line_block_constraints(canonical, annotation)
    subject = next(inst for inst in out["field_instances"] if inst["label"] == "DOC_SUBJECT")

    assert subject["line_ids"] == ["p0_l7", "p0_l8", "p0_l9"]
    assert "So 08" not in subject["text"]
    assert "CO QUAN THUONG TRUC" not in subject["text"]


def test_doc_subject_allows_referenced_dates_and_document_numbers():
    lines = [
        _line("p0_l5", "So 01-KH/BCD", 168.0, 182.0),
        _line("p0_l6", "KE HOACH", 200.0, 213.0),
        _line("p0_l9", "Trien khai thuc hien Thong bao ket luan so 07-TB/CQTTBCD ngay 15 thang 10", 217.0, 234.0),
        _line("p0_l10", "nam 2025 cua dong chi Tong Bi thu To Lam, Truong Ban Chi dao Trung uong", 235.0, 250.0),
        _line("p0_l11", "phat trien khoa hoc, cong nghe, doi moi sang tao va chuyen doi so tai Hoi nghi", 251.0, 266.0),
        _line("p0_l12", "so ket tinh hinh trien khai thuc hien Nghi quyet so 57-NQ/TW cua Bo Chinh tri", 267.0, 280.0),
        _line("p0_l13", "trong quy III/2025 va nhiem vu giai phap trong tam cuoi nam 2025", 281.0, 296.0),
        _line("p0_l14", "-----", 303.0, 307.0),
        _line("p0_l15", "Thuc hien chi dao cua dong chi Tong Bi thu To Lam", 323.0, 340.0),
    ]
    words = [_word(line["line_id"], line["text"], line["bbox"][1], line["bbox"][3]) for line in lines]
    canonical = {
        "pages": [
            {
                "page_index": 0,
                "width": 600.0,
                "height": 800.0,
                "lines": lines,
                "words": words,
            }
        ]
    }
    predicted_line_ids = ["p0_l6", "p0_l10", "p0_l12", "p0_l13"]
    predicted_lines = [line for line in lines if line["line_id"] in predicted_line_ids]
    annotation = {
        "field_instances": [
            {
                "field_id": "subject",
                "label": "DOC_SUBJECT",
                "page_index": 0,
                "text": "\n".join(line["text"] for line in predicted_lines),
                "line_ids": predicted_line_ids,
                "word_ids": [f"{line_id}_w0" for line_id in predicted_line_ids],
                "bbox": [70.0, 200.0, 300.0, 296.0],
            },
            {
                "field_id": "number",
                "label": "DOC_NUMBER_SYMBOL",
                "page_index": 0,
                "text": lines[0]["text"],
                "line_ids": [lines[0]["line_id"]],
                "word_ids": [f"{lines[0]['line_id']}_w0"],
                "bbox": lines[0]["bbox"],
            },
        ]
    }

    out = apply_doc_subject_line_block_constraints(canonical, annotation)
    subject = next(inst for inst in out["field_instances"] if inst["label"] == "DOC_SUBJECT")

    assert subject["line_ids"] == ["p0_l6", "p0_l9", "p0_l10", "p0_l11", "p0_l12", "p0_l13"]
    assert "ngay 15 thang 10" in subject["text"]
    assert "Nghi quyet so 57-NQ/TW" in subject["text"]
    assert "Thuc hien chi dao" not in subject["text"]


def test_doc_subject_keeps_lower_date_continuation_line():
    lines = [
        _line("p0_l3", "TP. Ho Chi Minh, ngay 25 thang 3 nam 2026", 76.0, 88.0),
        _line("p0_l4", "So 198-BC/TU", 95.0, 109.0),
        _line("p0_l5", "BAO CAO", 131.0, 146.0),
        _line("p0_l6", "Ket qua buoc dau ve viec lanh dao, chi dao, to chuc thuc hien va nhung kho khan,", 150.0, 166.0),
        _line("p0_l7", "vuong mac, kien nghi co lien quan sau van hanh chinh quyen dia phuong 02", 166.0, 182.0),
        _line("p0_l8", "cap; viec thuc hien cac nhiem vu, giai phap nham bao dam thuc hien muc tieu", 184.0, 199.0),
        _line("p0_l9", "tang truong 02 con so; viec thuc hien Nghi quyet so 57-NQ/TW,", 199.0, 216.0),
        _line("p0_l10", "ngay 22 thang 12 nam 2024 cua Bo Chinh tri", 220.0, 234.0),
        _line("p0_l11", "Thuc hien yeu cau cua Doan kiem tra, giam sat cua Bo Chinh tri", 275.0, 290.0),
    ]
    words = [_word(line["line_id"], line["text"], line["bbox"][1], line["bbox"][3]) for line in lines]
    canonical = {
        "pages": [
            {
                "page_index": 0,
                "width": 600.0,
                "height": 800.0,
                "lines": lines,
                "words": words,
            }
        ]
    }
    predicted_line_ids = ["p0_l5", "p0_l9"]
    predicted_lines = [line for line in lines if line["line_id"] in predicted_line_ids]
    annotation = {
        "field_instances": [
            {
                "field_id": "subject",
                "label": "DOC_SUBJECT",
                "page_index": 0,
                "text": "\n".join(line["text"] for line in predicted_lines),
                "line_ids": predicted_line_ids,
                "word_ids": [f"{line_id}_w0" for line_id in predicted_line_ids],
                "bbox": [70.0, 131.0, 300.0, 216.0],
            },
            {
                "field_id": "number",
                "label": "DOC_NUMBER_SYMBOL",
                "page_index": 0,
                "text": lines[1]["text"],
                "line_ids": [lines[1]["line_id"]],
                "word_ids": [f"{lines[1]['line_id']}_w0"],
                "bbox": lines[1]["bbox"],
            },
            {
                "field_id": "date",
                "label": "PLACE_DATE",
                "page_index": 0,
                "text": lines[0]["text"],
                "line_ids": [lines[0]["line_id"]],
                "word_ids": [f"{lines[0]['line_id']}_w0"],
                "bbox": lines[0]["bbox"],
            },
        ]
    }

    out = apply_doc_subject_line_block_constraints(canonical, annotation)
    subject = next(inst for inst in out["field_instances"] if inst["label"] == "DOC_SUBJECT")

    assert subject["line_ids"] == ["p0_l5", "p0_l6", "p0_l7", "p0_l8", "p0_l9", "p0_l10"]
    assert "ngay 22 thang 12 nam 2024 cua Bo Chinh tri" in subject["text"]
    assert "TP. Ho Chi Minh" not in subject["text"]
    assert "Thuc hien yeu cau" not in subject["text"]


def test_place_date_does_not_expand_to_full_merged_ocr_line():
    words = [
        {"id": "p0_l0_w0", "line_id": "p0_l0", "text": "CO", "bbox": [80.0, 70.0, 95.0, 84.0], "order": 0},
        {"id": "p0_l0_w1", "line_id": "p0_l0", "text": "QUAN", "bbox": [100.0, 70.0, 135.0, 84.0], "order": 1},
        {"id": "p0_l0_w2", "line_id": "p0_l0", "text": "TP.", "bbox": [330.0, 70.0, 350.0, 84.0], "order": 2},
        {"id": "p0_l0_w3", "line_id": "p0_l0", "text": "Ho", "bbox": [355.0, 70.0, 375.0, 84.0], "order": 3},
        {"id": "p0_l0_w4", "line_id": "p0_l0", "text": "Chi", "bbox": [380.0, 70.0, 405.0, 84.0], "order": 4},
        {"id": "p0_l0_w5", "line_id": "p0_l0", "text": "Minh,", "bbox": [410.0, 70.0, 450.0, 84.0], "order": 5},
        {"id": "p0_l0_w6", "line_id": "p0_l0", "text": "ngay", "bbox": [455.0, 70.0, 490.0, 84.0], "order": 6},
        {"id": "p0_l0_w7", "line_id": "p0_l0", "text": "15", "bbox": [495.0, 70.0, 510.0, 84.0], "order": 7},
        {"id": "p0_l0_w8", "line_id": "p0_l0", "text": "thang", "bbox": [515.0, 70.0, 555.0, 84.0], "order": 8},
        {"id": "p0_l0_w9", "line_id": "p0_l0", "text": "12", "bbox": [560.0, 70.0, 575.0, 84.0], "order": 9},
    ]
    canonical = {
        "pages": [
            {
                "page_index": 0,
                "width": 600.0,
                "height": 800.0,
                "lines": [
                    {
                        "id": "p0_l0",
                        "line_id": "p0_l0",
                        "text": "CO QUAN TP. Ho Chi Minh, ngay 15 thang 12",
                        "bbox": [80.0, 70.0, 575.0, 84.0],
                        "word_ids": [word["id"] for word in words],
                    }
                ],
                "words": words,
            }
        ]
    }
    date_word_ids = [word["id"] for word in words[2:]]
    annotation = {
        "field_instances": [
            {
                "field_id": "date",
                "label": "PLACE_DATE",
                "page_index": 0,
                "text": "TP. Ho Chi Minh, ngay 15 thang 12",
                "line_ids": ["p0_l0"],
                "word_ids": date_word_ids,
                "bbox": [330.0, 70.0, 575.0, 84.0],
            }
        ]
    }

    out = apply_single_line_block_constraints(canonical, annotation)
    place_date = next(inst for inst in out["field_instances"] if inst["label"] == "PLACE_DATE")

    assert place_date["text"] == "TP. Ho Chi Minh, ngay 15 thang 12"
    assert "CO QUAN" not in place_date["text"]


def test_doc_subject_can_expand_beyond_six_lines_until_layout_stop():
    lines = [
        _line("p0_l3", "THONG BAO", 135.0, 151.0),
        _line("p0_l4", "CUA CO QUAN BAN HANH", 155.0, 171.0),
        _line("p0_l5", "ve ket qua giam sat doi voi don vi", 173.0, 189.0),
        _line("p0_l6", "trong viec lanh dao chi dao va to chuc thuc hien", 193.0, 209.0),
        _line("p0_l7", "chuong trinh hanh dong so 65-CT/TU,", 213.0, 227.0),
        _line("p0_l8", "ngay 15 thang 3 nam 2025 ve tiep tuc doi moi,", 228.0, 245.0),
        _line("p0_l9", "sap xep to chuc bo may cua he thong", 247.0, 263.0),
        _line("p0_l10", "chinh tri tinh gon, hoat dong hieu luc, hieu qua;", 266.0, 282.0),
        _line("p0_l11", "chuong trinh hanh dong so 63-CT/TU, ngay 27 thang 02 nam 2025", 284.0, 300.0),
        _line("p0_l12", "ve dot pha phat trien khoa hoc, cong nghe", 302.0, 318.0),
        _line("p0_l13", "-----", 330.0, 334.0),
        _line("p0_l14", "Xet bao cao cua doan giam sat", 355.0, 371.0),
    ]
    words = [_word(line["line_id"], line["text"], line["bbox"][1], line["bbox"][3]) for line in lines]
    canonical = {
        "pages": [
            {
                "page_index": 0,
                "width": 600.0,
                "height": 800.0,
                "lines": lines,
                "words": words,
            }
        ]
    }
    predicted_line_ids = ["p0_l3", "p0_l4", "p0_l5", "p0_l6", "p0_l7"]
    annotation = {
        "field_instances": [
            {
                "field_id": "subject",
                "label": "DOC_SUBJECT",
                "page_index": 0,
                "text": "\n".join(line["text"] for line in lines[:5]),
                "line_ids": predicted_line_ids,
                "word_ids": [f"{line_id}_w0" for line_id in predicted_line_ids],
                "bbox": [70.0, 135.0, 300.0, 227.0],
            }
        ]
    }

    out = apply_doc_subject_line_block_constraints(canonical, annotation)
    subject = next(inst for inst in out["field_instances"] if inst["label"] == "DOC_SUBJECT")

    assert subject["line_ids"] == [line["line_id"] for line in lines[:10]]
    assert "ve dot pha phat trien khoa hoc" in subject["text"]
    assert "Xet bao cao" not in subject["text"]


def test_doc_subject_skips_urgency_stamp_inside_subject_block():
    lines = [
        _line("p0_l6", "Ve tham muu bao cao danh gia ket qua thuc hien", 122.0, 135.0),
        _line("p0_l7", "Nghi quyet so 57-NQ/TW cua Bo Chinh tri", 136.0, 149.0),
        _line("p0_l8", "KHAN", 146.0, 164.0, 20.0, 62.0),
        _line("p0_l9", "phuc vu Doan kiem tra, giam sat cua", 150.0, 163.0),
        _line("p0_l10", "Bo Chinh tri, Ban Bi thu nam 2026 (dot 2)", 164.0, 177.0),
        _line("p0_l11", "Kinh gui: Co quan lien quan", 201.0, 216.0),
    ]
    words = [_word(line["line_id"], line["text"], line["bbox"][1], line["bbox"][3], line["bbox"][0], line["bbox"][2]) for line in lines]
    canonical = {
        "pages": [
            {
                "page_index": 0,
                "width": 600.0,
                "height": 800.0,
                "lines": lines,
                "words": words,
            }
        ]
    }
    predicted_line_ids = ["p0_l6", "p0_l7", "p0_l8", "p0_l9", "p0_l10"]
    annotation = {
        "field_instances": [
            {
                "field_id": "subject",
                "label": "DOC_SUBJECT",
                "page_index": 0,
                "text": "\n".join(line["text"] for line in lines[:5]),
                "line_ids": predicted_line_ids,
                "word_ids": [f"{line_id}_w0" for line_id in predicted_line_ids],
                "bbox": [20.0, 122.0, 300.0, 177.0],
            }
        ]
    }

    out = apply_doc_subject_line_block_constraints(canonical, annotation)
    subject = next(inst for inst in out["field_instances"] if inst["label"] == "DOC_SUBJECT")

    assert subject["line_ids"] == ["p0_l6", "p0_l7", "p0_l9", "p0_l10"]
    assert "KHAN" not in subject["text"]
    assert "phuc vu Doan kiem tra" in subject["text"]
    assert "Kinh gui" not in subject["text"]


def test_issue_org_repair_does_not_truncate_predicted_name_block():
    lines = [
        _line("p0_l0", "BAN CHI DAO TRUNG UONG VE", 58.0, 72.0, 80.0, 320.0),
        _line("p0_l1", "PHAT TRIEN KHOA HOC, CONG NGHE,", 74.0, 90.0, 80.0, 320.0),
        _line("p0_l2", "DOI MOI SANG TAO VA CHUYEN DOI SO", 91.0, 106.0, 80.0, 325.0),
        _line("p0_l3", "DON VI THUONG TRUC", 107.0, 122.0, 120.0, 285.0),
        _line("p0_l4", "DANG CONG SAN VIET NAM", 58.0, 75.0, 350.0, 555.0),
        _line("p0_l5", "Ha Noi, ngay 29 thang 12 nam 2025", 88.0, 102.0, 350.0, 555.0),
        _line("p0_l6", "So 17-TB/CQTTBCD", 139.0, 154.0, 140.0, 260.0),
    ]
    words = [
        _word(line["line_id"], line["text"], line["bbox"][1], line["bbox"][3], line["bbox"][0], line["bbox"][2])
        for line in lines
    ]
    canonical = {
        "pages": [
            {
                "page_index": 0,
                "width": 600.0,
                "height": 800.0,
                "lines": lines,
                "words": words,
            }
        ]
    }
    issue_line_ids = ["p0_l0", "p0_l1", "p0_l2", "p0_l3"]
    annotation = {
        "field_instances": [
            {
                "field_id": "issue_org_name",
                "label": "ISSUE_ORG_NAME",
                "page_index": 0,
                "text": "\n".join(line["text"] for line in lines[:4]),
                "line_ids": issue_line_ids,
                "word_ids": [f"{line_id}_w0" for line_id in issue_line_ids],
                "bbox": [80.0, 58.0, 325.0, 122.0],
            },
            {
                "field_id": "regime",
                "label": "REGIME_HEADER",
                "page_index": 0,
                "text": lines[4]["text"],
                "line_ids": ["p0_l4"],
                "word_ids": ["p0_l4_w0"],
                "bbox": lines[4]["bbox"],
            },
            {
                "field_id": "date",
                "label": "PLACE_DATE",
                "page_index": 0,
                "text": lines[5]["text"],
                "line_ids": ["p0_l5"],
                "word_ids": ["p0_l5_w0"],
                "bbox": lines[5]["bbox"],
            },
            {
                "field_id": "number",
                "label": "DOC_NUMBER_SYMBOL",
                "page_index": 0,
                "text": lines[6]["text"],
                "line_ids": ["p0_l6"],
                "word_ids": ["p0_l6_w0"],
                "bbox": lines[6]["bbox"],
            },
        ]
    }

    out = apply_issue_org_header_constraints(canonical, annotation)
    name = next(inst for inst in out["field_instances"] if inst["label"] == "ISSUE_ORG_NAME")

    assert name["line_ids"] == issue_line_ids
    assert "DON VI THUONG TRUC" in name["text"]
    assert not any(inst["label"] == "ISSUE_ORG_SUPERIOR" for inst in out["field_instances"])
