"""
Tests for application configuration (Settings class).

Validates:
- Default values are sane for a safety-first cultivation system
- Environment variable overrides are correctly applied
- Validators reject invalid configurations
"""

from __future__ import annotations

import pytest


class TestDefaultSettings:
    """Verify default values are appropriate for the cultivation domain."""

    def test_default_advisory_mode_is_true(self) -> None:
        """ADVISORY_MODE must default to True — no actuator writes without explicit opt-in."""
        from src.app.config.settings import Settings

        s = Settings()
        assert s.ADVISORY_MODE is True, (
            "Default ADVISORY_MODE must be True to prevent accidental actuator writes "
            "in development/test environments."
        )

    def test_safety_limits_are_reasonable(self) -> None:
        """Hard safety ceilings must be within biologically plausible ranges."""
        from src.app.config.settings import Settings

        s = Settings()
        # EC ceiling well below lethal salt stress level (>6 mS/cm kills most cultivars)
        assert s.MAX_EC_ABSOLUTE < 5.0, (
            f"MAX_EC_ABSOLUTE={s.MAX_EC_ABSOLUTE} seems dangerously high. "
            "Cannabis tolerates up to ~3–4 mS/cm; >5 causes severe salt stress."
        )
        assert s.MAX_EC_ABSOLUTE > 0.0, "MAX_EC_ABSOLUTE must be positive."

        # Temperature range must make biological sense
        assert s.MIN_TEMP_ABSOLUTE < s.MAX_TEMP_ABSOLUTE, (
            "MIN_TEMP_ABSOLUTE must be less than MAX_TEMP_ABSOLUTE."
        )
        assert s.MAX_TEMP_ABSOLUTE <= 40.0, (
            f"MAX_TEMP_ABSOLUTE={s.MAX_TEMP_ABSOLUTE}°C is above the thermal damage "
            "threshold for cannabis (~38–40°C)."
        )
        assert s.MIN_TEMP_ABSOLUTE >= 0.0, (
            f"MIN_TEMP_ABSOLUTE={s.MIN_TEMP_ABSOLUTE}°C would freeze root zone."
        )

    def test_vpd_target_range(self) -> None:
        """VPD_TARGET_MIN must be less than VPD_TARGET_MAX, both in kPa range 0–3."""
        from src.app.config.settings import Settings

        s = Settings()
        assert s.VPD_TARGET_MIN < s.VPD_TARGET_MAX, (
            "VPD_TARGET_MIN must be less than VPD_TARGET_MAX."
        )
        assert 0.0 < s.VPD_TARGET_MIN < 3.0, (
            f"VPD_TARGET_MIN={s.VPD_TARGET_MIN} kPa is outside plausible range (0–3 kPa)."
        )
        assert 0.0 < s.VPD_TARGET_MAX < 3.0, (
            f"VPD_TARGET_MAX={s.VPD_TARGET_MAX} kPa is outside plausible range (0–3 kPa)."
        )

    def test_ph_target_range_sensible(self) -> None:
        """pH targets must be in the hydroponic optimum range (5.5–7.0)."""
        from src.app.config.settings import Settings

        s = Settings()
        assert s.PH_TARGET_MIN < s.PH_TARGET_MAX
        assert 5.0 <= s.PH_TARGET_MIN <= 6.5, f"PH_TARGET_MIN={s.PH_TARGET_MIN} outside typical range."
        assert 5.5 <= s.PH_TARGET_MAX <= 7.0, f"PH_TARGET_MAX={s.PH_TARGET_MAX} outside typical range."

    def test_ec_target_range_sensible(self) -> None:
        """EC targets must be within realistic hydroponic nutrient ranges."""
        from src.app.config.settings import Settings

        s = Settings()
        assert s.EC_TARGET_MIN < s.EC_TARGET_MAX
        assert 0.3 <= s.EC_TARGET_MIN <= 3.0, f"EC_TARGET_MIN={s.EC_TARGET_MIN} is unusual."
        assert 0.5 <= s.EC_TARGET_MAX <= 4.0, f"EC_TARGET_MAX={s.EC_TARGET_MAX} is unusual."

    def test_database_url_default_uses_asyncpg(self) -> None:
        """The default DATABASE_URL must use the asyncpg driver."""
        from src.app.config.settings import Settings

        s = Settings()
        assert "asyncpg" in s.DATABASE_URL, (
            f"DATABASE_URL '{s.DATABASE_URL}' must include '+asyncpg' driver."
        )

    def test_control_rate_limit_seconds_positive(self) -> None:
        """Rate limit must be a positive integer."""
        from src.app.config.settings import Settings

        s = Settings()
        assert s.CONTROL_RATE_LIMIT_SECONDS > 0


class TestSettingsFromEnv:
    """Verify that environment variables are correctly picked up by Settings."""

    def test_settings_from_env_advisory_mode(self, monkeypatch) -> None:
        """ADVISORY_MODE env var must override the default."""
        monkeypatch.setenv("ADVISORY_MODE", "false")
        from src.app.config.settings import get_settings
        get_settings.cache_clear()
        s = get_settings()
        assert s.ADVISORY_MODE is False
        get_settings.cache_clear()

    def test_settings_from_env_app_env(self, monkeypatch) -> None:
        """APP_ENV env var must be picked up correctly."""
        monkeypatch.setenv("APP_ENV", "production")
        from src.app.config.settings import get_settings
        get_settings.cache_clear()
        s = get_settings()
        assert s.APP_ENV == "production"
        assert s.is_production is True
        get_settings.cache_clear()

    def test_settings_from_env_log_level(self, monkeypatch) -> None:
        """LOG_LEVEL env var must be applied."""
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        from src.app.config.settings import get_settings
        get_settings.cache_clear()
        s = get_settings()
        assert s.LOG_LEVEL == "DEBUG"
        get_settings.cache_clear()

    def test_settings_from_env_vpd_targets(self, monkeypatch) -> None:
        """Custom VPD targets from env vars must be loaded and validated."""
        monkeypatch.setenv("VPD_TARGET_MIN", "0.9")
        monkeypatch.setenv("VPD_TARGET_MAX", "1.4")
        from src.app.config.settings import get_settings
        get_settings.cache_clear()
        s = get_settings()
        assert s.VPD_TARGET_MIN == pytest.approx(0.9)
        assert s.VPD_TARGET_MAX == pytest.approx(1.4)
        get_settings.cache_clear()

    def test_settings_from_env_cors_origins_json_list(self, monkeypatch) -> None:
        """CORS_ORIGINS can be set as a JSON-encoded list string via env var.

        pydantic-settings v2.x parses List fields from env vars as JSON arrays.
        """
        monkeypatch.setenv(
            "CORS_ORIGINS",
            '["http://localhost:3000","http://app.example.com"]',
        )
        from src.app.config.settings import get_settings
        get_settings.cache_clear()
        s = get_settings()
        assert "http://localhost:3000" in s.CORS_ORIGINS
        assert "http://app.example.com" in s.CORS_ORIGINS
        get_settings.cache_clear()


class TestSettingsValidators:
    """Verify that validators catch invalid configurations."""

    def test_database_url_requires_asyncpg(self, monkeypatch) -> None:
        """DATABASE_URL with a non-asyncpg driver must raise ValueError."""
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@localhost/db")
        from src.app.config.settings import get_settings, Settings
        import pydantic

        get_settings.cache_clear()
        with pytest.raises((ValueError, pydantic.ValidationError)):
            Settings()
        get_settings.cache_clear()

    def test_postgres_url_is_rewritten_to_asyncpg(self, monkeypatch) -> None:
        """Plain postgres:// URLs must be silently rewritten to use asyncpg."""
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost/mydb")
        from src.app.config.settings import get_settings, Settings
        get_settings.cache_clear()
        s = Settings()
        assert "asyncpg" in s.DATABASE_URL
        assert s.DATABASE_URL.startswith("postgresql+asyncpg://")
        get_settings.cache_clear()

    def test_vpd_min_must_be_less_than_max(self, monkeypatch) -> None:
        """VPD_TARGET_MIN >= VPD_TARGET_MAX must raise ValidationError."""
        monkeypatch.setenv("VPD_TARGET_MIN", "1.5")
        monkeypatch.setenv("VPD_TARGET_MAX", "1.0")
        from src.app.config.settings import get_settings, Settings
        import pydantic

        get_settings.cache_clear()
        with pytest.raises((ValueError, pydantic.ValidationError)):
            Settings()
        get_settings.cache_clear()

    def test_temp_min_must_be_less_than_max(self, monkeypatch) -> None:
        """MIN_TEMP_ABSOLUTE >= MAX_TEMP_ABSOLUTE must raise ValidationError."""
        monkeypatch.setenv("MIN_TEMP_ABSOLUTE", "35.0")
        monkeypatch.setenv("MAX_TEMP_ABSOLUTE", "20.0")
        from src.app.config.settings import get_settings, Settings
        import pydantic

        get_settings.cache_clear()
        with pytest.raises((ValueError, pydantic.ValidationError)):
            Settings()
        get_settings.cache_clear()
