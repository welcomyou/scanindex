-- Kho lưu trữ schema v2
--
-- v1 → v2: documents table now stores 14 RAW KIE fields directly (10 trained
-- labels + 3 rule-based marks + DOC_TYPE) plus a full `kie_annotation_json`
-- column carrying every field's bbox/page/score from the canonical JSON.
-- The 20-column HSLTCQ projection is computed on-the-fly at xlsx export
-- time only — see archive_export/hsltcq_mapper.py.
--
-- SQLite is the source of truth; Tantivy is the derived full-text index
-- reconciled against this on startup.

PRAGMA foreign_keys = ON;

-- =====================================================================
-- Hồ sơ (Dossier) — folder-level container, holds many documents.
-- 4 IdentityCodes captured at Step 1 form the natural unique key.
-- Aggregate fields (start_date / end_date / page_count / doc_count /
-- confidentiality) are derived from the dossier's documents.
-- =====================================================================
CREATE TABLE IF NOT EXISTS dossiers (
    dossier_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 4 codes from Step 1's IdentityCodes (auto-generated when
    -- is_unstructured=1; user-filled when 0)
    ma_dinh_danh    TEXT NOT NULL,           -- Mã định danh đơn vị lưu trữ
    fonds           TEXT NOT NULL,           -- Mã phông
    fonds_name      TEXT,                    -- Tên phông (≤1000 chars; Excel display)
    catalog         TEXT NOT NULL,           -- Mục lục (≤2 chars)
    catalog_name    TEXT,                    -- Tên mục lục (≤1000 chars; Excel display)
    dossier_code    TEXT NOT NULL,           -- Hồ sơ (≤5 chars)
    -- Tên hồ sơ (≤1000 chars). Required for unstructured dossiers,
    -- optional for structured ones (empty = use composite code).
    title           TEXT,
    -- 1 = title-only dossier (codes auto-generated, code-system bypass);
    -- 0 = standard archive structure with the 4 codes user-filled.
    is_unstructured INTEGER NOT NULL DEFAULT 0,
    -- Optional dossier-level metadata
    retention       TEXT,                    -- Thời hạn bảo quản
    term            TEXT,                    -- Nhiệm kỳ
    storage_unit    TEXT,                    -- Đơn vị bảo quản số
    physical_state  TEXT,                    -- Tình trạng vật lý
    topic           TEXT,                    -- Chuyên đề (≤1000 chars; not exported)
    note            TEXT,                    -- Chú thích (≤1000 chars; not exported)
    -- Derived counters / spans (refresh after import)
    start_date      TEXT,
    end_date        TEXT,
    page_count      INTEGER,
    doc_count       INTEGER,
    confidentiality TEXT,                    -- Highest level among member docs
    -- Bookkeeping
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER,
    UNIQUE(ma_dinh_danh, fonds, catalog, dossier_code)
);
CREATE INDEX IF NOT EXISTS idx_dossiers_fonds   ON dossiers(fonds);
CREATE INDEX IF NOT EXISTS idx_dossiers_catalog ON dossiers(catalog);

-- =====================================================================
-- Văn bản (Document) — 14 raw KIE fields, lossless from canonical JSON.
-- doc_id identifies one document instance in one dossier. The first imported
-- copy may use sha256 directly; later copies of the same PDF in other dossiers
-- get a suffix. sha256 remains stored separately for duplicate/audit checks.
-- =====================================================================
CREATE TABLE IF NOT EXISTS documents (
    doc_id                    TEXT PRIMARY KEY,
    dossier_id                INTEGER REFERENCES dossiers(dossier_id) ON DELETE CASCADE,
    file_name                 TEXT NOT NULL,    -- canonical name, no _ocr suffix
    file_path                 TEXT NOT NULL,    -- relative to Kholuutru/pdf
    -- 10 trained KIE labels (raw OCR span text, multiline preserved)
    kie_regime_header         TEXT,
    kie_issue_org_superior    TEXT,
    kie_issue_org_name        TEXT,
    kie_doc_number_symbol     TEXT,
    kie_place_date            TEXT,
    kie_doc_subject           TEXT,
    kie_addressee             TEXT,
    kie_recipients            TEXT,
    kie_signer_role           TEXT,
    kie_signer_name           TEXT,
    -- 3 rule-based marks
    kie_urgency_mark          TEXT,
    kie_secrecy_mark          TEXT,
    kie_circulation_mark      TEXT,
    -- 1 deterministic post-process
    kie_doc_type              TEXT,
    -- Full canonical KIE annotation block — preserves bbox / page_index /
    -- score / field_id for re-display and downstream re-export.
    kie_annotation_json       TEXT,
    -- System
    page_count                INTEGER,
    sha256                    TEXT NOT NULL,
    indexed_status            TEXT NOT NULL DEFAULT 'pending',  -- pending/indexed/failed/deleted
    indexed_at                INTEGER,
    doc_version               INTEGER NOT NULL DEFAULT 1,
    created_at                INTEGER NOT NULL,
    updated_at                INTEGER
);
CREATE INDEX IF NOT EXISTS idx_documents_dossier    ON documents(dossier_id);
CREATE INDEX IF NOT EXISTS idx_documents_status     ON documents(indexed_status);
CREATE INDEX IF NOT EXISTS idx_documents_signer     ON documents(kie_signer_name);
CREATE INDEX IF NOT EXISTS idx_documents_doc_number ON documents(kie_doc_number_symbol);
CREATE INDEX IF NOT EXISTS idx_documents_issue_org  ON documents(kie_issue_org_name);
CREATE INDEX IF NOT EXISTS idx_documents_doc_type   ON documents(kie_doc_type);
CREATE INDEX IF NOT EXISTS idx_documents_secrecy    ON documents(kie_secrecy_mark);
CREATE INDEX IF NOT EXISTS idx_documents_sha256     ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_file_name  ON documents(file_name);

-- =====================================================================
-- Chunks — retrieval units. Block-aware splitting; merge_reason logged
-- so we can debug why a chunk was created or merged.
-- =====================================================================
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id            TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    doc_version       INTEGER NOT NULL,
    -- 'metadata' = synthesised KIE summary (1 per doc, Tantivy only).
    -- 'body'     = paragraph-level OCR text.
    -- 'noise' rows are not stored — filtered upstream in chunker.
    chunk_type        TEXT NOT NULL DEFAULT 'body',
    page              INTEGER NOT NULL,
    block_idx         INTEGER NOT NULL,
    text_original     TEXT NOT NULL,
    text_no_diacritic TEXT NOT NULL,
    text_segmented    TEXT,
    bbox              TEXT NOT NULL,
    word_count        INTEGER NOT NULL,
    source_blocks     TEXT,
    merge_reason      TEXT,           -- debug only: kie_metadata / body_merged / body_split / body_single
    indexed_status    TEXT NOT NULL DEFAULT 'pending',
    created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc    ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(indexed_status);
CREATE INDEX IF NOT EXISTS idx_chunks_page   ON chunks(doc_id, page);
CREATE INDEX IF NOT EXISTS idx_chunks_type   ON chunks(chunk_type);

-- =====================================================================
-- =====================================================================
-- =====================================================================
-- Index meta — versions, counters, config snapshot.
-- =====================================================================
CREATE TABLE IF NOT EXISTS index_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER NOT NULL
);

-- =====================================================================
-- Import history — one row per import session for audit / debug.
-- =====================================================================
CREATE TABLE IF NOT EXISTS import_history (
    import_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_folder TEXT NOT NULL,
    started_at    INTEGER NOT NULL,
    finished_at   INTEGER,
    status        TEXT NOT NULL,
    docs_imported INTEGER NOT NULL DEFAULT 0,
    docs_skipped  INTEGER NOT NULL DEFAULT 0,
    docs_failed   INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_import_status ON import_history(status);
