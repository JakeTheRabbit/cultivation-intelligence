"""
Pytest fixtures for the cultivation-intelligence test suite.

Provides:
- FastAPI app and HTTP client fixtures (sync and async)
- Mock database session and Redis client
- Canonical sample data dictionaries (batch, sensor reading, features)
- Settings override with in-memory/test values
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app():
    """Return the FastAPI application instance.

    Importing here (rather than at module level) allows settings overrides to
    be applied before the app initialises its lifespan dependencies.
    """
    from src.app.main import create_app
    return create_app()


@pytest.fixture
def client(app):
    """Synchronous TestClient for the FastAPI app.

    Bypasses startup/shutdown lifespan events by default.
    """
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest_asyncio.fixture
async def async_client(app):
    """Async HTTP client for use in async test functions."""
    async with AsyncClient(app=app, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Database mock
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    """Return a MagicMock that mimics an async SQLAlchemy AsyncSession.

    Supports:
    - await db.execute(...)
    - async context manager usage (async with db:)
    - db.add(), db.flush(), db.commit(), db.rollback(), db.close()
    """
    db = MagicMock()

    # Make execute return an awaitable that yields a result proxy
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = 0
    mock_result.scalars.return_value.all.return_value = []
    mock_result.all.return_value = []

    async def _execute(*args, **kwargs):
        return mock_result

    db.execute = _execute
    db.add = MagicMock()

    async def _flush(*args, **kwargs):
        return None

    db.flush = _flush

    async def _commit():
        return None

    db.commit = _commit

    async def _rollback():
        return None

    db.rollback = _rollback

    async def _close():
        return None

    db.close = _close

    # Support async context manager
    async def _aenter():
        return db

    async def _aexit(*args):
        return None

    db.__aenter__ = _aenter
    db.__aexit__ = _aexit

    return db


# ---------------------------------------------------------------------------
# Redis mock
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis():
    """Return an AsyncMock mimicking a redis.asyncio Redis client."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.setex = AsyncMock(return_value=True)
    redis.publish = AsyncMock(return_value=1)
    redis.ping = AsyncMock(return_value=True)
    redis.aclose = AsyncMock(return_value=None)
    return redis


# ---------------------------------------------------------------------------
# Canonical IDs
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_batch_id() -> UUID:
    """Return a fixed UUID for use as a batch ID in tests.

    Using a fixed value makes test failures deterministic and easier to trace.
    """
    return UUID("12345678-1234-5678-1234-567812345678")


# ---------------------------------------------------------------------------
# Sample data dicts
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_sensor_reading(sample_batch_id: UUID) -> dict:
    """Return a valid SensorReadingCreate-compatible payload dict."""
    return {
        "sensor_id": "sensor.grow_room_temperature",
        "batch_id": str(sample_batch_id),
        "sensor_type": "TEMPERATURE",
        "value": 24.5,
        "unit": "°C",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "raw_entity_id": "sensor.grow_room_temperature",
        "source": "HA_PUSH",
    }


@pytest.fixture
def sample_batch_create() -> dict:
    """Return a valid BatchCreate-compatible payload dict."""
    today = date.today()
    return {
        "batch_name": "Test Batch Alpha",
        "strain": "Blue Dream",
        "room_id": "room_01",
        "start_date": today.isoformat(),
        "target_yield_g": 500.0,
        "planned_veg_days": 28,
        "planned_flower_days": 63,
        "notes": "Integration test batch.",
        "metadata": {"test": True},
    }


@pytest.fixture
def sample_features() -> dict:
    """Return a realistic feature dict representing a mid-VEG batch.

    All keys match the feature names produced by the feature engineering
    pipeline and consumed by the recommendation engine / model inference.
    """
    return {
        # Environment — 1-hour means
        "vpd_mean_1h": 1.1,
        "ec_mean_1h": 2.1,
        "ph_mean_1h": 6.0,
        "vwc_mean_1h": 55.0,
        "co2_mean_1h": 1200.0,
        "ppfd_mean_1h": 450.0,
        "temperature_mean_1h": 24.5,
        "humidity_mean_1h": 62.0,
        # Lighting
        "dli_accumulated": 32.5,
        # VPD exceedance (minutes outside target range in last hour)
        "vpd_exceedance_minutes_above": 0,
        "vpd_exceedance_minutes_below": 15,
        # Nutrient solution drift / stability
        "ec_drift_rate_24h": 0.02,   # mS/cm per hour — within normal
        "ph_swing_24h": 0.2,          # pH units over 24 h — stable
        # Substrate / rootzone
        "substrate_dryback_pct": 18.0,  # % dryback before lights-on — healthy
        # Stage & harvest
        "normalized_stage_day": 0.4,    # 40% through current stage
        "days_to_harvest_estimate": 28,
        # Batch context
        "batch_stage": "VEG",
    }


# ---------------------------------------------------------------------------
# Settings override fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_override(monkeypatch):
    """Return a Settings instance configured for testing.

    Uses an in-memory SQLite URL (asyncpg not supported for sqlite, but the
    fixture demonstrates the override mechanism — tests that need real DB
    queries should use postgresql+asyncpg with a test database).

    Advisory mode is True so no real actuator calls can be made.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("ADVISORY_MODE", "true")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("HA_TOKEN", "test-token-not-real")
    monkeypatch.setenv("HA_BASE_URL", "http://ha-test.local:8123")

    # Clear the lru_cache so Settings re-reads from the patched environment
    from src.app.config.settings import get_settings
    get_settings.cache_clear()

    settings = get_settings()
    yield settings

    # Restore cache state after the test
    get_settings.cache_clear()
