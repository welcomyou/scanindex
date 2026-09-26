"""Tests nghiệm thu cho resume theo version (review R1–R8).

Bộ test này khớp các tiêu chí nghiệm thu trong REVIEW_secret_scan_versioned_resume.md:
1. Journal v2: av/mig/rs/rpf replay, compact giữ state, torn-tail giữ kết
   quả cũ HOẶC mới nguyên vẹn (R2).
2. Registry: bump_versions_exact + snapshot lưu bền vững qua close/reload
   (R3), kế thừa entry thiếu (R4) với provenance.
3. Worker: 3 chế độ RESTART/RESUME/RESUME_RESCAN — số file quét đúng, bỏ
   qua cache khi restart, dedupe task, hai pha có rào (R5/R6), commit thay
   thế nguyên vẹn, lỗi giữ kết quả cũ (R2/R8).
4. Excel round-trip: file 8 cột cũ nạp rồi xuất 9 cột không mâu thuẫn.
5. Benchmark migration 300k entry (không OCR).
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

pytest.importorskip("openpyxl")
pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from scanindex.core import secret_scan_progress as ssp  # noqa: E402
from scanindex.ui.screens import secret_file_scan_screen as sfss  # noqa: E402
from scanindex.ui.screens.secret_file_scan_screen import (  # noqa: E402
    ResumeDecision,
    SecretFileScanScreen,
    SecretScanMatch,
    _declass_cell_text,
    export_matches_to_excel,
    load_secret_matches_from_excel,
)

NEW_VERSION = "test-9.9.9"
OLD_VERSION = "test-0.9.0"


@pytest.fixture(autouse=True)
def _progress_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ssp, "progress_dir", lambda: str(tmp_path / "scan_progress")
    )
    return tmp_path / "scan_progress"


@pytest.fixture(autouse=True)
def _pinned_version(monkeypatch):
    monkeypatch.setattr(
        "scanindex.infra.version.get_version", lambda: NEW_VERSION
    )


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------
# 1. Journal v2 — replay/compact/torn-tail (R1/R2)
# ---------------------------------------------------------------------------

def _old_match(source_path: str, keyword: str = "MẬT") -> dict:
    return {
        "source_path": source_path,
        "relative_path": os.path.basename(source_path),
        "keyword": keyword,
        "page_number": 1,
        "mode": "Trang đầu",
        "ocr_pdf_path": "",
        "note": "PDF gốc; trang đầu",
    }


def test_journal_roundtrip_state_events(tmp_path) -> None:
    folder = str(tmp_path / "kho")
    os.makedirs(folder, exist_ok=True)
    prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
    prog.record_file("a.pdf", "ok")
    prog.record_matches([_old_match(os.path.join(folder, "a.pdf"))])
    prog.begin_migration(NEW_VERSION, 3)
    prog.set_rescan_pending(["a.pdf"])
    prog.save()
    prog.close()

    loaded = ssp.SecretScanProgress.load(folder, "fast")
    assert loaded is not None
    assert loaded.app_version == OLD_VERSION
    assert loaded.migration == {"target": NEW_VERSION, "choice": 3}
    assert loaded.rescan_pending == {"a.pdf"}
    assert loaded.done_files() == {"a.pdf"}
    assert len(loaded.matches) == 1
    assert loaded.needs_continue() is True


def test_journal_rpf_commit_and_compact(tmp_path) -> None:
    folder = str(tmp_path / "kho")
    os.makedirs(folder, exist_ok=True)
    a = os.path.join(folder, "a.pdf")
    prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
    prog.record_file("a.pdf", "ok")
    prog.record_matches([_old_match(a), _old_match(a + ":p2", keyword="MẬT")])
    prog.begin_migration(NEW_VERSION, 3)
    prog.set_rescan_pending(["a.pdf"])

    new_dicts = [
        {
            "source_path": a,
            "relative_path": "a.pdf",
            "keyword": "TUYỆT MẬT",
            "page_number": 1,
            "mode": "Trang đầu",
            "ocr_pdf_path": "",
            "note": "mới",
            "issue_year": 1996,
            "declass_years": 30,
            "declass_due": True,
            "source_version": NEW_VERSION,
        }
    ]
    prog.commit_replace_file("a.pdf", ssp._norm_path(a), new_dicts)
    prog.accept_version(NEW_VERSION)
    prog.finish_migration()
    prog.save()
    prog._compact()  # ép compact — state phải sống sót
    prog.close()

    loaded = ssp.SecretScanProgress.load(folder, "fast")
    assert loaded.app_version == NEW_VERSION
    assert loaded.migration is None
    assert loaded.rescan_pending == set()          # đã commit → bỏ khỏi tập
    assert loaded.needs_continue() is False
    # 2 dòng cũ của file a bị thay bằng 1 dòng mới; dòng KHÁC file giữ nguyên.
    keywords = sorted(d["keyword"] for d in loaded.matches)
    assert keywords == ["MẬT", "TUYỆT MẬT"]
    new_row = next(d for d in loaded.matches if d["keyword"] == "TUYỆT MẬT")
    assert new_row["issue_year"] == 1996 and new_row["declass_due"] is True


def test_journal_torn_rpf_keeps_old_result_wholly(tmp_path) -> None:
    """Mất điện giữa chừng dòng rpf: replay cắt đuôi hỏng → kết quả CŨ còn
    nguyên vẹn và file vẫn nằm trong tập quét lại (R2)."""
    folder = str(tmp_path / "kho")
    os.makedirs(folder, exist_ok=True)
    a = os.path.join(folder, "a.pdf")
    prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
    prog.record_file("a.pdf", "ok")
    prog.record_matches([_old_match(a)])
    prog.set_rescan_pending(["a.pdf"])
    prog.save()
    prog.close()

    # Giả lập dòng rpf bị cắt dở (chỉ nửa dòng ghi xuống đĩa).
    path = ssp.journal_path(folder, "fast")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": "rpf", "p": "a.pdf", "a": ssp._norm_path(a)})[:-8])

    loaded = ssp.SecretScanProgress.load(folder, "fast")
    assert loaded is not None
    assert len(loaded.matches) == 1                 # kết quả cũ nguyên vẹn
    assert loaded.matches[0]["keyword"] == "MẬT"
    assert loaded.rescan_pending == {"a.pdf"}       # vẫn chờ quét lại


def test_journal_legacy_v1_is_loaded(tmp_path) -> None:
    """Journal v1 của bản phát hành trước vẫn đọc được (av rỗng → coi là
    khác version → hộp thoại 3 lựa chọn)."""
    folder = str(tmp_path / "kho")
    os.makedirs(folder, exist_ok=True)
    legacy = ssp.legacy_journal_path(folder, "fast")
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    with open(legacy, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"t": "h", "v": 1, "folder": folder, "mode": "fast"}) + "\n")
        f.write(json.dumps({"t": "f", "p": "a.pdf", "s": "ok"}) + "\n")
        f.write(json.dumps({"t": "m", "d": _old_match(os.path.join(folder, "a.pdf"))}) + "\n")

    prog = ssp.SecretScanProgress.load(folder, "fast")
    assert prog is not None
    assert prog.app_version == ""
    assert prog.done_files() == {"a.pdf"}
    assert len(prog.matches) == 1
    # Tạo mới phải dọn cả journal v1 lẫn v2 (ghi đè v2 mới).
    ssp.SecretScanProgress.create(folder, "fast", NEW_VERSION)
    assert not os.path.exists(legacy)
    prog2 = ssp.SecretScanProgress.load(folder, "fast")
    assert prog2 is not None and prog2.app_version == NEW_VERSION


# ---------------------------------------------------------------------------
# 2. Registry — bump bền vững + kế thừa (R3/R4)
# ---------------------------------------------------------------------------

def test_registry_bump_persists_after_reload(tmp_path) -> None:
    """Repro R3 của review: sửa _entries rồi save() KHÔNG lưu — phải dùng
    snapshot(); đóng/mở lại phải thấy version mới."""
    reg = ssp.FileRegistry()
    path = str(tmp_path / "a.pdf")
    open(path, "wb").write(b"x" * 5)
    st = os.stat(path)
    reg.record(path, st.st_size, st.st_mtime, "fast", OLD_VERSION, [])
    reg.close()

    reg2 = ssp.FileRegistry.load()
    changed = reg2.bump_versions_exact([path], "fast", NEW_VERSION)
    reg2.snapshot()
    reg2.close()
    assert changed == 1

    reg3 = ssp.FileRegistry.load()
    cached = reg3.lookup(path, st.st_size, st.st_mtime, "fast", NEW_VERSION)
    assert cached == []
    assert reg3.lookup(path, st.st_size, st.st_mtime, "fast", OLD_VERSION) is None
    reg3.close()


def test_registry_inherited_provenance_persists(tmp_path) -> None:
    reg = ssp.FileRegistry()
    path = str(tmp_path / "inh.pdf")
    open(path, "wb").write(b"x" * 7)
    st = os.stat(path)
    reg.record(path, st.st_size, st.st_mtime, "fast", NEW_VERSION, [], inherited=True)
    reg.snapshot()
    reg.close()

    reg2 = ssp.FileRegistry.load()
    assert reg2.has_entry(path, "fast")
    assert reg2.inherited_count() == 1
    # Ghi đè bằng kết quả quét thật → hết cờ kế thừa.
    reg2.record(path, st.st_size, st.st_mtime, "fast", NEW_VERSION, [])
    reg2.snapshot()
    reg2.close()
    assert ssp.FileRegistry.load().inherited_count() == 0


def test_registry_bump_exact_keys_only(tmp_path) -> None:
    reg = ssp.FileRegistry()
    in_folder = str(tmp_path / "in.pdf")
    sibling = str(tmp_path.parent / "sibling.pdf")  # thư mục anh em
    for p in (in_folder, sibling):
        open(p, "wb").write(b"x")
    for p in (in_folder, sibling):
        st = os.stat(p)
        reg.record(p, st.st_size, st.st_mtime, "fast", OLD_VERSION, [])
    reg.bump_versions_exact([in_folder], "fast", NEW_VERSION)
    reg.snapshot()
    reg.close()

    reg2 = ssp.FileRegistry.load()
    st_in = os.stat(in_folder)
    st_sib = os.stat(sibling)
    assert reg2.lookup(in_folder, st_in.st_size, st_in.st_mtime, "fast", NEW_VERSION) == []
    assert reg2.lookup(sibling, st_sib.st_size, st_sib.st_mtime, "fast", OLD_VERSION) == []
    reg2.close()


# ---------------------------------------------------------------------------
# 3. Hộp thoại — hàm quyết định thuần (tiêu chí 8)
# ---------------------------------------------------------------------------

def test_resume_dialog_kind() -> None:
    kind = SecretFileScanScreen._resume_dialog_kind
    assert kind("", NEW_VERSION, needs_continue=False) == "legacy3"
    assert kind(OLD_VERSION, NEW_VERSION, needs_continue=False) == "legacy3"
    assert kind(NEW_VERSION, NEW_VERSION, needs_continue=False) == "normal"
    assert kind(NEW_VERSION, NEW_VERSION, needs_continue=True) == "continue"
    assert kind("", NEW_VERSION, needs_continue=True) == "continue"


def test_rescan_targets_uses_normalized_source_path(tmp_path) -> None:
    """R6: định danh theo source_path tuyệt đối đã chuẩn hóa, lọc trong root."""
    folder = tmp_path / "kho"
    sub = folder / "hop1"
    sub.mkdir(parents=True)
    f1 = sub / "bi-mat.pdf"
    f1.write_bytes(b"x")
    outside = tmp_path / "ngoai.pdf"
    outside.write_bytes(b"x")

    class _P:  # stand-in prog
        matches = [
            _old_match(str(f1)),                       # trong root
            _old_match(str(outside)),                   # ngoài root
            _old_match(str(f1), keyword="TUYỆT MẬT"),   # trùng file
        ]

    targets = SecretFileScanScreen._rescan_targets(_P, str(folder))
    assert targets == [ssp._norm_path(str(f1))]


# ---------------------------------------------------------------------------
# 4. Worker tích hợp (mock bộ quét — tiêu chí 1/2/3/5/6)
# ---------------------------------------------------------------------------

def _mk_tree(tmp_path, n: int = 5):
    folder = tmp_path / "kho"
    folder.mkdir(exist_ok=True)
    paths = []
    for i in range(n):
        p = folder / f"f{i}.pdf"
        p.write_bytes(b"%PDF-1.4 fake")
        paths.append(str(p))
    return str(folder), paths


def _seed_journal(folder, paths, old_results: dict[str, list[dict]], err=None, pending=()):
    """Journal 'bản cũ': av=OLD_VERSION; file ok/err theo tham số; file trong
    ``pending`` KHÔNG ghi gì (chưa quét — sẽ nằm ở hàng đợi của lượt resume)."""
    prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
    for p in paths:
        if p in pending:
            continue
        rel = os.path.basename(p)
        if p == err:
            prog.record_file(rel, "err", "lỗi cũ")
        else:
            prog.record_file(rel, "ok")
        for d in old_results.get(p, []):
            prog.record_matches([d])
    prog.save()
    prog.close()
    return prog


def _screen(qapp) -> SecretFileScanScreen:
    return SecretFileScanScreen()


def _run_sync(screen, folder, decision):
    screen._busy = True
    screen._cancel_event.clear()
    screen._run_worker(folder, True, decision)
    # Worker chạy đồng bộ nhưng signal phát từ thread pool → queued; pump
    # event loop như app thật trước khi assert trạng thái bảng.
    from PySide6.QtCore import QCoreApplication

    for _ in range(200):
        QCoreApplication.processEvents()


def _restore_rows(screen, prog, folder):
    for d in prog.matches:
        screen._add_result(SecretScanMatch(**d))


def test_worker_resume_rescan_end_to_end(qapp, tmp_path) -> None:
    """Tiêu chí 1 (9 file giả lập theo tỷ lệ 5/2/2) + 5 + 2 (R8)."""
    folder, paths = _mk_tree(tmp_path, 5)
    f0, f1, f2, f3, f4 = paths
    # Bản cũ: f0, f1 là mật; f2 sạch (ok); f3 chưa quét (pending); f4 lỗi.
    old = {
        f0: [_old_match(f0), _old_match(f0, keyword="TỐI MẬT")],
        f1: [_old_match(f1)],  # sẽ bị thay bằng kết quả RỖNG (FP rớt)
    }
    prog = _seed_journal(folder, paths, old, err=f4, pending=(f3,))

    calls = []

    def fake_scan(source_path, rel, file_work, first_page_only, cancel, log):
        calls.append(source_path)
        if source_path == f0:
            m = SecretScanMatch(
                source_path=f0, relative_path=rel, keyword="MẬT", page_number=1,
                mode="Trang đầu", ocr_pdf_path="", note="mới",
                issue_year=2016, declass_years=10, declass_due=True,
                source_version=NEW_VERSION,
            )
            return [m]
        return []  # f1 giờ là FP (rớt), f2/f3/f4 sạch

    monkeypatch_holder = pytest.MonkeyPatch()
    monkeypatch_holder.setattr(sfss, "scan_one_file_for_secret", fake_scan)
    try:
        screen = _screen(qapp)
        _restore_rows(screen, prog, folder)
        assert screen.table.rowCount() == 3  # 2 dòng f0 + 1 dòng f1

        decision = ResumeDecision(
            ResumeDecision.RESUME_RESCAN,
            prog=ssp.SecretScanProgress.load(folder, "fast"),
            rescan_abs=[ssp._norm_path(f0), ssp._norm_path(f1)],
        )
        _run_sync(screen, folder, decision)

        # f3 (pending) + f4 (err retry) + f0, f1 (rescan) — f2 kế thừa không quét.
        assert sorted(calls) == sorted([f0, f1, f3, f4])
        # f0: 2 dòng cũ thay bằng 1 dòng mới; f1: FP rớt.
        matches = screen._current_matches()
        by_file = {}
        for m in matches:
            by_file.setdefault(ssp._norm_path(m.source_path), []).append(m)
        assert len(by_file.get(ssp._norm_path(f0), [])) == 1
        assert ssp._norm_path(f1) not in by_file
        new_m = by_file[ssp._norm_path(f0)][0]
        assert new_m.declass_due is True and new_m.source_version == NEW_VERSION
        # Hoàn tất sạch → journal bị xóa, registry có đủ entry version mới.
        assert ssp.SecretScanProgress.load(folder, "fast") is None
        reg = ssp.FileRegistry.load()
        st = os.stat(f2)
        assert reg.lookup(f2, st.st_size, st.st_mtime, "fast", NEW_VERSION) == []
        assert reg.inherited_count() >= 1  # f2 kế thừa (thiếu entry từ journal cũ)
        reg.close()
    finally:
        monkeypatch_holder.undo()


def test_worker_restart_bypasses_cache(qapp, tmp_path) -> None:
    """R5: restart bắt buộc quét thật dù registry có entry cùng version."""
    folder, paths = _mk_tree(tmp_path, 2)
    f0, f1 = paths
    prog = _seed_journal(folder, paths, {})  # mọi file "đã quét" bản cũ
    prog.close()
    # Registry đã nâng sẵn lên version mới (giả lập đã chọn 2 lần trước).
    reg = ssp.FileRegistry.load()
    for p in paths:
        st = os.stat(p)
        reg.record(p, st.st_size, st.st_mtime, "fast", NEW_VERSION, [])
    reg.close()

    calls = []

    def fake_scan(*args):
        calls.append(args[0])
        return []

    mp = pytest.MonkeyPatch()
    mp.setattr(sfss, "scan_one_file_for_secret", fake_scan)
    try:
        screen = _screen(qapp)
        screen.history_checkbox.setChecked(True)
        _run_sync(screen, folder, ResumeDecision(ResumeDecision.RESTART))
        assert sorted(calls) == sorted(paths)  # không ăn cache
    finally:
        mp.undo()


def test_worker_rescan_failure_keeps_old_rows(qapp, tmp_path) -> None:
    """Tiêu chí 5: lỗi quét lại → giữ kết quả cũ, không trùng hàng."""
    folder, paths = _mk_tree(tmp_path, 2)
    f0, f1 = paths
    old = {f0: [_old_match(f0)]}
    prog = _seed_journal(folder, paths, old)

    def fake_scan(source_path, *args):
        if source_path == f0:
            raise RuntimeError("OCR hỏng")
        return []

    mp = pytest.MonkeyPatch()
    mp.setattr(sfss, "scan_one_file_for_secret", fake_scan)
    try:
        screen = _screen(qapp)
        _restore_rows(screen, prog, folder)
        decision = ResumeDecision(
            ResumeDecision.RESUME_RESCAN,
            prog=ssp.SecretScanProgress.load(folder, "fast"),
            rescan_abs=[ssp._norm_path(f0)],
        )
        _run_sync(screen, folder, decision)
        matches = screen._current_matches()
        assert [ssm.keyword for ssm in matches] == ["MẬT"]  # hàng cũ giữ nguyên
        # Còn lỗi → journal giữ, file f0 vẫn chờ quét lại.
        loaded = ssp.SecretScanProgress.load(folder, "fast")
        assert loaded is not None
        assert loaded.rescan_pending == {"f0.pdf"}
    finally:
        mp.undo()


def test_worker_cancel_mid_rescan_then_continue(qapp, tmp_path, monkeypatch) -> None:
    """Tiêu chí 3: dừng sau 1/2 file mật → resume cùng version chỉ quét phần
    còn lại, không quét lại file đã commit."""
    # Cửa sổ 1 task + 1 worker để thứ tự tất định: f0 xong mới tới f1.
    monkeypatch.setattr(sfss, "_SCAN_MAX_INFLIGHT_FILES", 1)
    monkeypatch.setenv("SECRET_SCAN_MAX_FILE_WORKERS", "1")
    folder, paths = _mk_tree(tmp_path, 3)
    f0, f1, f2 = paths
    old = {f0: [_old_match(f0)], f1: [_old_match(f1)]}
    _seed_journal(folder, paths, old, pending=(f2,))

    screen = _screen(qapp)

    def fake_scan(source_path, *args):
        if source_path == f0:
            # f0 vẫn hoàn tất và commit, nhưng sau nó người dùng bấm Dừng.
            screen._cancel_event.set()
            return [_m(f0, keyword="MẬT", source_version=NEW_VERSION)]
        return []

    monkeypatch.setattr(sfss, "scan_one_file_for_secret", fake_scan)
    decision = ResumeDecision(
        ResumeDecision.RESUME_RESCAN,
        prog=ssp.SecretScanProgress.load(folder, "fast"),
        rescan_abs=[ssp._norm_path(f0), ssp._norm_path(f1)],
    )
    _run_sync(screen, folder, decision)
    loaded = ssp.SecretScanProgress.load(folder, "fast")
    assert loaded is not None
    assert "f0.pdf" not in loaded.rescan_pending   # f0 đã commit
    assert loaded.rescan_pending == {"f1.pdf"}     # f1 còn chờ
    assert loaded.app_version == NEW_VERSION       # migration đã xong

    # Lần 2: tiếp tục — version khớp nhưng còn rescan_pending (tc 8).
    assert (
        SecretFileScanScreen._resume_dialog_kind(
            loaded.app_version, NEW_VERSION, loaded.needs_continue()
        )
        == "continue"
    )
    calls2 = []

    def fake_scan2(source_path, *args):
        calls2.append(source_path)
        return []

    monkeypatch.setattr(sfss, "scan_one_file_for_secret", fake_scan2)
    decision2 = ResumeDecision(ResumeDecision.RESUME_RESCAN, prog=loaded)
    _run_sync(screen, folder, decision2)
    # Chỉ f1 (rescan dở) + f2 (pending) — f0 không quét lại.
    assert sorted(calls2) == sorted([f1, f2])
    assert ssp.SecretScanProgress.load(folder, "fast") is None  # sạch


def test_worker_err_file_single_task(qapp, tmp_path) -> None:
    """Tiêu chí 6: file vừa có match cũ vừa err → đúng MỘT task quét lại."""
    folder, paths = _mk_tree(tmp_path, 2)
    f0, f1 = paths
    old = {f0: [_old_match(f0)]}
    prog = _seed_journal(folder, paths, old, err=f0)

    calls = []

    def fake_scan(source_path, *args):
        calls.append(source_path)
        return []

    mp = pytest.MonkeyPatch()
    mp.setattr(sfss, "scan_one_file_for_secret", fake_scan)
    try:
        screen = _screen(qapp)
        decision = ResumeDecision(
            ResumeDecision.RESUME_RESCAN,
            prog=ssp.SecretScanProgress.load(folder, "fast"),
            rescan_abs=[ssp._norm_path(f0)],
        )
        _run_sync(screen, folder, decision)
        assert calls.count(f0) == 1
    finally:
        mp.undo()


# ---------------------------------------------------------------------------
# 5. Excel round-trip (R8)
# ---------------------------------------------------------------------------

def _m(source_path, **kw):
    base = dict(
        source_path=source_path,
        relative_path=os.path.basename(source_path),
        keyword="MẬT",
        page_number=1,
        mode="Trang đầu",
        ocr_pdf_path="",
        note="PDF gốc; trang đầu",
    )
    base.update(kw)
    return SecretScanMatch(**base)


def test_excel_old_8col_file_roundtrip_consistent(tmp_path) -> None:
    """File 8 cột cũ (không có cột giải mật) → nạp → xuất 9 cột: dòng có
    ghi chú giải mật được dựng lại field, không mâu thuẫn biểu diễn."""
    import openpyxl

    src = str(tmp_path / "cu.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    headers = [
        "STT", "Độ mật", "Tên tệp", "File (trong thư mục quét)",
        "Đường dẫn đầy đủ", "Trang", "Chế độ", "Ghi chú",
    ]
    for c, h in enumerate(headers, start=1):
        ws.cell(row=1, column=c, value=h)
    ws.append([
        1, "MẬT", "a.pdf", "a.pdf", r"D:\kho\a.pdf", 1, "Trang đầu",
        "PDF gốc; trang đầu; Đáp ứng thời gian giải mật (văn bản 2016, MẬT 10 năm)",
    ])
    ws.append([2, "MẬT", "b.pdf", "b.pdf", r"D:\kho\b.pdf", 1, "Trang đầu", ""])
    wb.save(src)

    loaded = load_secret_matches_from_excel(src)
    assert len(loaded) == 2
    assert loaded[0].issue_year == 2016 and loaded[0].declass_due is True
    assert loaded[1].issue_year == 0 and loaded[1].source_version == ""

    dest = str(tmp_path / "moi.xlsx")
    export_matches_to_excel(loaded, dest)
    ws2 = openpyxl.load_workbook(dest).active
    assert ws2.cell(row=1, column=11).value == "Đáp ứng giải mật"
    assert ws2.cell(row=2, column=11).value == "Đáp ứng (2016, 10 năm)"
    # Chưa đạt (không dữ liệu) → ô trống.
    assert ws2.cell(row=3, column=11).value in ("", None)

    # Nạp lại file mới: field dựng đúng từ cột; dòng không dữ liệu → kế thừa.
    reloaded = load_secret_matches_from_excel(dest)
    assert reloaded[0].issue_year == 2016 and reloaded[0].declass_due is True
    assert reloaded[1].source_version == ""
    assert _declass_cell_text(reloaded[1], NEW_VERSION) == ""


# ---------------------------------------------------------------------------
# 6. Benchmark migration 300k entry (không OCR — tiêu chí 11)
# ---------------------------------------------------------------------------

def test_registry_bump_benchmark_300k(tmp_path) -> None:
    paths = [f"D:\\kho\\f{i:06d}.pdf" for i in range(300_000)]
    reg = ssp.FileRegistry()
    for i, p in enumerate(paths):
        reg._entries[reg._key(p, "fast")] = (1, 2.0, OLD_VERSION, [])
    reg._lines_at_compact = 1

    t0 = time.perf_counter()
    changed = reg.bump_versions_exact(paths, "fast", NEW_VERSION)
    reg.snapshot()
    bump_secs = time.perf_counter() - t0
    assert changed == 300_000

    t1 = time.perf_counter()
    reg2 = ssp.FileRegistry.load()
    load_secs = time.perf_counter() - t1
    sample = reg2._entries[reg2._key(paths[0], "fast")]
    assert sample[2] == NEW_VERSION
    reg2.close()

    # Giới hạn lỏng (máy CI chậm): bump+snapshot toàn cục dưới 60s.
    assert bump_secs < 60, f"bump+snapshot quá chậm: {bump_secs:.1f}s"
    assert load_secs < 30, f"reload quá chậm: {load_secs:.1f}s"


# ---------------------------------------------------------------------------
# 7. Bug thực tế: journal v1 resume mồ côi + rescan trượt chữ HOA/thường
# ---------------------------------------------------------------------------

def test_journal_legacy_v1_resume_appends_loadable(tmp_path) -> None:
    """Repro lỗi người dùng (1.1.9 → 1.1.11, dừng, mở lại): resume từ
    journal v1 của bản cũ rồi append tiến độ mới — lần load SAU phải thấy
    tiến độ MỚI, không rơi về state cũ của file v1."""
    folder = str(tmp_path / "kho")
    os.makedirs(folder, exist_ok=True)
    legacy = ssp.legacy_journal_path(folder, "fast")
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    with open(legacy, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"t": "h", "v": 1, "folder": folder, "mode": "fast"}) + "\n")
        for i in range(100):
            f.write(json.dumps({"t": "f", "p": f"f{i:03d}.pdf", "s": "ok"}) + "\n")
        f.write(
            json.dumps({"t": "m", "d": _old_match(os.path.join(folder, "f000.pdf"))})
            + "\n"
        )

    # Lượt 2 (bản mới): load journal cũ → nâng cấp + thay file mật + quét thêm.
    prog = ssp.SecretScanProgress.load(folder, "fast")
    assert prog is not None
    prog.begin_migration(NEW_VERSION, 3)
    prog.accept_version(NEW_VERSION)
    prog.finish_migration()
    prog.commit_replace_file(
        "f000.pdf", ssp._norm_path(os.path.join(folder, "f000.pdf")), []
    )
    for i in range(100, 150):
        prog.record_file(f"f{i:03d}.pdf", "ok")
    prog.save()
    prog.close()

    # Lượt 3: KHÔNG được rơi về journal v1 cũ (100 file, av rỗng).
    prog3 = ssp.SecretScanProgress.load(folder, "fast")
    assert prog3 is not None
    assert len(prog3.done_files()) == 150
    assert prog3.app_version == NEW_VERSION
    assert prog3.matches == []            # f000 đã thay bằng kết quả rỗng
    assert not os.path.exists(legacy)     # file v1 đã được gộp/xóa


def test_worker_rescan_mixed_case_filename(qapp, tmp_path, monkeypatch) -> None:
    """Repro: file tên có CHỮ HOA — rescan phải chạy THẬT (không bị coi là
    'không còn trong thư mục'), tập chờ sạch, journal hoàn tất bị xóa."""
    monkeypatch.setattr(sfss, "_SCAN_MAX_INFLIGHT_FILES", 1)
    monkeypatch.setenv("SECRET_SCAN_MAX_FILE_WORKERS", "1")
    folder = tmp_path / "Kho"
    folder.mkdir()
    fa = folder / "A06.35.25-CongVan.PDF"   # mật cũ — CHỮ HOA
    fb = folder / "B06.35.25-KeHoach.PDF"   # mật cũ — CHỮ HOA
    fc = folder / "c06.35.25-thuong.pdf"    # chưa quét
    for p in (fa, fb, fc):
        p.write_bytes(b"%PDF-1.4 fake")
    fa_s, fb_s, fc_s = str(fa), str(fb), str(fc)
    paths = [fa_s, fb_s, fc_s]

    old = {fa_s: [_old_match(fa_s)], fb_s: [_old_match(fb_s)]}
    _seed_journal(str(folder), paths, old, pending=(fc_s,))

    screen = _screen(qapp)
    calls = []

    def fake_scan(source_path, *args):
        calls.append(source_path)
        return []

    monkeypatch.setattr(sfss, "scan_one_file_for_secret", fake_scan)
    prog = ssp.SecretScanProgress.load(str(folder), "fast")
    decision = ResumeDecision(
        ResumeDecision.RESUME_RESCAN,
        prog=prog,
        rescan_abs=SecretFileScanScreen._rescan_targets(prog, str(folder)),
    )
    _run_sync(screen, str(folder), decision)

    # 2 file mật cũ được quét lại thật + 1 file pending — không file nào bị
    # đếm nhầm "không còn trong thư mục".
    assert sorted(calls) == sorted([fa_s, fb_s, fc_s])
    loaded = ssp.SecretScanProgress.load(str(folder), "fast")
    assert loaded is None                 # hoàn tất sạch — journal bị xóa
    assert screen.table.rowCount() == 0   # rescan trả rỗng → hàng cũ biến mất


def test_worker_rescan_heals_poisoned_pending(qapp, tmp_path, monkeypatch) -> None:
    """Journal từng bị ghi rs chữ THƯỜNG (bản lỗi normcase) kèm file mật cũ
    đã bị xóa khỏi đĩa: resume 'continue' phải quét lại đúng file còn, bỏ
    file mất khỏi tập chờ — không kẹt hộp thoại 'còn dở' vô hạn."""
    monkeypatch.setattr(sfss, "_SCAN_MAX_INFLIGHT_FILES", 1)
    monkeypatch.setenv("SECRET_SCAN_MAX_FILE_WORKERS", "1")
    folder = tmp_path / "Kho"
    folder.mkdir()
    fa = folder / "A06.35.25-CongVan.PDF"
    fb = folder / "DaXoa.PDF"
    fa_s, fb_s = str(fa), str(fb)
    for p in (fa, fb):
        p.write_bytes(b"%PDF-1.4 fake")

    folder_s = str(folder)
    prog = ssp.SecretScanProgress.create(folder_s, "fast", NEW_VERSION)
    prog.record_file("A06.35.25-CongVan.PDF", "ok")
    prog.record_matches([_old_match(fa_s)])
    prog.record_file("DaXoa.PDF", "ok")
    prog.record_matches([_old_match(fb_s)])
    # Giả lập journal do bản lỗi chuẩn hóa ghi: rel chữ thường.
    prog.set_rescan_pending(["a06.35.25-congvan.pdf", "daxoa.pdf"])
    prog.save()
    prog.close()
    os.remove(fb_s)  # file mật cũ bị xóa khỏi đĩa sau đó

    screen = _screen(qapp)
    calls = []

    def fake_scan(source_path, *args):
        calls.append(source_path)
        return [_m(fa_s, source_version=NEW_VERSION)]

    monkeypatch.setattr(sfss, "scan_one_file_for_secret", fake_scan)
    loaded = ssp.SecretScanProgress.load(folder_s, "fast")
    assert loaded is not None and loaded.rescan_pending
    decision = ResumeDecision(
        ResumeDecision.RESUME_RESCAN,
        prog=loaded,
        rescan_abs=SecretFileScanScreen._rescan_targets(loaded, folder_s),
    )
    _run_sync(screen, folder_s, decision)

    assert calls == [fa_s]                # chỉ file còn trên đĩa được quét lại
    assert screen.table.rowCount() == 1   # hàng cũ thay bằng hàng mới
    after = ssp.SecretScanProgress.load(folder_s, "fast")
    assert after is None                  # pending sạch → journal xóa hẳn


def test_continue_mode_rebuilds_rescan_from_mig_choice() -> None:
    """Crash giữa begin_migration và set_rescan_pending: nút 'Tiếp tục hoàn
    tất' vẫn phải chọn RESUME_RESCAN theo choice=3 đã lưu (R1 — ý định nâng
    cấp phải dựng lại được)."""

    class _Prog:
        def __init__(self, pending, migration):
            self.rescan_pending = pending
            self.migration = migration

    targets = [ssp._norm_path(r"D:\kho\A.PDF")]
    assert (
        SecretFileScanScreen._continue_mode(
            _Prog(set(), {"target": NEW_VERSION, "choice": 3}), targets
        )
        == ResumeDecision.RESUME_RESCAN
    )
    # choice 2 / migration đã xong / không còn targets → RESUME thường.
    assert (
        SecretFileScanScreen._continue_mode(
            _Prog(set(), {"target": NEW_VERSION, "choice": 2}), targets
        )
        == ResumeDecision.RESUME
    )
    assert (
        SecretFileScanScreen._continue_mode(_Prog(set(), None), targets)
        == ResumeDecision.RESUME
    )
    # Còn tập quét lại → RESUME_RESCAN bất kể migration.
    assert (
        SecretFileScanScreen._continue_mode(_Prog({"a.pdf"}, None), [])
        == ResumeDecision.RESUME_RESCAN
    )


def test_worker_resume_progress_counts_whole_folder(
    qapp, tmp_path, monkeypatch
) -> None:
    """Resume khi 2/3 file đã quét: thanh tiến độ phải chạy theo TOÀN thư
    mục (2/3 → 3/3), không reset 0% theo phần việc còn lại của lượt."""
    monkeypatch.setenv("SECRET_SCAN_MAX_FILE_WORKERS", "1")
    folder, paths = _mk_tree(tmp_path, 3)
    f0, f1, f2 = paths
    _seed_journal(folder, paths, {}, pending=(f2,))  # f0, f1 đã quét

    screen = _screen(qapp)
    seen = []
    screen._progress_changed.connect(
        lambda cur, tot: seen.append((cur, tot))
    )

    def fake_scan(source_path, *args):
        return []

    monkeypatch.setattr(sfss, "scan_one_file_for_secret", fake_scan)
    decision = ResumeDecision(
        ResumeDecision.RESUME,
        prog=ssp.SecretScanProgress.load(folder, "fast"),
    )
    _run_sync(screen, folder, decision)

    assert seen and seen[0] == (2, 3)   # khởi động ở 2/3, không phải 0/1
    assert seen[-1] == (3, 3)           # kết thúc đúng 100% toàn thư mục
    assert max(c for c, _ in seen) == 3  # không vượt total
