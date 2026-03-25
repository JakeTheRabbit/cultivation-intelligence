"""
Home Assistant REST API control adapter with safety guard integration.

Every actuator write goes through SafetyController.check_*() before the HTTP
call is made.  Failed safety checks raise ControlActionBlocked or
AdvisoryModeBlock — never silently proceeding.

Docs: https://developers.home-assistant.io/docs/api/rest/
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import httpx
import structlog

from src.app.controls.safety import (
    AdvisoryModeBlock,
    ControlActionBlocked,
    SafetyController,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Default timeouts
# ---------------------------------------------------------------------------
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)


class HAControlAdapter:
    """Adapter for the Home Assistant REST API.

    Wraps HA API calls with:
    - Safety constraint checking (hard limits, rate limits, advisory mode).
    - Structured logging for every outgoing action.
    - Audit trail via the ``control_actions`` DB table (when a session is
      provided — optional, to avoid circular imports).

    Args:
        base_url: Base URL of the HA instance, e.g. ``http://homeassistant.local:8123``.
        token: Long-lived access token.
        safety: SafetyController instance for constraint enforcement.
        verify_ssl: Whether to verify the HA TLS certificate. Default False
                    (many local HA instances use self-signed certs).
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        safety: SafetyController,
        verify_ssl: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.safety = safety
        self.verify_ssl = verify_ssl
        self._log = log.bind(component="HAControlAdapter", ha_base_url=self.base_url)

    # ------------------------------------------------------------------
    # Auth header
    # ------------------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        """Return the HTTP headers required for every HA API call."""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Core HTTP wrappers
    # ------------------------------------------------------------------

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Call a Home Assistant service via POST /api/services/{domain}/{service}.

        Args:
            domain: HA domain, e.g. ``climate``, ``switch``, ``humidifier``.
            service: Service name, e.g. ``set_temperature``, ``turn_on``.
            data: Service data payload dict.

        Returns:
            Parsed JSON response from HA (list of affected state objects).

        Raises:
            httpx.HTTPStatusError: If HA returns a non-2xx response.
            httpx.RequestError: On connection / timeout failure.
        """
        url = f"{self.base_url}/api/services/{domain}/{service}"
        self._log.info(
            "ha_service_call",
            domain=domain,
            service=service,
            data=data,
        )
        async with httpx.AsyncClient(
            headers=self._headers, verify=self.verify_ssl, timeout=_DEFAULT_TIMEOUT
        ) as client:
            resp = await client.post(url, json=data)
            resp.raise_for_status()
            try:
                result: dict[str, Any] = resp.json()
            except Exception:
                result = {"raw": resp.text}
            self._log.debug(
                "ha_service_call_success",
                domain=domain,
                service=service,
                status_code=resp.status_code,
            )
            return result

    async def set_entity_state(
        self,
        entity_id: str,
        state: Any,
        attributes: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Set the state (and optional attributes) of an entity via the HA states API.

        POST /api/states/{entity_id}

        This bypasses HA's normal state machine — use ``call_service`` for most
        actuator controls. This is useful for virtual/helper entities.

        Args:
            entity_id: Full entity ID, e.g. ``input_number.ec_setpoint``.
            state: The new state value (will be serialised to string by HA).
            attributes: Optional dict of attributes to merge.

        Returns:
            HA response body as a dict.
        """
        url = f"{self.base_url}/api/states/{entity_id}"
        payload: dict[str, Any] = {"state": str(state)}
        if attributes:
            payload["attributes"] = attributes

        self._log.info(
            "ha_set_entity_state",
            entity_id=entity_id,
            state=state,
            attributes=attributes,
        )
        async with httpx.AsyncClient(
            headers=self._headers, verify=self.verify_ssl, timeout=_DEFAULT_TIMEOUT
        ) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            self._log.debug(
                "ha_set_entity_state_success",
                entity_id=entity_id,
                status_code=resp.status_code,
            )
            return result

    async def get_entity_state(self, entity_id: str) -> Optional[dict[str, Any]]:
        """Fetch the current state of a single entity.

        GET /api/states/{entity_id}

        Returns:
            HA state object as a dict, or None if the entity is not found (404).
        """
        url = f"{self.base_url}/api/states/{entity_id}"
        async with httpx.AsyncClient(
            headers=self._headers, verify=self.verify_ssl, timeout=_DEFAULT_TIMEOUT
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                self._log.warning("ha_entity_not_found", entity_id=entity_id)
                return None
            resp.raise_for_status()
            return resp.json()

    async def ping(self) -> bool:
        """Check HA connectivity by calling GET /api/.

        Returns:
            True if HA responds with HTTP 200; False otherwise.
        """
        url = f"{self.base_url}/api/"
        try:
            async with httpx.AsyncClient(
                headers=self._headers, verify=self.verify_ssl, timeout=httpx.Timeout(5.0)
            ) as client:
                resp = await client.get(url)
                return resp.status_code == 200
        except Exception as exc:
            self._log.warning("ha_ping_failed", error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Climate / environment control
    # ------------------------------------------------------------------

    async def adjust_climate_entity(
        self,
        entity_id: str,
        temperature: Optional[float] = None,
        humidity: Optional[float] = None,
        batch_id: Optional[UUID] = None,
        recommendation_id: Optional[UUID] = None,
    ) -> dict[str, Any]:
        """Adjust a climate entity's temperature and/or humidity setpoint.

        Safety checks are always performed first. If any check fails,
        ``ControlActionBlocked`` is raised and no HTTP call is made.

        Args:
            entity_id: HA entity ID of the climate device, e.g.
                       ``climate.grow_room_ac``.
            temperature: Desired temperature setpoint in °C (optional).
            humidity: Desired humidity setpoint in % (optional).
            batch_id: Associated grow batch UUID for audit logging.
            recommendation_id: Recommendation that triggered this action (if any).

        Returns:
            HA service call response dict.

        Raises:
            ControlActionBlocked: If any safety check fails.
            httpx.HTTPStatusError: If HA returns an error response.
        """
        # --- Safety check ---
        ok, reason = self.safety.check_climate_action(entity_id, temperature, humidity)
        if not ok:
            self._log.warning(
                "climate_action_blocked_by_safety",
                entity_id=entity_id,
                temperature=temperature,
                humidity=humidity,
                reason=reason,
            )
            raise ControlActionBlocked(reason=reason, entity_id=entity_id)

        result: dict[str, Any] = {}

        # --- Temperature control via climate domain ---
        if temperature is not None:
            self._log.info(
                "ha_adjust_temperature",
                entity_id=entity_id,
                temperature=temperature,
                batch_id=str(batch_id) if batch_id else None,
            )
            temp_result = await self.call_service(
                domain="climate",
                service="set_temperature",
                data={"entity_id": entity_id, "temperature": temperature},
            )
            result["temperature_result"] = temp_result
            self.safety.log_action(
                entity_id=entity_id,
                action_type="SET_TEMPERATURE",
                value=temperature,
                batch_id=batch_id,
            )

        # --- Humidity control via humidifier domain (if entity is a humidifier) ---
        if humidity is not None:
            # Humidifier entities use a different domain from climate entities.
            # Try climate first (some thermostats also manage humidity).
            self._log.info(
                "ha_adjust_humidity",
                entity_id=entity_id,
                humidity=humidity,
                batch_id=str(batch_id) if batch_id else None,
            )
            domain = "humidifier" if entity_id.startswith("humidifier.") else "climate"
            service = "set_humidity"
            hum_result = await self.call_service(
                domain=domain,
                service=service,
                data={"entity_id": entity_id, "humidity": humidity},
            )
            result["humidity_result"] = hum_result
            self.safety.log_action(
                entity_id=entity_id,
                action_type="SET_HUMIDITY",
                value=humidity,
                batch_id=batch_id,
            )

        result.update(
            {
                "entity_id": entity_id,
                "temperature": temperature,
                "humidity": humidity,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "recommendation_id": str(recommendation_id) if recommendation_id else None,
            }
        )
        return result

    # ------------------------------------------------------------------
    # AquaPro nutrient / pH control
    # ------------------------------------------------------------------

    async def adjust_aquapro_setpoint(
        self,
        parameter: str,
        value: float,
        batch_id: Optional[UUID] = None,
        recommendation_id: Optional[UUID] = None,
    ) -> dict[str, Any]:
        """Write a new setpoint to the AquaPro dosing controller via HA.

        AquaPro parameters are controlled through Home Assistant helper entities
        (``input_number.*``) which the AquaPro HA integration reads.

        Args:
            parameter: ``"ec"`` or ``"ph"``.
            value: New setpoint value (mS/cm for EC, pH unit for pH).
            batch_id: Associated grow batch UUID for audit logging.
            recommendation_id: Recommendation that triggered this action (if any).

        Returns:
            HA response dict.

        Raises:
            AdvisoryModeBlock: If the system is in advisory mode.
            ControlActionBlocked: If any hard limit or rate limit check fails.
            ValueError: If ``parameter`` is not ``"ec"`` or ``"ph"``.
        """
        if parameter not in ("ec", "ph"):
            raise ValueError(f"AquaPro parameter must be 'ec' or 'ph', got '{parameter}'.")

        # --- Safety check ---
        ok, reason = self.safety.check_aquapro_action(parameter, value)
        if not ok:
            # Advisory mode generates a specific exception type
            if "ADVISORY MODE" in reason:
                self._log.info(
                    "aquapro_advisory_mode_block",
                    parameter=parameter,
                    value=value,
                )
                raise AdvisoryModeBlock(
                    f"System in advisory mode, cannot actuate AquaPro {parameter} → {value}"
                )
            self._log.warning(
                "aquapro_action_blocked_by_safety",
                parameter=parameter,
                value=value,
                reason=reason,
            )
            raise ControlActionBlocked(reason=reason, entity_id=f"aquapro.{parameter}", value=value)

        # Map parameter to the HA entity IDs used by the AquaPro integration
        entity_map: dict[str, str] = {
            "ec": "input_number.aquapro_ec_setpoint",
            "ph": "input_number.aquapro_ph_setpoint",
        }
        entity_id = entity_map[parameter]

        self._log.info(
            "ha_adjust_aquapro_setpoint",
            parameter=parameter,
            value=value,
            entity_id=entity_id,
            batch_id=str(batch_id) if batch_id else None,
        )

        # Write the new setpoint value to the HA input_number entity
        result = await self.call_service(
            domain="input_number",
            service="set_value",
            data={"entity_id": entity_id, "value": value},
        )

        # Update the safety controller's cached last-known value
        action_type = "SET_EC" if parameter == "ec" else "SET_PH"
        self.safety.log_action(
            entity_id=f"aquapro.{parameter}",
            action_type=action_type,
            value=value,
            batch_id=batch_id,
        )

        self._log.info(
            "aquapro_setpoint_applied",
            parameter=parameter,
            value=value,
            entity_id=entity_id,
        )

        return {
            "parameter": parameter,
            "value": value,
            "entity_id": entity_id,
            "ha_response": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendation_id": str(recommendation_id) if recommendation_id else None,
        }
