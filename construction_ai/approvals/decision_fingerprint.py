"""Decision fingerprint composition (Phase 6).

The project-wide state_fingerprint (migration 018) changes when ANY project
state changes — including unrelated invoices — which produces false stale
positives. The decision fingerprint binds the EXACT state used for one approval
decision:

    F = H(InvoiceSnapshot ∥ VerificationPacket ∥ EvidenceSet
           ∥ PolicyVersion ∥ ApprovalRequirements)

where:

  InvoiceSnapshot       — the billed invoice's financial fields (vendor,
                          invoice_number, total, subtotal, tax, currency,
                          po_number, quote_number, invoice_date).
  VerificationPacket    — the canonical_hash of the immutable versioned
                          verification packet (Phase 19) bound to the approval.
  EvidenceSet           — the sorted set of (evidence_id, field, source_type,
                          observed_at, content_hash) for every evidence record
                          the approval rests on.
  PolicyVersion         — the authority policy version under which the approver
                          acted.
  ApprovalRequirements  — amount, currency, quorum_threshold,
                          required_approvals, approval_type.

The fingerprint is SHA-256 over the canonical JSON rendering of these
components. The executor recomputes it from current authoritative rows before
ERP execution and refuses if it differs from the approved one (APPROVAL_STALE).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from construction_ai.approvals.policy import ApprovalPolicy, DEFAULT_POLICY
from construction_ai.domain.models import Approval, Evidence, Invoice
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories

#: The default policy version under which decisions are made.
POLICY_VERSION = "authority:v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def invoice_snapshot(invoice: Invoice) -> dict[str, Any]:
    """The financial fields of the billed invoice, canonicalized."""
    return {
        "invoice_id": str(invoice.invoice_id),
        "invoice_number": invoice.invoice_number,
        "vendor_name": invoice.vendor_name,
        "vendor_company_id": str(invoice.vendor_company_id) if invoice.vendor_company_id else None,
        "total": str(invoice.total) if invoice.total is not None else None,
        "subtotal": str(invoice.subtotal) if invoice.subtotal is not None else None,
        "tax": str(invoice.tax) if invoice.tax is not None else None,
        "currency": invoice.currency,
        "po_number": invoice.po_number,
        "quote_number": invoice.quote_number,
        "invoice_date": str(invoice.invoice_date) if invoice.invoice_date else None,
    }


def evidence_set(evidence: list[Evidence]) -> list[dict[str, Any]]:
    """The sorted set of evidence records the approval rests on."""
    records = [
        {
            "evidence_id": str(e.evidence_id),
            "field": e.field,
            "source_type": e.source_type,
            "source_id": e.source_id,
            "observed_at": str(e.observed_at) if e.observed_at else None,
            "content_hash": getattr(e, "content_hash", None),
        }
        for e in evidence
    ]
    return sorted(records, key=lambda r: r["evidence_id"])


def approval_requirements(approval: Approval) -> dict[str, Any]:
    """The approval's requirements as decided.

    rc7 Phase 12: Strengthened to include organization_id and invoice_id
    (via subject_id) so the fingerprint binds to the exact financial subject
    and tenant. This prevents cross-tenant or cross-invoice approval replay.
    """
    return {
        "approval_id": str(approval.approval_id),
        "approval_type": approval.type,
        "amount": str(approval.amount) if approval.amount is not None else None,
        "currency": approval.currency,
        "quorum_threshold": approval.quorum_threshold,
        "subject_id": str(approval.subject_id),
        "organization_id": str(approval.organization_id) if hasattr(approval, "organization_id") and approval.organization_id else None,
    }


def compute_decision_fingerprint(
    *,
    invoice: Invoice,
    evidence: list[Evidence],
    approval: Approval,
    verification_packet_hash: str | None,
    policy_version: str | None = None,
    policy_hash: str | None = None,
    policy: ApprovalPolicy | None = None,
) -> str:
    """Compute the decision fingerprint from its authoritative components.

    The fingerprint binds:
    1. InvoiceSnapshot (financial fields)
    2. VerificationPacket (canonical hash of immutable verification packet)
    3. EvidenceSet (sorted evidence IDs and content hashes)
    4. PolicyVersion & PolicyHash (exact active policy configuration and digest)
    5. ApprovalRequirements (amount, currency, quorum, subject)
    """
    if policy is not None:
        p_version = policy.version
        p_hash = policy.policy_hash()
    else:
        p_version = policy_version or POLICY_VERSION
        p_hash = policy_hash or DEFAULT_POLICY.policy_hash()

    payload = {
        "invoice_snapshot": invoice_snapshot(invoice),
        "verification_packet_hash": verification_packet_hash,
        "evidence_set": evidence_set(evidence),
        "policy_version": p_version,
        "policy_hash": p_hash,
        "approval_requirements": approval_requirements(approval),
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


class ApprovalPolicyCorrupt(Exception):
    """rc7: The stored policy snapshot does not match the stored policy hash.

    This is a data-integrity failure that should fail closed — the approval
    cannot be safely executed because the policy under which it was approved
    cannot be verified.
    """


class ApprovalPolicyMissing(Exception):
    """rc7: The approval has no persisted policy snapshot or hash.

    rc6 allowed falling back to DEFAULT_POLICY for legacy approvals, but rc7
    tightens the invariant: an executable approval MUST have its exact policy
    snapshot. Without it, the decision fingerprint cannot be verified and the
    approval requires revalidation.
    """


def _policy_from_snapshot(approval: Approval) -> ApprovalPolicy | None:
    """rc6/rc7: Reconstruct an ApprovalPolicy from the snapshot stored on the approval.

    Returns None if the approval has no persisted policy snapshot.
    rc7: Also validates that Hash(reconstructed_policy) == stored policy_hash.
    If the hash does not match, raises ApprovalPolicyCorrupt.
    """
    snapshot = getattr(approval, "policy_snapshot", None)
    if not snapshot:
        return None
    try:
        from decimal import Decimal as _Decimal
        threshold = snapshot.get("dual_approval_threshold")
        policy = ApprovalPolicy(
            version=snapshot.get("version") or POLICY_VERSION,
            creator_cannot_approve=bool(snapshot.get("creator_cannot_approve", True)),
            dual_approval_threshold=_Decimal(str(threshold)) if threshold is not None else None,
            dual_approval_currency=snapshot.get("dual_approval_currency") or "CAD",
            required_authentication_strength=snapshot.get("required_authentication_strength") or "dev",
        )
        # rc7: Explicitly validate that the reconstructed policy hash matches
        # the stored policy_hash. If they differ, the snapshot is corrupt.
        stored_hash = getattr(approval, "policy_hash", None)
        if stored_hash is not None:
            computed_hash = policy.policy_hash()
            if computed_hash != stored_hash:
                raise ApprovalPolicyCorrupt(
                    f"policy snapshot hash mismatch: stored={stored_hash[:16]}... "
                    f"computed={computed_hash[:16]}... — approval policy is corrupt"
                )
        return policy
    except ApprovalPolicyCorrupt:
        raise
    except Exception:
        return None


def compute_decision_fingerprint_for_approval(
    repos: Repositories,
    *,
    scope: Scope,
    approval: Approval,
    policy: ApprovalPolicy | None = None,
) -> str:
    """Recompute the decision fingerprint from current authoritative rows.

    This is the executor's precondition path: load the invoice, evidence, and
    latest verification packet hash from persisted state and recompute. If the
    result differs from the approval's stored decision_fingerprint, the approval
    is stale.

    rc6: If `policy` is not supplied, the function reconstructs the policy from
    the snapshot stored on the approval at decision time.

    rc7: For executable approvals, policy_snapshot and policy_hash MUST be
    present. If they are missing, the function raises ApprovalPolicyMissing
    rather than falling back to DEFAULT_POLICY. This removes the legacy
    compatibility escape hatch from the authority path. Callers that want
    legacy fallback (e.g. non-execution paths like diagnostics) can catch
    the exception. The executor must NOT catch it — it must propagate as
    REVALIDATION_REQUIRED.

    rc7: If the reconstructed policy hash does not match the stored hash,
    ApprovalPolicyCorrupt is raised. The executor must treat this as a
    hard failure.
    """
    if policy is None:
        reconstructed = _policy_from_snapshot(approval)
        if reconstructed is None:
            # rc7: No policy snapshot — the approval cannot be safely
            # executed. This is a hard failure for the execution path.
            raise ApprovalPolicyMissing(
                f"approval {approval.approval_id} has no policy_snapshot — "
                "cannot recompute decision fingerprint without the exact "
                "policy in force at decision time (rc7: no DEFAULT_POLICY fallback)"
            )
        policy = reconstructed

    invoice = repos.invoices.get(scope=scope, invoice_id=UUID(approval.subject_id))
    if invoice is None:
        # The invoice the approval was for no longer exists — definitely stale.
        # Use a sentinel that will never match.
        return hashlib.sha256(b"STALE:invoice-missing").hexdigest()

    evidence: list[Evidence] = []
    if approval.evidence_ids:
        evidence = repos.evidence.get_many(
            scope=scope, evidence_ids=[UUID(eid) for eid in approval.evidence_ids]
        )

    packet_meta = repos.approval_packets.latest_meta_for_approval(
        scope=scope, approval_id=UUID(approval.approval_id)
    )
    packet_hash = packet_meta["canonical_hash"] if packet_meta else None

    return compute_decision_fingerprint(
        invoice=invoice, evidence=evidence, approval=approval,
        verification_packet_hash=packet_hash,
        policy=policy,
    )
