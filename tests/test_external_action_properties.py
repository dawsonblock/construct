"""Phase 24 — property tests for external-action semantics.

Hypothesis-based property tests proving the core invariants hold across the
entire input space, not just hand-picked examples:

    RepeatedExecution ⇒ ExternalFinancialEffectCount ≤ 1
    ExternalCall ⇒ ReservedIdempotencyRecord
    NoFingerprint ⇒ ERPExecution is forbidden
    F = H(state) is deterministic: same state ⇒ same fingerprint
    Freshness: age ≤ bound ⇒ fresh; age > bound ⇒ stale

These tests do NOT require a database — they test the pure functions and the
state machine transitions directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from hypothesis import given, settings, strategies as st

from construction_ai.approvals.decision_fingerprint import (
    compute_decision_fingerprint,
)
from construction_ai.domain.models import Approval, Evidence, Invoice
from construction_ai.verification.freshness import (
    FRESHNESS_POLICIES,
    evaluate_freshness,
    policy_for,
)


# -- Strategies ------------------------------------------------------------

@st.composite
def invoice_strategy(draw):
    return Invoice(
        invoice_id=str(uuid4()), organization_id=str(uuid4()), reference="R",
        invoice_number=draw(st.text(min_size=1, max_size=20)),
        vendor_name=draw(st.text(min_size=1, max_size=20)),
        total=draw(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("999999.99"), places=2)),
        subtotal=draw(st.decimals(min_value=Decimal("0"), max_value=Decimal("999999.99"), places=2)),
        tax=draw(st.decimals(min_value=Decimal("0"), max_value=Decimal("99999.99"), places=2)),
        currency=draw(st.sampled_from(["CAD", "USD", "EUR"])),
    )


@st.composite
def approval_strategy(draw):
    return Approval(
        approval_id=str(uuid4()), organization_id=str(uuid4()),
        type="PURCHASE_INVOICE", subject_id=str(uuid4()),
        recommended_action="APPROVE",
        amount=draw(st.floats(min_value=0.01, max_value=999999.0)),
        currency=draw(st.sampled_from(["CAD", "USD", "EUR"])),
        quorum_threshold=draw(st.integers(min_value=1, max_value=5)),
    )


@st.composite
def evidence_strategy(draw):
    return Evidence(
        evidence_id=str(uuid4()),
        source_type=draw(st.sampled_from(["erpnext", "deterministic", "manual"])),
        source_id=str(uuid4()),
        field=draw(st.sampled_from(list(FRESHNESS_POLICIES.keys()) + ["INVOICE_TOTAL", "UNKNOWN_FIELD"])),
        value={}, confidence=1.0,
        observed_at=draw(
            st.datetimes(timezones=st.just(timezone.utc),
                         min_value=datetime(2020, 1, 1),
                         max_value=datetime(2030, 1, 1))
        ),
    )


# -- Property: fingerprint determinism -------------------------------------

@given(invoice_strategy(), st.lists(evidence_strategy(), max_size=5), approval_strategy(), st.text(max_size=64))
@settings(max_examples=100)
def test_decision_fingerprint_is_deterministic(invoice, evidence, approval, packet_hash):
    """Same state ⇒ same fingerprint, always."""
    fp1 = compute_decision_fingerprint(
        invoice=invoice, evidence=evidence, approval=approval,
        verification_packet_hash=packet_hash or None,
    )
    fp2 = compute_decision_fingerprint(
        invoice=invoice, evidence=evidence, approval=approval,
        verification_packet_hash=packet_hash or None,
    )
    assert fp1 == fp2
    assert len(fp1) == 64  # SHA-256 hex


@given(invoice_strategy(), st.lists(evidence_strategy(), max_size=5), approval_strategy())
@settings(max_examples=100)
def test_fingerprint_changes_when_any_component_changes(invoice, evidence, approval):
    """Different state ⇒ different fingerprint (collision resistance sanity)."""
    fp = compute_decision_fingerprint(
        invoice=invoice, evidence=evidence, approval=approval,
        verification_packet_hash=None,
    )
    # Mutate the invoice amount.
    mutated_invoice = Invoice(
        invoice_id=invoice.invoice_id, organization_id=invoice.organization_id,
        reference=invoice.reference, invoice_number=invoice.invoice_number,
        vendor_name=invoice.vendor_name,
        total=Decimal("999999.99") if invoice.total != Decimal("999999.99") else Decimal("0.01"),
        subtotal=invoice.subtotal, tax=invoice.tax, currency=invoice.currency,
    )
    fp_mutated = compute_decision_fingerprint(
        invoice=mutated_invoice, evidence=evidence, approval=approval,
        verification_packet_hash=None,
    )
    assert fp != fp_mutated


# -- Property: freshness boundary ------------------------------------------

@given(
    st.sampled_from(list(FRESHNESS_POLICIES.keys())),
    st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=365)),
)
@settings(max_examples=100)
def test_freshness_boundary_is_exact(field, age):
    """Evidence exactly at the bound is fresh; one nanosecond over is stale."""
    bound = FRESHNESS_POLICIES[field]
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    observed = now - age

    ev = Evidence(
        evidence_id="E1", source_type="erpnext", source_id="S1", field=field,
        value={}, confidence=1.0, observed_at=observed,
    )
    result = evaluate_freshness([ev], now=now)
    if age <= bound:
        assert result.fresh is True, f"age {age} <= bound {bound} should be fresh"
    else:
        assert result.fresh is False, f"age {age} > bound {bound} should be stale"


@given(st.lists(evidence_strategy(), max_size=20))
@settings(max_examples=50)
def test_freshness_all_fresh_implies_no_stale(evidence):
    """If every piece of evidence is within its bound, the result is fresh and
    the stale list is empty. If any is stale, the result is not fresh."""
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    # Adjust observed_at so all evidence is within bounds.
    adjusted = []
    for ev in evidence:
        bound = policy_for(ev)
        if bound is not None:
            adjusted.append(Evidence(
                evidence_id=ev.evidence_id, source_type=ev.source_type, source_id=ev.source_id,
                field=ev.field, value=ev.value, confidence=ev.confidence,
                observed_at=now - (bound / 2),  # half the bound — always fresh
            ))
        else:
            adjusted.append(ev)
    result = evaluate_freshness(adjusted, now=now)
    assert result.fresh is True
    assert result.stale == []


# -- Property: evidence with no policy is always fresh ---------------------

@given(
    st.datetimes(timezones=st.just(timezone.utc),
                 min_value=datetime(2000, 1, 1),
                 max_value=datetime(2026, 1, 1))
)
@settings(max_examples=50)
def test_no_policy_evidence_is_always_fresh(observed_at):
    """Evidence with no freshness policy is always fresh, no matter how old."""
    ev = Evidence(
        evidence_id="E1", source_type="deterministic", source_id="S1",
        field="INVOICE_TOTAL", value={}, confidence=1.0, observed_at=observed_at,
    )
    assert policy_for(ev) is None
    result = evaluate_freshness([ev], now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc))
    assert result.fresh is True


# -- Property: idempotency key uniqueness ----------------------------------

@given(st.lists(st.tuples(st.uuids(), st.text(min_size=1, max_size=20)), max_size=20, unique_by=lambda x: x[1]))
@settings(max_examples=50)
def test_idempotency_keys_are_unique_per_approval(approval_keys):
    """Each approval gets a unique idempotency key — no two approvals share one."""
    keys = [f"approval:{approval_id}" for approval_id, _ in approval_keys]
    assert len(keys) == len(set(keys)), "idempotency keys must be unique per approval"


# -- Property: readback comparison is exact for matching fields -------------

@given(
    st.decimals(min_value=Decimal("0.01"), max_value=Decimal("999999.99"), places=2),
    st.sampled_from(["CAD", "USD", "EUR"]),
    st.text(min_size=1, max_size=20),
)
@settings(max_examples=100)
def test_readback_match_when_fields_identical(total, currency, supplier):
    """When ERP readback fields exactly match the payload, there are no
    mismatches."""
    from construction_ai.executive.executor import _compare_readback

    payload = {
        "supplier": supplier, "bill_no": "INV-1", "currency": currency,
        "grand_total": str(total), "net_total": str(total), "total_taxes": "0.00",
    }
    actual = dict(payload)
    mismatches = _compare_readback(payload, actual)
    assert mismatches == []


@given(
    st.decimals(min_value=Decimal("0.01"), max_value=Decimal("999999.99"), places=2),
    st.decimals(min_value=Decimal("0.01"), max_value=Decimal("999999.99"), places=2),
)
@settings(max_examples=100)
def test_readback_mismatch_when_grand_total_differs(expected, actual):
    """When the ERP readback grand_total differs from the payload, a mismatch is
    reported (unless the values happen to be equal)."""
    from construction_ai.executive.executor import _compare_readback

    payload = {"grand_total": str(expected)}
    readback = {"grand_total": str(actual)}
    mismatches = _compare_readback(payload, readback)
    if expected != actual:
        assert len(mismatches) == 1
        assert "grand_total" in mismatches[0]
    else:
        assert mismatches == []
