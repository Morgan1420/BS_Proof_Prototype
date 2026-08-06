"""Tests for app.services.consensus_engine.

Pure scoring-math tests (paper weighting, the consensus formula, evidence
level/grade bucketing) call the engine's static/class methods directly
and require no mocking at all. The full evaluate_ingredient() pipeline is
tested by mocking ConsensusEngine._evaluate_papers_with_llm() -- the seam
between scoring math and the external Gemini API -- so those tests run
without requiring the google-genai SDK. A supplementary test mocks the
real Gemini SDK client and is skipped automatically if google-genai isn't
installed.
"""

import asyncio
import importlib.util
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.schemas.ingredient_grade import EvidenceGrade, EvidenceLevel
from app.services.consensus_engine import (
    COI_PENALTY_MULTIPLIER,
    EVIDENCE_LEVEL_HIGH_AVG_WEIGHT,
    EVIDENCE_LEVEL_MODERATE_AVG_WEIGHT,
    SMALL_SAMPLE_PENALTY_MULTIPLIER,
    SMALL_SAMPLE_SIZE_THRESHOLD,
    STUDY_TIER_WEIGHTS,
    ConsensusEngine,
    ConsensusEngineError,
    ConsensusEvaluationResult,
    PaperEvaluation,
    PaperEvaluationBatch,
    ScoredPaper,
    StudyTier,
)
from app.services.pubmed_service import PubMedPaper

GENAI_AVAILABLE = importlib.util.find_spec("google.genai") is not None


def run(coro):
    """Run an async test coroutine without requiring pytest-asyncio."""
    return asyncio.run(coro)


def make_paper(pmid: str, sample_size=None, coi_statement=None, **overrides) -> PubMedPaper:
    defaults = dict(
        pmid=pmid,
        title=f"Paper {pmid}",
        abstract="An abstract.",
        publication_types=[],
        sample_size=sample_size,
        coi_statement=coi_statement,
    )
    defaults.update(overrides)
    return PubMedPaper(**defaults)


def make_evaluation(
    pmid: str, tier: StudyTier, directional_value: float, claim: str = "Test Claim", has_coi: bool = False, **overrides
) -> PaperEvaluation:
    defaults = dict(
        pmid=pmid,
        study_design_tier=tier,
        primary_claim=claim,
        directional_value=directional_value,
        has_conflict_of_interest=has_coi,
    )
    defaults.update(overrides)
    return PaperEvaluation(**defaults)


def make_scored(tier: StudyTier, weight: float, directional_value: float = 1.0, claim: str = "C", pmid: str = "1") -> ScoredPaper:
    return ScoredPaper(
        pmid=pmid,
        tier=tier,
        tier_weight=STUDY_TIER_WEIGHTS[tier],
        sample_size=None,
        has_conflict_of_interest=False,
        directional_value=directional_value,
        primary_claim=claim,
        weight=weight,
    )


@pytest.fixture
def settings_with_gemini_key() -> Settings:
    return Settings(_env_file=None, ncbi_entrez_email="test@example.com", gemini_api_key="test-gemini-key")


@pytest.fixture
def engine(settings_with_gemini_key: Settings) -> ConsensusEngine:
    return ConsensusEngine(settings=settings_with_gemini_key)


class TestComputePaperWeight:
    """Pure math: W_paper = W_tier x small-sample modifier x COI modifier."""

    @pytest.mark.parametrize(
        "tier,expected",
        [
            (StudyTier.META_ANALYSIS, 1.00),
            (StudyTier.RCT, 0.85),
            (StudyTier.OBSERVATIONAL, 0.50),
            (StudyTier.ANIMAL, 0.15),
            (StudyTier.IN_VITRO, 0.05),
        ],
    )
    def test_tier_weight_with_no_penalties(self, tier, expected):
        weight = ConsensusEngine.compute_paper_weight(tier, sample_size=None, has_conflict_of_interest=False)
        assert weight == pytest.approx(expected)

    def test_small_sample_size_penalty_applied(self):
        weight = ConsensusEngine.compute_paper_weight(StudyTier.RCT, sample_size=20, has_conflict_of_interest=False)
        assert weight == pytest.approx(0.85 * SMALL_SAMPLE_PENALTY_MULTIPLIER)

    def test_sample_size_at_threshold_is_not_penalized(self):
        weight = ConsensusEngine.compute_paper_weight(
            StudyTier.RCT, sample_size=SMALL_SAMPLE_SIZE_THRESHOLD, has_conflict_of_interest=False
        )
        assert weight == pytest.approx(0.85)

    def test_sample_size_just_below_threshold_is_penalized(self):
        weight = ConsensusEngine.compute_paper_weight(
            StudyTier.RCT, sample_size=SMALL_SAMPLE_SIZE_THRESHOLD - 1, has_conflict_of_interest=False
        )
        assert weight == pytest.approx(0.85 * SMALL_SAMPLE_PENALTY_MULTIPLIER)

    def test_unknown_sample_size_is_not_penalized(self):
        weight = ConsensusEngine.compute_paper_weight(StudyTier.RCT, sample_size=None, has_conflict_of_interest=False)
        assert weight == pytest.approx(0.85)

    def test_coi_penalty_applied(self):
        weight = ConsensusEngine.compute_paper_weight(StudyTier.RCT, sample_size=None, has_conflict_of_interest=True)
        assert weight == pytest.approx(0.85 * COI_PENALTY_MULTIPLIER)

    def test_small_sample_and_coi_penalties_are_multiplicative(self):
        weight = ConsensusEngine.compute_paper_weight(StudyTier.RCT, sample_size=10, has_conflict_of_interest=True)
        assert weight == pytest.approx(0.85 * SMALL_SAMPLE_PENALTY_MULTIPLIER * COI_PENALTY_MULTIPLIER)


class TestWeightedDirectionalConsensus:
    """Consensus Score = sum(directional_value * W_paper) / sum(W_paper)."""

    def test_unanimous_positive(self):
        scored = [
            make_scored(StudyTier.META_ANALYSIS, weight=1.0, directional_value=1.0),
            make_scored(StudyTier.RCT, weight=0.85, directional_value=1.0),
        ]
        assert ConsensusEngine._weighted_directional_consensus(scored) == pytest.approx(1.0)

    def test_unanimous_negative(self):
        scored = [make_scored(StudyTier.RCT, weight=0.85, directional_value=-1.0)]
        assert ConsensusEngine._weighted_directional_consensus(scored) == pytest.approx(-1.0)

    def test_mixed_consensus_is_weighted_not_averaged_evenly(self):
        scored = [
            make_scored(StudyTier.META_ANALYSIS, weight=1.0, directional_value=1.0),
            make_scored(StudyTier.RCT, weight=0.85, directional_value=-1.0),
        ]
        expected = (1.0 * 1.0 + (-1.0) * 0.85) / (1.0 + 0.85)
        assert ConsensusEngine._weighted_directional_consensus(scored) == pytest.approx(expected)

    def test_empty_list_returns_zero_without_dividing_by_zero(self):
        assert ConsensusEngine._weighted_directional_consensus([]) == 0.0


class TestEvidenceLevelBucketing:
    def test_high_at_threshold(self):
        group = [make_scored(StudyTier.RCT, weight=EVIDENCE_LEVEL_HIGH_AVG_WEIGHT)]
        assert ConsensusEngine._evidence_level_for_group(group) == EvidenceLevel.HIGH

    def test_moderate_just_below_high_threshold(self):
        group = [make_scored(StudyTier.RCT, weight=EVIDENCE_LEVEL_HIGH_AVG_WEIGHT - 0.01)]
        assert ConsensusEngine._evidence_level_for_group(group) == EvidenceLevel.MODERATE

    def test_moderate_at_threshold(self):
        group = [make_scored(StudyTier.RCT, weight=EVIDENCE_LEVEL_MODERATE_AVG_WEIGHT)]
        assert ConsensusEngine._evidence_level_for_group(group) == EvidenceLevel.MODERATE

    def test_low_just_below_moderate_threshold(self):
        group = [make_scored(StudyTier.RCT, weight=EVIDENCE_LEVEL_MODERATE_AVG_WEIGHT - 0.01)]
        assert ConsensusEngine._evidence_level_for_group(group) == EvidenceLevel.LOW

    def test_low_for_empty_group(self):
        assert ConsensusEngine._evidence_level_for_group([]) == EvidenceLevel.LOW


class TestGradeForScore:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (100.0, EvidenceGrade.A),
            (90.0, EvidenceGrade.A),
            (89.99, EvidenceGrade.B),
            (80.0, EvidenceGrade.B),
            (79.99, EvidenceGrade.C),
            (70.0, EvidenceGrade.C),
            (69.99, EvidenceGrade.D),
            (60.0, EvidenceGrade.D),
            (59.99, EvidenceGrade.F),
            (0.0, EvidenceGrade.F),
        ],
    )
    def test_grade_bands(self, score, expected):
        assert ConsensusEngine._grade_for_score(score) == expected


class TestBuildValidatedClaims:
    def test_groups_by_claim_case_insensitively_and_sorts_by_support(self):
        scored = [
            make_scored(StudyTier.META_ANALYSIS, weight=1.0, directional_value=1.0, claim="Stress Reduction", pmid="1"),
            make_scored(StudyTier.RCT, weight=0.85, directional_value=1.0, claim="stress reduction", pmid="2"),
            make_scored(StudyTier.OBSERVATIONAL, weight=0.5, directional_value=0.0, claim="Sleep Quality", pmid="3"),
        ]
        claims = ConsensusEngine._build_validated_claims(scored)

        assert [c.claim for c in claims] == ["Stress Reduction", "Sleep Quality"]
        assert claims[0].supporting_studies_count == 2
        assert claims[0].consensus_score == pytest.approx(1.0)
        assert claims[1].supporting_studies_count == 1
        assert claims[1].consensus_score == pytest.approx(0.0)


class TestComputeEvidenceSummary:
    def test_full_positive_consensus_maps_to_100(self):
        summary = ConsensusEngine._compute_evidence_summary(
            [make_scored(StudyTier.META_ANALYSIS, weight=1.0, directional_value=1.0)]
        )
        assert summary.composite_score == pytest.approx(100.0)
        assert summary.evidence_grade == EvidenceGrade.A

    def test_full_negative_consensus_maps_to_0(self):
        summary = ConsensusEngine._compute_evidence_summary(
            [make_scored(StudyTier.META_ANALYSIS, weight=1.0, directional_value=-1.0)]
        )
        assert summary.composite_score == pytest.approx(0.0)
        assert summary.evidence_grade == EvidenceGrade.F

    def test_neutral_consensus_maps_to_50(self):
        summary = ConsensusEngine._compute_evidence_summary(
            [make_scored(StudyTier.META_ANALYSIS, weight=1.0, directional_value=0.0)]
        )
        assert summary.composite_score == pytest.approx(50.0)

    def test_confidence_score_scales_with_volume_and_quality(self):
        one_paper = [make_scored(StudyTier.META_ANALYSIS, weight=1.0, pmid="1")]
        ten_papers = [make_scored(StudyTier.META_ANALYSIS, weight=1.0, pmid=str(i)) for i in range(10)]

        assert ConsensusEngine._compute_evidence_summary(one_paper).overall_confidence_score == pytest.approx(0.1)
        assert ConsensusEngine._compute_evidence_summary(ten_papers).overall_confidence_score == pytest.approx(1.0)

    def test_confidence_score_capped_at_one_beyond_volume_target(self):
        twelve_papers = [make_scored(StudyTier.META_ANALYSIS, weight=1.0, pmid=str(i)) for i in range(12)]
        summary = ConsensusEngine._compute_evidence_summary(twelve_papers)
        assert summary.overall_confidence_score == pytest.approx(1.0)


class TestEvaluateIngredientMissingDataHandling:
    """evaluate_ingredient must never raise -- every failure mode degrades gracefully."""

    def test_no_papers_returns_insufficient_evidence(self, engine):
        result = run(engine.evaluate_ingredient("ing_x", "X", []))

        assert isinstance(result, ConsensusEvaluationResult)
        assert result.evidence_summary.composite_score == 0.0
        assert result.evidence_summary.evidence_grade == EvidenceGrade.F
        assert result.evidence_summary.overall_confidence_score == 0.0
        assert result.validated_claims == []

    def test_llm_call_exception_returns_insufficient_evidence(self, engine):
        papers = [make_paper("1")]
        with patch.object(ConsensusEngine, "_evaluate_papers_with_llm", side_effect=RuntimeError("down")):
            result = run(engine.evaluate_ingredient("ing_x", "X", papers))

        assert result.evidence_summary.composite_score == 0.0
        assert result.evidence_summary.evidence_grade == EvidenceGrade.F

    def test_llm_returns_no_evaluations_returns_insufficient_evidence(self, engine):
        papers = [make_paper("1")]
        with patch.object(ConsensusEngine, "_evaluate_papers_with_llm", return_value=[]):
            result = run(engine.evaluate_ingredient("ing_x", "X", papers))

        assert result.evidence_summary.composite_score == 0.0

    def test_evaluation_for_unknown_pmid_is_dropped(self, engine):
        papers = [make_paper("1")]
        evaluations = [make_evaluation("999", StudyTier.RCT, 1.0)]  # pmid not present in papers
        with patch.object(ConsensusEngine, "_evaluate_papers_with_llm", return_value=evaluations):
            result = run(engine.evaluate_ingredient("ing_x", "X", papers))

        assert result.evidence_summary.composite_score == 0.0  # no scored papers remain
        assert result.validated_claims == []

    def test_duplicate_evaluation_for_same_pmid_counted_once(self, engine):
        papers = [make_paper("1")]
        evaluations = [
            make_evaluation("1", StudyTier.META_ANALYSIS, 1.0, claim="Claim A"),
            make_evaluation("1", StudyTier.RCT, -1.0, claim="Claim B"),  # duplicate pmid, ignored
        ]
        with patch.object(ConsensusEngine, "_evaluate_papers_with_llm", return_value=evaluations):
            result = run(engine.evaluate_ingredient("ing_x", "X", papers))

        assert len(result.validated_claims) == 1
        assert result.validated_claims[0].claim == "Claim A"
        assert result.evidence_summary.total_papers_analyzed == 1


class TestEvaluateIngredientFullPipeline:
    """Mocks _evaluate_papers_with_llm to test the full orchestration end-to-end."""

    def test_multi_paper_multi_claim_scoring(self, engine):
        papers = [
            make_paper("1", sample_size=500),
            make_paper("2", sample_size=15, coi_statement="Funded by the ingredient's patent holder."),
            make_paper("3", sample_size=200),
        ]
        evaluations = [
            make_evaluation("1", StudyTier.META_ANALYSIS, 1.0, claim="Stress Reduction", has_coi=False),
            make_evaluation("2", StudyTier.RCT, 1.0, claim="stress reduction", has_coi=True),
            make_evaluation("3", StudyTier.OBSERVATIONAL, 0.0, claim="Sleep Quality", has_coi=False),
        ]

        with patch.object(ConsensusEngine, "_evaluate_papers_with_llm", return_value=evaluations):
            result = run(engine.evaluate_ingredient("ing_ashwagandha_01", "Withania Somnifera", papers))

        assert result.ingredient_id == "ing_ashwagandha_01"
        assert result.evidence_summary.total_papers_analyzed == 3

        claims_by_name = {c.claim.lower(): c for c in result.validated_claims}
        assert len(result.validated_claims) == 2
        assert claims_by_name["stress reduction"].supporting_studies_count == 2
        assert claims_by_name["stress reduction"].consensus_score == pytest.approx(1.0)
        # The merged "stress reduction" group's average weight is pulled below the
        # HIGH threshold by paper 2's small-sample (n=15) + COI penalties.
        assert claims_by_name["stress reduction"].evidence_level == EvidenceLevel.MODERATE
        assert claims_by_name["sleep quality"].supporting_studies_count == 1
        assert claims_by_name["sleep quality"].consensus_score == pytest.approx(0.0)

        # Recompute the expected pooled composite/confidence score from the same
        # published constants the engine uses, to verify evaluate_ingredient's
        # orchestration wires per-paper weights through to the ingredient-level
        # summary correctly (rather than hardcoding hand-computed decimals).
        w1 = STUDY_TIER_WEIGHTS[StudyTier.META_ANALYSIS]
        w2 = STUDY_TIER_WEIGHTS[StudyTier.RCT] * SMALL_SAMPLE_PENALTY_MULTIPLIER * COI_PENALTY_MULTIPLIER
        w3 = STUDY_TIER_WEIGHTS[StudyTier.OBSERVATIONAL]
        overall_consensus = (1.0 * w1 + 1.0 * w2 + 0.0 * w3) / (w1 + w2 + w3)
        expected_composite = round((overall_consensus + 1.0) / 2.0 * 100.0, 2)
        expected_confidence = round(((w1 + w2 + w3) / 3) * min(1.0, 3 / 10), 4)

        assert result.evidence_summary.composite_score == pytest.approx(expected_composite)
        assert result.evidence_summary.overall_confidence_score == pytest.approx(expected_confidence)


class TestConfiguration:
    def test_missing_api_key_raises_configuration_error(self):
        settings = Settings(_env_file=None, ncbi_entrez_email="test@example.com", gemini_api_key=None)
        with pytest.raises(ConsensusEngineError, match="GEMINI_API_KEY is missing or invalid."):
            ConsensusEngine(settings=settings)

    def test_uses_default_model_when_not_overridden(self, settings_with_gemini_key):
        engine = ConsensusEngine(settings=settings_with_gemini_key)
        assert engine._model == "gemini-2.5-flash"

    def test_model_override_is_respected(self, settings_with_gemini_key):
        engine = ConsensusEngine(settings=settings_with_gemini_key, model="gemini-1.5-flash")
        assert engine._model == "gemini-1.5-flash"


@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai SDK not installed")
class TestGeminiIntegration:
    """Exercises the real _evaluate_papers_with_llm code path with the SDK client mocked (no network)."""

    def test_parses_structured_json_response(self, settings_with_gemini_key):
        from google import genai

        papers = [make_paper("1", sample_size=100)]
        batch = PaperEvaluationBatch(evaluations=[make_evaluation("1", StudyTier.RCT, 1.0, claim="Test Claim")])
        mock_response = MagicMock(text=batch.model_dump_json())

        engine = ConsensusEngine(settings=settings_with_gemini_key)

        with patch.object(genai, "Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

            result = run(engine.evaluate_ingredient("ing_x", "X", papers))

        assert result.evidence_summary.total_papers_analyzed == 1
        assert result.validated_claims[0].claim == "Test Claim"
        mock_client.aio.models.generate_content.assert_called_once()
