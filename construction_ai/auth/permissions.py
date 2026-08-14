"""The permission catalog. The authoritative list lives in migration 007; this
module mirrors it so policy code can reference permissions by name and tests can
assert the DB catalog matches the code catalog."""
from __future__ import annotations

from enum import Enum


class Permission(str, Enum):
    INVOICE_READ = "invoice.read"
    INVOICE_REVIEW = "invoice.review"
    INVOICE_HOLD = "invoice.hold"
    INVOICE_APPROVE = "invoice.approve"
    INVOICE_REJECT = "invoice.reject"
    INVOICE_SUBMIT_ERP = "invoice.submit_erp"
    RELATIONSHIP_REVIEW = "relationship.review"
    RELATIONSHIP_PROMOTE = "relationship.promote"
    PROJECT_ADMIN = "project.admin"
    ORGANIZATION_ADMIN = "organization.admin"
    POLICY_ADMIN = "policy.admin"


ALL_PERMISSIONS: frozenset[str] = frozenset(p.value for p in Permission)

# Permissions that decide an invoice's financial fate. `invoice.review` is
# deliberately excluded: working the review queue is not itself an approval.
APPROVAL_PERMISSIONS: frozenset[str] = frozenset(
    {Permission.INVOICE_APPROVE.value, Permission.INVOICE_REJECT.value, Permission.INVOICE_HOLD.value}
)
