"""Sequential, per-paper grade + conclusion-synthesis pipeline
(Phase 5), with ingredient relevance verification (Phase 6).

Runs after app/services/paper_search.py has found/persisted new papers
for an ingredient (see app/services/grading.py::grade_ingredient) — for
every currently-*active* ResearchPaper stored for that ingredient (see
"Discard logic" below), in order:

  1. Grade it (app/services/paper_grader.py::grade_single_paper) — a
     no-op, no Gemini call, if it's already graded. The same Gemini call
     that grades also relevance-checks the paper against its ingredient
     (Phase 6) and sets `paper.status` accordingly.
  2. If the paper was just marked `PAPER_STATUS_DISCARDED_IRRELEVANT`
     (i.e. Gemini determined it isn't actually about the target
     ingredient — e.g. a Vitamin D paper found during a Vitamin C
     search), log a warning and skip straight to the next paper —
     conclusion synthesis never runs for it.
  3. Otherwise, if (and only if) its grade_score ends up > 50,
     synthesize/merge its findings into the ingredient's running
     PaperConclusion set
     (app/services/conclusion_grader.py::process_paper_conclusions).

Deliberately per-paper and sequential — never bulk/batched into fewer,
larger Gemini calls — specifically to stay within free-tier requests-
per-minute / tokens-per-day limits: many small calls spread over the
loop, rather than one huge call trying to grade+synthesize an entire
ingredient's paper set at once (which would also blow past most models'
practical context/output limits well before it blew past rate limits).

Each paper's steps are individually wrapped in their own try/except (see
analyze_ingredient_papers below) — a failure grading or synthesizing
conclusions for one paper (rate limit, transient network error,
malformed Gemini response) is logged and skipped, never allowed to
abort the loop or roll back already-committed progress on earlier
papers. Both paper_grader.grade_single_paper and
conclusion_grader.process_paper_conclusions commit their own work
independently for exactly this reason — see each module's docstring.

**Discard logic (Phase 6).** A paper marked
`PAPER_STATUS_DISCARDED_IRRELEVANT` is permanently excluded from future
pipeline runs too — the initial paper query below filters it out, so a
re-grade request never re-relevance-checks (or re-logs a warning for) a
paper already known to be off-topic. It's also excluded from every
paper-list/summary query in app/services/search.py (`get_ingredient_papers`),
so it no longer counts toward "Total studies"/"Average grade" or shows
up in the "List of Studies"/recommendations panels on the frontend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import Session, select

from app.models.research import PAPER_STATUS_DISCARDED_IRRELEVANT, ResearchPaper
from app.services.conclusion_grader import ConclusionGradingError, process_paper_conclusions
from app.services.paper_grader import PaperGradingError, grade_single_paper

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """Summary of one analyze_ingredient_papers() run — informational
    only (nothing currently branches on it beyond logging), since the
    whole point of this pipeline's error handling is that individual
    paper failures don't need to be surfaced as a hard error to the
    caller; whatever succeeded is already durably saved.
    """

    papers_considered: int = 0
    papers_graded_this_run: int = 0
    papers_grading_failed: int = 0
    # Incremented whenever a paper's grading result (this run) marks it
    # PAPER_STATUS_DISCARDED_IRRELEVANT — see "Discard logic" in the
    # module docstring. Conclusion synthesis is skipped for these.
    papers_discarded_irrelevant: int = 0
    # "Attempted" rather than "processed": process_paper_conclusions
    # returns False (not a failure) for a paper that graded at/below the
    # MIN_GRADE_SCORE_FOR_CONCLUSIONS threshold — this counts every
    # paper it actually ran Gemini for, not every paper considered.
    papers_conclusions_attempted: int = 0
    papers_conclusions_failed: int = 0


def analyze_ingredient_papers(
    session: Session, ingredient_id: int, ingredient_name: str
) -> PipelineResult:
    """Runs the grade -> relevance-check -> (maybe) synthesize-conclusions
    loop over every currently-active ResearchPaper stored for
    `ingredient_id`.

    Safe to call repeatedly for the same ingredient (e.g. once per grade
    request, even if some papers were already processed by an earlier
    call): grading is idempotent per paper (grade_single_paper no-ops on
    an already-graded row), conclusion merging is idempotent per (paper,
    conclusion) pair (a paper's id is only ever appended to a
    supporting/contradicting list once — see conclusion_grader.py), and
    an already-discarded paper is excluded from the initial query
    entirely (see module docstring's "Discard logic").

    Never raises for a single paper's failure — see module docstring.
    Only propagates if something outside any individual paper's control
    breaks (e.g. the initial paper lookup query itself failing), which
    would indicate a broken session/DB connection rather than a
    transient Gemini/rate-limit issue.

    Args:
        session: An open SQLModel session.
        ingredient_id: The canonical Ingredient whose stored
            ResearchPaper rows should be graded/analyzed.
        ingredient_name: That same Ingredient's `name` — passed
            explicitly (rather than re-fetched here) since every caller
            already has the Ingredient row in hand (see
            app/services/grading.py::grade_ingredient); used only for
            the "[Pipeline] Discarded Paper ID #..." log message below —
            `grade_single_paper` independently fetches the Ingredient
            itself for the actual relevance-check prompt.

    Returns:
        A PipelineResult summarizing what happened — see its docstring.
    """
    papers = session.exec(
        select(ResearchPaper)
        .where(ResearchPaper.ingredient_id == ingredient_id)
        .where(ResearchPaper.status != PAPER_STATUS_DISCARDED_IRRELEVANT)
    ).all()

    result = PipelineResult(papers_considered=len(papers))

    for paper in papers:
        try:
            graded_paper = grade_single_paper(session, paper)
            result.papers_graded_this_run += 1
        except PaperGradingError as exc:
            logger.warning(
                "Skipping paper id=%s (%r) for ingredient %s — grading "
                "failed, previously completed papers remain saved: %s",
                paper.id,
                paper.title,
                ingredient_id,
                exc,
            )
            result.papers_grading_failed += 1
            # Can't synthesize conclusions from a paper that failed to
            # grade (no grade_score to gate on) — move on to the next one.
            continue

        if graded_paper.status == PAPER_STATUS_DISCARDED_IRRELEVANT:
            logger.warning(
                "[Pipeline] Discarded Paper ID #%s: Unrelated to target "
                "ingredient %r",
                graded_paper.id,
                ingredient_name,
            )
            result.papers_discarded_irrelevant += 1
            # Requirement: skip conclusion extraction/grading completely
            # for a paper judged irrelevant — never call
            # process_paper_conclusions for it at all (on top of that
            # function's own defensive status gate — see
            # conclusion_grader.py).
            continue

        try:
            attempted = process_paper_conclusions(session, ingredient_id, graded_paper)
            if attempted:
                result.papers_conclusions_attempted += 1
        except ConclusionGradingError as exc:
            logger.warning(
                "Skipping conclusion synthesis for paper id=%s (%r) on "
                "ingredient %s — previously completed grades/conclusions "
                "remain saved: %s",
                paper.id,
                paper.title,
                ingredient_id,
                exc,
            )
            result.papers_conclusions_failed += 1
            continue

    return result
