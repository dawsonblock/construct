"""Fixtures for the PostgreSQL-backed tests.

The tenancy boundary is made of relational keys and row-level security, so it
cannot be tested against a stand-in. These tests need a real PostgreSQL with the
migrations applied, connected as the **non-superuser app role** — a superuser
bypasses RLS and would make every isolation assertion pass vacuously.

They skip when no database is reachable, so `make test` still works on a laptop
with nothing running. CI and `make test-stack` set `REQUIRE_INTEGRATION=1`, which
turns that skip into a failure: a silently skipped isolation suite is worse than
no isolation suite.
"""
from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(autouse=True)
def _enable_erp_execution_for_tests(monkeypatch):
    """rc7 Phase 43: ERP execution is feature-gated (default: disabled).
    Tests need it enabled to exercise the execution path."""
    monkeypatch.setenv("ERP_EXECUTION_ENABLED", "true")


from construction_ai.persistence.db import Database, Scope
from construction_ai.persistence.repositories import Repositories

DEFAULT_TEST_DSN = "postgresql://construction_app:construction_app@localhost:5432/construction_ai"


def _dsn() -> str:
    return os.getenv("TEST_DATABASE_URL") or os.getenv("APP_DATABASE_URL") or DEFAULT_TEST_DSN


@pytest.fixture(scope="session")
def database() -> Database:
    db = Database(_dsn())
    if not db.healthy():
        message = f"no PostgreSQL at {_dsn().rsplit('@', 1)[-1]} — start the stack with `make up`"
        if os.getenv("REQUIRE_INTEGRATION") == "1":
            pytest.fail(message)
        pytest.skip(message)
    _assert_rls_applies(db)
    return db


def _assert_rls_applies(db: Database) -> None:
    """Guard against testing isolation as a role that bypasses it."""
    with db.unscoped_auth() as cur:
        cur.execute("SELECT current_user, usesuper FROM pg_user WHERE usename = current_user")
        row = cur.fetchone()
        if row and row[1]:
            pytest.fail(
                f"tests are connected as superuser {row[0]!r}; row-level security does not apply to it, "
                "so every isolation assertion would pass vacuously. Use the construction_app role."
            )


@pytest.fixture()
def repos(database: Database) -> Repositories:
    return Repositories(database)


@pytest.fixture()
def organizations(repos: Repositories):
    """Two throwaway tenants, removed afterwards.

    Both get the same project and vendor references on purpose — references are
    unique per organization, and tests that used globally distinct fixtures would
    not notice if that stopped being true.
    """
    created = []

    def make(label: str):
        slug = f"test-{label}-{uuid.uuid4().hex[:12]}"
        organization = repos.organizations.create(slug=slug, name=f"Test {label}")
        created.append(organization.organization_id)
        scope = Scope(organization.organization_id)
        token = repos.organizations.issue_api_key(organization_id=organization.organization_id, label="test")
        company = repos.companies.create(
            scope=scope, reference="ABC-ELECTRIC", name="ABC Electric",
            company_type="subcontractor", erp_supplier_id="ABC Electric",
        )
        project = repos.projects.create(
            scope=scope, reference="PRJ-0042", name=f"{label} Project", address="421 8th St E",
            identifiers={"po": ["PO-1042-17"], "thread": ["THR-0042"]},
        )
        repos.projects.add_company(
            scope=scope, project_id=uuid.UUID(project.project_id), company_id=uuid.UUID(company.company_id)
        )
        return {
            "organization_id": organization.organization_id,
            "scope": scope,
            "token": token,
            "project": project,
            "company": company,
        }

    a, b = make("alpha"), make("beta")
    yield a, b

    with repos.db.unscoped_auth() as cur:
        for organization_id in created:
            cur.execute("DELETE FROM organizations WHERE organization_id = %s", (organization_id,))


@pytest.fixture()
def org_a(organizations):
    return organizations[0]


@pytest.fixture()
def org_b(organizations):
    return organizations[1]
