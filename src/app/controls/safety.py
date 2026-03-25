"""
Safety controller — hard limits, rate limiting, kill-switch, and advisory-mode
enforcement for all actuator control actions.

All control pathways MUST pass through SafetyController before interacting
with any physical device (Home Assistant, AquaPro, etc.).

Exception hierarchy:
    ControlActionBlocked  — safety check failed; do not actuate
    AdvisoryModeBlock     — system is in advisory-only mode; cannot write to AquaPro
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Hard limits — these cannot be overridden at runtime
# ---------------------------------------------------------------------------
HARD_LIMITS: dict[str, float] = {
    "ec_max_absolute": 3.5,
    "ec_min_absolute": 0.2,
    "ph_max_absolute": 7.0,
    "ph_min_absolute": 5.2,
    "temperature_max_c": 35.0,
    "temperature_min_c": 15.0,
    "humidity_max_pct": 95.0,
    "humidity_min_pct": 20.0,
}

RATE_LIMIT_SECONDS: int = 300          # 5 minutes between actions on the same entity
MAX_EC_ADJUSTMENT_SINGLE: float = 0.2  # max EC change per single action (mS/cm)
MAX_PH_ADJUSTMENT_SINGLE: float = 0.3  # max pH change per single action

# Path used as a global kill switch flag (can also be overridden via env var)
_KILL_SWITCH_FILE: str = os.environ.get(
    "CULTIVATION_KILL_SWITCH_PATH", "/tmp/cultivation_kill_switch"
)
_KILL_SWITCH_ENV_VAR: str = "CULTIVATION_KILL_SWITCH_ACTIVE"


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class ControlActionBlocked(Exception):
    """Raised when a safety check prevents a control action from executing."""

    def __init__(self, reason: str, entity_id: str = "", value: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.entity_id = entity_id
        self.value = value


class AdvisoryModeBlock(Exception):
    """Raised when the system is in advisory mode and a write action is attempted."""

    def __init__(self, message: str = "System is in advisory mode; actuator writes are disabled.") -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# SafetyController
# ---------------------------------------------------------------------------


class SafetyController:
    """Enforces hard safety constraints on all control actions.

    All check methods return ``(True, "OK")`` when the action is safe, or
    ``(False, <reason_string>)`` when it should be blocked.

    The controller also maintains a rolling in-memory action log for
    rate-limiting purposes. This log is not persisted — restarting the process
    resets rate limits, which is acceptable for safety (restart implies
    operator involvement).

    Args:
        advisory_mode: When True, AquaPro write actions are blocked entirely.
        settings: Application settings (used for configured limits).
    """

    def __init__(self, advisory_mode: bool, settings) -> None:
        self.advisory_mode = advisory_mode
        self.settings = settings
        self._action_log: list[dict[str, Any]] = []
        self._last_known_ec: Optional[float] = None
        self._last_known_ph: Optional[float] = None
        self._log = log.bind(component="SafetyController")

    # ------------------------------------------------------------------
    # Climate checks
    # ------------------------------------------------------------------

    def check_climate_action(
        self,
        entity_id: str,
        temperature: Optional[float] = None,
        humidity: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Validate a climate adjustment (temperature or humidity set-point).

        Checks performed (in order):
        1. Global kill switch active?
        2. Temperature within hard limits?
        3. Humidity within hard limits?
        4. Rate limit: same entity acted on within RATE_LIMIT_SECONDS?

        Returns:
            (True, "OK")          — safe to proceed
            (False, reason_str)   — blocked; reason describes the violation
        """
        # 1. Kill switch
        if self.is_global_kill_switch_active():
            reason = "Global kill switch is active — all control actions are blocked."
            self._log.critical("kill_switch_blocked_climate", entity_id=entity_id)
            return False, reason

        # 2. Temperature bounds
        if temperature is not None:
            if temperature > HARD_LIMITS["temperature_max_c"]:
                reason = (
                    f"Temperature set-point {temperature}°C exceeds hard maximum "
                    f"{HARD_LIMITS['temperature_max_c']}°C."
                )
                self._log.warning("temperature_max_exceeded", entity_id=entity_id, value=temperature)
                return False, reason
            if temperature < HARD_LIMITS["temperature_min_c"]:
                reason = (
                    f"Temperature set-point {temperature}°C is below hard minimum "
                    f"{HARD_LIMITS['temperature_min_c']}°C."
                )
                self._log.warning("temperature_min_exceeded", entity_id=entity_id, value=temperature)
                return False, reason

        # 3. Humidity bounds
        if humidity is not None:
            if humidity > HARD_LIMITS["humidity_max_pct"]:
                reason = (
                    f"Humidity set-point {humidity}% exceeds hard maximum "
                    f"{HARD_LIMITS['humidity_max_pct']}%."
                )
                self._log.warning("humidity_max_exceeded", entity_id=entity_id, value=humidity)
                return False, reason
            if humidity < HARD_LIMITS["humidity_min_pct"]:
                reason = (
                    f"Humidity set-point {humidity}% is below hard minimum "
                    f"{HARD_LIMITS['humidity_min_pct']}%."
                )
                self._log.warning("humidity_min_exceeded", entity_id=entity_id, value=humidity)
                return False, reason

        # 4. Rate limit
        if self._is_rate_limited(entity_id):
            reason = (
                f"Rate limit: entity '{entity_id}' was actuated within the last "
                f"{RATE_LIMIT_SECONDS} seconds. Wait before issuing another command."
            )
            self._log.info("rate_limit_blocked", entity_id=entity_id)
            return False, reason

        return True, "OK"

    # ------------------------------------------------------------------
    # AquaPro nutrient / pH checks
    # ------------------------------------------------------------------

    def check_aquapro_action(
        self,
        parameter: str,
        value: float,
        last_known_value: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Validate an AquaPro EC or pH setpoint change.

        Checks performed (in order):
        1. Global kill switch active?
        2. Advisory mode active?
        3. Parameter must be 'ec' or 'ph'.
        4. Value within absolute hard limits.
        5. Single-step adjustment size within allowed maximum.
        6. Rate limit.

        Args:
            parameter: "ec" or "ph"
            value: Proposed new setpoint value.
            last_known_value: If provided, used to check adjustment magnitude.
                              Falls back to internal cache if None.

        Returns:
            (True, "OK")          — safe to proceed
            (False, reason_str)   — blocked
        """
        # 1. Kill switch
        if self.is_global_kill_switch_active():
            reason = "Global kill switch is active — all control actions are blocked."
            self._log.critical("kill_switch_blocked_aquapro", parameter=parameter, value=value)
            return False, reason

        # 2. Advisory mode
        if self.advisory_mode:
            reason = (
                f"System is in ADVISORY MODE. AquaPro {parameter} write (→ {value}) "
                "is not permitted. Switch to CONTROL MODE to enable actuation."
            )
            self._log.info("advisory_mode_blocked_aquapro", parameter=parameter, value=value)
            return False, reason

        # 3. Parameter validation
        if parameter not in ("ec", "ph"):
            return False, f"Unknown AquaPro parameter '{parameter}'. Must be 'ec' or 'ph'."

        # 4. Hard limits
        if parameter == "ec":
            if value > HARD_LIMITS["ec_max_absolute"]:
                return False, (
                    f"EC setpoint {value} mS/cm exceeds hard maximum "
                    f"{HARD_LIMITS['ec_max_absolute']} mS/cm."
                )
            if value < HARD_LIMITS["ec_min_absolute"]:
                return False, (
                    f"EC setpoint {value} mS/cm is below hard minimum "
                    f"{HARD_LIMITS['ec_min_absolute']} mS/cm."
                )

            # 5a. Single-step EC adjustment check
            reference = last_known_value if last_known_value is not None else self._last_known_ec
            if reference is not None:
                delta = abs(value - reference)
                if delta > MAX_EC_ADJUSTMENT_SINGLE:
                    return False, (
                        f"EC adjustment of {delta:.2f} mS/cm (from {reference:.2f} to {value:.2f}) "
                        f"exceeds the maximum single-step change of {MAX_EC_ADJUSTMENT_SINGLE} mS/cm. "
                        "Apply changes incrementally."
                    )

        elif parameter == "ph":
            if value > HARD_LIMITS["ph_max_absolute"]:
                return False, (
                    f"pH setpoint {value} exceeds hard maximum {HARD_LIMITS['ph_max_absolute']}."
                )
            if value < HARD_LIMITS["ph_min_absolute"]:
                return False, (
                    f"pH setpoint {value} is below hard minimum {HARD_LIMITS['ph_min_absolute']}."
                )

            # 5b. Single-step pH adjustment check
            reference = last_known_value if last_known_value is not None else self._last_known_ph
            if reference is not None:
                delta = abs(value - reference)
                if delta > MAX_PH_ADJUSTMENT_SINGLE:
                    return False, (
                        f"pH adjustment of {delta:.2f} (from {reference:.2f} to {value:.2f}) "
                        f"exceeds the maximum single-step change of {MAX_PH_ADJUSTMENT_SINGLE}. "
                        "Apply changes incrementally."
                    )

        # 6. Rate limit (use parameter as pseudo entity_id for AquaPro)
        aquapro_entity = f"aquapro.{parameter}"
        if self._is_rate_limited(aquapro_entity):
            return False, (
                f"Rate limit: AquaPro '{parameter}' was adjusted within the last "
                f"{RATE_LIMIT_SECONDS} seconds."
            )

        return True, "OK"

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def is_global_kill_switch_active(self) -> bool:
        """Return True if the global kill switch is active.

        Checks both a file sentinel and an environment variable so that the
        kill switch can be activated without filesystem access.
        """
        if os.environ.get(_KILL_SWITCH_ENV_VAR, "").lower() in ("1", "true", "yes"):
            return True
        return os.path.exists(_KILL_SWITCH_FILE)

    def emergency_stop(self) -> dict[str, Any]:
        """Activate the global kill switch.

        Creates the sentinel file and sets the environment variable.
        Returns a status dict confirming activation.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            with open(_KILL_SWITCH_FILE, "w") as fh:
                fh.write(f"KILL SWITCH ACTIVATED AT {timestamp}\n")
        except OSError as exc:
            self._log.critical(
                "emergency_stop_file_write_failed",
                error=str(exc),
                path=_KILL_SWITCH_FILE,
            )
            # Still set the env var as a fallback
        os.environ[_KILL_SWITCH_ENV_VAR] = "1"
        self._log.critical(
            "EMERGENCY_STOP_ACTIVATED",
            kill_switch_file=_KILL_SWITCH_FILE,
            timestamp=timestamp,
        )
        return {
            "status": "emergency_stop_activated",
            "timestamp": timestamp,
            "kill_switch_file": _KILL_SWITCH_FILE,
        }

    def clear_emergency_stop(self) -> dict[str, Any]:
        """Deactivate the global kill switch.

        Removes the sentinel file and clears the environment variable.
        Should only be called after a manual safety inspection.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        removed_file = False
        if os.path.exists(_KILL_SWITCH_FILE):
            try:
                os.remove(_KILL_SWITCH_FILE)
                removed_file = True
            except OSError as exc:
                self._log.error(
                    "emergency_stop_clear_file_error",
                    error=str(exc),
                    path=_KILL_SWITCH_FILE,
                )
        os.environ.pop(_KILL_SWITCH_ENV_VAR, None)
        self._log.warning(
            "EMERGENCY_STOP_CLEARED",
            kill_switch_file_removed=removed_file,
            timestamp=timestamp,
        )
        return {
            "status": "emergency_stop_cleared",
            "timestamp": timestamp,
            "file_removed": removed_file,
        }

    # ------------------------------------------------------------------
    # Action logging & rate limiting
    # ------------------------------------------------------------------

    def log_action(
        self,
        entity_id: str,
        action_type: str,
        value: Any,
        batch_id: Optional[UUID] = None,
    ) -> None:
        """Record a control action in the in-memory rolling log.

        Also updates the last-known EC/pH cache so that subsequent adjustment
        magnitude checks are accurate.
        """
        entry: dict[str, Any] = {
            "entity_id": entity_id,
            "action_type": action_type,
            "value": value,
            "timestamp": datetime.now(timezone.utc),
            "batch_id": str(batch_id) if batch_id else None,
        }
        self._action_log.append(entry)

        # Update EC/pH caches for single-step magnitude checks
        if action_type == "SET_EC" and isinstance(value, (int, float)):
            self._last_known_ec = float(value)
        elif action_type == "SET_PH" and isinstance(value, (int, float)):
            self._last_known_ph = float(value)

        # Trim old entries (keep only last 1000 or those within last 24 h)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        self._action_log = [
            e for e in self._action_log if e["timestamp"] >= cutoff
        ][-1000:]

        self._log.debug(
            "action_logged",
            entity_id=entity_id,
            action_type=action_type,
            value=value,
        )

    def _is_rate_limited(self, entity_id: str) -> bool:
        """Return True if the same entity_id was acted on within RATE_LIMIT_SECONDS."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=RATE_LIMIT_SECONDS)
        for entry in reversed(self._action_log):
            if entry["entity_id"] == entity_id and entry["timestamp"] >= cutoff:
                elapsed = (now - entry["timestamp"]).total_seconds()
                self._log.debug(
                    "rate_limit_check_hit",
                    entity_id=entity_id,
                    elapsed_seconds=elapsed,
                    limit_seconds=RATE_LIMIT_SECONDS,
                )
                return True
        return False
