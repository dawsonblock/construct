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

from typing import Any
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
        decisions=None,
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
        self._decisions = decisions

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
            decisions=repos.decisions,
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

    def replay(self, *, scope: Scope, project_id: UUID | None = None) -> dict[str, Any]:
        """Replay reconstruction and verify decision validity (item 35).

        Reconstructs the project from current state, computes the fingerprint,
        and compares against stored decision state_fingerprints. Returns:
        - state_fingerprint: the current ProjectState fingerprint
        - decisions_total: total decisions for this project
        - decisions_valid: decisions whose state_fingerprint matches current
        - decisions_stale: decisions whose state_fingerprint differs (state drifted)
        - decisions_without_fingerprint: decisions made before fingerprinting
        """

        state = self.project(scope=scope, project_id=project_id)
        current_fp = state.fingerprint()

        project_scope = scope.for_project(project_id) if project_id else scope
        decisions = self._decisions.for_project(scope=project_scope)

        valid = [d for d in decisions if d.state_fingerprint == current_fp]
        stale = [d for d in decisions if d.state_fingerprint is not None and d.state_fingerprint != current_fp]
        no_fp = [d for d in decisions if d.state_fingerprint is None]

        return {
            "state_fingerprint": current_fp,
            "decisions_total": len(decisions),
            "decisions_valid": len(valid),
            "decisions_stale": len(stale),
            "decisions_without_fingerprint": len(no_fp),
            "stale_decision_ids": [str(d.decision_id) for d in stale],
        }
