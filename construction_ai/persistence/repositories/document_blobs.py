"""Content-addressed document blobs — the immutable byte storage layer.

A document_blob is one set of bytes, content-addressed by SHA-256 within a
tenant. Multiple document_versions can point to the same blob — the same PDF
attached to two projects is one blob, two occurrences.

Blob storage is append-only: once a blob is created, its content cannot
change (the content_hash is the identity). The extracted text and tables are
stored on the blob, not the version, so extraction happens once per unique
content.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository


@dataclass(frozen=True)
class DocumentBlob:
    blob_id: UUID
    organization_id: UUID
    content_hash: str
    byte_size: int
    mime_type: str
    storage_uri: str
    extracted_text: str
    extraction_warnings: list[str]
    tables: list
    created_at: datetime


BLOB_COLUMNS = (
    "organization_id, blob_id, content_hash, byte_size, mime_type, storage_uri, "
    "extracted_text, extraction_warnings, tables, created_at"
)


class DocumentBlobRepository(Repository):
    table = "document_blobs"
    id_column = "blob_id"

    def get_or_create(
        self,
        *,
        scope: Scope,
        data: bytes,
        storage_uri: str,
        mime_type: str = "application/octet-stream",
        extracted_text: str = "",
        extraction_warnings: list[str] | None = None,
        tables: list | None = None,
    ) -> DocumentBlob:
        """Content-addressed and idempotent: identical bytes are one blob."""
        from psycopg.types.json import Jsonb

        from construction_ai.persistence.serialization import dumps

        content_hash = hashlib.sha256(data).hexdigest()
        with self.db.scoped(scope.organization_only) as cur:
            # Check for an existing blob with this content hash.
            cur.execute(
                f"SELECT {BLOB_COLUMNS} FROM document_blobs "
                "WHERE organization_id = %s AND content_hash = %s",
                (scope.organization_id, content_hash),
            )
            row = cur.fetchone()
            if row is not None:
                columns = [c.name for c in cur.description]
                return _to_blob(dict(zip(columns, row, strict=True)))

            cur.execute(
                f"""INSERT INTO document_blobs(organization_id, content_hash, byte_size, mime_type,
                       storage_uri, extracted_text, extraction_warnings, tables)
                   VALUES(%s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING {BLOB_COLUMNS}""",
                (
                    scope.organization_id,
                    content_hash,
                    len(data),
                    mime_type,
                    storage_uri,
                    extracted_text,
                    Jsonb(extraction_warnings or [], dumps=dumps),
                    Jsonb(tables or [], dumps=dumps),
                ),
            )
            columns = [c.name for c in cur.description]
            return _to_blob(dict(zip(columns, cur.fetchone(), strict=True)))

    def find_by_hash(self, *, scope: Scope, content_hash: str) -> DocumentBlob | None:
        row = self._fetch_one(
            scope,
            f"SELECT {BLOB_COLUMNS} FROM document_blobs WHERE organization_id = %s AND content_hash = %s",
            [scope.organization_id, content_hash],
        )
        return _to_blob(row) if row else None

    def get(self, *, scope: Scope, blob_id: UUID) -> DocumentBlob | None:
        row = self._fetch_one(
            scope,
            f"SELECT {BLOB_COLUMNS} FROM document_blobs WHERE organization_id = %s AND blob_id = %s",
            [scope.organization_id, blob_id],
        )
        return _to_blob(row) if row else None


def _to_blob(row: dict[str, Any]) -> DocumentBlob:
    return DocumentBlob(
        blob_id=row["blob_id"],
        organization_id=row["organization_id"],
        content_hash=row["content_hash"],
        byte_size=row.get("byte_size") or 0,
        mime_type=row.get("mime_type") or "application/octet-stream",
        storage_uri=row["storage_uri"],
        extracted_text=row.get("extracted_text") or "",
        extraction_warnings=list(row.get("extraction_warnings") or []),
        tables=list(row.get("tables") or []),
        created_at=row.get("created_at"),
    )
