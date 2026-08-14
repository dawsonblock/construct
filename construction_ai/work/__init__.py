"""Work completion is evidence, not a request field (item 12).

A client may request that an invoice be evaluated; it cannot declare the physical
work complete. Work completion is an authoritative record written by a human
(superintendent, inspector) or a verified external source (ERP goods receipt,
completed work order). The invoice verifier reads these records.
"""
from __future__ import annotations

from construction_ai.work.confirmation import WorkConfirmationService
from construction_ai.work.models import WorkConfirmation
from construction_ai.work.repository import WorkConfirmationRepository

__all__ = ["WorkConfirmation", "WorkConfirmationRepository", "WorkConfirmationService"]
