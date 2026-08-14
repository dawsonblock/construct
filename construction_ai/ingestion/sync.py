from __future__ import annotations

from construction_ai.ingestion.attachments import AttachmentPipeline
from construction_ai.persistence.db import Scope


class MailSyncService:
    """Fetches source messages and attachments into scoped, immutable storage.

    The connector is told which organization it is fetching for, but that string
    is only used to stamp the normalized record. What actually decides where the
    data lands is the `Scope` the caller was authenticated into.
    """

    def __init__(self, *, connector, repositories, attachment_pipeline: AttachmentPipeline):
        self.connector = connector
        self.repos = repositories
        self.attachments = attachment_pipeline

    def sync_message(self, *, scope: Scope, message_id: str) -> dict:
        communication, descriptors = self.connector.fetch_message(message_id, str(scope.organization_id))
        record, created = self.repos.communications.ingest(scope=scope, communication=communication)
        if not created:
            return {"status": "duplicate", "communication_id": record.communication_id, "documents": []}

        from uuid import UUID

        source_id = UUID(record.communication_id)
        versions = []
        for descriptor in descriptors:
            data = self.connector.fetch_attachment(message_id, descriptor["attachment_id"])
            versions.append(
                self.attachments.ingest(
                    scope=scope, filename=descriptor["filename"], data=data, source_id=source_id
                )
            )
        return {
            "status": "accepted",
            "communication_id": record.communication_id,
            "documents": [str(v.document_id) for v in versions],
        }
