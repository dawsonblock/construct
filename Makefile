.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose
SCRATCH := .venv

.PHONY: help env up down logs ps migrate migrate-status seed acceptance gate test test-integration test-stack lint lock clean qualify-external-effects qualify qualify-full verify-artifact verify-schema verify-runtime

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from .env.example if missing
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

up: env ## Start the full stack (postgres, redis, minio, migrate, erpnext-stub, api, worker)
	$(COMPOSE) up -d --build
	@echo "api      http://localhost:$${API_PORT:-8000}"
	@echo "web UI   http://localhost:$${API_PORT:-8000}/approval-ui?id=<approval-id>"
	@echo "minio    http://localhost:$${MINIO_CONSOLE_PORT:-9001}"

down: ## Stop the stack and remove volumes
	$(COMPOSE) down -v

logs: ## Tail logs from every service
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps

migrate: env ## Apply pending migrations
	$(COMPOSE) run --rm migrate

migrate-status: env ## Report pending migrations without applying
	$(COMPOSE) run --rm migrate python scripts/migrate.py --status

seed: env ## Seed the demo organizations and issue their API keys
	$(COMPOSE) run --rm api python scripts/seed_demo.py

acceptance: ## Run the Phase 1 acceptance gate against the running stack
	$(COMPOSE) run --rm -e API_BASE=http://api:8000 api python scripts/acceptance.py

gate: up seed acceptance ## Full gate: up -> seed -> acceptance

test: ## Run the test suite on the host (DB tests skip if the stack is down)
	python -m pytest -q

test-integration: ## Run the test suite and FAIL if the database tests skip
	REQUIRE_INTEGRATION=1 python -m pytest -q

test-stack: env ## Run the full suite inside the app image, integration required
	INSTALL_DEV=1 $(COMPOSE) build api
	INSTALL_DEV=1 $(COMPOSE) run --rm -e REQUIRE_INTEGRATION=1 api python -m pytest -q

lint: ## Lint with the pinned ruff
	ruff check .

lock: ## Regenerate requirements.lock.txt / requirements-dev.lock.txt
	@rm -rf $(SCRATCH)-lock $(SCRATCH)-devlock
	python -m venv $(SCRATCH)-lock
	$(SCRATCH)-lock/bin/pip install -q --upgrade pip
	$(SCRATCH)-lock/bin/pip install -q .
	@{ echo "# Runtime lock for construction-ai-ops. Regenerate with 'make lock'; never hand-edit."; \
	   $(SCRATCH)-lock/bin/pip freeze --exclude-editable | grep -v '^construction-ai-ops' | sort; } > requirements.lock.txt
	python -m venv $(SCRATCH)-devlock
	$(SCRATCH)-devlock/bin/pip install -q --upgrade pip
	$(SCRATCH)-devlock/bin/pip install -q pytest==8.4.2 pytest-cov==7.0.0 ruff==0.16.0 hypothesis==6.151.4
	@{ echo "# Dev/test lock. Install on top of requirements.lock.txt. Regenerate with 'make lock'; never hand-edit."; \
	   $(SCRATCH)-devlock/bin/pip freeze | sort; } > requirements-dev.lock.txt
	@rm -rf $(SCRATCH)-lock $(SCRATCH)-devlock
	@echo "locks regenerated"

qualify-external-effects: ## Full clean-slate qualification of external-effect safety (requires PostgreSQL + Redis + ERP stub)
	@echo "==> Qualifying external-effect safety: clean-slate PostgreSQL + Redis + ERP stub"
	@echo "    This target requires the full stack to be running (make up)."
	@echo "    It runs every external-effect test with REQUIRE_INTEGRATION=1 so skips fail."
	REQUIRE_INTEGRATION=1 python -m pytest -q \
		tests/test_executor.py \
		tests/test_reconciliation.py \
		tests/test_reconcile_unknown.py \
		tests/test_stale_approval.py \
		tests/test_decision_fingerprint.py \
		tests/test_execution_preconditions.py \
		tests/test_external_action_properties.py \
		tests/test_external_action_state_machine.py \
		tests/test_schedule_of_values.py \
		tests/test_duplicate_detection.py \
		tests/test_verification_packet_immutable.py \
		tests/test_evidence_freshness.py \
		tests/test_integration_pipeline.py \
		tests/test_erp_supplier_verification.py \
		tests/test_crash_injection.py \
		tests/test_crash_injection_erp.py \
		tests/test_replay_fingerprints.py
	@echo "==> External-effect qualification passed."

qualify: ## Full qualification: acyclic attestation chain (requires full stack)
	@echo "==> Full qualification: acyclic attestation chain"
	@echo "    rc7: PayloadTree -> PAYLOAD_MANIFEST -> Qualification -> RELEASE_ATTESTATION"
	@echo "    This target requires the full stack to be running (make up)."
	@echo ""
	@echo "[1/4] Generating payload manifest (PAYLOAD_MANIFEST.json)..."
	DATABASE_URL="postgresql://construction:construction@localhost:5432/construction_ai" python scripts/release_manifest.py --output PAYLOAD_MANIFEST.json
	@echo ""
	@echo "[2/4] Generating gate artifacts and qualification report..."
	python scripts/generate_gate_artifacts.py
	python scripts/qualification_report.py --pytest --output QUALIFICATION_REPORT.json
	@echo ""
	@echo "[3/4] Generating release attestation (RELEASE_ATTESTATION.json)..."
	python scripts/release_attestation.py --output RELEASE_ATTESTATION.json
	@echo ""
	@echo "[4/4] Verifying payload manifest..."
	python scripts/verify_payload_manifest.py --manifest PAYLOAD_MANIFEST.json
	@echo ""
	@echo "==> Release attestation chain complete"
	@cat QUALIFICATION_REPORT.json | python -c "import json,sys; r=json.load(sys.stdin); print(f'Qualified: {r[\"qualified\"]}')"

qualify-full: ## Full clean-slate qualification: reset DB + run all categories + report (requires full stack)
	@echo "==> Full clean-slate qualification (Phase 30)"
	@echo "    This target requires the full stack to be running (make up)."
	@echo "    It will DROP and RECREATE the database schema."
	python scripts/full_qualification.py

verify-artifact: ## Verify payload manifest against files on disk (no DB required)
	@echo "==> Verifying payload manifest (artifact-only, no DB)..."
	python scripts/verify_payload_manifest.py --manifest PAYLOAD_MANIFEST.json

verify-schema: ## Verify database schema, migrations, and RLS (requires PostgreSQL)
	@echo "==> Verifying database schema (requires PostgreSQL)..."
	DATABASE_URL="postgresql://construction:construction@localhost:5432/construction_ai" python scripts/upgrade_gate.py
	DATABASE_URL="postgresql://construction:construction@localhost:5432/construction_ai" python scripts/security_gate.py

verify-runtime: ## Verify runtime services (Redis, ERP stub, API)
	@echo "==> Verifying runtime services..."
	@echo "    Checking Redis..."
	@redis-cli ping 2>/dev/null || echo "    WARNING: Redis not reachable"
	@echo "    Checking ERP stub..."
	@curl -s http://localhost:8000/health 2>/dev/null || echo "    WARNING: ERP stub not reachable"
	@echo "    Checking API..."
	@curl -s http://localhost:8001/health 2>/dev/null || echo "    WARNING: API not reachable"
	@echo "==> Runtime verification complete"

clean: ## Remove local caches and runtime artifacts
	rm -rf .pytest_cache .ruff_cache object_store construction_ai_ops.db
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
