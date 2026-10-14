# Neura developer commands. Run `make help` for the list.
.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv
COMPOSE := docker compose -f deploy/compose/compose.yaml

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: ## Install the workspace and dev tools into .venv
	$(UV) sync --frozen

.PHONY: gen
gen: ## Regenerate code from proto/, catalog/ and contract models
	$(UV) run python tools/gen_reason_codes.py
	$(UV) run python tools/gen_proto.py
	$(UV) run python tools/gen_schemas.py

.PHONY: examples
examples: ## Refresh example execution-profile bindings to current digests
	$(UV) run python tools/refresh_example_bindings.py

.PHONY: check-generated
check-generated: ## Fail if any generated file is stale
	$(UV) run python tools/gen_reason_codes.py --check
	$(UV) run python tools/gen_proto.py --check
	$(UV) run python tools/gen_schemas.py --check
	$(UV) run python tools/refresh_example_bindings.py --check

.PHONY: fmt
fmt: ## Format code
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

.PHONY: lint
lint: ## Lint and check formatting
	$(UV) run ruff check .
	$(UV) run ruff format --check .

.PHONY: typecheck
typecheck: ## Strict type checking
	$(UV) run mypy packages tools tests

.PHONY: test
test: ## Run the test suite
	$(UV) run pytest

.PHONY: check
check: lint typecheck check-generated test ## Everything CI runs, except the dev-stack smoke test

.PHONY: dev
dev: ## Start the local stack (Postgres, control plane, site agent with its data plane, simulated robots)
	$(COMPOSE) up --build --detach --wait
	@echo "control plane: http://127.0.0.1:8080/v1/meta"
	@echo "site agent:    http://127.0.0.1:8090/v1/local/status (unenrolled until you run tools/smoke_api.py)"
	@echo "data plane:    http://127.0.0.1:8091/v1/local/dataplane"

.PHONY: dev-down
dev-down: ## Stop the local stack and delete its data
	$(COMPOSE) down --volumes --remove-orphans

.PHONY: dev-logs
dev-logs: ## Follow logs from the local stack
	$(COMPOSE) logs --follow

.PHONY: smoke
smoke: ## Build, start and verify the local stack end to end, then stop it
	tools/smoke_dev_stack.sh
