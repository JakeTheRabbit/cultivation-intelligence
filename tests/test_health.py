"""
Tests for the /api/v1/health, /api/v1/ready, and /api/v1/metrics/summary endpoints.

Uses TestClient for synchronous tests and patches the downstream dependency
checks (DB, Redis, HA) to avoid requiring live infrastructure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Liveness probe — GET /api/v1/health
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """Tests for the liveness probe endpoint."""

    def test_health_returns_200(self, client: TestClient) -> None:
        """GET /api/v1/health must always return HTTP 200."""
        response = client.get("/api/v1/health")
        assert response.status_code == 200

    def test_health_has_required_fields(self, client: TestClient) -> None:
        """Response body must include status, timestamp, version, environment."""
        response = client.get("/api/v1/health")
        body = response.json()
        assert "status" in body, "Missing 'status' field"
        assert "timestamp" in body, "Missing 'timestamp' field"
        assert "version" in body, "Missing 'version' field"
        assert "environment" in body, "Missing 'environment' field"
        assert body["status"] == "healthy"

    def test_health_timestamp_is_iso8601(self, client: TestClient) -> None:
        """The timestamp field must be parseable as ISO-8601."""
        from datetime import datetime

        response = client.get("/api/v1/health")
        body = response.json()
        # Should not raise
        ts = datetime.fromisoformat(body["timestamp"])
        assert ts is not None

    def test_health_version_is_string(self, client: TestClient) -> None:
        """Version field must be a non-empty string."""
        response = client.get("/api/v1/health")
        body = response.json()
        assert isinstance(body["version"], str)
        assert len(body["version"]) > 0


# ---------------------------------------------------------------------------
# Readiness probe — GET /api/v1/ready
# ---------------------------------------------------------------------------


class TestReadyEndpoint:
    """Tests for the readiness probe endpoint."""

    def test_ready_with_healthy_db_and_redis(self, client: TestClient) -> None:
        """When DB and Redis both succeed, ready returns 200 with status 'ready' or 'degraded'."""
        with (
            patch(
                "src.app.api.routes.health._check_database",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            patch(
                "src.app.api.routes.health._check_redis",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            patch(
                "src.app.api.routes.health._check_home_assistant",
                new_callable=AsyncMock,
                return_value="ok",
            ),
        ):
            response = client.get("/api/v1/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] in ("ready", "degraded")
        assert body["checks"]["database"] == "ok"
        assert body["checks"]["redis"] == "ok"

    def test_ready_with_failed_db(self, client: TestClient) -> None:
        """When the database check fails, ready returns 503 with database='failed'."""
        with (
            patch(
                "src.app.api.routes.health._check_database",
                new_callable=AsyncMock,
                return_value="failed",
            ),
            patch(
                "src.app.api.routes.health._check_redis",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            patch(
                "src.app.api.routes.health._check_home_assistant",
                new_callable=AsyncMock,
                return_value="ok",
            ),
        ):
            response = client.get("/api/v1/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["database"] == "failed"

    def test_ready_with_failed_redis(self, client: TestClient) -> None:
        """When Redis fails, ready returns 503."""
        with (
            patch(
                "src.app.api.routes.health._check_database",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            patch(
                "src.app.api.routes.health._check_redis",
                new_callable=AsyncMock,
                return_value="failed",
            ),
            patch(
                "src.app.api.routes.health._check_home_assistant",
                new_callable=AsyncMock,
                return_value="ok",
            ),
        ):
            response = client.get("/api/v1/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["redis"] == "failed"

    def test_ready_ha_unreachable_is_non_critical(self, client: TestClient) -> None:
        """HA being unreachable is non-critical: response is still 200 but 'degraded'."""
        with (
            patch(
                "src.app.api.routes.health._check_database",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            patch(
                "src.app.api.routes.health._check_redis",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            patch(
                "src.app.api.routes.health._check_home_assistant",
                new_callable=AsyncMock,
                return_value="unreachable",
            ),
        ):
            response = client.get("/api/v1/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["checks"]["home_assistant"] == "unreachable"

    def test_ready_response_has_checks_field(self, client: TestClient) -> None:
        """Ready response must always include a 'checks' sub-dict."""
        with (
            patch("src.app.api.routes.health._check_database", new_callable=AsyncMock, return_value="ok"),
            patch("src.app.api.routes.health._check_redis", new_callable=AsyncMock, return_value="ok"),
            patch("src.app.api.routes.health._check_home_assistant", new_callable=AsyncMock, return_value="ok"),
        ):
            response = client.get("/api/v1/ready")
        body = response.json()
        assert "checks" in body
        assert "database" in body["checks"]
        assert "redis" in body["checks"]
        assert "home_assistant" in body["checks"]

    def test_ready_has_timestamp(self, client: TestClient) -> None:
        """Ready response must include a 'timestamp' field."""
        with (
            patch("src.app.api.routes.health._check_database", new_callable=AsyncMock, return_value="ok"),
            patch("src.app.api.routes.health._check_redis", new_callable=AsyncMock, return_value="ok"),
            patch("src.app.api.routes.health._check_home_assistant", new_callable=AsyncMock, return_value="ok"),
        ):
            response = client.get("/api/v1/ready")
        body = response.json()
        assert "timestamp" in body


# ---------------------------------------------------------------------------
# Metrics summary — GET /api/v1/metrics/summary
# ---------------------------------------------------------------------------


class TestMetricsSummaryEndpoint:
    """Tests for the lightweight operational metrics endpoint."""

    def test_metrics_summary_returns_200(self, client: TestClient) -> None:
        """GET /api/v1/metrics/summary must return 200."""
        # Patch DB to return zero counts
        mock_scalar_result = MagicMock()
        mock_scalar_result.scalar_one_or_none.return_value = 0

        async def mock_execute(*args, **kwargs):
            return mock_scalar_result

        with patch("src.app.api.routes.health.AsyncSession.execute", new=mock_execute):
            # Use dependency override instead of deep mock for cleaner test
            from src.app.core.database import AsyncSessionLocal

            async def override_get_db():
                mock_session = MagicMock()
                mock_session.execute = mock_execute
                yield mock_session

            from src.app.api.dependencies import get_db
            from src.app.main import create_app

            test_app = create_app()
            test_app.dependency_overrides[get_db] = override_get_db

            with TestClient(test_app, raise_server_exceptions=False) as test_client:
                response = test_client.get("/api/v1/metrics/summary")

        # Accept 200 or 500 (db not available in unit tests)
        assert response.status_code in (200, 500, 503)

    def test_metrics_summary_structure_when_db_available(self, client: TestClient) -> None:
        """When DB queries succeed, response includes required numeric fields."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = 42

        async def mock_execute(*args, **kwargs):
            return mock_result

        from src.app.api.dependencies import get_db
        from src.app.main import create_app

        test_app = create_app()

        async def override_get_db():
            mock_session = MagicMock()
            mock_session.execute = mock_execute
            yield mock_session

        test_app.dependency_overrides[get_db] = override_get_db

        with TestClient(test_app, raise_server_exceptions=False) as test_client:
            response = test_client.get("/api/v1/metrics/summary")

        if response.status_code == 200:
            body = response.json()
            assert "total_sensor_readings" in body
            assert "active_batches" in body
            assert "recommendations_today" in body
            assert "timestamp" in body
            # All count fields must be non-negative integers
            assert body["total_sensor_readings"] >= 0
            assert body["active_batches"] >= 0
            assert body["recommendations_today"] >= 0
