"""Phase 20 — evidence freshness policies.

Proves the invariant:

    StaleEvidence ⇒ (refresh → reverify → fingerprint comparison)
    StaleEvidence that changes decision state ⇒ APPROVAL_STALE

The freshness service evaluates whether each piece of evidence an approval rests
on is still within its policy bound. ERP observations decay fast (15m); work
confirmations decay in 7d; stable extraction evidence has no bound.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from construction_ai.domain.models import Evidence
from construction_ai.verification.freshness import (
    evaluate_freshness,
    evaluate_freshness_for_approval,
    policy_for,
)


def _ev(field, *, source_type="erpnext", observed_at=None, evidence_id="E1"):
    return Evidence(
        evidence_id=evidence_id, source_type=source_type, source_id="S1", field=field,
        value={}, confidence=1.0, observed_at=observed_at or datetime.now(timezone.utc),
    )


def test_erp_purchase_order_evidence_stales_after_15_minutes():
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    ev = _ev("ERP_PURCHASE_ORDER_SNAPSHOT", observed_at=now - timedelta(minutes=20))
    result = evaluate_freshness([ev], now=now)
    assert result.fresh is False
    assert len(result.stale) == 1
    assert result.stale[0].field == "ERP_PURCHASE_ORDER_SNAPSHOT"
    assert result.stale[0].max_age == timedelta(minutes=15)


def test_erp_evidence_within_bound_is_fresh():
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    ev = _ev("ERP_PURCHASE_ORDER_SNAPSHOT", observed_at=now - timedelta(minutes=10))
    result = evaluate_freshness([ev], now=now)
    assert result.fresh is True
    assert result.stale == []


def test_work_confirmation_stales_after_7_days():
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    ev = _ev("WORK_CONFIRMATION", observed_at=now - timedelta(days=8))
    result = evaluate_freshness([ev], now=now)
    assert result.fresh is False
    assert result.stale[0].max_age == timedelta(days=7)


def test_work_confirmation_within_7_days_is_fresh():
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    ev = _ev("WORK_CONFIRMATION", observed_at=now - timedelta(days=6))
    result = evaluate_freshness([ev], now=now)
    assert result.fresh is True


def test_extraction_evidence_has_no_freshness_bound():
    """Stable extraction evidence (the invoice text) never stales."""
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    ev = _ev("INVOICE_TOTAL", source_type="deterministic", observed_at=now - timedelta(days=365))
    result = evaluate_freshness([ev], now=now)
    assert result.fresh is True
    assert policy_for(ev) is None


def test_default_erp_freshness_for_unfielded_erpnext_evidence():
    """ERPNext-sourced evidence without a specific field still decays (15m
    default) — never assumed fresh indefinitely."""
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    ev = _ev("SOME_OTHER_ERP_FIELD", source_type="erpnext", observed_at=now - timedelta(minutes=30))
    result = evaluate_freshness([ev], now=now)
    assert result.fresh is False


def test_mixed_evidence_reports_only_stale_subset():
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    fresh_ev = _ev("ERP_PURCHASE_ORDER_SNAPSHOT", observed_at=now - timedelta(minutes=5), evidence_id="F1")
    stale_ev = _ev("WORK_CONFIRMATION", observed_at=now - timedelta(days=10), evidence_id="S1")
    stable_ev = _ev("INVOICE_TOTAL", source_type="deterministic", observed_at=now - timedelta(days=100), evidence_id="ST1")
    result = evaluate_freshness([fresh_ev, stale_ev, stable_ev], now=now)
    assert result.fresh is False
    assert len(result.stale) == 1
    assert result.stale[0].evidence_id == "S1"


# -- DB-backed: evaluate_freshness_for_approval ----------------------------

def test_freshness_for_approval_loads_evidence(repos, org_a):
    scope = org_a["scope"]
    now = datetime.now(timezone.utc)
    # Record a stale ERP PO snapshot (observed 30m ago).
    stale = repos.evidence.record(
        scope=scope, field="ERP_PURCHASE_ORDER_SNAPSHOT", value={"grand_total": 1000},
        confidence=1.0, authority=0.95, source_type="erpnext", source_id="PO-1",
        observed_at=now - timedelta(minutes=30),
    )
    result = evaluate_freshness_for_approval(
        repos, scope=scope, evidence_ids=[UUID(stale.evidence_id)], now=now,
    )
    assert result.fresh is False
    assert len(result.stale) == 1


def test_freshness_for_approval_missing_evidence_is_stale(repos, org_a):
    """Fail closed: evidence that cannot be loaded is treated as stale."""
    import uuid
    scope = org_a["scope"]
    result = evaluate_freshness_for_approval(
        repos, scope=scope, evidence_ids=[uuid.uuid4()], now=datetime.now(timezone.utc),
    )
    assert result.fresh is False
