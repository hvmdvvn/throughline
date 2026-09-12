"""Tests for connectivity helpers and worker wiring."""

from arq.connections import RedisSettings

from throughline.connectivity import host_port_from_url
from throughline.workers.settings import WorkerSettings, ping


def test_host_port_from_database_url() -> None:
    host, port = host_port_from_url(
        "postgresql://throughline:throughline@db:5432/throughline",
        5432,
    )
    assert host == "db"
    assert port == 5432


def test_host_port_from_redis_url() -> None:
    host, port = host_port_from_url("redis://redis:6379/0", 6379)
    assert host == "redis"
    assert port == 6379


def test_worker_settings_wired() -> None:
    assert ping in WorkerSettings.functions
    assert isinstance(WorkerSettings.redis_settings, RedisSettings)
