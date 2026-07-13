"""N-gram whitelist layer for the error detector.

Loads trusted multi-word terms from dictionaries/ and exposes two operations:

  1. covers(span) -> bool
       True if `span` (a subsequence of words, space-joined) appears in the
       trusted-terms set. Used to SUPPRESS false positives: if a flagged span
       is a known collocation, it is not an error.

  2. missing_inside(span_left, span_right) -> str | None
       Convenience used by the GAP probe: if the model wants word W between
       left and right, but left+W or W+right is already a trusted term, the
       flag is almost certainly spurious (the model is "completing" a known
       phrase that is fine as-is).

The whitelist is loaded once into a set of normalized surfaces. We also keep
a set of the individual words of every term so covers() can answer per-word
questions cheaply.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Set

_DICT_DIR = Path(__file__).resolve().parents[2] / "dictionaries"


def _norm(s: str) -> str:
    """Normalize for matching: lowercase, collapse spaces, strip CR."""
    return " ".join(s.lower().replace("\r", "").split())


class Whitelist:
    def __init__(self, terms: Iterable[str]):
        self.terms: Set[str] = set()
        self.words: Set[str] = set()
        for t in terms:
            n = _norm(t)
            if not n:
                continue
            self.terms.add(n)
            for w in n.split():
                self.words.add(w)

    @classmethod
    def from_files(cls, *paths: str) -> "Whitelist":
        out = []
        for p in paths:
            fp = Path(p)
            if not fp.is_absolute():
                fp = _DICT_DIR / fp
            if not fp.exists():
                continue
            with fp.open(encoding="utf-8", errors="ignore") as f:
                out.extend(line for line in f)
        return cls(out)

    def covers(self, span: str) -> bool:
        """True if the full span (or a normalized form) is a trusted term."""
        n = _norm(span)
        if not n:
            return False
        if n in self.terms:
            return True
        # also accept if every word of the span is part of some trusted term
        # that starts the same way — too loose, skip.
        return False

    def covers_word(self, w: str) -> bool:
        return _norm(w) in self.words

    def adjacent_forms_trusted(self, left: str, guess: str, right: str) -> bool:
        """If 'left guess' or 'guess right' is a trusted term, the GAP flag is
        likely the model over-completing an already-fine phrase."""
        if not guess.strip():
            return False
        return self.covers(f"{left} {guess}") or self.covers(f"{guess} {right}")

    def __len__(self):
        return len(self.terms)


DEFAULT_FILES = ("trusted_party_terms.txt", "party_frequency_lexicon_v8_no_person_names.txt")


if __name__ == "__main__":
    wl = Whitelist.from_files(*DEFAULT_FILES)
    print(f"Loaded {len(wl)} trusted terms, {len(wl.words)} unique words.")
    for probe in ["cấp ủy", "thành ủy", "văn phòng", "hệ thống", "báo cáo",
                   "dữ liệu", "hệ thống quản trị", "văn phòng thành ủy"]:
        print(f"  covers('{probe}') = {wl.covers(probe)}")
