"""Pydantic schemas for Phase 2: Standardized Ingredient Grade Schema (SIFG).

Mirrors the SIFG JSON schema defined in ``docs/Architecture.md`` (Phase 2).
An ``IngredientGradeSchema`` instance is the cached, consensus-scored
output of the PubMed retrieval -> LLM Paper Evaluator -> Consensus Engine
pipeline for a single ingredient, stored globally to avoid redundant
re-grading across products.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class EvidenceGrade(str, Enum):
    """Overall letter grade summarizing evidence quality for an ingredient."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"


class EvidenceLevel(str, Enum):
    """Per-claim evidence strength, derived from the Risk of Bias & Quality Weighting Matrix."""

    HIGH = "High"
    MODERATE = "Moderate"
    LOW = "Low"


class SafetyRating(str, Enum):
    """Overall safety classification for an ingredient."""

    SAFE = "Safe"
    CAUTION = "Caution"
    UNSAFE = "Unsafe"


class EvidenceSummary(BaseModel):
    """Aggregate evidence statistics across all papers analyzed for an ingredient.

    Matches the ``evidence_summary`` object in the Architecture.md SIFG schema.
    """

    total_papers_analyzed: int = Field(
        ..., ge=0, description="Count of papers retrieved and evaluated from PubMed (top N per Phase 2)."
    )
    composite_score: float = Field(
        ...,
        ge=0,
        le=100,
        description="Final SIFG composite score (0-100), produced by the Consensus Engine from "
        "tier-weighted, rigor-adjusted directional consensus across all evaluated papers. "
        "Added in Step 4; not present in the original Architecture.md example.",
    )
    evidence_grade: EvidenceGrade = Field(
        ..., description="Overall evidence quality grade (A-F), thresholded from composite_score."
    )
    overall_confidence_score: float = Field(
        ..., ge=0, le=1, description="Aggregate confidence score in the evidence base, 0.0-1.0."
    )


class DosageBenchmarks(BaseModel):
    """Dose thresholds synthesized from the evidence base.

    Matches the ``dosage_benchmarks`` object in the Architecture.md SIFG schema.
    """

    minimum_effective_dose_mg: float = Field(
        ..., ge=0, description="Lowest dose shown across studies to produce a measurable effect."
    )
    optimal_dose_range_mg: str = Field(
        ..., description="Human-readable optimal dose range, e.g. '600-900'."
    )
    maximum_safe_daily_dose_mg: float = Field(
        ..., ge=0, description="Upper safety bound for daily intake."
    )
    unit: str = Field(default="mg", description="Unit applying to the dosage fields above.")


class ValidatedClaim(BaseModel):
    """A single efficacy/safety claim and its consensus score across studies.

    Matches one entry of the ``validated_claims`` array in the
    Architecture.md SIFG schema. ``consensus_score`` follows the
    Consensus Score Formula: sum(directional_value * paper_weight) /
    sum(paper_weight), bounded to [-1.0, 1.0].
    """

    claim: str = Field(..., description="Short description of the claim, e.g. 'Stress & Anxiety Reduction'.")
    consensus_score: float = Field(
        ..., ge=-1.0, le=1.0, description="Weighted consensus score per the Consensus Score Formula."
    )
    evidence_level: EvidenceLevel = Field(..., description="Strength of evidence supporting this claim.")
    supporting_studies_count: int = Field(
        ..., ge=0, description="Number of studies contributing to this claim's consensus score."
    )


class SafetyAndSideEffects(BaseModel):
    """Safety profile synthesized from the evidence base.

    Matches the ``safety_and_side_effects`` object in the Architecture.md SIFG schema.
    """

    safety_rating: SafetyRating = Field(..., description="Overall safety classification.")
    known_interactions: List[str] = Field(
        default_factory=list, description="Known drug/supplement interactions, e.g. ['Thyroid Medications']."
    )
    common_side_effects: List[str] = Field(
        default_factory=list, description="Commonly reported side effects, e.g. ['Mild Drowsiness']."
    )


class IngredientGradeSchema(BaseModel):
    """Standardized Ingredient Grade Schema (SIFG).

    The cached, per-ingredient output of Phase 2. Stored globally in the
    DB cache (see Architecture.md Phase 2 flow: "Store SIFG JSON in
    Database Cache") and reused across products so ingredients are never
    re-graded once evaluated.

    ``dosage_benchmarks`` and ``safety_and_side_effects`` are nullable:
    the Consensus Engine (Step 4) only computes ``evidence_summary`` and
    ``validated_claims``. Dose-response and adverse-event extraction is a
    separate, not-yet-implemented step. These fields are ``None`` rather
    than fabricated placeholder numbers until that step exists --
    inventing a "maximum_safe_daily_dose_mg" would be actively dangerous
    in a supplement-safety application.
    """

    ingredient_id: str = Field(..., description="Canonical ingredient identifier, e.g. 'ing_ashwagandha_01'.")
    canonical_name: str = Field(..., description="Scientific/canonical name, e.g. 'Withania Somnifera'.")
    evidence_summary: EvidenceSummary
    dosage_benchmarks: Optional[DosageBenchmarks] = Field(
        default=None, description="None until dose-response extraction (not yet implemented) has run."
    )
    validated_claims: List[ValidatedClaim] = Field(default_factory=list)
    safety_and_side_effects: Optional[SafetyAndSideEffects] = Field(
        default=None, description="None until adverse-event extraction (not yet implemented) has run."
    )
