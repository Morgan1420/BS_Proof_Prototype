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
pouches, blister packs). Packaging is often multi-language (e.g. \
English/Spanish/Italian/French printed together) and dense with \
parenthetical percentages, elemental breakdowns, and ratios — your job \
is to pull out CLEAN, STRUCTURED data, not transcribe the label verbatim.

Carefully examine the ENTIRE image, focusing specifically on the \
Supplement Facts / Nutrition Facts panel.

Extract the following:
- product_name: the product's name as printed on the packaging, if \
visible in the image. Use null if it is not visible.
- serving_size: the serving size exactly as printed (e.g. "1 capsule", \
"2 scoops (10 g)"). Use null if not shown.
- ingredients: every row listed in the Supplement Facts / Nutrition \
Facts panel, in the exact order printed. For each ingredient, extract:
  - name: the CANONICAL ingredient/compound name in ENGLISH ONLY — \
and nothing else. This field is used to deduplicate the same compound \
across many different scanned products, so it must be clean and \
consistent. Follow these rules strictly:
    1. Translate to English if the label prints the name in Spanish, \
Italian, French, German, or any other language (e.g. "Bisglicinato de \
magnesio" -> "Magnesium Bisglycinate").
    2. Use the standard/common compound name, not a brand or marketing \
name (e.g. "Vitamin B6", "Pantothenic Acid", "Vitamin C").
    3. STRIP OUT everything that is not the compound name itself: \
percentages, elemental/composition breakdowns, ratios, dosage numbers, \
unit strings, alternate-language repeats of the same name, and any \
parenthetical annotation. Numbers and percentages belong in `amount`, \
`unit`, and `daily_value` — NEVER inside `name`.
    4. If the label repeats the same ingredient in multiple languages \
(e.g. "Bisglicinato de magnesio / Bisglicinato di magnesio / Magnesium \
bisglycinate"), collapse all of them into the single English canonical \
name — do not concatenate the translations together.
  - amount: the numeric amount per serving, as a string, exactly as \
printed (e.g. "5000", "1.5", "500-600"). Do not include the unit here, \
and do not include elemental-breakdown percentages here either (e.g. \
skip the "11.7%" from "(11.7% elemental magnesium)" entirely — that \
detail is dropped, not moved anywhere).
  - unit: the unit of measurement for the amount (e.g. "mg", "g", \
"mcg", "IU"). Do not use "%" here — see daily_value below.
  - daily_value: the "% Daily Value" column value if printed \
(e.g. "25%"), otherwise null. This is specifically the label's %DV \
column — NOT an elemental-composition percentage like "11.7% elemental \
magnesium" (that percentage is unrelated to %DV and must be dropped \
entirely, not placed here).

Worked example — given raw label text:
  "Bisglicinato de magnesio (11,7% de magnesio elemental) / \
Bisglicinato di magnesio / Magnesium bisglycinate — 500mg"
the correct extraction for that row is:
  {"name": "Magnesium Bisglycinate", "amount": "500", "unit": "mg", \
"daily_value": null}
Note everything else — the elemental percentage, the Spanish and \
Italian repeats — is discarded, not appended to `name` or `amount`.

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
