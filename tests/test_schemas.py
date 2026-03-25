"""
Tests for Pydantic v2 schema validation in the cultivation-intelligence platform.

Covers:
- SensorReadingCreate: valid readings, future timestamp rejection, range validation
- BatchCreate / BatchStageUpdate: valid creation, one-way stage progression enforcement
- RecommendationAcknowledge: only ACCEPTED/REJECTED statuses allowed
- RiskScoreResponse: risk_level computed_field derivation
- SensorType enum completeness
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# SensorReadingCreate
# ---------------------------------------------------------------------------


class TestSensorReadingCreate:
    """Tests for the sensor ingestion input schema."""

    def test_sensor_reading_valid(self) -> None:
        """A reading with all fields set correctly must pass validation."""
        from src.app.schemas.sensor import SensorReadingCreate, SensorSource, SensorType

        reading = SensorReadingCreate(
            sensor_id="sensor.grow_temp",
            batch_id=uuid4(),
            sensor_type=SensorType.TEMPERATURE,
            value=24.5,
            unit="°C",
            timestamp=datetime.now(timezone.utc),
            source=SensorSource.HA_PUSH,
        )
        assert reading.value == 24.5
        assert reading.sensor_type == SensorType.TEMPERATURE

    def test_sensor_reading_future_timestamp_rejected(self) -> None:
        """A timestamp more than 1 hour in the future must raise ValidationError."""
        from src.app.schemas.sensor import SensorReadingCreate, SensorType

        future_ts = datetime.now(timezone.utc) + timedelta(hours=2)
        with pytest.raises(ValidationError) as exc_info:
            SensorReadingCreate(
                sensor_id="sensor.test",
                batch_id=uuid4(),
                sensor_type=SensorType.TEMPERATURE,
                value=22.0,
                unit="°C",
                timestamp=future_ts,
            )
        errors = exc_info.value.errors()
        assert any("future" in str(e).lower() or "timestamp" in str(e).lower() for e in errors)

    def test_sensor_reading_impossible_temperature_rejected(self) -> None:
        """Temperature of 200°C must be rejected as physically implausible."""
        from src.app.schemas.sensor import SensorReadingCreate, SensorType

        with pytest.raises(ValidationError):
            SensorReadingCreate(
                sensor_id="sensor.temp",
                batch_id=uuid4(),
                sensor_type=SensorType.TEMPERATURE,
                value=200.0,  # Way above 50°C max
                unit="°C",
            )

    def test_sensor_reading_impossible_ph_rejected(self) -> None:
        """pH of 15 must be rejected (valid pH range: 0–14)."""
        from src.app.schemas.sensor import SensorReadingCreate, SensorType

        with pytest.raises(ValidationError):
            SensorReadingCreate(
                sensor_id="sensor.ph",
                batch_id=uuid4(),
                sensor_type=SensorType.PH,
                value=15.0,
                unit="pH",
            )

    def test_sensor_reading_valid_ph_boundary(self) -> None:
        """pH values at the boundary (0.0 and 14.0) must be accepted."""
        from src.app.schemas.sensor import SensorReadingCreate, SensorType

        for ph_val in (0.0, 7.0, 14.0):
            reading = SensorReadingCreate(
                sensor_id="sensor.ph",
                batch_id=uuid4(),
                sensor_type=SensorType.PH,
                value=ph_val,
                unit="pH",
            )
            assert reading.value == ph_val

    def test_sensor_reading_valid_co2(self) -> None:
        """CO2 reading of 1200 ppm must pass validation."""
        from src.app.schemas.sensor import SensorReadingCreate, SensorType

        reading = SensorReadingCreate(
            sensor_id="sensor.co2",
            batch_id=uuid4(),
            sensor_type=SensorType.CO2,
            value=1200.0,
            unit="ppm",
        )
        assert reading.value == 1200.0

    def test_sensor_reading_explicit_none_timestamp_becomes_now(self) -> None:
        """When timestamp=None is passed explicitly, the validator must set it to now (UTC).

        Pydantic v2 with mode='before' fires the validator when the field value
        is explicitly provided (including explicit None), converting None -> now().
        """
        from src.app.schemas.sensor import SensorReadingCreate, SensorType

        before = datetime.now(timezone.utc)
        reading = SensorReadingCreate(
            sensor_id="sensor.temp",
            batch_id=uuid4(),
            sensor_type=SensorType.TEMPERATURE,
            value=23.0,
            unit="°C",
            timestamp=None,   # explicit None triggers validator
        )
        after = datetime.now(timezone.utc)
        assert reading.timestamp is not None, (
            "Explicit timestamp=None must be converted to now() by the validator."
        )
        ts = reading.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        assert before <= ts <= after, (
            f"timestamp {ts} should be within [{before.isoformat()}, {after.isoformat()}]"
        )


# ---------------------------------------------------------------------------
# Batch schemas
# ---------------------------------------------------------------------------


class TestBatchSchemas:
    """Tests for BatchCreate and BatchStageUpdate validation."""

    def test_batch_valid_creation(self) -> None:
        """A fully specified BatchCreate must pass validation."""
        from src.app.schemas.batch import BatchCreate

        batch = BatchCreate(
            batch_name="Alpha Run",
            strain="OG Kush",
            room_id="room_01",
            start_date=date.today(),
            target_yield_g=500.0,
            planned_veg_days=28,
            planned_flower_days=63,
        )
        assert batch.batch_name == "Alpha Run"
        assert batch.planned_veg_days == 28

    def test_batch_valid_stage_transition(self) -> None:
        """PROPAGATION → VEG is a valid one-way progression."""
        from src.app.schemas.batch import GrowStage, VALID_STAGE_TRANSITIONS

        assert GrowStage.VEG in VALID_STAGE_TRANSITIONS[GrowStage.PROPAGATION]

    def test_batch_invalid_stage_transition_harvest_to_veg(self) -> None:
        """HARVEST → VEG must not be in the valid transitions map."""
        from src.app.schemas.batch import GrowStage, VALID_STAGE_TRANSITIONS

        allowed_from_harvest = VALID_STAGE_TRANSITIONS.get(GrowStage.HARVEST, [])
        assert GrowStage.VEG not in allowed_from_harvest, (
            "Backwards stage transition HARVEST → VEG must not be allowed."
        )

    def test_batch_complete_is_terminal(self) -> None:
        """COMPLETE stage must have no allowed transitions."""
        from src.app.schemas.batch import GrowStage, VALID_STAGE_TRANSITIONS

        allowed = VALID_STAGE_TRANSITIONS.get(GrowStage.COMPLETE, [])
        assert len(allowed) == 0, "COMPLETE is a terminal stage — no transitions should be allowed."

    def test_batch_stage_order_is_monotonic(self) -> None:
        """stage_index must be strictly increasing through the grow lifecycle."""
        from src.app.schemas.batch import GrowStage

        lifecycle = [
            GrowStage.PROPAGATION,
            GrowStage.VEG,
            GrowStage.EARLY_FLOWER,
            GrowStage.MID_FLOWER,
            GrowStage.LATE_FLOWER,
            GrowStage.FLUSH,
            GrowStage.HARVEST,
            GrowStage.COMPLETE,
        ]
        indices = [s.stage_index for s in lifecycle]
        assert indices == sorted(indices), "Stage indices must be strictly increasing."

    def test_batch_start_date_far_future_rejected(self) -> None:
        """start_date more than 30 days in the future must be rejected."""
        from src.app.schemas.batch import BatchCreate

        far_future = date.today() + timedelta(days=60)
        with pytest.raises((ValidationError, ValueError)):
            BatchCreate(
                batch_name="Future Batch",
                strain="Sativa X",
                room_id="room_02",
                start_date=far_future,
            )


# ---------------------------------------------------------------------------
# RecommendationAcknowledge
# ---------------------------------------------------------------------------


class TestRecommendationAcknowledge:
    """Tests for the recommendation acknowledgement schema."""

    def test_recommendation_acknowledge_accepted_is_valid(self) -> None:
        """Status ACCEPTED must pass validation."""
        from src.app.schemas.prediction import RecommendationAcknowledge, RecommendationStatus

        ack = RecommendationAcknowledge(
            status=RecommendationStatus.ACCEPTED,
            operator_id="grower_001",
        )
        assert ack.status == RecommendationStatus.ACCEPTED

    def test_recommendation_acknowledge_rejected_is_valid(self) -> None:
        """Status REJECTED must pass validation."""
        from src.app.schemas.prediction import RecommendationAcknowledge, RecommendationStatus

        ack = RecommendationAcknowledge(
            status=RecommendationStatus.REJECTED,
            operator_id="grower_001",
            notes="Already addressed manually.",
        )
        assert ack.status == RecommendationStatus.REJECTED

    def test_recommendation_acknowledge_pending_rejected(self) -> None:
        """PENDING is not a valid acknowledgement status — must raise ValidationError."""
        from src.app.schemas.prediction import RecommendationAcknowledge, RecommendationStatus

        with pytest.raises(ValidationError) as exc_info:
            RecommendationAcknowledge(
                status=RecommendationStatus.PENDING,
                operator_id="grower_001",
            )
        errors = exc_info.value.errors()
        assert len(errors) > 0

    def test_recommendation_acknowledge_expired_rejected(self) -> None:
        """EXPIRED status must also be rejected in acknowledgement."""
        from src.app.schemas.prediction import RecommendationAcknowledge, RecommendationStatus

        with pytest.raises(ValidationError):
            RecommendationAcknowledge(
                status=RecommendationStatus.EXPIRED,
                operator_id="grower_002",
            )


# ---------------------------------------------------------------------------
# RiskScoreResponse — risk_level computed_field
# ---------------------------------------------------------------------------


class TestRiskScoreLevel:
    """Tests for the risk_level computed field derivation."""

    def _make_risk_score(self, score: float):
        from src.app.schemas.prediction import RiskScoreResponse

        return RiskScoreResponse(
            batch_id=uuid4(),
            risk_score=score,
            factors=[],
            confidence=0.85,
            model_version="v1.0.0",
            computed_at=datetime.now(timezone.utc),
            explanation="Test risk score.",
        )

    def test_risk_score_low(self) -> None:
        """risk_score=0.10 must map to 'LOW'."""
        rs = self._make_risk_score(0.10)
        assert rs.risk_level == "LOW"

    def test_risk_score_medium(self) -> None:
        """risk_score=0.40 must map to 'MEDIUM'."""
        rs = self._make_risk_score(0.40)
        assert rs.risk_level == "MEDIUM"

    def test_risk_score_high(self) -> None:
        """risk_score=0.60 must map to 'HIGH'."""
        rs = self._make_risk_score(0.60)
        assert rs.risk_level == "HIGH"

    def test_risk_score_critical(self) -> None:
        """risk_score=0.90 must map to 'CRITICAL'."""
        rs = self._make_risk_score(0.90)
        assert rs.risk_level == "CRITICAL"

    def test_risk_score_boundary_0_25(self) -> None:
        """risk_score=0.25 is the boundary — must map to 'MEDIUM' (>= 0.25)."""
        rs = self._make_risk_score(0.25)
        assert rs.risk_level == "MEDIUM"

    def test_risk_score_boundary_0_75(self) -> None:
        """risk_score=0.75 is the boundary — must map to 'CRITICAL' (>= 0.75)."""
        rs = self._make_risk_score(0.75)
        assert rs.risk_level == "CRITICAL"


# ---------------------------------------------------------------------------
# SensorType enum
# ---------------------------------------------------------------------------


class TestSensorTypeEnum:
    """Verify the SensorType enum includes all expected types."""

    def test_sensor_type_enum_values(self) -> None:
        """All core sensor types used in cultivation must be present."""
        from src.app.schemas.sensor import SensorType

        expected = {
            "TEMPERATURE",
            "HUMIDITY",
            "VPD_CALCULATED",
            "EC",
            "PH",
            "VWC",
            "CO2",
            "PPFD",
            "FLOW_RATE",
            "DISSOLVED_OXYGEN",
            "WEIGHT",
        }
        actual = {member.value for member in SensorType}
        missing = expected - actual
        assert not missing, f"SensorType enum is missing: {missing}"
