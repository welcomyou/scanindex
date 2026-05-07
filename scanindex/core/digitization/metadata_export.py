"""Output writers for the archive (Số hóa lưu trữ) workflow.

Two artefacts per output folder:
  1. Per-file canonical JSON enriched with KIE annotations (named
     `<stem>_ocr.pdf.json`, written next to its `_ocr.pdf` if present).
  2. A single Excel workbook `MetaDuLieu.xlsx` aggregating every file's
     extracted metadata in the schema of the official "Văn bản" sheet.

The Excel column order matches the standard government archive metadata
schema; columns we cannot fill from KIE are left blank.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Iterable

# Column order matches MetaDuLieu.xlsx "Văn bản" sheet (20 columns)
EXCEL_COLUMNS = [
    "Tên cơ quan, tổ chức ban hành văn bản",
    "Tên loại văn bản",
    # NBSP between "văn" and "bản" — verbatim from the official
    # HSLTCQ template; importers that match the exact header string
    # rely on this byte-for-byte equality.
    "Số của văn\xa0bản",
    "Ký hiệu của văn bản",
    "Ngày, tháng, năm văn bản",
    "Trích yếu nội dung",
    "Loại bản",
    "Ngôn ngữ",
    "Ghi chú",
    "Bút tích",
    "Người ký",
    "Chuyên đề",
    "Ký hiệu thông tin",
    "Từ khóa",
    "Chế độ sử dụng",
    "Độ mật",
    "Mức độ tin cậy",
    "Tình trạng vật lý",
    "Tên tệp",
    "Thời gian tài liệu",
]

# "Hồ sơ" sheet (13 cols, one row for the dossier itself).
HOSO_COLUMNS = [
    "Tiêu đề hồ sơ",
    "Thời hạn bảo quản",
    "Phông",
    "Mục lục",
    "Nhiệm kỳ",
    "Thời gian bắt đầu",
    "Thời gian kết thúc",
    "Tình trạng vật lý",
    "Số lượng trang",
    "Tổng số văn bản trong hồ sơ",
    "Độ mật",
    "Đơn vị bảo quản số",
    "Thời gian tài liệu",
]

# "Ảnh" / "Video" sheets are part of the official HSLTCQ workbook but the
# OCR pipeline never produces image- or video-track metadata, so they're
# emitted as header-only sheets to match the reference layout.
ANH_COLUMNS = [
    "Số lưu trữ",
    "Ký hiệu thông tin",
    "Tên sự kiện",
    "Tiêu đề phim/ảnh",
    "Ghi chú",
    "Tác giả",
    "Địa điểm chụp",
    "Thời gian chụp",
    "Màu sắc",
    "Cỡ phim/ảnh",
    "Tài liệu đi kèm",
    "Chế độ sử dụng",
    "Tình trạng vật lý",
    "Tên tệp",
]
VIDEO_COLUMNS = [
    "Số lưu trữ",
    "Ký hiệu thông tin",
    "Tên sự kiện",
    "Tiêu đề\xa0phim/âm thanh",
    "Tác giả",
    "Địa điểm",
    "Thời gian",
    "Ngôn ngữ",
    "Thời lượng",
    "Tài liệu đi kèm",
    "Chế độ sử dụng",
    "Chất lượng",
    "Tình trạng vật lý",
    "Ghi chú",
    "Tên tệp",
]

# Secrecy ordering (low → high) so we can pick the dossier-level max
# from the per-doc marks emitted by KIE.
_SECRECY_RANK = {"": 0, "Mật": 1, "Tối mật": 2, "Tuyệt mật": 3}

# KIE label → Excel column mapping
LABEL_TO_COLUMN = {
    "ISSUE_ORG_NAME": "Tên cơ quan, tổ chức ban hành văn bản",
    "DOC_TYPE": "Tên loại văn bản",
    "DOC_SUBJECT": "Trích yếu nội dung",
    "SIGNER_NAME": "Người ký",
    "SECRECY_MARK": "Độ mật",
    "CIRCULATION_MARK": "Chế độ sử dụng",
}

_DATE_NUMERIC_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[/. -]\s*(\d{1,2})\s*[/. -]\s*(\d{2,4})(?!\d)")
_DATE_TEXT_RE = re.compile(r"\bngay\s*(\d{1,2})\s*thang\s*(\d{1,2})\s*(?:nam)?\s*(\d{2,4})(?!\d)", re.IGNORECASE)


def _single_line_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _fold_vn_ascii(text: str | None) -> str:
    text = (text or "").replace("Đ", "D").replace("đ", "d")
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped.lower()).strip()


def _first_field(annotation: dict, label: str) -> dict | None:
    for f in annotation.get("field_instances", []) or []:
        if f.get("label") == label:
            return f
    return None


def _parse_doc_number(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    try:
        from scanindex.core.kie.ontology import split_doc_number_symbol_text
        return split_doc_number_symbol_text(text)
    except Exception:
        return None, None


def _parse_date_from_place_date(text: str | None) -> str | None:
    if not text:
        return None
    folded = _fold_vn_ascii(text)
    m = _DATE_TEXT_RE.search(folded) or _DATE_NUMERIC_RE.search(folded)
    if not m:
        return None
    d, mth, y = m.group(1), m.group(2), m.group(3)
    if len(y) == 2:
        y = "20" + y if int(y) < 50 else "19" + y
    return f"{int(d):02d}/{int(mth):02d}/{int(y):04d}"


def annotation_to_row(annotation: dict, file_name: str) -> dict:
    """Build one Excel row dict from a KIE annotation block.

    The 14 raw KIE labels are projected onto the 20 HSLTCQ columns:

    +-------------------------+--------------------------------------------+
    | KIE label               | HSLTCQ column                              |
    +-------------------------+--------------------------------------------+
    | ISSUE_ORG_NAME          | "Tên cơ quan, tổ chức ban hành văn bản"   |
    | (+ ISSUE_ORG_SUPERIOR)  |   (concatenated as "<name> <superior>")   |
    | DOC_TYPE                | "Tên loại văn bản"                         |
    | DOC_NUMBER_SYMBOL       | split → "Số" + "Ký hiệu của văn bản"      |
    | PLACE_DATE              | "Ngày, tháng, năm văn bản" (date parsed)  |
    | DOC_SUBJECT             | "Trích yếu nội dung"                       |
    | SIGNER_NAME             | "Người ký"                                 |
    | SECRECY_MARK            | "Độ mật"                                   |
    | CIRCULATION_MARK        | "Chế độ sử dụng"                           |
    +-------------------------+--------------------------------------------+

    Labels with no HSLTCQ counterpart (REGIME_HEADER, ADDRESSEE, RECIPIENTS,
    SIGNER_ROLE, URGENCY_MARK) are deliberately dropped — those concepts
    live outside the 20-column government archive schema.
    """
    row = {col: "" for col in EXCEL_COLUMNS}
    row["Tên tệp"] = file_name
    row["Loại bản"] = "Bản chính"     # default assumption
    row["Ngôn ngữ"] = "Tiếng Việt"    # default assumption
    # Default access mode — the receiving HSLTCQ importer rejects rows
    # where "Chế độ sử dụng" is null ("Cannot convert null object").
    # CIRCULATION_MARK from KIE will override below if present.
    row["Chế độ sử dụng"] = "Công khai"

    if not annotation:
        return row

    for label, col in LABEL_TO_COLUMN.items():
        f = _first_field(annotation, label)
        if f and f.get("text"):
            row[col] = f["text"].strip()

    # ISSUE_ORG_SUPERIOR sits above ISSUE_ORG_NAME in the document
    # header. The HSLTCQ column stores one readable value: issuer first,
    # then superior, separated by a normal space.
    sup = _first_field(annotation, "ISSUE_ORG_SUPERIOR")
    name = _first_field(annotation, "ISSUE_ORG_NAME")
    sup_text = _single_line_text((sup or {}).get("text", ""))
    name_text = _single_line_text((name or {}).get("text", ""))
    if name_text and sup_text:
        row["Tên cơ quan, tổ chức ban hành văn bản"] = _single_line_text(
            f"{name_text} {sup_text}"
        )
    elif sup_text and not name_text:
        row["Tên cơ quan, tổ chức ban hành văn bản"] = sup_text
    # (name-only case already handled by the LABEL_TO_COLUMN loop above.)

    # DOC_NUMBER_SYMBOL → split into number + symbol columns
    doc_num_field = _first_field(annotation, "DOC_NUMBER_SYMBOL")
    if doc_num_field and doc_num_field.get("text"):
        number, symbol = _parse_doc_number(doc_num_field["text"])
        if number:
            row["Số của văn\xa0bản"] = number
        if symbol:
            row["Ký hiệu của văn bản"] = symbol

    # PLACE_DATE → date only into "Ngày, tháng, năm văn bản"
    pd_field = _first_field(annotation, "PLACE_DATE")
    if pd_field and pd_field.get("text"):
        date = _parse_date_from_place_date(pd_field["text"])
        if date:
            row["Ngày, tháng, năm văn bản"] = date

    return row


def write_enriched_canonical_json(canonical_json_path: str, annotation: dict,
                                    output_path: str | None = None) -> str:
    """Inject `annotation` into the canonical JSON's `annotations` block and
    write back. Returns the path written."""
    with open(canonical_json_path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    doc["annotations"] = {
        "schema": annotation.get("schema", "kie_vi_official_v3"),
        "source": annotation.get("source", "unknown"),
        "status": "predicted",
        "selected_pages": annotation.get("selected_pages", []),
        "field_instances": annotation.get("field_instances", []),
        "relations": annotation.get("relations", []),
    }
    if annotation.get("postprocess"):
        doc["annotations"]["postprocess"] = annotation.get("postprocess")
    out = output_path or canonical_json_path
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(tmp, out)
    return out


def _parse_ddmmyyyy(s: str | None) -> tuple[int, int, int] | None:
    if not s:
        return None
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", s)
    if not m:
        return None
    return (int(m.group(3)), int(m.group(2)), int(m.group(1)))  # y, m, d


def build_hoso_row(identity, vanban_rows: list[dict],
                    documents: list[dict] | None = None) -> dict:
    """Build the single-row "Hồ sơ" sheet content from the dossier identity
    plus the already-projected per-document rows.

    `identity`: an `IdentityCodes` (duck-typed — anything with `.title /
    ma_phong / muc_luc / ho_so / is_unstructured` works) or None.
    `vanban_rows`: rows produced by `annotation_to_row` — used to derive
    Thời gian bắt đầu/kết thúc and the dossier-level Độ mật.
    `documents`: optional GUI doc dicts (used for total page count).
    """
    row = {col: "" for col in HOSO_COLUMNS}

    if identity is not None:
        title = (getattr(identity, "title", "") or "").strip()
        ma_phong = (
            getattr(identity, "ma_phong", "")
            or getattr(identity, "fonds", "")
            or ""
        ).strip()
        muc_luc = (
            getattr(identity, "muc_luc", "")
            or getattr(identity, "catalog", "")
            or ""
        ).strip()
        ten_phong = (
            getattr(identity, "ten_phong", "")
            or getattr(identity, "fonds_name", "")
            or ""
        ).strip()
        ten_muc_luc = (
            getattr(identity, "ten_muc_luc", "")
            or getattr(identity, "catalog_name", "")
            or ""
        ).strip()
        ho_so = (
            getattr(identity, "ho_so", "")
            or getattr(identity, "dossier_code", "")
            or ""
        ).strip()
        # Tiêu đề hồ sơ: prefer free-text title; fall back to coded composite
        # so the row is never blank when codes are present.
        if title:
            row["Tiêu đề hồ sơ"] = title
        elif ma_phong or muc_luc or ho_so:
            row["Tiêu đề hồ sơ"] = f"{ma_phong}/{muc_luc}/{ho_so}"
        row["Phông"] = ten_phong or ma_phong
        row["Mục lục"] = ten_muc_luc or muc_luc
        row["Đơn vị bảo quản số"] = ho_so
        # Three optional fields collected by DossierInfoDialog.
        row["Thời hạn bảo quản"] = (
            getattr(identity, "thoi_han_bao_quan", "") or ""
        ).strip()
        row["Tình trạng vật lý"] = (
            getattr(identity, "tinh_trang_vat_ly", "") or ""
        ).strip()
        row["Nhiệm kỳ"] = (getattr(identity, "nhiem_ky", "") or "").strip()[:10]

    # Thời gian bắt đầu/kết thúc: min/max of parsed PLACE_DATE.
    parsed = []
    for r in vanban_rows or []:
        d = _parse_ddmmyyyy(r.get("Ngày, tháng, năm văn bản"))
        if d is not None:
            parsed.append(d)
    if parsed:
        parsed.sort()
        y0, m0, d0 = parsed[0]
        y1, m1, d1 = parsed[-1]
        row["Thời gian bắt đầu"] = f"{d0:02d}/{m0:02d}/{y0:04d}"
        row["Thời gian kết thúc"] = f"{d1:02d}/{m1:02d}/{y1:04d}"
        # "Thời gian tài liệu" is left empty here — the reference
        # workbook keeps it null and the receiving system reads the
        # range from "Thời gian bắt đầu" / "Thời gian kết thúc"
        # instead. Synthesising a "start - end" string here was
        # rejected by the importer ("Cannot convert null object")
        # because it expected a single date, not a range.

    # Tổng số văn bản trong hồ sơ — stored as STRING (the reference
    # workbook keeps numeric counters as strings so the downstream
    # importer's string-typed reader doesn't choke).
    row["Tổng số văn bản trong hồ sơ"] = str(len(vanban_rows or []))

    # Độ mật: max secrecy across docs (highest wins).
    max_rank = 0
    max_label = ""
    for r in vanban_rows or []:
        s = (r.get("Độ mật") or "").strip()
        rank = _SECRECY_RANK.get(s, 0)
        if rank > max_rank:
            max_rank = rank
            max_label = s
    row["Độ mật"] = max_label

    # Số lượng trang: sum across source PDFs (cheap to count via pypdf).
    if documents:
        try:
            from pypdf import PdfReader
            total_pages = 0
            for d in documents:
                p = d.get("pdf_path") or d.get("path") or ""
                if p and os.path.isfile(p):
                    try:
                        total_pages += len(PdfReader(p).pages)
                    except Exception:
                        pass
            if total_pages:
                # String type matches the reference workbook (and the
                # HSLTCQ importer reads it as text).
                row["Số lượng trang"] = str(total_pages)
        except Exception:
            pass

    return row


def _resolve_template_path() -> str:
    """Locate the official HSLTCQ MetaDuLieu template shipped with the
    app. Works both from source (`<repo>/assets/...`) and from the
    PyInstaller-frozen exe, which bundles the same `assets/` tree."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        from scanindex.infra.paths import get_base_dir
        base = get_base_dir()
    except Exception:
        base = str(Path(__file__).resolve().parents[3])
    candidates = [
        os.path.join(base, "assets", "MetaDuLieu_template.xlsx"),
        os.path.join(getattr(sys, "_MEIPASS", here), "assets", "MetaDuLieu_template.xlsx"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "MetaDuLieu_template.xlsx not found in assets/. "
        "The official HSLTCQ template must ship with the app."
    )


def _column_index_by_header(ws, expected_columns: list[str]) -> dict[str, int]:
    """Read row 1 of `ws` and return ``{column_name: 1-based-col-index}``.
    Header strings are matched after stripping NBSP and whitespace, so
    minor template revisions (e.g. someone re-typing "Số của văn bản"
    without the NBSP) still resolve correctly."""
    def _norm(text):
        if text is None:
            return ""
        return " ".join(str(text).replace("\xa0", " ").split())
    expected_norm = {_norm(c): c for c in expected_columns}
    out: dict[str, int] = {}
    for c in range(1, ws.max_column + 1):
        key = _norm(ws.cell(1, c).value)
        if key in expected_norm:
            out[expected_norm[key]] = c
    return out


def write_excel(rows: Iterable[dict], output_xlsx_path: str,
                 hoso_row: dict | None = None) -> str:
    """Write the HSLTCQ workbook to `output_xlsx_path` by **filling the
    official template** (`assets/MetaDuLieu_template.xlsx`) with
    extracted metadata.

    Using the template instead of generating a new workbook from scratch
    guarantees byte-for-byte structural fidelity (sheet names, header
    text including NBSP, row heights, default styles, empty Ảnh/Video
    sheets) — exactly what the reference HSLTCQ workbook prescribes.

    The template ships with one demo data row in each of "Hồ sơ" /
    "Văn bản"; we clear those before writing the operator's data.
    """
    import re as _re
    import zipfile
    import xml.etree.ElementTree as ET

    rows = list(rows)
    template_path = _resolve_template_path()

    os.makedirs(os.path.dirname(output_xlsx_path) or ".", exist_ok=True)

    def _xlsx_text_or_blank(value) -> str | None:
        """Write populated cells as text; leave empty fields as true blanks.

        The reference HSLTCQ workbook stores dates and counters as text, but
        its empty fields are absent cells. Empty inline-string cells are what
        some importers surface as null objects.
        """
        if value is None:
            return None
        if isinstance(value, str):
            return value if value != "" else None
        return str(value)

    def _col_letter(index: int) -> str:
        out = ""
        while index:
            index, rem = divmod(index - 1, 26)
            out = chr(65 + rem) + out
        return out

    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    mc_ns = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    x14ac_ns = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"
    xml_ns = "http://www.w3.org/XML/1998/namespace"
    ET.register_namespace("", main_ns)
    ET.register_namespace("r", rel_ns)
    ET.register_namespace("mc", mc_ns)
    ET.register_namespace("x14ac", x14ac_ns)

    def q(name: str) -> str:
        return f"{{{main_ns}}}{name}"

    with zipfile.ZipFile(template_path, "r") as zin:
        entries = [(info, zin.read(info.filename)) for info in zin.infolist()]
    files = {info.filename: data for info, data in entries}

    sst_root = ET.fromstring(files["xl/sharedStrings.xml"])
    shared_index: dict[str, int] = {}
    for idx, si in enumerate(sst_root.findall(q("si"))):
        text = "".join(t.text or "" for t in si.iter(q("t")))
        shared_index.setdefault(text, idx)

    def _shared_string_index(value: str) -> int:
        idx = shared_index.get(value)
        if idx is not None:
            return idx
        si = ET.SubElement(sst_root, q("si"))
        t = ET.SubElement(si, q("t"))
        if value != value.strip():
            t.set(f"{{{xml_ns}}}space", "preserve")
        t.text = value
        idx = len(sst_root.findall(q("si"))) - 1
        shared_index[value] = idx
        return idx

    def _string_cell(coord: str, value: str):
        c = ET.Element(q("c"), {"r": coord, "t": "s"})
        v = ET.SubElement(c, q("v"))
        v.text = str(_shared_string_index(value))
        return c

    def _xml_bytes(root) -> bytes:
        body = ET.tostring(root, encoding="utf-8", short_empty_elements=True)
        return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n' + body

    def _rewrite_sheet(xml_bytes: bytes, columns: list[str],
                       data_rows: list[dict]) -> bytes:
        root = ET.fromstring(xml_bytes)
        dimension = root.find(q("dimension"))
        sheet_data = root.find(q("sheetData"))
        if sheet_data is None:
            raise ValueError("Invalid HSLTCQ template: missing sheetData")

        for row_el in list(sheet_data.findall(q("row"))):
            try:
                row_idx = int(row_el.get("r", "0"))
            except ValueError:
                row_idx = 0
            if row_idx >= 2:
                sheet_data.remove(row_el)

        max_col = len(columns)
        max_row = max(1, len(data_rows) + 1)
        if dimension is not None:
            dimension.set("ref", f"A1:{_col_letter(max_col)}{max_row}")

        for row_offset, row in enumerate(data_rows, start=2):
            row_el = ET.Element(
                q("row"),
                {"r": str(row_offset), "spans": f"1:{max_col}"},
            )
            row_el.set(f"{{{x14ac_ns}}}dyDescent", "0.3")
            for col_idx, col_name in enumerate(columns, start=1):
                value = _xlsx_text_or_blank(row.get(col_name, ""))
                if value is None:
                    continue
                coord = f"{_col_letter(col_idx)}{row_offset}"
                row_el.append(_string_cell(coord, value))
            sheet_data.append(row_el)

        return _xml_bytes(root)

    hoso_rows = [hoso_row] if hoso_row else []
    files["xl/worksheets/sheet1.xml"] = _rewrite_sheet(
        files["xl/worksheets/sheet1.xml"],
        HOSO_COLUMNS,
        hoso_rows,
    )
    files["xl/worksheets/sheet2.xml"] = _rewrite_sheet(
        files["xl/worksheets/sheet2.xml"],
        EXCEL_COLUMNS,
        rows,
    )

    shared_ref_count = 0
    for name, data in files.items():
        if not _re.match(r"xl/worksheets/sheet\d+\.xml$", name):
            continue
        root = ET.fromstring(data)
        shared_ref_count += sum(
            1 for cell in root.iter(q("c")) if cell.get("t") == "s"
        )
    sst_root.set("count", str(shared_ref_count))
    sst_root.set("uniqueCount", str(len(sst_root.findall(q("si")))))
    files["xl/sharedStrings.xml"] = _xml_bytes(sst_root)

    # Ảnh / Video are left untouched; the template already ships them as
    # empty sheets, which is what the official HSLTCQ workbook uses.
    with zipfile.ZipFile(output_xlsx_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, original_data in entries:
            data = files.get(info.filename, original_data)
            new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new_info.comment = info.comment
            new_info.extra = info.extra
            new_info.internal_attr = info.internal_attr
            new_info.external_attr = info.external_attr
            new_info.create_system = info.create_system
            new_info.compress_type = info.compress_type
            zout.writestr(new_info, data)
    return output_xlsx_path
