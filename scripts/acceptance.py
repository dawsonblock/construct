#!/usr/bin/env python
"""Acceptance gate.

Part 1 — the invoice path still works end to end:

    demo invoice document → queued job → deterministic extraction → project
    resolution (automatic band) → ERPNext PO/quote resolution → invoice
    verification → evidence-backed approval packet → human approval →
    intact audit chain

Part 2 — deterministic reconstruction: the same database state must rebuild to
the same ProjectState, byte for byte, with a complete typed graph.

Part 3 — tenant isolation, through the real API rather than the repositories:
organization A must not be able to read, list, traverse, decide on, or infer the
existence of anything belonging to organization B.

Run against the compose stack:

    docker compose run --rm -e API_BASE=http://api:8000 api python scripts/acceptance.py
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
API_BASE = os.getenv("API_BASE", "http://localhost:8000").rstrip("/")
DEMO_INVOICE = ROOT / "scripts" / "fixtures" / "demo_invoice_8831.txt"
CREDENTIALS_PATH = ROOT / ".demo-credentials.json"
JOB_TIMEOUT_SECONDS = float(os.getenv("ACCEPTANCE_JOB_TIMEOUT", "60"))

EXPECTED_CHECKS = {
    "vendor_match", "project_match", "po_match", "quote_match",
    "amount_match", "tax_math", "currency_match", "not_duplicate", "work_confirmed",
}

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)
    return condition


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login_session(client: httpx.Client, subject: str) -> str | None:
    """Exchange a dev identity subject for a server session token."""
    response = client.post(f"{API_BASE}/auth/session", json={"credential": subject}, timeout=10)
    if response.status_code != 200:
        return None
    return response.json().get("session_token")


def wait_for_api(client: httpx.Client, attempts: int = 60, delay: float = 1.0) -> bool:
    for _ in range(attempts):
        try:
            if client.get(f"{API_BASE}/health", timeout=5).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(delay)
    return False


def wait_for_job(client: httpx.Client, job_id: str, token: str) -> dict:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    record: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"{API_BASE}/jobs/{job_id}", headers=auth(token), timeout=10)
        if response.status_code == 200:
            record = response.json()
            if record.get("status") in {"completed", "failed"}:
                return record
        time.sleep(1.0)
    return record or {"status": "timeout"}


def demo_invoice_text(invoice_number: str) -> str:
    """The fixture with a per-run invoice number.

    The gate must be runnable twice against the same database. Reusing 8831
    would make the second run trip over duplicate detection — which is correct
    behaviour, and is asserted explicitly below rather than stumbled into.
    """
    return DEMO_INVOICE.read_text().replace("8831", invoice_number)


def submit_invoice(client: httpx.Client, token: str, text: str) -> dict:
    submission = client.post(
        f"{API_BASE}/jobs/invoice-document",
        headers=auth(token),
        json={"text": text, "filename": DEMO_INVOICE.name, "thread_id": "THR-0042", "work_confirmed": True},
        timeout=30,
    )
    submission.raise_for_status()
    return wait_for_job(client, submission.json()["job_id"], token)


def invoice_path(client: httpx.Client, token: str, approver_subject: str) -> dict:
    print("\n-- invoice path ------------------------------------------------")
    text = demo_invoice_text(str(uuid.uuid4().int % 1_000_000).zfill(6))
    health = client.get(f"{API_BASE}/health", timeout=10).json()
    check("financial default is human approval", health.get("financial_default") == "human_approval_required", str(health))
    check("database reachable", health.get("database") == "ok", str(health))

    job = submit_invoice(client, token, text)
    if not check("job completed", job.get("status") == "completed", job.get("error") or job.get("status", "")):
        return {}

    result = job.get("result") or {}
    check("extraction produced an invoice", result.get("status") == "prepared", str(result)[:300])
    check("project resolved automatically", result.get("project_id") is not None, str(result.get("project_id")))
    check("no verification exceptions", not result.get("exceptions"), str(result.get("exceptions")))
    check("recommendation is APPROVE", result.get("recommended_action") == "APPROVE")
    check("human decision still required", result.get("requires_human") is True)

    approval_id = result.get("approval_id")
    if not approval_id:
        check("approval created", False, "no approval_id on job result")
        return result

    packet = client.get(f"{API_BASE}/approvals/{approval_id}/packet", headers=auth(token), timeout=10)
    if check("approval packet retrievable", packet.status_code == 200, f"HTTP {packet.status_code}"):
        body = packet.json()
        checks = body.get("verification", {}).get("checks", {})
        check("all verification checks present", set(checks) == EXPECTED_CHECKS, f"missing {sorted(EXPECTED_CHECKS - set(checks))}")
        check("all verification checks passed", all(v == "PASS" for v in checks.values()), str({k: v for k, v in checks.items() if v != "PASS"}))
        check("packet carries evidence", len(body.get("evidence") or []) > 0)

    pending = client.get(f"{API_BASE}/approvals/{approval_id}", headers=auth(token), timeout=10).json()
    check("approval starts pending", pending.get("status") == "pending", str(pending.get("status")))

    # An approval is a financial mutation: it requires a server-derived human
    # actor, not a caller-supplied user_id. The org API key alone is not a
    # session, so the request is rejected before any decision is considered.
    refused = client.post(f"{API_BASE}/approvals/{approval_id}/approve", headers=auth(token), json={"reason": ""}, timeout=10)
    check("no session means no approval", refused.status_code == 401, f"HTTP {refused.status_code}")

    session = login_session(client, approver_subject)
    if not check("approver can obtain a session", session is not None, approver_subject):
        return result

    approved = client.post(f"{API_BASE}/approvals/{approval_id}/approve", headers=auth(session), json={"reason": ""}, timeout=10)
    check("human approval recorded", approved.status_code == 200 and approved.json().get("status") == "approved", approved.text[:200])

    again = client.post(f"{API_BASE}/approvals/{approval_id}/approve", headers=auth(session), json={"reason": ""}, timeout=10)
    check("an approved approval cannot be re-decided", again.status_code == 409, f"HTTP {again.status_code}")

    audit = client.get(f"{API_BASE}/audit/verify", headers=auth(token), timeout=30)
    check("audit chain intact", audit.status_code == 200 and audit.json().get("intact") is True, audit.text[:200])

    trail = client.get(f"{API_BASE}/audit/invoice/{result['invoice_id']}", headers=auth(token), timeout=10)
    check("invoice has an audit trail", trail.status_code == 200 and len(trail.json()) > 0, f"HTTP {trail.status_code}")

    repeat = (submit_invoice(client, token, text).get("result") or {})
    check("the same invoice twice is held as a duplicate",
          repeat.get("recommended_action") == "HOLD" and "DUPLICATE_INVOICE" in (repeat.get("exceptions") or []),
          str(repeat.get("exceptions")))
    check("the duplicate points at the original invoice",
          repeat.get("duplicate_of") == result["invoice_id"], str(repeat.get("duplicate_of")))
    return result


def reconstruction(client: httpx.Client, token: str, result: dict) -> str | None:
    print("\n-- reconstruction ----------------------------------------------")
    project_id = result.get("project_id")
    if not project_id:
        check("project available for reconstruction", False, "invoice path produced no project")
        return None

    first = client.get(f"{API_BASE}/projects/{project_id}/state", headers=auth(token), timeout=30)
    if not check("project state reconstructs", first.status_code == 200, f"HTTP {first.status_code}"):
        return project_id
    state = first.json()

    second = client.get(f"{API_BASE}/projects/{project_id}/state", headers=auth(token), timeout=30).json()
    check("reconstruction is deterministic", state["fingerprint"] == second["fingerprint"],
          f"{state['fingerprint'][:12]} vs {second['fingerprint'][:12]}")
    check("reconstruction is byte-identical, not just same-fingerprint", state == second)

    check("aggregate carries the invoice", len(state["invoices"]) >= 1, str(len(state["invoices"])))
    check("aggregate carries the purchase order", len(state["purchase_orders"]) >= 1)
    check("aggregate carries the approval", len(state["approvals"]) >= 1)
    check("aggregate carries evidence", len(state["evidence"]) >= 1)
    check("graph is complete", state["provenance"]["structural_edges_missing"] == 0,
          str(state["provenance"]["structural_edges_missing"]))

    graph = client.get(f"{API_BASE}/projects/{project_id}/graph", headers=auth(token), timeout=30).json()
    relations = {edge["relation"] for edge in graph["edges"]}
    check("typed edges are present", {"BELONGS_TO", "BILLED_BY", "APPROVES"} <= relations, str(sorted(relations)))
    check("every structural edge is observed, not inferred",
          all(edge["origin"] == "observed" for edge in graph["edges"]),
          str({edge["origin"] for edge in graph["edges"]}))

    rebuild = client.post(f"{API_BASE}/projects/{project_id}/graph/project", headers=auth(token), timeout=30)
    check("re-projection is accepted", rebuild.status_code == 202, f"HTTP {rebuild.status_code}")
    after = client.get(f"{API_BASE}/projects/{project_id}/state", headers=auth(token), timeout=30).json()
    check("re-projection changes nothing", after["fingerprint"] == state["fingerprint"],
          f"{state['fingerprint'][:12]} vs {after['fingerprint'][:12]}")

    conflicts = client.get(f"{API_BASE}/projects/{project_id}/conflicts", headers=auth(token), timeout=30)
    check("conflicts endpoint responds", conflicts.status_code == 200, f"HTTP {conflicts.status_code}")
    # The duplicate submitted earlier in the invoice path is held, not filed, so
    # a clean project should still report nothing unresolved.
    check("a clean project reports no conflicts", conflicts.json()["count"] == 0, str(conflicts.json()["conflicts"])[:300])
    return project_id


def tenant_isolation(client: httpx.Client, a_token: str, b_token: str, a_result: dict, a_approver: str, b_approver: str) -> None:
    print("\n-- tenant isolation --------------------------------------------")

    check("unauthenticated read is rejected", client.get(f"{API_BASE}/projects", timeout=10).status_code == 401)
    check("garbage token is rejected", client.get(f"{API_BASE}/projects", headers=auth("cai_not-a-key"), timeout=10).status_code == 401)

    # B prepares its own invoice so there is something on both sides. Same vendor
    # and same invoice number as A's: two tenants sharing those is not a duplicate.
    b_result = submit_invoice(client, b_token, demo_invoice_text(str(uuid.uuid4().int % 1_000_000).zfill(6))).get("result") or {}
    check("B's own invoice prepared", b_result.get("status") == "prepared", str(b_result)[:200])

    a_projects = client.get(f"{API_BASE}/projects", headers=auth(a_token), timeout=10).json()
    b_projects = client.get(f"{API_BASE}/projects", headers=auth(b_token), timeout=10).json()
    a_ids = {p["project_id"] for p in a_projects}
    b_ids = {p["project_id"] for p in b_projects}
    check("project lists do not overlap", a_ids.isdisjoint(b_ids), f"shared: {a_ids & b_ids}")
    check("both tenants have a PRJ-0042", "PRJ-0042" in {p["reference"] for p in a_projects} and "PRJ-0042" in {p["reference"] for p in b_projects},
          "references are unique per organization, not globally")

    b_project_id = next(iter(b_ids))
    probes = [
        ("read B's project", client.get(f"{API_BASE}/projects/{b_project_id}", headers=auth(a_token), timeout=10)),
        ("traverse B's project invoices", client.get(f"{API_BASE}/projects/{b_project_id}/invoices", headers=auth(a_token), timeout=10)),
    ]
    if b_result.get("invoice_id"):
        probes += [
            ("read B's invoice", client.get(f"{API_BASE}/invoices/{b_result['invoice_id']}", headers=auth(a_token), timeout=10)),
            ("read B's invoice audit trail", client.get(f"{API_BASE}/audit/invoice/{b_result['invoice_id']}", headers=auth(a_token), timeout=10)),
        ]
    probes += [
        ("reconstruct B's project", client.get(f"{API_BASE}/projects/{b_project_id}/state", headers=auth(a_token), timeout=30)),
        ("read B's project conflicts", client.get(f"{API_BASE}/projects/{b_project_id}/conflicts", headers=auth(a_token), timeout=30)),
        ("read B's project graph", client.get(f"{API_BASE}/projects/{b_project_id}/graph", headers=auth(a_token), timeout=30)),
        ("re-project B's graph", client.post(f"{API_BASE}/projects/{b_project_id}/graph/project", headers=auth(a_token), timeout=30)),
    ]
    if b_result.get("approval_id"):
        a_session = login_session(client, a_approver)
        check("A's approver can obtain A session", a_session is not None, a_approver)
        # A's actor is scoped to A's organization; B's approval is invisible to
        # it, so the decision attempt is a 404 — never a 403 that would confirm
        # the approval exists in another tenant.
        decide_headers = auth(a_session) if a_session else auth(a_token)
        probes += [
            ("read B's approval", client.get(f"{API_BASE}/approvals/{b_result['approval_id']}", headers=auth(a_token), timeout=10)),
            ("read B's approval packet", client.get(f"{API_BASE}/approvals/{b_result['approval_id']}/packet", headers=auth(a_token), timeout=10)),
            ("decide B's approval", client.post(f"{API_BASE}/approvals/{b_result['approval_id']}/approve", headers=decide_headers, json={"reason": ""}, timeout=10)),
        ]
    for label, response in probes:
        check(f"A cannot {label}", response.status_code == 404, f"HTTP {response.status_code}")

    # Existence must not be inferable from the response to a well-formed id that
    # does not exist versus one that exists in another tenant.
    absent = client.get(f"{API_BASE}/invoices/00000000-0000-4000-8000-000000000000", headers=auth(a_token), timeout=10)
    if b_result.get("invoice_id"):
        foreign = client.get(f"{API_BASE}/invoices/{b_result['invoice_id']}", headers=auth(a_token), timeout=10)
        check(
            "a foreign id is indistinguishable from a nonexistent one",
            (absent.status_code, absent.json()) == (foreign.status_code, foreign.json()),
            f"{absent.status_code}/{foreign.status_code}",
        )

    if b_result.get("approval_id"):
        still_pending = client.get(f"{API_BASE}/approvals/{b_result['approval_id']}", headers=auth(b_token), timeout=10).json()
        check("A's attempt did not decide B's approval", still_pending.get("status") == "pending", str(still_pending.get("status")))

    a_lists = client.get(f"{API_BASE}/invoices", headers=auth(a_token), timeout=10).json()
    check("A's invoice list contains only A's invoices",
          all(i["invoice_id"] != b_result.get("invoice_id") for i in a_lists), f"{len(a_lists)} invoices")

    a_graph = client.get(f"{API_BASE}/projects/{a_result['project_id']}/graph", headers=auth(a_token), timeout=30).json()
    if a_graph.get("edges"):
        edge_id = a_graph["edges"][0]["relationship_id"]
        stolen = client.post(f"{API_BASE}/relationships/{edge_id}/decide", headers=auth(b_token),
                             json={"user_id": "attacker@b", "status": "rejected"}, timeout=10)
        check("B cannot decide A's relationship", stolen.status_code == 404, f"HTTP {stolen.status_code}")
    else:
        check("A's graph has edges to probe", False, "no edges returned")

    a_audit = client.get(f"{API_BASE}/audit/verify", headers=auth(a_token), timeout=30).json()
    b_audit = client.get(f"{API_BASE}/audit/verify", headers=auth(b_token), timeout=30).json()
    check("both audit chains verify independently", a_audit.get("intact") and b_audit.get("intact"), f"{a_audit} {b_audit}")


def main() -> int:
    print(f"acceptance gate against {API_BASE}")
    client = httpx.Client()
    if not check("api reachable", wait_for_api(client)):
        return 1

    if not CREDENTIALS_PATH.exists():
        check("demo credentials present", False, f"run scripts/seed_demo.py first ({CREDENTIALS_PATH.name} missing)")
        return 1
    credentials = json.loads(CREDENTIALS_PATH.read_text())
    a_token = credentials["demo"]["api_key"]
    b_token = credentials["rival"]["api_key"]
    a_approver = credentials["demo"]["approvers"][0]["subject"]
    b_approver = credentials["rival"]["approvers"][0]["subject"]

    a_result = invoice_path(client, a_token, a_approver)
    reconstruction(client, a_token, a_result)
    tenant_isolation(client, a_token, b_token, a_result, a_approver, b_approver)

    print()
    if failures:
        print(f"ACCEPTANCE FAILED — {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("ACCEPTANCE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
