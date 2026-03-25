# Product Brief

**Cultivation Intelligence — v1 Product Specification**

*Legacy Ag Limited | Internal Document*
*Version: 0.4 (Draft) | Date: March 2026*
*Owner: Technical Lead | Reviewers: Head Grower, Facility Manager*

---

## 1. Product Vision and Mission

### Vision

To give every grower at Legacy Ag Limited access to the full analytical power of every batch the facility has ever run — at the moment they need it, in a form they can act on.

### Mission

Build and operate an internal cultivation intelligence platform that transforms the facility's raw sensor and batch data into explainable, timely recommendations that improve consistency, reduce reactive decision-making, and encode institutional knowledge in a persistent, queryable form.

### Strategic Context

Legacy Ag Limited operates under a New Zealand medicinal cannabis licence. Product quality, batch-to-batch consistency, and comprehensive record-keeping are not optional — they are regulatory requirements and competitive differentiators in a market where therapeutic efficacy claims depend on demonstrated product consistency.

The facility has made a significant capital investment in sensor infrastructure (Zigbee network, ESPHome nodes, AquaPro dosing unit AQU1AD04A42) and in the Home Assistant automation platform. The Cultivation Intelligence project converts that existing infrastructure investment into an active analytical asset rather than a passive monitoring system.

---

## 2. Target Personas

### Persona 1: Head Grower (Primary User)

**Name:** Sam
**Role:** Head Grower
**Experience:** 8 years in commercial cannabis cultivation, 3 years at Legacy Ag Limited
**Technical comfort:** High for horticulture systems, moderate for software tools

**Daily responsibilities:**
- Sets environmental targets for each room and grow stage
- Reviews plant health observations and adjusts protocols
- Investigates anomalies flagged by the team or observed during walk-throughs
- Makes decisions on nutrient adjustments, irrigation timing, defoliation scheduling
- Prepares batch records and contributes to post-harvest analysis

**Current frustrations:**
- Investigating a problem mid-batch requires pulling data from HA, the dosing log, and shift notes separately — "it takes an hour to answer a question that should take five minutes"
- Post-harvest analysis is superficial because there is not enough time to do a thorough data review before the next batch starts
- Knows from experience that certain early-stage conditions predict a difficult late-flower, but cannot easily verify this across multiple batches
- Concerned about knowledge transfer — "if I left tomorrow, a lot of what I know about this facility would leave with me"

**Goals from this system:**
- Fast access to current batch status vs. historical profiles
- Early alerts when a batch is deviating from expected patterns
- Confidence-rated recommendations with clear reasoning
- A tool that learns from the facility's own data, not generic industry averages

---

### Persona 2: Facility Manager

**Name:** Jordan
**Role:** Facility Manager
**Experience:** 10 years in regulated manufacturing environments, 2 years at Legacy Ag Limited
**Technical comfort:** Moderate — comfortable with dashboards and reports, not a data scientist

**Responsibilities:**
- Oversees compliance with Medicinal Cannabis Agency requirements
- Manages operational budget and resource allocation
- Coordinates between cultivation, processing, quality, and administration
- Presents operational performance to the board

**Goals from this system:**
- High-level visibility into batch status and risk exposure without needing to interrogate raw sensor data
- Evidence that the facility is operating within validated environmental parameters
- Trend data to support investment decisions (e.g., "our VPD control in Room 3 has improved significantly since the HVAC upgrade")
- Confidence that the analytical platform does not introduce compliance risk

---

### Persona 3: Data Analyst / Systems Developer

**Name:** Alex
**Role:** Internal Data / Systems Analyst
**Experience:** 3 years in data engineering and ML, 1 year at Legacy Ag Limited
**Technical comfort:** High — proficient in Python, SQL, familiar with ML concepts

**Responsibilities:**
- Maintains the Cultivation Intelligence platform
- Develops and trains new models
- Manages the data pipeline and infrastructure
- Supports the growing team in interpreting analytical outputs

**Goals from this system:**
- A well-documented, modular codebase that is maintainable without specialist ML expertise
- A model registry and experiment tracking setup that supports reproducible research
- Clear observability into pipeline health and model performance drift
- A feedback loop mechanism so operator decisions can be used to improve models

---

### Persona 4: Future Automation Engineer (Phase 2+)

**Name:** (Future hire or contractor)
**Role:** Controls / Automation Engineer
**Technical comfort:** High — familiar with PLC logic, HVAC control systems, feedback loops

**Responsibilities (future):**
- Implements write-back automation from CI recommendations to facility actuators
- Designs safe control loops with appropriate interlocks and overrides
- Validates automated control behaviour against established safety bounds

**Goals from this system:**
- A well-defined API that exposes model recommendations in a structured, machine-readable form
- Clear documentation of the recommendation confidence intervals and operating bounds
- Audit trail of every automated action for compliance and debugging

---

## 3. Problem Statement

### 3.1 Data Is Siloed and Inaccessible

The facility generates rich, continuous sensor data across multiple rooms and grow stages. This data is stored in Home Assistant's recorder database (SQLite or MariaDB), the AquaPro dosing unit's internal log, and manually in spreadsheets and shift notes. There is no single place to query a question like: *"What was the average VPD in Room 2 during weeks 3 and 4 of flower across the last 8 batches?"*

**Pain point:** Answering cross-batch, cross-variable analytical questions takes hours of manual data extraction and reconciliation, so it rarely happens.

### 3.2 Problem Detection Is Reactive

The team currently identifies problems when they become visually apparent or when a sensor alert crosses a threshold set in Home Assistant. These thresholds are static — they do not adapt to grow stage, expected trajectory, or the specific conditions of the current batch.

**Pain point:** By the time a problem is detected, recovery options are limited. A two-day lag in detecting an EC drift that began on day 7 of flower may result in measurable yield or quality impact.

### 3.3 Batch Outcomes Are Unpredictable

Batch performance — yield index, cannabinoid profile consistency, plant uniformity — varies from run to run in ways that are not well understood. The team has hypotheses about which environmental factors drive variability, but cannot easily test those hypotheses against the data because the data is not in a form that supports analysis.

**Pain point:** Without a model that relates environmental conditions to outcomes, it is impossible to deliberately optimise — only to react after the fact.

### 3.4 Knowledge Lives in People's Heads

The Head Grower and senior staff have accumulated significant facility-specific knowledge: which rooms run warmer, which strains are sensitive to VPD in late flower, what the dosing unit's behaviour is when the nutrient concentrate is getting low. This knowledge is not documented or systematised.

**Pain point:** Staff changes, even temporary ones during leave, result in decisions being made with less context than usual. There is no mechanism for the facility to learn as an institution rather than relying on individual memory.

### 3.5 No Batch Outcome Prediction

There is currently no mechanism to predict — early in a batch — whether the batch is on track for a good outcome or accumulating risk. All assessment is retrospective.

**Pain point:** Early intervention based on a predicted outcome is more effective and less costly than late-stage remediation. Without prediction, late-stage remediation is the only option.

---

## 4. Proposed Solution

The Cultivation Intelligence platform addresses each problem statement directly:

| Problem | Solution |
|---|---|
| Data is siloed | TimescaleDB as a unified time-series store; all sensor streams ingested automatically |
| Reactive problem detection | Continuous anomaly scoring; pattern-based alerts tied to grow stage, not static thresholds |
| Unpredictable batch outcomes | LightGBM batch outcome model trained on historical batch x environmental condition data |
| Knowledge in people's heads | Annotated batch records; SHAP-explained models that make implicit knowledge explicit |
| No batch outcome prediction | Real-time batch risk score updated as new data arrives; trajectory comparison vs. historical batches |

The platform is built as a set of modular, independently deployable services. Each module delivers value independently; none are required for the others to function.

---

## 5. MVP Scope (Phase 0 and Phase 1)

### Phase 0 — Data Foundation (Current)

**Goal:** Ensure reliable, complete, queryable historical and real-time sensor data exists in a form suitable for ML.

| Feature | Status | Notes |
|---|---|---|
| HA webhook receiver | Complete | Receives all HA entity state changes |
| Entity normalisation and validation | Complete | Maps HA entity IDs to canonical sensor names; validates units and ranges |
| TimescaleDB hypertable schema | Complete | Partitioned by time; 1-minute and 5-minute resolutions |
| AquaPro dosing event ingestion | Complete | Device AQU1AD04A42; events stored with timestamp, volume, nutrient ID |
| Grow batch metadata registry | In progress | Batch ID, strain, room, start/end dates, grow stage schedule |
| Feature engineering pipeline | In progress | Lag features, rolling stats, VPD derivation, DLI accumulation, stage indicators |
| Data quality monitoring | In progress | Missing data rates, sensor fault detection, range exceedance logging |

### Phase 1 — First Models and Recommendations (Next 3–6 Months)

**Goal:** Deploy the first production model and recommendation engine; deliver measurable value to the growing team.

| Feature | Priority | Description |
|---|---|---|
| VPD recommendation engine | P0 | Per-room, per-stage VPD targets with real-time deviation alerts |
| DLI tracking and recommendation | P0 | Real-time DLI accumulation vs. stage target; photoperiod adjustment recommendations |
| EC/pH monitoring and alerting | P0 | Pattern-based (not static threshold) alerts for nutrient solution anomalies |
| Batch outcome model v1 | P1 | LightGBM model: predict yield index from week-3 environmental features |
| Risk scoring dashboard | P1 | Per-batch, per-room risk score (0–100) updated in real time |
| Batch trajectory comparison | P1 | Current batch vs. historical percentile bands for key metrics |
| Recommendation explanation (SHAP) | P1 | Every model output accompanied by top-3 feature contributions in plain language |
| Recommendation audit log | P1 | Every recommendation timestamped; operator response (accepted / overridden / ignored) logged |
| FastAPI recommendation endpoint | P2 | REST API for dashboard and future integrations |
| Grafana dashboard (read-only) | P2 | Pre-built dashboards: batch overview, room environment, risk scores |

### 6. Out of Scope for MVP

The following capabilities are explicitly excluded from the Phase 0 and Phase 1 scope. They are listed here to set expectations and prevent scope creep.

| Out-of-scope Item | Rationale |
|---|---|
| Write-back automation to actuators | Safety and trust must be established first; advisory-only for v1 |
| Deep learning models (LSTM, Transformer) | Insufficient batch history; LightGBM outperforms at low N |
| Mobile application | Web dashboard sufficient for Phase 1; mobile deferred to Phase 2 |
| Multi-facility support | Single facility for v1; multi-tenancy adds architectural complexity |
| Automated nutrient mixing calculations | AquaPro integration is read-only in v1 |
| Computer vision / plant health imaging | Significant infrastructure addition; deferred to Phase 3 |
| Integration with QMS / LIMS | Compliance system integration deferred; requires formal validation |
| Public API or third-party integrations | Internal tooling only for v1 |
| Real-time mobile push notifications | Web-based alerts sufficient for Phase 1 |

---

## 7. User Stories

### Head Grower Stories

**US-001**
*As a* Head Grower,
*I want* to see the current VPD, temperature, humidity, CO₂, and PPFD for every room on a single dashboard,
*So that* I can assess environmental status across the facility at a glance without logging into multiple systems.

**US-002**
*As a* Head Grower,
*I want* to receive an alert when any room's VPD deviates from the stage-appropriate target range for more than 30 minutes,
*So that* I can investigate and correct the issue before it causes measurable plant stress.

**US-003**
*As a* Head Grower,
*I want* to view the current batch's daily light integral accumulation against the target DLI for the current grow stage,
*So that* I can make informed decisions about supplemental lighting or photoperiod adjustments.

**US-004**
*As a* Head Grower,
*I want* to compare the current batch's environmental profile against the historical average for the same strain and grow stage,
*So that* I can identify whether the current batch is tracking normally or accumulating risk.

**US-005**
*As a* Head Grower,
*I want* to see a plain-language explanation of why the system has generated a specific recommendation,
*So that* I can evaluate it critically rather than following it blindly.

**US-006**
*As a* Head Grower,
*I want* to mark a recommendation as "accepted," "overridden," or "not applicable" and record my reasoning,
*So that* my decisions are logged and can be used to improve future recommendations.

**US-007**
*As a* Head Grower,
*I want* to query the historical environmental conditions for any completed batch during any grow stage,
*So that* I can conduct a thorough post-harvest analysis without spending hours extracting data manually.

**US-008**
*As a* Head Grower,
*I want* to receive an early warning when the current batch's trajectory suggests a higher-than-normal risk of a poor outcome,
*So that* I have maximum time to investigate and intervene.

**US-009**
*As a* Head Grower,
*I want* to annotate the batch record with observations (e.g., "noticed early signs of tip burn in Row 3 on Day 22"),
*So that* qualitative observations are captured alongside quantitative sensor data and available for future analysis.

### Facility Manager Stories

**US-010**
*As a* Facility Manager,
*I want* to view a high-level batch status summary showing which batches are on track, which are elevated risk, and which have active alerts,
*So that* I have situational awareness without needing to understand the underlying sensor data.

**US-011**
*As a* Facility Manager,
*I want* to view trend data showing environmental compliance (percentage of time each room was within target parameters) over the past 90 days,
*So that* I can identify rooms or periods that require infrastructure attention.

**US-012**
*As a* Facility Manager,
*I want* to export a batch environmental summary report for any completed batch,
*So that* I have supporting documentation available for compliance audits or quality reviews.

### Data Analyst Stories

**US-013**
*As a* Data Analyst,
*I want* to query the TimescaleDB directly using SQL with appropriate access controls,
*So that* I can perform ad-hoc analysis and model development without being constrained by the UI.

**US-014**
*As a* Data Analyst,
*I want* to register a new trained model version in the model registry with metadata (training data range, evaluation metrics, feature list),
*So that* model versions are tracked and deployments are reproducible.

**US-015**
*As a* Data Analyst,
*I want* to view a live data quality report showing ingestion rates, missing data percentages, and out-of-range readings for each sensor stream,
*So that* I can identify and address data quality issues before they affect model training or recommendations.

**US-016**
*As a* Data Analyst,
*I want* to retrain the batch outcome model with the latest completed batch data and compare its evaluation metrics against the previous version,
*So that* models continuously improve as more batch history accumulates.

**US-017**
*As a* Data Analyst,
*I want* to access SHAP feature importance values for any model prediction via the API,
*So that* I can build explainability interfaces and debug unexpected recommendations.

---

## 8. Success Metrics

### Phase 1 Quantitative Targets

| Metric | Target | Measurement Method |
|---|---|---|
| Batch outcome model MAE (yield index) | < 8% of mean yield | Hold-out evaluation on most recent 2 batches |
| Recommendation adoption rate | > 60% accepted or acted upon | Audit log: accepted / (accepted + overridden) |
| Time to identify an active risk condition | < 30 minutes from onset | Retrospective comparison: alert timestamp vs. manual detection timestamp |
| Operator time saved (batch analysis) | > 2 hours per batch | Grower survey pre/post deployment |
| False positive rate (risk alerts) | < 20% | Audit log: operator marks as "not applicable" |
| Dashboard availability | > 99.5% during operational hours (06:00–22:00 NZST) | Uptime monitoring |
| Ingestion pipeline latency | < 2 minutes (sensor reading → stored in TSDB) | Pipeline metrics |
| Data completeness | > 98% of expected readings present | Data quality report |

### Phase 2 Aspirational Targets

| Metric | Target |
|---|---|
| Batch-to-batch yield variance reduction | > 15% reduction vs. pre-platform baseline |
| Early risk detection rate | > 80% of adverse events flagged > 24h before they become critical |
| Knowledge capture coverage | > 90% of major grower decisions annotated in the system |

---

## 9. Technical Constraints

### Data Constraints

- **Limited batch history.** At the time of Phase 1 model training, the facility has approximately 10–20 completed batches with reliable sensor records. This constrains model complexity significantly. Deep learning models are not viable at this batch count. See [Theory Document](./theory.md) for detailed justification.
- **Single facility.** All training data comes from one facility with one set of rooms, one HVAC configuration, and one set of strains. Models will not generalise to other facilities without retraining.
- **Historical data gaps.** Pre-platform sensor data in Home Assistant's recorder database has variable completeness depending on HA version, recorder settings, and historical hardware issues. Gap-handling strategy is documented in the data dictionary.
- **Label quality.** Batch outcome labels (yield, cannabinoid profile) are manually recorded and subject to measurement and transcription error.

### Infrastructure Constraints

- **On-premises only.** No cloud compute. All processing runs on facility hardware. Model training must complete in a reasonable time on available hardware.
- **Network topology.** The CI platform is on the facility LAN. Internet connectivity is available but not relied upon for operations.
- **New Zealand timezone.** All timestamps stored in UTC; display layer converts to NZST (UTC+12 standard, UTC+13 daylight saving).

### Regulatory Constraints

- **Data residency.** All data must remain on-premises (see Executive Summary for detail).
- **Audit trail.** The platform must maintain an immutable audit log of all recommendations and operator responses.
- **No unapproved automation.** Any write-back to controlled equipment requires explicit management sign-off and a defined safety review process.

---

## 10. Non-Functional Requirements

### Availability

- The ingestion pipeline must run 24/7 with < 0.5% downtime per month during normal operations.
- The recommendation API and dashboard must be available during operational hours (06:00–22:00 NZST) with < 0.5% downtime.
- Planned maintenance windows should be scheduled outside operational hours.
- Pipeline failure must not affect facility operations (see fail-safe principle in [Architecture](./architecture.md)).

### Latency

- Sensor reading to stored in TimescaleDB: < 2 minutes end-to-end.
- Sensor reading to updated recommendation: < 5 minutes end-to-end.
- Dashboard page load: < 3 seconds for standard views.
- API response for recommendation queries: < 500 ms at P95.

### Auditability

- Every sensor reading must be stored with its original timestamp, HA entity ID, and ingestion timestamp.
- Every model prediction must be stored with the model version, input feature values, and timestamp.
- Every recommendation must be stored with its source prediction, confidence interval, and timestamp.
- Every operator response to a recommendation must be stored with the operator ID, response type, and timestamp.
- Audit logs must be append-only (no updates or deletions).

### Explainability

- Every model recommendation surfaced to operators must be accompanied by a plain-language explanation of the top contributing factors.
- SHAP values must be computed for every prediction and stored alongside the prediction.
- Model cards must be maintained for every model version in production, documenting training data, evaluation metrics, known limitations, and intended use.

### Security

- API endpoints must require authentication (API key or JWT).
- Read-only database access for the dashboard service; write access only for the ingestion and training services.
- HA API token stored in environment variables, never in code.
- Network access to TimescaleDB restricted to the application subnet.

---

## 11. Competitor and Alternatives Analysis

### Alternative 1: Spreadsheets (Current Partial Practice)

**Description:** Environmental data extracted manually from HA, pasted into spreadsheets, analysed with Excel or Google Sheets formulas.

**Strengths:** Zero infrastructure cost; team is already familiar with the format; no new software to learn.

**Weaknesses:** Manual and time-consuming; not real-time; prone to transcription errors; does not scale to cross-batch analysis; no alerting; knowledge is not reusable or transferable; no predictive capability.

**Why CI is better:** Automated ingestion eliminates manual data work; real-time alerting is not possible with spreadsheets; statistical models across multiple batches cannot be maintained manually at any reasonable scale.

### Alternative 2: Commercial Grow Management Software

**Description:** Purpose-built SaaS platforms for commercial cannabis cultivation (e.g., Growlink, Argus Controls, TrolMaster, or similar).

**Strengths:** Purpose-built for the domain; some include automation and integration capabilities; vendor support.

**Weaknesses:** Significant licensing cost (typically USD 10,000–50,000+ per year for commercial facilities); vendor lock-in for data; data residency concerns for NZ-based facility; limited customisability; may not support the specific sensor hardware already deployed; generic models not trained on facility-specific data; black-box recommendations with no explainability.

**Why CI is better:** Zero licensing cost; full control over data; models trained on Legacy Ag's own data; full explainability; integrates with existing HA infrastructure without replacement; on-premises data residency.

### Alternative 3: Generic Time-Series Dashboarding (Grafana + InfluxDB / Prometheus)

**Description:** Extend the existing Grafana setup with a purpose-built time-series database (InfluxDB or Prometheus) and build comprehensive dashboards.

**Strengths:** Excellent visualisation; relatively simple to implement; good community support; no ML complexity.

**Weaknesses:** Dashboards show historical data — they do not recommend, predict, or learn. Static threshold alerting is already available in HA. This approach addresses the visibility problem but not the prediction, recommendation, or knowledge capture problems.

**Why CI is better:** CI includes dashboarding capabilities (via Grafana integration) while adding ML-based recommendations, batch outcome prediction, and intelligent alerting that generic dashboarding cannot provide. The two approaches are not mutually exclusive — CI uses Grafana as its primary visualisation layer.

### Alternative 4: Manual Statistical Analysis by External Consultant

**Description:** Engage a data science consultant to periodically analyse batch data and produce reports.

**Strengths:** No infrastructure investment; access to specialist expertise on demand.

**Weaknesses:** Not real-time; insights are retrospective, not prospective; expensive on an ongoing basis; knowledge leaves with the consultant; no persistent system; not integrated into operational workflow.

**Why CI is better:** CI provides continuous, real-time analysis integrated directly into the operational workflow, at zero marginal cost per recommendation once deployed.

---

## 12. Appendix: Open Questions and Decisions Pending

| Question | Owner | Target Resolution |
|---|---|---|
| What batch outcome metric is the primary prediction target? (yield weight vs. cannabinoid index vs. combined score?) | Head Grower + Technical Lead | Phase 1 kickoff |
| How will grow stage boundaries be defined programmatically? (calendar-based vs. sensor-triggered?) | Head Grower + Data Analyst | Feature engineering sprint |
| What is the minimum confidence threshold below which a recommendation should be suppressed? | Head Grower + Data Analyst | Phase 1 model evaluation |
| Should the risk score be a single composite or multi-dimensional? | Head Grower + Facility Manager | Dashboard design sprint |
| What is the process for a grower to flag that a recommendation was wrong? | All stakeholders | UX design sprint |

---

*Document owner: Technical Lead. Review cycle: quarterly or on major scope change. This document supersedes all previous product requirement notes.*
