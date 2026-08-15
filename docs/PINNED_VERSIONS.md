# Pinned versions — v0.5.0-rc6.dev0 baseline

Everything the running system depends on is pinned here. Nothing in the stack
resolves a floating tag at build or run time.

## Runtime

| Component | Pin |
|---|---|
| Python | `3.12.12` (image digest below) |
| Direct Python deps | `pyproject.toml`, exact `==` |
| Transitive closure | `requirements.lock.txt` (installed with `--no-deps`) |
| Dev/test deps | `requirements-dev.lock.txt` |

Regenerate both locks with `make lock`. They are generated artifacts — never
hand-edit them.

## Container images

Compose references every image by digest, so a re-pull cannot silently change
the baseline. Tags are recorded alongside for human readability only.

| Service | Tag | Digest |
|---|---|---|
| PostgreSQL | `postgres:16.10-bookworm` | `sha256:38471f330eb885e04de130b768d6db4e10469e2311879c7e5c699f6d2d8a1c74` |
| Redis | `redis:7.4.6-alpine` | `sha256:3b73847e72874be07e6657b129a94761662b79bc0f679273757d4218573b2a98` |
| MinIO (object storage) | `minio/minio:RELEASE.2025-09-07T16-13-09Z` | `sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e` |
| App base | `python:3.12.12-slim-bookworm` | `sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c` |

## PostgreSQL

Server major version is pinned to **16**. The schema is applied only by
`scripts/migrate.py`, which records every applied file and its checksum in
`schema_migrations`. A migration whose contents change after being applied is a
hard error, not a silent re-run.

## ERPNext / Frappe

The default stack runs `apps/erpnext_stub` — a pinned, in-repo fake that serves
the exact ERPNext REST shapes `ERPNextEvidenceResolver` reads (`Supplier`,
`Purchase Order`, `Supplier Quotation`). It exists so the acceptance gate can run
offline and deterministically.

To point at a live instance instead, set `ERP_NEXT_URL`, `ERP_NEXT_API_KEY` and
`ERP_NEXT_API_SECRET` to a real ERPNext deployment. The connector code is
identical either way; only the base URL changes.

Upstream source snapshots used for this build are in `vendor/upstream_sources/`
(gitignored — 74 MB of archives). Their digests are the pin:

| Archive | sha256 |
|---|---|
| `erpnext-develop.zip` | `6c89620edd7017a09ddeea2849374cea5efa5889955ce7b0ab39f3cd4ee0ca7d` |
| `frappe-develop.zip` | `cfc53737ffbfc9148bcaa3b1ecc68a6e0d4e21cdfd23b89a9432b185b64759e6` |
| `hrms-develop.zip` | `4707adfc43e1f5223278fe7a024fab5c784ac2f3a47c8286cedc3ce722cc6706` |
| `langgraph-main.zip` | `ec6e49c1f5df286853df963ead27c10a3e2db5cf59fa6248b870b4cb88b259e0` |

These are `develop`/`main` branch snapshots, so the digest is the only durable
identifier — there is no upstream commit hash recorded in the archives. Before a
pilot deployment, re-vendor from tagged releases and record the tag plus commit
SHA here.

## LangGraph

`langgraph==1.2.6` is pinned in the optional `graph` extra and deliberately kept
out of the runtime closure: no module imports it yet. The executive controller
(`construction_ai/executive/controller.py`) is a plain deterministic function
today. Phase 35 is where that pin becomes load-bearing.

## Known gaps in this baseline

Recorded here rather than left implicit:

- `sqlalchemy` was declared as a dependency in v0.3.0 but never imported. Removed.
- `redis` was declared but unused until the worker landed in this phase.
- Multi-tenant isolation is **not** enforced at the query layer. `get_object`,
  `evidence_payloads`, `approval_payload` and friends take no `organization_id`.
  That is Phase 3 and it is a blocker for onboarding any real company.
