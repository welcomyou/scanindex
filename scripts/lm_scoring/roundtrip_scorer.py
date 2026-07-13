"""Round-trip translation scoring (Vi -> En -> Vi) via Google Translate.

Unlike an MLM, an NMT model cannot give us P(word | context) for the *source*
language. The only practical signal is: translate to En then back to Vi, and
measure how much the round-trip diverges from the original. Words that change a
lot are flagged as candidates for OCR/grammar errors.

We measure divergence per-word by Levenshtein-style alignment between the
original and round-tripped text at the *word* level.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List

_LEVENSHTEIN_OPS = ("keep", "replace", "delete", "insert")


@dataclass
class RoundTripWordScore:
    word: str
    status: str   # "keep" | "replace" | "delete" | "insert-source"
    note: str = ""


def _translate(text: str, sl: str, tl: str, timeout: int = 10) -> str:
    """Free Google translate endpoint. Returns translated plain text."""
    url = "https://translate.googleapis.com/translate_a/single"
    params = urllib.parse.urlencode(
        {"client": "gtx", "sl": sl, "tl": tl, "dt": "t", "q": text}
    )
    full = f"{url}?{params}"
    req = urllib.request.Request(full, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        import json
        data = json.load(resp)
    # data[0] is a list of [translatedChunk, originalChunk, ...]
    return "".join(seg[0] for seg in data[0] if seg[0])


def roundtrip(text_vi: str, retries: int = 2) -> str:
    last_err = None
    for _ in range(retries + 1):
        try:
            en = _translate(text_vi, "vi", "en")
            back = _translate(en, "en", "vi")
            return back
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.0)
    raise RuntimeError(f"round-trip failed: {last_err}")


def _word_align(a: List[str], b: List[str]) -> List[tuple]:
    """Standard DP Levenshtein at word level. Returns list of ops."""
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if a[i - 1].lower() == b[j - 1].lower() else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )
    # backtrack
    ops = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and (a[i - 1].lower() == b[j - 1].lower()):
            ops.append(("keep", a[i - 1], ""))
            i -= 1
            j -= 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            ops.append(("replace", a[i - 1], b[j - 1]))
            i -= 1
            j -= 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            ops.append(("delete", a[i - 1], ""))
            i -= 1
        else:
            ops.append(("insert", "", b[j - 1]))
            j -= 1
    ops.reverse()
    return ops


def score_roundtrip(original: str, back_translated: str) -> List[RoundTripWordScore]:
    from underthesea import word_tokenize

    src = word_tokenize(original)
    tgt = word_tokenize(back_translated)
    ops = _word_align(src, tgt)
    out: List[RoundTripWordScore] = []
    for op, a, b in ops:
        out.append(RoundTripWordScore(word=a if a else b, status=op, note=b))
    return out
