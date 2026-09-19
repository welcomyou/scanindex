from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scanindex.core import secret_scan_progress as ssp


@pytest.fixture(autouse=True)
def _progress_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ssp, "progress_dir", lambda: str(tmp_path / "scan_progress")
    )
    return tmp_path / "scan_progress"


FOLDER = r"D:\tailieu"


def _make(folder: str = FOLDER, mode: str = "fast") -> ssp.SecretScanProgress:
    return ssp.SecretScanProgress.create(folder, mode)


def test_create_save_load_roundtrip() -> None:
    prog = _make()
    prog.record_file("a.pdf", "ok")
    prog.record_file("b.pdf", "err", "PDF hỏng")
    prog.record_matches([{"keyword": "Mật", "page_number": 1}])
    prog.save()

    loaded = ssp.SecretScanProgress.load(FOLDER, "fast")
    assert loaded is not None
    assert loaded.done_files() == {"a.pdf"}
    done, errors, skipped, found = loaded.stats()
    assert (done, errors, skipped, found) == (1, 1, 0, 1)
    assert loaded.matches == [{"keyword": "Mật", "page_number": 1}]
    prog.discard()


def test_skip_state_is_not_retried_on_resume() -> None:
    """File hỏng (0 byte / không phải PDF) ghi trạng thái "skip": tính vào
    done_files() để resume bỏ qua luôn, tách khỏi _errors, giữ nguyên lý do."""
    prog = _make()
    prog.record_file("ok.pdf", "ok")
    prog.record_file("rac.pdf", "skip", "Không phải PDF hợp lệ (nội dung hỏng)")
    prog.record_file("trong.pdf", "skip", "File rỗng (0 byte)")
    prog.record_file("loi.pdf", "err", "OCR thất bại")
    assert prog.done_files() == {"ok.pdf", "rac.pdf", "trong.pdf"}
    assert prog.stats() == (1, 1, 2, 0)

    prog.save()
    loaded = ssp.SecretScanProgress.load(FOLDER, "fast")
    assert loaded is not None
    assert loaded.done_files() == {"ok.pdf", "rac.pdf", "trong.pdf"}
    assert loaded._skipped == {
        "rac.pdf": "Không phải PDF hợp lệ (nội dung hỏng)",
        "trong.pdf": "File rỗng (0 byte)",
    }
    assert loaded._errors == {"loi.pdf": "OCR thất bại"}

    # File hỏng sau đó "ok" (được sửa/chép lại) → phải rời khỏi skip.
    loaded.record_file("rac.pdf", "ok")
    assert loaded.done_files() == {"ok.pdf", "rac.pdf", "trong.pdf"}
    assert loaded._skipped == {"trong.pdf": "File rỗng (0 byte)"}
    prog.discard()
    loaded.discard()


def test_skip_state_survives_compaction() -> None:
    prog = _make()
    prog.record_file("rac.pdf", "skip", "PDF không có trang")
    prog.record_file("ok.pdf", "ok")
    prog.save()
    prog.close()

    loaded = ssp.SecretScanProgress.load(FOLDER, "fast")
    assert loaded is not None
    # Nén ngay: snapshot phải giữ đủ dòng skip.
    loaded._compact()
    reloaded = ssp.SecretScanProgress.load(FOLDER, "fast")
    assert reloaded is not None
    assert reloaded.done_files() == {"ok.pdf", "rac.pdf"}
    assert reloaded._skipped == {"rac.pdf": "PDF không có trang"}
    prog.discard()
    loaded.discard()
    reloaded.discard()


def test_error_overrides_ok_for_same_file() -> None:
    prog = _make()
    prog.record_file("x.pdf", "ok")
    prog.record_file("x.pdf", "err", "lỗi sau")
    assert prog.done_files() == set()
    prog.save()

    loaded = ssp.SecretScanProgress.load(FOLDER, "fast")
    assert loaded.done_files() == set()
    assert loaded._errors == {"x.pdf": "lỗi sau"}
    prog.discard()


def test_mode_and_folder_keyed_separately() -> None:
    prog = _make(mode="fast")
    prog.record_file("a.pdf", "ok")
    prog.save()
    prog2 = _make(folder=r"D:\khac", mode="fast")
    prog2.record_file("b.pdf", "ok")
    prog2.save()

    assert ssp.SecretScanProgress.load(FOLDER, "thorough") is None
    assert ssp.SecretScanProgress.load(r"D:\khac", "fast").done_files() == {"b.pdf"}
    prog.discard()
    prog2.discard()


def test_load_missing_or_garbage_returns_none() -> None:
    assert ssp.SecretScanProgress.load(r"D:\chua_quet", "fast") is None
    path = ssp.journal_path(FOLDER, "fast")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{dòng rác không phải json\n")
    assert ssp.SecretScanProgress.load(FOLDER, "fast") is None
    # File rỗng (header chưa kịp ghi khi mất điện).
    with open(path, "w", encoding="utf-8") as f:
        f.write("")
    assert ssp.SecretScanProgress.load(FOLDER, "fast") is None


def test_torn_tail_keeps_prior_events() -> None:
    """Mất điện giữa lúc ghi dòng cuối: replay giữ mọi sự kiện trước đó."""
    prog = _make()
    prog.record_file("a.pdf", "ok")
    prog.record_file("b.pdf", "ok")
    prog.save()
    prog.close()
    with open(ssp.journal_path(FOLDER, "fast"), "a", encoding="utf-8") as f:
        f.write('{"t":"f","p":"c.p')  # dòng bị đứt giữa chừng

    loaded = ssp.SecretScanProgress.load(FOLDER, "fast")
    assert loaded.done_files() == {"a.pdf", "b.pdf"}


def test_discard_removes_journal() -> None:
    prog = _make()
    prog.record_file("a.pdf", "ok")
    prog.save()
    assert os.path.isfile(ssp.journal_path(FOLDER, "fast"))
    prog.discard()
    assert not os.path.isfile(ssp.journal_path(FOLDER, "fast"))
    prog.discard()  # discard lần nữa không lỗi


def test_save_is_atomic_no_tmp_leftover() -> None:
    prog = _make()
    prog.record_file("a.pdf", "ok")
    prog.save()
    leftovers = [
        n for n in os.listdir(ssp.progress_dir()) if n.endswith(".tmp")
    ]
    assert leftovers == []
    with open(ssp.journal_path(FOLDER, "fast"), encoding="utf-8") as f:
        header = json.loads(f.readline())
    assert header["t"] == "h" and header["folder"] == FOLDER
    prog.discard()


def test_compaction_collapses_overwritten_events(monkeypatch) -> None:
    """Ghi quá ngưỡng (floor hạ thấp) → journal được nén: các sự kiện cũ
    bị ghi đè (ok→err→ok trên cùng file) phải gộp lại thành 1 dòng/file."""
    monkeypatch.setattr(ssp, "_JOURNAL_FLOOR_LINES", 5)
    prog = _make()
    # Cùng 1 file ghi 20 lần — journal thô sẽ có 20 dòng, nén còn 1.
    for i in range(10):
        prog.record_file("x.pdf", "ok")
        prog.record_file("x.pdf", "err", f"lần {i}")
    prog.record_matches([{"keyword": "Mật", "page_number": 1}])
    prog.save()  # 21 dòng > floor 5 → nén

    assert prog._appended_lines == 0  # compact đã chạy
    with open(ssp.journal_path(FOLDER, "fast"), encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 3  # 1 header + 1 file (trạng thái cuối) + 1 match
    file_events = [e for e in lines if e.get("t") == "f"]
    assert len(file_events) == 1 and file_events[0]["s"] == "err"

    loaded = ssp.SecretScanProgress.load(FOLDER, "fast")
    assert loaded.done_files() == set()
    assert loaded._errors == {"x.pdf": "lần 9"}
    assert len(loaded.matches) == 1
    prog.discard()


def test_compaction_on_loaded_journal(monkeypatch) -> None:
    """Resume từ journal lớn: nén phải kích hoạt trên nền snapshot đã có sẵn."""
    monkeypatch.setattr(ssp, "_JOURNAL_FLOOR_LINES", 5)
    prog = _make()
    for i in range(20):
        prog.record_file(f"f{i}.pdf", "ok")
    prog.save()
    prog.close()

    loaded = ssp.SecretScanProgress.load(FOLDER, "fast")
    assert loaded is not None
    for i in range(30):
        loaded.record_file(f"g{i}.pdf", "ok")
    loaded.save()  # 30 dòng mới > max(5, 21 dòng sẵn) → nén

    assert loaded._appended_lines == 0
    with open(ssp.journal_path(FOLDER, "fast"), encoding="utf-8") as f:
        line_count = sum(1 for _ in f)
    assert line_count == 1 + 20 + 30  # header + 20 file cũ + 30 file mới
    assert ssp.SecretScanProgress.load(FOLDER, "fast").done_files() >= {
        "f0.pdf", "g29.pdf"
    }
    loaded.discard()


def test_prune_stale_removes_old_keeps_new() -> None:
    old = ssp.journal_path(r"D:\cu", "fast")
    new = ssp.journal_path(r"D:\moi", "fast")
    for path in (old, new):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
    stale_ts = time.time() - 40 * 86400
    os.utime(old, (stale_ts, stale_ts))

    assert ssp.prune_stale(max_age_days=30) == 1
    assert not os.path.isfile(old)
    assert os.path.isfile(new)


def test_prune_stale_removes_orphan_tmp() -> None:
    directory = ssp.progress_dir()
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, "secret_scan_abc123.jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("{}")
    stale_ts = time.time() - 3 * 86400
    os.utime(tmp, (stale_ts, stale_ts))

    assert ssp.prune_stale() == 1
    assert not os.path.isfile(tmp)


def test_scale_record_then_load_100k_files() -> None:
    """Mô phỏng quy mô lớn: 100k file — ghi tiếp O(1)/file, load nhanh.

    Đây là test 'hơi' chậm (~vài giây) nhưng bảo đảm kiến trúc journal
    không thoái hóa theo số file như bản ghi-lại-toàn-bộ-JSON.
    """
    prog = _make()
    t0 = time.perf_counter()
    for i in range(100_000):
        prog.record_file(f"thu_muc_con/file_{i:06d}.pdf", "ok")
    prog.record_file("loi.pdf", "err", "hỏng")
    prog.save()
    record_secs = time.perf_counter() - t0

    t0 = time.perf_counter()
    loaded = ssp.SecretScanProgress.load(FOLDER, "fast")
    load_secs = time.perf_counter() - t0

    assert loaded.done_files() == {f"thu_muc_con/file_{i:06d}.pdf" for i in range(100_000)}
    assert loaded.stats() == (100_000, 1, 0, 0)
    # Ghi tiếp phải nhanh (nếu vẫn là ghi-lại-toàn-bộ thì 100k lần ghi
    # sẽ mất hàng phút). Nới lỏng để tránh test giật theo máy chậm.
    assert record_secs < 30, f"ghi {record_secs:.1f}s quá chậm"
    assert load_secs < 15, f"load {load_secs:.1f}s quá chậm"
    prog.discard()


def test_split_pending_skips_done_files() -> None:
    from scanindex.ui.screens.secret_file_scan_screen import _split_pending

    folder = r"D:\tailieu"
    files = [
        os.path.join(folder, "a.pdf"),
        os.path.join(folder, "b.pdf"),
        os.path.join(folder, "sub", "c.pdf"),
    ]
    pending, skipped = _split_pending(files, folder, {"a.pdf"})
    assert skipped == 1
    assert pending == [(2, files[1]), (3, files[2])]


def test_split_pending_nothing_done() -> None:
    from scanindex.ui.screens.secret_file_scan_screen import _split_pending

    files = [r"D:\tailieu\a.pdf"]
    pending, skipped = _split_pending(files, r"D:\tailieu", set())
    assert skipped == 0
    assert pending == [(1, files[0])]


# ── FileRegistry: sổ file toàn cục (bỏ qua file đã quét, không đổi) ──────

VER = "1.2.0"
MATCH = [{"keyword": "Mật", "page_number": 1, "note": "x"}]


def _reg() -> ssp.FileRegistry:
    return ssp.FileRegistry.load()  # chưa có gì → registry rỗng


def test_registry_record_lookup_roundtrip() -> None:
    reg = _reg()
    reg.record(r"D:\a\b\c.pdf", 123, 1000.5, "fast", VER, MATCH)
    reg.save()

    loaded = ssp.FileRegistry.load()
    hit = loaded.lookup(r"D:\a\b\c.pdf", 123, 1000.5, "fast", VER)
    assert hit == MATCH


def test_registry_miss_when_file_changed() -> None:
    reg = _reg()
    reg.record(r"D:\a\c.pdf", 123, 1000.5, "fast", VER, MATCH)
    assert reg.lookup(r"D:\a\c.pdf", 999, 1000.5, "fast", VER) is None  # đổi size
    assert reg.lookup(r"D:\a\c.pdf", 123, 2000.0, "fast", VER) is None  # đổi mtime


def test_registry_miss_when_version_or_mode_differs() -> None:
    reg = _reg()
    reg.record(r"D:\a\c.pdf", 123, 1000.5, "fast", VER, MATCH)
    assert reg.lookup(r"D:\a\c.pdf", 123, 1000.5, "fast", "9.9.9") is None
    assert reg.lookup(r"D:\a\c.pdf", 123, 1000.5, "thorough", VER) is None


def test_registry_case_insensitive_path_on_windows() -> None:
    reg = _reg()
    reg.record(r"D:\A\Report.PDF", 10, 5.0, "fast", VER, MATCH)
    assert reg.lookup(r"d:\a\report.pdf", 10, 5.0, "fast", VER) == MATCH


def test_registry_cross_folder_semantics() -> None:
    """Quét xong b/c (ghi registry) → quét a/ tra cùng đường dẫn file vẫn hit."""
    reg = _reg()
    for name in ("p1.pdf", "p2.pdf"):
        reg.record(os.path.join(r"D:\a\b\c", name), 50, 7.0, "fast", VER, [])
    reg.save()

    loaded = ssp.FileRegistry.load()
    assert loaded.lookup(
        os.path.join(r"D:\a\b\c", "p1.pdf"), 50, 7.0, "fast", VER
    ) == []
    assert loaded.lookup(os.path.join(r"D:\a\b\c", "p2.pdf"), 50, 7.0, "fast", VER) == []
    assert loaded.lookup(os.path.join(r"D:\a\b\c", "p3.pdf"), 50, 7.0, "fast", VER) is None


def test_registry_record_overwrites_old_entry() -> None:
    reg = _reg()
    reg.record(r"D:\a\c.pdf", 123, 1000.5, "fast", VER, [])
    reg.record(r"D:\a\c.pdf", 200, 2000.0, "fast", VER, MATCH)
    assert reg.lookup(r"D:\a\c.pdf", 200, 2000.0, "fast", VER) == MATCH
    assert reg.lookup(r"D:\a\c.pdf", 123, 1000.5, "fast", VER) is None


def test_registry_compaction(monkeypatch) -> None:
    monkeypatch.setattr(ssp, "_JOURNAL_FLOOR_LINES", 5)
    reg = _reg()
    for i in range(10):
        reg.record(r"D:\a\c.pdf", i, float(i), "fast", VER, MATCH)  # cùng key
    reg.save()  # 10 dòng > floor 5 → nén còn header + 1 entry

    assert reg._appended_lines == 0
    with open(ssp.registry_path(), encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 2  # header + 1 entry cuối cùng
    loaded = ssp.FileRegistry.load()
    assert loaded.lookup(r"D:\a\c.pdf", 9, 9.0, "fast", VER) == MATCH


def test_registry_corrupt_file_degrades_to_empty() -> None:
    os.makedirs(ssp.progress_dir(), exist_ok=True)
    with open(ssp.registry_path(), "w", encoding="utf-8") as f:
        f.write("không phải json\n")
    reg = ssp.FileRegistry.load()
    assert reg.lookup(r"D:\a\c.pdf", 1, 1.0, "fast", VER) is None
    # Vẫn ghi được tiếp sau khi phục hồi rỗng.
    reg.record(r"D:\a\c.pdf", 1, 1.0, "fast", VER, MATCH)
    reg.save()
    reg.close()
    assert ssp.FileRegistry.load().lookup(r"D:\a\c.pdf", 1, 1.0, "fast", VER) == MATCH


def test_journal_torn_tail_self_heals_for_future_appends() -> None:
    """Đuôi đứt được cắt bỏ khi load: sự kiện ghi SAU khi resume phải đọc
    lại được (không nằm phía sau dòng hỏng vĩnh viễn)."""
    prog = _make()
    prog.record_file("a.pdf", "ok")
    prog.save()
    prog.close()
    with open(ssp.journal_path(FOLDER, "fast"), "a", encoding="utf-8") as f:
        f.write('{"t":"f","p":"c.p')  # mất điện giữa lúc ghi

    resumed = ssp.SecretScanProgress.load(FOLDER, "fast")
    assert resumed.done_files() == {"a.pdf"}
    resumed.record_file("b.pdf", "ok")  # ghi tiếp sau khi phục hồi
    resumed.save()
    resumed.close()

    again = ssp.SecretScanProgress.load(FOLDER, "fast")
    assert again.done_files() == {"a.pdf", "b.pdf"}


def test_clear_all_removes_journal_and_registry() -> None:
    prog = _make()
    prog.record_file("a.pdf", "ok")
    prog.save()
    reg = _reg()
    reg.record(r"D:\a\c.pdf", 1, 1.0, "fast", VER, MATCH)
    reg.save()
    directory = ssp.progress_dir()
    before = os.listdir(directory)
    assert any(n.startswith("secret_scan_") for n in before)
    assert any(n.startswith("file_registry") for n in before)

    # Như màn hình: đóng handle cuối lượt quét rồi mới xóa được trên Windows.
    prog.close()
    reg.close()
    removed = ssp.clear_all()

    assert removed == len(before)
    assert os.listdir(directory) == []
    assert ssp.SecretScanProgress.load(FOLDER, "fast") is None
    assert ssp.FileRegistry.load().lookup(r"D:\a\c.pdf", 1, 1.0, "fast", VER) is None
