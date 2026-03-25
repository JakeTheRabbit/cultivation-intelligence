---
name: Bug Report
about: Report a defect or unexpected behavior in the cultivation intelligence system
title: "[BUG] "
labels: bug
assignees: ""
---

## Describe the Bug

<!-- Provide a clear and concise description of the bug. What happened? What did you observe? -->

## Component Affected

<!-- Check all components where you observed the issue. -->

- [ ] Sensor Ingestion (MQTT, polling, raw data pipeline)
- [ ] Feature Engineering (time-series aggregation, derived metrics)
- [ ] Model Training (training pipeline, dataset preparation)
- [ ] Inference / Predictions (real-time scoring, batch predictions)
- [ ] Recommendations Engine (grow advice, alerts, action suggestions)
- [ ] Controls / Safety Systems (environment control, fail-safes, thresholds)
- [ ] Home Assistant Integration (entities, webhooks, automations)
- [ ] AquaPro Integration (nutrient dosing, EC/pH readings)
- [ ] API (FastAPI endpoints, authentication, rate limiting)
- [ ] Database (TimescaleDB queries, migrations, continuous aggregates)
- [ ] Monitoring / Observability (metrics, dashboards, alerts)
- [ ] Documentation

## To Reproduce

<!-- Provide clear, numbered steps to reproduce the behavior. Be as specific as possible. -->

1.
2.
3.
4.

**Reproducible consistently?**
- [ ] Yes, always
- [ ] Intermittent / flaky
- [ ] Happened once, unable to reproduce

## Expected Behavior

<!-- What did you expect to happen? -->

## Actual Behavior

<!-- What actually happened? How does it differ from the expected behavior? -->

## Environment

| Field | Value |
|---|---|
| OS / Platform | <!-- e.g., Ubuntu 22.04, Raspberry Pi OS, macOS 14 --> |
| Python Version | <!-- e.g., 3.11.8 --> |
| App Version / Git SHA | <!-- e.g., v0.4.2 or commit abc1234 --> |
| Deployment Method | <!-- Docker Compose / bare metal / Kubernetes --> |
| TimescaleDB Version | <!-- e.g., 2.14 on pg15 --> |
| Redis Version | <!-- e.g., 7.2 --> |
| Home Assistant Version | <!-- if HA integration is involved, e.g., 2024.3.0 --> |
| AquaPro Firmware | <!-- if AquaPro integration is involved --> |

## Logs

<!-- Paste relevant error output, stack traces, or log lines here. -->
<!-- Tip: Set LOG_LEVEL=DEBUG to get more detail. Redact any sensitive values. -->

```
# Error output / stack trace
```

**Application logs (structured JSON):**
<details>
<summary>Click to expand</summary>

```json

```

</details>

## Data Context

<!-- If the bug is related to a specific grow, batch, or sensor reading, provide context here. -->
<!-- This helps reproduce issues related to specific data states. Redact any sensitive identifiers if needed. -->

- **Grow stage affected:** <!-- e.g., Late Flower, Week 6 -->
- **Batch ID (if applicable):** <!-- e.g., batch-2024-03-A -->
- **Sensor types affected:** <!-- e.g., VPD sensor, CO2, canopy temp -->
- **Approximate time of occurrence:** <!-- e.g., 2024-03-15 ~14:30 UTC -->
- **Active grow parameters at time of bug:** <!-- e.g., lights-on, high CO2 enrichment -->

## Possible Fix

<!-- If you have a hypothesis about what is causing this bug or how it might be fixed, describe it here. -->
<!-- This is optional but helps prioritize and resolve the issue faster. -->

## Additional Context

<!-- Add any other context about the problem here. -->
<!-- e.g., Did this work in a previous version? Does it affect all grow rooms or just one? -->
<!-- Screenshots, sensor graphs, or metric dashboard snapshots are welcome. -->
