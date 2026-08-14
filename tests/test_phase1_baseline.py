"""Phase 1 — the frozen baseline is reproducible.

Migration ordering and drift detection, extraction regressions, object storage,
and the pinning invariants in the compose file. Nothing here touches a database:
everything that does lives in tests/test_integration_*.py, which needs a real
PostgreSQL because tenancy is enforced by relational keys and RLS.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from construction_ai.domain.models import Project
from construction_ai.extraction.invoice import extract_invoice_deterministic
from construction_ai.ingestion.attachments import LocalObjectStore, S3ObjectStore, create_object_store
from construction_ai.jobs import handlers
from construction_ai.persistence.migrations import plan

ROOT = Path(__file__).resolve().parents[1]
ORG = "ORG-TEST"
DEMO_INVOICE = (ROOT / "scripts" / "fixtures" / "demo_invoice_8831.txt").read_text()


# --------------------------------------------------------------------------
# Migration runner
# --------------------------------------------------------------------------

def test_migrations_are_discovered_in_filename_order():
    from construction_ai.persistence.migrations import discover

    names = [p.name for p in discover()]
    assert names == sorted(names)
    assert "001_control_plane.sql" in names


def test_plan_reports_pending_and_ignores_applied(tmp_path):
    first = tmp_path / "001_a.sql"
    first.write_text("SELECT 1;")
    second = tmp_path / "002_b.sql"
    second.write_text("SELECT 2;")

    import hashlib

    applied = {"001_a.sql": hashlib.sha256(first.read_bytes()).hexdigest()}
    pending, drifted = plan([first, second], applied)
    assert [p.name for p in pending] == ["002_b.sql"]
    assert drifted == []


def test_plan_flags_a_migration_edited_after_application(tmp_path):
    path = tmp_path / "001_a.sql"
    path.write_text("SELECT 1;")
    pending, drifted = plan([path], {"001_a.sql": "stale-checksum"})
    assert pending == []
    assert drifted == ["001_a.sql"]


def test_migrate_rejects_non_postgres_dsn():
    from scripts.migrate import main as migrate_main

    assert migrate_main(["--dsn", "sqlite:///x.db"]) == 2


# --------------------------------------------------------------------------
# Extraction regressions
# --------------------------------------------------------------------------

def test_subtotal_is_not_read_as_the_invoice_total():
    invoice, _, warnings = extract_invoice_deterministic(
        organization_id=ORG, source_id="SRC-1", text=DEMO_INVOICE, filename="demo.txt"
    )
    assert invoice is not None, warnings
    assert invoice.total == 4760.00
    assert invoice.subtotal == 4533.33
    assert invoice.tax == 226.67


def test_invoice_heading_is_not_read_as_the_invoice_number():
    invoice, _, _ = extract_invoice_deterministic(
        organization_id=ORG, source_id="SRC-1", text=DEMO_INVOICE, filename="demo.txt"
    )
    assert invoice.invoice_number == "8831"
    assert invoice.po_number == "PO-1042-17"
    assert invoice.quote_number == "Q-8821"


def test_missing_total_fails_closed():
    invoice, _, warnings = extract_invoice_deterministic(
        organization_id=ORG, source_id="SRC-1", text="ACME Ltd\nInvoice Number: 42\n", filename="x.txt"
    )
    assert invoice is None
    assert "total_missing" in [w for w in warnings if w]


# --------------------------------------------------------------------------
# Project address signal
# --------------------------------------------------------------------------

def _projects() -> list[Project]:
    return [
        Project("PRJ-0042", ORG, "Wilson Residence", address="421 8th St E", company_ids=["ABC Electric"], identifiers={"po": ["PO-1042-17"]}),
        Project("PRJ-0063", ORG, "Parker Residence", address="900 Main St", company_ids=["Northline Drywall"], identifiers={"po": ["PO-1077-02"]}),
    ]


def test_address_signal_matches_a_single_project():
    assert handlers.address_signal(DEMO_INVOICE, _projects()) == "421 8th St E"


def test_address_signal_stays_silent_when_two_projects_match():
    text = "Work at 421 8th St E and also at 900 Main St"
    assert handlers.address_signal(text, _projects()) is None


# --------------------------------------------------------------------------
# Object storage
# --------------------------------------------------------------------------

def test_object_store_factory_defaults_to_local(tmp_path, monkeypatch):
    monkeypatch.delenv("OBJECT_STORE_BACKEND", raising=False)
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "objects"))
    assert isinstance(create_object_store(), LocalObjectStore)


def test_object_store_factory_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "gcs")
    with pytest.raises(RuntimeError, match="unknown OBJECT_STORE_BACKEND"):
        create_object_store()


def test_s3_backend_requires_a_bucket(monkeypatch):
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", "")
    with pytest.raises(RuntimeError, match="S3_BUCKET"):
        create_object_store()


class _FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.buckets: set[str] = set()

    def head_bucket(self, Bucket):  # noqa: N803 - boto3 signature
        if Bucket not in self.buckets:
            raise KeyError(Bucket)

    def create_bucket(self, Bucket):  # noqa: N803
        self.buckets.add(Bucket)

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.objects[f"{Bucket}/{Key}"] = Body

    def get_object(self, Bucket, Key):  # noqa: N803
        import io

        return {"Body": io.BytesIO(self.objects[f"{Bucket}/{Key}"])}


def test_s3_object_store_is_content_addressed_and_round_trips():
    client = _FakeS3()
    storage = S3ObjectStore("docs", client=client)
    first = storage.put(ORG, "invoice.pdf", b"bytes")
    second = storage.put(ORG, "invoice.pdf", b"bytes")
    assert first == second
    assert storage.get(first) == b"bytes"
    assert storage.put(ORG, "invoice.pdf", b"other") != first


# --------------------------------------------------------------------------
# ERPNext stub
# --------------------------------------------------------------------------

def test_erpnext_stub_serves_the_shapes_the_resolver_reads():
    from fastapi.testclient import TestClient

    from apps.erpnext_stub.main import app as stub_app

    client = TestClient(stub_app)
    response = client.get(
        "/api/resource/Purchase Order",
        params={"filters": json.dumps([["name", "=", "PO-1042-17"]]), "fields": json.dumps(["name", "supplier", "grand_total", "project"])},
    )
    assert response.status_code == 200
    rows = response.json()["data"]
    assert rows == [{"name": "PO-1042-17", "supplier": "ABC Electric", "grand_total": 4760.00, "project": "PRJ-0042"}]


def test_erpnext_stub_and_resolver_agree_end_to_end():
    from fastapi.testclient import TestClient

    from apps.erpnext_stub.main import app as stub_app
    from construction_ai.integrations.erpnext import ERPNextEvidenceResolver

    class _TestClientTransport:
        def __init__(self, client):
            self.client = client

        def get(self, path, params=None):
            return self.client.get(path, params=params).json()

    resolver = ERPNextEvidenceResolver(_TestClientTransport(TestClient(stub_app)), ORG)
    po, evidence = resolver.resolve_purchase_order("PO-1042-17")
    assert po.project_id == "PRJ-0042"
    assert po.amount == 4760.00
    # ERP observations are first-class snapshots (item 8).
    assert [e.field for e in evidence] == ["ERP_PURCHASE_ORDER_SNAPSHOT"]
    assert evidence[0].value["normalized_fields"]["supplier_id"] == "ABC Electric"
    quote, _ = resolver.resolve_quote("Q-8821")
    assert quote.approved is True
    assert resolver.resolve_quote("Q-9014")[0].approved is False


# --------------------------------------------------------------------------
# Reproducibility invariants
# --------------------------------------------------------------------------

COMPOSE = (ROOT / "docker-compose.yml").read_text()
REQUIRED_SERVICES = ["postgres", "redis", "minio", "migrate", "erpnext-stub", "api", "worker"]


@pytest.mark.parametrize("service", REQUIRED_SERVICES)
def test_compose_declares_every_required_service(service):
    assert re.search(rf"^  {re.escape(service)}:$", COMPOSE, re.M), f"{service} missing from docker-compose.yml"


def test_every_compose_image_is_pinned_by_digest():
    images = re.findall(r"^\s+image:\s*(\S+)$", COMPOSE, re.M)
    assert images, "no images found in docker-compose.yml"
    unpinned = [i for i in images if "@sha256:" not in i]
    assert unpinned == [], f"unpinned images: {unpinned}"


def test_dockerfile_base_image_is_pinned_by_digest():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^FROM \S+@sha256:[0-9a-f]{64}$", dockerfile, re.M)


def test_runtime_lock_is_fully_pinned():
    lines = [line.strip() for line in (ROOT / "requirements.lock.txt").read_text().splitlines()]
    requirements = [line for line in lines if line and not line.startswith("#")]
    assert requirements
    assert all("==" in line for line in requirements), [line for line in requirements if "==" not in line]


def test_env_example_documents_every_variable_the_code_reads():
    documented = {
        line.split("=", 1)[0].strip()
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line.strip() and not line.startswith("#") and "=" in line
    }
    sources = list((ROOT / "construction_ai").rglob("*.py")) + list((ROOT / "apps").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))
    read: set[str] = set()
    for path in sources:
        read.update(re.findall(r"os\.getenv\(\s*[\"']([A-Z0-9_]+)[\"']", path.read_text()))
        read.update(re.findall(r"os\.environ\[\s*[\"']([A-Z0-9_]+)[\"']", path.read_text()))
    # Names supplied by the runtime rather than by operators.
    ambient = {"LOG_LEVEL", "PATH", "HOME", "HEALTH_PATH", "ACCEPTANCE_JOB_TIMEOUT", "API_BASE", "TEST_DATABASE_URL", "REQUIRE_INTEGRATION"}
    missing = read - documented - ambient
    assert missing == set(), f"undocumented environment variables: {sorted(missing)}"


def test_no_declared_dependency_is_unused():
    """v0.3.0 declared sqlalchemy and never imported it. Keep that from recurring."""
    pyproject = (ROOT / "pyproject.toml").read_text()
    block = re.search(r"^dependencies = \[(.*?)^\]", pyproject, re.M | re.S)
    assert block, "could not locate the runtime dependencies block"
    declared = set(re.findall(r'"([a-zA-Z0-9_.-]+)(?:\[[^\]]*\])?==', block.group(1)))
    runtime_only = {"uvicorn"}  # entrypoint, never imported
    import_names = {"python-docx": "docx", "psycopg": "psycopg", "pypdf": "pypdf"}
    sources = "\n".join(p.read_text() for p in list((ROOT / "construction_ai").rglob("*.py")) + list((ROOT / "apps").rglob("*.py")))
    for package in declared - runtime_only:
        module = import_names.get(package, package.replace("-", "_"))
        assert re.search(rf"\b(?:import|from)\s+{re.escape(module)}\b", sources), f"{package} is declared but never imported"


def test_gitignore_excludes_local_runtime_state():
    ignored = (ROOT / ".gitignore").read_text()
    for pattern in ["*.db", "object_store/", ".env"]:
        assert pattern in ignored, f"{pattern} not gitignored"
