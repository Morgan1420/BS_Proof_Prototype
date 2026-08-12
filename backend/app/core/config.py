"""Application configuration, loaded from backend/.env (or real env vars)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> parents[2] == backend/
# Resolved absolutely so settings load correctly regardless of the current
# working directory the server is launched from.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """Runtime configuration for the backend.

    Required keys (see backend/.env):
      - GEMINI_API_KEY: API key for the Google Gemini API.
      - GEMINI_MODEL: Model identifier, e.g. "gemini-2.5-flash".
    """

    gemini_api_key: str
    gemini_model: str = "gemini-2.5-flash"

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Cached Settings singleton so .env is parsed once per process.

    Raises:
        pydantic_core.ValidationError: if GEMINI_API_KEY is missing from
            both the environment and backend/.env.
    """
    return Settings()
