"""Documents and their immutable versions.

A `documents` row is the mutable identity of a thing ("Drawing A3"); a
`document_versions` row is a specific set of bytes. Evidence points at a version,
never at the document — otherwise revising a quote silently re-points every
existing piece of evidence at numbers nobody verified.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository


@dataclass(frozen=True)
class DocumentVersion:
    organization_id: UUID
    document_id: UUID
    document_version_id: UUID
    project_id: UUID | None
    version_number: int
    content_hash: str
    storage_uri: str
    mime_type: str
    extracted_text: str
    revision_label: str | None = None
    extraction_warnings: list[str] | None = None


DOCUMENT_COLUMNS = "organization_id, document_id, project_id, filename, document_type, document_family, current_version, source_id"
VERSION_COLUMNS = (
    "organization_id, document_id, document_version_id, project_id, version_number, revision_label, "
    "content_hash, byte_size, mime_type, storage_uri, extracted_text, extraction_warnings"
)


class DocumentRepository(Repository):
    table = "documents"
    id_column = "document_id"

    def create(
        self,
        *,
        scope: Scope,
        filename: str,
        document_type: str = "unknown",
        document_family: str | None = None,
        source_id: UUID | None = None,
        created_by: str = "system",
    ) -> UUID:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO documents(organization_id, project_id, filename, document_type, document_family, source_id, created_by)
                   VALUES(%s, %s, %s, %s, %s, %s, %s) RETURNING document_id""",
                (scope.organization_id, scope.project_id, filename, document_type, document_family, source_id, created_by),
            )
            return cur.fetchone()[0]

    def get(self, *, scope: Scope, document_id: UUID) -> dict[str, Any] | None:
        return self.get_row(scope=scope, record_id=document_id, columns=DOCUMENT_COLUMNS)

    def list(self, *, scope: Scope) -> list[dict[str, Any]]:
        clause, params = self._tenant_clause(scope)
        return self._fetch_all(
            scope, f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE {clause} ORDER BY created_at", params
        )

    def assign_project(self, *, scope: Scope, document_id: UUID, project_id: UUID) -> bool:
        """Filing a document. The composite FKs re-file its versions by cascade."""
        with self.db.scoped(scope.organization_only) as cur:
            cur.execute(
                "UPDATE documents SET project_id = %s WHERE organization_id = %s AND document_id = %s RETURNING document_id",
                (project_id, scope.organization_id, document_id),
            )
            return cur.fetchone() is not None

    # -- versions -----------------------------------------------------------

    def add_version(
        self,
        *,
        scope: Scope,
        document_id: UUID,
        data: bytes,
        storage_uri: str,
        mime_type: str = "application/octet-stream",
        extracted_text: str = "",
        extraction_warnings: list[str] | None = None,
        tables: list | None = None,
        revision_label: str | None = None,
        created_by: str = "system",
        blob_id: UUID | None = None,
    ) -> DocumentVersion:
        """Content-addressed and idempotent: identical bytes are one version.

        v0.4.5: uses a per-document advisory lock to prevent two concurrent
        uploads from racing on version_number. The blob_id (if provided) links
        this version to a content-addressed document_blobs row — the same bytes
        in multiple documents share one blob.
        """
        from psycopg.types.json import Jsonb

        content_hash = hashlib.sha256(data).hexdigest()
        with self.db.scoped(scope.organization_only) as cur:
            # If a blob_id is provided, check if this blob already has a version
            # in this document — if so, return it (idempotent within a document).
            if blob_id is not None:
                cur.execute(
                    f"SELECT {VERSION_COLUMNS} FROM document_versions "
                    "WHERE organization_id = %s AND document_id = %s AND blob_id = %s",
                    (scope.organization_id, document_id, blob_id),
                )
                existing = cur.fetchone()
                if existing:
                    columns = [c.name for c in cur.description]
                    return _to_version(dict(zip(columns, existing, strict=True)))
            else:
                # Backward-compatible path: check by content_hash.
                cur.execute(
                    f"SELECT {VERSION_COLUMNS} FROM document_versions WHERE organization_id = %s AND content_hash = %s",
                    (scope.organization_id, content_hash),
                )
                existing = cur.fetchone()
                if existing:
                    columns = [c.name for c in cur.description]
                    return _to_version(dict(zip(columns, existing, strict=True)))

            # Concurrency-safe version numbering: lock the document's version
            # sequence within this transaction. Two concurrent uploads will
            # serialize — one gets version N, the other gets version N+1.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"doc_version:{scope.organization_id}:{document_id}",),
            )

            cur.execute(
                """SELECT d.project_id, COALESCE(MAX(dv.version_number), 0) + 1
                   FROM documents d LEFT JOIN document_versions dv
                     ON dv.organization_id = d.organization_id AND dv.document_id = d.document_id
                   WHERE d.organization_id = %s AND d.document_id = %s
                   GROUP BY d.project_id""",
                (scope.organization_id, document_id),
            )
            head = cur.fetchone()
            if head is None:
                raise LookupError(f"document {document_id} not found in this organization")
            project_id, next_version = head

            cur.execute(
                f"""INSERT INTO document_versions(organization_id, document_id, project_id, version_number,
                        revision_label, content_hash, byte_size, mime_type, storage_uri, extracted_text,
                        extraction_warnings, tables, blob_id, created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING {VERSION_COLUMNS}""",
                (
                    scope.organization_id, document_id, project_id, next_version, revision_label,
                    content_hash, len(data), mime_type, storage_uri, extracted_text,
                    Jsonb(extraction_warnings or []), Jsonb(tables or []), blob_id, created_by,
                ),
            )
            columns = [c.name for c in cur.description]
            version = _to_version(dict(zip(columns, cur.fetchone(), strict=True)))
            cur.execute(
                "UPDATE documents SET current_version = %s WHERE organization_id = %s AND document_id = %s",
                (next_version, scope.organization_id, document_id),
            )
            return version

    def find_version_by_hash(self, *, scope: Scope, content_hash: str) -> DocumentVersion | None:
        """Content-addressed lookup — the same bytes are never stored twice."""
        row = self._fetch_one(
            scope,
            f"SELECT {VERSION_COLUMNS} FROM document_versions WHERE organization_id = %s AND content_hash = %s",
            [scope.organization_id, content_hash],
        )
        return _to_version(row) if row else None

    def get_version(self, *, scope: Scope, document_version_id: UUID) -> DocumentVersion | None:
        clause, params = self._tenant_clause(scope)
        row = self._fetch_one(
            scope,
            f"SELECT {VERSION_COLUMNS} FROM document_versions WHERE {clause} AND document_version_id = %s",
            [*params, document_version_id],
        )
        return _to_version(row) if row else None

    def versions_for_project(self, *, scope: Scope) -> list[DocumentVersion]:
        """Every version of every document on this project, ordered stably."""
        project_id = scope.require_project()
        rows = self._fetch_all(
            scope,
            f"""SELECT {VERSION_COLUMNS} FROM document_versions
                WHERE organization_id = %s AND project_id = %s
                ORDER BY document_id, version_number""",
            [scope.organization_id, project_id],
        )
        return [_to_version(row) for row in rows]

    def versions(self, *, scope: Scope, document_id: UUID) -> list[DocumentVersion]:
        clause, params = self._tenant_clause(scope)
        rows = self._fetch_all(
            scope,
            f"SELECT {VERSION_COLUMNS} FROM document_versions WHERE {clause} AND document_id = %s ORDER BY version_number",
            [*params, document_id],
        )
        return [_to_version(row) for row in rows]


def _to_version(row: dict[str, Any]) -> DocumentVersion:
    return DocumentVersion(
        organization_id=row["organization_id"],
        document_id=row["document_id"],
        document_version_id=row["document_version_id"],
        project_id=row.get("project_id"),
        version_number=row["version_number"],
        content_hash=row["content_hash"],
        storage_uri=row["storage_uri"],
        mime_type=row["mime_type"],
        extracted_text=row.get("extracted_text") or "",
        revision_label=row.get("revision_label"),
        extraction_warnings=list(row.get("extraction_warnings") or []),
    )
