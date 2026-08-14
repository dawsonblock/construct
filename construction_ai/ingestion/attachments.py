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
        existing = documents.find_version_by_hash(scope=scope.organization_only, content_hash=digest)
        if existing is not None:
            return existing

        # Extraction reads from a local path; object storage may be remote, so the
        # bytes we already hold are written to a scratch file with the original
        # suffix (extract_document dispatches on it).
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / Path(filename).name
            path.write_bytes(data)
            extracted = extract_document(path)

        uri = self.storage.put(str(scope.organization_id), filename, data)
        document_id = documents.create(
            scope=scope,
            filename=filename,
            document_type=document_type or extracted.document_type,
            source_id=source_id,
            created_by="ingestion",
        )
        return documents.add_version(
            scope=scope,
            document_id=document_id,
            data=data,
            storage_uri=uri,
            mime_type=extracted.mime_type,
            extracted_text=extracted.text,
            extraction_warnings=extracted.warnings,
            tables=extracted.tables,
            created_by="ingestion",
        )
