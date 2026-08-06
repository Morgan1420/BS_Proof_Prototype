"""Phase 2 Consensus Evaluation & SIFG Scoring Engine.

Takes the PubMed literature retrieved for an ingredient (see
``app.services.pubmed_service.PubMedService``) and produces the
evidence-scoring portion of its Standardized Ingredient Grade Schema
(SIFG): the "LLM Paper Evaluator -> Consensus Engine" steps in
docs/Architecture.md (Phase 2).

Two distinct stages, deliberately kept separate for testability:
    1. LLM evaluation (Gemini): classifies each paper's study design
       tier, primary claim, directional value, and conflict-of-interest
       status. This is the only part of this module that talks to the
       network -- everything below it is pure, synchronous math.
    2. Scoring math: applies the Risk of Bias & Quality Weighting Matrix
       (tier weights) and Rigor Modifiers (sample-size and COI
       penalties) to compute per-claim consensus scores and an overall
       composite score / letter grade / confidence score. See
       docs/Architecture.md, Step 4 implementation note, for the exact
       formulas and which constants are our own calibration choices
       (the spec fixes tier weights and the COI penalty; it does not
       specify the sample-size penalty magnitude, grade bands, evidence
       level thresholds, or the confidence-score formula).

Scope: this engine does NOT compute ``dosage_benchmarks`` or
``safety_and_side_effects`` (see app.schemas.ingredient_grade) -- those
require separate dose-response / adverse-event extraction not performed
here. Its output, ``ConsensusEvaluationResult``, covers only
``evidence_summary`` and ``validated_claims``; assembling a complete
``IngredientGradeSchema`` means combining this with that future step's
output.

Per CLAUDE.md's "Asynchronous Execution" standard, the Gemini call is
async so it can be awaited from a FastAPI async background job / task
queue at the API layer (not implemented in this step).
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.schemas.ingredient_grade import (
    EvidenceGrade,
    EvidenceLevel,
    EvidenceSummary,
    ValidatedClaim,
)
from app.services.pubmed_service import PubMedPaper

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


class StudyTier(str, Enum):
    """Study design tiers per docs/Architecture.md's Risk of Bias & Quality Weighting Matrix."""

    META_ANALYSIS = "meta_analysis"  # Tier 1 (1.0x): Meta-Analyses & Systematic Reviews
    RCT = "rct"  # Tier 2 (0.85x): (Double-Blind) Randomized Controlled Trials
    OBSERVATIONAL = "observational"  # Tier 3 (0.50x): Open-Label / Cohort / Observational human studies
    ANIMAL = "animal"  # Tier 4 (0.15x): Animal Models (in-vivo)
    IN_VITRO = "in_vitro"  # Tier 5 (0.05x): Cell Culture (in-vitro)


STUDY_TIER_WEIGHTS: Dict[StudyTier, float] = {
    StudyTier.META_ANALYSIS: 1.00,
    StudyTier.RCT: 0.85,
    StudyTier.OBSERVATIONAL: 0.50,
    StudyTier.ANIMAL: 0.15,
    StudyTier.IN_VITRO: 0.05,
}

# Rigor Modifiers (docs/Architecture.md Phase 2, Section 1).
SMALL_SAMPLE_SIZE_THRESHOLD = 30
# Architecture.md specifies only that a penalty applies for n < 30, not its magnitude.
# 0.75x (a 25% reduction) is our calibration -- see docs/Architecture.md Step 4 note.
SMALL_SAMPLE_PENALTY_MULTIPLIER = 0.75
# Architecture.md specifies this exactly: "-30% penalty if directly funded by
# ingredient patent-holder/brand."
COI_PENALTY_MULTIPLIER = 0.70

# Per-claim evidence_level thresholds (our calibration; on the group's average W_paper).
EVIDENCE_LEVEL_HIGH_AVG_WEIGHT = 0.75
EVIDENCE_LEVEL_MODERATE_AVG_WEIGHT = 0.40

# Composite-score (0-100) -> letter grade bands (our calibration; conventional grading bands).
GRADE_BANDS: List[Tuple[float, EvidenceGrade]] = [
    (90.0, EvidenceGrade.A),
    (80.0, EvidenceGrade.B),
    (70.0, EvidenceGrade.C),
    (60.0, EvidenceGrade.D),
]

# Confidence-score volume factor target: a "full" retrieval batch (see
# pubmed_service.DEFAULT_RETMAX) counts as maximum evidence volume.
CONFIDENCE_VOLUME_TARGET_PAPERS = 10

EVALUATION_SYSTEM_PROMPT = (
    "You are a rigorous evidence-based-medicine reviewer classifying scientific "
    "papers about a dietary supplement ingredient. For EACH paper provided, "
    "determine:\n"
    "1. study_design_tier -- classify the study design into exactly one of:\n"
    "   - meta_analysis: Meta-Analyses and Systematic Reviews\n"
    "   - rct: Randomized Controlled Trials (blinded or open-label human RCTs)\n"
    "   - observational: Open-label, cohort, or other observational human studies\n"
    "   - animal: Animal / in-vivo models\n"
    "   - in_vitro: Cell culture / in-vitro studies\n"
    "2. primary_claim -- a short (3-6 word) label for the main efficacy or safety "
    "claim this paper investigates for the ingredient, e.g. 'Stress & Anxiety "
    "Reduction'. Use consistent, comparable wording across papers studying the "
    "same claim so results can be grouped together.\n"
    "3. directional_value -- exactly -1.0 if the paper's findings are "
    "adverse/negative for the ingredient, 0.0 if neutral/inconclusive/mixed, or "
    "1.0 if the findings are positive/supportive. Use only these three values.\n"
    "4. has_conflict_of_interest -- true only if the conflict-of-interest "
    "statement indicates funding, employment, or a financial relationship with "
    "the ingredient's manufacturer or patent holder; false if no COI is declared "
    "or the COI is unrelated (e.g. an unrelated grant).\n"
    "5. rationale -- one sentence justifying your tier and directional_value.\n\n"
    "Base every judgment only on the title, abstract, publication types, and "
    "conflict-of-interest statement provided -- do not use outside knowledge "
    "about the ingredient. Return exactly one evaluation per paper, echoing back "
    "each paper's pmid so evaluations can be matched to their source paper."
)


class PaperEvaluation(BaseModel):
    """Per-paper LLM judgment: study design tier, claim, directionality, and COI flag.

    This is (one element of) the Gemini structured-output contract for
    the "LLM Paper Evaluator -> Extract Risk of Bias & Quantitative Data"
    step. ``directional_value`` is typed as a bounded float rather than
    ``Literal[-1.0, 0.0, 1.0]`` for reliable Gemini JSON-schema
    translation; the prompt is what actually constrains it to those
    three values.
    """

    pmid: str = Field(..., description="PMID of the paper this evaluation applies to, echoed back from the input.")
    study_design_tier: StudyTier = Field(
        ..., description="Study design tier per the Risk of Bias & Quality Weighting Matrix."
    )
    primary_claim: str = Field(
        ..., description="Short label for the main efficacy/safety claim investigated, e.g. 'Stress & Anxiety Reduction'."
    )
    directional_value: float = Field(
        ...,
        ge=-1.0,
        le=1.0,
        description="-1.0 adverse, 0.0 neutral/inconclusive, or 1.0 positive/supportive (exactly one of these three).",
    )
    has_conflict_of_interest: bool = Field(
        default=False,
        description="True if the COI statement indicates funding/ties to the ingredient's manufacturer or patent holder.",
    )
    rationale: Optional[str] = Field(default=None, description="One-sentence justification for the tier/directional_value.")


class PaperEvaluationBatch(BaseModel):
    """Wrapper so a single Gemini call returns one JSON object covering all papers."""

    evaluations: List[PaperEvaluation] = Field(default_factory=list)


class ScoredPaper(BaseModel):
    """A retrieved paper combined with its LLM evaluation and final rigor-adjusted weight."""

    pmid: str
    tier: StudyTier
    tier_weight: float
    sample_size: Optional[int]
    has_conflict_of_interest: bool
    directional_value: float
    primary_claim: str
    weight: float


class ConsensusEvaluationResult(BaseModel):
    """Output of the Consensus Engine: the evidence-scoring portion of an ingredient's SIFG.

    Deliberately excludes ``dosage_benchmarks`` and
    ``safety_and_side_effects`` -- see module docstring. Callers
    assembling a full ``IngredientGradeSchema`` combine this with that
    future step's output.
    """

    ingredient_id: str = Field(..., description="Canonical ingredient identifier, e.g. 'ing_ashwagandha_01'.")
    canonical_name: str = Field(..., description="Scientific/canonical name, e.g. 'Withania Somnifera'.")
    evidence_summary: EvidenceSummary
    validated_claims: List[ValidatedClaim] = Field(default_factory=list)


class ConsensusEngineError(Exception):
    """Raised for configuration failures (e.g. missing Gemini API key).

    Retrieval/evaluation-time failures (LLM errors, malformed output) are
    caught inside ``evaluate_ingredient`` and degrade to an
    insufficient-evidence result rather than raising.
    """


class ConsensusEngine:
    """Scores an ingredient's retrieved literature into a consensus evidence summary."""

    def __init__(self, settings: Optional[Settings] = None, model: Optional[str] = None) -> None:
        """Configure the engine.

        Args:
            settings: Injected ``Settings``; defaults to ``get_settings()``.
            model: Override the Gemini model, e.g. "gemini-1.5-flash".
                Defaults to ``DEFAULT_GEMINI_MODEL`` ("gemini-2.5-flash").

        Raises:
            ConsensusEngineError: If ``GEMINI_API_KEY`` is missing/blank.
        """
        self._settings = settings or get_settings()
        self._api_key = self._validate_api_key(self._settings.gemini_api_key)
        self._model = model or DEFAULT_GEMINI_MODEL

    @staticmethod
    def _validate_api_key(api_key: Optional[str]) -> str:
        """Ensure the Gemini API key is present and non-empty (see vision_parser.py precedent)."""
        if not api_key or not api_key.strip():
            raise ConsensusEngineError("GEMINI_API_KEY is missing or invalid.")
        return api_key.strip()

    # -- Public API -----------------------------------------------------------

    async def evaluate_ingredient(
        self,
        ingredient_id: str,
        canonical_name: str,
        papers: List[PubMedPaper],
    ) -> ConsensusEvaluationResult:
        """Evaluate an ingredient's retrieved literature into a scored consensus result.

        Args:
            ingredient_id: Canonical ingredient identifier.
            canonical_name: Scientific/canonical ingredient name.
            papers: Papers retrieved via ``PubMedService.search_ingredient``.

        Returns:
            A ``ConsensusEvaluationResult``. Never raises: no papers, a
            failed/empty LLM evaluation, or evaluations that can't be
            matched back to a paper all degrade to an insufficient-
            evidence result (composite_score=0, grade=F, confidence=0,
            no validated claims) rather than raising.
        """
        if not papers:
            return self._insufficient_evidence_result(ingredient_id, canonical_name)

        try:
            evaluations = await self._evaluate_papers_with_llm(papers)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all fallback boundary
            logger.warning(
                "Gemini paper evaluation failed for %r (%s: %s); scoring with no evaluated papers.",
                canonical_name,
                type(exc).__name__,
                exc,
            )
            evaluations = []

        scored_papers = self._pair_evaluations_with_papers(papers, evaluations)
        if not scored_papers:
            return self._insufficient_evidence_result(ingredient_id, canonical_name)

        return ConsensusEvaluationResult(
            ingredient_id=ingredient_id,
            canonical_name=canonical_name,
            evidence_summary=self._compute_evidence_summary(scored_papers),
            validated_claims=self._build_validated_claims(scored_papers),
        )

    # -- Gemini call ------------------------------------------------------------

    async def _evaluate_papers_with_llm(self, papers: List[PubMedPaper]) -> List[PaperEvaluation]:
        """Call Gemini once with all papers and return the parsed per-paper evaluations."""
        from google import genai  # local import: optional dependency, only needed here
        from google.genai import types

        client = genai.Client(api_key=self._api_key)
        prompt = self._build_evaluation_prompt(papers)

        response = await client.aio.models.generate_content(
            model=self._model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                system_instruction=EVALUATION_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=PaperEvaluationBatch,
            ),
        )

        if not response.text:
            raise ConsensusEngineError("Gemini response did not include any structured output text.")
        batch = PaperEvaluationBatch.model_validate(json.loads(response.text))
        return batch.evaluations

    @staticmethod
    def _build_evaluation_prompt(papers: List[PubMedPaper]) -> str:
        blocks = []
        for paper in papers:
            blocks.append(
                "\n".join(
                    [
                        f"PMID: {paper.pmid}",
                        f"Title: {paper.title}",
                        f"Publication types (as indexed by PubMed): "
                        f"{', '.join(paper.publication_types) or 'unknown'}",
                        f"Abstract: {paper.abstract or '(no abstract available)'}",
                        f"Conflict-of-interest statement: {paper.coi_statement or '(none declared in record)'}",
                    ]
                )
            )
        joined = "\n\n---\n\n".join(blocks)
        return f"Evaluate the following {len(papers)} paper(s):\n\n{joined}"

    # -- Matching LLM output back to source papers -------------------------------

    def _pair_evaluations_with_papers(
        self,
        papers: List[PubMedPaper],
        evaluations: List[PaperEvaluation],
    ) -> List[ScoredPaper]:
        """Join evaluations to their source paper by pmid, computing each paper's final weight.

        Evaluations for an unknown pmid (the LLM hallucinated one) and
        duplicate evaluations for the same pmid (keep the first) are
        dropped rather than raising -- one bad element in the LLM's
        output shouldn't invalidate the whole batch.
        """
        papers_by_pmid = {p.pmid: p for p in papers}
        scored: List[ScoredPaper] = []
        seen_pmids = set()

        for evaluation in evaluations:
            if evaluation.pmid in seen_pmids:
                logger.warning("Duplicate Gemini evaluation for pmid=%s; keeping the first.", evaluation.pmid)
                continue
            paper = papers_by_pmid.get(evaluation.pmid)
            if paper is None:
                logger.warning("Gemini returned an evaluation for unknown pmid=%s; skipping.", evaluation.pmid)
                continue
            seen_pmids.add(evaluation.pmid)

            weight = self.compute_paper_weight(
                evaluation.study_design_tier, paper.sample_size, evaluation.has_conflict_of_interest
            )
            scored.append(
                ScoredPaper(
                    pmid=paper.pmid,
                    tier=evaluation.study_design_tier,
                    tier_weight=STUDY_TIER_WEIGHTS[evaluation.study_design_tier],
                    sample_size=paper.sample_size,
                    has_conflict_of_interest=evaluation.has_conflict_of_interest,
                    directional_value=evaluation.directional_value,
                    primary_claim=evaluation.primary_claim,
                    weight=weight,
                )
            )
        return scored

    # -- Pure scoring math (no I/O; directly unit-testable) -----------------------

    @staticmethod
    def compute_paper_weight(tier: StudyTier, sample_size: Optional[int], has_conflict_of_interest: bool) -> float:
        """Compute a single paper's rigor-adjusted evidentiary weight W_paper.

        W_paper = W_tier x (small-sample modifier) x (COI modifier), per
        docs/Architecture.md Phase 2, Section 1. A paper with unknown
        sample size is not penalized for it (regex-based extraction in
        PubMedService is best-effort and often can't detect n).
        """
        weight = STUDY_TIER_WEIGHTS[tier]
        if sample_size is not None and sample_size < SMALL_SAMPLE_SIZE_THRESHOLD:
            weight *= SMALL_SAMPLE_PENALTY_MULTIPLIER
        if has_conflict_of_interest:
            weight *= COI_PENALTY_MULTIPLIER
        return weight

    @staticmethod
    def _weighted_directional_consensus(scored: List[ScoredPaper]) -> float:
        """Consensus Score = sum(directional_value * W_paper) / sum(W_paper), per Architecture.md."""
        total_weight = sum(p.weight for p in scored)
        if total_weight <= 0:
            return 0.0
        weighted_sum = sum(p.directional_value * p.weight for p in scored)
        return max(-1.0, min(1.0, weighted_sum / total_weight))

    @staticmethod
    def _evidence_level_for_group(scored: List[ScoredPaper]) -> EvidenceLevel:
        if not scored:
            return EvidenceLevel.LOW
        avg_weight = sum(p.weight for p in scored) / len(scored)
        if avg_weight >= EVIDENCE_LEVEL_HIGH_AVG_WEIGHT:
            return EvidenceLevel.HIGH
        if avg_weight >= EVIDENCE_LEVEL_MODERATE_AVG_WEIGHT:
            return EvidenceLevel.MODERATE
        return EvidenceLevel.LOW

    @classmethod
    def _build_validated_claims(cls, scored_papers: List[ScoredPaper]) -> List[ValidatedClaim]:
        """Group scored papers by (normalized) primary_claim and score each group."""
        groups: Dict[str, List[ScoredPaper]] = {}
        display_labels: Dict[str, str] = {}
        for paper in scored_papers:
            key = paper.primary_claim.strip().lower()
            groups.setdefault(key, []).append(paper)
            display_labels.setdefault(key, paper.primary_claim.strip())

        claims = [
            ValidatedClaim(
                claim=display_labels[key],
                consensus_score=round(cls._weighted_directional_consensus(group), 4),
                evidence_level=cls._evidence_level_for_group(group),
                supporting_studies_count=len(group),
            )
            for key, group in groups.items()
        ]
        claims.sort(key=lambda c: c.supporting_studies_count, reverse=True)
        return claims

    @classmethod
    def _grade_for_score(cls, score: float) -> EvidenceGrade:
        for threshold, grade in GRADE_BANDS:
            if score >= threshold:
                return grade
        return EvidenceGrade.F

    @classmethod
    def _compute_evidence_summary(cls, scored_papers: List[ScoredPaper]) -> EvidenceSummary:
        overall_consensus = cls._weighted_directional_consensus(scored_papers)  # [-1, 1]
        composite_score = round((overall_consensus + 1.0) / 2.0 * 100.0, 2)  # [0, 100]

        avg_weight = sum(p.weight for p in scored_papers) / len(scored_papers)
        volume_factor = min(1.0, len(scored_papers) / CONFIDENCE_VOLUME_TARGET_PAPERS)
        confidence_score = round(max(0.0, min(1.0, avg_weight * volume_factor)), 4)

        return EvidenceSummary(
            total_papers_analyzed=len(scored_papers),
            composite_score=composite_score,
            evidence_grade=cls._grade_for_score(composite_score),
            overall_confidence_score=confidence_score,
        )

    @staticmethod
    def _insufficient_evidence_result(ingredient_id: str, canonical_name: str) -> ConsensusEvaluationResult:
        """Minimal, schema-valid result for "no evaluable evidence" (no papers, or LLM evaluation failed)."""
        return ConsensusEvaluationResult(
            ingredient_id=ingredient_id,
            canonical_name=canonical_name,
            evidence_summary=EvidenceSummary(
                total_papers_analyzed=0,
                composite_score=0.0,
                evidence_grade=EvidenceGrade.F,
                overall_confidence_score=0.0,
            ),
            validated_claims=[],
        )
