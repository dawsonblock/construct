"""The invoice path over scoped repositories, plus the job and ingestion paths."""
from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from apps.worker.main import run as run_worker
from construction_ai.domain.models import PurchaseOrder, Quote
from construction_ai.executive.invoice_pipeline import InvoicePipeline
from construction_ai.extraction.invoice import extract_invoice_deterministic
from construction_ai.ingestion.attachments import AttachmentPipeline, LocalObjectStore
from construction_ai.jobs import handlers
from construction_ai.jobs.queue import JobQueue

ROOT = Path(__file__).resolve().parents[1]
DEMO_INVOICE = (ROOT / "scripts" / "fixtures" / "demo_invoice_8831.txt").read_text()


class StubERP:
    """Mirrors apps/erpnext_stub/fixtures.json without the HTTP hop."""

    def __init__(self, project_reference: str = "PRJ-0042"):
        self.project_reference = project_reference

    def resolve_supplier(self, vendor_name):
        return {"name": "ABC Electric"} if vendor_name.strip() == "ABC Electric" else None

    def resolve_purchase_order(self, po_number):
        if po_number != "PO-1042-17":
            return None, []
        return PurchaseOrder("ERP-PO", "ERP", "PO-1042-17", self.project_reference, "ABC Electric", 4760.00, "Q-8821"), []

    def resolve_quote(self, quote_number):
        if quote_number != "Q-8821":
            return None, []
        return Quote("ERP-Q", "ERP", "Q-8821", self.project_reference, "ABC Electric", 4760.00, True), []


def _extract():
    invoice, evidence, warnings = extract_invoice_deterministic(
        organization_id="ORG", source_id="SRC-1", text=DEMO_INVOICE, filename="demo.txt"
    )
    assert invoice is not None, warnings
    return invoice, evidence


def _signals(project):
    return {"po_number": "PO-1042-17", "address": project.address}


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------

def test_demo_invoice_reaches_an_approve_recommendation(repos, org_a):
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=StubERP()).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=True
    )
    assert result["status"] == "prepared"
    assert result["project_id"] == org_a["project"].project_id
    assert result["exceptions"] == []
    assert result["recommended_action"] == "APPROVE"
    assert result["requires_human"] is True


def test_prepared_invoice_is_stored_under_its_tenant_and_project(repos, org_a, org_b):
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=StubERP()).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=True
    )
    invoice_id = UUID(result["invoice_id"])
    assert repos.invoices.get(scope=org_a["scope"], invoice_id=invoice_id) is not None
    assert repos.invoices.get(scope=org_b["scope"], invoice_id=invoice_id) is None
    project_scope = org_a["scope"].for_project(UUID(org_a["project"].project_id))
    assert [i.invoice_id for i in repos.invoices.for_project(scope=project_scope)] == [result["invoice_id"]]


def test_missing_erp_holds_the_invoice_rather_than_approving_it(repos, org_a):
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=None).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=True
    )
    assert result["recommended_action"] == "HOLD"
    assert "NO_OR_MISMATCHED_PO" in result["exceptions"]


def test_a_po_belonging_to_another_project_fails_project_match(repos, org_a):
    """ERPNext says the PO is on a project we did not resolve to."""
    repos.projects.create(scope=org_a["scope"], reference="PRJ-0099", name="Other", address="99 Other Rd")
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=StubERP(project_reference="PRJ-0099")).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=True
    )
    assert result["recommended_action"] == "HOLD"
    assert "INVALID_PROJECT" in result["exceptions"]


def test_the_same_invoice_twice_is_detected_as_a_duplicate(repos, org_a):
    pipeline = InvoicePipeline(repositories=repos, erp_resolver=StubERP())
    first = pipeline.process(scope=org_a["scope"], extracted=_extract()[0], signals=_signals(org_a["project"]), work_confirmed=True)
    second = pipeline.process(scope=org_a["scope"], extracted=_extract()[0], signals=_signals(org_a["project"]), work_confirmed=True)
    assert first["duplicate_of"] is None
    assert second["duplicate_of"] == first["invoice_id"]
    assert "DUPLICATE_INVOICE" in second["exceptions"]
    assert second["recommended_action"] == "HOLD"


def test_the_same_invoice_in_two_tenants_is_not_a_duplicate(repos, org_a, org_b):
    """Same vendor name, same invoice number, different companies. Different invoices."""
    pipeline = InvoicePipeline(repositories=repos, erp_resolver=StubERP())
    a = pipeline.process(scope=org_a["scope"], extracted=_extract()[0], signals=_signals(org_a["project"]), work_confirmed=True)
    b = pipeline.process(scope=org_b["scope"], extracted=_extract()[0], signals=_signals(org_b["project"]), work_confirmed=True)
    assert a["duplicate_of"] is None and b["duplicate_of"] is None
    assert a["invoice_id"] != b["invoice_id"]


def test_preparing_an_invoice_writes_an_audit_event(repos, org_a):
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=StubERP()).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=True
    )
    trail = repos.audit.for_object(scope=org_a["scope"], object_type="invoice", object_id=UUID(result["invoice_id"]))
    assert [event["event_type"] for event in trail] == ["INVOICE_APPROVAL_PREPARED"]
    assert repos.audit.verify_chain(scope=org_a["scope"]) is True


def test_evidence_is_recorded_against_the_resolved_project(repos, org_a):
    extracted, evidence = _extract()
    InvoicePipeline(repositories=repos, erp_resolver=StubERP()).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=True
    )
    project_scope = org_a["scope"].for_project(UUID(org_a["project"].project_id))
    fields = {e.field for e in repos.evidence.for_project(scope=project_scope)}
    assert {"invoice_number", "total", "po_number"} <= fields


# --------------------------------------------------------------------------
# Jobs and the worker
# --------------------------------------------------------------------------

class FakeRedis:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[str, dict[str, dict]] = {}
        self._msg_counter = 0

    def rpush(self, name, value):
        self.lists.setdefault(name, []).append(value)

    def blpop(self, name, timeout=0):
        items = self.lists.get(name) or []
        return (name, items.pop(0)) if items else None

    def llen(self, name):
        return len(self.lists.get(name) or [])

    def xadd(self, stream, fields):
        self._msg_counter += 1
        msg_id = f"0-{self._msg_counter}"
        self.streams.setdefault(stream, []).append((msg_id, dict(fields)))
        return msg_id

    def xgroup_create(self, stream, group, id="0", mkstream=False):
        if mkstream and stream not in self.streams:
            self.streams[stream] = []
        if stream not in self.streams:
            raise Exception("ERR no such key")
        if group in self.groups.get(stream, {}):
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self.groups.setdefault(stream, {})[group] = {
            "consumers": {},
            "pending": {},
            "last_delivered_id": id,
        }

    def xreadgroup(self, group, consumer, streams, count=1, block=0):
        results = []
        for stream_name, start_id in streams.items():
            if stream_name not in self.streams:
                continue
            stream = self.streams[stream_name]
            grp = self.groups.get(stream_name, {}).get(group)
            if grp is None:
                continue
            grp["consumers"].setdefault(consumer, [])
            delivered = []
            for msg_id, fields in stream:
                if start_id == ">":
                    if msg_id in grp["pending"]:
                        continue
                    delivered.append((msg_id, fields))
                    grp["pending"][msg_id] = consumer
                    grp["consumers"][consumer].append(msg_id)
                    if len(delivered) >= count:
                        break
            if delivered:
                results.append((stream_name, delivered))
        return results if results else []

    def xack(self, stream, group, *msg_ids):
        grp = self.groups.get(stream, {}).get(group)
        if grp is None:
            return 0
        acked = 0
        for mid in msg_ids:
            if mid in grp["pending"]:
                del grp["pending"][mid]
                acked += 1
        return acked

    def xlen(self, stream):
        return len(self.streams.get(stream, []))

    def xpending(self, stream, group):
        grp = self.groups.get(stream, {}).get(group)
        if grp is None:
            return 0
        return len(grp["pending"])


@pytest.fixture()
def queue(repos):
    return JobQueue(repos, FakeRedis(), "test:jobs")


def test_the_queue_carries_only_ids_and_the_row_carries_the_scope(queue, org_a):
    job = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    # v0.4.4: enqueue writes to the outbox, not directly to Redis. The relay
    # pushes unpublished outbox rows to Redis.
    queue.relay_outbox(limit=10)
    tokens = [e[1]["token"] for e in queue.redis.streams["test:jobs"]]
    assert tokens == [f"{org_a['organization_id']}:{job.job_id}"]
    assert queue.get(scope=org_a["scope"], job_id=job.job_id).payload == {"text": "hello"}


def test_a_job_payload_cannot_redirect_the_worker_to_another_tenant(queue, repos, org_a, org_b, monkeypatch):
    """The classic escalation: put someone else's organization in the payload."""
    monkeypatch.setattr(handlers, "create_erpnext_resolver", lambda organization_id: StubERP())
    job = queue.enqueue(
        scope=org_a["scope"],
        job_type="invoice_document",
        payload={"text": DEMO_INVOICE, "filename": "demo.txt", "work_confirmed": True,
                 "organization_id": str(org_b["organization_id"]), "project_id": org_b["project"].project_id},
    )
    run_worker(queue, once=True, timeout=0)

    result = queue.get(scope=org_a["scope"], job_id=job.job_id).result
    assert result["status"] == "prepared"
    assert result["project_id"] == org_a["project"].project_id, "payload steered the worker into another tenant"
    assert repos.invoices.list(scope=org_b["scope"]) == []


def test_worker_records_a_handler_exception_rather_than_crashing(queue, org_a, monkeypatch):
    def boom(repos, job):
        raise ValueError("extraction exploded")

    monkeypatch.setitem(handlers.HANDLERS, "invoice_document", boom)
    job = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={})
    run_worker(queue, once=True, timeout=0)
    record = queue.get(scope=org_a["scope"], job_id=job.job_id)
    # v0.4.4: first failure requeues for retry (not 'failed').
    assert record.status == "queued"
    assert "ValueError: extraction exploded" in record.error
    # Run all retry cycles to reach dead_letter.
    for _ in range(job.max_attempts):
        record = queue.get(scope=org_a["scope"], job_id=job.job_id)
        if record.status in ("completed", "dead_letter", "failed"):
            break
        run_worker(queue, once=True, timeout=0)
    record = queue.get(scope=org_a["scope"], job_id=job.job_id)
    assert record.status == "dead_letter"
    assert "ValueError: extraction exploded" in record.error


def test_worker_fails_a_job_with_no_handler(queue, org_a):
    job = queue.enqueue(scope=org_a["scope"], job_type="unknown_type", payload={})
    assert run_worker(queue, once=True, timeout=0) == 1
    record = queue.get(scope=org_a["scope"], job_id=job.job_id)
    # v0.4.4: first failure requeues for retry.
    assert record.status == "queued" and "no handler" in record.error
    # Run all retry cycles to reach dead_letter.
    for _ in range(job.max_attempts):
        record = queue.get(scope=org_a["scope"], job_id=job.job_id)
        if record.status in ("completed", "dead_letter", "failed"):
            break
        run_worker(queue, once=True, timeout=0)
    record = queue.get(scope=org_a["scope"], job_id=job.job_id)
    assert record.status == "dead_letter" and "no handler" in record.error


def test_unextractable_document_asks_for_a_human(queue, org_a, monkeypatch):
    monkeypatch.setattr(handlers, "create_erpnext_resolver", lambda organization_id: StubERP())
    job = queue.enqueue(scope=org_a["scope"], job_type="invoice_document",
                        payload={"text": "scanned page with no text layer", "filename": "scan.pdf"})
    run_worker(queue, once=True, timeout=0)
    result = queue.get(scope=org_a["scope"], job_id=job.job_id).result
    assert result["status"] == "extraction_incomplete"
    assert result["requires_human"] is True


def test_a_malformed_queue_token_is_dropped_not_guessed(queue):
    queue.redis.rpush("test:jobs", "not-a-uuid:also-not-a-uuid")
    assert queue.reserve(timeout=0) is None


# --------------------------------------------------------------------------
# Attachment ingestion
# --------------------------------------------------------------------------

def test_attachment_ingestion_is_content_idempotent_within_a_tenant(repos, org_a):
    with tempfile.TemporaryDirectory() as directory:
        pipeline = AttachmentPipeline(LocalObjectStore(Path(directory) / "objects"), repos)
        data = b"INVOICE 8831\nAmount Due: $100.00"
        first = pipeline.ingest(scope=org_a["scope"], filename="invoice.txt", data=data)
        second = pipeline.ingest(scope=org_a["scope"], filename="renamed.txt", data=data)
        assert first.document_version_id == second.document_version_id
        assert first.extracted_text


def test_the_same_bytes_in_two_tenants_are_two_documents(repos, org_a, org_b):
    """Shared content addressing across tenants would be an existence oracle."""
    with tempfile.TemporaryDirectory() as directory:
        pipeline = AttachmentPipeline(LocalObjectStore(Path(directory) / "objects"), repos)
        data = b"INVOICE 8831\nAmount Due: $100.00"
        a = pipeline.ingest(scope=org_a["scope"], filename="invoice.txt", data=data)
        b = pipeline.ingest(scope=org_b["scope"], filename="invoice.txt", data=data)
        assert a.document_id != b.document_id
        assert a.content_hash == b.content_hash


def test_a_document_version_is_not_readable_from_another_tenant(repos, org_a, org_b):
    with tempfile.TemporaryDirectory() as directory:
        pipeline = AttachmentPipeline(LocalObjectStore(Path(directory) / "objects"), repos)
        version = pipeline.ingest(scope=org_a["scope"], filename="invoice.txt", data=b"secret bytes")
        assert repos.documents.get_version(scope=org_b["scope"], document_version_id=version.document_version_id) is None


# --------------------------------------------------------------------------
# Mail sync
# --------------------------------------------------------------------------

class MailConnector:
    def fetch_message(self, message_id, organization_id):
        from construction_ai.ingestion.email import normalize_email

        communication = normalize_email(
            organization_id=organization_id, source="gmail", message_id=message_id,
            sender="bob@abc.ca", recipients=["pm@co.ca"], subject="Invoice", body="attached",
            received_at="2026-08-13T10:00:00Z", attachments=["inv.txt"],
        )
        return communication, [{"attachment_id": "a1", "filename": "inv.txt", "mime_type": "text/plain"}]

    def fetch_attachment(self, message_id, attachment_id):
        return b"ABC Electric\nINVOICE 8831\nAmount Due: $4760.00"


def test_mail_sync_ingests_a_message_once(repos, org_a):
    from construction_ai.ingestion.sync import MailSyncService

    message_id = f"m-{uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory() as directory:
        service = MailSyncService(
            connector=MailConnector(),
            repositories=repos,
            attachment_pipeline=AttachmentPipeline(LocalObjectStore(Path(directory) / "objects"), repos),
        )
        first = service.sync_message(scope=org_a["scope"], message_id=message_id)
        assert first["status"] == "accepted" and len(first["documents"]) == 1
        repeat = service.sync_message(scope=org_a["scope"], message_id=message_id)
        assert repeat["status"] == "duplicate"


def test_the_same_message_id_in_two_tenants_is_two_communications(repos, org_a, org_b):
    from construction_ai.ingestion.sync import MailSyncService

    message_id = f"m-{uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory() as directory:
        pipeline = AttachmentPipeline(LocalObjectStore(Path(directory) / "objects"), repos)
        service = MailSyncService(connector=MailConnector(), repositories=repos, attachment_pipeline=pipeline)
        a = service.sync_message(scope=org_a["scope"], message_id=message_id)
        b = service.sync_message(scope=org_b["scope"], message_id=message_id)
        assert a["status"] == "accepted" and b["status"] == "accepted"
        assert a["communication_id"] != b["communication_id"]
