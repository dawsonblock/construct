from __future__ import annotations
import hashlib, os, re, tempfile
from dataclasses import dataclass
from pathlib import Path
from construction_ai.documents.extract import extract_document


def _safe_org(organization_id: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]+','_',organization_id).strip('._') or 'org'


class LocalObjectStore:
    def __init__(self, root: str|Path='object_store'): self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
    def key(self, organization_id: str, filename: str, digest: str) -> str:
        return f'{_safe_org(organization_id)}/{digest[:2]}/{digest}/{Path(filename).name}'
    def put(self, organization_id: str, filename: str, data: bytes) -> str:
        digest=hashlib.sha256(data).hexdigest()
        out=self.root/self.key(organization_id,filename,digest)
        out.parent.mkdir(parents=True,exist_ok=True); out.write_bytes(data)
        return out.resolve().as_uri()
    def get(self, uri: str) -> bytes:
        return Path(uri.removeprefix('file://')).read_bytes()


class S3ObjectStore:
    """S3/MinIO-backed immutable document storage.

    Objects are content-addressed, so a repeated upload of identical bytes lands
    on the same key. Returns an s3:// URI.
    """
    def __init__(self, bucket: str, *, endpoint_url: str|None=None, region: str|None=None, client=None):
        self.bucket=bucket
        if client is not None:
            self.client=client
        else:
            import boto3
            self.client=boto3.client('s3',endpoint_url=endpoint_url,region_name=region or 'us-east-1')
        self._ensure_bucket()

    def _ensure_bucket(self):
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except Exception:
                # Another process may have won the race; a genuine failure will
                # surface on the first put rather than being swallowed here.
                pass

    def key(self, organization_id: str, filename: str, digest: str) -> str:
        return f'{_safe_org(organization_id)}/{digest[:2]}/{digest}/{Path(filename).name}'

    def put(self, organization_id: str, filename: str, data: bytes) -> str:
        digest=hashlib.sha256(data).hexdigest()
        key=self.key(organization_id,filename,digest)
        self.client.put_object(Bucket=self.bucket,Key=key,Body=data)
        return f's3://{self.bucket}/{key}'

    def get(self, uri: str) -> bytes:
        key=uri.removeprefix(f's3://{self.bucket}/')
        return self.client.get_object(Bucket=self.bucket,Key=key)['Body'].read()


def create_object_store():
    """Backend selected by OBJECT_STORE_BACKEND: 'local' (default) or 's3'."""
    backend=os.getenv('OBJECT_STORE_BACKEND','local').strip().lower()
    if backend=='s3':
        bucket=os.getenv('S3_BUCKET','').strip()
        if not bucket:
            raise RuntimeError('OBJECT_STORE_BACKEND=s3 requires S3_BUCKET')
        return S3ObjectStore(bucket,endpoint_url=os.getenv('S3_ENDPOINT_URL') or None,region=os.getenv('AWS_DEFAULT_REGION'))
    if backend!='local':
        raise RuntimeError(f'unknown OBJECT_STORE_BACKEND: {backend!r}')
    return LocalObjectStore(os.getenv('OBJECT_STORE_ROOT','object_store'))


@dataclass
class AttachmentPipeline:
    """Attachment bytes → immutable object storage → a document version row.

    Idempotent on content: the same bytes ingested twice yield the same version,
    within the tenant that ingested them. Two organizations that receive the same
    PDF get two independent versions — content addressing is scoped, because a
    shared address space would be a cross-tenant existence oracle.
    """

    storage: LocalObjectStore | S3ObjectStore
    repositories: object

    def ingest(self, *, scope, filename: str, data: bytes, source_id=None, document_type: str | None = None):
        digest = hashlib.sha256(data).hexdigest()
        documents = self.repositories.documents
        blobs = self.repositories.document_blobs

        # v0.4.5: check for an existing blob first — the same bytes in multiple
        # documents share one blob. If the blob exists, we still create a new
        # document + version pointing to it (a new occurrence).
        existing_blob = blobs.find_by_hash(scope=scope.organization_only, content_hash=digest)

        if existing_blob is not None:
            # The blob already exists — reuse its extracted text/tables.
            uri = existing_blob.storage_uri
            extracted_text = existing_blob.extracted_text
            extraction_warnings = existing_blob.extraction_warnings
            extracted_tables = existing_blob.tables
            mime_type = existing_blob.mime_type
            # Classify from the existing text if no explicit type was given.
            if document_type is None:
                from construction_ai.documents.extract import classify
                document_type = classify(filename, extracted_text)
        else:
            # Extraction reads from a local path; object storage may be remote.
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / Path(filename).name
                path.write_bytes(data)
                extracted = extract_document(path)

            uri = self.storage.put(str(scope.organization_id), filename, data)
            extracted_text = extracted.text
            extraction_warnings = extracted.warnings
            extracted_tables = extracted.tables
            mime_type = extracted.mime_type
            if document_type is None:
                document_type = extracted.document_type

        # Create or get the blob (idempotent on content hash).
        blob = blobs.get_or_create(
            scope=scope,
            data=data,
            storage_uri=uri,
            mime_type=mime_type,
            extracted_text=extracted_text,
            extraction_warnings=extraction_warnings,
            tables=extracted_tables,
        )

        document_id = documents.create(
            scope=scope,
            filename=filename,
            document_type=document_type,
            source_id=source_id,
            created_by="ingestion",
        )
        return documents.add_version(
            scope=scope,
            document_id=document_id,
            data=data,
            storage_uri=uri,
            mime_type=mime_type,
            extracted_text=extracted_text,
            extraction_warnings=extraction_warnings,
            tables=extracted_tables,
            created_by="ingestion",
            blob_id=blob.blob_id,
        )
