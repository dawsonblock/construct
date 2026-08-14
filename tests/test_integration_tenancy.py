"""The tenancy trust boundary, at the repository and database level.

`scripts/acceptance.py` runs the same attacks through the HTTP API. Both matter:
this file proves the repositories and RLS hold, the gate proves nothing above
them re-opens the door.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from construction_ai.persistence.db import Scope, ScopeError


# --------------------------------------------------------------------------
# Scope is required, and typed
# --------------------------------------------------------------------------

def test_scope_rejects_a_string_organization_id():
    with pytest.raises(ScopeError):
        Scope("not-a-uuid")


def test_scope_rejects_a_string_project_id():
    with pytest.raises(ScopeError):
        Scope(uuid4(), "not-a-uuid")


def test_project_scoped_operations_refuse_a_project_less_scope(repos, org_a):
    with pytest.raises(ScopeError, match="project-scoped"):
        repos.invoices.for_project(scope=org_a["scope"])
    with pytest.raises(ScopeError, match="project-scoped"):
        repos.evidence.for_project(scope=org_a["scope"])


def test_repositories_expose_no_unscoped_read():
    """A regression guard on the shape of the API, not on one call site."""
    import inspect

    from construction_ai.persistence.repositories import Repositories

    offenders = []
    for name in dir(Repositories):
        if name.startswith("_"):
            continue
        attribute = getattr(Repositories, name)
        repository_type = getattr(attribute, "func", None)
        if repository_type is None:
            continue
        annotation = inspect.signature(repository_type).return_annotation
        if not hasattr(annotation, "__mro__"):
            continue
        for method_name, method in inspect.getmembers(annotation, inspect.isfunction):
            if method_name.startswith("_") or method_name in {"from_env"}:
                continue
            parameters = inspect.signature(method).parameters
            if "scope" not in parameters:
                offenders.append(f"{annotation.__name__}.{method_name}")
    # Credential resolution runs *before* a scope exists — it is how a scope is
    # established. These are the unscoped entry points, mirroring
    # OrganizationRepository.authenticate, and each is surveyed by the authority
    # tests in test_authority.py.
    unscoped_entry_points = {
        "OrganizationRepository.",            # bearer token -> organization
        "UserRepository.resolve_identity",   # provider subject -> user (login)
        "SessionRepository.resolve",         # session token -> session (auth)
    }
    offenders = [
        o for o in offenders
        if not any(o.startswith(prefix) for prefix in unscoped_entry_points)
    ]
    assert offenders == [], f"repository methods without a scope: {offenders}"


# --------------------------------------------------------------------------
# References are per-tenant, ids never collide
# --------------------------------------------------------------------------

def test_two_tenants_may_share_a_project_reference(org_a, org_b):
    assert org_a["project"].reference == org_b["project"].reference == "PRJ-0042"
    assert org_a["project"].project_id != org_b["project"].project_id


def test_a_reference_resolves_only_inside_its_own_tenant(repos, org_a, org_b):
    a_project = repos.projects.get_by_reference(scope=org_a["scope"], reference="PRJ-0042")
    b_project = repos.projects.get_by_reference(scope=org_b["scope"], reference="PRJ-0042")
    assert a_project.project_id == org_a["project"].project_id
    assert b_project.project_id == org_b["project"].project_id
    assert a_project.name != b_project.name


# --------------------------------------------------------------------------
# Cross-tenant reads, writes and deletes
# --------------------------------------------------------------------------

def _seed_invoice(repos, org, reference="INV-8831"):
    scope = org["scope"].for_project(UUID(org["project"].project_id))
    return repos.invoices.create(
        scope=scope, reference=reference, invoice_number="8831", vendor_name="ABC Electric",
        total=4760.00, subtotal=4533.33, tax=226.67, po_reference="PO-1042-17",
        vendor_company_id=UUID(org["company"].company_id),
    )


def test_b_cannot_read_a_project(repos, org_a, org_b):
    assert repos.projects.get(scope=org_b["scope"], project_id=UUID(org_a["project"].project_id)) is None


def test_b_cannot_read_a_invoice(repos, org_a, org_b):
    invoice = _seed_invoice(repos, org_a)
    assert repos.invoices.get(scope=org_b["scope"], invoice_id=UUID(invoice.invoice_id)) is None


def test_b_cannot_list_a_invoices(repos, org_a, org_b):
    _seed_invoice(repos, org_a)
    assert repos.invoices.list(scope=org_b["scope"]) == []


def test_b_cannot_traverse_into_a_project(repos, org_a, org_b):
    _seed_invoice(repos, org_a)
    foreign = org_b["scope"].for_project(UUID(org_a["project"].project_id))
    assert repos.invoices.for_project(scope=foreign) == []


def test_b_cannot_update_a_invoice(repos, org_a, org_b):
    invoice = _seed_invoice(repos, org_a)
    changed = repos.invoices.set_status(scope=org_b["scope"], invoice_id=UUID(invoice.invoice_id), status="approved")
    assert changed is False
    still = repos.invoices.get(scope=org_a["scope"], invoice_id=UUID(invoice.invoice_id))
    assert still is not None


def test_b_cannot_delete_a_invoice(repos, org_a, org_b):
    invoice = _seed_invoice(repos, org_a)
    assert repos.invoices.delete(scope=org_b["scope"], record_id=UUID(invoice.invoice_id)) is False
    assert repos.invoices.get(scope=org_a["scope"], invoice_id=UUID(invoice.invoice_id)) is not None


def test_b_cannot_decide_a_approval(repos, org_a, org_b):
    invoice = _seed_invoice(repos, org_a)
    scope = org_a["scope"].for_project(UUID(org_a["project"].project_id))
    approval = repos.approvals.create(
        scope=scope, reference="APR-TEST", approval_type="PURCHASE_INVOICE", subject_type="invoice",
        subject_id=UUID(invoice.invoice_id), recommended_action="APPROVE", amount=4760.00,
    )
    assert repos.approvals.decide(
        scope=org_b["scope"], approval_id=UUID(approval.approval_id), status="approved", decided_by="attacker@b"
    ) is None
    assert repos.approvals.get(scope=org_a["scope"], approval_id=UUID(approval.approval_id)).status.value == "pending"


def test_b_cannot_read_a_evidence(repos, org_a, org_b):
    scope = org_a["scope"].for_project(UUID(org_a["project"].project_id))
    evidence = repos.evidence.record(
        scope=scope, field="total", value=4760.0, confidence=0.9, authority=0.75,
        source_type="document", source_id="SRC-1",
    )
    assert repos.evidence.get_many(scope=org_b["scope"], evidence_ids=[UUID(evidence.evidence_id)]) == []


def test_b_cannot_read_a_jobs(repos, org_a, org_b):
    job = repos.jobs.create(scope=org_a["scope"], job_type="invoice_document", payload={"text": "secret"})
    assert repos.jobs.get(scope=org_b["scope"], job_id=job.job_id) is None
    assert repos.jobs.list(scope=org_b["scope"]) == []


def test_b_cannot_claim_a_job(repos, org_a, org_b):
    """The queue token names an organization; claiming still checks it."""
    job = repos.jobs.create(scope=org_a["scope"], job_type="invoice_document", payload={})
    assert repos.jobs.claim(scope=org_b["scope"], job_id=job.job_id) is None
    assert repos.jobs.get(scope=org_a["scope"], job_id=job.job_id).status == "queued"


# --------------------------------------------------------------------------
# Audit chains are per tenant
# --------------------------------------------------------------------------

def test_audit_chains_are_independent_and_both_verify(repos, org_a, org_b):
    for index in range(3):
        repos.audit.append(scope=org_a["scope"], event_type="TEST_A", actor="ai", object_type="test", payload={"i": index})
        repos.audit.append(scope=org_b["scope"], event_type="TEST_B", actor="ai", object_type="test", payload={"i": index})
    assert repos.audit.verify_chain(scope=org_a["scope"]) is True
    assert repos.audit.verify_chain(scope=org_b["scope"]) is True


def test_each_tenant_chain_starts_at_one(repos, org_a, org_b):
    """A shared sequence would leak one tenant's activity rate to the other."""
    a = repos.audit.append(scope=org_a["scope"], event_type="TEST", actor="ai", object_type="test")
    b = repos.audit.append(scope=org_b["scope"], event_type="TEST", actor="ai", object_type="test")
    assert a.sequence == b.sequence == 1
    assert a.prev_hash is None and b.prev_hash is None
    assert a.entry_hash != b.entry_hash


def test_tampering_with_a_payload_breaks_the_chain(repos, org_a):
    repos.audit.append(scope=org_a["scope"], event_type="TEST", actor="ai", object_type="test", payload={"amount": 100})
    repos.audit.append(scope=org_a["scope"], event_type="TEST", actor="ai", object_type="test", payload={"amount": 200})
    assert repos.audit.verify_chain(scope=org_a["scope"]) is True
    with repos.db.scoped(org_a["scope"]) as cur:
        cur.execute(
            "UPDATE audit_events SET payload = '{\"amount\": 999}'::jsonb WHERE organization_id = %s AND sequence = 1",
            (org_a["organization_id"],),
        )
    assert repos.audit.verify_chain(scope=org_a["scope"]) is False


def test_b_cannot_see_a_audit_events(repos, org_a, org_b):
    repos.audit.append(scope=org_a["scope"], event_type="SECRET", actor="ai", object_type="test")
    with repos.db.scoped(org_b["scope"]) as cur:
        cur.execute("SELECT count(*) FROM audit_events WHERE event_type = 'SECRET'")
        assert cur.fetchone()[0] == 0


# --------------------------------------------------------------------------
# Row-level security, tested independently of the repositories
# --------------------------------------------------------------------------

TENANT_TABLES = ["projects", "companies", "invoices", "evidence", "approvals", "approval_packets", "jobs", "audit_events"]


def test_a_connection_that_never_set_a_scope_sees_nothing(repos, org_a):
    """The second wall, with the first one removed."""
    _seed_invoice(repos, org_a)
    with repos.db.unscoped_auth() as cur:
        for table in TENANT_TABLES:
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed list
            assert cur.fetchone()[0] == 0, f"{table} was readable without app.organization_id"


def test_raw_sql_with_no_where_clause_still_only_sees_one_tenant(repos, org_a, org_b):
    _seed_invoice(repos, org_a)
    _seed_invoice(repos, org_b)
    with repos.db.scoped(org_a["scope"]) as cur:
        cur.execute("SELECT organization_id FROM invoices")
        owners = {row[0] for row in cur.fetchall()}
    assert owners == {org_a["organization_id"]}


def test_rls_blocks_writing_into_another_tenant(repos, org_a, org_b):
    """A hand-written INSERT naming B's id, executed under A's scope."""
    import psycopg

    with pytest.raises(psycopg.errors.Error):
        with repos.db.scoped(org_a["scope"]) as cur:
            cur.execute(
                "INSERT INTO projects(organization_id, reference, name) VALUES(%s, %s, %s)",
                (org_b["organization_id"], "SMUGGLED", "Smuggled project"),
            )
    assert repos.projects.get_by_reference(scope=org_b["scope"], reference="SMUGGLED") is None


def test_the_scope_guc_does_not_survive_the_transaction(repos, org_a):
    """`SET LOCAL` semantics: a reused connection cannot inherit a stale tenant."""
    with repos.db.scoped(org_a["scope"]) as cur:
        cur.execute("SELECT current_setting('app.organization_id', true)")
        assert cur.fetchone()[0] == str(org_a["organization_id"])
    with repos.db.unscoped_auth() as cur:
        cur.execute("SELECT current_setting('app.organization_id', true)")
        assert cur.fetchone()[0] in (None, "")


# --------------------------------------------------------------------------
# Composite foreign keys
# --------------------------------------------------------------------------

def test_invoice_lines_follow_their_invoice_when_it_is_filed(repos, org_a):
    """`ON UPDATE CASCADE` on the scope-preserving key does the re-filing."""
    scope = org_a["scope"]
    invoice = repos.invoices.create(
        scope=scope, reference="INV-UNFILED", invoice_number="9001", vendor_name="ABC Electric", total=100.00,
    )
    assert invoice.project_id is None
    repos.invoices.add_line(scope=scope, invoice_id=UUID(invoice.invoice_id), line_number=1, description="rough-in", amount=100.00)

    project_id = UUID(org_a["project"].project_id)
    assert repos.invoices.assign_project(scope=scope, invoice_id=UUID(invoice.invoice_id), project_id=project_id) is True

    with repos.db.scoped(scope) as cur:
        cur.execute("SELECT project_id FROM invoice_lines WHERE organization_id = %s AND invoice_id = %s",
                    (org_a["organization_id"], UUID(invoice.invoice_id)))
        assert cur.fetchone()[0] == project_id


def test_an_invoice_line_cannot_reference_a_foreign_invoice(repos, org_a, org_b):
    invoice = _seed_invoice(repos, org_a)
    with pytest.raises(LookupError):
        repos.invoices.add_line(
            scope=org_b["scope"], invoice_id=UUID(invoice.invoice_id), line_number=1, description="x", amount=1.0
        )
