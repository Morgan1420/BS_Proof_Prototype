"""Gemini-backed vision service for parsing supplement label images."""

from __future__ import annotations

from functools import lru_cache

from google import genai
from google.genai import types

from app.core.config import get_settings
from app.schemas.supplement import SupplementAnalysis

# Kept as a single, strict system prompt per the project's parsing
# requirements: only extract what's actually legible, preserve label
# ordering, and respond with nothing but the JSON payload.
SYSTEM_PROMPT = """\
You are an expert at reading "Supplement Facts" and "Nutrition Facts" \
panels from photos of dietary supplement packaging (bottles, boxes, \
pouches, blister packs).

Carefully examine the ENTIRE image, focusing specifically on the \
Supplement Facts / Nutrition Facts panel.

Extract the following:
- product_name: the product's name as printed on the packaging, if \
visible in the image. Use null if it is not visible.
- serving_size: the serving size exactly as printed (e.g. "1 capsule", \
"2 scoops (10 g)"). Use null if not shown.
- ingredients: every row listed in the Supplement Facts / Nutrition \
Facts panel, in the exact order printed. For each ingredient, extract:
  - name: the ingredient/nutrient name exactly as printed (e.g. \
"Vitamin D3 (Cholecalciferol)", "Creatine Monohydrate").
  - amount: the numeric amount per serving, as a string, exactly as \
printed (e.g. "5000", "1.5", "500-600"). Do not include the unit here.
  - unit: the unit of measurement for the amount (e.g. "mg", "g", \
"mcg", "IU", "%").
  - daily_value: the "% Daily Value" column value if printed \
(e.g. "25%"), otherwise null.

Rules:
- Only extract information that is actually visible and legible in the \
image. Do not guess, estimate, or hallucinate values you cannot read.
- If the image does not contain a legible Supplement Facts / Nutrition \
Facts panel, return an empty ingredients list rather than inventing data.
- Preserve the exact order ingredients appear on the label.
- Respond with ONLY the JSON object matching the required schema — no \
explanations, no markdown formatting, no surrounding commentary.
"""


class VisionServiceError(RuntimeError):
    """Raised when Gemini fails to return a usable label analysis."""


@lru_cache
def _get_client() -> genai.Client:
    """Cached Gemini client, built from backend/.env configuration."""
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key)


def analyze_supplement_label(
    image_bytes: bytes, mime_type: str
) -> SupplementAnalysis:
    """Send a label image to Gemini and parse the response into a
    SupplementAnalysis.

    Args:
        image_bytes: Raw bytes of the uploaded label image.
        mime_type: MIME type of the image (e.g. "image/jpeg").

    Returns:
        A validated SupplementAnalysis.

    Raises:
        VisionServiceError: if the Gemini request fails, or the response
            cannot be parsed against the SupplementAnalysis schema.
    """
    settings = get_settings()
    client = _get_client()
    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=[SYSTEM_PROMPT, image_part],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SupplementAnalysis,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surface as a clean service error
        raise VisionServiceError(f"Gemini request failed: {exc}") from exc

    # The SDK populates `.parsed` with a validated instance of the Pydantic
    # model passed as response_schema when parsing succeeds.
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, SupplementAnalysis):
        return parsed

    # Fall back to manually validating the raw text in case `.parsed` wasn't
    # populated (e.g. SDK version differences).
    raw_text = getattr(response, "text", None)
    if not raw_text:
        raise VisionServiceError("Gemini returned an empty response.")

    try:
        return SupplementAnalysis.model_validate_json(raw_text)
    except Exception as exc:  # noqa: BLE001
        raise VisionServiceError(
            f"Gemini response did not match the expected schema: {exc}"
        ) from exc
