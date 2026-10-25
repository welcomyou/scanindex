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


def _artifact_scan(fake):
    """Bọc fake trả list matches thành fake trả SecretScanArtifact (worker v4
    gọi `scan_one_file_for_secret_artifact` thay cho wrapper list cũ). Chỉ
    forward CÁC KWARG HỢP ĐỒNG CŨ (page_limit/run_id) — kwarg nội bộ mới
    (kie_spool_cb...) không đụng tới fake (fake giả lập tầng scan, không phải
    tầng spool)."""
    def wrapped(source_path, rel, file_work, first_page_only, cancel, log, **kw):
        legacy = {
            k: v for k, v in kw.items() if k in ("page_limit", "run_id")
        }
        matches = fake(source_path, rel, file_work, first_page_only, cancel, log, **legacy)
        return sfss.SecretScanArtifact(
            matches=list(matches),
            source_pdf=source_path,
            canonical={},
            status="clean",
        )
    return wrapped


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
    monkeypatch_holder.setattr(sfss, "scan_one_file_for_secret_artifact", _artifact_scan(fake_scan))
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
    mp.setattr(sfss, "scan_one_file_for_secret_artifact", _artifact_scan(fake_scan))
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
    mp.setattr(sfss, "scan_one_file_for_secret_artifact", _artifact_scan(fake_scan))
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

    monkeypatch.setattr(sfss, "scan_one_file_for_secret_artifact", _artifact_scan(fake_scan))
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

    monkeypatch.setattr(sfss, "scan_one_file_for_secret_artifact", _artifact_scan(fake_scan2))
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
    mp.setattr(sfss, "scan_one_file_for_secret_artifact", _artifact_scan(fake_scan))
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

    monkeypatch.setattr(sfss, "scan_one_file_for_secret_artifact", _artifact_scan(fake_scan))
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

    monkeypatch.setattr(sfss, "scan_one_file_for_secret_artifact", _artifact_scan(fake_scan))
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

    monkeypatch.setattr(sfss, "scan_one_file_for_secret_artifact", _artifact_scan(fake_scan))
    decision = ResumeDecision(
        ResumeDecision.RESUME,
        prog=ssp.SecretScanProgress.load(folder, "fast"),
    )
    _run_sync(screen, folder, decision)

    assert seen and seen[0] == (2, 3)   # khởi động ở 2/3, không phải 0/1
    assert seen[-1] == (3, 3)           # kết thúc đúng 100% toàn thư mục
    assert max(c for c, _ in seen) == 3  # không vượt total


# ---------------------------------------------------------------------------
# Review FIX REPORT (lượt 2) — P1-D: nghĩa vụ retry qua các lượt; P1-E:
# commit muộn sau khi lượt đóng không được chạm journal.
#
# Hai kịch bản này điều phối Qt worker + commit KIE trên pool thread — chạy
# IN-PROCESS trong pytest tái hiện abort native (PySide/threads/deepcopy —
# chỉ xảy ra trong môi trường pytest, không tái hiện khi chạy script cùng
# luồng lệnh: 3/3 sạch). Vì vậy chạy qua SUBPROCESS — cô lập như app thật;
# kịch bản tự assert và exit 0 khi đạt.
# ---------------------------------------------------------------------------

_P1D_SCRIPT = """import os, sys, tempfile
sys.path.insert(0, r"D:\\App\\ocrtool")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
import pytest_shim  # noqa: F401  (đảm bảo import lỗi rõ ràng nếu thiếu)
"""


def _run_worker_scenario(script: str) -> None:
    import subprocess

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo, env=env, timeout=600,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, (
        "kịch bản worker thất bại (exit %s):\n%s\n%s"
        % (r.returncode, r.stdout[-2000:], r.stderr[-2000:])
    )


def test_rescan_retry_keeps_pending_until_metadata_ok(tmp_path) -> None:
    """P1-D: file quét lại (replace) metadata chưa hoàn tất → rescan_pending
    phải GIỮ qua các lượt retry — `commit_replace_file` xóa rel khỏi tập thì
    phải thêm lại ngay (trước đây nhánh replace không thêm lại → lượt retry
    lỗi xóa nghĩa vụ retry, journal bị discard). Chuỗi 3 lượt: lỗi → lỗi →
    thành công, reload journal/registry giữa các lượt."""
    script = """
import os, sys, tempfile
sys.path.insert(0, r"%(repo)s")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import scanindex.core.secret_scan_progress as ssp
import scanindex.ui.screens.secret_file_scan_screen as sfss
from scanindex.ui.screens.secret_file_scan_screen import (
    SecretScanMatch, ResumeDecision,
)

NEW_VERSION, OLD_VERSION = "test-9.9.9", "test-0.9.0"
import scanindex.infra.version as _ver
_ver.get_version = lambda: NEW_VERSION

tmp = tempfile.mkdtemp()
folder = os.path.join(tmp, "kho")
os.makedirs(folder)
f0 = os.path.join(folder, "f0.pdf")
open(f0, "wb").write(b"%%PDF-1.4 fake")
ssp.progress_dir = lambda: os.path.join(tmp, "scan_progress")

prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
prog.record_file("f0.pdf", "ok")
prog.record_matches([{"source_path": f0, "relative_path": "f0.pdf",
                      "keyword": "MẬT", "page_number": 1,
                      "mode": "Trang đầu", "ocr_pdf_path": "", "note": "x"}])
prog.save(); prog.close()
prog = ssp.SecretScanProgress.load(folder, "fast")
prog.set_rescan_pending(["f0.pdf"])
prog.save(); prog.close()

applied = {"applied": False}

def fake_scan(source_path, rel, file_work, first_page_only, cancel, log, **kw):
    return sfss.SecretScanArtifact(
        matches=[SecretScanMatch(
            source_path=source_path, relative_path=rel, keyword="MẬT",
            page_number=1, mode="Trang đầu", ocr_pdf_path="",
        )],
        source_pdf=source_path, canonical={},
        status="confirmed_positive", metadata_pending=True,
        kie_state={"applied": applied["applied"]},
    )

sfss.scan_one_file_for_secret_artifact = fake_scan

def run_once():
    screen = sfss.SecretFileScanScreen()
    screen._busy = True
    screen._cancel_event.clear()
    screen._run_worker(folder, True, ResumeDecision(
        ResumeDecision.RESUME_RESCAN,
        prog=ssp.SecretScanProgress.load(folder, "fast"),
        rescan_abs=[ssp._norm_path(f0)],
    ))
    for _ in range(200):
        QCoreApplication.processEvents()

# Lượt 1+2: metadata lỗi → rescan_pending phải GIỮ (nhánh replace)
for run_idx in (1, 2):
    run_once()
    after = ssp.SecretScanProgress.load(folder, "fast")
    assert after is not None, "lượt dở → journal không được discard"
    assert "f0.pdf" in after.rescan_pending, (
        f"lượt {run_idx}: metadata lỗi → file phải còn trong tập quét lại "
        f"(thấy {after.rescan_pending})"
    )
    assert after.needs_continue(), "journal phải được giữ"

# Lượt 3: metadata đủ → nghĩa vụ retry gỡ, registry cache-final
applied["applied"] = True
run_once()
after = ssp.SecretScanProgress.load(folder, "fast")
if after is not None:
    assert "f0.pdf" not in after.rescan_pending, (
        f"metadata đủ → không còn nghĩa vụ quét lại (thấy {after.rescan_pending})"
    )
reg = ssp.FileRegistry.load()
hit = reg.lookup_with_status(
    f0, os.path.getsize(f0), os.path.getmtime(f0), "fast", NEW_VERSION
)
reg.close()
assert hit is not None, "metadata đủ → phải được cache-final"
print("P1D-OK")
""" % {"repo": os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))}
    _run_worker_scenario(script)


def test_late_commit_after_close_touches_nothing(tmp_path) -> None:
    """P1-E: callback commit muộn sau khi lượt đã đóng (cancel giữa chừng)
    KHÔNG được mở lại journal/close-file — chỉ được bỏ; file chưa có dòng
    journal thì lượt sau tự quét lại (trước đây: mở lại journal ghi event
    'f' không header, reader trả None)."""
    script = """
import os, sys, tempfile
sys.path.insert(0, r"%(repo)s")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import scanindex.core.secret_scan_progress as ssp
import scanindex.ui.screens.secret_file_scan_screen as sfss
from scanindex.ui.screens.secret_file_scan_screen import ResumeDecision

NEW_VERSION, OLD_VERSION = "test-9.9.9", "test-0.9.0"
import scanindex.infra.version as _ver
_ver.get_version = lambda: NEW_VERSION

tmp = tempfile.mkdtemp()
folder = os.path.join(tmp, "kho")
os.makedirs(folder)
f0 = os.path.join(folder, "f0.pdf")
open(f0, "wb").write(b"%%PDF-1.4 fake")
ssp.progress_dir = lambda: os.path.join(tmp, "scan_progress")

prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
# f0 CHƯA ghi gì — nằm hàng đợi pending của lượt RESUME (nếu ghi "ok" thì
# lượt kế thừa bỏ qua, fake scan không bao giờ chạy)
prog.save(); prog.close()

holder = {}

def fake_scan(source_path, rel, file_work, first_page_only, cancel, log, **kw):
    art = sfss.SecretScanArtifact(
        matches=[], source_pdf=source_path, canonical={},
        status="clean", metadata_pending=True,
        kie_state={"applied": False},
    )
    holder["state"] = art.kie_state
    cancel.set()  # hủy lượt SAU khi file scan xong — drain cuối lượt bị bỏ
    return art

sfss.scan_one_file_for_secret_artifact = fake_scan

screen = sfss.SecretFileScanScreen()
screen._busy = True
screen._cancel_event.clear()
screen._run_worker(folder, True, ResumeDecision(
    ResumeDecision.RESUME, prog=ssp.SecretScanProgress.load(folder, "fast"),
))
for _ in range(200):
    QCoreApplication.processEvents()
rows_before = screen.table.rowCount()
state = holder.get("state")
assert state is not None, "phải có kie_state chờ ACK"
cb = state.get("on_applied")
assert cb is not None, "entry phải đăng ký on_applied"
cb(); cb()  # callback muộn SAU khi lượt đóng — gọi nhiều lần phải an toàn
for _ in range(50):
    QCoreApplication.processEvents()
assert screen.table.rowCount() == rows_before, (
    "commit muộn sau close không được phát thêm hàng"
)
after = ssp.SecretScanProgress.load(folder, "fast")
if after is not None:
    assert "f0.pdf" not in after.ok_files(), (
        "commit muộn sau close không được ghi journal — file phải còn "
        "nghĩa vụ quét lại ở lượt sau (chưa có dòng ok)"
    )
print("P1E-OK")
""" % {"repo": os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))}
    _run_worker_scenario(script)


def _run_worker_scenario_script(script: str) -> None:
    import subprocess

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo, env=env, timeout=600,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, (
        "kịch bản worker thất bại (exit %s):\n%s\n%s"
        % (r.returncode, r.stdout[-2500:], r.stderr[-2500:])
    )


def test_commit_persist_error_retries_without_duplicate(tmp_path) -> None:
    """R2-02: persist (prog.save) của commit lỗi MỘT LẦN rồi thành công →
    đúng 1 hàng UI, đúng 1 match trong journal, scanned=1, failures=0
    (trước đây retry phát lại emit + append trùng: 2 hàng, match 1→2)."""
    script = """
import os, sys, tempfile
sys.path.insert(0, r"%(repo)s")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import scanindex.core.secret_scan_progress as ssp
import scanindex.ui.screens.secret_file_scan_screen as sfss
from scanindex.ui.screens.secret_file_scan_screen import (
    SecretScanMatch, ResumeDecision,
)

NEW_VERSION, OLD_VERSION = "test-9.9.9", "test-0.9.0"
import scanindex.infra.version as _ver
_ver.get_version = lambda: NEW_VERSION

tmp = tempfile.mkdtemp()
folder = os.path.join(tmp, "kho")
os.makedirs(folder)
f0 = os.path.join(folder, "f0.pdf")
open(f0, "wb").write(b"%%PDF-1.4 fake")
ssp.progress_dir = lambda: os.path.join(tmp, "scan_progress")

prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
prog.save(); prog.close()

# Fault injection: save THỨ HAI SAU KHI SCAN (persist của commit deferred
# trong drain — save đầu sau scan là file-worker finally) ném OSError một
# lần, các lần sau thành công.
_orig_save = ssp.SecretScanProgress.save
_save_state = {"after_scan": 0, "scan": False, "failed": False}

def _save_marked(self):
    if _save_state["scan"]:
        _save_state["after_scan"] += 1
        if _save_state["after_scan"] == 2 and not _save_state["failed"]:
            _save_state["failed"] = True
            raise OSError("injected fsync failure (commit persist)")
    _orig_save(self)

ssp.SecretScanProgress.save = _save_marked

summary = {}

def fake_scan(source_path, rel, file_work, first_page_only, cancel, log, **kw):
    _save_state["scan"] = True
    return sfss.SecretScanArtifact(
        matches=[SecretScanMatch(
            source_path=source_path, relative_path=rel, keyword="MẬT",
            page_number=1, mode="Trang đầu", ocr_pdf_path="",
        )],
        source_pdf=source_path, canonical={},
        status="confirmed_positive", metadata_pending=True,
        # applied=False: metadata về TẠI DRAIN (đúng probe R2-02) — save
        # sau scan: [1] file-worker finally, [2] persist của commit drain.
        kie_state={"applied": False},
    )

sfss.scan_one_file_for_secret_artifact = fake_scan

screen = sfss.SecretFileScanScreen()
screen._scan_finished.connect(lambda s: summary.update(s))
screen._busy = True
screen._cancel_event.clear()
screen._run_worker(folder, True, ResumeDecision(
    ResumeDecision.RESUME, prog=ssp.SecretScanProgress.load(folder, "fast"),
))
for _ in range(200):
    QCoreApplication.processEvents()

assert screen.table.rowCount() == 1, (
    f"phải đúng 1 hàng UI dù persist lỗi một lần (thấy {screen.table.rowCount()})"
)
assert summary.get("scanned") == 1, f"scanned phải là 1 (thấy {summary})"
assert summary.get("failures") == 0, (
    f"persist lỗi một lần phải retry thành công — failures=0 (thấy {summary})"
)
after = ssp.SecretScanProgress.load(folder, "fast")
assert after is not None and len(after.matches) == 1, (
    f"journal phải đúng 1 match (thấy {len(after.matches) if after else None})"
)
assert "f0.pdf" in after.ok_files()
# applied=False (metadata chưa áp) → KHÔNG cache-final (P1-07) nhưng phải
# còn nghĩa vụ quét lại; retry persist KHÔNG được phát lại hàng/match.
assert "f0.pdf" in after.rescan_pending, (
    f"metadata chưa áp → file phải còn nghĩa vụ quét lại "
    f"(thấy {after.rescan_pending})"
)
reg = ssp.FileRegistry.load()
hit = reg.lookup_with_status(
    f0, os.path.getsize(f0), os.path.getmtime(f0), "fast", NEW_VERSION
)
reg.close()
assert hit is None, (
    "metadata chưa áp → KHÔNG được cache-final (P1-07 giữ nguyên)"
)
print("R2-02-OK")
""" % {"repo": os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))}
    _run_worker_scenario_script(script)


def test_commit_publish_error_keeps_retry_obligation(tmp_path) -> None:
    """R2-01: ACK-sớm + commit PUBLISH lỗi (lặp) → entry phải còn trong cơ
    chế retry: failures được đếm, rescan_pending giữ file, journal KHÔNG bị
    discard — sau reload file phải còn nghĩa vụ quét lại (không mất cả kết
    quả lẫn nghĩa vụ). Lượt sau (không lỗi) phải chuyển nghĩa vụ thành kết
    quả bền vững."""
    script = """
import os, sys, tempfile
sys.path.insert(0, r"%(repo)s")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import scanindex.core.secret_scan_progress as ssp
import scanindex.ui.screens.secret_file_scan_screen as sfss
from scanindex.ui.screens.secret_file_scan_screen import (
    SecretScanMatch, ResumeDecision,
)

NEW_VERSION, OLD_VERSION = "test-9.9.9", "test-0.9.0"
import scanindex.infra.version as _ver
_ver.get_version = lambda: NEW_VERSION

tmp = tempfile.mkdtemp()
folder = os.path.join(tmp, "kho")
os.makedirs(folder)
f0 = os.path.join(folder, "f0.pdf")
open(f0, "wb").write(b"%%PDF-1.4 fake")
ssp.progress_dir = lambda: os.path.join(tmp, "scan_progress")

prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
prog.save(); prog.close()

# Fault injection: journal publish (commit_replace_file) LUÔN lỗi trong
# lượt 1 — mô phỏng lỗi ghi dữ liệu lặp. Bật/tắt qua file cờ.
fail_flag = os.path.join(tmp, "fail_commit")
open(fail_flag, "w").write("1")

_orig_crf = ssp.SecretScanProgress.commit_replace_file

def _crf_marked(self, rel, abs_norm, match_dicts):
    if os.path.exists(fail_flag):
        raise OSError("injected journal publish failure")
    _orig_crf(self, rel, abs_norm, match_dicts)

ssp.SecretScanProgress.commit_replace_file = _crf_marked

applied = {"applied": True}
summary = {}

def fake_scan(source_path, rel, file_work, first_page_only, cancel, log, **kw):
    return sfss.SecretScanArtifact(
        matches=[SecretScanMatch(
            source_path=source_path, relative_path=rel, keyword="MẬT",
            page_number=1, mode="Trang đầu", ocr_pdf_path="",
        )],
        source_pdf=source_path, canonical={},
        status="confirmed_positive", metadata_pending=True,
        kie_state={"applied": applied["applied"]},
    )

sfss.scan_one_file_for_secret_artifact = fake_scan

def run_once():
    screen = sfss.SecretFileScanScreen()
    screen._scan_finished.connect(lambda s: summary.update(s))
    screen._busy = True
    screen._cancel_event.clear()
    screen._run_worker(folder, True, ResumeDecision(
        ResumeDecision.RESUME_RESCAN,
        prog=ssp.SecretScanProgress.load(folder, "fast"),
        rescan_abs=[ssp._norm_path(f0)],
    ))
    for _ in range(200):
        QCoreApplication.processEvents()
    return screen

# Lượt 1: publish luôn lỗi → failures được đếm, rescan_pending giữ,
# journal KHÔNG discard (không mất cả kết quả lẫn nghĩa vụ retry).
screen = run_once()
assert summary.get("failures", 0) >= 1, (
    f"commit lỗi lặp phải báo failure (thấy {summary})"
)
after = ssp.SecretScanProgress.load(folder, "fast")
assert after is not None, "còn nghĩa vụ retry → journal phải được giữ"
assert "f0.pdf" in after.rescan_pending, (
    f"file phải còn trong tập quét lại (thấy {after.rescan_pending})"
)
assert "f0.pdf" not in after.ok_files(), (
    "publish lỗi → không được coi là đã có kết quả bền vững"
)

# Lượt 2: hết lỗi → nghĩa vụ chuyển thành kết quả bền vững.
os.remove(fail_flag)
applied["applied"] = True
summary.clear()
screen = run_once()
after = ssp.SecretScanProgress.load(folder, "fast")
if after is not None:
    assert "f0.pdf" not in after.rescan_pending, (
        f"lượt thành công → hết nghĩa vụ (thấy {after.rescan_pending})"
    )
reg = ssp.FileRegistry.load()
hit = reg.lookup_with_status(
    f0, os.path.getsize(f0), os.path.getmtime(f0), "fast", NEW_VERSION
)
reg.close()
assert hit is not None, "lượt 2 thành công → registry phải có kết quả"
print("R2-01-OK")
""" % {"repo": os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))}
    _run_worker_scenario_script(script)


# ---------------------------------------------------------------------------
# Review vòng 4 (R3) — R3-01 registry append, R3-02 cửa sổ đăng ký,
# R3-05 fingerprint của đúng phiên bản đã đọc. Chạy SUBPROCESS như các test
# P1-D/P1-E/R2-01/R2-02 (cô lập native abort của pytest-env).
# ---------------------------------------------------------------------------

def test_registry_record_error_one_shot_retries_via_drain(tmp_path) -> None:
    """R3-01 (K deferred ON, replace, có entry cũ): registry record lỗi ĐÚNG
    MỘT lần → lỗi phải đi lên cơ chế commit retry (không nuốt như
    `except OSError: pass` cũ) — drain thử lại ngay: scanned=1, failures=0,
    reload registry THẤY kết quả, journal hoàn tất sạch."""
    script = """
import os, sys, tempfile
sys.path.insert(0, r"%(repo)s")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import scanindex.core.secret_scan_progress as ssp
import scanindex.ui.screens.secret_file_scan_screen as sfss
from scanindex.ui.screens.secret_file_scan_screen import (
    SecretScanMatch, ResumeDecision,
)

NEW_VERSION, OLD_VERSION = "test-9.9.9", "test-0.9.0"
import scanindex.infra.version as _ver
_ver.get_version = lambda: NEW_VERSION

tmp = tempfile.mkdtemp()
folder = os.path.join(tmp, "kho")
os.makedirs(folder)
f0 = os.path.join(folder, "f0.pdf")
open(f0, "wb").write(b"%%PDF-1.4 fake")
ssp.progress_dir = lambda: os.path.join(tmp, "scan_progress")

# Journal bản cũ: f0 đã quét (có entry cũ trong registry — kịch bản replace).
prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
prog.record_file("f0.pdf", "ok")
prog.record_matches([{"source_path": f0, "relative_path": "f0.pdf",
                      "keyword": "MẬT", "page_number": 1,
                      "mode": "Trang đầu", "ocr_pdf_path": "", "note": "x"}])
prog.save(); prog.close()
reg = ssp.FileRegistry.load()
st0 = os.stat(f0)
reg.record(f0, st0.st_size, st0.st_mtime, "fast", OLD_VERSION, [])
reg.save(); reg.close()
prog = ssp.SecretScanProgress.load(folder, "fast")
prog.set_rescan_pending(["f0.pdf"])
prog.save(); prog.close()

# Fault: append event 'r' của registry lỗi ĐÚNG MỘT LẦN — bật TRONG fake_scan
# (sau đường nâng cấp kế thừa, đúng lúc commit kết quả quét).
_orig_al = ssp.FileRegistry._append_line
_fault = {"left": 0}

def _al_marked(self, event):
    if _fault["left"] > 0 and event.get("t") == "r":
        _fault["left"] -= 1
        raise OSError("injected registry append failure")
    return _orig_al(self, event)

ssp.FileRegistry._append_line = _al_marked

def fake_scan(source_path, rel, file_work, first_page_only, cancel, log, **kw):
    _fault["left"] = 1
    return sfss.SecretScanArtifact(
        matches=[SecretScanMatch(
            source_path=source_path, relative_path=rel, keyword="MẬT",
            page_number=1, mode="Trang đầu", ocr_pdf_path="",
        )],
        source_pdf=source_path, canonical={},
        status="confirmed_positive", metadata_pending=True,
        kie_state={"applied": True},
    )

sfss.scan_one_file_for_secret_artifact = fake_scan

summary = {}
screen = sfss.SecretFileScanScreen()
screen._scan_finished.connect(lambda s: summary.update(s))
screen._busy = True
screen._cancel_event.clear()
screen._run_worker(folder, True, ResumeDecision(
    ResumeDecision.RESUME_RESCAN,
    prog=ssp.SecretScanProgress.load(folder, "fast"),
    rescan_abs=[ssp._norm_path(f0)],
))
for _ in range(200):
    QCoreApplication.processEvents()

assert summary.get("updated") == 1, (
    f"lỗi một lần phải retry thành công qua drain — nhánh replace đếm "
    f"updated=1 (thấy {summary})"
)
assert summary.get("failures") == 0, (
    f"lỗi một lần KHÔNG được đếm failure (thấy {summary})"
)
after = ssp.SecretScanProgress.load(folder, "fast")
if after is not None:
    assert "f0.pdf" not in after.rescan_pending, (
        f"commit thành công → hết nghĩa vụ quét lại (thấy {after.rescan_pending})"
    )
reg = ssp.FileRegistry.load()
hit = reg.lookup_with_status(f0, os.path.getsize(f0), os.path.getmtime(f0),
                             "fast", NEW_VERSION)
reg.close()
assert hit is not None and hit[0] and hit[0][0]["keyword"] == "MẬT", (
    "R3-01: kết quả phải NẰM TRÊN ĐĨA sau retry (trước đây bị nuốt → mất)"
)
print("R3-01-ONESHOT-OK")
""" % {"repo": os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))}
    _run_worker_scenario_script(script)


def test_registry_record_error_sync_counts_failure_keeps_journal(tmp_path) -> None:
    """R3-01 (K OFF — đường đồng bộ, không entry cũ): record lỗi → KHÔNG được
    `scanned=1, failures=0` rồi mất cả registry lẫn journal: failures >= 1,
    registry reload KHÔNG có entry ma; lượt sau (hết lỗi) phải chuyển nghĩa
    vụ thành kết quả bền vững."""
    script = """
import os, sys, tempfile
sys.path.insert(0, r"%(repo)s")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import scanindex.core.secret_scan_progress as ssp
import scanindex.ui.screens.secret_file_scan_screen as sfss
from scanindex.ui.screens.secret_file_scan_screen import (
    SecretScanMatch, ResumeDecision,
)

NEW_VERSION, OLD_VERSION = "test-9.9.9", "test-0.9.0"
import scanindex.infra.version as _ver
_ver.get_version = lambda: NEW_VERSION

tmp = tempfile.mkdtemp()
folder = os.path.join(tmp, "kho")
os.makedirs(folder)
f0 = os.path.join(folder, "f0.pdf")
open(f0, "wb").write(b"%%PDF-1.4 fake")
ssp.progress_dir = lambda: os.path.join(tmp, "scan_progress")

prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
prog.save(); prog.close()  # f0 chưa quét — hàng đợi pending của RESUME

_orig_al = ssp.FileRegistry._append_line
_fault = {"left": 0}

def _al_marked(self, event):
    if _fault["left"] > 0 and event.get("t") == "r":
        _fault["left"] -= 1
        raise OSError("injected registry append failure")
    return _orig_al(self, event)

ssp.FileRegistry._append_line = _al_marked

_runs = {"n": 0}

def fake_scan(source_path, rel, file_work, first_page_only, cancel, log, **kw):
    _runs["n"] += 1
    if _runs["n"] == 1:
        _fault["left"] = 1  # chỉ lượt ĐẦU lỗi; lượt sau phải thành công
    return sfss.SecretScanArtifact(
        matches=[SecretScanMatch(
            source_path=source_path, relative_path=rel, keyword="MẬT",
            page_number=1, mode="Trang đầu", ocr_pdf_path="",
        )],
        source_pdf=source_path, canonical={}, status="clean",
    )

sfss.scan_one_file_for_secret_artifact = fake_scan

summary = {}
screen = sfss.SecretFileScanScreen()
screen._scan_finished.connect(lambda s: summary.update(s))
screen._busy = True
screen._cancel_event.clear()
screen._run_worker(folder, True, ResumeDecision(
    ResumeDecision.RESUME, prog=ssp.SecretScanProgress.load(folder, "fast"),
))
for _ in range(200):
    QCoreApplication.processEvents()

assert summary.get("failures", 0) >= 1, (
    f"R3-01: record lỗi phải đếm failure — không được scanned=1/failures=0 "
    f"(thấy {summary})"
)
after = ssp.SecretScanProgress.load(folder, "fast")
assert after is not None, "chưa có kết quả bền vững → journal phải được giữ"
reg = ssp.FileRegistry.load()
hit = reg.lookup_with_status(f0, os.path.getsize(f0), os.path.getmtime(f0),
                             "fast", NEW_VERSION)
reg.close()
assert hit is None, (
    "record lỗi → registry KHÔNG được có entry ma (RAM phải rollback)"
)

# Lượt 2: hết lỗi → nghĩa vụ chuyển thành kết quả bền vững.
sfss.scan_one_file_for_secret_artifact = fake_scan
summary.clear()
screen = sfss.SecretFileScanScreen()
screen._scan_finished.connect(lambda s: summary.update(s))
screen._busy = True
screen._cancel_event.clear()
screen._run_worker(folder, True, ResumeDecision(
    ResumeDecision.RESUME, prog=ssp.SecretScanProgress.load(folder, "fast"),
))
for _ in range(200):
    QCoreApplication.processEvents()
after = ssp.SecretScanProgress.load(folder, "fast")
if after is not None:
    assert "f0.pdf" not in after.rescan_pending, (
        f"lượt thành công → hết nghĩa vụ (thấy {after.rescan_pending})"
    )
reg = ssp.FileRegistry.load()
hit = reg.lookup_with_status(f0, os.path.getsize(f0), os.path.getmtime(f0),
                             "fast", NEW_VERSION)
reg.close()
assert hit is not None and hit[0], "lượt 2 thành công → registry phải có kết quả"
print("R3-01-SYNC-OK")
""" % {"repo": os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))}
    _run_worker_scenario_script(script)


def test_registry_record_error_repeated_keeps_retry_obligation(tmp_path) -> None:
    """R3-01 (K deferred ON, lỗi LẶP): entry phải ở lại cơ chế retry —
    failures được đếm, rescan_pending giữ file, journal KHÔNG discard,
    registry không có kết quả."""
    script = """
import os, sys, tempfile
sys.path.insert(0, r"%(repo)s")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import scanindex.core.secret_scan_progress as ssp
import scanindex.ui.screens.secret_file_scan_screen as sfss
from scanindex.ui.screens.secret_file_scan_screen import (
    SecretScanMatch, ResumeDecision,
)

NEW_VERSION, OLD_VERSION = "test-9.9.9", "test-0.9.0"
import scanindex.infra.version as _ver
_ver.get_version = lambda: NEW_VERSION

tmp = tempfile.mkdtemp()
folder = os.path.join(tmp, "kho")
os.makedirs(folder)
f0 = os.path.join(folder, "f0.pdf")
open(f0, "wb").write(b"%%PDF-1.4 fake")
ssp.progress_dir = lambda: os.path.join(tmp, "scan_progress")

prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
prog.record_file("f0.pdf", "ok")
prog.record_matches([{"source_path": f0, "relative_path": "f0.pdf",
                      "keyword": "MẬT", "page_number": 1,
                      "mode": "Trang đầu", "ocr_pdf_path": "", "note": "x"}])
prog.save(); prog.close()
prog = ssp.SecretScanProgress.load(folder, "fast")
prog.set_rescan_pending(["f0.pdf"])
prog.save(); prog.close()

_fail_flag = os.path.join(tmp, "fail_registry")  # bật TRONG fake_scan

_orig_al = ssp.FileRegistry._append_line

def _al_marked(self, event):
    if os.path.exists(_fail_flag) and event.get("t") == "r":
        raise OSError("injected registry append failure (repeated)")
    return _orig_al(self, event)

ssp.FileRegistry._append_line = _al_marked

def fake_scan(source_path, rel, file_work, first_page_only, cancel, log, **kw):
    open(_fail_flag, "w").write("1")  # sau đường nâng cấp kế thừa
    return sfss.SecretScanArtifact(
        matches=[SecretScanMatch(
            source_path=source_path, relative_path=rel, keyword="MẬT",
            page_number=1, mode="Trang đầu", ocr_pdf_path="",
        )],
        source_pdf=source_path, canonical={},
        status="confirmed_positive", metadata_pending=True,
        kie_state={"applied": True},
    )

sfss.scan_one_file_for_secret_artifact = fake_scan

summary = {}
screen = sfss.SecretFileScanScreen()
screen._scan_finished.connect(lambda s: summary.update(s))
screen._busy = True
screen._cancel_event.clear()
screen._run_worker(folder, True, ResumeDecision(
    ResumeDecision.RESUME_RESCAN,
    prog=ssp.SecretScanProgress.load(folder, "fast"),
    rescan_abs=[ssp._norm_path(f0)],
))
for _ in range(200):
    QCoreApplication.processEvents()

assert summary.get("failures", 0) >= 1, (
    f"lỗi lặp phải báo failure (thấy {summary})"
)
after = ssp.SecretScanProgress.load(folder, "fast")
assert after is not None, "còn nghĩa vụ retry → journal phải được giữ"
assert "f0.pdf" in after.rescan_pending, (
    f"file phải còn nghĩa vụ quét lại (thấy {after.rescan_pending})"
)
reg = ssp.FileRegistry.load()
hit = reg.lookup_with_status(f0, os.path.getsize(f0), os.path.getmtime(f0),
                             "fast", NEW_VERSION)
inherited = reg.inherited_count()
reg.close()
assert inherited >= 1, (
    "record lỗi → entry bền vững còn lại phải là entry KẾ THỪA (RAM rollback "
    "giữ entry cũ); entry kết quả mới KHÔNG được ghi đè bền vững"
)
assert hit is not None and hit[0], (
    "kịch bản replace phải còn entry cũ tra được (không mất dữ liệu cũ)"
)
print("R3-01-REPEATED-OK")
""" % {"repo": os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))}
    _run_worker_scenario_script(script)


def test_commit_records_scan_stat_not_fresh_stat(tmp_path) -> None:
    """R3-05: nguồn đổi SAU khi quét/validate nhưng TRƯỚC record (fault point
    của probe chuyên gia) → registry phải record fingerprint của ĐÚNG phiên
    bản đã đọc (scan_stat) — lookup theo fingerprint nguồn MỚI phải MISS
    (trước đây stat lại → cache hit cho nội dung chưa hề quét)."""
    script = """
import os, sys, tempfile
sys.path.insert(0, r"%(repo)s")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import scanindex.core.secret_scan_progress as ssp
import scanindex.ui.screens.secret_file_scan_screen as sfss
from scanindex.ui.screens.secret_file_scan_screen import (
    SecretScanMatch, ResumeDecision,
)

NEW_VERSION, OLD_VERSION = "test-9.9.9", "test-0.9.0"
import scanindex.infra.version as _ver
_ver.get_version = lambda: NEW_VERSION

tmp = tempfile.mkdtemp()
folder = os.path.join(tmp, "kho")
os.makedirs(folder)
f0 = os.path.join(folder, "f0.pdf")
open(f0, "wb").write(b"%%PDF-1.4 fake")
ssp.progress_dir = lambda: os.path.join(tmp, "scan_progress")

prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
prog.save(); prog.close()
st_scan = os.stat(f0)  # fingerprint của phiên bản SẼ được đọc

# Fault point "sau validation, trước record": nguồn đổi ngay sau khi
# commit_replace_file chạy (validation đã xong), TRƯỚC khi registry.record.
_orig_crf = ssp.SecretScanProgress.commit_replace_file

def _crf_mutate(self, rel, abs_norm, match_dicts):
    _orig_crf(self, rel, abs_norm, match_dicts)
    with open(_mutate["path"], "ab") as f:
        f.write(b"-modified-after-scan")

_crf_mutate_path = None
_mutate = {"path": f0}
ssp.SecretScanProgress.commit_replace_file = _crf_mutate

def fake_scan(source_path, rel, file_work, first_page_only, cancel, log, **kw):
    return sfss.SecretScanArtifact(
        matches=[SecretScanMatch(
            source_path=source_path, relative_path=rel, keyword="MẬT",
            page_number=1, mode="Trang đầu", ocr_pdf_path="",
        )],
        source_pdf=source_path, canonical={}, status="clean",
    )

sfss.scan_one_file_for_secret_artifact = fake_scan

summary = {}
screen = sfss.SecretFileScanScreen()
screen._scan_finished.connect(lambda s: summary.update(s))
screen._busy = True
screen._cancel_event.clear()
screen._run_worker(folder, True, ResumeDecision(
    ResumeDecision.RESUME, prog=ssp.SecretScanProgress.load(folder, "fast"),
))
for _ in range(200):
    QCoreApplication.processEvents()

st_after = os.stat(f0)
assert (st_after.st_size, st_after.st_mtime) != (st_scan.st_size, st_scan.st_mtime), (
    "kịch bản phải thực sự đổi nguồn sau khi quét"
)
reg = ssp.FileRegistry.load()
hit_new = reg.lookup_with_status(
    f0, st_after.st_size, st_after.st_mtime, "fast", NEW_VERSION
)
hit_scan = reg.lookup_with_status(
    f0, st_scan.st_size, st_scan.st_mtime, "fast", NEW_VERSION
)
reg.close()
assert hit_new is None, (
    "R3-05: kết quả của phiên bản ĐÃ ĐỌC không được gắn cho phiên bản MỚI "
    "(stat lại để record là cache hit cho nội dung chưa quét)"
)
assert hit_scan is not None and hit_scan[0], (
    "fingerprint của phiên bản đã đọc phải trúng cache (kết quả đúng chủ)"
)
print("R3-05-OK")
""" % {"repo": os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))}
    _run_worker_scenario_script(script)


def test_callback_window_race_keeps_retry_entry(tmp_path) -> None:
    """R3-02: callback KIE chạy TRONG cửa sổ publish/đăng ký (hoặc trước/sau
    — cả ba lịch ACK) + commit lỗi MỘT lần → entry phải còn trong cơ chế
    retry (đăng ký TRƯỚC publish): drain thử lại → đúng một kết quả bền.
    (Bản cũ: callback đặt `committing` rồi worker bỏ qua append → entry
    không nằm ở đâu cả → mất cả kết quả lẫn nghĩa vụ.)"""
    script = """
import os, sys, tempfile, threading
sys.path.insert(0, r"%(repo)s")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import scanindex.core.secret_scan_progress as ssp
import scanindex.core.secret_scan_kie as ssk
import scanindex.ui.screens.secret_file_scan_screen as sfss
from scanindex.ui.screens.secret_file_scan_screen import (
    SecretScanMatch, ResumeDecision,
)
from concurrent.futures import ThreadPoolExecutor

NEW_VERSION, OLD_VERSION = "test-9.9.9", "test-0.9.0"
import scanindex.infra.version as _ver
_ver.get_version = lambda: NEW_VERSION

tmp = tempfile.mkdtemp()
folder = os.path.join(tmp, "kho")
os.makedirs(folder)
f0 = os.path.join(folder, "f0.pdf")
open(f0, "wb").write(b"%%PDF-1.4 fake")
canon = os.path.join(tmp, "canon.json")
open(canon, "w").write("{}")
ssp.progress_dir = lambda: os.path.join(tmp, "scan_progress")

prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
prog.save(); prog.close()

# Fault: journal publish (commit_replace_file) lỗi ĐÚNG MỘT LẦN — đúng điểm
# inject của probe R3-02 (sau quyết định bỏ đăng ký của bản cũ).
_orig_crf = ssp.SecretScanProgress.commit_replace_file
_fault = {"left": 1}

def _crf_marked(self, rel, abs_norm, match_dicts):
    if _fault["left"] > 0:
        _fault["left"] -= 1
        raise OSError("injected journal publish failure")
    return _orig_crf(self, rel, abs_norm, match_dicts)

ssp.SecretScanProgress.commit_replace_file = _crf_marked

# Coordinator thật (thread pool) — callback chạy trên thread delivery, đua
# với file-worker ở đoạn publish/đăng ký.
_coord = ssk.KieCoordinator(
    processes=1,
    executor_factory=lambda n: ThreadPoolExecutor(max_workers=n),
    job_fn=lambda p: {"status": "ready",
                      "fields_by_source": {0: [{"label": "X"}]}},
)
ssk._coordinator = _coord

def fake_scan(source_path, rel, file_work, first_page_only, cancel, log, **kw):
    state = {"applied": False, "error_code": None, "registered": False,
             "on_applied": None, "lock": threading.Lock()}

    def _deliver():
        with state["lock"]:
            deliver = state.get("on_applied") if state["registered"] else None
        if deliver is not None:
            try:
                deliver()
            except Exception:
                pass

    def _on_done(result):
        state["applied"] = True
        _deliver()

    accepted, reason = _coord.submit(
        {"job_id": "j1", "generation": 0, "canonical_path": canon,
         "selected_ordinals": [0], "model_fp": "", "run_id": ""},
        _on_done,
    )
    assert accepted, f"job phải được nhận ({reason})"
    return sfss.SecretScanArtifact(
        matches=[SecretScanMatch(
            source_path=source_path, relative_path=rel, keyword="MẬT",
            page_number=1, mode="Trang đầu", ocr_pdf_path="",
        )],
        source_pdf=source_path, canonical={},
        status="confirmed_positive", metadata_pending=True,
        kie_state=state,
    )

sfss.scan_one_file_for_secret_artifact = fake_scan

summary = {}
screen = sfss.SecretFileScanScreen()
screen._scan_finished.connect(lambda s: summary.update(s))
screen._busy = True
screen._cancel_event.clear()
screen._run_worker(folder, True, ResumeDecision(
    ResumeDecision.RESUME, prog=ssp.SecretScanProgress.load(folder, "fast"),
))
for _ in range(300):
    QCoreApplication.processEvents()

assert summary.get("scanned") == 1, (
    f"lỗi một lần phải retry thành công qua drain — scanned=1 (thấy {summary})"
)
assert summary.get("failures") == 0, (
    f"lỗi một lần KHÔNG được đếm failure (thấy {summary})"
)
reg = ssp.FileRegistry.load()
hit = reg.lookup_with_status(f0, os.path.getsize(f0), os.path.getmtime(f0),
                             "fast", NEW_VERSION)
reg.close()
assert hit is not None and hit[0], (
    "R3-02: đúng MỘT kết quả bền vững phải tồn tại sau retry — entry mất ở "
    "cửa sổ đăng ký là mất cả kết quả lẫn nghĩa vụ (bản cũ)"
)
_coord.shutdown(wait=True)
print("R3-02-WINDOW-OK")
""" % {"repo": os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))}
    _run_worker_scenario_script(script)


# ---------------------------------------------------------------------------
# Review chốt (D2) — durable KIE spool + KIE-only resume: restart chỉ resubmit
# metadata khi snapshot còn hợp lệ; OCR-call counter PHẢI = 0 (bài bắt buộc
# #7). Snapshot hỏng/nguồn đổi → quét lại toàn bộ kèm lý do rõ ràng.
# ---------------------------------------------------------------------------

def test_kie_only_resume_ocr_call_zero_then_full_rescan_when_stale(tmp_path) -> None:
    script = """
import hashlib, json, os, sys, tempfile, threading, time
sys.path.insert(0, r"%(repo)s")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
from concurrent.futures import ThreadPoolExecutor

import scanindex.core.secret_scan_progress as ssp
import scanindex.core.secret_scan_kie as ssk
import scanindex.ui.screens.secret_file_scan_screen as sfss
from scanindex.ui.screens.secret_file_scan_screen import (
    SecretScanMatch, ResumeDecision,
)

NEW_VERSION, OLD_VERSION = "test-9.9.9", "test-0.9.0"
import scanindex.infra.version as _ver
_ver.get_version = lambda: NEW_VERSION

tmp = tempfile.mkdtemp()
folder = os.path.join(tmp, "kho")
os.makedirs(folder)
f0 = os.path.join(folder, "f0.pdf")
open(f0, "wb").write(b"%%PDF-1.4 fake")
ssp.progress_dir = lambda: os.path.join(tmp, "scan_progress")
os.environ["OCRTOOL_KIE_DEFERRED"] = "1"
os.environ["OCRTOOL_KIE_REFUSED_WAIT_S"] = "0"

st0 = os.stat(f0)
baseline = [{
    "source_path": f0, "relative_path": "f0.pdf", "keyword": "MẬT",
    "page_number": 1, "mode": "Trang đầu", "ocr_pdf_path": "",
    "note": "PDF gốc; trang đầu",
}]

# ── dựng spool bền cho f0: canonical snapshot + manifest + kspool record
spool_dir = os.path.join(tmp, "scan_progress", "kie_spool", "d0test")
os.makedirs(spool_dir, exist_ok=True)
canon_snapshot = os.path.join(spool_dir, "canonical.snapshot")
canon_payload = json.dumps(
    {"pages": [{"page_index": 0, "lines": [
        {"text": "ỦY BAN NHÂN DÂN TỈNH A", "bbox": [10, 5, 300, 20]},
        {"text": "BIÊN BẢN NGÀY 11 THÁNG 2 NĂM 2003", "bbox": [10, 130, 300, 145]},
        {"text": "NGÀY 12 THÁNG 5 NĂM 1999 (bản sao)", "bbox": [10, 170, 300, 185]},
        {"text": "NGÀY 13 THÁNG 6 NĂM 2001 (phụ lục)", "bbox": [10, 210, 300, 225]},
    ]}]},
    ensure_ascii=False).encode("utf-8")
with open(canon_snapshot, "wb") as f:
    f.write(canon_payload); f.flush(); os.fsync(f.fileno())
canon_hash = hashlib.sha256(canon_payload).hexdigest()

prog = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
prog.set_kie_spool("f0.pdf", {
    "canonical": canon_snapshot,
    "spool_dir": spool_dir,
    "hash": canon_hash,
    "ordinals": [0],
    "matches": baseline,
    "fp": [st0.st_size, st0.st_mtime],
    "status": "confirmed_positive",
})
prog.save(); prog.close()

# ── OCR-call counter: hàm scan THẬT KHÔNG ĐƯỢC chạy trong KIE-only resume
ocr_calls = []

def fake_scan(*a, **kw):
    ocr_calls.append(a[0] if a else "?")
    return []

def wrapped(source_path, rel, file_work, first_page_only, cancel, log, **kw):
    legacy = {k: v for k, v in kw.items() if k in ("page_limit", "run_id")}
    return sfss.SecretScanArtifact(
        matches=list(fake_scan(source_path, rel, file_work, first_page_only,
                               cancel, log, **legacy)),
        source_pdf=source_path, canonical={}, status="clean",
    )

sfss.scan_one_file_for_secret_artifact = wrapped

kie_fields = [{"label": "PLACE_DATE", "page_index": 0,
               "text": "ngày 20 tháng 3 năm 2004"}]
coord = ssk.KieCoordinator(
    processes=1,
    executor_factory=lambda n: ThreadPoolExecutor(max_workers=n),
    job_fn=lambda p: {"status": "ready", "fields_by_source": {0: kie_fields}},
)
ssk._coordinator = coord

def run_once():
    summary = {}
    screen = sfss.SecretFileScanScreen()
    screen.log_message.connect(lambda m, lvl="info": print(f"[{lvl}] {m}"))
    screen._scan_finished.connect(lambda s: summary.update(s))
    screen.history_checkbox.setChecked(True)
    screen._busy = True
    screen._cancel_event.clear()
    screen._run_worker(folder, True, ResumeDecision(
        ResumeDecision.RESUME, prog=ssp.SecretScanProgress.load(folder, "fast"),
    ))
    for _ in range(300):
        QCoreApplication.processEvents()
    return summary

summary = run_once()

# BÀI BẮT BUỘC #7: OCR-call counter PHẢI = 0 khi snapshot còn hợp lệ.
assert ocr_calls == [], (
    f"KIE-only resume KHÔNG được OCR lại (OCR-call phải = 0, thấy {ocr_calls})"
)
assert summary.get("scanned") == 1, (
    f"file phải hoàn tất qua KIE-only (thấy {summary})"
)
assert summary.get("failures") == 0, f"không được có lỗi (thấy {summary})"
reg = ssp.FileRegistry.load()
hit = reg.lookup_with_status(f0, st0.st_size, st0.st_mtime, "fast", NEW_VERSION)
reg.close()
assert hit is not None and hit[0] and hit[0][0].get("issue_year") == 2004, (
    f"metadata KIE phải áp lên kết quả resume (thấy {hit})"
)
deadline = time.monotonic() + 5.0
while time.monotonic() < deadline and coord.telemetry()["outstanding"]:
    time.sleep(0.05)
coord.shutdown(wait=True)
print("D2-KIE-ONLY-OK")

# ── Phụ: snapshot HỎNG (hash lệch) → phải quét lại TOÀN BỘ kèm lý do
# Đổi nguồn + fp mới (để cache miss — cache hit hợp lệ thì không cần OCR):
# fp khớp nhưng HASH snapshot lệch → đường KIE-only phải từ chối và quét lại.
with open(f0, "ab") as f:
    f.write(b"-v2")
st0 = os.stat(f0)
prog2 = ssp.SecretScanProgress.create(folder, "fast", OLD_VERSION)
prog2.set_kie_spool("f0.pdf", {
    "canonical": canon_snapshot, "spool_dir": spool_dir,
    "hash": "0" * 64,  # hash SAI
    "ordinals": [0], "matches": baseline,
    "fp": [st0.st_size, st0.st_mtime], "status": "confirmed_positive",
})
prog2.save(); prog2.close()

summary2 = run_once()
assert ocr_calls and ocr_calls[0] == f0, (
    f"snapshot hỏng → phải quét lại toàn bộ (OCR calls: {ocr_calls})"
)
assert summary2.get("scanned") == 1
print("D2-STALE-FULL-RESCAN-OK")
""" % {"repo": os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))}
    _run_worker_scenario_script(script)
