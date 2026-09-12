"""Minimal arq worker settings. Real jobs land in later foundation issues."""

from typing import ClassVar

from arq.connections import RedisSettings

from throughline.config import settings


async def ping(ctx: dict) -> str:
    """No-op job proving worker wiring; expand in issue #9."""
    return "pong"


class WorkerSettings:
    """Run with: ``arq throughline.workers.settings.WorkerSettings``."""

    functions: ClassVar[list] = [ping]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
