"""Pydantic response models for the Phase 2/3/5 research/grading
endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# Only imported for the ACTIVE default below — these are plain string
# constants (not ORM/DB machinery), so importing them here doesn't pull
# app.schemas.research into any SQLModel/SQLAlchemy dependency chain.
from app.models.research import PAPER_STATUS_ACTIVE


class RubricEvaluationResponse(BaseModel):
    """Structured per-category breakdown backing a paper's `grade`/
    `grade_score` — mirrors app/services/paper_grader.py's
    RubricEvaluation shape (and, in turn,
    ResearchPaper.rubric_evaluation's stored JSON) field-for-field, so a
    dict straight off the DB row validates into this without any
    reshaping. Rendered by the frontend's Rubric Breakdown modal (see
    `src/components/StudiesList.tsx`) — one field pair per rubric
    category (docs/paper_grading_rubric.json) plus an overall summary.
    """

    study_type: str
    study_type_score: int
    journal_reputation: str
    journal_score: int
    sample_info: str
    sample_score: int
    funding_status: str
    funding_score: int
    total_score: int
    summary_notes: str


class ResearchPaperResponse(BaseModel):
    """A single stored ResearchPaper row (app/models/research.py), as
    returned to the frontend for the "List of Studies" panel on a
    standalone IngredientCard.

    Mirrors the ResearchPaper table columns directly — no derived/
    reshaped fields — so the frontend's StudiesList component can render
    straight off this shape (see src/components/StudiesList.tsx).
    """

    id: int
    title: str
    abstract: Optional[str] = None
    authors: Optional[str] = None
    publication_date: Optional[str] = None
    source_url: str
    source_domain: str
    ingredient_id: int
    # Every Gemini-generated search keyword that surfaced this paper
    # (app/models/research.py::ResearchPaper.keywords, parsed from its
    # stored comma-separated string form via parse_keywords()). Rendered
    # as "Matched Keywords" pill tags in the frontend's paper info modal
    # (src/components/StudiesList.tsx).
    keywords: List[str] = Field(default_factory=list)
    # --- Phase 3: automated paper grading (app/services/paper_grader.py) ---
    # All three are None until search_papers_for_ingredient() successfully
    # grades this paper (best-effort — a Gemini/parsing failure at grade
    # time leaves a paper permanently ungraded rather than retrying), so
    # the frontend must handle a null `grade` (no badge rendered) as a
    # normal, expected state, not an error.
    grade: Optional[str] = None
    grade_score: Optional[int] = None
    rubric_evaluation: Optional[RubricEvaluationResponse] = None
    # --- Phase 6: ingredient relevance verification
    # (app/services/paper_grader.py) ---
    # One of PAPER_STATUS_ACTIVE / PAPER_STATUS_DISCARDED_IRRELEVANT
    # (app/models/research.py). In practice the frontend never actually
    # receives a DISCARDED_IRRELEVANT paper through the normal list
    # endpoints — app/services/search.py::get_ingredient_papers filters
    # those out server-side — but the on-demand single-paper-grade
    # endpoint (POST /api/v1/papers/{paper_id}/grade,
    # GradePaperResponse.paper below) returns the just-graded paper
    # regardless of its outcome, so the frontend needs this field to
    # detect and remove a just-discarded paper from local state — see
    # src/components/IngredientCard.tsx::handlePaperGraded. Defaults to
    # PAPER_STATUS_ACTIVE (not Optional) since every ResearchPaper row
    # always has a non-null `status` — see that column's own docstring.
    status: str = PAPER_STATUS_ACTIVE
    # --- Phase 19: extracted conclusions
    # (app/services/paper_grader.py::grade_paper) ---
    # Mirrors ResearchPaper.extracted_conclusions directly — see that
    # column's docstring in app/models/research.py. None until this paper
    # is graded (same convention as grade/grade_score/rubric_evaluation
    # above); the frontend's paper info modal
    # (src/components/StudiesList.tsx) renders a "No specific conclusions
    # extracted for this source yet." fallback for that case.
    extracted_conclusions: Optional[List[str]] = None


class PaperConclusionResponse(BaseModel):
    """A single synthesized cross-paper conclusion/claim for an
    ingredient (Phase 5) — mirrors PaperConclusion
    (app/models/research.py) field-for-field. See
    app/services/conclusion_grader.py for how these are built and kept
    up to date as more graded papers come in.

    `rubric_evaluation` is a loose `Dict[str, Any]` rather than a strict
    submodel (unlike ResearchPaperResponse.rubric_evaluation, which uses
    RubricEvaluationResponse) — a merged conclusion's stored evaluation
    is built by spreading a previous dict and overwriting a few keys
    (see conclusion_grader.py::process_paper_conclusions), so its exact
    key set is less rigidly fixed than a freshly-graded paper's; a loose
    dict tolerates that without risking a validation error on an
    otherwise-fine row.
    """

    id: int
    ingredient_id: int
    claim_summary: str
    detailed_conclusion: Optional[str] = None
    dosage_mentioned: Optional[str] = None
    rubric_evaluation: Optional[Dict[str, Any]] = None
    confidence_score: int
    confidence_grade: str
    cross_paper_consensus: int
    supporting_paper_ids: List[int] = Field(default_factory=list)
    contradicting_paper_ids: List[int] = Field(default_factory=list)


class AlignedConclusionResponse(BaseModel):
    """One classified entry from VerifiedResource.aligned_conclusions
    (Phase 22 — app/services/resource_aligner.py). A plain, loosely-typed
    dict on the DB/model side (see that column's docstring in
    app/models/research.py for why — same convention as
    PaperConclusion.rubric_evaluation elsewhere in this module); given its
    own schema here purely so the API response is self-documenting and
    the frontend gets real field names/types instead of `dict`.
    """

    text: str
    alignment: str
    target_claim: Optional[str] = None
    notes: Optional[str] = None


class VerifiedResourceResponse(BaseModel):
    """A single stored VerifiedResource row (app/models/research.py,
    Phase 7/8) — mirrors that table's columns directly, field-for-field,
    so the frontend's `VerifiedResourcesList` component can render
    straight off this shape (see `src/components/VerifiedResourcesList.tsx`).

    Every row this endpoint returns has already cleared the backend's
    strict domain allow-list at fetch time (see
    app/services/resource_fetcher.py::_is_verified_domain) — the frontend
    never needs to re-validate `domain` itself, only display it (and
    derive an "NIH"/"USDA"/"EFSA"-style authority badge from it).

    `grade`/`score`/`reasoning_summary` (Phase 8 — see
    app/services/resource_grader.py) are a separate quality signal on top
    of that domain gate — all three are `None` until
    app/services/resource_fetcher.py successfully grades this resource
    (best-effort at fetch time — a Gemini/parsing failure leaves a
    resource permanently ungraded rather than retried, same convention as
    ResearchPaper.grade), so the frontend must handle a null `grade` (no
    badge rendered) as a normal, expected state, not an error.
    """

    id: int
    ingredient_id: int
    title: str
    publisher: str
    url: str
    domain: str
    summary: Optional[str] = None
    grade: Optional[str] = None
    score: Optional[int] = None
    reasoning_summary: Optional[str] = None
    # --- Phase 19: extracted conclusions
    # (app/services/resource_extractor.py::extract_claims_from_resource) ---
    # Mirrors VerifiedResource.extracted_conclusions directly — see that
    # column's docstring in app/models/research.py for why it's kept
    # separate from extracted_data. None until Stage 1 extraction runs for
    # this resource; the frontend's resource info modal
    # (src/components/VerifiedResourcesList.tsx) renders a "No specific
    # conclusions extracted for this source yet." fallback for that case.
    extracted_conclusions: Optional[List[str]] = None
    # --- Phase 20: extraction failure reason
    # (app/services/resource_extractor.py::extract_claims_from_resource) ---
    # Mirrors VerifiedResource.extraction_failure_reason directly. `None`
    # whenever `extracted_conclusions` is non-empty, OR when Stage 1
    # extraction simply hasn't run yet for this resource — a real,
    # short explanatory string whenever an attempt was made and came back
    # empty (see that column's own docstring in app/models/research.py
    # for the full list of possible reasons). The frontend's resource
    # info modal (src/components/VerifiedResourcesList.tsx) renders this
    # inside a highlighted notice box when `extracted_conclusions` is
    # empty/null, falling back to a generic message if this is also null.
    extraction_failure_reason: Optional[str] = None
    # --- Phase 22: claim alignment / cross-referencing
    # (app/services/resource_aligner.py::align_resource_conclusions_for_ingredient) ---
    # Mirrors VerifiedResource.aligned_conclusions directly — one entry
    # per string in extracted_conclusions above, in the same order, each
    # classifying how that specific conclusion relates to this
    # ingredient's existing paper evidence. See that column's own
    # docstring in app/models/research.py for the full per-item shape
    # explanation and the "None = not yet classified, [] = classified but
    # this resource had zero extracted_conclusions to classify" states.
    # The frontend's resource info modal
    # (src/components/VerifiedResourcesList.tsx) renders a colored badge
    # per item (green Agrees / red Contradicts / blue Distinct-New).
    aligned_conclusions: Optional[List[AlignedConclusionResponse]] = None


class ScientificConclusionScoreBreakdown(BaseModel):
    """The four Multi-Source Confidence Rubric category scores backing
    one `ScientificConclusionResponse.total_score`/`confidence_grade`
    (Phase 23 — docs/multi_source_confidence_rubric.json,
    app/services/conclusion_grader.py::synthesize_ingredient_summary).
    Mirrors `Ingredient.scientific_conclusions[].score_breakdown` directly
    — see that column's docstring in app/models/supplement.py for the
    scoring/derivation reasoning.

    Phase 24: renamed from `RecommendedUseScoreBreakdown` — task:
    "Rename recommended_uses fields and models to scientific_conclusions".
    """

    paper_evidence_quality: int
    official_authority_backing: int
    multi_source_consensus: int
    claim_specificity: int


class ScientificConclusionResponse(BaseModel):
    """One synthesized, rubric-scored scientific conclusion claim (Phase
    23, renamed Phase 24) — mirrors one entry of
    `Ingredient.scientific_conclusions` directly, given its own schema
    here purely so the API response is self-documenting and the frontend
    gets real field names/types instead of `dict`, same convention as
    `AlignedConclusionResponse` above.

    `confidence_grade`/`total_score` are always server-derived (never
    Gemini's own pick) — see this column's own docstring in
    app/models/supplement.py for the full scoring pipeline, including
    Phase 24's Direct Injection Safety Net (`synthesize_ingredient_summary`'s
    own docstring) that guarantees every claim ends up represented in
    this array whether Gemini synthesized it or Python force-appended it.

    Phase 24: renamed from `RecommendedUseResponse` — same task/reasoning
    as `ScientificConclusionScoreBreakdown` above. The task's literal
    example name (`ScientificConclusionsResponse`, plural, one wrapper
    class) doesn't fit this codebase's established convention of a
    response model per LIST ITEM with the array itself named after the
    field it lives on (see `PaperConclusionResponse`/
    `VerifiedResourceResponse`/`AlignedConclusionResponse` above, all
    singular-item classes) — this class follows that existing pattern
    instead, a deliberate, documented deviation from the task's literal
    class name.
    """

    claim: str
    confidence_grade: str
    total_score: int
    score_breakdown: ScientificConclusionScoreBreakdown
    supporting_study_count: int = 0
    supporting_resource_count: int = 0
    sources_summary: List[str] = Field(default_factory=list)
    grade_justification: str


class GeneralInfoFieldResponse(BaseModel):
    """One resolved field (`description` or `daily_dosage`) of Phase 33's
    General Information feature — mirrors
    `app/services/general_info_extractor.py::GeneralInfoFieldResult`
    field-for-field, given its own schema here purely so the API response
    is self-documenting, same convention as `AlignedConclusionResponse`/
    `ScientificConclusionResponse` above.

    `is_available=False` (with every other field `None`) is a real,
    legitimate result, not an error or "not loaded yet" state — the
    frontend renders a fixed notice ("No high-grade (Grade A or B) source
    available containing this information.") for that case. `source_grade`
    is never anything other than "A"/"B"/`None` — see
    `general_info_extractor.py`'s module docstring for how Grade C/D/E
    sources are excluded before Gemini ever sees them, not merely
    filtered after the fact.
    """

    text: Optional[str] = None
    source_name: Optional[str] = None
    source_type: Optional[str] = None
    source_grade: Optional[str] = None
    is_available: bool = False


class GeneralInfoResponse(BaseModel):
    """Phase 33 — mirrors `Ingredient.general_info` (app/models/supplement.py)
    directly: always both fields, each independently resolved or marked
    unavailable. See `GeneralInfoFieldResponse`'s own docstring for the
    per-field shape.
    """

    description: GeneralInfoFieldResponse
    daily_dosage: GeneralInfoFieldResponse


class IngredientDetailResponse(BaseModel):
    """Response body for GET /api/v1/ingredients/{id}.

    A single canonical Ingredient plus its full list of stored
    ResearchPaper rows — added so a standalone IngredientCard can load
    its "List of Studies" panel (and current grade state) without first
    requiring a POST .../grade call. Grading (see GradeIngredientResponse
    below) still returns the same `papers` shape so the panel can update
    immediately after a fresh grade request too, without a second round
    trip.

    `conclusions` (Phase 5) is every *active* synthesized
    PaperConclusion for this ingredient, highest-confidence first — see
    app/services/search.py::get_ingredient_conclusions. `verified_resources`
    (Phase 7) is every stored VerifiedResource for this ingredient — see
    app/services/search.py::get_ingredient_resources. Neither is yet
    returned by GradeIngredientResponse/GradePaperResponse below (those
    still only refresh `papers`) — the frontend re-fetches ingredient
    detail to see updated conclusions/verified resources after a grade
    request, same as it does today for any state this endpoint alone
    exposes.
    """

    id: int
    name: str
    recommended_daily_dosage: Optional[str] = "x"
    scientific_data: Optional[str] = "n/a"
    product_count: int = 0
    is_graded: bool = False
    grade_badge_text: Optional[str] = None
    # Phase 11 — see app/models/supplement.py::Ingredient.summary_description
    # and app/services/conclusion_grader.py::synthesize_ingredient_summary.
    # None until a grade request has both evidence to synthesize from and
    # a successful Gemini call — the frontend falls back to a
    # client-computed heuristic sentence in that case (see
    # src/components/IngredientCard.tsx's `scientificSummary`).
    summary_description: Optional[str] = None
    # Phase 23, renamed Phase 24 — see
    # app/models/supplement.py::Ingredient.scientific_conclusions and
    # app/services/conclusion_grader.py::synthesize_ingredient_summary's
    # module-docstring "Phase 23"/"Phase 24" paragraphs for the
    # Multi-Source Confidence Rubric each claim is scored against and the
    # Direct Injection Safety Net that guarantees every parsed
    # VerifiedResource conclusion ends up represented here. Empty list
    # (not None) both before any synthesis has run and after a synthesis
    # that genuinely found no specific claim to recommend — the frontend
    # doesn't need to distinguish those two cases differently from how it
    # already treats an empty conclusions/verified_resources list.
    scientific_conclusions: List[ScientificConclusionResponse] = Field(default_factory=list)
    papers: List[ResearchPaperResponse] = Field(default_factory=list)
    conclusions: List[PaperConclusionResponse] = Field(default_factory=list)
    verified_resources: List[VerifiedResourceResponse] = Field(default_factory=list)
    # Phase 33 — see app/models/supplement.py::Ingredient.general_info and
    # app/services/general_info_extractor.py::extract_general_info.
    # `None` until a grade request has run this extraction at least once
    # (same "None = not attempted yet" convention as `summary_description`
    # above) — the frontend's General Information cards render a "not
    # generated yet" state for that case, distinct from a genuinely
    # resolved-but-unavailable field (`is_available=False` inside a
    # present `general_info`). Not yet included in GradeIngredientResponse
    # below — same "frontend re-fetches ingredient detail to see it"
    # caveat as `conclusions`/`verified_resources`/`scientific_conclusions`.
    general_info: Optional[GeneralInfoResponse] = None


class GradeIngredientResponse(BaseModel):
    """Response body for POST /api/v1/ingredients/{id}/grade."""

    status: str
    ingredient_id: int
    is_graded: bool
    grade_badge_text: Optional[str] = None
    papers_found: int
    # Full paper list (not just the count) so the frontend's StudiesList
    # can refresh immediately after grading, without a follow-up
    # GET /api/v1/ingredients/{id} call.
    papers: List[ResearchPaperResponse] = Field(default_factory=list)


class GradePaperResponse(BaseModel):
    """Response body for POST /api/v1/papers/{paper_id}/grade — on-demand
    single-paper grading, triggered by tapping a gray "(-)" ungraded
    badge in the frontend's StudiesList (see
    app/services/paper_grader.py::grade_single_paper). `paper` is the
    full updated row (already-graded papers are returned unchanged,
    since grading is idempotent — see that function's docstring), using
    the same `ResearchPaperResponse` shape as every other paper-bearing
    endpoint so the frontend can splice it into local state without any
    reshaping.
    """

    status: str
    paper: ResearchPaperResponse
