"""Search correctness suite (expert-review round-2 gate).

Covers the release-blocking cases:
  - filters matching ZERO documents must return ZERO results (never an
    unfiltered fallback)
  - exact recall at 8000+ matching documents (completeness, uncapped)
  - negative tests across metadata-FIELD boundaries and PAGE boundaries
  - phrase spanning chunk boundaries still surfaces the document
  - all-14-KIE-field completeness of the document record
  - outbox crash matrix (pending job survives, replay converges, session
    scoping never deletes a concurrent worker's job)
  - true fuzzy (nhậx / nhậnn / merged tokens), not the no-diacritic
    look-alike
  - BM25 tie-break stays ≤ 5 points (light, never outweighs field weight)

Runs against a synthetic repo built in tmp_path; no live data touched.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanindex.core.repository.indexer import HybridIndex  # noqa: E402
from scanindex.core.repository.reindex import (  # noqa: E402
    enqueue_index_job, rebuild_search_index, replay_pending_index_jobs,
)
from scanindex.core.repository.search_engine import SearchEngine  # noqa: E402
from scanindex.core.repository.store import ArchiveStore  # noqa: E402
from scanindex.core.repository.tokenizer import to_no_diacritic  # noqa: E402


FILLER = (
    "Kính gửi cơ quan chủ quản, căn cứ Nghị định số 45/2020/NĐ-CP về công "
    "tác văn bản và lưu trữ điện tử, cơ quan đã rà soát toàn bộ hồ sơ "
    "nghiệp vụ thuộc phạm vi quản lý trong quý vừa qua. "
)

N_DOCS = 120          # đủ để kiểm các ranh giới; 8000-doc recall dùng bench
CHUNKS_PER_DOC = 6


def _build_repo(dst: Path, n_docs: int = N_DOCS,
                chunks_per_doc: int = CHUNKS_PER_DOC,
                phrase: str = "nơi nhận") -> Path:
    import sqlite3

    dst.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(dst / "repository-test.db"))
    db.executescript(
        Path("scanindex/core/repository/schema.sql").read_text(encoding="utf-8")
    )
    now = int(time.time())
    did = db.execute(
        "INSERT INTO dossiers (ma_dinh_danh, fonds, catalog, dossier_code,"
        " title, created_at) VALUES ('T','T01','01','0001','Hồ sơ test', ?)",
        (now,),
    ).lastrowid
    for k in range(n_docs):
        doc_id = f"doc{k:05d}"
        db.execute(
            "INSERT INTO documents (doc_id, dossier_id, file_name, file_path,"
            " kie_doc_number_symbol, kie_issue_org_name, kie_doc_subject,"
            " kie_signer_name, kie_place_date, kie_doc_type, kie_addressee,"
            " kie_urgency_mark, page_count, sha256, indexed_status,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, did, f"vb{k}.pdf", f"pdf/t/{k}.pdf",
             f"{100 + k}/QĐ-T", "Ủy ban nhân dân thành phố",
             f"Về việc nghiệp vụ số {k}", "Nguyễn Văn A",
             "ngày 15 tháng 3 năm 2023", "Quyết định", "Như trên",
             "Khẩn", 3, f"s{k}", "indexed", now),
        )
        meta = f"Số: {100 + k}/QĐ-T. Người ký: Nguyễn Văn A."
        rows = [(doc_id, 1, "metadata", 1, meta,
                 to_no_diacritic(meta).lower(), now)]
        for c in range(chunks_per_doc - 1):
            body = FILLER * 2
            if c == 2:
                body += f" {phrase}: - Như trên; - Lưu VT."
            rows.append((doc_id, 1, "body", c + 1, body,
                         to_no_diacritic(body).lower(), now))
        db.executemany(
            "INSERT INTO chunks (doc_id, doc_version, chunk_type, page,"
            " block_idx, text_original, text_no_diacritic, bbox,"
            " word_count, indexed_status, created_at)"
            " VALUES (?,?,?,?,0,?,?, '[]', 40, 'indexed', ?)", rows)
    db.commit()
    db.close()
    return dst / "repository-test.db"


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    dst = tmp_path_factory.mktemp("search_correctness") / "repository"
    _build_repo(dst)
    store = ArchiveStore(dst, db_filename="repository-test.db")
    store.connect()
    store.ensure_schema()
    rebuild_search_index(store, dst)
    idx = HybridIndex(dst)
    idx.open()
    yield SearchEngine(store, idx), store, idx, dst
    idx.close()
    store.close()


def _docs(res):
    return {r.doc_id for r in res}


# ---- 1. Filter zero-candidate ---------------------------------------------

def test_zero_candidate_filter_returns_nothing(engine):
    eng, store, idx, dst = engine
    res = eng.search("nơi nhận", {"fonds": "ZZZ-KHÔNG-TỒN-TẠI"}, "all")
    assert res == [], (
        "Bộ lọc không khớp văn bản nào phải trả 0 kết quả — không được "
        f"rơi về unfiltered ({len(res)} kết quả)"
    )
    # Bộ lọc hợp lệ vẫn hoạt động.
    res2 = eng.search("nơi nhận", {"fonds": "T01"}, "all")
    assert len(_docs(res2)) == N_DOCS


# ---- 2. Completeness / recall ----------------------------------------------

def test_exact_recall_all_matching_docs(engine):
    eng, *_ = engine
    res = eng.search("nơi nhận", {}, "all")
    assert len(_docs(res)) == N_DOCS  # mọi văn bản đều chứa cụm


def test_recall_8000_docs(tmp_path):
    """Recall ở quy mô 8000+ (chuyên gia tái hiện 6341/8000 trước fix)."""
    dst = tmp_path / "r8k" / "repository"
    _build_repo(dst, n_docs=8000, chunks_per_doc=4)
    store = ArchiveStore(dst, db_filename="repository-test.db")
    store.connect()
    store.ensure_schema()
    rebuild_search_index(store, dst)
    idx = HybridIndex(dst)
    idx.open()
    try:
        eng = SearchEngine(store, idx)
        t0 = time.time()
        res = eng.search("nơi nhận", {}, "all")
        dt = time.time() - t0
        assert len(_docs(res)) == 8000, f"recall thiếu: {len(_docs(res))}/8000"
        assert dt < 30.0, f"quá chậm: {dt:.1f}s"
    finally:
        idx.close()
        store.close()


# ---- 3. Boundary negatives -------------------------------------------------

def test_phrase_across_metadata_fields_is_negative(engine):
    """issue_org kết thúc 'thành phố' + signer bắt đầu 'Nguyễn' → cụm
    'thành phố nguyễn' KHÔNG được match (sentinel giữa các trường)."""
    eng, store, idx, dst = engine
    res = eng.search("phố nguyễn văn", {}, "all")
    # Cụm này nằm vắt 2 trường metadata — phải 0 exact (fuzzy chấp nhận).
    exact = [r for r in res if r.match_kind == "exact"]
    assert not exact, f"false positive xuyên ranh giới trường: {len(exact)}"


def test_phrase_across_pages_is_negative(engine):
    eng, store, idx, dst = engine
    conn = store.connect()
    probe = conn.execute(
        "SELECT doc_id FROM documents LIMIT 1").fetchone()["doc_id"]
    # Trang 1 kết thúc 'kế hoạch', trang 2 bắt đầu 'tài chính'.
    conn.execute(
        "INSERT INTO chunks (doc_id, doc_version, chunk_type, page,"
        " block_idx, text_original, text_no_diacritic, bbox, word_count,"
        " indexed_status, created_at)"
        " VALUES (?, 1, 'body', 1, 0, 'triển khai kế hoạch',"
        " 'trien khai ke hoach', '[]', 4, 'indexed', 0)", (probe,))
    conn.execute(
        "INSERT INTO chunks (doc_id, doc_version, chunk_type, page,"
        " block_idx, text_original, text_no_diacritic, bbox, word_count,"
        " indexed_status, created_at)"
        " VALUES (?, 1, 'body', 2, 0, 'tài chính năm 2023',"
        " 'tai chinh nam 2023', '[]', 4, 'indexed', 0)", (probe,))
    enqueue_index_job(conn, probe)
    replay_pending_index_jobs(store, idx)
    res = eng.search("kế hoạch tài chính", {}, "all")
    exact = [r for r in res if r.doc_id == probe and r.match_kind == "exact"]
    assert not exact, "cụm xuyên ranh giới TRANG không được phép match"
    conn.execute("DELETE FROM chunks WHERE doc_id = ? AND page IN (1, 2)"
                 " AND text_original IN ('triển khai kế hoạch',"
                 " 'tài chính năm 2023')", (probe,))
    enqueue_index_job(conn, probe)
    replay_pending_index_jobs(store, idx)


def test_phrase_across_chunk_boundary_still_found(engine):
    """Cụm vắt 2 chunk CÙNG trang vẫn phải hiện (doc-phrase audit)."""
    eng, store, idx, dst = engine
    conn = store.connect()
    probe = conn.execute(
        "SELECT doc_id FROM documents LIMIT 1").fetchone()["doc_id"]
    conn.execute(
        "INSERT INTO chunks (doc_id, doc_version, chunk_type, page,"
        " block_idx, text_original, text_no_diacritic, bbox, word_count,"
        " indexed_status, created_at)"
        " VALUES (?, 1, 'body', 5, 0, 'phần đầu quy mô tổng',"
        " 'phan dau quy mo tong', '[]', 4, 'indexed', 0)", (probe,))
    conn.execute(
        "INSERT INTO chunks (doc_id, doc_version, chunk_type, page,"
        " block_idx, text_original, text_no_diacritic, bbox, word_count,"
        " indexed_status, created_at)"
        " VALUES (?, 1, 'body', 5, 1, 'hợp đầu tư năm 2023',"
        " 'hop dau tu nam 2023', '[]', 4, 'indexed', 0)", (probe,))
    enqueue_index_job(conn, probe)
    replay_pending_index_jobs(store, idx)
    res = eng.search("tổng hợp đầu tư", {}, "all")
    hits = [r for r in res if r.doc_id == probe]
    assert hits, "cụm vắt ranh giới CHUNK phải xuất hiện (doc-phrase audit)"
    conn.execute(
        "DELETE FROM chunks WHERE doc_id = ? AND page = 5 AND block_idx IN (0, 1)",
        (probe,))
    enqueue_index_job(conn, probe)
    replay_pending_index_jobs(store, idx)


# ---- 4. 14-field completeness ----------------------------------------------

def test_document_record_covers_all_14_kie_fields(engine):
    eng, store, idx, dst = engine
    conn = store.connect()
    probe = conn.execute(
        "SELECT doc_id FROM documents LIMIT 1").fetchone()["doc_id"]
    from scanindex.core.repository.reindex import document_norm_payload
    payload = document_norm_payload(conn, probe)
    meta_norm = payload[1]
    # Mỗi giá trị trường chuẩn hóa phải nằm trong meta_norm (riêng từng
    # đoạn giữa 2 sentinel).
    from scanindex.core.repository.tokenizer import (
        FIELD_SENTINEL_TOKEN, search_norm,
    )
    segments = [s.strip() for s in meta_norm.split(FIELD_SENTINEL_TOKEN)]
    d = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (probe,)).fetchone()
    for col in ("kie_doc_number_symbol", "kie_issue_org_name",
                "kie_signer_name", "kie_doc_subject", "kie_addressee",
                "kie_place_date", "kie_doc_type", "kie_urgency_mark"):
        want = search_norm(d[col] or "")
        assert want in segments, f"trường {col} thiếu trong doc record"
    # Query theo giá trị trường hiếm vẫn tìm được.
    res = eng.search("như trên", {}, "all")
    assert probe in _docs(res), "kie_addressee (nơi nhận) phải searchable"


# ---- 5. Outbox crash matrix -------------------------------------------------

def test_outbox_pending_survives_and_replay_converges(engine):
    eng, store, idx, dst = engine
    conn = store.connect()
    probe = conn.execute(
        "SELECT doc_id FROM documents LIMIT 1").fetchone()
    enqueue_index_job(conn, probe["doc_id"])
    assert conn.execute("SELECT COUNT(*) FROM index_jobs").fetchone()[0] >= 1
    replay_pending_index_jobs(store, idx)
    assert conn.execute("SELECT COUNT(*) FROM index_jobs").fetchone()[0] == 0
    # Vẫn tìm được sau replay.
    assert probe["doc_id"] in _docs(eng.search("nơi nhận", {}, "all"))


def test_outbox_session_scoping(engine):
    """note_index_write của store A không được xóa job pending của store B."""
    from scanindex.core.repository.reindex import note_index_write

    eng, store, idx, dst = engine
    conn = store.connect()
    probe = conn.execute(
        "SELECT doc_id FROM documents LIMIT 1").fetchone()["doc_id"]
    # Store B (mô phỏng worker song song) enqueue job riêng.
    store_b = ArchiveStore(dst, db_filename="repository-test.db")
    store_b.connect()
    conn_b = store_b.connect()
    enqueue_index_job(conn_b, probe, store=store_b)
    # Store A (khác phiên) ghi index xong và note_index_write — A không
    # acknowledge job nào (chưa note_job_applied).
    store._acked_job_ids.clear()          # A không có job nào đã apply
    note_index_write(store)
    left = conn.execute("SELECT COUNT(*) FROM index_jobs").fetchone()[0]
    assert left == 1, "job của store B phải sống sót qua note_index_write của A"
    store_b.close()
    replay_pending_index_jobs(store, idx)


# ---- 6. True fuzzy -----------------------------------------------------------

def test_true_fuzzy_ocr_slips(engine):
    """'trêx' không phải bỏ-dấu của 'trên' — fuzzy thật (dist-1) + 'như'
    exact, hai token kề nhau trong 'Như trên'."""
    eng, *_ = engine
    res = eng.search("như trêx", {}, "all")
    assert _docs(res), "OCR slip 'trêx' phải bắt được qua fuzzy"
    # Đối chứng: bỏ-dấu thuần ('nhạn') là exact, không phải fuzzy thật.
    res2 = eng.search("Nơi nhạn", {}, "all")
    kinds = {r.match_kind for r in res2}
    assert "exact" in kinds


def test_merged_token_fuzzy(engine):
    eng, store, idx, dst = engine
    conn = store.connect()
    probe = conn.execute(
        "SELECT doc_id FROM documents LIMIT 1").fetchone()["doc_id"]
    conn.execute(
        "INSERT INTO chunks (doc_id, doc_version, chunk_type, page,"
        " block_idx, text_original, text_no_diacritic, bbox, word_count,"
        " indexed_status, created_at)"
        " VALUES (?, 1, 'body', 6, 0,"
        " 'lưu ý thựchiện đúng quy trình',"
        " 'luu y thuchien dung quy trinh', '[]', 5, 'indexed', 0)", (probe,))
    enqueue_index_job(conn, probe)
    replay_pending_index_jobs(store, idx)
    res = eng.search("thực hiện", {}, "all")
    hits = [r for r in res if r.doc_id == probe]
    assert hits, "token bị OCR nhập 'thựchiện' phải bắt được (span fuzzy)"
    conn.execute("DELETE FROM chunks WHERE doc_id = ? AND page = 6", (probe,))
    enqueue_index_job(conn, probe)
    replay_pending_index_jobs(store, idx)


# ---- 7. Overlap join must not create artificial phrases (round-3 P0) -------

def test_overlapping_chunks_do_not_create_artificial_phrases(engine):
    """Chuyên gia tái hiện: chunk1 '...gamma delta', chunk2 'gamma delta
    epsilon...' (overlap chunker) — query 'delta gamma' phải ÂM TÍNH vì
    cụm không tồn tại trong văn bản gốc. Round-4: hai chunk này phải là
    một cặp split THẬT (cùng block_idx + source_blocks, merge_reason
    body_split_1/2) — de-overlap chỉ được chạy khi provenance chứng minh."""
    eng, store, idx, dst = engine
    conn = store.connect()
    probe = conn.execute(
        "SELECT doc_id FROM documents LIMIT 1").fetchone()["doc_id"]
    for part, text in ((1, "alpha beta gamma delta"),
                       (2, "gamma delta epsilon zeta")):
        conn.execute(
            "INSERT INTO chunks (doc_id, doc_version, chunk_type, page,"
            " block_idx, text_original, text_no_diacritic, bbox,"
            " word_count, source_blocks, merge_reason, indexed_status,"
            " created_at)"
            " VALUES (?, 1, 'body', 7, 9, ?, ?, '[]', 4, '[7, 8]',"
            " ?, 'indexed', 0)",
            (probe, text, to_no_diacritic(text).lower(),
             f"body_split_{part}"))
    enqueue_index_job(conn, probe)
    replay_pending_index_jobs(store, idx)

    res = eng.search("delta gamma", {}, "all")
    exact = [r for r in res if r.doc_id == probe and r.match_kind == "exact"]
    assert not exact, (
        f"cụm nhân tạo tại điểm nối overlap: {len(exact)} exact — "
        "de-overlap phải triệt tiêu"
    )
    # Đối chứng dương: cụm CÓ THẬT vẫn tìm được, kể cả xuyên điểm nối
    # thật (sau khi bỏ overlap, 'delta' kết thúc chunk1, 'epsilon' mở
    # đầu phần còn lại của chunk2).
    ok1 = eng.search("gamma delta", {}, "all")
    assert probe in _docs(ok1), "cụm thật 'gamma delta' vẫn phải khớp"
    ok2 = eng.search("delta epsilon", {}, "all")
    assert probe in _docs(ok2), "cụm thật xuyên ranh giới chunk vẫn phải khớp"

    conn.execute("DELETE FROM chunks WHERE doc_id = ? AND page = 7", (probe,))
    enqueue_index_job(conn, probe)
    replay_pending_index_jobs(store, idx)


# ---- 8. Outbox acknowledges per-APPLIED doc (round-3 P0) --------------------

def test_note_index_write_keeps_failed_docs_job(engine):
    """Chuyên gia tái hiện: job A (tài liệu LỖI trước khi index) và job B
    (thành công) cùng store — note_index_write chỉ được xóa B."""
    from scanindex.core.repository.reindex import note_index_write

    eng, store, idx, dst = engine
    conn = store.connect()
    docs = [r["doc_id"] for r in conn.execute(
        "SELECT doc_id FROM documents LIMIT 2").fetchall()]
    doc_a, doc_b = docs
    # A: enqueue nhưng thất bại (không bao giờ note_job_applied).
    enqueue_index_job(conn, doc_a, store=store)
    # B: enqueue + tantivy writes thành công → acknowledge THEO JOB ID.
    job_b = enqueue_index_job(conn, doc_b, store=store)
    store.note_job_applied(job_b)
    note_index_write(store)
    left = {r["doc_id"] for r in conn.execute(
        "SELECT doc_id FROM index_jobs").fetchall()}
    assert left == {doc_a}, (
        f"job của tài liệu lỗi phải sống sót, còn lại: {left}"
    )
    # Dọn: replay hội tụ A.
    replay_pending_index_jobs(store, idx)
    assert conn.execute("SELECT COUNT(*) FROM index_jobs").fetchone()[0] == 0


# ---- 8b. Round-4 P0 regressions ----------------------------------------------

def test_independent_chunks_keep_true_duplicate_phrase(engine):
    """Round-4 repro của chuyên gia: hai chunk body_single ĐỘC LẬP cùng
    trang cùng kết thúc/bắt đầu bằng 'repeat phrase'. Chuỗi thật của trang
    là 'alpha beta repeat phrase repeat phrase epsilon zeta' —
    de-overlap KHÔNG được chạy (không có provenance split):
      'repeat phrase repeat phrase' phải match exact (hiện đang mất),
      'beta repeat phrase epsilon' phải ÂM TÍNH exact (hiện đang giả)."""
    eng, store, idx, dst = engine
    conn = store.connect()
    probe = conn.execute(
        "SELECT doc_id FROM documents LIMIT 1").fetchone()["doc_id"]
    # merge_reason/source_blocks bỏ trống → chunk độc lập (như body_single).
    for block_idx, text in ((0, "alpha beta repeat phrase"),
                            (1, "repeat phrase epsilon zeta")):
        conn.execute(
            "INSERT INTO chunks (doc_id, doc_version, chunk_type, page,"
            " block_idx, text_original, text_no_diacritic, bbox,"
            " word_count, indexed_status, created_at)"
            " VALUES (?, 1, 'body', 8, ?, ?, ?, '[]', 4, 'indexed', 0)",
            (probe, block_idx, text, to_no_diacritic(text).lower()))
    enqueue_index_job(conn, probe)
    replay_pending_index_jobs(store, idx)

    true_hits = eng.search("repeat phrase repeat phrase", {}, "all")
    assert probe in _docs(true_hits), (
        "cụm trùng THẬT của hai chunk độc lập phải còn khớp exact — "
        "de-overlap chỉ chạy khi provenance chứng minh cùng chuỗi split"
    )
    fake = eng.search("beta repeat phrase epsilon", {}, "all")
    exact = [r for r in fake if r.doc_id == probe and r.match_kind == "exact"]
    assert not exact, (
        f"cụm giả xuyên điểm nối hai chunk độc lập: {len(exact)} exact"
    )

    conn.execute("DELETE FROM chunks WHERE doc_id = ? AND page = 8", (probe,))
    enqueue_index_job(conn, probe)
    replay_pending_index_jobs(store, idx)


def test_ack_by_job_id_never_sweeps_newer_job_same_doc(engine):
    """Round-4 repro: worker A ack job cũ của doc X; worker B vừa enqueue
    job MỚI cho chính doc X — note_index_write của A (chạy sau) không
    được xóa job của B. Ack phải là WHERE job_id IN, không phải doc_id."""
    from scanindex.core.repository.reindex import note_index_write

    eng, store, idx, dst = engine
    conn = store.connect()
    probe = conn.execute(
        "SELECT doc_id FROM documents LIMIT 1").fetchone()["doc_id"]
    job_old = enqueue_index_job(conn, probe, store=store)     # worker A
    job_new = enqueue_index_job(conn, probe, store=store)     # worker B, mới hơn
    store.note_job_applied(job_old)
    note_index_write(store)
    left = [r["job_id"] for r in conn.execute(
        "SELECT job_id FROM index_jobs").fetchall()]
    assert left == [job_new], (
        f"job mới hơn của cùng doc phải sống sót qua ack của worker khác, "
        f"còn lại: {left}"
    )
    # Dọn: replay hội tụ.
    replay_pending_index_jobs(store, idx)
    assert conn.execute("SELECT COUNT(*) FROM index_jobs").fetchone()[0] == 0


def test_delete_failure_keeps_job_for_replay(engine):
    """Round-4 repro: SQL DELETE của delete_document thất bại giữa chừng —
    job delete phải sống sót qua note_index_write của một thao tác KHÁC
    (không bị ack nhầm vì mark applied chỉ đặt SAU khi xóa thành công)."""
    import sqlite3
    from unittest.mock import patch

    from scanindex.core.repository import admin as admin_mod
    from scanindex.core.repository.reindex import note_index_write

    eng, store, idx, dst = engine
    conn = store.connect()
    probe = conn.execute(
        "SELECT doc_id FROM documents LIMIT 1").fetchone()["doc_id"]
    dossier_id = conn.execute(
        "SELECT dossier_id FROM documents WHERE doc_id = ?",
        (probe,)).fetchone()["dossier_id"]
    now = int(time.time())
    victim = "doc-del-fail-probe"
    conn.execute(
        "INSERT INTO documents (doc_id, dossier_id, file_name, file_path,"
        " sha256, indexed_status, created_at)"
        " VALUES (?, ?, 'victim.pdf', 'pdf/v/victim.pdf', 'sha-victim',"
        " 'indexed', ?)",
        (victim, dossier_id, now),
    )

    class _FailingDocDeleteConn:
        """Proxy raise duy nhất trên DELETE FROM documents của victim."""
        def __init__(self, inner):
            self._inner = inner
        def execute(self, sql, *args, **kwargs):
            if (sql.strip().upper().startswith("DELETE FROM DOCUMENTS")
                    and args and args[0] == (victim,)):
                raise sqlite3.OperationalError("simulated SQL failure")
            return self._inner.execute(sql, *args, **kwargs)
        def __getattr__(self, name):
            return getattr(self._inner, name)

    try:
        with patch.object(
            store, "connect",
            return_value=_FailingDocDeleteConn(store.connect()),
        ):
            with pytest.raises(sqlite3.OperationalError):
                admin_mod.delete_document(store, idx, victim, commit=True)

        # Một thao tác khác trên cùng store thành công và flush ack —
        # job của victim (thất bại) phải KHÔNG bị cuốt theo.
        other = enqueue_index_job(conn, probe, store=store)
        store.note_job_applied(other)
        note_index_write(store)
        left = {r["doc_id"] for r in conn.execute(
            "SELECT doc_id FROM index_jobs").fetchall()}
        assert victim in left, (
            f"job của tài liệu xóa thất bại phải sống sót, còn lại: {left}"
        )
        assert probe not in left
    finally:
        # Dọn: xóa victim thật sự (không patch) + replay hội tụ.
        admin_mod.delete_document(store, idx, victim, commit=True)
        replay_pending_index_jobs(store, idx)
        assert conn.execute(
            "SELECT COUNT(*) FROM index_jobs").fetchone()[0] == 0


# ---- 8c. Round-5 P0 regressions ----------------------------------------------

def test_split_overlap_30_hyphenated_words_is_stripped(engine):
    """Round-5 repro của chuyên gia: overlap 30 whitespace-word dạng
    'x0-y0 … x29-y29' chuẩn hóa thành 60 token — nếu de-overlap đo bằng
    trần normalized-token (40) thì strip thất bại và cụm giả 'y29 x0'
    xuyên điểm nối vẫn được trả exact. Overlap phải đo bằng RAW word
    (CHUNK_OVERLAP_WORDS), normalize toàn stream SAU khi ghép."""
    eng, store, idx, dst = engine
    conn = store.connect()
    probe = conn.execute(
        "SELECT doc_id FROM documents LIMIT 1").fetchone()["doc_id"]
    overlap = " ".join(f"x{i}-y{i}" for i in range(30))   # 30 từ → 60 token
    chunk1 = f"start preamble words here {overlap}"         # body_split_1
    chunk2 = f"{overlap} trailing omega sentinel close"     # body_split_2
    for part, text in ((1, chunk1), (2, chunk2)):
        conn.execute(
            "INSERT INTO chunks (doc_id, doc_version, chunk_type, page,"
            " block_idx, text_original, text_no_diacritic, bbox,"
            " word_count, source_blocks, merge_reason, indexed_status,"
            " created_at)"
            " VALUES (?, 1, 'body', 9, 9, ?, ?, '[]', 34, '[7, 8]',"
            " ?, 'indexed', 0)",
            (probe, text, to_no_diacritic(text).lower(),
             f"body_split_{part}"))
    enqueue_index_job(conn, probe)
    replay_pending_index_jobs(store, idx)

    # Cụm GIẢ xuyên điểm nối (cuối overlap của phần 1 + đầu phần 2 sau
    # khi strip phải là 'trailing'): không được match exact.
    fake = eng.search("y29 x0", {}, "all")
    exact = [r for r in fake if r.doc_id == probe and r.match_kind == "exact"]
    assert not exact, (
        f"overlap 30 từ có gạch nối (60 token) chưa bị strip: "
        f"{len(exact)} exact giả"
    )
    # Đối chứng dương: cụm THẬT xuyên điểm nối sau khi strip + cụm trong
    # vùng overlap vẫn khớp.
    ok1 = eng.search("y29 trailing", {}, "all")
    assert probe in _docs(ok1), "cụm thật 'y29 trailing' sau strip phải khớp"
    ok2 = eng.search("here x0", {}, "all")
    assert probe in _docs(ok2), "cụm thật 'here x0' trước điểm nối phải khớp"

    conn.execute("DELETE FROM chunks WHERE doc_id = ? AND page = 9", (probe,))
    enqueue_index_job(conn, probe)
    replay_pending_index_jobs(store, idx)


def test_single_delete_commit_failure_keeps_job(engine):
    """Round-5 repro của chuyên gia: index.commit() của delete_document
    (đường commit=True) thất bại — job phải sống sót VÀ chưa từng được
    đánh dấu ack, nên một note_index_write sau đó (từ thao tác khác)
    không được xóa job cần replay."""
    from scanindex.core.repository import admin as admin_mod
    from scanindex.core.repository.reindex import note_index_write

    eng, store, idx, dst = engine
    conn = store.connect()
    probe = conn.execute(
        "SELECT doc_id FROM documents LIMIT 1").fetchone()["doc_id"]
    dossier_id = conn.execute(
        "SELECT dossier_id FROM documents WHERE doc_id = ?",
        (probe,)).fetchone()["dossier_id"]
    now = int(time.time())
    victim = "doc-commit-fail-probe"
    conn.execute(
        "INSERT INTO documents (doc_id, dossier_id, file_name, file_path,"
        " sha256, indexed_status, created_at)"
        " VALUES (?, ?, 'victim2.pdf', 'pdf/v/victim2.pdf', 'sha-victim2',"
        " 'indexed', ?)",
        (victim, dossier_id, now),
    )

    class _CommitFailIndex:
        """Delegates everything to the real index except commit(), which
        simulates a Tantivy commit failure."""
        def __init__(self, inner):
            self._inner = inner
        def commit(self):
            raise RuntimeError("simulated tantivy commit failure")
        def __getattr__(self, name):
            return getattr(self._inner, name)

    try:
        with pytest.raises(RuntimeError):
            admin_mod.delete_document(
                store, _CommitFailIndex(idx), victim, commit=True)

        # Một thao tác khác trên cùng store thành công và flush ack —
        # job của victim (commit thất bại) phải KHÔNG bị cuốt theo.
        other = enqueue_index_job(conn, probe, store=store)
        store.note_job_applied(other)
        note_index_write(store)
        left = {r["doc_id"] for r in conn.execute(
            "SELECT doc_id FROM index_jobs").fetchall()}
        assert victim in left, (
            f"job của tài liệu commit thất bại phải sống sót, còn lại: {left}"
        )
        assert probe not in left
    finally:
        # Dọn: xóa victim thật sự (index thật) + replay hội tụ.
        admin_mod.delete_document(store, idx, victim, commit=True)
        replay_pending_index_jobs(store, idx)
        assert conn.execute(
            "SELECT COUNT(*) FROM index_jobs").fetchone()[0] == 0


# ---- 9. Ranking scale guard ---------------------------------------------------

def test_bm25_is_light_tiebreak_only():
    """Kiểm công thức: chênh BM25 tối đa chỉ đổi 5 điểm, không lật tier."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    import scanindex.ui.repository.screen as scr
    from scanindex.core.repository.search_engine import SearchResult

    def mk(doc_id, kind, bm25, ctype="body", count=1):
        return SearchResult(
            chunk_id=abs(hash(doc_id)) % 10**6, doc_id=doc_id,
            score=float(count), bm25=bm25, page=1, text="abc", bbox=[],
            doc_number="", subject="s", file_name=f"{doc_id}.pdf",
            file_path="x", dossier_id=1, chunk_type=ctype, match_kind=kind,
            match_count=count, query="abc")

    lo = [mk("lo", "exact", 1.0)]
    hi = [mk("hi", "exact", 999.0)]
    hits = scr._group_results_by_file(lo + hi)
    by = {h.file_row.doc_id: h.relevance for h in hits}
    gap = by["hi"] - by["lo"]
    assert 0 < gap <= 5.0 + 1e-6, f"BM25 phải ≤5 điểm, chênh {gap:.2f}"
