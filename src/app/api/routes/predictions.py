"""
ML inference and agronomic recommendations endpoints.

Routes:
    GET  /predictions/{batch_id}/risk                                    — risk score
    GET  /predictions/{batch_id}/yield                                   — yield forecast
    GET  /predictions/{batch_id}/recommendations                         — recommendation list
    POST /predictions/{batch_id}/recommendations/{rec_id}/acknowledge    — ack/reject
    GET  /predictions/{batch_id}/features                                — feature vector
    GET  /predictions/history/{batch_id}                                 — prediction history
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.dependencies import CommonQueryParams, get_db
from src.app.config.settings import get_settings
from src.app.core.database import (
    Batch,
    Recommendation,
    SensorReading,
)
from src.app.core.logging import get_logger

log = get_logger(__name__)
settings = get_settings()

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class RiskFactor(BaseModel):
    """A single contributing factor to the overall risk score."""

    name: str
    contribution: float  # 0.0–1.0 relative weight
    current_value: Optional[float] = None
    target_min: Optional[float] = None
    target_max: Optional[float] = None
    unit: Optional[str] = None
    severity: str = "LOW"  # LOW | MEDIUM | HIGH | CRITICAL


class RiskScoreResponse(BaseModel):
    """Aggregated risk assessment for a cultivation batch."""

    batch_id: uuid.UUID
    score: float = Field(..., ge=0.0, le=1.0, description="0 = no risk, 1 = critical")
    risk_level: str  # LOW | MEDIUM | HIGH | CRITICAL
    factors: List[RiskFactor]
    recommendations: List[str]
    confidence: float = Field(..., ge=0.0, le=1.0)
    computed_at: datetime
    model_version: Optional[str] = None


class YieldPredictionResponse(BaseModel):
    """Yield forecast for a cultivation batch."""

    batch_id: uuid.UUID
    point_estimate_grams: float
    confidence_interval_low: float
    confidence_interval_high: float
    confidence: float = Field(..., ge=0.0, le=1.0)
    days_to_harvest: Optional[int] = None
    input_features: Dict[str, Any]
    computed_at: datetime
    model_version: Optional[str] = None


class RecommendationResponse(BaseModel):
    """API representation of a Recommendation row."""

    id: uuid.UUID
    batch_id: uuid.UUID
    recommendation_type: str
    priority: str
    title: str
    description: str
    suggested_actions: Optional[List[Dict[str, Any]]]
    confidence: Optional[float]
    status: str
    operator_notes: Optional[str]
    acknowledged_by: Optional[str]
    acknowledged_at: Optional[datetime]
    expires_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class AcknowledgeRequest(BaseModel):
    """Operator acknowledgement payload."""

    decision: str = Field(
        ...,
        description="Operator decision: 'ACCEPTED' or 'REJECTED'",
        pattern="^(ACCEPTED|REJECTED)$",
    )
    operator_id: Optional[str] = Field(
        default=None, max_length=200, description="Identifier of the operator taking action"
    )
    notes: Optional[str] = Field(default=None, description="Optional operator notes")


class FeatureVector(BaseModel):
    """Current computed feature vector for a batch (for transparency)."""

    batch_id: uuid.UUID
    features: Dict[str, Optional[float]]
    feature_names: List[str]
    computed_at: datetime
    data_window_hours: int = 24
    missing_features: List[str]


class PredictionHistoryItem(BaseModel):
    """One historical prediction record."""

    prediction_type: str  # risk | yield
    score: Optional[float]
    confidence: Optional[float]
    created_at: datetime
    model_version: Optional[str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _risk_level_from_score(score: float) -> str:
    """Convert a numeric risk score to a categorical label."""
    if score < 0.25:
        return "LOW"
    if score < 0.50:
        return "MEDIUM"
    if score < 0.75:
        return "HIGH"
    return "CRITICAL"


async def _get_batch_or_404(db: AsyncSession, batch_id: uuid.UUID) -> Batch:
    """Fetch a batch by ID or raise 404."""
    result = await db.execute(select(Batch).where(Batch.id == batch_id))
    batch = result.scalar_one_or_none()
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found.")
    return batch


async def _compute_recent_features(
    db: AsyncSession,
    batch_id: uuid.UUID,
    window_hours: int = 24,
) -> Tuple[Dict[str, Optional[float]], List[str]]:
    """Aggregate the most recent sensor readings into a feature vector.

    For each sensor type, computes mean, min, max, and std over the last
    *window_hours*.  Returns ``(features_dict, missing_feature_names)``.
    """
    from datetime import timedelta

    from sqlalchemy import and_

    cutoff = _utcnow() - timedelta(hours=window_hours)

    result = await db.execute(
        select(
            SensorReading.sensor_type,
            func.avg(SensorReading.value).label("mean"),
            func.min(SensorReading.value).label("min"),
            func.max(SensorReading.value).label("max"),
            func.stddev(SensorReading.value).label("std"),
            func.count(SensorReading.id).label("count"),
        )
        .where(
            and_(
                SensorReading.batch_id == batch_id,
                SensorReading.time >= cutoff,
            )
        )
        .group_by(SensorReading.sensor_type)
    )
    rows = result.all()

    features: Dict[str, Optional[float]] = {}
    for row in rows:
        st = row.sensor_type.lower()
        features[f"{st}_mean"] = float(row.mean) if row.mean is not None else None
        features[f"{st}_min"] = float(row.min) if row.min is not None else None
        features[f"{st}_max"] = float(row.max) if row.max is not None else None
        features[f"{st}_std"] = float(row.std) if row.std is not None else None
        features[f"{st}_count"] = float(row.count)

    # Expected feature names for the model
    expected = [
        "temperature_mean", "temperature_std",
        "humidity_mean", "humidity_std",
        "vpd_mean", "vpd_min", "vpd_max",
        "co2_mean",
        "ec_mean", "ec_std",
        "ph_mean", "ph_std",
        "ppfd_mean",
    ]
    missing = [f for f in expected if f not in features]
    return features, missing


def _heuristic_risk_score(features: Dict[str, Optional[float]]) -> Tuple[float, List[RiskFactor]]:
    """Compute a rule-based risk score when no ML model is available.

    Each parameter is checked against its target window.  Deviations are
    converted to a normalised penalty that contributes to the total score.

    Returns ``(score: float, factors: List[RiskFactor])`` where score is 0–1.
    """
    factors: List[RiskFactor] = []
    total_penalty = 0.0
    weight_sum = 0.0

    def _add_factor(
        name: str,
        feature_key: str,
        target_min: float,
        target_max: float,
        unit: str,
        weight: float,
    ) -> None:
        nonlocal total_penalty, weight_sum
        value = features.get(feature_key)
        if value is None:
            return
        target_mid = (target_min + target_max) / 2
        target_range = (target_max - target_min) / 2 or 1.0
        deviation = abs(value - target_mid) / target_range
        # Clamp penalty to [0, 1]
        penalty = min(deviation / 2.0, 1.0)
        severity = "LOW"
        if penalty > 0.75:
            severity = "CRITICAL"
        elif penalty > 0.50:
            severity = "HIGH"
        elif penalty > 0.25:
            severity = "MEDIUM"
        factors.append(
            RiskFactor(
                name=name,
                contribution=round(penalty * weight, 4),
                current_value=round(value, 3),
                target_min=target_min,
                target_max=target_max,
                unit=unit,
                severity=severity,
            )
        )
        total_penalty += penalty * weight
        weight_sum += weight

    _add_factor("VPD", "vpd_mean", settings.VPD_TARGET_MIN, settings.VPD_TARGET_MAX, "kPa", 0.30)
    _add_factor("EC", "ec_mean", settings.EC_TARGET_MIN, settings.EC_TARGET_MAX, "mS/cm", 0.25)
    _add_factor("pH", "ph_mean", settings.PH_TARGET_MIN, settings.PH_TARGET_MAX, "", 0.25)
    _add_factor("Temperature", "temperature_mean", 18.0, 28.0, "°C", 0.10)
    _add_factor("CO₂", "co2_mean", 800.0, 1_500.0, "ppm", 0.10)

    score = (total_penalty / weight_sum) if weight_sum > 0.0 else 0.0
    return round(min(score, 1.0), 4), factors


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/predictions/{batch_id}/risk",
    response_model=RiskScoreResponse,
    summary="Get current risk score for a batch",
)
async def get_risk_score(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> RiskScoreResponse:
    """Compute the current agronomic risk score for a batch.

    First tries to call the LightGBM inference service.  Falls back to a
    heuristic rule-based scorer if the model is unavailable.

    Raises:
        404: Batch not found.
        503: Inference service timed out (only when model is required and
             no fallback is available).
    """
    await _get_batch_or_404(db, batch_id)

    features, missing = await _compute_recent_features(db, batch_id)

    # Attempt model inference; fall back to heuristic
    model_version: Optional[str] = None
    confidence: float = 0.5  # default for heuristic

    try:
        inference_svc = None
        try:
            from src.app.api.dependencies import get_inference_service  # type: ignore[import]
            inference_svc = get_inference_service()
        except HTTPException:
            pass  # model not loaded — use heuristic

        if inference_svc is not None:
            result = await asyncio.wait_for(
                asyncio.to_thread(inference_svc.predict_risk, features),
                timeout=settings.MODEL_SERVING_TIMEOUT,
            )
            score = float(result["score"])
            confidence = float(result.get("confidence", 0.7))
            model_version = result.get("model_version")
            # Rebuild factors from model output if provided
            factors = [
                RiskFactor(
                    name=f["name"],
                    contribution=f["contribution"],
                    current_value=f.get("value"),
                    severity=f.get("severity", "LOW"),
                )
                for f in result.get("factors", [])
            ]
        else:
            score, factors = _heuristic_risk_score(features)
    except asyncio.TimeoutError:
        log.warning("inference_timeout", batch_id=str(batch_id), operation="risk")
        score, factors = _heuristic_risk_score(features)

    risk_level = _risk_level_from_score(score)

    # Build plain-text recommendation hints
    rec_texts: List[str] = []
    for f in factors:
        if f.severity in ("HIGH", "CRITICAL"):
            rec_texts.append(
                f"Investigate {f.name}: current {f.current_value} {f.unit or ''} "
                f"(target {f.target_min}–{f.target_max} {f.unit or ''})"
            )

    return RiskScoreResponse(
        batch_id=batch_id,
        score=score,
        risk_level=risk_level,
        factors=factors,
        recommendations=rec_texts,
        confidence=confidence,
        computed_at=_utcnow(),
        model_version=model_version,
    )


@router.get(
    "/predictions/{batch_id}/yield",
    response_model=YieldPredictionResponse,
    summary="Get yield prediction for a batch",
)
async def get_yield_prediction(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> YieldPredictionResponse:
    """Predict final dry-weight yield for a batch.

    Uses historical sensor aggregates, plant count, and growth stage as
    input features.  Returns a point estimate with a 90 % confidence
    interval.

    Raises:
        404: Batch not found.
        503: Inference timed out and no fallback is possible.
    """
    batch = await _get_batch_or_404(db, batch_id)
    features, missing = await _compute_recent_features(db, batch_id)

    # Add batch-level features
    features["plant_count"] = float(batch.plant_count)
    stage_encoding = {
        "GERMINATION": 0,
        "VEG": 1,
        "FLOWER": 2,
        "HARVEST": 3,
        "ARCHIVED": 4,
    }
    features["stage_encoded"] = float(stage_encoding.get(batch.stage, 0))

    # Days since start
    days_since_start = (
        (_utcnow() - batch.started_at).days if batch.started_at else 0
    )
    features["days_since_start"] = float(days_since_start)

    # Attempt model; fall back to naive estimate
    model_version: Optional[str] = None
    confidence = 0.45  # heuristic confidence is low

    try:
        inference_svc = None
        try:
            from src.app.api.dependencies import get_inference_service  # type: ignore[import]
            inference_svc = get_inference_service()
        except HTTPException:
            pass

        if inference_svc is not None:
            result = await asyncio.wait_for(
                asyncio.to_thread(inference_svc.predict_yield, features),
                timeout=settings.MODEL_SERVING_TIMEOUT,
            )
            point = float(result["point_estimate_grams"])
            ci_low = float(result["confidence_interval_low"])
            ci_high = float(result["confidence_interval_high"])
            confidence = float(result.get("confidence", 0.7))
            model_version = result.get("model_version")
        else:
            # Naive heuristic: 40g per plant at median conditions
            base_per_plant = 40.0
            vpd_mean = features.get("vpd_mean") or settings.VPD_TARGET_MAX
            vpd_penalty = max(0.0, 1.0 - abs(vpd_mean - 1.0) * 0.3)
            point = base_per_plant * batch.plant_count * vpd_penalty
            ci_low = point * 0.7
            ci_high = point * 1.3
    except asyncio.TimeoutError:
        log.warning("inference_timeout", batch_id=str(batch_id), operation="yield")
        raise HTTPException(
            status_code=503,
            detail="Yield inference timed out. Retry after model warms up.",
        )

    days_to_harvest: Optional[int] = None
    if batch.expected_harvest_at:
        delta = (batch.expected_harvest_at - _utcnow()).days
        days_to_harvest = max(0, delta)

    return YieldPredictionResponse(
        batch_id=batch_id,
        point_estimate_grams=round(point, 2),
        confidence_interval_low=round(ci_low, 2),
        confidence_interval_high=round(ci_high, 2),
        confidence=round(confidence, 4),
        days_to_harvest=days_to_harvest,
        input_features={k: v for k, v in features.items() if v is not None},
        computed_at=_utcnow(),
        model_version=model_version,
    )


@router.get(
    "/predictions/{batch_id}/recommendations",
    response_model=List[RecommendationResponse],
    summary="List recommendations for a batch",
)
async def list_recommendations(
    batch_id: uuid.UUID,
    rec_status: Optional[str] = Query(
        default=None,
        alias="status",
        description="Filter by status (PENDING, ACKNOWLEDGED, ACCEPTED, REJECTED, EXPIRED)",
    ),
    params: CommonQueryParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> List[RecommendationResponse]:
    """Return recommendations for a batch, optionally filtered by status.

    Results are ordered newest-first.

    Raises:
        404: Batch not found.
    """
    await _get_batch_or_404(db, batch_id)

    query = (
        select(Recommendation)
        .where(Recommendation.batch_id == batch_id)
        .order_by(desc(Recommendation.created_at))
        .offset(params.skip)
        .limit(params.limit)
    )
    if rec_status:
        query = query.where(Recommendation.status == rec_status.upper())

    result = await db.execute(query)
    recs = result.scalars().all()
    return [RecommendationResponse.model_validate(r) for r in recs]


@router.post(
    "/predictions/{batch_id}/recommendations/{recommendation_id}/acknowledge",
    response_model=RecommendationResponse,
    summary="Acknowledge or reject a recommendation",
)
async def acknowledge_recommendation(
    batch_id: uuid.UUID,
    recommendation_id: uuid.UUID,
    payload: AcknowledgeRequest,
    db: AsyncSession = Depends(get_db),
) -> RecommendationResponse:
    """Allow an operator to accept or reject a recommendation.

    Raises:
        404: Batch or recommendation not found.
        409: Recommendation has already been acknowledged.
    """
    await _get_batch_or_404(db, batch_id)

    result = await db.execute(
        select(Recommendation).where(
            Recommendation.id == recommendation_id,
            Recommendation.batch_id == batch_id,
        )
    )
    rec = result.scalar_one_or_none()
    if rec is None:
        raise HTTPException(
            status_code=404,
            detail=f"Recommendation {recommendation_id} not found for batch {batch_id}.",
        )

    if rec.status not in ("PENDING", "ACKNOWLEDGED"):
        raise HTTPException(
            status_code=409,
            detail=f"Recommendation is already in terminal state '{rec.status}'.",
        )

    rec.status = payload.decision  # ACCEPTED or REJECTED
    rec.acknowledged_at = _utcnow()
    rec.acknowledged_by = payload.operator_id
    if payload.notes:
        rec.operator_notes = payload.notes

    await db.flush()

    log.info(
        "recommendation_acknowledged",
        recommendation_id=str(recommendation_id),
        batch_id=str(batch_id),
        decision=payload.decision,
        operator=payload.operator_id,
    )
    return RecommendationResponse.model_validate(rec)


@router.get(
    "/predictions/{batch_id}/features",
    response_model=FeatureVector,
    summary="Return the current feature vector for a batch (debug/transparency)",
)
async def get_batch_features(
    batch_id: uuid.UUID,
    window_hours: int = Query(
        default=24,
        ge=1,
        le=168,
        description="Aggregation window in hours (1–168)",
    ),
    db: AsyncSession = Depends(get_db),
) -> FeatureVector:
    """Expose the exact feature vector that would be fed to the ML model.

    Useful for operator transparency and debugging model decisions.

    Raises:
        404: Batch not found.
    """
    await _get_batch_or_404(db, batch_id)

    features, missing = await _compute_recent_features(db, batch_id, window_hours=window_hours)

    return FeatureVector(
        batch_id=batch_id,
        features=features,
        feature_names=sorted(features.keys()),
        computed_at=_utcnow(),
        data_window_hours=window_hours,
        missing_features=missing,
    )


@router.get(
    "/predictions/history/{batch_id}",
    response_model=List[PredictionHistoryItem],
    summary="Historical prediction records for a batch",
)
async def get_prediction_history(
    batch_id: uuid.UUID,
    prediction_type: Optional[str] = Query(
        default=None,
        description="Filter by type: 'risk' or 'yield'",
    ),
    params: CommonQueryParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> List[PredictionHistoryItem]:
    """Return historical risk and yield predictions for a batch.

    Predictions are sourced from the ``recommendations`` table (which stores
    confidence and model version) and from any ``control_actions`` of inference
    type.  Results are ordered newest-first.

    Raises:
        404: Batch not found.
    """
    await _get_batch_or_404(db, batch_id)

    # Query recommendations as a proxy for prediction history (each recommendation
    # is generated from a model run and carries confidence + created_at).
    query = (
        select(Recommendation)
        .where(Recommendation.batch_id == batch_id)
        .order_by(desc(Recommendation.created_at))
        .offset(params.skip)
        .limit(params.limit)
    )

    if prediction_type:
        type_map = {
            "risk": ["ADJUST_EC", "ADJUST_PH", "ADJUST_VPD", "ALERT"],
            "yield": ["IRRIGATION", "LIGHT"],
        }
        allowed = type_map.get(prediction_type.lower(), [])
        if allowed:
            from sqlalchemy import or_

            query = query.where(
                or_(
                    *[Recommendation.recommendation_type == t for t in allowed]
                )
            )

    result = await db.execute(query)
    recs = result.scalars().all()

    history: List[PredictionHistoryItem] = []
    for rec in recs:
        # Map recommendation type to prediction type
        risk_types = {"ADJUST_EC", "ADJUST_PH", "ADJUST_VPD", "ALERT"}
        ptype = "risk" if rec.recommendation_type in risk_types else "yield"

        history.append(
            PredictionHistoryItem(
                prediction_type=ptype,
                score=rec.confidence,
                confidence=rec.confidence,
                created_at=rec.created_at,
                model_version=None,  # stored in prediction_ids if populated
            )
        )

    return history
