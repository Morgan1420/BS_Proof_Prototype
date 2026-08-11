"""Single-step vision scan service.

Sends a supplement/health product label image to Google Gemini in ONE
call and extracts exactly what's printed on it: brand/product name,
serving size, servings per container, and each ingredient's name, form,
amount/dosage, and % Daily Value. This is now the entire backend
pipeline -- there is no further PubMed retrieval, paper evaluation, or
consensus scoring step (see ``docs/Architecture.md``); a scan's result
is handed straight to ``app.services.storage.ScanStorage`` for local
persistence.

Failure handling: unlike the old multi-phase pipeline, this service does
NOT silently degrade to a fabricated "Unknown Brand" draft record on
failure -- a bad image, network error, or malformed/incomplete Gemini
response raises ``VisionParsingError`` so the API layer can return a
clear error instead of persisting a garbage scan. Never fabricates a
dose/percentage/form that wasn't actually printed on the label; any
field Gemini can't read is left ``None``.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple, Union

from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.schemas.scan import ScannedIngredient, ScannedProductMetadata, ScanResult

logger = logging.getLogger(__name__)

# Image formats accepted by the Gemini API for inline image input.
SUPPORTED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}

ImageInput = Union[str, Path, bytes]

EXTRACTION_SYSTEM_PROMPT = (
    "You are a meticulous supplement label OCR system. Read the product "
    "label image and extract exactly what is printed: brand name, "
    "product name, serving size, servings per container, and every "
    "ingredient listed in the Supplement Facts / Nutrition Facts panel. "
    "For each ingredient extract: its name; its form as printed, if any "
    "(a specific extract name, a chemical form like 'Citrate' or "
    "'Chelate', or a delivery form like 'Capsule'); its dose amount and "
    "unit; and its % Daily Value if a percentage is printed next to it. "
    "Do not guess or invent values that are not visible on the label -- "
    "return null for any field that isn't printed or legible, rather "
    "than fabricating one. For servings_per_container specifically: "
    "return null (not 0, and not a guess) if the label does not state a "
    "single fixed count -- e.g. many non-US labels print a variable "
    "dosing range instead of one servings-per-container figure. Never "
    "return 0 for servings_per_container. Respond only with JSON "
    "matching the provided response schema."
)


class _ExtractedIngredient(BaseModel):
    """Gemini structured-output contract for ONE ingredient line -- label fields only.

    Deliberately a separate, narrower model from ``app.schemas.scan.ScannedIngredient``
    (rather than reusing it directly), even though the two overlap
    heavily: ``ScannedIngredient`` also carries per-ingredient grading
    fields (``grade_status``, ``sifg_grade``, ``raw_consensus``, etc. --
    see ``app.services.grading_service``). If Gemini's ``response_schema``
    here were built from the full ``ScannedIngredient`` model, it would
    ask the vision model to also fill in those grading fields from a
    label photo, which makes no sense and would violate "never fabricate"
    just as badly as guessing a dose. ``VisionParserService.scan_label``
    maps each ``_ExtractedIngredient`` into a full ``ScannedIngredient``
    afterwards, letting that model's own defaults (server-assigned
    ``ingredient_id``, ``grade_status="pending"``) apply.
    """

    name: str = Field(..., description="Ingredient name as printed on the label, e.g. 'Ashwagandha'.")
    form: Optional[str] = Field(
        default=None,
        description="Form as printed, e.g. a specific extract name ('KSM-66 Root Extract'), a chemical "
        "form ('Citrate', 'Chelate'), or a delivery form ('Capsule'). Null if not stated.",
    )
    amount: Optional[float] = Field(default=None, ge=0, description="Dose amount per serving, e.g. 600.")
    unit: Optional[str] = Field(default=None, description="Unit for `amount`, e.g. 'mg', 'mcg', 'IU'.")
    percent_daily_value: Optional[str] = Field(
        default=None,
        description="% Daily Value as printed next to this ingredient, e.g. '150%'. Null if the label "
        "doesn't print one (common for ingredients with no established Daily Value, often shown as a "
        "dagger symbol instead of a percentage).",
    )


class _ExtractedLabelData(BaseModel):
    """Gemini structured-output contract for a single scan.

    Doubles as the ``response_schema`` passed to Gemini and as the
    validation target for whatever JSON it returns. Every field is
    optional except ``ingredients`` (which defaults to empty) since
    ``_ExtractedIngredient``/``ScannedProductMetadata`` are themselves
    fully nullable -- see their docstrings for why (never fabricate label
    data).
    """

    brand_name: Optional[str] = Field(default=None, description="Brand or manufacturer name as printed.")
    product_name: Optional[str] = Field(default=None, description="Product display name as printed.")
    serving_size: Optional[str] = Field(default=None, description="Serving size as printed, e.g. '2 capsules'.")
    servings_per_container: Optional[int] = Field(
        default=None,
        description="Servings per container as printed. Null if not legible or not a single fixed "
        "count -- never 0.",
    )
    ingredients: List[_ExtractedIngredient] = Field(
        default_factory=list,
        description="Every ingredient line from the Supplement/Nutrition Facts panel.",
    )


class VisionParsingError(Exception):
    """Raised for every expected failure mode: missing/invalid API key, a
    bad image input, or a failed/malformed Gemini response.

    Unlike the old pipeline's ``VisionParsingError``, this is NOT caught
    and converted to a fallback payload internally -- it propagates to
    the API layer (see ``app/api/routes.py``), which maps it to a clean
    HTTP error instead of silently persisting a fabricated draft scan.
    """


class VisionParserService:
    """Extracts a ``ScanResult`` from a label image via a single Gemini call."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
    ) -> None:
        """Configure the service.

        Args:
            settings: Injected ``Settings``; defaults to ``get_settings()``.

        Raises:
            VisionParsingError: If ``GEMINI_API_KEY`` is missing, blank, or
                whitespace-only.

        Note: there is no ``model`` argument here -- which Gemini model
        gets called is read from ``self._settings.gemini_model`` (env var
        ``GEMINI_MODEL``) at call time by ``gemini_client.generate_content``,
        not configured per-instance. See that module's docstring.
        """
        self._settings = settings or get_settings()
        self._api_key = self._validate_api_key(self._settings.gemini_api_key)

    @staticmethod
    def _validate_api_key(api_key: Optional[str]) -> str:
        """Ensure the Gemini API key is present and non-empty.

        Checked explicitly (rather than relying on the SDK to fail) so a
        missing/blank ``GEMINI_API_KEY`` surfaces immediately as a clear
        ``VisionParsingError`` at construction time, instead of as an
        opaque auth error the first time the client is used.
        """
        if not api_key or not api_key.strip():
            raise VisionParsingError("GEMINI_API_KEY is missing or invalid.")
        return api_key.strip()

    # -- Public API -------------------------------------------------------

    async def scan_label(self, image: ImageInput) -> ScanResult:
        """Send one Gemini vision call and return the extracted ``ScanResult``.

        Args:
            image: A filesystem path (``str``/``Path``) or raw image bytes.

        Raises:
            VisionParsingError: For a bad image input, a Gemini/network
                failure, or a response that doesn't match the expected
                schema. This is the ONLY error type this method raises --
                callers can catch it alone.
        """
        image_bytes, media_type = self._load_image(image)

        try:
            raw_output = await self._call_vision_llm(image_bytes, media_type)
            extracted = _ExtractedLabelData.model_validate(raw_output)
        except VisionParsingError:
            raise  # already a clean, specific error (see _call_vision_llm) -- pass it through as-is
        except Exception as exc:  # noqa: BLE001 - normalize every other failure to VisionParsingError too
            raise VisionParsingError(f"Vision parsing failed: {type(exc).__name__}: {exc}") from exc

        return ScanResult(
            scan_id=self._generate_scan_id(),
            scanned_at=datetime.now(timezone.utc),
            product=ScannedProductMetadata(
                brand_name=extracted.brand_name,
                product_name=extracted.product_name,
                serving_size=extracted.serving_size,
                servings_per_container=extracted.servings_per_container,
            ),
            # Map Gemini's narrow _ExtractedIngredient (label fields only) into the
            # full ScannedIngredient -- picking up its defaults for the fields Gemini
            # never sees or fills in: a fresh server-assigned ingredient_id and
            # grade_status="pending" (see _ExtractedIngredient's docstring above).
            ingredients=[
                ScannedIngredient(
                    name=item.name,
                    form=item.form,
                    amount=item.amount,
                    unit=item.unit,
                    percent_daily_value=item.percent_daily_value,
                )
                for item in extracted.ingredients
            ],
        )

    # -- Internals ----------------------------------------------------------

    @staticmethod
    def _generate_scan_id() -> str:
        return f"scan_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _load_image(image: ImageInput) -> Tuple[bytes, str]:
        """Load image bytes and resolve a supported media type.

        Raises:
            VisionParsingError: For missing files, empty payloads, or
                unsupported/undetected media types.
        """
        if isinstance(image, (str, Path)):
            path = Path(image)
            if not path.is_file():
                raise VisionParsingError(f"Image path does not exist: {path}")
            media_type, _ = mimetypes.guess_type(path.name)
            image_bytes = path.read_bytes()
        elif isinstance(image, bytes):
            image_bytes = image
            media_type = "image/jpeg"  # best-effort default for raw bytes with no filename
        else:
            raise VisionParsingError(f"Unsupported image input type: {type(image)!r}")

        if not image_bytes:
            raise VisionParsingError("Image payload is empty.")
        if media_type not in SUPPORTED_MEDIA_TYPES:
            raise VisionParsingError(f"Unsupported or undetected media type: {media_type!r}")
        return image_bytes, media_type

    async def _call_vision_llm(self, image_bytes: bytes, media_type: str) -> dict:
        """Call the Gemini API and return the raw JSON-decoded structured output.

        Uses ``response_mime_type="application/json"`` + ``response_schema``
        (Gemini's structured output mode) rather than function/tool
        calling, so the model is constrained to return JSON matching
        ``_ExtractedLabelData`` directly.

        Routed through ``gemini_client.generate_content`` -- a single
        attempt against whatever ``self._settings.gemini_model`` currently
        says (env var ``GEMINI_MODEL``, fully dynamic -- see
        ``app.services.gemini_client`` module docstring), no retries, no
        fallback to another model, and a mandatory 60s pause after the
        call (success or failure) before any subsequent Gemini call
        anywhere in the process is allowed. Raises ``VisionParsingError``
        (wrapping the underlying ``GeminiCallError``) if the single
        attempt fails.
        """
        from google import genai  # local import: optional dependency, only needed here
        from google.genai import types

        from app.services.gemini_client import generate_content

        # Re-validate defensively: a service instance can outlive a settings
        # object that gets mutated, and this is the point that actually
        # builds the Gemini client.
        api_key = self._validate_api_key(self._api_key)
        client = genai.Client(api_key=api_key)

        try:
            response = await generate_content(
                client,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=media_type),
                    "Extract this supplement label.",
                ],
                config=types.GenerateContentConfig(
                    system_instruction=EXTRACTION_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=_ExtractedLabelData,
                ),
                settings=self._settings,
            )
        except Exception as exc:  # noqa: BLE001 - normalize every Gemini-call failure to VisionParsingError
            raise VisionParsingError(f"Gemini call failed: {type(exc).__name__}: {exc}") from exc

        if not response.text:
            raise VisionParsingError("Gemini response did not include any structured output text.")
        return json.loads(response.text)
