"""Quét thư mục nhập Số hóa lưu trữ — nhận diện cấu trúc chuẩn CSDL_SOHOA.

Bộ quét ĐƯỜNG DẪN (không OCR, không mở PDF) cho chức năng "Nhập thư mục"
tại Bước 2 Số hóa lưu trữ (kế hoạch nhập thư mục nhiều hồ sơ). Cấu trúc
chuẩn — dùng lại parser/validator của ``scanindex.core.rename_tree``:

    [CSDL_SOHOA/]                        ← lớp bao ngoài KHÔNG bắt buộc;
    └── <Mã định danh>/                     gốc logic xác định theo NỘI DUNG cây
        └── <Mã phông>/
            └── <Mã mục lục>/
                └── <MãĐD>-<MãPhông>-<ML>-<Hồ sơ>/
                    └── <MãĐD>-<MãPhông>-<ML>-<HS>-<STT>.pdf

Quy tắc (mục 3 của kế hoạch):
  * Hỗ trợ gốc chứa TRỰC TIẾP các thư mục mã định danh, hoặc gốc có MỘT lớp
    bao ngoài (ví dụ ``CSDL_SOHOA``). Không tạo/di chuyển gì để chuẩn hóa.
  * Đối chiếu ĐỦ mã: tên hồ sơ phải khớp chuỗi cha; PDF phải khớp mã hồ sơ.
    Đúng số đoạn nhưng sai mã cha vẫn là LỘI.
  * Giữ số 0 đầu; nhận cả ``.pdf`` lẫn ``.PDF``; nhận hậu tố tùy chọn sau
    STT (``PdfName.extra`` — giữ tương thích công cụ đổi tên cây).
  * Độ rộng mã: mục lục đúng 2 chữ số; hồ sơ 4–6 chữ số (quyết định của
    người dùng — nới hơn chuẩn 4 chữ số của công cụ đổi tên; hồ sơ 5–6 số
    vẫn chọn/OCR/xuất được, chỉ không đổi tên được bằng quy tắc 4 số).
  * Mỗi lỗi báo kèm ĐƯỜNG DẪN và LÝ DO; không gộp thành một c-flag. Hồ sơ
    còn lỗi cấu trúc bên trong không được xác nhận xử lý một phần âm thầm.
  * File không phải PDF không vào danh sách OCR (bỏ qua im lặng).

Module chỉ dùng stdlib để chạy được cả trong thread nền lẫn test headless.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from scanindex.core import rename_tree as rt

# Độ rộng mã hồ sơ chấp nhận khi QUÉT: 4 (chuẩn công cụ đổi tên) đến 6
# (nới theo quyết định người dùng — hộp thoại thông tin hồ sơ cũng cho 6).
DOSSIER_CODE_MIN_DIGITS = 4
DOSSIER_CODE_MAX_DIGITS = 6
# STT PDF: chỉ chấp nhận chữ số (không ép đúng 3 số như khi đánh số mới —
# giữ tương thích dữ liệu cũ có số 0 đầu khác độ dài).
_MAX_STT_DIGITS = 6


@dataclass(frozen=True)
class ScanIssue:
    """Một lỗi/cảnh báo cụ thể: đường dẫn tương đối + lý do."""

    rel: str
    reason: str

    def __str__(self) -> str:  # tiện hiển thị trong UI/log
        return f"{self.rel}: {self.reason}"


@dataclass(frozen=True)
class DocumentScan:
    """Một PDF thuộc hồ sơ (đã parse + đối chiếu mã thành công)."""

    rel: str            # rel posix từ gốc người dùng chọn
    name: str           # tên file gốc (giữ nguyên .pdf/.PDF)
    stt: str            # số thứ tự trong tên (chuỗi gốc, giữ số 0 đầu)
    title_hint: str = ""  # hậu tố tùy chọn sau STT (PdfName.extra)


@dataclass
class DossierScan:
    """Một thư mục hồ sơ nhận diện được — kèm đầy đủ lỗi/warning của nhánh."""

    rel: str                    # rel posix của thư mục hồ sơ
    name: str                   # tên thư mục (4 đoạn)
    codes: rt.DossierName
    documents: list[DocumentScan] = field(default_factory=list)
    errors: list[ScanIssue] = field(default_factory=list)
    warnings: list[ScanIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Hồ sơ hợp lệ để đưa vào OCR (không còn lỗi cấu trúc bên trong)."""
        return not self.errors


@dataclass
class FolderScanResult:
    """Kết quả quét toàn cây: hồ sơ, PDF, gốc logic, lỗi và cảnh báo."""

    root: Path                              # gốc người dùng chọn
    logical_root: Path | None = None        # gốc chứa các thư mục mã định danh
    wrapper: str = ""                       # tên lớp bao ngoài ("" nếu không có)
    dossiers: list[DossierScan] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)   # lỗi ngoài hồ sơ
    warnings: list[ScanIssue] = field(default_factory=list)
    pdf_count: int = 0                      # tổng số PDF thấy được trong cây

    @property
    def valid_dossiers(self) -> list[DossierScan]:
        return [d for d in self.dossiers if d.ok]

    @property
    def has_pdfs(self) -> bool:
        return self.pdf_count > 0


def _check_code_widths(codes: rt.DossierName, rel: str) -> ScanIssue | None:
    """Mã mục lục đúng 2 chữ số; mã hồ sơ 4–6 chữ số (số 0 đầu giữ nguyên)."""
    if not (len(codes.muc_luc) == 2 and codes.muc_luc.isdigit()):
        return ScanIssue(
            rel,
            f"Mã mục lục \"{codes.muc_luc}\" phải là đúng 2 chữ số.",
        )
    if not (codes.ho_so.isdigit()
            and DOSSIER_CODE_MIN_DIGITS <= len(codes.ho_so)
            <= DOSSIER_CODE_MAX_DIGITS):
        return ScanIssue(
            rel,
            f"Mã hồ sơ \"{codes.ho_so}\" phải là {DOSSIER_CODE_MIN_DIGITS}–"
            f"{DOSSIER_CODE_MAX_DIGITS} chữ số.",
        )
    return None


def _codes_match_chain(rel_parts: tuple[str, ...], codes: rt.DossierName,
                       wrapper: str) -> ScanIssue | None:
    """Đối chiếu mã trong tên hồ sơ với tên các thư mục cha (mục 3.1).

    Chỉ nêu ĐÚNG chỗ khác nhau — phần khớp không nhắc lại cho đỡ nhiễu."""
    chain = list(rel_parts[:-1])
    if wrapper:
        chain = chain[1:]  # bỏ lớp bao ngoài (CSDL_SOHOA…)
    if len(chain) != 3:
        return ScanIssue(rel_parts[-1], "Cấu trúc cấp cha không đủ 3 cấp.")
    mdd, phong, ml = chain
    diffs: list[str] = []
    if mdd != codes.ma_dinh_danh:
        diffs.append(
            f"mã định danh ({codes.ma_dinh_danh} ≠ {mdd})")
    if phong != codes.ma_phong:
        diffs.append(f"mã phông ({codes.ma_phong} ≠ {phong})")
    if ml != codes.muc_luc:
        diffs.append(f"mã mục lục ({codes.muc_luc} ≠ {ml})")
    if not diffs:
        return None
    return ScanIssue(
        "/".join(rel_parts),
        "Tên hồ sơ lệch mã cha — " + "; ".join(diffs) + ".",
    )


def _choose_wrapper_depth(root: Path, candidates: dict[str, int]) -> tuple[int, str]:
    """Chọn lớp bao ngoài (0 hoặc 1 cấp) theo NỘI DUNG cây.

    ``candidates``: rel posix của mọi thư mục parse được tên 4 đoạn → độ sâu.
    Trả về (độ sâu hồ sơ tính từ root, tên lớp bao ngoài hoặc "").

    Gốc chứa trực tiếp mã định danh → hồ sơ ở độ sâu 4; gốc có một lớp bao
    ngoài (CSDL_SOHOA…) → độ sâu 5. Chọn độ sâu có nhiều hồ sơ parse được
    nhất (hòa → ưu tiên nông). Các thư mục 4 đoạn ở độ sâu khác bị coi là
    lệch mức và do bước đối chiếu báo lỗi, không bị nuốt im lặng.
    """
    depth4 = sum(1 for d in candidates.values() if d == 4)
    depth5 = sum(1 for d in candidates.values() if d == 5)
    if depth5 > depth4:
        return 5
    return 4


def _wrapper_name(root: Path, parts_by_rel: dict[str, tuple[str, ...]],
                  dossier_depth: int) -> str:
    if dossier_depth != 5:
        return ""
    for parts in parts_by_rel.values():
        if len(parts) == dossier_depth:
            return parts[0]
    return ""


def scan_archive_folder(root: Path) -> FolderScanResult:
    """Quét đường dẫn toàn cây ``root`` và nhận diện hồ sơ chuẩn (mục 3).

    Chỉ scandir/stat — không đọc nội dung PDF, an toàn gọi trong thread nền.
    """
    root = Path(root)
    result = FolderScanResult(root=root)

    # Lượt 1: gom cấu trúc thư mục + toàn bộ PDF (rel posix).
    dir_parts: dict[str, tuple[str, ...]] = {}
    pdf_parts: list[tuple[str, ...]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort(key=str.lower)
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir.parts:
            dir_parts[rel_dir.as_posix()] = rel_dir.parts
        for name in sorted(filenames, key=str.lower):
            if name.lower().endswith(".pdf"):
                pdf_parts.append((*rel_dir.parts, name))
    result.pdf_count = len(pdf_parts)

    if not dir_parts and not pdf_parts:
        return result

    # Thư mục ứng viên hồ sơ: parse được tên 4 đoạn (chưa đối chiếu mã cha).
    candidates: dict[str, int] = {}
    for rel, parts in dir_parts.items():
        if rt.parse_dossier_folder_name(parts[-1]) is not None:
            candidates[rel] = len(parts)
    dossier_depth = _choose_wrapper_depth(root, candidates)
    wrapper = _wrapper_name(root, dir_parts, dossier_depth)
    result.wrapper = wrapper

    dossier_rels = {
        rel for rel, depth in candidates.items() if depth == dossier_depth
    }

    # Lượt 2: lập hồ sơ + đối chiếu mã cha. rel đã tính TỪ gốc người dùng
    # chọn nên luôn chứa sẵn lớp bao ngoài (nếu có) — không thêm prefix nữa.
    by_rel: dict[str, DossierScan] = {}
    for rel in sorted(dossier_rels):
        parts = Path(rel).parts
        codes = rt.parse_dossier_folder_name(parts[-1])
        assert codes is not None  # đã lọc ở lượt 1
        scan = DossierScan(rel=rel, name=parts[-1], codes=codes)
        issue = _check_code_widths(codes, rel)
        if issue:
            scan.errors.append(issue)
        issue = _codes_match_chain(parts, codes, wrapper)
        if issue:
            scan.errors.append(issue)
        by_rel[rel] = scan
        result.dossiers.append(scan)

    # Lượt 3: phân loại PDF — thuộc hồ sơ (đúng mức con trực tiếp) hay lạc.
    dossier_prefixes = {
        rel: Path(rel).parts for rel in by_rel
    }
    docs_by_dossier: dict[str, list[DocumentScan]] = {
        rel: [] for rel in by_rel
    }
    for parts in pdf_parts:
        rel = "/".join(parts)
        matched = None
        for drel, dparts in dossier_prefixes.items():
            if parts[:len(dparts)] == dparts:
                matched = (drel, dparts)
                break
        if matched is None:
            result.issues.append(ScanIssue(
                rel,
                "PDF nằm ngoài thư mục hồ sơ chuẩn "
                "(Mã định danh/Phông/Mục lục/Hồ sơ).",
            ))
            continue
        drel, dparts = matched
        scan = by_rel[drel]
        if len(parts) != len(dparts) + 1:
            scan.errors.append(ScanIssue(
                rel,
                "PDF nằm trong thư mục con của hồ sơ — cấu trúc chuẩn yêu "
                "cầu PDF nằm trực tiếp trong thư mục hồ sơ.",
            ))
            continue
        parsed = rt.parse_pdf_name(parts[-1])
        if parsed is None:
            scan.errors.append(ScanIssue(
                rel,
                "Tên PDF không khớp quy ước 5 đoạn "
                "(MãĐD-Phông-MụcLục-HồSơ-STT[+thông tin tùy chọn]).",
            ))
            continue
        # Chỉ nêu ĐÚNG đoạn lệch — các đoạn khớp không nhắc lại.
        pdf_codes = (parsed.ma_dinh_danh, parsed.ma_phong, parsed.muc_luc,
                     parsed.ho_so)
        dossier_codes = (scan.codes.ma_dinh_danh, scan.codes.ma_phong,
                         scan.codes.muc_luc, scan.codes.ho_so)
        code_labels = ("mã định danh", "mã phông", "mã mục lục",
                       "mã hồ sơ")
        diffs = [
            f"{lbl} ({a} ≠ {b})"
            for lbl, a, b in zip(code_labels, pdf_codes, dossier_codes)
            if a != b
        ]
        if diffs:
            scan.errors.append(ScanIssue(
                rel,
                "Tên PDF lệch mã hồ sơ — " + "; ".join(diffs) + ".",
            ))
            continue
        if not parsed.stt.isdigit() or len(parsed.stt) > _MAX_STT_DIGITS:
            scan.errors.append(ScanIssue(
                rel,
                f"Số thứ tự \"{parsed.stt}\" phải là chữ số "
                f"(tối đa {_MAX_STT_DIGITS} số).",
            ))
            continue
        docs_by_dossier[drel].append(DocumentScan(
            rel=rel, name=parts[-1], stt=parsed.stt,
            title_hint=parsed.extra,
        ))

    # Lượt 4: thứ tự trong hồ sơ theo STT (số học, giữ nguyên chuỗi gốc) —
    # trùng STT là xung đột định danh → lỗi hồ sơ (không âm thầm giữ một).
    for drel, docs in docs_by_dossier.items():
        scan = by_rel[drel]
        seen: dict[str, DocumentScan] = {}
        for doc in docs:
            if doc.stt in seen:
                scan.errors.append(ScanIssue(
                    doc.rel,
                    f"Trùng số thứ tự {doc.stt} với {seen[doc.stt].rel} "
                    "trong cùng hồ sơ.",
                ))
            else:
                seen[doc.stt] = doc
        scan.documents = sorted(
            docs, key=lambda d: (int(d.stt), d.name.lower()))

    # Thư mục 4 đoạn ở độ sâu KHÁC mức đã chọn → lệch mức, báo rõ đường dẫn.
    for rel, depth in sorted(candidates.items()):
        if depth != dossier_depth:
            result.issues.append(ScanIssue(
                rel,
                "Thư mục có dạng tên hồ sơ nhưng nằm lệch mức cấu trúc "
                f"(cần sâu đúng {dossier_depth} cấp từ gốc đã chọn).",
            ))

    result.dossiers.sort(key=lambda d: d.rel.lower())
    result.issues.sort(key=lambda i: i.rel.lower())
    result.logical_root = root / wrapper if wrapper else root
    return result
