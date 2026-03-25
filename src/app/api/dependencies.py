"""
Shared FastAPI dependencies.

Import these with ``Depends(...)`` in route handlers:

    @router.get("/items")
    async def list_items(
        db: AsyncSession = Depends(get_db),
        redis: Redis = Depends(get_redis),
        params: CommonQueryParams = Depends(),
    ):
        ...
"""

from __future__ import annotations

import uuid
from typing import Annotated, AsyncGenerator, Optional

import redis.asyncio as aioredis  # type: ignore[import]
from fastapi import Depends, Header, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.config.settings import Settings, get_settings
from src.app.core.database import AsyncSessionLocal


# ---------------------------------------------------------------------------
# Database session
# ---------------------------------------------------------------------------


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session; commit on success, rollback on exception."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Redis client
# ---------------------------------------------------------------------------

_redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> AsyncGenerator[aioredis.Redis, None]:
    """Yield a Redis client, reusing the process-level connection pool.

    The client is initialised lazily on the first request and reused for all
    subsequent requests in the same process.
    """
    global _redis_client
    settings = get_settings()
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    yield _redis_client


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def get_settings_dep() -> Settings:
    """Return the cached settings singleton as a FastAPI dependency."""
    return get_settings()


# ---------------------------------------------------------------------------
# Inference service singleton
# ---------------------------------------------------------------------------

_inference_service = None


def get_inference_service():
    """Return the InferenceService singleton, initialising it on first call.

    Returns:
        An :class:`~src.app.services.inference.InferenceService` instance.

    Raises:
        HTTPException 503: If the model registry path is missing or models
            cannot be loaded.
    """
    global _inference_service
    if _inference_service is None:
        try:
            from src.app.services.inference import InferenceService  # type: ignore[import]

            settings = get_settings()
            _inference_service = InferenceService(
                model_registry_path=settings.MODEL_REGISTRY_PATH,
                timeout=settings.MODEL_SERVING_TIMEOUT,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Inference service unavailable: {exc}",
            )
    return _inference_service


# ---------------------------------------------------------------------------
# Recommendation engine singleton
# ---------------------------------------------------------------------------

_recommendation_engine = None


def get_recommendation_engine():
    """Return the RecommendationEngine singleton.

    Returns:
        A :class:`~src.app.services.recommendations.RecommendationEngine` instance.
    """
    global _recommendation_engine
    if _recommendation_engine is None:
        try:
            from src.app.services.recommendations import RecommendationEngine  # type: ignore[import]

            _recommendation_engine = RecommendationEngine(settings=get_settings())
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Recommendation engine unavailable: {exc}",
            )
    return _recommendation_engine


# ---------------------------------------------------------------------------
# Home Assistant HTTP client
# ---------------------------------------------------------------------------


async def get_ha_client() -> AsyncGenerator[None, None]:
    """Yield a configured httpx AsyncClient for the Home Assistant REST API.

    Callers receive an ``httpx.AsyncClient`` with:
    - ``Authorization: Bearer <HA_TOKEN>`` header pre-set
    - ``Content-Type: application/json`` header
    - SSL verification controlled by ``HA_VERIFY_SSL`` setting

    Usage::

        @router.get("/ha-check")
        async def ha_check(client: httpx.AsyncClient = Depends(get_ha_client)):
            resp = await client.get("/api/states")
    """
    import httpx  # type: ignore[import]

    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {settings.HA_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(
        base_url=settings.HA_BASE_URL,
        headers=headers,
        verify=settings.HA_VERIFY_SSL,
        timeout=10.0,
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Pagination params
# ---------------------------------------------------------------------------


class CommonQueryParams:
    """Reusable pagination query parameters for list endpoints."""

    def __init__(
        self,
        skip: int = Query(default=0, ge=0, description="Number of records to skip"),
        limit: int = Query(
            default=50,
            ge=1,
            le=500,
            description="Maximum number of records to return (max 500)",
        ),
    ) -> None:
        self.skip = skip
        self.limit = limit


# ---------------------------------------------------------------------------
# Batch ID path parameter
# ---------------------------------------------------------------------------


def validate_batch_id(
    batch_id: uuid.UUID = Path(..., description="UUID of the cultivation batch"),
) -> uuid.UUID:
    """Path parameter dependency that parses and validates a batch UUID.

    FastAPI will automatically return 422 if the path segment cannot be
    converted to a valid UUID.
    """
    return batch_id


# ---------------------------------------------------------------------------
# Advisory mode safety guard
# ---------------------------------------------------------------------------

# A simple set of admin API keys for the safety guard.  In production this
# should be replaced with a real authentication mechanism.
_ADMIN_KEYS: frozenset[str] = frozenset()


async def require_advisory_mode(
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    """Dependency that enforces the ADVISORY_MODE safety flag.

    When ``ADVISORY_MODE`` is *True* (the default), write endpoints that
    would issue real control actions are permitted — but their effects are
    simulated / logged only.

    When ``ADVISORY_MODE`` is *False*, the system is in live-control mode.
    Only requests that carry a valid ``X-Admin-Key`` header are allowed through.
    All other callers receive ``403 Forbidden``.

    Raises:
        HTTPException 403: If ADVISORY_MODE is False and the caller has no
            valid admin key.
    """
    if settings.ADVISORY_MODE:
        # Advisory mode is on — all callers are permitted.
        return

    # Live-control mode: require admin key
    if not x_admin_key or x_admin_key not in _ADMIN_KEYS:
        raise HTTPException(
            status_code=403,
            detail=(
                "ADVISORY_MODE is disabled. "
                "A valid X-Admin-Key header is required to issue control actions."
            ),
        )
