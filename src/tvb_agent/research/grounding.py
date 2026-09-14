"""Quote verification.

The extractor asks the model to return, alongside every fact, the exact sentence
it read that fact in.  This module decides whether that sentence really appears
in the page we fetched.  A claim whose quote cannot be located is discarded,
which means a fabricated funding figure or invented founder never reaches the
qualification engine even if the model states it confidently.

Matching is tolerant of formatting (whitespace, smart quotes, casing) but not of
content: a paraphrase that shares few consecutive words with the source fails.
"""

from __future__ import annotations

import re
import unicodedata

_PUNCT_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-",
    " ": " ", " ": " ", " ": " ", "​": "",
}

MIN_QUOTE_CHARS = 12
MIN_RUN_TOKENS = 6
MIN_RUN_RATIO = 0.65


def normalise(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    for src, dst in _PUNCT_MAP.items():
        t = t.replace(src, dst)
    t = t.lower()
    t = re.sub(r"[^\w\s%$€£₹.,:/+-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(text: str) -> list[str]:
    return normalise(text).split()


def _longest_common_run(a: list[str], b: list[str]) -> int:
    """Longest run of consecutive tokens shared by both sequences."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


class GroundingResult:
    __slots__ = ("ok", "mode", "detail")

    def __init__(self, ok: bool, mode: str, detail: str = ""):
        self.ok = ok
        self.mode = mode      # exact | approximate | rejected
        self.detail = detail

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:  # pragma: no cover
        return f"GroundingResult({self.ok}, {self.mode!r})"


def verify_quote(quote: str, source_text: str) -> GroundingResult:
    """Is ``quote`` actually present in ``source_text``?"""
    if not quote or not source_text:
        return GroundingResult(False, "rejected", "empty quote or source")
    if len(quote.strip()) < MIN_QUOTE_CHARS:
        return GroundingResult(False, "rejected", "quote too short to be meaningful")

    nq, ns = normalise(quote), normalise(source_text)
    if not nq:
        return GroundingResult(False, "rejected", "quote normalised to nothing")
    if nq in ns:
        return GroundingResult(True, "exact")

    # Models often stitch a quote across a line break or drop a trailing clause.
    qt, st = tokens(quote), tokens(source_text)
    if len(qt) < MIN_RUN_TOKENS:
        return GroundingResult(False, "rejected", "quote too short for approximate matching")
    run = _longest_common_run(qt, st)
    ratio = run / len(qt)
    if run >= MIN_RUN_TOKENS and ratio >= MIN_RUN_RATIO:
        return GroundingResult(True, "approximate", f"{run}/{len(qt)} consecutive tokens matched")
    return GroundingResult(False, "rejected", f"only {run}/{len(qt)} consecutive tokens matched")


def snippet_around(quote: str, source_text: str, window: int = 320) -> str:
    """Return the real source text around the quote, for display in the UI."""
    ns, nq = normalise(source_text), normalise(quote)
    idx = ns.find(nq)
    if idx == -1:
        return quote.strip()[:window]
    ratio = len(source_text) / max(len(ns), 1)
    start = max(0, int(idx * ratio) - window // 4)
    return re.sub(r"\s+", " ", source_text[start : start + window]).strip()
