# ADR-0003: LightGBM Baseline Before Deep Learning Models

**Status:** Accepted
**Date:** 2025-01-17
**Deciders:** Engineering Lead, Head Grower
**Supersedes:** N/A
**Superseded by:** N/A (trigger for revisitation: ≥ 30 completed batches, Phase 3 prerequisites met)

---

## Context and Problem Statement

The cultivation intelligence system must make predictions about the outcome of in-progress cannabis cultivation batches — specifically, expected yield (g/plant) and quality band — based on environmental sensor data accumulated during the grow cycle. It must also generate risk scores that quantify how much the current environmental conditions deviate from optimal.

The modelling decision requires choosing between two broad families of approaches:

1. **Gradient boosted trees (LightGBM, XGBoost):** Classical ensemble methods that operate on tabular feature vectors. Require explicit feature engineering to represent temporal structure. Well-understood, interpretable via SHAP, fast to train and serve.

2. **Deep learning sequence models (LSTM, Temporal Convolutional Networks, Temporal Fusion Transformer):** Neural network architectures designed to learn representations directly from raw time-series data. Can learn temporal dependencies without explicit feature engineering. Require significantly more data to generalise well. Computationally intensive to train.

The choice is not permanent — this ADR governs the Phase 1 baseline approach and establishes explicit criteria for when deep learning models are justified.

### The Data Reality at Project Start

At the time Phase 1 begins, the available training data will consist of:
- Estimated 5–15 completed batches with full environmental sensor data from the commissioning of the HA integration
- Potentially additional historical batches imported from CSV records, with varying data quality and completeness
- Each batch produces one labelled outcome (yield, quality band)
- Each batch spans 8–14 weeks of daily environmental readings

This is a small dataset by any measure. The median ML practitioner's intuition about what is "enough" data is calibrated on benchmark datasets with thousands to millions of samples. Cultivation batches are expensive and slow to produce; 10 batches represents 1–2 years of facility operation.

---

## Decision Drivers

1. **Sample efficiency:** The chosen model family must learn meaningful generalisations from ≤ 15 training examples.
2. **Interpretability:** Operators must understand why the model makes a given prediction. "Black box" predictions that cannot be explained will not be trusted and will not be acted on. This is non-negotiable for Phase 1 and 2.
3. **Training speed:** The model retraining cycle runs weekly. Training must complete in minutes, not hours, to avoid blocking the weekly maintenance window.
4. **Serving latency:** The daily prediction job must complete in reasonable time without GPU hardware.
5. **Operational simplicity:** No GPU server, no CUDA dependencies, no complex training infrastructure in Phase 1.
6. **Domain knowledge integration:** Feature engineering is the mechanism for encoding Head Grower knowledge into the model — knowledge about which environmental parameters matter, how to compute VPD, what rolling windows are agronomically meaningful. This is a significant advantage at small data scales.
7. **Upgrade path preservation:** The Phase 1 choice must not create architectural coupling that prevents a clean upgrade to deep learning models in Phase 3.
8. **Scientific basis:** The decision should be grounded in published literature on model selection for tabular data with limited samples, not intuition or trend-following.

---

## Considered Options

### Option 1: LightGBM with Engineered Temporal Features (Chosen)

Train a LightGBM regression model to predict yield and a LightGBM classifier to predict quality band, using a tabular feature vector derived by aggregating the sensor time-series into per-phase rolling statistics.

**Feature engineering approach:**
- For each sensor entity and each growth phase (propagation, veg, early_flower, late_flower), compute: mean, standard deviation, minimum, maximum, P5, P95, and the number of hours above/below agronomic thresholds
- Derived features: VPD time-series summary statistics, DLI (daily light integral) accumulated per phase, temperature differential (canopy vs ambient) statistics
- Phase duration features: days in each phase, total days to prediction point, days remaining to expected harvest
- Interaction features: mean humidity × mean temperature (as a proxy for VPD stability), CO2 deviation × phase (CO2 during flower matters more than during veg)

**Model specifics:**
- LightGBM with cross-validation (leave-one-out when batch count < 15, 5-fold otherwise)
- Hyperparameter tuning via Optuna with 50 trials (fast given small data)
- SHAP TreeExplainer for feature attribution
- Model registry with versioned storage

### Option 2: LSTM or GRU (Sequence Model)

Train an LSTM or GRU to process the raw sensor time-series directly, learning temporal representations without explicit feature engineering. The model would ingest a fixed-length window of sensor readings (e.g., last 7 days) and output a yield prediction.

**Why this is premature in Phase 1:**
- Requires substantially more data to avoid overfitting (typical LSTM minimum: hundreds of sequences)
- No GPU available in Phase 1 infrastructure; LSTM training on CPU for long sequences is slow
- Predictions are not natively interpretable; SHAP integration for LSTMs exists but is less stable than for tree models
- Raw sensor sequences require careful preprocessing (imputation of gaps, normalisation per entity) that adds pipeline complexity

### Option 3: Temporal Fusion Transformer (TFT)

The state-of-the-art architecture for multi-horizon time-series forecasting (Lim et al., 2021). Combines variable selection networks, gating mechanisms, and multi-head attention to process multiple time-series inputs with known and unknown future inputs.

**Why this is premature in Phase 1:**
- TFT is designed for multi-horizon forecasting (predicting future time-steps), not for tabular outcome regression
- Training complexity is high; convergence requires careful learning rate scheduling and gradient clipping
- Data requirements for TFT are substantially higher than LightGBM
- Not appropriate as a Phase 1 baseline — it should be the Phase 3 upgrade target for trajectory forecasting

### Option 4: Linear Regression with Regularisation (Ridge/LASSO)

A simple linear model with L1 or L2 regularisation. Highly interpretable, extremely sample-efficient, and trivially fast to train.

**Why not chosen:**
- Linear models cannot capture non-linear interactions between environmental parameters (e.g., the combined effect of high temperature AND high humidity creating stress that neither alone would)
- With proper feature engineering (interaction terms, polynomial features), linear models can approximate non-linearities, but this becomes cumbersome and defeats the purpose of using a model
- LightGBM with low depth and high regularisation is effectively a non-linear ridge regression with automatic feature interaction discovery — it dominates the linear baseline in the same sample efficiency regime

---

## Decision

**Option 1 — LightGBM with engineered temporal features — is chosen for Phase 1.**

Deep learning sequence models are explicitly gated behind Phase 3 prerequisites (≥ 30 completed batches, Phase 2 trust-building complete). The decision to upgrade is governed by the criteria in this document, not by the availability of new model architectures.

---

## Scientific Basis

### Grinsztajn et al. (2022) — "Why Tree-Based Models Still Outperform Deep Learning on Tabular Data"

This NeurIPS 2022 paper provides the most rigorous contemporary comparison of gradient boosted trees vs deep learning on tabular data. Key findings relevant to this decision:

- **Tree models outperform deep learning on medium-sized tabular datasets** (< 50,000 samples) in the majority of benchmarks, across a wide variety of dataset types
- The performance gap narrows but does not close as dataset size grows to 10,000+ samples
- Deep learning models are more sensitive to the presence of uninformative features; tree models are more robust
- The inherent inductive bias of tree models (learning piecewise constant functions, equivalent to axis-aligned splits) is well-matched to the kind of threshold effects present in plant physiology (e.g., temperature above 28°C → stress; temperature below 18°C → stress; optimal between 20–26°C)

The cultivation outcome prediction task is a tabular regression problem with engineered features — exactly the domain where this paper's findings apply most directly.

### Sample Efficiency Argument

The fundamental challenge at Phase 1 is not model architecture; it is data volume. With 10 completed batches:
- An LSTM trained on 10 sequences will memorise those 10 batches. Its generalisation error on a new batch will be dominated by overfitting, not architecture quality.
- A LightGBM model trained on 10 samples with regularisation can produce useful predictions because the tabular feature representation is compact and the tree structure limits memorisation capacity.

This is not a claim that LightGBM is universally superior to deep learning. It is a claim that **at the data scale available in Phase 1, LightGBM's sample efficiency advantage outweighs any representational advantage of deep learning**.

The crossover point — where deep learning's capacity to learn complex temporal representations provides a net benefit — depends on many factors but is unlikely to be below 30 batches for this task. Phase 3 is gated at ≥ 30 batches precisely for this reason.

### Feature Engineering as Domain Knowledge Encoding

In the low-data regime, feature engineering is not a workaround — it is a principled mechanism for incorporating domain expertise that the model cannot learn from data alone.

The Head Grower knows that:
- VPD is more important than temperature or humidity individually
- The variance of temperature over 24 hours (not just the mean) predicts stress
- CO2 concentration during the first 3 weeks of flower has disproportionate impact on final yield
- DLI accumulated over the batch (not just instantaneous PPFD) predicts cannabinoid accumulation

None of these relationships can be learned from 10 training examples. But if they are encoded as features, the model can discover their relative importance and learn the precise threshold effects. Feature engineering transforms Head Grower expertise into model structure, bridging the gap between small data and large domain knowledge.

### Interpretability as a Hard Requirement

LightGBM models are explainable via SHAP TreeExplainer, which provides:
- **Global feature importance:** Which parameters most influence yield across all batches
- **Local explanations:** For any specific prediction, which features are pushing the prediction up or down and by how much

SHAP values are used in two critical ways:
1. **Operator trust:** When the dashboard shows a recommendation or prediction, the operator can see the explanation ("High VPD variability over the last 24 hours is the primary driver of this elevated risk score"). Without this, operators cannot evaluate whether to trust the prediction.
2. **Model debugging:** When a prediction is wrong, SHAP values allow the engineering team to identify which features are causing the error — is it a feature engineering bug? A sensor drift problem? A genuine novel batch condition?

SHAP explanations for LSTM or TFT models exist (SHAP GradientExplainer, integrated gradients) but are less stable, harder to interpret for non-technical operators, and significantly more expensive to compute.

### Training Time and Infrastructure

LightGBM training on a dataset of 15 batches × ~200 features with Optuna hyperparameter search:
- Training time: 30–90 seconds
- Memory requirement: < 500 MB
- GPU: not required

LSTM training on equivalent data with proper early stopping:
- Training time: 5–30 minutes on CPU (depending on sequence length and architecture)
- Memory requirement: 1–4 GB depending on batch size and hidden dimensions
- GPU: recommended; without GPU, weekly retraining may conflict with normal operations

The operational simplicity of LightGBM is not a trivial consideration. Weekly retraining must be reliable and unobtrusive. A training job that fails silently or takes too long causes operational problems.

---

## Rationale for Phase 3 Deep Learning Gate

Deep learning models are justified in Phase 3 because the **task changes**, not just because the data grows.

Phase 1 task: *Given the environmental history of a batch so far, predict the final yield.*

Phase 3 task: *Given the current environmental state and trajectory, forecast how sensor conditions will evolve over the next 6–24 hours, and predict how much the evolving trajectory will diverge from the optimal profile.*

The Phase 3 task is fundamentally a **multi-step sequence-to-sequence forecasting** task. This is exactly what TFT and TCN architectures are designed for. The Phase 1 LightGBM approach, which collapses the time series into a static feature vector, cannot produce a trajectory forecast — it produces a single point estimate.

The requirements for Phase 3 are:
- **Multi-horizon output:** Predictions at 1h, 6h, 24h horizons simultaneously
- **Uncertainty quantification:** Quantile predictions that allow confidence intervals around the trajectory
- **Temporal dependencies:** The model must learn how the sequence of environmental conditions (not just their statistical summary) influences future conditions

These requirements genuinely justify the complexity of a deep learning approach — but only when 30+ batches of rich time-series data are available to train it.

---

## Upgrade Path

### API Compatibility

The prediction API is designed to be model-agnostic:

```python
# Current (Phase 1): LightGBM
GET /api/v1/batches/{batch_id}/prediction
→ {
    "batch_id": "LAL-B025",
    "yield_g_per_plant": 85.3,
    "yield_ci_lower": 67.0,
    "yield_ci_upper": 103.6,
    "quality_band": "mid",
    "model_version": "yield_lgbm_v3",
    "prediction_date": "2025-03-01"
  }

# Phase 3 addition (TFT trajectory):
GET /api/v1/batches/{batch_id}/trajectory
→ {
    "batch_id": "LAL-B025",
    "forecasts": [
      {"horizon_hours": 1, "entity_id": "sensor.room1_vpd", "p10": 0.8, "p50": 0.9, "p90": 1.1},
      {"horizon_hours": 6, "entity_id": "sensor.room1_vpd", "p10": 0.7, "p50": 1.0, "p90": 1.4},
      ...
    ],
    "model_version": "trajectory_tft_v1"
  }
```

The Phase 1 prediction endpoint remains unchanged in Phase 3. The TFT is added as a new endpoint for trajectory forecasting. Both the LightGBM and TFT models run in parallel during Phase 3 (A/B comparison). The LightGBM model is only retired if TFT demonstrates a significant performance advantage on the Phase 1 task (MASE improvement > 10% — see roadmap Phase 3 exit criteria).

### Feature Pipeline Compatibility

The Phase 1 feature pipeline computes rolling statistics and caches them in Redis. The TFT model will need raw time-series windows (not aggregated statistics). This requires adding a raw sequence retrieval path to the model inference code — the existing feature cache is not used for TFT. Both paths can coexist.

### Model Registry Compatibility

The versioned model registry structure accommodates multiple model types:

```
models/
├── yield/
│   ├── lgbm/
│   │   └── current -> v5/
│   └── tft/                    # Added in Phase 3
│       └── current -> v1/
└── trajectory/                 # New in Phase 3
    └── tft/
        └── current -> v1/
```

No breaking changes to the registry structure are required.

---

## When Deep Models Are Justified: Summary Criteria

| Criterion | Phase 1 State | Phase 3 Threshold |
|---|---|---|
| Training batch count | 5–15 | ≥ 30 |
| Task type | Static tabular outcome prediction | Multi-step trajectory forecasting |
| Interpretability requirement | High (operator trust building) | High (SHAP for trees) + Temporal attention (TFT) |
| Infrastructure | CPU only | CPU (acceptable), GPU (preferred) |
| Training time budget | < 5 minutes | < 4 hours |
| Team capacity | 1 developer | 1–2 developers |
| Phase 2 status | Not yet complete | Complete and sustained for 60+ days |

---

## Consequences

### Positive

1. **Reliable predictions from limited data:** LightGBM produces meaningful predictions from 5–15 training examples where deep learning models would overfit.
2. **Fast weekly retraining:** 30–90 second training time enables weekly retraining without operational impact.
3. **SHAP explanations from day 1:** Operator trust building via explainability is available immediately, not after Phase 3.
4. **No GPU dependency:** Phase 1 infrastructure does not require GPU hardware.
5. **Feature engineering captures domain knowledge:** Head Grower expertise is encoded durably in the feature pipeline, independent of model changes.
6. **Preserved upgrade path:** TFT integration in Phase 3 does not require changes to the prediction API.

### Negative

1. **Cannot produce trajectory forecasts:** LightGBM with aggregate features cannot predict future sensor trajectories. This limits early-warning capability until Phase 3.
2. **Feature engineering maintenance burden:** The feature pipeline encodes assumptions that must be updated if the cultivation protocol changes (new strains, new rooms, protocol changes). A deep learning model with learned representations would adapt automatically.
3. **Manual feature selection:** Important features that the Head Grower has not thought to encode will be missed. A deep model operating on raw sequences would discover these implicitly. This gap narrows as batch count grows.
4. **Phase 1 model may not be competitive with Phase 3 model on trajectory tasks:** This is expected and acceptable — Phase 1 sets a baseline, not a ceiling.

---

*This ADR references: Grinsztajn, Oyallon & Varoquaux (2022) "Why tree-based models still outperform deep learning on tabular data", NeurIPS 2022; Lim, Arık, Loeff & Pfister (2021) "Temporal Fusion Transformers for Interpretable Multi-horizon Time Series Forecasting", International Journal of Forecasting; and Prokhorenkova, Gusev, Vorobev, Dorogush & Gulin (2018) "CatBoost: unbiased boosting with categorical features" for context on gradient boosted tree architectures.*
