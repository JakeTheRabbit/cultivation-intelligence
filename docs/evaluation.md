# Evaluation and Test Strategy

**Project**: Cultivation Intelligence
**Facility**: Legacy Ag Limited — Indoor Medicinal Cannabis, New Zealand
**Last Updated**: 2026-03-25

---

## Table of Contents

1. [Evaluation Philosophy](#evaluation-philosophy)
2. [Model Evaluation](#model-evaluation)
3. [Data Evaluation](#data-evaluation)
4. [Recommendation Evaluation](#recommendation-evaluation)
5. [System Evaluation](#system-evaluation)
6. [Test Strategy](#test-strategy)
7. [Acceptance Criteria for Production Promotion](#acceptance-criteria-for-production-promotion)
8. [Drift Monitoring](#drift-monitoring)
9. [Feedback Collection](#feedback-collection)

---

## Evaluation Philosophy

### Predict, Measure, Compare — Not Just Train and Deploy

Machine learning systems in production have a lifecycle that extends far beyond the training run. A model that achieved 12% MAE in cross-validation on historical data may perform very differently when integrated into a live cultivation environment with real data latency, sensor faults, new strains, and changing operational practices.

The evaluation framework for this project is built on three commitments:

**1. Every prediction is tracked.** Every inference, whether acted upon or ignored, is stored in `model_predictions`. This creates a ground truth record against which we can measure, retrospectively, how well the model performed. Yield predictions made at the end of early flower are compared against actual yield at harvest. Risk scores generated at 03:00 are compared against whether the subsequent 24-hour period produced adverse outcomes.

**2. Baselines are always included.** No model metric has meaning in isolation. A yield MAE of 18 g/m² sounds good until you learn that simply predicting the historical mean achieves 15 g/m². The evaluation framework always includes: naive mean predictor, last-batch performance predictor, and simple linear regression as baseline comparators. All deployed models must beat all baselines on primary metrics.

**3. Evaluation is continuous, not point-in-time.** A model's performance at deployment is not a guarantee of its performance 6 months later. Sensor configurations change, new strains are introduced, growing protocols evolve. The evaluation framework operates continuously in production, recomputing key metrics weekly and flagging degradation.

---

## Model Evaluation

### Task 1: Yield Regression

**Primary Metric**: Mean Absolute Error (MAE) in g/m²

MAE is preferred over RMSE for this task because yield prediction errors are not normally distributed — occasional very poor batches (due to pest events, equipment failure) produce large errors that RMSE would over-penalise in a way that is not representative of typical model behaviour.

```python
from sklearn.metrics import mean_absolute_error, mean_squared_error
import numpy as np

def evaluate_yield_model(y_true: np.ndarray, y_pred: np.ndarray,
                          y_lower: np.ndarray, y_upper: np.ndarray) -> dict:
    """
    Comprehensive yield model evaluation.

    Parameters
    ----------
    y_true : actual yield values (g/m²)
    y_pred : point predictions (q50)
    y_lower : lower PI bound (q10)
    y_upper : upper PI bound (q90)
    """
    mae  = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    mape = np.mean(np.abs((y_true - y_pred) / (y_true + 1e-6))) * 100.0

    # PI Coverage: proportion of actuals within [q10, q90]
    within_80_pi = np.mean((y_true >= y_lower) & (y_true <= y_upper))

    # PI Width: average width of the 80% interval (narrower is better, all else equal)
    mean_pi_width = np.mean(y_upper - y_lower)

    # Bias: positive = model over-predicts, negative = under-predicts
    bias = np.mean(y_pred - y_true)

    # Direction accuracy: did the model correctly rank batches?
    from scipy.stats import spearmanr
    rank_corr, rank_p = spearmanr(y_true, y_pred)

    return {
        'mae_g_per_m2': round(mae, 2),
        'rmse_g_per_m2': round(rmse, 2),
        'mape_pct': round(mape, 1),
        'pi_coverage_80': round(float(within_80_pi), 3),
        'mean_pi_width_g_per_m2': round(mean_pi_width, 2),
        'bias_g_per_m2': round(bias, 2),
        'rank_correlation_spearman': round(float(rank_corr), 3),
        'n_batches': len(y_true),
    }
```

**Baseline Comparisons:**

```python
def compute_baselines(y_true_train: np.ndarray, y_true_test: np.ndarray) -> dict:
    """Compute naive baseline predictions for comparison."""
    # Baseline 1: Predict training set mean for all test instances
    naive_mean_pred = np.full_like(y_true_test, fill_value=y_true_train.mean(), dtype=float)
    naive_mean_mae = mean_absolute_error(y_true_test, naive_mean_pred)

    # Baseline 2: Predict the most recent batch's yield (last-value predictor)
    last_value_pred = np.full_like(y_true_test, fill_value=y_true_train[-1], dtype=float)
    last_value_mae = mean_absolute_error(y_true_test, last_value_pred)

    return {
        'naive_mean_mae': round(naive_mean_mae, 2),
        'last_value_mae': round(last_value_mae, 2),
    }
```

**PI Coverage Evaluation:**

The 80% PI should empirically contain the true value 80% ± 5% of the time. If coverage is < 75%, the interval is anti-conservative (dangerous — operators may trust interval bounds that are too tight). If coverage is > 85%, the interval is overly conservative (less useful — the interval is too wide to be actionable).

```python
def calibration_check(pi_coverage: float, nominal: float = 0.80, tolerance: float = 0.05) -> str:
    if abs(pi_coverage - nominal) <= tolerance:
        return f"PASS: Empirical coverage {pi_coverage:.1%} within {tolerance:.0%} of nominal {nominal:.0%}"
    elif pi_coverage < nominal - tolerance:
        return f"FAIL (anti-conservative): Coverage {pi_coverage:.1%} below lower bound {nominal - tolerance:.0%}"
    else:
        return f"WARN (over-conservative): Coverage {pi_coverage:.1%} above upper bound {nominal + tolerance:.0%}"
```

---

### Task 2: Quality Classification

**Primary Metric**: Weighted F1 Score

Weighted F1 accounts for class imbalance by weighting each class's F1 by its support in the test set. This prevents a model that always predicts "A" from appearing to perform well.

```python
from sklearn.metrics import (
    f1_score, confusion_matrix, classification_report,
    brier_score_loss
)
from sklearn.calibration import calibration_curve
import matplotlib.pyplot as plt

def evaluate_quality_model(
    y_true: np.ndarray,
    y_pred_class: np.ndarray,
    y_pred_proba: np.ndarray,
    class_names: list = ['C', 'B', 'A']
) -> dict:
    weighted_f1 = f1_score(y_true, y_pred_class, average='weighted')
    macro_f1    = f1_score(y_true, y_pred_class, average='macro')
    cm = confusion_matrix(y_true, y_pred_class)

    # Brier score for each class (probability calibration)
    brier_scores = {}
    for i, cls_name in enumerate(class_names):
        binary_true = (y_true == i).astype(int)
        brier_scores[cls_name] = brier_score_loss(binary_true, y_pred_proba[:, i])

    return {
        'weighted_f1': round(float(weighted_f1), 3),
        'macro_f1': round(float(macro_f1), 3),
        'confusion_matrix': cm.tolist(),
        'classification_report': classification_report(y_true, y_pred_class,
                                                       target_names=class_names),
        'brier_scores': {k: round(v, 4) for k, v in brier_scores.items()},
        'n_batches': len(y_true),
    }
```

**Calibration Curve:**

A calibration curve plots predicted probability against empirical frequency. A well-calibrated model's curve lies close to the diagonal. Significant deviation indicates that probability outputs should not be used as-is for risk scoring.

---

### Task 3: Risk Scoring

**Primary Metric**: Precision-Recall AUC (PR-AUC)

ROC-AUC is misleading when the positive class (high-risk batch-days) is rare. PR-AUC is more informative for imbalanced binary tasks because it focuses on the positive class.

```python
from sklearn.metrics import (
    precision_recall_curve, auc,
    average_precision_score
)

def evaluate_risk_model(
    y_true: np.ndarray,
    y_score: np.ndarray,
    alert_threshold: float = 0.7,
    lookback_hours: int = 24
) -> dict:
    pr_auc = average_precision_score(y_true, y_score)

    # At the operating threshold (0.7)
    from sklearn.metrics import precision_score, recall_score, f1_score
    y_pred_binary = (y_score >= alert_threshold).astype(int)
    precision = precision_score(y_true, y_pred_binary, zero_division=0)
    recall    = recall_score(y_true, y_pred_binary, zero_division=0)
    f1        = f1_score(y_true, y_pred_binary, zero_division=0)

    return {
        'pr_auc': round(float(pr_auc), 3),
        'precision_at_threshold': round(float(precision), 3),
        'recall_at_threshold': round(float(recall), 3),
        'f1_at_threshold': round(float(f1), 3),
        'alert_threshold': alert_threshold,
    }
```

**Alert Lead Time Analysis:**

For each true positive alert (risk score crossed 0.7 before an adverse outcome), measure how many hours in advance the alert fired:

```python
def compute_alert_lead_times(
    risk_score_series: pd.Series,
    adverse_outcome_times: pd.DatetimeIndex,
    alert_threshold: float = 0.7,
) -> pd.Series:
    """
    For each adverse outcome, find the first preceding risk score crossing threshold.
    Returns lead time in hours.
    """
    lead_times = []
    for outcome_time in adverse_outcome_times:
        # Find first threshold crossing before the outcome
        pre_outcome = risk_score_series[risk_score_series.index < outcome_time]
        crossings = pre_outcome[pre_outcome >= alert_threshold]
        if len(crossings) > 0:
            first_crossing = crossings.index[0]
            lead_time_hours = (outcome_time - first_crossing).total_seconds() / 3600.0
            lead_times.append(lead_time_hours)
        else:
            lead_times.append(0.0)  # No advance warning
    return pd.Series(lead_times)
```

**Target**: Mean lead time > 4 hours for CRITICAL risk events.

---

### Task 4: Stage Completion Prediction

```python
def evaluate_stage_model(
    y_true_days: np.ndarray,
    y_pred_days: np.ndarray,
    y_lower: np.ndarray,
    y_upper: np.ndarray
) -> dict:
    mae_days = mean_absolute_error(y_true_days, y_pred_days)
    pi_coverage = float(np.mean((y_true_days >= y_lower) & (y_true_days <= y_upper)))

    # Directional accuracy: did the model correctly predict early vs late harvest?
    pred_direction = np.sign(y_pred_days - y_pred_days.mean())
    true_direction = np.sign(y_true_days - y_true_days.mean())
    directional_accuracy = float(np.mean(pred_direction == true_direction))

    return {
        'mae_days': round(float(mae_days), 1),
        'pi_coverage_90': round(pi_coverage, 3),
        'directional_accuracy': round(directional_accuracy, 3),
    }
```

**Target**: MAE < 3 days at mid-flower stage.

---

## Data Evaluation

### Data Quality Metrics

Data quality is evaluated weekly and reported on the monitoring dashboard:

```python
def compute_data_quality_report(
    db: Connection,
    lookback_days: int = 7,
    sensors: list[str] = None
) -> pd.DataFrame:
    """
    Compute per-sensor data quality metrics for the lookback window.
    """
    return db.read_frame("""
        WITH expected AS (
            -- Expected readings: 1 per minute per sensor = 1440/day
            SELECT
                sensor_id,
                COUNT(*) AS actual_readings,
                %s * 1440 AS expected_readings
            FROM sensor_readings
            WHERE time >= now() - INTERVAL '%s days'
            GROUP BY sensor_id
        ),
        quality_breakdown AS (
            SELECT
                sensor_id,
                COUNT(*) FILTER (WHERE quality_flag = 'OK')      AS ok_count,
                COUNT(*) FILTER (WHERE quality_flag = 'SUSPECT') AS suspect_count,
                COUNT(*) FILTER (WHERE quality_flag = 'INVALID') AS invalid_count,
                COUNT(*)                                          AS total_count
            FROM sensor_readings
            WHERE time >= now() - INTERVAL '%s days'
            GROUP BY sensor_id
        )
        SELECT
            e.sensor_id,
            e.actual_readings,
            e.expected_readings,
            ROUND(e.actual_readings * 100.0 / e.expected_readings, 1) AS completeness_pct,
            q.ok_count,
            q.suspect_count,
            q.invalid_count,
            ROUND(q.ok_count * 100.0 / NULLIF(q.total_count, 0), 1) AS ok_pct
        FROM expected e
        JOIN quality_breakdown q USING (sensor_id)
        ORDER BY completeness_pct ASC
    """, (lookback_days, lookback_days, lookback_days))
```

**Key Metrics:**

| Metric | Target | Alert Threshold |
|--------|--------|----------------|
| Completeness (readings received vs expected) | > 95% | < 90% |
| OK rate (proportion of readings with quality_flag = OK) | > 90% | < 80% |
| Max gap duration (minutes) | < 10 min | > 30 min |
| Sensor offline events (per week) | < 2 | > 5 |

### Drift Detection — PSI and KL Divergence

Population Stability Index is computed weekly for all features in the model's feature set:

```python
def weekly_drift_report(
    db: Connection,
    model_version: str,
    training_features_path: str,
) -> pd.DataFrame:
    """
    Compare the distribution of current production features against
    the training distribution captured at model training time.
    """
    import pickle
    with open(training_features_path, 'rb') as f:
        training_distributions = pickle.load(f)  # dict[feature_name, np.ndarray]

    # Fetch recent production feature values
    recent_features = db.read_frame("""
        SELECT feature_name, feature_value
        FROM batch_features
        WHERE computed_at >= now() - INTERVAL '7 days'
    """)

    psi_results = []
    for feature_name, train_dist in training_distributions.items():
        prod_values = recent_features[
            recent_features['feature_name'] == feature_name
        ]['feature_value'].dropna().values

        if len(prod_values) < 10:
            psi_results.append({'feature': feature_name, 'psi': None, 'status': 'insufficient_data'})
            continue

        psi = compute_psi(train_dist, prod_values)
        status = 'ok' if psi < 0.1 else ('warning' if psi < 0.2 else 'alert')
        psi_results.append({'feature': feature_name, 'psi': round(psi, 4), 'status': status})

    return pd.DataFrame(psi_results).sort_values('psi', ascending=False, na_position='last')
```

**KL Divergence** is computed as a complement to PSI for features with continuous distributions:

```python
from scipy.stats import entropy
from scipy.special import rel_entr

def kl_divergence(p: np.ndarray, q: np.ndarray, n_bins: int = 20) -> float:
    """KL divergence from distribution q to distribution p."""
    bins = np.linspace(min(p.min(), q.min()), max(p.max(), q.max()), n_bins + 1)
    p_hist, _ = np.histogram(p, bins=bins, density=True)
    q_hist, _ = np.histogram(q, bins=bins, density=True)
    p_hist = np.clip(p_hist, 1e-9, None)
    q_hist = np.clip(q_hist, 1e-9, None)
    return float(np.sum(rel_entr(p_hist, q_hist)))
```

### Schema Validation

Every batch of sensor readings ingested from HA is validated against a JSON schema before database insertion:

```python
from pydantic import BaseModel, validator, Field
from typing import Literal
from datetime import datetime

class SensorReadingIngest(BaseModel):
    time: datetime
    sensor_id: str = Field(min_length=1, max_length=100)
    sensor_type: Literal[
        'TEMPERATURE', 'HUMIDITY', 'VPD', 'EC', 'PH',
        'VWC', 'CO2', 'PPFD', 'FLOW_RATE'
    ]
    value: float
    unit: str
    source: Literal['HA_PUSH', 'HA_POLL', 'CSV_IMPORT', 'MANUAL_CORRECTION']
    raw_entity_id: str | None = None

    @validator('value')
    def value_must_be_finite(cls, v):
        import math
        if not math.isfinite(v):
            raise ValueError(f"Sensor value must be finite, got {v}")
        return v

    @validator('value')
    def value_within_absolute_range(cls, v, values):
        ABSOLUTE_RANGES = {
            'TEMPERATURE': (-20, 60),
            'HUMIDITY': (0, 100),
            'EC': (0, 10),
            'PH': (0, 14),
            'VWC': (0, 100),
            'CO2': (0, 10000),
            'PPFD': (0, 5000),
            'FLOW_RATE': (0, 200),
        }
        sensor_type = values.get('sensor_type')
        if sensor_type in ABSOLUTE_RANGES:
            lo, hi = ABSOLUTE_RANGES[sensor_type]
            if not (lo <= v <= hi):
                raise ValueError(f"{sensor_type} value {v} outside absolute range [{lo}, {hi}]")
        return v
```

Schema validation failures are logged but not cause the entire ingest batch to fail — individual invalid readings are rejected with a `data_quality_events` record.

---

## Recommendation Evaluation

### Operator Acceptance Rate

The primary signal for recommendation quality is whether operators accept or reject recommendations:

```python
def recommendation_acceptance_report(
    db: Connection,
    lookback_days: int = 30
) -> pd.DataFrame:
    return db.read_frame("""
        SELECT
            recommendation_type,
            priority,
            COUNT(*)                                                    AS total,
            COUNT(*) FILTER (WHERE status = 'ACCEPTED')                AS accepted,
            COUNT(*) FILTER (WHERE status = 'REJECTED')                AS rejected,
            COUNT(*) FILTER (WHERE status = 'EXPIRED')                 AS expired,
            ROUND(
                COUNT(*) FILTER (WHERE status = 'ACCEPTED') * 100.0 /
                NULLIF(COUNT(*) FILTER (WHERE status IN ('ACCEPTED', 'REJECTED')), 0), 1
            ) AS acceptance_rate_pct,
            AVG(
                EXTRACT(EPOCH FROM (reviewed_at - created_at)) / 60.0
            ) FILTER (WHERE status IN ('ACCEPTED', 'REJECTED'))        AS avg_response_min
        FROM recommendations
        WHERE created_at >= now() - INTERVAL '%s days'
        GROUP BY recommendation_type, priority
        ORDER BY total DESC
    """, (lookback_days,))
```

**Target**: Overall acceptance rate > 60%. This is not a measure of operator agreement with the system — it is a measure of recommendation relevance and timing. A recommendation that arrives at the right time with the right action will be accepted. A recommendation for an action the operator already performed, or one that is impractical at that moment, will be rejected.

### Recommendation Actionability

A recommendation is considered actionable if:
1. The suggested setpoint is within the AquaPro's operational range
2. The suggested change is ≤ the bounded automation step size (even in advisory mode, large jumps are impractical)
3. The recommendation arrives during an active cultivation period (not during a flush when nutrient management is intentionally paused)

```python
def check_recommendation_actionability(recommendation: dict, current_state: dict) -> dict:
    suggested_ec = recommendation['suggested_actions']['actions'][0].get('target_value')
    current_ec   = current_state.get('ec_current')

    if suggested_ec is None or current_ec is None:
        return {'actionable': True, 'notes': 'Non-EC recommendation, not evaluated'}

    ec_delta = abs(suggested_ec - current_ec)
    actionable = ec_delta <= 0.5  # More than 0.5 mS/cm in one step is impractical

    return {
        'actionable': actionable,
        'suggested_delta': round(ec_delta, 2),
        'notes': '' if actionable else f"Delta {ec_delta:.2f} mS/cm exceeds practical single-step limit"
    }
```

### Outcome Tracking

When a recommendation is accepted and the corresponding action is taken, the system tracks whether the recommended action achieved its intended effect:

```python
def track_recommendation_outcome(
    db: Connection,
    recommendation_id: int,
    action_time: datetime,
    target_metric: str,        # e.g. 'ec_deviation_from_setpoint'
    pre_action_value: float,
    post_action_window_hours: int = 4,
) -> dict:
    """
    Compare the target metric before and after the action was taken.
    Positive improvement = recommendation was beneficial.
    """
    # Fetch feature value at action_time + post_action_window_hours
    post_value = db.fetchone("""
        SELECT feature_value
        FROM batch_features
        WHERE feature_name = %s
          AND computed_at BETWEEN %s AND %s
        ORDER BY computed_at DESC
        LIMIT 1
    """, (
        target_metric,
        action_time + timedelta(hours=post_action_window_hours - 1),
        action_time + timedelta(hours=post_action_window_hours + 1),
    ))

    if post_value is None:
        return {'outcome_tracked': False, 'reason': 'No feature data in post-action window'}

    improvement = pre_action_value - post_value['feature_value']
    return {
        'outcome_tracked': True,
        'pre_action_value': pre_action_value,
        'post_action_value': post_value['feature_value'],
        'improvement': round(improvement, 4),
        'improved': improvement > 0,
    }
```

---

## System Evaluation

### API Latency

API latency is measured via application-level instrumentation (Prometheus metrics exported from the FastAPI service):

```python
from prometheus_client import Histogram
import time

PREDICTION_LATENCY = Histogram(
    'cultivation_prediction_latency_seconds',
    'Time to compute a full prediction payload',
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0]
)

RECOMMENDATION_API_LATENCY = Histogram(
    'cultivation_recommendation_api_latency_seconds',
    'API response time for /api/v1/recommendations endpoint',
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5]
)

def get_recommendations_endpoint(batch_id: str):
    with RECOMMENDATION_API_LATENCY.time():
        return fetch_recommendations_from_db(batch_id)
```

**Targets:**

| Metric | P50 Target | P95 Target | P99 Target |
|--------|-----------|-----------|-----------|
| Recommendation API response | < 100ms | < 250ms | < 500ms |
| Full prediction pipeline | < 1s | < 2s | < 5s |
| Feature computation | < 500ms | < 1.5s | < 3s |
| Sensor ingest (per batch) | < 50ms | < 200ms | < 500ms |

### Ingest Throughput

The sensor ingest pipeline must handle the full sensor estate at the polling interval:

```
Sensors: ~30 Zigbee sensors + AquaPro (8 entities) = ~38 sensors
Polling interval: 60 seconds
Throughput: ~38 readings/minute = ~0.63 readings/second

Peak (on WebSocket reconnect, catching up missed readings): up to 200 readings/second
```

The ingest pipeline is designed for 500 readings/second sustained throughput — 20x the nominal load, providing headroom for future sensor additions and peak reconnect events.

### Prediction Latency Target

The full inference pipeline (feature computation → model inference → recommendation generation) must complete in < 2 seconds. This supports a 15-minute feature refresh cycle with no blocking.

```python
import time

def timed_inference_pipeline(batch_id: str, db, models) -> dict:
    start = time.perf_counter()

    features = compute_features(batch_id, db)
    feature_time = time.perf_counter() - start

    predictions = run_all_models(features, models)
    inference_time = time.perf_counter() - start - feature_time

    recommendations = generate_recommendations(predictions, features)
    total_time = time.perf_counter() - start

    return {
        'predictions': predictions,
        'recommendations': recommendations,
        'timing': {
            'feature_computation_sec': round(feature_time, 3),
            'model_inference_sec': round(inference_time, 3),
            'total_sec': round(total_time, 3),
        }
    }
```

### Data Pipeline Freshness

The dashboard shows "last updated" timestamps for each sensor and each prediction. Freshness degradation (sensor data not updated in > 5 minutes) triggers a warning state on the dashboard:

```python
def compute_pipeline_freshness(db: Connection) -> dict:
    result = db.fetchone("""
        SELECT
            MAX(time) AS latest_reading,
            EXTRACT(EPOCH FROM (now() - MAX(time))) / 60.0 AS minutes_since_last_reading
        FROM sensor_readings
        WHERE time > now() - INTERVAL '1 hour'
    """)

    return {
        'latest_reading': result['latest_reading'].isoformat() if result['latest_reading'] else None,
        'minutes_since_last_reading': round(result['minutes_since_last_reading'] or 9999, 1),
        'freshness_status': (
            'ok' if (result['minutes_since_last_reading'] or 9999) < 2 else
            'warning' if (result['minutes_since_last_reading'] or 9999) < 5 else
            'critical'
        )
    }
```

---

## Test Strategy

### Unit Tests

Unit tests cover all deterministic, isolated functions. Target: 90% line coverage on `feature_engineering/`, `models/`, and `safety/` modules.

**Feature Transform Tests:**

```python
# tests/unit/test_vpd.py
import numpy as np
import pytest
from feature_engineering.physics import compute_vpd

def test_vpd_at_known_conditions():
    # At 25°C and 60% RH, VPD should be approximately 1.27 kPa
    vpd = compute_vpd(temperature_c=25.0, rh_pct=60.0)
    assert abs(vpd - 1.27) < 0.05, f"Expected ~1.27 kPa, got {vpd}"

def test_vpd_is_zero_at_100_rh():
    vpd = compute_vpd(temperature_c=25.0, rh_pct=100.0)
    assert vpd == pytest.approx(0.0, abs=1e-6)

def test_vpd_increases_with_temperature():
    vpd_low  = compute_vpd(20.0, 60.0)
    vpd_high = compute_vpd(30.0, 60.0)
    assert vpd_high > vpd_low

def test_vpd_increases_with_decreasing_humidity():
    vpd_humid = compute_vpd(25.0, 80.0)
    vpd_dry   = compute_vpd(25.0, 40.0)
    assert vpd_dry > vpd_humid

def test_vpd_never_negative():
    # Edge case: RH slightly above 100 (sensor noise)
    vpd = compute_vpd(temperature_c=22.0, rh_pct=101.0)
    assert vpd >= 0.0
```

**Safety Constraint Tests:**

```python
# tests/unit/test_safety_constraints.py
from controls.safety import SafetyConstraintChecker

def test_ec_above_maximum_is_blocked():
    checker = SafetyConstraintChecker()
    action = {'action_type': 'EC_ADJUST', 'entity_id': 'number.aquapro_ec', 'new_value': 4.0, 'zone_id': 'zone_1'}
    permitted, reason = checker.check(action)
    assert not permitted
    assert '3.5' in reason

def test_ph_below_minimum_is_blocked():
    checker = SafetyConstraintChecker()
    action = {'action_type': 'PH_ADJUST', 'entity_id': 'number.aquapro_ph', 'new_value': 4.9, 'zone_id': 'zone_1'}
    permitted, reason = checker.check(action)
    assert not permitted
    assert '5.2' in reason

def test_rate_limit_blocks_second_action():
    checker = SafetyConstraintChecker()
    action = {'action_type': 'EC_ADJUST', 'entity_id': 'number.aquapro_ec', 'new_value': 2.0, 'zone_id': 'zone_1'}
    permitted_1, _ = checker.check(action)
    checker.record_write('zone_1', 'number.aquapro_ec')
    permitted_2, reason_2 = checker.check(action)
    assert permitted_1
    assert not permitted_2
    assert 'Rate limit' in reason_2

def test_manual_override_blocks_all_actions():
    checker = SafetyConstraintChecker()
    checker.set_manual_override(True)
    action = {'action_type': 'EC_ADJUST', 'entity_id': 'number.aquapro_ec', 'new_value': 2.0, 'zone_id': 'zone_1'}
    permitted, reason = checker.check(action)
    assert not permitted
    assert 'MANUAL_OVERRIDE' in reason
```

### Integration Tests

Integration tests verify the full pipeline against a test database and a mock Home Assistant server.

**HA Mock Server:**

```python
# tests/integration/conftest.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

@pytest.fixture(scope='session')
def mock_ha_server():
    app = FastAPI()

    MOCK_STATES = {
        'sensor.aquapro_aq1ad04a42_ec': {'state': '2.1', 'attributes': {'unit_of_measurement': 'mS/cm'}},
        'sensor.aquapro_aq1ad04a42_ph': {'state': '6.0', 'attributes': {}},
        'sensor.room1_temperature': {'state': '24.5', 'attributes': {'unit_of_measurement': '°C'}},
        'sensor.room1_humidity': {'state': '62.0', 'attributes': {'unit_of_measurement': '%'}},
    }

    @app.get('/api/states/{entity_id}')
    def get_state(entity_id: str):
        return MOCK_STATES.get(entity_id, {'state': 'unavailable'})

    return TestClient(app)
```

**DB Write/Read Integration Test:**

```python
def test_sensor_reading_round_trip(test_db, mock_sensor_reading):
    """Verify that a sensor reading can be written and re-read with the same values."""
    from ingest.writer import write_sensor_readings
    write_sensor_readings(test_db, [mock_sensor_reading])

    retrieved = test_db.fetchone(
        "SELECT * FROM sensor_readings WHERE sensor_id = %s ORDER BY time DESC LIMIT 1",
        (mock_sensor_reading.sensor_id,)
    )
    assert retrieved is not None
    assert abs(retrieved['value'] - mock_sensor_reading.value) < 1e-6
    assert retrieved['quality_flag'] == 'OK'
```

### Property-Based Tests

The `hypothesis` library is used to generate edge-case inputs for feature transforms:

```python
# tests/property/test_feature_properties.py
from hypothesis import given, assume, settings
from hypothesis import strategies as st
from feature_engineering.physics import compute_vpd, compute_dli_from_measurements
import numpy as np

@given(
    temperature_c=st.floats(min_value=10.0, max_value=40.0, allow_nan=False),
    rh_pct=st.floats(min_value=0.0, max_value=100.0, allow_nan=False),
)
def test_vpd_is_always_non_negative(temperature_c, rh_pct):
    vpd = compute_vpd(temperature_c, rh_pct)
    assert vpd >= 0.0

@given(
    temperature_c=st.floats(min_value=10.0, max_value=40.0, allow_nan=False),
    rh_pct=st.floats(min_value=0.0, max_value=100.0, allow_nan=False),
)
def test_vpd_monotonic_in_temperature(temperature_c, rh_pct):
    """Higher temperature always produces higher or equal VPD at same RH."""
    assume(temperature_c < 39.9)
    vpd_low  = compute_vpd(temperature_c, rh_pct)
    vpd_high = compute_vpd(temperature_c + 0.1, rh_pct)
    assert vpd_high >= vpd_low - 1e-10  # allow tiny float error

@given(
    ppfd_values=st.lists(st.floats(min_value=0, max_value=2000), min_size=60, max_size=1440),
)
def test_dli_is_always_non_negative(ppfd_values):
    import pandas as pd
    series = pd.Series(ppfd_values)
    dli = compute_dli_from_measurements(series, None, sampling_interval_sec=60)
    assert dli >= 0.0
```

### Acceptance Tests

Acceptance tests verify that the full system, given historical batch data, produces sensible recommendations:

```python
# tests/acceptance/test_recommendation_sanity.py

def test_high_ec_produces_reduce_recommendation(historical_batch_fixture):
    """
    Given a batch where EC has been consistently above setpoint for 24h,
    the system should produce a recommendation to reduce EC.
    """
    batch_id = historical_batch_fixture['high_ec_batch']

    # Run the full inference pipeline on this batch's historical data
    recommendations = run_inference_pipeline(batch_id)

    # At least one recommendation should be EC-related
    ec_recs = [r for r in recommendations if 'EC' in r['recommendation_type']]
    assert len(ec_recs) > 0, "Expected EC recommendation for batch with elevated EC"

    # The EC recommendation should suggest a decrease
    ec_rec = ec_recs[0]
    action = ec_rec['suggested_actions']['actions'][0]
    assert action['target_value'] < action['current_value'], (
        "Expected recommendation to decrease EC"
    )

def test_good_conditions_produce_no_critical_alerts(historical_batch_fixture):
    """
    Given a batch with ideal environmental conditions throughout,
    the system should not produce any CRITICAL priority recommendations.
    """
    batch_id = historical_batch_fixture['ideal_conditions_batch']
    recommendations = run_inference_pipeline(batch_id)
    critical_recs = [r for r in recommendations if r['priority'] == 'CRITICAL']
    assert len(critical_recs) == 0
```

---

## Acceptance Criteria for Production Promotion

A model version is promoted to production when ALL of the following criteria are satisfied:

| Criterion | Threshold | Measured On |
|-----------|-----------|-------------|
| Yield MAE | < 15% MAPE | Held-out evaluation batches |
| Quality weighted F1 | > 0.65 | Held-out evaluation batches |
| Risk PR-AUC | > 0.60 | Held-out batch-day observations |
| PI coverage (80%) | Within 75%–85% empirically | Held-out evaluation batches |
| Safety constraint violations in shadow mode | 0 | 30-day shadow period |
| Operator acceptance rate | > 50% | 30-day shadow period (tracked but advisory) |
| All unit tests passing | 100% | CI pipeline |
| All integration tests passing | 100% | CI pipeline |
| No regression on incumbent | Must not be worse on any single batch by > 25% | Held-out evaluation batches |
| Performance gate vs incumbent | Must beat by ≥ 5% on primary metric | Held-out evaluation batches |

### Production Promotion Checklist

```
[ ] Model training completed without errors
[ ] MLflow run logged with all metrics and artifacts
[ ] Evaluation script run and metrics recorded in model registry
[ ] All promotion thresholds met (see table above)
[ ] 14-day shadow mode completed with zero safety constraint violations
[ ] Operator acceptance rate tracked during shadow mode
[ ] Model registered in models/ directory with metadata.json
[ ] Data team lead sign-off
[ ] Facility manager notification
[ ] Promotion date and version logged in CHANGELOG
```

---

## Drift Monitoring

### PSI Monitoring Schedule

PSI is computed for all 20 highest-importance features (by SHAP rank in the current model) every 7 days:

```python
def weekly_psi_monitor(db, training_distributions, model_version):
    drift_report = weekly_drift_report(db, model_version, training_distributions)

    # Alert on any feature with PSI > 0.2
    alerts = drift_report[drift_report['psi'] > 0.2]
    if len(alerts) > 0:
        send_drift_alert(
            features=alerts['feature'].tolist(),
            psi_values=alerts['psi'].tolist(),
        )
        log_retraining_recommendation(db, reason='PSI_THRESHOLD_EXCEEDED', details=alerts.to_dict())

    # Warning on any feature with PSI in [0.1, 0.2]
    warnings = drift_report[(drift_report['psi'] >= 0.1) & (drift_report['psi'] <= 0.2)]
    if len(warnings) > 0:
        log_drift_warning(db, warnings)

    return drift_report
```

### Model Prediction Distribution Tracking

The distribution of model outputs (predicted yields, risk scores) is tracked and compared to the training-time output distribution:

```python
def monitor_prediction_distribution(db, model_version, lookback_days=30):
    recent_preds = db.read_frame("""
        SELECT value, prediction_type
        FROM model_predictions
        WHERE model_version = %s
          AND time >= now() - INTERVAL '%s days'
    """, (model_version, lookback_days))

    for pred_type in recent_preds['prediction_type'].unique():
        values = recent_preds[recent_preds['prediction_type'] == pred_type]['value'].values
        # Compare to training-time distribution stored in model metadata
        training_distribution = load_training_output_distribution(model_version, pred_type)
        psi = compute_psi(training_distribution, values)
        if psi > 0.2:
            send_prediction_drift_alert(pred_type, psi)
```

### Per-Feature Drift Tracking

Individual sensor features are tracked over rolling 30-day windows. A sensor that exhibits consistent upward or downward drift (not just distributional shift) may indicate a calibration issue rather than a genuine environmental change:

```python
def detect_sensor_calibration_drift(db, sensor_id, lookback_days=30):
    """
    Detect slow linear drift in sensor readings vs the fleet median.
    This indicates calibration issues, not genuine environmental change.
    """
    fleet_median = db.fetchone("""
        SELECT time_bucket('1 hour', time) AS hour,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY value) AS median_value
        FROM sensor_readings
        WHERE sensor_type = (
            SELECT sensor_type FROM sensor_readings WHERE sensor_id = %s LIMIT 1
        )
          AND quality_flag = 'OK'
          AND time >= now() - INTERVAL '%s days'
        GROUP BY hour
    """, (sensor_id, lookback_days))
    # ... compute residuals and linear slope
```

---

## Feedback Collection

### Logging Schema for Accepted/Rejected Recommendations

```python
class RecommendationFeedback(BaseModel):
    recommendation_id: int
    operator_id: str
    decision: Literal['ACCEPTED', 'REJECTED', 'DEFERRED']
    rejection_reason: str | None = None   # required if REJECTED
    operator_notes: str | None = None
    timestamp: datetime
```

Rejection reasons are selected from a controlled vocabulary to enable analysis:

```
ALREADY_DONE         - Operator had already taken this action before seeing recommendation
DISAGREE_TIMING      - Action makes sense but not right now
DISAGREE_MAGNITUDE   - Direction is right, but suggested magnitude is too large/small
DISAGREE_DIRECTION   - Disagree with the recommendation entirely
SENSOR_ISSUE         - Suspect the triggering sensor is faulty
IMPRACTICAL          - Cannot be performed due to operational constraints
OTHER                - Free text only
```

### Outcome Attribution Methodology

Attributing outcomes to recommendations is inherently difficult because:
1. Multiple actions occur simultaneously
2. Plant response to environmental changes has a 24–72 hour lag
3. Confounding factors (strain variation, batch age) affect outcomes

The attribution methodology uses a difference-in-differences approach:

1. For each accepted recommendation, identify a comparable batch from the historical dataset that had similar conditions but where the recommended action was not taken.
2. Compare the metric trajectory (EC deviation, VPD stability) in the 48 hours following the action between the two batches.
3. Estimate the treatment effect as the difference in metric improvement.

This is an approximation — true causal attribution requires a controlled experiment. The methodology is documented explicitly so operators understand its limitations. The attribution results are used to improve recommendation templates and feature engineering, not as a hard signal for model training.

```python
def estimate_recommendation_treatment_effect(
    db: Connection,
    recommendation_id: int,
    counterfactual_batch_ids: list[str],
    target_metric: str,
    post_action_hours: int = 48,
) -> dict:
    """
    Estimate the effect of accepting a recommendation using a
    matched comparison group approach.
    """
    # Fetch metric trajectory for the treated batch (recommendation accepted)
    treated = fetch_metric_trajectory(db, recommendation_id, target_metric, post_action_hours)

    # Fetch metric trajectories for comparable batches (no action taken)
    counterfactual = [
        fetch_metric_trajectory_for_batch(db, b, target_metric, post_action_hours)
        for b in counterfactual_batch_ids
    ]
    counterfactual_mean = np.mean([cf['change'] for cf in counterfactual])

    return {
        'treated_change': treated['change'],
        'counterfactual_mean_change': counterfactual_mean,
        'estimated_effect': treated['change'] - counterfactual_mean,
        'n_counterfactuals': len(counterfactual_batch_ids),
        'confidence': 'low' if len(counterfactual_batch_ids) < 5 else 'moderate',
    }
```
