# Contributing to Cultivation Intelligence

Thank you for contributing to the cultivation-intelligence platform. This guide covers everything you need to get a working development environment, understand our coding standards, and submit high-quality pull requests.

---

## Table of Contents

1. [Project Overview](#project-overview)
2. [Development Environment Setup](#development-environment-setup)
3. [Pre-commit Hooks](#pre-commit-hooks)
4. [Branch Naming Conventions](#branch-naming-conventions)
5. [Commit Message Format](#commit-message-format)
6. [Pull Request Process](#pull-request-process)
7. [Code Style Guide](#code-style-guide)
8. [Testing Guide](#testing-guide)
9. [Documentation Guide](#documentation-guide)
10. [Data Contracts](#data-contracts)
11. [Domain Knowledge Primers](#domain-knowledge-primers)
12. [Security Notes](#security-notes)
13. [Getting Help](#getting-help)

---

## Project Overview

Cultivation Intelligence is a FastAPI microservice that ingests real-time sensor telemetry from a Home Assistant / ESPHome / Zigbee network inside Legacy Ag Limited's indoor medicinal cannabis facility. It stores data in TimescaleDB, computes agronomic features (VPD, DLI, EC, pH, substrate moisture), and serves LightGBM-backed recommendations via a REST API.

The system operates in **advisory mode** by default — it never actuates physical devices without explicit operator approval. Safety is a first-class concern.

---

## Development Environment Setup

### Prerequisites

- **Python 3.11+** (3.12 recommended) — use `pyenv` or the official installer
- **Docker Desktop** (for TimescaleDB and Redis)
- **Make** (GNU Make; on macOS: `brew install make`; on Windows: via Git Bash or WSL2)
- **Git** 2.40+

### Installation Steps

```bash
# 1. Clone the repository
git clone https://github.com/legacy-ag-limited/cultivation-intelligence.git
cd cultivation-intelligence

# 2. Create and activate a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate          # macOS/Linux
# .\.venv\Scripts\activate          # Windows (PowerShell)

# 3. Install all dependencies (including dev extras) and pre-commit hooks
make install

# 4. Copy and configure environment variables
cp .env.example .env
# Edit .env — set HA_TOKEN, DATABASE_URL (or leave defaults for Docker)

# 5. Start backing services
make docker-up

# 6. Run database migrations
make db-migrate

# 7. (Optional) Seed with fixture data
make seed-db

# 8. Start the development server
make dev
```

The API will be available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

### Verifying the Setup

```bash
make test-fast      # Quick smoke test — should pass in < 30 s
curl http://localhost:8000/health   # Should return {"status": "ok"}
```

---

## Pre-commit Hooks

We use [pre-commit](https://pre-commit.com/) to enforce standards before every commit. `make install` sets up the hooks automatically.

Hooks that run on every commit:

| Hook | Purpose |
|------|---------|
| `ruff` | Linting and auto-fix |
| `ruff-format` | Code formatting |
| `mypy` | Static type checking |
| `check-yaml` | YAML syntax validation |
| `check-toml` | TOML syntax validation |
| `detect-private-key` | Prevents accidental secret commits |
| `trailing-whitespace` | Normalises whitespace |
| `end-of-file-fixer` | Ensures files end with a newline |

To run all hooks manually against all files:

```bash
pre-commit run --all-files
```

To temporarily skip hooks (e.g., a WIP commit):

```bash
git commit --no-verify -m "chore: WIP — do not merge"
```

> **Note:** `--no-verify` is acceptable for WIP commits on feature branches, but **never** on commits destined for `main`.

---

## Branch Naming Conventions

| Prefix | Usage | Example |
|--------|-------|---------|
| `feature/` | New functionality | `feature/vpd-alert-service` |
| `fix/` | Bug fixes | `fix/ec-sensor-null-handling` |
| `docs/` | Documentation only | `docs/adr-004-advisory-mode` |
| `chore/` | Tooling, CI, deps, refactor | `chore/upgrade-lightgbm-4.4` |
| `hotfix/` | Critical production fix | `hotfix/ph-clamp-overflow` |
| `experiment/` | Research or ML experiments (may not be merged) | `experiment/lstm-vpd-forecast` |

Branch names must be lowercase, use hyphens (not underscores), and be descriptive enough that the purpose is clear without reading commits.

---

## Commit Message Format

We follow [Conventional Commits](https://www.conventionalcommits.org/) with cultivation-specific scope labels.

### Structure

```
<type>(<scope>): <short summary in imperative mood>

[optional body — explain WHY, not what]

[optional footer — BREAKING CHANGE, closes #issue]
```

### Types

| Type | When to use |
|------|-------------|
| `feat` | New feature or API endpoint |
| `fix` | Bug fix |
| `perf` | Performance improvement |
| `refactor` | Code restructure without behaviour change |
| `test` | Adding or updating tests |
| `docs` | Documentation only |
| `chore` | Build system, deps, tooling |
| `ci` | CI/CD pipeline changes |

### Scopes (cultivation-specific)

`ingest`, `features`, `model`, `api`, `db`, `controls`, `safety`, `sensors`, `ha`, `redis`, `infra`, `docs`

### Examples

```
feat(ingest): add WebSocket reconnection with exponential backoff

The HA WebSocket client now retries with configurable backoff
instead of crashing on connection drop. Fixes intermittent data
gaps observed during HA restarts.

Closes #42
```

```
fix(features): clamp VPD to [0, 10] before persisting

Saturated humidity sensors occasionally return >100% RH, causing
negative VPD values that corrupt the feature matrix. Added a
clamping step with a structured log warning for debugging.
```

```
feat(controls): implement advisory recommendation endpoint

BREAKING CHANGE: /api/v1/recommend now returns a RecommendationResponse
schema instead of a plain dict. Clients must update to the new schema.
```

```
chore(deps): upgrade lightgbm 4.3→4.4, pandas 2.2→2.2.2
```

---

## Pull Request Process

### Before Opening a PR

1. Ensure your branch is up to date with `main`:
   ```bash
   git fetch origin
   git rebase origin/main
   ```
2. Run the full CI gate locally:
   ```bash
   make all     # lint + type-check + test
   ```
3. Update documentation if your change affects public APIs, data schemas, or agronomic logic.
4. For ML changes, attach a brief evaluation summary (metrics before/after) in the PR description.

### PR Template

When you open a PR, the template will prompt you for:

- **What** was changed (summary)
- **Why** (motivation, linked issue)
- **How** it was tested (unit tests, integration tests, manual steps)
- **Safety impact** — does this change affect control actions or sensor thresholds?
- **Documentation** — what docs were updated?

### Review Requirements

- At minimum **one approving review** from a team member before merge.
- All CI checks must pass: lint, type-check, test coverage ≥ 80%.
- PRs that touch `controls/`, `safety/`, or agronomic threshold constants require **two approvals**.
- No self-merges on `main`.

### Merging

We use **squash merge** for feature branches to keep `main` history clean. Write a clean squash commit message following the Conventional Commits format — do not auto-accept the concatenated commit log.

---

## Code Style Guide

### General Principles

- **Async-first**: all I/O (database queries, HTTP calls, Redis ops) must use `async/await`. Never use blocking I/O in an async context.
- **Explicit over implicit**: prefer explicit type annotations, named arguments, and early returns over nested conditions.
- **Fail loudly**: raise typed exceptions rather than returning `None` or empty results on unexpected conditions.
- **Structured logging**: use `structlog` throughout — never `print()` or the stdlib `logging` module directly.

### Formatting and Linting

- Line length: **120 characters** (enforced by Ruff).
- All code must pass `ruff check` and `ruff format --check` with zero warnings.
- Import ordering: standard library → third-party → local, enforced by `isort` via Ruff.

### Structlog Usage

```python
import structlog

log = structlog.get_logger(__name__)

# Bind context at request boundary
log = log.bind(zone_id=zone_id, sensor_entity=entity_id)

# Use structured key-value pairs — never f-strings in log messages
log.info("sensor_reading_ingested", value=reading.value, unit=reading.unit)
log.warning("vpd_out_of_range", vpd=vpd_value, target_min=target_min, target_max=target_max)
log.error("database_write_failed", exc_info=True, table="sensor_readings")
```

### Pydantic Models

- All API request/response schemas must be Pydantic `BaseModel` subclasses.
- Use `model_config = ConfigDict(frozen=True)` for value objects that should be immutable.
- Validate physical units at the schema boundary (e.g., `Field(ge=0.0, le=100.0)` for percentages).

### Database / SQLAlchemy

- Use SQLAlchemy 2.x async sessions (`AsyncSession`) via dependency injection.
- Never construct raw SQL strings — use the ORM or `text()` with bound parameters.
- Wrap mutations in explicit transactions; rely on context managers, not manual `commit()`.

### Settings / Configuration

- All configuration flows through `pydantic-settings` `Settings` class.
- Never hardcode hostnames, tokens, or thresholds — always read from settings.
- Access settings via dependency injection in FastAPI routes, not module-level globals.

---

## Testing Guide

### Test Structure

```
tests/
├── unit/            # Pure Python tests — no I/O, no DB, no network
│   ├── test_features.py
│   ├── test_vpd.py
│   └── test_model_inference.py
├── integration/     # Tests that require running DB and Redis
│   ├── test_ingest_pipeline.py
│   └── test_api_endpoints.py
└── conftest.py      # Shared fixtures (async DB sessions, test client, etc.)
```

### Running Tests

```bash
make test              # Full suite with coverage
make test-fast         # Fail-fast, no coverage (fast feedback loop)
make test-integration  # Integration tests only (requires docker-up)
```

### Writing Unit Tests

```python
import pytest
from cultivation_intelligence.features.vpd import compute_vpd

@pytest.mark.unit
def test_compute_vpd_nominal():
    """VPD at 25°C, 60% RH should be approximately 1.26 kPa."""
    vpd = compute_vpd(temperature_c=25.0, relative_humidity_pct=60.0)
    assert 1.20 <= vpd <= 1.30

@pytest.mark.unit
def test_compute_vpd_saturated_humidity():
    """100% RH should yield VPD of 0."""
    vpd = compute_vpd(temperature_c=25.0, relative_humidity_pct=100.0)
    assert vpd == pytest.approx(0.0, abs=0.01)
```

### Writing Integration Tests

```python
import pytest
from httpx import AsyncClient

@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_endpoint(async_client: AsyncClient):
    response = await async_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
```

### Fixture Patterns

Define shared fixtures in `tests/conftest.py`. Use `pytest-asyncio` with `asyncio_mode = "auto"`. For database fixtures, use a transaction rollback pattern to keep tests isolated:

```python
@pytest.fixture
async def db_session(async_engine):
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(conn) as session:
            yield session
        await conn.run_sync(Base.metadata.drop_all)
```

### Coverage Requirements

- Minimum **80% line coverage** across `src/cultivation_intelligence/`.
- Coverage below 80% will fail CI.
- Aim for 90%+ on `features/` and `controls/` modules — these are safety-critical.
- Exclude untestable boilerplate (e.g., `__repr__`, `raise NotImplementedError`) with `# pragma: no cover`.

---

## Documentation Guide

### When to Update Docs

- New API endpoint → update `docs/data-model/entities.md` and OpenAPI description strings.
- New feature variable → update `docs/features/catalogue.md`.
- New agronomic threshold → update `docs/controls/safety.md`.
- Architectural change → write or update an ADR (see below).
- Any change that affects how cultivators interpret recommendations → update `docs/theory.md`.

### ADR Process

Architectural Decision Records (ADRs) document significant design choices. When proposing a material architectural change:

1. Copy `docs/adrs/adr-000-template.md` to `docs/adrs/adr-NNN-short-title.md`.
2. Fill in: Status (`Proposed`), Context, Decision, Consequences.
3. Add the ADR to the nav in `mkdocs.yml`.
4. The ADR status moves to `Accepted` when the PR is merged.

### Building Docs Locally

```bash
make docs-serve    # Live-reload at http://localhost:8001
make docs-build    # Validate and build to site/
```

---

## Data Contracts

The ingest pipeline, feature store, and API each have defined data contracts encoded as Pydantic schemas and (for external integrations) JSON Schema files in `schemas/`.

### Updating a Schema

1. Modify the Pydantic model in `src/cultivation_intelligence/schemas/`.
2. Regenerate the corresponding JSON Schema: `python scripts/export_schemas.py`.
3. Update `schemas/*.json` and commit both the model and the generated file.
4. Update `docs/data-model/entities.md` to reflect the change.

### Backward Compatibility

- Adding **optional fields** with defaults is backward-compatible.
- Removing or renaming fields is a **breaking change** — bump the API version and document a migration path.
- Changes to physical unit conventions (e.g., EC from mS/cm to µS/cm) are breaking changes and require an ADR.

---

## Domain Knowledge Primers

### VPD (Vapour Pressure Deficit)

VPD is the difference between the moisture content the air *could* hold (saturation vapour pressure) and what it *actually* holds. Measured in kilopascals (kPa).

- **Low VPD (< 0.4 kPa)**: air is near saturation; stomata close, transpiration slows, risk of Botrytis (bud rot).
- **Target VPD (0.8–1.2 kPa)**: optimal transpiration, nutrient uptake, and canopy cooling.
- **High VPD (> 1.5 kPa)**: plants stress-close stomata, growth stalls, terpene loss increases.

Formula: `VPD = SVP × (1 - RH/100)` where `SVP = 0.6108 × exp(17.27 × T / (T + 237.3))`.

### DLI (Daily Light Integral)

DLI is the total amount of photosynthetically active radiation (PAR) received in a day. Measured in mol/m²/day.

- Calculated by integrating PPFD (µmol/m²/s) over the photoperiod.
- Target for flowering cannabis: **38–45 mol/m²/day**.
- Below 30: underpowered, yield loss. Above 50 (without CO₂ enrichment): light saturation, heat stress.

### EC (Electrical Conductivity)

EC measures ion concentration in the nutrient solution — a proxy for total dissolved solids. Measured in mS/cm (millisiemens per centimetre).

- **Seedling/clone**: 0.5–0.8 mS/cm
- **Vegetative**: 1.2–1.8 mS/cm
- **Flowering**: 1.8–2.4 mS/cm
- **Late flush**: 0.2–0.4 mS/cm

### pH

pH of the nutrient solution determines nutrient availability. Cannabis roots uptake nutrients most efficiently at **5.8–6.2** (hydroponic/coco) or **6.0–6.5** (soil).

Outside this range, specific nutrients lock out regardless of EC. The system monitors pH drift as a leading indicator of dosing system faults.

### VWC (Volumetric Water Content)

VWC is the percentage of substrate volume occupied by water. Measured by capacitive soil sensors.

- **Field capacity (post-irrigation)**: 65–75% for coco coir.
- **Trigger point (pre-irrigation)**: 55–60% — irrigation event fires when VWC drops to trigger.
- **Wilt point**: < 30% — emergency threshold, never intentionally reached.

---

## Security Notes

### Home Assistant Token Handling

The `HA_TOKEN` in `.env` is a Long-Lived Access Token with full HA API access. Treat it as a password:

- Never commit `.env` to version control (`git status` should show it as untracked).
- Never log the token value — structlog is configured to redact fields named `token`, `password`, `secret`.
- Rotate the token immediately if it is exposed. Generate a new one in HA under your user profile.
- In production, store the token in a secrets manager (e.g., HashiCorp Vault, AWS Secrets Manager) and inject at runtime — not as a file on disk.

### No Secrets in Code

- No hardcoded credentials, IPs, or API keys anywhere in `src/` or `tests/`.
- The `detect-private-key` pre-commit hook will reject commits containing common private key headers.
- If a secret is accidentally committed, treat the repository as compromised: rotate the credential immediately, then use `git filter-repo` to purge the history.

### ADVISORY_MODE

The `ADVISORY_MODE=true` environment variable prevents the system from sending any control commands to Home Assistant. This flag must remain `true` in production until the full validation protocol in `docs/controls/advisory-mode.md` has been completed and signed off by the Head Grower and an engineer.

---

## Getting Help

| Channel | Use for |
|---------|---------|
| GitHub Issues | Bug reports, feature requests |
| GitHub Discussions | Architecture questions, agronomic queries |
| `#cultivation-intelligence` Slack | Day-to-day engineering chat |
| `engineering@legacyag.co.nz` | Confidential or compliance-related queries |

When opening a bug report, please include:
1. The full error message and stack trace.
2. The output of `python --version` and `pip show cultivation-intelligence`.
3. Relevant sensor entity IDs or zone configuration.
4. Whether you are running in Docker or directly.

---

*This document is maintained by the Legacy Ag engineering team. If something is unclear or outdated, please open a PR to fix it — documentation improvements are always welcome.*
