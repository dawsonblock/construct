"""Project reconstruction as an application service.

Reconstruction reads across most of the repositories. Hanging it off
`ProjectRepository` made that repository a service locator for everything else —
fine while there was one caller, expensive once a reasoning layer starts
reaching for `repos.projects.reconstruct(...)` from everywhere.

The dependencies are declared, not discovered:

    ProjectReconstructor
        ├── ProjectRepository       ├── EntityRepository
        ├── DocumentRepository      ├── RelationshipRepository
        ├── EvidenceRepository      ├── InvoiceRepository
        ├── CompanyRepository       ├── PurchaseOrderRepository
        ├── QuoteRepository         ├── ApprovalRepository
        └── conflict checks
"""
from __future__ import annotations

from uuid import UUID

from construction_ai.persistence.db import Scope, ScopeError
from construction_ai.reconstruction.project_state import ProjectState
from construction_ai.reconstruction.project_state import reconstruct as _reconstruct


class ProjectReconstructor:
    def __init__(
        self,
        *,
        projects,
        companies,
        documents,
        evidence,
        invoices,
        purchase_orders,
        quotes,
        approvals,
        entities,
        relationships,
    ):
        self.projects = projects
        self.companies = companies
        self.documents = documents
        self.evidence = evidence
        self.invoices = invoices
        self.purchase_orders = purchase_orders
        self.quotes = quotes
        self.approvals = approvals
        self.entities = entities
        self.relationships = relationships

    @classmethod
    def from_repositories(cls, repos) -> ProjectReconstructor:
        return cls(
            projects=repos.projects,
            companies=repos.companies,
            documents=repos.documents,
            evidence=repos.evidence,
            invoices=repos.invoices,
            purchase_orders=repos.purchase_orders,
            quotes=repos.quotes,
            approvals=repos.approvals,
            entities=repos.entities,
            relationships=repos.relationships,
        )

    def project(self, *, scope: Scope, project_id: UUID | None = None) -> ProjectState:
        """Rebuild one project deterministically.

        `project_id` may be passed explicitly with an organization-level scope,
        or carried on the scope. Passing both with different values is a bug
        worth failing on rather than silently resolving in one direction.
        """
        if project_id is None:
            project_id = scope.require_project()
        elif scope.project_id is not None and scope.project_id != project_id:
            raise ScopeError(
                f"scope is bound to project {scope.project_id} but reconstruction was asked for {project_id}"
            )
        return _reconstruct(self, scope.for_project(project_id))
