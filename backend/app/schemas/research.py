"""Pydantic response models for the Phase 2/3 research/grading endpoints."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


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


class IngredientDetailResponse(BaseModel):
    """Response body for GET /api/v1/ingredients/{id}.

    A single canonical Ingredient plus its full list of stored
    ResearchPaper rows — added so a standalone IngredientCard can load
    its "List of Studies" panel (and current grade state) without first
    requiring a POST .../grade call. Grading (see GradeIngredientResponse
    below) still returns the same `papers` shape so the panel can update
    immediately after a fresh grade request too, without a second round
    trip.
    """

    id: int
    name: str
    recommended_daily_dosage: Optional[str] = "x"
    scientific_data: Optional[str] = "n/a"
    product_count: int = 0
    is_graded: bool = False
    grade_badge_text: Optional[str] = None
    papers: List[ResearchPaperResponse] = Field(default_factory=list)


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
