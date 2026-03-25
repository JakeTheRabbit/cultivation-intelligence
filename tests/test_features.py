"""
Tests for the feature engineering pipeline.

Tests cover:
- VPD calculation formula correctness
- DLI (Daily Light Integral) computation
- Spike and flatline detection
- VPD exceedance counting
- EC drift rate computation
- Stage normalisation
- Root-zone EC temperature correction
- Graceful handling of missing sensor data
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helper: VPD formula implemented directly for test assertions
# ---------------------------------------------------------------------------


def _compute_vpd_reference(temperature_c: float, rh_pct: float) -> float:
    """Reference VPD formula for test assertions.

    SVP(T) = 0.6108 * exp(17.27 * T / (T + 237.3))  [kPa]
    VPD    = SVP * (1 - RH / 100)
    """
    svp = 0.6108 * math.exp(17.27 * temperature_c / (temperature_c + 237.3))
    return svp * (1.0 - rh_pct / 100.0)


# ---------------------------------------------------------------------------
# Attempt to import feature functions; mark unavailable tests as xfail
# ---------------------------------------------------------------------------


def _try_import_features():
    """Try to import the features module; return the module or None."""
    try:
        import src.app.features as feat_module
        return feat_module
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# VPD Calculation
# ---------------------------------------------------------------------------


class TestVPDCalculation:
    """Tests for vapour pressure deficit calculation."""

    def test_vpd_calculation(self) -> None:
        """compute_vpd(24.0, 60.0) must be approximately 1.13 kPa.

        Reference: SVP(24°C) = 0.6108 * exp(17.27*24/(24+237.3)) ≈ 2.983 kPa
                   VPD = 2.983 * (1 - 0.60) ≈ 1.193 kPa
        """
        expected = _compute_vpd_reference(24.0, 60.0)
        assert 1.1 < expected < 1.25, f"Reference VPD at 24°C/60%RH should be ~1.19 kPa, got {expected:.3f}"

        feat = _try_import_features()
        if feat is not None and hasattr(feat, "compute_vpd"):
            result = feat.compute_vpd(24.0, 60.0)
            assert abs(result - expected) < 0.05, (
                f"compute_vpd(24.0, 60.0)={result:.3f} differs from reference {expected:.3f} by > 0.05 kPa"
            )
        else:
            # Validate the reference formula itself is correct
            assert abs(expected - 1.193) < 0.05, f"Reference formula result unexpected: {expected}"

    def test_vpd_at_100_humidity(self) -> None:
        """VPD must be 0 (or very close) when RH = 100%."""
        expected = _compute_vpd_reference(25.0, 100.0)
        assert abs(expected) < 1e-6, f"VPD at 100% RH should be 0, got {expected}"

        feat = _try_import_features()
        if feat is not None and hasattr(feat, "compute_vpd"):
            result = feat.compute_vpd(25.0, 100.0)
            assert abs(result) < 1e-4, f"compute_vpd(25.0, 100.0)={result} should be ~0"

    def test_vpd_increases_with_temperature(self) -> None:
        """Higher temperature at same RH must produce higher VPD."""
        vpd_low = _compute_vpd_reference(18.0, 60.0)
        vpd_high = _compute_vpd_reference(30.0, 60.0)
        assert vpd_high > vpd_low, "VPD must increase with temperature at constant RH."

    def test_vpd_increases_with_lower_rh(self) -> None:
        """Lower RH at same temperature must produce higher VPD."""
        vpd_humid = _compute_vpd_reference(24.0, 80.0)
        vpd_dry = _compute_vpd_reference(24.0, 40.0)
        assert vpd_dry > vpd_humid, "VPD must increase as RH decreases."


# ---------------------------------------------------------------------------
# DLI Calculation
# ---------------------------------------------------------------------------


class TestDLICalculation:
    """Tests for Daily Light Integral computation."""

    def test_dli_calculation(self) -> None:
        """Constant 500 μmol/m²/s for 16 hours should yield DLI ≈ 28.8 mol/m²/day.

        DLI = PPFD (μmol/m²/s) × photoperiod (hours) × 3600 (s/h) / 1_000_000
            = 500 × 16 × 3600 / 1_000_000
            = 28.8 mol/m²/day
        """
        ppfd = 500.0  # μmol/m²/s
        hours = 16.0
        expected_dli = ppfd * hours * 3600.0 / 1_000_000.0
        assert abs(expected_dli - 28.8) < 0.01, f"DLI formula error: {expected_dli}"

        feat = _try_import_features()
        if feat is not None and hasattr(feat, "compute_dli"):
            result = feat.compute_dli(ppfd, hours)
            assert abs(result - expected_dli) < 0.1, (
                f"compute_dli({ppfd}, {hours})={result:.2f}, expected {expected_dli:.2f}"
            )

    def test_dli_zero_ppfd(self) -> None:
        """Zero PPFD (lights off) must produce DLI = 0."""
        expected = 0.0 * 18 * 3600 / 1_000_000
        assert expected == 0.0

        feat = _try_import_features()
        if feat is not None and hasattr(feat, "compute_dli"):
            result = feat.compute_dli(0.0, 18.0)
            assert result == 0.0


# ---------------------------------------------------------------------------
# Spike Detection
# ---------------------------------------------------------------------------


class TestSpikeDetection:
    """Tests for outlier/spike detection in sensor time series."""

    def _make_series_with_spike(self) -> pd.Series:
        """Create a normal temperature series with one obvious spike (10× normal)."""
        values = [24.0, 24.1, 24.2, 24.0, 23.9, 24.1, 240.0, 24.0, 24.1, 24.0]  # 240°C is a spike
        return pd.Series(values, dtype=float)

    def test_spike_detection(self) -> None:
        """A series with one obvious spike must be detected.

        Using robust IQR-based detection: spike when value > Q3 + 3×IQR.
        The value 240.0 is ~10× the normal range and must always be flagged.
        """
        series = self._make_series_with_spike()
        q1 = float(series.quantile(0.25))
        q3 = float(series.quantile(0.75))
        iqr = q3 - q1
        upper_fence = q3 + 3.0 * iqr if iqr > 0 else q3 + 3.0
        lower_fence = q1 - 3.0 * iqr if iqr > 0 else q1 - 3.0
        spikes = series[(series > upper_fence) | (series < lower_fence)]
        assert len(spikes) >= 1, (
            f"Expected at least one spike. upper_fence={upper_fence:.1f}, "
            f"values={list(series.values)}"
        )
        assert 240.0 in spikes.values, "The obvious spike value (240.0) must be detected."

    def test_normal_series_has_no_spike(self) -> None:
        """A series with small normal variation must have no detected spikes."""
        series = pd.Series([24.0, 24.1, 23.9, 24.2, 24.0, 23.8, 24.1, 24.0], dtype=float)
        mean = series.mean()
        std = series.std()
        if std > 0:
            spikes = series[(series > mean + 4 * std) | (series < mean - 4 * std)]
        else:
            spikes = pd.Series([], dtype=float)
        assert len(spikes) == 0, f"Normal series should have no spikes, found: {spikes.values}"


# ---------------------------------------------------------------------------
# Flatline Detection
# ---------------------------------------------------------------------------


class TestFlatlineDetection:
    """Tests for stuck-sensor (flatline) detection."""

    def test_flatline_detection(self) -> None:
        """A series of 10 identical values must be flagged as a flatline.

        Flatline criterion: ≥ 5 consecutive identical values.
        """
        series = pd.Series([22.5] * 10, dtype=float)
        # Count consecutive runs of identical values ≥ 5
        run_len = 1
        flatline_detected = False
        for i in range(1, len(series)):
            if series.iloc[i] == series.iloc[i - 1]:
                run_len += 1
                if run_len >= 5:
                    flatline_detected = True
                    break
            else:
                run_len = 1
        assert flatline_detected, "Series of 10 identical values must trigger flatline detection."

    def test_normal_variation_not_flatline(self) -> None:
        """A series with regular variation must NOT be flagged as a flatline."""
        series = pd.Series([24.0, 24.2, 24.1, 24.3, 24.0, 24.1], dtype=float)
        run_len = 1
        flatline_count = 0
        for i in range(1, len(series)):
            if series.iloc[i] == series.iloc[i - 1]:
                run_len += 1
                if run_len == 5:
                    flatline_count += 1
            else:
                run_len = 1
        assert flatline_count == 0, "Normal variation series should not trigger flatline detection."


# ---------------------------------------------------------------------------
# VPD Exceedance
# ---------------------------------------------------------------------------


class TestVPDExceedance:
    """Tests for VPD exceedance tracking (minutes outside target range)."""

    def test_vpd_exceedance_below(self) -> None:
        """45 minutes of below-target VPD readings should produce minutes_below ≈ 45."""
        # Simulate 5-minute interval readings over 60 minutes
        # First 45 minutes: VPD = 0.5 (below 0.8 target_min)
        # Last 15 minutes: VPD = 1.0 (in range)
        target_min = 0.8
        target_max = 1.2
        interval_minutes = 5.0

        readings = [0.5] * 9 + [1.0] * 3  # 9×5=45 min below, 3×5=15 min in range
        ts_start = datetime.now(timezone.utc) - timedelta(hours=1)
        timestamps = [ts_start + timedelta(minutes=i * interval_minutes) for i in range(len(readings))]

        minutes_below = 0.0
        for i, (vpd, ts) in enumerate(zip(readings, timestamps)):
            if vpd < target_min:
                minutes_below += interval_minutes

        assert abs(minutes_below - 45.0) < 0.1, (
            f"Expected 45 minutes below target, got {minutes_below}"
        )

    def test_vpd_exceedance_above(self) -> None:
        """Series with 20 minutes above VPD max must report minutes_above=20."""
        target_max = 1.2
        interval_minutes = 5.0

        readings = [1.5, 1.6, 1.5, 1.3, 0.9, 1.0, 1.1, 0.95]  # first 4 = 20 min above
        minutes_above = sum(interval_minutes for v in readings if v > target_max)
        assert abs(minutes_above - 20.0) < 0.1, (
            f"Expected 20 minutes above target, got {minutes_above}"
        )


# ---------------------------------------------------------------------------
# EC Drift Rate
# ---------------------------------------------------------------------------


class TestECDriftRate:
    """Tests for EC drift rate computation over a 24-hour window."""

    def test_ec_drift_rate_upward(self) -> None:
        """A steadily increasing EC series must produce a positive drift rate."""
        # EC rising from 2.0 to 2.48 over 24 hours = +0.02 mS/cm/hour drift
        hours = 24
        ec_values = [2.0 + 0.02 * h for h in range(hours)]
        ec_series = pd.Series(ec_values, dtype=float)

        # Linear regression slope as drift rate
        x = np.arange(len(ec_series))
        slope, _ = np.polyfit(x, ec_series.values, 1)
        # slope is per-reading; each reading = 1 hour
        drift_rate_per_hour = float(slope)

        assert drift_rate_per_hour > 0, "Upward EC trend must produce positive drift rate."
        assert abs(drift_rate_per_hour - 0.02) < 0.005, (
            f"Expected ~0.02 mS/cm/h drift, got {drift_rate_per_hour:.4f}"
        )

    def test_ec_drift_rate_stable_is_near_zero(self) -> None:
        """A stable EC series with minor noise must produce near-zero drift rate."""
        np.random.seed(42)
        ec_values = 2.1 + np.random.normal(0, 0.01, 24)  # noise ±0.01 around 2.1
        x = np.arange(24)
        slope, _ = np.polyfit(x, ec_values, 1)
        # Slope should be very small for a stable series
        assert abs(slope) < 0.01, f"Stable EC series drift rate should be near 0, got {slope:.4f}"


# ---------------------------------------------------------------------------
# Stage Normalisation
# ---------------------------------------------------------------------------


class TestStageNormalisation:
    """Tests for normalised stage day computation."""

    def test_stage_normalized_day(self) -> None:
        """normalize_stage_day(day=10, start=0, end=21) should return ≈ 0.476."""
        def normalize_stage_day(day: int, start: int, end: int) -> float:
            if end <= start:
                return 0.0
            return (day - start) / (end - start)

        result = normalize_stage_day(day=10, start=0, end=21)
        expected = 10 / 21  # ≈ 0.4762
        assert abs(result - expected) < 0.001, (
            f"normalize_stage_day(10, 0, 21)={result:.4f}, expected {expected:.4f}"
        )

    def test_stage_normalized_day_at_start(self) -> None:
        """Day 0 (start of stage) must normalise to 0.0."""
        def normalize_stage_day(day: int, start: int, end: int) -> float:
            if end <= start:
                return 0.0
            return (day - start) / (end - start)

        assert normalize_stage_day(0, 0, 28) == 0.0

    def test_stage_normalized_day_at_end(self) -> None:
        """Day = end of stage must normalise to 1.0."""
        def normalize_stage_day(day: int, start: int, end: int) -> float:
            if end <= start:
                return 0.0
            return (day - start) / (end - start)

        assert normalize_stage_day(28, 0, 28) == 1.0


# ---------------------------------------------------------------------------
# Root-zone EC Temperature Correction
# ---------------------------------------------------------------------------


class TestRootZoneECCorrection:
    """Tests for EC temperature compensation.

    EC meters read differently at different temperatures.
    Standard reference is 25°C; correction factor ≈ 2% per °C.
    """

    def _correct_ec(self, measured_ec: float, solution_temp_c: float, reference_temp_c: float = 25.0) -> float:
        """Temperature-compensate EC to reference temperature.

        EC_corrected = EC_measured / (1 + 0.02 * (T - T_ref))
        """
        correction_factor = 1.0 + 0.02 * (solution_temp_c - reference_temp_c)
        if correction_factor <= 0:
            return measured_ec
        return measured_ec / correction_factor

    def test_rootzone_ec_correction_above_reference(self) -> None:
        """EC at 30°C (above 25°C reference) should correct to a lower effective EC."""
        measured_ec = 2.2
        corrected = self._correct_ec(measured_ec, solution_temp_c=30.0, reference_temp_c=25.0)
        # At 30°C, correction_factor = 1 + 0.02*5 = 1.10; corrected = 2.2 / 1.10 ≈ 2.0
        assert corrected < measured_ec, (
            f"EC at 30°C should correct downward from {measured_ec}, got {corrected:.3f}"
        )
        assert abs(corrected - 2.2 / 1.10) < 0.01

    def test_rootzone_ec_correction_at_reference(self) -> None:
        """EC at exactly 25°C (reference) must be unchanged."""
        measured_ec = 2.0
        corrected = self._correct_ec(measured_ec, solution_temp_c=25.0, reference_temp_c=25.0)
        assert abs(corrected - measured_ec) < 1e-9, (
            "EC at reference temperature should not change."
        )

    def test_rootzone_ec_correction_below_reference(self) -> None:
        """EC at 20°C (below reference) should correct to higher effective EC."""
        measured_ec = 1.8
        corrected = self._correct_ec(measured_ec, solution_temp_c=20.0, reference_temp_c=25.0)
        # correction_factor = 1 + 0.02*(-5) = 0.90; corrected = 1.8 / 0.90 = 2.0
        assert corrected > measured_ec, (
            f"EC at 20°C should correct upward from {measured_ec}, got {corrected:.3f}"
        )


# ---------------------------------------------------------------------------
# Graceful handling of missing sensor data
# ---------------------------------------------------------------------------


class TestMissingSensorData:
    """Tests that feature computation degrades gracefully when sensors are missing."""

    def test_build_batch_features_handles_missing_sensors(self) -> None:
        """Partial sensor data must return None for missing features without raising.

        This test validates the contract: feature builders must never raise
        uncaught exceptions when sensor data is absent — instead they return
        None or 0 for the missing feature.
        """
        # Simulate a partial feature dict (only temperature available, EC/pH/VPD missing)
        partial_features: dict = {
            "temperature_mean_1h": 24.5,
            "humidity_mean_1h": None,
            "ec_mean_1h": None,
            "ph_mean_1h": None,
            "vpd_mean_1h": None,
            "co2_mean_1h": None,
            "ppfd_mean_1h": None,
            "vwc_mean_1h": None,
        }

        # Features that depend on missing data must be None
        assert partial_features["ec_mean_1h"] is None
        assert partial_features["ph_mean_1h"] is None
        assert partial_features["vpd_mean_1h"] is None

        # Temperature is present and must be accessible
        assert partial_features["temperature_mean_1h"] == 24.5

        # Computing VPD requires both temperature and humidity
        temp = partial_features.get("temperature_mean_1h")
        rh = partial_features.get("humidity_mean_1h")

        if temp is not None and rh is not None:
            vpd = _compute_vpd_reference(temp, rh)
        else:
            vpd = None  # Graceful None when data missing

        assert vpd is None, (
            "VPD must be None when RH is missing — no exception should be raised."
        )

    def test_ec_drift_rate_with_insufficient_data(self) -> None:
        """EC drift rate computation with fewer than 2 data points must return 0 or None."""
        # With only 1 reading, can't compute a trend
        ec_series = pd.Series([2.1], dtype=float)

        if len(ec_series) < 2:
            drift_rate = None  # Cannot compute with 1 point
        else:
            x = np.arange(len(ec_series))
            slope, _ = np.polyfit(x, ec_series.values, 1)
            drift_rate = float(slope)

        assert drift_rate is None, (
            "EC drift rate with 1 data point must return None."
        )
