"""Gemini-backed service that turns an ingredient name into a handful of
targeted scientific/medical search queries (Phase 2 grading pipeline).

Mirrors app/services/vision.py's Gemini usage pattern (cached client,
structured `response_schema` output, `.parsed` with a raw-text fallback)
but is text-only — no image part — and much simpler prompting.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.core.config import get_settings

SYSTEM_PROMPT = """\
You are a research assistant helping build a search strategy for a \
dietary supplement ingredient's scientific literature.

Given the name of a single ingredient/compound, generate 3 to 5 targeted \
search queries suitable for querying scientific/medical literature \
databases (PubMed, Europe PMC, Semantic Scholar) to find peer-reviewed \
research on it.

Guidelines:
- Each query should be short (a handful of words), not a full sentence.
- Cover a mix of angles: bioavailability/absorption, clinical trials/ \
efficacy, safety/toxicity, and mechanism of action — but only include \
angles that make sense for the specific ingredient given.
- Use the ingredient's actual name in most queries so results stay \
specific to it, not a generic class of compounds.
- Do not include site names, URLs, boolean operators (AND/OR), or \
quotation marks — just plain search terms, as a human would type them \
into a search box.
- Respond with ONLY the JSON object matching the required schema — no \
explanations, no markdown formatting, no surrounding commentary.

Example — given the ingredient "Magnesium Bisglycinate", a good response is:
{"keywords": ["Magnesium Bisglycinate bioavailability", \
"Magnesium Bisglycinate clinical trial", "magnesium absorption mechanism", \
"magnesium glycinate safety", "magnesium supplementation efficacy"]}
"""


class KeywordGenerationError(RuntimeError):
    """Raised when Gemini fails to return a usable keyword list."""


class _KeywordListSchema(BaseModel):
    """Structured output schema handed to Gemini as `response_schema`."""

    keywords: List[str] = Field(
        default_factory=list,
        description="3 to 5 targeted scientific/medical search queries for the ingredient.",
    )


# Hard cap on how many keywords are ever used, regardless of how many
# Gemini returns — keeps the downstream paper-search fan-out (one round
# of API calls per keyword, per source) bounded even if the model ignores
# the "3 to 5" instruction.
MAX_KEYWORDS = 5


@lru_cache
def _get_client() -> genai.Client:
    """Cached Gemini client, built from backend/.env configuration. Same
    cache as app/services/vision.py's — a separate `@lru_cache`-wrapped
    function here means a distinct cache entry, but both ultimately build
    an equivalent client from the same settings, so sharing one function
    across the two services wasn't worth the extra cross-module coupling.
    """
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key)


def _clean_keywords(raw_keywords: List[str]) -> List[str]:
    """Strips whitespace, drops empties/duplicates (case-insensitive),
    and caps the result at MAX_KEYWORDS.
    """
    cleaned: List[str] = []
    seen: set[str] = set()
    for keyword in raw_keywords:
        trimmed = (keyword or "").strip()
        if not trimmed:
            continue
        key = trimmed.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(trimmed)
        if len(cleaned) >= MAX_KEYWORDS:
            break
    return cleaned


def generate_ingredient_keywords(ingredient_name: str) -> List[str]:
    """Asks Gemini for 3-5 targeted scientific/medical search queries for
    `ingredient_name`.

    Args:
        ingredient_name: The canonical ingredient name (e.g.
            "Magnesium Bisglycinate").

    Returns:
        A cleaned, deduplicated list of up to MAX_KEYWORDS query strings.
        Falls back to a single query built from the ingredient name
        itself if Gemini's response is empty after cleaning (e.g. it
        returned only blank strings) — this still lets the paper-search
        step run rather than failing the whole grading request over a
        degenerate-but-technically-successful Gemini response.

    Raises:
        KeywordGenerationError: if the Gemini request itself fails, or
            its response can't be parsed against the expected schema at
            all (as opposed to parsing fine but being empty, handled by
            the fallback above).
    """
    settings = get_settings()
    client = _get_client()

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=[SYSTEM_PROMPT, f"Ingredient: {ingredient_name}"],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_KeywordListSchema,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surface as a clean service error
        raise KeywordGenerationError(f"Gemini request failed: {exc}") from exc

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, _KeywordListSchema):
        raw_text = getattr(response, "text", None)
        if not raw_text:
            raise KeywordGenerationError("Gemini returned an empty response.")
        try:
            parsed = _KeywordListSchema.model_validate_json(raw_text)
        except Exception as exc:  # noqa: BLE001
            raise KeywordGenerationError(
                f"Gemini response did not match the expected schema: {exc}"
            ) from exc

    cleaned = _clean_keywords(parsed.keywords)
    if cleaned:
        return cleaned

    # Degenerate-but-parseable response (e.g. `{"keywords": []}` or all
    # blank strings) — fall back to a single query from the raw name
    # rather than failing the whole grading request.
    return [ingredient_name.strip()] if ingredient_name.strip() else []
