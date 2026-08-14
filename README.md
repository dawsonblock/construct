<div align="center">

# Construction AI Ops

### Evidence-First Construction Operations Control Plane

[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.128-009688.svg)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791.svg)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-7.4-DC382D.svg)](https://redis.io/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Tests](https://img.shields.io/badge/tests-116%20passed-brightgreen.svg)]()
[![Lint](https://img.shields.io/badge/lint-ruff%20clean-brightgreen.svg)]()

**Financial actions fail closed. Confidence never substitutes for authorization.**

</div>

---

## What is this?

Construction AI Ops is an AI control plane for construction back-office operations. It sits between your ERP (ERPNext) and your inbox, turning unstructured invoices and documents into evidence-backed, verified, human-approved financial actions.

The pipeline stages are deliberately **not collapsed** — each stage is deterministic, auditable, and independently testable:

```text
Observation → Evidence → Resolved State → Decision → Verification → Policy → Human/Action
```

No model output can authorize a financial transaction. Only a named human can.

### Current Vertical Slice

Email/attachment → immutable normalization → document extraction → vendor/project evidence → exact ERPNext PO/quote resolution → deterministic invoice verification → evidence-backed approval packet → human approval → tamper-evident, per-tenant audit chain.

---

## Quick Start

```bash
cp .env.example .env && docker compose up -d --wait
```

That starts, all images pinned by digest:

| Service | Description |
| --- | --- |
| **postgres** | Authoritative relational state with row-level security |
| **redis** | Job queue (ids only — state lives in the database) |
| **minio** | Content-addressed object storage for documents |
| **migrate** | One-shot: checksummed migrations + app-role bootstrap |
| **erpnext-stub** | In-repo ERPNext REST fixture server (swap for live via `ERP_NEXT_URL`) |
| **api** | FastAPI app + approval web UI at `/approval-ui?id=<approval-id>` |
| **worker** | Job consumer |

### Common Commands

```bash
make gate              # up → seed demo tenants → full acceptance gate
make test              # test suite (DB tests skip if stack is down)
make test-integration  # same, but fail rather than skip DB tests
make lint              # ruff check
make down              # stop and drop volumes
```

---

## Architecture

```text
Gmail / Microsoft Graph
        ↓
message fetch + attachment fetch
        ↓
immutable communication + content-addressed document
        ↓
text/table extraction + document classification
        ↓
conservative invoice extraction
        ↓
exact-first vendor/project resolution
        ↓
ERPNext PO + supplier quotation evidence
        ↓
deterministic verification (8 checks)
        ↓
evidence-backed approval packet
        ↓
human approve / hold / reject
        ↓
ERPNext submit policy gate
        ↓
tamper-evident audit
```

### Invoice Verification Checks

Every invoice passes through 8 deterministic checks before reaching a human approver:

| Check | Exception on failure | Description |
| --- | --- | --- |
| `vendor_match` | `UNKNOWN_OR_MISMATCHED_VENDOR` | Vendor identity matches the PO |
| `project_match` | `INVALID_PROJECT` | Invoice project aligns with PO project |
| `po_match` | `NO_OR_MISMATCHED_PO` | PO reference matches |
| `quote_match` | `NO_OR_UNAPPROVED_QUOTE` | Quote exists and is approved |
| `amount_match` | `AMOUNT_MISMATCH` | Invoice total matches PO (and quote if present) |
| `tax_math` | `TAX_MISMATCH` | Subtotal + tax = total |
| `not_duplicate` | `DUPLICATE_INVOICE` | Not a re-submission of an existing invoice |
| `work_confirmed` | `WORK_NOT_CONFIRMED` | Work completion has been confirmed |

---

## API Reference

All endpoints require a bearer token (`Authorization: Bearer cai_...`). Tenant identity is resolved server-side — no request body or query parameter can select an organization.

### Health

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Service health, version, database status |

### Ingestion

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/ingest/gmail` | Accept a Gmail webhook payload |
| `POST` | `/ingest/microsoft` | Accept a Microsoft Graph webhook payload |

### Projects

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/projects` | List all projects for the tenant |
| `GET` | `/projects/{id}` | Get a single project |
| `GET` | `/projects/{id}/invoices` | List invoices for a project |
| `GET` | `/projects/{id}/state` | Deterministic reconstruction + fingerprint |
| `GET` | `/projects/{id}/conflicts` | Detected conflicts (`?severity=high`) |
| `GET` | `/projects/{id}/graph` | Graph view (`?status=proposed`) |
| `POST` | `/projects/{id}/graph/project` | Idempotent re-derivation of graph |

### Invoices

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/invoices` | List invoices (`?status=prepared`) |
| `GET` | `/invoices/{id}` | Get a single invoice |

### Jobs

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/jobs/invoice-document` | Enqueue an invoice document for processing |
| `GET` | `/jobs/{id}` | Get job status |

### Approvals

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/approvals` | List approvals (`?status=pending`) |
| `GET` | `/approvals/{id}` | Get approval details |
| `GET` | `/approvals/{id}/packet` | Get the evidence-backed approval packet |
| `POST` | `/approvals/{id}/approve` | Approve (requires human actor) |
| `POST` | `/approvals/{id}/hold` | Hold (requires human actor) |
| `POST` | `/approvals/{id}/reject` | Reject (requires human actor) |

### Relationships

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/relationships/{id}/decide` | Promote or reject an inferred relationship |

### Audit

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/audit/verify` | Verify the tamper-evident audit chain |
| `GET` | `/audit/invoice/{id}` | Audit trail for a specific invoice |

### Web UI

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/approval-ui` | Human approval web interface (`?id=<approval-id>`) |

---

## Tenancy is the Trust Boundary

[docs/TENANCY.md](docs/TENANCY.md) is the contract. In short:

- **`organization_id`** is the leading column of every tenant-owned primary key. Nothing derives ownership from JSON.
- Primary keys are **UUIDs**; `PRJ-0042` and `INV-8831` are `reference` columns, unique **per organization**. Two tenants can both have a `PRJ-0042`.
- Repositories have **no unscoped method** — `invoices.get(scope=..., invoice_id=...)` is the only shape there is.
- Tenant identity comes from a **bearer token** resolved server-side. No request body, query parameter, or job payload can select an organization.
- **Row-level security** is forced on every tenant table. The app connects as a **non-superuser** role — superusers bypass RLS and would make the whole thing decorative.
- Cross-tenant reads return **404, never 403** — a 403 is an existence oracle.

---

## Reconstruction

```
same database state ⇒ same reconstructed project state
```

`repos.projects.reconstruct(scope=...)` rebuilds a project from persistent state alone — no model call, no clock, total ordering, writes nothing. `ProjectState.fingerprint()` makes that testable.

The typed graph keeps `G_observed`, `G_derived`, and `G_approved` distinguishable:

- Structural edges are projected from authoritative columns
- Inference may only write `status='proposed'`
- Promotion needs a human or a named deterministic rule
- A database CHECK blocks `ai` from ever being the decider

Conflicts are **derived at reconstruction time** rather than stored, so they cannot drift from the state they describe.

---

## Acceptance Gates

`make gate` runs `scripts/acceptance.py` against the live stack. It fails on any regression:

- **Invoice path** — demo document → queued job → deterministic extraction → automatic project resolution → ERPNext PO/quote resolution → 8 verification checks → evidence-backed packet → human approval (an `ai` actor is refused; a decided approval cannot be re-decided) → intact audit chain → the same invoice again is held as a duplicate.
- **Reconstruction** — two rebuilds are byte-identical, the graph is complete, every structural edge is `observed`, re-projection changes nothing, and a clean project reports no conflicts.
- **Cross-tenant attacks** — organization A attempting to read, list, traverse, decide on, or infer the existence of B's projects, invoices, approvals, packets, jobs, audit trail, reconstructed state, conflicts, graph, and relationships. Plus raw SQL with no `WHERE` clause under a scope, a connection that never set one, and an `INSERT` naming another tenant's id.

CI runs all gates on every push with `REQUIRE_INTEGRATION=1`.

---

## Reproducibility

- Dependencies, images, and upstream sources are **pinned by digest** — see [docs/PINNED_VERSIONS.md](docs/PINNED_VERSIONS.md)
- Locks are generated artifacts — regenerate with `make lock`, never hand-edit
- Schema changes go in a new `migrations/NNN_*.sql` — editing an applied migration is a hard error (checksum comparison)
- Project resolution benchmark: **1,000 cases** — top-1 accuracy 1.000, zero unsafe automatic assignments

---

## Project Layout

```text
apps/
  api/               FastAPI app and approval UI
  worker/            job consumer
  erpnext_stub/      ERPNext REST fixture server
construction_ai/
  persistence/       Scope, Database, connection pool, scoped repositories
  graph/             structural projection into typed nodes and edges
  reconstruction/    deterministic ProjectState and conflict detection
  extraction/        deterministic document extraction
  resolution/        project and identity resolution
  verification/      invoice checks (8 deterministic checks)
  policy/            what may happen without a human
  executive/         the invoice pipeline + controller
  jobs/              Redis-backed queue and handlers
  integrations/      ERPNext HTTP adapter + evidence resolver
  domain/            core domain models (Invoice, PO, Quote, Approval, ...)
  documents/         document extraction + classification
  ingestion/         email normalization (Gmail / Microsoft Graph)
migrations/          ordered SQL, applied by scripts/migrate.py
scripts/             migrate, bootstrap_app_role, create_organization, acceptance
tests/               unit tests + PostgreSQL-backed tenancy and pipeline suites
docs/                architecture, tenancy, reconstruction, pinned versions
```

---

## Tech Stack

| Layer | Technology |
| --- | --- |
| Language | Python 3.12 |
| API | FastAPI 0.128 + Uvicorn |
| Database | PostgreSQL 16 (psycopg 3.3, RLS-enforced) |
| Queue | Redis 7.4 |
| Object Storage | MinIO (S3-compatible) |
| ERP Integration | ERPNext REST API |
| Document Extraction | pypdf, pdfplumber, openpyxl, python-docx |
| Linting | ruff 0.16 |
| Testing | pytest 8.4 (116 tests) |
| Containerization | Docker Compose (images pinned by digest) |

---

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and adjust:

```bash
cp .env.example .env
```

Key variables:

| Variable | Default | Description |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://...` | Database for migrations (superuser) |
| `APP_DATABASE_URL` | `postgresql://...` | Database for app (non-superuser, RLS-enforced) |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection for job queue |
| `ERP_NEXT_URL` | `http://localhost:8100` | ERPNext base URL (stub by default) |
| `API_PORT` | `8000` | API port |
| `OBJECT_STORE_BACKEND` | `local` | `local` or `s3` |
| `S3_BUCKET` | `construction-ai-documents` | S3 bucket for document storage |

---

## Known Gaps

Stated plainly so they are not mistaken for solved problems:

- **Nothing calls `propose()` in production yet.** The candidate path is built, tested, and enforced, but the first real producers are entity deduplication and invoice-to-PO matching.
- **`decisions` has a table and no writer.** The executive controller returns actions without persisting them.
- **No source-authority ordering.** Conflicts are surfaced, never resolved; `EVIDENCE_CONTRADICTION` reports disagreement rather than picking a winner.
- **API keys are the whole auth model.** No users, roles, sessions, or separation of duties beyond "the decider must not be `ai`".
- **No mailbox synchronization.** Gmail/Graph connectors fetch individual messages; there is no cursor, no history tracking, no incremental sync.
- **No entity deduplication or onboarding importer.** An organization starts with empty AI memory.
- **Project resolution is conservative by construction.** Automatic filing requires corroborated hard identifiers; confidence is hand-weighted, not calibrated.

---

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system architecture and authority boundary
- [docs/TENANCY.md](docs/TENANCY.md) — tenant isolation contract
- [docs/RECONSTRUCTION.md](docs/RECONSTRUCTION.md) — deterministic reconstruction
- [docs/PINNED_VERSIONS.md](docs/PINNED_VERSIONS.md) — dependency and image pins
- [docs/BUILD_REPORT.md](docs/BUILD_REPORT.md) — v0.3.0 qualification report

---

<div align="center">

**v0.4.0.dev0** — Proprietary. Not for distribution.

</div>