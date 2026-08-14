"""Deterministic candidate producers.

Every producer here calls `relationships.propose()` and never `observe()`:

    machine inference can increase candidate quality, but cannot increase authority

Scoring is split from persistence on purpose. The `score_*` functions are pure
functions over plain records, so the regression corpus can measure them without
a database, and a later ML or LLM matcher has a number to beat rather than a
vibe to argue with.
"""
from construction_ai.matching.signals import MatchSignal, normalize_identifier, normalize_name, similarity

__all__ = ["MatchSignal", "normalize_identifier", "normalize_name", "similarity"]
