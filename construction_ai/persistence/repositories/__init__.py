"""Scoped repositories.

Import `Repositories` and reach everything from it. There is no module-level
convenience function that takes an id without a scope, and there should never be
one — see docs/TENANCY.md §4.
"""
from __future__ import annotations

from functools import cached_property

from construction_ai.persistence.db import Database, Scope, ScopeError
from construction_ai.persistence.repositories.approvals import ApprovalPacketRepository, ApprovalRepository
from construction_ai.persistence.repositories.audit import AuditRepository
from construction_ai.persistence.repositories.auth import (
    ApprovalAuthorityRepository,
    ApprovalDecisionRepository,
    SessionRepository,
    UserRepository,
)
from construction_ai.persistence.repositories.communications import CommunicationRepository
from construction_ai.persistence.repositories.commercial import InvoiceRepository, PurchaseOrderRepository, QuoteRepository
from construction_ai.persistence.repositories.documents import DocumentRepository
from construction_ai.persistence.repositories.evidence import EvidenceRepository
from construction_ai.persistence.repositories.graph import EntityRepository, RelationshipRepository
from construction_ai.persistence.repositories.jobs import JobRepository
from construction_ai.persistence.repositories.organizations import OrganizationRepository
from construction_ai.persistence.repositories.projects import CompanyRepository, ProjectRepository

__all__ = [
    "ApprovalAuthorityRepository",
    "ApprovalDecisionRepository",
    "ApprovalPacketRepository",
    "ApprovalRepository",
    "AuditRepository",
    "CommunicationRepository",
    "CompanyRepository",
    "Database",
    "DocumentRepository",
    "EntityRepository",
    "EvidenceRepository",
    "InvoiceRepository",
    "JobRepository",
    "OrganizationRepository",
    "ProjectRepository",
    "RelationshipRepository",
    "PurchaseOrderRepository",
    "QuoteRepository",
    "Repositories",
    "Scope",
    "ScopeError",
    "SessionRepository",
    "UserRepository",
]


class Repositories:
    """Every repository over one database handle."""

    def __init__(self, db: Database):
        self.db = db

    @classmethod
    def from_env(cls) -> Repositories:
        return cls(Database.from_env())

    @cached_property
    def organizations(self) -> OrganizationRepository:
        return OrganizationRepository(self.db)

    @cached_property
    def projects(self) -> ProjectRepository:
        return ProjectRepository(self.db)

    @cached_property
    def companies(self) -> CompanyRepository:
        return CompanyRepository(self.db)

    @cached_property
    def communications(self) -> CommunicationRepository:
        return CommunicationRepository(self.db)

    @cached_property
    def documents(self) -> DocumentRepository:
        return DocumentRepository(self.db)

    @cached_property
    def evidence(self) -> EvidenceRepository:
        return EvidenceRepository(self.db)

    @cached_property
    def entities(self) -> EntityRepository:
        return EntityRepository(self.db)

    @cached_property
    def relationships(self) -> RelationshipRepository:
        return RelationshipRepository(self.db)

    @cached_property
    def purchase_orders(self) -> PurchaseOrderRepository:
        return PurchaseOrderRepository(self.db)

    @cached_property
    def quotes(self) -> QuoteRepository:
        return QuoteRepository(self.db)

    @cached_property
    def invoices(self) -> InvoiceRepository:
        return InvoiceRepository(self.db)

    @cached_property
    def approvals(self) -> ApprovalRepository:
        return ApprovalRepository(self.db)

    @cached_property
    def approval_packets(self) -> ApprovalPacketRepository:
        return ApprovalPacketRepository(self.db)

    @cached_property
    def jobs(self) -> JobRepository:
        return JobRepository(self.db)

    @cached_property
    def audit(self) -> AuditRepository:
        return AuditRepository(self.db)

    @cached_property
    def users(self) -> UserRepository:
        return UserRepository(self.db)

    @cached_property
    def authorities(self) -> ApprovalAuthorityRepository:
        return ApprovalAuthorityRepository(self.db)

    @cached_property
    def sessions(self) -> SessionRepository:
        return SessionRepository(self.db)

    @cached_property
    def approval_decisions(self) -> ApprovalDecisionRepository:
        return ApprovalDecisionRepository(self.db)

    @cached_property
    def work_confirmations(self):
        from construction_ai.work.repository import WorkConfirmationRepository

        return WorkConfirmationRepository(self.db)

    @cached_property
    def reconstruction(self):
        """Cross-repository application service — not a repository itself."""
        from construction_ai.reconstruction.service import ProjectReconstructor

        return ProjectReconstructor.from_repositories(self)
