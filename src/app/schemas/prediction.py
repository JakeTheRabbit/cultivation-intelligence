"""
Pydantic v2 models for ML predictions, risk scores, yield estimates, and
agronomic recommendations.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator


class PredictionType(str, Enum):
    YIELD_ESTIMATE = "YIELD_ESTIMATE"
    QUALITY_SCORE = "QUALITY_SCORE"
    RISK_SCORE = "RISK_SCORE"
    STAGE_COMPLETION_DAYS = "STAGE_COMPLETION_DAYS"


class RecommendationPriority(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFORMATIONAL = "INFORMATIONAL"

    @property
    def color_code(self) -> str:
        """Hex color code for UI rendering."""
        _colors: dict[str, str] = {
            RecommendationPriority.CRITICAL: "#FF0000",
            RecommendationPriority.HIGH: "#FF6600",
            RecommendationPriority.MEDIUM: "#FFAA00",
            RecommendationPriority.LOW: "#0099FF",
            RecommendationPriority.INFORMATIONAL: "#888888",
        }
        return _colors[self]


class RecommendationStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class RecommendationActionType(str, Enum):
    ADJUST_EC = "ADJUST_EC"
    ADJUST_PH = "ADJUST_PH"
    ADJUST_VPD = "ADJUST_VPD"
    ADJUST_IRRIGATION_FREQUENCY = "ADJUST_IRRIGATION_FREQUENCY"
    ADJUST_IRRIGATION_DURATION = "ADJUST_IRRIGATION_DURATION"
    ADJUST_LIGHTING = "ADJUST_LIGHTING"
    MANUAL_INSPECTION = "MANUAL_INSPECTION"
    NOTIFY_GROWER = "NOTIFY_GROWER"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class RiskFactor(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., max_length=512)
    contribution: float = Field(
        ...,
        ge=-1.0,
        le=1.0,
        description="Signed contribution to total risk score. "
                    "Positive = increases risk, negative = decreases risk.",
    )
    current_value: Optional[float] = None
    target_value: Optional[float] = None
    unit: Optional[str] = Field(default=None, max_length=32)


class SuggestedAction(BaseModel):
    action_type: RecommendationActionType
    description: str = Field(..., max_length=512)
    parameter: Optional[str] = Field(default=None, max_length=128)
    current_value: Optional[float] = None
    suggested_value: Optional[float] = None
    unit: Optional[str] = Field(default=None, max_length=32)
    expected_impact: str = Field(..., max_length=512)


# ---------------------------------------------------------------------------
# Primary response models
# ---------------------------------------------------------------------------

class RiskScoreResponse(BaseModel):
    batch_id: UUID
    risk_score: float = Field(..., ge=0.0, le=1.0)
    factors: List[RiskFactor] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    model_version: str = Field(..., min_length=1, max_length=64)
    computed_at: datetime
    explanation: str = Field(..., max_length=2048)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def risk_level(self) -> str:
        """Categorical risk level derived from the numeric risk_score."""
        if self.risk_score < 0.25:
            return "LOW"
        elif self.risk_score < 0.50:
            return "MEDIUM"
        elif self.risk_score < 0.75:
            return "HIGH"
        else:
            return "CRITICAL"

    model_config = {"from_attributes": True}


class YieldPredictionResponse(BaseModel):
    batch_id: UUID
    point_estimate_g: float = Field(..., ge=0.0)
    confidence_interval_lower: float = Field(..., ge=0.0)
    confidence_interval_upper: float = Field(..., ge=0.0)
    confidence: float = Field(..., ge=0.0, le=1.0)
    model_version: str = Field(..., min_length=1, max_length=64)
    computed_at: datetime
    feature_contributions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Top 5 SHAP values. Each dict contains 'feature', 'value', 'shap_value'.",
    )
    days_to_harvest_estimate: int = Field(..., ge=0)
    explanation: str = Field(..., max_length=2048)

    @model_validator(mode="after")
    def validate_confidence_interval(self) -> "YieldPredictionResponse":
        if self.confidence_interval_lower > self.confidence_interval_upper:
            raise ValueError(
                "confidence_interval_lower must be <= confidence_interval_upper. "
                f"Got [{self.confidence_interval_lower}, {self.confidence_interval_upper}]."
            )
        if not (
            self.confidence_interval_lower
            <= self.point_estimate_g
            <= self.confidence_interval_upper
        ):
            raise ValueError(
                "point_estimate_g must fall within [confidence_interval_lower, "
                "confidence_interval_upper]."
            )
        return self

    @model_validator(mode="after")
    def validate_shap_values(self) -> "YieldPredictionResponse":
        if len(self.feature_contributions) > 5:
            # Silently trim to top 5 rather than hard error
            object.__setattr__(
                self, "feature_contributions", self.feature_contributions[:5]
            )
        return self

    model_config = {"from_attributes": True}


class RecommendationResponse(BaseModel):
    id: UUID
    batch_id: UUID
    recommendation_type: str = Field(..., max_length=64)
    priority: RecommendationPriority
    status: RecommendationStatus
    title: str = Field(..., min_length=1, max_length=256)
    description: str = Field(..., max_length=2048)
    rationale: str = Field(..., max_length=2048)
    actions: List[SuggestedAction] = Field(default_factory=list)
    created_at: datetime
    expires_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None
    operator_notes: Optional[str] = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_expiry_after_creation(self) -> "RecommendationResponse":
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError(
                f"expires_at ({self.expires_at}) must be after created_at ({self.created_at})."
            )
        return self

    model_config = {"from_attributes": True}


class RecommendationAcknowledge(BaseModel):
    status: RecommendationStatus
    operator_id: str = Field(..., min_length=1, max_length=128)
    notes: Optional[str] = Field(default=None, max_length=2048)

    @field_validator("status", mode="after")
    @classmethod
    def validate_acknowledgeable_status(cls, v: RecommendationStatus) -> RecommendationStatus:
        """Only ACCEPTED and REJECTED are valid acknowledgement statuses."""
        allowed = {RecommendationStatus.ACCEPTED, RecommendationStatus.REJECTED}
        if v not in allowed:
            raise ValueError(
                f"RecommendationAcknowledge.status must be one of "
                f"{[s.value for s in allowed]}, got '{v.value}'."
            )
        return v


class BatchFeaturesResponse(BaseModel):
    batch_id: UUID
    computed_at: datetime
    features: Dict[str, Optional[float]] = Field(
        default_factory=dict,
        description="Flat dictionary of feature_name -> computed value (None if unavailable).",
    )
    feature_quality: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-feature quality flag: 'ok', 'missing', 'suspect_range', or 'suspect_quality'."
        ),
    )

    @model_validator(mode="after")
    def validate_quality_keys_match_features(self) -> "BatchFeaturesResponse":
        feature_keys = set(self.features.keys())
        quality_keys = set(self.feature_quality.keys())
        missing_quality = feature_keys - quality_keys
        if missing_quality:
            raise ValueError(
                f"feature_quality is missing entries for features: {missing_quality}. "
                "Every feature must have a corresponding quality flag."
            )
        return self

    model_config = {"from_attributes": True}
