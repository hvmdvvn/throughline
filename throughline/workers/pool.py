"""Shared arq Redis pool helpers for API and tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from throughline.config import settings


def redis_settings_from_config() -> RedisSettings:
    """Return arq RedisSettings from project ``REDIS_URL`` (same as Compose/worker)."""
    return RedisSettings.from_dsn(settings.redis_url)


@asynccontextmanager
async def arq_redis_pool() -> AsyncIterator[ArqRedis]:
    """Open a short-lived arq Redis pool and close it on exit."""
    pool = await create_pool(redis_settings_from_config())
    try:
        yield pool
    finally:
        await pool.aclose()
