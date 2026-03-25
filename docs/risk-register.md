# Risk Register — Cultivation Intelligence System
## Legacy Ag Limited | Indoor Medicinal Cannabis | New Zealand

**Document Version:** 1.2
**Last Updated:** 2025-01-20
**Owner:** Engineering Lead
**Review Frequency:** Monthly (or after any significant incident)
**Classification:** Internal — Restricted

---

## Risk Scoring Methodology

**Likelihood:** 1 (rare) – 5 (almost certain)
**Impact:** 1 (negligible) – 5 (catastrophic)
**Risk Score:** Likelihood × Impact (1–25)

| Score | Level | Treatment |
|---|---|---|
| 1–4 | Low | Accept, monitor |
| 5–9 | Medium | Mitigate, monitor monthly |
| 10–14 | High | Active mitigation, monthly review |
| 15–25 | Critical | Immediate mitigation, weekly review |

---

## Technical Risks

---

### RISK-T01: Insufficient Historical Batch Data for Model Training

**ID:** RISK-T01
**Category:** Technical
**Likelihood:** 3 (possible — facility is operational but relatively young)
**Impact:** 4 (major — models trained on insufficient data produce unreliable predictions that erode operator trust)
**Risk Score:** 12 (High)
**Status:** Active — monitoring

**Full Description:**

The LightGBM batch outcome model requires a minimum viable dataset of completed batches with both environmental sensor data and recorded yield/quality outcomes. If fewer than 5–10 batches are available at Phase 1, the model will have high variance (confidence intervals wider than the range of possible outcomes), making its predictions effectively meaningless. Worse, predictions that appear confident but are wrong will damage operator trust in a way that is difficult to recover from.

A secondary concern is data heterogeneity: if available batches span multiple strains, rooms, or significantly different cultivation protocols, the small dataset is further fragmented and the model may simply overfit to superficial correlates.

**Mitigations:**

1. **Phase gate:** Phase 1 does not begin model training until a minimum of 5 completed batches with sensor data are available. This is an explicit Phase 0 deliverable.
2. **Historical CSV import:** `scripts/export_features.py` supports importing historical data from before the system was deployed. Operators should retrieve any available historical records (even hand-logged data) before Phase 1.
3. **Conservative model reporting:** Until batch count reaches 20, all model predictions are displayed with wide uncertainty bands and a banner noting "Predictions based on limited historical data — treat as indicative".
4. **Cross-validation on small data:** Use leave-one-out cross-validation rather than a fixed train/test split when batch count is < 15 to maximise use of available data.
5. **Delay Phase 3 hard gate:** Phase 3 deep learning models require 30+ batches and this is a hard gate, not a guideline.

**Owner:** Engineering Lead
**Review:** Monthly during Phase 0–1

---

### RISK-T02: Sensor Drift Causing Model Input Distribution Shift

**ID:** RISK-T02
**Category:** Technical
**Likelihood:** 3 (possible — sensor drift is a known characteristic of capacitive humidity sensors and EC probes)
**Impact:** 3 (moderate — model inputs silently shift, causing prediction and risk score degradation without obvious failure)
**Risk Score:** 9 (Medium)
**Status:** Active — partial mitigation in Phase 2

**Full Description:**

Grow environment sensors drift over time due to contamination, aging, and calibration creep. A temperature sensor that reads 0.5°C low and a humidity sensor that reads 3% high will cause the computed VPD to diverge from reality. The model was trained on earlier data when sensors were accurately calibrated — the drift creates a silent distribution mismatch (covariate shift) that degrades prediction accuracy without any obvious error signal.

EC and pH probes used in AquaPro are particularly susceptible to drift in nutrient-rich water and typically require calibration every 4–8 weeks.

**Mitigations:**

1. **Sensor calibration schedule:** Establish a regular calibration schedule (monthly for EC/pH, quarterly for temperature/humidity). Document calibration events in the `maintenance_events` table.
2. **Distribution drift monitoring (Phase 2):** Automated KL divergence tracking compares current 7-day sensor distributions against a 30-day rolling baseline. Alerts when drift exceeds threshold.
3. **Calibration event logging:** When operators calibrate a sensor, they log it in the system. The feature pipeline can flag predictions made in the window before calibration as lower confidence.
4. **Reference sensor:** Consider deploying a calibrated reference temperature/humidity sensor (e.g., NIST-traceable probe) in each room for periodic spot-checking.
5. **Model re-calibration trigger:** If drift monitoring detects persistent shift in a sensor entity, flag for engineering review and potential model recalibration using recent post-calibration data.

**Owner:** Cultivation Manager + Engineering Lead
**Review:** Monthly

---

### RISK-T03: Home Assistant API Breaking Changes on Upgrade

**ID:** RISK-T03
**Category:** Technical
**Likelihood:** 2 (unlikely in any given month, but certain over 12+ months of operation)
**Impact:** 4 (major — HA is the source of all sensor data; an API break stops ingest entirely)
**Risk Score:** 8 (Medium)
**Status:** Active — mitigated by version pinning

**Full Description:**

Home Assistant releases updates frequently, and while the REST API is relatively stable, the webhook payload format, entity naming conventions, and authentication token API have changed in past major versions. If HA is upgraded without testing the ingest integration first, the cultivation intelligence system may silently fail to receive data (no error, just no new readings) or receive malformed data.

**Mitigations:**

1. **Pin HA version in deployment configuration.** Document the exact HA version in use and do not upgrade automatically.
2. **Upgrade test procedure:** Before any HA upgrade, stand up a test instance of the cultivation ingest service against the new HA version and verify that sensor readings flow correctly.
3. **Ingest monitoring:** The staleness alerting (ALERT-001, ALERT-002) will catch a complete ingest failure within 10 minutes. However, it will not catch a schema change that silently sends wrong values.
4. **Integration test suite:** Maintain a set of integration tests that replay known HA webhook payloads and assert the resulting database records. Run these against any new HA version before upgrading.
5. **HA change log monitoring:** Subscribe to HA release notes and breaking change announcements. Block HA upgrades until the team has reviewed the changelog for API changes.

**Owner:** Engineering Lead
**Review:** Before any HA upgrade

---

### RISK-T04: AquaPro Entity Schema Changes Without Notice

**ID:** RISK-T04
**Category:** Technical
**Likelihood:** 2 (unlikely — proprietary system with infrequent firmware updates)
**Impact:** 3 (moderate — water quality data lost; nutrient dosing decisions rely on manual observation)
**Risk Score:** 6 (Medium)
**Status:** Active — monitoring

**Full Description:**

The AquaPro water quality monitoring system surfaces its data through a custom Home Assistant integration. Firmware updates to the AquaPro unit or changes to the integration may alter entity IDs, unit conventions, or measurement ranges without any notification to the cultivation intelligence team. Because this is a proprietary third-party system, there is no advance notification of schema changes.

Unlike HA API changes, which are well-documented, AquaPro changes are invisible until a sensor reading fails validation or disappears from the dashboard.

**Mitigations:**

1. **Entity schema snapshot:** Document the current AquaPro entity IDs, units, and expected ranges in `config/sensor_registry.yaml`. Compare against live entities after any AquaPro firmware update.
2. **Value range validation:** The ingest service validates incoming readings against configured ranges. AquaPro readings outside plausible bounds are flagged rather than silently stored.
3. **Staleness alerts:** Standard staleness alerting will catch a complete loss of AquaPro readings within 10 minutes.
4. **Firmware update log:** Operators must notify engineering before applying AquaPro firmware updates.
5. **Manual fallback:** Water quality is checked manually during operator walkthroughs. A loss of AquaPro integration reduces convenience but does not eliminate visibility.

**Owner:** Engineering Lead + Facility Operations
**Review:** Quarterly, or after AquaPro firmware update

---

### RISK-T05: TimescaleDB Performance Degradation with Data Volume

**ID:** RISK-T05
**Category:** Technical
**Likelihood:** 2 (unlikely in the short term; likely within 2–3 years of continuous operation)
**Impact:** 3 (moderate — slow queries degrade dashboard responsiveness; extreme degradation could affect ingest)
**Risk Score:** 6 (Medium)
**Status:** Low priority — monitor

**Full Description:**

TimescaleDB performs well at large data volumes when properly configured (compression, chunk sizing, continuous aggregates). However, without ongoing maintenance, the hypertable chunks can grow large, compression may not run, and queries spanning long time ranges can become slow. At 10 sensors × 15 readings per minute × 60 minutes × 24 hours, the facility generates approximately 200,000 rows per day. Over 3 years, that is ~200 million rows of raw data before compression.

Unoptimised queries (e.g., full table scans on `sensor_readings` without proper time-range predicates) will become progressively slower.

**Mitigations:**

1. **Compression policy:** TimescaleDB compression is configured to run automatically on chunks older than 7 days. Compression typically achieves 10–20x reduction in storage and improves scan performance on compressed chunks.
2. **Continuous aggregates:** 1h, 6h, and 24h rollup aggregates are maintained as continuous aggregate materialized views. Dashboard queries should use these views, not raw `sensor_readings`, for historical data.
3. **Chunk interval tuning:** Default chunk interval is 7 days. If query patterns change, rechunk to match the most common query window.
4. **Vacuum schedule:** Automated VACUUM is enabled. Monitor dead tuple accumulation after large deletes or imports.
5. **Query review:** All dashboard queries are instrumented with query duration logging. Queries exceeding 2s trigger a slow-query log entry reviewed in weekly maintenance.
6. **Retention policy review:** If regulatory requirements allow, implement a data downsampling policy after 2 years (retain 1h aggregates, drop raw minutely data).

**Owner:** Engineering Lead
**Review:** Quarterly

---

### RISK-T06: Feature Engineering Pipeline Failure Causing Stale Predictions

**ID:** RISK-T06
**Category:** Technical
**Likelihood:** 2 (unlikely per run; likely over 12 months of continuous operation given software complexity)
**Impact:** 3 (moderate — stale features → stale risk scores → operators see outdated information without knowing it)
**Risk Score:** 6 (Medium)
**Status:** Active — monitoring and alerting in place

**Full Description:**

The feature engineering pipeline runs every 15 minutes via APScheduler. If a pipeline run fails silently (exception caught without alerting, or job scheduler hangs), the Redis feature cache becomes stale. Subsequent risk scoring reads stale features and produces risk scores that reflect conditions 30, 60, or 120 minutes ago. The dashboard will not visibly indicate this unless the cache TTL is correctly configured and checked.

A particularly dangerous failure mode is a partial pipeline run: some entities updated, others not. The risk score may then reflect a mixed-time-snapshot that is internally inconsistent.

**Mitigations:**

1. **Pipeline heartbeat:** Each successful pipeline run writes a `last_run_timestamp` to Redis. The health check endpoint reads this and returns `"feature_pipeline": "stale"` if more than 20 minutes have passed since the last successful run.
2. **ALERT-005 (Feature Pipeline Stale):** An alert is raised if the pipeline has not completed successfully within 25 minutes.
3. **Atomic feature updates:** Features for each batch are written to Redis as a single atomic operation (using a Redis MULTI/EXEC transaction) so either all features for a batch are updated or none are.
4. **Exception logging and alerting:** All exceptions in the pipeline are logged at ERROR level with full stack traces. A Slack/email alert is sent on consecutive failures.
5. **Cache TTL as a backstop:** Redis keys have a TTL of 20 minutes. If the pipeline fails to update them, they expire and the risk scorer detects the absence of features rather than using stale ones.

**Owner:** Engineering Lead
**Review:** Monthly

---

### RISK-T07: Model Overfitting to Specific Strains in Limited Batch History

**ID:** RISK-T07
**Category:** Technical
**Likelihood:** 3 (possible — with < 20 batches, strain confounding is very likely)
**Impact:** 3 (moderate — model predictions are inaccurate for new strains; if operators trust the model, poor decisions follow)
**Risk Score:** 9 (Medium)
**Status:** Active — mitigated by transparency

**Full Description:**

If the majority of completed batches in the training set are from a single strain or a narrow set of strains, the model will learn strain-specific patterns and generalise poorly to new strains. For example, if all historical batches are a CBD-dominant cultivar with a 10-week flower cycle, the model may have learned an implicit correlation between days-in-flower and yield that is strain-specific and will not transfer to a THC-dominant cultivar with a 9-week cycle.

With a small facility and a limited number of strains, this risk is structural and cannot be fully mitigated without more data. It must be acknowledged in how predictions are presented.

**Mitigations:**

1. **Strain as a feature:** Strain identity is included as a feature in the LightGBM model, allowing it to learn per-strain coefficients when data is available.
2. **New strain warning:** When a prediction is made for a batch with a strain that has fewer than 3 completed historical batches, the dashboard displays a warning: "Limited historical data for this strain — prediction uncertainty is elevated".
3. **Prediction confidence interval widening:** For new strains, conformal prediction intervals are widened by a configurable multiplier.
4. **SHAP review:** When adding a new strain, Engineering Lead reviews SHAP feature importances to confirm the model's explanation is plausible (not relying on spurious correlates).
5. **Head Grower consultation:** For any new strain batch, Head Grower provides initial target profiles and expected outcome ranges, which are used as a prior alongside model predictions.

**Owner:** Engineering Lead + Head Grower
**Review:** When new strains are added to the grow program

---

### RISK-T08: Automated Control Action Causing Crop Damage (Phase 4)

**ID:** RISK-T08
**Category:** Technical
**Likelihood:** 2 (unlikely with constraint enforcement; possible if constraints are misconfigured)
**Impact:** 5 (catastrophic — loss of a full flower room batch has significant financial and regulatory impact)
**Risk Score:** 10 (High — monitored closely)
**Status:** Not applicable until Phase 4 — flagged for pre-Phase 4 review

**Full Description:**

In Phase 4, the system will be authorised to make minor automated control adjustments. Even with hard constraint enforcement, there is residual risk that an automated action taken at the wrong time (e.g., EC correction during a flush period, or temperature adjustment when a pest treatment is active) could harm the crop. The model may be confident in an action that is contextually inappropriate because the context (e.g., "currently flushing") is not represented in the model's features.

The compounding risk is that an automated action executed by software has no "common sense" check that a human operator would apply.

**Mitigations:**

1. **Minimal automation scope:** Only minor, low-risk adjustments (EC ±0.1) are candidates for Phase 4 automation. Major interventions are permanently manual.
2. **Hard constraint enforcement:** All automated actions are bounded by hard-coded limits that cannot be exceeded regardless of model output.
3. **Context blackout periods:** Automation is automatically suspended during configured blackout periods (e.g., flush period, days post-transplant, pest treatment period). Operators mark blackout periods in the batch record.
4. **Rate limiting:** No more than 1 automated action per actuator per 4 hours prevents cascading corrections.
5. **60-day shadow mode:** No actual control actions taken until 60-day shadow period is complete and reviewed.
6. **Rollback:** Every automated action is reversible by operator within 30 minutes.
7. **Dead-man switch:** If sensor data is stale, automation halts immediately.

**Owner:** Engineering Lead + Head Grower + Facility Manager
**Review:** Monthly during Phase 4 design; weekly during Phase 4 shadow operation

---

### RISK-T09: Redis Cache Inconsistency Causing Stale Recommendations

**ID:** RISK-T09
**Category:** Technical
**Likelihood:** 2 (unlikely — Redis is reliable, but cache invalidation logic can have bugs)
**Impact:** 2 (minor — stale recommendations are visible but do not cause active harm)
**Risk Score:** 4 (Low)
**Status:** Accepted — monitor

**Full Description:**

Redis is used to cache computed features. If the cache invalidation logic is incorrect (e.g., a new entity is added but the cached feature key format doesn't include it, or TTL is set too long), the risk scorer may read stale features and produce recommendations based on outdated sensor context. The recommendation would be technically valid for the cached state but not for the current state.

**Mitigations:**

1. **TTL backstop:** All cache keys have a maximum TTL of 20 minutes, bounding the maximum staleness.
2. **Cache-aside pattern:** The application always reads from cache and falls back to computing from the database if the key is missing. A missing key is never an error.
3. **Feature generation timestamp:** Each cached feature set includes the generation timestamp, which is surfaced in the risk breakdown API. Operators can see when features were last computed.
4. **Integration tests:** Cache invalidation paths are covered by integration tests run in CI.

**Owner:** Engineering Lead
**Review:** Quarterly

---

### RISK-T10: Network Partition Isolating Cultivation Intelligence from Home Assistant

**ID:** RISK-T10
**Category:** Technical
**Likelihood:** 2 (unlikely in a single-facility LAN environment, but possible with WiFi, switch failures)
**Impact:** 3 (moderate — system becomes blind to current sensor state; risk scores stop updating)
**Risk Score:** 6 (Medium)
**Status:** Active — alerting in place

**Full Description:**

If a network partition occurs between the server running the cultivation intelligence system and the server or device running Home Assistant, the ingest service will stop receiving data. The system does not fail loudly — it simply stops receiving new readings. Without staleness alerting, operators could believe the system is operational when it is actually operating on data that is hours old.

**Mitigations:**

1. **HA connectivity health check:** `/health/ha` endpoint explicitly tests connectivity to the HA API on each health check call.
2. **ALERT-004 (HA Connection Lost):** Alert raised within 5 minutes of connectivity loss.
3. **Polling fallback:** If webhooks fail, the ingest service falls back to polling the HA state API every 2 minutes. This provides resilience against webhook delivery failures (not network partitions, but partial failures).
4. **Physical manual operations:** Operators are trained to continue physical grow room management based on in-room instrumentation if the cultivation intelligence system is unavailable. The system is advisory; its unavailability does not stop operations.
5. **Network redundancy:** Where possible, the server and HA host should be connected via wired Ethernet, not WiFi, to minimise partition risk.

**Owner:** Engineering Lead + Facility Operations
**Review:** Quarterly

---

## Operational Risks

---

### RISK-O01: Operator Ignores High-Priority Recommendations

**ID:** RISK-O01
**Category:** Operational
**Likelihood:** 3 (possible — recommendation fatigue is a known risk in advisory systems)
**Impact:** 3 (moderate — suboptimal crop outcomes; audit trail shows recommendation was available but not acted on)
**Risk Score:** 9 (Medium)
**Status:** Active — addressed in Phase 2

**Full Description:**

If operators routinely dismiss or ignore high-priority recommendations without reading them, the system provides no value despite generating correct outputs. This can occur for several reasons: recommendations appear too frequently (fatigue), recommendations are not actionable (trust failure), recommendations arrive at inconvenient times (workflow mismatch), or operators do not believe the system is accurate.

There is also a regulatory dimension: if a batch fails and the audit trail shows a high-priority recommendation was dismissed without reason, this may create liability.

**Mitigations:**

1. **Phase 2 shadow operation:** The 30-day shadow period provides a no-pressure environment for operators to engage with recommendations and provide feedback. Acceptance rate < 50% triggers a review before recommendations are presented as operational.
2. **Recommendation quality threshold:** Suppress recommendations with low confidence scores. Better to produce fewer, higher-confidence recommendations than many uncertain ones.
3. **Mandatory acknowledgement:** Recommendations cannot be left pending for more than 4 hours without a system alert. Operators must explicitly act on each recommendation.
4. **Deduplication:** The same condition does not generate repeated recommendations within a configurable suppression window.
5. **Feedback loop:** Rejected recommendations are reviewed by the Head Grower weekly. If operators are consistently rejecting a recommendation type, the threshold or template is revised.
6. **Operator training:** All operators are trained on recommendation interpretation and the importance of engagement with the system for regulatory compliance.

**Owner:** Head Grower + Cultivation Manager
**Review:** Monthly (track acceptance rate trend)

---

### RISK-O02: Inconsistent Manual Data Entry Corrupting Training Labels

**ID:** RISK-O02
**Category:** Operational
**Likelihood:** 3 (possible — manual data entry is error-prone and convention-dependent)
**Impact:** 4 (major — model trained on wrong labels produces confidently wrong predictions)
**Risk Score:** 12 (High)
**Status:** Active — mitigation required

**Full Description:**

The model's training labels (yield in g/plant, quality band) are entered manually by operators at batch completion. If these entries are inconsistent (different operators using different measurement conventions, misidentifying the weight basis — wet vs dry weight, per plant vs per square metre), the training data will be noisy. Noisy labels are one of the most damaging data quality issues for ML models because the model learns to predict the error rather than the signal.

Common inconsistencies observed in similar operations:
- Yield recorded as wet weight by some operators, dry weight by others
- Partial harvests logged as full batch outcomes
- Quality band assigned based on visual inspection rather than lab assay

**Mitigations:**

1. **Standardised batch completion form:** The batch completion UI enforces a structured form with dropdown fields (not free text) for quality band and mandatory unit selection for yield weight (dry/wet/trimmed).
2. **Data entry validation:** Range checks on yield entry (e.g., flag if yield per plant is below 10g or above 300g dry weight for flower).
3. **Documentation:** A one-page data entry guide is posted in each grow room and embedded in the UI as a help tooltip.
4. **Dual entry for first 5 batches:** Head Grower reviews and countersigns all batch completion entries for the first 5 batches after go-live.
5. **Label auditing:** Before each model retraining, the training label distribution is reviewed and obvious outliers are flagged for human review.

**Owner:** Head Grower + Engineering Lead
**Review:** Before each model retraining cycle

---

### RISK-O03: Staff Turnover Causing Loss of Domain Knowledge

**ID:** RISK-O03
**Category:** Operational
**Likelihood:** 2 (unlikely in the short term; increases over the facility's operating life)
**Impact:** 3 (moderate — new operators may not understand system limitations or how to interpret recommendations)
**Risk Score:** 6 (Medium)
**Status:** Accepted — mitigated by documentation

**Full Description:**

The cultivation intelligence system embeds domain knowledge in multiple places: the recommendation templates (written with Head Grower input), the target profiles (set by Head Grower), and the model training labels (entered by experienced operators). If key staff leave, this knowledge leaves with them. New operators may misinterpret the system, enter incorrect batch completion data, or fail to apply recommendations correctly.

Additionally, a new operator who did not participate in the Phase 2 trust-building process may either over-trust the system (treating recommendations as mandatory) or under-trust it (ignoring all recommendations).

**Mitigations:**

1. **Onboarding documentation:** The operations runbook (this document) provides comprehensive operator onboarding material.
2. **Explainability (Phase 2):** SHAP explanations make recommendations interpretable without domain knowledge of the underlying model.
3. **Target profile ownership:** Target profiles and recommendation thresholds are stored in versioned configuration files with git history, so the rationale for each threshold is documented in commit messages.
4. **Knowledge capture sessions:** When the Head Grower or senior operators contribute calibration decisions to the system, these are documented in the operations log.
5. **Buddy training:** New operators shadow an experienced operator for the first two weeks, specifically on recommendation interpretation.

**Owner:** Facility Manager + Head Grower
**Review:** Annually, or when key staff leave

---

### RISK-O04: Grow Environment Event Not Captured (Manual Intervention Without Logging)

**ID:** RISK-O04
**Category:** Operational
**Likelihood:** 4 (likely — operators will sometimes act without logging, especially during urgent situations)
**Impact:** 3 (moderate — model training data is missing context; recommendations may misinterpret the plant's response to the logged intervention as a natural environmental change)
**Risk Score:** 12 (High)
**Status:** Active — operational control required

**Full Description:**

When an operator manually intervenes in the grow environment (adjusts nutrient concentration, changes lighting setpoints, applies a foliar spray, switches to flush), this event must be logged in the system so the feature pipeline and model can correctly interpret subsequent sensor data changes. An unlogged EC adjustment, for example, looks like a natural EC fluctuation in the database — the model may generate an EC-related recommendation when none is appropriate.

Over time, unlogged interventions degrade the training data quality because the model cannot distinguish between "environment changed because of operator action" and "environment changed due to a problem the model should detect".

**Mitigations:**

1. **Quick-log workflow:** The dashboard provides a single-click "Log Manual Intervention" button accessible from the batch detail view and mobile browser, minimising friction.
2. **Operator culture:** Head Grower emphasises that logging interventions is as important as the intervention itself, for both model quality and regulatory compliance.
3. **Post-hoc logging:** The system accepts retrospective intervention logs with a custom timestamp, allowing operators to log actions they took earlier in the day.
4. **Anomaly detection context:** When a sensor value changes significantly (> 2 standard deviations) and no intervention is logged, the system flags it as an unexplained change in the data quality events table, prompting operator review.
5. **Regulatory linkage:** For medicinal cannabis compliance, all interventions affecting the batch record should be logged regardless of system requirements — framing logging as a compliance activity, not just a system feature.

**Owner:** Head Grower + Cultivation Manager
**Review:** Monthly (review unlogged anomaly events)

---

### RISK-O05: Backup Failure Causing Data Loss

**ID:** RISK-O05
**Category:** Operational
**Likelihood:** 2 (unlikely in normal operation; backups are automated — but automation can fail silently)
**Impact:** 4 (major — loss of historical sensor data and batch records; regulatory implications for traceability)
**Risk Score:** 8 (Medium)
**Status:** Active — monitoring

**Full Description:**

Automated backups run daily using `pg_dump` to a local backup directory. If the backup script fails silently (disk full, permission error, pg_dump timeout on large database), the backup file may be missing or corrupt without any visible alert. The failure is only discovered when a restore is needed — the worst possible time.

**Mitigations:**

1. **Backup success logging:** The backup script writes a success record to a backup manifest file after each successful run. The health check reads this manifest and raises ALERT-006 if no backup has succeeded in the past 25 hours.
2. **Backup verification:** After each backup, `pg_restore --list` is run on the backup file to verify it can be opened. A file that cannot be listed is treated as a failed backup.
3. **Off-server backup:** Once a week, the most recent backup is copied to an off-server location (NAS or cloud storage). This protects against server-level hardware failure.
4. **Restore testing:** Monthly, a restore test is performed to a separate database to confirm the backup is actually restorable — not just a valid file.
5. **Disk space monitoring:** Backup directory disk usage is monitored; ALERT-006 fires when disk > 80% to prevent backup failures due to full disk.

**Owner:** Engineering Lead + Facility Operations
**Review:** Weekly (verify backup freshness in weekly maintenance checklist)

---

### RISK-O06: System Running in Advisory Mode When Operator Believes It Is Automated

**ID:** RISK-O06
**Category:** Operational
**Likelihood:** 2 (unlikely if onboarding is adequate; increases if turnover is high)
**Impact:** 4 (major — if operator believes automation is handling an issue, no manual action is taken; crop damage results)
**Risk Score:** 8 (Medium)
**Status:** Active — managed by design and training

**Full Description:**

There is a risk of mode confusion: an operator believes the system is taking automated corrective action when it is only generating an advisory recommendation. This could occur if an operator is poorly onboarded, if the dashboard UI is ambiguous about the system's mode, or if recommendations are worded in a way that implies action ("The system is correcting VPD" vs "Consider adjusting dehumidifier setpoint").

This risk increases significantly in Phase 4 if some actions are automated but others are not — operators may generalise that "the system handles it" across all parameters.

**Mitigations:**

1. **Unambiguous UI language:** All recommendations are worded as directives to the operator ("Recommend reducing dehumidifier setpoint from 65% to 60%"), never as system actions ("Adjusting dehumidifier to 60%").
2. **Advisory mode banner:** The dashboard displays a persistent, prominent banner: "This system is advisory only. No automated control actions are taken." This banner is removed only if Phase 4 automation is active for a specific actuator.
3. **Operator training and sign-off:** All operators sign a training confirmation that they understand the system is advisory. This is reviewed at annual recertification.
4. **Phase 4 scope clarity:** If Phase 4 automation is activated for any actuator, the specific actuator and bounds are documented and communicated explicitly to all operators. No automation is "invisible".

**Owner:** Head Grower + Facility Manager
**Review:** During operator onboarding; at Phase 4 transition

---

## Business and Compliance Risks

---

### RISK-B01: NZ Medicinal Cannabis Regulatory Requirements for Data Traceability

**ID:** RISK-B01
**Category:** Business/Compliance
**Likelihood:** 1 (the risk is not that regulation changes — it is that current operations do not comply)
**Impact:** 5 (catastrophic — loss of licence, or inability to supply legally)
**Risk Score:** 5 (Medium — ongoing compliance management required)
**Status:** Active — ongoing

**Full Description:**

The NZ Medicinal Cannabis Scheme (under the Misuse of Drugs (Medicinal Cannabis) Regulations 2019) requires licence holders to maintain comprehensive records of cultivation activities, including growing conditions, interventions, and batch traceability. The cultivation intelligence system interacts with this requirement in several ways:

- Sensor data stored in the system may constitute part of the official batch record
- Automated actions (Phase 4) must be traceable and attributable
- If the system generates a recommendation that is not acted on, and a batch fails a quality test, the unanswered recommendation may be examined during a compliance review
- Data retention requirements must be understood before any retention policy is implemented

**Mitigations:**

1. **Legal review before Phase 4:** Legal review of Phase 4 automation is a hard prerequisite, specifically addressing how automated control actions are recorded in the batch record.
2. **Immutable audit trail:** All operator actions (recommendation accept/reject, batch events, manual intervention logs) are written to an append-only audit table. Records cannot be modified or deleted.
3. **Data retention policy:** Discuss minimum data retention requirements with the compliance officer before implementing any data purging or downsampling. Current default is indefinite retention.
4. **Compliance officer consultation:** Engage the facility's compliance officer to review the system's data schema and confirm it satisfies regulatory record-keeping requirements.
5. **Export capability:** The system provides a batch record export function (`GET /api/v1/batches/{batch_id}/export`) that produces a structured JSON export suitable for regulatory submission.

**Owner:** Facility Manager + Compliance Officer
**Review:** Annually, or when regulations change

---

### RISK-B02: Intellectual Property Protection of Cultivation Models

**ID:** RISK-B02
**Category:** Business/Compliance
**Likelihood:** 1 (unlikely — this is a single-facility internal system)
**Impact:** 3 (moderate — trained models encode cultivation IP; if leaked, competitors could reproduce target profiles and outcomes)
**Risk Score:** 3 (Low)
**Status:** Accepted — monitor

**Full Description:**

The trained LightGBM models encode the relationship between environmental conditions and crop outcomes specific to Legacy Ag Limited's facility, strains, and cultivation protocols. The feature importance and SHAP explanations reveal which environmental parameters most influence outcome quality. This constitutes proprietary cultivation intelligence. If model files or SHAP outputs are accessible to unauthorised parties, this IP could be extracted.

**Mitigations:**

1. **Access control:** The cultivation intelligence API requires authentication for all endpoints. Model files are stored on the server's internal filesystem, not accessible via the API.
2. **Network isolation:** The system runs on a facility-internal network; the API is not exposed to the internet.
3. **Model artifact encryption:** Model files at rest should be encrypted using filesystem-level encryption if deemed necessary by the Facility Manager.
4. **Staff confidentiality:** Employment agreements cover proprietary system data.

**Owner:** Facility Manager
**Review:** Annually

---

### RISK-B03: Scope Creep Leading to Phase 4 Premature Advancement

**ID:** RISK-B03
**Category:** Business/Compliance
**Likelihood:** 3 (possible — business pressure to automate is a well-known pattern in ML project governance)
**Impact:** 4 (major — premature automation with insufficient operator trust or model maturity increases crop risk and regulatory exposure)
**Risk Score:** 12 (High)
**Status:** Active — managed by governance

**Full Description:**

As the system demonstrates value in Phases 0–2, there will be natural pressure from management or operators to "just automate it" before the formal Phase 4 prerequisites are met. This pressure may be intensified by commercial competition, staff shortages, or the superficial appearance of model confidence. Bypassing Phase 3 or Phase 4 prerequisites would expose the facility to crop loss, regulatory non-compliance, and erosion of the trust-based operator relationship the roadmap is designed to build.

**Mitigations:**

1. **Documented roadmap with hard gates:** This roadmap document and its exit criteria are the governing document. Phase transitions require written sign-off from the designated gate owners listed in the Phase Summary table.
2. **Formal Phase 4 prerequisites:** The Phase 4 prerequisites include legal review and Facility Manager approval — neither of which can be bypassed by engineering or operational pressure.
3. **Stakeholder education:** Facility Manager and Head Grower are briefed on why the phased approach exists and the specific risks of skipping phases.
4. **No advisory-to-automation shortcut in code:** The advisory architecture (ADR-0004) deliberately does not include control integration in Phases 0–3. Adding control integration requires an explicit engineering change, not just a configuration switch.

**Owner:** Engineering Lead + Facility Manager
**Review:** At each phase gate review

---

### RISK-B04: Overreliance on ML Reducing Grower Skill Development

**ID:** RISK-B04
**Category:** Business/Compliance
**Likelihood:** 2 (unlikely in the short term; possible over 2–3 years as new growers join who have only known the system)
**Impact:** 3 (moderate — if the system fails or is unavailable, growers who rely on it may not be able to operate effectively without it)
**Risk Score:** 6 (Medium)
**Status:** Accepted — monitor

**Full Description:**

A sophisticated advisory system that consistently produces correct recommendations may inadvertently deskill operators. New growers who learn cultivation practice alongside the system may develop a dependency on its recommendations rather than developing independent environmental reading skills. If the system goes offline for a significant period (hardware failure, network outage), operators who lack autonomous diagnostic skills may not detect environmental problems without system alerts.

This is especially important in a medicinal cannabis context where regulatory scrutiny requires knowledgeable licensed personnel to be in responsible charge of the cultivation operation.

**Mitigations:**

1. **Advisory-only framing:** The system is always framed as supporting operator decision-making, not replacing it. Recommendations require operator acceptance; they do not execute themselves.
2. **Explanation-first design:** SHAP explanations and the recommendation rationale are designed to teach operators environmental relationships, not just tell them what to do.
3. **System-offline drills:** Quarterly, the system is deliberately taken offline for 2 hours during a walkthrough to ensure operators can conduct a manual environmental assessment without dashboard assistance.
4. **Senior grower mentorship:** New growers are paired with experienced growers for the first 3 months, with system access but explicit instruction to form their own assessment before reading the dashboard.
5. **Anomaly detection education:** Operators are trained in what each recommendation type means and the environmental logic behind it, not just how to acknowledge it.

**Owner:** Head Grower + Cultivation Manager
**Review:** Annually; at each new operator hire

---

## Risk Register Summary

| ID | Category | Description | L | I | Score | Level | Status |
|---|---|---|---|---|---|---|---|
| RISK-T01 | Technical | Insufficient historical batch data | 3 | 4 | 12 | High | Active |
| RISK-T02 | Technical | Sensor drift / covariate shift | 3 | 3 | 9 | Medium | Active |
| RISK-T03 | Technical | HA API breaking changes | 2 | 4 | 8 | Medium | Active |
| RISK-T04 | Technical | AquaPro schema changes | 2 | 3 | 6 | Medium | Active |
| RISK-T05 | Technical | TimescaleDB performance degradation | 2 | 3 | 6 | Medium | Monitor |
| RISK-T06 | Technical | Feature pipeline failure / stale predictions | 2 | 3 | 6 | Medium | Active |
| RISK-T07 | Technical | Model overfit to limited strains | 3 | 3 | 9 | Medium | Active |
| RISK-T08 | Technical | Automated action causing crop damage (Phase 4) | 2 | 5 | 10 | High | Pre-Phase 4 |
| RISK-T09 | Technical | Redis cache inconsistency | 2 | 2 | 4 | Low | Accepted |
| RISK-T10 | Technical | Network partition from HA | 2 | 3 | 6 | Medium | Active |
| RISK-O01 | Operational | Operators ignoring recommendations | 3 | 3 | 9 | Medium | Active |
| RISK-O02 | Operational | Inconsistent manual data entry | 3 | 4 | 12 | High | Active |
| RISK-O03 | Operational | Staff turnover / knowledge loss | 2 | 3 | 6 | Medium | Accepted |
| RISK-O04 | Operational | Unlogged manual interventions | 4 | 3 | 12 | High | Active |
| RISK-O05 | Operational | Backup failure / data loss | 2 | 4 | 8 | Medium | Active |
| RISK-O06 | Operational | Mode confusion (advisory vs automated) | 2 | 4 | 8 | Medium | Active |
| RISK-B01 | Business/Compliance | NZ regulatory traceability requirements | 1 | 5 | 5 | Medium | Active |
| RISK-B02 | Business/Compliance | IP protection of cultivation models | 1 | 3 | 3 | Low | Accepted |
| RISK-B03 | Business/Compliance | Scope creep to premature automation | 3 | 4 | 12 | High | Active |
| RISK-B04 | Business/Compliance | Overreliance reducing grower skill | 2 | 3 | 6 | Medium | Monitor |

---

*Risk register reviewed monthly by Engineering Lead. Significant changes require notification to Facility Manager.*
