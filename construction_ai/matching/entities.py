"""Entity matching — the first candidate producer.

    SAME_AS proposed  ≠  records merged
    SAME_AS approved  ≠  records merged

Both bear repeating because the second is the one people get wrong. An approved
`SAME_AS` asserts that two entities refer to the same real-world thing. It does
not authorize consolidating the rows behind them: identity resolution and record
consolidation are separate operations with different blast radii, and nothing in
this module or the graph performs the latter.

The cascade, strongest first:

    business identifier → ERP supplier id → normalized legal name
    → alias → fuzzy name

A fuzzy name match alone is capped below any plausible auto-promotion threshold.
"Never automatically merge entities on fuzzy name similarity" is enforced by the
cap, not by hoping nobody sets the threshold low.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from uuid import UUID

from construction_ai.matching.signals import MatchSignal, combine, normalize_identifier, normalize_name, similarity

RULE_VERSION = "entity-match-v1"
PRODUCER = f"entity-dedup:{RULE_VERSION}"

#: A fuzzy-name-only candidate can never exceed this, whatever the string
#: similarity says. Two unrelated subcontractors called "Northern Electric" are
#: a real thing, and a name is not an identifier.
FUZZY_ONLY_CEILING = 0.75

#: Below this, nothing is proposed at all — noise is worse than silence.
PROPOSAL_FLOOR = 0.55


@dataclass(frozen=True)
class EntityMatchCandidate:
    left_id: str
    right_id: str
    left_label: str
    right_label: str
    score: float
    signals: tuple[MatchSignal, ...]
    rule_version: str = RULE_VERSION
    contradictions: tuple[str, ...] = ()
    decisive_signal: str | None = None
    capped_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "left_id": self.left_id,
            "right_id": self.right_id,
            "left_label": self.left_label,
            "right_label": self.right_label,
            "score": self.score,
            "signals": [s.as_dict() for s in self.signals],
            "contradictions": list(self.contradictions),
            "decisive_signal": self.decisive_signal,
            "capped_reason": self.capped_reason,
            "rule_version": self.rule_version,
        }


def score_companies(left, right) -> EntityMatchCandidate:
    """Pure comparison of two company records. No I/O, no clock."""
    left_tax, right_tax = normalize_identifier(left.tax_id if hasattr(left, "tax_id") else None), normalize_identifier(
        right.tax_id if hasattr(right, "tax_id") else None
    )
    left_erp, right_erp = normalize_identifier(left.erp_supplier_id), normalize_identifier(right.erp_supplier_id)
    left_name, right_name = normalize_name(left.name), normalize_name(right.name)
    left_aliases = {normalize_name(a) for a in (left.aliases or [])} | {left_name}
    right_aliases = {normalize_name(a) for a in (right.aliases or [])} | {right_name}

    contradictions: list[str] = []
    signals: list[MatchSignal] = []
    decisive: str | None = None

    # 1. Business identifier. Agreement is near-decisive; disagreement is fatal.
    if left_tax and right_tax:
        agree = left_tax == right_tax
        signals.append(MatchSignal("tax_id", 1.0 if agree else 0.0, 0.40, f"{left_tax} vs {right_tax}"))
        if agree:
            decisive = "tax_id"
        else:
            contradictions.append("TAX_ID_MISMATCH")
    else:
        signals.append(MatchSignal("tax_id", 0.0, 0.40, "not recorded on both sides", available=False))

    # 2. ERP supplier identity.
    if left_erp and right_erp:
        agree = left_erp == right_erp
        signals.append(MatchSignal("erp_supplier_id", 1.0 if agree else 0.0, 0.25, f"{left_erp} vs {right_erp}"))
        if agree and decisive is None:
            decisive = "erp_supplier_id"
    else:
        signals.append(MatchSignal("erp_supplier_id", 0.0, 0.25, "not recorded on both sides", available=False))

    # 3. Normalized legal name.
    name_exact = bool(left_name) and left_name == right_name
    signals.append(MatchSignal("normalized_name", 1.0 if name_exact else 0.0, 0.20, f"{left_name!r} vs {right_name!r}"))
    if name_exact and decisive is None:
        decisive = "normalized_name"

    # 4. Alias overlap — how a company writes its own name in different systems.
    alias_hit = bool(left_aliases & right_aliases)
    signals.append(
        MatchSignal("alias", 1.0 if alias_hit else 0.0, 0.10, "shared alias" if alias_hit else "no shared alias")
    )
    if alias_hit and decisive is None:
        decisive = "alias"

    # 5. Fuzzy fallback.
    fuzzy = similarity(left_name, right_name)
    signals.append(MatchSignal("fuzzy_name", fuzzy, 0.05, f"similarity {fuzzy}"))
    if decisive is None and fuzzy >= 0.85:
        decisive = "fuzzy_name"

    # Signals a people-level producer will supply; declared so the vector shape
    # stays honest rather than silently shorter for companies.
    signals.append(MatchSignal("email_domain", 0.0, 0.0, "companies carry no contact record yet", available=False))
    signals.append(MatchSignal("phone", 0.0, 0.0, "companies carry no contact record yet", available=False))

    score = combine(tuple(signals))
    capped_reason = None

    if contradictions:
        # A conflicting business identifier is not outweighed by a matching name.
        score = min(score, 0.30)
        capped_reason = "contradicted by a stronger signal"
    elif decisive in {None, "fuzzy_name"}:
        if score > FUZZY_ONLY_CEILING:
            capped_reason = "no identifier or exact-name corroboration"
        score = min(score, FUZZY_ONLY_CEILING)

    return EntityMatchCandidate(
        left_id=left.company_id,
        right_id=right.company_id,
        left_label=left.reference or left.name,
        right_label=right.reference or right.name,
        score=round(score, 6),
        signals=tuple(signals),
        contradictions=tuple(contradictions),
        decisive_signal=decisive,
        capped_reason=capped_reason,
    )


def find_company_candidates(companies, *, floor: float = PROPOSAL_FLOOR) -> list[EntityMatchCandidate]:
    """All pairs scoring at or above the floor, in a stable order."""
    candidates = [score_companies(left, right) for left, right in combinations(companies, 2)]
    kept = [c for c in candidates if c.score >= floor]
    return sorted(kept, key=lambda c: (-c.score, c.left_id, c.right_id))


def propose_company_matches(repos, scope, *, floor: float = PROPOSAL_FLOOR) -> list[dict]:
    """Score every company pair in the tenant and propose the survivors.

    Proposals only. Nothing here promotes, and nothing here merges: a company row
    that gains an approved `SAME_AS` edge is still its own row afterwards.
    """
    companies = sorted(repos.companies.list(scope=scope.organization_only), key=lambda c: c.company_id)
    proposals: list[dict] = []
    for candidate in find_company_candidates(companies, floor=floor):
        left_entity = repos.entities.ensure(
            scope=scope, entity_type="VENDOR", record_table="companies",
            record_id=UUID(candidate.left_id), label=candidate.left_label,
        )
        right_entity = repos.entities.ensure(
            scope=scope, entity_type="VENDOR", record_table="companies",
            record_id=UUID(candidate.right_id), label=candidate.right_label,
        )
        relationship = repos.relationships.propose(
            scope=scope,
            source_entity_id=left_entity.entity_id,
            target_entity_id=right_entity.entity_id,
            relation="SAME_AS",
            confidence=candidate.score,
            annotations=candidate.as_dict(),
            producer=PRODUCER,
        )
        proposals.append({"relationship_id": str(relationship.relationship_id), **candidate.as_dict()})
    return proposals
