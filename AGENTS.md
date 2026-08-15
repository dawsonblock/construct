# Build & Test Commands

## Test
- `python -m pytest -q` — full suite (DB tests skip if PostgreSQL is down)
- `REQUIRE_INTEGRATION=1 python -m pytest -q` — full suite (skips become failures)
- `make qualify-external-effects` — external-effect safety tests under REQUIRE_INTEGRATION=1
- `make test-integration` — full suite with skips as failures

## Lint
- `ruff check .` — lint with pinned ruff
- `ruff check . --fix` — auto-fix lint errors

## Database
- `DATABASE_URL="postgresql://construction:construction@localhost:5432/construction_ai" python scripts/migrate.py` — apply migrations
- `DATABASE_URL=... python scripts/migrate.py --status` — check migration status

## Lock files
- `make lock` — regenerate requirements.lock.txt and requirements-dev.lock.txt

## Release
- `DATABASE_URL=... python scripts/release_manifest.py --output MANIFEST.<version>.json` — generate release manifest
- `python scripts/qualification_report.py --output QUALIFICATION_REPORT.json` — generate qualification report (without tests)
- `python scripts/qualification_report.py --pytest --output QUALIFICATION_REPORT.json` — generate qualification report (runs tests)
- `make qualify` — run all tests + generate qualification report (requires full stack)
- `make qualify-full` — full clean-slate: reset DB + run all categories + report (requires full stack)

## Full stack
- `make up` — start Docker Compose stack (PostgreSQL, Redis, MinIO, ERP stub, API, worker)
- `make down` — stop stack and remove volumes

# Architecture Notes

## Central invariant
"Approved Local Intent ⇒ Exactly One Verified External Financial Effect"

The executor enforces this via a conjunction of preconditions:
1. Approval is in `approved` status
2. Approval is not stale (project-wide + decision fingerprint match)
3. Evidence is fresh (within policy bounds)
4. External action is reserved atomically (INSERT ON CONFLICT DO NOTHING)
5. ERP readback confirms all financial fields match

## Version surfaces
All version surfaces must agree (enforced by test_repository_integrity.py):
- `VERSION` file
- `pyproject.toml` project version
- `construction_ai/__init__.py` __version__
- `apps/erpnext_stub/main.py` FastAPI version
- `Dockerfile` ARG APP_VERSION

## Migrations
Migrations are transactional and forward-only. Never edit an applied migration.
Current count: 29 (through 029_rc8_supersession_uniqueness.sql).

## Test categories
- Unit tests (pure functions, no DB)
- Integration tests (need PostgreSQL)
- Security tests (need API HTTP server)
- Adversarial corpus (hostile inputs against invariants)
- Property tests (Hypothesis-based)
- Crash/recovery tests (crash hooks at external boundaries)
- External-effect tests (state machine, idempotency, readback)
- Release-integrity tests (repository + artifact)
