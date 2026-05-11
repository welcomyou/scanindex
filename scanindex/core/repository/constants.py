"""Constants for Kho lÆ°u trá»¯ (Archive store).

Version strings here are checked against `index_meta` table on startup;
mismatch triggers a rebuild prompt. Bump when behavior changes
in a way that invalidates the on-disk index.
"""

# ---------- Versions (bump to trigger rebuild) ----------
SCHEMA_VERSION       = "8"      # v8: lexical-only repository; vector index removed
CHUNKER_VERSION      = "2.1"    # tighter merge: TARGET=100 / MAX=250 / Y_RATIO=6 + tiny-chunk rescue
TOKENIZER_NAME       = "underthesea"
TOKENIZER_VERSION    = "1.x"           # filled at runtime if available

# ---------- Default folder layout ----------
DEFAULT_REPOSITORY_DIRNAME = "repository"   # relative to app base dir
DEFAULT_ARCHIVE_DIRNAME    = DEFAULT_REPOSITORY_DIRNAME
PDF_SUBDIR              = "pdf"
TANTIVY_SUBDIR          = "tantivy_index"
SQLITE_FILE             = "repository.db"
IMPORT_LOG_FILE         = "import_log.json"

# ---------- Search parameters ----------
TANTIVY_TOP_K          = 100
MIN_RESULTS            = 5              # below this -> fallback

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
