"""Approval policy — configurable separation of duties and amount thresholds.

The full versioned-policy engine lands in v0.4.6 (item 16). v0.4.1 needs the
separation-of-duties rules now, so a small, explicit policy object is the
vehicle. It is deliberately a value type: every decision records the policy
version string on the `approval_decisions` row, so historical decisions stay
explainable after the rules change.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

POLICY_VERSION = "authority:v1"


@dataclass(frozen=True)
class ApprovalPolicy:
    """Separation of duties and amount gating for invoice approvals."""

    version: str = POLICY_VERSION
    # A human may never approve a request they originated.
    creator_cannot_approve: bool = True
    # Above this amount (in `dual_approval_currency`), a second *distinct*
    # approver is required. None disables dual approval.
    dual_approval_threshold: Decimal | None = None
    dual_approval_currency: str = "CAD"
    # Authentication strength required for any approval. 'dev' is the weakest;
    # production policy requires 'oidc' or stronger.
    required_authentication_strength: str = "dev"

    def requires_second_distinct_approver(self, amount: Decimal | float | int | None, currency: str) -> bool:
        if self.dual_approval_threshold is None or amount is None:
            return False
        if currency != self.dual_approval_currency:
            return False
        return Decimal(str(amount)) >= self.dual_approval_threshold

    def policy_hash(self) -> str:
        """Compute the deterministic cryptographic digest of this exact policy configuration."""
        import hashlib
        import json

        payload = {
            "version": self.version,
            "creator_cannot_approve": self.creator_cannot_approve,
            "dual_approval_threshold": str(self.dual_approval_threshold) if self.dual_approval_threshold is not None else None,
            "dual_approval_currency": self.dual_approval_currency,
            "required_authentication_strength": self.required_authentication_strength,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


DEFAULT_POLICY = ApprovalPolicy()
