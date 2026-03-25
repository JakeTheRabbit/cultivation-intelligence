"""
Home Assistant data ingestion via REST API and WebSocket.

Provides:
    HAEntityMapper       — maps HA entity IDs / device_class to SensorType
    HomeAssistantIngester — polls /api/states or /api/history via httpx
    AquaProIngester      — thin specialisation for AquaPro nutrient sensors
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Optional
from uuid import UUID

import httpx
import structlog

from src.app.ingest.base import BaseIngester
from src.app.schemas.sensor import SensorSource, SensorType

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Entity → SensorType mapping
# ---------------------------------------------------------------------------


class HAEntityMapper:
    """Maps Home Assistant entity IDs and device_class attributes to SensorType values."""

    # Ordered list of (compiled_pattern, SensorType) tuples.
    # Patterns are evaluated in order; first match wins.
    ENTITY_PATTERN_MAP: list[tuple[re.Pattern[str], SensorType]] = [
        (re.compile(r".*temperature.*", re.IGNORECASE), SensorType.TEMPERATURE),
        (re.compile(r".*humidity.*", re.IGNORECASE), SensorType.HUMIDITY),
        (re.compile(r".*\bvpd\b.*", re.IGNORECASE), SensorType.VPD_CALCULATED),
        (re.compile(r".*\bec\b.*|.*conductivity.*", re.IGNORECASE), SensorType.EC),
        (re.compile(r".*\bph\b.*", re.IGNORECASE), SensorType.PH),
        (re.compile(r".*\bvwc\b.*|.*moisture.*", re.IGNORECASE), SensorType.VWC),
        (re.compile(r".*\bco2\b.*", re.IGNORECASE), SensorType.CO2),
        (re.compile(r".*ppfd.*|.*\bpar\b.*", re.IGNORECASE), SensorType.PPFD),
        (re.compile(r".*flow.*rate.*|.*flow_rate.*", re.IGNORECASE), SensorType.FLOW_RATE),
        (
            re.compile(r".*dissolved.*oxygen.*|.*\bdo\b.*", re.IGNORECASE),
            SensorType.DISSOLVED_OXYGEN,
        ),
        (re.compile(r".*\bweight\b.*", re.IGNORECASE), SensorType.WEIGHT),
    ]

    # device_class attribute values → SensorType
    _DEVICE_CLASS_MAP: dict[str, SensorType] = {
        "temperature": SensorType.TEMPERATURE,
        "humidity": SensorType.HUMIDITY,
        "carbon_dioxide": SensorType.CO2,
        "illuminance": SensorType.PPFD,
        "weight": SensorType.WEIGHT,
    }

    # Canonical unit strings for each SensorType
    UNIT_MAP: dict[str, str] = {
        "°C": "°C",
        "°F": "°F",
        "%": "%",
        "mS/cm": "mS/cm",
        "µS/cm": "mS/cm",  # normalise micro → milli
        "ms/cm": "mS/cm",
        "pH": "pH",
        "m³/m³": "m³/m³",
        "VWC": "m³/m³",
        "ppm": "ppm",
        "µmol/m²/s": "µmol/m²/s",
        "umol/m2/s": "µmol/m²/s",
        "L/min": "L/min",
        "L/h": "L/h",
        "mg/L": "mg/L",
        "kg": "kg",
        "g": "g",
    }

    @classmethod
    def detect_sensor_type(
        cls, entity_id: str, attributes: dict
    ) -> Optional[SensorType]:
        """Detect SensorType from entity_id pattern, then fall back to device_class.

        Returns None when no mapping can be established.
        """
        # 1. Try entity_id regex patterns
        entity_id_lower = entity_id.lower()
        for pattern, sensor_type in cls.ENTITY_PATTERN_MAP:
            if pattern.match(entity_id_lower):
                return sensor_type

        # 2. Fall back to device_class attribute
        device_class = (attributes or {}).get("device_class", "")
        if device_class:
            return cls._DEVICE_CLASS_MAP.get(device_class.lower())

        return None

    @classmethod
    def normalise_unit(cls, raw_unit: Optional[str]) -> str:
        """Return canonical unit string, or raw_unit if no mapping found."""
        if not raw_unit:
            return ""
        return cls.UNIT_MAP.get(raw_unit, raw_unit)


# ---------------------------------------------------------------------------
# Home Assistant REST ingester
# ---------------------------------------------------------------------------


class HomeAssistantIngester(BaseIngester):
    """Ingests sensor readings from a Home Assistant instance via its REST API.

    Supports:
        - Bulk current-state polling (/api/states)
        - Historical period fetching (/api/history/period)
        - WebSocket subscription for real-time state_changed events
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        batch_id: UUID,
        verify_ssl: bool = False,
        batch_size: int = 100,
    ) -> None:
        super().__init__(batch_size=batch_size)
        # Strip trailing slash so we can always do base_url + "/api/..."
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.batch_id = batch_id
        self.verify_ssl = verify_ssl
        self._mapper = HAEntityMapper()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=self._get_headers(),
            verify=self.verify_ssl,
            timeout=30.0,
        )

    # ------------------------------------------------------------------
    # REST API methods
    # ------------------------------------------------------------------

    async def fetch_states(self) -> list[dict]:
        """GET /api/states — return all entities whose state is a numeric value."""
        url = f"{self.base_url}/api/states"
        async with self._build_client() as client:
            response = await client.get(url)
            response.raise_for_status()
            all_states: list[dict] = response.json()

        numeric_states: list[dict] = []
        for entity in all_states:
            state_val = entity.get("state", "")
            try:
                float(state_val)
                numeric_states.append(entity)
            except (ValueError, TypeError):
                pass

        self.logger.debug(
            "fetch_states_complete",
            total=len(all_states),
            numeric=len(numeric_states),
        )
        return numeric_states

    async def fetch_entity_history(
        self,
        entity_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict]:
        """GET /api/history/period/{start} filtered to a single entity.

        Returns a flat list of state dicts for the entity within the window.
        """
        start_str = start_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        url = f"{self.base_url}/api/history/period/{start_str}"

        params: dict[str, str] = {
            "filter_entity_id": entity_id,
            "end_time": end_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "minimal_response": "true",
            "no_attributes": "false",
            "significant_changes_only": "false",
        }

        async with self._build_client() as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data: list[list[dict]] = response.json()

        # HA returns a list-of-lists (one inner list per entity)
        flat: list[dict] = []
        for entity_history in data:
            flat.extend(entity_history)

        self.logger.debug(
            "fetch_history_complete",
            entity_id=entity_id,
            records=len(flat),
        )
        return flat

    async def get_entity_state(self, entity_id: str) -> Optional[dict]:
        """GET /api/states/{entity_id} — returns the state dict or None on 404."""
        url = f"{self.base_url}/api/states/{entity_id}"
        async with self._build_client() as client:
            response = await client.get(url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()

    # ------------------------------------------------------------------
    # BaseIngester implementation
    # ------------------------------------------------------------------

    async def fetch_readings(  # type: ignore[override]
        self,
        entity_ids: Optional[list[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        **kwargs,
    ) -> AsyncIterator[list[dict]]:
        """Yield batches of raw HA state dicts.

        If entity_ids is provided along with start_time/end_time, fetches
        historical data for each entity.  Otherwise fetches current states.
        """
        if entity_ids and start_time and end_time:
            # Historical mode: one entity at a time, yield in batch_size chunks
            buffer: list[dict] = []
            for eid in entity_ids:
                records = await self.fetch_entity_history(eid, start_time, end_time)
                buffer.extend(records)
                while len(buffer) >= self.batch_size:
                    yield buffer[: self.batch_size]
                    buffer = buffer[self.batch_size :]
            if buffer:
                yield buffer
        else:
            # Current-state mode
            states = await self.fetch_states()
            # Optionally filter to requested entity_ids
            if entity_ids:
                entity_id_set = set(entity_ids)
                states = [s for s in states if s.get("entity_id") in entity_id_set]

            for i in range(0, max(len(states), 1), self.batch_size):
                chunk = states[i : i + self.batch_size]
                if chunk:
                    yield chunk

    async def validate_reading(self, raw: dict) -> tuple[bool, str]:
        """Validate a raw HA state dict.

        Rejects:
            - States of "unavailable", "unknown", "none" (case-insensitive)
            - Non-parseable last_changed timestamps
            - Non-numeric state values
        """
        state_val: str = str(raw.get("state", "")).strip().lower()
        if state_val in {"unavailable", "unknown", "none", ""}:
            return False, f"state is '{state_val}'"

        # Must be numeric
        try:
            float(raw["state"])
        except (KeyError, TypeError, ValueError):
            return False, f"state '{raw.get('state')}' is not numeric"

        # last_changed must be parseable
        last_changed = raw.get("last_changed") or raw.get("last_updated", "")
        if not last_changed:
            return False, "missing last_changed and last_updated fields"

        try:
            datetime.fromisoformat(str(last_changed).replace("Z", "+00:00"))
        except ValueError:
            return False, f"cannot parse timestamp: {last_changed}"

        return True, ""

    async def transform_reading(self, raw: dict) -> dict:
        """Transform a validated HA state dict into a SensorReadingCreate dict."""
        entity_id: str = raw.get("entity_id", "")
        attributes: dict = raw.get("attributes", {})
        state_str: str = str(raw.get("state", "0"))
        last_changed: str = (
            raw.get("last_changed") or raw.get("last_updated", "")
        )

        value = float(state_str)

        # Parse timestamp
        ts = datetime.fromisoformat(last_changed.replace("Z", "+00:00"))

        # Detect sensor type
        sensor_type = self._mapper.detect_sensor_type(entity_id, attributes)
        if sensor_type is None:
            # Default to TEMPERATURE as a safe fallback — caller can filter
            sensor_type = SensorType.TEMPERATURE

        # Normalise unit
        raw_unit = attributes.get("unit_of_measurement", "")
        unit = self._mapper.normalise_unit(raw_unit) or raw_unit or "unknown"

        return {
            "sensor_id": entity_id,
            "batch_id": self.batch_id,
            "sensor_type": sensor_type,
            "value": value,
            "unit": unit,
            "timestamp": ts,
            "raw_entity_id": entity_id,
            "source": SensorSource.HA_POLL,
        }

    # ------------------------------------------------------------------
    # WebSocket subscription
    # ------------------------------------------------------------------

    async def subscribe_websocket(
        self,
        entity_ids: list[str],
        callback: Callable[[dict], Any],
    ) -> None:
        """Connect to HA WebSocket API and stream state_changed events.

        Authentication flow:
            1. Receive auth_required
            2. Send auth message with long-lived token
            3. Receive auth_ok
            4. Subscribe to state_changed events
            5. Filter incoming events to entity_ids and invoke callback

        Args:
            entity_ids: List of HA entity IDs to monitor.
            callback:   Async or sync callable receiving the transformed
                        reading dict for each matched event.
        """
        try:
            import websockets  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "websockets package is required for WebSocket support. "
                "Install with: pip install websockets"
            ) from exc

        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/api/websocket"

        entity_id_set = set(entity_ids)
        msg_id = 1

        self.logger.info("websocket_connecting", url=ws_url, entities=entity_ids)

        async with websockets.connect(ws_url) as ws:
            # Step 1: Receive auth_required
            raw_msg = await ws.recv()
            msg = json.loads(raw_msg)
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"Expected auth_required, got: {msg}")

            # Step 2: Authenticate
            await ws.send(
                json.dumps({"type": "auth", "access_token": self.token})
            )

            # Step 3: Receive auth_ok
            raw_msg = await ws.recv()
            msg = json.loads(raw_msg)
            if msg.get("type") != "auth_ok":
                raise RuntimeError(f"Authentication failed: {msg}")

            self.logger.info("websocket_authenticated")

            # Step 4: Subscribe to state_changed events
            await ws.send(
                json.dumps(
                    {
                        "id": msg_id,
                        "type": "subscribe_events",
                        "event_type": "state_changed",
                    }
                )
            )
            msg_id += 1

            # Confirm subscription
            raw_msg = await ws.recv()
            sub_result = json.loads(raw_msg)
            if not sub_result.get("success", False):
                raise RuntimeError(f"Subscription failed: {sub_result}")

            self.logger.info("websocket_subscribed")

            # Step 5: Stream events
            async for raw_msg in ws:
                try:
                    event_wrapper = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue

                if event_wrapper.get("type") != "event":
                    continue

                event_data = event_wrapper.get("event", {}).get("data", {})
                changed_entity = event_data.get("entity_id", "")

                if changed_entity not in entity_id_set:
                    continue

                new_state = event_data.get("new_state")
                if not new_state:
                    continue

                valid, err = await self.validate_reading(new_state)
                if not valid:
                    self.logger.debug(
                        "ws_reading_rejected", entity_id=changed_entity, reason=err
                    )
                    continue

                transformed = await self.transform_reading(new_state)

                if callable(callback):
                    import asyncio

                    if asyncio.iscoroutinefunction(callback):
                        await callback(transformed)
                    else:
                        callback(transformed)


# ---------------------------------------------------------------------------
# AquaPro specialisation
# ---------------------------------------------------------------------------


class AquaProIngester(HomeAssistantIngester):
    """Ingests data from AquaPro AQ1AD04A42 nutrient-dosing sensors.

    Provides a structured dict of all AquaPro readings via fetch_aquapro_state,
    and delegates to the parent's validate/transform pipeline.
    """

    AQUAPRO_ENTITIES: dict[str, str] = {
        "ec": "sensor.aquapro_aq1ad04a42_ec",
        "ph": "sensor.aquapro_aq1ad04a42_ph",
        "flow_rate": "sensor.aquapro_aq1ad04a42_flow_rate",
        "total_volume": "sensor.aquapro_aq1ad04a42_total_volume",
    }

    async def fetch_aquapro_state(self) -> dict[str, Any]:
        """Fetch all AquaPro entity states and return a structured dict.

        Returns:
            {
                "ec":           {"value": 2.1, "unit": "mS/cm", "timestamp": ...},
                "ph":           {"value": 6.2, "unit": "pH",    "timestamp": ...},
                "flow_rate":    {"value": 1.4, "unit": "L/min", "timestamp": ...},
                "total_volume": {"value": 42.0, "unit": "L",    "timestamp": ...},
            }

        Missing / unavailable entities are omitted from the dict.
        """
        result: dict[str, Any] = {}

        for key, entity_id in self.AQUAPRO_ENTITIES.items():
            state = await self.get_entity_state(entity_id)
            if state is None:
                self.logger.warning("aquapro_entity_missing", entity_id=entity_id)
                continue

            state_str = str(state.get("state", "")).lower()
            if state_str in {"unavailable", "unknown", "none", ""}:
                self.logger.debug(
                    "aquapro_entity_unavailable",
                    entity_id=entity_id,
                    state=state_str,
                )
                continue

            try:
                value = float(state["state"])
            except (KeyError, ValueError, TypeError):
                continue

            attributes = state.get("attributes", {})
            raw_unit = attributes.get("unit_of_measurement", "")
            unit = self._mapper.normalise_unit(raw_unit) or raw_unit

            last_changed = state.get("last_changed") or state.get("last_updated", "")
            try:
                ts = datetime.fromisoformat(last_changed.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                ts = datetime.now(timezone.utc)

            result[key] = {
                "entity_id": entity_id,
                "value": value,
                "unit": unit,
                "timestamp": ts.isoformat(),
                "attributes": attributes,
                "state": state,
            }

        return result

    async def fetch_readings(self, **kwargs) -> AsyncIterator[list[dict]]:  # type: ignore[override]
        """Yield a single batch containing all current AquaPro sensor states."""
        aquapro_data = await self.fetch_aquapro_state()

        batch: list[dict] = []
        for _key, entry in aquapro_data.items():
            # Pass the raw HA state dict so validate/transform can work normally
            batch.append(entry["state"])

        if batch:
            yield batch
