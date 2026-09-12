"""Application settings loaded from environment with safe defaults."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Env-backed settings. Secrets must not be committed; use env vars / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Throughline"
    debug: bool = False


def get_settings() -> Settings:
    """Return a settings instance (defaults load without any env required)."""
    return Settings()


settings = get_settings()
