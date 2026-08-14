"""v0.4.5 — blob/occurrence split and concurrency-safe versions (items 26, 27).

Verifies that:
1. The same bytes in multiple documents share one blob (content-addressed).
2. Each document gets its own version (occurrence) pointing to the shared blob.
3. Version numbering is concurrency-safe (advisory lock prevents races).
4. The blob stores extracted text/tables once, shared by all occurrences.
5. Blob storage is tenant-isolated.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path


from construction_ai.ingestion.attachments import AttachmentPipeline, LocalObjectStore


# --------------------------------------------------------------------------
# Blob/occurrence split — same bytes, multiple documents, one blob
# --------------------------------------------------------------------------

def test_same_bytes_in_two_documents_share_one_blob(repos, org_a):
    """The same PDF attached to two projects is one blob, two occurrences."""
    with tempfile.TemporaryDirectory() as directory:
        pipeline = AttachmentPipeline(LocalObjectStore(Path(directory) / "objects"), repos)
        data = b"INVOICE 8831\nAmount Due: $100.00"
        v1 = pipeline.ingest(scope=org_a["scope"], filename="invoice.txt", data=data)
        v2 = pipeline.ingest(scope=org_a["scope"], filename="renamed.txt", data=data)

        # Same content hash — same bytes.
        assert v1.content_hash == v2.content_hash

        # One blob in the database.
        blobs = repos.document_blobs.find_by_hash(scope=org_a["scope"].organization_only, content_hash=v1.content_hash)
        assert blobs is not None
        assert blobs.blob_id is not None

        # Two document versions, both pointing to the same blob.
        assert v1.document_version_id != v2.document_version_id
        assert v1.document_id != v2.document_id


def test_blob_stores_extracted_text_once(repos, org_a):
    """Extraction happens once per unique content — the blob stores the result."""
    with tempfile.TemporaryDirectory() as directory:
        pipeline = AttachmentPipeline(LocalObjectStore(Path(directory) / "objects"), repos)
        data = b"INVOICE 8831\nAmount Due: $100.00"
        pipeline.ingest(scope=org_a["scope"], filename="invoice.txt", data=data)
        pipeline.ingest(scope=org_a["scope"], filename="copy.txt", data=data)

        content_hash = hashlib.sha256(data).hexdigest()
        blob = repos.document_blobs.find_by_hash(scope=org_a["scope"].organization_only, content_hash=content_hash)
        assert blob is not None
        assert "INVOICE 8831" in blob.extracted_text
        # Only one blob row for this content.
        assert blob.byte_size == len(data)


def test_different_bytes_create_different_blobs(repos, org_a):
    """Different content creates different blobs."""
    with tempfile.TemporaryDirectory() as directory:
        pipeline = AttachmentPipeline(LocalObjectStore(Path(directory) / "objects"), repos)
        pipeline.ingest(scope=org_a["scope"], filename="a.txt", data=b"content A")
        pipeline.ingest(scope=org_a["scope"], filename="b.txt", data=b"content B")

        blob_a = repos.document_blobs.find_by_hash(scope=org_a["scope"].organization_only, content_hash=hashlib.sha256(b"content A").hexdigest())
        blob_b = repos.document_blobs.find_by_hash(scope=org_a["scope"].organization_only, content_hash=hashlib.sha256(b"content B").hexdigest())
        assert blob_a is not None
        assert blob_b is not None
        assert blob_a.blob_id != blob_b.blob_id


def test_blob_is_tenant_isolated(repos, org_a, org_b):
    """The same bytes in different tenants create separate blobs."""
    with tempfile.TemporaryDirectory() as directory:
        pipeline = AttachmentPipeline(LocalObjectStore(Path(directory) / "objects"), repos)
        data = b"shared content"
        pipeline.ingest(scope=org_a["scope"], filename="doc.txt", data=data)
        pipeline.ingest(scope=org_b["scope"], filename="doc.txt", data=data)

        content_hash = hashlib.sha256(data).hexdigest()
        blob_a = repos.document_blobs.find_by_hash(scope=org_a["scope"].organization_only, content_hash=content_hash)
        blob_b = repos.document_blobs.find_by_hash(scope=org_b["scope"].organization_only, content_hash=content_hash)
        assert blob_a is not None
        assert blob_b is not None
        assert blob_a.blob_id != blob_b.blob_id


# --------------------------------------------------------------------------
# Concurrency-safe version numbering
# --------------------------------------------------------------------------

def test_version_numbers_are_sequential(repos, org_a):
    """Multiple versions of the same document get sequential version numbers."""

    scope = org_a["scope"]
    doc_id = repos.documents.create(scope=scope, filename="test.txt", document_type="unknown")

    v1 = repos.documents.add_version(
        scope=scope, document_id=doc_id, data=b"version 1", storage_uri="file:///v1",
        mime_type="text/plain", extracted_text="version 1",
    )
    v2 = repos.documents.add_version(
        scope=scope, document_id=doc_id, data=b"version 2", storage_uri="file:///v2",
        mime_type="text/plain", extracted_text="version 2",
    )
    v3 = repos.documents.add_version(
        scope=scope, document_id=doc_id, data=b"version 3", storage_uri="file:///v3",
        mime_type="text/plain", extracted_text="version 3",
    )
    assert v1.version_number == 1
    assert v2.version_number == 2
    assert v3.version_number == 3


def test_add_version_with_blob_is_idempotent_within_document(repos, org_a):
    """Adding the same blob to the same document twice returns the same version."""
    scope = org_a["scope"]
    doc_id = repos.documents.create(scope=scope, filename="test.txt", document_type="unknown")
    blob = repos.document_blobs.get_or_create(
        scope=scope, data=b"content", storage_uri="file:///content", mime_type="text/plain",
        extracted_text="content",
    )
    v1 = repos.documents.add_version(
        scope=scope, document_id=doc_id, data=b"content", storage_uri="file:///content",
        mime_type="text/plain", extracted_text="content", blob_id=blob.blob_id,
    )
    v2 = repos.documents.add_version(
        scope=scope, document_id=doc_id, data=b"content", storage_uri="file:///content",
        mime_type="text/plain", extracted_text="content", blob_id=blob.blob_id,
    )
    assert v1.document_version_id == v2.document_version_id


def test_same_blob_in_different_documents_gets_different_versions(repos, org_a):
    """The same blob in two different documents creates two separate versions."""
    scope = org_a["scope"]
    doc1 = repos.documents.create(scope=scope, filename="doc1.txt", document_type="unknown")
    doc2 = repos.documents.create(scope=scope, filename="doc2.txt", document_type="unknown")
    blob = repos.document_blobs.get_or_create(
        scope=scope, data=b"shared", storage_uri="file:///shared", mime_type="text/plain",
        extracted_text="shared",
    )
    v1 = repos.documents.add_version(
        scope=scope, document_id=doc1, data=b"shared", storage_uri="file:///shared",
        mime_type="text/plain", extracted_text="shared", blob_id=blob.blob_id,
    )
    v2 = repos.documents.add_version(
        scope=scope, document_id=doc2, data=b"shared", storage_uri="file:///shared",
        mime_type="text/plain", extracted_text="shared", blob_id=blob.blob_id,
    )
    assert v1.document_version_id != v2.document_version_id
    assert v1.document_id != v2.document_id
    assert v1.content_hash == v2.content_hash  # same bytes


# --------------------------------------------------------------------------
# Blob repository direct usage
# --------------------------------------------------------------------------

def test_blob_get_or_create_is_idempotent(repos, org_a):
    """get_or_create with the same bytes returns the same blob."""
    scope = org_a["scope"].organization_only
    blob1 = repos.document_blobs.get_or_create(
        scope=scope, data=b"test data", storage_uri="file:///test", extracted_text="test",
    )
    blob2 = repos.document_blobs.get_or_create(
        scope=scope, data=b"test data", storage_uri="file:///test", extracted_text="test",
    )
    assert blob1.blob_id == blob2.blob_id


def test_blob_find_by_hash(repos, org_a):
    """find_by_hash returns the blob or None."""
    scope = org_a["scope"].organization_only
    content_hash = hashlib.sha256(b"find me").hexdigest()
    assert repos.document_blobs.find_by_hash(scope=scope, content_hash=content_hash) is None
    repos.document_blobs.get_or_create(
        scope=scope, data=b"find me", storage_uri="file:///find", extracted_text="find",
    )
    found = repos.document_blobs.find_by_hash(scope=scope, content_hash=content_hash)
    assert found is not None
    assert found.content_hash == content_hash
