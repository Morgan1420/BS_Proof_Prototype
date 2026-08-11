"""Tests for app.services.grading_service.IngredientGradingService.

The orchestration tests mock both external seams (literature_search's
aggregate_literature and the private _evaluate_with_gemini step)
directly, so they run without requiring the google-genai SDK. A
supplementary class exercises the real Gemini call path with the SDK's
client class mocked (no network call); it's skipped automatically if
google-genai isn't installed -- same pattern as test_vision_parser.py.
"""

import asyncio
import importlib.util
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.schemas.scan import ScannedIngredient
from app.services.grading_service import (
    GradingError,
    GradingResult,
    GradingStats,
    IngredientGradingService,
    SifgConsensus,
)
from app.services.literature_search import AggregatedLiteratureResult, RawPaper

GENAI_AVAILABLE = importlib.util.find_spec("google.genai") is not None


def run(coro):
    return asyncio.run(coro)


def make_settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, gemini_api_key="test-key")
    defaults.update(overrides)
    return Settings(**defaults)


def make_ingredient(**overrides) -> ScannedIngredient:
    defaults = dict(name="Ashwagandha", form="KSM-66 Root Extract", amount=600, unit="mg")
    defaults.update(overrides)
    return ScannedIngredient(**defaults)


def make_retrieval(**overrides) -> AggregatedLiteratureResult:
    defaults = dict(
        studies=[],
        queries_used=[],
        provider_counts={"PubMed": 0, "Europe PMC": 0, "OpenAlex": 0, "Semantic Scholar": 0},
        provider_errors={},
        papers_found=0,
        papers_analyzed=0,
    )
    defaults.update(overrides)
    return AggregatedLiteratureResult(**defaults)


SAMPLE_CONSENSUS = SifgConsensus(
    sifg_grade="B+",
    sifg_score=78.0,
    efficacy_safety_evaluation="Generally well tolerated in the reviewed studies.",
    dosage_appropriateness="600mg/day is within the range studied.",
    evidence_summary="Based on 2 provided studies.",
    studies_considered=["12345678"],
)


class TestConstructorApiKeyValidation:
    def test_missing_api_key_raises_grading_error(self):
        with pytest.raises(GradingError, match="GEMINI_API_KEY"):
            IngredientGradingService(settings=make_settings(gemini_api_key=None))

    def test_blank_api_key_raises_grading_error(self):
        with pytest.raises(GradingError, match="GEMINI_API_KEY"):
            IngredientGradingService(settings=make_settings(gemini_api_key="   "))

    def test_valid_api_key_constructs_cleanly(self):
        service = IngredientGradingService(settings=make_settings(gemini_api_key="real-key"))
        assert service is not None


class TestGradeIngredientOrchestration:
    """grade_ingredient's own logic: literature aggregation then Gemini evaluation, plus stats assembly.

    Mocks aggregate_literature and _evaluate_with_gemini directly -- no
    google-genai SDK or network required.
    """

    def test_successful_retrieval_results_are_passed_through_to_gemini(self):
        service = IngredientGradingService(settings=make_settings(gemini_model="gemini-test-model"))
        studies = [RawPaper(source="PubMed", title="A Study", abstract="Abstract text.", pmid="12345678")]
        retrieval = make_retrieval(
            studies=studies,
            queries_used=["Ashwagandha supplementation"],
            provider_counts={"PubMed": 1, "Europe PMC": 0, "OpenAlex": 0, "Semantic Scholar": 0},
            papers_found=1,
            papers_analyzed=1,
        )

        with patch(
            "app.services.grading_service.aggregate_literature", new=AsyncMock(return_value=retrieval)
        ), patch.object(
            IngredientGradingService, "_evaluate_with_gemini", new=AsyncMock(return_value=SAMPLE_CONSENSUS)
        ) as mock_evaluate:
            result = run(service.grade_ingredient(make_ingredient()))

        assert isinstance(result, GradingResult)
        assert result.consensus is SAMPLE_CONSENSUS
        passed_studies, passed_search_failed = mock_evaluate.call_args.args[1], mock_evaluate.call_args.args[2]
        assert passed_studies == studies
        assert passed_search_failed is False

        assert result.stats.papers_found == 1
        assert result.stats.papers_analyzed == 1
        assert result.stats.search_queries == ["Ashwagandha supplementation"]
        assert result.stats.provider_counts == {
            "PubMed": 1, "Europe PMC": 0, "OpenAlex": 0, "Semantic Scholar": 0,
        }
        assert result.stats.model_used == "gemini-test-model"
        assert result.stats.grading_duration_seconds >= 0

    def test_total_retrieval_failure_degrades_to_empty_studies_with_failed_flag(self):
        service = IngredientGradingService(settings=make_settings())
        retrieval = make_retrieval(
            studies=[],
            queries_used=["Ashwagandha KSM-66 Root Extract supplementation"],
            provider_counts={"PubMed": 0, "Europe PMC": 0, "OpenAlex": 0, "Semantic Scholar": 0},
            provider_errors={
                "PubMed": "boom", "Europe PMC": "boom", "OpenAlex": "boom", "Semantic Scholar": "boom",
            },
            papers_found=0,
            papers_analyzed=0,
        )

        with patch(
            "app.services.grading_service.aggregate_literature", new=AsyncMock(return_value=retrieval)
        ), patch.object(
            IngredientGradingService, "_evaluate_with_gemini", new=AsyncMock(return_value=SAMPLE_CONSENSUS)
        ) as mock_evaluate:
            result = run(service.grade_ingredient(make_ingredient()))

        assert result.consensus is SAMPLE_CONSENSUS  # grading still succeeds despite the search failure
        passed_studies, passed_search_failed = mock_evaluate.call_args.args[1], mock_evaluate.call_args.args[2]
        assert passed_studies == []
        assert passed_search_failed is True

        assert result.stats.papers_found == 0
        assert result.stats.papers_analyzed == 0
        assert result.stats.search_queries == ["Ashwagandha KSM-66 Root Extract supplementation"]

    def test_partial_retrieval_failure_is_not_treated_as_a_total_search_failure(self):
        # Only one provider erroring should NOT flip search_failed to True --
        # the others still contributed papers.
        service = IngredientGradingService(settings=make_settings())
        studies = [RawPaper(source="Europe PMC", title="A Study", abstract="Abstract.", doi="10.1/x")]
        retrieval = make_retrieval(
            studies=studies,
            queries_used=["q"],
            provider_counts={"PubMed": 0, "Europe PMC": 1, "OpenAlex": 0, "Semantic Scholar": 0},
            provider_errors={"PubMed": "boom"},
            papers_found=1,
            papers_analyzed=1,
        )

        with patch(
            "app.services.grading_service.aggregate_literature", new=AsyncMock(return_value=retrieval)
        ), patch.object(
            IngredientGradingService, "_evaluate_with_gemini", new=AsyncMock(return_value=SAMPLE_CONSENSUS)
        ) as mock_evaluate:
            run(service.grade_ingredient(make_ingredient()))

        passed_search_failed = mock_evaluate.call_args.args[2]
        assert passed_search_failed is False

    def test_retrieval_failure_does_not_raise_grading_error(self):
        # A totally dead literature pipeline must not itself fail the grade --
        # only a failing Gemini call should raise GradingError (see the class below).
        service = IngredientGradingService(settings=make_settings())
        retrieval = make_retrieval(
            provider_errors={
                "PubMed": "boom", "Europe PMC": "boom", "OpenAlex": "boom", "Semantic Scholar": "boom",
            },
        )

        with patch(
            "app.services.grading_service.aggregate_literature", new=AsyncMock(return_value=retrieval)
        ), patch.object(
            IngredientGradingService, "_evaluate_with_gemini", new=AsyncMock(return_value=SAMPLE_CONSENSUS)
        ):
            run(service.grade_ingredient(make_ingredient()))  # should not raise

    def test_gemini_failure_after_a_retrieval_attaches_partial_stats_to_the_error(self):
        service = IngredientGradingService(settings=make_settings(gemini_model="gemini-test-model"))
        studies = [RawPaper(source="PubMed", title="T", abstract="A", pmid="1")]
        retrieval = make_retrieval(
            studies=studies,
            queries_used=["q1"],
            provider_counts={"PubMed": 1, "Europe PMC": 0, "OpenAlex": 0, "Semantic Scholar": 0},
            papers_found=1,
            papers_analyzed=1,
        )

        with patch(
            "app.services.grading_service.aggregate_literature", new=AsyncMock(return_value=retrieval)
        ), patch.object(
            IngredientGradingService,
            "_evaluate_with_gemini",
            new=AsyncMock(side_effect=GradingError("Gemini grading call failed: boom")),
        ):
            with pytest.raises(GradingError) as exc_info:
                run(service.grade_ingredient(make_ingredient()))

        stats = exc_info.value.stats
        assert stats is not None
        assert stats.papers_found == 1
        assert stats.papers_analyzed == 1
        assert stats.search_queries == ["q1"]
        assert stats.model_used == "gemini-test-model"


class TestBuildPrompt:
    def test_prompt_includes_ingredient_context(self):
        prompt = IngredientGradingService._build_prompt(
            make_ingredient(name="Zinc", form="Zinc Citrate", amount=15, unit="mg", percent_daily_value="136%"),
            studies=[],
            search_failed=False,
        )

        assert "Zinc" in prompt
        assert "Zinc Citrate" in prompt
        assert "15.0 mg" in prompt
        assert "136%" in prompt

    def test_prompt_notes_when_search_failed_explicitly(self):
        prompt = IngredientGradingService._build_prompt(make_ingredient(), studies=[], search_failed=True)

        assert "FAILED" in prompt

    def test_prompt_notes_zero_results_when_search_succeeded_but_empty(self):
        prompt = IngredientGradingService._build_prompt(make_ingredient(), studies=[], search_failed=False)

        assert "found no studies" in prompt
        assert "FAILED" not in prompt

    def test_prompt_includes_study_excerpts_when_present(self):
        studies = [
            RawPaper(source="PubMed", title="A Great Study", abstract="Some abstract body.", pmid="999")
        ]

        prompt = IngredientGradingService._build_prompt(make_ingredient(), studies=studies, search_failed=False)

        assert "PubMed | 999" in prompt
        assert "A Great Study" in prompt
        assert "Some abstract body." in prompt

    def test_prompt_includes_metadata_bits_when_present(self):
        studies = [
            RawPaper(
                source="Europe PMC",
                title="A Study",
                abstract="Abstract.",
                doi="10.1/x",
                citation_count=42,
                publication_year=2022,
                study_type="Randomized Controlled Trial",
            )
        ]

        prompt = IngredientGradingService._build_prompt(make_ingredient(), studies=studies, search_failed=False)

        assert "Year: 2022" in prompt
        assert "Citations: 42" in prompt
        assert "Type (inferred): Randomized Controlled Trial" in prompt

    def test_prompt_falls_back_to_doi_when_no_pmid(self):
        studies = [RawPaper(source="OpenAlex", title="A Study", abstract="Abstract.", doi="10.1/y", pmid=None)]

        prompt = IngredientGradingService._build_prompt(make_ingredient(), studies=studies, search_failed=False)

        assert "OpenAlex | 10.1/y" in prompt

    def test_prompt_notes_no_id_available_when_neither_pmid_nor_doi(self):
        studies = [RawPaper(source="OpenAlex", title="A Study", abstract="Abstract.")]

        prompt = IngredientGradingService._build_prompt(make_ingredient(), studies=studies, search_failed=False)

        assert "OpenAlex | no id available" in prompt


class TestResolveModel:
    def test_strips_models_prefix(self):
        service = IngredientGradingService(settings=make_settings(gemini_model="models/gemini-2.0-flash"))
        assert service._resolve_model() == "gemini-2.0-flash"

    def test_bare_model_name_is_unaffected(self):
        service = IngredientGradingService(settings=make_settings(gemini_model="gemini-2.0-flash"))
        assert service._resolve_model() == "gemini-2.0-flash"


@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai SDK not installed")
class TestGeminiEvaluationIntegration:
    """Exercises the real _evaluate_with_gemini -> gemini_client.generate_content code
    path with the SDK client mocked (no network). aggregate_literature is also mocked
    here (its own providers/dedup/ranking have dedicated tests in
    test_literature_search.py) so this focuses purely on the Gemini evaluation step,
    and its wiring into grade_ingredient's GradingResult (consensus + stats).
    """

    @staticmethod
    def _patched_sleep():
        return patch("app.services.gemini_client.asyncio.sleep", new_callable=AsyncMock)

    @staticmethod
    def _empty_retrieval():
        return AsyncMock(return_value=make_retrieval(queries_used=["q"]))

    def test_grade_ingredient_parses_structured_json_response(self):
        from google import genai

        consensus_json = {
            "sifg_grade": "A-",
            "sifg_score": 85.0,
            "efficacy_safety_evaluation": "Well supported by the provided studies.",
            "dosage_appropriateness": "Within typical studied range.",
            "evidence_summary": "Based on 1 provided study.",
            "studies_considered": ["12345678"],
        }
        mock_response = MagicMock(text=json.dumps(consensus_json))
        studies = [RawPaper(source="PubMed", title="A Study", abstract="Abstract.", pmid="12345678")]
        retrieval = make_retrieval(
            studies=studies,
            queries_used=["Ashwagandha"],
            provider_counts={"PubMed": 1, "Europe PMC": 0, "OpenAlex": 0, "Semantic Scholar": 0},
            papers_found=1,
            papers_analyzed=1,
        )

        service = IngredientGradingService(settings=make_settings())

        with patch.object(genai, "Client") as mock_client_cls, patch(
            "app.services.grading_service.aggregate_literature", new=AsyncMock(return_value=retrieval)
        ), self._patched_sleep():
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content = MagicMock(return_value=mock_response)

            result = run(service.grade_ingredient(make_ingredient()))

        assert isinstance(result, GradingResult)
        assert result.consensus.sifg_grade == "A-"
        assert result.consensus.sifg_score == 85.0
        assert isinstance(result.stats, GradingStats)
        assert result.stats.papers_found == 1
        assert result.stats.papers_analyzed == 1
        assert result.stats.search_queries == ["Ashwagandha"]
        mock_client.models.generate_content.assert_called_once()

    def test_gemini_call_failure_raises_grading_error(self):
        from google import genai

        class FakeNotFoundError(Exception):
            code = 404

        service = IngredientGradingService(settings=make_settings())

        with patch.object(genai, "Client") as mock_client_cls, patch(
            "app.services.grading_service.aggregate_literature", new=self._empty_retrieval()
        ), self._patched_sleep():
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content = MagicMock(side_effect=FakeNotFoundError("model not found"))

            with pytest.raises(GradingError):
                run(service.grade_ingredient(make_ingredient()))

    def test_empty_gemini_response_text_raises_grading_error(self):
        from google import genai

        service = IngredientGradingService(settings=make_settings())

        with patch.object(genai, "Client") as mock_client_cls, patch(
            "app.services.grading_service.aggregate_literature", new=self._empty_retrieval()
        ), self._patched_sleep():
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content = MagicMock(return_value=MagicMock(text=""))

            with pytest.raises(GradingError):
                run(service.grade_ingredient(make_ingredient()))

    def test_malformed_json_response_raises_grading_error(self):
        from google import genai

        service = IngredientGradingService(settings=make_settings())

        with patch.object(genai, "Client") as mock_client_cls, patch(
            "app.services.grading_service.aggregate_literature", new=self._empty_retrieval()
        ), self._patched_sleep():
            mock_client = mock_client_cls.return_value
            # Missing every required SifgConsensus field.
            mock_client.models.generate_content = MagicMock(return_value=MagicMock(text=json.dumps({})))

            with pytest.raises(GradingError):
                run(service.grade_ingredient(make_ingredient()))
