import threading
import unicodedata
import re
import csv
import os

import time
from scanindex.infra import paths as portable_utils
from scanindex.infra.paths import get_base_dir

_bt_tokenizer = None
_bt_translator = None
_pt_tokenizer = None
_pt_translator = None
_client_lock = threading.Lock()


def _load_ctranslate2():
    import ctranslate2

    return ctranslate2

MODEL_PROTON_CT2_OPT = 'protonx-models/protonx-legal-tc-[CTranslate2-OPTIMIZE]'
CURRENT_MODEL = MODEL_PROTON_CT2_OPT # Default

# Model Configuration (Token limits, Chunk sizes)
# Proton Use T5 architecture: 512 tokens max usually.
CONFIG = {
    "PROTON_OPT": {
        "CHUNK_LIMIT": 250,
        "MAX_LENGTH": 160,
        "BEAM_SIZE": 1        # Greedy decoding: ~3x faster, same accent-fix quality
    }
}


BASE_DIR = get_base_dir()
MODELS_DIR = os.path.join(BASE_DIR, "models")

# distilled-protonx-legal-tc CT2 int8: 8.3s/13 trang, accuracy 99.550%
# proton_ct2_opt (full):               14.5s/13 trang, accuracy 99.561%
# Dùng distilled: nhanh hơn 1.73x, mất đúng 1 lỗi/46 so với full.
PATH_PROTON_CT2_OPT = os.path.join(MODELS_DIR, "distilled_ct2")

# GPU Preference: "CPU" (Forced)
USE_GPU = "CPU"


def proton_ct2_available():
    """Return True only when the optional CT2 correction model is fully bundled."""
    required = (
        "config.json",
        "model.bin",
        "tokenizer.json",
        "tokenizer_config.json",
    )
    return all(os.path.isfile(os.path.join(PATH_PROTON_CT2_OPT, name)) for name in required)


def set_gpu_preference(preference, log_callback=print):
    """Set GPU preference: Ignored, always CPU."""
    log_callback("GPU usage is disabled. Using CPU.", "info")


def init_client(log_callback=print):
    global _bt_tokenizer, _bt_translator, _pt_tokenizer, _pt_translator

    device = "cpu"

    with _client_lock:

        if CURRENT_MODEL == MODEL_PROTON_CT2_OPT and (_pt_tokenizer is None or _pt_translator is None):
            if not proton_ct2_available():
                log_callback("Sửa chính tả Proton CT2 không có trong bản portable; bỏ qua.", "info")
                return False
            try:
                from transformers.models.t5.tokenization_t5_fast import T5TokenizerFast

                log_callback("Correction Engine using CPU.", "info")
                log_callback(f"Initializing CTranslate2 (Proton Legal TC OPT) from {PATH_PROTON_CT2_OPT}...", "info")
                _pt_tokenizer = T5TokenizerFast.from_pretrained(PATH_PROTON_CT2_OPT, local_files_only=True)
                ctranslate2 = _load_ctranslate2()
                # Proton CT2 OPT - CPU Mode
                cores = os.cpu_count() or 4
                inter = min(2, cores)
                intra = max(1, cores // inter)
                log_callback(f"  > CPU mode: Using inter_threads={inter}, intra_threads={intra} for speed.", "debug")
                _pt_translator = ctranslate2.Translator(PATH_PROTON_CT2_OPT, device=device, inter_threads=inter, intra_threads=intra)
                
                log_callback("Proton Legal TC CTranslate2 OPTIMIZED initialized.", "success")
                return True
            except Exception as e:
                log_callback(f"Failed to init Proton Legal TC CTranslate2 OPT: {e}", "err")
                return False
    return _pt_tokenizer is not None and _pt_translator is not None

def get_current_model_type():
    global _pt_translator
    if _pt_translator is None: return "none"
    ctranslate2 = _load_ctranslate2()
    if isinstance(_pt_translator, ctranslate2.Translator):
        if _pt_translator.device == "cuda": return "ct2_gpu"
        return "ct2_cpu"
    return "hf"


def set_model_name(model_name, log_callback=print):
    global CURRENT_MODEL
    # Update list to include OPT
    if model_name == MODEL_PROTON_CT2_OPT:
        if CURRENT_MODEL != model_name:
            CURRENT_MODEL = model_name
            log_callback(f"Correction Model set to: {CURRENT_MODEL}", "info")
            
            global _pt_tokenizer, _pt_translator
            # Force reload of Proton components
            _pt_tokenizer = None
            _pt_translator = None

            init_client(log_callback)
    else:
        log_callback(f"Warning: Unknown model name '{model_name}'. Using default.", "err")



IGNORED_WORDS_FILE = os.path.join(BASE_DIR, "ignored_words.txt")
_ignored_words_cache = set()
_ignored_phrase_cache = []
_ignored_words_loaded = False
_v8_resources_cache = None

DICTIONARIES_DIR = os.path.join(BASE_DIR, "dictionaries")
V8_LEXICON_FILE = os.path.join(DICTIONARIES_DIR, "party_frequency_lexicon_v8_no_person_names.txt")
V8_CORRECTION_MAP_FILE = os.path.join(DICTIONARIES_DIR, "v8_correction_map.tsv")
TRUSTED_DICT_FILE = os.path.join(DICTIONARIES_DIR, "trusted_party_terms.txt")
SURNAMES_FILE = os.path.join(DICTIONARIES_DIR, "person_surnames_vi_top200.txt")

TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
BOUNDARY_PUNCT_RE = re.compile(r"[,;:.!?-]+[\"')\]]*$")

STOP_BOUNDARY = {
    "cua", "va", "ve", "voi", "trong", "ngoai", "theo", "tai", "cho",
    "tu", "den", "de", "la", "co", "cac", "nhung", "mot", "so", "nam",
    "ngay", "thang", "duoc", "bi", "da", "se", "dang", "can", "phai",
    "neu", "khi", "thi", "ma", "nham", "do", "qua",
}
PERSON_CUE_BASES = {"dc", "ong", "ba"}
COMMON_MIDDLE_BASES = {
    "ai", "anh", "ba", "bao", "cam", "chi", "cong", "dang", "dinh", "duc",
    "duy", "gia", "hai", "hanh", "hieu", "hoa", "hoai", "hoang", "hong",
    "huu", "khanh", "kim", "lan", "le", "linh", "manh", "minh", "my",
    "ngoc", "nhat", "nhu", "phuc", "phuong", "quang", "quoc", "tan",
    "thanh", "thi", "thu", "thuy", "tien", "trong", "trung", "tuan",
    "van", "viet", "xuan",
}
ORG_FIRST_BASES = {
    "ban", "bo", "chi", "cong", "cuc", "dang", "hoi", "huyen", "khoi",
    "mat", "pho", "phong", "so", "thi", "tieu", "tinh", "to", "truong",
    "ubnd", "ubmttq", "uy", "xa",
}
ORG_PHRASE_BASES = {
    "ban chi dao", "ban chap hanh", "ban thuong vu", "ban to chuc",
    "bo chinh tri", "chi bo", "cong an", "doan kiem tra", "hoi dong",
    "mat tran", "tieu ban", "tinh uy", "trung uong", "uy ban",
}
TRUSTED_PUBLIC_PERSON_BASES = {"ho chi minh", "ho chi"}
ROLE_BASES = {"bi", "chu", "doc", "giam", "pho", "thu", "truong", "uy", "vien"}
ROLE_TITLE_PREFIX_BASES = {"tong bi", "tong bi thu", "pho bi", "pho bi thu"}
PLACE_PREFIX_BASES = {"ba ria", "vung tau", "dat do", "loc an", "phuoc hai", "long tan"}
VOWELS = set("aeiouy")

def load_ignored_words():
    global _ignored_words_cache, _ignored_phrase_cache, _ignored_words_loaded
    _ignored_words_cache = set()
    _ignored_phrase_cache = []
    _ignored_words_loaded = True
    if os.path.exists(IGNORED_WORDS_FILE):
        try:
            with open(IGNORED_WORDS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    word = line.strip()
                    if word:
                        phrase = tuple(_tokenize_text(word.lower()))
                        if len(phrase) > 1:
                            _ignored_phrase_cache.append(phrase)
                        else:
                            _ignored_words_cache.add(word.lower())
            print(f"Loaded {len(_ignored_words_cache)} ignored words and {len(_ignored_phrase_cache)} ignored phrases.")
        except Exception as e:
            print(f"Failed to load ignored words: {e}")


def _strip_accents(text):
    text = str(text or "").replace("\u0111", "d").replace("\u0110", "D")
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def _tokenize_text(text):
    return TOKEN_RE.findall((text or "").lower())


def _word_norm(text):
    return " ".join(_tokenize_text(text))


def _norm_phrase(text):
    return " ".join(_tokenize_text(text))


def _base_token(text):
    return re.sub(r"[^a-z0-9]+", "", _strip_accents(text).lower())


def _base_phrase(text):
    return " ".join(x for x in (_base_token(t) for t in _tokenize_text(text)) if x)


def _word_base_for_align(word):
    return "".join(_base_token(t) for t in _tokenize_text(word))


def _read_term_set(path):
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {_norm_phrase(line) for line in f if _norm_phrase(line)}


def _terms_by_base(terms):
    out = {}
    for term in terms:
        base = _base_phrase(term)
        if base:
            out.setdefault(base, set()).add(term)
    return out


def _load_v8_resources():
    global _v8_resources_cache
    if _v8_resources_cache is not None:
        return _v8_resources_cache

    lexicon = _read_term_set(V8_LEXICON_FILE)
    trusted = _read_term_set(TRUSTED_DICT_FILE)
    surnames = []
    if os.path.exists(SURNAMES_FILE):
        with open(SURNAMES_FILE, "r", encoding="utf-8") as f:
            surnames = [_norm_phrase(line) for line in f if _norm_phrase(line)]
    surname_bases = {_base_token(x) for x in surnames if _base_token(x)}
    strong_surname_bases = {_base_token(x) for x in surnames[:80] if _base_token(x)}

    cmap = {}
    if os.path.exists(V8_CORRECTION_MAP_FILE):
        try:
            with open(V8_CORRECTION_MAP_FILE, "r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f, delimiter="\t"):
                    target = _norm_phrase(row.get("target", ""))
                    base = row.get("base", "")
                    if not target or target not in lexicon or not base:
                        continue
                    cmap[base] = {
                        "target": target,
                        "df": int(float(row.get("df") or 0)),
                        "tf": int(float(row.get("tf") or 0)),
                        "dominance": float(row.get("dominance") or 0),
                    }
        except Exception as e:
            print(f"Failed to load V8 correction map: {e}")

    enabled = bool(lexicon and cmap)
    _v8_resources_cache = {
        "enabled": enabled,
        "lexicon": lexicon,
        "trusted": trusted,
        "trusted_by_base": _terms_by_base(trusted),
        "cmap": cmap,
        "surname_bases": surname_bases,
        "strong_surname_bases": strong_surname_bases,
    }
    if enabled:
        print(f"Loaded V8 correction gate: {len(lexicon)} terms, {len(cmap)} correction bases.")
    else:
        print("V8 correction gate unavailable; falling back to accent-only filtering.")
    return _v8_resources_cache


def _boundary_stop(base):
    parts = base.split()
    return bool(parts and (parts[0] in STOP_BOUNDARY or parts[-1] in STOP_BOUNDARY))


def _is_roman_base(base):
    return bool(re.fullmatch(r"[ivxlcdm]+", base or "")) and len(base) >= 2


def _acronym_like(raw):
    letters = "".join(ch for ch in raw or "" if ch.isalpha())
    if not letters:
        return False
    base = _base_token(letters)
    if not base:
        return False
    if _is_roman_base(base):
        return True
    if letters == letters.upper():
        if len(base) <= 2:
            return True
        if not any(ch in VOWELS for ch in base) and len(base) <= 8:
            return True
    return False


def _code_like(raw):
    if re.search(r"\d", raw or ""):
        return True
    if re.search(r"[/\\_.:;()\[\]{}<>@#%&*+=|~`]", raw or ""):
        return True
    return _acronym_like(raw)


def _candidate_allowed(cand):
    n = cand["n"]
    dom = cand["dominance"]
    df = cand["target_df"]
    base = cand["base"]
    if n == 2 and dom < 4.0:
        return False
    if n == 3 and dom < 3.0:
        return False
    if n >= 4 and dom < 3.0:
        return False
    if _boundary_stop(base):
        if n < 3:
            return False
        if dom < 8.0 or df < 80:
            return False
    return True


def _scan_v8_candidates(orig_words, resources):
    toks = []
    for idx, raw in enumerate(orig_words):
        parts = _tokenize_text(raw)
        if len(parts) != 1:
            continue
        toks.append({
            "word_idx": idx,
            "raw": raw,
            "token": parts[0],
            "base": _base_token(parts[0]),
        })

    out = []
    seen = set()
    lexicon = resources["lexicon"]
    cmap = resources["cmap"]
    L = len(toks)
    for n in range(min(5, L), 1, -1):
        for i in range(0, L - n + 1):
            window = toks[i:i + n]
            if window[-1]["word_idx"] - window[0]["word_idx"] != n - 1:
                continue
            if any(_code_like(x["raw"]) for x in window):
                continue
            phrase = " ".join(x["token"] for x in window)
            if phrase in lexicon:
                continue
            base = " ".join(x["base"] for x in window)
            item = cmap.get(base)
            if not item:
                continue
            target = item["target"]
            if target == phrase or len(target.split()) != n:
                continue
            cand = {
                "start_word": window[0]["word_idx"],
                "end_word": window[-1]["word_idx"] + 1,
                "source": phrase,
                "target": target,
                "source_tokens": phrase.split(),
                "target_tokens": target.split(),
                "base": base,
                "n": n,
                "dominance": item["dominance"],
                "target_df": item["df"],
                "target_tf": item["tf"],
            }
            key = (cand["start_word"], cand["end_word"], cand["target"])
            if key not in seen and _candidate_allowed(cand):
                out.append(cand)
                seen.add(key)
    return out


def _matching_candidate(candidates, idx, corr_norms):
    matches = []
    for cand in candidates:
        start = cand["start_word"]
        end = cand["end_word"]
        if not (start <= idx < end):
            continue
        if corr_norms[start:end] == cand["target_tokens"]:
            matches.append(cand)
    if not matches:
        return None
    matches.sort(key=lambda c: (c["n"], c["dominance"], c["target_df"]), reverse=True)
    return matches[0]


def _clean_word(word):
    return (word or "").strip(" \t\r\n\"'.,;:!?()[]{}<>")


def _terminal_boundary(word):
    return bool(BOUNDARY_PUNCT_RE.search(word or ""))


def _is_title_word(word):
    letters = "".join(ch for ch in _clean_word(word) if ch.isalpha())
    if len(letters) < 2:
        return False
    return letters[0].isupper() and not letters.isupper()


def _phrase_base(words):
    return " ".join(_base_token(w) for w in words if _base_token(w))


def _is_org_like_base(base):
    parts = base.split()
    if not parts:
        return False
    if parts[0] in ORG_FIRST_BASES:
        return True
    for n in range(2, min(4, len(parts)) + 1):
        for i in range(0, len(parts) - n + 1):
            if " ".join(parts[i:i + n]) in ORG_PHRASE_BASES:
                return True
    return False


def _contains_place_prefix_base(base):
    return any(base.startswith(x) for x in PLACE_PREFIX_BASES)


def _is_trusted_public_person_base(base):
    return any(base == x or base.startswith(x + " ") for x in TRUSTED_PUBLIC_PERSON_BASES)


def _cue_before(words, idx):
    prev = _base_token(words[idx - 1]) if idx > 0 else ""
    prev2 = _base_token(words[idx - 2]) if idx > 1 else ""
    if prev == "ba" and _base_token(words[idx]) in {"ria", "riaa"}:
        return False
    if prev in PERSON_CUE_BASES:
        return True
    return prev2 == "dong" and prev == "chi"


def _consume_title_run(words, start, max_len=5):
    end = start
    while end < len(words) and end < start + max_len and _is_title_word(words[end]):
        end += 1
        if _terminal_boundary(words[end - 1]):
            break
    return end


def _detect_person_spans(words, resources):
    spans = []
    surname_bases = resources["surname_bases"]
    strong_surname_bases = resources["strong_surname_bases"]
    for i in range(len(words)):
        if not _is_title_word(words[i]):
            continue
        end = _consume_title_run(words, i)
        if end - i < 2:
            continue
        base = _phrase_base(words[i:end])
        if _is_trusted_public_person_base(base) or _contains_place_prefix_base(base):
            continue
        if _cue_before(words, i):
            if (
                _base_token(words[i]) in ROLE_BASES
                or _is_org_like_base(base)
                or any(base.startswith(x) for x in ROLE_TITLE_PREFIX_BASES)
            ):
                continue
            spans.append((i, end))
            continue
        parts = base.split()
        if _base_token(words[i]) not in strong_surname_bases:
            continue
        if _is_org_like_base(base):
            continue
        if len(parts) >= 3 and parts[1] in COMMON_MIDDLE_BASES:
            spans.append((i, end))
    if not spans:
        return []
    spans.sort()
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _idx_in_person_span(words, idx, resources):
    for start, end in _detect_person_spans(words, resources):
        if start <= idx < end:
            return True
    return False


def _source_spans_for_candidate(cand, idx):
    if cand["start_word"] <= idx < cand["end_word"]:
        return [(cand["start_word"], cand["end_word"])]
    return []


def _target_phrase_is_trusted(cand, resources):
    target = cand["target"]
    return target in resources["trusted"] or (cand["n"] >= 4 and target in resources["lexicon"])


def _target_has_trusted_local_phrase(norms, idx, new_norm, resources):
    target_norms = list(norms)
    target_norms[idx] = new_norm
    trusted = resources["trusted"]
    for n in range(2, 5):
        left_min = max(0, idx - n + 1)
        left_max = min(idx, len(target_norms) - n)
        for s in range(left_min, left_max + 1):
            if " ".join(target_norms[s:s + n]) in trusted:
                return True
    return False


def _trusted_neighbor_competitor_guard(norms, idx, new_norm, resources):
    trusted_by_base = resources["trusted_by_base"]
    for n in range(2, 6):
        left_min = max(0, idx - n + 1)
        left_max = min(idx, len(norms) - n)
        for s in range(left_min, left_max + 1):
            source_tokens = norms[s:s + n]
            target_tokens = source_tokens.copy()
            target_tokens[idx - s] = new_norm
            source_phrase = " ".join(source_tokens)
            target_phrase = " ".join(target_tokens)
            trusted = trusted_by_base.get(_base_phrase(source_phrase))
            if trusted and source_phrase in trusted and target_phrase not in trusted:
                return True
    return False


def _trusted_overlapping_competitor_guard(norms, idx, new_norm, cand, resources):
    trusted_by_base = resources["trusted_by_base"]
    source_span = (cand["start_word"], cand["end_word"])
    for n in range(2, 5):
        left_min = max(0, idx - n + 1)
        left_max = min(idx, len(norms) - n)
        for s in range(left_min, left_max + 1):
            e = s + n
            if (s, e) == source_span:
                continue
            source_tokens = norms[s:e]
            target_tokens = source_tokens.copy()
            target_tokens[idx - s] = new_norm
            target_phrase = " ".join(target_tokens)
            trusted = trusted_by_base.get(_base_phrase(" ".join(source_tokens)))
            if not trusted or target_phrase in trusted:
                continue
            offset = idx - s
            for trusted_phrase in trusted:
                trusted_tokens = trusted_phrase.split()
                if len(trusted_tokens) != n:
                    continue
                if all(pos == offset or target_tokens[pos] == trusted_tokens[pos] for pos in range(n)):
                    return True
    return False


def _longer_source_phrase_guard(norms, idx, new_norm, cand, resources):
    lexicon = resources["lexicon"]
    for start, end in _source_spans_for_candidate(cand, idx):
        for left in range(0, 3):
            for right in range(0, 4):
                s = start - left
                e = end + right
                if s < 0 or e > len(norms) or e - s <= end - start or e - s > 5:
                    continue
                source_expanded = " ".join(norms[s:e])
                target_tokens = norms[s:e].copy()
                target_tokens[idx - s] = new_norm
                target_expanded = " ".join(target_tokens)
                if source_expanded != target_expanded and source_expanded in lexicon and target_expanded not in lexicon:
                    return True
    return False


def _v8_reject_change(orig_words, norms, idx, old_norm, new_norm, cand, resources):
    if _idx_in_person_span(orig_words, idx, resources):
        return True
    if _target_phrase_is_trusted(cand, resources):
        return False
    if _target_has_trusted_local_phrase(norms, idx, new_norm, resources):
        return False
    if _trusted_neighbor_competitor_guard(norms, idx, new_norm, resources):
        return True
    return _longer_source_phrase_guard(norms, idx, new_norm, cand, resources)


def _idx_in_ignored_phrase(orig_words, idx):
    if not _ignored_phrase_cache:
        return False
    norms = [_word_norm(w) for w in orig_words]
    for phrase in _ignored_phrase_cache:
        n = len(phrase)
        for s in range(max(0, idx - n + 1), min(idx, len(norms) - n) + 1):
            if tuple(norms[s:s + n]) == phrase:
                return True
    return False


def _has_v8_candidates(text):
    resources = _load_v8_resources()
    if not resources.get("enabled"):
        return True
    return bool(_scan_v8_candidates((text or "").split(), resources))

def remove_accents(input_str):
    s = str(input_str)
    # Manual handling for D/d stroke before NFD if we want consistency, 
    # but NFD doesn't split it. So replace manually.
    s = s.replace("Đ", "D").replace("đ", "d")
    s = unicodedata.normalize('NFD', s)
    s = "".join(c for c in s if unicodedata.category(c) != 'Mn')
    # Filter out spaces to compare base characters only (for separation check)
    return s.replace(" ", "").lower()

def align_and_filter(original, corrected):
    """
    Aligns original and corrected text.
    STRICT MODE: 
    1. Preserves exact whitespace from original.
    2. Only accepts correction if word count matches 1:1.
    3. V8 final: accept only locked candidate corrections, then apply
       ignored phrase, person-name, and context guards.
    """
    # 1. Tokenize preserving whitespace
    # 'Original   Text' -> ['Original', '   ', 'Text']
    orig_tokens = re.split(r'(\s+)', original)
    
    # Filter out empty strings from splitting (e.g. start/end)
    orig_tokens = [t for t in orig_tokens if t]
    
    # Get just the words for comparison
    orig_words = [t for t in orig_tokens if t.strip()]
    corr_words = corrected.split() # Standard split (we don't care about corrected's spacing)
    
    # 2. STRICT Count Check: If counts differ, REJECT whole line to prevent "CH UY E N"
    if len(orig_words) != len(corr_words):
        # print(f"DEBUG: Rejected correction due to word count mismatch: {len(orig_words)} vs {len(corr_words)}")
        # print(f"  Orig: '{original}'")
        # print(f"  Corr: '{corrected}'")
        return original
        
    global _ignored_words_cache, _ignored_words_loaded
    if not _ignored_words_loaded:
        load_ignored_words()
    resources = _load_v8_resources()
    v8_enabled = bool(resources.get("enabled"))
    candidates = _scan_v8_candidates(orig_words, resources) if v8_enabled else []
    orig_norms = [_word_norm(w) for w in orig_words]
    corr_norms = [_word_norm(w) for w in corr_words]

    final_tokens = []
    word_idx = 0
    
    for token in orig_tokens:
        if not token.strip():
            # It's whitespace, keep exact original
            final_tokens.append(token)
        else:
            # It's a word, align with corrected
            o_word = token
            c_word = corr_words[word_idx]
            word_idx += 1
            
            # Check if word is in ignored list (case-insensitive)
            if o_word.lower() in _ignored_words_cache or _idx_in_ignored_phrase(orig_words, word_idx - 1):
                final_tokens.append(o_word)
                continue

            # Check Base Match (Accent Only)
            base_o = _word_base_for_align(o_word)
            base_c = _word_base_for_align(c_word)
            
            if base_o != base_c or o_word == c_word:
                final_tokens.append(o_word)
                continue

            if v8_enabled:
                cand = _matching_candidate(candidates, word_idx - 1, corr_norms)
                if not cand:
                    final_tokens.append(o_word)
                    continue
                if _v8_reject_change(
                    orig_words,
                    orig_norms,
                    word_idx - 1,
                    _word_norm(o_word),
                    _word_norm(c_word),
                    cand,
                    resources,
                ):
                    final_tokens.append(o_word)
                    continue

            final_tokens.append(c_word)
                
    return "".join(final_tokens)

def _prepare_lines(text, line_limit=800):
    if not text: return []
    raw_lines = text.split('\n')
    lines = []
    for rl in raw_lines:
        if len(rl) > line_limit:
            # Split by sentence to stay within model token limits
            sublines = re.split(r'([.?!])\s+', rl)
            temp = ""
            for s in sublines:
                if s in ".?!":
                    temp += s
                    lines.append(temp)
                    temp = ""
                else:
                    temp = s
            if temp: lines.append(temp)
        else:
            lines.append(rl)
    return lines

def correct_text(text):
    """
    Corrects the provided text, enforcing accent-only changes.
    """
    if not text or not text.strip():
        return text

    if not any(_has_v8_candidates(line) for line in text.split("\n") if line.strip()):
        return text

    init_client()
    

    if CURRENT_MODEL == MODEL_PROTON_CT2_OPT:
         if _pt_translator is None: return text
         
         # DEBUG TRACE
         lines = text.split('\n')
         # print(f"[DEBUG Engine] Input Lines: {len(lines)}")
         
         return _correct_text_proton_ct2(text)
         
    else: 
        return text


def _chunk_by_tokens(text, tokenizer, max_tokens=150):
    """
    Split text into chunks that fit within max_tokens limit.
    Splits at word boundaries to preserve meaning.
    Uses max_tokens=150 to leave room for special tokens.
    """
    words = text.split()
    if not words:
        return [text]
    
    chunks = []
    current_chunk_words = []
    current_token_count = 0
    
    for word in words:
        # Count tokens for this word (including space)
        word_tokens = len(tokenizer.encode(word, add_special_tokens=False))
        
        if current_token_count + word_tokens > max_tokens and current_chunk_words:
            # Save current chunk and start new one
            chunks.append(' '.join(current_chunk_words))
            current_chunk_words = [word]
            current_token_count = word_tokens
        else:
            current_chunk_words.append(word)
            current_token_count += word_tokens
    
    # Don't forget the last chunk
    if current_chunk_words:
        chunks.append(' '.join(current_chunk_words))
    
    return chunks if chunks else [text]


def _correct_text_proton_ct2(text):
    # Nano/Distilled Proton CT2
    cfg = CONFIG["PROTON_OPT"]
        
    MAX_LENGTH = cfg["MAX_LENGTH"]
    BEAM = cfg["BEAM_SIZE"]
    MAX_INPUT_TOKENS = 150  # Leave room for special tokens within 160 limit
    
    # Split lines and preserve empty ones for structure
    original_lines = text.split('\n')
    final_corrected_lines = [None] * len(original_lines)
    
    # Prepare chunks: each item is (line_idx, chunk_idx, chunk_text, original_chunk_text)
    all_chunks = []
    line_chunk_counts = {}  # line_idx -> number of chunks
    
    for line_idx, line in enumerate(original_lines):
        if not line.strip():
            # Empty line, keep as is
            final_corrected_lines[line_idx] = line
            continue

        if not _has_v8_candidates(line):
            final_corrected_lines[line_idx] = line
            continue
        
        # Check token count
        tokens = _pt_tokenizer.encode(line, add_special_tokens=False)
        
        if len(tokens) <= MAX_INPUT_TOKENS:
            # Line fits, single chunk
            all_chunks.append((line_idx, 0, line, line))
            line_chunk_counts[line_idx] = 1
        else:
            # Line too long, need to split into chunks
            chunks = _chunk_by_tokens(line, _pt_tokenizer, MAX_INPUT_TOKENS)
            line_chunk_counts[line_idx] = len(chunks)
            for chunk_idx, chunk in enumerate(chunks):
                all_chunks.append((line_idx, chunk_idx, chunk, chunk))
    
    if not all_chunks:
        return text

    try:
        # Batch processing — larger batch amortizes overhead, beam=1 no score needed
        BATCH_SIZE = 96
        corrected_chunks = {}  # (line_idx, chunk_idx) -> corrected_text

        for i in range(0, len(all_chunks), BATCH_SIZE):
            batch = all_chunks[i : i + BATCH_SIZE]

            # Tokenize batch
            source_tokens = [
                _pt_tokenizer.convert_ids_to_tokens(_pt_tokenizer.encode(item[2]))
                for item in batch
            ]

            # Translate batch (beam=1 greedy: no score needed, ~3x faster than beam=4)
            results = _pt_translator.translate_batch(
                source_tokens,
                max_decoding_length=MAX_LENGTH,
                beam_size=BEAM,
            )

            # Decode and store results
            for j, res in enumerate(results):
                line_idx, chunk_idx, _, original_chunk = batch[j]

                decoded = _pt_tokenizer.decode(
                    _pt_tokenizer.convert_tokens_to_ids(res.hypotheses[0])
                )

                # Apply Strict Filter (Accent Only) per chunk
                corrected = align_and_filter(original_chunk, decoded)
                corrected_chunks[(line_idx, chunk_idx)] = corrected
        
        # Reassemble lines from chunks
        for line_idx in line_chunk_counts:
            num_chunks = line_chunk_counts[line_idx]
            if num_chunks == 1:
                # Single chunk, direct assignment
                final_corrected_lines[line_idx] = corrected_chunks.get((line_idx, 0), original_lines[line_idx])
            else:
                # Multiple chunks, join with space
                reassembled = ' '.join(
                    corrected_chunks.get((line_idx, c_idx), '')
                    for c_idx in range(num_chunks)
                )
                final_corrected_lines[line_idx] = reassembled
        
        # Fill any remaining None values
        for i in range(len(final_corrected_lines)):
            if final_corrected_lines[i] is None:
                final_corrected_lines[i] = original_lines[i]
        
        return '\n'.join(final_corrected_lines)

    except Exception as e:
        print(f"Proton CT2 error: {e}")
        import traceback
        traceback.print_exc()
        return text





