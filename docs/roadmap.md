# Cultivation Intelligence — Product Roadmap
## Legacy Ag Limited | Indoor Medicinal Cannabis | New Zealand

**Version:** 1.1
**Date:** 2025-01-20
**Status:** Active
**Owner:** Engineering Lead + Head Grower

---

## Roadmap Philosophy

This roadmap is designed around a single governing principle: **operator trust must be earned before any system capability is expanded**. The system begins as a purely passive observer, graduates to an advisor, and only approaches bounded automation after demonstrating sustained, reliable, useful advisory outputs.

Each phase has hard exit criteria. No phase begins until the previous phase's exit criteria are formally signed off by both the Engineering Lead and the Head Grower. Phase dates are indicative — exit criteria gate advancement, not calendar time.

The sequencing reflects practical realities of an early-stage ML system at a single facility:
- You have limited historical data; the models must earn their accuracy
- The operators are domain experts; the system must earn their trust
- The crop has real commercial and regulatory value; stability trumps velocity
- One to two developers are maintaining this; operational simplicity is a feature

---

## Phase Summary

| Phase | Name | Weeks | Primary Goal | Gate Owner |
|---|---|---|---|---|
| 0 | Foundation | 1–4 | Reliable data pipeline | Engineering Lead |
| 1 | Intelligence Baseline | 5–10 | Working ML, risk scores | Engineering Lead + Head Grower |
| 2 | Operator Trust | 11–18 | Recommendation adoption | Head Grower |
| 3 | Advanced Modeling | 19–30 | Trajectory forecasting | Engineering Lead |
| 4 | Bounded Automation | 31–52+ | Safe, narrow automation | Facility Manager + Legal |

---

## Phase 0 — Foundation (Weeks 1–4)

### Goal

Build a reliable, observable data pipeline. Before any intelligence can be developed, the system must ingest, store, and serve grow environment data without loss or corruption. The foundation must be solid enough that operators can trust the data they see in the dashboard.

### Deliverables

- [ ] Git repository with CI (linting, type checks, basic tests)
- [ ] TimescaleDB schema: `sensor_readings` hypertable, `batches`, `batch_phases`, `sensor_entities`, `data_quality_events` tables
- [ ] TimescaleDB continuous aggregates for 1h, 6h, 24h rollups
- [ ] Home Assistant ingest: webhook receiver + polling fallback, entity validation, unit normalisation
- [ ] AquaPro sensor integration (water quality: EC, pH, DO, water temperature)
- [ ] CSV historical data import script (`scripts/export_features.py`)
- [ ] FastAPI service with health check endpoints (`/health`, `/health/ha`, `/health/db`)
- [ ] Basic sensor status dashboard (last-seen timestamps, entity inventory)
- [ ] Batch CRUD API (create, read, update batch records and phase transitions)
- [ ] Automated daily backup to `/backups/timescaledb/`
- [ ] Docker Compose production configuration with restart policies
- [ ] Ops runbook (this system's documentation)
- [ ] `.env.example` with all required variables documented

### Key Technical Decisions Made in This Phase

- **TimescaleDB** as the single persistent store for both time-series and relational data (see ADR-0002)
- **FastAPI** monolith with internal module boundaries (see ADR-0001)
- **Advisory-only architecture** — no control integration in Phase 0 (see ADR-0004)
- **LightGBM** as the baseline model when data is available (see ADR-0003)

### Exit Criteria

All of the following must be demonstrated before Phase 1 begins:

1. System ingests 30 days of Home Assistant sensor data without measurable loss (< 1% missing readings vs HA history)
2. Data quality score across all monitored entities is > 95% (measured by `scripts/data_quality_report.py`)
3. `/health` API endpoint returns `"status": "healthy"` with all sub-checks passing
4. AquaPro water quality readings are appearing in the database with < 5% gap rate
5. Historical CSV import of at least 2 completed batch records has been performed successfully
6. Backup and restore procedure has been tested end-to-end on a test database
7. Head Grower has reviewed the dashboard sensor feeds and confirmed they match physical reality

### Risks in This Phase

- **HA API stability:** HA version upgrades can break webhook formats. Pin HA version and test before upgrading.
- **AquaPro integration complexity:** Proprietary sensor protocol may require custom integration work. Allocate buffer time.
- **Historical data quality:** Imported CSV data may have inconsistent timestamps or units from manual export processes.

---

## Phase 1 — Intelligence Baseline (Weeks 5–10)

### Goal

Transform the data pipeline into an intelligent monitoring system. Train the first predictive models on available batch history, produce risk scores that reflect real environmental stress, and present predictions and risk information to operators through a functional dashboard.

### Deliverables

- [ ] Feature engineering pipeline:
  - Rolling statistics: mean, std, min, max, percentile (P5, P95) over 1h, 6h, 24h windows per entity
  - VPD computation from temperature + humidity (where not already provided by HA)
  - Phase-aware features (day of phase, days to harvest)
  - Inter-entity ratios and derived features (e.g., leaf-to-air temperature delta)
  - Feature cache in Redis with 15-minute TTL
- [ ] LightGBM batch outcome model:
  - Target: final yield (g/plant), cannabinoid potency band (low/mid/high)
  - Feature set: environment statistics aggregated over each growth phase
  - Training pipeline with cross-validation and held-out test set
  - Model registry: versioned storage of trained models with training metadata
- [ ] Risk scoring engine:
  - Per-parameter deviation scoring against phase-specific target profiles (configurable in `config/target_profiles.yaml`)
  - Weighted composite risk score 0–100
  - Risk score persisted to database every 15 minutes
  - Configurable alert thresholds
- [ ] Prediction API:
  - `GET /api/v1/batches/{batch_id}/prediction` — current yield and quality prediction with confidence interval
  - `GET /api/v1/risk/current` — current risk scores for all active batches
  - `GET /api/v1/risk/breakdown/{batch_id}` — per-parameter risk contributions
- [ ] Operator dashboard (Phase 1 scope):
  - Active batch list with current risk scores (colour-coded)
  - Batch detail view: real-time sensor charts, current risk breakdown, current prediction
  - Sensor status page (carried over from Phase 0)
- [ ] Automated model retraining job (weekly, Sunday 02:00 NZST)
- [ ] Model accuracy tracking: compare predictions to actuals on completion of each batch

### Feature Engineering Architecture

```
Raw sensor readings (TimescaleDB)
          │
          ▼
Feature Pipeline (APScheduler, 15 min)
          │
          ├─▶ Rolling stats per entity per window
          ├─▶ Phase-aware features
          └─▶ Derived features (VPD, temperature delta)
          │
          ▼
Feature cache (Redis, TTL 15 min)
          │
          ├─▶ Risk Scorer (reads cache, writes risk scores to DB)
          └─▶ LightGBM Inference (reads cache, writes predictions to DB)
```

### Exit Criteria

1. LightGBM yield prediction achieves MAE < 20% (relative to actual yield) on held-out batches from the historical dataset
2. Risk score produces an alert within 2 hours of a known environmental exceedance event (validated against batch notes)
3. Operator can view current risk scores, predictions, and sensor charts for all active batches without engineering assistance
4. Feature pipeline runs reliably at 15-minute intervals for 14 consecutive days without failure
5. Head Grower reviews the first 5 recommendations generated by the risk score and confirms they are "sensible" (even if not acted on yet)
6. Model accuracy tracking is operational (predictions compared to actuals automatically on batch completion)

### Prerequisites from Phase 0

- Minimum 5 completed batches with environmental data and recorded yield/quality outcomes available for initial model training
- Phase 0 exit criteria fully met

### Risks in This Phase

- **Insufficient training data:** With < 10 batches, the LightGBM model will have high variance. Accept this — the goal is a functional baseline, not a production-grade model. The exit criterion (MAE < 20%) may require relaxation if historical data is genuinely limited.
- **Feature importance misleading with small data:** SHAP values on 5–10 training samples can be unstable. Do not present SHAP explanations to operators until Phase 2 with more data.
- **Risk score calibration:** Initial thresholds in `target_profiles.yaml` will be informed by Head Grower domain knowledge. Expect calibration iterations in the first 4 weeks.

---

## Phase 2 — Operator Trust (Weeks 11–18)

### Goal

Transform the system from a monitoring tool the engineering team uses into a system operators actively engage with. Build the feedback loops that allow the system to improve from operator knowledge. Establish explainability so operators understand why the system makes recommendations. Run 30 days of shadow operation where operators are aware of recommendations but not required to follow them — measuring acceptance rate as the trust signal.

### Deliverables

- [ ] Recommendation engine:
  - Rule-based trigger layer: specific recommendation templates for each alert type (VPD out of range, CO2 below target, temperature spike, etc.)
  - Natural language recommendation text generated from template + current sensor context
  - Priority levels: informational, advisory, urgent
  - Deduplication: suppress repeat recommendations for the same condition within a configurable window
- [ ] Recommendation dashboard:
  - Pending recommendations list with priority ordering
  - Full recommendation detail view with supporting sensor data context
  - Accept / Reject / Defer workflow with mandatory notes field
  - Recommendation history with outcomes
- [ ] Operator feedback logging:
  - All accept/reject/defer actions written to `operator_feedback` table with timestamp, operator ID, and notes
  - Feedback linked back to the recommendation and the sensor context at the time
  - Feedback data available for future training (reinforcement signal — not used until Phase 3)
- [ ] SHAP explainability:
  - SHAP values computed for each model prediction
  - Top 5 feature contributions displayed in the Batch Detail view
  - Plain-language translation of SHAP output (e.g., "High humidity variability over last 24h is the primary factor elevating this prediction's uncertainty")
  - SHAP waterfall chart rendered in dashboard
- [ ] Data quality monitoring:
  - Automated drift detection on sensor entity distributions (KL divergence vs 30-day baseline)
  - Data gap detection and alerting (> 10 minutes without reading = gap event)
  - Weekly data quality report (automated, emailed or accessible in dashboard)
  - Data quality events table in database
- [ ] 30-day shadow operation protocol:
  - Document all recommendations generated during shadow period
  - Track acceptance rate weekly
  - Head Grower reviews all rejected recommendations with reasoning
  - Engineering reviews all accepted recommendations to confirm action taken

### Exit Criteria

1. Operator acceptance rate for recommendations > 50% over the 30-day shadow period (measured on recommendations where operator had enough information to decide)
2. Zero recommendations that operators describe as "unexplained" or "confusing" after SHAP explanations are live (qualitative assessment via structured interviews with operators)
3. Recommendation deduplication is working correctly (no operators reporting "same recommendation appearing repeatedly")
4. Data quality monitoring has caught at least one real sensor anomaly during the shadow period and alerted correctly
5. 30-day shadow operation formally completed and signed off by Head Grower
6. Recommendation acceptance rate trend is stable or improving week-over-week
7. All operator feedback is captured and auditable

### Dependencies

- Phase 1 exit criteria fully met
- Minimum 10 completed batches with environmental data (improves SHAP stability)
- At least 3 operators trained on the recommendation workflow

### Risks in This Phase

- **Operator fatigue from too many recommendations:** If the recommendation engine is too aggressive, operators will dismiss everything. Tune thresholds conservatively — better to miss a soft alert than to erode trust with noise.
- **SHAP explanations not meaningful to growers:** Technical language will fail. Involve Head Grower in reviewing and editing explanation templates before release. Test with operators before declaring done.
- **Acceptance rate metric gaming:** Operators may accept recommendations they disagree with to "keep the number up". Include qualitative review to detect this. The metric is a signal, not a target.

---

## Phase 3 — Advanced Modeling (Weeks 19–30)

### Goal

Upgrade the predictive capability from batch-level outcome prediction to trajectory forecasting — the ability to predict how environmental conditions will evolve and how a batch's trajectory is diverging from optimal. This enables earlier intervention recommendations and quantified uncertainty, making the system more useful during the batch rather than only at its end.

### Deliverables

- [ ] Temporal Fusion Transformer (TFT) for sensor trajectory forecasting:
  - Multi-step ahead prediction (1h, 6h, 24h horizons) for key parameters (temperature, humidity, VPD, CO2)
  - Trained on full time-series history from TimescaleDB
  - Input: recent sensor history (sliding window), batch phase metadata, time-of-day/week features
  - Output: predicted sensor trajectory with quantile predictions (P10, P50, P90)
- [ ] Conformal prediction intervals:
  - Calibrated prediction intervals on yield/quality predictions using conformal regression
  - Intervals displayed alongside point predictions in dashboard
  - Uncertainty narrows as batch progresses and more data is accumulated
- [ ] A/B model comparison framework:
  - Both LightGBM baseline and TFT running in parallel
  - Predictions from both models stored and compared against actuals
  - Dashboard view showing performance comparison
  - Formal criteria for promoting TFT to primary model (MASE improvement > 10% on trajectory forecasting)
- [ ] Multi-step risk forecasting:
  - "Risk in 6 hours" and "Risk in 24 hours" forecasts based on predicted sensor trajectories
  - Early warning recommendations: "Based on current trajectory, VPD is likely to exceed threshold in 4–6 hours"
- [ ] Model training pipeline improvements:
  - GPU training support (optional — TFT benefits from GPU but can train on CPU in acceptable time with 30+ batches)
  - Hyperparameter optimisation via Optuna (weekly, run automatically)
  - Training experiment tracking (MLflow or local experiment database)

### Prerequisites

These prerequisites are hard gates — Phase 3 does not begin without them:

- Phase 2 exit criteria fully met (operator acceptance rate > 50%, shadow operation complete)
- **Minimum 30 completed batches** in the training database with high-quality environmental data and recorded outcomes
- Phase 1 LightGBM model has been in production for at least 3 months (ensuring baseline comparison is meaningful)
- Engineering team capacity: TFT training and debugging requires more senior ML engineering time than Phases 0–2

### Exit Criteria

1. TFT outperforms LightGBM baseline on sensor trajectory forecasting by MASE (Mean Absolute Scaled Error) improvement > 10% on held-out test batches
2. Conformal prediction intervals are calibrated: the stated 80% interval contains the actual value in ≥ 80% of test cases
3. Multi-step risk forecasting produces at least one demonstrably useful early warning (validated in review with Head Grower against batch notes)
4. A/B model comparison framework is operational and both models are logging predictions
5. TFT training pipeline runs end-to-end without engineering intervention

### Risks in This Phase

- **Data insufficiency:** TFT requires significantly more data than LightGBM. If only 30 batches are available and they are heterogeneous (different strains, rooms), the model may not converge. Consider starting Phase 3 only with 40+ batches.
- **Computational cost:** TFT training on CPU may take several hours per run. This can impact the weekly retraining schedule if hardware is limited.
- **TFT not outperforming baseline:** This is a real possibility. If LightGBM already captures the variance in the tabular batch features, TFT on trajectory data may not improve much. Accept this outcome and keep LightGBM as primary if TFT fails the MASE criterion.
- **Feature drift at scale:** As batch count grows, older batches may not reflect current facility conditions (room upgrades, new strains, seasonal patterns). Implement a rolling training window (e.g., last 50 batches) to prevent old data from degrading the model.

---

## Phase 4 — Bounded Automation (Weeks 31–52+)

### Goal

Enable the system to take narrow, low-risk, operator-approved automated adjustments to a strictly defined set of controls — only after a formal safety review, legal review, and 60-day shadow automation period. The scope of automation is intentionally minimal: the principle is to automate what is tedious and low-risk, not to automate what is high-impact.

This phase is **contingent**. It may not begin if Phase 2 exit criteria cannot be sustained, if regulatory review raises objections, or if the Head Grower withdraws approval.

### Candidate Actions for Automation (Initial Scope)

Only the following actions are candidates for Phase 4 automation:

| Action | Condition | Constraint |
|---|---|---|
| Minor EC correction (±0.1 mS/cm) | EC drift > 0.2 mS/cm from setpoint during stable growth phase | Max 1 correction per 4 hours; never outside business hours without operator on-site |
| Nutrient dosing micro-adjustment | Only if automated dosing hardware is integrated and validated | Requires separate hardware safety review |
| Alert-driven notification (no physical action) | Always permitted | Already operational in Phases 0–2 |

**Permanently manual (never automated):**
- Lighting schedule changes
- Major nutrient formula changes
- pH adjustment beyond ±0.2
- Harvest initiation
- Transplanting or pruning interventions
- Any action during active pest/disease treatment

### Deliverables

- [ ] Automation framework:
  - Automation action definitions in `config/automation_rules.yaml`
  - Hard constraint enforcement layer (cannot be overridden by model output)
  - Action proposal queue: model proposes action → enters queue → waits for operator approval window or auto-executes if shadow mode
  - Execution log: every automated action written to immutable audit table
- [ ] Rollback system:
  - Each automated action is reversible
  - Rollback can be triggered by operator within 30 minutes of action
  - Rollback writes to audit trail
- [ ] Safety constraint engine:
  - Minimum/maximum bounds hard-coded per actuator type
  - Rate limiting: no more than N actions per hour per actuator
  - Dead-man switch: if sensor data is stale, automation is immediately suspended
  - Human-on-site requirement: automation suspended if no operator has logged in within X hours
- [ ] 60-day shadow automation:
  - System proposes actions but does not execute them
  - All proposed actions reviewed by Head Grower weekly
  - Metrics: proposal frequency, estimated impact if executed, proportion that Head Grower would have approved
- [ ] Formal safety review documentation
- [ ] Audit trail and regulatory reporting capability

### Prerequisites (All Required)

- Phase 2 exit criteria continuously met for 60+ days (not just at Phase 2 handoff)
- Phase 3 exit criteria met (better models reduce false automation triggers)
- **Written sign-off from Head Grower** on the specific automation scope
- **Legal review** confirming automation is consistent with NZ Medicinal Cannabis Scheme licence conditions (specifically: who is responsible for automated adjustments in the grow record)
- **Facility Manager approval** of the safety review document
- Engineering peer review of the constraint enforcement code by an external reviewer

### Exit Criteria for Full Automation Activation

1. 60-day shadow automation completed with zero proposals that would have caused crop harm (validated by Head Grower review)
2. Shadow automation proposal acceptance rate > 70% (Head Grower agrees with what the system would have done)
3. Hard constraint enforcement validated: automated adversarial test suite passes (attempts to exceed bounds are rejected)
4. Rollback mechanism tested and confirmed working within 5 minutes of action
5. Audit trail format reviewed and accepted by compliance officer
6. Formal sign-off from Head Grower, Facility Manager, and legal counsel documented in project records

### Risks in This Phase

- **Regulatory risk:** NZ Medicinal Cannabis Scheme requires detailed records of all interventions. Automated adjustments must be captured in the batch record. Legal review is non-negotiable.
- **Model error causing crop damage:** A misprediction that triggers an automated EC correction at the wrong time could stress plants. The ±0.1 constraint is designed to make damage extremely unlikely but not impossible.
- **Automation scope creep:** Once automation is live, there will be pressure to expand it. Resist adding new actions without completing the full approval cycle for each new action type.

---

## Inter-Phase Dependencies

```
Phase 0 ──────────────────────────────────▶ Phase 1
(data pipeline must work)         (needs reliable data)

Phase 1 ─────────────────────────────────▶ Phase 2
(risk scores must be meaningful)  (needs a baseline to explain)

Phase 2 ───────────────────────┬──────────▶ Phase 3
(operator trust must be         │           (needs enough batches + trust)
established)                   │
                                └──────────▶ Phase 4
                                            (trust must be sustained)

Phase 3 ────────────────────────────────▶ Phase 4
(better models reduce automation errors)
```

---

## Resource Estimates

| Phase | Engineering Effort | External Dependencies | Infrastructure Cost |
|---|---|---|---|
| 0 | 3–4 weeks (1 developer) | HA setup, AquaPro connectivity | Existing server hardware |
| 1 | 5–6 weeks (1 developer) | Head Grower time for calibration | No change |
| 2 | 6–8 weeks (1 developer) | 3+ operator training sessions, Head Grower shadow review | No change |
| 3 | 8–10 weeks (1–2 developers) | 30+ completed batches | Optional GPU for training |
| 4 | 10–14 weeks (2 developers + legal) | Legal review, Head Grower approval, external safety review | Hardware integration if adding actuators |

---

## What Is Explicitly Out of Scope

The following capabilities are not planned and require a separate proposal with full justification before being considered:

- **Computer vision** for plant health assessment (Phase 0–4 scope)
- **Multi-facility deployment** (this system is designed for a single facility)
- **Integration with ERP or batch tracking systems** (may be added as API export in Phase 2+)
- **Customer-facing reporting** (internal operations only)
- **Mobile application** (browser-based responsive dashboard is the delivery vehicle)
- **Real-time streaming dashboard** (15-minute refresh is sufficient for operations)

---

*Roadmap reviewed and updated quarterly. Next scheduled review: 2025-04-01.*
