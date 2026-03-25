"""
Data quality and model performance monitoring.

DataQualityMonitor  — per-sensor and batch-level data quality reports.
ModelDriftMonitor   — Population Stability Index (PSI) based feature drift detection.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.database import SensorReading

log = structlog.get_logger(__name__)

# Sensor type → expected readings per hour (approximate polling interval)
_EXPECTED_READINGS_PER_HOUR: dict[str, int] = {
    "TEMPERATURE": 12,     # every 5 minutes
    "HUMIDITY": 12,
    "EC": 4,               # every 15 minutes
    "PH": 4,
    "VWC": 4,
    "CO2": 12,
    "PPFD": 12,
    "FLOW_RATE": 4,
    "VPD_CALCULATED": 12,
    "DISSOLVED_OXYGEN": 4,
    "WEIGHT": 1,
}

# Physical plausibility bounds per sensor type (min, max)
_SENSOR_BOUNDS: dict[str, tuple[float, float]] = {
    "TEMPERATURE": (-10.0, 50.0),
    "HUMIDITY": (0.0, 100.0),
    "EC": (0.0, 10.0),
    "PH": (0.0, 14.0),
    "VWC": (0.0, 100.0),
    "CO2": (0.0, 5000.0),
    "PPFD": (0.0, 3000.0),
    "FLOW_RATE": (0.0, 100.0),
    "DISSOLVED_OXYGEN": (0.0, 20.0),
}

# PSI interpretation thresholds
PSI_NEGLIGIBLE = 0.1
PSI_MODERATE = 0.2
PSI_SIGNIFICANT = 0.25


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DataQualityReport:
    """Summarises data quality for a single sensor over a time window."""

    batch_id: UUID
    sensor_id: str
    sensor_type: str
    period_start: datetime
    period_end: datetime
    total_expected_readings: int          # based on sensor frequency and window
    actual_readings: int                  # readings actually present in DB
    completeness_pct: float               # actual / expected * 100
    spike_count: int                      # readings flagged as suspect spikes
    flatline_count: int                   # consecutive identical readings
    out_of_range_count: int               # readings outside physical bounds
    gap_count: int                        # gaps larger than 2× expected interval
    mean_gap_duration_min: float          # average gap length in minutes
    quality_score: float                  # composite 0–1 (1 = perfect quality)

    def __post_init__(self) -> None:
        # Clamp quality_score to [0, 1]
        self.quality_score = max(0.0, min(1.0, self.quality_score))


@dataclass
class ModelPerformanceReport:
    """Tracks ML model prediction quality over a time period."""

    model_id: str
    prediction_type: str
    period: str                            # e.g. "2025-01-15/24h"
    predictions_made: int
    mean_prediction: float
    std_prediction: float
    vs_actuals_mae: Optional[float] = None      # Mean Absolute Error vs actuals
    coverage_pct: Optional[float] = None        # Prediction interval coverage
    psi_score: Optional[float] = None           # Feature distribution shift score

    @property
    def drift_flag(self) -> str:
        """Human-readable PSI interpretation."""
        if self.psi_score is None:
            return "unknown"
        if self.psi_score < PSI_NEGLIGIBLE:
            return "negligible"
        if self.psi_score < PSI_MODERATE:
            return "moderate"
        return "significant"


# ---------------------------------------------------------------------------
# DataQualityMonitor
# ---------------------------------------------------------------------------


class DataQualityMonitor:
    """Computes data quality metrics for sensor readings stored in the database.

    Args:
        db: Open async SQLAlchemy session.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._log = log.bind(component="DataQualityMonitor")

    async def compute_sensor_report(
        self,
        sensor_id: str,
        batch_id: UUID,
        window_hours: int = 24,
    ) -> DataQualityReport:
        """Generate a full data quality report for one sensor over *window_hours*.

        Queries the sensor_readings table, then computes:
        - Completeness (actual vs expected readings)
        - Spike detection (value > mean ± 4σ)
        - Flatline detection (≥ 5 consecutive identical values)
        - Out-of-range detection (outside physical bounds)
        - Gap detection (gaps > 2× expected interval)
        - Composite quality score

        Args:
            sensor_id: The sensor's entity ID string.
            batch_id: Grow batch UUID.
            window_hours: Analysis window in hours (default 24).

        Returns:
            DataQualityReport instance with all metrics populated.
        """
        period_end = datetime.now(timezone.utc)
        period_start = period_end - timedelta(hours=window_hours)

        # Query readings within window
        result = await self.db.execute(
            select(SensorReading)
            .where(
                and_(
                    SensorReading.sensor_id == sensor_id,
                    SensorReading.batch_id == batch_id,
                    SensorReading.time >= period_start,
                    SensorReading.time <= period_end,
                )
            )
            .order_by(SensorReading.time)
        )
        rows = list(result.scalars().all())
        actual_readings = len(rows)

        # Determine expected readings from sensor type
        sensor_type = rows[0].sensor_type if rows else "UNKNOWN"
        readings_per_hour = _EXPECTED_READINGS_PER_HOUR.get(sensor_type, 12)
        total_expected = readings_per_hour * window_hours

        if actual_readings == 0:
            return DataQualityReport(
                batch_id=batch_id,
                sensor_id=sensor_id,
                sensor_type=sensor_type,
                period_start=period_start,
                period_end=period_end,
                total_expected_readings=total_expected,
                actual_readings=0,
                completeness_pct=0.0,
                spike_count=0,
                flatline_count=0,
                out_of_range_count=0,
                gap_count=0,
                mean_gap_duration_min=float(window_hours * 60),
                quality_score=0.0,
            )

        completeness_pct = min(100.0, (actual_readings / total_expected) * 100.0)

        # Build a pandas Series for vector operations
        values = pd.Series([r.value for r in rows], dtype=float)
        timestamps = pd.Series([r.time for r in rows])

        # Spike detection: value outside mean ± 4σ
        mean_val = float(values.mean())
        std_val = float(values.std()) if len(values) > 1 else 0.0
        if std_val > 0:
            spike_mask = (values < mean_val - 4 * std_val) | (values > mean_val + 4 * std_val)
        else:
            spike_mask = pd.Series([False] * len(values))
        spike_count = int(spike_mask.sum())

        # Flatline detection: runs of ≥ 5 consecutive identical values
        flatline_count = 0
        if len(values) >= 5:
            run_len = 1
            for i in range(1, len(values)):
                if values.iloc[i] == values.iloc[i - 1]:
                    run_len += 1
                    if run_len == 5:
                        flatline_count += 1
                else:
                    run_len = 1

        # Out-of-range detection
        bounds = _SENSOR_BOUNDS.get(sensor_type)
        if bounds:
            lo, hi = bounds
            out_of_range_count = int(((values < lo) | (values > hi)).sum())
        else:
            out_of_range_count = 0

        # Gap detection
        expected_interval_min = 60.0 / readings_per_hour
        gap_threshold_min = expected_interval_min * 2.0
        gap_count = 0
        gap_durations: list[float] = []

        for i in range(1, len(timestamps)):
            try:
                t_prev = timestamps.iloc[i - 1]
                t_curr = timestamps.iloc[i]
                # Handle timezone-aware vs naive datetimes
                if hasattr(t_prev, "tzinfo") and t_prev.tzinfo is not None:
                    delta_min = (t_curr - t_prev).total_seconds() / 60.0
                else:
                    delta_min = (t_curr - t_prev).total_seconds() / 60.0
                if delta_min > gap_threshold_min:
                    gap_count += 1
                    gap_durations.append(delta_min)
            except Exception:
                pass

        mean_gap_duration_min = float(np.mean(gap_durations)) if gap_durations else 0.0

        # Composite quality score (0–1)
        # Components (each weighted):
        #   - Completeness:      40%
        #   - Spike-free rate:   20%
        #   - Flatline penalty:  15%
        #   - In-range rate:     15%
        #   - Gap penalty:       10%
        completeness_score = completeness_pct / 100.0
        spike_free_score = 1.0 - (spike_count / actual_readings) if actual_readings > 0 else 0.0
        flatline_penalty = min(1.0, flatline_count / max(1, actual_readings / 5))
        flatline_score = 1.0 - flatline_penalty
        in_range_score = 1.0 - (out_of_range_count / actual_readings) if actual_readings > 0 else 0.0
        gap_penalty = min(1.0, gap_count / max(1, total_expected / 12))
        gap_score = 1.0 - gap_penalty

        quality_score = (
            0.40 * completeness_score
            + 0.20 * spike_free_score
            + 0.15 * flatline_score
            + 0.15 * in_range_score
            + 0.10 * gap_score
        )

        self._log.info(
            "sensor_quality_report_computed",
            sensor_id=sensor_id,
            batch_id=str(batch_id),
            completeness_pct=round(completeness_pct, 1),
            quality_score=round(quality_score, 3),
        )

        return DataQualityReport(
            batch_id=batch_id,
            sensor_id=sensor_id,
            sensor_type=sensor_type,
            period_start=period_start,
            period_end=period_end,
            total_expected_readings=total_expected,
            actual_readings=actual_readings,
            completeness_pct=round(completeness_pct, 2),
            spike_count=spike_count,
            flatline_count=flatline_count,
            out_of_range_count=out_of_range_count,
            gap_count=gap_count,
            mean_gap_duration_min=round(mean_gap_duration_min, 1),
            quality_score=round(quality_score, 4),
        )

    async def detect_sensor_offline(
        self,
        entity_id: str,
        expected_interval_min: int = 5,
        timeout_min: int = 30,
    ) -> bool:
        """Return True if the sensor has not reported within *timeout_min* minutes.

        Args:
            entity_id: The HA entity ID / sensor_id.
            expected_interval_min: Normal reporting interval in minutes.
            timeout_min: Minutes of silence before declaring offline.

        Returns:
            True if offline (last reading older than timeout_min minutes).
        """
        result = await self.db.execute(
            select(SensorReading.time)
            .where(SensorReading.sensor_id == entity_id)
            .order_by(SensorReading.time.desc())
            .limit(1)
        )
        last_time = result.scalar_one_or_none()

        if last_time is None:
            self._log.warning("sensor_offline_no_readings", entity_id=entity_id)
            return True

        # Normalise to UTC-aware
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        silence_min = (now - last_time).total_seconds() / 60.0
        offline = silence_min > timeout_min

        if offline:
            self._log.warning(
                "sensor_offline_detected",
                entity_id=entity_id,
                silence_minutes=round(silence_min, 1),
                timeout_min=timeout_min,
            )

        return offline

    async def compute_batch_quality_summary(self, batch_id: UUID) -> dict[str, Any]:
        """Compute aggregated quality metrics across all sensors in a batch.

        Queries distinct sensor_id values for the batch, computes a report for
        each over the last 24 hours, then aggregates into an overall health score.

        Returns:
            Dict with ``overall_health_score``, ``sensor_count``,
            ``sensors_below_threshold`` (quality < 0.6),
            and ``per_sensor`` mapping sensor_id → quality_score.
        """
        # Get distinct sensors for this batch
        sensor_result = await self.db.execute(
            select(SensorReading.sensor_id, SensorReading.sensor_type)
            .where(SensorReading.batch_id == batch_id)
            .distinct()
        )
        sensor_pairs = list(sensor_result.all())

        if not sensor_pairs:
            return {
                "batch_id": str(batch_id),
                "overall_health_score": None,
                "sensor_count": 0,
                "sensors_below_threshold": 0,
                "per_sensor": {},
                "computed_at": datetime.now(timezone.utc).isoformat(),
            }

        per_sensor: dict[str, float] = {}
        for (sid, _stype) in sensor_pairs:
            try:
                report = await self.compute_sensor_report(sid, batch_id, window_hours=24)
                per_sensor[sid] = report.quality_score
            except Exception as exc:
                self._log.warning(
                    "sensor_quality_compute_failed",
                    sensor_id=sid,
                    batch_id=str(batch_id),
                    error=str(exc),
                )
                per_sensor[sid] = 0.0

        scores = list(per_sensor.values())
        overall_health_score = float(np.mean(scores)) if scores else 0.0
        sensors_below_threshold = sum(1 for s in scores if s < 0.6)

        return {
            "batch_id": str(batch_id),
            "overall_health_score": round(overall_health_score, 4),
            "sensor_count": len(scores),
            "sensors_below_threshold": sensors_below_threshold,
            "per_sensor": {sid: round(score, 4) for sid, score in per_sensor.items()},
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# ModelDriftMonitor
# ---------------------------------------------------------------------------


class ModelDriftMonitor:
    """Monitors for feature distribution drift using Population Stability Index (PSI).

    PSI thresholds (standard industry interpretation):
        PSI < 0.10  — negligible shift, model likely still valid
        PSI 0.10–0.20 — moderate shift, investigate
        PSI > 0.20  — significant shift, model retraining recommended

    Args:
        registry: Model registry object (used to retrieve training metadata).
                  Typing is kept generic to avoid circular imports.
    """

    def __init__(self, registry: Any) -> None:
        self.registry = registry
        self._log = log.bind(component="ModelDriftMonitor")

    def compute_psi(
        self,
        reference: pd.Series,
        current: pd.Series,
        n_bins: int = 10,
    ) -> float:
        """Compute the Population Stability Index between two distributions.

        PSI = Σ (actual_pct - expected_pct) × ln(actual_pct / expected_pct)

        Where:
            expected_pct = fraction of reference distribution in each bin
            actual_pct   = fraction of current distribution in each bin

        Edge cases:
            - Bins with zero reference count are assigned a small epsilon to
              avoid log(0).
            - Bins with zero current count are also assigned epsilon.
            - Empty series returns 0.0.

        Args:
            reference: Training/baseline distribution series.
            current: Current (scoring) distribution series.
            n_bins: Number of equal-width histogram bins.

        Returns:
            PSI score as a float (0 = identical distribution, > 0.2 = significant drift).
        """
        reference = reference.dropna()
        current = current.dropna()

        if len(reference) == 0 or len(current) == 0:
            self._log.warning("psi_empty_series", reference_len=len(reference), current_len=len(current))
            return 0.0

        # Build bins on the reference distribution's range
        ref_min = float(reference.min())
        ref_max = float(reference.max())

        if ref_min == ref_max:
            # No variation — PSI is undefined; treat as zero drift
            return 0.0

        # Bin edges covering the full reference range plus a tiny margin
        eps_edge = (ref_max - ref_min) * 0.001
        bin_edges = np.linspace(ref_min - eps_edge, ref_max + eps_edge, n_bins + 1)

        ref_counts, _ = np.histogram(reference.values, bins=bin_edges)
        cur_counts, _ = np.histogram(current.values, bins=bin_edges)

        epsilon = 1e-6  # small value to avoid division by zero

        ref_pct = np.where(ref_counts > 0, ref_counts / len(reference), epsilon)
        cur_pct = np.where(cur_counts > 0, cur_counts / len(current), epsilon)

        # Ensure no zeros remain after the where clause
        ref_pct = np.maximum(ref_pct, epsilon)
        cur_pct = np.maximum(cur_pct, epsilon)

        psi_bins = (cur_pct - ref_pct) * np.log(cur_pct / ref_pct)
        psi = float(np.sum(psi_bins))

        return round(psi, 6)

    async def check_prediction_drift(
        self,
        model_id: str,
        recent_predictions: pd.DataFrame,
        training_features: pd.DataFrame,
    ) -> dict[str, Any]:
        """Compute per-feature PSI between training and current prediction features.

        Checks the top-5 numeric features by variance in the training set.
        Returns a dict with feature-level PSI scores and an alert flag.

        Args:
            model_id: Registry key for the model.
            recent_predictions: DataFrame of recent scoring-time feature rows.
            training_features: DataFrame of training-time feature rows.

        Returns:
            Dict with:
                - ``feature_psi``: {feature_name: psi_score}
                - ``alert``: True if any feature PSI > 0.2
                - ``max_psi_feature``: name of feature with highest PSI
                - ``max_psi_score``: its PSI score
                - ``interpretation``: human-readable summary
        """
        # Select top-5 features by variance in training set
        numeric_cols = training_features.select_dtypes(include=[np.number]).columns.tolist()
        if not numeric_cols:
            return {"feature_psi": {}, "alert": False, "interpretation": "No numeric features found."}

        variances = training_features[numeric_cols].var().sort_values(ascending=False)
        top_features = variances.head(5).index.tolist()

        feature_psi: dict[str, float] = {}
        for feat in top_features:
            if feat not in recent_predictions.columns:
                continue
            ref_series = training_features[feat].dropna()
            cur_series = recent_predictions[feat].dropna()
            psi_val = self.compute_psi(ref_series, cur_series)
            feature_psi[feat] = psi_val
            self._log.debug("feature_psi_computed", model_id=model_id, feature=feat, psi=psi_val)

        alert = any(v > PSI_MODERATE for v in feature_psi.values())

        if feature_psi:
            max_feat = max(feature_psi, key=lambda k: feature_psi[k])
            max_psi = feature_psi[max_feat]
        else:
            max_feat = ""
            max_psi = 0.0

        # Interpretation
        if max_psi < PSI_NEGLIGIBLE:
            interpretation = "No significant feature drift detected. Model predictions are stable."
        elif max_psi < PSI_MODERATE:
            interpretation = (
                f"Moderate drift detected in feature '{max_feat}' (PSI={max_psi:.3f}). "
                "Monitor closely; consider incremental retraining."
            )
        else:
            interpretation = (
                f"Significant drift detected in feature '{max_feat}' (PSI={max_psi:.3f}). "
                "Model retraining is recommended. Current predictions may be unreliable."
            )

        if alert:
            self._log.warning(
                "model_drift_alert",
                model_id=model_id,
                max_psi_feature=max_feat,
                max_psi_score=max_psi,
            )

        return {
            "model_id": model_id,
            "feature_psi": feature_psi,
            "alert": alert,
            "max_psi_feature": max_feat,
            "max_psi_score": round(max_psi, 6),
            "interpretation": interpretation,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

    def compute_feature_psi_batch(
        self,
        training_df: pd.DataFrame,
        current_df: pd.DataFrame,
        n_bins: int = 10,
    ) -> dict[str, float]:
        """Compute PSI for every numeric feature shared between both DataFrames.

        Args:
            training_df: Reference (training-time) feature DataFrame.
            current_df: Current (scoring-time) feature DataFrame.
            n_bins: Number of bins for the histogram.

        Returns:
            Dict mapping feature_name → PSI score.  Only features present
            in both DataFrames are included.
        """
        training_numeric = set(training_df.select_dtypes(include=[np.number]).columns)
        current_numeric = set(current_df.select_dtypes(include=[np.number]).columns)
        shared_features = training_numeric & current_numeric

        psi_results: dict[str, float] = {}
        for feat in sorted(shared_features):
            ref = training_df[feat].dropna()
            cur = current_df[feat].dropna()
            psi_val = self.compute_psi(ref, cur, n_bins=n_bins)
            psi_results[feat] = psi_val

        self._log.info(
            "batch_psi_computed",
            feature_count=len(psi_results),
            alerted_features=[f for f, v in psi_results.items() if v > PSI_MODERATE],
        )
        return psi_results
