from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class WorkConfirmation:
    confirmation_id: UUID
    organization_id: UUID
    project_id: UUID | None
    scope_id: UUID | None
    invoice_id: UUID | None
    confirmed_by_user_id: UUID | None
    confirmation_type: str
    status: str
    percent_complete: float | None
    occurred_at: datetime
    sov_item_id: UUID | None = None  # Phase 15: scope-specific binding
