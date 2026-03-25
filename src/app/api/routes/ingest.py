"""
Sensor data and batch lifecycle ingest endpoints.

Routes:
    POST   /ingest/sensor                        — single sensor reading
    POST   /ingest/sensor/batch                  — bulk sensor readings (≤ 1000)
    POST   /ingest/batch                         — create cultivation batch
    PUT    /ingest/batch/{batch_id}/stage        — advance batch stage
    GET    /ingest/batches                       — list batches
    POST   /ingest/irrigation-event             — log irrigation event
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis  # type: ignore[import]
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.dependencies import CommonQueryParams, get_db, get_redis
from src.app.core.database import (
    Batch,
    IrrigationEvent,
    SensorReading,
    is_valid_stage_transition,
)
from src.app.core.logging import get_logger

log = get_logger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Sensor value sanity ranges — reject physiologically impossible readings
# ---------------------------------------------------------------------------

SENSOR_RANGES: Dict[str, tuple[float, float]] = {
    "TEMPERATURE": (-10.0, 60.0),     # °C
    "HUMIDITY": (0.0, 100.0),         # %RH
    "CO2": (100.0, 10_000.0),         # ppm
    "VPD": (0.0, 10.0),               # kPa
    "EC": (0.0, 20.0),                # mS/cm
    "PH": (0.0, 14.0),
    "PPFD": (0.0, 3_000.0),           # µmol/m²/s
    "LIGHT_INTENSITY": (0.0, 200_000.0),  # lux
    "PRESSURE": (800.0, 1_200.0),     # hPa
    "WATER_TEMP": (0.0, 50.0),        # °C
    "FLOW_RATE": (0.0, 100_000.0),    # mL/min
}


def _validate_sensor_value(sensor_type: str, value: float) -> Optional[str]:
    """Return an error message if *value* is out of range for *sensor_type*, else None."""
    limits = SENSOR_RANGES.get(sensor_type.upper())
    if limits is None:
        return None  # Unknown type — accept without range check
    lo, hi = limits
    if not (lo <= value <= hi):
        return (
            f"Value {value} is out of the acceptable range [{lo}, {hi}] "
            f"for sensor type '{sensor_type}'"
        )
    return None


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class SensorReadingCreate(BaseModel):
    """Schema for creating a single sensor reading."""

    sensor_id: str = Field(..., min_length=1, max_length=200)
    sensor_type: str = Field(..., min_length=1, max_length=100)
    value: float
    unit: str = Field(..., min_length=1, max_length=50)
    time: Optional[datetime] = Field(
        default=None,
        description="Measurement timestamp (UTC). Defaults to now if omitted.",
    )
    batch_id: Optional[uuid.UUID] = None
    quality_flag: str = Field(default="OK", max_length=50)
    source: str = Field(default="home_assistant", max_length=100)
    raw_entity_id: Optional[str] = Field(default=None, max_length=300)

    @field_validator("time", mode="before")
    @classmethod
    def reject_future_timestamps(cls, v: Any) -> Any:
        if v is None:
            return v
        # Normalise to timezone-aware
        if isinstance(v, datetime):
            ts = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
            if ts > datetime.now(tz=timezone.utc):
                raise ValueError("Sensor reading timestamp cannot be in the future.")
        return v

    @field_validator("sensor_type", mode="before")
    @classmethod
    def uppercase_sensor_type(cls, v: str) -> str:
        return v.upper().strip()


class SensorReadingResponse(BaseModel):
    """Schema returned after persisting a sensor reading."""

    id: uuid.UUID
    sensor_id: str
    sensor_type: str
    value: float
    unit: str
    time: datetime
    batch_id: Optional[uuid.UUID]
    quality_flag: str
    source: str

    model_config = {"from_attributes": True}


class BatchCreate(BaseModel):
    """Schema for creating a new cultivation batch."""

    name: str = Field(..., min_length=1, max_length=200)
    strain: Optional[str] = Field(default=None, max_length=200)
    stage: str = Field(default="GERMINATION", max_length=50)
    started_at: Optional[datetime] = None
    expected_harvest_at: Optional[datetime] = None
    plant_count: int = Field(default=1, ge=1, le=10_000)
    grow_medium: Optional[str] = Field(default=None, max_length=100)
    tent_id: Optional[str] = Field(default=None, max_length=100)
    notes: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("stage", mode="before")
    @classmethod
    def uppercase_stage(cls, v: str) -> str:
        return v.upper().strip()


class BatchResponse(BaseModel):
    """Schema returned for batch objects."""

    id: uuid.UUID
    name: str
    strain: Optional[str]
    stage: str
    started_at: datetime
    expected_harvest_at: Optional[datetime]
    harvested_at: Optional[datetime]
    plant_count: int
    grow_medium: Optional[str]
    tent_id: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class StageUpdateRequest(BaseModel):
    """Request body for advancing a batch to a new stage."""

    stage: str = Field(..., min_length=1, max_length=50)
    notes: Optional[str] = None

    @field_validator("stage", mode="before")
    @classmethod
    def uppercase_stage(cls, v: str) -> str:
        return v.upper().strip()


class IrrigationEventCreate(BaseModel):
    """Schema for logging an irrigation/fertigation event."""

    batch_id: uuid.UUID
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    volume_ml: Optional[float] = Field(default=None, ge=0.0)
    ec_in: Optional[float] = Field(default=None, ge=0.0, le=20.0)
    ph_in: Optional[float] = Field(default=None, ge=0.0, le=14.0)
    ec_runoff: Optional[float] = Field(default=None, ge=0.0, le=20.0)
    ph_runoff: Optional[float] = Field(default=None, ge=0.0, le=14.0)
    runoff_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    nutrient_recipe: Optional[str] = Field(default=None, max_length=200)
    triggered_by: str = Field(default="manual", max_length=100)
    notes: Optional[str] = None


class IrrigationEventResponse(BaseModel):
    """Schema returned for irrigation events."""

    id: uuid.UUID
    batch_id: uuid.UUID
    started_at: datetime
    ended_at: Optional[datetime]
    volume_ml: Optional[float]
    ec_in: Optional[float]
    ph_in: Optional[float]
    ec_runoff: Optional[float]
    ph_runoff: Optional[float]
    runoff_percent: Optional[float]
    nutrient_recipe: Optional[str]
    triggered_by: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helper: publish event to Redis
# ---------------------------------------------------------------------------


async def _publish_sensor_event(
    redis_client: aioredis.Redis,
    reading: SensorReading,
) -> None:
    """Publish a serialised sensor reading to the ``sensor_events`` Redis channel."""
    import json

    payload = {
        "id": str(reading.id),
        "sensor_id": reading.sensor_id,
        "sensor_type": reading.sensor_type,
        "value": reading.value,
        "unit": reading.unit,
        "time": reading.time.isoformat() if reading.time else None,
        "batch_id": str(reading.batch_id) if reading.batch_id else None,
    }
    try:
        await redis_client.publish("sensor_events", json.dumps(payload))
    except Exception as exc:
        # Publishing failure should not abort the ingest request
        log.warning("redis_publish_failed", channel="sensor_events", error=str(exc))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/ingest/sensor",
    response_model=SensorReadingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a single sensor reading",
)
async def ingest_sensor(
    payload: SensorReadingCreate,
    db: AsyncSession = Depends(get_db),
    redis_client: aioredis.Redis = Depends(get_redis),
) -> SensorReadingResponse:
    """Validate and persist a single sensor measurement.

    Also publishes the reading to the ``sensor_events`` Redis pub/sub channel
    so that downstream consumers (e.g. the real-time dashboard) can react
    immediately.
    """
    # Range validation
    error_msg = _validate_sensor_value(payload.sensor_type, payload.value)
    if error_msg:
        raise HTTPException(status_code=422, detail=error_msg)

    reading = SensorReading(
        sensor_id=payload.sensor_id,
        sensor_type=payload.sensor_type,
        value=payload.value,
        unit=payload.unit,
        time=payload.time or datetime.now(tz=timezone.utc),
        batch_id=payload.batch_id,
        quality_flag=payload.quality_flag,
        source=payload.source,
        raw_entity_id=payload.raw_entity_id,
    )
    db.add(reading)
    await db.flush()  # Populate reading.id before publishing

    await _publish_sensor_event(redis_client, reading)

    log.info(
        "sensor_reading_ingested",
        sensor_id=reading.sensor_id,
        sensor_type=reading.sensor_type,
        value=reading.value,
    )
    return SensorReadingResponse.model_validate(reading)


@router.post(
    "/ingest/sensor/batch",
    status_code=status.HTTP_207_MULTI_STATUS,
    summary="Bulk ingest sensor readings (max 1 000)",
)
async def ingest_sensor_batch(
    payloads: List[SensorReadingCreate],
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Bulk-insert up to 1 000 sensor readings using a single SQLAlchemy Core
    ``INSERT`` statement for maximum throughput.

    Returns a multi-status summary with per-row error details.
    """
    if len(payloads) > 1000:
        raise HTTPException(
            status_code=422,
            detail=f"Batch size {len(payloads)} exceeds maximum of 1 000 readings.",
        )

    accepted_rows: list[dict] = []
    rejected: list[dict] = []

    for idx, item in enumerate(payloads):
        error_msg = _validate_sensor_value(item.sensor_type, item.value)
        if error_msg:
            rejected.append({"index": idx, "sensor_id": item.sensor_id, "error": error_msg})
            continue

        accepted_rows.append(
            {
                "id": uuid.uuid4(),
                "sensor_id": item.sensor_id,
                "sensor_type": item.sensor_type,
                "value": item.value,
                "unit": item.unit,
                "time": item.time or datetime.now(tz=timezone.utc),
                "batch_id": item.batch_id,
                "quality_flag": item.quality_flag,
                "source": item.source,
                "raw_entity_id": item.raw_entity_id,
            }
        )

    if accepted_rows:
        await db.execute(insert(SensorReading), accepted_rows)

    log.info(
        "sensor_batch_ingested",
        accepted=len(accepted_rows),
        rejected=len(rejected),
    )

    return {
        "accepted": len(accepted_rows),
        "rejected": len(rejected),
        "errors": rejected,
    }


@router.post(
    "/ingest/batch",
    response_model=BatchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new cultivation batch",
)
async def create_batch(
    payload: BatchCreate,
    db: AsyncSession = Depends(get_db),
) -> BatchResponse:
    """Create a new grow batch and persist it to the database."""
    batch = Batch(
        name=payload.name,
        strain=payload.strain,
        stage=payload.stage,
        started_at=payload.started_at or datetime.now(tz=timezone.utc),
        expected_harvest_at=payload.expected_harvest_at,
        plant_count=payload.plant_count,
        grow_medium=payload.grow_medium,
        tent_id=payload.tent_id,
        notes=payload.notes,
        batch_metadata=payload.metadata,
    )
    db.add(batch)
    await db.flush()

    log.info("batch_created", batch_id=str(batch.id), name=batch.name, stage=batch.stage)
    return BatchResponse.model_validate(batch)


@router.put(
    "/ingest/batch/{batch_id}/stage",
    response_model=BatchResponse,
    summary="Advance a batch to a new growth stage",
)
async def update_batch_stage(
    batch_id: uuid.UUID,
    payload: StageUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> BatchResponse:
    """Transition a batch to the requested growth stage.

    Validates that the transition follows the allowed DAG:
    ``GERMINATION → VEG → FLOWER → HARVEST → ARCHIVED``

    Raises:
        404: Batch not found.
        422: Stage transition is not permitted.
    """
    result = await db.execute(select(Batch).where(Batch.id == batch_id))
    batch = result.scalar_one_or_none()
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found.")

    if not is_valid_stage_transition(batch.stage, payload.stage):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot transition batch from stage '{batch.stage}' to '{payload.stage}'. "
                "Check VALID_STAGE_TRANSITIONS for allowed transitions."
            ),
        )

    old_stage = batch.stage
    batch.stage = payload.stage
    batch.updated_at = datetime.now(tz=timezone.utc)
    if payload.notes:
        batch.notes = (batch.notes or "") + f"\n[{payload.stage}] {payload.notes}"

    if payload.stage == "HARVEST":
        batch.harvested_at = datetime.now(tz=timezone.utc)
    if payload.stage == "ARCHIVED":
        batch.is_active = False

    await db.flush()
    log.info(
        "batch_stage_updated",
        batch_id=str(batch_id),
        old_stage=old_stage,
        new_stage=payload.stage,
    )
    return BatchResponse.model_validate(batch)


@router.get(
    "/ingest/batches",
    response_model=List[BatchResponse],
    summary="List cultivation batches",
)
async def list_batches(
    status_filter: Optional[str] = Query(
        default=None,
        alias="status",
        description="Filter by stage (e.g. VEG, FLOWER). Omit for all.",
    ),
    active_only: bool = Query(default=True, description="Only return active batches"),
    params: CommonQueryParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> List[BatchResponse]:
    """Return a paginated list of cultivation batches.

    Supports filtering by ``status`` (growth stage) and ``active_only`` flag.
    """
    query = select(Batch)
    if active_only:
        query = query.where(Batch.is_active == True)  # noqa: E712
    if status_filter:
        query = query.where(Batch.stage == status_filter.upper())

    query = query.order_by(Batch.created_at.desc()).offset(params.skip).limit(params.limit)
    result = await db.execute(query)
    batches = result.scalars().all()
    return [BatchResponse.model_validate(b) for b in batches]


@router.post(
    "/ingest/irrigation-event",
    response_model=IrrigationEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Log an irrigation / fertigation event",
)
async def log_irrigation_event(
    payload: IrrigationEventCreate,
    db: AsyncSession = Depends(get_db),
) -> IrrigationEventResponse:
    """Persist an irrigation or fertigation event for a batch.

    Raises:
        404: Referenced batch not found.
    """
    # Verify the batch exists
    batch_result = await db.execute(select(Batch).where(Batch.id == payload.batch_id))
    if batch_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=404, detail=f"Batch {payload.batch_id} not found."
        )

    event = IrrigationEvent(
        batch_id=payload.batch_id,
        started_at=payload.started_at or datetime.now(tz=timezone.utc),
        ended_at=payload.ended_at,
        volume_ml=payload.volume_ml,
        ec_in=payload.ec_in,
        ph_in=payload.ph_in,
        ec_runoff=payload.ec_runoff,
        ph_runoff=payload.ph_runoff,
        runoff_percent=payload.runoff_percent,
        nutrient_recipe=payload.nutrient_recipe,
        triggered_by=payload.triggered_by,
        notes=payload.notes,
    )
    db.add(event)
    await db.flush()

    log.info(
        "irrigation_event_logged",
        event_id=str(event.id),
        batch_id=str(payload.batch_id),
        volume_ml=payload.volume_ml,
        triggered_by=payload.triggered_by,
    )
    return IrrigationEventResponse.model_validate(event)
