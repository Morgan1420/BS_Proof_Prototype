"""Tests for app.services.vision_parser.

The core tests mock ``VisionParserService._call_vision_llm`` -- the seam
between our schema-validation logic and the external Gemini API -- so
they run without requiring the ``google-genai`` SDK to be installed. A
supplementary test exercises the real Gemini call path with the SDK's
client class itself mocked (no network call); it is skipped
automatically if ``google-genai`` isn't installed.
"""

import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.schemas.product import MatchStatus, StructuredProductPayload
from app.services.vision_parser import (
    ExtractedLabelData,
    VisionParserService,
    VisionParsingError,
)

GENAI_AVAILABLE = importlib.util.find_spec("google.genai") is not None

VALID_TOOL_RESPONSE = {
    "upc": "012345678905",
    "brand_name": "Example Labs",
    "product_name": "Daily Focus Boost",
    "formula_version": 2,
    "serving_size": "2 capsules",
    "servings_per_container": 30,
    "certifications": ["NSF", "GMP"],
    "ingredients": [
        {
            "raw_name": "KSM-66 Ashwagandha",
            "dose_amount": 600,
            "dose_unit": "mg",
            "standardization": "5% Withanolides",
        },
        {
            "raw_name": "Energy Blend",
            "dose_amount": 400,
            "dose_unit": "mg",
            "is_proprietary_blend": True,
            "blend_components": ["Caffeine Anhydrous", "L-Theanine"],
        },
    ],
}


def run(coro):
    """Run an async test coroutine without requiring pytest-asyncio."""
    return asyncio.run(coro)


@pytest.fixture
def settings_with_gemini_key() -> Settings:
    return Settings(ncbi_entrez_email="test@example.com", gemini_api_key="test-gemini-key")


@pytest.fixture
def service(settings_with_gemini_key: Settings) -> VisionParserService:
    return VisionParserService(settings=settings_with_gemini_key)


@pytest.fixture
def sample_image_path(tmp_path: Path) -> Path:
    """A minimal valid 1x1 PNG file, for path-based input tests."""
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6360000002000100ffff03000006000557bfabd4000000"
        "004945454e44ae426082"
    )
    path = tmp_path / "label.png"
    path.write_bytes(png_bytes)
    return path


class TestParseLabelImageSuccess:
    """Valid, fully-formed structured output should produce a matching payload."""

    def test_valid_response_instantiates_structured_payload(self, service, sample_image_path):
        with patch.object(VisionParserService, "_call_vision_llm", return_value=VALID_TOOL_RESPONSE):
            payload = run(service.parse_label_image(sample_image_path, product_id="prod_test_001"))

        assert isinstance(payload, StructuredProductPayload)
        assert payload.match_status == MatchStatus.DRAFT
        assert payload.similarity_score is None
        assert payload.product_metadata.product_id == "prod_test_001"
        assert payload.product_metadata.brand_name == "Example Labs"
        assert payload.product_metadata.upc == "012345678905"
        assert payload.product_metadata.servings_per_container == 30
        assert len(payload.product_ingredients) == 2
        assert payload.product_ingredients[0].raw_name == "KSM-66 Ashwagandha"
        assert payload.product_ingredients[1].is_proprietary_blend is True
        assert payload.product_ingredients[1].blend_components == [
            "Caffeine Anhydrous",
            "L-Theanine",
        ]

    def test_generates_product_id_when_not_provided(self, service, sample_image_path):
        with patch.object(VisionParserService, "_call_vision_llm", return_value=VALID_TOOL_RESPONSE):
            payload = run(service.parse_label_image(sample_image_path))

        assert payload.product_metadata.product_id.startswith("prod_")

    def test_raw_bytes_input_is_accepted(self, service, sample_image_path):
        image_bytes = sample_image_path.read_bytes()
        with patch.object(VisionParserService, "_call_vision_llm", return_value=VALID_TOOL_RESPONSE):
            payload = run(service.parse_label_image(image_bytes, product_id="prod_bytes"))

        assert payload.product_metadata.product_id == "prod_bytes"


class TestParseLabelImageFallback:
    """Every failure mode should degrade to a draft payload, never raise."""

    def test_missing_required_fields_falls_back_to_draft(self, service, sample_image_path):
        incomplete_response = {"brand_name": "Example Labs"}  # missing product_name, serving_size, etc.
        with patch.object(VisionParserService, "_call_vision_llm", return_value=incomplete_response):
            payload = run(service.parse_label_image(sample_image_path, product_id="prod_incomplete"))

        assert isinstance(payload, StructuredProductPayload)
        assert payload.match_status == MatchStatus.DRAFT
        assert payload.product_metadata.product_id == "prod_incomplete"
        assert payload.product_metadata.brand_name == "Unknown Brand"
        assert payload.product_ingredients == []

    def test_llm_call_exception_falls_back_to_draft(self, service, sample_image_path):
        with patch.object(
            VisionParserService, "_call_vision_llm", side_effect=RuntimeError("network error")
        ):
            payload = run(service.parse_label_image(sample_image_path, product_id="prod_error"))

        assert isinstance(payload, StructuredProductPayload)
        assert payload.match_status == MatchStatus.DRAFT
        assert payload.product_metadata.brand_name == "Unknown Brand"

    def test_nonexistent_image_path_falls_back_to_draft(self, service, tmp_path):
        missing_path = tmp_path / "does_not_exist.png"

        payload = run(service.parse_label_image(missing_path, product_id="prod_missing"))

        assert isinstance(payload, StructuredProductPayload)
        assert payload.match_status == MatchStatus.DRAFT
        assert payload.product_metadata.product_id == "prod_missing"

    def test_unsupported_media_type_falls_back_to_draft(self, service, tmp_path):
        bogus_file = tmp_path / "label.txt"
        bogus_file.write_text("not an image")

        payload = run(service.parse_label_image(bogus_file, product_id="prod_bad_type"))

        assert isinstance(payload, StructuredProductPayload)
        assert payload.match_status == MatchStatus.DRAFT

    def test_empty_response_text_falls_back_to_draft(self, service, sample_image_path):
        with patch.object(VisionParserService, "_call_vision_llm", side_effect=VisionParsingError("empty")):
            payload = run(service.parse_label_image(sample_image_path, product_id="prod_empty"))

        assert isinstance(payload, StructuredProductPayload)
        assert payload.match_status == MatchStatus.DRAFT


class TestProviderConfiguration:
    """Constructor-level configuration checks; no network or SDK required."""

    def test_no_api_key_raises_configuration_error(self, monkeypatch):
        # Clear the real process environment so a locally-exported
        # GEMINI_API_KEY can't leak into this test, and disable dotenv
        # loading (`_env_file=None`) so `backend/.env` can't either --
        # only then is `gemini_api_key=None` guaranteed to hold.
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        settings = Settings(_env_file=None, ncbi_entrez_email="test@example.com", gemini_api_key=None)

        with pytest.raises(VisionParsingError, match="GEMINI_API_KEY is missing or invalid."):
            VisionParserService(settings=settings)

    def test_blank_api_key_raises_configuration_error(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        settings = Settings(_env_file=None, ncbi_entrez_email="test@example.com", gemini_api_key="   ")

        with pytest.raises(VisionParsingError, match="GEMINI_API_KEY is missing or invalid."):
            VisionParserService(settings=settings)

    def test_uses_default_model_when_not_overridden(self, settings_with_gemini_key):
        service = VisionParserService(settings=settings_with_gemini_key)
        assert service._model == "gemini-2.5-flash"

    def test_model_override_is_respected(self, settings_with_gemini_key):
        service = VisionParserService(settings=settings_with_gemini_key, model="gemini-1.5-flash")
        assert service._model == "gemini-1.5-flash"


class TestExtractedLabelDataSchema:
    """The structured-output contract should enforce the same invariants as ProductIngredient."""

    def test_rejects_blend_components_without_blend_flag(self):
        with pytest.raises(ValidationError):
            ExtractedLabelData(
                brand_name="X",
                product_name="Y",
                serving_size="1 capsule",
                servings_per_container=30,
                ingredients=[
                    {
                        "raw_name": "Energy Blend",
                        "dose_amount": 100,
                        "dose_unit": "mg",
                        "blend_components": ["Caffeine"],
                    }
                ],
            )

    def test_requires_core_fields(self):
        with pytest.raises(ValidationError):
            ExtractedLabelData()


@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai SDK not installed")
class TestGeminiProviderIntegration:
    """Exercises the real _call_vision_llm code path with the SDK client mocked (no network)."""

    def test_call_gemini_parses_structured_json_response(self, settings_with_gemini_key, sample_image_path):
        from google import genai

        mock_response = MagicMock(text=json.dumps(VALID_TOOL_RESPONSE))

        service = VisionParserService(settings=settings_with_gemini_key)

        with patch.object(genai, "Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

            payload = run(service.parse_label_image(sample_image_path, product_id="prod_gemini"))

        assert payload.product_metadata.product_id == "prod_gemini"
        assert payload.product_metadata.brand_name == "Example Labs"
        assert len(payload.product_ingredients) == 2
        mock_client.aio.models.generate_content.assert_called_once()

    def test_empty_gemini_response_text_falls_back_to_draft(self, settings_with_gemini_key, sample_image_path):
        from google import genai

        mock_response = MagicMock(text="")

        service = VisionParserService(settings=settings_with_gemini_key)

        with patch.object(genai, "Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

            payload = run(service.parse_label_image(sample_image_path, product_id="prod_empty_gemini"))

        assert payload.match_status == MatchStatus.DRAFT
        assert payload.product_metadata.brand_name == "Unknown Brand"
