# Feature Engineering Documentation

**Project**: Cultivation Intelligence
**Facility**: Legacy Ag Limited — Indoor Medicinal Cannabis, New Zealand
**Last Updated**: 2026-03-25

---

## Table of Contents

1. [Philosophy](#philosophy)
2. [VPD Calculation](#vpd-calculation)
3. [DLI Calculation](#dli-calculation)
4. [EC and pH Features](#ec-and-ph-features)
5. [VWC Features](#vwc-features)
6. [Temporal Window Features](#temporal-window-features)
7. [Stage-Aware Features](#stage-aware-features)
8. [Event Features (Irrigation)](#event-features-irrigation)
9. [Cross-Sensor Interactions](#cross-sensor-interactions)
10. [Derived Risk Indicators](#derived-risk-indicators)
11. [QC and Drift Handling](#qc-and-drift-handling)
12. [Pipeline Architecture](#pipeline-architecture)
13. [Feature Importance Feedback Loop](#feature-importance-feedback-loop)
14. [Feature Registry](#feature-registry)

---

## Philosophy

The guiding principle of this feature engineering layer is: **encode domain knowledge as explicit features, not implicit model complexity.**

A gradient boosted tree with 50 domain-informed features will outperform a deep neural network given 200 raw sensor columns, at this data scale. The reasons are:

1. **Data volume**: A single Legacy Ag cultivation facility produces on the order of 5–10 completed batches per year. Even with multiple rooms and strains, the total batch count available for supervised learning will be in the low dozens for the first few years. High-dimensional raw inputs will overfit this.

2. **Interpretability**: Regulatory and operator trust requirements demand that every prediction can be explained. A feature named `vpd_exceedance_minutes_6h` is self-explanatory. A raw humidity reading at 14:32:07 contributing to a deep network's output is not.

3. **Domain knowledge is free**: The agronomic relationships between VPD, transpiration, stomatal conductance, and yield are known. EC-pH interaction at the root zone is characterized. Encoding this as features costs computation at inference time, not labeled data at training time.

4. **Sensor noise robustness**: Raw sensor readings from Zigbee devices in a humid cultivation environment carry noise. Aggregated and derived features are substantially more robust to individual sensor faults.

Features are organized into groups: environmental physics (VPD, DLI), solution chemistry (EC, pH), substrate status (VWC), temporal statistics, stage context, event history, cross-sensor interactions, and risk indicators. Each group is computed by a dedicated transformer in the pipeline.

---

## VPD Calculation

### Formula

Vapour Pressure Deficit is computed as:

```
VPD = (1 - RH/100) × SVP(T)

where:
SVP(T) = 0.6108 × exp(17.27 × T / (T + 237.3))   [kPa]
```

This is the Tetens-Magnus approximation for saturation vapour pressure, valid from -40°C to 60°C (well within cultivation range).

### Term-by-Term Explanation

- **SVP(T)**: Saturation vapour pressure in kPa at temperature T (°C). This is the maximum water vapour the air can hold at temperature T. It increases non-linearly with temperature — at 25°C, SVP ≈ 3.17 kPa; at 28°C, SVP ≈ 3.78 kPa.
- **RH/100**: Fractional relative humidity. At 60% RH, the air is holding 60% of its maximum possible water vapour.
- **(1 - RH/100)**: The vapour pressure deficit fraction — how much additional water vapour the air could still absorb.
- **VPD**: The actual deficit in kPa. Higher VPD → drier air → stronger pull on plant transpiration.

### Why VPD Matters in Cannabis Cultivation

VPD drives transpiration rate through stomata. Too low → stomata close, CO2 uptake drops, humidity builds favouring Botrytis. Too high → stomatal closure as a drought response, nutrient uptake declines. The relationship between VPD and yield is non-linear and stage-dependent.

### Target Ranges by Stage

| Stage         | VPD Target (kPa) | Notes                                          |
|---------------|-----------------|------------------------------------------------|
| PROPAGATION   | 0.4 – 0.7       | Low VPD protects unrooted cuttings             |
| VEG           | 0.8 – 1.2       | Moderate transpiration supports vegetative growth |
| EARLY_FLOWER  | 1.0 – 1.5       | Increasing VPD encourages flower initiation    |
| MID_FLOWER    | 1.2 – 1.6       | Peak demand; maintain consistently             |
| LATE_FLOWER   | 1.4 – 2.0       | Elevated to reduce Botrytis risk               |
| FLUSH         | 1.0 – 1.6       | Maintain transpiration without nutrient push   |

### Python Implementation

```python
import numpy as np

def compute_vpd(temperature_c: np.ndarray, rh_pct: np.ndarray) -> np.ndarray:
    """
    Compute Vapour Pressure Deficit using the Magnus-Tetens approximation.

    Parameters
    ----------
    temperature_c : array-like, degrees Celsius
    rh_pct : array-like, relative humidity 0-100

    Returns
    -------
    vpd_kpa : np.ndarray, VPD in kPa
    """
    temperature_c = np.asarray(temperature_c, dtype=float)
    rh_pct = np.asarray(rh_pct, dtype=float)

    # Saturation vapour pressure (Tetens-Magnus)
    svp = 0.6108 * np.exp(17.27 * temperature_c / (temperature_c + 237.3))

    # VPD = deficit fraction × SVP
    vpd = (1.0 - rh_pct / 100.0) * svp

    return np.clip(vpd, 0.0, None)  # VPD cannot be negative


class VPDTransformer(BaseEstimator, TransformerMixin):
    """Derives VPD from temperature and humidity columns."""

    def __init__(self, temp_col='temperature_c', rh_col='humidity_pct'):
        self.temp_col = temp_col
        self.rh_col = rh_col

    def fit(self, X, y=None):
        return self

    def transform(self, X):
        X = X.copy()
        X['vpd_kpa'] = compute_vpd(X[self.temp_col], X[self.rh_col])
        return X
```

---

## DLI Calculation

### Formula

Daily Light Integral (DLI) is the total photosynthetically active photons delivered per square metre per day:

```
DLI = PPFD × photoperiod_seconds / 1,000,000   [mol/m²/day]

where photoperiod_seconds = lights_on_hours × 3600
```

PPFD (µmol/m²/s) × seconds gives total µmol/m², divided by 1,000,000 to convert to mol/m²/day.

### Accumulation Logic

DLI is accumulated from midnight to midnight (NZ local time, stored as UTC offset). Because lights-on times are fixed schedules (not continuous measurement), DLI can be computed two ways:

1. **Schedule-based** (preferred for forward projection): multiply current PPFD setpoint by scheduled photoperiod hours.
2. **Measurement-based** (for actual DLI): integrate real PPFD sensor readings over the day.

```python
def compute_dli_from_measurements(
    ppfd_series: pd.Series,
    time_index: pd.DatetimeIndex,
    sampling_interval_sec: int = 60
) -> float:
    """
    Compute DLI by integrating PPFD readings over a day.
    Assumes uniform sampling at sampling_interval_sec.

    Returns DLI in mol/m²/day.
    """
    # Only include readings where lights are on (PPFD > threshold)
    ppfd_positive = ppfd_series.clip(lower=0.0)

    # Integrate: sum(PPFD) × dt / 1e6
    total_umol_per_m2 = ppfd_positive.sum() * sampling_interval_sec
    dli = total_umol_per_m2 / 1_000_000.0
    return dli
```

### Target DLI by Stage

| Stage         | DLI Target (mol/m²/day) | Typical Setup             |
|---------------|------------------------|---------------------------|
| PROPAGATION   | 10 – 15                | Lower intensity, 18h light |
| VEG           | 25 – 35                | High intensity, 18h light  |
| EARLY_FLOWER  | 35 – 45                | 12h light, elevated PPFD   |
| MID_FLOWER    | 40 – 50                | 12h, maximum PPFD          |
| LATE_FLOWER   | 35 – 45                | 12h, slightly reduced      |
| FLUSH         | 25 – 35                | 12h, reduced load          |

### DLI Accumulation Feature

A key feature is `dli_accumulated_today` — the running DLI since midnight, updated every 15 minutes. This supports real-time advisory: "lights have delivered 32 of 45 target mol/m² today."

---

## EC and pH Features

### Raw EC Features

The AquaPro dosing unit (AQU1AD04A42) reports EC via `sensor.aquapro_aq1ad04a42_ec`. Raw EC is stored in `sensor_readings` and the following derived features are computed:

```python
def ec_features(ec_series: pd.Series, ec_setpoint: float) -> dict:
    """
    Compute EC-derived features.
    ec_series: time-ordered EC readings (mS/cm)
    ec_setpoint: target EC for current stage (mS/cm)
    """
    deviation = ec_series - ec_setpoint

    # Rate of change: EC drift per hour (rolling linear slope)
    time_hours = np.arange(len(ec_series)) / (60.0 / 1)  # assuming 1-min interval
    slope, _ = np.polyfit(time_hours[-24:], ec_series.iloc[-24:], 1)  # last 24 readings

    # Stability index: 1 - normalized std over last 6 readings
    stability = 1.0 - (ec_series.iloc[-6:].std() / (ec_setpoint + 1e-6))

    return {
        'ec_current': ec_series.iloc[-1],
        'ec_deviation_from_setpoint': deviation.iloc[-1],
        'ec_mean_deviation_1h': deviation.iloc[-60:].mean(),
        'ec_mean_deviation_6h': deviation.iloc[-360:].mean(),
        'ec_drift_rate_per_hour': slope,
        'ec_stability_index': np.clip(stability, 0.0, 1.0),
        'ec_abs_deviation_max_6h': deviation.iloc[-360:].abs().max(),
    }
```

**Key EC Features:**

- `ec_deviation_from_setpoint`: Signed deviation. Negative = under-feeding, positive = over-feeding.
- `ec_drift_rate_per_hour`: Slope of EC over recent history. Positive drift suggests accumulation in the root zone. Negative drift suggests dilution or heavy uptake.
- `ec_stability_index`: 0–1. Low stability during the 12h post-irrigation window is expected; sustained instability may indicate dosing pump issues.

### Raw pH Features

```python
def ph_features(ph_series: pd.Series, ph_setpoint: float) -> dict:
    deviation = ph_series - ph_setpoint
    swing = ph_series.iloc[-360:].max() - ph_series.iloc[-360:].min()

    return {
        'ph_current': ph_series.iloc[-1],
        'ph_deviation_from_setpoint': deviation.iloc[-1],
        'ph_swing_magnitude_6h': swing,
        'ph_mean_deviation_24h': deviation.iloc[-1440:].mean(),
        'ph_above_7_minutes_24h': (ph_series.iloc[-1440:] > 7.0).sum(),
        'ph_below_5_5_minutes_24h': (ph_series.iloc[-1440:] < 5.5).sum(),
    }
```

**pH Swing Magnitude** (`ph_swing_magnitude_6h`) is particularly important: a swing > 0.5 units within 6 hours indicates dosing instability that can cause nutrient lockout even if the mean pH is correct.

---

## VWC Features

### Substrate Moisture Curves

Volumetric Water Content (VWC) in rockwool slabs follows a characteristic sawtooth pattern: irrigation shots drive VWC up rapidly, then it declines as plants transpire. Key features are extracted from this pattern.

```python
def vwc_features(vwc_series: pd.Series, irrigation_times: pd.DatetimeIndex) -> dict:
    """
    Extract substrate moisture curve features.
    """
    current_vwc = vwc_series.iloc[-1]

    # Field capacity: maximum VWC reached within 5 minutes of irrigation
    # (rockwool drains quickly; peak VWC ≈ field capacity)
    field_capacity_estimates = []
    for irr_time in irrigation_times:
        post_irr = vwc_series[irr_time: irr_time + pd.Timedelta('10min')]
        if len(post_irr) > 0:
            field_capacity_estimates.append(post_irr.max())
    field_capacity = np.median(field_capacity_estimates) if field_capacity_estimates else np.nan

    # Dry-back percentage: how far VWC has dropped from field capacity
    # High dry-back (>15%) before lights-on is agronomically desirable
    dry_back_pct = (field_capacity - current_vwc) / (field_capacity + 1e-6) * 100.0

    # Overnight dry-back: VWC change from lights-off to lights-on
    # (computed externally using schedule; placeholder here)
    overnight_dry_back_pct = None  # set by schedule-aware transformer

    return {
        'vwc_current': current_vwc,
        'vwc_field_capacity_estimate': field_capacity,
        'vwc_dry_back_pct': np.clip(dry_back_pct, 0.0, 100.0),
        'vwc_mean_24h': vwc_series.iloc[-1440:].mean(),
        'vwc_min_24h': vwc_series.iloc[-1440:].min(),
        'vwc_max_24h': vwc_series.iloc[-1440:].max(),
    }
```

### Field Capacity Detection

Field capacity is detected empirically as the peak VWC reading within 5–10 minutes of an irrigation shot. In rockwool, this typically occurs within 2–3 minutes of irrigation completion.

### Dry-Back Percentage

Dry-back is the controlled reduction in substrate moisture between the end of the last irrigation and the start of the next. It promotes oxygen availability at the root zone and triggers plant hormonal responses associated with flower development.

Target dry-back:
- **Day dry-back**: 5–8% reduction from field capacity (measured shot-to-shot during lights-on)
- **Night dry-back**: 8–15% from last lights-on shot to first lights-on shot next day

---

## Temporal Window Features

For each sensor type, the following window features are computed. Windows are computed on `quality_flag IN ('OK', 'SUSPECT')` readings only.

```python
WINDOWS = {
    '15min': 15,
    '1h':    60,
    '6h':    360,
    '24h':   1440,
    '48h':   2880,
    '7d':    10080,
}

def temporal_window_features(
    series: pd.Series,
    sensor_type: str,
    quality_flags: pd.Series
) -> dict:
    """
    Compute rolling window statistics for a single sensor type.
    Series assumed to be 1-minute resampled with NaN for missing.
    """
    # Mask out INVALID readings
    valid = series.where(quality_flags != 'INVALID')
    features = {}

    for label, minutes in WINDOWS.items():
        window = valid.iloc[-minutes:]
        prefix = f"{sensor_type.lower()}_{label}"
        features[f"{prefix}_mean"]  = window.mean()
        features[f"{prefix}_std"]   = window.std()
        features[f"{prefix}_min"]   = window.min()
        features[f"{prefix}_max"]   = window.max()
        features[f"{prefix}_range"] = window.max() - window.min()

    # Deviation from 24h baseline (current value vs 24h mean)
    mean_24h = valid.iloc[-1440:].mean()
    features[f"{sensor_type.lower()}_deviation_from_24h_baseline"] = (
        valid.iloc[-1] - mean_24h if not np.isnan(mean_24h) else np.nan
    )

    return features
```

Temporal features are the backbone of the model. The 24h and 7d windows capture slow trends (EC accumulation, pH drift). The 15min and 1h windows capture acute events (temperature spike post-light-on, VPD crash from humidity flush).

---

## Stage-Aware Features

Growth stage determines what "normal" looks like for every sensor. A VPD of 1.0 kPa is ideal in VEG but potentially low in LATE_FLOWER. Stage-aware features make this explicit.

```python
# Stage durations (typical, in days) — used for normalized_stage_day
STAGE_DURATIONS = {
    'PROPAGATION': 14,
    'VEG': 21,
    'EARLY_FLOWER': 14,
    'MID_FLOWER': 21,
    'LATE_FLOWER': 14,
    'FLUSH': 7,
}

def stage_aware_features(
    batch: dict,
    current_date: date,
    stage_start_date: date
) -> dict:
    stage = batch['current_stage']
    typical_duration = STAGE_DURATIONS.get(stage, 14)

    stage_day = (current_date - stage_start_date).days
    normalized_stage_day = np.clip(stage_day / typical_duration, 0.0, 1.0)

    # Estimate days to harvest
    # Remaining stages after current
    stage_order = ['PROPAGATION', 'VEG', 'EARLY_FLOWER', 'MID_FLOWER',
                   'LATE_FLOWER', 'FLUSH']
    current_idx = stage_order.index(stage) if stage in stage_order else 0
    remaining_stages = stage_order[current_idx + 1:]
    days_remaining_in_stage = max(0, typical_duration - stage_day)
    days_to_harvest = days_remaining_in_stage + sum(
        STAGE_DURATIONS.get(s, 14) for s in remaining_stages
    )

    # Accumulated stage days (total days since propagation start)
    total_batch_day = (current_date - batch['start_date']).days

    return {
        'stage_encoded': stage_order.index(stage) if stage in stage_order else -1,
        'normalized_stage_day': normalized_stage_day,
        'stage_day_absolute': stage_day,
        'days_to_harvest_estimate': days_to_harvest,
        'total_batch_day': total_batch_day,
        # Stage one-hot (for models that benefit from explicit stage flags)
        **{f"is_stage_{s.lower()}": int(s == stage) for s in stage_order},
    }
```

### Stage Accumulation Metrics

Accumulated metrics over the entire current stage (reset at stage transition):

- `stage_accumulated_dli`: Total DLI delivered so far in this stage (mol/m²)
- `stage_mean_vpd`: Mean VPD since stage start
- `stage_ec_time_above_setpoint_hours`: Hours where EC exceeded setpoint during this stage
- `stage_ph_exceedance_hours`: Hours outside pH 5.5–6.5 band

---

## Event Features (Irrigation)

```python
def irrigation_event_features(
    irrigation_events: pd.DataFrame,
    lookback_days: int = 7
) -> dict:
    """
    Compute features from irrigation event history.
    irrigation_events: DataFrame with columns [time, duration_seconds, volume_ml,
                        ec_setpoint, ph_setpoint, ec_actual, ph_actual]
    """
    recent = irrigation_events[
        irrigation_events['time'] >= pd.Timestamp.now() - pd.Timedelta(days=lookback_days)
    ]

    shots_per_day = len(recent) / max(lookback_days, 1)

    # Shot duration trend: is duration increasing (stress response) or decreasing?
    if len(recent) >= 3:
        durations = recent['duration_seconds'].values
        duration_trend = np.polyfit(np.arange(len(durations)), durations, 1)[0]
    else:
        duration_trend = 0.0

    # EC drift post-irrigation: difference between ec_setpoint and ec_actual
    ec_delivery_error = (recent['ec_actual'] - recent['ec_setpoint']).abs().mean()
    ph_delivery_error = (recent['ph_actual'] - recent['ph_setpoint']).abs().mean()

    # Time since last irrigation (minutes)
    if len(recent) > 0:
        last_irr_time = recent['time'].max()
        minutes_since_last_irrigation = (
            pd.Timestamp.now(tz='UTC') - last_irr_time
        ).total_seconds() / 60.0
    else:
        minutes_since_last_irrigation = np.nan

    return {
        'irrigation_shots_per_day_7d': shots_per_day,
        'irrigation_duration_trend': duration_trend,
        'ec_delivery_error_mean': ec_delivery_error,
        'ph_delivery_error_mean': ph_delivery_error,
        'minutes_since_last_irrigation': minutes_since_last_irrigation,
        'total_volume_ml_24h': recent[
            recent['time'] >= pd.Timestamp.now() - pd.Timedelta(hours=24)
        ]['volume_ml'].sum(),
    }
```

---

## Cross-Sensor Interactions

### Temperature × Humidity Interaction

The VPD formula already encodes the primary temperature-humidity interaction. Additional interactions:

```python
def cross_sensor_features(
    temp_c: float,
    rh_pct: float,
    ec_mS: float,
    rootzone_temp_c: float
) -> dict:
    """
    Compute cross-sensor interaction features.
    """
    # Rootzone EC correction: EC readings are temperature-dependent.
    # Standard reference is 25°C. Correction factor: ~2% per °C.
    ec_temp_corrected = ec_mS / (1 + 0.02 * (rootzone_temp_c - 25.0))

    # Heat × VPD interaction: high temp + high VPD is more stressful
    # than either alone (compound heat stress indicator)
    vpd = compute_vpd(temp_c, rh_pct)
    heat_vpd_stress = max(0.0, (temp_c - 28.0)) * max(0.0, (vpd - 1.6))

    # Canopy-to-air temperature delta (if canopy temp sensor available)
    # canopy_temp_delta = canopy_temp_c - temp_c  # set externally

    return {
        'ec_temp_corrected': ec_temp_corrected,
        'ec_temp_correction_factor': 1 + 0.02 * (rootzone_temp_c - 25.0),
        'heat_vpd_compound_stress': heat_vpd_stress,
    }
```

### Rootzone Temperature Correction for EC

EC meters measure ion conductivity, which is temperature-dependent. At 30°C rootzone temp vs 20°C, the same solution reads ~20% higher EC. The temperature-corrected EC is the agronomically meaningful value and should be used as the model feature rather than raw EC.

---

## Derived Risk Indicators

```python
def risk_indicator_features(
    sensor_data: dict,
    stage: str,
    vpd_targets: dict,
    ec_setpoint: float,
    ph_range: tuple = (5.5, 6.5)
) -> dict:
    """
    Compute derived risk indicators — the leading signals of future problems.
    """
    vpd_target_low, vpd_target_high = vpd_targets.get(stage, (0.8, 1.5))
    vpd_readings = sensor_data['vpd_1min_series']
    ec_readings  = sensor_data['ec_1min_series']
    ph_readings  = sensor_data['ph_1min_series']

    # VPD exceedance: minutes outside target range in last 6h
    vpd_6h = vpd_readings.iloc[-360:]
    vpd_exceedance_min = int(
        ((vpd_6h < vpd_target_low) | (vpd_6h > vpd_target_high)).sum()
    )

    # EC drift rate (mS/cm per hour, over last 6h)
    ec_6h = ec_readings.iloc[-360:]
    ec_drift_rate = np.polyfit(np.arange(len(ec_6h)), ec_6h.values, 1)[0] * 60

    # pH swing magnitude over 6h
    ph_6h = ph_readings.iloc[-360:]
    ph_swing = ph_6h.max() - ph_6h.min()

    # pH time out of agronomic range
    ph_exceedance_min = int(
        ((ph_6h < ph_range[0]) | (ph_6h > ph_range[1])).sum()
    )

    return {
        'vpd_exceedance_minutes_6h': vpd_exceedance_min,
        'vpd_exceedance_pct_6h': vpd_exceedance_min / 360.0,
        'ec_drift_rate_per_hour': ec_drift_rate,
        'ph_swing_magnitude_6h': ph_swing,
        'ph_exceedance_minutes_6h': ph_exceedance_min,
        'risk_vpd_score': np.clip(vpd_exceedance_min / 180.0, 0.0, 1.0),
        'risk_ec_score': np.clip(abs(ec_drift_rate) / 0.5, 0.0, 1.0),
        'risk_ph_score': np.clip(ph_swing / 1.5, 0.0, 1.0),
        # Composite risk: max of sub-scores (not mean — any single high risk matters)
        'composite_risk_score': max(
            np.clip(vpd_exceedance_min / 180.0, 0.0, 1.0),
            np.clip(abs(ec_drift_rate) / 0.5, 0.0, 1.0),
            np.clip(ph_swing / 1.5, 0.0, 1.0),
        ),
    }
```

---

## QC and Drift Handling

### Outlier Detection

Two complementary methods are applied:

**IQR Method (for spikes):**
```python
def iqr_outlier_mask(series: pd.Series, window: int = 20, k: float = 3.0) -> pd.Series:
    """Returns boolean mask: True where value is an outlier."""
    rolling_median = series.rolling(window, center=True, min_periods=5).median()
    rolling_q1 = series.rolling(window, center=True, min_periods=5).quantile(0.25)
    rolling_q3 = series.rolling(window, center=True, min_periods=5).quantile(0.75)
    rolling_iqr = rolling_q3 - rolling_q1
    lower = rolling_median - k * rolling_iqr
    upper = rolling_median + k * rolling_iqr
    return (series < lower) | (series > upper)
```

**3-Sigma Method (for sustained drift):**
```python
def zscore_outlier_mask(series: pd.Series, window: int = 60, threshold: float = 3.0) -> pd.Series:
    rolling_mean = series.rolling(window, min_periods=10).mean()
    rolling_std  = series.rolling(window, min_periods=10).std()
    z_scores = (series - rolling_mean) / (rolling_std + 1e-9)
    return z_scores.abs() > threshold
```

Readings flagged by either method receive `quality_flag = 'SUSPECT'`. Readings flagged by both, or with |z| > 6, receive `quality_flag = 'INVALID'`.

### Interpolation Policy for Gaps

- **Gap < 5 minutes**: Linear interpolation is applied before feature computation. The interpolated values are not stored in `sensor_readings` (to preserve append-only integrity) but are used transiently in the feature pipeline.
- **Gap 5–30 minutes**: Forward-fill the last valid reading for features that require continuity (e.g., VWC moisture curve). Flag as `SUSPECT` in the interpolated span.
- **Gap > 30 minutes**: Do not interpolate. The feature computation is halted for that sensor; features requiring that sensor return `NaN`. The model must handle NaN inputs explicitly (LightGBM handles NaN natively).

### Explicit NaN Handling Strategy

```python
# LightGBM handles NaN in input features by assigning them to the optimal
# branch at each split node. This is acceptable for most features.
# However, safety-critical features (EC, pH) should never be NaN at inference time.
# If they are, the inference pipeline should:
# 1. Log a data quality event
# 2. Return a HIGH-priority recommendation to check sensor connectivity
# 3. Suppress yield/quality predictions (return None, not a stale value)

CRITICAL_FEATURES = {'ec_current', 'ph_current', 'temperature_1h_mean', 'vpd_kpa'}

def check_critical_features(feature_dict: dict) -> list[str]:
    """Returns list of missing critical features."""
    return [f for f in CRITICAL_FEATURES if np.isnan(feature_dict.get(f, np.nan))]
```

### Sensor Drift Detection

Slow linear drift in sensor residuals (the difference between a sensor's reading and the fleet median for the same sensor type in the same room) indicates calibration drift:

```python
def detect_drift(residuals: pd.Series, window_days: int = 7) -> dict:
    """
    Fit a linear trend to the residual series.
    If slope is significant, flag as drifting.
    """
    x = np.arange(len(residuals), dtype=float)
    slope, intercept = np.polyfit(x, residuals.values, 1)
    slope_per_day = slope * (1440)  # convert from per-sample to per-day

    return {
        'drift_slope_per_day': slope_per_day,
        'drift_detected': abs(slope_per_day) > 0.05,  # threshold: 0.05 units/day
        'drift_direction': 'positive' if slope_per_day > 0 else 'negative',
    }
```

---

## Pipeline Architecture

The feature pipeline is implemented using scikit-learn's `Pipeline` and `ColumnTransformer` primitives, with custom transformers inheriting from `BaseEstimator` and `TransformerMixin`.

```
Raw sensor_readings (DB query)
    │
    ├── ResampleTransformer          # Resample to 1-min, mark gaps
    │
    ├── QualityFilterTransformer     # Mask INVALID readings, flag SUSPECT
    │
    ├── PhysicsFeaturesTransformer   # VPD, DLI accumulation
    │
    ├── ChemistryFeaturesTransformer # EC/pH deviation, drift, stability
    │
    ├── SubstrateFeaturesTransformer # VWC, field capacity, dry-back
    │
    ├── TemporalWindowTransformer    # 15min/1h/6h/24h/48h/7d stats (all sensors)
    │
    ├── StageAwareTransformer        # normalized_stage_day, days_to_harvest
    │
    ├── IrrigationEventTransformer   # shots/day, EC delivery error
    │
    ├── CrossSensorTransformer       # EC temp correction, compound stress
    │
    ├── RiskIndicatorTransformer     # exceedance minutes, composite risk score
    │
    └── NaNAuditTransformer          # Log NaN rates, enforce critical feature presence
            │
            └──> feature_dict (dict[str, float])
                        │
                        ├──> LightGBM model inference
                        └──> batch_features table (cache)
```

**Stage-Aware Branching:**

The `StageAwareTransformer` also adjusts feature scaling. For example, `normalized_stage_day` is computed relative to the current stage's expected duration. Some downstream features (risk thresholds, setpoint targets) are looked up from a stage-specific configuration YAML rather than being hardcoded.

---

## Feature Importance Feedback Loop

After each model training run, SHAP values are computed across the training set and stored in MLflow as an artifact. A scheduled job:

1. Loads the SHAP summary from the latest model version.
2. Identifies the bottom 20% of features by mean absolute SHAP value.
3. Checks if those features have been in the bottom 20% for 3 consecutive model versions.
4. If yes, creates a GitHub issue (or Jira ticket) suggesting feature removal.
5. Identifies the top 10 features and cross-checks against the feature registry for documentation completeness.

This creates a feedback loop: features that consistently lack predictive value are flagged for removal, keeping the pipeline lean. Features that are consistently high-importance are candidates for richer engineering (e.g., adding more granular window widths, or adding interaction terms).

```python
def shap_feature_selection_report(
    shap_values: np.ndarray,
    feature_names: list[str],
    model_version: str
) -> pd.DataFrame:
    mean_abs_shap = np.abs(shap_values).mean(axis=0)
    df = pd.DataFrame({
        'feature': feature_names,
        'mean_abs_shap': mean_abs_shap,
        'model_version': model_version,
    }).sort_values('mean_abs_shap', ascending=False)
    df['rank'] = range(1, len(df) + 1)
    df['bottom_20pct'] = df['rank'] > (0.8 * len(df))
    return df
```

---

## Feature Registry

All features are documented in a machine-readable registry at `config/feature_registry.yml`. Each entry specifies:

```yaml
features:
  - name: vpd_kpa
    group: physics
    sensor_types: [TEMPERATURE, HUMIDITY]
    description: "Vapour pressure deficit computed from Magnus-Tetens formula"
    unit: kPa
    expected_range: [0.0, 4.0]
    stage_specific: false
    critical: true

  - name: ec_deviation_from_setpoint
    group: chemistry
    sensor_types: [EC]
    description: "Current EC minus stage-specific EC setpoint (mS/cm)"
    unit: mS/cm
    expected_range: [-2.0, 2.0]
    stage_specific: true
    critical: true

  - name: vwc_dry_back_pct
    group: substrate
    sensor_types: [VWC]
    description: "Percentage drop in VWC from estimated field capacity"
    unit: "%"
    expected_range: [0.0, 50.0]
    stage_specific: false
    critical: false
```

The registry serves three purposes: documentation, automated range validation at inference time, and feature schema versioning (breaking changes to a feature's definition require a version bump).
