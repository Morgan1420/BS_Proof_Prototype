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
    papers: List[ResearchPaperResponse] = Field(default_factory=list)
    conclusions: List[PaperConclusionResponse] = Field(default_factory=list)
    verified_resources: List[VerifiedResourceResponse] = Field(default_factory=list)


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
