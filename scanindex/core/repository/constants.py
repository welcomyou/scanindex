"""Constants for Kho lÆ°u trá»¯ (Archive store).

Version strings here are checked against `index_meta` table on startup;
mismatch triggers a rebuild prompt. Bump when behavior changes
in a way that invalidates the on-disk index.
"""

# ---------- Versions (bump to trigger rebuild) ----------
SCHEMA_VERSION       = "11"     # v11: index_jobs outbox (crash-safe SQLite↔Tantivy sync)
CHUNKER_VERSION      = "2.1"    # tighter merge: TARGET=100 / MAX=250 / Y_RATIO=6 + tiny-chunk rescue
TOKENIZER_NAME       = "underthesea"
TOKENIZER_VERSION    = "1.x"           # filled at runtime if available

# ---------- Default folder layout ----------
DEFAULT_REPOSITORY_DIRNAME = "repository"   # relative to app base dir
DEFAULT_ARCHIVE_DIRNAME    = DEFAULT_REPOSITORY_DIRNAME
PDF_SUBDIR              = "pdf"
# The Tantivy folder is versioned like the SQLite file (version-per-file):
# bumping INDEXER_VERSION must bump this name so a new index is built
# alongside the old one. Older app releases keep reading their own folder,
# which makes downgrading safe (worst case: stale search until re-import).
TANTIVY_SUBDIR          = "tantivy_index-v3"
LEGACY_TANTIVY_SUBDIRS  = ("tantivy_index", "tantivy_index-v2")  # wiped only by reset
SQLITE_FILE             = "repository.db"
IMPORT_LOG_FILE         = "import_log.json"

# ---------- Search parameters ----------
TANTIVY_TOP_K          = 100
MIN_RESULTS            = 5              # below this -> fallback
# Completeness guard: the Tantivy passes cap candidates at TANTIVY_TOP_K
# *chunks*, so a phrase spread over many chunks per document (e.g. "Nơi
# nhận" in nearly every doc) can hide whole documents. The literal-phrase
# SQL pass therefore ALWAYS unions into the exact group — DOCUMENT-aware:
# at most PER_DOC matching chunks per document (enough for ranking, the
# UI's relevance sums the top-3 chunks) and at most MAX_DOCS documents.
PHRASE_COMPLETENESS_PER_DOC_CHUNKS = 3
PHRASE_COMPLETENESS_MAX_DOCS = 5000
# How many chunk-phrase hits to verify per-chunk (snippet + true match
# counts). Documents matched only by the doc-level phrase audit beyond
# this horizon still appear (count-1 tail) but skip per-chunk
# verification — bounding Python work on phrases that hit every chunk.
PHRASE_CHUNK_RANK_LIMIT = 2000
# The Python span-fuzzy fallback (rapidfuzz over every chunk) is the one
# linear-in-archive-size stage left. Give it a wall-clock budget: past it,
# the search returns what it already collected (index passes + SQL phrase
# pass) instead of grinding for minutes on a huge archive.
SPAN_FUZZY_TIME_BUDGET_SEC = 3.0

# ---------- Indexer schema ----------
# v2: no-diacritic + trigram twins on every searchable field, so queries
# typed without Vietnamese diacritics and OCR slips are served by the
# index instead of the linear SQLite fallbacks.
# v3: DOCUMENT-level records (doc_meta_norm / doc_body_norm on a canonical
#     token stream, page sentinels between pages) so true phrase queries
#     retrieve every matching document from the index; the linear SQL
#     instr completeness scan becomes a fallback instead of the main path.
# Bump + change TANTIVY_SUBDIR when fields change.
INDEXER_VERSION        = "3"
TRIGRAM_TOKENIZER_NAME = "scanindex_trigram"
TRIGRAM_MIN_GRAM       = 3
TRIGRAM_MAX_GRAM       = 3
# Query-time weight scales for the derived fields (relative to the base
# TANTIVY_FIELD_WEIGHTS entry): a diacritic-stripped hit is worth slightly
# less than the exact one; a substring hit less still.
NODIAC_WEIGHT_SCALE    = 0.9
TRIGRAM_WEIGHT_SCALE   = 0.6

# ---------- Per-field weights for Tantivy hybrid scoring ----------
# Higher = more important. Chunker v2 splits chunks by type:
#   - metadata chunks: doc_number / signer_name / issue_org / subject /
#     recipients / metadata_text are populated. body_* are blank.
#   - body chunks: body_* are populated. metadata fields are blank.
# Boosting metadata fields above body lets exact-ish queries hit metadata first.
TANTIVY_FIELD_WEIGHTS = {
    "doc_number":          6.0,
    "signer_name":         4.0,
    "issue_org":           3.0,
    "subject":             3.0,
    "recipients":          2.0,
    "metadata_text":       2.5,
    "body_segmented":      1.2,
    "body_original":       1.0,
    "body_no_diacritic":   0.8,
}

# ---------- Chunker parameters (v2.1) ----------
# Goal: paragraph-level chunks, ~3â€“6 chunks/page. v2.0 produced ~25
# chunks/page on real documents because OCR-extracted PDF blocks split
# at every paragraph break (gap > 3Ã— line-height) AND very short
# blocks (<MIN words) emitted as standalone chunks.
# v2.1 tightens both: bigger Y tolerance to merge across paragraph
# spacing, plus aggressive "fold tiny chunk into previous" rescue.
CHUNK_MIN_BODY_WORDS    = 30            # below this â†’ fold into previous (or next) chunk
CHUNK_TARGET_BODY_WORDS = 100           # ~half page; grow paragraphs to here before emitting
CHUNK_MAX_BODY_WORDS    = 250           # hard cap; sentence-split above this
CHUNK_OVERLAP_WORDS     = 30            # overlap between split parts
CHUNK_MERGE_Y_RATIO     = 6.0           # merge across vertical gaps up to 6Ã— line-height (cross paragraph)
# Noise filter: tokens that, when seen alone in a body block, are dropped
# (case + diacritic-insensitive comparison). Metadata short tokens like
# "Máº­t" / "Kháº©n" / "Báº£n chÃ­nh" already live in the metadata chunk so
# losing them from body is safe.
CHUNK_NOISE_TOKENS = frozenset({
    "stt", "so", "trang", "ghi chu", "muc", "phu luc",
    "tieu de", "noi dung", "ten", "dia chi",
})
CHUNK_NOISE_MIN_WORDS   = 3             # body blocks with <3 words are noise unless metadata-shaped

# ---------- Maintenance ----------
MODEL_IDLE_UNLOAD_SEC   = 300           # unload heavy ONNX models after this idle window
