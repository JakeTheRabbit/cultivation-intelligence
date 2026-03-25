# Controls and Safety Documentation

**Project**: Cultivation Intelligence
**Facility**: Legacy Ag Limited — Indoor Medicinal Cannabis, New Zealand
**Device**: AquaPro Dosing Unit — Serial AQU1AD04A42
**Last Updated**: 2026-03-25
**Status**: ADVISORY MODE ONLY — No automated actuation is currently enabled.

---

## Table of Contents

1. [Design Philosophy](#design-philosophy)
2. [Home Assistant Integration Architecture](#home-assistant-integration-architecture)
3. [AquaPro Dosing Integration](#aquapro-dosing-integration)
4. [Advisory Mode (Current)](#advisory-mode-current)
5. [Bounded Automation Mode (Future)](#bounded-automation-mode-future)
6. [Hard Constraints — Safety Layer](#hard-constraints--safety-layer)
7. [Kill Switches](#kill-switches)
8. [Rollback Logic](#rollback-logic)
9. [Audit Trail](#audit-trail)
10. [Human Override](#human-override)
11. [Feedback Loop Risks](#feedback-loop-risks)
12. [Incident Response](#incident-response)

---

## Design Philosophy

### Advisory-First

The cultivation intelligence system is, and will remain by default, an advisory system. It observes sensor data, computes predictions, and presents recommendations to operators. It does not act.

This is not a technical limitation — the system is architected to support automation. It is a deliberate design choice based on the following reasoning:

1. **Operator trust is the primary metric.** A system that automates control without operator understanding will be switched off the first time it does something unexpected. A system that earns operator trust through consistently correct and explainable recommendations will eventually be granted automation authority. Trust is built incrementally.

2. **Cannabis cultivation at Legacy Ag involves compliance obligations.** Every control action affecting cultivation conditions is potentially subject to regulatory scrutiny. An operator-in-the-loop requirement ensures that a human is accountable for every significant change to a licensed cultivation environment.

3. **Model errors in an advisory system are recoverable.** A bad recommendation that an operator rejects costs nothing. A bad automated action that adjusts EC 0.5 mS/cm upward at 02:00 when the rootzone was already stressed can cause irreversible damage to a batch worth tens of thousands of dollars.

4. **The system is early-stage.** As of 2026, the model has been trained on a limited number of completed batches from one facility. The model's behaviour in edge cases is not fully characterized. Advisory mode provides a natural circuit breaker: operators will reject recommendations that "feel wrong" before the model has enough data to be well-calibrated.

### Automation is Earned, Not Assumed

The path to bounded automation has defined milestones (see Bounded Automation Mode). No automation feature is enabled without:
- A formal safety review
- A defined test period in shadow mode
- Explicit written approval from the facility manager
- Logging of all automated actions at the same audit standard as manual actions

---

## Home Assistant Integration Architecture

### Overview

Home Assistant (HA) is the facility's primary integration layer. All sensors (Zigbee, wired, AquaPro) expose their state via HA entities. The cultivation intelligence system interacts with HA as a client, not as an alternative control system.

### REST API Polling

The system polls the HA REST API for sensor states on a configurable interval (default: 60 seconds):

```python
import httpx

class HomeAssistantRESTClient:
    def __init__(self, base_url: str, access_token: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip('/')
        self.headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        }
        self.timeout = timeout

    async def get_state(self, entity_id: str) -> dict:
        url = f"{self.base_url}/api/states/{entity_id}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self.headers, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()

    async def get_states_bulk(self, entity_ids: list[str]) -> list[dict]:
        # HA doesn't support multi-entity GET natively; use the /api/states endpoint
        # and filter client-side (acceptable for < 100 entities)
        url = f"{self.base_url}/api/states"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self.headers, timeout=self.timeout)
            resp.raise_for_status()
            all_states = resp.json()
        entity_set = set(entity_ids)
        return [s for s in all_states if s['entity_id'] in entity_set]
```

**Polling Limitations:**
- REST polling has up to 60 seconds of latency for state changes
- Missed state changes between polls are not recoverable
- REST polling is the fallback when WebSocket is unavailable

### WebSocket Subscriptions

The preferred real-time mechanism is HA's WebSocket API with `subscribe_events` for `state_changed`:

```python
import aiohttp
import json

class HomeAssistantWebSocketClient:
    def __init__(self, base_url: str, access_token: str):
        self.ws_url = base_url.replace('http', 'ws') + '/api/websocket'
        self.access_token = access_token
        self._message_id = 0
        self._subscriptions: dict[str, list] = {}

    def _next_id(self) -> int:
        self._message_id += 1
        return self._message_id

    async def connect_and_subscribe(self, entity_ids: set[str], callback):
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.ws_url) as ws:
                # Authenticate
                auth_msg = await ws.receive_json()
                assert auth_msg['type'] == 'auth_required'
                await ws.send_json({'type': 'auth', 'access_token': self.access_token})
                auth_result = await ws.receive_json()
                assert auth_result['type'] == 'auth_ok'

                # Subscribe to state_changed events
                sub_id = self._next_id()
                await ws.send_json({
                    'id': sub_id,
                    'type': 'subscribe_events',
                    'event_type': 'state_changed',
                })

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get('type') == 'event':
                            event = data['event']
                            entity_id = event['data']['entity_id']
                            if entity_id in entity_ids:
                                await callback(event['data'])
```

**WebSocket Reliability:**
- HA WebSocket disconnects periodically (HA restart, network blip). The client must implement reconnect with exponential backoff.
- On reconnect, perform a REST poll to catch any state changes missed during the disconnected window.

### Entity Mapping

HA entity IDs are mapped to internal sensor IDs in a configuration file (`config/entity_mapping.yml`):

```yaml
entity_mappings:
  # Room 1 sensors
  - ha_entity_id: sensor.room1_temperature
    internal_sensor_id: zigbee_room1_temp_01
    sensor_type: TEMPERATURE
    unit: "°C"
    room_id: flower_room_1

  - ha_entity_id: sensor.room1_humidity
    internal_sensor_id: zigbee_room1_rh_01
    sensor_type: HUMIDITY
    unit: "%"
    room_id: flower_room_1

  # AquaPro sensors
  - ha_entity_id: sensor.aquapro_aq1ad04a42_ec
    internal_sensor_id: aquapro_aq1ad04a42_ec
    sensor_type: EC
    unit: mS/cm
    device_serial: AQU1AD04A42

  - ha_entity_id: sensor.aquapro_aq1ad04a42_ph
    internal_sensor_id: aquapro_aq1ad04a42_ph
    sensor_type: PH
    unit: pH
    device_serial: AQU1AD04A42
```

### Authentication

Authentication uses a Home Assistant Long-Lived Access Token. This token is:
- Generated in the HA user profile under "Long-Lived Access Tokens"
- Stored as an environment variable `HA_ACCESS_TOKEN` (never in source code)
- Rotated every 90 days as part of the facility security policy
- Scoped to a dedicated HA user account (`cultivation-intelligence`) with minimal privileges

```python
import os

HA_ACCESS_TOKEN = os.environ['HA_ACCESS_TOKEN']
HA_BASE_URL = os.environ['HA_BASE_URL']  # e.g. http://homeassistant.local:8123
```

### Rate Limiting

The HA REST API has no documented rate limit, but the HA instance is running on embedded hardware. The cultivation intelligence system applies self-imposed rate limits:
- Maximum 1 REST poll per entity per 30 seconds
- Maximum 10 write calls per minute across all entities
- Write calls are queued and dispatched with 100ms inter-call spacing

---

## AquaPro Dosing Integration

### Device: AQU1AD04A42

The AquaPro dosing unit (serial AQU1AD04A42) is integrated into Home Assistant via the AquaPro HA integration. It exposes the following entity types.

### Available HA Entities

**Read-Only Sensors (push via WebSocket):**

| Entity ID | Type | Unit | Description |
|-----------|------|------|-------------|
| `sensor.aquapro_aq1ad04a42_ec` | sensor | mS/cm | Current EC measurement at delivery point |
| `sensor.aquapro_aq1ad04a42_ph` | sensor | pH | Current pH measurement at delivery point |
| `sensor.aquapro_aq1ad04a42_flow_rate` | sensor | L/min | Current flow rate |
| `sensor.aquapro_aq1ad04a42_dose_a_volume` | sensor | mL | Volume dispensed from tank A (nutrient A) |
| `sensor.aquapro_aq1ad04a42_dose_b_volume` | sensor | mL | Volume dispensed from tank B (nutrient B) |
| `sensor.aquapro_aq1ad04a42_ph_down_volume` | sensor | mL | Volume dispensed from pH down tank |
| `sensor.aquapro_aq1ad04a42_status` | sensor | enum | Unit status: IDLE, DOSING, ERROR, CALIBRATING |
| `sensor.aquapro_aq1ad04a42_last_dose_time` | sensor | ISO8601 | Timestamp of last completed dose event |

**Writable Controls (require write permission):**

| Entity ID | Type | Write Type | Description |
|-----------|------|-----------|-------------|
| `number.aquapro_aq1ad04a42_ec_setpoint` | number | service call | Target EC setpoint (mS/cm) |
| `number.aquapro_aq1ad04a42_ph_setpoint` | number | service call | Target pH setpoint |
| `switch.aquapro_aq1ad04a42_pump_enable` | switch | service call | Enable/disable dosing pump |
| `button.aquapro_aq1ad04a42_trigger_dose` | button | service call | Trigger a manual dose cycle |

### Read vs Write Capabilities

**Current system (Advisory Mode):**
- All sensor entities are READ (polled and subscribed)
- No write calls are issued to any AquaPro entity
- Recommended setpoint changes are displayed to the operator who then makes the change manually via the HA dashboard or AquaPro panel

**Future system (Bounded Automation):**
- `number.aquapro_aq1ad04a42_ec_setpoint` — WRITE permitted within bounded limits
- `number.aquapro_aq1ad04a42_ph_setpoint` — WRITE permitted within bounded limits
- `switch.aquapro_aq1ad04a42_pump_enable` — WRITE only via explicit operator confirmation
- `button.aquapro_aq1ad04a42_trigger_dose` — WRITE only via explicit operator confirmation

### Dosing Command Schema

When automation is enabled, dosing commands are issued as HA service calls:

```python
async def set_ec_setpoint(
    ha_client: HomeAssistantRESTClient,
    new_ec: float,
    action_id: int,        # control_actions.id for audit trail
    safety_check_passed: bool,
) -> bool:
    """
    Set AquaPro EC setpoint via HA service call.
    Only executes if safety_check_passed is True.
    """
    if not safety_check_passed:
        raise ValueError("Cannot issue write command without safety check clearance.")

    # Hard constraint enforcement (redundant with safety layer — defence in depth)
    if not (0.5 <= new_ec <= 3.5):
        raise ValueError(f"EC setpoint {new_ec} violates hard constraints [0.5, 3.5]")

    payload = {
        'entity_id': 'number.aquapro_aq1ad04a42_ec_setpoint',
        'value': new_ec,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{ha_client.base_url}/api/services/number/set_value",
            headers=ha_client.headers,
            json=payload,
            timeout=10.0,
        )
        resp.raise_for_status()

    # Confirm the value was applied by polling the entity after 5 seconds
    await asyncio.sleep(5)
    state = await ha_client.get_state('number.aquapro_aq1ad04a42_ec_setpoint')
    applied_value = float(state['state'])
    return abs(applied_value - new_ec) < 0.05
```

---

## Advisory Mode (Current)

### How Advisory Mode Works

In advisory mode, the system:

1. Continuously reads sensor data via HA WebSocket and REST polling
2. Computes features every 15 minutes
3. Runs inference on all active model tasks
4. Evaluates recommendations against the rule-based recommendation engine
5. Persists recommendations to the `recommendations` table
6. Exposes recommendations via the operator dashboard API
7. **Does not issue any write calls to HA**

The operator sees a dashboard showing:
- Current sensor readings (live)
- Active recommendations (sorted by priority)
- Explanation of each recommendation's rationale
- Accept / Reject / Defer buttons per recommendation

When an operator accepts a recommendation, the system:
- Updates `recommendations.status = 'ACCEPTED'`
- Records `recommendations.operator_id_reviewed` and `reviewed_at`
- Logs a `control_actions` row with `source = 'SYSTEM_ADVISORY'` and `operator_id` set
- **Does not issue the actuating command** — the operator executes it manually

This creates a clean record: every recommendation outcome (accepted/rejected) is tracked, and the causal chain from recommendation → control action is preserved.

### Logging Acceptance and Rejection

```python
def record_recommendation_decision(
    db: Connection,
    recommendation_id: int,
    operator_id: str,
    decision: str,  # 'ACCEPTED' | 'REJECTED' | 'DEFERRED'
    notes: str | None = None,
) -> None:
    db.execute("""
        UPDATE recommendations
        SET status = %s,
            operator_id_reviewed = %s,
            reviewed_at = now(),
            outcome_notes = %s
        WHERE id = %s
    """, (decision, operator_id, notes, recommendation_id))
```

---

## Bounded Automation Mode (Future)

### Activation Criteria

Bounded automation will not be enabled until ALL of the following are satisfied:

1. **Operator approval**: Facility manager provides written approval stored in the operations log.
2. **Performance threshold**: The yield regression model has demonstrated MAE < 15% across at least 10 complete batch predictions.
3. **Recommendation track record**: Operator acceptance rate > 60% over 60 consecutive days in advisory mode.
4. **Formal safety review**: A documented review of the hard constraints, rollback logic, and kill switch mechanisms. Review must be signed by the data team lead and facility manager.
5. **Shadow mode**: All automation actions must be simulated in shadow mode for 14 days with zero constraint violations before going live.

### Permitted Automated Actions

In bounded automation mode, only the following actions are permitted without explicit per-action operator confirmation:

| Action | Maximum Step | Condition |
|--------|-------------|-----------|
| EC setpoint increase | +0.1 mS/cm | Current EC below setpoint by > 0.2 mS/cm for > 2h |
| EC setpoint decrease | -0.1 mS/cm | Current EC above setpoint by > 0.2 mS/cm for > 2h |
| Climate temperature setpoint | ±0.5°C | VPD outside target by > 0.2 kPa for > 1h |
| Humidity target adjustment | ±2% | VPD outside target by > 0.2 kPa for > 1h |

Maximum 1 automated action per zone per 5-minute window. Maximum 3 automated actions per zone per hour.

### Actions That Are ALWAYS Manual

Regardless of automation mode, the following actions **always require explicit operator confirmation before execution**:

- Lighting schedule changes (photoperiod, intensity)
- EC/pH setpoint changes greater than ±0.3 mS/cm / ±0.2 pH units
- Pump enable/disable (other than routine dosing continuation)
- Stage transition (propagation → veg → flower etc.)
- Harvest decisions
- Nutrient tank refills or recipe changes
- Any action during an active `MANUAL_OVERRIDE` flag
- Any action when `sensor.aquapro_aq1ad04a42_status != 'IDLE'`

---

## Hard Constraints — Safety Layer

The safety layer is a stateless constraint checker applied to every proposed action, regardless of source (operator, SYSTEM_ADVISORY, or SYSTEM_AUTO). It is the last line of defence before any write is issued to HA.

### Absolute Hard Limits

These constraints are hardcoded. They cannot be overridden by model recommendations, operator preference, or any configuration file. A system administrator must deploy a code change to modify them, and any modification requires documented justification.

```python
HARD_CONSTRAINTS = {
    'ec_max_mS_cm': 3.5,
    'ec_min_mS_cm': 0.3,
    'ph_max': 7.0,
    'ph_min': 5.2,
    'temperature_max_c': 35.0,
    'temperature_min_c': 15.0,
    'max_writes_per_zone_per_5min': 1,
    'max_write_rate_global_per_min': 10,
    'min_interval_between_conflicting_actions_min': 15,
}

class SafetyConstraintChecker:
    """
    Stateful constraint checker. Maintains a write history for rate limiting.
    Must be a singleton — one instance per control engine.
    """

    def __init__(self, constraints: dict = HARD_CONSTRAINTS):
        self.constraints = constraints
        self._write_history: list[tuple] = []  # (timestamp, zone_id, entity_id)
        self._override_active: bool = False

    def check(self, proposed_action: dict) -> tuple[bool, str]:
        """
        Returns (is_permitted, reason).
        If not permitted, reason describes the violated constraint.
        """
        # 1. Check manual override flag
        if self._override_active:
            return False, "MANUAL_OVERRIDE flag is active. No automated writes permitted."

        entity_id = proposed_action['entity_id']
        new_value  = proposed_action.get('new_value')
        zone_id    = proposed_action.get('zone_id', 'global')
        action_type = proposed_action['action_type']

        # 2. EC absolute limits
        if action_type == 'EC_ADJUST' and new_value is not None:
            if new_value > self.constraints['ec_max_mS_cm']:
                return False, f"Proposed EC {new_value} exceeds absolute maximum {self.constraints['ec_max_mS_cm']} mS/cm."
            if new_value < self.constraints['ec_min_mS_cm']:
                return False, f"Proposed EC {new_value} below absolute minimum {self.constraints['ec_min_mS_cm']} mS/cm."

        # 3. pH absolute limits
        if action_type == 'PH_ADJUST' and new_value is not None:
            if new_value > self.constraints['ph_max']:
                return False, f"Proposed pH {new_value} exceeds absolute maximum {self.constraints['ph_max']}."
            if new_value < self.constraints['ph_min']:
                return False, f"Proposed pH {new_value} below absolute minimum {self.constraints['ph_min']}."

        # 4. Rate limiting: max 1 write per zone per 5 minutes
        now = datetime.utcnow()
        recent_zone_writes = [
            ts for ts, z, _ in self._write_history
            if z == zone_id and (now - ts).total_seconds() < 300
        ]
        if len(recent_zone_writes) >= self.constraints['max_writes_per_zone_per_5min']:
            return False, f"Rate limit exceeded: {len(recent_zone_writes)} writes to zone {zone_id} in last 5 minutes."

        # 5. Conflicting action check: no conflicting action on same entity within 15 minutes
        recent_entity_writes = [
            ts for ts, _, e in self._write_history
            if e == entity_id and (now - ts).total_seconds() < 900
        ]
        if len(recent_entity_writes) > 0:
            return False, f"Conflicting action: entity {entity_id} was written {len(recent_entity_writes)} time(s) in last 15 minutes."

        return True, "All constraints satisfied."

    def record_write(self, zone_id: str, entity_id: str) -> None:
        now = datetime.utcnow()
        self._write_history.append((now, zone_id, entity_id))
        # Prune history older than 1 hour
        self._write_history = [
            (ts, z, e) for ts, z, e in self._write_history
            if (now - ts).total_seconds() < 3600
        ]

    def set_manual_override(self, active: bool) -> None:
        self._override_active = active
```

### Constraint Monitoring

Constraint violations are logged to `control_actions` with `safety_check_result` populated and the action not executed:

```python
is_permitted, reason = safety_checker.check(proposed_action)

db.execute("""
    INSERT INTO control_actions
        (time, batch_id, action_type, entity_id, old_value, new_value,
         source, recommendation_id, operator_id, safety_checked, safety_check_result)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
""", (
    datetime.utcnow(), batch_id, action_type, entity_id,
    json.dumps(old_value), json.dumps(new_value),
    source, recommendation_id, operator_id,
    True,  # safety_checked = True
    json.dumps({'permitted': is_permitted, 'reason': reason})
))

if not is_permitted:
    logger.warning(f"Safety constraint blocked action: {reason}")
    return  # Do not issue HA write call
```

---

## Kill Switches

Multiple kill switches exist, in order of scope:

### 1. Advisory Mode Flag (ADVISORY_MODE)

Environment variable: `ADVISORY_MODE=true`

When set to `true`, the control engine refuses all write calls unconditionally. This is the system default.

```python
import os

ADVISORY_MODE = os.environ.get('ADVISORY_MODE', 'true').lower() == 'true'

def maybe_execute_action(action: dict) -> None:
    if ADVISORY_MODE:
        logger.info(f"ADVISORY_MODE active — action suppressed: {action}")
        return
    # ... proceed with safety check and execution
```

### 2. Per-Batch Automation Disable

Each batch has an automation disable flag in `batches.metadata`:

```json
{
  "automation_enabled": false,
  "automation_disabled_reason": "Operator intervention in progress",
  "automation_disabled_at": "2026-03-20T14:30:00Z"
}
```

This is set via the operator dashboard with a required reason field.

### 3. Global Automation Disable Endpoint

A POST to `/api/v1/control/emergency-stop` immediately:
- Sets `MANUAL_OVERRIDE = True` on the `SafetyConstraintChecker` singleton
- Logs a `control_actions` event with `action_type = 'EMERGENCY_STOP'`
- Sends a notification to all registered operator contacts
- Persists to a file flag (`/tmp/cultivation_emergency_stop`) that survives process restarts

### 4. Watchdog Process

A separate watchdog process monitors the control engine. If the control engine issues more than `MAX_WRITES_PER_HOUR = 20` write calls in any 60-minute window:
- The watchdog sends a SIGTERM to the control engine
- The watchdog starts the control engine in `ADVISORY_MODE=true`
- An alert is sent to the on-call operator

---

## Rollback Logic

### Rollback Windows

Every automated control action has a rollback plan defined before the action is executed:

```python
def plan_rollback(action: dict, current_state: dict) -> dict:
    """
    Given an action about to be executed, define the rollback action.
    """
    return {
        'entity_id': action['entity_id'],
        'action_type': action['action_type'],
        'new_value': current_state['value'],  # restore to pre-action value
        'rollback_reason': f"Automatic rollback for action_id {action['id']}",
        'rollback_deadline': datetime.utcnow() + timedelta(minutes=15),
    }
```

The rollback action is stored in `control_actions.rollback_at`. A background worker polls for past-due rollbacks every 30 seconds.

### Automatic Rollback Triggers

A rollback is executed early (before `rollback_at`) if:

1. **Sensor anomaly detected post-action**: Within 5 minutes of the action, if any sensor in the same zone transitions to `quality_flag = 'INVALID'`, the rollback fires immediately.
2. **Out-of-range reading post-action**: If EC or pH moves outside safe bounds within 10 minutes of the action, rollback fires.
3. **AquaPro status error**: If `sensor.aquapro_aq1ad04a42_status = 'ERROR'` within 5 minutes of a write, rollback fires.

```python
async def rollback_monitor(db, ha_client, safety_checker):
    """Background task: monitors for rollback triggers."""
    while True:
        await asyncio.sleep(30)

        # Find actions with pending rollbacks
        pending = db.fetchall("""
            SELECT id, batch_id, entity_id, action_type, new_value,
                   rollback_at, time as action_time
            FROM control_actions
            WHERE rollback_at IS NOT NULL
              AND rollback_at > now()
              AND rolled_back_by IS NULL
        """)

        for action in pending:
            should_rollback, reason = await check_rollback_triggers(
                db, ha_client, action
            )
            if should_rollback:
                await execute_rollback(db, ha_client, safety_checker, action, reason)
```

### Manual Rollback

Operators can manually trigger a rollback from the dashboard for any action in the last 60 minutes. Manual rollbacks bypass the 15-minute window but still go through the safety constraint checker (to prevent rollback from violating hard limits — e.g., rolling back an EC setpoint that would push EC below the minimum).

---

## Audit Trail

Every control action, whether executed or blocked, is logged in `control_actions`. The audit trail is write-once (no row is ever deleted or modified after creation).

### Audit Record Schema

```
control_actions row:
  id                   — surrogate key
  time                 — UTC timestamp of the action
  batch_id             — which batch was affected
  action_type          — EC_ADJUST, PH_ADJUST, STAGE_TRANSITION, etc.
  entity_id            — HA entity_id that was (or would have been) written
  old_value            — state before action (JSON)
  new_value            — state after action (JSON)
  source               — OPERATOR | SYSTEM_ADVISORY | SYSTEM_AUTO
  recommendation_id    — nullable FK to recommendations.id
  operator_id          — nullable; present for OPERATOR and SYSTEM_ADVISORY (reviewed by)
  safety_checked       — always true for system-generated actions
  safety_check_result  — JSON: {permitted: bool, reason: str}
  rollback_at          — nullable; set for SYSTEM_AUTO actions
  rolled_back_by       — nullable FK to the rollback action row
  notes                — free text
```

### Regulatory Export

The audit trail is exportable as a signed PDF or CSV for regulatory inspection:

```python
def export_audit_trail(
    db: Connection,
    batch_id: str,
    output_format: str = 'csv'
) -> bytes:
    rows = db.fetchall("""
        SELECT
            ca.time AT TIME ZONE 'Pacific/Auckland' AS nzt_time,
            ca.action_type,
            ca.entity_id,
            ca.old_value,
            ca.new_value,
            ca.source,
            ca.operator_id,
            ca.safety_check_result->>'permitted' AS safety_permitted,
            r.title AS recommendation_title
        FROM control_actions ca
        LEFT JOIN recommendations r ON r.id = ca.recommendation_id
        WHERE ca.batch_id = %s
        ORDER BY ca.time
    """, (batch_id,))

    if output_format == 'csv':
        return to_csv(rows)
    # ... PDF export
```

---

## Human Override

### Operator Dashboard Override

The operator dashboard provides a direct override interface that bypasses the recommendation system and issues actions directly. Override actions are:
- Still subject to hard safety constraints
- Logged in `control_actions` with `source = 'OPERATOR'`
- Required to include a reason (free text field, mandatory)

When an operator overrides a system recommendation:
- The corresponding recommendation is marked `REJECTED` with `outcome_notes` containing the override reason
- The override pattern is flagged for review in the weekly model performance report

### System Learns from Overrides

Override patterns feed the model improvement process:
- High frequency of overrides for a specific recommendation type → investigate model error or feature drift
- Overrides that improve outcomes (as measured by subsequent sensor readings) → potential training signal
- Overrides that worsen outcomes → potential training signal for "what not to do"

```python
def analyse_override_patterns(db: Connection, lookback_days: int = 30) -> pd.DataFrame:
    return db.read_frame("""
        SELECT
            r.recommendation_type,
            r.priority,
            COUNT(*) FILTER (WHERE r.status = 'REJECTED') AS rejections,
            COUNT(*) FILTER (WHERE r.status = 'ACCEPTED') AS acceptances,
            COUNT(*) AS total,
            ROUND(
                COUNT(*) FILTER (WHERE r.status = 'REJECTED')::numeric /
                NULLIF(COUNT(*), 0) * 100, 1
            ) AS rejection_rate_pct
        FROM recommendations r
        WHERE r.created_at >= now() - INTERVAL '%s days'
        GROUP BY r.recommendation_type, r.priority
        ORDER BY rejection_rate_pct DESC
    """, (lookback_days,))
```

---

## Feedback Loop Risks

### Distribution Shift Under Own Policy

When the system begins issuing automated actions (even bounded), it changes the distribution of cultivation conditions. The model was trained on data generated under human control. Under automation, new input distributions may occur that the model has never seen, causing uncertain predictions.

**Mitigation**:
- Monitor PSI on all input features continuously
- PSI > 0.2 on any critical feature triggers immediate revert to advisory mode
- Retrain the model on data collected under automation once sufficient batch count is accumulated

### Reward Hacking

The system optimises for metrics that proxy yield and quality. If the model finds a setpoint pattern that "looks good" on measured features but is not actually beneficial (e.g., specific EC patterns that make the EC sensor happy but don't improve plant health), this is reward hacking.

**Mitigation**:
- Target metric is actual yield at harvest (not sensor readings during cultivation)
- Maintain human oversight: operator reviews every week's trend even in automation mode
- Canary batches: periodically run a batch under full manual control to maintain a distribution-uncontaminated dataset

### Sensor Fault Amplification

An automated system responding to faulty sensor data will issue incorrect actions. A stuck EC sensor reading 1.0 mS/cm when the solution is at 2.4 mS/cm could cause the system to continuously increase EC setpoint, over-dosing nutrients.

**Mitigation**:
- All recommendations and automated actions are gated on `quality_flag` of the triggering sensor being `OK`
- `SUSPECT` readings generate advisory recommendations only (never automated actions)
- `INVALID` readings immediately pause automated actions for the affected zone and alert the operator

---

## Incident Response

### When the Control System Acts Unexpectedly

If an automated action occurs that the operator did not expect or approve:

1. **Immediate**: Activate the emergency stop endpoint (`POST /api/v1/control/emergency-stop`). This disables all automation immediately.

2. **Assess**: Check `control_actions` for the past 2 hours. Identify what was written and why (`recommendation_id`, `safety_check_result`).

3. **Verify physical state**: Check HA dashboard to confirm current actual entity states. If EC or pH is at an unsafe level, correct manually.

4. **Root cause**: Was this an expected automated action that the operator didn't know was enabled? Was it a safety constraint violation that slipped through? Was it triggered by a faulty sensor reading?

5. **Document**: Every incident must be documented in an incident log within 24 hours. Include: timeline, affected entities, root cause, corrective action, process change.

6. **Resume**: Restart in `ADVISORY_MODE=true`. Only re-enable automation after the root cause is understood and corrective measures are in place.

### Escalation Contacts

Defined in `config/contacts.yml` (not stored in this repository):

```yaml
escalation:
  level_1:  # On-call operator (24/7)
    name: "[Facility Operator Name]"
    phone: "[NZ Mobile]"
    notification_methods: [sms, push]

  level_2:  # Data/systems team
    name: "[Data Team Lead]"
    phone: "[NZ Mobile]"
    notification_methods: [email, sms]
    business_hours_only: false

  level_3:  # Facility Manager
    name: "[Facility Manager]"
    notification_methods: [email, phone]
```

Notifications are sent via the notification service (`services/notifications.py`) which wraps SMS (Twilio or similar NZ-compatible provider) and HA push notifications.
