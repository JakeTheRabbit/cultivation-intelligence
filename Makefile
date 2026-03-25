# =============================================================================
# cultivation-intelligence — GNU Makefile
# =============================================================================
# Usage:  make <target>
# Run `make help` to list all available targets.
# =============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Colour helpers
BOLD  := $(shell tput bold 2>/dev/null || echo '')
RESET := $(shell tput sgr0 2>/dev/null || echo '')
GREEN := $(shell tput setaf 2 2>/dev/null || echo '')
CYAN  := $(shell tput setaf 6 2>/dev/null || echo '')

# Project settings
SRC_DIR        := src
TEST_DIR       := tests
PYTHON         := python
PIP            := pip
UVICORN_APP    := cultivation_intelligence.api.main:app
ALEMBIC        := alembic
DOCKER_COMPOSE := docker compose

# Coverage threshold (mirrors pyproject.toml)
COV_THRESHOLD  := 80

.PHONY: help install dev lint format type-check test test-fast test-integration \
        docs-serve docs-build docker-build docker-up docker-down docker-logs \
        db-migrate db-revision seed-db train export-features clean all

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
help: ## Print all available targets with descriptions
	@echo ""
	@echo "$(BOLD)cultivation-intelligence$(RESET) — available make targets"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"; printf "$(CYAN)%-22s$(RESET) %s\n", "Target", "Description"} \
	      /^[a-zA-Z_-]+:.*?##/ { printf "$(GREEN)%-22s$(RESET) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

# ---------------------------------------------------------------------------
# Development Setup
# ---------------------------------------------------------------------------
install: ## Install the project in editable mode with dev dependencies and set up pre-commit
	$(PIP) install -e ".[dev]"
	pre-commit install
	pre-commit install --hook-type commit-msg
	@echo "$(GREEN)Installation complete. Run 'make dev' to start the dev server.$(RESET)"

dev: ## Run the FastAPI development server with hot-reload
	uvicorn $(UVICORN_APP) \
	    --host $${APP_HOST:-0.0.0.0} \
	    --port $${APP_PORT:-8000} \
	    --reload \
	    --reload-dir $(SRC_DIR) \
	    --log-level $${LOG_LEVEL:-info}

# ---------------------------------------------------------------------------
# Code Quality
# ---------------------------------------------------------------------------
lint: ## Run ruff linter and check formatting without making changes
	ruff check $(SRC_DIR) $(TEST_DIR)
	ruff format --check $(SRC_DIR) $(TEST_DIR)

format: ## Auto-format and fix lint issues with ruff
	ruff format $(SRC_DIR) $(TEST_DIR)
	ruff check --fix $(SRC_DIR) $(TEST_DIR)

type-check: ## Run mypy static type checking on src/
	mypy $(SRC_DIR) --config-file pyproject.toml

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------
test: ## Run full test suite with coverage report
	pytest $(TEST_DIR) \
	    --cov=$(SRC_DIR)/cultivation_intelligence \
	    --cov-report=term-missing \
	    --cov-report=html:htmlcov \
	    --cov-report=xml:coverage.xml \
	    --cov-fail-under=$(COV_THRESHOLD) \
	    -v

test-fast: ## Run tests quickly — stop on first failure, no coverage
	pytest $(TEST_DIR) -x --no-cov -q

test-integration: ## Run integration tests only (requires running db and redis)
	pytest $(TEST_DIR)/integration/ \
	    -v \
	    --tb=long \
	    -m integration

# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------
docs-serve: ## Serve MkDocs documentation locally with live-reload
	mkdocs serve --dev-addr 0.0.0.0:8001

docs-build: ## Build static MkDocs documentation into site/
	mkdocs build --strict --clean

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
docker-build: ## Build all Docker images defined in docker-compose.yml
	$(DOCKER_COMPOSE) build --no-cache

docker-up: ## Start all services in detached mode
	$(DOCKER_COMPOSE) up -d
	@echo "$(GREEN)Services started. API available at http://localhost:8000$(RESET)"
	@echo "$(CYAN)Run 'make docker-logs' to tail application logs.$(RESET)"

docker-down: ## Stop and remove all containers (data volumes preserved)
	$(DOCKER_COMPOSE) down

docker-logs: ## Tail logs from the app container
	$(DOCKER_COMPOSE) logs -f app

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
db-migrate: ## Apply all pending Alembic migrations (upgrade head)
	$(ALEMBIC) upgrade head

db-revision: ## Generate a new Alembic migration from model changes
	@read -p "Migration message: " msg; \
	$(ALEMBIC) revision --autogenerate -m "$$msg"

seed-db: ## Seed the database with development fixture data
	$(PYTHON) scripts/seed_db.py

# ---------------------------------------------------------------------------
# ML / Training
# ---------------------------------------------------------------------------
train: ## Train the baseline LightGBM cultivation model
	$(PYTHON) -m cultivation_intelligence.training.train_baseline

export-features: ## Export feature matrix from TimescaleDB to parquet for offline analysis
	$(PYTHON) scripts/export_features.py

# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------
clean: ## Remove all generated artefacts (caches, build dirs, coverage)
	find . -type d -name "__pycache__"     -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache"   -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache"     -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache"     -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov"         -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "dist"            -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "build"           -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info"      -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "coverage.xml"    -delete 2>/dev/null || true
	find . -type f -name ".coverage"       -delete 2>/dev/null || true
	find . -type f -name "*.pyc"           -delete 2>/dev/null || true
	@echo "$(GREEN)Clean complete.$(RESET)"

# ---------------------------------------------------------------------------
# All (CI gate)
# ---------------------------------------------------------------------------
all: lint type-check test ## Run lint, type-check, and full test suite (CI gate)
