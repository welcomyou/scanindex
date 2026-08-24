"""Search gold-set benchmark for Kho lưu trữ (offline, synthetic).

Builds a synthetic archive of arbitrary size, rebuilds the search index,
then runs a fixed query battery (dense phrase / medium / sparse / no-match
/ filter+phrase / substring / fuzzy) and reports:
  - wall time per query (warm cache)
  - document coverage vs the SQL ground truth (recall = all matching docs)
  - the virtualized list population cost at that result size

Usage:
  python -X utf8 scripts/bench_search.py [--docs 7700] [--chunks-per-doc 13]
       [--match-frac 0.73] [--keep] [--label mylabel]

Examples (chunk budgets):
  ~53k chunks : --docs 4100 --chunks-per-doc 13
  ~100k chunks: --docs 7700 --chunks-per-doc 13
  ~500k chunks: --docs 38500 --chunks-per-doc 13
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FILLER = (
    "Kính gửi cơ quan chủ quản, căn cứ Nghị định số 45/2020/NĐ-CP về công "
    "tác văn bản và lưu trữ điện tử, cơ quan đã rà soát toàn bộ hồ sơ nghiệp "
    "vụ thuộc phạm vi quản lý trong quý vừa qua. Các đơn vị liên quan có "
    "trách nhiệm phối hợp thực hiện đúng tiến độ đã cam kết, báo cáo kết "
    "quả bằng văn bản có chữ ký của người có thẩm quyền trước ngày 30 hằng "
    "tháng. "
)


def build_repo(dst: Path, docs: int, chunks_per_doc: int,
               match_frac: float) -> dict:
    from scanindex.core.repository.tokenizer import to_no_diacritic

    rng = random.Random(42)
    n_match = int(docs * match_frac)
    db = sqlite3.connect(str(dst / "repository-bench.db"))
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        Path("scanindex/core/repository/schema.sql").read_text(encoding="utf-8")
    )
    now = int(time.time())
    dossier_id = db.execute(
        "INSERT INTO dossiers (ma_dinh_danh, fonds, catalog, dossier_code,"
        " title, fonds_name, retention, created_at)"
        " VALUES ('BENCH','B01','01','0001','Hồ sơ benchmark','Phông bench',"
        " 'Vĩnh viễn', ?)", (now,),
    ).lastrowid
    for k in range(docs):
        doc_id = f"benchdoc{k:06d}"
        matches = k < n_match
        db.execute(
            "INSERT INTO documents (doc_id, dossier_id, file_name, file_path,"
            " kie_doc_number_symbol, kie_issue_org_name, kie_doc_subject,"
            " kie_recipients, kie_signer_name, kie_place_date, kie_doc_type,"
            " kie_secrecy_mark, page_count, sha256, indexed_status,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, dossier_id, f"vb{k:06d}.pdf", f"pdf/b/{k}.pdf",
             f"{1000 + k}/QĐ-BENCH", "Ban benchmark",
             f"Về việc nghiệp vụ số {k}", "", "Người ký benchmark",
             "ngày 15 tháng 3 năm 2023", "Quyết định", "", 12, f"sha{k}",
             "indexed", now),
        )
        meta = f"Số ký hiệu: {1000 + k}/QĐ-BENCH. Trích yếu: Về việc {k}."
        rows = [(doc_id, 1, "metadata", 1, meta, to_no_diacritic(meta).lower(), now)]
        for c in range(chunks_per_doc - 1):
            body = FILLER * 2
            if matches and c in (3, 9):
                body += " Nơi nhận: - Như trên; - Lưu VT."
            if k % 97 == 0:
                body += " internetdangcap"   # chất liệu cho substring probe
            rows.append((doc_id, 1, "body", c + 1, body,
                         to_no_diacritic(body).lower(), now))
        db.executemany(
            "INSERT INTO chunks (doc_id, doc_version, chunk_type, page,"
            " block_idx, text_original, text_no_diacritic, bbox, word_count,"
            " indexed_status, created_at)"
            " VALUES (?,?,?,?, 0, ?,?, '[]', 40, 'indexed', ?)", rows)
    db.commit()
    stats = {
        "docs": db.execute(
            "SELECT COUNT(*) FROM documents WHERE indexed_status='indexed'"
        ).fetchone()[0],
        "chunks": db.execute(
            "SELECT COUNT(*) FROM chunks WHERE indexed_status='indexed'"
        ).fetchone()[0],
        "truth_phrase_docs": db.execute(
            "SELECT COUNT(DISTINCT doc_id) FROM chunks "
            "WHERE instr(lower(text_no_diacritic), 'noi nhan') > 0"
        ).fetchone()[0],
    }
    db.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=7700)
    ap.add_argument("--chunks-per-doc", type=int, default=13)
    ap.add_argument("--match-frac", type=float, default=0.73)
    ap.add_argument("--label", default="")
    ap.add_argument("--keep", action="store_true",
                    help="keep the temp repo (print path)")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="scanindex_bench_"))
    dst = tmp / "repository"
    dst.mkdir()
    label = args.label or f"{args.docs}d{args.chunks_per_doc}c"
    try:
        t0 = time.time()
        stats = build_repo(dst, args.docs, args.chunks_per_doc, args.match_frac)
        print(f"[{label}] build: {stats} ({time.time() - t0:.1f}s)")

        from scanindex.core.repository.reindex import rebuild_search_index
        from scanindex.core.repository.store import ArchiveStore
        from scanindex.core.repository.indexer import HybridIndex
        from scanindex.core.repository.search_engine import SearchEngine

        store = ArchiveStore(dst, db_filename="repository-bench.db")
        store.connect()
        store.ensure_schema()
        t0 = time.time()
        rb = rebuild_search_index(store, dst)
        print(f"[{label}] rebuild: {rb} ({time.time() - t0:.1f}s)")
        idx = HybridIndex(dst)
        idx.open()
        engine = SearchEngine(store, idx)

        # Gold-set battery.
        battery = [
            ("dense-phrase", "Nơi nhận", {}),
            ("medium-phrase", "văn bản", {}),
            ("sparse-phrase", "quyết định 45/2020", {}),
            ("no-match", "không tồn tại xyzzy", {}),
            ("filtered-phrase", "Nơi nhận", {"fonds": "B01"}),
            ("substring", "ternetdan", {}),
            ("fuzzy-ocr", "Nơi nhạn", {}),
        ]
        report = {"label": label, "setup": stats, "queries": []}
        for name, q, filters in battery:
            t0 = time.time()
            res = engine.search(q, filters, "all")
            dt_ms = (time.time() - t0) * 1000
            docs = {r.doc_id for r in res}
            kinds = {}
            for r in res:
                kinds[r.match_kind] = kinds.get(r.match_kind, 0) + 1
            row = {"query": name, "ms": round(dt_ms), "docs": len(docs),
                   "chunks": len(res), "kinds": kinds}
            if name == "dense-phrase":
                row["recall"] = (len(docs) / stats["truth_phrase_docs"]
                                 if stats["truth_phrase_docs"] else 1.0)
                row["truth"] = stats["truth_phrase_docs"]
            report["queries"].append(row)
            print(f"[{label}] {name:16s} {dt_ms:7.0f} ms  "
                  f"{len(docs):6d} docs  kinds={kinds}")

        # Virtualized list population cost at the dense size.
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        import scanindex.ui.repository.screen as scr
        res = engine.search("Nơi nhận", {}, "all")
        t0 = time.time()
        hits = scr._group_results_by_file(res)
        t_group = (time.time() - t0) * 1000
        model = scr._SearchResultsModel()
        entries = [("group", "Khớp từ khóa", len(hits))]
        entries += [("hit", i + 1, len(str(len(hits))), h)
                    for i, h in enumerate(hits)]
        t0 = time.time()
        model.set_entries(entries)
        t_model = (time.time() - t0) * 1000
        report["ui"] = {"group_ms": round(t_group, 1),
                        "model_ms": round(t_model, 2),
                        "rows": model.rowCount()}
        print(f"[{label}] UI: group={t_group:.0f} ms, "
              f"model.populate={t_model:.2f} ms, rows={model.rowCount()} "
              f"(virtualized — chỉ vẽ rows nhìn thấy)")

        out = Path(tempfile.gettempdir()) / f"scanindex_bench_{label}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"[{label}] report: {out}")
        idx.close()
        store.close()
    finally:
        if args.keep:
            print(f"[{label}] repo kept at: {dst}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
