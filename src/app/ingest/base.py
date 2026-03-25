"""
Abstract base class for all data ingestion pipelines.

Every concrete ingester (HomeAssistant, CSV, MQTT, etc.) inherits from
BaseIngester and implements fetch_readings / validate_reading / transform_reading.
The orchestration logic lives here in run().
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator

import structlog


@dataclass
class IngestResult:
    """Summary of a completed ingestion run."""

    accepted: int
    rejected: int
    errors: list[dict]
    source: str

    @property
    def total(self) -> int:
        return self.accepted + self.rejected

    @property
    def acceptance_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.accepted / self.total


class BaseIngester(ABC):
    """Abstract interface for sensor data ingestion.

    Subclasses must implement:
        - fetch_readings  — async generator yielding batches of raw dicts
        - validate_reading — returns (is_valid, error_message)
        - transform_reading — returns a SensorReadingCreate-compatible dict
    """

    def __init__(self, batch_size: int = 100) -> None:
        self.batch_size = batch_size
        self.logger = structlog.get_logger(self.__class__.__name__)

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch_readings(self, **kwargs) -> AsyncIterator[list[dict]]:
        """Fetch raw readings from source, yielding batches of raw dicts.

        Each yielded list contains at most self.batch_size items.
        Implementations are responsible for pagination / streaming.
        """
        # This yield makes the method an async generator at the type level;
        # concrete subclasses must provide a real implementation.
        yield  # pragma: no cover

    @abstractmethod
    async def validate_reading(self, raw: dict) -> tuple[bool, str]:
        """Validate a single raw reading.

        Returns:
            (True, "")         — reading is valid, proceed to transform
            (False, "<reason>") — reading should be rejected with this reason
        """
        ...

    @abstractmethod
    async def transform_reading(self, raw: dict) -> dict:
        """Transform a validated raw reading into a SensorReadingCreate dict.

        The returned dict must contain at minimum:
            sensor_id, batch_id, sensor_type, value, unit, timestamp, source
        """
        ...

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    async def run(self, **kwargs) -> IngestResult:
        """Execute the full fetch → validate → transform pipeline.

        Iterates over all batches from fetch_readings, validates each
        individual reading, transforms valid ones, and accumulates
        accept/reject counts and error details.

        Keyword arguments are forwarded to fetch_readings.
        """
        accepted: int = 0
        rejected: int = 0
        errors: list[dict] = []

        self.logger.info("ingestion_started", ingester=self.__class__.__name__)

        async for batch in self.fetch_readings(**kwargs):
            self.logger.debug("processing_batch", size=len(batch))

            for raw in batch:
                try:
                    valid, err_msg = await self.validate_reading(raw)
                except Exception as exc:  # noqa: BLE001
                    # validation itself raised — treat as rejection
                    rejected += 1
                    errors.append(
                        {
                            "raw": str(raw)[:200],
                            "error": f"validate_reading raised: {exc}",
                        }
                    )
                    self.logger.warning(
                        "validate_raised",
                        raw_preview=str(raw)[:120],
                        exc=str(exc),
                    )
                    continue

                if not valid:
                    rejected += 1
                    errors.append({"raw": str(raw)[:200], "error": err_msg})
                    self.logger.debug(
                        "reading_rejected",
                        reason=err_msg,
                        raw_preview=str(raw)[:80],
                    )
                    continue

                try:
                    _transformed = await self.transform_reading(raw)
                    accepted += 1
                except Exception as exc:  # noqa: BLE001
                    rejected += 1
                    errors.append(
                        {
                            "raw": str(raw)[:200],
                            "error": f"transform_reading raised: {exc}",
                        }
                    )
                    self.logger.warning(
                        "transform_raised",
                        raw_preview=str(raw)[:120],
                        exc=str(exc),
                    )

        result = IngestResult(
            accepted=accepted,
            rejected=rejected,
            errors=errors,
            source=self.__class__.__name__,
        )

        self.logger.info(
            "ingestion_complete",
            accepted=accepted,
            rejected=rejected,
            error_count=len(errors),
            acceptance_rate=f"{result.acceptance_rate:.1%}",
        )

        return result
