"""
CSV historical data importer.

Supports reading sensor data from CSV files exported by Home Assistant,
manual logging spreadsheets, or any tabular format via CsvColumnConfig.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional
from uuid import UUID

import pandas as pd
import structlog

from src.app.ingest.base import BaseIngester
from src.app.schemas.sensor import SensorSource, SensorType

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Column configuration
# ---------------------------------------------------------------------------


@dataclass
class CsvColumnConfig:
    """Maps CSV column names to the canonical sensor reading fields.

    All column names are case-sensitive and must match the CSV header exactly.
    Optional columns default to None, meaning the field will be inferred or
    left empty.
    """

    timestamp_col: str = "timestamp"
    sensor_id_col: str = "sensor_id"
    value_col: str = "value"
    unit_col: Optional[str] = "unit"
    sensor_type_col: Optional[str] = "sensor_type"
    # Optional extra columns to carry through as metadata
    extra_cols: list[str] = field(default_factory=list)


# Preset configs for common export formats
PRESET_CONFIGS: dict[str, CsvColumnConfig] = {
    # Home Assistant Statistics export (Settings → System → Storage → Statistics)
    "ha_export": CsvColumnConfig(
        timestamp_col="statistic_id",  # HA uses 'start' for the time column
        sensor_id_col="statistic_id",
        value_col="mean",
        unit_col="unit",
        sensor_type_col=None,
        extra_cols=["min", "max", "sum", "state"],
    ),
    # Simple hand-logged format
    "manual_log": CsvColumnConfig(
        timestamp_col="date_time",
        sensor_id_col="sensor_name",
        value_col="reading",
        unit_col="units",
        sensor_type_col="type",
    ),
    # Generic tidy-data format (default)
    "tidy": CsvColumnConfig(
        timestamp_col="timestamp",
        sensor_id_col="sensor_id",
        value_col="value",
        unit_col="unit",
        sensor_type_col="sensor_type",
    ),
}

# Strings that are accepted as SensorType names (case-insensitive)
_SENSOR_TYPE_ALIASES: dict[str, SensorType] = {
    st.value.lower(): st for st in SensorType
} | {
    "temp": SensorType.TEMPERATURE,
    "rh": SensorType.HUMIDITY,
    "relative_humidity": SensorType.HUMIDITY,
    "electroconductivity": SensorType.EC,
    "electrical_conductivity": SensorType.EC,
    "acidity": SensorType.PH,
    "soil_moisture": SensorType.VWC,
    "volumetric_water_content": SensorType.VWC,
    "carbon_dioxide": SensorType.CO2,
    "par": SensorType.PPFD,
    "flow": SensorType.FLOW_RATE,
    "dissolved_o2": SensorType.DISSOLVED_OXYGEN,
    "do": SensorType.DISSOLVED_OXYGEN,
}


def _parse_sensor_type(raw: str) -> Optional[SensorType]:
    """Attempt to parse a sensor type string to a SensorType enum value."""
    if not raw:
        return None
    return _SENSOR_TYPE_ALIASES.get(str(raw).strip().lower())


def _parse_timestamp(raw) -> Optional[datetime]:
    """Parse a timestamp value to a timezone-aware datetime."""
    if pd.isna(raw):
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    try:
        ts = pd.to_datetime(str(raw), utc=True)
        return ts.to_pydatetime()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# CSV ingester
# ---------------------------------------------------------------------------


class CsvIngester(BaseIngester):
    """Reads sensor readings from a CSV file in configurable batches.

    Uses pandas chunked reading to keep memory usage bounded even for
    large historical exports.

    Args:
        file_path:   Path to the CSV file.
        batch_id:    Grow batch UUID to associate with all readings.
        config:      Column mapping configuration.
        chunk_size:  Number of CSV rows per yielded batch.
    """

    def __init__(
        self,
        file_path: Path,
        batch_id: UUID,
        config: CsvColumnConfig,
        chunk_size: int = 1000,
        batch_size: int = 100,
    ) -> None:
        super().__init__(batch_size=batch_size)
        self.file_path = Path(file_path)
        self.batch_id = batch_id
        self.config = config
        self.chunk_size = chunk_size

    # ------------------------------------------------------------------
    # BaseIngester implementation
    # ------------------------------------------------------------------

    async def fetch_readings(self, **kwargs) -> AsyncIterator[list[dict]]:  # type: ignore[override]
        """Read the CSV file in chunks and yield each chunk as a list of dicts.

        Uses asyncio.to_thread so the blocking pandas read does not block the
        event loop.  Each yielded list contains up to chunk_size items.
        """
        if not self.file_path.exists():
            raise FileNotFoundError(f"CSV file not found: {self.file_path}")

        self.logger.info(
            "csv_fetch_start", file=str(self.file_path), chunk_size=self.chunk_size
        )

        def _read_chunk(skip_rows: int) -> Optional[pd.DataFrame]:
            """Read one chunk from the CSV, return None at EOF."""
            try:
                return pd.read_csv(
                    self.file_path,
                    skiprows=range(1, skip_rows + 1) if skip_rows > 0 else None,
                    nrows=self.chunk_size,
                )
            except Exception:  # noqa: BLE001
                return None

        # Use pandas chunked reader (blocking I/O offloaded to thread pool)
        loop = asyncio.get_event_loop()

        def _iter_chunks():
            """Generator that yields DataFrames from the CSV in chunks."""
            try:
                reader = pd.read_csv(self.file_path, chunksize=self.chunk_size)
                for chunk_df in reader:
                    yield chunk_df
            except Exception as exc:
                raise RuntimeError(f"Failed to read CSV {self.file_path}: {exc}") from exc

        # Run blocking CSV iteration in a thread executor
        import concurrent.futures

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        chunks_future = loop.run_in_executor(
            executor, lambda: list(_iter_chunks())
        )
        chunks: list[pd.DataFrame] = await chunks_future
        executor.shutdown(wait=False)

        for chunk_df in chunks:
            rows = chunk_df.to_dict(orient="records")
            self.logger.debug("csv_chunk_yielded", rows=len(rows))
            yield rows

    async def validate_reading(self, raw: dict) -> tuple[bool, str]:
        """Validate a single CSV row dict.

        Checks:
            1. Timestamp column exists and is parseable.
            2. Value column exists and is a valid float.
            3. If sensor_type_col is configured, the value must be recognised.
        """
        cfg = self.config

        # --- Timestamp ---
        ts_raw = raw.get(cfg.timestamp_col)
        if ts_raw is None or (isinstance(ts_raw, float) and pd.isna(ts_raw)):
            return False, f"missing or null timestamp column '{cfg.timestamp_col}'"

        ts = _parse_timestamp(ts_raw)
        if ts is None:
            return False, f"cannot parse timestamp: '{ts_raw}'"

        # --- Value ---
        value_raw = raw.get(cfg.value_col)
        if value_raw is None:
            return False, f"missing value column '{cfg.value_col}'"
        try:
            float_val = float(value_raw)
        except (ValueError, TypeError):
            return False, f"value '{value_raw}' is not numeric"

        if pd.isna(float_val):
            return False, "value is NaN"

        # --- Sensor type (optional column) ---
        if cfg.sensor_type_col:
            type_raw = raw.get(cfg.sensor_type_col, "")
            if type_raw and not isinstance(type_raw, float):
                st = _parse_sensor_type(str(type_raw))
                if st is None:
                    return False, f"unrecognised sensor_type: '{type_raw}'"

        return True, ""

    async def transform_reading(self, raw: dict) -> dict:
        """Transform a validated CSV row dict into a SensorReadingCreate dict."""
        cfg = self.config

        ts = _parse_timestamp(raw[cfg.timestamp_col])

        value = float(raw[cfg.value_col])

        sensor_id = str(raw.get(cfg.sensor_id_col, "csv_sensor")).strip()

        unit = ""
        if cfg.unit_col and cfg.unit_col in raw:
            unit_raw = raw[cfg.unit_col]
            unit = str(unit_raw).strip() if not pd.isna(unit_raw) else ""

        sensor_type: Optional[SensorType] = None
        if cfg.sensor_type_col and cfg.sensor_type_col in raw:
            type_raw = raw[cfg.sensor_type_col]
            if type_raw and not (isinstance(type_raw, float) and pd.isna(type_raw)):
                sensor_type = _parse_sensor_type(str(type_raw))

        # Fall back to TEMPERATURE if still unknown
        if sensor_type is None:
            sensor_type = SensorType.TEMPERATURE

        return {
            "sensor_id": sensor_id,
            "batch_id": self.batch_id,
            "sensor_type": sensor_type,
            "value": value,
            "unit": unit or "unknown",
            "timestamp": ts,
            "raw_entity_id": sensor_id,
            "source": SensorSource.CSV_IMPORT,
        }

    # ------------------------------------------------------------------
    # Alternative programmatic entry point
    # ------------------------------------------------------------------

    async def load_from_dataframe(
        self, df: pd.DataFrame, batch_id: UUID
    ) -> AsyncIterator[list[dict]]:
        """Yield batches from an in-memory DataFrame instead of reading from disk.

        Useful when the caller has already loaded or constructed the DataFrame
        programmatically (e.g., after data cleaning or merging).

        The DataFrame must have columns matching the CsvColumnConfig.

        Args:
            df:       Source DataFrame.
            batch_id: Grow batch UUID to embed in every reading.
        """
        # Override the instance batch_id for this call
        original_batch_id = self.batch_id
        self.batch_id = batch_id

        try:
            all_rows = df.to_dict(orient="records")
            for i in range(0, max(len(all_rows), 1), self.chunk_size):
                chunk = all_rows[i : i + self.chunk_size]
                if chunk:
                    self.logger.debug("df_chunk_yielded", rows=len(chunk))
                    yield chunk
        finally:
            self.batch_id = original_batch_id
