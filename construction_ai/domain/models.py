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


@dataclass
class VerificationResult:
    subject_id: str
    checks: dict[str, bool]
    exceptions: list[str]
    evidence_ids: list[str]

    @property
    def passed(self) -> bool:
        return all(self.checks.values()) and not self.exceptions


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
