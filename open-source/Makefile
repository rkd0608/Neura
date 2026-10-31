# Common tasks for neura-robot. `make help` lists them.
.DEFAULT_GOAL := help
PYTHON ?= python

.PHONY: help
help: ## List the tasks
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

.PHONY: setup
setup: ## Install neura-robot for development, in the active environment
	$(PYTHON) -m pip install -e ".[dev,openpi]"

.PHONY: setup-all
setup-all: ## Everything: also LeRobot (SO-101, its server) and MuJoCo (Python 3.12+)
	$(PYTHON) -m pip install -e ".[dev,openpi,sim,so101,lerobot]"

.PHONY: assets
assets: ## Fetch the simulated arms' models (MuJoCo Menagerie, with git, once)
	$(PYTHON) -c "from neura_robots.sim.menagerie import fetch; print(fetch())"

.PHONY: doctor
doctor: ## Check this machine for everything the repository can use
	$(PYTHON) scripts/doctor.py

.PHONY: lint
lint: ## Ruff: lint and formatting
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

.PHONY: format
format: ## Fix what ruff can
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .

.PHONY: typecheck
typecheck: ## mypy, strict
	$(PYTHON) -m mypy packages examples scripts

.PHONY: test
test: ## The tests that run here; skips say what they need
	$(PYTHON) -m pytest

.PHONY: quickstart
quickstart: ## The quickstart: a simulated arm, a policy, a fault, its incident record
	$(PYTHON) examples/quickstart.py

.PHONY: check
check: lint typecheck test quickstart ## Everything CI runs on every Python

.PHONY: test-docker
test-docker: ## openpi's own server and ROS 2 Jazzy under packet loss, in Docker
	docker build -t neura-openpi-test:dev packages/neura-runtime/tests/openpi
	docker build -t neura-ros2-test:dev packages/neura-robots/tests/ros2
	NEURA_TEST_OPENPI_IMAGE=neura-openpi-test:dev NEURA_TEST_ROS2_IMAGE=neura-ros2-test:dev \
		$(PYTHON) -m pytest packages/neura-runtime/tests/test_local.py packages/neura-robots/tests/test_ros2.py
