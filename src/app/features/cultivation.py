"""
Domain-specific feature transforms and stage-aware target ranges
for the cultivation intelligence platform.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd
import structlog

from app.schemas.batch import GrowStage

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Stage-aware targets
# ---------------------------------------------------------------------------

class StageAwareTransformer:
    """
    Provides stage-specific target ranges for key cultivation parameters.

    Target ranges vary significantly across the grow cycle: VPD climbs as
    plants mature to drive transpiration; EC increases through vegetative and
    flower stages; DLI requirements are highest during mid-flower.
    """

    # VPD targets (kPa): (min, max)
    STAGE_VPD_TARGETS: dict[GrowStage, tuple[float, float]] = {
        GrowStage.PROPAGATION:   (0.4, 0.8),
        GrowStage.VEG:           (0.8, 1.2),
        GrowStage.EARLY_FLOWER:  (1.0, 1.4),
        GrowStage.MID_FLOWER:    (1.2, 1.6),
        GrowStage.LATE_FLOWER:   (1.4, 1.8),
        GrowStage.FLUSH:         (0.8, 1.2),
        GrowStage.HARVEST:       (0.8, 1.2),
        GrowStage.COMPLETE:      (0.8, 1.2),
    }

    # EC targets (mS/cm): (min, max)
    STAGE_EC_TARGETS: dict[GrowStage, tuple[float, float]] = {
        GrowStage.PROPAGATION:   (0.5, 1.0),
        GrowStage.VEG:           (1.4, 2.0),
        GrowStage.EARLY_FLOWER:  (1.8, 2.4),
        GrowStage.MID_FLOWER:    (2.0, 2.6),
        GrowStage.LATE_FLOWER:   (1.8, 2.4),
        GrowStage.FLUSH:         (0.0, 0.5),   # Plain water flush
        GrowStage.HARVEST:       (0.0, 0.5),
        GrowStage.COMPLETE:      (0.0, 0.5),
    }

    # DLI targets (mol/m²/day): (min, max)
    STAGE_DLI_TARGETS: dict[GrowStage, tuple[float, float]] = {
        GrowStage.PROPAGATION:   (10.0, 20.0),
        GrowStage.VEG:           (25.0, 35.0),
        GrowStage.EARLY_FLOWER:  (35.0, 45.0),
        GrowStage.MID_FLOWER:    (38.0, 50.0),
        GrowStage.LATE_FLOWER:   (35.0, 45.0),
        GrowStage.FLUSH:         (20.0, 35.0),
        GrowStage.HARVEST:       (10.0, 20.0),
        GrowStage.COMPLETE:      (0.0, 0.0),
    }

    # pH targets: same across all stages for most cannabis grows
    STAGE_PH_TARGETS: dict[GrowStage, tuple[float, float]] = {
        GrowStage.PROPAGATION:   (5.5, 6.0),
        GrowStage.VEG:           (5.8, 6.2),
        GrowStage.EARLY_FLOWER:  (5.8, 6.2),
        GrowStage.MID_FLOWER:    (5.8, 6.2),
        GrowStage.LATE_FLOWER:   (5.8, 6.3),
        GrowStage.FLUSH:         (6.0, 6.5),
        GrowStage.HARVEST:       (6.0, 6.5),
        GrowStage.COMPLETE:      (6.0, 6.5),
    }

    def get_target_ranges(self, stage: GrowStage) -> dict:
        """
        Return the full set of target parameter ranges for the given grow stage.

        Returns:
            dict with keys: "vpd", "ec", "dli", "ph" — each a (min, max) tuple.

        Example:
            >>> t = StageAwareTransformer()
            >>> t.get_target_ranges(GrowStage.MID_FLOWER)
            {"vpd": (1.2, 1.6), "ec": (2.0, 2.6), "dli": (38.0, 50.0), "ph": (5.8, 6.2)}
        """
        return {
            "vpd": self.STAGE_VPD_TARGETS.get(stage, (0.8, 1.2)),
            "ec": self.STAGE_EC_TARGETS.get(stage, (1.8, 2.4)),
            "dli": self.STAGE_DLI_TARGETS.get(stage, (35.0, 45.0)),
            "ph": self.STAGE_PH_TARGETS.get(stage, (5.8, 6.2)),
        }


# ---------------------------------------------------------------------------
# Standalone domain feature functions
# ---------------------------------------------------------------------------

def compute_canopy_temp_delta(air_temp: float, canopy_temp: float) -> float:
    """
    Compute the delta between air temperature and canopy temperature.

    A positive delta (air > canopy) indicates the canopy is cooler than the
    surrounding air, which is unusual and may indicate evapotranspiration
    stress or sensor placement issues. A negative delta is typical under HID/LED.

    Args:
        air_temp: Ambient air temperature in °C.
        canopy_temp: Canopy surface temperature in °C (e.g. from IR sensor).

    Returns:
        delta = air_temp - canopy_temp (°C). Raises ValueError if outside
        plausible range (>20 °C difference).
    """
    if not (-10.0 <= air_temp <= 60.0):
        raise ValueError(f"air_temp {air_temp} °C outside plausible range.")
    if not (-10.0 <= canopy_temp <= 60.0):
        raise ValueError(f"canopy_temp {canopy_temp} °C outside plausible range.")

    delta = air_temp - canopy_temp
    if abs(delta) > 20.0:
        logger.warning(
            "canopy_temp_delta_unusually_large",
            air_temp=air_temp,
            canopy_temp=canopy_temp,
            delta=delta,
        )
    return round(delta, 3)


def compute_ph_swing(ph_series: pd.Series, window_hours: int = 24) -> float:
    """
    Compute the pH swing (instability indicator) over a rolling window.

    pH swing = max(pH) - min(pH) within the window. Values > 0.5 suggest
    nutrient solution or dosing instability.

    Args:
        ph_series: DatetimeIndex Series of pH readings.
        window_hours: Window size in hours for the rolling calculation.

    Returns:
        Maximum pH swing (float). Returns 0.0 if fewer than 2 readings.
    """
    clean = ph_series.dropna()
    if len(clean) < 2:
        logger.warning("compute_ph_swing: insufficient data", n=len(clean))
        return 0.0

    if not isinstance(clean.index, pd.DatetimeIndex):
        # No time index: compute swing over the full series
        return float(clean.max() - clean.min())

    window = f"{window_hours}h"
    rolling_max = clean.rolling(window, min_periods=2).max()
    rolling_min = clean.rolling(window, min_periods=2).min()
    swing_series = rolling_max - rolling_min

    max_swing = float(swing_series.max())
    return round(max_swing, 4) if not math.isnan(max_swing) else 0.0


def compute_irrigation_frequency(
    irrigation_events: pd.DataFrame,
    window_hours: int = 24,
) -> float:
    """
    Compute the number of irrigation shots per day within a rolling window.

    Args:
        irrigation_events: DataFrame with a DatetimeIndex (or a 'created_at'
                           column) where each row represents one irrigation event.
        window_hours: Look-back window in hours.

    Returns:
        Shots per day (float). Returns 0.0 if no events in window.
    """
    if irrigation_events.empty:
        return 0.0

    # Resolve DatetimeIndex
    if not isinstance(irrigation_events.index, pd.DatetimeIndex):
        if "created_at" in irrigation_events.columns:
            irrigation_events = irrigation_events.set_index(
                pd.to_datetime(irrigation_events["created_at"])
            )
        else:
            raise ValueError(
                "irrigation_events must have a DatetimeIndex or a 'created_at' column."
            )

    cutoff = irrigation_events.index.max() - pd.Timedelta(hours=window_hours)
    in_window = irrigation_events[irrigation_events.index >= cutoff]
    n_shots = len(in_window)

    # Normalise to shots per 24 h
    shots_per_day = n_shots * (24.0 / window_hours)
    return round(shots_per_day, 3)


def compute_nutrient_uptake_indicator(
    ec_pre: float,
    ec_post: float,
    volume_in: float,
    volume_out: float,
) -> Optional[float]:
    """
    Compute a simplified nutrient uptake ratio from irrigation and leachate data.

    Uptake indicator = (EC_in * Vol_in - EC_out * Vol_out) / (EC_in * Vol_in)

    A value near 1.0 means nearly all nutrients were absorbed (high uptake).
    A value near 0.0 means leachate carries similar nutrient load to input.
    Negative values indicate salt accumulation (leachate EC > input load).

    Args:
        ec_pre: EC of irrigation solution (mS/cm).
        ec_post: EC of leachate / run-off (mS/cm).
        volume_in: Volume of irrigation applied (mL).
        volume_out: Volume of leachate collected (mL).

    Returns:
        Uptake indicator float in (-∞, 1.0], or None if inputs are invalid
        (e.g. zero volume_in).
    """
    if volume_in <= 0.0:
        logger.warning(
            "compute_nutrient_uptake_indicator: volume_in must be > 0",
            volume_in=volume_in,
        )
        return None
    if ec_pre < 0.0 or ec_post < 0.0:
        logger.warning(
            "compute_nutrient_uptake_indicator: negative EC value",
            ec_pre=ec_pre,
            ec_post=ec_post,
        )
        return None

    nutrient_in = ec_pre * volume_in
    nutrient_out = ec_post * volume_out

    if nutrient_in == 0.0:
        # Flushing with plain water — undefined ratio
        return None

    uptake = (nutrient_in - nutrient_out) / nutrient_in
    return round(uptake, 4)


def compute_feature_quality_flags(
    features: dict,
    stage: Optional[GrowStage] = None,
) -> dict[str, str]:
    """
    Assign quality flags to each feature in a feature dict.

    Quality levels:
        "ok"            — value present and within expected range for stage
        "missing"       — value is None
        "suspect_range" — value is present but outside plausible physical bounds
        "suspect_quality" — value is present but outside stage-specific target range

    Args:
        features: dict[str, Optional[float]] of feature_name -> value.
        stage: Optional GrowStage used to look up stage-specific targets.

    Returns:
        dict[str, str] with the same keys as `features`, each mapped to a
        quality flag string.
    """
    # Physical plausibility bounds (sensor-agnostic hard limits)
    _physical_bounds: dict[str, tuple[float, float]] = {
        "temperature_mean": (-10.0, 50.0),
        "humidity_mean": (0.0, 100.0),
        "ec_mean": (0.0, 10.0),
        "ph_mean": (0.0, 14.0),
        "vwc_mean": (0.0, 100.0),
        "co2_mean": (0.0, 5000.0),
        "ppfd_mean": (0.0, 3000.0),
        "vpd_mean": (0.0, 10.0),
        "dli_today": (0.0, 80.0),
        "vwc_dryback_pct": (0.0, 100.0),
        "stage_progress_normalized": (0.0, 1.0),
        "vpd_pct_in_range": (0.0, 100.0),
    }

    # Stage-specific advisory bounds — use StageAwareTransformer if stage known
    transformer = StageAwareTransformer()
    stage_targets: dict = {}
    if stage is not None:
        ranges = transformer.get_target_ranges(stage)
        stage_targets = {
            "vpd_mean": ranges["vpd"],
            "ec_mean": ranges["ec"],
            "dli_today": ranges["dli"],
            "ph_mean": ranges["ph"],
        }

    quality: dict[str, str] = {}
    for key, value in features.items():
        if value is None or (isinstance(value, float) and math.isnan(value)):
            quality[key] = "missing"
            continue

        # Check physical plausibility
        phys = _physical_bounds.get(key)
        if phys is not None:
            lo, hi = phys
            if not (lo <= value <= hi):
                quality[key] = "suspect_range"
                continue

        # Check stage-specific target range (advisory, softer flag)
        stage_range = stage_targets.get(key)
        if stage_range is not None:
            lo, hi = stage_range
            if not (lo <= value <= hi):
                quality[key] = "suspect_quality"
                continue

        quality[key] = "ok"

    return quality


# ---------------------------------------------------------------------------
# EC temperature correction
# ---------------------------------------------------------------------------

class RootzoneTempCorrection:
    """
    Corrects EC readings for rootzone temperature deviation from a reference.

    EC measurements are temperature-dependent: warmer solutions have lower
    resistivity (higher apparent EC). The standard correction factor for
    nutrient solutions is approximately 2% per °C.

    Reference: METER Group application note on EC temperature correction.
    """

    # Standard temperature coefficient for nutrient solutions: 1.9–2.0% per °C
    _TEMPERATURE_COEFFICIENT: float = 0.019  # per °C

    def correct_ec(
        self,
        ec: float,
        rootzone_temp: float,
        reference_temp: float = 25.0,
    ) -> float:
        """
        Correct a raw EC reading to the standard reference temperature.

        Formula:
            EC_corrected = EC_raw / (1 + α * (T_rootzone - T_reference))

        where α = 0.019 (1.9 % / °C).

        Args:
            ec: Raw EC measurement (mS/cm) at rootzone_temp.
            rootzone_temp: Actual rootzone temperature (°C).
            reference_temp: Reference temperature to normalise to (default 25 °C).

        Returns:
            Temperature-corrected EC (mS/cm).

        Raises:
            ValueError: If ec < 0, or if temperatures are outside [-5, 60] °C.
        """
        if ec < 0.0:
            raise ValueError(f"EC must be non-negative, got {ec}.")
        if not (-5.0 <= rootzone_temp <= 60.0):
            raise ValueError(
                f"rootzone_temp {rootzone_temp} °C is outside plausible range [-5, 60]."
            )
        if not (-5.0 <= reference_temp <= 60.0):
            raise ValueError(
                f"reference_temp {reference_temp} °C is outside plausible range [-5, 60]."
            )

        correction_factor = 1.0 + self._TEMPERATURE_COEFFICIENT * (
            rootzone_temp - reference_temp
        )
        if correction_factor <= 0.0:
            raise ValueError(
                f"Correction factor {correction_factor} is non-positive; "
                "check temperature inputs."
            )

        ec_corrected = ec / correction_factor
        return round(ec_corrected, 4)
