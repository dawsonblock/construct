"""HTTP API.

Tenant identity comes from the bearer token and nowhere else. There is no
`organization_id` request field to spoof, and every handler works through a
`Scope` the server constructed.

Cross-tenant reads return 404, never 403: a 403 confirms the record exists,
which is enough to enumerate another tenant's ids. See docs/TENANCY.md §5.
"""
from __future__ import annotations

import threading
from pathlib import Path
from uuid import UUID

try:
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.responses import HTMLResponse
    from pydantic import BaseModel
except Exception:  # pragma: no cover - import guard for tooling without FastAPI
    FastAPI = None

from construction_ai.jobs.handlers import INVOICE_DOCUMENT
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories

VERSION = (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()

app = FastAPI(title="Construction AI Ops", version=VERSION) if FastAPI else None
repos = Repositories.from_env() if app else None
_queue = None
_queue_lock = threading.Lock()


def get_queue():
    """Lazy so the API still serves reads when Redis is unreachable."""
    global _queue
    if _queue is None:
        with _queue_lock:
            if _queue is not None:
                return _queue
            from construction_ai.jobs.queue import JobQueue

            _queue = JobQueue.from_env(repos)
    return _queue


if app:

    def current_scope(authorization: str = Header(default="")) -> Scope:
        """Bearer token → Scope. The only place tenant identity is established."""
        parts = authorization.split(" ", 1)
        token = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
        organization_id = repos.organizations.authenticate(token) if token else None
        if organization_id is None:
            raise HTTPException(401, "a valid organization API key is required")
        return Scope(organization_id)

    def _uuid(value: str, label: str) -> UUID:
        try:
            return UUID(value)
        except ValueError:
            # Same shape as "not found": a malformed id must not be
            # distinguishable from another tenant's valid one.
            raise HTTPException(404, f"{label} not found") from None

    class ApprovalAction(BaseModel):
        user_id: str

    class RelationshipDecision(BaseModel):
        user_id: str
        status: str = "approved"

    class InvoiceDocumentJob(BaseModel):
        text: str
        filename: str = "invoice.txt"
        source_id: str | None = None
        thread_id: str | None = None
        work_confirmed: bool = False

    # -- unauthenticated ----------------------------------------------------

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "version": VERSION,
            "financial_default": "human_approval_required",
            "tenancy": "relational_keys_plus_rls",
            "database": "ok" if repos.db.healthy() else "unavailable",
        }

    # -- ingestion ----------------------------------------------------------

    @app.post("/ingest/gmail", status_code=202)
    def ingest_gmail(payload: dict, scope: Scope = Depends(current_scope)):
        from construction_ai.ingestion.email import from_gmail_webhook

        return _ingest(from_gmail_webhook(payload, str(scope.organization_id)), scope)

    @app.post("/ingest/microsoft", status_code=202)
    def ingest_microsoft(payload: dict, scope: Scope = Depends(current_scope)):
        from construction_ai.ingestion.email import from_microsoft_webhook

        return _ingest(from_microsoft_webhook(payload, str(scope.organization_id)), scope)

    def _ingest(communication, scope: Scope):
        record, created = repos.communications.ingest(scope=scope, communication=communication)
        return {
            "status": "accepted" if created else "duplicate",
            "communication_id": record.communication_id,
        }

    # -- projects -----------------------------------------------------------

    @app.get("/projects")
    def list_projects(scope: Scope = Depends(current_scope)):
        return [
            {"project_id": p.project_id, "reference": p.reference, "name": p.name, "address": p.address, "status": p.status}
            for p in repos.projects.list(scope=scope)
        ]

    @app.get("/projects/{project_id}")
    def get_project(project_id: str, scope: Scope = Depends(current_scope)):
        project = repos.projects.get(scope=scope, project_id=_uuid(project_id, "project"))
        if not project:
            raise HTTPException(404, "project not found")
        return {
            "project_id": project.project_id,
            "reference": project.reference,
            "name": project.name,
            "address": project.address,
            "status": project.status,
            "company_ids": project.company_ids,
        }

    @app.get("/projects/{project_id}/invoices")
    def project_invoices(project_id: str, scope: Scope = Depends(current_scope)):
        project_scope = scope.for_project(_uuid(project_id, "project"))
        if repos.projects.get(scope=project_scope, project_id=project_scope.project_id) is None:
            raise HTTPException(404, "project not found")
        return [
            {"invoice_id": i.invoice_id, "reference": i.reference, "invoice_number": i.invoice_number, "total": i.total}
            for i in repos.invoices.for_project(scope=project_scope)
        ]

    # -- reconstruction and graph -------------------------------------------

    def _project_scope(project_id: str, scope: Scope) -> Scope:
        project_scope = scope.for_project(_uuid(project_id, "project"))
        if repos.projects.get(scope=project_scope, project_id=project_scope.project_id) is None:
            raise HTTPException(404, "project not found")
        return project_scope

    @app.get("/projects/{project_id}/state")
    def project_state(project_id: str, scope: Scope = Depends(current_scope)):
        """Deterministic reconstruction. Same database state, same response."""
        _project_scope(project_id, scope)  # 404s before reconstruction touches anything
        state = repos.reconstruction.project(scope=scope, project_id=_uuid(project_id, "project"))
        return {**state.as_dict(), "fingerprint": state.fingerprint()}

    @app.get("/projects/{project_id}/conflicts")
    def project_conflicts(project_id: str, severity: str | None = None, scope: Scope = Depends(current_scope)):
        _project_scope(project_id, scope)
        state = repos.reconstruction.project(scope=scope, project_id=_uuid(project_id, "project"))
        conflicts = state.conflicts_of_severity(severity) if severity else state.unresolved_conflicts
        return {"project_id": project_id, "count": len(conflicts), "conflicts": conflicts}

    @app.get("/projects/{project_id}/graph")
    def project_graph_view(project_id: str, status: str | None = None, scope: Scope = Depends(current_scope)):
        project_scope = _project_scope(project_id, scope)
        entities = repos.entities.for_project(scope=project_scope)
        relationships = repos.relationships.for_project(scope=project_scope, status=status)
        labels = {e.entity_id: e.label for e in entities}
        return {
            "nodes": [
                {"entity_id": str(e.entity_id), "type": e.entity_type, "label": e.label,
                 "record_table": e.record_table, "record_id": str(e.record_id) if e.record_id else None}
                for e in entities
            ],
            "edges": [
                {"relationship_id": str(r.relationship_id), "relation": r.relation, "status": r.status,
                 "origin": r.origin, "confidence": r.confidence, "decided_by": r.decided_by,
                 "source": labels.get(r.source_entity_id, str(r.source_entity_id)),
                 "target": labels.get(r.target_entity_id, str(r.target_entity_id))}
                for r in relationships
            ],
        }

    @app.post("/projects/{project_id}/graph/project", status_code=202)
    def rebuild_project_graph(project_id: str, scope: Scope = Depends(current_scope)):
        """Re-derive structural nodes and edges. Idempotent; touches no proposals."""
        from construction_ai.graph.projection import project_graph

        project_scope = _project_scope(project_id, scope)
        counts = project_graph(repos, project_scope)
        repos.audit.append(
            scope=project_scope, event_type="GRAPH_PROJECTED", actor="system",
            object_type="project", object_id=project_scope.project_id, payload=counts,
        )
        return counts

    @app.post("/relationships/{relationship_id}/decide")
    def decide_relationship(relationship_id: str, body: RelationshipDecision, scope: Scope = Depends(current_scope)):
        """Promote or reject an inferred relationship. `ai` is never a valid decider."""
        if not body.user_id or body.user_id == "ai":
            raise HTTPException(422, "promoting an inferred relationship requires a human")
        relationship_uuid = _uuid(relationship_id, "relationship")
        decided = repos.relationships.decide(
            scope=scope, relationship_id=relationship_uuid, status=body.status, decided_by=body.user_id
        )
        if decided is None:
            existing = repos.relationships.get(scope=scope, relationship_id=relationship_uuid)
            if existing is None:
                raise HTTPException(404, "relationship not found")
            raise HTTPException(409, f"relationship already {existing.status}")
        repos.audit.append(
            scope=scope.for_project(decided.project_id),
            event_type="RELATIONSHIP_DECIDED", actor=body.user_id,
            object_type="relationship", object_id=relationship_uuid,
            payload={"status": decided.status, "relation": decided.relation, "origin": decided.origin},
        )
        return {"relationship_id": str(decided.relationship_id), "status": decided.status, "by": body.user_id}

    # -- invoices -----------------------------------------------------------

    @app.get("/invoices")
    def list_invoices(status: str | None = None, scope: Scope = Depends(current_scope)):
        return [
            {"invoice_id": i.invoice_id, "reference": i.reference, "invoice_number": i.invoice_number,
             "vendor_name": i.vendor_name, "total": i.total, "project_id": i.project_id}
            for i in repos.invoices.list(scope=scope, status=status)
        ]

    @app.get("/invoices/{invoice_id}")
    def get_invoice(invoice_id: str, scope: Scope = Depends(current_scope)):
        invoice = repos.invoices.get(scope=scope, invoice_id=_uuid(invoice_id, "invoice"))
        if not invoice:
            raise HTTPException(404, "invoice not found")
        return invoice

    # -- jobs ---------------------------------------------------------------

    @app.post("/jobs/invoice-document", status_code=202)
    def enqueue_invoice_document(body: InvoiceDocumentJob, scope: Scope = Depends(current_scope)):
        try:
            job = get_queue().enqueue(scope=scope, job_type=INVOICE_DOCUMENT, payload=body.model_dump())
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, f"job queue unavailable: {type(exc).__name__}") from exc
        return {"job_id": str(job.job_id), "status": job.status}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, scope: Scope = Depends(current_scope)):
        job = repos.jobs.get(scope=scope, job_id=_uuid(job_id, "job"))
        if not job:
            raise HTTPException(404, "job not found")
        return job.as_dict()

    # -- approvals ----------------------------------------------------------

    @app.get("/approvals")
    def list_approvals(status: str | None = None, scope: Scope = Depends(current_scope)):
        return [
            {"approval_id": a.approval_id, "reference": a.reference, "status": a.status.value,
             "recommended_action": a.recommended_action, "amount": a.amount, "exceptions": a.exceptions}
            for a in repos.approvals.list(scope=scope, status=status)
        ]

    @app.get("/approvals/{approval_id}")
    def get_approval(approval_id: str, scope: Scope = Depends(current_scope)):
        approval = repos.approvals.get(scope=scope, approval_id=_uuid(approval_id, "approval"))
        if not approval:
            raise HTTPException(404, "approval not found")
        return {
            "approval_id": approval.approval_id,
            "reference": approval.reference,
            "subject_id": approval.subject_id,
            "status": approval.status.value,
            "recommended_action": approval.recommended_action,
            "amount": approval.amount,
            "exceptions": approval.exceptions,
            "decided_by": approval.approved_by,
        }

    @app.get("/approvals/{approval_id}/packet")
    def get_packet_for_approval(approval_id: str, scope: Scope = Depends(current_scope)):
        packet = repos.approval_packets.for_approval(scope=scope, approval_id=_uuid(approval_id, "approval"))
        if not packet:
            raise HTTPException(404, "packet not found")
        return packet

    def _decide(approval_id: str, user_id: str, status: str, scope: Scope):
        if not user_id or user_id == "ai":
            raise HTTPException(422, "an approval decision requires a human actor")
        approval_uuid = _uuid(approval_id, "approval")
        decided = repos.approvals.decide(scope=scope, approval_id=approval_uuid, status=status, decided_by=user_id)
        if decided is None:
            # Either it is not ours, or it was already decided. Both are 404/409
            # without revealing which — an existence oracle is a tenancy leak.
            existing = repos.approvals.get(scope=scope, approval_id=approval_uuid)
            if existing is None:
                raise HTTPException(404, "approval not found")
            raise HTTPException(409, f"approval already {existing.status.value}")
        repos.audit.append(
            scope=scope.for_project(UUID(decided.project_id) if decided.project_id else None),
            event_type="APPROVAL_DECIDED",
            actor=user_id,
            object_type="approval",
            object_id=approval_uuid,
            payload={"status": status, "subject_id": decided.subject_id},
        )
        return {"approval_id": decided.approval_id, "status": decided.status.value, "by": user_id}

    @app.post("/approvals/{approval_id}/approve")
    def approve_api(approval_id: str, body: ApprovalAction, scope: Scope = Depends(current_scope)):
        return _decide(approval_id, body.user_id, "approved", scope)

    @app.post("/approvals/{approval_id}/hold")
    def hold_api(approval_id: str, body: ApprovalAction, scope: Scope = Depends(current_scope)):
        return _decide(approval_id, body.user_id, "held", scope)

    @app.post("/approvals/{approval_id}/reject")
    def reject_api(approval_id: str, body: ApprovalAction, scope: Scope = Depends(current_scope)):
        return _decide(approval_id, body.user_id, "rejected", scope)

    # -- audit --------------------------------------------------------------

    @app.get("/audit/verify")
    def verify_audit(scope: Scope = Depends(current_scope)):
        """Recomputes this organization's chain only. Phase 22 adds per-transaction reconstruction."""
        return {"intact": repos.audit.verify_chain(scope=scope)}

    @app.get("/audit/invoice/{invoice_id}")
    def audit_for_invoice(invoice_id: str, scope: Scope = Depends(current_scope)):
        invoice_uuid = _uuid(invoice_id, "invoice")
        if repos.invoices.get(scope=scope, invoice_id=invoice_uuid) is None:
            raise HTTPException(404, "invoice not found")
        return repos.audit.for_object(scope=scope, object_type="invoice", object_id=invoice_uuid)

    # -- web UI -------------------------------------------------------------

    @app.get("/approval-ui", response_class=HTMLResponse)
    def approval_ui():
        return (Path(__file__).resolve().parent.parent / "web" / "approval.html").read_text()
