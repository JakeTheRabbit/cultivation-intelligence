"""
Application settings loaded from environment variables / .env file.

Usage:
    from src.app.config.settings import get_settings
    settings = get_settings()
    print(settings.DATABASE_URL)
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, List, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All application configuration, sourced from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------------------------------------------------------------------
    # Application
    # ---------------------------------------------------------------------------
    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_RELOAD: bool = False
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str = "change-me-in-production"
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:8080"]

    # ---------------------------------------------------------------------------
    # Database (TimescaleDB via asyncpg)
    # ---------------------------------------------------------------------------
    DATABASE_URL: str = "postgresql+asyncpg://cultivation:cultivation@localhost:5432/cultivation"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20

    # ---------------------------------------------------------------------------
    # Redis
    # ---------------------------------------------------------------------------
    REDIS_URL: str = "redis://localhost:6379/0"

    # ---------------------------------------------------------------------------
    # Home Assistant
    # ---------------------------------------------------------------------------
    HA_BASE_URL: str = "http://homeassistant.local:8123"
    HA_TOKEN: str = ""
    HA_WEBSOCKET_URL: str = "ws://homeassistant.local:8123/api/websocket"
    HA_VERIFY_SSL: bool = True
    HA_POLL_INTERVAL_SECONDS: int = 60

    # ---------------------------------------------------------------------------
    # AquaPro device
    # ---------------------------------------------------------------------------
    AQUAPRO_DEVICE_SERIAL: str = ""
    AQUAPRO_ENTITY_PREFIX: str = "sensor.aquapro"

    # ---------------------------------------------------------------------------
    # Cultivation targets
    # ---------------------------------------------------------------------------
    VPD_TARGET_MIN: float = 0.8   # kPa
    VPD_TARGET_MAX: float = 1.2   # kPa
    EC_TARGET_MIN: float = 1.2    # mS/cm
    EC_TARGET_MAX: float = 2.4    # mS/cm
    PH_TARGET_MIN: float = 5.8
    PH_TARGET_MAX: float = 6.2
    DLI_TARGET: float = 40.0      # mol/m²/day

    # ---------------------------------------------------------------------------
    # Model / inference
    # ---------------------------------------------------------------------------
    MODEL_REGISTRY_PATH: str = "./models"
    MODEL_SERVING_TIMEOUT: float = 5.0   # seconds
    PREDICTION_CONFIDENCE_THRESHOLD: float = 0.65

    # ---------------------------------------------------------------------------
    # Monitoring
    # ---------------------------------------------------------------------------
    ENABLE_METRICS: bool = True
    METRICS_PORT: int = 9090
    DRIFT_ALERT_THRESHOLD: float = 0.15

    # ---------------------------------------------------------------------------
    # Safety
    # ---------------------------------------------------------------------------
    ADVISORY_MODE: bool = True
    MAX_EC_ABSOLUTE: float = 4.0     # mS/cm — hard ceiling regardless of targets
    MAX_TEMP_ABSOLUTE: float = 35.0  # °C
    MIN_TEMP_ABSOLUTE: float = 15.0  # °C
    CONTROL_RATE_LIMIT_SECONDS: int = 300  # minimum seconds between control actions

    # ---------------------------------------------------------------------------
    # Validators
    # ---------------------------------------------------------------------------

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def ensure_asyncpg_driver(cls, v: Any) -> str:
        """Rewrite postgres:// or postgresql:// URLs to use asyncpg driver."""
        url = str(v)
        # Replace plain postgresql:// with postgresql+asyncpg://
        if re.match(r"^postgresql://", url):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        # Replace postgres:// shorthand
        elif re.match(r"^postgres://", url):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        # If already has a driver suffix that is NOT asyncpg, warn but allow
        if "postgresql+" in url and "+asyncpg" not in url:
            raise ValueError(
                f"DATABASE_URL must use the asyncpg driver. Got: {url}. "
                "Expected format: postgresql+asyncpg://user:pass@host/db"
            )
        return url

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> List[str]:
        """Accept either a Python list or a comma-separated string."""
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        raise ValueError(f"CORS_ORIGINS must be a list or comma-separated string, got {type(v)}")

    @model_validator(mode="after")
    def validate_cultivation_targets(self) -> "Settings":
        """Sanity-check target ranges."""
        if self.VPD_TARGET_MIN >= self.VPD_TARGET_MAX:
            raise ValueError("VPD_TARGET_MIN must be less than VPD_TARGET_MAX")
        if self.EC_TARGET_MIN >= self.EC_TARGET_MAX:
            raise ValueError("EC_TARGET_MIN must be less than EC_TARGET_MAX")
        if self.PH_TARGET_MIN >= self.PH_TARGET_MAX:
            raise ValueError("PH_TARGET_MIN must be less than PH_TARGET_MAX")
        if self.MIN_TEMP_ABSOLUTE >= self.MAX_TEMP_ABSOLUTE:
            raise ValueError("MIN_TEMP_ABSOLUTE must be less than MAX_TEMP_ABSOLUTE")
        return self

    # ---------------------------------------------------------------------------
    # Convenience properties
    # ---------------------------------------------------------------------------

    @property
    def is_production(self) -> bool:
        """Return True when running in the production environment."""
        return self.APP_ENV.lower() == "production"

    @property
    def is_development(self) -> bool:
        """Return True when running in development mode."""
        return self.APP_ENV.lower() == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    Uses lru_cache so that the .env file is only parsed once per process.
    Call ``get_settings.cache_clear()`` in tests to force re-evaluation.
    """
    return Settings()
