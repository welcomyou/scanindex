"""Error DETECTION (localization only, no correction).

The grammar errors in the target document live at three different levels, so a
single signal can't find them all accurately. We use three targeted probes,
each matched to one error type:

  A. TOKEN  — single token is anomalous in context  -> catches merged words
             ("sach song" = two words glued). Probe: MLM surprisal at token.

  B. GAP    — a word is MISSING between two adjacent words. Probe: insert a
             <mask> at each gap; if the model strongly wants a *content* word
             there, flag the gap.

  C. SWAP   — two adjacent words are in the wrong order. Probe: compare the
             MLM score of the bigram (a,b) vs the swapped (b,a) in context;
             if swapping clearly improves it, flag the pair.

Each probe is precision-oriented: it only fires when the evidence is strong,
because the goal is to LOCATE errors accurately (few false positives), not to
recall every possible issue.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List

import torch
from transformers import AutoModelForMaskedLM, AutoTokenizer


@dataclass
class Flag:
    kind: str          # "TOKEN" | "GAP" | "SWAP"
    where: str         # human-readable location, e.g. "giữa 'tổng hợp' và 'trên'"
    evidence: str      # why we flagged it
    snippet: str       # the text span we are confident is wrong


def _load(model_name: str):
    tok = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForMaskedLM.from_pretrained(model_name).eval()
    return tok, model


def _encode_word(tok, w: str) -> List[int]:
    subs = tok.tokenize(w)
    return tok.convert_tokens_to_ids(subs) if subs else []


def _mask_score(tok, model, words_with_mask_at: int) -> torch.Tensor:
    """Return softmax distribution at the single <mask> position."""
    text = " ".join(words_with_mask_at)
    enc = tok(text, return_tensors="pt", truncation=True, max_length=512)
    mask_pos = (enc["input_ids"][0] == tok.mask_token_id).nonzero()[0].item()
    with torch.no_grad():
        logits = model(**enc).logits[0, mask_pos]
    return torch.softmax(logits, dim=-1)


def _word_prob_at_mask(tok, model, words: list[str], idx: int) -> float:
    """P(real word at idx | context) via single-mask MLM."""
    w = words[idx]
    sub_ids = _encode_word(tok, w)
    if not sub_ids:
        return float("nan")
    masked = list(words)
    masked[idx] = tok.mask_token
    probs = _mask_score(tok, model, masked)
    # geometric mean over subwords (proxy; first subword dominates)
    ps = probs[sub_ids].tolist()
    if any(p <= 0 for p in ps):
        return 0.0
    return math.exp(sum(math.log(p) for p in ps) / len(ps))


# Vietnamese function words / punctuation that naturally appear at gaps — we do
# NOT treat strong predictions of these as "missing content word".
_FUNCTION_TOKENS = {
    ",", ".", ";", ":", "-", "và", "hoặc", "của", "cho", "trong", "với", "từ",
    "là", "được", "các", "những", "một", "để", "về", "theo", "tại", "khi",
    "này", "đó", "cũng", "đã", "sẽ", "vẫn", "có", "không",
}


def detect(model_name: str, words: List[str]) -> List[Flag]:
    tok, model = _load(model_name)
    n = len(words)
    flags: List[Flag] = []

    # ---------- A. TOKEN probe ----------
    # Flag a token if (1) its MLM prob is low AND (2) the model's own top-1 is
    # NOT the token (i.e. model disagrees it belongs here).
    for i, w in enumerate(words):
        if not w.strip() or all(c in ",.;:-–()\"'*" for c in w):
            continue
        p = _word_prob_at_mask(tok, model, words, i)
        if math.isnan(p) or p > 0.05:
            continue
        masked = list(words); masked[i] = tok.mask_token
        probs = _mask_score(tok, model, masked)
        top_id = int(probs.argmax().item())
        my_ids = set(_encode_word(tok, w))
        if top_id in my_ids:
            continue  # model actually agrees -> not anomalous
        flags.append(Flag(
            "TOKEN", f"#{i} '{w}'",
            f"P('{w}'|ctx)={p:.3f} thấp, model lại đoá là "
            f"'{tok.convert_ids_to_tokens([top_id])[0]}'",
            snippet=w,
        ))

    # ---------- B. GAP probe ----------
    # Insert a <mask> between words[i] and words[i+1]. If the model's top-1 is
    # a *content* word with high prob, a word is likely missing there.
    for i in range(n - 1):
        left, right = words[i], words[i + 1]
        if not left.strip() or not right.strip():
            continue
        masked = words[:i + 1] + [tok.mask_token] + words[i + 1:]
        probs = _mask_score(tok, model, masked)
        top_id = int(probs.argmax().item())
        top_token = tok.convert_ids_to_tokens([top_id])[0]
        top_prob = float(probs[top_id].item())
        # clean token surface for the function-word check
        surface = top_token.replace("@@", "").replace("Ġ", "").lower()
        if top_prob < 0.25:
            continue
        if surface in _FUNCTION_TOKENS or len(surface) <= 1:
            continue
        flags.append(Flag(
            "GAP", f"giữa '{left}' và '{right}'",
            f"model muốn chèn '{top_token}' (P={top_prob:.2f})",
            snippet=f"{left} [?] {right}",
        ))

    # ---------- C. SWAP probe ----------
    # For adjacent (a,b), compare P(b | a-prefix) vs P(a | b-prefix) by scoring
    # the bigram in context both ways. If swap clearly helps, flag.
    for i in range(n - 2):
        a, b = words[i], words[i + 1]
        if not a.strip() or not b.strip():
            continue
        if any(c in a for c in ",.;:()-") or any(c in b for c in ",.;:()-"):
            continue
        # original order: ... a [mask=b] c ...
        # swapped order: ... b [mask=a] c ...
        ctx_orig = words[:i] + [a, tok.mask_token] + words[i + 2:]
        ctx_swap = words[:i] + [b, tok.mask_token] + words[i + 2:]
        po = _mask_score(tok, model, ctx_orig)
        ps = _mask_score(tok, model, ctx_swap)
        b_ids = _encode_word(tok, b)
        a_ids = _encode_word(tok, a)
        if not b_ids or not a_ids:
            continue
        p_orig = float(po[b_ids[0]].item())   # P(b | a in place)
        p_swap = float(ps[a_ids[0]].item())   # P(a | b in place)
        # flag if swapping makes the second word MUCH more expected
        if p_swap > 0.2 and p_swap > p_orig * 5 and p_orig < 0.05:
            flags.append(Flag(
                "SWAP", f"đảo cặp '{a} {b}' (#{i}-{i+1})",
                f"P('{b}' sau '{a}')={p_orig:.3f} << P('{a}' sau '{b}')={p_swap:.3f}",
                snippet=f"{a} {b}",
            ))

    return flags


def report(words: List[str], flags: List[Flag]) -> None:
    print(f"Detected {len(flags)} flag(s):\n")
    # group by position for readability
    for f in flags:
        print(f"  [{f.kind:5}] {f.where}")
        print(f"          snippet : ...{f.snippet}...")
        print(f"          evidence: {f.evidence}")
        print()


# ---- run on the user's text ----
if __name__ == "__main__":
    from underthesea import word_tokenize

    TEXT = """Thực hiện ý kiến chỉ đạo của Thường trực Thành ủy về đẩy mạnh công tác chuyển đổi số, đổi mới phương thức làm việc trên môi trường số, đặc biệt là thay đổi chế độ thông tin, báo cáo, từ báo cáo giấy sang báo cáo dữ liệu theo thời gian thực, đảm bảo "đúng, đủ, sạch sống, thống nhất, dùng chung", giảm áp lực cáo cho cấp dưới, phục vụ xây dựng Hệ thống thông lãnh đạo, chỉ đạo của cấp ủy, cơ quan đảng, Văn phòng Thành ủy kính mời các đồng chí tham dự họp cuộc, cụ thể:
- Hiện trạng triển khai tổng hợp dữ trên Hệ thống quản trị thực thi của Thành phố.
- Chia sẻ liệu dữ cho Văn phòng Thành ủy."""

    words = word_tokenize(TEXT)
    print(f"Tokenized: {len(words)} words\n")
    flags = detect("vinai/phobert-base-v2", words)
    report(words, flags)
