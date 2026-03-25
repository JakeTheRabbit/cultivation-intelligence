"""
Feature engineering pipeline for cultivation sensor data.

Transforms raw time-series sensor readings into model-ready features
used by yield prediction, risk scoring, and recommendation engines.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class FeatureConfig:
    """Configuration for feature engineering pipeline."""

    vpd_target_min: float = 0.8
    vpd_target_max: float = 1.2
    ec_target_min: float = 1.8
    ec_target_max: float = 2.4
    ph_target_min: float = 5.8
    ph_target_max: float = 6.2
    dli_target: float = 38.0
    # Rolling window sizes in minutes: 15 min, 1 h, 6 h, 1 day, 2 days
    window_sizes: list = field(
        default_factory=lambda: [15, 60, 360, 1440, 2880]
    )
    min_readings_for_feature: int = 3


class CultivationFeaturePipeline:
    """
    Feature engineering pipeline for cultivation sensor data.

    Converts raw, time-indexed sensor DataFrames into flat feature dicts
    suitable for scikit-learn estimators or direct API response serialization.
    """

    def __init__(self, config: Optional[FeatureConfig] = None) -> None:
        self.config = config or FeatureConfig()
        logger.info(
            "CultivationFeaturePipeline initialized",
            vpd_target_min=self.config.vpd_target_min,
            vpd_target_max=self.config.vpd_target_max,
            window_sizes=self.config.window_sizes,
        )

    # ------------------------------------------------------------------
    # Thermodynamic helpers
    # ------------------------------------------------------------------

    def compute_vpd(self, temperature_c: float, humidity_rh: float) -> float:
        """
        Compute Vapour Pressure Deficit (kPa) using the Magnus formula.

        VPD = (1 - RH/100) * SVP
        SVP = 0.6108 * exp(17.27 * T / (T + 237.3))   [Tetens / Magnus]

        Args:
            temperature_c: Air temperature in degrees Celsius.
            humidity_rh: Relative humidity in percent (0–100).

        Returns:
            VPD in kPa. Always >= 0.
        """
        if not (-10.0 <= temperature_c <= 60.0):
            raise ValueError(
                f"temperature_c {temperature_c} is outside the plausible range [-10, 60]."
            )
        if not (0.0 <= humidity_rh <= 100.0):
            raise ValueError(
                f"humidity_rh {humidity_rh} is outside the valid range [0, 100]."
            )

        svp = 0.6108 * math.exp(17.27 * temperature_c / (temperature_c + 237.3))
        vpd = (1.0 - humidity_rh / 100.0) * svp
        return max(0.0, vpd)

    # ------------------------------------------------------------------
    # Daily Light Integral
    # ------------------------------------------------------------------

    def compute_dli(
        self,
        df_ppfd: pd.DataFrame,
        date: pd.Timestamp,
        photoperiod_hours: float,
    ) -> float:
        """
        Compute Daily Light Integral (DLI) in mol/m²/day.

        Uses the trapezoidal rule to integrate PPFD (µmol/m²/s) over the
        photoperiod and converts µmol to mol.

        Args:
            df_ppfd: DataFrame with a DatetimeIndex and a 'value' column
                     containing PPFD readings in µmol/m²/s.
            date: The calendar date for which to compute DLI.
            photoperiod_hours: Intended photoperiod length in hours (used only
                               to log if actual data coverage differs).

        Returns:
            DLI in mol/m²/day. Returns 0.0 if fewer than 2 data points exist.
        """
        if df_ppfd.empty or "value" not in df_ppfd.columns:
            logger.warning("compute_dli: empty DataFrame or missing 'value' column")
            return 0.0

        # Filter to the target date
        day_start = pd.Timestamp(date).normalize()
        day_end = day_start + pd.Timedelta(days=1)
        mask = (df_ppfd.index >= day_start) & (df_ppfd.index < day_end)
        day_df = df_ppfd.loc[mask].copy()

        if len(day_df) < 2:
            logger.warning(
                "compute_dli: insufficient data points for integration",
                date=str(date),
                n_points=len(day_df),
            )
            return 0.0

        day_df = day_df.sort_index()
        # Convert index to elapsed seconds since first reading
        t_seconds = (day_df.index - day_df.index[0]).total_seconds().to_numpy()
        ppfd_values = day_df["value"].to_numpy(dtype=float)

        # Replace negatives with 0 (sensor noise at night)
        ppfd_values = np.clip(ppfd_values, 0.0, None)

        # Integrate using trapezoid rule: result is in µmol/m²
        integral_umol_per_m2 = float(np.trapz(ppfd_values, t_seconds))

        # Convert µmol → mol
        dli = integral_umol_per_m2 / 1_000_000.0

        actual_coverage_h = t_seconds[-1] / 3600.0
        if abs(actual_coverage_h - photoperiod_hours) > 1.0:
            logger.warning(
                "compute_dli: data coverage differs significantly from photoperiod",
                photoperiod_hours=photoperiod_hours,
                actual_coverage_hours=round(actual_coverage_h, 2),
            )

        return max(0.0, dli)

    # ------------------------------------------------------------------
    # Rolling statistics
    # ------------------------------------------------------------------

    def compute_rolling_stats(
        self,
        df: pd.DataFrame,
        sensor_col: str,
        window_minutes: int,
    ) -> pd.DataFrame:
        """
        Compute rolling mean, std, min, and max for a sensor column.

        Args:
            df: DataFrame with a DatetimeIndex and at least `sensor_col`.
            sensor_col: Name of the column to compute statistics for.
            window_minutes: Rolling window size in minutes.

        Returns:
            DataFrame with columns: `{sensor_col}_mean`, `{sensor_col}_std`,
            `{sensor_col}_min`, `{sensor_col}_max` — same index as input.
        """
        if sensor_col not in df.columns:
            raise KeyError(f"Column '{sensor_col}' not found in DataFrame.")

        window = f"{window_minutes}min"
        series = df[sensor_col].astype(float)

        result = pd.DataFrame(index=df.index)
        result[f"{sensor_col}_mean_{window_minutes}m"] = (
            series.rolling(window, min_periods=1).mean()
        )
        result[f"{sensor_col}_std_{window_minutes}m"] = (
            series.rolling(window, min_periods=2).std()
        )
        result[f"{sensor_col}_min_{window_minutes}m"] = (
            series.rolling(window, min_periods=1).min()
        )
        result[f"{sensor_col}_max_{window_minutes}m"] = (
            series.rolling(window, min_periods=1).max()
        )
        return result

    # ------------------------------------------------------------------
    # Anomaly detection
    # ------------------------------------------------------------------

    def detect_spikes(
        self,
        series: pd.Series,
        window: int = 10,
        threshold: float = 3.0,
    ) -> pd.Series:
        """
        Detect sensor spikes using a rolling z-score.

        A reading is flagged as a spike when |z-score| >= threshold, where
        z = (value - rolling_mean) / rolling_std.

        Args:
            series: 1-D numeric Series of sensor readings.
            window: Number of consecutive readings in the rolling window.
            threshold: Z-score magnitude above which a reading is a spike.

        Returns:
            Boolean Series: True where a spike is detected, same index as input.
        """
        if len(series) < window:
            return pd.Series(False, index=series.index)

        rolling_mean = series.rolling(window, min_periods=max(2, window // 2)).mean()
        rolling_std = series.rolling(window, min_periods=max(2, window // 2)).std()

        # Avoid division by zero: where std is 0 or NaN, z-score is 0
        z_scores = (series - rolling_mean) / rolling_std.replace(0.0, np.nan)
        z_scores = z_scores.fillna(0.0)

        spike_mask = z_scores.abs() >= threshold
        return spike_mask.astype(bool)

    def detect_flatline(
        self,
        series: pd.Series,
        window: int = 20,
        threshold: float = 0.001,
    ) -> pd.Series:
        """
        Detect stuck / frozen sensor readings (flatline).

        A window is considered a flatline when the rolling standard deviation
        of readings falls below `threshold`, indicating the sensor value has
        not changed meaningfully.

        Args:
            series: 1-D numeric Series of sensor readings.
            window: Number of consecutive readings to check for flatline.
            threshold: Maximum allowable std; below this is flagged as flatline.

        Returns:
            Boolean Series: True where a flatline is detected.
        """
        if len(series) < window:
            return pd.Series(False, index=series.index)

        rolling_std = series.rolling(window, min_periods=window // 2).std()
        flatline_mask = rolling_std < threshold
        # First (window-1) entries cannot be assessed — mark as not flatline
        flatline_mask.iloc[: window - 1] = False
        return flatline_mask.fillna(False).astype(bool)

    # ------------------------------------------------------------------
    # VPD stress metrics
    # ------------------------------------------------------------------

    def compute_vpd_exceedance(
        self,
        vpd_series: pd.Series,
        vpd_min: float,
        vpd_max: float,
    ) -> dict:
        """
        Compute how often VPD was outside the target range.

        Assumes each observation represents one minute. Fractions are
        approximated proportionally when the series is not minute-frequency.

        Args:
            vpd_series: Series of VPD values (kPa), indexed by datetime.
            vpd_min: Lower bound of target VPD range.
            vpd_max: Upper bound of target VPD range.

        Returns:
            dict with keys:
                "minutes_above" (int): estimated minutes VPD > vpd_max
                "minutes_below" (int): estimated minutes VPD < vpd_min
                "pct_in_range" (float): percentage of observations in range
        """
        n = len(vpd_series)
        if n == 0:
            return {"minutes_above": 0, "minutes_below": 0, "pct_in_range": 0.0}

        clean = vpd_series.dropna()
        above_mask = clean > vpd_max
        below_mask = clean < vpd_min
        in_range_mask = ~above_mask & ~below_mask

        # Estimate real elapsed minutes from the index if datetime-indexed
        if isinstance(vpd_series.index, pd.DatetimeIndex) and len(clean) >= 2:
            total_minutes = (
                (clean.index[-1] - clean.index[0]).total_seconds() / 60.0
            )
            fraction_above = above_mask.sum() / len(clean)
            fraction_below = below_mask.sum() / len(clean)
            minutes_above = int(round(fraction_above * total_minutes))
            minutes_below = int(round(fraction_below * total_minutes))
        else:
            # Fallback: count observations as proxy for minutes
            minutes_above = int(above_mask.sum())
            minutes_below = int(below_mask.sum())

        pct_in_range = 100.0 * in_range_mask.sum() / len(clean) if len(clean) > 0 else 0.0

        return {
            "minutes_above": minutes_above,
            "minutes_below": minutes_below,
            "pct_in_range": round(pct_in_range, 2),
        }

    # ------------------------------------------------------------------
    # EC drift
    # ------------------------------------------------------------------

    def compute_ec_drift_rate(
        self,
        ec_series: pd.Series,
        window_hours: int = 24,
    ) -> float:
        """
        Compute the linear drift rate of EC over a rolling window.

        Uses numpy's polyfit (degree-1 polynomial) on the most recent
        `window_hours` of data.

        Args:
            ec_series: DatetimeIndex Series of EC readings (mS/cm).
            window_hours: Look-back window in hours.

        Returns:
            Slope in mS/cm per hour. Positive = EC rising, negative = falling.
            Returns 0.0 if insufficient data.
        """
        if not isinstance(ec_series.index, pd.DatetimeIndex):
            logger.warning("compute_ec_drift_rate: index is not DatetimeIndex; returning 0.0")
            return 0.0

        cutoff = ec_series.index.max() - pd.Timedelta(hours=window_hours)
        window_data = ec_series[ec_series.index >= cutoff].dropna()

        if len(window_data) < self.config.min_readings_for_feature:
            logger.warning(
                "compute_ec_drift_rate: insufficient readings in window",
                n=len(window_data),
                required=self.config.min_readings_for_feature,
            )
            return 0.0

        # Convert timestamps to hours since start for polyfit
        t0 = window_data.index[0]
        t_hours = (window_data.index - t0).total_seconds() / 3600.0
        values = window_data.to_numpy(dtype=float)

        coeffs = np.polyfit(t_hours, values, deg=1)
        slope = float(coeffs[0])  # mS/cm per hour
        return round(slope, 6)

    # ------------------------------------------------------------------
    # Substrate dryback
    # ------------------------------------------------------------------

    def compute_substrate_dryback(self, vwc_series: pd.Series) -> dict:
        """
        Compute substrate dryback metrics from a VWC (volumetric water content) series.

        Dryback is the % drop in VWC from the peak (post-irrigation) to
        the trough (just before next irrigation). Used to assess root zone
        management.

        Args:
            vwc_series: DatetimeIndex Series of VWC readings (0–100 %).

        Returns:
            dict with keys:
                "peak_vwc" (float): maximum VWC observed
                "trough_vwc" (float): minimum VWC after the peak
                "dryback_pct" (float): (peak - trough) / peak * 100
                "time_to_dryback_min" (float): minutes from peak to trough
        """
        clean = vwc_series.dropna()
        if len(clean) < 2:
            return {
                "peak_vwc": float("nan"),
                "trough_vwc": float("nan"),
                "dryback_pct": float("nan"),
                "time_to_dryback_min": float("nan"),
            }

        peak_idx = clean.idxmax()
        peak_vwc = float(clean[peak_idx])

        # Trough = minimum value that occurs AFTER the peak
        post_peak = clean[clean.index >= peak_idx]
        trough_idx = post_peak.idxmin()
        trough_vwc = float(post_peak[trough_idx])

        dryback_pct = (
            ((peak_vwc - trough_vwc) / peak_vwc * 100.0) if peak_vwc > 0 else 0.0
        )

        time_to_dryback_min: float
        if isinstance(clean.index, pd.DatetimeIndex):
            time_to_dryback_min = float(
                (trough_idx - peak_idx).total_seconds() / 60.0
            )
        else:
            # Fallback: use integer index difference
            peak_pos = clean.index.get_loc(peak_idx)
            trough_pos = clean.index.get_loc(trough_idx)
            time_to_dryback_min = float(trough_pos - peak_pos)

        return {
            "peak_vwc": round(peak_vwc, 3),
            "trough_vwc": round(trough_vwc, 3),
            "dryback_pct": round(max(0.0, dryback_pct), 3),
            "time_to_dryback_min": round(max(0.0, time_to_dryback_min), 1),
        }

    # ------------------------------------------------------------------
    # Stage normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_stage_day(
        current_day: int,
        stage_start_day: int,
        stage_end_day: int,
    ) -> float:
        """
        Normalise the current day within a grow stage to [0.0, 1.0].

        0.0 = first day of stage, 1.0 = last day of stage.

        Args:
            current_day: Absolute day-of-grow.
            stage_start_day: Absolute day when the stage began.
            stage_end_day: Absolute day when the stage ends (exclusive).

        Returns:
            Float in [0.0, 1.0]. Clamped if current_day is outside the stage.
        """
        duration = stage_end_day - stage_start_day
        if duration <= 0:
            return 0.0
        raw = (current_day - stage_start_day) / duration
        return float(max(0.0, min(1.0, raw)))

    # ------------------------------------------------------------------
    # Master feature builder
    # ------------------------------------------------------------------

    def build_batch_features(
        self,
        sensor_df: pd.DataFrame,
        batch_metadata: dict,
    ) -> dict:
        """
        Orchestrate all feature computations for a single batch.

        Expects `sensor_df` to have:
            - DatetimeIndex
            - columns named after sensor types (e.g. 'TEMPERATURE', 'HUMIDITY',
              'VPD_CALCULATED', 'EC', 'PH', 'VWC', 'PPFD', 'CO2')
        Missing columns result in None values with warning logs rather than
        exceptions, so the pipeline degrades gracefully.

        `batch_metadata` dict expected keys (all optional, None if absent):
            - "stage" (str): current GrowStage value
            - "days_in_stage" (int)
            - "stage_start_day" (int)
            - "stage_end_day" (int)
            - "planned_flower_days" (int)
            - "photoperiod_hours" (float)

        Returns:
            Flat dict of feature_name -> value (float or None).
        """
        features: dict[str, Optional[float]] = {}
        log = logger.bind(batch_id=batch_metadata.get("batch_id", "unknown"))

        def _safe(name: str, fn, *args, **kwargs) -> Optional[float]:
            """Call fn(*args) and catch all exceptions, returning None on failure."""
            try:
                result = fn(*args, **kwargs)
                if isinstance(result, (int, float)) and not math.isnan(result):
                    return float(result)
                return None
            except Exception as exc:
                log.warning("feature_computation_failed", feature=name, error=str(exc))
                return None

        def _col(name: str) -> Optional[pd.Series]:
            """Return a column Series if it exists, else None with a warning."""
            if name in sensor_df.columns:
                series = sensor_df[name].dropna()
                if len(series) >= self.config.min_readings_for_feature:
                    return series
            log.warning("sensor_column_unavailable", column=name)
            return None

        # ---- Scalar averages ----
        for sensor in ("TEMPERATURE", "HUMIDITY", "EC", "PH", "VWC", "CO2", "PPFD"):
            col = _col(sensor)
            features[f"{sensor.lower()}_mean"] = (
                float(col.mean()) if col is not None else None
            )
            features[f"{sensor.lower()}_std"] = (
                float(col.std()) if col is not None and len(col) >= 2 else None
            )
            features[f"{sensor.lower()}_min"] = (
                float(col.min()) if col is not None else None
            )
            features[f"{sensor.lower()}_max"] = (
                float(col.max()) if col is not None else None
            )

        # ---- VPD (computed or measured) ----
        temp_col = _col("TEMPERATURE")
        hum_col = _col("HUMIDITY")
        vpd_col = _col("VPD_CALCULATED")

        if vpd_col is not None:
            features["vpd_mean"] = float(vpd_col.mean())
            features["vpd_std"] = float(vpd_col.std()) if len(vpd_col) >= 2 else None
        elif temp_col is not None and hum_col is not None:
            # Compute VPD from temperature and humidity
            combined = pd.concat([temp_col.rename("T"), hum_col.rename("H")], axis=1).dropna()
            if len(combined) >= self.config.min_readings_for_feature:
                vpd_vals = combined.apply(
                    lambda row: self.compute_vpd(row["T"], row["H"]), axis=1
                )
                features["vpd_mean"] = float(vpd_vals.mean())
                features["vpd_std"] = float(vpd_vals.std()) if len(vpd_vals) >= 2 else None
                vpd_exceedance = self.compute_vpd_exceedance(
                    vpd_vals, self.config.vpd_target_min, self.config.vpd_target_max
                )
                features["vpd_minutes_above"] = float(vpd_exceedance["minutes_above"])
                features["vpd_minutes_below"] = float(vpd_exceedance["minutes_below"])
                features["vpd_pct_in_range"] = vpd_exceedance["pct_in_range"]
            else:
                features["vpd_mean"] = None
                features["vpd_std"] = None
        else:
            features["vpd_mean"] = None
            features["vpd_std"] = None

        # ---- EC drift ----
        if ec_col := _col("EC"):
            features["ec_drift_rate_per_hour"] = _safe(
                "ec_drift_rate", self.compute_ec_drift_rate, ec_col
            )
        else:
            features["ec_drift_rate_per_hour"] = None

        # ---- VWC dryback ----
        if vwc_col := _col("VWC"):
            dryback = {}
            try:
                dryback = self.compute_substrate_dryback(vwc_col)
            except Exception as exc:
                log.warning("dryback_computation_failed", error=str(exc))
            features["vwc_peak"] = dryback.get("peak_vwc")
            features["vwc_trough"] = dryback.get("trough_vwc")
            features["vwc_dryback_pct"] = dryback.get("dryback_pct")
            features["vwc_time_to_dryback_min"] = dryback.get("time_to_dryback_min")
        else:
            features["vwc_peak"] = None
            features["vwc_trough"] = None
            features["vwc_dryback_pct"] = None
            features["vwc_time_to_dryback_min"] = None

        # ---- DLI ----
        if ppfd_col := _col("PPFD"):
            ppfd_df = ppfd_col.to_frame(name="value")
            photoperiod = float(batch_metadata.get("photoperiod_hours", 18.0))
            today = pd.Timestamp.now().normalize()
            features["dli_today"] = _safe(
                "dli", self.compute_dli, ppfd_df, today, photoperiod
            )
        else:
            features["dli_today"] = None

        # ---- Spike and flatline counts ----
        for sensor in ("TEMPERATURE", "HUMIDITY", "EC", "PH"):
            col = _col(sensor)
            if col is not None:
                spikes = self.detect_spikes(col)
                flatlines = self.detect_flatline(col)
                features[f"{sensor.lower()}_spike_count"] = float(spikes.sum())
                features[f"{sensor.lower()}_flatline_count"] = float(flatlines.sum())
            else:
                features[f"{sensor.lower()}_spike_count"] = None
                features[f"{sensor.lower()}_flatline_count"] = None

        # ---- Stage normalisation ----
        stage_start = batch_metadata.get("stage_start_day")
        stage_end = batch_metadata.get("stage_end_day")
        days_in_stage = batch_metadata.get("days_in_stage")
        if (
            stage_start is not None
            and stage_end is not None
            and days_in_stage is not None
        ):
            current_day = int(stage_start) + int(days_in_stage)
            features["stage_progress_normalized"] = self.normalize_stage_day(
                current_day, int(stage_start), int(stage_end)
            )
        else:
            features["stage_progress_normalized"] = None

        # ---- Batch-level scalars from metadata ----
        features["planned_veg_days"] = float(
            batch_metadata["planned_veg_days"]
        ) if "planned_veg_days" in batch_metadata else None
        features["planned_flower_days"] = float(
            batch_metadata["planned_flower_days"]
        ) if "planned_flower_days" in batch_metadata else None
        features["days_elapsed"] = float(
            batch_metadata["days_elapsed"]
        ) if "days_elapsed" in batch_metadata else None

        log.info(
            "batch_features_built",
            n_features=len(features),
            n_missing=sum(1 for v in features.values() if v is None),
        )
        return features
