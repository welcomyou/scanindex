"""Error DETECTION v2 — ensemble PhoBERT v2 + BamiBERT, clean token display.

Changes from v1:
  * Ensemble: both models must agree on a GAP/SWAP flag, OR one model very
    strongly fires (top-prob >= 0.5). This trades a little recall for much
    higher precision, which is what "locate accurately" needs.
  * Tokens are decoded with convert_tokens_to_string(), so BamiBERT's
    XLM-R byte-level subwords render as proper Vietnamese (no mojibake).
  * Dropped the TOKEN probe entirely — on the test paragraph it fired on
    almost every token (PhoBERT is out-of-domain on Party admin text), so it
    is useless for localization. Keep GAP + SWAP which both localized real
    errors well.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

import torch
from transformers import AutoModelForMaskedLM, AutoTokenizer

MODELS = ["vinai/phobert-base-v2", "Qualcomm-AI-Research/BamiBERT"]


@dataclass
class Flag:
    kind: str            # "GAP" | "SWAP"
    where: str           # human-readable location
    evidence: str        # why we flagged it
    snippet: str         # the suspect span
    votes: List[str] = field(default_factory=list)  # which models agreed


# ---------- tokenizer/model helpers ----------

def _load(model_name: str):
    # Let HF pick fast vs slow automatically; both models load fine this way.
    tok = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForMaskedLM.from_pretrained(model_name).eval()
    return tok, model


def _ids_for_word(tok, w: str) -> List[int]:
    """Subword ids for a word, no special tokens."""
    enc = tok(w, add_special_tokens=False).input_ids
    return enc if isinstance(enc, list) else list(enc)


def _decode_ids(tok, ids: List[int]) -> str:
    """Render subword ids back to clean surface text (handles BamiBERT bytes)."""
    return tok.convert_tokens_to_string(tok.convert_ids_to_tokens(ids)).strip()


def _mask_score(tok, model, words_with_one_mask) -> torch.Tensor:
    text = " ".join(words_with_one_mask)
    enc = tok(text, return_tensors="pt", truncation=True, max_length=512)
    mask_pos = (enc["input_ids"][0] == tok.mask_token_id).nonzero()[0].item()
    with torch.no_grad():
        logits = model(**enc).logits[0, mask_pos]
    return torch.softmax(logits, dim=-1)


# Function words that are NOT "missing content word" at a gap.
_FUNC = {
    ",", ".", ";", ":", "-", "“", "”", '"', "(", ")",
    "và", "hoặc", "của", "cho", "trong", "với", "từ", "là", "được", "các",
    "những", "một", "để", "về", "theo", "tại", "khi", "này", "đó", "cũng",
    "đã", "sẽ", "vẫn", "có", "không", "vừa", "còn", "như", "mà", "bởi",
}


def _is_content(tok, top_id: int) -> bool:
    surface = _decode_ids(tok, [top_id]).lower()
    # strip leading punctuation/spaces
    surface = "".join(c for c in surface if c.isalnum() or c.isspace()).strip()
    if not surface or len(surface) <= 1:
        return False
    return surface.split()[0] not in _FUNC


def _gap_probe_one(tok, model, words, i):
    """Top-1 (token id, prob, decoded string) at the gap between i and i+1."""
    left, right = words[i], words[i + 1]
    if not left.strip() or not right.strip():
        return None
    masked = words[:i + 1] + [tok.mask_token] + words[i + 1:]
    probs = _mask_score(tok, model, masked)
    top_id = int(probs.argmax().item())
    top_prob = float(probs[top_id].item())
    return top_id, top_prob, _decode_ids(tok, [top_id])


def _swap_probe_one(tok, model, words, i):
    """Return (p_orig, p_swap): P(b|a...) vs P(a|b...) for adjacent (a,b)."""
    a, b = words[i], words[i + 1]
    if not a.strip() or not b.strip():
        return None
    if any(c in a for c in ",.;:()–-") or any(c in b for c in ",.;:()–-"):
        return None
    b_ids = _ids_for_word(tok, b)
    a_ids = _ids_for_word(tok, a)
    if not b_ids or not a_ids:
        return None
    ctx_orig = words[:i] + [a, tok.mask_token] + words[i + 2:]
    ctx_swap = words[:i] + [b, tok.mask_token] + words[i + 2:]
    po = _mask_score(tok, model, ctx_orig)
    ps = _mask_score(tok, model, ctx_swap)
    return float(po[b_ids[0]].item()), float(ps[a_ids[0]].item())


# ---------- main ensemble detector ----------

def detect(words: List[str], models: List[str] = MODELS) -> List[Flag]:
    loaded = {m: _load(m) for m in models}
    n = len(words)
    flags: List[Flag] = []

    # ----- GAP: flag a gap if BOTH models want a content word there, or
    #       one model very strongly (>=0.5). Evidence = union of guesses.
    for i in range(n - 1):
        per_model = {}
        for m in models:
            r = _gap_probe_one(*loaded[m], words, i)
            if r:
                per_model[m] = r
        if len(per_model) < len(models):
            # need at least data to decide; skip if any model couldn't score
            continue
        # agreement check
        strong = [(m, r) for m, r in per_model.items() if r[1] >= 0.5 and _is_content(loaded[m][0], r[0])]
        both_content = all(_is_content(loaded[m][0], r[0]) and r[1] >= 0.2 for m, r in per_model.items())
        if not strong and not both_content:
            continue
        left, right = words[i], words[i + 1]
        guesses = ", ".join(f"{m.split('/')[-1]}:'{r[2]}'(P={r[1]:.2f})" for m, r in per_model.items())
        flags.append(Flag(
            "GAP",
            f"giữa '{left}' và '{right}'",
            f"thiếu từ? {guesses}",
            snippet=f"{left} [?] {right}",
            votes=[m for m, r in strong],
        ))

    # ----- SWAP: flag pair if BOTH models agree swap helps, or one strongly.
    for i in range(n - 2):
        per_model = {}
        for m in models:
            r = _swap_probe_one(*loaded[m], words, i)
            if r:
                per_model[m] = r
        if len(per_model) < len(models):
            continue
        # each model's verdict: swap-better if p_swap > 5x p_orig and p_orig < 0.05
        verdicts = {}
        for m, (po, ps) in per_model.items():
            verdicts[m] = (ps > 0.2 and ps > po * 5 and po < 0.05, po, ps)
        agree_count = sum(1 for v in verdicts.values() if v[0])
        strong = any(v[1] < 0.01 and v[2] > 0.3 for v in verdicts.values())
        if agree_count < len(models) and not strong:
            continue
        a, b = words[i], words[i + 1]
        ev = "; ".join(
            f"{m.split('/')[-1]}: P({b}|{a})={v[1]:.3f} << P({a}|{b})={v[2]:.3f}"
            for m, v in verdicts.items()
        )
        flags.append(Flag(
            "SWAP",
            f"đảo cặp '{a} {b}'",
            ev,
            snippet=f"{a} {b}",
            votes=[m for m, v in verdicts.items() if v[0]],
        ))

    # free memory
    del loaded
    torch.cuda.empty_cache() if torch.cuda.is_available() else None
    return flags


def filter_with_whitelist(flags: List[Flag], wl) -> tuple[List[Flag], List[Flag]]:
    """Split flags into (kept, suppressed).

    Suppress ONLY when the flagged span, as it actually appears in the text,
    is a trusted collocation. We deliberately do NOT suppress based on the
    model's guess: if the model wants to insert a word, that guess being a
    known term is evidence OF an error, not against it.

    GAP  : suppress if "left right" (the two words adjacent in the source,
           ignoring the hypothesized gap) is a trusted term — e.g. "cấp ủy".
    SWAP : suppress if the pair as-written is already a trusted term — e.g.
           "kính mời". We do NOT suppress just because the swapped form is
           trusted; that's the whole point of the flag.
    """
    kept, suppressed = [], []
    for f in flags:
        suppress = False
        if f.kind == "GAP":
            parts = f.snippet.split("[?]")
            left = parts[0].strip() if parts else ""
            right = parts[1].strip() if len(parts) > 1 else ""
            if wl.covers(f"{left} {right}"):
                suppress = True
        elif f.kind == "SWAP":
            pair = f.snippet.strip()
            if wl.covers(pair):
                suppress = True
        (suppressed if suppress else kept).append(f)
    return kept, suppressed


def report(words: List[str], flags: List[Flag], suppressed: Optional[List[Flag]] = None) -> None:
    if not flags:
        print("No errors flagged.")
        return
    print(f"Detected {len(flags)} flag(s):\n")
    for f in flags:
        votes = ",".join(m.split("/")[-1] for m in f.votes) or "ensemble"
        print(f"  [{f.kind:4}] ({votes}) {f.where}")
        print(f"          snippet : ...{f.snippet}...")
        print(f"          evidence: {f.evidence}")
        print()


if __name__ == "__main__":
    from underthesea import word_tokenize

    TEXT = """Thực hiện ý kiến chỉ đạo của Thường trực Thành ủy về đẩy mạnh công tác chuyển đổi số, đổi mới phương thức làm việc trên môi trường số, đặc biệt là thay đổi chế độ thông tin, báo cáo, từ báo cáo giấy sang báo cáo dữ liệu theo thời gian thực, đảm bảo "đúng, đủ, sạch sống, thống nhất, dùng chung", giảm áp lực cáo cho cấp dưới, phục vụ xây dựng Hệ thống thông lãnh đạo, chỉ đạo của cấp ủy, cơ quan đảng, Văn phòng Thành ủy kính mời các đồng chí tham dự họp cuộc, cụ thể:
- Hiện trạng triển khai tổng hợp dữ trên Hệ thống quản trị thực thi của Thành phố.
- Chia sẻ liệu dữ cho Văn phòng Thành ủy."""

    words = word_tokenize(TEXT)
    print(f"Tokenized into {len(words)} words.\n")
    raw_flags = detect(words)
    # apply whitelist layer
    from whitelist import Whitelist, DEFAULT_FILES
    wl = Whitelist.from_files(*DEFAULT_FILES)
    print(f"Whitelist: {len(wl)} trusted terms loaded.")
    kept, suppressed = filter_with_whitelist(raw_flags, wl)
    print(f"Suppressed {len(suppressed)} FP by whitelist; {len(kept)} flag(s) remain.\n")
    report(words, kept, suppressed)
    if suppressed:
        print("\n--- suppressed (whitelist) ---")
        for f in suppressed:
            print(f"  [{f.kind:4}] {f.where}  ({f.snippet})")
