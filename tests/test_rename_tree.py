"""Tests cho scanindex.core.rename_tree (đổi tên theo cây CSDL_SOHOA)."""
import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scanindex.core import rename_tree as rt


MDD = "A29.244.01"
PHONG = "A29.244.01.002"
PHONG2 = "A29.244.01.005"


def _make_tree(root: Path) -> None:
    """Fixture cây CSDL_SOHOA chuẩn + một số case lệch."""
    def dossier(phong: str, ml: str, hs: str, pdfs: list[str], extra=""):
        d = root / MDD / phong / ml / f"{MDD}-{phong}-{ml}-{hs}"
        d.mkdir(parents=True)
        for p in pdfs:
            name = f"{MDD}-{phong}-{ml}-{hs}-{p}.pdf"
            (d / name).write_bytes(name.encode("ascii"))
        if extra:
            (d / extra).write_bytes(b"x")
        return d

    dossier(PHONG, "01", "0001", ["001", "002"], extra="notes.txt")
    dossier(PHONG, "01", "0002", ["001"])
    dossier(PHONG, "02", "0001", ["001"])
    dossier(PHONG2, "01", "0003", ["001"])
    # Thư mục hồ sơ sai quy ước → bị skip khi cascade.
    bad = root / MDD / PHONG / "01" / "SaiTen"
    bad.mkdir(parents=True)
    (bad / f"{MDD}-{PHONG}-01-0001-001.pdf").write_bytes(b"x")


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "CSDL_SOHOA"
    root.mkdir()
    _make_tree(root)
    return root


# ---------------------------------------------------------------- parsers

def test_parse_dossier_and_pdf_names():
    d = rt.parse_dossier_folder_name(f"{MDD}-{PHONG}-01-0001")
    assert d == rt.DossierName(MDD, PHONG, "01", "0001")
    assert rt.parse_dossier_folder_name("SaiTen") is None
    assert rt.parse_dossier_folder_name("a-b-c") is None
    assert rt.parse_dossier_folder_name("a-b-c-d-e") is None

    p = rt.parse_pdf_name(f"{MDD}-{PHONG}-01-0001-012.PDF")
    assert p.stt == "012" and p.ext == ".PDF"
    assert p.compose() == f"{MDD}-{PHONG}-01-0001-012.PDF"
    assert rt.parse_pdf_name("tailieu.pdf") is None
    assert rt.parse_pdf_name(f"{MDD}-{PHONG}-01-0001.pdf") is None  # thiếu STT
    assert rt.parse_pdf_name(f"{MDD}-{PHONG}-01-0001-001.docx") is None


def test_validate_component_rejects_bad_values():
    with pytest.raises(ValueError):
        rt.validate_component("Mã định danh", "  ")
    with pytest.raises(ValueError):
        rt.validate_component("Mã định danh", "a/b")
    with pytest.raises(ValueError):
        rt.validate_component("Phông", "a-b")  # dấu "-" phá cú pháp tách đoạn
    assert rt.validate_component("Mục lục", " 02 ") == "02"
    # Độ rộng bắt buộc: mục lục 2 chữ số, hồ sơ 4 chữ số.
    assert rt.validate_component("Mục lục", "02",
                                 level=rt.Level.MUC_LUC) == "02"
    with pytest.raises(ValueError, match="đúng 2 chữ số"):
        rt.validate_component("Mục lục", "2", level=rt.Level.MUC_LUC)
    with pytest.raises(ValueError, match="đúng 2 chữ số"):
        rt.validate_component("Mục lục", "0a", level=rt.Level.MUC_LUC)
    assert rt.validate_component("Hồ sơ", "0009",
                                 level=rt.Level.HO_SO) == "0009"
    with pytest.raises(ValueError, match="đúng 4 chữ số"):
        rt.validate_component("Hồ sơ", "009", level=rt.Level.HO_SO)
    # Mã định danh / phông không bị ép độ rộng.
    assert rt.validate_component("Phông", "B.009") == "B.009"


def test_rename_width_enforced_in_planner(tree):
    """plan_folder_rename ép độ rộng: hồ sơ 4 số, mục lục 2 số."""
    hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0002"
    with pytest.raises(ValueError, match="đúng 4 chữ số"):
        rt.plan_folder_rename(tree, hs, "009")
    with pytest.raises(ValueError, match="đúng 2 chữ số"):
        rt.plan_folder_rename(tree, Path(MDD) / PHONG / "01", "3")


def test_level_of():
    assert rt.level_of(Path("M")) is rt.Level.MA_DINH_DANH
    assert rt.level_of(Path("M/P")) is rt.Level.PHONG
    assert rt.level_of(Path("M/P/01")) is rt.Level.MUC_LUC
    assert rt.level_of(Path("M/P/01/H")) is rt.Level.HO_SO
    assert rt.level_of(Path("M/P/01/H/x.pdf")) is None


# ---------------------------------------------------------------- level 4

def test_rename_ho_so_cascades_pdfs(tree):
    hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    plan = rt.plan_folder_rename(tree, hs, "0005")

    assert plan.new_name == f"{MDD}-{PHONG}-01-0005"
    assert plan.affected_dirs == 1          # chính thư mục hồ sơ
    assert plan.affected_pdfs == 2          # 001 + 002
    # notes.txt không phải PDF đúng quy ước → đi theo thư mục nhưng bị báo skip.
    assert plan.skipped == [
        f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001/notes.txt"
    ]

    res = rt.execute_plan(plan)
    assert res.done_ops == 3 and not res.rolled_back and res.error == ""

    new_dir = tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0005"
    names = sorted(p.name for p in new_dir.iterdir())
    assert names == [
        f"{MDD}-{PHONG}-01-0005-001.pdf",
        f"{MDD}-{PHONG}-01-0005-002.pdf",
        "notes.txt",  # file khác đi theo thư mục, không bị đổi tên
    ]
    assert not (tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0001").exists()


def test_rename_ho_so_accepts_full_name_and_rejects_wrong_parents(tree):
    hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    full = f"{MDD}-{PHONG}-01-0009"
    plan = rt.plan_folder_rename(tree, hs, full)
    assert plan.new_name == full

    wrong = f"{MDD}-OTHER.001-01-0009"
    with pytest.raises(ValueError, match="trùng với thư mục cha"):
        rt.plan_folder_rename(tree, hs, wrong)


def test_rename_ho_so_only_selected_dossier(tree):
    hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    plan = rt.plan_folder_rename(tree, hs, "0005")
    # Hồ sơ anh em 0002 không nằm trong kế hoạch.
    sibling = (tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0002").as_posix()
    assert all(sibling not in op.src.as_posix() for op in plan.ops)


# ---------------------------------------------------------------- level 3

def test_rename_muc_luc_cascades(tree):
    ml = Path(MDD) / PHONG / "01"
    plan = rt.plan_folder_rename(tree, ml, "03")

    assert plan.new_name == "03"
    assert plan.affected_dirs == 3  # 2 hồ sơ hợp lệ + thư mục mục lục
    assert plan.affected_pdfs == 3
    assert set(plan.skipped) == {
        f"{MDD}/{PHONG}/01/SaiTen",
        f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001/notes.txt",
    }

    rt.execute_plan(plan)

    ml_dir = tree / MDD / PHONG / "03"
    dirs = sorted(p.name for p in ml_dir.iterdir())
    assert dirs == [f"{MDD}-{PHONG}-03-0001", f"{MDD}-{PHONG}-03-0002", "SaiTen"]
    pdfs = sorted(
        p.name for p in (ml_dir / f"{MDD}-{PHONG}-03-0001").iterdir()
        if p.suffix == ".pdf"
    )
    assert pdfs == [f"{MDD}-{PHONG}-03-0001-001.pdf",
                    f"{MDD}-{PHONG}-03-0001-002.pdf"]
    # Hồ sơ sai quy ước đi theo nhưng giữ nguyên tên.
    assert (ml_dir / "SaiTen" / f"{MDD}-{PHONG}-01-0001-001.pdf").exists()
    # Mục lục 02 bên cạnh không bị động tới.
    assert (tree / MDD / PHONG / "02" / f"{MDD}-{PHONG}-02-0001").is_dir()


def test_rename_muc_luc_collision(tree):
    ml = Path(MDD) / PHONG / "01"
    with pytest.raises(ValueError, match="trùng"):
        rt.plan_folder_rename(tree, ml, "02")  # đã có mục lục 02


# ---------------------------------------------------------------- level 2

def test_rename_phong_cascades(tree):
    plan = rt.plan_folder_rename(tree, Path(MDD) / PHONG, "B.009")

    assert plan.new_name == "B.009"
    assert plan.affected_dirs == 4  # 3 hồ sơ (SaiTen skip) + phông
    assert plan.affected_pdfs == 4

    rt.execute_plan(plan)

    phong_dir = tree / MDD / "B.009"
    hs = phong_dir / "01" / f"{MDD}-B.009-01-0001"
    assert (hs / f"{MDD}-B.009-01-0001-001.pdf").is_file()
    assert (phong_dir / "02" / f"{MDD}-B.009-02-0001").is_dir()
    assert (phong_dir / "01" / f"{MDD}-B.009-01-0002").is_dir()


# ---------------------------------------------------------------- level 1

def test_rename_ma_dinh_danh_cascades_everything(tree):
    """Đổi mã định danh: tên PHÔNG và MỤC LỤC giữ nguyên (mã độc lập), chỉ
    đoạn 1 trong tên hồ sơ / PDF bên dưới được thay."""
    plan = rt.plan_folder_rename(tree, Path(MDD), "B12.345.67")

    assert plan.new_name == "B12.345.67"
    # 4 hồ sơ hợp lệ + mã định danh (KHÔNG có op đổi tên phông); SaiTen skip.
    assert plan.affected_dirs == 5
    assert plan.affected_pdfs == 5
    assert set(plan.skipped) == {
        f"{MDD}/{PHONG}/01/SaiTen",
        f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001/notes.txt",
    }

    rt.execute_plan(plan)

    root_new = tree / "B12.345.67"
    assert (root_new / PHONG).is_dir()    # phông GIỮ NGUYÊN tên
    assert (root_new / PHONG2).is_dir()
    hs1 = root_new / PHONG / "01" / f"B12.345.67-{PHONG}-01-0001"
    assert sorted(p.name for p in hs1.glob("*.pdf")) == [
        f"B12.345.67-{PHONG}-01-0001-001.pdf",
        f"B12.345.67-{PHONG}-01-0001-002.pdf",
    ]
    assert (root_new / PHONG2 / "01"
            / f"B12.345.67-{PHONG2}-01-0003").is_dir()
    assert not (tree / MDD).exists()


def test_rename_mdd_keeps_unrelated_phong_name(tree):
    free = tree / "FREE.001" / "PhongTuDo" / "01"
    hs = free / f"FREE.001-PhongTuDo-01-0007"
    hs.mkdir(parents=True)
    (hs / "FREE.001-PhongTuDo-01-0007-001.pdf").write_bytes(b"x")

    plan = rt.plan_folder_rename(tree, Path("FREE.001"), "NEW.002")
    rt.execute_plan(plan)

    new_hs = tree / "NEW.002" / "PhongTuDo" / "01" / "NEW.002-PhongTuDo-01-0007"
    assert (new_hs / "NEW.002-PhongTuDo-01-0007-001.pdf").is_file()


# --------------------------------------------- hồ sơ / PDF lệch mã cha

def test_rename_ml_rebuilds_stale_children(tree):
    """Đổi mục lục: hồ sơ / PDF đang LỆCH mã cha (tên cũ không khớp chuỗi
    cha) vẫn được dựng lại theo bộ mã mới — không bị bỏ qua."""
    ml = tree / MDD / PHONG / "88"
    stale = ml / "OLD.1-OLD.2-02-0088"          # lệch cả 3 đoạn đầu
    stale.mkdir(parents=True)
    (stale / "OLD.1-OLD.2-02-0088-001.pdf").write_bytes(b"x")
    (stale / "notes.txt").write_bytes(b"x")

    plan = rt.plan_folder_rename(tree, Path(MDD) / PHONG / "88", "66")
    assert plan.affected_dirs == 2   # hồ sơ lệch mã + thư mục mục lục
    assert plan.affected_pdfs == 1
    rt.execute_plan(plan)

    new_hs = tree / MDD / PHONG / "66" / f"{MDD}-{PHONG}-66-0088"
    assert (new_hs / f"{MDD}-{PHONG}-66-0088-001.pdf").is_file()
    assert (new_hs / "notes.txt").is_file()   # file khác đi theo, giữ tên
    assert not (tree / MDD / PHONG / "88").exists()


def test_rename_mdd_rebuilds_stale_dossier_segments(tree):
    """Đổi mã định danh: hồ sơ / PDF lệch mã cũ được dựng lại theo mã định
    danh mới + tên phông/mục lục thực tế; tên phông, mục lục giữ nguyên."""
    stale = tree / MDD / PHONG / "01" / "OLD.999-OLD.003-07-0009"
    stale.mkdir()
    (stale / "OLD.999-OLD.003-07-0009-004.pdf").write_bytes(b"x")

    plan = rt.plan_folder_rename(tree, Path(MDD), "NEW.1")
    rt.execute_plan(plan)

    new_hs = tree / "NEW.1" / PHONG / "01" / f"NEW.1-{PHONG}-01-0009"
    assert (new_hs / f"NEW.1-{PHONG}-01-0009-004.pdf").is_file()
    # Phông giữ nguyên tên.
    assert (tree / "NEW.1" / PHONG).is_dir()


def test_rename_ho_so_rebuilds_stale_name_and_pdfs(tree):
    """Đổi hồ sơ (nhập số mới): tên hồ sơ ghép từ mã chuỗi cha THỰC TẾ (sửa
    luôn hồ sơ lệch mã), PDF bên dưới dựng lại theo tên hồ sơ mới."""
    stale = tree / MDD / PHONG / "01" / "OLD.1-OLD.2-88-0042"
    stale.mkdir()
    (stale / "OLD.1-OLD.2-88-0042-009.pdf").write_bytes(b"x")

    plan = rt.plan_folder_rename(
        tree, Path(MDD) / PHONG / "01" / "OLD.1-OLD.2-88-0042", "0050")
    assert plan.new_name == f"{MDD}-{PHONG}-01-0050"
    rt.execute_plan(plan)

    new_hs = tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0050"
    assert (new_hs / f"{MDD}-{PHONG}-01-0050-009.pdf").is_file()
    assert not stale.exists()


# ---------------------------------------------------------------- misc

def test_invalid_level_and_names(tree):
    # Đổi tên một hồ sơ tên LẠC quy ước ("SaiTen") giờ được phép: nhập số hồ
    # sơ mới → tên được dựng lại từ mã chuỗi cha thực tế.
    plan = rt.plan_folder_rename(
        tree, Path(MDD) / PHONG / "01" / "SaiTen", "0009")
    assert plan.new_name == f"{MDD}-{PHONG}-01-0009"
    with pytest.raises(ValueError):
        rt.plan_folder_rename(tree, Path(MDD) / PHONG, "co-dau-gach")
    with pytest.raises(ValueError):
        rt.plan_folder_rename(tree, Path(MDD) / PHONG / "01" / "x" / "y", "01")


def test_case_only_rename_uses_staging(tmp_path):
    """Đổi mã định danh chỉ khác hoa/thường → mọi op qua tên tạm."""
    root = tmp_path / "CSDL_SOHOA"
    d = root / "X.Y" / "Z.W" / "01" / "X.Y-Z.W-01-ab01"
    d.mkdir(parents=True)
    (d / "X.Y-Z.W-01-ab01-001.pdf").write_bytes(b"x")

    plan = rt.plan_folder_rename(root, Path("X.Y"), "x.y")
    assert plan.new_name == "x.y"
    res = rt.execute_plan(plan)
    assert res.done_ops == len(plan.ops) and not res.rolled_back
    new_hs = root / "x.y" / "Z.W" / "01" / "x.y-Z.W-01-ab01"
    assert (new_hs / "x.y-Z.W-01-ab01-001.pdf").is_file()
    # exists() trên Windows không phân biệt hoa/thường → so tên chính xác.
    assert [p.name for p in root.iterdir()] == ["x.y"]


def test_execute_rolls_back_on_failure(tree, monkeypatch):
    ml = Path(MDD) / PHONG / "01"
    plan = rt.plan_folder_rename(tree, ml, "03")
    assert len(plan.ops) >= 5

    real_move = rt._move_path_with_retry
    calls = {"n": 0}

    def flaky(src, dst, **kw):
        calls["n"] += 1
        if calls["n"] == 4:  # hỏng giữa chừng
            raise PermissionError("simulated lock")
        real_move(src, dst, **kw)

    monkeypatch.setattr(rt, "_move_path_with_retry", flaky)
    res = rt.execute_plan(plan)
    monkeypatch.undo()

    assert res.rolled_back and "Đã lùi về như cũ" in res.error
    # Cây nguyên vẹn như trước khi đổi.
    assert (tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0001").is_dir()
    assert (tree / MDD / PHONG / "01" / "SaiTen").is_dir()
    assert not (tree / MDD / PHONG / "03").exists()


def test_path_map_and_map_path(tree):
    plan = rt.plan_folder_rename(tree, Path(MDD), "B12.345.67")
    old_pdf = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001/{MDD}-{PHONG}-01-0001-001.pdf"
    assert plan.path_map[old_pdf] == (
        f"B12.345.67/{PHONG}/01/B12.345.67-{PHONG}-01-0001/"
        f"B12.345.67-{PHONG}-01-0001-001.pdf"
    )
    # File không được đổi tên nhưng nằm trong cây bị đổi vẫn map được.
    assert rt.map_path(plan, f"{MDD}/{PHONG}/01/0001-old.txt").endswith(
        f"{PHONG}/01/0001-old.txt"
    )
    assert rt.map_path(plan, "Khac/abc.pdf") == "Khac/abc.pdf"


def test_scan_stats(tree):
    stats = rt.scan_stats(tree)
    assert stats == {"mdd": 1, "phong": 2, "ho_so": 5, "pdf": 6}


# ---------------------------------------------------------------- UI screen

def test_rename_tree_screen_flow(tree, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication

    from scanindex.infra import translations
    from scanindex.ui.screens import rename_tree_screen as rts

    app = QApplication.instance() or QApplication([])
    del app
    monkeypatch.setattr(rts, "_SETTINGS_FILE",
                        str(tmp_path / "rename_tree_settings.json"))

    # Test assert nhãn cấp tiếng Việt → pin ngôn ngữ vi trong suốt test.
    translations.set_lang("vi")
    try:
        screen = rts.RenameTreeScreen()
        screen._set_root(tree)

        # Cấp 1 được nạp ngay, cấp sâu chỉ nạp khi cần.
        assert screen.tree.topLevelItemCount() == 1
        top = screen.tree.topLevelItem(0)
        assert top.text(0).endswith(MDD) and top.text(1) == "Mã định danh"

        hs_rel = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001"
        hs = screen._item_for_rel(hs_rel)
        assert hs is not None and hs.text(1) == "Hồ sơ"
        screen.tree.expandItem(top)
        screen.tree.expandItem(screen._item_for_rel(f"{MDD}/{PHONG}"))
        screen.tree.expandItem(screen._item_for_rel(f"{MDD}/{PHONG}/01"))
        screen.tree.expandItem(hs)  # bung hồ sơ để nạp danh sách PDF

        child_names = [hs.child(i).text(0) for i in range(hs.childCount())]
        assert any(f"{MDD}-{PHONG}-01-0001-001.pdf" in n for n in child_names)
        # Thư mục sai quy ước hiện ⚠.
        bad = screen._item_for_rel(f"{MDD}/{PHONG}/01/SaiTen")
        assert bad is not None and "⚠" in bad.text(0)

        # Click PDF → viewer page 1, nhớ file đang xem.
        screen._on_current_changed(hs.child(0), None)
        assert screen.preview_stack.currentIndex() == 1
        assert screen._current_pdf_rel == f"{hs_rel}/{MDD}-{PHONG}-01-0001-001.pdf"

        # Đổi tên mã định danh qua đúng luồng screen (như worker xong việc).
        screen.pdf_viewer.release_file_handles()
        plan = rt.plan_folder_rename(tree, Path(MDD), "B12.345.67")
        old_expanded = screen._collect_expanded()
        assert hs_rel in old_expanded
        res = rt.execute_plan(plan)
        assert not res.rolled_back
        screen._on_rename_done(
            plan, rt.ExecuteResult(done_ops=len(plan.ops)),
            old_expanded, hs_rel, screen._current_pdf_rel,
        )

        # Cây mới giữ trạng thái bung, hồ sơ và PDF đã đổi tên (phông giữ
        # nguyên), viewer theo path mới.
        assert screen.tree.topLevelItem(0).text(0).endswith("B12.345.67")
        assert screen._item_for_rel(
            f"B12.345.67/{PHONG}/01/B12.345.67-{PHONG}-01-0001"
        ) is not None
        assert screen._current_pdf_rel == (
            f"B12.345.67/{PHONG}/01/B12.345.67-{PHONG}-01-0001/"
            f"B12.345.67-{PHONG}-01-0001-001.pdf"
        )
        assert screen._item_for_rel(hs_rel) is None  # path cũ không còn

        screen.deleteLater()
    finally:
        translations.set_lang("en")


def test_screen_open_expands_whole_tree(tree, monkeypatch, tmp_path):
    """Mở chức năng (show màn hình): toàn bộ cây CSDL được nạp + bung full."""
    from PySide6.QtWidgets import QApplication

    from scanindex.ui.screens import rename_tree_screen as rts

    screen = _make_screen(tree, monkeypatch, tmp_path)
    top = screen.tree.topLevelItem(0)
    assert not top.isExpanded()          # chưa mở màn: cây chưa bung

    screen.show()
    QApplication.processEvents()         # xả singleShot nạp + bung cây
    assert top.isExpanded()

    def walk(item):
        yield item
        for i in range(item.childCount()):
            yield from walk(item.child(i))

    items = [
        it for k in range(screen.tree.topLevelItemCount())
        for it in walk(screen.tree.topLevelItem(k))
    ]
    dirs = [it for it in items if it.data(0, rts._ROLE_ISDIR)]
    assert dirs and all(it.data(0, rts._ROLE_LOADED) for it in dirs)
    assert all(it.isExpanded() for it in dirs)
    # Cấp sâu nhất đã nạp đủ: hồ sơ có đủ 2 PDF + notes.txt.
    hs_item = screen._item_for_rel(f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001")
    assert hs_item.childCount() == 3

    screen.hide()
    screen.deleteLater()


# ---------------------------------------------------------------- reorder PDF

def _pdf_names(tree, phong=PHONG, ml="01", hs="0001"):
    d = tree / MDD / phong / ml / f"{MDD}-{phong}-{ml}-{hs}"
    return d, sorted(p.name for p in d.glob("*.pdf"))


def test_pdf_reorder_renames_by_position(tree):
    hs_rel = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    d, names = _pdf_names(tree)
    assert names == [f"{MDD}-{PHONG}-01-0001-001.pdf",
                     f"{MDD}-{PHONG}-01-0001-002.pdf"]
    old_contents = {n: (d / n).read_bytes() for n in names}

    # Kéo file 002 lên đầu: 002 → 001, 001 → 002 (hoán đổi qua staging).
    plan = rt.plan_pdf_reorder(tree, hs_rel, list(reversed(names)))
    assert plan.staging_dir is not None
    assert len(plan.ops) == 4  # 2 phase × 2 file
    assert plan.path_map == {
        f"{hs_rel.as_posix()}/{names[0]}": f"{hs_rel.as_posix()}/{names[1]}",
        f"{hs_rel.as_posix()}/{names[1]}": f"{hs_rel.as_posix()}/{names[0]}",
    }
    res = rt.execute_plan(plan)
    assert res.done_ops == 4 and not res.error

    # Tên như cũ nhưng nội dung đã hoán đổi; không còn thư mục staging.
    assert sorted(p.name for p in d.glob("*.pdf")) == names
    for n in names:
        swapped = names[1] if n == names[0] else names[0]
        assert (d / n).read_bytes() == old_contents[swapped]
    assert not list(d.glob(".reorder-*"))


def test_pdf_reorder_moves_middle_file(tree):
    # Thêm file 003, đưa 003 lên đầu: 003→001, 001→002, 002→003.
    hs_rel = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    d = tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    n3 = f"{MDD}-{PHONG}-01-0001-003.pdf"
    (d / n3).write_bytes(n3.encode())
    order = [n3,
             f"{MDD}-{PHONG}-01-0001-001.pdf",
             f"{MDD}-{PHONG}-01-0001-002.pdf"]
    plan = rt.plan_pdf_reorder(tree, hs_rel, order)
    rt.execute_plan(plan)

    prefix = f"{MDD}-{PHONG}-01-0001"
    assert (d / f"{prefix}-001.pdf").read_bytes() == n3.encode()
    assert (d / f"{prefix}-002.pdf").read_bytes() == f"{prefix}-001.pdf".encode()
    assert (d / f"{prefix}-003.pdf").read_bytes() == f"{prefix}-002.pdf".encode()


def test_pdf_reorder_noop_when_order_unchanged(tree):
    hs_rel = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    _, names = _pdf_names(tree)
    plan = rt.plan_pdf_reorder(tree, hs_rel, names)
    assert plan.ops == [] and plan.staging_dir is None


def test_pdf_reorder_validation(tree):
    hs_rel = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    _, names = _pdf_names(tree)
    with pytest.raises(ValueError, match="liệt kê đủ"):
        rt.plan_pdf_reorder(tree, hs_rel, names[:1])  # thiếu file
    with pytest.raises(ValueError, match="trùng"):
        rt.plan_pdf_reorder(tree, hs_rel, [names[0], names[0]])
    with pytest.raises(ValueError, match="không thuộc"):
        rt.plan_pdf_reorder(tree, hs_rel, [names[0], names[1], "khac.pdf"])
    with pytest.raises(ValueError, match="hồ sơ"):
        rt.plan_pdf_reorder(tree, Path(MDD) / PHONG, names)  # sai cấp


def test_pdf_reorder_rolls_back_and_cleans_staging(tree, monkeypatch):
    hs_rel = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    d, names = _pdf_names(tree)
    before = {n: (d / n).read_bytes() for n in names}
    plan = rt.plan_pdf_reorder(tree, hs_rel, list(reversed(names)))

    real_move = rt._move_path_with_retry
    calls = {"n": 0}

    def flaky(src, dst, **kw):
        calls["n"] += 1
        if calls["n"] == 3:  # hỏng giữa phase đặt tên cuối
            raise PermissionError("simulated lock")
        real_move(src, dst, **kw)

    monkeypatch.setattr(rt, "_move_path_with_retry", flaky)
    res = rt.execute_plan(plan)
    monkeypatch.undo()

    assert res.rolled_back and "Đã lùi về như cũ" in res.error
    assert sorted(p.name for p in d.glob("*.pdf")) == names
    for n in names:
        assert (d / n).read_bytes() == before[n]
    assert not list(d.glob(".reorder-*"))


def test_screen_pdf_same_ho_so_drop_only_reorders_visually(tree, monkeypatch,
                                                          tmp_path):
    """Thả PDF vào vị trí trong CÙNG hồ sơ: chỉ đổi thứ tự hiển thị."""
    from PySide6.QtWidgets import QApplication

    screen = _make_screen(tree, monkeypatch, tmp_path)
    hs_rel = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001"
    n1 = f"{MDD}-{PHONG}-01-0001-001.pdf"
    n2 = f"{MDD}-{PHONG}-01-0001-002.pdf"
    before = sorted(p.name for p in (tree / Path(hs_rel)).glob("*.pdf"))

    screen._handle_pdf_move([f"{hs_rel}/{n2}"], hs_rel, 0)  # kéo 002 lên đầu
    QApplication.processEvents()

    assert screen._child_order(hs_rel, dirs=False) == [n2, n1]
    assert screen._manual_order[hs_rel] == [n2, n1]
    assert sorted(p.name for p in (tree / Path(hs_rel)).glob("*.pdf")) == before
    screen.deleteLater()


def test_tree_mouse_drag_reorders_within_dossier(tree, monkeypatch, tmp_path):
    from PySide6.QtCore import Qt as QtConst
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from scanindex.ui.screens import rename_tree_screen as rts

    app = QApplication.instance() or QApplication([])
    del app
    monkeypatch.setattr(rts, "_SETTINGS_FILE",
                        str(tmp_path / "rename_tree_settings.json"))

    screen = rts.RenameTreeScreen()
    screen._set_root(tree)
    hs_rel = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001"
    # Bung đủ chuỗi cha để item nằm trong viewport (visualItemRect mới hợp lệ).
    for part in range(1, 5):
        rel = "/".join(hs_rel.split("/")[:part])
        screen.tree.expandItem(screen._item_for_rel(rel))
    hs_item = screen._item_for_rel(hs_rel)
    assert hs_item.childCount() == 3  # 001.pdf, 002.pdf, notes.txt

    received: list[tuple[list, str, int]] = []
    monkeypatch.setattr(
        screen, "_handle_pdf_move",
        lambda rels, t, i=-1, **kw: received.append((list(rels), t, i)),
    )

    item1 = hs_item.child(0)  # 001.pdf
    item2 = hs_item.child(1)  # 002.pdf
    # Cần layout thật để visualItemRect hợp lệ (offscreen không tự layout).
    screen.tree.resize(600, 400)
    screen.tree.show()
    screen.resize(1000, 600)
    QApplication.processEvents()

    vp = screen.tree.viewport()
    p1 = screen.tree.visualItemRect(item1).center()
    p2 = screen.tree.visualItemRect(item2).center()

    # Kéo 002 (press) → rê lên 001 (move vượt ngưỡng) → thả (release).
    QTest.mousePress(vp, QtConst.MouseButton.LeftButton,
                     QtConst.KeyboardModifier.NoModifier, p2)
    QTest.mouseMove(vp, p1)
    assert screen.tree._dragging  # đã vào trạng thái kéo
    QTest.mouseRelease(vp, QtConst.MouseButton.LeftButton,
                       QtConst.KeyboardModifier.NoModifier, p1)
    QApplication.processEvents()

    # 002 đặt lên trước 001: yêu cầu chuyển tới vị trí 0, không đổi tên.
    assert received == [(
        [f"{hs_rel}/{MDD}-{PHONG}-01-0001-002.pdf"], hs_rel, 0,
    )]
    assert not screen.tree._dragging

    # Kéo một thư mục hồ sơ (không phải PDF) không phát sinh gì.
    received.clear()
    hs2 = screen._item_for_rel(f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0002")
    screen.tree.expandItem(hs2)
    QApplication.processEvents()
    p_hs2 = screen.tree.visualItemRect(hs2).center()
    p_above = screen.tree.visualItemRect(
        screen._item_for_rel(f"{MDD}/{PHONG}/01")).center()
    QTest.mousePress(vp, QtConst.MouseButton.LeftButton,
                     QtConst.KeyboardModifier.NoModifier, p_hs2)
    QTest.mouseMove(vp, p_above)
    QTest.mouseRelease(vp, QtConst.MouseButton.LeftButton,
                       QtConst.KeyboardModifier.NoModifier, p_above)
    QApplication.processEvents()
    assert received == []
    screen.deleteLater()


# ---------------------------------------------------------------- move folder

def test_move_muc_luc_to_other_phong_cascades(tree):
    # PHONG2 chỉ có mục lục "01" → chuyển mục lục "02" của PHONG sang đó.
    plan = rt.plan_folder_move(tree, Path(MDD) / PHONG / "02",
                               Path(MDD) / PHONG2)
    assert plan.kind == "move"
    assert plan.new_name == "02"
    assert plan.affected_dirs == 2   # hồ sơ + chính mục lục
    assert plan.affected_pdfs == 1

    res = rt.execute_plan(plan)
    assert res.done_ops == len(plan.ops) and not res.error

    new_hs = tree / MDD / PHONG2 / "02" / f"{MDD}-{PHONG2}-02-0001"
    assert (new_hs / f"{MDD}-{PHONG2}-02-0001-001.pdf").is_file()
    assert not (tree / MDD / PHONG / "02").exists()
    # path_map dẫn cả hồ sơ về vị trí mới.
    assert plan.path_map[f"{MDD}/{PHONG}/02/{MDD}-{PHONG}-02-0001"] == \
        f"{MDD}/{PHONG2}/02/{MDD}-{PHONG2}-02-0001"


def test_move_muc_luc_collision_raises(tree):
    # PHONG2 đã có mục lục "01" → trùng tên tại đích.
    with pytest.raises(ValueError, match="Đã có sẵn"):
        rt.plan_folder_move(tree, Path(MDD) / PHONG / "01",
                            Path(MDD) / PHONG2)


def test_move_muc_luc_rebuilds_all_children(tree):
    """Mọi hồ sơ / PDF trong mục lục được dựng lại đúng mã cha đích — kể cả
    hồ sơ lệch mã và PDF lệch tên (được "sửa" luôn khi chuyển)."""
    phong3 = "A29.244.01.007"
    (tree / MDD / phong3).mkdir()
    ml02 = tree / MDD / PHONG / "02"
    # Hồ sơ parse được nhưng lệch mã cha + PDF lệch tên trong hồ sơ chuẩn.
    lech = ml02 / "Q-W-02-0009"
    lech.mkdir()
    (lech / "Q-W-02-0009-001.pdf").write_bytes(b"lech")
    (ml02 / f"{MDD}-{PHONG}-02-0001" / "tailieu.pdf").write_bytes(b"sai")

    plan = rt.plan_folder_move(tree, Path(MDD) / PHONG / "02",
                               Path(MDD) / phong3)
    rt.execute_plan(plan)

    base = tree / MDD / phong3 / "02"
    hs1 = base / f"{MDD}-{phong3}-02-0001"
    # Hồ sơ chuẩn: đổi mã; PDF chuẩn giữ NNN 001, PDF lệch tên nhận 002.
    assert (hs1 / f"{MDD}-{phong3}-02-0001-001.pdf").is_file()
    assert (hs1 / f"{MDD}-{phong3}-02-0001-002.pdf").read_bytes() == b"sai"
    # Hồ sơ lệch mã được dựng lại đúng theo mã cha mới.
    assert (base / f"{MDD}-{phong3}-02-0009"
            / f"{MDD}-{phong3}-02-0009-001.pdf").is_file()
    assert not lech.exists()


def test_move_ho_so_to_other_muc_luc_same_phong(tree):
    # Hồ sơ 0002 (mục lục 01) → mục lục 02 (đang có 0001, không đụng 0002).
    hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0002"
    plan = rt.plan_folder_move(tree, hs, Path(MDD) / PHONG / "02")
    assert plan.new_name == f"{MDD}-{PHONG}-02-0002"

    rt.execute_plan(plan)

    new_dir = tree / MDD / PHONG / "02" / f"{MDD}-{PHONG}-02-0002"
    assert (new_dir / f"{MDD}-{PHONG}-02-0002-001.pdf").is_file()
    assert not (tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0002").exists()
    # Hồ sơ 0001 của mục lục 02 không bị động tới.
    assert (tree / MDD / PHONG / "02" / f"{MDD}-{PHONG}-02-0001").is_dir()


def test_move_ho_so_to_other_phong(tree):
    # Hồ sơ 0002 (phông .002) → mục lục 01 của phông .005 (có 0003, không đụng).
    hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0002"
    tgt = Path(MDD) / PHONG2 / "01"
    plan = rt.plan_folder_move(tree, hs, tgt)
    assert plan.new_name == f"{MDD}-{PHONG2}-01-0002"

    rt.execute_plan(plan)

    new_dir = tree / MDD / PHONG2 / "01" / f"{MDD}-{PHONG2}-01-0002"
    assert (new_dir / f"{MDD}-{PHONG2}-01-0002-001.pdf").is_file()


def test_move_ho_so_with_unparseable_name_auto_numbered(tree):
    """Hồ sơ tên lệch quy ước vẫn chuyển được — tự gán số hồ sơ kế tiếp."""
    sai_ten = Path(MDD) / PHONG / "01" / "SaiTen"
    tgt_ml = Path(MDD) / PHONG / "02"  # đã có hồ sơ 0001
    plan = rt.plan_folder_move(tree, sai_ten, tgt_ml)
    assert plan.new_name == f"{MDD}-{PHONG}-02-0002"  # số kế tiếp còn tự do

    rt.execute_plan(plan)

    new_dir = tree / tgt_ml / plan.new_name
    # PDF bên trong được dựng lại đúng bộ mã hồ sơ mới (giữ NNN cũ).
    assert (new_dir / f"{MDD}-{PHONG}-02-0002-001.pdf").is_file()
    assert not (tree / sai_ten).exists()


def test_move_ho_so_collision_auto_numbers(tree):
    """Mã hồ sơ trùng tại đích → tự chuyển sang số kế tiếp, không chặn."""
    hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    tgt_ml = Path(MDD) / PHONG / "02"  # đã có hồ sơ 0001
    plan = rt.plan_folder_move(tree, hs, tgt_ml)
    assert plan.new_name == f"{MDD}-{PHONG}-02-0002"

    rt.execute_plan(plan)

    new_dir = tree / tgt_ml / plan.new_name
    # PDF giữ NNN cũ (còn tự do tại hồ sơ mới), notes.txt đi theo giữ nguyên.
    assert (new_dir / f"{MDD}-{PHONG}-02-0002-001.pdf").is_file()
    assert (new_dir / f"{MDD}-{PHONG}-02-0002-002.pdf").is_file()
    assert (new_dir / "notes.txt").is_file()
    assert "notes.txt" in [Path(s).name for s in plan.skipped]

    # Hoàn tác (chuyển ngược, ép đúng tên gốc) → mã hồ sơ 0001 quay về.
    back = rt.plan_folder_move(
        tree, tgt_ml / plan.new_name, Path(MDD) / PHONG / "01",
        new_name=f"{MDD}-{PHONG}-01-0001")
    rt.execute_plan(back)
    old_dir = tree / hs
    assert (old_dir / f"{MDD}-{PHONG}-01-0001-001.pdf").is_file()
    assert (old_dir / f"{MDD}-{PHONG}-01-0001-002.pdf").is_file()
    assert not (tree / tgt_ml / f"{MDD}-{PHONG}-02-0002").exists()


def test_move_phong_to_other_mdd_keeps_name(tree):
    """Phông chuyển sang mã định danh khác: tên phông GIỮ NGUYÊN, chỉ hồ sơ /
    PDF con đổi đoạn mã định danh theo cha mới."""
    (tree / "B.999").mkdir()
    plan = rt.plan_folder_move(tree, Path(MDD) / PHONG, Path("B.999"))
    assert plan.new_name == PHONG  # không tự đổi tiền tố

    rt.execute_plan(plan)

    hs = tree / "B.999" / PHONG / "01" / f"B.999-{PHONG}-01-0001"
    assert (hs / f"B.999-{PHONG}-01-0001-001.pdf").is_file()
    assert not (tree / MDD / PHONG).exists()


def test_move_rejections(tree):
    # Đã nằm sẵn trong cha hiện tại.
    with pytest.raises(ValueError, match="đã nằm sẵn"):
        rt.plan_folder_move(tree, Path(MDD) / PHONG / "01", Path(MDD) / PHONG)
    # Chuyển vào chính nó / thư mục con.
    with pytest.raises(ValueError, match="chính nó"):
        rt.plan_folder_move(tree, Path(MDD) / PHONG, Path(MDD) / PHONG / "01")
    # Sai cấp đích (hồ sơ phải vào mục lục, không vào phông).
    with pytest.raises(ValueError, match="chỉ chuyển được vào"):
        rt.plan_folder_move(
            tree, Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0002",
            Path(MDD) / PHONG2)
    # Mã định danh là cấp cao nhất.
    with pytest.raises(ValueError, match="Chỉ di chuyển được"):
        rt.plan_folder_move(tree, Path(MDD), Path("Khac"))


def test_move_roundtrip_restores_original_names(tree):
    """Chuyển mục lục sang phông khác rồi chuyển ngược về → tên như cũ."""
    ml = Path(MDD) / PHONG / "02"
    hs_old = f"{MDD}-{PHONG}-02-0001"
    rt.execute_plan(rt.plan_folder_move(tree, ml, Path(MDD) / PHONG2))

    back = rt.plan_folder_move(tree, Path(MDD) / PHONG2 / "02",
                               Path(MDD) / PHONG)
    rt.execute_plan(back)

    assert (tree / MDD / PHONG / "02" / hs_old).is_dir()
    assert (tree / MDD / PHONG / "02" / hs_old
            / f"{MDD}-{PHONG}-02-0001-001.pdf").is_file()
    assert not (tree / MDD / PHONG2 / "02").exists()


# ----------------------------------------------------------- đánh số lại

def test_renumber_ho_so_from_selected(tree):
    """Đánh số từ hồ sơ 0003 của PHONG2/01 (giữ nguyên) → hồ sơ sau tiếp diễn.

    PHONG2/01 có sẵn 0003; thêm 0005 phía sau: đánh số từ 0003 → 0005 thành 0004.
    """
    start_hs = Path(MDD) / PHONG2 / "01" / f"{MDD}-{PHONG2}-01-0003"
    extra = tree / MDD / PHONG2 / "01" / f"{MDD}-{PHONG2}-01-0005"
    extra.mkdir()
    (extra / f"{MDD}-{PHONG2}-01-0005-001.pdf").write_bytes(b"5")

    plan = rt.plan_renumber_siblings_from(tree, start_hs)
    assert plan.kind == "renumber"
    assert plan.new_name == plan.old_name  # chính nó không đổi

    rt.execute_plan(plan)

    ml = tree / MDD / PHONG2 / "01"
    names = sorted(p.name for p in ml.iterdir() if p.is_dir())
    assert names == [f"{MDD}-{PHONG2}-01-0003",
                     f"{MDD}-{PHONG2}-01-0004"]  # cũ 0005
    assert (ml / f"{MDD}-{PHONG2}-01-0004"
            / f"{MDD}-{PHONG2}-01-0004-001.pdf").is_file()


def test_renumber_muc_luc_cascades_children(tree):
    """Đánh số từ mục lục 01 (giữ nguyên) trong PHONG2: mục sau đổi, con theo.

    PHONG2 có [01, 05?] — dựng sẵn mục lục 05 có hồ sơ để thấy cascade.
    """
    ml05 = tree / MDD / PHONG2 / "05"
    ml05.mkdir()
    hs = ml05 / f"{MDD}-{PHONG2}-05-0001"
    hs.mkdir()
    (hs / f"{MDD}-{PHONG2}-05-0001-001.pdf").write_bytes(b"x")

    plan = rt.plan_renumber_siblings_from(
        tree, Path(MDD) / PHONG2 / "01")
    rt.execute_plan(plan)

    # Mục lục 05 → 02; hồ sơ + PDF bên dưới đổi đoạn mục lục.
    new_hs = tree / MDD / PHONG2 / "02" / f"{MDD}-{PHONG2}-02-0001"
    assert (new_hs / f"{MDD}-{PHONG2}-02-0001-001.pdf").is_file()
    assert not ml05.exists()


def test_renumber_validations_and_noop(tree):
    # Sai cấp.
    with pytest.raises(ValueError, match="Hồ sơ, một Mục lục"):
        rt.plan_renumber_siblings_from(tree, Path(MDD))
    # Mục được chọn chưa có số hợp lệ.
    with pytest.raises(ValueError, match="có sẵn số hợp lệ"):
        rt.plan_renumber_siblings_from(
            tree, Path(MDD) / PHONG / "01" / "SaiTen")
    # Không có gì phía sau → plan rỗng.
    plan = rt.plan_renumber_siblings_from(
        tree, Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0002")
    assert plan.ops == []


def test_renumber_with_explicit_order(tree):
    """``order`` đưa thứ tự tùy ý (visual order) — không sort theo tên."""
    ml01 = tree / MDD / PHONG / "01"
    hs2 = f"{MDD}-{PHONG}-01-0002"
    order = [hs2, f"{MDD}-{PHONG}-01-0001", "SaiTen"]
    plan = rt.plan_renumber_siblings_from(
        tree, Path(MDD) / PHONG / "01" / hs2, order)
    rt.execute_plan(plan)

    # 0002 giữ số (đứng đầu visual); 0001 xếp sau nó → nhận 0003.
    names = sorted(p.name for p in ml01.iterdir() if p.is_dir())
    assert names == [f"{MDD}-{PHONG}-01-0002",
                     f"{MDD}-{PHONG}-01-0003",
                     "SaiTen"]
    assert (ml01 / f"{MDD}-{PHONG}-01-0003"
            / f"{MDD}-{PHONG}-01-0003-001.pdf").is_file()


# ------------------------------------------------------ đổi số thứ tự PDF

def test_pdf_rename_stt(tree):
    hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    n1 = f"{MDD}-{PHONG}-01-0001-001.pdf"
    plan = rt.plan_pdf_rename_stt(tree, hs / n1, "005")
    assert plan.new_name == f"{MDD}-{PHONG}-01-0001-005.pdf"

    rt.execute_plan(plan)
    assert (tree / hs / f"{MDD}-{PHONG}-01-0001-005.pdf").is_file()
    assert not (tree / hs / n1).exists()


def test_pdf_rename_stt_validations(tree):
    hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    n1 = f"{MDD}-{PHONG}-01-0001-001.pdf"
    with pytest.raises(ValueError, match="đúng 3 chữ số"):
        rt.plan_pdf_rename_stt(tree, hs / n1, "1")
    with pytest.raises(ValueError, match="đúng 3 chữ số"):
        rt.plan_pdf_rename_stt(tree, hs / n1, "0001")
    with pytest.raises(ValueError, match="đúng 3 chữ số"):
        rt.plan_pdf_rename_stt(tree, hs / n1, "a01")
    with pytest.raises(ValueError, match="Không phải tài liệu PDF"):
        rt.plan_pdf_rename_stt(tree, hs / "notes.txt", "005")
    # Trùng số sẵn có → chặn.
    with pytest.raises(ValueError, match="Đã có sẵn"):
        rt.plan_pdf_rename_stt(tree, hs / n1, "002")
    # Không đổi gì → plan rỗng.
    plan = rt.plan_pdf_rename_stt(tree, hs / n1, "001")
    assert plan.ops == []


# ---------------------------------------------------------------- move pdf

def test_pdf_move_keeps_own_number_when_free(tree):
    """Chuyển PDF sang hồ sơ khác: GIỮ số trang cũ (002 còn tự do → giữ 002)."""
    src_hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    tgt = Path(MDD) / PHONG / "02" / f"{MDD}-{PHONG}-02-0009"
    (tree / tgt).mkdir(parents=True)
    content = (tree / src_hs / f"{MDD}-{PHONG}-01-0001-002.pdf").read_bytes()

    plan = rt.plan_pdf_move(
        tree, [src_hs / f"{MDD}-{PHONG}-01-0001-002.pdf"], tgt)
    rt.execute_plan(plan)
    # Giữ nguyên NNN 002 (còn tự do tại đích).
    assert (tree / tgt / f"{MDD}-{PHONG}-02-0009-002.pdf").read_bytes() == content


def test_pdf_move_collision_takes_next_free(tree):
    """NNN 001 đã có ở đích → nhận số kế tiếp; file đích KHÔNG bị đổi tên."""
    src_hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    tgt = Path(MDD) / PHONG2 / "01" / f"{MDD}-{PHONG2}-01-0003"  # có 001
    plan = rt.plan_pdf_move(
        tree, [src_hs / f"{MDD}-{PHONG}-01-0001-001.pdf"], tgt)
    assert plan.affected_pdfs == 1
    rt.execute_plan(plan)
    d = tree / tgt
    assert (d / f"{MDD}-{PHONG2}-01-0003-001.pdf").is_file()   # nguyên gốc
    assert (d / f"{MDD}-{PHONG2}-01-0003-002.pdf").is_file()   # file chuyển


def test_pdf_move_group_each_keeps_or_takes_free(tree):
    """Nhóm [001, 002] sang hồ sơ có sẵn 001: 001→002, 002→003, giữ thứ tự."""
    src_hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    tgt = Path(MDD) / PHONG2 / "01" / f"{MDD}-{PHONG2}-01-0003"  # có 001
    c1 = (tree / src_hs / f"{MDD}-{PHONG}-01-0001-001.pdf").read_bytes()
    c2 = (tree / src_hs / f"{MDD}-{PHONG}-01-0001-002.pdf").read_bytes()

    plan = rt.plan_pdf_move(tree, [
        src_hs / f"{MDD}-{PHONG}-01-0001-001.pdf",
        src_hs / f"{MDD}-{PHONG}-01-0001-002.pdf",
    ], tgt)
    rt.execute_plan(plan)

    d = tree / tgt
    assert (d / f"{MDD}-{PHONG2}-01-0003-002.pdf").read_bytes() == c1
    assert (d / f"{MDD}-{PHONG2}-01-0003-003.pdf").read_bytes() == c2
    assert len(list(d.glob("*.pdf"))) == 3


def test_pdf_move_misnamed_gets_number_and_same_ho_so_noop(tree):
    hs_rel = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    tgt = Path(MDD) / PHONG2 / "01" / f"{MDD}-{PHONG2}-01-0003"
    # Lệch tên → đánh số tự do đầu tiên.
    (tree / hs_rel / "tailieu.pdf").write_bytes(b"sai")
    plan = rt.plan_pdf_move(tree, [hs_rel / "tailieu.pdf"], tgt)
    rt.execute_plan(plan)
    assert (tree / tgt / f"{MDD}-{PHONG2}-01-0003-002.pdf").read_bytes() == b"sai"
    # File đã đúng chỗ đúng tên → plan rỗng.
    plan2 = rt.plan_pdf_move(
        tree, [tgt / f"{MDD}-{PHONG2}-01-0003-002.pdf"], tgt)
    assert plan2.ops == []


def test_pdf_move_validations(tree):
    hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    tgt = Path(MDD) / PHONG2 / "01" / f"{MDD}-{PHONG2}-01-0003"
    with pytest.raises(ValueError, match="Chỉ chuyển được file PDF"):
        rt.plan_pdf_move(tree, [hs / "notes.txt"], tgt)
    with pytest.raises(ValueError, match="Không có file nào"):
        rt.plan_pdf_move(tree, [], tgt)
    with pytest.raises(ValueError, match="chỉ chuyển được vào một hồ sơ"):
        rt.plan_pdf_move(tree, [hs / f"{MDD}-{PHONG}-01-0001-001.pdf"],
                         Path(MDD) / PHONG / "01")


def test_renumber_pdfs_from_selected(tree):
    """Đánh số từ tài liệu 001 (giữ số) đến hết hồ sơ: 002 giữ, gap lấp."""
    hs = tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    n4 = f"{MDD}-{PHONG}-01-0001-004.pdf"
    (hs / n4).write_bytes(b"4")  # [001, 002, 004] → đánh từ 001: 002 giữ, 004→003

    rel = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001" / \
        f"{MDD}-{PHONG}-01-0001-001.pdf"
    plan = rt.plan_renumber_siblings_from(tree, rel)
    rt.execute_plan(plan)

    names = sorted(p.name for p in hs.glob("*.pdf"))
    assert names == [f"{MDD}-{PHONG}-01-0001-001.pdf",
                     f"{MDD}-{PHONG}-01-0001-002.pdf",
                     f"{MDD}-{PHONG}-01-0001-003.pdf"]


def test_renumber_pdfs_follows_visual_order(tree):
    """order đưa thứ tự kéo-thả: [004, 001, 002] → từ 004: 001→005, 002→006."""
    hs = tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    n4 = f"{MDD}-{PHONG}-01-0001-004.pdf"
    (hs / n4).write_bytes(b"4")
    prefix = f"{MDD}-{PHONG}-01-0001"
    order = [n4, f"{prefix}-001.pdf", f"{prefix}-002.pdf"]

    rel = Path(MDD) / PHONG / "01" / prefix / n4
    plan = rt.plan_renumber_siblings_from(tree, rel, order)
    rt.execute_plan(plan)

    assert (hs / f"{prefix}-004.pdf").is_file()          # giữ số
    assert (hs / f"{prefix}-005.pdf").read_bytes() == \
        f"{prefix}-001.pdf".encode()
    assert (hs / f"{prefix}-006.pdf").read_bytes() == \
        f"{prefix}-002.pdf".encode()


def test_renumber_pdf_requires_own_number(tree):
    hs = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    (tree / hs / "tailieu.pdf").write_bytes(b"sai")
    with pytest.raises(ValueError, match="số trang hợp lệ"):
        rt.plan_renumber_siblings_from(tree, hs / "tailieu.pdf")


def test_rollback_failure_is_reported(tree, monkeypatch):
    """Rollback hỏng một file phải được báo rõ, không tuyên bố thành công."""
    hs_rel = Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    d, names = _pdf_names(tree)
    plan = rt.plan_pdf_reorder(tree, hs_rel, list(reversed(names)))

    real_move = rt._move_path_with_retry
    calls = {"n": 0}

    def flaky(src, dst, **kw):
        calls["n"] += 1
        # Call 3 = op đổi tên cuối (forward) — fail; call 4 = op hoàn tác
        # ĐẦU TIÊN (file 002 trong staging) — cũng fail.
        if calls["n"] in {3, 4}:
            raise PermissionError("simulated lock")
        real_move(src, dst, **kw)

    monkeypatch.setattr(rt, "_move_path_with_retry", flaky)
    res = rt.execute_plan(plan)
    monkeypatch.undo()

    assert res.rolled_back
    assert "không khôi phục được 1 mục" in res.error
    assert names[0] in res.error  # nêu đích danh file kẹt (note cuối message)
    assert "Đã lùi về như cũ một phần" in res.error
    # 002 được trả về chỗ cũ; 001 (op hoàn tác đầu tiên, call#4 fail) còn
    # kẹt trong thư mục staging.
    assert (d / names[1]).is_file()
    assert not (d / names[0]).exists()
    staging = next(d.glob(".reorder-*"))
    assert (staging / names[0]).is_file()


def test_screen_failed_pdf_move_keeps_viewer_document(tree, monkeypatch,
                                                      tmp_path):
    """Thao tác lỗi giữa chừng → rollback, viewer quay về ĐÚNG file đang xem."""
    from PySide6.QtWidgets import QApplication

    from scanindex.ui.screens import rename_tree_screen as rts

    app = QApplication.instance() or QApplication([])
    del app
    monkeypatch.setattr(rts, "_SETTINGS_FILE",
                        str(tmp_path / "rename_tree_settings.json"))

    screen = rts.RenameTreeScreen()
    screen._set_root(tree)
    hs_rel = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001"
    tgt = f"{MDD}/{PHONG2}/01/{MDD}-{PHONG2}-01-0003"
    n1 = f"{MDD}-{PHONG}-01-0001-001.pdf"
    screen._show_pdf(f"{hs_rel}/{n1}")
    assert screen._current_pdf_rel == f"{hs_rel}/{n1}"

    real_move = rt._move_path_with_retry

    def flaky(src, dst, **kw):
        if str(dst).startswith(str(tree / Path(tgt))):
            raise PermissionError("simulated lock")
        real_move(src, dst, **kw)

    monkeypatch.setattr(rt, "_move_path_with_retry", flaky)
    plan = rt.plan_pdf_move(tree, [Path(hs_rel) / n1], Path(tgt))
    screen._execute_plan(plan)
    deadline = time.monotonic() + 10.0
    while screen.is_busy() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    monkeypatch.undo()
    assert not screen.is_busy()

    # File nguyên vẹn, viewer vẫn ở tài liệu đang xem.
    assert screen._current_pdf_rel == f"{hs_rel}/{n1}"
    assert (tree / Path(hs_rel) / n1).read_bytes() == n1.encode()
    assert not (tree / Path(tgt) / n1).exists()
    screen.deleteLater()


def _wait_not_busy(screen) -> None:
    from PySide6.QtWidgets import QApplication

    deadline = time.monotonic() + 10.0
    while screen.is_busy() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert not screen.is_busy()


def _make_screen(tree, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication

    from scanindex.ui.screens import rename_tree_screen as rts

    app = QApplication.instance() or QApplication([])
    del app
    monkeypatch.setattr(rts, "_SETTINGS_FILE",
                        str(tmp_path / "rename_tree_settings.json"))
    screen = rts.RenameTreeScreen()
    screen._set_root(tree)
    return screen


def test_tree_drag_pdf_onto_other_dossier_requests_move(tree, monkeypatch,
                                                        tmp_path):
    from PySide6.QtCore import Qt as QtConst
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    screen = _make_screen(tree, monkeypatch, tmp_path)
    hs_a = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001"
    hs_b = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0002"
    for part in range(1, 5):
        for rel in (hs_a, hs_b):
            screen.tree.expandItem(
                screen._item_for_rel("/".join(rel.split("/")[:part])))

    received: list[tuple[list, str, int]] = []
    monkeypatch.setattr(
        screen, "_handle_pdf_move",
        lambda rels, t, i=-1, **kw: received.append((list(rels), t, i)),
    )

    pdf_item = screen._item_for_rel(f"{hs_a}/{MDD}-{PHONG}-01-0001-001.pdf")
    hs_b_item = screen._item_for_rel(hs_b)
    screen.tree.resize(600, 400)
    screen.tree.show()
    screen.resize(1000, 600)
    QApplication.processEvents()

    vp = screen.tree.viewport()
    QTest.mousePress(vp, QtConst.MouseButton.LeftButton,
                     QtConst.KeyboardModifier.NoModifier,
                     screen.tree.visualItemRect(pdf_item).center())
    QTest.mouseMove(vp, screen.tree.visualItemRect(hs_b_item).center())
    assert screen.tree._dragging
    assert screen.tree._drop_item is hs_b_item  # khung sáng quanh hồ sơ đích
    QTest.mouseRelease(vp, QtConst.MouseButton.LeftButton,
                       QtConst.KeyboardModifier.NoModifier,
                       screen.tree.visualItemRect(hs_b_item).center())
    QApplication.processEvents()

    assert received == [([f"{hs_a}/{MDD}-{PHONG}-01-0001-001.pdf"], hs_b, -1)]
    screen.deleteLater()


def test_tree_drag_ho_so_onto_muc_luc_requests_move(tree, monkeypatch,
                                                    tmp_path):
    from PySide6.QtCore import Qt as QtConst
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    screen = _make_screen(tree, monkeypatch, tmp_path)
    hs_rel = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001"
    for part in range(1, 5):
        screen.tree.expandItem(
            screen._item_for_rel("/".join(hs_rel.split("/")[:part])))
    ml_02 = screen._item_for_rel(f"{MDD}/{PHONG}/02")
    screen.tree.expandItem(ml_02)

    received: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        screen, "_handle_ho_so_drop",
        lambda r, t, i=-1, **kw: received.append((r, t, i)),
    )

    hs_item = screen._item_for_rel(hs_rel)
    screen.tree.resize(600, 400)
    screen.tree.show()
    screen.resize(1000, 600)
    QApplication.processEvents()

    vp = screen.tree.viewport()
    p_hs = screen.tree.visualItemRect(hs_item).center()
    p_ml = screen.tree.visualItemRect(ml_02).center()
    QTest.mousePress(vp, QtConst.MouseButton.LeftButton,
                     QtConst.KeyboardModifier.NoModifier, p_hs)
    QTest.mouseMove(vp, p_ml)
    assert screen.tree._dragging and screen.tree._drop_item is ml_02
    QTest.mouseRelease(vp, QtConst.MouseButton.LeftButton,
                       QtConst.KeyboardModifier.NoModifier, p_ml)
    QApplication.processEvents()

    # Thả lên hàng mục lục → đặt xuống cuối (vị trí -1).
    assert received == [(hs_rel, f"{MDD}/{PHONG}/02", -1)]
    screen.deleteLater()


def test_tree_drag_ho_so_between_rows_places_at_position(tree, monkeypatch,
                                                         tmp_path):
    """Thả hồ sơ lên NỬA TRÊN hàng hồ sơ đầu của mục lục khác → vị trí 0."""
    from PySide6.QtCore import QPoint, Qt as QtConst
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    screen = _make_screen(tree, monkeypatch, tmp_path)
    hs_rel = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001"
    for part in range(1, 5):
        screen.tree.expandItem(
            screen._item_for_rel("/".join(hs_rel.split("/")[:part])))
    ml_02 = screen._item_for_rel(f"{MDD}/{PHONG}/02")
    screen.tree.expandItem(ml_02)

    received: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        screen, "_handle_ho_so_drop",
        lambda r, t, i=-1, **kw: received.append((r, t, i)),
    )

    hs_item = screen._item_for_rel(hs_rel)
    tgt_hs = screen._item_for_rel(f"{MDD}/{PHONG}/02/{MDD}-{PHONG}-02-0001")
    screen.tree.resize(600, 400)
    screen.tree.show()
    screen.resize(1000, 600)
    QApplication.processEvents()

    vp = screen.tree.viewport()
    p_hs = screen.tree.visualItemRect(hs_item).center()
    rect = screen.tree.visualItemRect(tgt_hs)
    p_top = QPoint(rect.center().x(), rect.top() + 2)  # nửa trên hàng
    QTest.mousePress(vp, QtConst.MouseButton.LeftButton,
                     QtConst.KeyboardModifier.NoModifier, p_hs)
    QTest.mouseMove(vp, p_top)
    assert screen.tree._dragging and screen.tree._marker_y is not None
    QTest.mouseRelease(vp, QtConst.MouseButton.LeftButton,
                       QtConst.KeyboardModifier.NoModifier, p_top)
    QApplication.processEvents()

    assert received == [(hs_rel, f"{MDD}/{PHONG}/02", 0)]
    screen.deleteLater()


def test_screen_move_pdf_then_undo_restores(tree, monkeypatch, tmp_path):
    screen = _make_screen(tree, monkeypatch, tmp_path)
    hs_a = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001"
    hs_b = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0002"
    n1 = f"{MDD}-{PHONG}-01-0001-001.pdf"
    content = (tree / hs_a / n1).read_bytes()

    screen._handle_pdf_move([f"{hs_a}/{n1}"], hs_b)
    _wait_not_busy(screen)
    assert screen._undo_stack  # thao tác thành công → có mục hoàn tác
    assert (tree / Path(hs_b) / f"{MDD}-{PHONG}-01-0002-002.pdf").is_file()

    screen._undo_last()
    _wait_not_busy(screen)
    assert (tree / Path(hs_a) / n1).read_bytes() == content  # đúng tên cũ
    assert not (tree / Path(hs_b) / f"{MDD}-{PHONG}-01-0002-002.pdf").exists()
    screen.deleteLater()


def test_screen_move_ho_so_then_undo_restores(tree, monkeypatch, tmp_path):
    screen = _make_screen(tree, monkeypatch, tmp_path)
    hs_rel = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0002"
    tgt_ml = f"{MDD}/{PHONG}/02"

    screen._handle_ho_so_drop(hs_rel, tgt_ml, -1)
    _wait_not_busy(screen)
    assert (tree / MDD / PHONG / "02" / f"{MDD}-{PHONG}-02-0002").is_dir()

    screen._undo_last()
    _wait_not_busy(screen)
    assert (tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0002").is_dir()
    assert (tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0002"
            / f"{MDD}-{PHONG}-01-0002-001.pdf").is_file()
    assert not (tree / MDD / PHONG / "02" / f"{MDD}-{PHONG}-02-0002").exists()
    screen.deleteLater()


# ------------------------------------------------------ thứ tự hồ sơ thủ công

def test_ho_so_drop_same_ml_only_changes_visual_order(tree, monkeypatch,
                                                      tmp_path):
    """Thả hồ sơ vào vị trí trong CÙNG mục lục: chỉ đổi thứ tự hiển thị,
    không đụng đĩa, không đổi tên ai."""
    from PySide6.QtWidgets import QApplication

    screen = _make_screen(tree, monkeypatch, tmp_path)
    ml01 = f"{MDD}/{PHONG}/01"
    hs2 = f"{ml01}/{MDD}-{PHONG}-01-0002"
    for part in range(1, 4):
        screen.tree.expandItem(
            screen._item_for_rel("/".join(ml01.split("/")[:part])))
    ml_item = screen._item_for_rel(ml01)
    screen.tree.expandItem(ml_item)
    before = [p.name for p in (tree / Path(ml01)).iterdir()]
    QApplication.processEvents()

    screen._handle_ho_so_drop(hs2, ml01, 0)  # kéo 0002 lên đầu
    QApplication.processEvents()

    # Cây hiển thị 0002 trước 0001 (không sort theo tên), đĩa nguyên vẹn.
    names = screen._dir_child_order(ml01)
    assert names.index(f"{MDD}-{PHONG}-01-0002") < \
        names.index(f"{MDD}-{PHONG}-01-0001")
    assert screen._manual_order[ml01] == names
    assert [p.name for p in (tree / Path(ml01)).iterdir()] == before
    screen.deleteLater()


def test_ho_so_drop_cross_ml_moves_and_places_at_position(tree, monkeypatch,
                                                          tmp_path):
    """Thả hồ sơ vào vị trí 0 của mục lục khác: chuyển sang + đứng đầu."""
    from PySide6.QtWidgets import QApplication

    screen = _make_screen(tree, monkeypatch, tmp_path)
    ml01, ml02 = f"{MDD}/{PHONG}/01", f"{MDD}/{PHONG}/02"
    hs2 = f"{ml01}/{MDD}-{PHONG}-01-0002"
    for part in range(1, 4):
        for ml in (ml01, ml02):
            screen.tree.expandItem(
                screen._item_for_rel("/".join(ml.split("/")[:part])))
    screen.tree.expandItem(screen._item_for_rel(ml02))
    QApplication.processEvents()

    screen._handle_ho_so_drop(hs2, ml02, 0)
    _wait_not_busy(screen)

    assert (tree / MDD / PHONG / "02" / f"{MDD}-{PHONG}-02-0002").is_dir()
    assert not (tree / Path(ml02) / f"{MDD}-{PHONG}-01-0002").exists()
    names = screen._dir_child_order(ml02)
    assert names[0] == f"{MDD}-{PHONG}-02-0002"  # đứng đầu dù số lớn hơn 0001
    screen.deleteLater()


def test_renumber_follows_manual_visual_order(tree, monkeypatch, tmp_path):
    """Đánh số theo THỨ TỰ HIỂN THỊ (kéo-thả), không theo sort tên."""
    from PySide6.QtWidgets import QApplication

    screen = _make_screen(tree, monkeypatch, tmp_path)
    ml01 = f"{MDD}/{PHONG}/01"
    hs1 = f"{ml01}/{MDD}-{PHONG}-01-0001"
    hs2 = f"{ml01}/{MDD}-{PHONG}-01-0002"
    for part in range(1, 4):
        screen.tree.expandItem(
            screen._item_for_rel("/".join(ml01.split("/")[:part])))
    ml_item = screen._item_for_rel(ml01)
    screen.tree.expandItem(ml_item)
    QApplication.processEvents()

    # Kéo 0002 lên đầu rồi đánh số từ 0002 (giữ số 0002, 0001 → 0003).
    screen._handle_ho_so_drop(hs2, ml01, 0)
    screen._renumber_from(screen._item_for_rel(hs2))
    _wait_not_busy(screen)

    names = sorted(p.name for p in (tree / Path(ml01)).iterdir() if p.is_dir())
    assert names == [f"{MDD}-{PHONG}-01-0002",
                     f"{MDD}-{PHONG}-01-0003",  # cũ 0001, đứng sau 0002
                     "SaiTen"]
    # Tên giờ đã mang đúng thứ tự → thứ tự thủ công được dọn.
    assert ml01 not in screen._manual_order
    screen.deleteLater()


def test_screen_undo_rename_and_reorder(tree, monkeypatch, tmp_path):
    screen = _make_screen(tree, monkeypatch, tmp_path)
    hs_rel = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001"
    n1 = f"{MDD}-{PHONG}-01-0001-001.pdf"
    n2 = f"{MDD}-{PHONG}-01-0001-002.pdf"

    # Chuyển PDF sang hồ sơ khác (giữ số khi tự do) rồi hoàn tác → về đúng chỗ.
    tgt = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0002"  # trống
    screen._handle_pdf_move([f"{hs_rel}/{n2}"], tgt)
    _wait_not_busy(screen)
    d = tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0001"
    assert (tree / Path(tgt) / f"{MDD}-{PHONG}-01-0002-002.pdf").is_file()
    screen._undo_last()
    _wait_not_busy(screen)
    assert (d / n2).read_bytes() == n2.encode()
    assert not (tree / Path(tgt) / f"{MDD}-{PHONG}-01-0002-002.pdf").exists()

    # Đổi tên hồ sơ qua luồng screen rồi hoàn tác → tên cũ trở lại.
    plan = rt.plan_folder_rename(
        tree, Path(MDD) / PHONG / "01" / f"{MDD}-{PHONG}-01-0001", "0007")
    screen._execute_plan(plan)
    _wait_not_busy(screen)
    assert (tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0007").is_dir()
    screen._undo_last()
    _wait_not_busy(screen)
    assert (tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0001").is_dir()
    assert not (tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0007").exists()
    screen.deleteLater()


# ------------------------------------------- UI kéo nhóm PDF + chèn vị trí

def test_tree_multi_drag_pdf_group_requests_move(tree, monkeypatch, tmp_path):
    """Chọn 2 PDF (Ctrl) rồi kéo lên hàng hồ sơ khác → nhóm theo thứ tự hiển thị."""
    from PySide6.QtCore import Qt as QtConst
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    screen = _make_screen(tree, monkeypatch, tmp_path)
    hs_a = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001"
    hs_b = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0002"
    for part in range(1, 5):
        for rel in (hs_a, hs_b):
            screen.tree.expandItem(
                screen._item_for_rel("/".join(rel.split("/")[:part])))
    # Bung hồ sơ A để thấy 2 PDF, chọn cả hai.
    screen.tree.expandItem(screen._item_for_rel(hs_a))
    p1 = screen._item_for_rel(f"{hs_a}/{MDD}-{PHONG}-01-0001-001.pdf")
    p2 = screen._item_for_rel(f"{hs_a}/{MDD}-{PHONG}-01-0001-002.pdf")
    QApplication.processEvents()

    received: list[tuple[list, str, int]] = []
    monkeypatch.setattr(
        screen, "_handle_pdf_move",
        lambda rels, t, i=-1, **kw: received.append((list(rels), t, i)),
    )

    hs_b_item = screen._item_for_rel(hs_b)
    screen.tree.resize(600, 400)
    screen.tree.show()
    screen.resize(1000, 600)
    QApplication.processEvents()

    # Nhấp giữ Ctrl để chọn thêm file thứ hai (file đầu chọn bằng nhấp thường).
    vp = screen.tree.viewport()
    QTest.mouseClick(vp, QtConst.MouseButton.LeftButton,
                     QtConst.KeyboardModifier.NoModifier,
                     screen.tree.visualItemRect(p1).center())
    QTest.mouseClick(vp, QtConst.MouseButton.LeftButton,
                     QtConst.KeyboardModifier.ControlModifier,
                     screen.tree.visualItemRect(p2).center())
    assert {i.text(0) for i in screen.tree.selectedItems()} == {
        f"{MDD}-{PHONG}-01-0001-001.pdf", f"{MDD}-{PHONG}-01-0001-002.pdf"}

    QTest.mousePress(vp, QtConst.MouseButton.LeftButton,
                     QtConst.KeyboardModifier.NoModifier,
                     screen.tree.visualItemRect(p1).center())
    QTest.mouseMove(vp, screen.tree.visualItemRect(hs_b_item).center())
    assert screen.tree._dragging
    assert len(screen.tree._press_group) == 2
    QTest.mouseRelease(vp, QtConst.MouseButton.LeftButton,
                       QtConst.KeyboardModifier.NoModifier,
                       screen.tree.visualItemRect(hs_b_item).center())
    QApplication.processEvents()

    assert received == [(
        [f"{hs_a}/{MDD}-{PHONG}-01-0001-001.pdf",
         f"{hs_a}/{MDD}-{PHONG}-01-0001-002.pdf"],
        hs_b, -1,
    )]
    screen.deleteLater()


def test_screen_renumber_from_context_menu_flow(tree, monkeypatch, tmp_path):
    """Đánh số từ hồ sơ được chọn về sau qua đúng luồng màn hình + hoàn tác."""
    screen = _make_screen(tree, monkeypatch, tmp_path)
    extra = tree / MDD / PHONG / "01" / f"{MDD}-{PHONG}-01-0005"
    extra.mkdir()
    (extra / f"{MDD}-{PHONG}-01-0005-001.pdf").write_bytes(b"5")

    item = screen._item_for_rel(f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0002")
    assert item is not None
    screen._renumber_from(item)
    _wait_not_busy(screen)

    ml = tree / MDD / PHONG / "01"
    names = sorted(p.name for p in ml.iterdir() if p.is_dir())
    assert names == [f"{MDD}-{PHONG}-01-0001",
                     f"{MDD}-{PHONG}-01-0002",
                     f"{MDD}-{PHONG}-01-0003",   # cũ 0005
                     "SaiTen"]
    assert (ml / f"{MDD}-{PHONG}-01-0003"
            / f"{MDD}-{PHONG}-01-0003-001.pdf").is_file()

    screen._undo_last()
    _wait_not_busy(screen)
    assert (ml / f"{MDD}-{PHONG}-01-0005").is_dir()
    assert not (ml / f"{MDD}-{PHONG}-01-0003").exists()
    screen.deleteLater()


def test_screen_arrow_keys_browse_pdfs_across_dossiers(tree, monkeypatch,
                                                       tmp_path):
    """Mũi tên lên/xuống lướt qua các PDF (xuyên qua hồ sơ) → viewer theo."""
    from PySide6.QtCore import Qt as QtConst
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication


    screen = _make_screen(tree, monkeypatch, tmp_path)
    hs_a = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001"
    hs_b = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0002"
    for part in range(1, 5):
        for rel in (hs_a, hs_b):
            screen.tree.expandItem(
                screen._item_for_rel("/".join(rel.split("/")[:part])))
    QApplication.processEvents()

    p1 = screen._item_for_rel(f"{hs_a}/{MDD}-{PHONG}-01-0001-001.pdf")
    screen.tree.setCurrentItem(p1)
    QApplication.processEvents()
    assert screen._current_pdf_rel == f"{hs_a}/{MDD}-{PHONG}-01-0001-001.pdf"

    screen.tree.setFocus()
    QTest.keyClick(screen.tree, QtConst.Key.Key_Down)
    QApplication.processEvents()
    assert screen._current_pdf_rel == f"{hs_a}/{MDD}-{PHONG}-01-0001-002.pdf"
    # Tiếp xuống liên tục: qua notes.txt / hàng hồ sơ (viewer giữ nguyên) cho
    # tới PDF đầu tiên của HỒ SƠ BÊN DƯỚI — lướt xuyên qua ranh giới hồ sơ.
    target_rel = f"{hs_b}/{MDD}-{PHONG}-01-0002-001.pdf"
    for _ in range(5):
        if screen._current_pdf_rel == target_rel:
            break
        QTest.keyClick(screen.tree, QtConst.Key.Key_Down)
        QApplication.processEvents()
    assert screen._current_pdf_rel == target_rel

    # Lên ngược lại (qua các hàng không phải PDF) — viewer rời file hiện tại.
    for _ in range(5):
        if screen._current_pdf_rel != target_rel:
            break
        QTest.keyClick(screen.tree, QtConst.Key.Key_Up)
        QApplication.processEvents()
    assert screen._current_pdf_rel != target_rel
    screen.deleteLater()


def test_screen_renumber_pdf_from_menu_follows_visual_order(tree, monkeypatch,
                                                             tmp_path):
    """Chuột phải PDF: đánh số từ nó theo thứ tự hiển thị đến hết hồ sơ."""
    from PySide6.QtWidgets import QApplication

    screen = _make_screen(tree, monkeypatch, tmp_path)
    hs_rel = f"{MDD}/{PHONG}/01/{MDD}-{PHONG}-01-0001"
    for part in range(1, 5):
        screen.tree.expandItem(
            screen._item_for_rel("/".join(hs_rel.split("/")[:part])))
    QApplication.processEvents()

    # Kéo 002 lên đầu (thứ tự thủ công) rồi đánh số từ 002.
    screen._handle_pdf_move(
        [f"{hs_rel}/{MDD}-{PHONG}-01-0001-002.pdf"], hs_rel, 0)
    item = screen._item_for_rel(f"{hs_rel}/{MDD}-{PHONG}-01-0001-002.pdf")
    screen._renumber_from(item)
    _wait_not_busy(screen)

    d = tree / Path(hs_rel)
    prefix = f"{MDD}-{PHONG}-01-0001"
    assert (d / f"{prefix}-002.pdf").is_file()  # giữ số, đứng đầu
    assert (d / f"{prefix}-003.pdf").read_bytes() == \
        f"{prefix}-001.pdf".encode()  # cũ 001 xếp sau → 003
    assert not (d / f"{prefix}-001.pdf").exists()
    assert hs_rel not in screen._manual_order  # đã ghi vào tên → dọn thứ tự
    screen.deleteLater()
