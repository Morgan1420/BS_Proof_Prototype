"""Application configuration and environment variable management.

Centralizes all runtime configuration (Gemini/NCBI Entrez credentials and
database connection info) using ``pydantic-settings`` so that values are
loaded from environment variables / a ``.env`` file, validated once at
startup, and typed throughout the codebase. Downstream modules should
depend on :func:`get_settings` rather than reading ``os.environ``
directly.
"""

from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strictly typed application settings sourced from the environment.

    Values are read from a ``.env`` file (see ``backend/.env``), with real
    process environment variables taking precedence over the file. A
    missing required field (e.g. ``NCBI_ENTREZ_EMAIL``) raises a
    ``pydantic.ValidationError`` immediately at startup instead of failing
    silently later inside the PubMed retrieval step (Phase 2).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- General ---
    app_name: str = Field(
        default="BS Proof - Supplement Grading API",
        description="Human-readable service name, used in API docs/metadata.",
    )
    environment: str = Field(
        default="development",
        description="Deployment environment: development | staging | production.",
    )
    debug: bool = Field(
        default=False,
        description="Enables verbose logging and FastAPI debug mode.",
    )
    cors_origins: List[str] = Field(
        default_factory=lambda: ["*"],
        description="Allowed CORS origins for the API. Defaults to '*' for early development -- "
        "lock this down to specific origins before production.",
    )

    # --- Google Gemini (Phase 1: Vision OCR / label parsing) ---
    gemini_api_key: Optional[str] = Field(
        default=None,
        description="API key for the Google Gemini API (Free Tier), used by the Vision Parsing "
        "service to OCR and structure product label images. Generate one at "
        "https://aistudio.google.com/apikey.",
    )

    # --- NCBI / PubMed Entrez (Phase 2: Ingredient Grading) ---
    ncbi_entrez_email: str = Field(
        ...,
        description="Contact email required by NCBI Entrez for API usage tracking (Bio.Entrez.email).",
    )
    ncbi_api_key: Optional[str] = Field(
        default=None,
        description="Optional NCBI API key; raises the Entrez rate limit from 3 to 10 requests/second.",
    )

    # --- Database ---
    database_url: str = Field(
        default="sqlite:///./bs_proof.db",
        description="SQLAlchemy-style connection string for the SIFG/product cache database.",
    )

    @field_validator("ncbi_entrez_email")
    @classmethod
    def validate_entrez_email(cls, value: str) -> str:
        """Ensure the Entrez contact email looks like a valid address.

        NCBI requires a reachable contact email on every Entrez request;
        a malformed value would only surface as an opaque HTTP error deep
        inside the Phase 2 PubMed search step.
        """
        if "@" not in value or "." not in value.split("@")[-1]:
            raise ValueError("NCBI_ENTREZ_EMAIL must be a valid email address")
        return value


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached ``Settings`` instance.

    Cached via ``lru_cache`` so environment parsing/validation runs once
    per process. Use as a FastAPI dependency, e.g.
    ``settings: Settings = Depends(get_settings)``.
    """
    return Settings()
