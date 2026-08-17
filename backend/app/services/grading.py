"""Orchestrates the ingredient-grading pipeline:

  1. Gemini generates search keywords for the ingredient's name
     (app/services/research_keywords.py).
  2. Those keywords are used to query PubMed / Europe PMC / Semantic
     Scholar / OpenAlex and persist new ResearchPaper rows
     (app/services/paper_search.py) — search-only, committed immediately
     (see below) so newly-found papers are durable before step 3 runs.
  2b. The official government/regulatory APIs configured in
     docs/verified_resource_apis.json are queried by the ingredient's own
     name and persist new VerifiedResource rows
     (app/services/resource_fetcher.py, Phase 7) — independent of steps 1/2
     (no Gemini call, no keywords needed), so a failure here is caught and
     logged rather than failing the whole grade request; see below.
  3. Every stored ResearchPaper for this ingredient (new ones just
     persisted, plus any left ungraded/unsynthesized by an earlier,
     partially-failed run) is graded and — where its grade clears the
     conclusion-synthesis threshold — has its findings merged into the
     ingredient's PaperConclusion set, one paper at a time
     (app/services/paper_analysis_pipeline.py::analyze_ingredient_papers,
     Phase 5). That function never raises for a single paper's failure,
     so a rate limit or transient error partway through this step still
     leaves everything completed so far durably saved.
  4. `is_graded=True` and `grade_badge_text` are set to the total stored
     paper count, formatted three times (e.g. "14 / 14 / 14") — a
     **debug-only** placeholder badge, not derived from the Phase 5
     conclusion confidence grades yet.

Kept as its own service (rather than inlined in the route) so the whole
pipeline can be unit-tested / reused without going through FastAPI, same
reasoning as app/services/storage.py::save_scan being separate from the
/scan route.
"""

from __future__ import annotations

import logging

from sqlmodel import Session, func, select

from app.models.research import ResearchPaper
from app.models.supplement import Ingredient
from app.services.paper_analysis_pipeline import analyze_ingredient_papers
from app.services.paper_search import search_papers_for_ingredient
from app.services.research_keywords import (
    KeywordGenerationError,
    generate_ingredient_keywords,
)
from app.services.resource_fetcher import fetch_verified_resources_for_ingredient

logger = logging.getLogger(__name__)


class GradingError(RuntimeError):
    """Raised when the grading pipeline can't produce a result at all."""


def grade_ingredient(session: Session, ingredient: Ingredient) -> int:
    """Runs the full grading pipeline for `ingredient` — see module
    docstring for the steps.

    Unlike the old single-transaction version, this now commits twice
    before its own final commit: once right after paper search and
    verified-resource lookup (so newly found papers/resources survive
    even if step 3 fails entirely), and again,
    internally, after each paper processed by
    analyze_ingredient_papers's grade/conclusion-synthesis steps (see
    that module and app/services/paper_grader.py /
    app/services/conclusion_grader.py) — deliberately, so partial
    progress from rate limiting or a transient failure mid-loop is never
    rolled back.

    Args:
        session: An open SQLModel session.
        ingredient: The Ingredient row to grade (already fetched by the
            caller — this function doesn't look it up).

    Returns:
        The total paper count now stored for this ingredient (not just
        newly-added ones from this call).

    Raises:
        GradingError: if Gemini keyword generation fails outright, the
            post-search commit fails, or the final commit fails.
            Paper-search failures for individual sources/keywords (see
            paper_search.py's `_safe_query_async`) and per-paper
            grading/conclusion-synthesis failures (see
            analyze_ingredient_papers) are all handled internally and
            never raise — a partial result is preferred over failing the
            whole request because one source, one paper, or one Gemini
            call hiccupped.
    """
    try:
        keywords = generate_ingredient_keywords(ingredient.name)
    except KeywordGenerationError as exc:
        raise GradingError(
            f"Keyword generation failed for '{ingredient.name}': {exc}"
        ) from exc

    # Persists new (deduplicated) ResearchPaper rows — flushed, not
    # committed yet (see that function's docstring for why).
    search_papers_for_ingredient(session, ingredient.id, keywords)

    # Persists new (deduplicated) VerifiedResource rows from the official
    # government/regulatory APIs (Phase 7 — see
    # app/services/resource_fetcher.py) — also flushed, not committed yet,
    # so it lands in the same first commit as the newly found papers just
    # below. Independent of paper search/grading (no Gemini call, no
    # shared state), so — unlike keyword generation above, which fails the
    # whole request — a failure here is caught and logged: a resource-
    # lookup hiccup shouldn't take down grading itself, same "one
    # subsystem's failure isn't everyone's failure" philosophy as
    # paper-search's own per-source error handling
    # (paper_search.py::_safe_query_async).
    try:
        fetch_verified_resources_for_ingredient(session, ingredient.id, ingredient.name)
    except Exception as exc:  # noqa: BLE001 - see comment above
        logger.warning(
            "Verified resource fetch failed for ingredient %r (id=%s): %s",
            ingredient.name,
            ingredient.id,
            exc,
        )

    # Commit now, before the grade/conclusion-synthesis loop starts, so
    # the newly found papers (and any newly found verified resources) are
    # durable regardless of what happens in step 3 below (which does its
    # own per-paper commits from here on).
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        raise GradingError(f"Failed to save newly found papers: {exc}") from exc

    # Grades every stored paper for this ingredient (idempotent — a
    # no-op for already-graded ones), relevance-checks each against
    # `ingredient.name` and discards off-topic ones (Phase 6), and
    # synthesizes/merges conclusions for any that clear the quality
    # threshold — see module docstring.
    analyze_ingredient_papers(session, ingredient.id, ingredient.name)

    paper_count = session.exec(
        select(func.count())
        .select_from(ResearchPaper)
        .where(ResearchPaper.ingredient_id == ingredient.id)
    ).one()

    ingredient.is_graded = True
    ingredient.grade_badge_text = f"{paper_count} / {paper_count} / {paper_count}"
    session.add(ingredient)

    try:
        session.commit()
        session.refresh(ingredient)
    except Exception as exc:
        session.rollback()
        raise GradingError(f"Failed to save grading result: {exc}") from exc

    return paper_count
