# ADR-0002: TimescaleDB + Redis vs Alternatives for Cultivation Data Storage

**Status:** Accepted
**Date:** 2025-01-16
**Deciders:** Engineering Lead, Facility Manager
**Supersedes:** N/A
**Superseded by:** N/A (trigger for revisitation: multi-facility deployment, event streaming requirement, > 1 billion rows)

---

## Context and Problem Statement

The cultivation intelligence system must store and serve four distinct categories of data with different access patterns, volume characteristics, and consistency requirements:

1. **Time-series sensor telemetry:** High write volume, append-only, time-range queries dominant. Approximately 10–30 entities × 1–4 readings per minute = up to 120 rows/minute, 170,000 rows/day. Growing indefinitely. Queries are always bounded by time range; full table scans are never correct. Recent data (last 24h) accessed frequently; historical data (> 30 days) accessed rarely but cannot be deleted (regulatory).

2. **Batch and facility metadata:** Low volume, relational structure, CRUD operations, joins between batches, phases, rooms, strains, and sensor entities. Classic relational data with foreign key relationships.

3. **Computed feature cache:** Fast read requirement (15-minute update cycle, risk scorer reads on every cycle), short TTL (20 minutes), no durability requirement (features can be recomputed from source data). High temporal locality — only the most recent feature set per batch matters.

4. **ML model artifacts:** Binary files (serialised LightGBM models, SHAP explainers, scaler objects), versioned, infrequently written, moderately frequent read (once per daily prediction run), no query requirement.

The storage stack must also satisfy:
- **On-premises deployment:** No external cloud services. The facility operates in a controlled environment where data must remain on-site.
- **SQL compatibility:** The facility may want to run ad-hoc reports or integrate with other tools. A SQL-compatible database dramatically broadens the available tooling.
- **Single operator:** The engineering team is small. Operational complexity of the storage layer is a significant cost.

---

## Decision Drivers

1. **Time-series query performance:** Sensor data queries are always time-bounded. The storage engine must natively optimise for this access pattern.
2. **Storage efficiency:** 170,000 rows/day of raw sensor data will grow substantially. Compression is essential to maintain manageable storage costs over a 3–5 year retention horizon.
3. **SQL compatibility:** The entire data ecosystem benefits from SQL access — reporting, ad-hoc queries, integration with data analysis tools (psql, DBeaver, pandas read_sql).
4. **Operational simplicity:** With a small team, a storage system that requires minimal day-to-day operational attention is strongly preferred.
5. **Feature cache speed:** Risk scoring runs every 15 minutes and reads feature vectors for each active batch. This is latency-sensitive within the pipeline (target: < 100ms to read all batch feature sets).
6. **Durability vs TTL for cache:** Feature cache data does not need to survive a Redis restart — it can be recomputed. Simplifying Redis configuration is acceptable.
7. **On-premises requirement:** Cloud-managed time-series services (AWS Timestream, Google Cloud Bigtable, InfluxDB Cloud) are eliminated immediately.
8. **Schema flexibility for evolving sensor set:** New sensor entities are added over time. The schema must accommodate new entity types without schema migrations.

---

## Considered Options

### Option 1: TimescaleDB + Redis + Local Filesystem (Chosen)

**TimescaleDB** (PostgreSQL extension) for all persistent data:
- `sensor_readings` hypertable partitioned by time (7-day chunks)
- Continuous aggregates for 1h, 6h, 24h rollups (materialized, auto-refreshed)
- Compression policy on chunks older than 7 days
- Regular PostgreSQL tables for batches, rooms, strains, recommendations, audit log

**Redis** for:
- Computed feature cache (hash per batch, TTL 20 minutes)
- APScheduler job state
- Pub/sub for ingest event fan-out (future)

**Local filesystem** for:
- Model artifacts in a versioned directory structure (`models/{model_type}/{version}/`)

### Option 2: InfluxDB + PostgreSQL + Redis

Separate time-series store (InfluxDB) and relational store (PostgreSQL). InfluxDB is purpose-built for time-series and offers excellent write throughput and native compression. However, it uses a proprietary query language (Flux) and requires maintaining two separate database systems with different backup procedures, connection management, and query interfaces.

### Option 3: Apache Kafka + ClickHouse

Kafka as a durable event streaming layer for sensor telemetry ingestion, ClickHouse as an OLAP column store for historical analytics. This is an enterprise-grade architecture appropriate for multi-facility, high-volume deployments. For a single facility with 170,000 rows/day, it is engineering theatre — the operational complexity is wholly disproportionate to the actual data volume.

### Option 4: SQLite (Single-file, Simplest)

SQLite is a file-based database with no server process. It is trivially simple to deploy and operate. However, SQLite does not support concurrent writers, has no native time-series optimisations, no compression, and no streaming replication. At 170,000 rows/day for multi-year retention, SQLite query performance on unindexed time-range queries would degrade, and there is no path to TimescaleDB's continuous aggregates or compression policies. SQLite is appropriate for prototypes and is inappropriate for the production system.

### Option 5: DynamoDB / Cloud Time-Series (Rejected)

Cloud-managed databases are rejected immediately by the on-premises deployment requirement. No further evaluation performed.

---

## Decision

**Option 1 — TimescaleDB + Redis + local filesystem — is chosen.**

---

## Full Technical Justification

### TimescaleDB Hypertables

TimescaleDB extends PostgreSQL with automatic time-based table partitioning (hypertables). When data is inserted into the `sensor_readings` hypertable, TimescaleDB automatically routes it to the appropriate time chunk (a child table covering a 7-day window). This has three critical benefits:

**Query performance:** Time-bounded queries only scan the relevant chunks. A query for "last 24 hours of temperature readings" touches at most 1 chunk instead of scanning the entire table. This remains true regardless of how large the historical dataset grows — query performance scales with the size of the time window, not the size of the table.

**Chunk-level operations:** Compression, index creation, and data retention policies operate at the chunk level. Compressing an old chunk does not lock recent chunks. Dropping an expired chunk is an O(1) operation (metadata deletion) rather than a DELETE cascade.

**Transparent SQL:** Hypertables are queried using standard SQL. There is no new query language to learn. Every existing PostgreSQL tool works without modification.

The `sensor_readings` schema:

```sql
CREATE TABLE sensor_readings (
    time        TIMESTAMPTZ NOT NULL,
    entity_id   TEXT NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    unit        TEXT NOT NULL,
    quality     SMALLINT DEFAULT 1,  -- 1=good, 0=suspect, -1=bad
    batch_id    TEXT,                -- nullable; populated by entity-room-batch lookup
    CONSTRAINT sensor_readings_pkey PRIMARY KEY (time, entity_id)
);

SELECT create_hypertable('sensor_readings', 'time', chunk_time_interval => INTERVAL '7 days');

-- Compression policy: compress chunks older than 7 days
ALTER TABLE sensor_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'entity_id',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('sensor_readings', INTERVAL '7 days');
```

The `compress_segmentby = 'entity_id'` configuration is critical: it ensures that readings from the same entity are stored contiguously within a compressed chunk. Queries that filter by entity_id can skip entire segments, dramatically reducing I/O for entity-specific time-range queries.

### Continuous Aggregates

Dashboard queries for historical trend charts, weekly data quality reports, and model training feature computation all require aggregated statistics (mean, min, max) over hourly or daily windows. Computing these aggregations on-demand over raw minutely data would be expensive. TimescaleDB continuous aggregates maintain these rollups as materialised views that are automatically kept in sync with new data:

```sql
CREATE MATERIALIZED VIEW sensor_readings_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    entity_id,
    AVG(value) AS mean,
    MIN(value) AS min,
    MAX(value) AS max,
    STDDEV(value) AS stddev,
    COUNT(*) AS reading_count
FROM sensor_readings
GROUP BY bucket, entity_id;

SELECT add_continuous_aggregate_policy('sensor_readings_1h',
    start_offset => INTERVAL '3 hours',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour');
```

Dashboard queries for historical charts use `sensor_readings_1h` or `sensor_readings_6h`. Only real-time charts (last 30 minutes) query the raw `sensor_readings` table.

### Compression Performance

TimescaleDB compression on time-series data typically achieves 10–20x compression ratios for numeric sensor data due to the highly repetitive nature of the values and timestamps within a chunk. The compression algorithm is column-oriented (using Gorilla-style delta-of-delta encoding for timestamps and value columns), which is optimal for the access pattern.

Expected storage impact:
- Uncompressed: ~170,000 rows/day × 7 days/chunk × ~50 bytes/row = ~60 MB/chunk
- Compressed: ~60 MB × 0.07 compression ratio ≈ 4 MB/chunk
- 3-year retention: ~52 chunks/year × 3 years × 4 MB ≈ ~620 MB for compressed historical data

This is highly manageable on commodity server hardware.

### Redis Feature Cache

The feature engineering pipeline computes up to 200+ features per batch (rolling statistics at multiple windows for 10–30 entities). Computing these features takes 2–5 seconds per batch. Caching them in Redis allows:

1. **Risk scorer** (runs every 15 minutes) to read pre-computed features in < 10ms instead of spending 2–5 seconds recomputing them.
2. **Prediction API** (on-demand) to serve feature vectors without database computation.
3. **Feature debugging** via `redis-cli hgetall "features:LAL-B025"` — engineers can inspect exactly what feature values the model is seeing.

Cache key structure:

```
features:{batch_id}      → Hash of feature_name → value (float)
features:{batch_id}:meta → Hash of generated_at, pipeline_version, entity_count
```

TTL of 20 minutes ensures that stale features expire even if the pipeline fails, preventing the risk scorer from operating on indefinitely old data.

Redis is configured without persistence (`--save ""`) in the Docker Compose file. The feature cache is entirely recomputable from TimescaleDB. Redis durability adds operational complexity (AOF log, RDB snapshots) for no benefit in this use case.

### Local Filesystem for Model Artifacts

Model artifacts (serialised LightGBM models, SHAP explainers, input scaler objects) are versioned binary files. They are:
- Infrequently written (weekly retraining)
- Moderately frequently read (daily prediction run, on-demand from API)
- Small (LightGBM model for this feature set: ~1–5 MB)
- Not relational (no SQL queries)

Storing them in the database (as bytea blobs) would add unnecessary complexity to backup and restore procedures and prevent direct file-level inspection and versioning. Storing them in a dedicated object store (S3, MinIO) adds operational overhead without benefit for a single-server deployment.

The filesystem structure:

```
models/
├── yield/
│   ├── v1/
│   │   ├── model.lgb
│   │   ├── scaler.pkl
│   │   ├── shap_explainer.pkl
│   │   └── metadata.json       # training date, metrics, feature list, batch count
│   ├── v2/
│   │   └── ...
│   └── current -> v2/          # symlink to current production version
└── quality/
    └── ...
```

The `current` symlink allows atomic model promotion: train a new version, validate it, then atomically update the symlink. The API reads `models/yield/current/model.lgb`. No restart required.

### Why Not InfluxDB?

InfluxDB is an excellent time-series database, but it has two properties that make it a poor fit for this system:

1. **Separate query language:** Flux (the InfluxDB 2.x query language) is powerful but requires learning a new paradigm. The cultivation team and engineering team are familiar with SQL. InfluxDB forces a query language that is not compatible with standard reporting tools, ORM libraries, or data analysis notebooks that use `pd.read_sql`.

2. **Two-database operational burden:** If sensor data lives in InfluxDB and batch metadata lives in PostgreSQL, every operation that spans both (e.g., "find all sensor readings for batches where yield > 80g/plant") requires a query to both databases and application-level join. In TimescaleDB, this is a single SQL join. Two databases mean two backup procedures, two connection pools, two sets of credentials, two health checks.

InfluxDB would be the right choice if the team were already fluent in Flux and the data volume were high enough to justify a purpose-built time-series engine. Neither condition applies.

### Why Not ClickHouse?

ClickHouse is an OLAP column store optimised for analytical queries at massive scale. It is appropriate when you have billions of rows and need sub-second aggregation across the full dataset. At 170,000 rows/day, TimescaleDB with continuous aggregates delivers equivalent analytical query performance with a fraction of the operational complexity.

ClickHouse also does not support the CRUD operations needed for batch and facility metadata management. A ClickHouse deployment would still require a separate relational database, reintroducing the two-database problem.

---

## Tradeoffs and Risks

### TimescaleDB Operational Complexity vs Plain PostgreSQL

TimescaleDB adds operational surface area beyond plain PostgreSQL:
- Compression policies must be monitored and occasionally tuned
- Continuous aggregates must be refreshed and their refresh policies maintained
- Hypertable chunk interval should be matched to query patterns
- TimescaleDB extension must be explicitly included in backup/restore procedures (`timescaledb` extension state must be present in the restored database)

These costs are real but manageable. The TimescaleDB documentation is comprehensive and its Docker image is well-maintained. The compression and continuous aggregate benefits are substantial enough to justify the complexity for a time-series workload of this nature.

**Mitigation:** The operations runbook documents all TimescaleDB-specific maintenance procedures explicitly. Compression policies are configured via SQL and are self-maintaining once set.

### Redis as Single Point of Failure

If Redis goes down, the feature cache is unavailable. The risk scoring engine cannot serve current risk scores. The pipeline will still run but will need to recompute features from the database on each cycle (adding 2–5 seconds of latency per cycle).

**Mitigation:** The feature pipeline is designed to gracefully fall back to in-memory computation if Redis is unavailable. The health check marks the system as "degraded" (not "unhealthy") if Redis is down but the database is up. Operators are notified. Redis restart is fast (< 10 seconds) and does not require data recovery.

Redis replication (Redis Sentinel) would reduce this risk but adds significant operational complexity for a facility that runs one server. The tradeoff favours simplicity.

### Filesystem Model Registry — No Atomic Cross-Version Queries

If a model is promoted (symlink updated) while a prediction request is in flight, there is a brief race where one request may read from the old model directory and another from the new. In practice, this is not a problem because model promotions are rare, the model registry is read by a single daily batch job (not a concurrent API), and the outputs of both versions are close enough that a one-prediction discrepancy is inconsequential.

A future improvement would be to track the model version used for each prediction in the `batch_predictions` table, which is already part of the schema design.

---

## Consequences

### Positive

1. **Single database for all persistent data:** One connection pool, one backup procedure, one query language, one set of credentials.
2. **Native time-series optimisations:** Chunk pruning and continuous aggregates deliver time-series query performance comparable to purpose-built databases.
3. **SQL compatibility:** All reporting, ad-hoc analysis, and integration tooling works out of the box.
4. **Excellent compression:** 10–20x compression ratio makes multi-year retention economically feasible.
5. **Redis is operationally simple:** No persistence configuration, fast restart, transparent failure behavior (graceful degradation).
6. **Model artifacts are inspectable:** Engineers can directly examine model files, check training metadata, and roll back by updating a symlink.

### Negative

1. **TimescaleDB extension dependency:** Cannot run plain PostgreSQL without loss of time-series functionality. Upgrading PostgreSQL requires coordinating the TimescaleDB extension upgrade.
2. **Redis is a SPOF for feature caching:** An unplanned Redis restart causes a brief degradation in risk score freshness.
3. **No streaming ingestion:** The current architecture is webhook/polling based. If a future requirement demands Kafka-grade streaming throughput, a significant architectural change is required.
4. **Filesystem model registry is not distributed:** Cannot be shared across multiple servers without a shared filesystem or object store.

---

## Review Triggers

This decision should be revisited if:
- The facility expands to multiple grow sites requiring centralised data aggregation
- Sensor density increases to > 100 entities and write volume exceeds TimescaleDB compression headroom
- A real-time streaming requirement emerges (current 15-minute pipeline cycle is adequate)
- The team requires multi-user concurrent model training across multiple workers

---

*This ADR was written with reference to: TimescaleDB documentation v2.x, "InfluxDB vs TimescaleDB" benchmarks (TimescaleDB, 2023), and the internal data volume projections based on the current HA sensor entity inventory.*
