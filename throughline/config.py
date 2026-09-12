"""Application settings loaded from environment with mode-aware secret checks.

Modes (documented equivalent of base / development / production):

- **Base:** shared ``Settings`` fields and env loading (this module).
- **development:** local/Compose defaults for ``DATABASE_URL`` / ``REDIS_URL``.
- **production:** refuses to boot when required secrets are unset.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Host-side local defaults (Compose overrides via service env with host ``db`` / ``redis``).
_DEV_DATABASE_URL = "postgresql://throughline:throughline@localhost:5432/throughline"
_DEV_REDIS_URL = "redis://localhost:6379/0"


class Environment(StrEnum):
    """Runtime environment mode."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Env-backed settings. Secrets must not be committed; use env vars / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Mode: development (default) or production.
    environment: Environment = Environment.DEVELOPMENT

    app_name: str = "Throughline"
    debug: bool = False

    # Empty string means unset; development fills documented local defaults.
    database_url: str = ""
    redis_url: str = ""

    # Temporary admin list-API auth until hosted JWT (issue #8). Bearer token.
    # Development default is intentional and documented; override via env in real deploys.
    admin_api_key: str = "dev-admin-api-key"

    # Optional stubs for upcoming foundation work (issues #8+). Not required to boot.
    clerk_secret_key: str | None = Field(default=None)
    workos_api_key: str | None = Field(default=None)
    anthropic_api_key: str | None = Field(default=None)
    openai_api_key: str | None = Field(default=None)

    @model_validator(mode="after")
    def _apply_environment_rules(self) -> Self:
        if self.environment == Environment.PRODUCTION:
            missing = [
                name
                for name, value in (
                    ("DATABASE_URL", self.database_url),
                    ("REDIS_URL", self.redis_url),
                )
                if not value.strip()
            ]
            if missing:
                raise ValueError(
                    "Refusing to start in production: required secrets unset: "
                    + ", ".join(missing)
                    + ". Set them via environment variables (see .env.example)."
                )
            return self

        # development (and any non-production): apply local defaults when unset.
        # Mutate in place — returning model_copy from an after-validator is ignored on __init__.
        if not self.database_url.strip():
            self.database_url = _DEV_DATABASE_URL
        if not self.redis_url.strip():
            self.redis_url = _DEV_REDIS_URL
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION


def get_settings() -> Settings:
    """Return a settings instance loaded from the environment / ``.env``."""
    return Settings()


settings = get_settings()
