"""
Structured logging configuration using structlog.

Example usage::

    from src.app.core.logging import get_logger, configure_logging

    configure_logging("INFO")
    log = get_logger(__name__)

    log.info("sensor_received", sensor_id="temp_01", value=24.5, unit="C")

    # Bind context for the duration of a request
    bound = log.bind(request_id="abc-123", batch_id="batch-456")
    bound.info("processing_started")

The module selects JSON rendering in production and a human-friendly
ConsoleRenderer in all other environments.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog
from structlog.types import EventDict, Processor


# ---------------------------------------------------------------------------
# Optional context dataclass for passing structured metadata around
# ---------------------------------------------------------------------------


@dataclass
class LogContext:
    """Lightweight container for correlated log fields.

    Pass an instance of this to ``log.bind(**log_ctx.as_dict())`` to add
    consistent trace fields to a bound logger.
    """

    request_id: Optional[str] = field(default=None)
    batch_id: Optional[str] = field(default=None)
    sensor_id: Optional[str] = field(default=None)

    def as_dict(self) -> dict[str, Any]:
        """Return non-None fields as a plain dict suitable for structlog.bind()."""
        return {k: v for k, v in self.__dict__.items() if v is not None}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _drop_color_message_key(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    """Remove the internal ``color_message`` key injected by uvicorn access logs."""
    event_dict.pop("color_message", None)
    return event_dict


def _build_processors(is_production: bool) -> list[Processor]:
    """Construct the structlog processor chain for the given environment."""
    shared: list[Processor] = [
        # Merge context variables added via structlog.contextvars
        structlog.contextvars.merge_contextvars,
        # Add log level as a string field
        structlog.stdlib.add_log_level,
        # Add logger name
        structlog.stdlib.add_logger_name,
        # Render exception info as a string
        structlog.processors.format_exc_info,
        # Render stack info
        structlog.processors.StackInfoRenderer(),
        # ISO-8601 timestamp
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Drop uvicorn internal key
        _drop_color_message_key,
    ]

    if is_production:
        shared.append(structlog.processors.JSONRenderer())
    else:
        shared.append(
            structlog.dev.ConsoleRenderer(
                colors=True,
                exception_formatter=structlog.dev.plain_traceback,
            )
        )

    return shared


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def configure_logging(log_level: str, *, is_production: bool = False) -> None:
    """Configure structlog and the standard library root logger.

    Call this **once** at application startup (e.g. inside the lifespan context
    manager) before any loggers are created.

    Args:
        log_level: A standard level string such as ``"INFO"`` or ``"DEBUG"``.
        is_production: When *True* emit JSON log lines; otherwise use a
            human-friendly coloured console renderer.
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Standard-library integration: forward all stdlib log records into structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
    )

    # Quieten noisy third-party loggers in production
    if is_production:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    processors = _build_processors(is_production)

    structlog.configure(
        processors=processors,
        # Use PrintLoggerFactory so output goes to stdout (captured by Docker etc.)
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        # Cache the logger on the class so ``get_logger`` is cheap after first call
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = __name__) -> structlog.stdlib.BoundLogger:
    """Return a structlog bound logger for *name*.

    Args:
        name: Typically ``__name__`` of the calling module.

    Returns:
        A structlog :class:`~structlog.stdlib.BoundLogger` instance.
    """
    return structlog.get_logger(name)
