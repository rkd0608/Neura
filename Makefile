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

.PHONY: models-setup
models-setup: ## Also install PyTorch and LeRobot (the models group), for model tests and benchmarks
	$(UV) sync --frozen --group models

.PHONY: gen
gen: ## Regenerate code and reference docs from proto/, catalog/, contract models and the API spec
	$(UV) run python tools/gen_reason_codes.py
	$(UV) run python tools/gen_outbound.py
	$(UV) run python tools/gen_dashboards.py
	$(UV) run python tools/gen_proto.py
	$(UV) run python tools/gen_schemas.py
	$(UV) run python tools/gen_sdk.py
	$(UV) run python tools/gen_docs.py
	cd console && pnpm run gen
	$(UV) run python tools/codemap.py

.PHONY: examples
examples: ## Refresh example execution-profile bindings to current digests
	$(UV) run python tools/refresh_example_bindings.py

.PHONY: check-generated
check-generated: ## Fail if any generated file is stale
	$(UV) run python tools/gen_reason_codes.py --check
	$(UV) run python tools/gen_outbound.py --check
	$(UV) run python tools/gen_dashboards.py --check
	$(UV) run python tools/gen_proto.py --check
	$(UV) run python tools/gen_schemas.py --check
	$(UV) run python tools/refresh_example_bindings.py --check
	$(UV) run python tools/gen_sdk.py --check
	$(UV) run python tools/gen_docs.py --check
	$(UV) run python tools/codemap.py --check

.PHONY: fmt
fmt: ## Format code
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

.PHONY: lint
lint: ## Lint, check formatting, and keep files under the size budget
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run python tools/check_sizes.py

.PHONY: typecheck
typecheck: ## Strict type checking
	$(UV) run mypy packages tools tests console/e2e

.PHONY: test
test: ## Run the test suite
	$(UV) run pytest

.PHONY: check
check: lint typecheck check-generated test ## Everything CI runs, except the dev-stack smoke test

VERIFY ?=

.PHONY: verify
verify: ## Prove a change works: the checks for what changed since origin/main (VERIFY="--e2e --models --all --plan")
	$(UV) run python tools/verify.py $(VERIFY)

.PHONY: shots
shots: ## Build the NucleiLabs site and docs, open every page in Chromium, fail on errors (.verify/shots)
	cd landing && pnpm run build
	cd site && pnpm run build
	node tools/shoot.mjs

.PHONY: dev-report
dev-report: ## What is running here and what went wrong: dev stack, local sites, logs, errors first
	$(UV) run python tools/dev_report.py

.PHONY: test-models
test-models: ## Model tests: LeRobot adapters, real workers, benchmarks, the SO-101 integration, evaluation and fine-tuning, the improvement loop end to end (needs models-setup; the loop's tests also NEURA_TEST_DATABASE_URL)
	$(UV) run --group models pytest packages/neura-adapters packages/neura-bench packages/neura-robots packages/neura-learn tests/integration/test_act_worker.py tests/integration/test_shadow_worker.py packages/neura-control/tests/test_improvement_e2e.py packages/neura-control/tests/test_shadow_e2e.py
	$(UV) run --group models neura-bench run --preset smoke --out $${TMPDIR:-/tmp}/neura-bench-smoke

.PHONY: qualify-emulated
qualify-emulated: ## The hardware fault suite and parity on the emulated SO-101 arm (needs models-setup)
	$(UV) run --group models neura-qualify run --emulate --out $${TMPDIR:-/tmp}/neura-qualify-emulated.json

SOAK_HOURS ?= 72

.PHONY: soak
soak: ## Soak the simulated site for SOAK_HOURS (default 72) simulated hours with injected faults
	$(UV) run neura-sim-soak --hours $(SOAK_HOURS) --report $${TMPDIR:-/tmp}/neura-soak.json

CHAOS_SECONDS ?= 300

.PHONY: chaos
chaos: ## Inject faults into a real gateway, its workers and the network for CHAOS_SECONDS
	$(UV) run neura-chaos --duration $(CHAOS_SECONDS) --report $${TMPDIR:-/tmp}/neura-chaos.json

BENCH_DEVICE ?= cuda:0

.PHONY: bench
bench: ## GPU-sharing benchmark on the GPU runner; report in bench/reports/<gpu>/
	$(UV) run --group models neura-bench run --preset gpu --device $(BENCH_DEVICE)

.PHONY: bench-cpu
bench-cpu: ## The benchmark on this machine's CPU (a development host; labeled as such)
	$(UV) run --group models neura-bench run --preset cpu

.PHONY: dev
dev: ## Start the local stack (Postgres, control plane, site agent with its data plane, simulated robots)
	$(COMPOSE) up --build --detach --wait
	@echo "console:       http://127.0.0.1:3000 (API token: see deploy/compose/compose.yaml)"
	@echo "control plane: http://127.0.0.1:8080/v1/meta"
	@echo "site agent:    http://127.0.0.1:8090/v1/local/status (unenrolled until you run tools/smoke_api.py)"
	@echo "data plane:    http://127.0.0.1:8091/v1/local/dataplane"

.PHONY: console-setup
console-setup: ## Install the console's dependencies (pnpm)
	cd console && pnpm install --frozen-lockfile

.PHONY: console-check
console-check: ## Console: format, lint, strict types, unit tests, generated API types
	cd console && pnpm run check && pnpm run check:gen

.PHONY: console-dev
console-dev: ## Console dev server on http://127.0.0.1:5173, against the API on :8080
	cd console && pnpm run dev

.PHONY: landing-setup
landing-setup: ## Install the NucleiLabs site's dependencies (pnpm)
	cd landing && pnpm install --frozen-lockfile

.PHONY: landing-check
landing-check: ## NucleiLabs site: format, lint, strict types, build
	cd landing && pnpm run check

.PHONY: landing-dev
landing-dev: ## NucleiLabs site dev server on http://127.0.0.1:5174
	cd landing && pnpm run dev

.PHONY: console-e2e
console-e2e: ## Console end-to-end tests: real backend, site agent and robots in Chromium
	cd console && pnpm run e2e

.PHONY: docs-setup
docs-setup: ## Install the docs site's dependencies (pnpm)
	cd site && pnpm install --frozen-lockfile

.PHONY: docs
docs: ## Docs site dev server on http://127.0.0.1:5175/docs/ (beside the dev stack's console on :3000)
	cd site && pnpm run start

.PHONY: docs-check
docs-check: ## Docs: labels, wording, generated references, types and a warning-free build
	$(UV) run python tools/gen_docs.py --check
	$(UV) run python tools/check_docs.py
	cd site && pnpm run typecheck && pnpm run build

.PHONY: dev-down
dev-down: ## Stop the local stack and delete its data
	$(COMPOSE) down --volumes --remove-orphans

.PHONY: dev-logs
dev-logs: ## Follow logs from the local stack
	$(COMPOSE) logs --follow

.PHONY: smoke
smoke: ## Build, start and verify the local stack end to end, then stop it
	tools/smoke_dev_stack.sh
