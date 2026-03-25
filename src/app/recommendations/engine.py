"""
Recommendation engine — orchestrates agronomic checks and produces
prioritised, deduplicated RecommendationResponse objects.

Each _check_* method encodes one agronomic heuristic and returns either a
RecommendationResponse or None. The engine collects all non-None results,
filters out types that already have a PENDING recommendation in the DB,
sorts by priority, persists new rows, and returns the list.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.config.settings import get_settings
from src.app.core.database import Recommendation
from src.app.schemas.prediction import (
    RecommendationActionType,
    RecommendationPriority,
    RecommendationResponse,
    RecommendationStatus,
    RiskFactor,
    RiskScoreResponse,
    SuggestedAction,
    YieldPredictionResponse,
)

log = structlog.get_logger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Stage-aware VPD target ranges (kPa)
# ---------------------------------------------------------------------------
_VPD_TARGETS: dict[str, tuple[float, float]] = {
    "PROPAGATION": (0.4, 0.8),
    "VEG": (0.8, 1.2),
    "EARLY_FLOWER": (1.0, 1.5),
    "MID_FLOWER": (1.2, 1.6),
    "LATE_FLOWER": (1.4, 2.0),
    "FLUSH": (1.0, 1.5),
    "HARVEST": (0.8, 1.4),
    "COMPLETE": (0.8, 1.4),
}

# Expiry windows by priority
_EXPIRY_HOURS: dict[RecommendationPriority, int] = {
    RecommendationPriority.CRITICAL: 2,
    RecommendationPriority.HIGH: 6,
    RecommendationPriority.MEDIUM: 24,
    RecommendationPriority.LOW: 72,
    RecommendationPriority.INFORMATIONAL: 168,
}

# Priority sort order (lower index = higher urgency)
_PRIORITY_ORDER: list[RecommendationPriority] = [
    RecommendationPriority.CRITICAL,
    RecommendationPriority.HIGH,
    RecommendationPriority.MEDIUM,
    RecommendationPriority.LOW,
    RecommendationPriority.INFORMATIONAL,
]


class RecommendationEngine:
    """Generates agronomic recommendations for a batch based on sensor features
    and model outputs.

    Args:
        settings: Application settings instance.
        db: An open async SQLAlchemy session.
    """

    def __init__(self, settings, db: AsyncSession) -> None:
        self.settings = settings
        self.db = db
        self._log = log.bind(component="RecommendationEngine")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate_recommendations(
        self,
        batch_id: UUID,
        risk_score: RiskScoreResponse,
        yield_prediction: YieldPredictionResponse,
        features: dict,
    ) -> list[RecommendationResponse]:
        """Orchestrate all heuristic checks and return a sorted, deduplicated list.

        Steps:
        1. Determine current batch stage from features.
        2. Run each _check_* coroutine.
        3. Load existing PENDING recommendations for this batch.
        4. Deduplicate: skip any type that already has a PENDING entry.
        5. Sort by priority (CRITICAL first).
        6. Persist new recommendations to the DB.
        7. Return the combined list (new + already-pending).
        """
        batch_stage: str = features.get("batch_stage", "VEG")

        self._log.info(
            "generating_recommendations",
            batch_id=str(batch_id),
            stage=batch_stage,
            risk_score=risk_score.risk_score,
        )

        # Run all checks concurrently-ish (sequential is fine; checks are cheap)
        candidates: list[RecommendationResponse] = []
        for check_result in [
            await self._check_vpd(features, batch_stage),
            await self._check_ec_drift(features, batch_stage),
            await self._check_ph_swing(features),
            await self._check_dryback(features),
            await self._check_high_risk(risk_score),
        ]:
            if check_result is not None:
                candidates.append(check_result)

        # Load currently PENDING recommendations for this batch
        pending_result = await self.db.execute(
            select(Recommendation).where(
                Recommendation.batch_id == batch_id,
                Recommendation.status == "PENDING",
            )
        )
        pending_rows: list[Recommendation] = list(pending_result.scalars().all())
        pending_types: list[str] = [r.recommendation_type for r in pending_rows]

        # Deduplicate: only keep candidates whose type is not already pending
        new_candidates = [
            c
            for c in candidates
            if not self._is_duplicate_pending(c.recommendation_type, pending_types)
        ]

        # Sort by priority
        new_candidates.sort(
            key=lambda r: _PRIORITY_ORDER.index(r.priority)
            if r.priority in _PRIORITY_ORDER
            else len(_PRIORITY_ORDER)
        )

        # Persist new recommendations to DB
        for rec in new_candidates:
            db_rec = Recommendation(
                id=rec.id,
                batch_id=batch_id,
                recommendation_type=rec.recommendation_type,
                priority=rec.priority.value,
                title=rec.title,
                description=rec.description,
                suggested_actions=[a.model_dump() for a in rec.actions],
                status="PENDING",
                expires_at=rec.expires_at,
                created_at=rec.created_at,
            )
            self.db.add(db_rec)

        if new_candidates:
            await self.db.flush()
            self._log.info(
                "recommendations_persisted",
                batch_id=str(batch_id),
                count=len(new_candidates),
            )

        # Build response from already-pending rows (convert ORM → response schema)
        already_pending_responses: list[RecommendationResponse] = []
        for row in pending_rows:
            already_pending_responses.append(
                RecommendationResponse(
                    id=row.id,
                    batch_id=row.batch_id,
                    recommendation_type=row.recommendation_type,
                    priority=RecommendationPriority(row.priority),
                    status=RecommendationStatus(row.status),
                    title=row.title,
                    description=row.description,
                    rationale="Previously generated — still pending operator acknowledgement.",
                    actions=[],
                    created_at=row.created_at,
                    expires_at=row.expires_at,
                )
            )

        all_recs = new_candidates + already_pending_responses
        all_recs.sort(
            key=lambda r: _PRIORITY_ORDER.index(r.priority)
            if r.priority in _PRIORITY_ORDER
            else len(_PRIORITY_ORDER)
        )

        self._log.info(
            "recommendations_complete",
            batch_id=str(batch_id),
            total=len(all_recs),
            new=len(new_candidates),
            pre_existing=len(already_pending_responses),
        )
        return all_recs

    # ------------------------------------------------------------------
    # Individual agronomic checks
    # ------------------------------------------------------------------

    async def _check_vpd(
        self, features: dict, batch_stage: str
    ) -> Optional[RecommendationResponse]:
        """Check whether the canopy VPD is within the stage-appropriate target range.

        Raises a recommendation only when the deviation has persisted for
        more than 30 minutes (exceedance_minutes feature).
        """
        current_vpd: Optional[float] = features.get("vpd_mean_1h")
        if current_vpd is None:
            return None

        target_min, target_max = _VPD_TARGETS.get(batch_stage, (0.8, 1.2))

        minutes_below: float = features.get("vpd_exceedance_minutes_below", 0.0) or 0.0
        minutes_above: float = features.get("vpd_exceedance_minutes_above", 0.0) or 0.0

        # Only act when exceedance has persisted > 30 minutes
        if current_vpd < target_min and minutes_below >= 30:
            current_temp: float = features.get("temperature_mean_1h", 24.0) or 24.0
            current_rh: float = features.get("humidity_mean_1h", 60.0) or 60.0
            action = self._calculate_suggested_vpd_action(
                current_vpd, target_min, target_max, current_temp, current_rh
            )
            deviation = target_min - current_vpd
            priority = (
                RecommendationPriority.HIGH
                if deviation > 0.3
                else RecommendationPriority.MEDIUM
            )
            return self._build_recommendation(
                rec_type="VPD_TOO_LOW",
                priority=priority,
                title="VPD Below Target Range — Humidity Reduction Needed",
                description=(
                    f"Canopy VPD is {current_vpd:.2f} kPa (target {target_min}–{target_max} kPa "
                    f"for {batch_stage}). VPD has been below target for {minutes_below:.0f} minutes. "
                    "Low VPD increases disease pressure and slows transpiration."
                ),
                rationale=(
                    f"Stage '{batch_stage}' requires VPD {target_min}–{target_max} kPa. "
                    f"Current VPD {current_vpd:.2f} kPa is {deviation:.2f} kPa below minimum. "
                    "Prolonged low VPD creates conditions for powdery mildew and botrytis."
                ),
                actions=[action],
            )

        if current_vpd > target_max and minutes_above >= 30:
            current_temp = features.get("temperature_mean_1h", 24.0) or 24.0
            current_rh = features.get("humidity_mean_1h", 60.0) or 60.0
            action = self._calculate_suggested_vpd_action(
                current_vpd, target_min, target_max, current_temp, current_rh
            )
            deviation = current_vpd - target_max
            priority = (
                RecommendationPriority.HIGH
                if deviation > 0.3
                else RecommendationPriority.MEDIUM
            )
            return self._build_recommendation(
                rec_type="VPD_TOO_HIGH",
                priority=priority,
                title="VPD Above Target Range — Humidity Increase Needed",
                description=(
                    f"Canopy VPD is {current_vpd:.2f} kPa (target {target_min}–{target_max} kPa "
                    f"for {batch_stage}). VPD has been above target for {minutes_above:.0f} minutes. "
                    "High VPD causes excessive water stress and reduced yield potential."
                ),
                rationale=(
                    f"Stage '{batch_stage}' requires VPD {target_min}–{target_max} kPa. "
                    f"Current VPD {current_vpd:.2f} kPa is {deviation:.2f} kPa above maximum. "
                    "Prolonged high VPD triggers stomatal closure and reduces CO₂ uptake."
                ),
                actions=[action],
            )

        return None

    async def _check_ec_drift(
        self, features: dict, batch_stage: str
    ) -> Optional[RecommendationResponse]:
        """Check for EC drift — a steady upward or downward creep in reservoir EC.

        Drift rate threshold: 0.05 mS/cm per hour sustained over 24 h is significant.
        """
        ec_drift_rate: Optional[float] = features.get("ec_drift_rate_24h")
        current_ec: Optional[float] = features.get("ec_mean_1h")

        if ec_drift_rate is None or current_ec is None:
            return None

        drift_threshold = 0.05  # mS/cm per hour
        if abs(ec_drift_rate) <= drift_threshold:
            return None

        # Determine direction and compute a suggested target value
        target_ec_mid = (self.settings.EC_TARGET_MIN + self.settings.EC_TARGET_MAX) / 2.0

        if ec_drift_rate > 0:
            # EC drifting upward — salts accumulating; recommend reducing feed strength
            suggested_ec = max(self.settings.EC_TARGET_MIN, current_ec - 0.2)
            direction = "upward"
            cause = "salt accumulation or reduced uptake"
            corrective = "Reduce nutrient solution EC by flushing or diluting reservoir"
        else:
            # EC drifting downward — plants consuming salts faster than replenishment
            suggested_ec = min(self.settings.EC_TARGET_MAX, current_ec + 0.2)
            direction = "downward"
            cause = "high nutrient uptake or dilution from water-only top-offs"
            corrective = "Increase nutrient concentration or reduce plain-water additions"

        priority = (
            RecommendationPriority.HIGH
            if abs(ec_drift_rate) > 0.15
            else RecommendationPriority.MEDIUM
        )

        action = SuggestedAction(
            action_type=RecommendationActionType.ADJUST_EC,
            description=corrective,
            parameter="ec_setpoint",
            current_value=round(current_ec, 2),
            suggested_value=round(suggested_ec, 2),
            unit="mS/cm",
            expected_impact=(
                f"Stabilise EC near {target_ec_mid:.1f} mS/cm, "
                f"reducing drift rate from {abs(ec_drift_rate):.3f} to < {drift_threshold} mS/cm/h"
            ),
        )

        return self._build_recommendation(
            rec_type="EC_DRIFT",
            priority=priority,
            title=f"EC Drifting {direction.title()} — Nutrient Adjustment Required",
            description=(
                f"Reservoir EC is drifting {direction} at {abs(ec_drift_rate):.3f} mS/cm/h "
                f"(24-h rate). Current mean EC is {current_ec:.2f} mS/cm "
                f"(target {self.settings.EC_TARGET_MIN}–{self.settings.EC_TARGET_MAX} mS/cm). "
                f"Likely cause: {cause}."
            ),
            rationale=(
                f"An EC drift rate above {drift_threshold} mS/cm/h indicates the nutrient solution "
                "is not in equilibrium with plant uptake. Left unaddressed, this leads to either "
                "salt toxicity (upward drift) or nutrient deficiency (downward drift)."
            ),
            actions=[action],
        )

    async def _check_ph_swing(self, features: dict) -> Optional[RecommendationResponse]:
        """Check for large pH swings within a 24-hour window.

        A swing > 0.5 pH units indicates unstable buffering which causes
        nutrient lock-out cycles.
        """
        ph_swing: Optional[float] = features.get("ph_swing_24h")
        current_ph: Optional[float] = features.get("ph_mean_1h")

        if ph_swing is None:
            return None

        swing_threshold = 0.5  # pH units
        if ph_swing <= swing_threshold:
            return None

        priority = (
            RecommendationPriority.HIGH if ph_swing > 1.0 else RecommendationPriority.MEDIUM
        )

        action = SuggestedAction(
            action_type=RecommendationActionType.ADJUST_PH,
            description=(
                "Check nutrient solution pH stability: verify buffer capacity, "
                "check for CO₂ injection affecting pH, inspect pH probe calibration, "
                f"and top-off reservoir to dilute concentration if pH is swinging due to volume loss."
            ),
            parameter="ph_stability",
            current_value=round(current_ph, 2) if current_ph is not None else None,
            suggested_value=(self.settings.PH_TARGET_MIN + self.settings.PH_TARGET_MAX) / 2.0,
            unit="pH",
            expected_impact=(
                f"Reduce 24-h pH swing from {ph_swing:.2f} to < {swing_threshold} pH units, "
                "improving nutrient availability and reducing lock-out risk."
            ),
        )

        return self._build_recommendation(
            rec_type="PH_SWING",
            priority=priority,
            title="Large pH Swing Detected — Check Nutrient Solution Stability",
            description=(
                f"pH has swung {ph_swing:.2f} pH units in the last 24 hours "
                f"(threshold: {swing_threshold}). "
                f"Current mean pH: {current_ph:.2f if current_ph else 'N/A'}. "
                "Large pH swings create nutrient lock-out cycles and stress the root zone."
            ),
            rationale=(
                "Stable pH in the 5.8–6.2 range is critical for nutrient availability. "
                f"A {ph_swing:.2f}-unit swing causes periodic unavailability of key macro- "
                "and micro-nutrients, reducing uptake efficiency and potentially causing deficiency symptoms."
            ),
            actions=[action],
        )

    async def _check_dryback(self, features: dict) -> Optional[RecommendationResponse]:
        """Check substrate dryback percentage before lights-on.

        Ideal dryback before irrigation: 8–20%. > 30% indicates the substrate
        is too dry, risking root stress; irrigation frequency should increase.
        """
        dryback_pct: Optional[float] = features.get("substrate_dryback_pct")
        if dryback_pct is None:
            return None

        dryback_threshold = 30.0  # percent
        if dryback_pct <= dryback_threshold:
            return None

        priority = (
            RecommendationPriority.HIGH
            if dryback_pct > 45.0
            else RecommendationPriority.MEDIUM
        )

        action = SuggestedAction(
            action_type=RecommendationActionType.ADJUST_IRRIGATION_FREQUENCY,
            description=(
                f"Increase irrigation frequency or move first irrigation earlier in the light period. "
                f"Current dryback is {dryback_pct:.1f}%; target pre-lights-on dryback is 8–20%."
            ),
            parameter="irrigation_frequency",
            current_value=round(dryback_pct, 1),
            suggested_value=15.0,  # Target midpoint of ideal range
            unit="%_dryback",
            expected_impact=(
                "Reduce pre-lights-on dryback to 8–20%, maintaining root zone moisture "
                "and preventing stress-induced secondary metabolite inhibition."
            ),
        )

        return self._build_recommendation(
            rec_type="EXCESSIVE_DRYBACK",
            priority=priority,
            title="Excessive Substrate Dryback — Increase Irrigation Frequency",
            description=(
                f"Substrate dryback before lights-on is {dryback_pct:.1f}% "
                f"(threshold: {dryback_threshold}%). "
                "Excessive dryback stresses roots and can trigger early senescence in late flower."
            ),
            rationale=(
                "Optimal substrate dryback before the start of the light period is 8–20%. "
                f"At {dryback_pct:.1f}% dryback, roots are experiencing water stress, "
                "which reduces nutrient uptake and can trigger premature ripening."
            ),
            actions=[action],
        )

    async def _check_high_risk(
        self, risk_score: RiskScoreResponse
    ) -> Optional[RecommendationResponse]:
        """Generate a CRITICAL recommendation when risk score exceeds 0.75."""
        if risk_score.risk_score <= 0.75:
            return None

        # Extract top contributing risk factors (sorted by absolute contribution, descending)
        top_factors: list[RiskFactor] = sorted(
            risk_score.factors,
            key=lambda f: abs(f.contribution),
            reverse=True,
        )[:3]

        factor_bullets = "\n".join(
            f"• {f.name}: {f.description} (contribution: {f.contribution:+.2f})"
            for f in top_factors
        ) or "• No specific factors identified."

        action = SuggestedAction(
            action_type=RecommendationActionType.MANUAL_INSPECTION,
            description=(
                "Perform immediate manual inspection of the grow environment. "
                "Check for signs of disease, pest pressure, equipment failure, "
                "or environmental parameter exceedance."
            ),
            parameter="manual_inspection",
            current_value=round(risk_score.risk_score, 3),
            suggested_value=0.5,
            unit="risk_score",
            expected_impact=(
                "Early detection and intervention can prevent crop loss and "
                "bring risk score below critical threshold within 24–48 hours."
            ),
        )

        notify_action = SuggestedAction(
            action_type=RecommendationActionType.NOTIFY_GROWER,
            description=f"Alert lead grower — risk score {risk_score.risk_score:.2f} exceeds CRITICAL threshold (0.75).",
            parameter="notification",
            current_value=round(risk_score.risk_score, 3),
            suggested_value=0.75,
            unit="risk_score",
            expected_impact="Ensure expert review within 1–2 hours.",
        )

        return self._build_recommendation(
            rec_type="HIGH_RISK_ALERT",
            priority=RecommendationPriority.CRITICAL,
            title=f"CRITICAL: Risk Score {risk_score.risk_score:.2f} — Immediate Attention Required",
            description=(
                f"The ML risk model has scored this batch at {risk_score.risk_score:.2f} "
                f"(confidence: {risk_score.confidence:.0%}), which exceeds the CRITICAL threshold "
                f"of 0.75. Risk level: {risk_score.risk_level}.\n\n"
                f"Top contributing factors:\n{factor_bullets}"
            ),
            rationale=(
                f"A risk score of {risk_score.risk_score:.2f} indicates a high probability of "
                "adverse crop outcomes if conditions are not corrected. "
                f"Model version: {risk_score.model_version}. "
                f"Explanation: {risk_score.explanation}"
            ),
            actions=[action, notify_action],
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_duplicate_pending(
        self, recommendation_type: str, pending: list[str]
    ) -> bool:
        """Return True if the same recommendation_type is already in the pending list."""
        return recommendation_type in pending

    def _calculate_suggested_vpd_action(
        self,
        current_vpd: float,
        target_min: float,
        target_max: float,
        current_temp: float,
        current_rh: float,
    ) -> SuggestedAction:
        """Compute a specific humidity adjustment to move VPD into the target range.

        VPD formula:
            SVP(T) = 0.6108 * exp(17.27 * T / (T + 237.3))   [kPa]
            VPD    = SVP * (1 - RH/100)

        Inverse: to achieve target_vpd with the same temperature,
            RH_target = (1 - target_vpd / SVP(T)) * 100

        We aim for the midpoint of the target range.
        """
        target_vpd = (target_min + target_max) / 2.0

        # Saturation vapour pressure at current temperature (kPa)
        svp = 0.6108 * math.exp(17.27 * current_temp / (current_temp + 237.3))

        if svp <= 0:
            # Fallback — should never happen with realistic temperatures
            target_rh = current_rh
        else:
            target_rh = (1.0 - target_vpd / svp) * 100.0
            target_rh = max(20.0, min(95.0, target_rh))

        rh_delta = target_rh - current_rh

        if current_vpd < target_min:
            # VPD too low → air too humid → reduce humidity
            action_desc = (
                f"Reduce relative humidity by {abs(rh_delta):.1f}% "
                f"(from {current_rh:.1f}% to {target_rh:.1f}%) "
                f"to raise VPD from {current_vpd:.2f} to {target_vpd:.2f} kPa. "
                "Increase dehumidifier output or improve air exchange."
            )
            action_type = RecommendationActionType.ADJUST_VPD
        else:
            # VPD too high → air too dry → increase humidity
            action_desc = (
                f"Increase relative humidity by {abs(rh_delta):.1f}% "
                f"(from {current_rh:.1f}% to {target_rh:.1f}%) "
                f"to lower VPD from {current_vpd:.2f} to {target_vpd:.2f} kPa. "
                "Reduce dehumidifier output or activate humidifier."
            )
            action_type = RecommendationActionType.ADJUST_VPD

        return SuggestedAction(
            action_type=action_type,
            description=action_desc,
            parameter="relative_humidity",
            current_value=round(current_rh, 1),
            suggested_value=round(target_rh, 1),
            unit="%RH",
            expected_impact=(
                f"Move VPD from {current_vpd:.2f} kPa to ~{target_vpd:.2f} kPa, "
                f"within the {target_min}–{target_max} kPa target range for this stage."
            ),
        )

    def _build_recommendation(
        self,
        rec_type: str,
        priority: RecommendationPriority,
        title: str,
        description: str,
        rationale: str,
        actions: list[SuggestedAction],
    ) -> RecommendationResponse:
        """Construct a RecommendationResponse with a generated ID and expiry time."""
        now = datetime.now(timezone.utc)
        expiry_hours = _EXPIRY_HOURS.get(priority, 24)
        expires_at = now + timedelta(hours=expiry_hours)

        return RecommendationResponse(
            id=uuid.uuid4(),
            batch_id=uuid.uuid4(),  # Placeholder; overwritten by caller with real batch_id
            recommendation_type=rec_type,
            priority=priority,
            status=RecommendationStatus.PENDING,
            title=title,
            description=description,
            rationale=rationale,
            actions=actions,
            created_at=now,
            expires_at=expires_at,
        )
