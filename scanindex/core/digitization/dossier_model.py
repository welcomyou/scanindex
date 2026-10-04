"""Mô hình nhiều hồ sơ cho phiên Số hóa lưu trữ (mục 6 của kế hoạch).

Phiên (``ArchiveSession``) mở rộng với danh sách hồ sơ CÓ THỨ TỰ; mỗi hồ sơ
có ID ổn định + ``IdentityCodes`` + nguồn; mỗi tài liệu có ID ổn định, liên
kết hồ sơ, đường dẫn nguồn và thứ tự.

Quy tắc ID (mục 6): ID nội bộ KHÔNG phải basename, chỉ số dòng, hash hay
bốn mã có thể chỉnh sửa — cấp một lần khi nhập, không đổi khi người dùng
đổi tên / kéo thứ tự / sửa mã. Định dạng: hồ sơ ``d001``, tài liệu
``d001-v001``.

Runtime: trạng thái xử lý (OCR/KIE/ký/status) sống trong doc-dict đi qua
Bước 2/3 (đã là cơ chế hiện có — list truyền theo tham chiếu); DocumentState
ở đây là registry theo ID để callback/runner/ký/xuất resolve đúng tài liệu
sau khi đổi thứ tự hay đổi focus (không map theo basename).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from scanindex.core import rename_tree as rt


@dataclass(frozen=True)
class DossierPick:
    """Một hồ sơ được chốt từ popup chọn hồ sơ (dữ liệu quét lần cuối)."""

    dossier_dir: str            # đường dẫn tuyệt đối thư mục hồ sơ
    documents: tuple            # đường dẫn tuyệt đối PDF theo thứ tự đã chỉnh
    codes: object               # rt.DossierName — 4 mã đọc từ tên thư mục


@dataclass(frozen=True)
class DossierSelection:
    """Snapshot kết quả popup ``DossierSelectionDialog`` trả về."""

    source_root: str                        # thư mục người dùng chọn
    logical_root: str                       # gốc logic (chứa thư mục mã định danh)
    dossiers: tuple = field(default=())     # tuple[DossierPick]


@dataclass
class DossierState:
    """Một hồ sơ trong phiên: ID ổn định + mã định danh + nguồn."""

    id: str
    identity: object            # IdentityCodes (import muộn để tránh vòng)
    source_directory: str = ""
    display_name: str = ""      # tên hiển thị trên Bước 2/3

    def composite_label(self) -> str:
        """Nhãn 4 mã (ưu tiên) hoặc tên hiển thị."""
        ident = self.identity
        if ident is not None and ident.is_complete():
            return (f"{ident.ma_dinh_danh}-{ident.ma_phong}-"
                    f"{ident.muc_luc}-{ident.ho_so}")
        return self.display_name or self.id


@dataclass
class DocumentState:
    """Một tài liệu trong phiên, liên kết hồ sơ bằng ID (không basename)."""

    id: str
    dossier_id: str
    source_path: str
    source_name: str
    order: int
    metadata: dict = field(default_factory=dict)
    ocr_path: str = ""
    canonical_path: str = ""
    signed_path: str = ""
    status: str = "Pending"


def identity_from_codes(codes: rt.DossierName, title: str = "",
                        **extras) -> object:
    """IdentityCodes cấu trúc từ 4 mã đọc được trên cây thư mục.

    Tên hồ sơ điền sẵn bằng tên thư mục (gợi ý, người dùng sửa được ở
    Bước 2/3); các trường bổ sung để trống — điền sau qua DossierInfoDialog.
    """
    from scanindex.core.digitization.session import IdentityCodes

    return IdentityCodes(
        ma_dinh_danh=codes.ma_dinh_danh,
        ma_phong=codes.ma_phong,
        muc_luc=codes.muc_luc,
        ho_so=codes.ho_so,
        title=title or codes.compose(),
        **extras,
    )


class _IdAllocator:
    """Cấp ID dNNN / dNNN-vNNN tăng dần trong phạm vi một lần nhập."""

    def __init__(self, dossier_start: int = 1):
        self.dossier_seq = int(dossier_start)
        self.doc_seq = 0

    def next_dossier_id(self) -> str:
        sid = f"d{self.dossier_seq:03d}"
        self.dossier_seq += 1
        self.doc_seq = 0
        return sid

    def next_document_id(self, dossier_id: str) -> str:
        self.doc_seq += 1
        return f"{dossier_id}-v{self.doc_seq:03d}"


def build_from_selection(
    selection: DossierSelection,
    *,
    dossier_start: int = 1,
) -> tuple[list[DossierState], list[DocumentState]]:
    """Chuyển snapshot popup thành (danh sách hồ sơ, danh sách tài liệu).

    Thứ tự hồ sơ = thứ tự popup; thứ tự tài liệu = thứ tự trong pick (theo
    STT đã chỉnh). Tên hồ sơ gợi ý = tên thư mục hồ sơ (mục 4.3/7).
    """
    alloc = _IdAllocator(dossier_start)
    dossiers: list[DossierState] = []
    documents: list[DocumentState] = []
    for pick in selection.dossiers:
        dossier_id = alloc.next_dossier_id()
        codes = pick.codes
        identity = identity_from_codes(codes, title=Path(pick.dossier_dir).name)
        dossiers.append(DossierState(
            id=dossier_id,
            identity=identity,
            source_directory=pick.dossier_dir,
            display_name=codes.compose(),
        ))
        for order, pdf_path in enumerate(pick.documents):
            documents.append(DocumentState(
                id=alloc.next_document_id(dossier_id),
                dossier_id=dossier_id,
                source_path=pdf_path,
                source_name=os.path.basename(pdf_path),
                order=order,
            ))
    return dossiers, documents


def build_single_dossier(
    directory: str,
    identity: object,
    pdf_paths: list[str],
    *,
    dossier_start: int = 1,
) -> tuple[list[DossierState], list[DocumentState]]:
    """Gom toàn bộ PDF (mọi độ sâu) thành MỘT hồ sơ — luồng "không chuẩn"
    (mục 5 của kế hoạch). Thứ tự = danh sách đường dẫn đã sort bên ngoài.
    """
    alloc = _IdAllocator(dossier_start)
    dossier_id = alloc.next_dossier_id()
    title = (getattr(identity, "title", "") or Path(directory).name)
    dossiers = [DossierState(
        id=dossier_id,
        identity=identity,
        source_directory=directory,
        display_name=title,
    )]
    documents = [
        DocumentState(
            id=alloc.next_document_id(dossier_id),
            dossier_id=dossier_id,
            source_path=p,
            source_name=os.path.basename(p),
            order=order,
        )
        for order, p in enumerate(pdf_paths)
    ]
    return dossiers, documents


def document_doc_dict(doc: DocumentState, dossier: DossierState) -> dict:
    """Doc-dict adapter cho Bước 2/main_window — cùng schema luồng hiện có,
    thêm ``_doc_id`` / ``_dossier_id`` / ``_dossier_label``."""
    label = dossier.composite_label()
    return {
        "pdf_path": doc.source_path,
        "path": doc.source_path,
        "output_path": None,
        "ocr_path": None,
        "json_path": None,
        "metadata": {},
        "zones": {},
        "status": "Pending",
        "_doc_id": doc.id,
        "_dossier_id": doc.dossier_id,
        "_dossier_label": label,
    }
