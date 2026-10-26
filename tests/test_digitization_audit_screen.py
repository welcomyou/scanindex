"""Smoke test màn hình 'Thẩm định số hóa' (offscreen, chỉ đọc)."""
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
import fitz
from PySide6.QtWidgets import QApplication, QAbstractItemView

from scanindex.infra import translations
from scanindex.ui.screens import digitization_audit_screen as das
from scanindex.ui.screens.digitization_audit_screen import (
    DigitizationAuditScreen,
)

MDD = "A29.244.01"
PHONG = "A29.244.01.002"


@pytest.fixture(autouse=True)
def _quarantine_quick_cache(tmp_path, monkeypatch):
    """Mọi test màn hình trỏ cache đĩa vào thư mục riêng — không ghi đè
    config/ thật và không nhiễm kết quả giữa các test qua cache."""
    monkeypatch.setattr(das, "_QUICK_CACHE_FILE",
                        str(tmp_path / "quick_cache.json"))


def _qapp():
    return QApplication.instance() or QApplication([])


def _wait(cond, timeout=15.0):
    app = _qapp()
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def _make_tree(root: Path):
    """1 hồ sơ: 1 PDF đạt (300 dpi màu + text), 1 PDF trượt DPI (xám 150
    dpi, không text) để test dấu !; 1 file lạ."""
    hs = root / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    hs.mkdir(parents=True)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2480, 3508))
    pix.set_rect(pix.irect, (30, 90, 200))
    page.insert_image(page.rect, stream=pix.tobytes("png"))
    page.insert_text((72, 100), "Quyết định số 01 về công chức.", fontsize=12)
    doc.save(hs / f"{hs.name}-001.pdf")

    bad = fitz.open()
    bad_page = bad.new_page(width=595, height=842)
    pix_gray = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 1240, 1754))
    pix_gray.clear_with(128)
    bad_page.insert_image(bad_page.rect, stream=pix_gray.tobytes("png"))
    bad.save(hs / f"{hs.name}-002.pdf")

    (hs / "ghi_chu.txt").write_text("x", encoding="utf-8")
    return hs / f"{hs.name}-001.pdf"


def test_screen_tree_folder_stats_and_pdf_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        das, "_SETTINGS_FILE", str(tmp_path / "settings.json"))
    root = tmp_path / "CSDL_SOHOA"
    pdf_path = _make_tree(root)

    _qapp()
    screen = DigitizationAuditScreen()
    screen._set_root(root)
    screen._expand_all_loaded()  # showEvent chỉ chạy khi visible — gọi tay
    assert _wait(lambda: screen._stats is not None), "stats chưa xong"

    # --- cây: gốc trên cùng + 3 cấp thư mục + file -----------------------
    root_item = screen.tree.topLevelItem(0)
    assert root_item is not None
    assert root_item.data(0, das._ROLE_REL) == ""
    assert root_item.text(1) == "Thư mục gốc"
    mdd_item = root_item.child(0)
    assert mdd_item.text(0) == MDD
    phong_item = mdd_item.child(0)
    assert phong_item.text(0) == PHONG
    hs_item = phong_item.child(0).child(0)
    assert hs_item.text(1) == translations.localize_text("Hồ sơ")
    # Số con của hồ sơ: 2 PDF + 1 file khác, không đếm dummy "…" (bỏ dấu !
    # nếu quét nền đã kịp gắn trước lúc assert).
    names = [hs_item.child(i).text(0).removesuffix(" !")
             for i in range(hs_item.childCount())]
    assert names == [f"{hs_item.text(0)}-001.pdf",
                     f"{hs_item.text(0)}-002.pdf",
                     "ghi_chu.txt"]

    # --- bấm thư mục gốc: Số tài liệu / Số trang / Dung lượng ------------
    screen.tree.setCurrentItem(root_item)
    assert _wait(lambda: screen.info_stack.currentIndex() == 1)
    assert _wait(lambda: screen.lbl_document_count.text() == "2")
    assert screen.lbl_page_count.text() == "2"  # mỗi PDF 1 trang
    # Dung lượng = chỉ tính 2 PDF trong subtree (bỏ qua ghi_chu.txt).
    expected_size = sum(p.stat().st_size
                        for p in pdf_path.parent.glob("*.pdf"))
    assert screen.lbl_dir_size.text() == das._fmt_size(expected_size), \
        screen.lbl_dir_size.text()

    # --- bấm PDF: viewer + 5 chỉ tiêu thẩm định --------------------------
    pdf_item = hs_item.child(0)
    screen.tree.setCurrentItem(pdf_item)
    assert _wait(lambda: screen.info_stack.currentIndex() == 2)
    # Kết quả quick của lượt quét cây hiển thị trước; chờ thẩm định full.
    assert _wait(lambda: screen._last_audit is not None
                 and not getattr(screen._last_audit, "quick", False))
    result = screen._last_audit
    assert not result.error
    assert result.dpi_ok is True
    assert result.color_ok is True
    assert result.ocr_ok is True
    assert result.signed is False          # PDF fixture chưa ký số
    assert not result.error
    assert result.dpi_ok is True
    assert result.color_ok is True
    assert result.ocr_ok is True
    # 4 thẻ chỉ tiêu: giá trị + trạng thái màu theo kết luận.
    assert screen.card_color.value.text() == translations.localize_text("Đúng")
    assert screen.card_compression.value.text() == (
        translations.localize_text("Đảm bảo"))
    assert screen.card_ocr.value.text() == translations.localize_text("Đúng")
    assert "dpi" in screen.card_dpi.value.text()
    assert screen.card_sign.value.text() == translations.localize_text(
        "Không")
    # Đạt hết (2 tiêu chí quan trọng) → chip "Đạt". Thiếu PDF/A chỉ là
    # mô tả trung tính trong hàng mô tả — KHÔNG phải cảnh báo ⚠ vàng.
    assert screen.chip_verdict.text() == translations.localize_text("Đạt")
    detail = screen.lbl_audit_detail.text()
    assert "PDF/A" in detail
    assert "không khai báo PDF/A" in detail
    assert "⚠" not in detail
    assert not hasattr(screen, "frm_audit_warn")

    # --- dấu ! trên cây: PDF trượt DPI bị đánh dấu, PDF đạt không -------
    assert _wait(
        lambda: bool(screen.lbl_audit_stats.text())
        and "Đang thẩm định" not in screen.lbl_audit_stats.text(),
    ), "chưa xong thẩm định toàn cây"
    good_item = hs_item.child(0)
    bad_item = hs_item.child(1)
    assert not good_item.text(0).endswith(" !")
    assert bad_item.text(0).endswith(" !")
    assert bad_item.text(0) == f"{hs_item.text(0)}-002.pdf !"
    # Dấu nằm trong _bad_rels → file vào lại cây (refresh) vẫn giữ dấu.
    assert bad_item.data(0, das._ROLE_REL) in screen._bad_rels

    # view-only: không bật sửa tên, không bật kéo thả
    assert screen.tree.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert not screen.tree.dragEnabled()

    screen.pdf_viewer.clear()
    screen.request_cancel()


def test_screen_tiff_badge_preview_and_audit(tmp_path, monkeypatch):
    """Hồ sơ TIFF (quy ước tên trang 5 đoạn): badge TIFF, đếm trang, thẩm
    định đủ 6 thẻ (TIFF đạt / TIFF xám-150dpi trượt dấu !), chip tổng kết."""
    monkeypatch.setattr(
        das, "_SETTINGS_FILE", str(tmp_path / "settings.json"))
    from PIL import Image
    root = tmp_path / "CSDL_SOHOA"
    hs = root / "A38-011-07-0123"          # thư mục hồ sơ chuẩn TIFF
    hs.mkdir(parents=True)
    good = hs / "A38-011-07-0123-001.tif"
    Image.new("RGB", (2480, 3508), (200, 30, 30)).save(
        good, dpi=(300, 300))
    bad = hs / "A38-011-07-0123-002.tif"
    Image.new("L", (1240, 1754), 128).save(bad, dpi=(150, 150))
    (hs / "ghi_chu.txt").write_text("x", encoding="utf-8")

    _qapp()
    screen = DigitizationAuditScreen()
    screen._set_root(root)
    screen._expand_all_loaded()
    assert _wait(lambda: screen._stats is not None)
    screen.tree.setCurrentItem(screen.tree.topLevelItem(0))
    assert _wait(lambda: screen.info_stack.currentIndex() == 1)
    # Hồ sơ chỉ có TIFF: "Số tài liệu" đếm PDF (0 → ẩn thẻ), 2 tệp TIFF
    # = 2 trang.
    assert _wait(lambda: screen.lbl_page_count.text() == "2")
    assert not screen.card_document_count.isVisible()

    good_item = screen._item_by_rel[
        "A38-011-07-0123/A38-011-07-0123-001.tif"]
    assert good_item.text(1) == translations.localize_text("TIFF")

    # Bấm TIFF đạt: trang preview + đủ tiêu chí nặng.
    screen.tree.setCurrentItem(good_item)
    assert _wait(lambda: screen.info_stack.currentIndex() == 2)
    assert _wait(lambda: isinstance(screen._last_audit,
                                    das.da.TiffAuditResult))
    result = screen._last_audit
    assert not result.error
    assert result.color_ok is True
    assert result.dpi_ok is True
    assert result.single_page_ok is True
    assert result.name_ok is True
    assert screen.card_ocr.value.text() == translations.localize_text("1 trang")
    assert screen.card_compression.value.text() == (
        translations.localize_text("Không nén"))
    assert screen.card_name.value.text() == translations.localize_text("Đúng")
    assert not screen.card_sign.isVisible()
    assert screen.chip_verdict.text() == translations.localize_text("Đạt")
    # Preview không được ép khung phình: nạp pixmap xong khung phải vẫn
    # thu nhỏ được (min width nhỏ hơn khổ cửa sổ thường).
    assert _wait(lambda: screen.img_viewer.pixmap() is not None
                 and not screen.img_viewer.pixmap().isNull())
    assert screen.info_stack.minimumSizeHint().width() < 750  # ngưỡng nới cho font offscreen

    # TIFF xám 150 dpi → trượt nặng, dấu ! trên cây.
    assert _wait(
        lambda: bool(screen.lbl_audit_stats.text())
        and "Đang thẩm định" not in screen.lbl_audit_stats.text(),
    )
    bad_item = screen._item_by_rel[
        "A38-011-07-0123/A38-011-07-0123-002.tif"]
    assert bad_item.text(0).endswith(" !")
    screen.tree.setCurrentItem(bad_item)
    assert _wait(lambda: getattr(screen._last_audit, "path", "").endswith(
        "A38-011-07-0123-002.tif") and not screen._last_audit.error)
    assert screen._last_audit.color_ok is False
    assert screen.chip_verdict.text() == (
        translations.localize_text("Không đạt"))

    screen.request_cancel()


def test_info_panel_shrinkable(tmp_path, monkeypatch):
    """Khung bên phải (thẻ chỉ tiêu + viewer) phải kéo hẹp được ở khổ cửa
    sổ thường: hàng 6 thẻ ngang cũ ép chiều rộng tối thiểu ~900px làm
    splitter kẹt (phải fullscreen mới kéo nhỏ); lưới 2×3 phải hạ xuống
    dưới ~700px."""
    monkeypatch.setattr(
        das, "_SETTINGS_FILE", str(tmp_path / "settings.json"))
    _qapp()
    screen = DigitizationAuditScreen()
    min_w = screen.info_stack.minimumSizeHint().width()
    assert 0 < min_w < 750, f"khung phải bị kẹt ở min width {min_w}px"
    screen.request_cancel()


def test_screen_remembers_root_in_settings(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(das, "_SETTINGS_FILE", str(settings_file))
    root = tmp_path / "CSDL_SOHOA"
    _make_tree(root)

    _qapp()
    screen = DigitizationAuditScreen()
    screen._set_root(root)
    screen._save_settings()
    assert settings_file.exists()
    import json
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["root"] == str(root)


def test_changed_file_reevaluated(tmp_path, monkeypatch):
    """File đổi (size + mtime) → lượt quét lại phải xét lại: dấu ! biến
    mất; file chưa đổi trong phiên thì tái sử dụng kết quả quick."""
    monkeypatch.setattr(
        das, "_SETTINGS_FILE", str(tmp_path / "settings.json"))
    root = tmp_path / "CSDL_SOHOA"
    hs = root / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    hs.mkdir(parents=True)
    bad_path = hs / f"{hs.name}-001.pdf"

    # Lần 1: PDF xám 150 dpi (không text) → trượt DPI → có dấu !.
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 1240, 1754))
    pix.clear_with(128)
    page.insert_image(page.rect, stream=pix.tobytes("png"))
    doc.save(bad_path)

    _qapp()
    screen = DigitizationAuditScreen()
    screen._set_root(root)
    screen._expand_all_loaded()
    assert _wait(lambda: bool(screen.lbl_audit_stats.text())
                 and "Đang thẩm định" not in screen.lbl_audit_stats.text())
    item = screen._item_by_rel[
        f"{MDD}/{PHONG}/01/{hs.name}/{hs.name}-001.pdf"]
    assert item.text(0).endswith(" !")

    # Sửa file thành PDF màu 300 dpi có text (size + mtime đổi).
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2480, 3508))
    pix.set_rect(pix.irect, (30, 90, 200))
    page.insert_image(page.rect, stream=pix.tobytes("png"))
    page.insert_text((72, 100), "Quyết định số 01 về công chức.",
                     fontsize=12)
    doc.save(str(bad_path) + ".new")
    os.replace(str(bad_path) + ".new", str(bad_path))

    # Quét lại (Làm mới): file đã đổi phải được xét lại — dấu ! biến mất.
    screen._refresh_tree()
    screen._expand_all_loaded()   # cây đã clear — nạp lại trước khi đọc item
    assert _wait(lambda: bool(screen.lbl_audit_stats.text())
                 and "Đang thẩm định" not in screen.lbl_audit_stats.text())
    item = screen._item_by_rel[
        f"{MDD}/{PHONG}/01/{hs.name}/{hs.name}-001.pdf"]
    assert not item.text(0).endswith(" !")
    screen.request_cancel()


class _FakeChunkPool:
    """Pool giả đúng interface AuditPool: future của từng chunk do test
    điều khiển, để chứng minh kết quả phát theo luồng chứ không gom đủ."""

    max_workers = 2

    def __init__(self):
        self.futures: list = []

    def ensure_started(self) -> bool:
        return True

    def map_chunks(self, paths, *, quick, policy, chunk):
        from concurrent.futures import Future
        futs = []
        for i in range(0, len(paths), chunk):
            futs.append(Future())
            self.futures.append(futs[-1])
        return futs


def _make_flat_pdfs(root: Path, n: int) -> list:
    """n PDF rỗng ngay gốc (nhanh, không ảnh) — đủ chia 2 chunk."""
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(1, n + 1):
        p = root / f"A38-011-07-{i:04d}.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(p)
        doc.close()
        paths.append(p)
    return paths


def _quick_chunk(paths):
    return [das.da.audit_file(str(p), quick=True, policy=das._AUDIT_POLICY)
            for p in paths]


def test_pooled_audit_streams_first_chunk_before_all_done(tmp_path):
    """Kho lớn phải thấy kết quả dần: chunk đầu xong là phát ngay 4 kết quả
    đầu dù chunk sau chưa xong — bản cũ gom đủ MỌI chunk mới trả iterator,
    khiến nhãn kẹt 'Đang khởi động bộ quét…' và bar chạy chạy cả lượt."""
    _qapp()
    paths = _make_flat_pdfs(tmp_path, 8)     # 2 chunk × 4 file
    worker = das._TreeAuditWorker(tmp_path,
                                  pool=_FakeChunkPool())
    pool = worker._pool
    it = worker._run_pooled(paths)
    assert it is not None

    pool.futures[0].set_result(_quick_chunk(paths[:4]))
    got = [next(it) for _ in range(4)]
    assert [p for p, _ in got] == paths[:4]      # đúng thứ tự file
    assert not pool.futures[1].done()            # chunk sau chưa xong mà

    pool.futures[1].set_result(_quick_chunk(paths[4:]))
    got += list(it)
    assert [p for p, _ in got] == paths
    assert all(not r.error for _, r in got)


def test_pooled_audit_chunk_failure_keeps_streamed_serializes_rest(tmp_path):
    """Chunk lỗi giữa chừng: phần đã phát giữ nguyên, đoạn chưa phát nối
    tuần tự — vẫn đủ 8 kết quả đúng thứ tự (bản cũ bỏ cả kho và quét lại
    toàn bộ tuần tự)."""
    _qapp()
    paths = _make_flat_pdfs(tmp_path, 8)
    worker = das._TreeAuditWorker(tmp_path,
                                  pool=_FakeChunkPool())
    pool = worker._pool
    it = worker._run_pooled(paths)

    pool.futures[0].set_result(_quick_chunk(paths[:4]))
    first4 = [next(it) for _ in range(4)]
    pool.futures[1].set_exception(RuntimeError("worker chết"))
    rest = list(it)

    got = first4 + rest
    assert [p for p, _ in got] == paths
    assert all(not r.error for _, r in got)
    # Chunk sau raise mà vẫn đủ 8 kết quả → đoạn cuối chắc chắn đến từ
    # nhánh tuần tự nối tiếp phần đã phát.


def _cache_entries_for(paths) -> dict:
    """Entry cache đĩa từ kết quả quick thật của các file (mô phỏng những
    gì GUI thu thập sau một lượt quét)."""
    entries = {}
    for p in paths:
        st = p.stat()
        entries[str(p)] = {
            "t": "tiff" if p.suffix.lower() in (".tif", ".tiff") else "pdf",
            "s": st.st_size, "m": st.st_mtime_ns,
            "r": asdict(das.da.audit_file(str(p), quick=True,
                                          policy=das._AUDIT_POLICY)),
        }
    return entries


def test_quick_disk_cache_skips_reaudit_of_unchanged_files(tmp_path,
                                                           monkeypatch):
    """Kho nguyên vẹn, lượt sau (khác phiên app) phải dùng cache đĩa: không
    gọi audit_file lần nào mà vẫn đủ thống kê và dấu trên cây."""
    paths = _make_flat_pdfs(tmp_path, 4)
    cache_file = tmp_path / "quick_cache.json"
    das._save_quick_cache(_cache_entries_for(paths), str(cache_file))

    def _must_not_audit(*a, **k):
        raise AssertionError("kho chưa đổi mà vẫn audit lại")
    monkeypatch.setattr(das.da, "audit_file", _must_not_audit)

    w = das._TreeAuditWorker(tmp_path, cache_path=str(cache_file))
    stats_box, marks = [], []
    w.stats_done.connect(stats_box.append)
    w.marked_batch.connect(marks.append)
    w.run()
    assert len(stats_box) == 1
    assert stats_box[0].total_docs == 4
    assert stats_box[0].total_pages == 4
    assert sum(len(m) for m in marks) == 4


def test_quick_disk_cache_reaudits_changed_file(tmp_path, monkeypatch):
    """File đổi (size+mtime lệch cache) là file duy nhất được audit lại;
    các file còn lại vẫn tái sử dụng và thống kê trang theo nội dung mới."""
    paths = _make_flat_pdfs(tmp_path, 4)
    cache_file = tmp_path / "quick_cache.json"
    das._save_quick_cache(_cache_entries_for(paths), str(cache_file))

    changed = paths[2]
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    doc.save(str(changed) + ".new")
    doc.close()
    os.replace(str(changed) + ".new", str(changed))

    real = das.da.audit_file
    audited = []

    def _spy(path, **k):
        audited.append(path)
        return real(path, **k)

    monkeypatch.setattr(das.da, "audit_file", _spy)

    w = das._TreeAuditWorker(tmp_path, cache_path=str(cache_file))
    stats_box = []
    w.stats_done.connect(stats_box.append)
    w.run()
    assert [Path(p) for p in audited] == [changed]
    assert stats_box[0].total_docs == 4
    assert stats_box[0].total_pages == 5      # 3 file 1 trang + file mới 2


def test_revive_quick_result_roundtrip(tmp_path):
    """asdict → _revive_quick_result phải dựng lại đúng dataclass PDF/TIFF
    ngang bằng bản gốc; entry hỏng trả None thay vì làm sập lượt quét."""
    from PIL import Image

    pdf = tmp_path / "A38-011-07-0001.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(pdf)
    doc.close()
    pdf_res = das.da.audit_file(str(pdf), quick=True,
                                policy=das._AUDIT_POLICY)
    revived = das._revive_quick_result(
        {"t": "pdf", "s": 1, "m": 2, "r": asdict(pdf_res)})
    assert type(revived) is das.da.PdfAuditResult
    assert revived == pdf_res

    tif = tmp_path / "A38-011-07-0001.tif"
    Image.new("RGB", (40, 40)).save(tif, dpi=(300, 300))
    tif_res = das.da.audit_file(str(tif), quick=True,
                                policy=das._AUDIT_POLICY)
    revived_t = das._revive_quick_result(
        {"t": "tiff", "s": 1, "m": 2, "r": asdict(tif_res)})
    assert type(revived_t) is das.da.TiffAuditResult
    assert revived_t == tif_res

    assert das._revive_quick_result({"t": "pdf", "r": {"pages": "hỏng"}}) \
        is None


def test_quick_cache_persists_across_scan_sessions(tmp_path, monkeypatch):
    """Trọn vòng đời qua hai màn hình (hai phiên): lượt 1 quét thật rồi ghi
    cache; lượt 2 trên cùng kho không gọi audit_file lần nào."""
    monkeypatch.setattr(
        das, "_SETTINGS_FILE", str(tmp_path / "settings.json"))
    cache_file = tmp_path / "quick_cache.json"
    root = tmp_path / "CSDL_SOHOA"
    _make_tree(root)

    _qapp()
    screen1 = DigitizationAuditScreen()
    screen1._set_root(root)
    assert _wait(lambda: bool(screen1.lbl_audit_stats.text())
                 and "Đang thẩm định" not in screen1.lbl_audit_stats.text())
    screen1.request_cancel()
    assert _wait(lambda: cache_file.exists()), "cache chưa được ghi đĩa"

    calls = []
    real = das.da.audit_file

    def _spy(path, **k):
        calls.append(path)
        return real(path, **k)

    monkeypatch.setattr(das.da, "audit_file", _spy)

    screen2 = DigitizationAuditScreen()
    screen2._set_root(root)
    assert _wait(lambda: bool(screen2.lbl_audit_stats.text())
                 and "Đang thẩm định" not in screen2.lbl_audit_stats.text())
    assert not calls, "lượt 2 phải dùng cache đĩa, không audit lại"
    screen2.request_cancel()


def test_audit_pool_worker_count_from_env(monkeypatch):
    """Số worker audit: mặc định theo CPU (2–12), env override tự do trong
    trần 32, giá trị rác rơi về mặc định."""
    monkeypatch.delenv("OCRTOOL_AUDIT_WORKERS", raising=False)
    assert 2 <= das.ap._configured_workers() <= 12
    monkeypatch.setenv("OCRTOOL_AUDIT_WORKERS", "9")
    assert das.ap._configured_workers() == 9
    monkeypatch.setenv("OCRTOOL_AUDIT_WORKERS", "99")
    assert das.ap._configured_workers() == 32
    monkeypatch.setenv("OCRTOOL_AUDIT_WORKERS", "không phải số")
    assert 2 <= das.ap._configured_workers() <= 12
