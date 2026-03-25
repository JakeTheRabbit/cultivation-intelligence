"""
Pydantic v2 models for batch/grow metadata, stage management, and irrigation events.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator


class GrowStage(str, Enum):
    PROPAGATION = "PROPAGATION"
    VEG = "VEG"
    EARLY_FLOWER = "EARLY_FLOWER"
    MID_FLOWER = "MID_FLOWER"
    LATE_FLOWER = "LATE_FLOWER"
    FLUSH = "FLUSH"
    HARVEST = "HARVEST"
    COMPLETE = "COMPLETE"

    @property
    def stage_index(self) -> int:
        """Ordinal position for one-way progression enforcement."""
        _order = {
            GrowStage.PROPAGATION: 0,
            GrowStage.VEG: 1,
            GrowStage.EARLY_FLOWER: 2,
            GrowStage.MID_FLOWER: 3,
            GrowStage.LATE_FLOWER: 4,
            GrowStage.FLUSH: 5,
            GrowStage.HARVEST: 6,
            GrowStage.COMPLETE: 7,
        }
        return _order[self]


class QualityGrade(str, Enum):
    GRADE_A = "GRADE_A"
    GRADE_B = "GRADE_B"
    GRADE_C = "GRADE_C"
    REJECTED = "REJECTED"


# ---------------------------------------------------------------------------
# Valid stage transitions — enforce one-way grow progression
# ---------------------------------------------------------------------------
VALID_STAGE_TRANSITIONS: Dict[GrowStage, List[GrowStage]] = {
    GrowStage.PROPAGATION: [GrowStage.VEG],
    GrowStage.VEG: [GrowStage.EARLY_FLOWER],
    GrowStage.EARLY_FLOWER: [GrowStage.MID_FLOWER],
    GrowStage.MID_FLOWER: [GrowStage.LATE_FLOWER],
    GrowStage.LATE_FLOWER: [GrowStage.FLUSH, GrowStage.HARVEST],
    GrowStage.FLUSH: [GrowStage.HARVEST],
    GrowStage.HARVEST: [GrowStage.COMPLETE],
    GrowStage.COMPLETE: [],  # Terminal stage
}


# ---------------------------------------------------------------------------
# Batch models
# ---------------------------------------------------------------------------

class BatchCreate(BaseModel):
    batch_name: str = Field(..., min_length=1, max_length=128)
    strain: str = Field(..., min_length=1, max_length=128)
    room_id: str = Field(..., min_length=1, max_length=64)
    start_date: date
    target_yield_g: Optional[float] = Field(default=None, gt=0.0)
    planned_veg_days: int = Field(default=28, ge=1, le=120)
    planned_flower_days: int = Field(default=63, ge=1, le=200)
    notes: Optional[str] = Field(default=None, max_length=4096)
    metadata: Optional[Dict[str, Any]] = Field(default=None)

    @field_validator("start_date", mode="after")
    @classmethod
    def validate_start_date(cls, v: date) -> date:
        """start_date cannot be more than 30 days in the future."""
        today = date.today()
        delta = (v - today).days
        if delta > 30:
            raise ValueError(
                f"start_date {v} is {delta} days in the future. "
                "Maximum allowed is 30 days ahead."
            )
        return v

    model_config = {"populate_by_name": True}


class BatchResponse(BaseModel):
    id: UUID
    batch_name: str
    strain: str
    room_id: str
    start_date: date
    target_yield_g: Optional[float] = None
    planned_veg_days: int
    planned_flower_days: int
    notes: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    current_stage: GrowStage
    actual_yield_g: Optional[float] = None
    quality_grade: Optional[QualityGrade] = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def days_elapsed(self) -> int:
        """Number of calendar days since the batch start date."""
        return (date.today() - self.start_date).days

    @computed_field  # type: ignore[prop-decorator]
    @property
    def estimated_harvest_date(self) -> Optional[date]:
        """
        Projected harvest date based on planned_veg_days + planned_flower_days
        after start_date. Returns None if the batch has already completed or
        is in HARVEST/COMPLETE stage.
        """
        if self.current_stage in (GrowStage.HARVEST, GrowStage.COMPLETE):
            return None
        total_days = self.planned_veg_days + self.planned_flower_days
        return self.start_date + timedelta(days=total_days)

    model_config = {"from_attributes": True}


class BatchStageUpdate(BaseModel):
    new_stage: GrowStage
    effective_date: Optional[datetime] = Field(default=None)
    notes: Optional[str] = Field(default=None, max_length=2048)

    @field_validator("effective_date", mode="before")
    @classmethod
    def set_effective_date(cls, v: Optional[datetime]) -> datetime:
        if v is None:
            return datetime.now(timezone.utc)
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


# ---------------------------------------------------------------------------
# Irrigation models
# ---------------------------------------------------------------------------

class IrrigationEventCreate(BaseModel):
    batch_id: UUID
    zone_id: str = Field(..., min_length=1, max_length=64)
    duration_seconds: int = Field(..., ge=1, le=86400)
    volume_ml: Optional[float] = Field(default=None, gt=0.0)
    ec_setpoint: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    ph_setpoint: Optional[float] = Field(default=None, ge=0.0, le=14.0)
    trigger_type: str = Field(default="MANUAL", max_length=64)
    recommendation_id: Optional[UUID] = Field(default=None)
    operator_id: Optional[str] = Field(default=None, max_length=128)

    model_config = {"populate_by_name": True}


class IrrigationEventResponse(BaseModel):
    id: UUID
    batch_id: UUID
    zone_id: str
    duration_seconds: int
    volume_ml: Optional[float] = None
    ec_setpoint: Optional[float] = None
    ph_setpoint: Optional[float] = None
    trigger_type: str
    recommendation_id: Optional[UUID] = None
    operator_id: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}
