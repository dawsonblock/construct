"""Phase 19 — immutable versioned verification packets.

Proves the invariant:

    ApprovalDecision → ExactVerificationPacket (immutable, versioned, hashed)

Once an approval decision references a verification packet, the packet cannot
mutate. A new verification produces a new packet row with a new canonical hash.
The app role cannot UPDATE or DELETE a packet.

Needs a real PostgreSQL as the non-superuser app role.
"""
from __future__ import annotations

from uuid import UUID

import psycopg.errors
import pytest


def _make_approval(repos, scope):
    """Create a minimal invoice + approval to attach a packet to."""
    invoice = repos.invoices.create(
        scope=scope, reference="INV-PKT-1", invoice_number="PKT-1", vendor_name="ABC Electric",
        total=1000, subtotal=1000, tax=0, currency="CAD", created_by="test",
    )
    approval = repos.approvals.create(
        scope=scope, reference="APR-PKT-1", approval_type="PURCHASE_INVOICE",
        subject_type="invoice", subject_id=UUID(invoice.invoice_id),
        recommended_action="APPROVE", amount=1000, exceptions=[], evidence_ids=[],
        requested_by="test",
    )
    return invoice, approval


def test_packet_records_canonical_hash_and_version(repos, org_a):
    scope = org_a["scope"]
    _, approval = _make_approval(repos, scope)
    payload = {"verification": {"checks": {"vendor_match": "PASS"}}, "subject": {"id": "x"}}
    packet_id = repos.approval_packets.create(
        scope=scope, approval_id=UUID(approval.approval_id), reference="APKT-1",
        payload=payload, verifier_version="2", policy_inputs={"checks": ["vendor_match"]},
    )

    meta = repos.approval_packets.get_with_meta(scope=scope, packet_id=packet_id)
    assert meta is not None
    assert meta["version"] == 1
    assert meta["canonical_hash"] is not None
    assert len(meta["canonical_hash"]) == 64  # SHA-256 hex
    assert meta["verifier_version"] == "2"
    assert meta["policy_inputs"] == {"checks": ["vendor_match"]}


def test_packet_canonical_hash_is_deterministic(repos, org_a):
    """Same payload → same canonical hash."""
    scope = org_a["scope"]
    _, approval = _make_approval(repos, scope)
    payload = {"b": 2, "a": 1, "nested": {"y": 2, "x": 1}}
    h = repos.approval_packets._canonical_hash(payload)
    # Canonical JSON sorts keys, so key order in the input must not matter.
    h2 = repos.approval_packets._canonical_hash({"a": 1, "b": 2, "nested": {"x": 1, "y": 2}})
    assert h == h2


def test_packet_is_append_only_app_role_cannot_update(repos, org_a):
    """The app role must not be able to UPDATE a packet row — packets are
    immutable once written."""
    scope = org_a["scope"]
    _, approval = _make_approval(repos, scope)
    packet_id = repos.approval_packets.create(
        scope=scope, approval_id=UUID(approval.approval_id), reference="APKT-IMMUT",
        payload={"v": 1},
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with repos.db.scoped(scope) as cur:
            cur.execute(
                "UPDATE approval_packets SET payload = '{\"v\": 2}'::jsonb "
                "WHERE organization_id = %s AND packet_id = %s",
                (scope.organization_id, packet_id),
            )


def test_new_verification_produces_new_packet_row(repos, org_a):
    """A second packet for the same approval is a new row, not a mutation of the
    first. The latest (by created_at) is returned by for_approval."""
    scope = org_a["scope"]
    _, approval = _make_approval(repos, scope)
    first = repos.approval_packets.create(
        scope=scope, approval_id=UUID(approval.approval_id), reference="APKT-V1",
        payload={"verification": {"checks": {"amount_match": "PASS"}}}, version=1,
    )
    second = repos.approval_packets.create(
        scope=scope, approval_id=UUID(approval.approval_id), reference="APKT-V2",
        payload={"verification": {"checks": {"amount_match": "FAIL"}}}, version=2,
    )
    assert first != second

    latest = repos.approval_packets.for_approval(scope=scope, approval_id=UUID(approval.approval_id))
    assert latest == {"verification": {"checks": {"amount_match": "FAIL"}}}

    meta = repos.approval_packets.latest_meta_for_approval(scope=scope, approval_id=UUID(approval.approval_id))
    assert meta["version"] == 2
    # Different payloads → different canonical hashes.
    first_meta = repos.approval_packets.get_with_meta(scope=scope, packet_id=first)
    assert first_meta["canonical_hash"] != meta["canonical_hash"]


def test_distinct_packets_have_distinct_hashes(repos, org_a):
    scope = org_a["scope"]
    _, approval = _make_approval(repos, scope)
    p1 = repos.approval_packets.create(
        scope=scope, approval_id=UUID(approval.approval_id), reference="APKT-H1",
        payload={"total": 1000},
    )
    p2 = repos.approval_packets.create(
        scope=scope, approval_id=UUID(approval.approval_id), reference="APKT-H2",
        payload={"total": 2000},
    )
    m1 = repos.approval_packets.get_with_meta(scope=scope, packet_id=p1)
    m2 = repos.approval_packets.get_with_meta(scope=scope, packet_id=p2)
    assert m1["canonical_hash"] != m2["canonical_hash"]
