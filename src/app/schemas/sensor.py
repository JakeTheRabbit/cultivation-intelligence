"""
Pydantic v2 models for sensor data ingestion, validation, and reporting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class SensorType(str, Enum):
    TEMPERATURE = "TEMPERATURE"
    HUMIDITY = "HUMIDITY"
    VPD_CALCULATED = "VPD_CALCULATED"
    EC = "EC"
    PH = "PH"
    VWC = "VWC"
    CO2 = "CO2"
    PPFD = "PPFD"
    FLOW_RATE = "FLOW_RATE"
    DISSOLVED_OXYGEN = "DISSOLVED_OXYGEN"
    WEIGHT = "WEIGHT"


class SensorSource(str, Enum):
    HA_PUSH = "HA_PUSH"
    HA_POLL = "HA_POLL"
    CSV_IMPORT = "CSV_IMPORT"
    MANUAL_ENTRY = "MANUAL_ENTRY"


class QualityFlag(str, Enum):
    OK = "OK"
    SUSPECT_SPIKE = "SUSPECT_SPIKE"
    SUSPECT_FLATLINE = "SUSPECT_FLATLINE"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    INVALID = "INVALID"
    SENSOR_OFFLINE = "SENSOR_OFFLINE"


# Sensor type -> (min, max) physical plausibility bounds
_SENSOR_RANGE_BOUNDS: dict[str, tuple[float, float]] = {
    SensorType.TEMPERATURE: (-10.0, 50.0),
    SensorType.HUMIDITY: (0.0, 100.0),
    SensorType.EC: (0.0, 10.0),
    SensorType.PH: (0.0, 14.0),
    SensorType.VWC: (0.0, 100.0),
    SensorType.CO2: (0.0, 5000.0),
    SensorType.PPFD: (0.0, 3000.0),
}


class SensorReadingCreate(BaseModel):
    sensor_id: str = Field(..., min_length=1, max_length=128)
    batch_id: UUID
    sensor_type: SensorType
    value: float
    unit: str = Field(..., min_length=1, max_length=32)
    timestamp: Optional[datetime] = Field(default=None)
    raw_entity_id: Optional[str] = Field(default=None, max_length=256)
    source: SensorSource = SensorSource.HA_PUSH

    @field_validator("timestamp", mode="before")
    @classmethod
    def set_and_validate_timestamp(cls, v: Optional[datetime]) -> datetime:
        """Default to now (UTC). Reject timestamps more than 1 hour in the future."""
        now = datetime.now(timezone.utc)
        if v is None:
            return now

        # Ensure timezone-aware for comparison
        if isinstance(v, datetime) and v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)

        one_hour_from_now = now.timestamp() + 3600
        if v.timestamp() > one_hour_from_now:
            raise ValueError(
                f"Timestamp {v.isoformat()} is more than 1 hour in the future "
                f"(current UTC: {now.isoformat()})."
            )
        return v

    @field_validator("value", mode="after")
    @classmethod
    def validate_value_range(cls, v: float, info) -> float:
        """Apply physical plausibility range checks based on sensor_type."""
        # info.data contains previously validated fields
        sensor_type = info.data.get("sensor_type")
        if sensor_type is None:
            return v

        bounds = _SENSOR_RANGE_BOUNDS.get(sensor_type)
        if bounds is None:
            # No range defined for this sensor type (e.g. FLOW_RATE, WEIGHT, DISSOLVED_OXYGEN)
            return v

        lo, hi = bounds
        if not (lo <= v <= hi):
            raise ValueError(
                f"Value {v} is outside the valid range [{lo}, {hi}] "
                f"for sensor type {sensor_type}."
            )
        return v

    model_config = {"populate_by_name": True}


class SensorReadingResponse(BaseModel):
    id: UUID
    sensor_id: str
    batch_id: UUID
    sensor_type: SensorType
    value: float
    unit: str
    timestamp: datetime
    raw_entity_id: Optional[str] = None
    source: SensorSource
    quality_flag: QualityFlag
    created_at: datetime

    model_config = {"from_attributes": True}


class SensorReadingBatchRequest(BaseModel):
    readings: List[SensorReadingCreate] = Field(..., max_length=1000)


class SensorReadingBatchResponse(BaseModel):
    accepted: int = Field(..., ge=0)
    rejected: int = Field(..., ge=0)
    errors: List[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> "SensorReadingBatchResponse":
        if self.accepted + self.rejected < 0:
            raise ValueError("accepted + rejected must be non-negative.")
        return self


class SensorStats(BaseModel):
    """Aggregated statistics for a sensor over a time window — used for data quality reporting."""

    sensor_id: str
    sensor_type: SensorType
    batch_id: UUID
    mean: Optional[float] = None
    std: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None
    count: int = 0
    last_value: Optional[float] = None
    last_timestamp: Optional[datetime] = None
    gap_count: int = Field(
        default=0,
        description="Number of detected data gaps exceeding expected reporting interval.",
    )
    quality_ok_pct: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="Percentage of readings flagged as QualityFlag.OK.",
    )

    model_config = {"from_attributes": True}
