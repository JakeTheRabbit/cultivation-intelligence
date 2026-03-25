"""
FastAPI application entry point for the Cultivation Intelligence API.

Startup sequence:
1. Configure structlog
2. Initialise the async database engine and create tables
3. Initialise TimescaleDB hypertables
4. Connect to Redis
5. Mount all API routers

Shutdown sequence:
1. Close Redis connection
2. Dispose the async SQLAlchemy engine
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.app.config.settings import get_settings
from src.app.core.database import engine, execute_hypertable_setup, init_db
from src.app.core.logging import configure_logging, get_logger

settings = get_settings()

# Configure structured logging as early as possible (before any other imports
# that might emit log lines).
configure_logging(settings.LOG_LEVEL, is_production=settings.is_production)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Async context manager that owns the full application lifecycle.

    Anything *before* the ``yield`` runs on startup; anything *after* runs
    on shutdown.
    """
    # ---- Startup ----
    log.info(
        "cultivation_intelligence_starting",
        environment=settings.APP_ENV,
        version=app.version,
        log_level=settings.LOG_LEVEL,
    )

    # Database: create tables
    try:
        await init_db()
        log.info("database_tables_created_or_verified")
    except Exception as exc:
        log.error("database_init_failed", error=str(exc))
        raise

    # Database: set up TimescaleDB hypertables (non-fatal if extension missing)
    try:
        await execute_hypertable_setup(engine)
        log.info("timescaledb_hypertables_ready")
    except Exception as exc:
        log.warning("timescaledb_hypertable_setup_failed", error=str(exc))

    # Redis: verify connectivity
    try:
        import redis.asyncio as aioredis  # type: ignore[import]

        _redis = aioredis.from_url(
            settings.REDIS_URL, encoding="utf-8", decode_responses=True
        )
        pong = await _redis.ping()
        if pong:
            log.info("redis_connected", url=settings.REDIS_URL)
        await _redis.aclose()
    except Exception as exc:
        # Redis is used for pub/sub caching; failure is degraded, not fatal
        log.warning("redis_connection_failed", error=str(exc))

    log.info("cultivation_intelligence_ready", host=settings.APP_HOST, port=settings.APP_PORT)

    yield

    # ---- Shutdown ----
    log.info("cultivation_intelligence_shutting_down")
    await engine.dispose()
    log.info("database_engine_disposed")


# ---------------------------------------------------------------------------
# App creation
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Construct and return the configured FastAPI application instance."""

    app = FastAPI(
        title="Cultivation Intelligence API",
        version="0.1.0",
        description=(
            "Indoor cannabis cultivation intelligence platform. "
            "Ingests sensor telemetry, runs ML inference, and surfaces "
            "agronomic recommendations to operators."
        ),
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # ---- CORS ----
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- Request-ID middleware ----
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next) -> Response:
        """Generate a UUID per request and attach it to the response headers.

        The request ID is also bound to the structlog context so that all log
        lines emitted during the request carry the same ``request_id`` field.
        """
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    # ---- Exception handlers ----
    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        log.warning("validation_error", error=str(exc), path=request.url.path)
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc), "type": "validation_error"},
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "unhandled_exception",
            error=str(exc),
            exc_info=True,
            path=request.url.path,
            method=request.method,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "type": "internal_error"},
        )

    # ---- Routers ----
    from src.app.api.routes.health import router as health_router
    from src.app.api.routes.ingest import router as ingest_router
    from src.app.api.routes.predictions import router as predictions_router

    app.include_router(health_router, prefix="/api/v1", tags=["Health"])
    app.include_router(ingest_router, prefix="/api/v1", tags=["Ingest"])
    app.include_router(predictions_router, prefix="/api/v1", tags=["Predictions"])

    # ---- Root endpoint ----
    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {
            "service": "cultivation-intelligence",
            "status": "operational",
            "version": app.version,
            "docs": "/api/docs",
        }

    return app


app = create_app()
