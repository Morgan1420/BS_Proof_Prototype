"""Orchestrates the Phase 2 ingredient-grading pipeline:

  1. Gemini generates search keywords for the ingredient's name
     (app/services/research_keywords.py).
  2. Those keywords are used to query PubMed / Europe PMC / Semantic
     Scholar and persist new ResearchPaper rows
     (app/services/paper_search.py).
  3. A **debug-only** grade is assigned: `is_graded=True` and
     `grade_badge_text` set to the total stored paper count, formatted
     three times (e.g. "14 / 14 / 14") — there is no real grading
     algorithm yet; this exists purely so the frontend badge has
     something derived from real data to show while that's built out.

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
from app.services.paper_search import search_papers_for_ingredient
from app.services.research_keywords import (
    KeywordGenerationError,
    generate_ingredient_keywords,
)

logger = logging.getLogger(__name__)


class GradingError(RuntimeError):
    """Raised when the grading pipeline can't produce a result at all."""


def grade_ingredient(session: Session, ingredient: Ingredient) -> int:
    """Runs the full grading pipeline for `ingredient` and commits the
    result (any new ResearchPaper rows + the updated
    is_graded/grade_badge_text) as a single transaction.

    Args:
        session: An open SQLModel session.
        ingredient: The Ingredient row to grade (already fetched by the
            caller — this function doesn't look it up).

    Returns:
        The total paper count now stored for this ingredient (not just
        newly-added ones from this call).

    Raises:
        GradingError: if Gemini keyword generation fails outright, or the
            final commit fails. Paper-search failures for individual
            sources/keywords are handled internally (see
            paper_search.py's `_safe_query`) and never raise — a partial
            result (fewer papers than ideal) is preferred over failing
            the whole request because one source hiccupped.
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
