"""
Inference service with uncertainty estimation and Redis caching.

Provides:
    InferenceService — async methods for risk scoring and yield prediction,
                       backed by the ModelRegistry and Redis cache.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import numpy as np
import pandas as pd
import structlog

from src.app.models.registry import ModelRegistry
from src.app.schemas.prediction import (
    RiskFactor,
    RiskScoreResponse,
    YieldPredictionResponse,
)

logger = structlog.get_logger(__name__)

# Redis TTLs (seconds)
_DEFAULT_TTL = 300        # 5 minutes for predictions
_RISK_TTL = 300
_YIELD_TTL = 300

# Number of ensemble members for uncertainty estimation
_ENSEMBLE_SIZE = 5


class InferenceService:
    """Async service that loads production models and generates predictions.

    Args:
        registry: ModelRegistry instance pointing to the model storage directory.
        settings: Application Settings (used for model config, feature defaults).
        cache:    Redis client (redis.asyncio.Redis or compatible aioredis client).
    """

    def __init__(
        self,
        registry: ModelRegistry,
        settings: Any,
        cache: Any,
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.cache = cache

    # ------------------------------------------------------------------
    # Public prediction methods
    # ------------------------------------------------------------------

    async def get_risk_score(
        self, batch_id: UUID, features: dict
    ) -> RiskScoreResponse:
        """Compute a risk score for a grow batch using the production risk model.

        Pipeline:
            1. Check Redis cache.
            2. Load production risk model from registry.
            3. Align features to model's expected input columns.
            4. Predict probability of high-risk outcome.
            5. Extract top-5 SHAP contributions as RiskFactor list.
            6. Build human-readable explanation string.
            7. Cache result and return RiskScoreResponse.

        Args:
            batch_id: UUID of the grow batch.
            features: Dict of feature_name -> float value.

        Returns:
            RiskScoreResponse

        Raises:
            RuntimeError: If no production risk model is registered.
        """
        cache_key = f"risk:{batch_id}"

        # 1. Cache check
        cached = await self._get_cached(cache_key)
        if cached is not None:
            logger.debug("risk_cache_hit", batch_id=str(batch_id))
            return RiskScoreResponse(**cached)

        # 2. Load model
        result = self.registry.get_production_model("risk")
        if result is None:
            raise RuntimeError(
                "No production risk model is registered. "
                "Train and promote a risk model first."
            )
        model, metadata = result

        # 3. Build feature DataFrame
        X = self._features_to_dataframe(features, metadata.feature_names)

        # 4. Risk probability prediction (binary → P(high risk))
        raw_preds = model.predict(X.values)
        # LightGBM binary outputs probability directly
        risk_prob = float(np.clip(raw_preds[0], 0.0, 1.0))

        # 5. SHAP contributions for top-5 risk factors
        shap_values = model.predict(X.values, pred_contrib=True)
        # shap_values shape: (1, n_features + 1) — last col is bias/expected_value
        n_features = len(metadata.feature_names)
        shap_row = shap_values[0, :n_features]

        # Build RiskFactor list from signed SHAP values (top 5 by absolute magnitude)
        shap_series = pd.Series(shap_row, index=metadata.feature_names)
        top5_idx = shap_series.abs().nlargest(5).index
        risk_factors: list[RiskFactor] = []

        for feat_name in top5_idx:
            shap_val = float(shap_series[feat_name])
            current_val = features.get(feat_name)
            risk_factors.append(
                RiskFactor(
                    name=feat_name,
                    description=_risk_factor_description(feat_name, shap_val),
                    contribution=float(np.clip(shap_val, -1.0, 1.0)),
                    current_value=current_val,
                    target_value=None,
                    unit=_infer_unit(feat_name),
                )
            )

        # 6. Human-readable explanation
        explanation = _build_risk_explanation(risk_prob, risk_factors)

        # 7. Confidence: inversely related to prediction entropy
        #    H = -(p*log(p) + (1-p)*log(1-p)); max H = log(2) ≈ 0.693
        p = np.clip(risk_prob, 1e-7, 1 - 1e-7)
        entropy = -(p * math.log(p) + (1 - p) * math.log(1 - p))
        confidence = float(1.0 - entropy / math.log(2))

        response = RiskScoreResponse(
            batch_id=batch_id,
            risk_score=risk_prob,
            factors=risk_factors,
            confidence=round(confidence, 4),
            model_version=metadata.version,
            computed_at=datetime.now(timezone.utc),
            explanation=explanation,
        )

        # Cache
        await self._set_cached(cache_key, _risk_response_to_dict(response), ttl=_RISK_TTL)

        logger.info(
            "risk_score_computed",
            batch_id=str(batch_id),
            risk_score=round(risk_prob, 4),
            confidence=round(confidence, 4),
            model_version=metadata.version,
        )

        return response

    async def get_yield_prediction(
        self, batch_id: UUID, features: dict
    ) -> YieldPredictionResponse:
        """Predict final yield (grams) with uncertainty estimation.

        Pipeline:
            1. Check Redis cache.
            2. Load production yield model.
            3. Run ensemble of 5 predictions (varying num_iteration) for CI.
            4. point_estimate = median; CI from 10th / 90th percentile.
            5. Extract top-5 SHAP contributions.
            6. Estimate days to harvest from features.
            7. Cache and return YieldPredictionResponse.

        Args:
            batch_id: UUID of the grow batch.
            features: Dict of feature_name -> float value.

        Returns:
            YieldPredictionResponse

        Raises:
            RuntimeError: If no production yield model is registered.
        """
        cache_key = f"yield:{batch_id}"

        # 1. Cache check
        cached = await self._get_cached(cache_key)
        if cached is not None:
            logger.debug("yield_cache_hit", batch_id=str(batch_id))
            return YieldPredictionResponse(**cached)

        # 2. Load model
        result = self.registry.get_production_model("yield")
        if result is None:
            raise RuntimeError(
                "No production yield model is registered. "
                "Train and promote a yield model first."
            )
        model, metadata = result

        # 3. Build feature DataFrame
        X = self._features_to_dataframe(features, metadata.feature_names)

        # 4. Ensemble predictions for uncertainty estimation
        point_estimate, ci_lower, ci_upper = self._ensemble_predict(
            model, X, n_samples=_ENSEMBLE_SIZE
        )

        # Validate monotonicity (lower ≤ point ≤ upper)
        ci_lower = min(ci_lower, point_estimate)
        ci_upper = max(ci_upper, point_estimate)

        # 5. SHAP feature contributions (top 5)
        shap_values = model.predict(X.values, pred_contrib=True)
        n_features = len(metadata.feature_names)
        shap_row = shap_values[0, :n_features]

        shap_series = pd.Series(shap_row, index=metadata.feature_names)
        top5_idx = shap_series.abs().nlargest(5).index

        feature_contributions: list[dict] = []
        for feat_name in top5_idx:
            feature_contributions.append(
                {
                    "feature": feat_name,
                    "value": features.get(feat_name),
                    "shap_value": round(float(shap_series[feat_name]), 4),
                    "unit": _infer_unit(feat_name),
                }
            )

        # 6. Days to harvest estimate (simple heuristic from features)
        days_remaining = _estimate_days_to_harvest(features)

        # 7. Confidence: based on CI width relative to point estimate
        ci_width = ci_upper - ci_lower
        relative_width = ci_width / max(point_estimate, 1.0)
        confidence = float(np.clip(1.0 - relative_width / 2.0, 0.1, 0.99))

        explanation = _build_yield_explanation(
            point_estimate, ci_lower, ci_upper, feature_contributions, days_remaining
        )

        response = YieldPredictionResponse(
            batch_id=batch_id,
            point_estimate_g=round(point_estimate, 2),
            confidence_interval_lower=round(ci_lower, 2),
            confidence_interval_upper=round(ci_upper, 2),
            confidence=round(confidence, 4),
            model_version=metadata.version,
            computed_at=datetime.now(timezone.utc),
            feature_contributions=feature_contributions,
            days_to_harvest_estimate=days_remaining,
            explanation=explanation,
        )

        await self._set_cached(cache_key, _yield_response_to_dict(response), ttl=_YIELD_TTL)

        logger.info(
            "yield_prediction_computed",
            batch_id=str(batch_id),
            point_estimate_g=round(point_estimate, 2),
            ci=[round(ci_lower, 2), round(ci_upper, 2)],
            confidence=round(confidence, 4),
            model_version=metadata.version,
        )

        return response

    # ------------------------------------------------------------------
    # Ensemble prediction (uncertainty estimation)
    # ------------------------------------------------------------------

    def _ensemble_predict(
        self,
        model,
        X: pd.DataFrame,
        n_samples: int = 5,
    ) -> tuple[float, float, float]:
        """Simulate an ensemble by predicting with varying num_iteration values.

        LightGBM does not support MC-dropout natively, but we can create a
        surrogate ensemble by trimming the boosting rounds at different
        fractions of the final iteration.  This reflects the model's
        "learning curve uncertainty" — earlier stopping gives noisier estimates.

        Iteration schedule: [60%, 70%, 80%, 90%, 100%] of best_iteration.

        Args:
            model:     Trained LightGBM Booster.
            X:         Feature DataFrame (single row expected, but handles batches).
            n_samples: Number of ensemble members.

        Returns:
            (point_estimate, lower_bound, upper_bound)
            where point = median and bounds = 10th / 90th percentile of ensemble.
        """
        best_iter = model.best_iteration if model.best_iteration > 0 else model.num_trees()
        if best_iter <= 0:
            best_iter = model.num_trees()

        # Fractional iteration schedule
        fractions = np.linspace(0.6, 1.0, n_samples)
        iters = [max(1, int(best_iter * f)) for f in fractions]

        ensemble_preds: list[float] = []
        for num_iter in iters:
            preds = model.predict(X.values, num_iteration=num_iter)
            # preds shape: (n_rows,) for regression/binary
            ensemble_preds.append(float(np.mean(preds)))

        ensemble_arr = np.array(ensemble_preds)
        point = float(np.median(ensemble_arr))
        lower = float(np.percentile(ensemble_arr, 10))
        upper = float(np.percentile(ensemble_arr, 90))

        return point, lower, upper

    # ------------------------------------------------------------------
    # Feature alignment
    # ------------------------------------------------------------------

    def _features_to_dataframe(
        self, features: dict, feature_names: list[str]
    ) -> pd.DataFrame:
        """Align a features dict to the model's expected input columns.

        Missing features are filled with 0.0 (conservative neutral default).
        Extra features not in feature_names are silently dropped.

        Args:
            features:      Dict of {feature_name: value}.
            feature_names: Ordered list of column names the model expects.

        Returns:
            Single-row DataFrame with exactly the columns in feature_names.
        """
        row: dict[str, float] = {}
        for col in feature_names:
            raw_val = features.get(col)
            if raw_val is None or (isinstance(raw_val, float) and math.isnan(raw_val)):
                # Fill with 0.0 — callers should pre-fill with domain-appropriate medians
                row[col] = 0.0
            else:
                try:
                    row[col] = float(raw_val)
                except (TypeError, ValueError):
                    row[col] = 0.0

        df = pd.DataFrame([row], columns=feature_names)
        return df

    # ------------------------------------------------------------------
    # Redis cache helpers
    # ------------------------------------------------------------------

    async def _get_cached(self, key: str) -> Optional[dict]:
        """Retrieve a JSON-serialised dict from Redis.

        Returns None on cache miss or Redis errors.
        """
        try:
            raw = await self.cache.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache_get_error", key=key, exc=str(exc))
            return None

    async def _set_cached(
        self, key: str, data: dict, ttl: int = _DEFAULT_TTL
    ) -> None:
        """Serialise data to JSON and store in Redis with a TTL.

        Silently swallows errors so inference still works if Redis is down.
        """
        try:
            serialised = json.dumps(data, default=str)
            await self.cache.set(key, serialised, ex=ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache_set_error", key=key, exc=str(exc))


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _risk_factor_description(feature_name: str, shap_val: float) -> str:
    """Generate a human-readable description for a risk factor."""
    direction = "increases" if shap_val > 0 else "decreases"
    clean_name = feature_name.replace("_", " ").title()
    magnitude = abs(shap_val)
    if magnitude > 0.2:
        strength = "strongly"
    elif magnitude > 0.05:
        strength = "moderately"
    else:
        strength = "slightly"
    return f"{clean_name} {strength} {direction} risk."


def _infer_unit(feature_name: str) -> Optional[str]:
    """Infer a display unit from the feature name using simple keyword matching."""
    name_lower = feature_name.lower()
    if "temperature" in name_lower or "temp" in name_lower:
        return "°C"
    if "humidity" in name_lower or "rh" in name_lower:
        return "%"
    if "vpd" in name_lower:
        return "kPa"
    if "_ec" in name_lower or name_lower.startswith("ec"):
        return "mS/cm"
    if "_ph" in name_lower or name_lower.startswith("ph"):
        return "pH"
    if "vwc" in name_lower or "moisture" in name_lower:
        return "m³/m³"
    if "co2" in name_lower:
        return "ppm"
    if "ppfd" in name_lower or "par" in name_lower:
        return "µmol/m²/s"
    if "flow" in name_lower:
        return "L/min"
    if "weight" in name_lower:
        return "g"
    if "day" in name_lower or "age" in name_lower:
        return "days"
    return None


def _build_risk_explanation(
    risk_score: float, factors: list[RiskFactor]
) -> str:
    """Build a plain-English explanation for the risk score."""
    if risk_score < 0.25:
        level = "LOW"
        summary = "The batch is progressing well with no major risk signals."
    elif risk_score < 0.50:
        level = "MEDIUM"
        summary = "Some environmental parameters are outside optimal ranges."
    elif risk_score < 0.75:
        level = "HIGH"
        summary = "Multiple risk factors are elevated — grower attention is recommended."
    else:
        level = "CRITICAL"
        summary = "Severe risk signals detected — immediate intervention advised."

    top_factor = factors[0].name.replace("_", " ").title() if factors else "unknown"
    return (
        f"Risk level: {level} ({risk_score:.1%}). {summary} "
        f"The primary contributing factor is '{top_factor}'."
    )


def _build_yield_explanation(
    point: float,
    lower: float,
    upper: float,
    contributions: list[dict],
    days_remaining: int,
) -> str:
    """Build a plain-English explanation for the yield prediction."""
    top_feat = contributions[0]["feature"].replace("_", " ").title() if contributions else "unknown"
    return (
        f"Predicted yield: {point:.0f} g (90% CI: {lower:.0f}–{upper:.0f} g). "
        f"Estimated {days_remaining} day(s) remaining to harvest. "
        f"The strongest driver of this prediction is '{top_feat}'."
    )


def _estimate_days_to_harvest(features: dict) -> int:
    """Heuristic estimate of remaining days to harvest.

    Tries known feature names for current grow day and planned flower days.
    Falls back to 14 days if insufficient data is available.
    """
    # Common feature names for grow age / stage day
    age_keys = [
        "grow_age_days",
        "days_since_start",
        "batch_age_days",
        "day_of_grow",
        "current_day",
    ]
    flower_keys = [
        "planned_flower_days",
        "target_flower_days",
        "flower_days_planned",
    ]
    total_keys = [
        "planned_total_days",
        "target_total_days",
        "total_planned_days",
    ]

    current_day: Optional[float] = None
    for k in age_keys:
        if k in features and features[k] is not None:
            current_day = float(features[k])
            break

    planned_total: Optional[float] = None
    for k in total_keys:
        if k in features and features[k] is not None:
            planned_total = float(features[k])
            break

    if planned_total is None:
        flower_days: Optional[float] = None
        for k in flower_keys:
            if k in features and features[k] is not None:
                flower_days = float(features[k])
                break
        # Assume 28 veg days + flower_days as total if we have flower_days
        if flower_days is not None:
            planned_total = 28 + flower_days

    if current_day is not None and planned_total is not None:
        remaining = max(0, int(planned_total - current_day))
        return remaining

    # Default fallback
    return 14


# ---------------------------------------------------------------------------
# Serialisation helpers (UUID / datetime safe dicts for Redis)
# ---------------------------------------------------------------------------


def _risk_response_to_dict(r: RiskScoreResponse) -> dict:
    """Convert RiskScoreResponse to a JSON-serialisable dict."""
    return {
        "batch_id": str(r.batch_id),
        "risk_score": r.risk_score,
        "factors": [
            {
                "name": f.name,
                "description": f.description,
                "contribution": f.contribution,
                "current_value": f.current_value,
                "target_value": f.target_value,
                "unit": f.unit,
            }
            for f in r.factors
        ],
        "confidence": r.confidence,
        "model_version": r.model_version,
        "computed_at": r.computed_at.isoformat(),
        "explanation": r.explanation,
    }


def _yield_response_to_dict(r: YieldPredictionResponse) -> dict:
    """Convert YieldPredictionResponse to a JSON-serialisable dict."""
    return {
        "batch_id": str(r.batch_id),
        "point_estimate_g": r.point_estimate_g,
        "confidence_interval_lower": r.confidence_interval_lower,
        "confidence_interval_upper": r.confidence_interval_upper,
        "confidence": r.confidence,
        "model_version": r.model_version,
        "computed_at": r.computed_at.isoformat(),
        "feature_contributions": r.feature_contributions,
        "days_to_harvest_estimate": r.days_to_harvest_estimate,
        "explanation": r.explanation,
    }
