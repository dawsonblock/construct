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
    from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except Exception:  # pragma: no cover - import guard for tooling without FastAPI
    FastAPI = None

from construction_ai.approvals.service import (
    ApprovalAlreadyDecided,
    ApprovalDecisionError,
    ApprovalNotFound,
    decide_approval,
)
from construction_ai.auth.authorization import AuthorizationError
from construction_ai.auth.models import AuthenticatedActor
from construction_ai.auth.sessions import AuthenticationError, actor_from_session, login, provider_from_env
from construction_ai.jobs.handlers import INVOICE_DOCUMENT
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories

VERSION = (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()

app = FastAPI(title="Construction AI Ops", version=VERSION) if FastAPI else None
repos = Repositories.from_env() if app else None
_queue = None
_queue_lock = threading.Lock()

# -- security headers (v0.4.3 item 21) -------------------------------------
# Every response gets a baseline set of hardening headers. The CSP is split:
# HTML pages get a policy that allows self-sourced scripts/styles and same-origin
# API calls; JSON/API responses get default-src 'none' since they are never
# rendered as documents. Inline scripts and handlers are blocked everywhere.
_API_CSP = "default-src 'none'; frame-ancestors 'none'"
_HTML_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

if app:
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        ct = response.headers.get("content-type", "")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        if "text/html" in ct:
            response.headers["Content-Security-Policy"] = _HTML_CSP
        else:
            response.headers["Content-Security-Policy"] = _API_CSP
        return response

    # Serve the approval UI's static assets (JS/CSS) from /static.
    _web_dir = Path(__file__).resolve().parent.parent / "web"
    app.mount("/static", StaticFiles(directory=str(_web_dir)), name="static")


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

    def current_actor(authorization: str = Header(default="")) -> AuthenticatedActor:
        """Bearer session token → AuthenticatedActor. The only place a *human*
        identity is established — never from a request field. Every financial
        mutation consumes this server-derived actor."""
        parts = authorization.split(" ", 1)
        token = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
        try:
            return actor_from_session(repos, token)
        except AuthenticationError as exc:
            raise HTTPException(401, str(exc)) from exc

    def _uuid(value: str, label: str) -> UUID:
        try:
            return UUID(value)
        except ValueError:
            # Same shape as "not found": a malformed id must not be
            # distinguishable from another tenant's valid one.
            raise HTTPException(404, f"{label} not found") from None

    class RelationshipDecision(BaseModel):
        user_id: str
        status: str = "approved"

    class InvoiceDocumentJob(BaseModel):
        text: str
        filename: str = "invoice.txt"
        source_id: str | None = None
        thread_id: str | None = None
        # work_confirmed is intentionally absent (item 12): a caller cannot
        # declare physical work complete. Work completion is read from
        # work_confirmations records by the pipeline.

    class SessionLogin(BaseModel):
        credential: str

    class ApprovalDecisionRequest(BaseModel):
        reason: str = ""

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

    # -- authentication -----------------------------------------------------

    @app.post("/auth/session")
    def create_session(body: SessionLogin):
        """Exchange a credential for a session token.

        The server resolves the credential to a user via the configured identity
        provider and issues a server-side session. The response carries the
        session token; the caller never chooses a user_id."""
        try:
            token = login(repos, provider=provider_from_env(), credential=body.credential)
        except AuthenticationError as exc:
            raise HTTPException(401, str(exc)) from exc
        except Exception as exc:  # provider misconfiguration, etc.
            raise HTTPException(503, f"authentication unavailable: {type(exc).__name__}") from exc
        return {"session_token": token}

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

    class WorkConfirmationRequest(BaseModel):
        confirmation_type: str
        percent_complete: float | None = None
        invoice_id: str | None = None

    @app.post("/projects/{project_id}/work-confirmations", status_code=201)
    def record_work_confirmation(project_id: str, body: WorkConfirmationRequest, scope: Scope = Depends(current_scope)):
        """Record that physical work is complete (item 12).

        This is the authoritative source the verifier reads. A caller cannot
        declare work complete on the invoice job; it must record it here."""
        project_scope = scope.for_project(_uuid(project_id, "project"))
        if repos.projects.get(scope=project_scope, project_id=project_scope.project_id) is None:
            raise HTTPException(404, "project not found")
        confirmation = repos.work_confirmations.record(
            scope=project_scope,
            project_id=project_scope.project_id,
            invoice_id=_uuid(body.invoice_id, "invoice") if body.invoice_id else None,
            confirmation_type=body.confirmation_type,
            percent_complete=body.percent_complete,
        )
        return {"confirmation_id": str(confirmation.confirmation_id), "status": confirmation.status}

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

    def _decide(approval_id: str, decision: str, actor: AuthenticatedActor, body: ApprovalDecisionRequest):
        approval_uuid = _uuid(approval_id, "approval")
        try:
            outcome = decide_approval(
                repos, actor=actor, approval_id=approval_uuid, decision=decision, reason=body.reason
            )
        except ApprovalNotFound:
            raise HTTPException(404, "approval not found") from None
        except ApprovalAlreadyDecided as exc:
            raise HTTPException(409, str(exc)) from exc
        except AuthorizationError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ApprovalDecisionError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "approval_id": str(outcome.approval_id),
            "status": outcome.decision,
            "by": str(outcome.actor_id),
            "decision_id": str(outcome.decision_id),
            "policy_version": outcome.policy_version,
        }

    @app.post("/approvals/{approval_id}/approve")
    def approve_api(approval_id: str, body: ApprovalDecisionRequest, actor: AuthenticatedActor = Depends(current_actor)):
        return _decide(approval_id, "approved", actor, body)

    @app.post("/approvals/{approval_id}/hold")
    def hold_api(approval_id: str, body: ApprovalDecisionRequest, actor: AuthenticatedActor = Depends(current_actor)):
        return _decide(approval_id, "held", actor, body)

    @app.post("/approvals/{approval_id}/reject")
    def reject_api(approval_id: str, body: ApprovalDecisionRequest, actor: AuthenticatedActor = Depends(current_actor)):
        return _decide(approval_id, "rejected", actor, body)

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
