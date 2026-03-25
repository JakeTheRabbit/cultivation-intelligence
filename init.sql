-- =============================================================================
-- Cultivation Intelligence — TimescaleDB Schema Initialisation
-- =============================================================================
-- Run order: this file is executed once on first container start via the
-- TimescaleDB Docker entrypoint (mounted as /docker-entrypoint-initdb.d/init.sql).
--
-- Database: cultivation
-- Extensions: timescaledb (required), pgcrypto (for gen_random_uuid())
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS pgcrypto;      -- gen_random_uuid()


-- ---------------------------------------------------------------------------
-- Enumerated types
-- ---------------------------------------------------------------------------

CREATE TYPE sensor_type_enum AS ENUM (
    'TEMPERATURE',          -- Ambient air temperature (°C)
    'HUMIDITY',             -- Relative humidity (%)
    'VPD_CALCULATED',       -- Vapour pressure deficit derived from T + RH (kPa)
    'EC',                   -- Electrical conductivity of nutrient solution (mS/cm)
    'PH',                   -- Nutrient solution pH
    'VWC',                  -- Volumetric water content of substrate (m³/m³)
    'CO2',                  -- Carbon dioxide concentration (ppm)
    'PPFD',                 -- Photosynthetic photon flux density (μmol/m²/s)
    'FLOW_RATE',            -- Irrigation flow rate (L/min)
    'DISSOLVED_OXYGEN',     -- Dissolved oxygen in reservoir (mg/L)
    'WEIGHT'                -- Plant / pot weight for gravimetric WC (kg)
);

CREATE TYPE quality_flag_enum AS ENUM (
    'OK',                   -- Passed all range and continuity checks
    'SUSPECT_SPIKE',        -- Statistically anomalous (> 4σ from rolling mean)
    'SUSPECT_FLATLINE',     -- Unchanged value for > 30 consecutive minutes
    'OUT_OF_RANGE',         -- Exceeds configured sensor-type limits
    'INVALID',              -- Failed structural or parsing validation
    'SENSOR_OFFLINE'        -- Sensor did not report within expected interval
);

CREATE TYPE data_source_enum AS ENUM (
    'HA_PUSH',              -- Home Assistant webhook push (real-time)
    'HA_POLL',              -- REST polling of HA API (fallback)
    'CSV_IMPORT',           -- Batch import from CSV files
    'MANUAL_ENTRY'          -- Operator-entered value via dashboard
);

CREATE TYPE grow_stage_enum AS ENUM (
    'PROPAGATION',          -- Seed germination / cutting rooting
    'VEG',                  -- Vegetative growth (18+ h photoperiod)
    'TRANSITION',           -- Photoperiod flip and first week of flower
    'FLOWER',               -- Active flowering (12 h photoperiod)
    'FLUSH',                -- Final 7-10 days plain water, no nutrients
    'HARVEST',              -- Physical harvest event
    'DRYING',               -- Drying room (7-14 days)
    'CURING',               -- Sealed jar curing
    'COMPLETE'              -- Batch closed; all quality data recorded
);

CREATE TYPE genetics_type_enum AS ENUM (
    'INDICA',
    'SATIVA',
    'HYBRID'
);

CREATE TYPE substrate_enum AS ENUM (
    'ROCKWOOL',
    'COCO_COIR',
    'SOIL',
    'HYDRO'
);

CREATE TYPE lighting_type_enum AS ENUM (
    'LED',
    'HPS',
    'CMH',
    'HYBRID'
);

CREATE TYPE recommendation_status_enum AS ENUM (
    'PENDING',              -- Awaiting operator review
    'ACCEPTED',             -- Operator approved; action executed or queued
    'REJECTED',             -- Operator dismissed
    'EXPIRED',              -- Window passed without action
    'SUPERSEDED'            -- A newer recommendation replaced this one
);

CREATE TYPE recommendation_type_enum AS ENUM (
    'ENVIRONMENT',          -- Adjust temperature, humidity, CO2, VPD
    'IRRIGATION',           -- Change irrigation schedule or volume
    'NUTRITION',            -- Adjust EC, pH, or nutrient formulation
    'LIGHTING',             -- Adjust photoperiod, intensity, or spectrum
    'PEST_DISEASE',         -- IPM intervention recommendation
    'HARVEST',              -- Harvest timing advisory
    'GENERAL'               -- Uncategorised operational note
);

CREATE TYPE priority_enum AS ENUM (
    'CRITICAL',             -- Immediate action required (crop at risk)
    'HIGH',                 -- Address within 4 hours
    'MEDIUM',               -- Address within 24 hours
    'LOW'                   -- Informational / nice to have
);

CREATE TYPE trigger_type_enum AS ENUM (
    'SCHEDULED',            -- Regular programmed event
    'VWC_TRIGGERED',        -- Triggered by VWC falling below threshold
    'EC_TRIGGERED',         -- Triggered by EC rising above threshold
    'MANUAL'                -- Operator-initiated via dashboard
);


-- ---------------------------------------------------------------------------
-- batches — grow batch records (one row per cultivation cohort)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS batches (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_name              VARCHAR(100)    NOT NULL UNIQUE,
    strain                  VARCHAR(100)    NOT NULL,
    room_id                 VARCHAR(50)     NOT NULL,
    start_date              DATE            NOT NULL,
    end_date                DATE,
    current_stage           grow_stage_enum NOT NULL DEFAULT 'PROPAGATION',

    -- Schedule targets
    planned_veg_days        SMALLINT        NOT NULL DEFAULT 28
                                            CHECK (planned_veg_days BETWEEN 7 AND 120),
    planned_flower_days     SMALLINT        NOT NULL DEFAULT 63
                                            CHECK (planned_flower_days BETWEEN 42 AND 120),

    -- Yield and quality
    target_yield_g          NUMERIC(8,2)    CHECK (target_yield_g >= 0),
    actual_yield_g          NUMERIC(8,2)    CHECK (actual_yield_g >= 0),
    quality_grade           VARCHAR(20),

    -- Genetics
    genetics_type           genetics_type_enum,
    genetics_thc_target_pct NUMERIC(5,2)    CHECK (genetics_thc_target_pct BETWEEN 0 AND 35),
    genetics_cbd_target_pct NUMERIC(5,2)    CHECK (genetics_cbd_target_pct BETWEEN 0 AND 25),

    -- Environment / infrastructure
    substrate               substrate_enum,
    lighting_type           lighting_type_enum,
    num_plants              SMALLINT        CHECK (num_plants BETWEEN 1 AND 500),
    room_dimensions_m3      NUMERIC(7,2)    CHECK (room_dimensions_m3 > 0),

    -- Free-text notes
    notes                   TEXT,

    -- Audit
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT batches_end_after_start CHECK (end_date IS NULL OR end_date >= start_date)
);

CREATE INDEX ON batches (room_id, current_stage);
CREATE INDEX ON batches (current_stage);
CREATE INDEX ON batches (start_date DESC);

COMMENT ON TABLE batches IS
    'One row per cannabis cultivation batch. A batch is a cohort of plants '
    'grown together in one room under a unified grow programme.';


-- ---------------------------------------------------------------------------
-- sensor_readings — primary time-series table (TimescaleDB hypertable)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sensor_readings (
    -- TimescaleDB requires the partitioning column first for best performance
    time                    TIMESTAMPTZ     NOT NULL,

    id                      UUID            NOT NULL DEFAULT gen_random_uuid(),
    sensor_id               VARCHAR(100)    NOT NULL,
    batch_id                UUID            NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    sensor_type             sensor_type_enum NOT NULL,
    value                   DOUBLE PRECISION NOT NULL,
    unit                    VARCHAR(20)     NOT NULL,
    source                  data_source_enum NOT NULL DEFAULT 'HA_PUSH',
    raw_entity_id           VARCHAR(255),   -- Home Assistant entity ID (nullable)
    quality_flag            quality_flag_enum NOT NULL DEFAULT 'OK',

    PRIMARY KEY (time, id)
);

-- Convert to hypertable, partitioned by time with 1-day chunks
-- (1-day chunks are appropriate for ~288 readings/sensor/day at 5-min intervals
--  with 8 sensors = ~2,300 rows/day; adjust chunk_time_interval for larger deployments)
SELECT create_hypertable(
    'sensor_readings',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Indexes optimised for the most common query patterns
CREATE INDEX ON sensor_readings (batch_id, time DESC);
CREATE INDEX ON sensor_readings (sensor_id, time DESC);
CREATE INDEX ON sensor_readings (sensor_type, batch_id, time DESC);
CREATE INDEX ON sensor_readings (quality_flag, time DESC) WHERE quality_flag != 'OK';

COMMENT ON TABLE sensor_readings IS
    'Primary time-series table for all facility sensor measurements. '
    'Partitioned by time via TimescaleDB hypertable. '
    'Compression applied after 7 days; data retained indefinitely.';


-- ---------------------------------------------------------------------------
-- irrigation_events — discrete irrigation run records
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS irrigation_events (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id                UUID            NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    zone_id                 VARCHAR(50)     NOT NULL,
    start_time              TIMESTAMPTZ     NOT NULL,
    end_time                TIMESTAMPTZ,
    duration_s              INTEGER         CHECK (duration_s > 0),
    volume_l                NUMERIC(7,3)    CHECK (volume_l >= 0),
    target_ec               NUMERIC(5,3),
    target_ph               NUMERIC(4,2),
    actual_ec               NUMERIC(5,3),
    actual_ph               NUMERIC(4,2),
    trigger_type            trigger_type_enum NOT NULL DEFAULT 'SCHEDULED',
    notes                   TEXT,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT irrigation_end_after_start CHECK (end_time IS NULL OR end_time >= start_time)
);

CREATE INDEX ON irrigation_events (batch_id, start_time DESC);
CREATE INDEX ON irrigation_events (zone_id, start_time DESC);

COMMENT ON TABLE irrigation_events IS
    'One row per discrete irrigation run. Captures timing, volume, '
    'and target vs actual EC/pH. Used for VWC and EC correlation analysis.';


-- ---------------------------------------------------------------------------
-- control_actions — audit log of all Home Assistant service calls
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS control_actions (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id                UUID            REFERENCES batches(id) ON DELETE SET NULL,
    recommendation_id       UUID,           -- FK added after recommendations table
    time                    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    ha_service              VARCHAR(100)    NOT NULL,   -- e.g. 'climate.set_temperature'
    ha_entity_id            VARCHAR(255)    NOT NULL,
    payload                 JSONB           NOT NULL,   -- service call data payload
    operator_id             VARCHAR(255),               -- email or user ID
    outcome                 VARCHAR(20),                -- 'SUCCESS', 'FAILED', 'TIMEOUT'
    outcome_detail          TEXT,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX ON control_actions (batch_id, time DESC);
CREATE INDEX ON control_actions (recommendation_id);
CREATE INDEX ON control_actions (time DESC);

COMMENT ON TABLE control_actions IS
    'Immutable audit log of every control action issued to Home Assistant. '
    'All actions require prior operator approval (recommendation acceptance). '
    'Never delete rows from this table.';


-- ---------------------------------------------------------------------------
-- recommendations — ML and rule-based advisory records
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS recommendations (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id                UUID            NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    recommendation_type     recommendation_type_enum NOT NULL,
    priority                priority_enum   NOT NULL DEFAULT 'MEDIUM',
    status                  recommendation_status_enum NOT NULL DEFAULT 'PENDING',

    title                   VARCHAR(200)    NOT NULL,
    body                    TEXT            NOT NULL,
    suggested_action        JSONB,          -- structured action payload for HA

    confidence_score        NUMERIC(4,3)    CHECK (confidence_score BETWEEN 0 AND 1),
    model_version           VARCHAR(50),    -- e.g. 'env-advisor-v1.2.0'
    feature_snapshot        JSONB,          -- feature values that triggered this rec

    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    expires_at              TIMESTAMPTZ,
    acted_at                TIMESTAMPTZ,
    acted_by                VARCHAR(255)    -- operator email or user ID
);

-- Add FK from control_actions to recommendations now that both tables exist
ALTER TABLE control_actions
    ADD CONSTRAINT fk_control_action_recommendation
    FOREIGN KEY (recommendation_id)
    REFERENCES recommendations(id)
    ON DELETE SET NULL;

CREATE INDEX ON recommendations (batch_id, created_at DESC);
CREATE INDEX ON recommendations (status, created_at DESC);
CREATE INDEX ON recommendations (priority, status) WHERE status = 'PENDING';

COMMENT ON TABLE recommendations IS
    'Advisory recommendations generated by the ML pipeline and rule engine. '
    'Advisory mode: all recommendations require explicit operator acceptance '
    'before any control action is issued to Home Assistant.';


-- ---------------------------------------------------------------------------
-- Continuous aggregate: hourly_sensor_stats
-- Materialises per-hour statistics for the dashboard and feature pipeline
-- without hitting the raw hypertable on every query.
-- ---------------------------------------------------------------------------

CREATE MATERIALIZED VIEW hourly_sensor_stats
    WITH (timescaledb.continuous)
AS
SELECT
    time_bucket('1 hour', time)     AS bucket,
    batch_id,
    sensor_id,
    sensor_type,
    COUNT(*)                        AS reading_count,
    AVG(value)                      AS value_avg,
    MIN(value)                      AS value_min,
    MAX(value)                      AS value_max,
    STDDEV(value)                   AS value_stddev,
    PERCENTILE_CONT(0.5)
        WITHIN GROUP (ORDER BY value) AS value_p50,
    PERCENTILE_CONT(0.95)
        WITHIN GROUP (ORDER BY value) AS value_p95,
    -- Count of non-OK quality flags for data quality monitoring
    COUNT(*) FILTER (WHERE quality_flag != 'OK') AS suspect_count
FROM sensor_readings
GROUP BY
    time_bucket('1 hour', time),
    batch_id,
    sensor_id,
    sensor_type
WITH NO DATA;   -- populate lazily; policy below will backfill

COMMENT ON MATERIALIZED VIEW hourly_sensor_stats IS
    'TimescaleDB continuous aggregate: per-hour, per-sensor statistics. '
    'Updated automatically by refresh policy. Used by dashboard stats '
    'endpoints and feature engineering pipeline.';

-- Refresh policy: materialise new data with a 1-hour lag (allow late arrivals)
-- and look back up to 3 days for any late-arriving corrections.
SELECT add_continuous_aggregate_policy(
    'hourly_sensor_stats',
    start_offset => INTERVAL '3 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour'
);


-- ---------------------------------------------------------------------------
-- Compression policy: compress sensor_readings older than 7 days
-- Typical compression ratio for time-series sensor data: 10-20x
-- ---------------------------------------------------------------------------

ALTER TABLE sensor_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'batch_id, sensor_id, sensor_type',
    timescaledb.compress_orderby   = 'time DESC'
);

SELECT add_compression_policy(
    'sensor_readings',
    compress_after => INTERVAL '7 days'
);


-- ---------------------------------------------------------------------------
-- Retention policy: retain raw sensor readings for 5 years
-- Compressed data is retained; only chunks older than 5 years are dropped.
-- ---------------------------------------------------------------------------

SELECT add_retention_policy(
    'sensor_readings',
    drop_after => INTERVAL '5 years'
);


-- ---------------------------------------------------------------------------
-- Utility: updated_at trigger for batches table
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

CREATE TRIGGER batches_set_updated_at
    BEFORE UPDATE ON batches
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();


-- ---------------------------------------------------------------------------
-- End of initialisation script
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    RAISE NOTICE 'Cultivation Intelligence schema initialised successfully.';
    RAISE NOTICE 'TimescaleDB version: %', extversion
        FROM pg_extension WHERE extname = 'timescaledb';
END;
$$;
