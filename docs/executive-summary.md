# Executive Summary

**Cultivation Intelligence — Decision Support Platform for Legacy Ag Limited**

*Prepared for: Facility Owner / Management Team*
*Date: March 2026*
*Classification: Internal — Confidential*

---

## What This System Does

Cultivation Intelligence is an internal software platform that collects data from the sensors already installed throughout the Legacy Ag Limited facility, organises that data into a usable form, and then uses it to help the growing team make better, more consistent decisions. Think of it as a highly organised logbook that never loses information, combined with an analytical assistant that can spot patterns across multiple batches and flag potential problems before they become serious.

Every environmental reading taken by the facility's sensors — air temperature, humidity, CO₂ levels, light intensity, nutrient solution strength and acidity, and substrate moisture — flows automatically into the system and is stored in a structured database. The platform then applies statistical and machine learning techniques to that data, producing recommendations such as "VPD is trending high in Room 2 during the afternoon — consider adjusting the dehumidification schedule" or "Batch EC readings in the first two weeks of flower are historically correlated with lower-than-expected yield — current batch is tracking 12% below the target profile." These recommendations appear on a dashboard and, where urgency warrants it, trigger an alert to the relevant team member.

Critically, the system does not control anything. It does not adjust dosing, open vents, or change lighting schedules on its own. Every recommendation is reviewed and acted upon — or consciously set aside — by an experienced member of the growing team. The platform is a tool in the hands of skilled professionals, not a replacement for them.

---

## Why This Was Built

### The Core Problem: Data That Cannot Be Used

The Legacy Ag Limited facility generates an enormous volume of sensor data every day. Dozens of sensors report environmental readings every minute; the AquaPro dosing unit logs every irrigation and nutrient adjustment event; growers record observations in shift notes and batch logs. This information exists, but it lives in disconnected places.

Home Assistant holds the raw sensor history, but querying across weeks or months is cumbersome. Batch records are in spreadsheets. Shift notes are in a shared document. When something goes wrong mid-batch — a pH excursion, an unexplained VPD spike, a slower-than-expected root development rate — the team must manually piece together what happened by hunting across multiple systems. When batch outcomes vary from one run to the next, it is difficult to pinpoint which environmental conditions during which grow stage drove that difference, because the data to answer that question has never been unified in one place.

The result is that decision-making is largely reactive. Problems are detected when they are visible — often too late to course-correct without impact to yield or quality. And the knowledge of what works and what does not is held primarily in the heads of individual staff members, rather than encoded in a system that can persist and improve over time.

### The Opportunity

The sensor network is already in place. The data is already being generated. What has been missing is a layer that ingests that data systematically, stores it in a form that supports analysis, and applies analytical methods to surface insights that would take a human analyst many hours to produce manually. That is precisely what this platform provides.

---

## How It Works — Without the Technical Details

The system operates in four broad stages, and a useful way to think about it is the cycle: **Collect → Organise → Analyse → Advise**.

**Collect.** Every 60 seconds, environmental sensors throughout the facility report their readings to the Home Assistant hub. The Cultivation Intelligence platform receives those readings automatically. Nutrient dosing events from the AquaPro unit are also captured as they occur. Nothing extra needs to be done by staff — the data flows in the background.

**Organise.** Raw sensor readings are cleaned, validated, and stored in a structured time-series database. Invalid readings (sensor faults, communication dropouts) are flagged and excluded. The system then derives additional calculated values — Vapour Pressure Deficit (a measure of plant transpiration stress), Daily Light Integral (total light energy received per day), and rolling averages across different time windows — that are more directly useful for growing decisions than the raw numbers alone.

**Analyse.** The organised data is fed into machine learning models that have been trained on the facility's own historical batch data. These models learn what environmental conditions during each stage of the grow cycle have historically corresponded with better or worse outcomes. They also learn what "normal" looks like so that deviations can be flagged as potential risks.

**Advise.** Model outputs are translated into plain-language recommendations and risk alerts that appear on a simple dashboard. Each recommendation explains its reasoning in terms a grower can evaluate and challenge. The team decides what action, if any, to take.

---

## Advisory-First Philosophy

This system is designed on a principle of earned trust. Machine learning models, even well-built ones, make mistakes. In a licensed medicinal cannabis facility where product quality, compliance, and consistency are paramount, a wrong automated action is far more costly than a correct automated action is beneficial.

For this reason, the platform is built to advise, not to act. In the first phase of deployment, every insight produced by the system is surfaced to a human operator who makes the final decision. Over time, as the team develops confidence in specific recommendations — by observing that the system's alerts reliably precede problems, and that its environmental targets reliably correlate with good batch outcomes — the scope of what the system recommends can gradually expand, and eventually, for well-understood and low-risk decisions, a degree of automation may be appropriate.

Trust is built through transparency. Every recommendation the system makes is accompanied by an explanation: which sensor readings triggered it, what the historical pattern is, and how confident the model is. The system never says "do this" without explaining "because of this." If a grower disagrees with a recommendation, their decision is logged alongside the recommendation. Over time, this creates a feedback loop that improves the models and reveals where human judgement consistently overrides the system — a useful signal in itself.

---

## What This System Is Not

It is important to be clear about what this platform does not do, both now and in its intended future form.

**It is not a replacement for experienced growers.** Cannabis cultivation — particularly for medicinal-grade product — involves nuanced, context-dependent decisions that draw on years of practical experience, visual observation, and facility-specific knowledge. The system can identify statistical patterns in sensor data; it cannot replace a trained eye or the intuition that comes from deep experience. The role of the platform is to give experienced growers better information, faster, so they can apply their expertise more effectively.

**It is not a fully autonomous control system.** Version 1 does not write back to any actuators, dosing systems, or environmental controllers. It reads data and produces recommendations. Any automation of facility controls remains entirely within the existing Home Assistant setup, configured and managed by the team.

**It is not a compliance or quality management system.** The platform does not replace the facility's existing record-keeping obligations under New Zealand medicinal cannabis regulations. It may complement those records by providing detailed environmental audit trails, but it is not designed or validated as a regulatory compliance tool.

**It is not infallible.** The models are trained on a limited history of batches from a single facility. Their recommendations should always be considered in context, not followed blindly. The system's value grows as more batch data accumulates.

---

## Current Capabilities vs. Future Roadmap

### What Works Today (Phase 0)

The data collection and storage infrastructure is operational. Sensor readings from the facility's Zigbee and ESPHome network flow automatically into the platform's time-series database via Home Assistant. Dosing events from the AquaPro unit (serial AQU1AD04A42) are captured. Data is retained indefinitely, timestamped, and available for query and analysis.

A feature engineering pipeline is under active development. This pipeline transforms raw sensor readings into the structured analytical inputs that machine learning models require — things like rolling averages, cumulative daily light sums, and grow-stage-aware calculations.

### What Is Coming in Phase 1 (Next 3–6 Months)

The first machine learning models will be trained on the historical batch data collected to date. The initial focus will be a batch outcome model that predicts expected yield index based on environmental conditions through the grow cycle, and a real-time VPD and daily light integral recommendation engine that advises on environmental targets for each grow stage.

A risk scoring dashboard will surface active alerts for conditions that have historically correlated with poor outcomes: EC drift outside target range, pH excursions, prolonged VPD stress, and insufficient or excessive light accumulation.

### Longer-Term Aspirations (Phase 2 and Beyond)

As the volume of historical batch data grows, more sophisticated models become viable. Multi-step-ahead environmental forecasting (predicting where temperature and humidity will be in four hours, given current trends and the lighting schedule) will allow more proactive environmental management. Knowledge capture features will allow experienced growers to annotate batch records with observations that can be incorporated into model training. Eventually, for specific low-risk decisions with well-established patterns, limited automation may be appropriate — but this is at minimum 12–18 months away and is subject to the team's evolving confidence in the system.

---

## Key Benefits Expected

### Consistency Across Batches

By maintaining a complete environmental record for every batch and comparing it against historical data, the platform makes it far easier to identify why one batch performed differently from another and to deliberately replicate the conditions associated with high-quality outcomes.

### Early Risk Detection

Many environmental problems that affect plant health and yield quality develop gradually over hours or days before they become visually apparent. The platform's continuous monitoring and pattern-matching can flag deviations from expected profiles earlier than manual observation, giving the team more time to intervene.

### Knowledge Capture and Institutional Memory

When experienced staff leave or change roles, their knowledge of what works in this specific facility leaves with them. The platform encodes some of that knowledge in its models and in the annotated batch records. Over time, the system becomes a repository of facility-specific best practice that persists regardless of staff changes.

### Reduced Manual Data Work

The team currently spends time manually compiling data from multiple sources to answer questions about batch history. The platform makes that data available in one place, queryable in seconds, freeing up time for higher-value work.

### Foundation for Future Optimisation

The data infrastructure being built now is the foundation for any future capability — whether that is more sophisticated models, automated controls, or multi-facility expansion. The investment in proper data collection now pays dividends across all future development.

---

## Integration with the Existing Home Assistant Setup

One of the core design principles of this platform is that it does not require a "forklift upgrade" — replacing the existing facility control and monitoring infrastructure. Home Assistant, ESPHome, and the Zigbee sensor network remain exactly as they are. The Cultivation Intelligence platform is an additional layer that reads data from Home Assistant without modifying or interfering with any existing automations or controls.

Technically, the platform connects to Home Assistant via its existing REST API and webhook functionality. From Home Assistant's perspective, the Cultivation Intelligence platform is simply another consumer of its data, no different from the existing Grafana dashboards. The AquaPro dosing unit's event data is captured via the same mechanism.

This means that if the Cultivation Intelligence platform were to fail for any reason — a software bug, a server restart, a database issue — it has zero effect on facility operations. The lights stay on, the dosing continues, the sensors keep reporting. The CI platform is purely additive.

---

## Data Privacy and New Zealand Compliance Considerations

All facility data remains on-premises. The platform runs on hardware within the Legacy Ag Limited facility network and does not transmit sensor data, batch records, or any other facility information to external servers or cloud services. This satisfies data residency requirements and ensures that commercially sensitive production data is never exposed to third parties.

The open-source components used in the platform (FastAPI, TimescaleDB, LightGBM, etc.) are well-established, widely audited tools with no proprietary data collection or telemetry. There are no SaaS subscriptions or vendor relationships that involve data sharing.

From a regulatory perspective under the Misuse of Drugs (Medicinal Cannabis) Regulations 2019 and associated guidance from the Ministry of Health, the platform functions as an internal operational tool. It does not handle patient data, does not replace required batch record-keeping systems, and does not form part of the product release or quality control process. These remain as currently implemented.

Should the platform's environmental records ever be useful as supplementary evidence for audit or compliance purposes, the data is archived in a tamper-evident time-series database with full timestamps and provenance.

---

## Investment Summary

The Cultivation Intelligence platform is built entirely on open-source software with no licensing fees. The primary costs are:

**Development time.** The platform has been designed and is being built by the internal technical team. Ongoing development time is the primary investment.

**Infrastructure.** The platform runs on a dedicated on-premises server within the facility. Hardware costs are modest — a mid-range server with sufficient storage for several years of sensor data is adequate for the current facility scale. The existing facility network requires no modification.

**Maintenance.** Once the initial phases are deployed, day-to-day operational overhead is low. The platform is designed for stability and observability — failures are surfaced immediately through the monitoring system, and routine maintenance tasks are documented in the operations runbook.

There are no per-seat licensing fees, no cloud compute costs, no vendor contracts, and no ongoing SaaS subscriptions. The total cost of ownership is primarily staff time.

---

## Frequently Asked Questions from Stakeholders

**Q: Can this system make decisions without telling us?**

No. In the current version, the system produces recommendations that are displayed to an operator. Nothing happens unless a person reads the recommendation and takes action. The system has no connection to any actuator, valve, pump, or lighting controller. That independence is a deliberate design choice.

**Q: What happens if the system has a bug or goes offline?**

If the Cultivation Intelligence platform stops running for any reason, facility operations are unaffected. The Home Assistant automation platform, which manages all facility controls, operates completely independently of this system. The CI platform is a reader of data — it does not control anything. A failure in CI means the team loses visibility into the analytical layer; it does not affect temperature control, lighting, irrigation, or nutrient dosing.

**Q: How long before we see real value from this?**

The data collection infrastructure (Phase 0) provides immediate value: the team gains a complete, queryable archive of all sensor data that previously existed only in fragmented form. The first machine learning recommendations (Phase 1) are expected within three to six months. More sophisticated capabilities improve continuously as more batch history accumulates.

**Q: Is our production data secure?**

All data is stored on hardware physically located within the facility. Nothing is sent to the internet or to any third-party service. The platform is isolated to the facility's internal network.

**Q: Does this require us to change how we work?**

The minimum viable change is for the Head Grower to check the recommendation dashboard as part of their routine — likely a two-to-five minute addition to the morning walk-through. Over time, the expectation is that the system becomes a natural part of decision-making, but the pace of integration is set by the team's comfort and confidence in the recommendations, not by a predetermined schedule.

---

## Governance and Oversight

The Cultivation Intelligence platform is an internal tool developed and maintained by Legacy Ag Limited. Key governance decisions include:

**Who owns the system?** The Technical Lead owns the software and infrastructure. The Head Grower is the primary subject-matter authority for cultivation decisions encoded in the system. The Facility Manager has operational oversight.

**Who can promote a model to production?** Model promotion (making a new trained model the active recommendation engine) requires sign-off from both the Data Analyst (technical evaluation) and the Head Grower (domain evaluation). The Facility Manager is informed of all production model changes.

**How are operator responses to recommendations used?** Operator responses — accepted, overridden, or not applicable — are logged and used as signal for model improvement. Systematically overridden recommendations are reviewed jointly by the Data Analyst and Head Grower to determine whether the model is wrong or the domain rule needs updating.

**How is the system audited?** The Technical Lead produces a monthly operational summary covering: data ingestion completeness rates, recommendation generation and adoption rates, model performance drift indicators, and any data quality incidents. This summary is shared with the Facility Manager.

---

## Summary

Cultivation Intelligence is a practical, incremental investment in making Legacy Ag Limited's existing sensor data useful for decision-making. It does not replace the expertise of the growing team; it amplifies it by putting better information in front of the right people at the right time. It is built on proven open-source technology, runs entirely on-premises, and is designed to grow in capability as the team's confidence in it grows. The foundation being built now — reliable data collection, structured storage, and an explainable recommendation framework — positions the facility well for the next stage of operational maturity.

The system reflects a simple conviction: the data to make better decisions is already being generated in this facility every minute of every day. The question has only been whether it is organised in a way that makes those decisions easier. Cultivation Intelligence is the answer to that question.

---

*For technical details, refer to the [Architecture Document](./architecture.md). For the full product specification, refer to the [Product Brief](./product-brief.md). For ML methodology, refer to the [Theory Document](./theory.md).*
