# Cultivation Intelligence

> AI-driven cultivation intelligence platform for Legacy Ag's indoor medicinal cannabis facility in New Zealand.

[![Build Status](https://img.shields.io/github/actions/workflow/status/legacyag/cultivation-intelligence/ci.yml?branch=main&label=build)](https://github.com/legacyag/cultivation-intelligence/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Docs](https://img.shields.io/badge/docs-mkdocs-blue)](https://cultivation-intelligence.legacyag.co.nz/docs)

---

## Overview

Cultivation Intelligence is the data and AI backbone for Legacy Ag Limited's indoor medicinal cannabis operation in New Zealand. The platform ingests real-time environmental and nutrient data from Home Assistant (backed by ESPHome sensors and Zigbee devices), stores it in a TimescaleDB time-series database, and runs a continuous feature engineering and machine learning pipeline to generate actionable growing recommendations for facility operators.

The system operates in a strictly **advisory** mode: every recommendation is presented to a human grower for review and approval before any control action is executed. This design meets Legacy Ag's compliance obligations under New Zealand's Misuse of Drugs (Medicinal Cannabis) Regulations 2019, which require human oversight of all cultivation decisions. Control actions (adjusting dosing pumps, setpoints, irrigation schedules) are written back to Home Assistant only after an operator explicitly accepts a recommendation through the dashboard.

The ML layer uses LightGBM for tabular yield and quality classification models trained on historical batch data, and a Temporal Fusion Transformer (TFT) for multi-step environmental forecasting. Models are versioned and stored in a local MLflow registry. The platform is designed to run entirely on-premises within the Legacy Ag facility — no cultivation data leaves the local network.

---

## Architecture

```mermaid
flowchart TB
    subgraph Facility["🌿 Facility Infrastructure"]
        HA[Home Assistant\nCore]
        ESP[ESPHome\nSensors]
        Zigbee[Zigbee\nDevices]
        Aqua[AquaPro\nDosing\nAQU1AD04A42]
    end

    subgraph Intelligence["🧠 Cultivation Intelligence"]
        Ingest[Ingestion\nService]
        Features[Feature\nEngineering]
        Models[ML Models\nLightGBM/TFT]
        Reco[Recommendation\nEngine]
        API[FastAPI\nService]
    end

    subgraph Storage["🗄️ Data Layer"]
        TSDB[(TimescaleDB\nSensor Data)]
        Redis[(Redis\nCache + Pubsub)]
        Registry[Model\nRegistry\nMLflow]
    end

    subgraph Operator["👩‍🌾 Operators"]
        Dashboard[Dashboard\nUI]
        Alerts[Alert\nSystem]
    end

    ESP --> HA
    Zigbee --> HA
    Aqua --> HA
    HA -->|"REST/WebSocket"| Ingest
    Ingest --> TSDB
    TSDB --> Features
    Features --> Models
    Models --> Reco
    Reco --> API
    API --> Dashboard
    API --> Alerts
    Models --- Registry
    Features --> Redis
    API -->|"advisory only\nhuman approval required"| HA
```

---

## What's Implemented vs Stubbed

| Feature | Status | Notes |
|---------|--------|-------|
| FastAPI application skeleton | Scaffolded | Routes defined, handlers stubbed |
| TimescaleDB schema (init.sql) | Implemented | Hypertable, continuous aggregates, compression policy |
| Home Assistant ingestion webhook | Scaffolded | Auth middleware present; batch insert logic pending |
| ESPHome sensor parsing | Planned | Will parse via HA state change events |
| AquaPro serial integration (AQU1AD04A42) | Planned | Serial protocol reverse-engineered; HA custom component pending |
| Sensor data quality flags | Scaffolded | Spike/flatline detection logic stubbed |
| Feature engineering pipeline | Scaffolded | VPD, DLI, EC/pH, VWC features implemented in export script |
| LightGBM yield prediction | Planned | Model architecture designed; requires completed batch training data |
| Temporal Fusion Transformer (environment forecast) | Planned | Research phase; TFT selected based on benchmark results |
| Recommendation engine | Scaffolded | Rule-based advisor working; ML-backed advisor planned |
| Operator dashboard | Planned | React frontend scaffolded; API integration pending |
| Alert system (email/SMS) | Planned | Alertmanager integration designed |
| MLflow model registry | Scaffolded | Local MLflow server in docker-compose; no models registered yet |
| Redis pub/sub for live sensor feed | Planned | Schema designed; pub/sub publisher not yet implemented |
| Continuous aggregate (hourly stats) | Implemented | TimescaleDB policy added in init.sql |
| Sensor data compression (7-day policy) | Implemented | TimescaleDB compression policy active |
| Database seeder | Implemented | scripts/seed_db.py generates realistic 30-day histories |
| Feature export to Parquet/CSV | Implemented | scripts/export_features.py with full feature pipeline |
| JSON Schema data contracts | Implemented | sensor_reading.json and batch_metadata.json (Draft 7) |
| Dockerfile (production) | Implemented | Multi-stage not yet; single-stage python:3.11-slim |
| Docker Compose (dev stack) | Scaffolded | TimescaleDB + Redis + API; no Grafana yet |
| CI/CD (GitHub Actions) | Planned | Workflow file stubs present |
| Test suite | Scaffolded | pytest structure in place; fixtures pending |

---

## Quickstart

### 1. Prerequisites

- **Docker** >= 24.0 and **Docker Compose** >= 2.20
- **Python** 3.11 or 3.12 (for running scripts locally)
- **Git**
- (Optional) **Make** for convenience targets

Ensure Docker has at least 4 GB RAM allocated (TimescaleDB and the API service together need ~2 GB).

### 2. Clone and setup

```bash
git clone https://github.com/legacyag/cultivation-intelligence.git
cd cultivation-intelligence

# Create a Python virtual environment for local tooling
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Install all dependencies including dev extras
pip install -e ".[dev]"
```

### 3. Configure environment

Copy the example environment file and edit it:

```bash
cp .env.example .env
```

Minimum required values for local development (defaults already set in .env.example):

```dotenv
DATABASE_URL=postgresql://cultivation:cultivation@localhost:5432/cultivation
REDIS_URL=redis://localhost:6379/0
HA_WEBHOOK_SECRET=change-me-in-production
SECRET_KEY=change-me-in-production
ENVIRONMENT=development
LOG_LEVEL=DEBUG
```

### 4. Start the Docker Compose stack

```bash
docker compose up -d
```

This starts:
- **TimescaleDB** (PostgreSQL 16 + TimescaleDB 2.x) on port 5432
- **Redis** 7 on port 6379
- **Cultivation Intelligence API** on port 8000
- **MLflow** tracking server on port 5001

Wait ~15 seconds for TimescaleDB to fully initialise (it runs `init.sql` on first start).

Check all services are healthy:

```bash
docker compose ps
```

### 5. Seed the database

```bash
python scripts/seed_db.py --days 30
```

This creates one sample batch (Wedding Cake, day 45 of flower), 30 days of sensor readings at 5-minute intervals across 8 sensor types, 180 irrigation events, and 3 advisory recommendations.

To reset and re-seed:

```bash
python scripts/seed_db.py --days 30 --reset
```

### 6. Verify the health endpoint

```bash
curl http://localhost:8000/health
# {"status": "ok", "version": "0.1.0", "db": "connected", "ts": "2025-09-14T08:30:00Z"}
```

### 7. Access API documentation

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **OpenAPI JSON**: http://localhost:8000/openapi.json

---

## Project Structure

```
cultivation-intelligence/
├── src/
│   └── app/
│       ├── main.py                  # FastAPI application factory
│       ├── config.py                # Pydantic settings (reads from .env)
│       ├── dependencies.py          # FastAPI dependency injection (db, auth)
│       ├── models/                  # SQLAlchemy ORM models
│       │   ├── batch.py
│       │   ├── sensor_reading.py
│       │   ├── recommendation.py
│       │   └── irrigation_event.py
│       ├── routers/                 # FastAPI route handlers
│       │   ├── ingest.py            # POST /ingest/readings, /ingest/batch
│       │   ├── batches.py           # CRUD for grow batches
│       │   ├── sensors.py           # Query sensor readings and stats
│       │   ├── recommendations.py   # List, accept, reject recommendations
│       │   └── health.py            # Health + readiness checks
│       ├── services/
│       │   ├── feature_engineering/ # Feature computation (VPD, DLI, etc.)
│       │   │   ├── environment.py
│       │   │   ├── nutrition.py
│       │   │   └── irrigation.py
│       │   ├── ingestion.py         # Validates and persists sensor readings
│       │   ├── quality_checker.py   # Spike/flatline/range quality flagging
│       │   └── recommendation_engine.py  # Rule + ML-backed advisor
│       └── ml/
│           ├── registry.py          # MLflow model loading and versioning
│           ├── yield_predictor.py   # LightGBM inference wrapper
│           └── env_forecaster.py    # TFT environment forecast (stub)
│
├── scripts/
│   ├── seed_db.py                   # Development database seeder
│   └── export_features.py           # Feature export to Parquet/CSV
│
├── data_contracts/
│   ├── sensor_reading.json          # JSON Schema Draft 7: SensorReading
│   └── batch_metadata.json          # JSON Schema Draft 7: BatchMetadata
│
├── tests/
│   ├── conftest.py                  # pytest fixtures (async DB, test client)
│   ├── test_ingest.py
│   ├── test_feature_engineering.py
│   └── test_recommendations.py
│
├── docs/                            # MkDocs documentation source
│   ├── index.md
│   ├── architecture.md
│   ├── api.md
│   └── data-contracts.md
│
├── init.sql                         # TimescaleDB schema initialisation
├── Dockerfile                       # Production container image
├── docker-compose.yml               # Local development stack
├── docker-compose.override.yml      # Dev overrides (hot reload, mounts)
├── pyproject.toml                   # Project metadata, deps, tool config
├── .env.example                     # Environment variable template
├── .ruff.toml                       # Ruff linter configuration
├── mkdocs.yml                       # Documentation site configuration
├── CONTRIBUTING.md
└── README.md
```

---

## Key Concepts

### VPD (Vapour Pressure Deficit)

VPD is the difference between the amount of moisture the air can hold at saturation (SVP) and the actual moisture in the air (AVP), expressed in kPa. It is the single most important environmental parameter for cannabis: too low (< 0.4 kPa) promotes fungal disease; too high (> 1.6 kPa) causes transpiration stress and stomatal closure. The platform tracks VPD continuously and targets 0.8–1.2 kPa during late flower.

Formula: `VPD = SVP × (1 - RH/100)`, where `SVP = 0.6108 × exp(17.27T / (T + 237.3))` (Tetens approximation, T in °C).

### DLI (Daily Light Integral)

DLI is the cumulative amount of photosynthetically active radiation (PAR) received by the canopy in a day, expressed in mol/m²/day. It integrates PPFD (μmol/m²/s) over time. Cannabis in late flower typically targets 40–50 mol/m²/day. The feature engineering pipeline computes DLI per day per batch from PPFD sensor readings.

Formula: `DLI = Σ(PPFD × Δt) / 1,000,000` where Δt is the interval in seconds.

### EC and pH Management

Electrical conductivity (EC, mS/cm) measures the total dissolved solids in the nutrient solution — a proxy for nutrient concentration. pH determines nutrient availability; at wrong pH values, individual nutrients become insoluble ("nutrient lockout"). For coco coir and hydro substrates, the optimal range is EC 1.8–2.8 and pH 5.8–6.2 during flower.

The platform monitors both continuously and tracks drift trends, generating recommendations when EC or pH leave target ranges for more than 4 hours.

### AquaPro Dosing Integration (AQU1AD04A42)

The AquaPro AQU1AD04A42 is a 4-channel peristaltic dosing controller connected via RS-232 serial (9600 baud, 8N1). It controls pH-up, pH-down, nutrient A, and nutrient B dosing. The integration path is: AquaPro serial → USB-serial adapter → Raspberry Pi running Home Assistant → REST API into this platform. The HA custom component (planned) will expose each channel as a `number` entity and dosing events as `event` entities.

### Advisory Mode

All ML-generated recommendations are advisory only. The recommendation engine outputs structured suggestions (see `data_contracts/`) with a confidence score and suggested action payload. Operators view these on the dashboard and explicitly accept or reject each one. Only on acceptance does the platform issue a service call back to Home Assistant to adjust a setpoint, trigger an irrigation event, or notify the team. This ensures all cultivation decisions remain under human control.

---

## Configuration

Environment variables are read via Pydantic Settings from `.env`. All variables can also be set as OS environment variables (OS env takes precedence over `.env`).

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL/TimescaleDB connection string | `postgresql://cultivation:cultivation@localhost:5432/cultivation` |
| `REDIS_URL` | Redis connection string | `redis://localhost:6379/0` |
| `HA_BASE_URL` | Home Assistant base URL | `http://homeassistant.local:8123` |
| `HA_TOKEN` | Home Assistant long-lived access token | — (required in production) |
| `HA_WEBHOOK_SECRET` | Shared secret for HA webhook push ingestion | — (required) |
| `SECRET_KEY` | Application secret key for token signing | — (required) |
| `ENVIRONMENT` | Runtime environment (`development`, `production`) | `development` |
| `LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `MODEL_REGISTRY_PATH` | Path to MLflow tracking server or local artifact store | `./mlruns` |
| `FEATURE_CACHE_TTL_S` | Redis TTL for cached feature vectors (seconds) | `300` |
| `RECOMMENDATION_MIN_CONFIDENCE` | Minimum model confidence to surface a recommendation | `0.70` |
| `MAX_INGESTION_BATCH_SIZE` | Maximum readings per ingestion batch request | `1000` |
| `AQUAPRO_SERIAL_PORT` | Serial port for AquaPro dosing controller | `/dev/ttyUSB0` |
| `AQUAPRO_BAUD_RATE` | AquaPro serial baud rate | `9600` |
| `TZ` | Container timezone | `Pacific/Auckland` |

---

## API Reference

All endpoints are prefixed with `/api/v1` except health and docs.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check — returns DB connectivity status and version |
| `GET` | `/ready` | Readiness probe — returns 503 if DB or Redis unavailable |
| `POST` | `/api/v1/ingest/reading` | Ingest a single sensor reading (HA push webhook) |
| `POST` | `/api/v1/ingest/batch` | Ingest up to 1,000 sensor readings in one call |
| `GET` | `/api/v1/batches` | List all batches (filterable by stage) |
| `POST` | `/api/v1/batches` | Create a new grow batch |
| `GET` | `/api/v1/batches/{batch_id}` | Get batch details and current summary statistics |
| `PATCH` | `/api/v1/batches/{batch_id}` | Update batch metadata (stage, notes, yield) |
| `GET` | `/api/v1/batches/{batch_id}/sensors` | Query sensor readings for a batch with time filtering |
| `GET` | `/api/v1/batches/{batch_id}/stats` | Hourly aggregated stats from continuous aggregate view |
| `GET` | `/api/v1/batches/{batch_id}/features` | Latest engineered feature vector for ML inference |
| `GET` | `/api/v1/recommendations` | List recommendations (filterable by status, batch) |
| `GET` | `/api/v1/recommendations/{rec_id}` | Get recommendation detail |
| `POST` | `/api/v1/recommendations/{rec_id}/accept` | Accept recommendation (triggers HA action if configured) |
| `POST` | `/api/v1/recommendations/{rec_id}/reject` | Reject recommendation with optional reason |
| `GET` | `/api/v1/irrigation` | List irrigation events for a batch |
| `GET` | `/docs` | Swagger UI — interactive API documentation |
| `GET` | `/redoc` | ReDoc — alternative API documentation |

---

## Development

### Installation

```bash
# Install with all development extras
pip install -e ".[dev]"

# Or using make
make install
```

### Running the dev server (hot reload)

```bash
make dev
# Equivalent to:
# uvicorn src.app.main:app --reload --host 0.0.0.0 --port 8000 --log-level debug
```

### Running tests

```bash
make test
# Equivalent to:
# pytest tests/ -v --asyncio-mode=auto --cov=src --cov-report=term-missing
```

### Linting and formatting

```bash
make lint
# Runs: ruff check src/ scripts/ tests/
# Runs: ruff format --check src/ scripts/ tests/
# Runs: mypy src/

make fmt
# Runs: ruff format src/ scripts/ tests/
# Runs: ruff check --fix src/ scripts/ tests/
```

### Generating and serving documentation

```bash
make docs-serve
# Equivalent to: mkdocs serve --dev-addr 0.0.0.0:8080
```

### Full Makefile targets

| Target | Description |
|--------|-------------|
| `make install` | Install package with dev extras |
| `make dev` | Start API server with hot reload |
| `make test` | Run test suite with coverage |
| `make lint` | Run ruff and mypy checks |
| `make fmt` | Auto-format and fix lint issues |
| `make seed` | Run database seeder (30 days) |
| `make export` | Export all completed batch features to Parquet |
| `make docs-serve` | Serve MkDocs documentation locally |
| `make docker-build` | Build production Docker image |
| `make docker-up` | Start full Docker Compose stack |
| `make docker-down` | Stop Docker Compose stack |
| `make clean` | Remove build artifacts and caches |

---

## Data Contracts

The `data_contracts/` directory contains JSON Schema (Draft 7) definitions for all data structures exchanged between system components. These schemas serve as the authoritative specification for:

- **Validation** at API ingestion endpoints (using `jsonschema` library)
- **Documentation** of expected data shapes for external integrators (e.g., Home Assistant automation authors)
- **Testing** fixtures and mock data generation

### sensor_reading.json

Defines a single `SensorReading` object and a `SensorReadingBatch` wrapper. Covers all sensor types (`TEMPERATURE`, `HUMIDITY`, `VPD_CALCULATED`, `EC`, `PH`, `VWC`, `CO2`, `PPFD`, `FLOW_RATE`, `DISSOLVED_OXYGEN`, `WEIGHT`), source provenance (`HA_PUSH`, `HA_POLL`, `CSV_IMPORT`, `MANUAL_ENTRY`), and quality flags.

### batch_metadata.json

Defines the `BatchMetadata` object capturing strain genetics, room configuration, grow schedule targets, environmental setup, and outcome labels. This is the primary record used by the feature engineering pipeline and as the basis for training label association.

To validate data against a schema:

```python
import json
import jsonschema

with open("data_contracts/sensor_reading.json") as f:
    schema = json.load(f)

reading = {
    "sensor_id": "temp_flower_room_1",
    "batch_id": "550e8400-e29b-41d4-a716-446655440000",
    "sensor_type": "TEMPERATURE",
    "value": 24.3,
    "unit": "°C",
    "timestamp": "2025-09-14T08:30:00Z",
    "source": "HA_PUSH",
    "quality_flag": "OK"
}

jsonschema.validate(instance=reading, schema=schema)  # raises if invalid
```

---

## Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting pull requests. Key points:

- All code must pass `make lint` before review
- New features require tests with >= 80% branch coverage for the changed module
- Data contract changes require a schema version bump and migration notes
- Advisory mode constraints are non-negotiable: no automated control actions without operator approval

---

## License

MIT License — Copyright (c) 2025 Legacy Ag Limited

See [LICENSE](LICENSE) for the full text.
