"""
Health and readiness check endpoints.

Routes:
    GET /health          — liveness probe
    GET /ready           — readiness probe (checks DB, Redis, Home Assistant)
    GET /metrics/summary — lightweight operational statistics
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.dependencies import get_db, get_redis
from src.app.config.settings import get_settings
from src.app.core.database import Batch, Recommendation, SensorReading
from src.app.core.logging import get_logger

log = get_logger(__name__)
settings = get_settings()

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


async def _check_database(db: AsyncSession) -> str:
    """Run a trivial query against the DB and return 'ok' or 'failed'."""
    try:
        await db.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        log.error("health_db_check_failed", error=str(exc))
        return "failed"


async def _check_redis() -> str:
    """Attempt to PING Redis and return 'ok' or 'failed'."""
    import redis.asyncio as aioredis  # type: ignore[import]

    try:
        client = aioredis.from_url(
            settings.REDIS_URL, encoding="utf-8", decode_responses=True
        )
        pong = await asyncio.wait_for(client.ping(), timeout=2.0)
        await client.aclose()
        return "ok" if pong else "failed"
    except Exception as exc:
        log.warning("health_redis_check_failed", error=str(exc))
        return "failed"


async def _check_home_assistant() -> str:
    """Call the HA REST API root and return 'ok' or 'unreachable'."""
    try:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {settings.HA_TOKEN}",
                "Content-Type": "application/json",
            },
            verify=settings.HA_VERIFY_SSL,
            timeout=3.0,
        ) as client:
            resp = await client.get(f"{settings.HA_BASE_URL}/api/")
            if resp.status_code == 200:
                return "ok"
            log.warning(
                "health_ha_check_non_200",
                status_code=resp.status_code,
            )
            return "unreachable"
    except Exception as exc:
        log.warning("health_ha_check_failed", error=str(exc))
        return "unreachable"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/health",
    summary="Liveness probe",
    response_model=None,
)
async def health() -> Dict[str, Any]:
    """Return a simple alive signal.

    Always returns 200 as long as the Python process is running.  Does **not**
    verify downstream dependencies — use ``/ready`` for that.
    """
    return {
        "status": "healthy",
        "timestamp": _utcnow(),
        "version": "0.1.0",
        "environment": settings.APP_ENV,
    }


@router.get(
    "/ready",
    summary="Readiness probe",
    response_model=None,
)
async def ready(
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Verify connectivity to all downstream dependencies.

    Returns:
        200 — when all *critical* checks pass (database + redis).
        503 — when any critical check fails.

    The Home Assistant check is non-critical: HA being unreachable downgrades
    the overall status to ``"degraded"`` but still returns 200.
    """
    # Run all checks concurrently
    db_status, redis_status, ha_status = await asyncio.gather(
        _check_database(db),
        _check_redis(),
        _check_home_assistant(),
    )

    checks: Dict[str, str] = {
        "database": db_status,
        "redis": redis_status,
        "home_assistant": ha_status,
    }

    # Determine overall readiness
    critical_ok = db_status == "ok" and redis_status == "ok"
    ha_ok = ha_status == "ok"

    if critical_ok and ha_ok:
        overall = "ready"
        http_status = status.HTTP_200_OK
    elif critical_ok and not ha_ok:
        overall = "degraded"
        http_status = status.HTTP_200_OK
    else:
        overall = "not_ready"
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE

    response.status_code = http_status

    return {
        "status": overall,
        "checks": checks,
        "timestamp": _utcnow(),
    }


@router.get(
    "/metrics/summary",
    summary="Operational metrics summary",
    response_model=None,
)
async def metrics_summary(
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Return lightweight operational statistics without requiring Prometheus.

    Queries:
    - Total sensor readings stored
    - Active (non-archived) batch count
    - Recommendations generated today
    """
    # Total sensor readings
    total_readings_result = await db.execute(
        select(func.count()).select_from(SensorReading)
    )
    total_readings: int = total_readings_result.scalar_one_or_none() or 0

    # Active batches
    active_batches_result = await db.execute(
        select(func.count())
        .select_from(Batch)
        .where(Batch.is_active == True)  # noqa: E712
    )
    active_batches: int = active_batches_result.scalar_one_or_none() or 0

    # Recommendations created today (UTC)
    today_start = datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    recs_today_result = await db.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(Recommendation.created_at >= today_start)
    )
    recs_today: int = recs_today_result.scalar_one_or_none() or 0

    return {
        "total_sensor_readings": total_readings,
        "active_batches": active_batches,
        "recommendations_today": recs_today,
        "timestamp": _utcnow(),
    }
