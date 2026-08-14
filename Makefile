.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose
SCRATCH := .venv

.PHONY: help env up down logs ps migrate migrate-status seed acceptance gate test test-integration test-stack lint lock clean

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
	$(SCRATCH)-devlock/bin/pip install -q pytest==8.4.2 pytest-cov==7.0.0 ruff==0.16.0
	@{ echo "# Dev/test lock. Install on top of requirements.lock.txt. Regenerate with 'make lock'."; \
	   $(SCRATCH)-devlock/bin/pip freeze | sort; } > requirements-dev.lock.txt
	@rm -rf $(SCRATCH)-lock $(SCRATCH)-devlock
	@echo "locks regenerated"

clean: ## Remove local caches and runtime artifacts
	rm -rf .pytest_cache .ruff_cache object_store construction_ai_ops.db
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
