from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    HELD = "held"
    REJECTED = "rejected"


class CheckStatus(str, Enum):
    """Tri-state (five-state) verification outcome.

    The v0.3 verifier used booleans, so a check that could not be evaluated read
    as `True` and silently passed. The invariant `MissingRequiredEvidence !=>
    PASS` requires a real status: UNAVAILABLE means the check could not run, not
    that it succeeded.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class DecisionAction(str, Enum):
    RETRIEVE = "RETRIEVE"
    VERIFY = "VERIFY"
    COMPARE = "COMPARE"
    CALCULATE = "CALCULATE"
    ASK_HUMAN = "ASK_HUMAN"
    CREATE_DRAFT = "CREATE_DRAFT"
    UPDATE_STATE = "UPDATE_STATE"
    ESCALATE = "ESCALATE"
    STOP = "STOP"


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source_type: str
    source_id: str
    field: str
    value: Any
    confidence: float
    authority: float = 0.5
    observed_at: datetime = field(default_factory=utcnow)
    extractor: str = "deterministic"
    organization_id: str = "ORG-1"
    source_version_id: Optional[str] = None
    project_id: Optional[str] = None
    subject_type: Optional[str] = None
    subject_id: Optional[str] = None


@dataclass
class Person:
    person_id: str
    organization_id: str
    name: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    company_id: Optional[str] = None
    role: Optional[str] = None
    active_projects: list[str] = field(default_factory=list)
    reference: Optional[str] = None


@dataclass
class Company:
    company_id: str
    organization_id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    type: str = "vendor"
    contacts: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    erp_supplier_id: Optional[str] = None
    reference: Optional[str] = None
    tax_id: Optional[str] = None


@dataclass
class Project:
    project_id: str
    organization_id: str
    name: str
    address: Optional[str] = None
    status: str = "active"
    client_id: Optional[str] = None
    project_manager_id: Optional[str] = None
    company_ids: list[str] = field(default_factory=list)
    identifiers: dict[str, list[str]] = field(default_factory=dict)
    reference: Optional[str] = None


@dataclass
class Task:
    task_id: str
    organization_id: str
    project_id: str
    name: str
    status: str = "open"
    assigned_company_id: Optional[str] = None
    assigned_person_id: Optional[str] = None
    due_date: Optional[str] = None


@dataclass
class Communication:
    communication_id: str
    organization_id: str
    source: str
    sender: str
    recipients: list[str]
    subject: str
    body: str
    received_at: datetime
    thread_id: Optional[str] = None
    attachments: list[str] = field(default_factory=list)
    raw_hash: Optional[str] = None


@dataclass
class Document:
    document_id: str
    organization_id: str
    filename: str
    sha256: str
    mime_type: str
    document_type: str
    source_id: Optional[str] = None
    storage_uri: Optional[str] = None
    text: str = ""
    tables: list[list[list[str]]] = field(default_factory=list)
    extraction_warnings: list[str] = field(default_factory=list)


@dataclass
class Invoice:
    invoice_id: str
    organization_id: str
    invoice_number: str
    vendor_name: str
    total: Optional[float] = None
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    currency: str = "CAD"
    po_number: Optional[str] = None
    quote_number: Optional[str] = None
    project_id: Optional[str] = None
    source_id: Optional[str] = None
    vendor_company_id: Optional[str] = None
    reference: Optional[str] = None
    source_version_id: Optional[str] = None
    invoice_date: Optional[Any] = None


@dataclass
class PurchaseOrder:
    po_id: str
    organization_id: str
    po_number: str
    project_id: Optional[str] = None
    vendor_company_id: Optional[str] = None
    amount: Optional[float] = None
    quote_number: Optional[str] = None
    reference: Optional[str] = None
    ordered_on: Optional[Any] = None
    currency: str = "CAD"


@dataclass
class Quote:
    quote_id: str
    organization_id: str
    quote_number: str
    project_id: Optional[str] = None
    vendor_company_id: Optional[str] = None
    amount: Optional[float] = None
    approved: bool = False
    reference: Optional[str] = None
    currency: str = "CAD"
    reference: Optional[str] = None


@dataclass
class VerificationResult:
    subject_id: str
    checks: dict[str, CheckStatus]
    exceptions: list[str]
    evidence_ids: list[str]
    #: Per-check provenance: observed/expected values, evidence ids, verifier
    #: version. The approval packet renders this so a human can see *why* a check
    #: passed, not just that it did.
    check_details: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """PASS only when every required check is PASS. Missing data never passes."""
        return all(status == CheckStatus.PASS for status in self.checks.values()) and not self.exceptions


@dataclass
class Approval:
    approval_id: str
    organization_id: str
    type: str
    subject_id: str
    recommended_action: str
    amount: Optional[float] = None
    exceptions: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    status: ApprovalStatus = ApprovalStatus.PENDING
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    reference: Optional[str] = None
    project_id: Optional[str] = None
    currency: str = "CAD"
    requested_by: str = "ai"
    state_fingerprint: Optional[str] = None  # v0.5.0-rc1 (item 48)
    quorum_threshold: int = 1  # v0.5.0-rc3 (Phase 7)
    decision_fingerprint: Optional[str] = None  # v0.5.0-rc3 (Phase 6)


@dataclass
class ApprovalPacket:
    packet_id: str
    approval: Approval
    subject: dict[str, Any]
    verification: dict[str, Any]
    evidence: list[Evidence]
    project_candidates: list[dict[str, Any]] = field(default_factory=list)
    related_records: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def serializable(self) -> dict[str, Any]:
        data=asdict(self)
        data["approval"]["status"] = self.approval.status.value
        return data


@dataclass
class DecisionState:
    goal: str
    project_id: Optional[str] = None
    known_facts: dict[str, Any] = field(default_factory=dict)
    missing_information: list[str] = field(default_factory=list)
    retrieved_evidence: list[str] = field(default_factory=list)
    completed_checks: list[str] = field(default_factory=list)
    next_action: Optional[DecisionAction] = None
    step_count: int = 0
    max_steps: int = 12

    def serializable(self) -> dict[str, Any]:
        data = asdict(self)
        if self.next_action:
            data["next_action"] = self.next_action.value
        return data


# -- Schedule of values (Phases 15, 16, 17) ---------------------------------
# Money is carried as Decimal so the progress-billing math never crosses a float
# boundary before the ERP serialization edge.

@dataclass(frozen=True)
class Contract:
    contract_id: str
    organization_id: str
    project_id: str
    company_id: str
    reference: str
    name: str = ""
    base_contract_value: Any = None  # Decimal
    currency: str = "CAD"
    status: str = "active"


@dataclass(frozen=True)
class SOVItem:
    sov_item_id: str
    organization_id: str
    contract_id: str
    reference: str
    name: str
    base_value: Any = None  # Decimal
    currency: str = "CAD"
    sort_order: int = 0


@dataclass(frozen=True)
class ChangeOrder:
    change_order_id: str
    organization_id: str
    contract_id: str
    reference: str
    name: str = ""
    amount: Any = None  # Decimal, signed
    currency: str = "CAD"
    status: str = "approved"


@dataclass(frozen=True)
class InvoiceAllocation:
    allocation_id: str
    organization_id: str
    invoice_id: str
    sov_item_id: str
    amount: Any = None  # Decimal
    currency: str = "CAD"


@dataclass(frozen=True)
class ProgressBillingResult:
    """Per-SOV-item progress-billing evaluation (Phase 16)."""
    sov_item_id: str
    adjusted_contract_value: Any  # Decimal
    verified_percent_complete: Any  # Decimal 0..100
    earned_value: Any  # Decimal
    previously_approved_billing: Any  # Decimal
    retainage: Any  # Decimal
    current_billable: Any  # Decimal
    invoice_amount: Any  # Decimal
    overbilled: bool
    currency: str = "CAD"
