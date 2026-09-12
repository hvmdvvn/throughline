"""TCP reachability checks for Postgres and Redis (dev stack proof)."""

from __future__ import annotations

import socket
from urllib.parse import urlparse

from throughline.config import settings


def host_port_from_url(url: str, default_port: int) -> tuple[str, int]:
    """Extract hostname and port from a database or Redis URL."""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or default_port
    return host, port


def check_tcp(host: str, port: int, timeout: float = 2.0) -> None:
    """Open and close a TCP connection; raises OSError on failure."""
    with socket.create_connection((host, port), timeout=timeout):
        pass


def check_postgres() -> None:
    """Verify the API process can reach Postgres over TCP."""
    host, port = host_port_from_url(settings.database_url, 5432)
    check_tcp(host, port)


def check_redis() -> None:
    """Verify the API process can reach Redis over TCP."""
    host, port = host_port_from_url(settings.redis_url, 6379)
    check_tcp(host, port)


def main() -> None:
    check_postgres()
    check_redis()
    print("ok")


if __name__ == "__main__":
    main()
