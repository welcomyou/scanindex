"""Đổi tên theo cây thư mục cho CSDL_SOHOA (cấu trúc thư mục số hóa PMKhoSohoa).

Cây thư mục chuẩn:

    <CSDL_SOHOA>/                                  ← thư mục gốc do người dùng chọn
    └─ <MãĐD>/                          A29.244.01
       └─ <MãPhông>/                     A29.244.01.002
          └─ <MụcLục>/                   01
             └─ <MãĐD>-<MãPhông>-<ML>-<HS>/    A29.244.01-A29.244.01.002-01-0001
                └─ ...-<NNN>.pdf          ...-0001-001.pdf

Quy ước đặt tên là **composable**: tên hồ sơ và tên file PDF ghép từ mã của
các thư mục cha, ngăn cách giữa các đoạn bằng "-" (bên trong một mã chỉ có
"." nên việc tách theo "-" là an toàn). Đổi tên một thư mục ở cấp bất kỳ
thì **tên hồ sơ / file PDF bên dưới được DỰNG LẠI từ bộ mã của chuỗi cha**
— còn TÊN THƯ MỤC phông / mục lục bên dưới luôn GIỮ NGUYÊN:

  - Cấp 1 (Mã định danh): hồ sơ / PDF bên dưới dựng lại với mã định danh
    mới (mã phông, mục lục lấy theo tên thư mục hiện có).
  - Cấp 2 (Phông): hồ sơ / PDF bên dưới dựng lại với mã phông mới (tên
    mục lục không đổi).
  - Cấp 3 (Mục lục): hồ sơ / PDF bên dưới dựng lại với số mục lục mới.
  - Cấp 4 (Hồ sơ): nhập số hồ sơ mới, thư mục ghép lại từ cha, PDF đổi
    theo tên hồ sơ mới.

Hồ sơ / PDF đang LỆCH mã cha (tên cũ không khớp thư mục cha — dữ liệu cũ
hoặc đã bị đổi tên thủ công bên ngoài) cũng được dựng lại luôn theo bộ mã
mới của chuỗi cha, giữ nguyên số hồ sơ và số thứ tự PDF. Chỉ mục KHÔNG
parse được quy ước 4/5 đoạn mới bị bỏ qua và báo trong
``RenamePlan.skipped``.

Độ rộng số BẮT BUỘC (``LEVEL_WIDTH`` / ``PDF_STT_WIDTH``): Mục lục đúng 2
chữ số, Hồ sơ đúng 4 chữ số, số thứ tự PDF (NNN) đúng 3 chữ số — ép khi
người dùng nhập (``validate_component`` / ``plan_pdf_rename_stt``) và khi
đánh số tự động.

Ngoài đổi tên tại chỗ, module còn lập kế hoạch **di chuyển** qua cha khác
(``plan_folder_move``): mục lục sang phông khác, hồ sơ sang mục lục khác,
phông sang mã định danh khác — số của chính mục được GIỮ NGUYÊN (trùng tại
đích thì tự gán số kế tiếp), toàn bộ hồ sơ / PDF bên dưới được dựng lại
đúng theo mã của cha đích, kể cả hồ sơ / PDF đang lệch tên được "sửa" luôn.
Di chuyển KHÔNG tự sắp xếp / đánh số lại — thứ tự hồ sơ lẫn PDF do người
dùng kéo-thả (UI giữ thứ tự thủ công) và chỉ được ghi vào tên khi chủ động
đánh số: ``plan_renumber_siblings_from`` đánh số tăng dần theo thứ tự hiển
thị đưa vào (``order``) từ một hồ sơ / mục lục / tài liệu PDF (giữ số của
nó) đến hết, không reset về 1. PDF chuyển hồ sơ dùng ``plan_pdf_move``
(đơn file hay NHÓM): giữ nguyên số trang cũ (trùng thì lấy số tự do kế
tiếp), không đổi tên file của hồ sơ đích; đổi riêng NNN bằng
``plan_pdf_rename_stt``. Mỗi kế hoạch mang ``kind`` ("rename" / "move" /
"pdf_move" / "pdf_reorder" / "renumber" / "invert"); hoàn tác Ctrl+Z =
``plan_invert`` đảo ngược toàn bộ op của kế hoạch đã chạy.

Thứ tự thực thi luôn **dưới lên trên** (PDF → hồ sơ → phông → thư mục đang
đổi) nên mọi op dùng đường dẫn cũ vẫn còn hợp lệ tại thời điểm chạy.

Thư mục / file không khớp quy ước được giữ nguyên và báo lại trong
``RenamePlan.skipped`` (không đổi mò). Các file không phải PDF (xlsx, zip,
sidecar...) nằm trong cây chỉ "đi theo" thư mục cha, không bị đổi tên.

Module chỉ dùng stdlib để chạy độc lập với kho SQLite/Tantivy; logic retry
lock Windows mirror theo ``scanindex.core.repository.admin``.
"""
from __future__ import annotations

import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

SEGMENT_SEP = "-"
_INVALID_PATH_PART_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class Level(Enum):
    MA_DINH_DANH = 1
    PHONG = 2
    MUC_LUC = 3
    HO_SO = 4


LEVEL_LABELS = {
    Level.MA_DINH_DANH: "Mã định danh",
    Level.PHONG: "Phông",
    Level.MUC_LUC: "Mục lục",
    Level.HO_SO: "Hồ sơ",
}

# Độ rộng BẮT BUỘC của số theo cấp (mã định danh / phông là mã tự do):
# Mục lục đúng 2 chữ số, Hồ sơ đúng 4 chữ số, số thứ tự PDF đúng 3 chữ số.
LEVEL_WIDTH = {Level.MUC_LUC: 2, Level.HO_SO: 4}
PDF_STT_WIDTH = 3


@dataclass(frozen=True)
class DossierName:
    """Tên thư mục hồ sơ `<MãĐD>-<MãPhông>-<ML>-<HS>`."""
    ma_dinh_danh: str
    ma_phong: str
    muc_luc: str
    ho_so: str

    def compose(self) -> str:
        return SEGMENT_SEP.join(
            (self.ma_dinh_danh, self.ma_phong, self.muc_luc, self.ho_so)
        )


@dataclass(frozen=True)
class PdfName:
    """Tên file `<MãĐD>-<MãPhông>-<ML>-<HS>-<NNN>.pdf` (giữ nguyên chuỗi STT)."""
    ma_dinh_danh: str
    ma_phong: str
    muc_luc: str
    ho_so: str
    stt: str
    ext: str  # ".pdf" hoặc ".PDF" — giữ nguyên như file gốc

    def compose(self) -> str:
        return (
            SEGMENT_SEP.join(
                (self.ma_dinh_danh, self.ma_phong, self.muc_luc,
                 self.ho_so, self.stt)
            ) + self.ext
        )


def parse_dossier_folder_name(name: str) -> DossierName | None:
    """Parse tên thư mục hồ sơ; trả None nếu không khớp quy ước 4 đoạn."""
    parts = name.split(SEGMENT_SEP)
    if len(parts) != 4 or not all(parts):
        return None
    return DossierName(*parts)


def parse_pdf_name(name: str) -> PdfName | None:
    """Parse tên file PDF; trả None nếu không khớp quy ước 5 đoạn + .pdf."""
    p = Path(name)
    if p.suffix.lower() != ".pdf":
        return None
    parts = p.stem.split(SEGMENT_SEP)
    if len(parts) != 5 or not all(parts):
        return None
    return PdfName(*parts, ext=p.suffix)


def validate_component(label: str, value: str,
                       level: Level | None = None) -> str:
    """Validate mã người dùng nhập cho cấp đang đổi tên. Raise ValueError.

    Với ``level`` thuộc ``LEVEL_WIDTH``: mã bắt buộc là ĐÚNG số chữ số quy
    định, toàn chữ số (Mục lục 2 số, Hồ sơ 4 số).
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Thiếu {label}.")
    if text in {".", ".."}:
        raise ValueError(f"{label} không hợp lệ: {text!r}")
    if _INVALID_PATH_PART_RE.search(text):
        raise ValueError(
            f"{label} chứa ký tự không hợp lệ (không dùng \\ / : * ? \" < > |): {text!r}"
        )
    if SEGMENT_SEP in text:
        raise ValueError(
            f"{label} không được chứa dấu \"{SEGMENT_SEP}\" vì đây là dấu phân "
            f"cách đoạn trong tên hồ sơ/file: {text!r}"
        )
    width = LEVEL_WIDTH.get(level)
    if width is not None and (not text.isdigit() or len(text) != width):
        raise ValueError(
            f"{label} bắt buộc phải là đúng {width} chữ số "
            f"(ví dụ: {'0' * (width - 1)}1) — nhận được: {text!r}"
        )
    return text


def level_of(rel: Path) -> Level | None:
    """Cấp của một đường dẫn tương đối với gốc CSDL_SOHOA (None nếu ngoài cây)."""
    depth = len(Path(rel).parts)
    if depth < 1 or depth > 4:
        return None
    return Level(depth)


def _move_path_with_retry(src: Path, dst: Path, *,
                          attempts: int = 16, delay: float = 0.15) -> None:
    """Move một path, chịu đựng lock tạm thời của Windows (winerror 5/32/33)."""
    for attempt in range(max(1, int(attempts))):
        try:
            # Check every move, including inverse/rollback moves after a parent
            # has moved: those collisions are invisible during preflight.
            if os.path.lexists(dst) and not (
                    src == dst and os.path.samefile(src, dst)):
                raise FileExistsError(f'Đã có sẵn "{dst}" — không ghi đè.')
            if os.name == "nt":
                # Windows rename refuses an occupied destination atomically.
                # shutil.move would fall back to copy+delete on failure and
                # could overwrite data or leave a copy after a sharing error.
                os.rename(src, dst)
            else:
                shutil.move(str(src), str(dst))
            return
        except OSError as exc:
            is_transient_lock = (
                isinstance(exc, PermissionError)
                or getattr(exc, "winerror", None) in {5, 32, 33}
            )
            if not is_transient_lock:
                raise
            if attempt + 1 >= max(1, int(attempts)):
                raise PermissionError(
                    f"File đang được chương trình khác sử dụng: {src}. "
                    "Hãy đóng cửa sổ đang mở hoặc khung xem trước Explorer "
                    "rồi thử lại."
                ) from exc
            time.sleep(max(0.0, float(delay)))


@dataclass
class RenameOp:
    src: Path
    dst: Path
    is_dir: bool
    # Windows: đổi tên chỉ khác hoa/thường phải đi qua tên tạm.
    needs_stage: bool = False


@dataclass
class RenamePlan:
    root: Path
    folder_rel: Path            # đường dẫn tương đối của thư mục được đổi tên
    level: Level
    old_name: str
    new_name: str               # tên mới đầy đủ của chính thư mục đó
    ops: list[RenameOp] = field(default_factory=list)   # thực thi theo thứ tự
    skipped: list[str] = field(default_factory=list)    # mục không khớp quy ước
    # Ánh xạ path cũ → mới (str, "/" phân cách, đã kết hợp chuỗi đổi tên lồng
    # nhau) để UI cập nhật cây / viewer mà không phải quét lại.
    path_map: dict[str, str] = field(default_factory=dict)
    # Mô tả thao tác để ghi log (đổi tên cấp / xếp lại thứ tự PDF).
    title: str = ""
    # Thư mục tạm dùng trong xếp lại thứ tự PDF (two-phase); được dọn sau khi
    # thực thi. None với kế hoạch đổi tên thường.
    staging_dir: Path | None = None
    # Loại kế hoạch: "rename" (đổi tên tại chỗ), "move" (chuyển phông/mục
    # lục/hồ sơ sang cha khác), "pdf_move" (chuyển PDF sang hồ sơ khác, đơn
    # hay nhóm), "pdf_reorder" (xếp lại thứ tự PDF), "renumber" (đánh số
    # lại hồ sơ/mục lục từ một mục về sau), "invert" (kế hoạch hoàn tác).
    # UI dùng kind để dựng mục hoàn tác (Ctrl+Z).
    kind: str = "rename"
    # Cha đích (rel path) với kind="move"/"pdf_move"; None với kind khác.
    target_parent_rel: Path | None = None

    @property
    def affected_dirs(self) -> int:
        return sum(1 for op in self.ops if op.is_dir)

    @property
    def affected_pdfs(self) -> int:
        return sum(1 for op in self.ops if not op.is_dir)


# --------------------------------------------------------------------------
# Lập kế hoạch
# --------------------------------------------------------------------------

def _rel_str(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _plan_ml_children(ml_dir: Path, *, new_mdd: str, new_phong: str,
                      new_ml: str, plan: RenamePlan) -> None:
    """Lên kế hoạch cho các hồ sơ + PDF bên trong một thư mục mục lục.

    Tên hồ sơ / PDF được DỰNG LẠI từ bộ mã mới của chuỗi cha (``new_mdd``,
    ``new_phong``, ``new_ml``) — kể cả khi tên cũ đang LỆCH mã cha (dữ liệu
    cũ hoặc đã đổi tên thủ công); số hồ sơ và số thứ tự PDF giữ nguyên.
    Thư mục hồ sơ không parse được quy ước 4 đoạn, hoặc file trong hồ sơ
    không parse được quy ước 5 đoạn, giữ nguyên (báo ``skipped``).
    """
    for ho_so_dir in sorted(ml_dir.iterdir()):
        if not ho_so_dir.is_dir():
            continue
        parsed = parse_dossier_folder_name(ho_so_dir.name)
        if parsed is None:
            plan.skipped.append(_rel_str(ho_so_dir, plan.root))
            continue
        dossier_new = DossierName(
            ma_dinh_danh=new_mdd, ma_phong=new_phong,
            muc_luc=new_ml, ho_so=parsed.ho_so,
        )
        new_name = dossier_new.compose()
        # PDF trong hồ sơ được đổi trước, dùng đường dẫn hồ sơ cũ.
        for entry in sorted(ho_so_dir.iterdir()):
            if entry.is_dir():
                continue
            pdf = parse_pdf_name(entry.name)
            if pdf is None:
                plan.skipped.append(_rel_str(entry, plan.root))
                continue
            new_pdf = PdfName(
                ma_dinh_danh=new_mdd, ma_phong=new_phong, muc_luc=new_ml,
                ho_so=parsed.ho_so, stt=pdf.stt, ext=pdf.ext,
            ).compose()
            if new_pdf != entry.name:
                plan.ops.append(RenameOp(
                    src=entry, dst=ho_so_dir / new_pdf, is_dir=False,
                ))
        if new_name != ho_so_dir.name:
            plan.ops.append(RenameOp(
                src=ho_so_dir, dst=ho_so_dir.parent / new_name, is_dir=True,
            ))


def plan_folder_rename(root: Path, folder_rel: Path,
                       new_component: str) -> RenamePlan:
    """Lập kế hoạch đổi tên 1 thư mục trong cây CSDL_SOHOA.

    ``new_component`` là mã của cấp đó (MãĐD / MãPhông / Mục lục / Hồ sơ).
    Với cấp Hồ sơ, chấp nhận nhập cả tên đầy đủ 4 đoạn — khi đó lấy đoạn 4
    làm mã mới (các đoạn trước phải khớp thư mục cha).

    Raise ``ValueError`` khi mã không hợp lệ hoặc phát sinh xung đột tên;
    khi đó không op nào được thực thi.
    """
    root = Path(root).resolve()
    rel = Path(folder_rel)
    if not rel.parts or rel.is_absolute():
        raise ValueError("Đường dẫn thư mục không hợp lệ.")
    level = level_of(rel)
    if level is None:
        raise ValueError(
            "Chỉ đổi tên được thư mục ở 4 cấp: Mã định danh, Phông, Mục lục, Hồ sơ."
        )
    folder = root.joinpath(*rel.parts)
    if not folder.is_dir():
        raise ValueError(f"Thư mục không tồn tại: {folder}")

    old_name = folder.name
    parts = rel.parts

    if level is Level.HO_SO and SEGMENT_SEP in str(new_component).strip():
        # Người dùng dán tên đầy đủ 4 đoạn: tách lấy mã hồ sơ (đoạn cuối).
        full = str(new_component).strip()
        segs = full.split(SEGMENT_SEP)
        if len(segs) != 4:
            raise ValueError(
                f"Tên hồ sơ phải có 4 đoạn ngăn cách bởi \"{SEGMENT_SEP}\": {full!r}"
            )
        expected = SEGMENT_SEP.join(parts[:3])
        if SEGMENT_SEP.join(segs[:3]) != expected:
            raise ValueError(
                f"3 đoạn đầu của tên mới ({full}) phải trùng với thư mục cha: "
                f"{expected}"
            )
        new_component = segs[3]

    label = LEVEL_LABELS[level]
    comp = validate_component(label, new_component, level=level)

    plan = RenamePlan(root=root, folder_rel=rel, level=level,
                      old_name=old_name, new_name=old_name)

    if level is Level.MA_DINH_DANH:
        # Mã phông và mục lục ĐỘC LẬP với mã định danh — đổi mã định danh
        # KHÔNG đổi tên phông / mục lục; hồ sơ / PDF bên dưới được DỰNG LẠI
        # từ mã định danh mới + tên phông / mục lục thực tế (kể cả hồ sơ
        # đang lệch mã cũ — được sửa luôn).
        new_mdd = comp
        plan.new_name = new_mdd
        for phong_dir in sorted(folder.iterdir()):
            if not phong_dir.is_dir():
                continue
            for ml_dir in sorted(phong_dir.iterdir()):
                if not ml_dir.is_dir():
                    continue
                _plan_ml_children(
                    ml_dir, new_mdd=new_mdd, new_phong=phong_dir.name,
                    new_ml=ml_dir.name, plan=plan,
                )
    elif level is Level.PHONG:
        mdd = parts[0]
        plan.new_name = comp
        for ml_dir in sorted(folder.iterdir()):
            if not ml_dir.is_dir():
                continue
            _plan_ml_children(
                ml_dir, new_mdd=mdd, new_phong=comp, new_ml=ml_dir.name,
                plan=plan,
            )
    elif level is Level.MUC_LUC:
        mdd, phong = parts[0], parts[1]
        plan.new_name = comp
        _plan_ml_children(
            folder, new_mdd=mdd, new_phong=phong, new_ml=comp, plan=plan,
        )
    else:  # HO_SO — chỉ chính hồ sơ được chọn, không đụng hồ sơ anh em.
        # Tên mới ghép từ mã THỰC TẾ của chuỗi cha (sửa luôn hồ sơ lệch mã);
        # PDF bên dưới dựng lại theo tên hồ sơ mới, giữ số thứ tự.
        dossier_new = DossierName(
            ma_dinh_danh=parts[0], ma_phong=parts[1], muc_luc=parts[2],
            ho_so=comp,
        )
        plan.new_name = dossier_new.compose()
        for entry in sorted(folder.iterdir()):
            if entry.is_dir():
                continue
            pdf = parse_pdf_name(entry.name)
            if pdf is None:
                plan.skipped.append(_rel_str(entry, plan.root))
                continue
            new_pdf = PdfName(
                ma_dinh_danh=parts[0], ma_phong=parts[1], muc_luc=parts[2],
                ho_so=comp, stt=pdf.stt, ext=pdf.ext,
            ).compose()
            if new_pdf != entry.name:
                plan.ops.append(RenameOp(
                    src=entry, dst=folder / new_pdf, is_dir=False,
                ))

    if plan.new_name != old_name:
        plan.ops.append(RenameOp(
            src=folder, dst=folder.parent / plan.new_name, is_dir=True,
        ))

    plan.title = f'đổi tên "{plan.old_name}" → "{plan.new_name}"'
    _validate_ops(plan)
    plan.path_map = _build_path_map(plan)
    return plan


def _next_free_number(used: set[str], width: int = 3) -> str:
    """Số kế tiếp (zero-pad theo ``width``) chưa xuất hiện trong ``used``."""
    width = max(width, max((len(u) for u in used), default=0))
    k = 1
    while str(k).zfill(width) in used:
        k += 1
    width = max(width, len(str(k)))
    return str(k).zfill(width)


def _plan_move_dossier_pdfs(ho_so_dir: Path,
                            new_codes: tuple[str, str, str, str],
                            plan: RenamePlan) -> None:
    """Đổi tên mọi file .pdf trong một hồ sơ (đang di chuyển) theo bộ mã mới.

    PDF đúng quy ước giữ nguyên số thứ tự (NNN) nếu chưa bị chiếm; PDF lệch
    tên nhận số tự do — sau khi di chuyển mọi PDF đều khớp mã hồ sơ mới.
    File không phải PDF đi theo nhưng giữ nguyên tên (báo ``skipped``).
    """
    mdd, phong, ml, hs = new_codes
    files: list[tuple[Path, PdfName | None]] = []
    width = 3
    for entry in sorted(ho_so_dir.iterdir()):
        if entry.is_dir():
            continue
        if entry.suffix.lower() != ".pdf":
            plan.skipped.append(_rel_str(entry, plan.root))
            continue
        parsed = parse_pdf_name(entry.name)
        files.append((entry, parsed))
        if parsed is not None:
            width = max(width, len(parsed.stt))
    # PDF hợp lệ xử lý trước để giữ NNN cũ; PDF lệch tên nhận số tự do sau.
    files.sort(key=lambda item: item[1] is None)
    used: set[str] = set()
    for entry, parsed in files:
        if parsed is not None and parsed.stt not in used:
            stt = parsed.stt
        else:
            stt = _next_free_number(used, width)
        used.add(stt)
        new_pdf = PdfName(
            ma_dinh_danh=mdd, ma_phong=phong, muc_luc=ml, ho_so=hs,
            stt=stt, ext=entry.suffix,
        ).compose()
        if new_pdf != entry.name:
            plan.ops.append(RenameOp(
                src=entry, dst=ho_so_dir / new_pdf, is_dir=False,
            ))


def _plan_move_ml_contents(ml_dir: Path, new_mdd: str, new_phong: str,
                           new_ml: str, plan: RenamePlan) -> None:
    """Dựng lại toàn bộ hồ sơ + PDF trong một mục lục (đang di chuyển).

    Khác với đổi tên tại chỗ (chỉ chạm hồ sơ khớp mã cha cũ), di chuyển
    re-anker cả những hồ sơ parse được nhưng đang lệch mã cha: sau khi
    chuyển, mọi hồ sơ đều mang đúng bộ mã của cha đích. Thư mục hồ sơ
    không parse được đi theo nhưng giữ nguyên tên (báo ``skipped``).
    """
    for ho_so_dir in sorted(ml_dir.iterdir()):
        if not ho_so_dir.is_dir():
            continue
        parsed = parse_dossier_folder_name(ho_so_dir.name)
        if parsed is None:
            plan.skipped.append(_rel_str(ho_so_dir, plan.root))
            continue
        _plan_move_dossier_pdfs(
            ho_so_dir, (new_mdd, new_phong, new_ml, parsed.ho_so), plan)
        new_name = DossierName(
            ma_dinh_danh=new_mdd, ma_phong=new_phong,
            muc_luc=new_ml, ho_so=parsed.ho_so,
        ).compose()
        if new_name != ho_so_dir.name:
            plan.ops.append(RenameOp(
                src=ho_so_dir, dst=ho_so_dir.parent / new_name, is_dir=True,
            ))


def plan_folder_move(root: Path, folder_rel: Path,
                     target_parent_rel: Path, *,
                     new_name: str | None = None) -> RenamePlan:
    """Lập kế hoạch di chuyển Phông / Mục lục / Hồ sơ sang cha khác.

    Sau khi chuyển, tên mọi mục con được **dựng lại đúng theo mã của cha
    đích**:

    - Phông → Mã định danh khác: tên phông GIỮ NGUYÊN (không tự đổi tiền
      tố); hồ sơ / PDF con đổi đoạn 1 (mã định danh) theo cha mới.
    - Mục lục → Phông khác: hồ sơ / PDF con đổi các đoạn theo mã cha mới
      (kể cả hồ sơ đang lệch mã — được "sửa" luôn khi chuyển).
    - Hồ sơ → Mục lục khác (có thể khác phông luôn): dựng lại 3 đoạn đầu
      của tên hồ sơ / PDF từ mã của cha đích. Nếu mã hồ sơ đã có tại đích
      (hoặc tên hồ sơ không parse được), tự gán số hồ sơ kế tiếp còn tự do.

    PDF đúng quy ước giữ NNN cũ nếu còn tự do, PDF lệch tên được đánh số
    lại cho đúng. File không phải PDF và thư mục hồ sơ không parse được đi
    theo nhưng giữ nguyên tên (báo ``skipped``). Raise ``ValueError`` khi
    đích sai cấp, đã nằm sẵn ở đích, chuyển vào chính nó / thư mục con,
    hoặc (phông / mục lục) trùng tên thư mục tại đích — khi đó không op
    nào được thực thi.

    ``new_name`` (chỉ cho Hồ sơ) ép đúng tên đích 4 đoạn khớp mã cha —
    dùng khi hoàn tác để lấy lại mã hồ sơ gốc đã bị tự đánh số khác.
    """
    root = Path(root).resolve()
    rel = Path(folder_rel)
    tgt = Path(target_parent_rel)
    for path, what in ((rel, "Thư mục cần chuyển"), (tgt, "Thư mục đích")):
        if not path.parts or path.is_absolute():
            raise ValueError(f"{what}: đường dẫn không hợp lệ.")
    level = level_of(rel)
    if level not in (Level.PHONG, Level.MUC_LUC, Level.HO_SO):
        raise ValueError(
            "Chỉ di chuyển được Phông, Mục lục và Hồ sơ — Mã định danh là "
            "cấp cao nhất của cây."
        )
    parent_level = Level(level.value - 1)
    folder = root.joinpath(*rel.parts)
    target = root.joinpath(*tgt.parts)
    if not folder.is_dir():
        raise ValueError(f"Thư mục cần chuyển không tồn tại: {folder}")
    if not target.is_dir():
        raise ValueError(f"Thư mục đích không tồn tại: {target}")
    if rel == tgt or tgt.parts[:len(rel.parts)] == rel.parts:
        raise ValueError(
            "Không thể chuyển thư mục vào chính nó hoặc vào thư mục con của nó."
        )
    if level_of(tgt) is not parent_level:
        raise ValueError(
            f"{LEVEL_LABELS[level]} chỉ chuyển được vào "
            f"{LEVEL_LABELS[parent_level].lower()}, không phải \"{tgt.name}\"."
        )
    if rel.parent == tgt:
        raise ValueError(
            f"{LEVEL_LABELS[level]} này đã nằm sẵn trong "
            f"{LEVEL_LABELS[parent_level].lower()} \"{tgt.name}\"."
        )

    old_name = folder.name
    plan = RenamePlan(root=root, folder_rel=rel, level=level,
                      old_name=old_name, new_name=old_name,
                      kind="move", target_parent_rel=tgt)

    if level is Level.PHONG:
        # Tên phông GIỮ NGUYÊN (không tự đổi tiền tố "<MãĐD>." theo cha mới) —
        # chỉ đổi vị trí; hồ sơ / PDF bên dưới được dựng lại từ mã định danh
        # mới + tên phông (giữ nguyên).
        new_mdd = tgt.parts[0]
        new_name = old_name
        for ml_dir in sorted(folder.iterdir()):
            if not ml_dir.is_dir():
                continue
            _plan_move_ml_contents(
                ml_dir, new_mdd=new_mdd, new_phong=old_name,
                new_ml=ml_dir.name, plan=plan,
            )
    elif level is Level.MUC_LUC:
        new_mdd, new_phong = tgt.parts[0], tgt.parts[1]
        new_name = old_name  # tên mục lục giữ nguyên, chỉ đổi mã cha trong con
        _plan_move_ml_contents(
            folder, new_mdd=new_mdd, new_phong=new_phong,
            new_ml=old_name, plan=plan,
        )
    else:  # HO_SO — dựng lại tên theo mã mục lục đích, tự đánh số nếu trùng.
        new_mdd, new_phong, new_ml = tgt.parts
        if new_name is not None:
            # Ép đúng tên đích (dùng khi hoàn tác để lấy lại mã hồ sơ gốc,
            # kể cả khi lần chuyển đi đã phải tự đánh số khác).
            forced = parse_dossier_folder_name(new_name)
            if forced is None or (forced.ma_dinh_danh, forced.ma_phong,
                                  forced.muc_luc) != (new_mdd, new_phong, new_ml):
                raise ValueError(
                    f"Tên đích \"{new_name}\" không khớp mã của mục lục đích "
                    f"\"{'/'.join(tgt.parts)}\"."
                )
            hs_code = forced.ho_so
        else:
            parsed = parse_dossier_folder_name(old_name)
            used_hs = {
                p.ho_so for p in (
                    parse_dossier_folder_name(e.name)
                    for e in target.iterdir() if e.is_dir()
                ) if p is not None
            }
            if parsed is not None and parsed.ho_so not in used_hs:
                hs_code = parsed.ho_so
            else:  # mã hồ sơ đã bị chiếm tại đích / tên không parse được
                hs_code = _next_free_number(used_hs, 4)
        new_name = DossierName(
            ma_dinh_danh=new_mdd, ma_phong=new_phong,
            muc_luc=new_ml, ho_so=hs_code,
        ).compose()
        _plan_move_dossier_pdfs(
            folder, (new_mdd, new_phong, new_ml, hs_code), plan)

    plan.new_name = new_name
    # Op di chuyển chính thư mục — luôn cần vì cha thay đổi (dù tên giữ nguyên).
    plan.ops.append(RenameOp(
        src=folder, dst=target / new_name, is_dir=True,
    ))
    plan.title = (f'chuyển "{old_name}" từ "{rel.parent.as_posix()}" '
                  f'sang "{tgt.as_posix()}"')
    _validate_ops(plan)
    plan.path_map = _build_path_map(plan)
    return plan


def _plan_renumber_pdfs_from(root: Path, rel: Path, item: Path,
                             order: list[str] | None) -> RenamePlan:
    """Đánh số tài liệu (PDF) tăng dần từ file được chọn đến hết hồ sơ.

    Giữ NNN của file được chọn; các PDF SAU nó (theo ``order`` — thứ tự hiển
    thị kéo-thả, mặc định theo tên) nhận số tiếp diễn tăng dần, KHÔNG reset
    về 001, số bị chiếm bởi phần đứng trước thì nhảy qua. Tên được dựng lại
    theo mã của hồ sơ chứa (sửa luôn file lệch tên). File khác / PDF không
    parse được giữ nguyên (báo ``skipped``).
    """
    ho_so_dir = item.parent
    codes = parse_dossier_folder_name(ho_so_dir.name)
    own = parse_pdf_name(item.name)
    if own is None or not own.stt.isdigit():
        raise ValueError(
            f"Tài liệu được chọn phải có sẵn số trang hợp lệ — hãy đổi tên "
            f'nó trước: "{item.name}"'
        )
    on_disk = {e.name: e for e in ho_so_dir.iterdir() if e.is_file()}
    if order is not None:
        listed = [on_disk[n] for n in order if n in on_disk]
        listed += [on_disk[n] for n in sorted(
            (n for n in on_disk if n not in set(order)), key=str.lower)]
    else:
        listed = [on_disk[n] for n in sorted(on_disk, key=str.lower)]
    idx = listed.index(item)

    digits = [p.stt for f in listed
              for p in (parse_pdf_name(f.name),)
              if p is not None and p.stt.isdigit()]
    width = max([len(c) for c in digits] + [len(own.stt), 3])
    used = set()
    for f in listed[:idx]:
        q = parse_pdf_name(f.name)
        if q is not None and q.stt.isdigit():
            used.add(q.stt)
    counter = int(own.stt) + 1

    plan = RenamePlan(root=root, folder_rel=rel.parent, level=Level.HO_SO,
                      old_name=item.name, new_name=item.name,
                      kind="renumber")
    changed: list[tuple[Path, Path]] = []
    for f in listed[idx + 1:]:
        p = parse_pdf_name(f.name)
        if p is None or not p.stt.isdigit():
            if p is None and f.suffix.lower() == ".pdf":
                plan.skipped.append(_rel_str(f, plan.root))
            continue
        while str(counter).zfill(width) in used:
            counter += 1
        stt = str(counter).zfill(width)
        used.add(stt)
        counter += 1
        base = codes if codes is not None else p
        final = PdfName(
            ma_dinh_danh=base.ma_dinh_danh, ma_phong=base.ma_phong,
            muc_luc=base.muc_luc, ho_so=base.ho_so, stt=stt,
            ext=f.suffix,
        ).compose()
        if final != f.name:
            changed.append((f, ho_so_dir / final))

    if len(changed) >= 2:  # dịch số có thể tạo chu trình tên → staging 2 pha
        staging = ho_so_dir / f".renumber-{uuid.uuid4().hex[:8]}"
        plan.staging_dir = staging
        for i, (src, dst) in enumerate(changed):
            plan.ops.append(RenameOp(
                src=src, dst=staging / f"{i:03d}_{src.name}", is_dir=False))
        for i, (src, dst) in enumerate(changed):
            plan.ops.append(RenameOp(
                src=staging / f"{i:03d}_{src.name}", dst=dst, is_dir=False))
    else:
        for src, dst in changed:
            plan.ops.append(RenameOp(src=src, dst=dst, is_dir=False))

    plan.title = (f'đánh số từ tài liệu "{item.name}" tự động đến hết hồ sơ '
                  f"({len(changed)} file đổi tên)")
    plan.path_map = _build_path_map(plan)
    return plan


def plan_renumber_siblings_from(root: Path, item_rel: Path,
                                order: list[str] | None = None) -> RenamePlan:
    """Đánh số lại tăng dần TỪ mục được chọn đến hết danh sách anh em.

    - Hồ sơ: giữ nguyên số của hồ sơ được chọn; các hồ sơ SAU nó (trong
      ``order`` — thứ tự hiển thị do người dùng kéo-thả, mặc định theo tên)
      được đánh số tiếp diễn tăng dần (KHÔNG reset về 1, số nhảy cóc được
      giữ nếu bị chiếm bởi phần đứng trước); PDF con dựng lại theo mã mới.
    - Mục lục: giữ nguyên số mục lục được chọn (không đổi tên nó); các mục
      lục sau nó trong cùng phông được đánh số tiếp diễn; toàn bộ hồ sơ /
      PDF bên dưới được dựng lại theo mã mục lục mới.

    Mục được chọn phải có sẵn số hợp lệ (người dùng tự đặt tên trước). Mục
    có mã không phải số ở phần sau giữ nguyên tên (báo ``skipped``).
    """
    root = Path(root).resolve()
    rel = Path(item_rel)
    if not rel.parts or rel.is_absolute():
        raise ValueError("Đường dẫn không hợp lệ.")
    item = root.joinpath(*rel.parts)
    if item.is_file() and item.suffix.lower() == ".pdf":
        return _plan_renumber_pdfs_from(root, rel, item, order)
    level = level_of(rel)
    if level not in (Level.HO_SO, Level.MUC_LUC):
        raise ValueError(
            "Chỉ đánh số lại được từ một Hồ sơ, một Mục lục hoặc một "
            "tài liệu PDF."
        )
    if not item.is_dir():
        raise ValueError(f"Thư mục không tồn tại: {item}")
    on_disk = {e.name: e for e in item.parent.iterdir() if e.is_dir()}
    if order is not None and level is Level.HO_SO:
        # Thứ tự tùy ý (visual order) — mục lạ/không còn thì bỏ qua, mục
        # mới xuất hiện (không nằm trong order) xếp sau theo tên.
        listed = [on_disk[n] for n in order if n in on_disk]
        siblings = listed + [on_disk[n] for n in sorted(
            (n for n in on_disk if n not in set(order)),
            key=str.lower)]
    else:
        siblings = [on_disk[n] for n in sorted(on_disk, key=str.lower)]
    if item not in siblings:
        raise ValueError(f"Thư mục không tồn tại: {item}")
    idx = siblings.index(item)

    if level is Level.HO_SO:
        parsed = parse_dossier_folder_name(item.name)
        own_code = parsed.ho_so if parsed is not None else None
    else:
        own_code = item.name
    if not (own_code or "").isdigit():
        raise ValueError(
            f"{LEVEL_LABELS[level]} được chọn phải có sẵn số hợp lệ — hãy "
            f'đặt tên cho nó trước: "{item.name}"'
        )

    if level is Level.HO_SO:
        codes = [p.ho_so for e in siblings
                 for p in (parse_dossier_folder_name(e.name),)
                 if p is not None and p.ho_so.isdigit()]
    else:
        codes = [e.name for e in siblings if e.name.isdigit()]
    width = max([len(c) for c in codes] + [len(own_code), LEVEL_WIDTH[level]])
    used = set(codes[:idx])  # phần đứng trước giữ nguyên số
    counter = int(own_code) + 1

    plan = RenamePlan(root=root, folder_rel=rel, level=level,
                      old_name=item.name, new_name=item.name,
                      kind="renumber")
    renames: list[tuple[Path, Path]] = []  # (src, dst)
    for e in siblings[idx + 1:]:
        if level is Level.HO_SO:
            p = parse_dossier_folder_name(e.name)
            e_code = p.ho_so if p is not None else None
        else:
            p, e_code = None, e.name if e.name.isdigit() else None
            if not e.name.isdigit():
                plan.skipped.append(_rel_str(e, plan.root))
        if e_code is None or not e_code.isdigit():
            if level is Level.HO_SO and p is None:
                plan.skipped.append(_rel_str(e, plan.root))
            continue  # mã không phải số: giữ nguyên, không tham gia
        while str(counter).zfill(width) in used:
            counter += 1
        code = str(counter).zfill(width)
        used.add(code)
        counter += 1
        if code == e_code:
            continue  # đã đúng số
        if level is Level.HO_SO:
            mdd, phong, ml = rel.parts[0], rel.parts[1], rel.parts[2]
            new_name = DossierName(
                ma_dinh_danh=mdd, ma_phong=phong, muc_luc=ml, ho_so=code,
            ).compose()
            _plan_move_dossier_pdfs(e, (mdd, phong, ml, code), plan)
        else:
            mdd, phong = rel.parts[0], rel.parts[1]
            new_name = code
            _plan_move_ml_contents(e, mdd, phong, code, plan)
        renames.append((e, e.parent / new_name))

    if len(renames) >= 2:
        staging = item.parent / f".renumber-{uuid.uuid4().hex[:8]}"
        plan.staging_dir = staging
        for src, dst in renames:
            plan.ops.append(RenameOp(
                src=src, dst=staging / src.name, is_dir=True))
        for src, dst in renames:
            plan.ops.append(RenameOp(
                src=staging / src.name, dst=dst, is_dir=True))
    else:
        for src, dst in renames:
            plan.ops.append(RenameOp(src=src, dst=dst, is_dir=True))

    plan.title = (f'đánh số lại từ "{item.name}" về sau '
                  f'({len(renames)} mục đổi tên)')
    _check_dir_ops(plan)
    plan.path_map = _build_path_map(plan)
    return plan


def _check_dir_ops(plan: RenamePlan) -> None:
    """Pre-flight mô phỏng cho chuỗi op THƯ MỤC của kế hoạch đánh số lại.

    Theo dõi tên hiện hữu theo từng thư mục cha (bỏ qua op vào/ra staging —
    staging nằm ở tầng khác). Tên đích được phép trùng tên sẵn có nếu op
    trước đó đã nhường lại (dịch số), nhưng không được đè lên tên còn giữ.
    """
    names: dict[Path, set[str]] = {}

    def table(d: Path) -> set[str]:
        if d not in names:
            try:
                names[d] = {e.name for e in d.iterdir() if e.is_dir()}
            except OSError:
                names[d] = set()
        return names[d]

    staging = plan.staging_dir
    for op in plan.ops:
        if not op.is_dir:
            continue
        if op.src.parent != staging:
            table(op.src.parent).discard(op.src.name)
        if op.dst.parent != staging:
            t = table(op.dst.parent)
            if op.dst.name in t and op.dst != op.src:
                raise ValueError(
                    f'Đã có sẵn "{op.dst.name}" — thao tác sẽ gây trùng tên.'
                )
            t.add(op.dst.name)


def plan_pdf_reorder(root: Path, ho_so_rel: Path,
                     new_order: list[str]) -> RenamePlan:
    """Lập kế hoạch xếp lại thứ tự PDF trong một hồ sơ theo ``new_order``.

    ``new_order`` là danh sách tên file PDF hợp lệ hiện có trong hồ sơ, xếp
    theo thứ tự mong muốn: file ở vị trí k (tính từ 1) sẽ được đổi tên thành
    ``...-<k:0w>d.pdf`` với w là độ rộng STT hiện có (thường 3). Danh sách
    phải chứa **đúng một lần mỗi** PDF hợp lệ của hồ sơ — không thiếu, không
    thừa, không trùng. File PDF lệch tên / file khác trong hồ sơ giữ nguyên.

    Hoán đổi tên (001↔002) xử lý bằng two-phase: chuyển hết các file đổi tên
    vào ``.reorder-<uuid>/`` rồi mới đặt tên cuối, nên không bao giờ đè tên.
    """
    root = Path(root).resolve()
    rel = Path(ho_so_rel)
    if level_of(rel) is not Level.HO_SO:
        raise ValueError("Chỉ xếp lại thứ tự PDF bên trong một hồ sơ.")
    ho_so_dir = root.joinpath(*rel.parts)
    if not ho_so_dir.is_dir():
        raise ValueError(f"Thư mục hồ sơ không tồn tại: {ho_so_dir}")

    folder_codes = parse_dossier_folder_name(ho_so_dir.name)
    if folder_codes is None:
        raise ValueError(
            f"Tên thư mục hồ sơ không khớp quy ước 4 đoạn: {ho_so_dir.name}"
        )

    on_disk: dict[str, PdfName] = {}
    for entry in sorted(ho_so_dir.iterdir()):
        if entry.is_file():
            parsed = parse_pdf_name(entry.name)
            if parsed is not None:
                on_disk[entry.name] = parsed

    order = [str(n) for n in new_order]
    if not order:
        raise ValueError("Danh sách thứ tự mới trống.")
    unknown = [n for n in order if n not in on_disk]
    if unknown:
        raise ValueError(
            f"File không thuộc hồ sơ hoặc không hợp lệ: {', '.join(unknown[:3])}"
        )
    if len(order) != len(on_disk):
        raise ValueError(
            f"Danh sách thứ tự mới có {len(order)} file nhưng hồ sơ có "
            f"{len(on_disk)} PDF hợp lệ — phải liệt kê đủ."
        )
    if len(set(order)) != len(order):
        raise ValueError("Danh sách thứ tự mới có tên file trùng nhau.")
    for name, parsed in on_disk.items():
        if (parsed.ma_dinh_danh, parsed.ma_phong, parsed.muc_luc,
                parsed.ho_so) != (
                folder_codes.ma_dinh_danh, folder_codes.ma_phong,
                folder_codes.muc_luc, folder_codes.ho_so):
            raise ValueError(
                f"File {name} có mã khác với tên hồ sơ {ho_so_dir.name}."
            )

    width = max(3, max(len(p.stt) for p in on_disk.values()),
                len(str(len(order))))
    staging = ho_so_dir / f".reorder-{uuid.uuid4().hex[:8]}"
    changed: list[tuple[str, str]] = []
    for k, old_name in enumerate(order, start=1):
        parsed = on_disk[old_name]
        new_name = PdfName(
            ma_dinh_danh=parsed.ma_dinh_danh, ma_phong=parsed.ma_phong,
            muc_luc=parsed.muc_luc, ho_so=parsed.ho_so,
            stt=str(k).zfill(width), ext=parsed.ext,
        ).compose()
        if new_name != old_name:
            changed.append((old_name, new_name))

    plan = RenamePlan(
        root=root, folder_rel=rel, level=Level.HO_SO,
        old_name=ho_so_dir.name, new_name=ho_so_dir.name,
        title=f"xếp lại thứ tự {len(changed)} PDF trong "
              f'"{ho_so_dir.name}"',
        staging_dir=staging if changed else None,
        kind="pdf_reorder",
    )
    # Phase 1: đưa các file đổi tên vào staging; phase 2: đặt tên cuối.
    for old_name, _ in changed:
        plan.ops.append(RenameOp(
            src=ho_so_dir / old_name, dst=staging / old_name, is_dir=False,
        ))
    for old_name, new_name in changed:
        plan.ops.append(RenameOp(
            src=staging / old_name, dst=ho_so_dir / new_name, is_dir=False,
        ))
    rel_prefix = rel.as_posix()
    plan.path_map = {
        f"{rel_prefix}/{old}": f"{rel_prefix}/{new}"
        for old, new in changed
    }
    return plan


def plan_pdf_move(root: Path, pdf_rels, target_ho_so_rel: Path) -> RenamePlan:
    """Chuyển (nhóm) PDF sang hồ sơ khác — GIỮ số thứ tự, KHÔNG đánh lại số.

    Tên file mới được dựng lại đúng bộ mã của hồ sơ đích nhưng **giữ nguyên
    NNN cũ** (thứ tự trang do người dùng sắp bằng kéo-thả; chỉ đổi khi chủ
    động "đánh số từ tài liệu này" ở menu chuột phải). NNN bị trùng tại đích
    hoặc PDF lệch tên → nhận số kế tiếp còn tự do. File khác của hồ sơ đích
    KHÔNG bị đổi tên. Nhóm giữ thứ tự đưa vào (thứ tự đang chọn).
    """
    root = Path(root).resolve()
    tgt = Path(target_ho_so_rel)
    if not tgt.parts or tgt.is_absolute():
        raise ValueError("Hồ sơ đích: đường dẫn không hợp lệ.")
    if level_of(tgt) is not Level.HO_SO:
        raise ValueError("PDF chỉ chuyển được vào một hồ sơ (cấp 4 của cây).")
    target_dir = root.joinpath(*tgt.parts)
    if not target_dir.is_dir():
        raise ValueError(f"Thư mục hồ sơ đích không tồn tại: {target_dir}")
    tgt_codes = parse_dossier_folder_name(target_dir.name)
    if tgt_codes is None:
        raise ValueError(
            f"Tên hồ sơ đích không khớp quy ước 4 đoạn: {target_dir.name}"
        )

    group: list[Path] = []
    seen: set[Path] = set()
    for rel in pdf_rels:
        r = Path(rel)
        if not r.parts or r.is_absolute():
            raise ValueError(f"File cần chuyển: đường dẫn không hợp lệ.")
        if level_of(r.parent) is not Level.HO_SO:
            raise ValueError(f"File không nằm trong một hồ sơ ở cấp 4: {r}")
        src = root.joinpath(*r.parts)
        if src in seen:
            continue
        seen.add(src)
        if not src.is_file():
            raise ValueError(f"File không tồn tại: {src}")
        if src.suffix.lower() != ".pdf":
            raise ValueError(f"Chỉ chuyển được file PDF (.pdf): {r.name}")
        group.append(src)
    if not group:
        raise ValueError("Không có file nào để chuyển.")

    # NNN đã bị chiếm tại đích (chỉ tính PDF khớp mã đích, trừ file nhóm).
    used: set[str] = set()
    width = 3
    tgt_seg = (tgt_codes.ma_dinh_danh, tgt_codes.ma_phong,
               tgt_codes.muc_luc, tgt_codes.ho_so)
    for entry in sorted(target_dir.iterdir()):
        if not entry.is_file() or entry in seen:
            continue
        q = parse_pdf_name(entry.name)
        if q is not None and (q.ma_dinh_danh, q.ma_phong, q.muc_luc,
                              q.ho_so) == tgt_seg:
            used.add(q.stt)
            width = max(width, len(q.stt))

    plan = RenamePlan(root=root, folder_rel=tgt, level=Level.HO_SO,
                      old_name=group[0].name, new_name=group[0].name,
                      kind="pdf_move", target_parent_rel=tgt)
    for src in group:
        parsed = parse_pdf_name(src.name)
        if parsed is not None and parsed.stt not in used:
            stt = parsed.stt  # giữ số trang cũ
        else:
            stt = _next_free_number(used, width)
        used.add(stt)
        final = PdfName(
            ma_dinh_danh=tgt_codes.ma_dinh_danh, ma_phong=tgt_codes.ma_phong,
            muc_luc=tgt_codes.muc_luc, ho_so=tgt_codes.ho_so,
            stt=stt, ext=src.suffix,
        ).compose()
        dst = target_dir / final
        if dst == src:
            continue  # đã đúng vị trí + đúng tên
        if dst.exists():
            raise ValueError(f"Đã có sẵn \"{final}\" trong hồ sơ đích.")
        plan.ops.append(RenameOp(src=src, dst=dst, is_dir=False))

    n = len(group)
    plan.title = (f'chuyển "{group[0].name}" vào "{tgt.name}"'
                  if n == 1 else
                  f"chuyển {n} file PDF vào \"{tgt.name}\"")
    _validate_ops(plan)
    plan.path_map = _build_path_map(plan)
    return plan


def plan_pdf_rename_stt(root: Path, pdf_rel: Path, new_stt: str) -> RenamePlan:
    """Đổi số thứ tự (NNN) của một tài liệu PDF — bắt buộc đúng 3 chữ số.

    Chỉ đoạn NNN trong tên file thay đổi; các đoạn mã cha giữ nguyên. Raise
    ``ValueError`` nếu sai định dạng, file lệch tên, hoặc số mới bị trùng.
    """
    root = Path(root).resolve()
    rel = Path(pdf_rel)
    if not rel.parts or rel.is_absolute():
        raise ValueError("Đường dẫn không hợp lệ.")
    if level_of(rel.parent) is not Level.HO_SO:
        raise ValueError("Tài liệu phải nằm trong một hồ sơ ở cấp 4 của cây.")
    src = root.joinpath(*rel.parts)
    if not src.is_file() or src.suffix.lower() != ".pdf":
        raise ValueError(f"Không phải tài liệu PDF: {rel.name}")
    parsed = parse_pdf_name(src.name)
    if parsed is None:
        raise ValueError(
            f"Tên tài liệu không khớp quy ước 5 đoạn nên không đổi được: "
            f"{src.name}"
        )
    stt = str(new_stt or "").strip()
    if not stt.isdigit() or len(stt) != PDF_STT_WIDTH:
        raise ValueError(
            f"Số thứ tự tài liệu bắt buộc phải là đúng {PDF_STT_WIDTH} chữ số "
            f"(ví dụ: {'0' * (PDF_STT_WIDTH - 1)}1) — nhận được: {stt!r}"
        )
    final = PdfName(
        ma_dinh_danh=parsed.ma_dinh_danh, ma_phong=parsed.ma_phong,
        muc_luc=parsed.muc_luc, ho_so=parsed.ho_so, stt=stt, ext=parsed.ext,
    ).compose()
    plan = RenamePlan(root=root, folder_rel=rel.parent, level=Level.HO_SO,
                      old_name=src.name, new_name=final, kind="rename",
                      title=f'đổi số thứ tự tài liệu "{src.name}" → "{final}"')
    if final != src.name:
        dst = src.parent / final
        if dst.exists():
            raise ValueError(f"Đã có sẵn \"{final}\" trong hồ sơ này.")
        plan.ops.append(RenameOp(src=src, dst=dst, is_dir=False))
    plan.path_map = _build_path_map(plan)
    return plan


def _validate_ops(plan: RenamePlan) -> None:
    """Pre-flight: phát hiện xung đột tên đích trước khi thực thi bất kỳ op nào."""
    seen_dst: dict[str, Path] = {}
    for op in plan.ops:
        key = str(op.dst).lower()
        if key in seen_dst:
            raise ValueError(
                f"Hai mục cùng đổi về một tên: {seen_dst[key]} và {op.src}"
            )
        seen_dst[key] = op.dst

    for op in plan.ops:
        if op.dst == op.src:
            continue
        if op.dst.exists():
            if op.dst.name.lower() == op.src.name.lower() \
                    and op.dst.parent == op.src.parent:
                op.needs_stage = True  # chỉ khác hoa/thường
            else:
                raise ValueError(
                    f"Đã có sẵn \"{op.dst.name}\" trong thư mục đích — đổi tên "
                    f"sẽ gây trùng. Hãy đổi tên mục kia trước hoặc chọn mã khác."
                )


def item_journeys(plan: RenamePlan) -> list[tuple[Path, Path, bool]]:
    """(path gốc, path cuối, là thư mục) của MỌI mục bị di chuyển trong plan.

    Áp lần lượt từng op theo thứ tự thực thi, cập nhật vị trí hiện tại của
    từng path gốc (op cha di chuyển thì con cháu theo prefix) — an toàn với
    staging 2 pha và cả hoán đổi tên (a→b, b→a) vì không truy đệ quy chuỗi.
    """
    current_to_orig: dict[Path, Path] = {}   # path hiện tại → path gốc
    orig_info: dict[Path, tuple[Path, bool]] = {}  # gốc → (hiện tại, là thư mục)

    for op in plan.ops:
        if op.src == op.dst:
            continue
        orig = current_to_orig.pop(op.src, None)
        if orig is None:
            orig, is_dir = op.src, op.is_dir  # lần đầu đi chuyển: src là gốc
        else:
            is_dir = orig_info[orig][1]
        # Op cha di chuyển → mọi gốc đang nằm dưới nó dời theo prefix.
        for other, (cur, d) in list(orig_info.items()):
            if other == orig:
                continue
            if op.src == cur or op.src in cur.parents:
                new_cur = op.dst / cur.relative_to(op.src)
                current_to_orig.pop(cur, None)
                orig_info[other] = (new_cur, d)
                current_to_orig[new_cur] = other
        orig_info[orig] = (op.dst, is_dir)
        current_to_orig[op.dst] = orig

    return [(o, cur, d) for o, (cur, d) in orig_info.items() if o != cur]


def _build_path_map(plan: RenamePlan) -> dict[str, str]:
    """Map path cũ → mới cho mọi op (dạng str, "/" phân cách)."""
    def rel_key(p: Path) -> str:
        try:
            return p.relative_to(plan.root).as_posix()
        except ValueError:
            return p.as_posix()

    return {rel_key(o): rel_key(c) for o, c, _d in item_journeys(plan)}


def plan_invert(plan: RenamePlan, title: str) -> RenamePlan:
    """Kế hoạch hoàn tác của một plan ĐÃ thực thi thành công.

    Đơn giản là đảo ngược thứ tự op và hoán đổi src↔dst: chạy ngược từ trạng
    thái cuối về trạng thái đầu (cùng nguyên tắc với rollback khi lỗi giữa
    chừng). Op từng đi qua staging thì hoàn tác cũng đi qua lại đúng staging
    đó (được tạo lại lúc thực thi và dọn sạch sau). Nếu cây đã đổi khác từ
    lúc thực thi, op thất bại → execute_plan rollback + báo lỗi.
    """
    inv = RenamePlan(
        root=plan.root, folder_rel=plan.folder_rel, level=plan.level,
        old_name=plan.new_name, new_name=plan.old_name,
        kind="invert", title=title,
        staging_dir=plan.staging_dir,
    )
    inv.ops = [
        RenameOp(src=op.dst, dst=op.src, is_dir=op.is_dir,
                 needs_stage=op.needs_stage)
        for op in reversed(plan.ops)
    ]
    inv.path_map = _build_path_map(inv)
    return inv


def map_path(plan: RenamePlan, path: str | Path) -> str:
    """Đổi một đường dẫn (posix, tương đối root; tuyệt đối cũng được) theo kế hoạch.

    Dùng longest-prefix match trên path_map để cả những file không bị đổi tên
    nhưng nằm trong thư mục bị đổi cũng ra được đường dẫn mới. Trả về cùng
    dạng (tương đối/tuyệt đối) như đường dẫn đưa vào.
    """
    p = Path(path)
    absolute = p.is_absolute()
    if absolute:
        try:
            p = p.relative_to(plan.root)
        except ValueError:
            return p.as_posix()
    key = p.as_posix()
    best = None
    for old, new in plan.path_map.items():
        if key == old or key.startswith(old + "/"):
            if best is None or len(old) > len(best[0]):
                best = (old, new)
    if best is None:
        mapped = key
    else:
        old, new = best
        mapped = new + key[len(old):]
    return (plan.root / mapped).as_posix() if absolute else mapped


# --------------------------------------------------------------------------
# Thực thi
# --------------------------------------------------------------------------

@dataclass
class ExecuteResult:
    done_ops: int = 0
    rolled_back: bool = False
    error: str = ""


def execute_plan(plan: RenamePlan, *,
                 progress_cb=None, cancel_cb=None) -> ExecuteResult:
    """Thực thi kế hoạch theo đúng thứ tự op; rollback best-effort khi lỗi.

    ``progress_cb(done, total)`` và ``cancel_cb() -> bool`` (trả True để hủy)
    được gọi giữa các op — an toàn vì mỗi op là một rename nguyên tử.
    """
    total = len(plan.ops)
    # Chốt cửa sổ hở giữa lúc lập kế hoạch và lúc chạy: _validate_ops chỉ
    # nhìn thấy đĩa tại thời điểm lập kế hoạch; nếu sau đó ai đó tạo file/
    # thư mục trùng tên đích, shutil.move trên Windows sẽ COPY ĐÈ lên file
    # có sẵn thay vì báo lỗi (os.rename hỏng thì rơi nhánh copy+delete).
    # Đích trùng với một src khác của chính kế hoạch là hợp lệ — two-phase
    # dời nó vào staging trước; needs_stage (chỉ khác hoa/thường) cũng vậy.
    sources = {op.src for op in plan.ops}
    for op in plan.ops:
        if op.dst == op.src or op.needs_stage or op.dst in sources:
            continue
        if op.dst.exists():
            return ExecuteResult(
                error=f'Đã có sẵn "{op.dst.name}" trong '
                      f'"{op.dst.parent.name or op.dst.parent}" — không thực '
                      "thi để tránh ghi đè. Hãy làm mới cây rồi thử lại.")
    if plan.staging_dir is not None and total:
        try:
            plan.staging_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ExecuteResult(
                rolled_back=True,
                error=f"Không tạo được thư mục tạm: {exc}",
            )
    completed: list[RenameOp] = []
    for idx, op in enumerate(plan.ops):
        if cancel_cb is not None and cancel_cb():
            failed = _rollback(completed)
            _cleanup_staging(plan)
            return ExecuteResult(
                rolled_back=True,
                error="Đã hủy. Các đổi tên đã thực hiện được lùi về như cũ."
                      + _rollback_note(failed),
            )
        staged: Path | None = None
        try:
            if op.needs_stage:
                staged = op.src.parent / f".rename-{uuid.uuid4().hex[:8]}"
                _move_path_with_retry(op.src, staged)
                _move_path_with_retry(staged, op.dst)
                staged = None
            else:
                _move_path_with_retry(op.src, op.dst)
            completed.append(op)
        except OSError as exc:
            if staged is not None:
                try:
                    _move_path_with_retry(staged, op.src, attempts=4)
                except OSError:
                    pass
            failed = _rollback(completed)
            _cleanup_staging(plan)
            if failed:
                undone = (f"Đã lùi về như cũ một phần "
                          f"({len(completed) - len(failed)}/{len(completed)} "
                          f"op đã làm)")
            else:
                undone = (f"Đã lùi về như cũ "
                          f"({len(completed)}/{total} op đã làm)")
            return ExecuteResult(
                rolled_back=True,
                error=f"Lỗi khi đổi tên {op.src}: {exc}\n"
                      f"{undone}." + _rollback_note(failed),
            )
        if progress_cb is not None:
            progress_cb(idx + 1, total)
    _cleanup_staging(plan)
    return ExecuteResult(done_ops=total)


def _rollback(completed: list[RenameOp]) -> list[Path]:
    """Best-effort hoàn tác các op đã làm theo thứ tự ngược.

    Trả về danh sách path đích KHÔNG khôi phục được (thường do file đang
    bị chương trình khác giữ) để người dùng biết cây còn lệch.
    """
    failed: list[Path] = []
    for op in reversed(completed):
        try:
            _move_path_with_retry(op.dst, op.src, attempts=4, delay=0.05)
        except OSError:
            failed.append(op.dst)
    return failed


def _rollback_note(failed: list[Path]) -> str:
    if not failed:
        return ""
    names = ", ".join(p.name for p in failed[:5])
    more = f" … (+{len(failed) - 5} mục)" if len(failed) > 5 else ""
    return (
        f"\nCẢNH BÁO: không khôi phục được {len(failed)} mục: {names}{more}. "
        "Các file này còn giữ tên MỚI — hãy đóng chương trình đang mở chúng "
        "(Viewer/Explorer) rồi thực hiện lại thao tác."
    )


def _cleanup_staging(plan: RenamePlan) -> None:
    """Best-effort dọn thư mục staging (chỉ xóa được khi rỗng)."""
    if plan.staging_dir is None:
        return
    try:
        plan.staging_dir.rmdir()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Thống kê nhanh cho UI
# --------------------------------------------------------------------------

def scan_stats(root: Path) -> dict[str, int]:
    """Đếm số mã định danh / phông / hồ sơ / PDF để hiển thị thanh trạng thái."""
    stats = {"mdd": 0, "phong": 0, "ho_so": 0, "pdf": 0}
    root = Path(root)
    if not root.is_dir():
        return stats
    for mdd in root.iterdir():
        if not mdd.is_dir():
            continue
        stats["mdd"] += 1
        for phong in mdd.iterdir():
            if not phong.is_dir():
                continue
            stats["phong"] += 1
            for ml in phong.iterdir():
                if not ml.is_dir():
                    continue
                for ho_so in ml.iterdir():
                    if not ho_so.is_dir():
                        continue
                    stats["ho_so"] += 1
                    for f in ho_so.iterdir():
                        if f.is_file() and f.suffix.lower() == ".pdf":
                            stats["pdf"] += 1
    return stats
