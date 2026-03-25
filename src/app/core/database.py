"""
SQLAlchemy async engine, session factory, ORM models, and TimescaleDB setup.

All database I/O in this application is performed through the async session
returned by :func:`get_db`.  The session is injected via FastAPI's dependency
injection system::

    @router.get("/example")
    async def example(db: AsyncSession = Depends(get_db)):
        result = await db.execute(select(SensorReading).limit(10))
        return result.scalars().all()
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSON, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.app.config.settings import get_settings

settings = get_settings()

# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    pool_pre_ping=True,
    echo=settings.is_development,
    future=True,
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Project-wide SQLAlchemy declarative base."""

    pass


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------


class Batch(Base):
    """Represents a single cultivation grow batch (seed-to-harvest lifecycle)."""

    __tablename__ = "batches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    strain: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    stage: Mapped[str] = mapped_column(
        String(50), nullable=False, default="GERMINATION"
    )  # GERMINATION | VEG | FLOWER | HARVEST | ARCHIVED
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    expected_harvest_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    harvested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    plant_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    grow_medium: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    tent_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    batch_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        "metadata", JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    # Relationships
    sensor_readings: Mapped[List["SensorReading"]] = relationship(
        "SensorReading", back_populates="batch", lazy="dynamic"
    )
    irrigation_events: Mapped[List["IrrigationEvent"]] = relationship(
        "IrrigationEvent", back_populates="batch", lazy="dynamic"
    )
    recommendations: Mapped[List["Recommendation"]] = relationship(
        "Recommendation", back_populates="batch", lazy="dynamic"
    )


# Valid stage transitions: key → set of allowed next stages
VALID_STAGE_TRANSITIONS: Dict[str, set] = {
    "GERMINATION": {"VEG", "ARCHIVED"},
    "VEG": {"FLOWER", "ARCHIVED"},
    "FLOWER": {"HARVEST", "ARCHIVED"},
    "HARVEST": {"ARCHIVED"},
    "ARCHIVED": set(),
}


def is_valid_stage_transition(current: str, next_stage: str) -> bool:
    """Return True if transitioning from *current* to *next_stage* is permitted."""
    return next_stage in VALID_STAGE_TRANSITIONS.get(current.upper(), set())


class SensorReading(Base):
    """Time-series sensor measurement.  This table is converted to a TimescaleDB
    hypertable partitioned on ``time``."""

    __tablename__ = "sensor_readings"
    __table_args__ = (
        Index("ix_sensor_readings_batch_id_time", "batch_id", "time"),
        Index("ix_sensor_readings_sensor_type_time", "sensor_type", "time"),
        {"timescaledb_hypertable": True},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True, default=datetime.utcnow
    )
    batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("batches.id", ondelete="SET NULL"), nullable=True
    )
    sensor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    sensor_type: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # e.g. TEMPERATURE, HUMIDITY, EC, PH, CO2, VPD, PPFD
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    quality_flag: Mapped[str] = mapped_column(
        String(50), nullable=False, default="OK"
    )  # OK | SUSPECT | OUTLIER | MISSING
    source: Mapped[str] = mapped_column(
        String(100), nullable=False, default="home_assistant"
    )
    raw_entity_id: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    # Relationship
    batch: Mapped[Optional[Batch]] = relationship("Batch", back_populates="sensor_readings")


class IrrigationEvent(Base):
    """Records each irrigation/fertigation event."""

    __tablename__ = "irrigation_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    volume_ml: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ec_in: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # mS/cm going in
    ph_in: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ec_runoff: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # mS/cm runoff
    ph_runoff: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    runoff_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    nutrient_recipe: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    triggered_by: Mapped[str] = mapped_column(
        String(100), nullable=False, default="manual"
    )  # manual | scheduled | ai_recommendation
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    batch: Mapped[Batch] = relationship("Batch", back_populates="irrigation_events")


class ControlAction(Base):
    """Audit log of every control action issued by the system or an operator."""

    __tablename__ = "control_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("batches.id", ondelete="SET NULL"), nullable=True
    )
    action_type: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # e.g. SET_LIGHT_SCHEDULE, TRIGGER_IRRIGATION, ADJUST_NUTRIENT_DOSING
    target_entity: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    initiated_by: Mapped[str] = mapped_column(
        String(100), nullable=False, default="system"
    )  # system | operator | ai
    advisory_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    executed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    executed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # SUCCESS | FAILED
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )


class Recommendation(Base):
    """AI-generated recommendation surfaced to the operator dashboard."""

    __tablename__ = "recommendations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    recommendation_type: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # ADJUST_EC | ADJUST_PH | ADJUST_VPD | IRRIGATION | LIGHT | ALERT
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="MEDIUM"
    )  # LOW | MEDIUM | HIGH | CRITICAL
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_actions: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        JSON, nullable=True
    )  # list of {"action": str, "value": Any, "unit": str}
    prediction_ids: Mapped[Optional[List[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )  # references to model prediction run IDs
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="PENDING"
    )  # PENDING | ACKNOWLEDGED | ACCEPTED | REJECTED | EXPIRED
    operator_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    batch: Mapped[Batch] = relationship("Batch", back_populates="recommendations")


# ---------------------------------------------------------------------------
# Dependency injection helper
# ---------------------------------------------------------------------------


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session and closes it afterward.

    Usage::

        @router.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Initialisation helpers
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """Create all tables (if they do not already exist).

    Called once during application startup.  TimescaleDB hypertable setup
    is handled separately by :func:`execute_hypertable_setup`.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def execute_hypertable_setup(async_engine=None) -> None:
    """Enable the TimescaleDB extension and convert ``sensor_readings`` to a
    hypertable partitioned on the ``time`` column.

    This is idempotent — safe to call on every startup.

    Args:
        async_engine: Optionally pass a custom engine; defaults to the module-level
            ``engine`` singleton.
    """
    target_engine = async_engine or engine

    ddl_statements = [
        # Enable TimescaleDB extension
        "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;",
        # Convert sensor_readings to a hypertable (no-op if already converted)
        "SELECT create_hypertable('sensor_readings', 'time', if_not_exists => TRUE);",
    ]

    async with target_engine.begin() as conn:
        for stmt in ddl_statements:
            try:
                await conn.execute(text(stmt))
            except Exception as exc:  # pragma: no cover
                # TimescaleDB might not be available in test environments — log
                # and continue rather than crashing startup.
                import warnings

                warnings.warn(
                    f"TimescaleDB setup statement failed (non-fatal): {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                break
