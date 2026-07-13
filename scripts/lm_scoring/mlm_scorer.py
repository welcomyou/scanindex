"""Masked-Language-Model scoring for Vietnamese error detection.

PhoBERT and BamiBERT are MLM (bidirectional) models. We score each word by
replacing it with the model's <mask> token and reading off the probability the
model assigns to the *real* word in context:

    surprisal = -log P(word | context_left, context_right)

A high surprisal = the model finds the real word unexpected = candidate error.

For PhoBERT the tokenizer is subword (BPE) and the model uses @@-joined subwords
to represent a word, so we mask the *whole* word at once using the tokenizer's
encoding of that word.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Tuple

import torch
from transformers import AutoModelForMaskedLM, AutoTokenizer


@dataclass
class TokenScore:
    word: str          # surface form in the source text
    surprisal: float   # -log P(word | context); higher = more suspect
    log_prob: float    # raw log-prob (may be -inf if uncomputable)
    rank: int          # rank of the word among model's top guesses (1 = best)
    top_k: List[Tuple[str, float]]  # model's top-k alternative words


def _encode_word(tokenizer, word: str) -> List[int]:
    """Encode a single word into subword ids *without* special tokens.

    PhoBERT needs '@@' suffixes between subwords so they glue back together.
    `convert_tokens_to_ids` on raw tokens gives the right ids; we go through
    `encode(' ' + word, add_special_tokens=False)` as a robust general method,
    but for PhoBERT that inserts a leading Ġ-like artifact, so instead we use
    `tokenizer.tokenize(word)` which yields subwords with @@ markers, then map
    to ids.
    """
    sub_tokens = tokenizer.tokenize(word)
    if not sub_tokens:
        return []
    return tokenizer.convert_tokens_to_ids(sub_tokens)


def score_text(
    model_name: str,
    words: List[str],
    top_k: int = 8,
    device: str = "cpu",
) -> List[TokenScore]:
    """Score every word in `words` with the MLM.

    Returns one TokenScore per input word, in the same order.
    """
    # PhoBERT only ships a slow tokenizer (sentencepiece-based, with @@ glue);
    # XLM-R-style models (BamiBERT) need the fast tokenizer. AutoTokenizer
    # picks correctly when we don't force use_fast, so we try fast first then
    # fall back to slow for PhoBERT.
    try:
        tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=True)
    except Exception:
        tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=False)
    model = AutoModelForMaskedLM.from_pretrained(model_name).to(device)
    model.eval()
    mask_id = tokenizer.mask_token_id
    assert mask_id is not None, f"{model_name} has no <mask> token"

    # Pre-encode every word into subword ids. If a word contains OOV chars the
    # encoding may be empty -> we skip scoring (mark as neutral).
    word_ids: List[List[int]] = []
    for w in words:
        ids = _encode_word(tokenizer, w)
        word_ids.append(ids)

    # Pre-tokenize the context (ids, no specials) so we can splice the mask in.
    # For PhoBERT the model was trained on space-joined underthesea tokens; we
    # reconstruct the raw string with single spaces and let the tokenizer split.
    scores: List[TokenScore] = []
    for idx, (word, sub_ids) in enumerate(zip(words, word_ids)):
        if not sub_ids:
            # cannot tokenize -> neutral
            scores.append(TokenScore(word, math.nan, math.nan, -1, []))
            continue

        # Build the masked sentence: same words, but the current word replaced
        # by one <mask> token. (Collapsing a multi-subword word to a single mask
        # is the standard PhoBERT-scoring recipe; see "pseudo-PPL" literature.)
        masked_words = list(words)
        masked_words[idx] = tokenizer.mask_token
        text = " ".join(masked_words)
        enc = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
        input_ids = enc["input_ids"][0]
        # find the mask position(s)
        mask_positions = (input_ids == mask_id).nonzero(as_tuple=False).flatten().tolist()
        if len(mask_positions) != 1:
            # fallback: if multiple masks somehow appear, take the first
            if not mask_positions:
                scores.append(TokenScore(word, math.nan, math.nan, -1, []))
                continue

        with torch.no_grad():
            logits = model(**{k: v.to(device) for k, v in enc.items()}).logits[0]
        probs_at_mask = torch.softmax(logits[mask_positions[0]], dim=-1)

        # Word log-prob = sum of subword log-probs computed greedily is the
        # proper MLM formulation, but that requires re-masking each subword.
        # For a fast single-pass score we approximate P(word) as the product of
        # subword probs at the same position weighted by their share. In
        # practice for Vietnamese BERT-style the first-subword prob is the most
        # discriminative signal; we use the geometric mean over subwords as a
        # stable proxy when len(sub_ids) > 1.
        sub_probs = probs_at_mask[sub_ids].tolist()
        if any(p <= 0 for p in sub_probs):
            log_prob = -float("inf")
            surprisal = float("inf")
        else:
            log_prob = sum(math.log(p) for p in sub_probs) / len(sub_ids)
            surprisal = -log_prob

        # rank of the word's top subword in the model's distribution
        ranked = torch.argsort(probs_at_mask, descending=True)
        # find rank of sub_ids[0]
        top_id = int(sub_ids[0])
        rank = int((ranked == top_id).nonzero(as_tuple=True)[0][0]) + 1

        topk_vals, topk_idx = torch.topk(probs_at_mask, k=top_k)
        top_k_list = [
            (tokenizer.convert_ids_to_tokens(int(i)), float(p))
            for i, p in zip(topk_idx.tolist(), topk_vals.tolist())
        ]

        scores.append(TokenScore(word, surprisal, log_prob, rank, top_k_list))

    return scores
