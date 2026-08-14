"""Evidence freshness policies (Phase 20).

Not all evidence is valid forever. Before financial execution, every piece of
evidence an approval rests on must be checked for freshness against its policy:

    erp_purchase_order: 15m
    erp_supplier:        24h
    work_confirmation:    7d
    document extraction:  90d   (the invoice text itself — stable)

If evidence is stale, execution must refresh → reverify → fingerprint
comparison. If the refresh changes the authoritative decision state, the
approval is APPROVAL_STALE and execution is refused (the human approved a
different state than the one we'd execute against).

This module provides the freshness evaluation. The executor (Phase 22) calls it
as one of its preconditions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from construction_ai.domain.models import Evidence
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories

#: Freshness policy by evidence field. A field not listed has no freshness
#: bound (never stale) — extraction evidence is stable, but ERP observations
#: and work confirmations decay.
FRESHNESS_POLICIES: dict[str, timedelta] = {
    "ERP_PURCHASE_ORDER_SNAPSHOT": timedelta(minutes=15),
    "ERP_QUOTE_SNAPSHOT": timedelta(minutes=15),
    "ERP_SUPPLIER_SNAPSHOT": timedelta(hours=24),
    "ERP_SUPPLIER": timedelta(hours=24),
    "WORK_CONFIRMATION": timedelta(days=7),
}

#: Default freshness for any ERPNext-sourced evidence without a specific field.
_DEFAULT_ERP_FRESHNESS = timedelta(minutes=15)


@dataclass(frozen=True)
class StaleEvidence:
    evidence_id: str
    field: str
    observed_at: datetime
    age: timedelta
    max_age: timedelta


@dataclass(frozen=True)
class FreshnessResult:
    fresh: bool
    stale: list[StaleEvidence] = field(default_factory=list)


def policy_for(evidence: Evidence) -> timedelta | None:
    """The freshness bound for a piece of evidence, or None if it never stales."""
    if evidence.field in FRESHNESS_POLICIES:
        return FRESHNESS_POLICIES[evidence.field]
    if evidence.source_type == "erpnext":
        return _DEFAULT_ERP_FRESHNESS
    return None


def evaluate_freshness(evidence: list[Evidence], *, now: datetime | None = None) -> FreshnessResult:
    """Evaluate the freshness of a set of evidence records.

    Returns fresh=True when no evidence is past its policy bound. Evidence with
    no policy is always fresh.
    """
    now = now or datetime.now(timezone.utc)
    stale: list[StaleEvidence] = []
    for ev in evidence:
        bound = policy_for(ev)
        if bound is None:
            continue
        observed = ev.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age = now - observed
        if age > bound:
            stale.append(StaleEvidence(
                evidence_id=ev.evidence_id, field=ev.field, observed_at=observed,
                age=age, max_age=bound,
            ))
    return FreshnessResult(fresh=not stale, stale=stale)


def evaluate_freshness_for_approval(
    repos: Repositories, *, scope: Scope, evidence_ids: list[UUID], now: datetime | None = None,
) -> FreshnessResult:
    """Load evidence by id and evaluate freshness — the executor's precondition
    path. Evidence that cannot be loaded is treated as stale (fail closed)."""
    now = now or datetime.now(timezone.utc)
    if not evidence_ids:
        return FreshnessResult(fresh=True)
    evidence = repos.evidence.get_many(scope=scope, evidence_ids=evidence_ids)
    if len(evidence) != len(evidence_ids):
        # Missing evidence — cannot prove freshness.
        return FreshnessResult(fresh=False, stale=[
            StaleEvidence(evidence_id="missing", field="MISSING", observed_at=now, age=timedelta.max, max_age=timedelta(0))
        ])
    return evaluate_freshness(evidence, now=now)
