# Operations Runbook — Cultivation Intelligence System
## Legacy Ag Limited | Indoor Medicinal Cannabis Facility | New Zealand

**Document Version:** 1.0
**Last Updated:** 2025-01-20
**Audience:** Facility Operators, Cultivation Managers, System Maintainers
**Classification:** Internal — Restricted

---

## Table of Contents

1. [System Overview for Operators](#1-system-overview-for-operators)
2. [Starting and Stopping the System](#2-starting-and-stopping-the-system)
3. [Daily Operator Checklist](#3-daily-operator-checklist)
4. [Weekly Maintenance](#4-weekly-maintenance)
5. [Common Operational Tasks](#5-common-operational-tasks)
6. [Troubleshooting Guide](#6-troubleshooting-guide)
7. [Alert Response Procedures](#7-alert-response-procedures)
8. [Database Maintenance](#8-database-maintenance)
9. [Log Access](#9-log-access)
10. [Configuration Changes](#10-configuration-changes)
11. [Contact and Escalation](#11-contact-and-escalation)

---

## 1. System Overview for Operators

The Cultivation Intelligence system is a decision-support platform that monitors grow environment sensors, tracks batch progress, generates risk scores, and produces agronomic recommendations. It does **not** automatically control any equipment. All actions remain with the operator.

### What the System Does

- Continuously ingests sensor telemetry from Home Assistant (temperature, humidity, CO2, VPD, PPFD, EC, pH, dissolved oxygen, water temperature)
- Stores all readings in a time-series database (TimescaleDB) with full historical retention
- Computes risk scores for active batches every 15 minutes based on deviation from target environmental profiles
- Generates plain-language recommendations when environmental parameters drift outside optimal windows
- Provides a prediction of expected yield and quality for each active batch, updated daily
- Maintains an audit trail of all operator actions (recommendations acknowledged, manual interventions logged)

### What the System Does Not Do

- The system does **not** send commands to any lighting, HVAC, fertigation, or irrigation hardware
- The system does **not** automatically adjust any grow environment settings
- The system does **not** lock operators out of any controls
- All decisions and physical interventions remain the operator's responsibility

### Architecture Summary

```
Home Assistant (HA)  ──webhook/polling──▶  Ingest Service  ──▶  TimescaleDB
                                                                      │
                                                             Feature Pipeline
                                                                      │
                                                             LightGBM Models
                                                                      │
                                                          Risk Scorer + Recommendation Engine
                                                                      │
                                                             FastAPI REST API
                                                                      │
                                                              Operator Dashboard (UI)
```

All components run as Docker containers on the facility server. Redis is used for caching computed features between pipeline runs.

### Data Flows

- **Ingest:** HA pushes sensor state changes via webhook; the ingest service validates, normalises units, and writes to TimescaleDB.
- **Feature Pipeline:** Runs every 15 minutes via APScheduler; reads raw sensor data, computes rolling statistics (mean, std, percentile over 1h/6h/24h windows), and caches results in Redis.
- **Risk Scoring:** Reads cached features, compares against batch-phase target profiles, produces a 0–100 risk score per batch.
- **Recommendations:** When risk score crosses a threshold, the recommendation engine creates a recommendation record and marks it pending operator review.
- **Model Inference:** Daily at 06:00 NZST, the yield/quality prediction model runs on each active batch and writes updated predictions to the database.

---

## 2. Starting and Stopping the System

### Prerequisites

- Docker Desktop (or Docker Engine + Compose) must be running on the server
- The `.env` file must be present in the project root with all required variables (see `.env.example`)
- You must be in the project root directory: `cd /opt/cultivation-intelligence`

### Starting the Full System

```bash
# Start all services in detached mode
docker compose up -d

# Verify all containers started
docker compose ps
```

Expected output — all services should show `Up` or `healthy`:

```
NAME                        STATUS          PORTS
cultivation-api             Up (healthy)    0.0.0.0:8000->8000/tcp
cultivation-worker          Up
cultivation-ui              Up (healthy)    0.0.0.0:3000->3000/tcp
timescaledb                 Up (healthy)    0.0.0.0:5432->5432/tcp
redis                       Up (healthy)    0.0.0.0:6379->6379/tcp
```

### Health Check Verification

After starting, verify each service is healthy before declaring the system operational:

```bash
# API health check
curl -s http://localhost:8000/health | python3 -m json.tool

# Expected response:
# {
#   "status": "healthy",
#   "database": "connected",
#   "redis": "connected",
#   "ha_connection": "connected",
#   "last_ingest": "2025-01-20T08:14:32+13:00",
#   "active_batches": 3
# }

# Check TimescaleDB directly
docker compose exec timescaledb pg_isready -U cultivation

# Check Redis
docker compose exec redis redis-cli ping
# Expected: PONG

# Check HA connectivity
curl -s http://localhost:8000/health/ha | python3 -m json.tool
```

If `ha_connection` shows `disconnected`, see [Section 6 — Troubleshooting](#6-troubleshooting-guide).

### Stopping the System

```bash
# Graceful stop (preserves all data, containers can restart)
docker compose stop

# Stop and remove containers (preserves database volumes — data is safe)
docker compose down

# DANGER: Stop and remove containers AND volumes (destroys all data — do not run unless instructed)
# docker compose down -v   # DO NOT RUN WITHOUT EXPLICIT AUTHORISATION
```

### Restarting a Single Service

If only one component needs to be restarted (e.g., after a config change):

```bash
# Restart just the API
docker compose restart cultivation-api

# Restart just the worker (feature pipeline, scheduler)
docker compose restart cultivation-worker

# Restart just the UI
docker compose restart cultivation-ui
```

### Checking Service Logs After Start

```bash
# Tail all logs
docker compose logs -f

# Tail only the API logs
docker compose logs -f cultivation-api

# Check for startup errors
docker compose logs cultivation-api | grep -i error
```

---

## 3. Daily Operator Checklist

Complete this checklist each morning before the first grow room walkthrough. The dashboard URL is `http://[server-ip]:3000`.

### Morning Checklist (target: 07:00–07:30 NZST)

**Step 1 — Verify System is Running**

- [ ] Navigate to dashboard and confirm the status indicator shows green
- [ ] Check `http://[server-ip]:8000/health` shows `"status": "healthy"`
- [ ] Note the `last_ingest` timestamp — it should be within the last 5 minutes

**Step 2 — Check Sensor Feed Freshness**

- [ ] In the dashboard, navigate to **Monitoring → Sensor Status**
- [ ] Every sensor entity should show a "Last Seen" timestamp within the last 10 minutes
- [ ] Any sensor showing as "Stale" (red indicator) must be investigated before proceeding
- [ ] Typical freshness check via API:

```bash
curl -s http://localhost:8000/api/v1/sensors/freshness | python3 -m json.tool
```

Expected: all entities with `"status": "fresh"` and `last_seen` within the past 10 minutes.

**Step 3 — Review Pending Recommendations**

- [ ] Navigate to **Recommendations → Pending**
- [ ] Read each pending recommendation carefully
- [ ] For each recommendation, either:
  - **Acknowledge and act:** Mark as accepted, record the action taken in the notes field
  - **Acknowledge and dismiss:** Mark as dismissed with a reason (e.g., "Already corrected manually at 06:45")
  - **Defer:** Leave pending only if you intend to act within the next 2 hours — do not leave recommendations pending indefinitely
- [ ] No recommendation should remain pending for more than 4 hours

**Step 4 — Check Risk Scores**

- [ ] Navigate to **Batches → Active**
- [ ] Review the current risk score for each active batch
- [ ] Risk score thresholds:
  - **0–30 (Green):** Normal — no immediate action required
  - **31–60 (Amber):** Elevated — review environmental data, verify parameters are trending correctly
  - **61–80 (Red):** High — immediate investigation required, likely a recommendation waiting
  - **81–100 (Critical):** Emergency — escalate immediately, check grow room conditions in person
- [ ] If any batch shows Amber or above, drill into **Batch Detail → Environmental Timeline** to identify the parameter causing the elevation

**Step 5 — Verify Home Assistant Connectivity**

- [ ] Dashboard header should show "HA Connected" with a green dot
- [ ] Navigate to **Monitoring → HA Status**
- [ ] Verify the entity count matches expected (compare against your entity inventory list)
- [ ] If entity count has dropped, a sensor may have gone offline

**Step 6 — Log Daily Check Completion**

- [ ] In the dashboard, navigate to **Operations → Daily Log**
- [ ] Click "Record Daily Check" and add a brief note (e.g., "All systems nominal, Batch 24 amber on humidity — adjusted dehumidifier setpoint")
- [ ] This creates an audit record of the check

---

## 4. Weekly Maintenance

Perform weekly maintenance every Monday before 10:00 NZST. Estimated time: 45–60 minutes.

### Data Quality Review

```bash
# Generate a data quality report for the past 7 days
docker compose exec cultivation-worker python scripts/data_quality_report.py --days 7

# Output will be saved to: reports/data_quality_YYYY-MM-DD.json
# Also displayed in dashboard under: Monitoring → Data Quality → Weekly Report
```

- [ ] Review the report for any entities with gap rates above 5% (more than 5% of expected readings missing)
- [ ] Check for any entities flagged with `"anomaly_detected": true` — these may indicate sensor hardware issues
- [ ] If a sensor shows persistent gaps, cross-reference with HA logs to determine if the entity went offline

### Model Prediction Accuracy Review

Once per week, compare recent model predictions against actuals for batches that completed during the week:

```bash
# Compare predictions vs actuals for completed batches in the last 30 days
docker compose exec cultivation-worker python scripts/model_accuracy_report.py --days 30

# Output: reports/model_accuracy_YYYY-MM-DD.json
```

- [ ] Review MAE (Mean Absolute Error) for yield predictions — target: < 20% relative error
- [ ] If MAE has risen above 25% for two consecutive weeks, flag for model retraining review
- [ ] Document any batches where predictions were materially wrong and the operator's explanation of why

### Backup Verification

- [ ] Verify automated backups ran successfully:

```bash
# List recent backups
ls -lh /backups/timescaledb/ | tail -10

# Verify the most recent backup is less than 25 hours old
stat /backups/timescaledb/$(ls -t /backups/timescaledb/ | head -1) | grep Modify
```

- [ ] Confirm backup file sizes are consistent with prior weeks (a sudden drop in size may indicate a partial backup)
- [ ] Once per month, perform a restore test to a separate environment (see Section 8)

### Log Rotation Check

```bash
# Check log volume hasn't grown unexpectedly
docker compose exec cultivation-api du -sh /app/logs/
```

- [ ] Log volume should not grow by more than ~100MB per week under normal operation
- [ ] If growing rapidly, check for repeated errors being logged at DEBUG level (see Section 9)

### Disk Space Check

```bash
# Overall disk usage
df -h

# Docker volumes specifically
docker system df
```

- [ ] Database volume should have at least 20% free space at all times
- [ ] If disk usage exceeds 80%, follow the Database Maintenance section to run compression and review retention policies

---

## 5. Common Operational Tasks

### Task 1 — Adding a New Batch

A new batch must be registered in the system when a new propagation or veg/flower cycle begins. This links all subsequent sensor readings and model predictions to the correct batch record.

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/batches \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CULTIVATION_API_TOKEN" \
  -d '{
    "batch_code": "LAL-B025",
    "strain": "CBD Isolate #3",
    "room_id": "flower-room-1",
    "phase": "early_flower",
    "start_date": "2025-01-20",
    "target_harvest_date": "2025-03-15",
    "plant_count": 96,
    "notes": "Clones from mother stock M-12, day 1 of 12/12 lighting"
  }'
```

**Via Dashboard UI:**

1. Navigate to **Batches → New Batch**
2. Fill in all required fields:
   - Batch Code (must match your internal tracking system)
   - Strain name (select from dropdown or add new)
   - Grow Room assignment
   - Current phase (propagation / veg / early_flower / late_flower)
   - Start date and target harvest date
   - Plant count
3. Click **Create Batch**
4. The system will immediately begin tracking environmental data against this batch
5. Navigate to the new batch record and verify it shows the correct room's sensor feeds

**Verification:**

```bash
curl -s http://localhost:8000/api/v1/batches/LAL-B025 | python3 -m json.tool
```

Confirm `"status": "active"` and `"sensors_linked": true`.

---

### Task 2 — Importing Historical CSV Data

Historical sensor data from before the system was deployed can be imported using the export/import script. This improves model training data volume.

**CSV Format Required:**

```
timestamp,entity_id,value,unit
2024-06-01T08:00:00+12:00,sensor.room1_temperature,24.3,°C
2024-06-01T08:00:00+12:00,sensor.room1_humidity,62.1,%
```

**Import Command:**

```bash
# Run inside the worker container
docker compose exec cultivation-worker python scripts/export_features.py \
  --mode import \
  --input /data/imports/historical_room1_2024.csv \
  --batch-code LAL-B018 \
  --validate \
  --dry-run

# If dry-run looks correct (no validation errors), run for real:
docker compose exec cultivation-worker python scripts/export_features.py \
  --mode import \
  --input /data/imports/historical_room1_2024.csv \
  --batch-code LAL-B018 \
  --validate
```

The `--validate` flag checks for:
- Timestamp continuity (flags gaps > 30 minutes)
- Value range plausibility (rejects physiologically impossible readings)
- Entity ID matching against the known entity registry

**Post-import verification:**

```bash
curl -s "http://localhost:8000/api/v1/batches/LAL-B018/sensor-coverage" | python3 -m json.tool
```

Confirm coverage percentages match expectations.

---

### Task 3 — Acknowledging Recommendations

Recommendations must be acknowledged (not left pending). Acknowledgement is part of the audit trail required for NZ medicinal cannabis compliance.

**Via Dashboard:**

1. Navigate to **Recommendations → Pending**
2. Click on a recommendation to expand it
3. Read the full recommendation text, associated sensor data, and the SHAP explanation (once available in Phase 2)
4. Choose one of:
   - **Accept**: You will take or have taken the recommended action
   - **Reject**: You disagree with the recommendation — enter a reason
   - **Defer**: You need more time — set a reminder time (max 4 hours)
5. If accepting, add a note describing the physical action taken (e.g., "Reduced dehumidifier setpoint from 65% to 58%, adjusted at 09:15")
6. Click **Submit**

**Via API:**

```bash
curl -X PATCH http://localhost:8000/api/v1/recommendations/REC-00142 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CULTIVATION_API_TOKEN" \
  -d '{
    "status": "accepted",
    "operator_notes": "Reduced dehumidifier setpoint from 65% to 58% at 09:15",
    "actioned_by": "operator-jane"
  }'
```

---

### Task 4 — Viewing Risk Scores

**Current risk scores for all active batches:**

```bash
curl -s http://localhost:8000/api/v1/risk/current | python3 -m json.tool
```

**Risk score history for a specific batch:**

```bash
curl -s "http://localhost:8000/api/v1/risk/history?batch_id=LAL-B025&hours=48" | python3 -m json.tool
```

**Risk score breakdown (which parameters are contributing):**

```bash
curl -s "http://localhost:8000/api/v1/risk/breakdown/LAL-B025" | python3 -m json.tool
```

This returns a per-parameter contribution showing which sensor dimensions are driving the score.

---

### Task 5 — Triggering Manual Model Retraining

Model retraining is normally scheduled automatically (weekly, Sunday 02:00 NZST). Manual retraining should only be triggered when:
- A significant number of new batches have just been completed and labelled
- A model accuracy review has identified performance degradation

```bash
# Check current model version and last training date
curl -s http://localhost:8000/api/v1/models/current | python3 -m json.tool

# Trigger retraining (this queues a background job — do not interrupt)
curl -X POST http://localhost:8000/api/v1/models/retrain \
  -H "Authorization: Bearer $CULTIVATION_API_TOKEN" \
  -d '{"reason": "Weekly review identified MAE above threshold"}'

# Monitor retraining progress
curl -s http://localhost:8000/api/v1/models/training-status | python3 -m json.tool
```

Retraining typically takes 2–5 minutes. The new model is validated against a held-out test set before being promoted to production. If validation fails (MAE worse than current model), the existing model is kept and an alert is generated.

---

### Task 6 — Adding a New Sensor Entity from Home Assistant

When a new sensor is added to HA (e.g., a new grow room is commissioned, or an additional sensor type is deployed):

**Step 1 — Identify the HA entity ID**

Log in to Home Assistant and navigate to **Settings → Devices & Services → Entities**. Copy the exact entity ID (e.g., `sensor.room2_co2_concentration`).

**Step 2 — Register the entity in the cultivation system**

```bash
curl -X POST http://localhost:8000/api/v1/sensors/entities \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CULTIVATION_API_TOKEN" \
  -d '{
    "entity_id": "sensor.room2_co2_concentration",
    "display_name": "Room 2 CO2",
    "unit": "ppm",
    "sensor_type": "co2",
    "room_id": "flower-room-2",
    "expected_range": {"min": 400, "max": 1500},
    "alert_on_stale_minutes": 10
  }'
```

**Step 3 — Verify ingestion is working**

Wait 5 minutes, then check:

```bash
curl -s "http://localhost:8000/api/v1/sensors/entities/sensor.room2_co2_concentration/latest" | python3 -m json.tool
```

The response should include a recent reading. If not, check the HA webhook configuration and ensure the entity is configured to push state changes.

**Step 4 — Link to feature pipeline**

```bash
# Add entity to the feature pipeline configuration
# Edit config/sensor_registry.yaml and add the new entity under the correct room and type
# Then reload the feature pipeline config (no restart required):
curl -X POST http://localhost:8000/api/v1/admin/reload-config \
  -H "Authorization: Bearer $CULTIVATION_API_TOKEN"
```

---

## 6. Troubleshooting Guide

| Symptom | Likely Cause | Resolution |
|---|---|---|
| **Sensor data not appearing in dashboard** | HA webhook not firing; ingest service down; entity not registered | 1. Check HA automation is active. 2. `docker compose logs cultivation-api \| grep ingest`. 3. Verify entity is in sensor registry via `GET /api/v1/sensors/entities`. 4. Restart ingest: `docker compose restart cultivation-api`. |
| **HA connection lost (dashboard shows red HA indicator)** | HA token expired; HA server unreachable; network issue | 1. From server, `ping [HA-IP]`. 2. Verify HA long-lived token hasn't expired in HA → Profile → Long-Lived Access Tokens. 3. Update `HA_TOKEN` in `.env` and restart API. 4. Check firewall rules between server and HA host. |
| **Predictions not updating (stale prediction dates)** | Daily prediction job failed; model file missing; worker container down | 1. `docker compose logs cultivation-worker \| grep prediction`. 2. Check worker is running: `docker compose ps cultivation-worker`. 3. Manually trigger: `docker compose exec cultivation-worker python -m cultivation.jobs.predict`. 4. Check model file exists: `ls -lh models/current/`. |
| **AquaPro not reporting (water quality sensor absent)** | AquaPro offline; Bluetooth/Modbus bridge lost connection; HA entity stale | 1. Check physical AquaPro unit — power indicator. 2. Restart AquaPro integration in HA: Settings → Integrations → AquaPro → Reload. 3. If Modbus: check RS-485 adapter connection. 4. Check HA logs for `aquapro` errors: `docker logs homeassistant \| grep -i aquapro`. |
| **High risk score with no obvious cause** | Historical data gap causing false signal; feature engineering bug; correct — investigate environment | 1. Navigate to Batch → Environmental Timeline, identify the parameter driving score. 2. `GET /api/v1/risk/breakdown/{batch_id}` to see per-parameter contributions. 3. Check for recent sensor gaps — a gap followed by return to normal can spike rolling-std features. 4. If score is misleading, document in Operations Log and flag for engineering review. |
| **Database disk full** | Uncompressed time-series data; backup files accumulating; logs on same volume | 1. `df -h` to confirm. 2. Immediately run TimescaleDB compression: see Section 8. 3. Remove old backups older than 90 days: `find /backups -name "*.dump" -mtime +90 -delete`. 4. Ensure log rotation is configured. 5. Alert engineering to add disk capacity. |
| **Application not starting (containers exit immediately)** | Missing env vars; database not ready; port conflict | 1. `docker compose logs [service-name]` to read exit reason. 2. Confirm `.env` file exists and has all required variables vs `.env.example`. 3. Start only DB first: `docker compose up -d timescaledb redis`, wait 30s, then `docker compose up -d`. 4. Check for port conflicts: `lsof -i :8000` and `lsof -i :3000`. |

### Extended Troubleshooting: HA Connection

If HA connectivity is intermittent (connecting and dropping repeatedly):

```bash
# Check how often the connection is dropping
docker compose logs cultivation-api | grep "ha_connection" | tail -50

# Test the HA API token manually
curl -H "Authorization: Bearer $HA_TOKEN" \
     http://[HA_HOST]:8123/api/states \
     | python3 -m json.tool | head -20
```

If the token test fails with 401, generate a new long-lived access token in HA and update `.env`.

### Extended Troubleshooting: Stale Feature Cache

If risk scores are not updating even though sensor data is flowing:

```bash
# Check when features were last computed
curl -s http://localhost:8000/api/v1/features/cache-status | python3 -m json.tool

# Force-invalidate the Redis feature cache and recompute
docker compose exec cultivation-worker python -m cultivation.jobs.compute_features --force-refresh

# Monitor Redis directly
docker compose exec redis redis-cli --scan --pattern "features:*" | wc -l
```

---

## 7. Alert Response Procedures

### ALERT-001: Sensor Stale — Single Entity

**Description:** One sensor entity has not reported a new reading for longer than the configured staleness threshold (default: 10 minutes).
**Severity:** Warning (amber)
**Immediate Action:**
1. Navigate to HA and verify the entity is reporting in HA's history
2. If HA shows values but cultivation system does not: check the webhook/polling configuration
3. If HA also shows no values: check the physical sensor (power, connectivity)
4. Document in Operations Log

**Escalation:** If unresolved within 1 hour and the sensor is critical to active batch environmental monitoring, escalate to cultivation manager.

---

### ALERT-002: Sensor Stale — Multiple Entities (3+)

**Description:** Three or more sensor entities in the same room have gone stale simultaneously.
**Severity:** High (red)
**Immediate Action:**
1. Likely indicates a network partition, HA restart, or room-level connectivity loss
2. Physically check the grow room — do not rely on sensor data until confirmed operational
3. Restart HA integration if HA itself is the issue
4. If network: check network switch and PoE injectors for the sensor cluster

**Escalation:** Immediate escalation to cultivation manager if any batch is in a critical growth stage (late flower, days 40–56).

---

### ALERT-003: Batch Risk Score Critical (> 80)

**Description:** A batch risk score has crossed the critical threshold, indicating severe environmental deviation.
**Severity:** Critical
**Immediate Action:**
1. **Do not dismiss this alert until you have physically entered the grow room**
2. Check temperature, humidity, CO2, VPD in room against displayed readings
3. Check that HVAC, dehumidifiers, and CO2 injection are operating
4. Review the risk breakdown (`GET /api/v1/risk/breakdown/{batch_id}`) to identify the primary driver
5. Take corrective action on the specific parameter
6. Document all actions in the Operations Log

**Escalation:** If environmental correction does not reduce risk score below 60 within 2 hours, escalate to cultivation manager and engineering.

---

### ALERT-004: HA Connection Lost

**Description:** The cultivation intelligence system has lost connectivity to Home Assistant for more than 5 minutes.
**Severity:** High
**Immediate Action:**
1. Check HA is accessible at `http://[HA_HOST]:8123` from your browser
2. Attempt manual connectivity test (see Troubleshooting section)
3. If HA is down, follow your HA maintenance runbook to restore it
4. Once HA is restored, the cultivation system reconnects automatically within 60 seconds
5. After reconnection, verify sensor freshness — there will be a data gap for the outage period

**Escalation:** If HA remains unreachable for more than 30 minutes, escalate to system maintainer.

---

### ALERT-005: Model Prediction Failure

**Description:** The daily prediction job failed to complete successfully.
**Severity:** Warning
**Immediate Action:**
1. Check worker logs: `docker compose logs cultivation-worker | grep -i "predict\|error" | tail -30`
2. Verify model files exist: `ls -lh /opt/cultivation-intelligence/models/current/`
3. Manually trigger a prediction run (see Task 5 in Section 5)
4. Previous predictions remain visible — the system is not blind, but predictions are 1+ days stale

**Escalation:** If retraining also fails (error during validation), escalate to engineering.

---

### ALERT-006: Database Disk Usage > 80%

**Description:** The TimescaleDB data volume is more than 80% full.
**Severity:** High
**Immediate Action:**
1. Run compression immediately (see Section 8)
2. Check if automated compression policy is running: `SELECT * FROM timescaledb_information.job_stats WHERE job_id = 1;`
3. Remove old backup files from the backup directory
4. Do not let disk reach 95% — database writes will fail and data will be lost

**Escalation:** Alert engineering to arrange disk expansion. This is not a recoverable situation through compression alone if growth rate continues.

---

## 8. Database Maintenance

### TimescaleDB Compression

TimescaleDB compression significantly reduces storage usage for historical time-series data. It is configured to run automatically but can be triggered manually:

```bash
# Connect to TimescaleDB
docker compose exec timescaledb psql -U cultivation -d cultivation_db

-- Check compression status
SELECT hypertable_name,
       pg_size_pretty(before_compression_total_bytes) as before,
       pg_size_pretty(after_compression_total_bytes) as after,
       compression_ratio
FROM timescaledb_information.compression_settings
JOIN (
  SELECT hypertable_schema, hypertable_name,
         SUM(before_compression_total_bytes) as before_compression_total_bytes,
         SUM(after_compression_total_bytes) as after_compression_total_bytes,
         ROUND(SUM(before_compression_total_bytes)::numeric /
               NULLIF(SUM(after_compression_total_bytes), 0), 2) as compression_ratio
  FROM chunk_compression_stats('sensor_readings')
  GROUP BY 1, 2
) s USING (hypertable_name);

-- Manually compress all eligible chunks (older than 7 days)
SELECT compress_chunk(c)
FROM show_chunks('sensor_readings', older_than => INTERVAL '7 days') c;
```

### Vacuum

```bash
docker compose exec timescaledb psql -U cultivation -d cultivation_db -c "VACUUM ANALYZE;"
```

Run after large imports or after deletion of old records. Normal PostgreSQL autovacuum handles routine cases.

### Backup Using pg_dump

```bash
# Full database backup (run this daily — also automated via backup service)
docker compose exec timescaledb pg_dump \
  -U cultivation \
  -Fc \
  -d cultivation_db \
  > /backups/timescaledb/cultivation_db_$(date +%Y%m%d_%H%M%S).dump

# Verify backup is valid
pg_restore --list /backups/timescaledb/cultivation_db_YYYYMMDD_HHMMSS.dump | head -20
```

### Restore Procedure

**CAUTION: Restore will overwrite the current database. Only perform on a test instance unless recovering from data loss.**

```bash
# Create a fresh database (if restoring to production, stop all services first)
docker compose stop cultivation-api cultivation-worker

# Drop and recreate the database
docker compose exec timescaledb psql -U cultivation -c "DROP DATABASE cultivation_db;"
docker compose exec timescaledb psql -U cultivation -c "CREATE DATABASE cultivation_db;"
docker compose exec timescaledb psql -U cultivation -d cultivation_db -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

# Restore from backup
docker compose exec timescaledb pg_restore \
  -U cultivation \
  -d cultivation_db \
  /backups/timescaledb/cultivation_db_YYYYMMDD_HHMMSS.dump

# Restart services
docker compose up -d cultivation-api cultivation-worker

# Verify data integrity
curl -s http://localhost:8000/health | python3 -m json.tool
```

### Retention Policy

The system retains raw sensor data indefinitely by default (regulatory requirement). Compressed chunks older than 90 days use approximately 5% of their original storage size. If storage becomes constrained, discuss with cultivation manager and engineering before modifying retention policies — there may be compliance implications.

---

## 9. Log Access

### Docker Container Logs

```bash
# All services, last 100 lines
docker compose logs --tail=100

# Follow live logs for a specific service
docker compose logs -f cultivation-api

# Logs with timestamps
docker compose logs -t cultivation-api

# Logs between specific times
docker compose logs --since="2025-01-20T06:00:00" --until="2025-01-20T09:00:00" cultivation-api
```

### Structured Log Querying with jq

The API and worker emit structured JSON logs. Use `jq` to filter and query:

```bash
# Find all ERROR level logs from the last hour
docker compose logs --since=1h cultivation-api | jq 'select(.level == "ERROR")'

# Find all ingest events (sensor readings received)
docker compose logs --since=1h cultivation-api | jq 'select(.event == "sensor_reading_ingested")'

# Find recommendation generation events
docker compose logs --since=24h cultivation-worker | jq 'select(.event == "recommendation_created") | {timestamp: .timestamp, batch_id: .batch_id, recommendation_type: .recommendation_type}'

# Find all events for a specific batch
docker compose logs --since=24h cultivation-api | jq 'select(.batch_id == "LAL-B025")'

# Find slow database queries (> 1000ms)
docker compose logs --since=24h cultivation-api | jq 'select(.db_query_duration_ms > 1000)'

# Count errors by type in the last 24 hours
docker compose logs --since=24h cultivation-api | jq 'select(.level == "ERROR") | .error_type' | sort | uniq -c | sort -rn
```

### Application Log Files

Structured logs are also written to files within the container:

```bash
# Access log files inside the API container
docker compose exec cultivation-api ls -lh /app/logs/

# Read the current log file
docker compose exec cultivation-api cat /app/logs/api.log | jq 'select(.level == "ERROR")'
```

---

## 10. Configuration Changes

### Safe Environment Variable Updates

Configuration is managed through the `.env` file in the project root. Most settings require a service restart to take effect.

**Safe to change and restart:**
- `HA_TOKEN` — HA long-lived access token
- `REDIS_TTL_SECONDS` — Feature cache TTL
- `RISK_SCORE_ALERT_THRESHOLD` — Risk score alert levels
- `LOG_LEVEL` — Logging verbosity (DEBUG / INFO / WARNING / ERROR)
- `FEATURE_PIPELINE_INTERVAL_MINUTES` — How often features are recomputed

**Requires more care (consult engineering before changing):**
- `DATABASE_URL` — Points to TimescaleDB; wrong value will cause data loss
- `MODEL_REGISTRY_PATH` — Where model artifacts are stored
- `HA_WEBHOOK_SECRET` — Must match what HA is configured to send

**How to apply a config change:**

```bash
# Edit the env file
nano /opt/cultivation-intelligence/.env

# Restart the affected service (not the database unless DATABASE_URL changed)
docker compose restart cultivation-api cultivation-worker

# Verify the change took effect
docker compose logs cultivation-api | head -20
curl -s http://localhost:8000/health | python3 -m json.tool
```

### When to Restart vs Hot Reload

| Change Type | Action Required |
|---|---|
| HA token rotation | `docker compose restart cultivation-api` |
| Log level change | `docker compose restart cultivation-api cultivation-worker` |
| Sensor registry update (new entity) | `POST /api/v1/admin/reload-config` (no restart) |
| Risk threshold changes | `POST /api/v1/admin/reload-config` (no restart) |
| Database connection change | Full `docker compose down && docker compose up -d` |
| Model artifact update | `POST /api/v1/models/reload` (no restart) |
| Docker image update | `docker compose pull && docker compose up -d` |

---

## 11. Contact and Escalation

### Escalation Tiers

| Tier | Role | Responsibility | Contact |
|---|---|---|---|
| Tier 1 | Shift Operator | Daily operations, routine alerts, recommendations | [Operator contact — fill in] |
| Tier 2 | Cultivation Manager | High-severity alerts, crop risk decisions, policy decisions | [Cultivation Manager contact — fill in] |
| Tier 3 | System Maintainer / Engineering | System failures, database issues, model failures | [Engineering contact — fill in] |
| Tier 4 | Facility Manager | Business-level escalation, regulatory incidents | [Facility Manager contact — fill in] |

### Escalation Decision Tree

```
Alert triggered
      │
      ▼
Is this a crop safety issue? (Risk score > 80, environmental control failure)
      │
   YES ──▶ Go to grow room immediately → Assess in person → Escalate to Tier 2 if unresolved in 30 min
      │
   NO
      │
      ▼
Is this a system technical failure? (Services down, DB error, persistent HA disconnect)
      │
   YES ──▶ Attempt standard troubleshooting (Section 6) → Escalate to Tier 3 after 30 min
      │
   NO
      │
      ▼
Is this a data quality or model accuracy issue?
      │
   YES ──▶ Document and notify Tier 2 → Review at next scheduled meeting
      │
   NO
      │
      ▼
Handle per standard operating procedure and document in Operations Log
```

### Regulatory Incident Response

If a system failure has resulted in a period where grow environment data was not captured and this may affect a medicinal cannabis batch record:

1. Document the exact outage period (start and end timestamps)
2. Retrieve any manual paper records made during the outage
3. Notify Tier 4 (Facility Manager) immediately
4. Do not attempt to reconstruct or fill in missing data
5. Prepare an incident report for potential regulatory notification per the Ministry of Health Medicinal Cannabis licensing conditions

---

*End of Operations Runbook. Review and update quarterly or after any significant system change.*
