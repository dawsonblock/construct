"""Communications.

Ingestion is idempotent per tenant on two keys: the source's own message id, and
a content hash. The same message delivered twice — re-fetched, or forwarded back
through a webhook — becomes one canonical communication.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from construction_ai.domain.models import Communication
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository

COMMUNICATION_COLUMNS = (
    "organization_id, communication_id, project_id, source, external_id, thread_id, sender, "
    "recipients, subject, body, content_hash, received_at"
)


def _to_communication(row: dict[str, Any]) -> Communication:
    return Communication(
        communication_id=str(row["communication_id"]),
        organization_id=str(row["organization_id"]),
        source=row["source"],
        sender=row["sender"],
        recipients=list(row.get("recipients") or []),
        subject=row["subject"],
        body=row["body"],
        received_at=row["received_at"],
        thread_id=row.get("thread_id"),
        attachments=[],
        raw_hash=row["content_hash"],
    )


class CommunicationRepository(Repository):
    table = "communications"
    id_column = "communication_id"

    def ingest(self, *, scope: Scope, communication: Communication) -> tuple[Communication, bool]:
        """Returns (record, created). `created=False` means it was already here."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""INSERT INTO communications(organization_id, project_id, source, external_id, thread_id,
                        sender, recipients, subject, body, content_hash, received_at, created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (organization_id, source, external_id) DO NOTHING
                    RETURNING {COMMUNICATION_COLUMNS}""",
                (
                    scope.organization_id, scope.project_id, communication.source, communication.communication_id,
                    communication.thread_id, communication.sender, communication.recipients,
                    communication.subject, communication.body, communication.raw_hash,
                    communication.received_at, "ingestion",
                ),
            )
            row = cur.fetchone()
            if row is not None:
                columns = [c.name for c in cur.description]
                return _to_communication(dict(zip(columns, row, strict=True))), True

            cur.execute(
                f"SELECT {COMMUNICATION_COLUMNS} FROM communications WHERE organization_id = %s AND source = %s AND external_id = %s",
                (scope.organization_id, communication.source, communication.communication_id),
            )
            columns = [c.name for c in cur.description]
            return _to_communication(dict(zip(columns, cur.fetchone(), strict=True))), False

    def get(self, *, scope: Scope, communication_id: UUID) -> Communication | None:
        row = self.get_row(scope=scope, record_id=communication_id, columns=COMMUNICATION_COLUMNS)
        return _to_communication(row) if row else None

    def for_thread(self, *, scope: Scope, thread_id: str) -> list[Communication]:
        clause, params = self._tenant_clause(scope)
        rows = self._fetch_all(
            scope,
            f"SELECT {COMMUNICATION_COLUMNS} FROM communications WHERE {clause} AND thread_id = %s ORDER BY received_at",
            [*params, thread_id],
        )
        return [_to_communication(row) for row in rows]
