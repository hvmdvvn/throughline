"""Smoke tests: app and settings import successfully."""

from fastapi import FastAPI

from throughline.api import app
from throughline.config import Settings, get_settings, settings


def test_app_imports() -> None:
    assert isinstance(app, FastAPI)
    assert app.title == settings.app_name


def test_settings_load() -> None:
    loaded = get_settings()
    assert isinstance(loaded, Settings)
    assert loaded.app_name == "Throughline"
    assert isinstance(settings.debug, bool)
    assert loaded.environment.value == "development"
    assert "postgresql" in loaded.database_url
    assert loaded.redis_url.startswith("redis://")
