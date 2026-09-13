"""CORS settings for the Next.js web shell (issue #24)."""

from throughline.config import Settings


def test_cors_origins_default_includes_local_web() -> None:
    settings = Settings(cors_origins="http://localhost:3000")
    assert settings.cors_origin_list == ["http://localhost:3000"]


def test_cors_origins_comma_separated() -> None:
    settings = Settings(
        cors_origins="http://localhost:3000, https://app.example.com "
    )
    assert settings.cors_origin_list == [
        "http://localhost:3000",
        "https://app.example.com",
    ]
