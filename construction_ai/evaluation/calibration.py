"""Calibration and threshold measurement for candidate producers.

A confidence that is not calibrated is a number that looks like a probability and
is not one. Before any threshold is used to promote anything automatically, the
question "when this says 0.95, how often is it right?" needs an answer measured
on a corpus rather than asserted in a docstring.

Everything here is a pure function of `(score, is_true_match)` pairs, so it works
identically for the deterministic matchers today and for whatever replaces them.
That is the point: a later ML or LLM matcher has a number to beat.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScoredCase:
    name: str
    score: float
    is_match: bool


@dataclass(frozen=True)
class ThresholdReport:
    threshold: float
    proposed: int
    correct: int
    incorrect: int
    missed: int

    @property
    def precision(self) -> float:
        """Of what we would auto-accept at this threshold, how much is right."""
        return self.correct / self.proposed if self.proposed else 1.0

    @property
    def recall(self) -> float:
        total_true = self.correct + self.missed
        return self.correct / total_true if total_true else 1.0

    @property
    def coverage(self) -> float:
        total = self.proposed + self.missed
        return self.proposed / total if total else 0.0

    def as_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "proposed": self.proposed,
            "correct": self.correct,
            "incorrect": self.incorrect,
            "missed": self.missed,
            "precision": round(self.precision, 6),
            "recall": round(self.recall, 6),
            "coverage": round(self.coverage, 6),
        }


@dataclass(frozen=True)
class ReliabilityBucket:
    lower: float
    upper: float
    count: int
    mean_score: float
    observed_rate: float

    @property
    def gap(self) -> float:
        """|predicted − observed|. Zero is perfect calibration for this bucket."""
        return abs(self.mean_score - self.observed_rate)


@dataclass(frozen=True)
class CalibrationReport:
    cases: int
    brier: float
    ece: float
    buckets: tuple[ReliabilityBucket, ...] = ()
    thresholds: tuple[ThresholdReport, ...] = field(default=())

    def at(self, threshold: float) -> ThresholdReport:
        for report in self.thresholds:
            if abs(report.threshold - threshold) < 1e-9:
                return report
        raise KeyError(f"no report at threshold {threshold}")

    def safe_threshold(self, *, minimum_precision: float = 1.0) -> float | None:
        """Lowest threshold whose precision still meets the bar, if any.

        Returns None when no threshold is safe — which is the honest answer for a
        producer that should not be promoting anything automatically yet.
        """
        eligible = [t for t in self.thresholds if t.proposed > 0 and t.precision >= minimum_precision]
        return min((t.threshold for t in eligible), default=None)

    def as_dict(self) -> dict:
        return {
            "cases": self.cases,
            "brier": round(self.brier, 6),
            "ece": round(self.ece, 6),
            "buckets": [
                {"range": [b.lower, b.upper], "count": b.count, "mean_score": round(b.mean_score, 4),
                 "observed_rate": round(b.observed_rate, 4), "gap": round(b.gap, 4)}
                for b in self.buckets
            ],
            "thresholds": [t.as_dict() for t in self.thresholds],
        }


DEFAULT_THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.97, 0.99)


def evaluate(cases: list[ScoredCase], *, thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS, buckets: int = 10) -> CalibrationReport:
    if not cases:
        return CalibrationReport(cases=0, brier=0.0, ece=0.0)

    brier = sum((c.score - (1.0 if c.is_match else 0.0)) ** 2 for c in cases) / len(cases)

    reliability: list[ReliabilityBucket] = []
    weighted_gap = 0.0
    for index in range(buckets):
        lower, upper = index / buckets, (index + 1) / buckets
        # Last bucket is closed on the right so a score of exactly 1.0 lands.
        members = [c for c in cases if lower <= c.score < upper or (index == buckets - 1 and c.score == 1.0)]
        if not members:
            continue
        mean_score = sum(c.score for c in members) / len(members)
        observed = sum(1 for c in members if c.is_match) / len(members)
        bucket = ReliabilityBucket(round(lower, 4), round(upper, 4), len(members), mean_score, observed)
        reliability.append(bucket)
        weighted_gap += (len(members) / len(cases)) * bucket.gap

    threshold_reports = []
    for threshold in thresholds:
        above = [c for c in cases if c.score >= threshold]
        below_true = [c for c in cases if c.score < threshold and c.is_match]
        threshold_reports.append(
            ThresholdReport(
                threshold=threshold,
                proposed=len(above),
                correct=sum(1 for c in above if c.is_match),
                incorrect=sum(1 for c in above if not c.is_match),
                missed=len(below_true),
            )
        )

    return CalibrationReport(
        cases=len(cases),
        brier=brier,
        ece=weighted_gap,
        buckets=tuple(reliability),
        thresholds=tuple(threshold_reports),
    )
