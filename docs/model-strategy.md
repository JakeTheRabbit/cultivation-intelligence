# Model Strategy Documentation

**Project**: Cultivation Intelligence
**Facility**: Legacy Ag Limited — Indoor Medicinal Cannabis, New Zealand
**Last Updated**: 2026-03-25

---

## Table of Contents

1. [Task Decomposition](#task-decomposition)
2. [Baseline Plan — LightGBM](#baseline-plan--lightgbm)
3. [Cross-Validation Strategy](#cross-validation-strategy)
4. [Feature Selection](#feature-selection)
5. [Hyperparameter Tuning](#hyperparameter-tuning)
6. [Model Registry](#model-registry)
7. [Advanced Plan — When to Upgrade](#advanced-plan--when-to-upgrade)
8. [Uncertainty Estimation](#uncertainty-estimation)
9. [Explainability](#explainability)
10. [Retraining Triggers](#retraining-triggers)
11. [Training Infrastructure](#training-infrastructure)

---

## Task Decomposition

The intelligence system addresses five distinct prediction problems. Each is formulated as a separate model task, trained independently, but sharing the same feature engineering pipeline.

### Task 1: Batch Yield Regression

**Goal**: Predict dry weight yield (grams per square metre of canopy, or grams per plant) at the point of harvest, given conditions observed up to any point during the batch.

**Why this matters**: Yield prediction at mid-flower allows early identification of at-risk batches. An estimated yield 20% below target at day 30 of flower signals that intervention is needed — adjusting EC, DLI, or VPD — while there is still time to recover.

**Prediction timing**:
- At end of VEG: first prediction with wide confidence interval
- At end of EARLY_FLOWER: primary commercial forecast
- At end of MID_FLOWER: final forecast (tightest CI)

**Target variable**: `batches.actual_yield_g / room_canopy_m2` (g/m²)

---

### Task 2: Quality Score Classification

**Goal**: Predict the quality grade (A, B, or C) of the harvested batch.

**Why this matters**: Grade determines price category in the NZ medicinal cannabis market. An early warning that a batch is trending toward B-grade (typically due to elevated VPD stress in late flower, EC excess, or light burn) enables corrective action.

**Target variable**: `batches.quality_grade` encoded as ordinal {A→2, B→1, C→0}

**Note**: With a small batch count, this may remain a 2-class problem (A vs below-A) initially, upgraded to 3-class as data accumulates.

---

### Task 3: Risk Scoring (Rolling)

**Goal**: Produce a continuous risk score (0–1) updated every 15 minutes, reflecting the probability that the current environment is causing suboptimal plant response that will affect yield or quality.

**Why this matters**: Unlike tasks 1 and 2, this is an operational signal rather than a forecast. A risk score spike at 03:00 means the on-call operator should check the system now. Risk scores > 0.7 for > 2 hours should trigger a `CRITICAL` recommendation.

**Target variable**: Constructed label — requires outcome labeling (see cross-validation notes). A batch-day is labeled "high risk" if the batch ultimately underperformed its target yield by > 15% AND the sensor data from that batch-day shows adverse patterns.

---

### Task 4: Stage Completion Prediction

**Goal**: Estimate the number of days until harvest, conditioned on current stage and observed batch trajectory.

**Why this matters**: Harvest scheduling requires 7–14 day advance notice (lab testing, harvest team, post-harvest logistics). An ML-assisted estimate reduces scheduling uncertainty.

**Target variable**: `(harvest_date - current_date).days` at each batch-day observation

---

### Task 5: Environmental Setpoint Recommendations

**Goal**: Given current batch state (features), recommend optimal VPD, EC, and DLI setpoints for the next 24 hours.

**Implementation approach**: This is not a direct regression task. Instead, it is implemented as:
1. Enumerate a grid of feasible setpoint combinations (e.g., EC: 1.6–2.8 in steps of 0.1, VPD: 0.8–2.0 in steps of 0.1)
2. For each candidate setpoint, compute predicted yield delta using Task 1 model
3. Select the setpoint combination maximising expected yield subject to:
   - Hard safety constraints (see controls-safety.md)
   - Rate-of-change limits (max EC change: ±0.3 per recommendation)
   - Current operator-set override preferences

This is an inverse use of the yield model, not a separate trained model.

---

## Baseline Plan — LightGBM

LightGBM is selected as the baseline algorithm for all tasks because:

- Handles tabular data with mixed feature types natively
- Handles NaN inputs without imputation preprocessing
- Supports quantile regression for uncertainty estimation
- Efficient training on CPU (no GPU required for this data scale)
- SHAP integration via the `shap` library is first-class
- Ensemble training (multiple seeds) is trivial

### Task 1: Yield Regression

```python
import lightgbm as lgb
from sklearn.pipeline import Pipeline
from feature_engineering import CultivationFeaturePipeline

# Input features: all features from feature registry, at end of EARLY_FLOWER stage
# ~120 features total

lgb_params_yield = {
    'objective': 'regression',
    'metric': ['rmse', 'mae'],
    'n_estimators': 500,
    'learning_rate': 0.05,
    'num_leaves': 31,          # conservative for small N
    'min_child_samples': 5,    # at least 5 batches per leaf
    'feature_fraction': 0.8,
    'bagging_fraction': 0.8,
    'bagging_freq': 5,
    'reg_alpha': 0.1,
    'reg_lambda': 0.1,
    'verbose': -1,
}

yield_pipeline = Pipeline([
    ('features', CultivationFeaturePipeline(target_stage='EARLY_FLOWER')),
    ('model', lgb.LGBMRegressor(**lgb_params_yield)),
])
```

**Evaluation Metrics:**
- MAE (primary): mean absolute error in g/m²
- MAPE: mean absolute percentage error
- RMSE: root mean squared error
- PI Coverage: proportion of actuals within [q10, q90] confidence interval (target: 80%)

**SHAP Interpretation:**
SHAP waterfall plots are generated for every prediction. The top 10 contributors are stored in `model_predictions.metadata` and surfaced in the recommendation rationale template.

**Calibration:**
Quantile estimates are calibrated using isotonic regression on a held-out calibration set (the most recent 20% of batches). Raw quantile outputs from LightGBM quantile regression tend to be slightly conservative; calibration corrects this.

---

### Task 2: Quality Classification

```python
lgb_params_quality = {
    'objective': 'multiclass',
    'num_class': 3,
    'metric': 'multi_logloss',
    'n_estimators': 300,
    'learning_rate': 0.05,
    'num_leaves': 15,          # more conservative — fewer labeled examples
    'class_weight': 'balanced', # A-grade is likely overrepresented early on
    'feature_fraction': 0.7,
}
```

**Evaluation Metrics:**
- Weighted F1 (primary): accounts for class imbalance
- Confusion matrix: surfaced in every model evaluation report
- Calibration curve: probability outputs should be reliable for downstream risk scoring

**Class Imbalance Handling:**
Early batches at Legacy Ag are expected to skew toward A/B grades as the facility matures. `class_weight='balanced'` adjusts loss weights inversely proportional to class frequency.

---

### Task 3: Risk Scoring

```python
lgb_params_risk = {
    'objective': 'binary',
    'metric': 'binary_logloss',
    'n_estimators': 200,
    'learning_rate': 0.03,
    'num_leaves': 15,
    'min_child_samples': 10,
    # Rolling 15-min windows; many observations per batch
    # Higher sample count than yield task
}
```

**Label Construction:**

```python
def construct_risk_labels(
    batch_data: pd.DataFrame,
    yield_outcomes: pd.Series,
    yield_target: pd.Series,
    risk_threshold_pct: float = 0.15
) -> pd.Series:
    """
    A batch-day is labeled 'high risk' (1) if:
    - The batch ultimately underperformed by > threshold%, AND
    - The sensor environment on this day showed adverse patterns
      (EC/VPD/pH exceedance features > 0.3 composite risk)
    """
    underperformed = (yield_outcomes / yield_target) < (1 - risk_threshold_pct)
    # Propagate batch-level outcome to all batch-days
    batch_underperformed = batch_data['batch_id'].map(underperformed)
    env_adverse = batch_data['composite_risk_score'] > 0.3
    return (batch_underperformed & env_adverse).astype(int)
```

**Evaluation Metrics:**
- Precision-Recall AUC (primary; positive class is rare)
- Alert lead time: how many hours before a yield-impacting event does risk score exceed 0.7?
- False alarm rate: fraction of CRITICAL alerts that did not precede a yield shortfall

---

### Task 4: Stage Completion Prediction

```python
lgb_params_stage = {
    'objective': 'regression',
    'metric': ['mae', 'rmse'],
    'n_estimators': 200,
    'learning_rate': 0.05,
    'num_leaves': 31,
}
```

**Evaluation Metrics:**
- MAE in days (target: < 3 days)
- Calibration of 90% PI (should contain actual harvest date 90% of the time)

---

## Cross-Validation Strategy

### The Data Leakage Problem

Standard k-fold cross-validation is invalid here for two reasons:

1. **Temporal leakage**: If batch 12 is in the training set and batch 11 is in the validation set, the model has "seen the future" relative to the validation batch's position in time.
2. **Batch-level leakage**: For the yield task, each batch contributes one label. Train/validation splits must be at the batch level, not at the sample level.

### Time-Series Split

```python
from sklearn.model_selection import TimeSeriesSplit

# Batches are ordered by start_date
# Use TimeSeriesSplit with gap to prevent leakage via overlapping aggregation windows
tscv = TimeSeriesSplit(n_splits=5, gap=1)
# gap=1: skip the batch immediately adjacent to the validation set
# (to avoid feature windows that span train/validation boundary)
```

### Leave-N-Batches-Out

For the yield regression task with very few total batches (< 30 at project start), standard TSCV may produce splits that are too small. An alternative is Leave-2-Batches-Out:

```python
def batch_level_cv_splits(batch_ids: list, n_val_batches: int = 2):
    """
    Generate train/validation splits at the batch level.
    Always validates on the most recent n_val_batches.
    Training always uses only historical data.
    """
    for i in range(n_val_batches, len(batch_ids)):
        val_batches = batch_ids[i - n_val_batches:i]
        train_batches = batch_ids[:i - n_val_batches]
        yield train_batches, val_batches
```

### Forward-Chaining CV

For the risk scoring task (which has many observations per batch), forward-chaining is applied at the observation level within the training batches:

```python
# Within each training fold, observations are split by time
# Validation observations always follow training observations in time
forward_splits = TimeSeriesSplit(n_splits=3, test_size=int(24*60/15))
# test_size = 24 hours of 15-minute observations
```

---

## Feature Selection

### Step 1: Boruta

Boruta is applied as a first-pass filter to remove features that have no predictive signal above random noise:

```python
from boruta import BorutaPy

estimator = lgb.LGBMRegressor(n_estimators=100, n_jobs=-1, verbose=-1)
boruta = BorutaPy(estimator, n_estimators='auto', verbose=0, random_state=42)
boruta.fit(X_train.values, y_train.values)
selected_features = X_train.columns[boruta.support_].tolist()
```

### Step 2: SHAP-Based Elimination

Features with mean |SHAP| < 0.001 across the validation set are candidates for removal. Removal requires confirmation across 3 consecutive model versions (see feedback loop in feature-engineering.md).

### Step 3: VIF for Collinearity

Highly collinear features degrade LightGBM less than linear models, but they inflate SHAP value variance (splitting importance across correlated features). VIF > 10 triggers a review.

```python
from statsmodels.stats.outliers_influence import variance_inflation_factor

def compute_vif(X: pd.DataFrame) -> pd.Series:
    vif = pd.Series(
        [variance_inflation_factor(X.values, i) for i in range(X.shape[1])],
        index=X.columns
    )
    return vif.sort_values(ascending=False)
```

Features with VIF > 10 are reviewed: one of the pair is dropped, preferring to keep the one with higher mean |SHAP|.

---

## Hyperparameter Tuning

### Optuna with Pruning

```python
import optuna
from optuna.integration import LightGBMPruningCallback

def objective(trial: optuna.Trial, X_train, y_train, cv_splits) -> float:
    params = {
        'objective': 'regression',
        'metric': 'mae',
        'verbosity': -1,
        'n_estimators': 1000,
        'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.1, log=True),
        'num_leaves': trial.suggest_int('num_leaves', 10, 60),
        'min_child_samples': trial.suggest_int('min_child_samples', 5, 30),
        'feature_fraction': trial.suggest_float('feature_fraction', 0.5, 1.0),
        'bagging_fraction': trial.suggest_float('bagging_fraction', 0.5, 1.0),
        'bagging_freq': trial.suggest_int('bagging_freq', 1, 10),
        'reg_alpha': trial.suggest_float('reg_alpha', 1e-4, 1.0, log=True),
        'reg_lambda': trial.suggest_float('reg_lambda', 1e-4, 1.0, log=True),
    }

    cv_scores = []
    for train_idx, val_idx in cv_splits:
        X_tr, X_val = X_train.iloc[train_idx], X_train.iloc[val_idx]
        y_tr, y_val = y_train.iloc[train_idx], y_train.iloc[val_idx]

        model = lgb.LGBMRegressor(**params)
        model.fit(
            X_tr, y_tr,
            eval_set=[(X_val, y_val)],
            callbacks=[
                lgb.early_stopping(50, verbose=False),
                LightGBMPruningCallback(trial, 'valid_0-mae'),
            ]
        )
        cv_scores.append(model.best_score_['valid_0']['l1'])

    return np.mean(cv_scores)

study = optuna.create_study(
    direction='minimize',
    pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=20)
)
study.optimize(objective, n_trials=100, timeout=3600)
```

### Search Space Rationale

- `num_leaves` capped at 60: with small N batches, deeper trees overfit aggressively
- `min_child_samples` minimum 5: at least 5 training batches per leaf node
- `learning_rate` log-uniform: small values (0.01) with more trees often outperforms large values
- Pruning: eliminates unpromising trials early (MedianPruner stops trials performing below median at intermediate steps)

---

## Model Registry

### Versioning Scheme

Models use semantic versioning: `MAJOR.MINOR.PATCH`

- **MAJOR**: Change in the set of tasks the model addresses, or fundamental algorithm change (e.g., LightGBM → TFT)
- **MINOR**: New features added, feature engineering changes, significant performance improvement
- **PATCH**: Retraining on new data with same architecture and features

Example: `yield_regressor_2.1.0` is the second architecture version, first feature set revision, trained from scratch.

### Promotion Criteria

A candidate model version is promoted to production only when:

1. **Performance gate**: Validation MAE (for regression) or weighted F1 (for classification) must beat the incumbent model by at least 5% on the held-out evaluation set.
2. **PI Coverage gate**: 80% PI coverage must be within 5% of 80% (i.e., 75%–85% empirically).
3. **No regression on any batch**: The candidate must not perform worse than the incumbent on any single batch in the evaluation set by more than 25%.
4. **Shadow mode evaluation**: New model runs in shadow mode (produces predictions, does not influence recommendations) for 7 days before promotion.
5. **Manual sign-off**: Senior operator or data team lead signs off on the promotion in the model registry.

```python
def check_promotion_criteria(
    incumbent_metrics: dict,
    candidate_metrics: dict,
    pi_coverage_empirical: float,
    pi_coverage_nominal: float = 0.80,
    improvement_threshold: float = 0.05,
    pi_tolerance: float = 0.05,
) -> tuple[bool, str]:
    primary_metric = 'mae'
    incumbent_score = incumbent_metrics[primary_metric]
    candidate_score = candidate_metrics[primary_metric]

    improvement = (incumbent_score - candidate_score) / incumbent_score
    pi_within_tolerance = abs(pi_coverage_empirical - pi_coverage_nominal) <= pi_tolerance

    if improvement < improvement_threshold:
        return False, f"Improvement {improvement:.1%} below threshold {improvement_threshold:.1%}"
    if not pi_within_tolerance:
        return False, f"PI coverage {pi_coverage_empirical:.1%} outside tolerance"
    return True, "All promotion criteria met"
```

### A/B Evaluation in Production

When a candidate model passes shadow mode, it enters an A/B period:
- 50% of recommendations are generated using the incumbent model
- 50% use the candidate
- Operator acceptance rate and outcome metrics are tracked per model version
- After 14 days, the model with higher acceptance rate and better outcome tracking is selected as the new incumbent

---

## Advanced Plan — When to Upgrade

### Triggers for Upgrading Beyond LightGBM

The baseline LightGBM approach should be retained as long as:
- Batch count < 50
- Prediction intervals are reasonably calibrated
- Operator acceptance rate is acceptable
- Training time is < 30 minutes

Upgrade triggers:

| Trigger | Threshold | Recommended Upgrade |
|---------|-----------|---------------------|
| Calibration failure | PI coverage < 65% after calibration | Conformal prediction wrapper |
| Temporal pattern complexity | SHAP shows high importance for lag features | TCN or TFT |
| Multi-horizon forecasting | Operators request 7-day ahead setpoint plans | TFT with known futures |
| Seasonality in residuals | DLI/VPD residuals show periodic patterns | NeuralProphet |
| Batch count > 100 | Scale justifies deeper models | Full DL pipeline |

### Temporal Convolutional Network (TCN)

TCN is appropriate for sensor trajectory forecasting (predicting future sensor states, not yield). Input: multivariate sensor time series over the last 48 hours. Output: predicted sensor readings for the next 4 hours. This would power "what-if" simulations for setpoint recommendations.

### Temporal Fusion Transformer (TFT)

TFT is designed for multi-horizon forecasting with known future inputs. For cultivation:
- **Known futures**: lighting schedule (on/off times are pre-programmed), irrigation schedule
- **Unknown futures**: EC uptake, VPD evolution
- **Static inputs**: strain, room_id, growth stage
- **Output horizons**: 1h, 4h, 12h, 24h ahead

TFT would replace the current "snapshot at stage boundary" yield model with a continuous, multi-step forecast.

### NeuralProphet for Seasonality

DLI accumulation and VPD both exhibit within-day periodicity (lights-on, lights-off). NeuralProphet can decompose these into trend + seasonality + residuals, making the residuals more predictable by downstream models.

---

## Uncertainty Estimation

### Quantile Regression

Three quantile models are trained in parallel for the yield regression task:

```python
models_quantile = {
    'q10': lgb.LGBMRegressor(objective='quantile', alpha=0.10, **lgb_base_params),
    'q50': lgb.LGBMRegressor(objective='quantile', alpha=0.50, **lgb_base_params),
    'q90': lgb.LGBMRegressor(objective='quantile', alpha=0.90, **lgb_base_params),
}

# Train all three
for q, model in models_quantile.items():
    model.fit(X_train, y_train)
```

The q10/q90 pair forms the 80% prediction interval. The q50 is the point estimate (preferred over mean for skewed distributions).

### Conformal Prediction Wrapper

Conformal prediction provides distribution-free coverage guarantees:

```python
from mapie.regression import MapieRegressor
from sklearn.base import clone

# Wrap the base LightGBM model with MAPIE conformal prediction
mapie = MapieRegressor(
    estimator=lgb.LGBMRegressor(**lgb_params_yield),
    method='plus',
    cv=5,
    random_state=42
)
mapie.fit(X_train, y_train)

# At inference: returns point prediction + interval
y_pred, y_pis = mapie.predict(X_test, alpha=[0.10, 0.20])  # 90% and 80% intervals
```

Conformal prediction guarantees that the 80% interval will contain the true value at least 80% of the time (marginally), without distributional assumptions. This is important when the yield distribution is non-Gaussian.

### Ensemble of 5 Models

```python
SEEDS = [42, 137, 2025, 999, 7]
ensemble_models = [
    lgb.LGBMRegressor(**{**lgb_params_yield, 'random_state': seed})
    for seed in SEEDS
]

def ensemble_predict(X, models):
    predictions = np.array([m.predict(X) for m in models])
    return {
        'mean': predictions.mean(axis=0),
        'std': predictions.std(axis=0),
        'q10': np.quantile(predictions, 0.10, axis=0),
        'q90': np.quantile(predictions, 0.90, axis=0),
    }
```

Ensemble variance is a useful signal: high variance across seeds indicates that the model is uncertain about a particular batch profile. This variance is surfaced as part of the recommendation UI.

---

## Explainability

### SHAP Waterfall Plots

For every yield prediction, a SHAP waterfall plot is generated and stored as an MLflow artifact:

```python
import shap

explainer = shap.TreeExplainer(yield_model)
shap_values = explainer.shap_values(X_inference)

# Waterfall plot for single prediction
shap.plots.waterfall(
    shap.Explanation(
        values=shap_values[0],
        base_values=explainer.expected_value,
        data=X_inference.iloc[0],
        feature_names=X_inference.columns.tolist()
    )
)
```

### Operator-Facing Explanation Templates

SHAP contributions are translated into natural language using a template system:

```python
EXPLANATION_TEMPLATES = {
    'ec_deviation_from_setpoint': {
        'positive_impact': "EC was {value:.2f} mS/cm above setpoint for {duration}, associated with +{shap:.0f} g/m² yield.",
        'negative_impact': "EC drifted {value:.2f} mS/cm above target for {duration} in {stage}. Historically associated with {shap:.0f} g/m² yield reduction.",
    },
    'vpd_exceedance_minutes_6h': {
        'negative_impact': "VPD was outside target range for {value:.0f} minutes in the last 6 hours, contributing an estimated {shap:.0f} g/m² reduction.",
    },
    'dli_accumulated_stage': {
        'positive_impact': "Cumulative DLI of {value:.1f} mol/m² in {stage} is tracking above target, associated with +{shap:.0f} g/m² yield.",
        'negative_impact': "Cumulative DLI of {value:.1f} mol/m² in {stage} is below target. Estimated yield impact: {shap:.0f} g/m².",
    },
}

def generate_explanation(shap_values: dict, feature_values: dict, stage: str) -> str:
    top_3 = sorted(shap_values.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
    sentences = []
    for feature, shap_val in top_3:
        template_set = EXPLANATION_TEMPLATES.get(feature, {})
        key = 'positive_impact' if shap_val > 0 else 'negative_impact'
        template = template_set.get(key)
        if template:
            sentences.append(template.format(
                value=feature_values.get(feature, 0),
                shap=abs(shap_val),
                stage=stage,
                duration="18h"  # computed from exceedance features
            ))
    return " ".join(sentences)
```

### Feature Contribution Summaries

A summary table is stored with every prediction:

```python
def build_shap_summary(shap_values, feature_names, feature_values, n_top=10):
    df = pd.DataFrame({
        'feature': feature_names,
        'shap_value': shap_values,
        'feature_value': [feature_values.get(f) for f in feature_names],
    })
    df['abs_shap'] = df['shap_value'].abs()
    df = df.nlargest(n_top, 'abs_shap')
    df['direction'] = df['shap_value'].apply(lambda x: 'increases_yield' if x > 0 else 'reduces_yield')
    return df
```

---

## Retraining Triggers

Retraining is triggered by any of the following conditions:

### 1. New Batch Completes

When a batch reaches `COMPLETE` status, its actual yield and quality grade are recorded. This adds one new labeled example to the training set. Retraining is scheduled 48 hours after harvest completion (to allow full data entry including quality grading).

### 2. Population Stability Index (PSI) Exceeds 0.2

PSI measures distributional shift in input features between training data and recent production data:

```python
def compute_psi(expected: np.ndarray, actual: np.ndarray, n_bins: int = 10) -> float:
    """Compute Population Stability Index."""
    # Bin edges from expected distribution
    breakpoints = np.percentile(expected, np.linspace(0, 100, n_bins + 1))
    breakpoints[0] = -np.inf
    breakpoints[-1] = np.inf

    expected_counts = np.histogram(expected, bins=breakpoints)[0] / len(expected)
    actual_counts   = np.histogram(actual,   bins=breakpoints)[0] / len(actual)

    # Replace zeros to avoid log(0)
    expected_counts = np.clip(expected_counts, 1e-6, None)
    actual_counts   = np.clip(actual_counts,   1e-6, None)

    psi = np.sum((actual_counts - expected_counts) * np.log(actual_counts / expected_counts))
    return psi

# PSI is computed weekly on the top 20 features
# PSI > 0.2 triggers a retraining alert
RETRAINING_PSI_THRESHOLD = 0.2
```

PSI interpretation: < 0.1 no significant change; 0.1–0.2 moderate shift, monitor; > 0.2 significant shift, retrain.

### 3. Operator Feedback Loop

When operators consistently reject recommendations of a particular type, the rejection pattern is logged. If the rejection rate for any recommendation type exceeds 40% over 30 days, a retraining review is triggered. This may indicate model drift or a systematic error in the feature engineering.

---

## Training Infrastructure

### Local Training Scripts

Training is executed locally on the cultivation intelligence server:

```
scripts/
├── train_yield_model.py       # Task 1
├── train_quality_model.py     # Task 2
├── train_risk_model.py        # Task 3
├── train_stage_model.py       # Task 4
├── evaluate_models.py         # Unified evaluation report
├── promote_model.py           # Registry promotion with checks
└── shadow_mode_compare.py     # A/B evaluation during shadow mode
```

### MLflow Tracking

All training runs are tracked with MLflow:

```python
import mlflow
import mlflow.lightgbm

with mlflow.start_run(run_name=f"yield_regressor_{model_version}"):
    mlflow.log_params(lgb_params_yield)
    mlflow.log_params({'cv_strategy': 'leave_2_batches_out', 'n_features': len(features)})

    # Train model
    model.fit(X_train, y_train)

    # Log metrics
    mlflow.log_metric('val_mae', val_mae)
    mlflow.log_metric('val_mape', val_mape)
    mlflow.log_metric('pi_coverage_80', pi_coverage)

    # Log model artifact
    mlflow.lightgbm.log_model(model, 'model')

    # Log SHAP summary
    mlflow.log_artifact('shap_summary.png')
    mlflow.log_artifact('feature_importance.csv')

    # Log feature registry snapshot (which features were used)
    mlflow.log_artifact('config/feature_registry.yml')
```

### Model Artifact Storage

Trained model artifacts are stored at:
```
models/
├── yield_regressor/
│   ├── 2.0.0/    # incumbent
│   └── 2.1.0/    # candidate (in shadow mode)
├── quality_classifier/
├── risk_scorer/
└── stage_completion/
```

Each version directory contains:
- `model.lgb` — LightGBM booster file
- `feature_names.json` — ordered list of expected input features
- `metadata.json` — training date, dataset fingerprint, evaluation metrics, promotion status
- `shap_explainer.pkl` — serialized SHAP TreeExplainer
- `calibration.pkl` — isotonic regression calibrator for quantile outputs
