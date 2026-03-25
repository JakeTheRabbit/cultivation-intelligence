#!/usr/bin/env python3
"""
Seed the cultivation database with sample data for development and testing.
Creates sample batches, sensor readings, and recommendations.

Usage:
    python scripts/seed_db.py
    python scripts/seed_db.py --days 14
    python scripts/seed_db.py --reset  # Drop and recreate all data
"""

import argparse
import asyncio
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import numpy as np

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://cultivation:cultivation@localhost:5432/cultivation",
)

# ---------------------------------------------------------------------------
# Realistic grow parameters
# ---------------------------------------------------------------------------

LIGHT_ON_HOUR = 6          # 06:00 NZST lights on
LIGHT_OFF_HOUR = 0         # 00:00 NZST lights off (18 h photoperiod)
PHOTOPERIOD_HOURS = 18

# Target VPD for late flower (kPa) – informational only, used in comments
TARGET_VPD = 1.2

# Irrigation schedule: 6 events per lights-on period, every 90 minutes starting at lights-on + 30 min
IRRIGATION_EVENTS_PER_DAY = 6
IRRIGATION_INTERVAL_MINUTES = 90
FIRST_IRRIGATION_OFFSET_MINUTES = 30   # 30 min after lights on


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _is_lights_on(dt: datetime) -> bool:
    """Return True if the given UTC datetime falls in the 18-h light window."""
    hour = dt.hour
    # NZST = UTC+13 in summer, UTC+12 in winter — for simplicity treat as UTC+13
    local_hour = (hour + 13) % 24
    return LIGHT_ON_HOUR <= local_hour < (LIGHT_ON_HOUR + PHOTOPERIOD_HOURS)


def _vpd_from_temp_rh(temp_c: float, rh_pct: float) -> float:
    """Compute VPD in kPa from temperature (°C) and relative humidity (%)."""
    svp = 0.6108 * np.exp(17.27 * temp_c / (temp_c + 237.3))  # kPa
    avp = svp * rh_pct / 100.0
    return round(svp - avp, 4)


# ---------------------------------------------------------------------------
# Core seed functions
# ---------------------------------------------------------------------------

async def create_sample_batch(db: asyncpg.Connection) -> UUID:
    """
    Insert a realistic Wedding Cake cannabis batch currently 45 days into flower.

    Returns the new batch_id UUID.
    """
    batch_id = uuid4()
    now = datetime.now(tz=timezone.utc)

    # Batch started propagation ~75 days ago; veg 21 days; flower 45 days in
    veg_days = 21
    flower_days_elapsed = 45
    start_date = now - timedelta(days=veg_days + flower_days_elapsed + 7)  # +7 for prop
    flower_start = start_date + timedelta(days=7 + veg_days)

    batch_name = "BATCH-2025-WC-001"
    strain = "Wedding Cake"
    room_id = "flower-room-1"

    await db.execute(
        """
        INSERT INTO batches (
            id,
            batch_name,
            strain,
            room_id,
            start_date,
            current_stage,
            planned_veg_days,
            planned_flower_days,
            target_yield_g,
            genetics_type,
            genetics_thc_target_pct,
            genetics_cbd_target_pct,
            substrate,
            lighting_type,
            num_plants,
            room_dimensions_m3,
            notes,
            created_at,
            updated_at
        )
        VALUES (
            $1, $2, $3, $4, $5,
            'FLOWER',
            21, 63,
            4500.0,
            'HYBRID', 24.5, 0.8,
            'COCO_COIR', 'LED',
            16, 28.0,
            'Wedding Cake (Triangle Kush × Animal Mints). Day 45 of 63 flower. '
            'Trichomes 30% cloudy, 60% clear. Flush scheduled for day 56.',
            NOW(), NOW()
        )
        """,
        batch_id,
        batch_name,
        strain,
        room_id,
        start_date.date(),
    )

    print(f"  Created batch '{batch_name}' (id={batch_id})")
    print(f"    Strain: {strain} | Stage: FLOWER (day 45/63)")
    print(f"    Room: {room_id} | Plants: 16 | Substrate: Coco Coir | Lights: LED")
    return batch_id


async def create_sensor_readings(
    db: asyncpg.Connection,
    batch_id: UUID,
    days: int = 30,
) -> int:
    """
    Generate realistic 5-minute-interval sensor readings for *days* days.

    Sensors simulated:
      - TEMPERATURE  : 22-26°C day / 18-20°C night with sinusoidal variation
      - HUMIDITY     : 55-70%, inversely correlated with temperature
      - VPD_CALCULATED: derived from temp + RH
      - CO2          : 1000-1400 ppm lights-on, ~400 ppm lights-off
      - PPFD         : square wave 450 μmol/m²/s on, 0 off (18 h photoperiod)
      - EC           : stable ~2.2 mS/cm with post-irrigation dips
      - PH           : 5.8-6.2 slight drift oscillations
      - VWC          : 35-70% saw-tooth following irrigation cycles

    Returns the total number of rows inserted.
    """
    now = datetime.now(tz=timezone.utc)
    start_ts = now - timedelta(days=days)
    interval = timedelta(minutes=5)

    # Pre-build timestamp array
    n_steps = int(days * 24 * 60 / 5)
    timestamps = [start_ts + interval * i for i in range(n_steps)]

    # Correlated noise using numpy
    rng = np.random.default_rng(seed=42)
    # Base noise terms (standard normal)
    base_noise = rng.standard_normal(n_steps)
    temp_noise = base_noise * 0.3 + rng.standard_normal(n_steps) * 0.1
    rh_noise = -base_noise * 1.5 + rng.standard_normal(n_steps) * 0.8  # inverse correlation
    co2_noise = rng.standard_normal(n_steps) * 30.0
    ec_noise = rng.standard_normal(n_steps) * 0.04
    ph_noise = rng.standard_normal(n_steps) * 0.03
    vwc_noise = rng.standard_normal(n_steps) * 0.5

    # Build records — batch insert for performance
    records = []
    reading_ts_prev = None
    vwc_current = 55.0          # Starting volumetric water content
    ph_drift = 5.9               # Starting pH (drifts slightly)
    ph_drift_direction = 1       # +1 = drifting up, -1 = drifting down

    # Track last irrigation minute for EC dip modeling
    last_irrigation_step = -999

    for i, ts in enumerate(timestamps):
        is_on = _is_lights_on(ts)
        local_hour = (ts.hour + 13) % 24
        minute_of_day = local_hour * 60 + ts.minute

        # ── PPFD (square wave, slight ramp at edges) ──────────────────────
        if is_on:
            # Brief ramp-up in first 10 min, ramp-down in last 10 min
            minutes_since_on = (minute_of_day - LIGHT_ON_HOUR * 60) % (24 * 60)
            minutes_until_off = (LIGHT_ON_HOUR + PHOTOPERIOD_HOURS) * 60 - minute_of_day
            if minutes_since_on < 10:
                ppfd = 450.0 * (minutes_since_on / 10.0)
            elif minutes_until_off < 10:
                ppfd = 450.0 * (minutes_until_off / 10.0)
            else:
                ppfd = 450.0
        else:
            ppfd = 0.0

        # ── TEMPERATURE (sinusoidal day/night) ────────────────────────────
        if is_on:
            # Peak at 3h into light period, sinusoidal 22-26°C
            minutes_since_on = (minute_of_day - LIGHT_ON_HOUR * 60) % (24 * 60)
            phase = np.pi * minutes_since_on / (PHOTOPERIOD_HOURS * 60)
            temp_base = 22.0 + 4.0 * np.sin(phase)
        else:
            temp_base = 18.5 + rng.random() * 1.5  # 18.5-20.0°C night
        temperature = round(float(temp_base + temp_noise[i]), 2)
        temperature = max(15.0, min(35.0, temperature))

        # ── HUMIDITY (inversely correlated with temp) ──────────────────────
        if is_on:
            rh_base = 65.0 - (temperature - 22.0) * 1.5
        else:
            rh_base = 70.0  # slightly higher at night
        humidity = round(float(rh_base + rh_noise[i]), 1)
        humidity = max(30.0, min(90.0, humidity))

        # ── VPD (calculated) ──────────────────────────────────────────────
        vpd = _vpd_from_temp_rh(temperature, humidity)

        # ── CO2 ───────────────────────────────────────────────────────────
        if is_on:
            co2_base = 1200.0
        else:
            co2_base = 420.0  # ambient
        co2 = round(float(co2_base + co2_noise[i]), 0)
        co2 = max(380.0, min(2000.0, co2))

        # ── Irrigation event detection (for EC/VWC) ───────────────────────
        irrigation_step = False
        if is_on:
            minutes_into_lights = (minute_of_day - LIGHT_ON_HOUR * 60) % (24 * 60)
            for event_n in range(IRRIGATION_EVENTS_PER_DAY):
                event_minute = (
                    FIRST_IRRIGATION_OFFSET_MINUTES
                    + event_n * IRRIGATION_INTERVAL_MINUTES
                )
                if abs(minutes_into_lights - event_minute) < 5:
                    irrigation_step = True
                    last_irrigation_step = i
                    break

        # ── VWC (saw-tooth) ───────────────────────────────────────────────
        if irrigation_step:
            # Jump up to 65-70%
            vwc_current = 65.0 + rng.random() * 5.0
        elif is_on:
            # Drain ~0.08% per 5-min step during lights on
            vwc_current = max(35.0, vwc_current - 0.08 + vwc_noise[i] * 0.02)
        else:
            # Very slow drain at night
            vwc_current = max(38.0, vwc_current - 0.01)
        vwc = round(float(vwc_current + vwc_noise[i] * 0.1), 2)
        vwc = max(20.0, min(80.0, vwc))

        # ── EC (stable with post-irrigation dip) ─────────────────────────
        steps_since_irrigation = i - last_irrigation_step
        if 0 <= steps_since_irrigation <= 3:
            # Brief dip immediately after irrigation (dilution effect)
            ec_base = 1.9 + 0.05 * steps_since_irrigation
        else:
            ec_base = 2.2
        ec = round(float(ec_base + ec_noise[i]), 3)
        ec = max(0.5, min(4.0, ec))

        # ── pH (slow drift with small oscillations) ───────────────────────
        ph_drift += ph_drift_direction * 0.001
        if ph_drift > 6.2:
            ph_drift_direction = -1
        elif ph_drift < 5.8:
            ph_drift_direction = 1
        ph = round(float(ph_drift + ph_noise[i]), 2)
        ph = max(4.5, min(7.5, ph))

        # ── Build sensor record tuples ────────────────────────────────────
        common = dict(batch_id=str(batch_id), source="HA_PUSH", quality_flag="OK")

        sensors = [
            ("temp_flower_room_1",    "TEMPERATURE",    temperature, "°C"),
            ("rh_flower_room_1",      "HUMIDITY",       humidity,    "%"),
            ("vpd_flower_room_1",     "VPD_CALCULATED", vpd,         "kPa"),
            ("co2_flower_room_1",     "CO2",            co2,         "ppm"),
            ("ppfd_flower_room_1",    "PPFD",           ppfd,        "μmol/m²/s"),
            ("ec_flower_room_1",      "EC",             ec,          "mS/cm"),
            ("ph_flower_room_1",      "PH",             ph,          "pH"),
            ("vwc_flower_room_1",     "VWC",            vwc,         "m³/m³"),
        ]

        for sensor_id, sensor_type, value, unit in sensors:
            records.append((
                uuid4(),
                sensor_id,
                str(batch_id),
                sensor_type,
                value,
                unit,
                ts,
                common["source"],
                None,   # raw_entity_id
                common["quality_flag"],
            ))

    # Batch insert in chunks of 1000 to avoid memory issues
    chunk_size = 1000
    total_inserted = 0
    for chunk_start in range(0, len(records), chunk_size):
        chunk = records[chunk_start : chunk_start + chunk_size]
        await db.executemany(
            """
            INSERT INTO sensor_readings (
                id, sensor_id, batch_id, sensor_type, value, unit,
                time, source, raw_entity_id, quality_flag
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT DO NOTHING
            """,
            chunk,
        )
        total_inserted += len(chunk)

    print(
        f"  Created {total_inserted} sensor readings "
        f"({n_steps} timestamps × 8 sensors, {days} days)"
    )
    return total_inserted


async def create_sample_irrigation_events(
    db: asyncpg.Connection,
    batch_id: UUID,
    days: int = 30,
) -> int:
    """
    Create irrigation events at realistic times.

    Schedule: 6 events per day during lights-on, every 90 minutes,
    starting 30 minutes after lights-on (06:30 local → 17:30 UTC for NZST+13).
    """
    now = datetime.now(tz=timezone.utc)
    start_ts = now - timedelta(days=days)

    records = []
    rng = np.random.default_rng(seed=99)

    for day_offset in range(days):
        day_start = start_ts + timedelta(days=day_offset)
        # Lights-on at 06:00 NZST = 17:00 UTC (UTC+13 offset)
        lights_on_utc = day_start.replace(
            hour=17, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
        )

        for event_n in range(IRRIGATION_EVENTS_PER_DAY):
            event_start = lights_on_utc + timedelta(
                minutes=FIRST_IRRIGATION_OFFSET_MINUTES
                + event_n * IRRIGATION_INTERVAL_MINUTES
            )
            # Duration 3-8 minutes
            duration_s = int(rng.integers(180, 480))
            event_end = event_start + timedelta(seconds=duration_s)

            # Volume roughly 0.3-0.8 L per plant × 16 plants
            volume_l = round(float(rng.uniform(0.3, 0.8) * 16), 2)
            # Target EC and pH for this run
            target_ec = round(float(rng.uniform(2.0, 2.4)), 2)
            target_ph = round(float(rng.uniform(5.8, 6.1)), 2)

            records.append((
                uuid4(),
                str(batch_id),
                "zone-flower-1",
                event_start,
                event_end,
                duration_s,
                volume_l,
                target_ec,
                target_ph,
                "SCHEDULED",
            ))

    await db.executemany(
        """
        INSERT INTO irrigation_events (
            id, batch_id, zone_id, start_time, end_time,
            duration_s, volume_l, target_ec, target_ph, trigger_type
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT DO NOTHING
        """,
        records,
    )

    print(f"  Created {len(records)} irrigation events ({days} days × 6/day)")
    return len(records)


async def create_sample_recommendations(
    db: asyncpg.Connection,
    batch_id: UUID,
) -> int:
    """
    Create 3 sample advisory recommendations for the batch:
      1. PENDING  — current actionable recommendation
      2. ACCEPTED — operator accepted and acted on
      3. EXPIRED  — window passed without action
    """
    now = datetime.now(tz=timezone.utc)

    recommendations = [
        {
            "id": uuid4(),
            "batch_id": str(batch_id),
            "recommendation_type": "ENVIRONMENT",
            "priority": "HIGH",
            "status": "PENDING",
            "title": "Reduce night-time VPD — elevated humidity risk",
            "body": (
                "Over the past 48 hours, night-time relative humidity has averaged 74% "
                "(target ≤ 70%). Current VPD at lights-off is 0.68 kPa, below the "
                "recommended minimum of 0.8 kPa for late-flower. "
                "Recommend increasing dehumidifier setpoint by 5% during dark period "
                "to reduce botrytis risk. Plants are in day 45/63 — a critical window."
            ),
            "suggested_action": (
                '{"action": "adjust_setpoint", "device": "dehumidifier_1", '
                '"parameter": "rh_setpoint_night", "current": 70, "target": 65, "unit": "%"}'
            ),
            "confidence_score": 0.91,
            "model_version": "env-advisor-v1.2.0",
            "feature_snapshot": (
                '{"avg_night_rh_48h": 74.1, "avg_night_vpd_48h": 0.68, '
                '"day_in_flower": 45, "botrytis_risk_score": 0.42}'
            ),
            "created_at": now - timedelta(hours=2),
            "expires_at": now + timedelta(hours=22),
            "acted_at": None,
            "acted_by": None,
        },
        {
            "id": uuid4(),
            "batch_id": str(batch_id),
            "recommendation_type": "IRRIGATION",
            "priority": "MEDIUM",
            "status": "ACCEPTED",
            "title": "Increase irrigation frequency — VWC recovery slow",
            "body": (
                "Volumetric water content is recovering to only 58% between irrigation "
                "events (target 65%). Root zone EC has trended up to 2.6 mS/cm over 3 days "
                "suggesting insufficient leachate. Recommend adding a 7th daily irrigation "
                "event at lights-on + 15 min (pre-dawn soak) to improve EC management "
                "and ensure adequate moisture in the root zone."
            ),
            "suggested_action": (
                '{"action": "add_irrigation_event", "zone": "zone-flower-1", '
                '"time_offset_from_lights_on_min": 15, "duration_s": 240}'
            ),
            "confidence_score": 0.84,
            "model_version": "irrigation-advisor-v0.9.1",
            "feature_snapshot": (
                '{"avg_vwc_post_irrigation_7d": 58.2, "avg_ec_runoff_3d": 2.61, '
                '"daily_irrigation_count": 6, "day_in_flower": 43}'
            ),
            "created_at": now - timedelta(days=2, hours=6),
            "expires_at": now - timedelta(days=1, hours=6),
            "acted_at": now - timedelta(days=2, hours=4),
            "acted_by": "grower@legacyag.co.nz",
        },
        {
            "id": uuid4(),
            "batch_id": str(batch_id),
            "recommendation_type": "NUTRITION",
            "priority": "LOW",
            "status": "EXPIRED",
            "title": "Minor pH drift detected — check dosing calibration",
            "body": (
                "Nutrient solution pH has drifted upward by 0.3 units over 5 days "
                "(5.85 → 6.15). While still within acceptable range (5.8-6.2), "
                "the consistent upward trend may indicate the pH-down peristaltic pump "
                "requires recalibration or the dosing reservoir level is low. "
                "Recommend checking AquaPro channel 2 (pH-down) flow rate calibration."
            ),
            "suggested_action": (
                '{"action": "inspect_device", "device": "aquapro_aqu1ad04a42", '
                '"channel": 2, "check": "flow_rate_calibration"}'
            ),
            "confidence_score": 0.71,
            "model_version": "nutrition-advisor-v1.0.3",
            "feature_snapshot": (
                '{"ph_5d_ago": 5.85, "ph_current": 6.15, "ph_slope_per_day": 0.06, '
                '"day_in_flower": 41}'
            ),
            "created_at": now - timedelta(days=4),
            "expires_at": now - timedelta(days=2),
            "acted_at": None,
            "acted_by": None,
        },
    ]

    for rec in recommendations:
        await db.execute(
            """
            INSERT INTO recommendations (
                id, batch_id, recommendation_type, priority, status,
                title, body, suggested_action, confidence_score,
                model_version, feature_snapshot,
                created_at, expires_at, acted_at, acted_by
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
            )
            ON CONFLICT DO NOTHING
            """,
            rec["id"],
            rec["batch_id"],
            rec["recommendation_type"],
            rec["priority"],
            rec["status"],
            rec["title"],
            rec["body"],
            rec["suggested_action"],
            rec["confidence_score"],
            rec["model_version"],
            rec["feature_snapshot"],
            rec["created_at"],
            rec["expires_at"],
            rec["acted_at"],
            rec["acted_by"],
        )

    status_list = [r["status"] for r in recommendations]
    print(
        f"  Created {len(recommendations)} recommendations "
        f"({', '.join(status_list)})"
    )
    return len(recommendations)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

async def reset_data(db: asyncpg.Connection) -> None:
    """Truncate all tables in reverse dependency order."""
    print("Resetting database tables...")
    await db.execute(
        """
        TRUNCATE TABLE recommendations, irrigation_events,
                       control_actions, sensor_readings, batches
        RESTART IDENTITY CASCADE
        """
    )
    print("  Tables truncated.")


async def main(days: int = 30, reset: bool = False) -> None:
    """Connect to the database and run all seed functions."""
    print(f"\n{'='*60}")
    print("  Cultivation Intelligence — Database Seeder")
    print(f"  Target: {DATABASE_URL.split('@')[-1]}")
    print(f"  Seeding {days} days of sensor history")
    print(f"{'='*60}\n")

    try:
        db: asyncpg.Connection = await asyncpg.connect(DATABASE_URL)
    except Exception as exc:
        print(f"ERROR: Could not connect to database: {exc}", file=sys.stderr)
        print(
            "  Ensure TimescaleDB is running and DATABASE_URL is set correctly.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        if reset:
            await reset_data(db)

        print("Step 1/4 — Creating sample batch...")
        batch_id = await create_sample_batch(db)

        print(f"\nStep 2/4 — Generating {days} days of sensor readings...")
        n_readings = await create_sensor_readings(db, batch_id, days=days)

        print(f"\nStep 3/4 — Creating irrigation events ({days} days)...")
        n_events = await create_sample_irrigation_events(db, batch_id, days=days)

        print("\nStep 4/4 — Creating advisory recommendations...")
        n_recs = await create_sample_recommendations(db, batch_id)

        print(f"\n{'='*60}")
        print("  Seed complete — Summary")
        print(f"{'='*60}")
        print(f"  Batch ID       : {batch_id}")
        print(f"  Sensor readings: {n_readings:,}")
        print(f"  Irrigation evts: {n_events:,}")
        print(f"  Recommendations: {n_recs}")
        print(f"\n  API docs: http://localhost:8000/docs")
        print(f"  Health  : http://localhost:8000/health")
        print()

    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Seed the cultivation database with sample data."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days of historical sensor data to generate (default: 30)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Truncate all tables before seeding (WARNING: destroys existing data)",
    )
    args = parser.parse_args()
    asyncio.run(main(days=args.days, reset=args.reset))
