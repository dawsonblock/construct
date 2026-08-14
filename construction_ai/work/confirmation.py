"""WorkConfirmationService — does the evidence support a 'work confirmed' check?

The verifier asks a yes/no question with an evidence-backed answer. This service
reads work_confirmations records and reports whether confirmed work exists for a
project/invoice. It never accepts a caller's assertion.
"""
from __future__ import annotations

from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.work.repository import WorkConfirmationRepository


class WorkConfirmationService:
    def __init__(self, repository: WorkConfirmationRepository):
        self.repository = repository

    def is_work_confirmed(self, *, scope: Scope, project_id: UUID | None, invoice_id: UUID | None = None) -> bool:
        """True only when an active, confirmed work record exists."""
        if invoice_id is not None:
            if self.repository.for_invoice(scope=scope, invoice_id=invoice_id):
                return True
        if project_id is not None:
            if self.repository.for_project(scope=scope, project_id=project_id):
                return True
        return False
