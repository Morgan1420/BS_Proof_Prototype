"""Phase 1 Vision Parsing Service.

Sends a supplement/health product label image to Google Gemini (Free
Tier) and uses structured JSON output to extract label data, matching
the "Vision-LLM Parsing -> Extract Raw Metadata & Ingredients" step in
docs/Architecture.md (Phase 1).

Scope: this service only performs OCR-style extraction of what is
printed on the label. It does not perform the Primary Identifier Lookup
(UPC / composite name hash DB match) that follows in the Phase 1 flow
diagram -- that is a separate downstream step. Every payload produced
here is therefore returned with ``match_status=MatchStatus.DRAFT`` and no
``similarity_score``, ready to be promoted to ``MATCHED`` by that lookup
step.

Failure handling: unreadable images, LLM/network errors, and malformed or
incomplete structured output are all caught at the ``parse_label_image``
boundary and logged; per the "Non-Blocking Fallbacks" architectural
decision, they degrade to a minimal, schema-valid draft
``StructuredProductPayload`` rather than raising, so the rest of the
pipeline can keep moving on the OCR data that *was* extracted.

Per CLAUDE.md's "Asynchronous Execution" standard, the LLM call is async
so it can be awaited from a FastAPI async background job / task queue at
the API layer (not implemented in this step).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import List, Optional, Tuple, Union

from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.schemas.product import (
    MatchStatus,
    ProductIngredient,
    ProductMetadata,
    StructuredProductPayload,
)

logger = logging.getLogger(__name__)

# Image formats accepted by the Gemini API for inline image input.
SUPPORTED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}

ImageInput = Union[str, Path, bytes]

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

EXTRACTION_SYSTEM_PROMPT = (
    "You are a meticulous supplement label OCR system. Read the product "
    "label image and extract exactly what is printed: brand, product "
    "name, serving size, servings per container, certifications, and the "
    "full Supplement/Nutrition Facts ingredient list with each "
    "ingredient's dose amount and unit. Mark a line as a proprietary "
    "blend only if the label discloses a blend name with an undisclosed "
    "total dose split across multiple listed sub-ingredients. Do not "
    "guess or invent values that are not visible on the label -- omit "
    "optional fields instead. Respond only with JSON matching the "
    "provided response schema."
)


class ExtractedLabelData(BaseModel):
    """Raw label fields extractable directly from an image via OCR/vision.

    Intentionally excludes ``product_id`` (an internal identifier we
    assign, never printed on a label) and ``match_status`` /
    ``similarity_score`` (outputs of the downstream Primary Identifier
    Lookup step, not the vision step). This model doubles as the Gemini
    ``response_schema`` (structured output contract) and as the
    validation target for whatever JSON Gemini returns.
    """

    upc: Optional[str] = Field(default=None, description="UPC barcode, if legible in the image.")
    brand_name: str = Field(..., description="Brand or manufacturer name as printed on the label.")
    product_name: str = Field(..., description="Product display name as printed on the label.")
    formula_version: int = Field(default=1, ge=1, description="Formula version, if printed; default 1.")
    serving_size: str = Field(..., description="Serving size as printed, e.g. '2 capsules'.")
    servings_per_container: int = Field(..., gt=0, description="Servings per container as printed.")
    certifications: List[str] = Field(
        default_factory=list, description="Certifications/seals visible on the label."
    )
    ingredients: List[ProductIngredient] = Field(
        default_factory=list,
        description="Every ingredient line from the Supplement/Nutrition Facts panel.",
    )


class VisionParsingError(Exception):
    """Raised internally for expected failure modes.

    Always caught at the ``parse_label_image`` boundary and converted to
    a draft fallback payload; exposed publicly only so configuration
    errors (e.g. no API key configured) can surface at construction time
    instead of being silently swallowed.
    """


class VisionParserService:
    """Parses supplement label images into ``StructuredProductPayload`` via Gemini."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        model: Optional[str] = None,
    ) -> None:
        """Configure the service.

        Args:
            settings: Injected ``Settings``; defaults to ``get_settings()``.
            model: Override the Gemini model, e.g. "gemini-1.5-flash".
                Defaults to ``DEFAULT_GEMINI_MODEL`` ("gemini-2.5-flash").

        Raises:
            VisionParsingError: If ``GEMINI_API_KEY`` is missing, blank, or
                whitespace-only.
        """
        self._settings = settings or get_settings()
        self._api_key = self._validate_api_key(self._settings.gemini_api_key)
        self._model = model or DEFAULT_GEMINI_MODEL

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

    async def parse_label_image(
        self,
        image: ImageInput,
        product_id: Optional[str] = None,
    ) -> StructuredProductPayload:
        """Extract a ``StructuredProductPayload`` from a label image.

        Args:
            image: A filesystem path (``str``/``Path``) or raw image bytes.
            product_id: Internal product ID to assign; a new one is
                generated if omitted.

        Returns:
            A ``StructuredProductPayload`` with ``match_status=DRAFT``.
            Never raises: any failure (bad image, API error, malformed or
            incomplete structured output) degrades to a minimal fallback
            payload per the Non-Blocking Fallbacks architecture decision.
        """
        product_id = product_id or self._generate_product_id()
        try:
            image_bytes, media_type = self._load_image(image)
            raw_output = await self._call_vision_llm(image_bytes, media_type)
            extracted = ExtractedLabelData.model_validate(raw_output)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all fallback boundary
            logger.warning(
                "Vision parsing failed for product_id=%s (%s: %s); returning draft fallback.",
                product_id,
                type(exc).__name__,
                exc,
            )
            return self._fallback_payload(product_id)

        return self._to_payload(product_id, extracted)

    # -- Internals ----------------------------------------------------------

    @staticmethod
    def _generate_product_id() -> str:
        return f"prod_{uuid.uuid4().hex[:12]}"

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
        ``ExtractedLabelData`` directly.
        """
        from google import genai  # local import: optional dependency, only needed here
        from google.genai import types

        # Re-validate defensively: a service instance can outlive a settings
        # object that gets mutated, and this is the point that actually
        # builds the Gemini client.
        api_key = self._validate_api_key(self._api_key)
        client = genai.Client(api_key=api_key)

        response = await client.aio.models.generate_content(
            model=self._model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=media_type),
                "Extract this supplement label.",
            ],
            config=types.GenerateContentConfig(
                system_instruction=EXTRACTION_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=ExtractedLabelData,
            ),
        )

        if not response.text:
            raise VisionParsingError("Gemini response did not include any structured output text.")
        return json.loads(response.text)

    def _to_payload(self, product_id: str, extracted: ExtractedLabelData) -> StructuredProductPayload:
        metadata = ProductMetadata(
            product_id=product_id,
            upc=extracted.upc,
            brand_name=extracted.brand_name,
            product_name=extracted.product_name,
            formula_version=extracted.formula_version,
            serving_size=extracted.serving_size,
            servings_per_container=extracted.servings_per_container,
            certifications=extracted.certifications,
        )
        return StructuredProductPayload(
            match_status=MatchStatus.DRAFT,
            similarity_score=None,
            product_metadata=metadata,
            product_ingredients=extracted.ingredients,
        )

    def _fallback_payload(self, product_id: str) -> StructuredProductPayload:
        """Minimal, schema-valid draft used when parsing fails outright.

        Per the "Non-Blocking Fallbacks" architectural decision: OCR
        failures should yield a Draft Record, not a hard user error.
        """
        metadata = ProductMetadata(
            product_id=product_id,
            upc=None,
            brand_name="Unknown Brand",
            product_name="Unrecognized Product (manual review required)",
            formula_version=1,
            serving_size="Unknown",
            servings_per_container=1,
            certifications=[],
        )
        return StructuredProductPayload(
            match_status=MatchStatus.DRAFT,
            similarity_score=None,
            product_metadata=metadata,
            product_ingredients=[],
        )
