"""Application settings loaded from environment with mode-aware secret checks.

Modes (documented equivalent of base / development / production):

- **Base:** shared ``Settings`` fields and env loading (this module).
- **development:** local/Compose defaults for ``DATABASE_URL`` / ``REDIS_URL``.
- **production:** refuses to boot when required secrets are unset.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator
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

    # Hosted auth: Clerk (issue #8). JWTs verified via JWKS; no custom password auth.
    # CLERK_ISSUER is required for authenticated routes (e.g. https://xxx.clerk.accounts.dev).
    clerk_issuer: str = ""
    # Optional override; default is ``{CLERK_ISSUER}/.well-known/jwks.json``.
    clerk_jwks_url: str = ""
    # Optional audience check; when empty, ``aud`` is not verified (Clerk session tokens).
    clerk_audience: str = ""
    # Static JWKS JSON for tests / offline (skips HTTP). Never put production keys in git.
    clerk_jwks_static_json: str = ""
    # Single-dev-org bootstrap: attach first-login users as admin to this org (UUID).
    # When unset in development, get-or-create org named ``clerk_bootstrap_org_name``.
    clerk_bootstrap_org_id: uuid.UUID | None = None
    clerk_bootstrap_org_name: str = "Dev Org"
    # Backend API key (optional; not used for JWT verification).
    clerk_secret_key: str | None = Field(default=None)

    # Browser CORS origins for the Next.js app (issue #24). Comma-separated.
    # Default allows local ``throughline/web`` (``next dev`` on port 3000).
    cors_origins: str = "http://localhost:3000"

    # Atlassian OAuth 2.0 (3LO) for Jira Cloud (issue #10).
    # Register an app at https://developer.atlassian.com/console/myapps/
    # Callback URL must match ATLASSIAN_REDIRECT_URI exactly, e.g.
    #   http://localhost:8000/connectors/jira/oauth/callback
    atlassian_client_id: str = ""
    atlassian_client_secret: str = ""
    atlassian_redirect_uri: str = ""
    # Space-delimited scopes; offline_access is always ensured by the authorize helper.
    atlassian_oauth_scopes: str = "read:jira-work read:jira-user offline_access"

    # Fernet key for encrypting per-org connector credentials (url-safe base64).
    # Generate:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Development may omit and use a documented insecure default; production must set it.
    credentials_encryption_key: str = ""

    # Next.js web app origin (issue #27). Used for OAuth return redirect and
    # report-ready email links. Empty → OAuth callback returns JSON (API-only).
    # Example: http://localhost:3000
    web_app_url: str = ""

    # Outbound email for diagnostic onboarding (issue #27). When EMAIL_ENABLED
    # is false or SMTP_* is incomplete, delivery is skipped (logged) so local
    # flows can complete without a mail server.
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True

    # Optional stubs for upcoming foundation work. Not required to boot.
    anthropic_api_key: str | None = Field(default=None)
    openai_api_key: str | None = Field(default=None)

    @field_validator("clerk_bootstrap_org_id", mode="before")
    @classmethod
    def _empty_bootstrap_org_id(cls, value: object) -> object:
        if value == "" or value is None:
            return None
        return value

    @model_validator(mode="after")
    def _apply_environment_rules(self) -> Self:
        if self.environment == Environment.PRODUCTION:
            missing = [
                name
                for name, value in (
                    ("DATABASE_URL", self.database_url),
                    ("REDIS_URL", self.redis_url),
                    ("CREDENTIALS_ENCRYPTION_KEY", self.credentials_encryption_key),
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

    @property
    def cors_origin_list(self) -> list[str]:
        """Parsed ``CORS_ORIGINS`` (empty entries dropped)."""
        return [part.strip() for part in self.cors_origins.split(",") if part.strip()]


def get_settings() -> Settings:
    """Return a settings instance loaded from the environment / ``.env``."""
    return Settings()


settings = get_settings()
