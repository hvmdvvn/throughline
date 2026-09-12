"""Settings modes and production secret refusal."""

import pytest
from pydantic import ValidationError

from throughline.config import Environment, Settings, get_settings


def test_development_applies_local_defaults() -> None:
    loaded = Settings(
        _env_file=None,
        environment=Environment.DEVELOPMENT,
        database_url="",
        redis_url="",
    )
    assert loaded.environment == Environment.DEVELOPMENT
    assert loaded.database_url.startswith("postgresql://")
    assert loaded.redis_url.startswith("redis://")
    assert not loaded.is_production


def test_development_preserves_explicit_urls() -> None:
    db = "postgresql://user:pass@db:5432/throughline"
    redis = "redis://redis:6379/1"
    loaded = Settings(
        _env_file=None,
        environment=Environment.DEVELOPMENT,
        database_url=db,
        redis_url=redis,
    )
    assert loaded.database_url == db
    assert loaded.redis_url == redis


def test_production_refuses_missing_secrets() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            environment=Environment.PRODUCTION,
            database_url="",
            redis_url="",
        )
    message = str(exc_info.value)
    assert "Refusing to start in production" in message
    assert "DATABASE_URL" in message
    assert "REDIS_URL" in message


def test_production_refuses_when_only_one_secret_set() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            environment=Environment.PRODUCTION,
            database_url="postgresql://u:p@db:5432/throughline",
            redis_url="",
        )
    assert "REDIS_URL" in str(exc_info.value)


def test_production_boots_when_required_secrets_set() -> None:
    loaded = Settings(
        _env_file=None,
        environment=Environment.PRODUCTION,
        database_url="postgresql://u:p@db:5432/throughline",
        redis_url="redis://redis:6379/0",
    )
    assert loaded.is_production
    assert loaded.database_url.startswith("postgresql://")
    assert loaded.redis_url.startswith("redis://")


def test_get_settings_defaults_to_development() -> None:
    loaded = get_settings()
    assert loaded.environment == Environment.DEVELOPMENT
    assert "postgresql" in loaded.database_url
