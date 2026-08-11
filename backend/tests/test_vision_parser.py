"""Tests for app.services.vision_parser.

The core tests mock ``VisionParserService._call_vision_llm`` -- the seam
between our schema-validation logic and the external Gemini API -- so
they run without requiring the ``google-genai`` SDK to be installed. A
supplementary test exercises the real Gemini call path with the SDK's
client class itself mocked (no network call); it is skipped
automatically if ``google-genai`` isn't installed.

Unlike the old multi-phase pipeline's vision parser, this service does
NOT degrade to a fabricated draft payload on failure -- every failure
mode raises ``VisionParsingError`` instead (see that class's docstring),
so these tests assert on the raised exception rather than a fallback
object's fields.
"""

import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.schemas.scan import ScanResult
from app.services.vision_parser import VisionParserService, VisionParsingError

GENAI_AVAILABLE = importlib.util.find_spec("google.genai") is not None

VALID_TOOL_RESPONSE = {
    "brand_name": "Example Labs",
    "product_name": "Daily Focus Boost",
    "serving_size": "2 capsules",
    "servings_per_container": 30,
    "ingredients": [
        {
            "name": "Ashwagandha",
            "form": "KSM-66 Root Extract",
            "amount": 600,
            "unit": "mg",
            "percent_daily_value": None,
        },
        {
            "name": "Vitamin D3",
            "form": "Cholecalciferol",
            "amount": 25,
            "unit": "mcg",
            "percent_daily_value": "125%",
        },
    ],
}


def run(coro):
    """Run an async test coroutine without requiring pytest-asyncio."""
    return asyncio.run(coro)


@pytest.fixture
def settings_with_gemini_key() -> Settings:
    # _env_file=None: don't let a locally-exported GEMINI_MODEL (or anything else in
    # backend/.env) leak into these tests.
    return Settings(_env_file=None, gemini_api_key="test-gemini-key")


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


class TestScanLabelSuccess:
    """Valid, fully-formed structured output should produce a matching ScanResult."""

    def test_valid_response_instantiates_scan_result(self, service, sample_image_path):
        with patch.object(VisionParserService, "_call_vision_llm", return_value=VALID_TOOL_RESPONSE):
            result = run(service.scan_label(sample_image_path))

        assert isinstance(result, ScanResult)
        assert result.scan_id.startswith("scan_")
        assert result.product.brand_name == "Example Labs"
        assert result.product.product_name == "Daily Focus Boost"
        assert result.product.servings_per_container == 30
        assert len(result.ingredients) == 2
        assert result.ingredients[0].name == "Ashwagandha"
        assert result.ingredients[0].form == "KSM-66 Root Extract"
        assert result.ingredients[0].amount == 600
        assert result.ingredients[0].unit == "mg"
        assert result.ingredients[0].percent_daily_value is None
        assert result.ingredients[1].percent_daily_value == "125%"

    def test_generates_a_unique_scan_id_each_call(self, service, sample_image_path):
        with patch.object(VisionParserService, "_call_vision_llm", return_value=VALID_TOOL_RESPONSE):
            first = run(service.scan_label(sample_image_path))
            second = run(service.scan_label(sample_image_path))

        assert first.scan_id != second.scan_id

    def test_raw_bytes_input_is_accepted(self, service, sample_image_path):
        image_bytes = sample_image_path.read_bytes()
        with patch.object(VisionParserService, "_call_vision_llm", return_value=VALID_TOOL_RESPONSE):
            result = run(service.scan_label(image_bytes))

        assert result.product.brand_name == "Example Labs"

    def test_missing_optional_fields_are_left_null_not_fabricated(self, service, sample_image_path):
        sparse_response = {
            "brand_name": "Example Labs",
            "ingredients": [{"name": "Zinc"}],
        }
        with patch.object(VisionParserService, "_call_vision_llm", return_value=sparse_response):
            result = run(service.scan_label(sample_image_path))

        assert result.product.brand_name == "Example Labs"
        assert result.product.product_name is None
        assert result.product.serving_size is None
        assert result.product.servings_per_container is None
        assert result.ingredients[0].name == "Zinc"
        assert result.ingredients[0].form is None
        assert result.ingredients[0].amount is None
        assert result.ingredients[0].unit is None
        assert result.ingredients[0].percent_daily_value is None


class TestScanLabelFailure:
    """Every failure mode should raise VisionParsingError, never fabricate a fallback."""

    def test_malformed_response_raises_vision_parsing_error(self, service, sample_image_path):
        # ingredients[0] is missing the required `name` field.
        malformed_response = {"brand_name": "Example Labs", "ingredients": [{"amount": 600}]}
        with patch.object(VisionParserService, "_call_vision_llm", return_value=malformed_response):
            with pytest.raises(VisionParsingError):
                run(service.scan_label(sample_image_path))

    def test_llm_call_exception_raises_vision_parsing_error(self, service, sample_image_path):
        with patch.object(
            VisionParserService, "_call_vision_llm", side_effect=RuntimeError("network error")
        ):
            with pytest.raises(VisionParsingError):
                run(service.scan_label(sample_image_path))

    def test_nonexistent_image_path_raises_vision_parsing_error(self, service, tmp_path):
        missing_path = tmp_path / "does_not_exist.png"

        with pytest.raises(VisionParsingError, match="does not exist"):
            run(service.scan_label(missing_path))

    def test_unsupported_media_type_raises_vision_parsing_error(self, service, tmp_path):
        bogus_file = tmp_path / "label.txt"
        bogus_file.write_text("not an image")

        with pytest.raises(VisionParsingError, match="Unsupported or undetected media type"):
            run(service.scan_label(bogus_file))

    def test_empty_image_bytes_raises_vision_parsing_error(self, service):
        with pytest.raises(VisionParsingError, match="empty"):
            run(service.scan_label(b""))


class TestProviderConfiguration:
    """Constructor-level configuration checks; no network or SDK required."""

    def test_no_api_key_raises_configuration_error(self, monkeypatch):
        # Clear the real process environment so a locally-exported
        # GEMINI_API_KEY can't leak into this test, and disable dotenv
        # loading (`_env_file=None`) so `backend/.env` can't either.
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        settings = Settings(_env_file=None, gemini_api_key=None)

        with pytest.raises(VisionParsingError, match="GEMINI_API_KEY is missing or invalid."):
            VisionParserService(settings=settings)

    def test_blank_api_key_raises_configuration_error(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        settings = Settings(_env_file=None, gemini_api_key="   ")

        with pytest.raises(VisionParsingError, match="GEMINI_API_KEY is missing or invalid."):
            VisionParserService(settings=settings)

    def test_service_has_no_model_selection_surface(self, settings_with_gemini_key):
        # Which model gets called is read dynamically from settings.gemini_model
        # (env var GEMINI_MODEL) by gemini_client, not configurable per
        # VisionParserService instance -- there is no `model=` constructor arg
        # and no `self._model` attribute.
        service = VisionParserService(settings=settings_with_gemini_key)
        assert not hasattr(service, "_model")

        with pytest.raises(TypeError):
            VisionParserService(settings=settings_with_gemini_key, model="gemini-2.5-flash")


class TestServingsPerContainerNullability:
    """servings_per_container is Optional[int] with no gt=0/exclusiveMinimum constraint.

    Non-US labels frequently print a variable dosing range instead of a
    single servings-per-container count, and Gemini's structured-output
    schema validator has rejected `exclusiveMinimum` in the past -- both
    are reasons this field must tolerate missing/None cleanly rather than
    raising.
    """

    def test_end_to_end_none_from_llm_is_accepted(self, service, sample_image_path):
        response = {**VALID_TOOL_RESPONSE, "servings_per_container": None}
        with patch.object(VisionParserService, "_call_vision_llm", return_value=response):
            result = run(service.scan_label(sample_image_path))

        assert result.product.brand_name == "Example Labs"
        assert result.product.servings_per_container is None

    def test_end_to_end_omitted_field_from_llm_is_accepted(self, service, sample_image_path):
        response = {k: v for k, v in VALID_TOOL_RESPONSE.items() if k != "servings_per_container"}
        with patch.object(VisionParserService, "_call_vision_llm", return_value=response):
            result = run(service.scan_label(sample_image_path))

        assert result.product.brand_name == "Example Labs"
        assert result.product.servings_per_container is None

    def test_zero_is_accepted_without_raising(self, service, sample_image_path):
        # No longer rejected: the prompt steers Gemini away from emitting 0,
        # but the schema itself must not choke on it (that was the reported bug).
        response = {**VALID_TOOL_RESPONSE, "servings_per_container": 0}
        with patch.object(VisionParserService, "_call_vision_llm", return_value=response):
            result = run(service.scan_label(sample_image_path))

        assert result.product.servings_per_container == 0


@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai SDK not installed")
class TestGeminiProviderIntegration:
    """Exercises the real _call_vision_llm -> gemini_client.generate_content code
    path with the SDK client mocked (no network). Every test here patches
    gemini_client's asyncio.sleep -- generate_content pays a real 60s
    mandatory pause per call (see app.services.gemini_client), and these
    tests would otherwise actually wait for it.

    The mocked client exposes the SYNCHRONOUS ``client.models.generate_content``
    method (a plain ``MagicMock``, not ``AsyncMock``) -- ``generate_content``
    calls it via ``asyncio.to_thread``, matching the "standard synchronous
    SDK call" rule in ``app.services.gemini_client``'s module docstring.
    """

    @staticmethod
    def _patched_sleep():
        return patch("app.services.gemini_client.asyncio.sleep", new_callable=AsyncMock)

    def test_call_gemini_parses_structured_json_response(self, settings_with_gemini_key, sample_image_path):
        from google import genai

        mock_response = MagicMock(text=json.dumps(VALID_TOOL_RESPONSE))

        service = VisionParserService(settings=settings_with_gemini_key)

        with patch.object(genai, "Client") as mock_client_cls, self._patched_sleep():
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content = MagicMock(return_value=mock_response)

            result = run(service.scan_label(sample_image_path))

        assert result.product.brand_name == "Example Labs"
        assert len(result.ingredients) == 2
        mock_client.models.generate_content.assert_called_once()
        # Model comes from settings.gemini_model (no "models/" prefix to strip here).
        assert (
            mock_client.models.generate_content.call_args.kwargs["model"]
            == settings_with_gemini_key.gemini_model
        )

    def test_call_gemini_strips_a_models_prefix_from_the_configured_model(self, sample_image_path):
        from google import genai

        settings = Settings(
            _env_file=None, gemini_api_key="test-gemini-key", gemini_model="models/gemini-2.0-flash"
        )
        mock_response = MagicMock(text=json.dumps(VALID_TOOL_RESPONSE))

        service = VisionParserService(settings=settings)

        with patch.object(genai, "Client") as mock_client_cls, self._patched_sleep():
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content = MagicMock(return_value=mock_response)

            run(service.scan_label(sample_image_path))

        assert mock_client.models.generate_content.call_args.kwargs["model"] == "gemini-2.0-flash"

    def test_a_404_fails_immediately_with_no_fallback_attempt(self, settings_with_gemini_key, sample_image_path):
        """Zero retries, zero fallback: a 404 must raise VisionParsingError
        immediately, with exactly one call made -- never a second attempt
        against anything.

        This intentionally inverts the old pipeline's fallback test: that
        mechanism has been removed entirely, not just reordered.
        """
        from google import genai

        class FakeNotFoundError(Exception):
            code = 404

        service = VisionParserService(settings=settings_with_gemini_key)

        with patch.object(genai, "Client") as mock_client_cls, self._patched_sleep():
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content = MagicMock(side_effect=FakeNotFoundError("model not found"))

            with pytest.raises(VisionParsingError):
                run(service.scan_label(sample_image_path))

        assert mock_client.models.generate_content.call_count == 1

    def test_a_429_lookalike_that_is_not_a_genai_clienterror_fails_immediately(
        self, settings_with_gemini_key, sample_image_path
    ):
        # Only a real google.genai.errors.ClientError triggers gemini_client's
        # 429 retry path (see test_gemini_client.py's TestRateLimitRetry, and
        # test_a_real_429_clienterror_is_retried_then_succeeds below) -- a
        # lookalike exception with a `.code = 429` attribute that isn't that
        # SDK type must still fail on the first attempt.
        from google import genai

        class FakeRateLimitError(Exception):
            code = 429

        service = VisionParserService(settings=settings_with_gemini_key)

        with patch.object(genai, "Client") as mock_client_cls, self._patched_sleep():
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content = MagicMock(side_effect=FakeRateLimitError("RESOURCE_EXHAUSTED"))

            with pytest.raises(VisionParsingError):
                run(service.scan_label(sample_image_path))

        assert mock_client.models.generate_content.call_count == 1

    def test_a_real_429_clienterror_is_retried_then_succeeds(self, settings_with_gemini_key, sample_image_path):
        from google import genai
        from google.genai import errors as genai_errors

        rate_limit_error = genai_errors.ClientError(
            429,
            {
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Resource has been exhausted (e.g. check quota).",
                    "details": [
                        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"}
                    ],
                }
            },
        )
        mock_response = MagicMock(text=json.dumps(VALID_TOOL_RESPONSE))

        service = VisionParserService(settings=settings_with_gemini_key)

        with patch.object(genai, "Client") as mock_client_cls, self._patched_sleep():
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content = MagicMock(
                side_effect=[rate_limit_error, mock_response]
            )

            result = run(service.scan_label(sample_image_path))

        assert result.product.brand_name == "Example Labs"
        assert mock_client.models.generate_content.call_count == 2

    def test_empty_gemini_response_text_raises_vision_parsing_error(self, settings_with_gemini_key, sample_image_path):
        from google import genai

        mock_response = MagicMock(text="")

        service = VisionParserService(settings=settings_with_gemini_key)

        with patch.object(genai, "Client") as mock_client_cls, self._patched_sleep():
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content = MagicMock(return_value=mock_response)

            with pytest.raises(VisionParsingError):
                run(service.scan_label(sample_image_path))
