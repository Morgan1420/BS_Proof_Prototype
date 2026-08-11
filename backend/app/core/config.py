"""Application configuration and environment variable management.

Centralizes all runtime configuration using ``pydantic-settings`` so that
values are loaded from environment variables / a ``.env`` file, validated
once at startup, and typed throughout the codebase. Downstream modules
should depend on :func:`get_settings` rather than reading ``os.environ``
directly.
"""

from functools import lru_cache
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strictly typed application settings sourced from the environment.

    Values are read from a ``.env`` file (see ``backend/.env``), with real
    process environment variables taking precedence over the file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- General ---
    app_name: str = Field(
        default="BS Proof - Supplement Scan API",
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

    # --- Google Gemini (single-step vision scan) ---
    gemini_api_key: Optional[str] = Field(
        default=None,
        description="API key for the Google Gemini API (Free Tier), used to OCR and structure "
        "product label images. Generate one at https://aistudio.google.com/apikey.",
    )
    gemini_model: str = Field(
        default="gemini-2.0-flash",
        description="Gemini model used for the vision scan (env var GEMINI_MODEL). Fully dynamic -- "
        "app.services.gemini_client.generate_content reads this fresh from Settings on every call "
        "and passes it straight to client.models.generate_content, with any leading 'models/' prefix "
        "stripped (so both 'gemini-2.0-flash' and 'models/gemini-2.0-flash' work). There is no "
        "allowlist and no fallback -- changing which model is called is just changing this one "
        "environment variable (see backend/.env's GEMINI_MODEL) and restarting the process; no code "
        "change needed. History of models tried here as free-tier availability/quota shifted: "
        "'gemini-2.5-flash' (404'd, no longer available to new users) -> 'gemini-2.0-flash' (429'd, "
        "free-tier quota set to 0) -> 'gemini-2.5-flash-lite' (worked). Run "
        "backend/scripts/list_gemini_models.py to check what your own API key currently has access to.",
    )

    # --- PubMed literature search (single-ingredient grading only) ---
    pubmed_api_key: Optional[str] = Field(
        default=None,
        description="Optional NCBI E-utilities API key (env var PUBMED_API_KEY). Not required -- "
        "NCBI allows ~3 requests/sec unauthenticated, which is plenty for grading one ingredient at a "
        "time -- but raises the rate limit to ~10/sec if set. Generate one at "
        "https://www.ncbi.nlm.nih.gov/account/settings/. Used only by "
        "app.services.pubmed_client, for POST /api/ingredients/{ingredient_id}/grade.",
    )
    pubmed_max_studies: int = Field(
        default=5,
        ge=0,
        description="Max number of PubMed studies fetched per grading request (env var "
        "PUBMED_MAX_STUDIES). Keeps the literature-search step, and the resulting Gemini prompt, "
        "bounded -- this is a single-ingredient on-demand grade, not a systematic review.",
    )
    pubmed_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        description="Timeout (seconds) for each literature-search provider's HTTP call -- PubMed, Europe "
        "PMC, OpenAlex, and Semantic Scholar all share this one setting (env var PUBMED_TIMEOUT_SECONDS). "
        "A slow/unreachable provider should fail fast rather than hang the grading request indefinitely "
        "-- see app.services.literature_search, which isolates each provider so one timing out doesn't "
        "block the others or fail the whole grade.",
    )

    # --- Multi-source literature aggregation (Europe PMC / OpenAlex / Semantic Scholar; PubMed uses the
    # pubmed_* settings above) -- see app.services.literature_search ---
    literature_max_papers_per_source: int = Field(
        default=25,
        ge=1,
        description="Max results requested from EACH of Europe PMC / OpenAlex / Semantic Scholar per "
        "grading request (env var LITERATURE_MAX_PAPERS_PER_SOURCE). PubMed's own per-source cap is "
        "pubmed_max_studies above. This bounds the pool app.services.literature_search.aggregate_literature "
        "deduplicates and ranks before selecting the top LITERATURE_TOP_PAPERS_LIMIT.",
    )
    literature_top_papers_limit: int = Field(
        default=20,
        ge=1,
        description="How many of the aggregated, deduplicated, ranked papers actually get sent to Gemini "
        "as context (env var LITERATURE_TOP_PAPERS_LIMIT) -- see "
        "app.services.literature_search.select_top_papers's weighted quality score (study type, citation "
        "count, recency, keyword match).",
    )
    semantic_scholar_api_key: Optional[str] = Field(
        default=None,
        description="Optional Semantic Scholar API key (env var SEMANTIC_SCHOLAR_API_KEY). Not required "
        "-- the public Graph API works unauthenticated at a lower rate limit -- but reduces the chance of "
        "429s during a grading run if set. Generate one at "
        "https://www.semanticscholar.org/product/api#api-key. Used only by "
        "app.services.literature_search's Semantic Scholar provider.",
    )


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached ``Settings`` instance.

    Cached via ``lru_cache`` so environment parsing/validation runs once
    per process. Use as a FastAPI dependency, e.g.
    ``settings: Settings = Depends(get_settings)``.
    """
    return Settings()
