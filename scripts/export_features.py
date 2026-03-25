#!/usr/bin/env python3
"""
Export engineered features from cultivation data to CSV/Parquet for model training.

This script fetches raw sensor readings from TimescaleDB, applies the same feature
engineering pipeline used at inference time, and writes the result to a flat file
suitable for training LightGBM or Temporal Fusion Transformer models.

Usage:
    # Export specific batches
    python scripts/export_features.py \\
        --batch-ids 550e8400-e29b-41d4-a716-446655440000 \\
                    6ba7b810-9dad-11d1-80b4-00c04fd430c8 \\
        --output features.parquet

    # Export all completed batches including training labels
    python scripts/export_features.py --all-completed --output training_set.parquet

    # Export without outcome labels (inference / scoring)
    python scripts/export_features.py --all-completed --no-outcomes --output score.csv
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from uuid import UUID

import asyncpg
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://cultivation:cultivation@localhost:5432/cultivation",
)

# Sensor types we expect in the wide-format pivot
SENSOR_COLUMNS = [
    "TEMPERATURE",
    "HUMIDITY",
    "VPD_CALCULATED",
    "CO2",
    "PPFD",
    "EC",
    "PH",
    "VWC",
    "FLOW_RATE",
    "DISSOLVED_OXYGEN",
]

# Resample frequency for base time-series
RESAMPLE_FREQ = "15min"

# Light schedule constants (used in DLI and photoperiod features)
LIGHT_ON_HOUR_LOCAL = 6         # 06:00 NZST
PHOTOPERIOD_HOURS = 18
PPFD_ACTIVE_THRESHOLD = 50.0    # μmol/m²/s — above this = lights on


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def fetch_batch_sensor_data(
    db: asyncpg.Connection,
    batch_id: UUID,
) -> pd.DataFrame:
    """
    Query all sensor_readings for *batch_id*, pivot to wide format,
    and resample to 15-minute intervals.

    Returns a DataFrame indexed by UTC timestamp with one column per sensor type.
    Missing values are forward-filled then back-filled.
    """
    rows = await db.fetch(
        """
        SELECT
            time,
            sensor_type,
            value
        FROM sensor_readings
        WHERE batch_id = $1
          AND quality_flag IN ('OK', 'SUSPECT_SPIKE')   -- exclude invalid/offline
        ORDER BY time ASC
        """,
        str(batch_id),
    )

    if not rows:
        return pd.DataFrame()

    df_raw = pd.DataFrame(rows, columns=["time", "sensor_type", "value"])
    df_raw["time"] = pd.to_datetime(df_raw["time"], utc=True)
    df_raw["value"] = pd.to_numeric(df_raw["value"], errors="coerce")

    # Pivot: index=time, columns=sensor_type, values=value
    # Use mean to collapse any duplicate timestamps per sensor
    df_wide = df_raw.pivot_table(
        index="time",
        columns="sensor_type",
        values="value",
        aggfunc="mean",
    )
    df_wide.columns.name = None

    # Ensure all expected sensor columns exist (fill missing with NaN)
    for col in SENSOR_COLUMNS:
        if col not in df_wide.columns:
            df_wide[col] = np.nan

    df_wide = df_wide[SENSOR_COLUMNS]  # consistent column order

    # Resample to regular 15-minute grid
    df_resampled = (
        df_wide
        .resample(RESAMPLE_FREQ)
        .mean()
        .ffill(limit=6)   # forward fill up to 90 min gap
        .bfill(limit=2)   # back fill at series start
    )

    return df_resampled


async def fetch_batch_metadata(
    db: asyncpg.Connection,
    batch_id: UUID,
) -> dict:
    """Fetch batch metadata row as a dictionary."""
    row = await db.fetchrow(
        """
        SELECT
            id,
            batch_name,
            strain,
            room_id,
            start_date,
            current_stage,
            planned_veg_days,
            planned_flower_days,
            genetics_type,
            genetics_thc_target_pct,
            substrate,
            lighting_type,
            num_plants,
            room_dimensions_m3
        FROM batches
        WHERE id = $1
        """,
        str(batch_id),
    )
    if row is None:
        raise ValueError(f"Batch {batch_id} not found in database")
    return dict(row)


async def fetch_batch_outcomes(
    db: asyncpg.Connection,
    batch_id: UUID,
) -> dict:
    """
    Fetch outcome labels for a completed batch.

    Returns a dict with keys:
      - yield_g          : total yield in grams (float or None)
      - yield_g_per_plant: per-plant yield (float or None)
      - quality_grade    : GRADE_A / GRADE_B / GRADE_C / REJECTED (str or None)
      - cycle_days       : actual number of days in grow cycle (int or None)
    """
    row = await db.fetchrow(
        """
        SELECT
            actual_yield_g,
            quality_grade,
            num_plants,
            start_date,
            end_date
        FROM batches
        WHERE id = $1
        """,
        str(batch_id),
    )
    if row is None:
        return {}

    outcomes: dict = {}
    outcomes["yield_g"] = row["actual_yield_g"]
    outcomes["quality_grade"] = row["quality_grade"]

    if row["actual_yield_g"] is not None and row["num_plants"]:
        outcomes["yield_g_per_plant"] = round(
            row["actual_yield_g"] / row["num_plants"], 2
        )
    else:
        outcomes["yield_g_per_plant"] = None

    if row["start_date"] and row["end_date"]:
        outcomes["cycle_days"] = (row["end_date"] - row["start_date"]).days
    else:
        outcomes["cycle_days"] = None

    return outcomes


async def fetch_all_completed_batch_ids(db: asyncpg.Connection) -> list[UUID]:
    """Return UUIDs of all batches with current_stage = 'COMPLETE'."""
    rows = await db.fetch(
        "SELECT id FROM batches WHERE current_stage = 'COMPLETE' ORDER BY start_date"
    )
    return [UUID(str(r["id"])) for r in rows]


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def _compute_vpd_features(df: pd.DataFrame) -> pd.Series:
    """
    Compute daily average VPD during lights-on and lights-off periods.
    Uses VPD_CALCULATED if available; falls back to deriving from TEMPERATURE + HUMIDITY.
    """
    features = {}
    if "VPD_CALCULATED" in df.columns and df["VPD_CALCULATED"].notna().any():
        vpd = df["VPD_CALCULATED"]
    elif "TEMPERATURE" in df.columns and "HUMIDITY" in df.columns:
        temp = df["TEMPERATURE"]
        rh = df["HUMIDITY"]
        svp = 0.6108 * np.exp(17.27 * temp / (temp + 237.3))
        vpd = svp * (1 - rh / 100.0)
    else:
        vpd = pd.Series(np.nan, index=df.index)

    ppfd = df.get("PPFD", pd.Series(0.0, index=df.index))
    lights_on_mask = ppfd > PPFD_ACTIVE_THRESHOLD

    features["vpd_mean_lights_on"] = vpd[lights_on_mask].mean()
    features["vpd_mean_lights_off"] = vpd[~lights_on_mask].mean()
    features["vpd_std"] = vpd.std()
    features["vpd_min"] = vpd.min()
    features["vpd_max"] = vpd.max()
    return pd.Series(features)


def _compute_dli(df: pd.DataFrame) -> float:
    """
    Compute Daily Light Integral (mol/m²/day) from PPFD readings.

    DLI = sum(PPFD × interval_seconds) / 1,000,000
    With 15-min intervals: interval_seconds = 900
    """
    if "PPFD" not in df.columns or df["PPFD"].isna().all():
        return np.nan
    interval_s = 900  # 15 minutes
    dli = float((df["PPFD"].fillna(0) * interval_s).sum() / 1_000_000)
    return round(dli, 3)


def _compute_ec_ph_features(df: pd.DataFrame) -> pd.Series:
    """EC and pH statistics plus drift metrics."""
    features = {}
    for col, prefix in [("EC", "ec"), ("PH", "ph")]:
        series = df.get(col, pd.Series(dtype=float))
        features[f"{prefix}_mean"] = series.mean()
        features[f"{prefix}_std"] = series.std()
        features[f"{prefix}_min"] = series.min()
        features[f"{prefix}_max"] = series.max()
        # Drift = last quartile mean minus first quartile mean
        q = len(series) // 4
        if q > 0:
            features[f"{prefix}_drift"] = series.iloc[-q:].mean() - series.iloc[:q].mean()
        else:
            features[f"{prefix}_drift"] = np.nan
    return pd.Series(features)


def _compute_vwc_features(df: pd.DataFrame) -> pd.Series:
    """
    Volumetric water content features: mean, trough (minimum between events),
    number of irrigation cycles, and depletion rate.
    """
    features = {}
    vwc = df.get("VWC", pd.Series(dtype=float))
    features["vwc_mean"] = vwc.mean()
    features["vwc_min"] = vwc.min()
    features["vwc_max"] = vwc.max()
    features["vwc_std"] = vwc.std()

    # Count irrigation events as peaks in VWC signal
    if len(vwc.dropna()) > 4:
        # A "peak" = value rises by > 8% from previous reading
        diff = vwc.diff()
        irrigation_events = (diff > 8.0).sum()
        features["irrigation_event_count"] = int(irrigation_events)
        # Average depletion: mean rate of decline between irrigations
        declining = diff[diff < 0]
        features["vwc_depletion_rate_per_15min"] = float(declining.mean()) if len(declining) else np.nan
    else:
        features["irrigation_event_count"] = np.nan
        features["vwc_depletion_rate_per_15min"] = np.nan

    return pd.Series(features)


def _compute_temperature_features(df: pd.DataFrame) -> pd.Series:
    """Temperature and humidity statistics split by light period."""
    features = {}
    ppfd = df.get("PPFD", pd.Series(0.0, index=df.index))
    lights_on_mask = ppfd > PPFD_ACTIVE_THRESHOLD
    lights_off_mask = ~lights_on_mask

    for col, prefix in [("TEMPERATURE", "temp"), ("HUMIDITY", "rh"), ("CO2", "co2")]:
        series = df.get(col, pd.Series(dtype=float))
        features[f"{prefix}_mean"] = series.mean()
        features[f"{prefix}_std"] = series.std()
        features[f"{prefix}_mean_lights_on"] = series[lights_on_mask].mean()
        features[f"{prefix}_mean_lights_off"] = series[lights_off_mask].mean()
        # Day/night differential
        features[f"{prefix}_day_night_diff"] = (
            features[f"{prefix}_mean_lights_on"]
            - features[f"{prefix}_mean_lights_off"]
        )

    return pd.Series(features)


async def compute_features_for_batch(
    batch_id: UUID,
    sensor_df: pd.DataFrame,
    batch_metadata: dict,
) -> pd.DataFrame:
    """
    Run the full feature pipeline on a batch's sensor data.

    Features are computed per calendar day and returned as a DataFrame
    with one row per day and all engineered features as columns.
    Batch-level metadata (strain, stage, plant count, etc.) are joined
    as constant columns on every row.
    """
    if sensor_df.empty:
        return pd.DataFrame()

    daily_records = []

    # Iterate over each unique date in the sensor data
    sensor_df["_date"] = sensor_df.index.date
    for date, day_df in sensor_df.groupby("_date"):
        day_df = day_df.drop(columns=["_date"])

        record: dict = {
            "batch_id": str(batch_id),
            "date": date,
            "n_sensor_readings": len(day_df),
        }

        # VPD features
        vpd_feats = _compute_vpd_features(day_df)
        record.update(vpd_feats.to_dict())

        # DLI
        record["dli_mol_m2_day"] = _compute_dli(day_df)

        # EC/pH
        ec_ph_feats = _compute_ec_ph_features(day_df)
        record.update(ec_ph_feats.to_dict())

        # VWC / irrigation
        vwc_feats = _compute_vwc_features(day_df)
        record.update(vwc_feats.to_dict())

        # Temperature / humidity / CO2
        env_feats = _compute_temperature_features(day_df)
        record.update(env_feats.to_dict())

        # PPFD summary
        ppfd = day_df.get("PPFD", pd.Series(dtype=float))
        record["ppfd_mean_lights_on"] = float(ppfd[ppfd > PPFD_ACTIVE_THRESHOLD].mean()) if (ppfd > PPFD_ACTIVE_THRESHOLD).any() else 0.0
        record["ppfd_photoperiod_h"] = float((ppfd > PPFD_ACTIVE_THRESHOLD).sum() * 0.25)  # 15-min intervals

        # Batch metadata features (constant across days)
        record["strain"] = batch_metadata.get("strain", "")
        record["genetics_type"] = batch_metadata.get("genetics_type", "")
        record["substrate"] = batch_metadata.get("substrate", "")
        record["lighting_type"] = batch_metadata.get("lighting_type", "")
        record["num_plants"] = batch_metadata.get("num_plants")
        record["room_dimensions_m3"] = batch_metadata.get("room_dimensions_m3")
        record["planned_veg_days"] = batch_metadata.get("planned_veg_days")
        record["planned_flower_days"] = batch_metadata.get("planned_flower_days")

        # Day-in-cycle feature
        start_date = batch_metadata.get("start_date")
        if start_date is not None:
            if hasattr(start_date, "date"):
                start_date = start_date.date()
            record["day_in_cycle"] = (date - start_date).days
            veg_days = batch_metadata.get("planned_veg_days", 21)
            record["day_in_flower"] = max(0, record["day_in_cycle"] - veg_days)
            record["pct_flower_complete"] = round(
                record["day_in_flower"] / batch_metadata.get("planned_flower_days", 63), 4
            )
        else:
            record["day_in_cycle"] = np.nan
            record["day_in_flower"] = np.nan
            record["pct_flower_complete"] = np.nan

        daily_records.append(record)

    if not daily_records:
        return pd.DataFrame()

    return pd.DataFrame(daily_records).set_index(["batch_id", "date"])


# ---------------------------------------------------------------------------
# Export orchestration
# ---------------------------------------------------------------------------

async def export_features(
    batch_ids: list[UUID],
    output_path: Path,
    include_outcomes: bool = True,
) -> None:
    """
    Fetch sensor data for all batches, compute features, optionally join
    outcome labels, and write to Parquet or CSV.
    """
    try:
        db: asyncpg.Connection = await asyncpg.connect(DATABASE_URL)
    except Exception as exc:
        print(f"ERROR: Could not connect to database: {exc}", file=sys.stderr)
        sys.exit(1)

    all_feature_dfs: list[pd.DataFrame] = []
    outcome_rows: list[dict] = []

    outcome_complete = 0
    min_date: datetime | None = None
    max_date: datetime | None = None

    print(f"\nExporting features for {len(batch_ids)} batch(es)...")

    try:
        for i, batch_id in enumerate(batch_ids, start=1):
            print(f"  [{i}/{len(batch_ids)}] Batch {batch_id}...", end=" ", flush=True)

            try:
                sensor_df = await fetch_batch_sensor_data(db, batch_id)
                if sensor_df.empty:
                    print("no sensor data, skipping.")
                    continue

                metadata = await fetch_batch_metadata(db, batch_id)
                feature_df = await compute_features_for_batch(batch_id, sensor_df, metadata)

                if feature_df.empty:
                    print("feature computation returned empty, skipping.")
                    continue

                all_feature_dfs.append(feature_df)

                # Track date range
                dates = sensor_df.index
                batch_min = dates.min().to_pydatetime()
                batch_max = dates.max().to_pydatetime()
                min_date = batch_min if min_date is None else min(min_date, batch_min)
                max_date = batch_max if max_date is None else max(max_date, batch_max)

                if include_outcomes:
                    outcomes = await fetch_batch_outcomes(db, batch_id)
                    if outcomes.get("yield_g") is not None:
                        outcome_complete += 1
                    outcomes["batch_id"] = str(batch_id)
                    outcome_rows.append(outcomes)

                print(f"{len(feature_df)} day-rows, {len(feature_df.columns)} features.")

            except Exception as exc:
                print(f"ERROR processing batch {batch_id}: {exc}", file=sys.stderr)
                continue

    finally:
        await db.close()

    if not all_feature_dfs:
        print("\nNo features computed — nothing to export.", file=sys.stderr)
        sys.exit(1)

    combined = pd.concat(all_feature_dfs, axis=0)

    # Join outcomes if requested
    if include_outcomes and outcome_rows:
        outcomes_df = pd.DataFrame(outcome_rows).set_index("batch_id")
        # Outcomes are batch-level; merge onto every row for that batch
        combined = combined.reset_index()
        combined = combined.merge(outcomes_df, on="batch_id", how="left")
        combined = combined.set_index(["batch_id", "date"])

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix.lower()
    if suffix == ".parquet":
        combined.reset_index().to_parquet(output_path, index=False, engine="pyarrow")
    elif suffix == ".csv":
        combined.reset_index().to_csv(output_path, index=False)
    else:
        # Default to parquet
        output_path = output_path.with_suffix(".parquet")
        combined.reset_index().to_parquet(output_path, index=False, engine="pyarrow")

    # Summary
    n_features = len(combined.columns)
    if include_outcomes and outcome_rows:
        outcome_cols = ["yield_g", "yield_g_per_plant", "quality_grade", "cycle_days"]
        n_features -= len([c for c in outcome_cols if c in combined.columns])

    date_range_str = (
        f"{min_date.date()} → {max_date.date()}"
        if min_date and max_date
        else "unknown"
    )
    outcome_pct = (
        f"{outcome_complete}/{len(batch_ids)} ({outcome_complete/len(batch_ids)*100:.0f}%)"
        if include_outcomes
        else "N/A (excluded)"
    )

    print(f"\n{'='*60}")
    print("  Feature Export Summary")
    print(f"{'='*60}")
    print(f"  Output file       : {output_path.resolve()}")
    print(f"  File format       : {output_path.suffix.upper()[1:]}")
    print(f"  Batches exported  : {len(all_feature_dfs)}")
    print(f"  Day-rows total    : {len(combined):,}")
    print(f"  Feature columns   : {n_features}")
    print(f"  Date range        : {date_range_str}")
    print(f"  Outcome completeness: {outcome_pct}")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export engineered features from cultivation sensor data "
            "to CSV or Parquet for model training."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    id_group = parser.add_mutually_exclusive_group(required=True)
    id_group.add_argument(
        "--batch-ids",
        nargs="+",
        metavar="UUID",
        help="One or more batch UUIDs to export",
    )
    id_group.add_argument(
        "--all-completed",
        action="store_true",
        help="Export all batches with current_stage = 'COMPLETE'",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("features_export.parquet"),
        help="Output file path (extension determines format: .parquet or .csv)",
    )
    parser.add_argument(
        "--include-outcomes",
        dest="include_outcomes",
        action="store_true",
        default=True,
        help="Include yield and quality outcome columns (default: true)",
    )
    parser.add_argument(
        "--no-outcomes",
        dest="include_outcomes",
        action="store_false",
        help="Exclude outcome columns (useful for inference/scoring sets)",
    )

    args = parser.parse_args()

    async def _run() -> None:
        if args.all_completed:
            try:
                db = await asyncpg.connect(DATABASE_URL)
            except Exception as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                sys.exit(1)
            batch_ids = await fetch_all_completed_batch_ids(db)
            await db.close()
            if not batch_ids:
                print("No completed batches found in database.", file=sys.stderr)
                sys.exit(0)
            print(f"Found {len(batch_ids)} completed batch(es).")
        else:
            try:
                batch_ids = [UUID(uid) for uid in args.batch_ids]
            except ValueError as exc:
                print(f"ERROR: Invalid UUID: {exc}", file=sys.stderr)
                sys.exit(1)

        await export_features(
            batch_ids=batch_ids,
            output_path=args.output,
            include_outcomes=args.include_outcomes,
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
