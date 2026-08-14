"""Signals, normalization and the weighted combination rule.

A candidate carries an explanation vector, not a bare float. A reviewer being
asked to promote a match needs to see *why* it scored what it did, and a
threshold that was tuned against opaque scores cannot be defended later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

#: Suffixes that carry no identifying information. "ABC Electric Ltd." and
#: "ABC Electric" are the same normalized name; they are still not automatically
#: the same company.
LEGAL_SUFFIXES = {
    "ltd", "ltda", "limited", "inc", "incorporated", "llc", "llp", "lp", "plc",
    "corp", "corporation", "co", "company", "gmbh", "sarl", "pty", "ulc",
}

_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class MatchSignal:
    """One dimension of a comparison.

    `available=False` means the data to evaluate this signal was absent — which
    is not the same as scoring zero, and must not be averaged in as though it
    were. Unavailable signals are excluded and the remaining weights renormalized.
    """

    name: str
    score: float
    weight: float
    detail: str = ""
    available: bool = True

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "score": round(self.score, 4) if self.available else None,
            "weight": self.weight,
            "available": self.available,
            "detail": self.detail,
        }


def normalize_name(value: str | None) -> str:
    """Lowercase, depunctuate, drop legal suffixes, collapse whitespace."""
    if not value:
        return ""
    cleaned = _PUNCTUATION.sub(" ", value.lower())
    tokens = [t for t in _WHITESPACE.split(cleaned) if t and t not in LEGAL_SUFFIXES]
    return " ".join(tokens)


def normalize_identifier(value: str | None) -> str:
    """Uppercase alphanumerics only — `GST-100 200 300` and `gst100200300` agree."""
    if not value:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def normalize_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    # Drop a North American country code so +1-306-555-0100 and 3065550100 agree.
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def email_domain(value: str | None) -> str:
    return (value or "").strip().lower().rpartition("@")[2]


def similarity(left: str, right: str) -> float:
    """Character similarity blended with token overlap, both on normalized text.

    Neither alone behaves well on company names: sequence matching over-rewards
    shared prefixes ("Northline Drywall" vs "Northline Drilling"), token overlap
    ignores order entirely. The blend is deterministic, which is what matters
    most — the corpus measures whether it is *good enough*, not whether it is
    clever.
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    sequence = SequenceMatcher(None, left, right).ratio()
    left_tokens, right_tokens = set(left.split()), set(right.split())
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens) if left_tokens | right_tokens else 0.0
    return round(0.5 * sequence + 0.5 * overlap, 6)


def combine(signals: tuple[MatchSignal, ...]) -> float:
    """Weighted mean over *available* signals only.

    Scoring an unavailable signal as zero would punish a match for data we never
    had; scoring it as one would invent corroboration. Excluding it and
    renormalizing is the only honest option, and the candidate reports which
    signals were excluded so a reviewer can see the score was computed on less.
    """
    usable = [s for s in signals if s.available]
    total_weight = sum(s.weight for s in usable)
    if total_weight <= 0:
        return 0.0
    return round(sum(s.score * s.weight for s in usable) / total_weight, 6)
