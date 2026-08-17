"""Gemini-backed automated paper quality grading + ingredient relevance
verification (Phase 3/4/6).

Mirrors app/services/research_keywords.py's Gemini usage pattern (cached
client, structured `response_schema` output, `.parsed` with a raw-text
fallback), but evaluates one already-found paper against
docs/paper_grading_rubric.json rather than generating search keywords.

`grade_paper()` (pure — no DB access) evaluates a paper's rubric quality
*and*, as of Phase 6, whether it's actually relevant to the target
ingredient it was found under (one Gemini call does both — see
`_RubricEvaluationSchema`'s `is_relevant_to_ingredient`/
`relevance_reasoning` fields below). `grade_single_paper()` (below,
DB-aware) is what actually calls it and persists the result — both for
`app/services/paper_analysis_pipeline.py::analyze_ingredient_papers`
(Phase 5/6's main entry point, grading every stored paper for an
ingredient) and for the on-demand "tap the ungraded badge to grade this
one paper" flow (`POST /api/v1/papers/{paper_id}/grade` —
app/api/routes.py). `grade_single_paper` is also what sets
`ResearchPaper.status` to `PAPER_STATUS_DISCARDED_IRRELEVANT` when
Gemini determines a paper isn't actually about its ingredient (e.g. a
Vitamin D paper that turned up during a Vitamin C search) — the pipeline
then skips conclusion synthesis for it and every paper-list/summary
query in app/services/search.py excludes it going forward.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.core.config import get_settings
from app.models.research import (
    PAPER_STATUS_ACTIVE,
    PAPER_STATUS_DISCARDED_IRRELEVANT,
    ResearchPaper,
)
from app.models.supplement import Ingredient

logger = logging.getLogger(__name__)

# backend/app/services/paper_grader.py -> parents[2] == backend/ ->
# parents[3] == repo root. Same absolute-path-resolution reasoning as
# paper_search.py's docs/paperApis.json lookup and app/db.py's database
# path — don't rely on the process's current working directory.
_REPO_ROOT = Path(__file__).resolve().parents[3]
RUBRIC_PATH = _REPO_ROOT / "docs" / "paper_grading_rubric.json"

_LETTER_GRADES = ("A", "B", "C", "D", "E")


class PaperGradingError(RuntimeError):
    """Raised when Gemini fails to return a usable rubric evaluation."""


class RubricEvaluation(TypedDict):
    """Shape persisted to ResearchPaper.rubric_evaluation (a JSON column)
    and returned by grade_paper() alongside `grade`/`grade_score` — see
    app/models/research.py and docs/paper_grading_rubric.json.
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


class GradeResult(TypedDict):
    """Return shape of grade_paper()."""

    grade: str
    grade_score: int
    rubric_evaluation: RubricEvaluation
    # --- Phase 6: ingredient relevance verification ---
    # Whether the paper's title/abstract actually studies the target
    # ingredient it was evaluated against (see grade_paper's
    # `ingredient_name` parameter) — strict check, not swayed by the
    # paper's own quality (a low-quality but genuinely on-topic paper is
    # still relevant; a high-quality but off-topic one is still not).
    # `grade_single_paper` uses this to set `ResearchPaper.status`.
    is_relevant_to_ingredient: bool
    # One concise sentence explaining the relevance determination —
    # surfaced in app/services/paper_analysis_pipeline.py's
    # "[Pipeline] Discarded Paper ID #..." warning log when False.
    relevance_reasoning: str


class _RubricEvaluationSchema(BaseModel):
    """Structured output schema handed to Gemini as `response_schema`.

    Deliberately does NOT include a `grade` letter field — the letter is
    always derived server-side from `total_score` via the rubric's
    `grade_bands` (see _score_to_grade below) rather than trusted
    directly from the model. Asking Gemini for both a score and a letter
    risks the two disagreeing (e.g. a 72 total paired with a "C"); computing
    the letter ourselves from the one number Gemini has to get right
    guarantees they're always consistent.

    `is_relevant_to_ingredient`/`relevance_reasoning` (Phase 6) are
    listed first, ahead of the rubric-quality fields — this is
    deliberately a *gating* question ("is this paper even about the
    target ingredient?"), conceptually prior to "how good is it?", even
    though both are produced by the same Gemini call and neither field's
    presence changes how the other four are scored (a paper judged
    irrelevant still gets a full rubric evaluation filled in — it's
    simply never used, since app/services/paper_analysis_pipeline.py
    skips conclusion synthesis for a discarded paper regardless of its
    grade_score).
    """

    is_relevant_to_ingredient: bool = Field(
        description=(
            "True only if the paper's title/abstract explicitly "
            "studies, tests, or analyzes the named target ingredient "
            "itself (or a direct synonym / specific active compound of "
            "it) — not a different ingredient, and not merely a broader "
            "category it happens to belong to. False for any paper "
            "about an unrelated substance or topic."
        )
    )
    relevance_reasoning: str = Field(
        description="One concise sentence explaining the relevance determination."
    )
    study_type: str = Field(description="The evaluated study design/hierarchy tier.")
    study_type_score: int = Field(description="Points awarded for study design, out of the category's max_score.")
    journal_reputation: str = Field(description="The evaluated journal/publisher rigor tier.")
    journal_score: int = Field(
        description=(
            "Points awarded for journal reputation, in the range -5 to "
            "15 (negative values penalize a predatory publisher, vanity "
            "press, unvetted pay-to-publish outlet, or fake peer review; "
            "see the rubric's journal_reputation category)."
        )
    )
    sample_info: str = Field(description="Description of the sample: human/animal/cell, size, diversity.")
    sample_score: int = Field(description="Points awarded for methodology & sample, out of the category's max_score.")
    funding_status: str = Field(description="The evaluated funding/conflict-of-interest tier.")
    funding_score: int = Field(
        description=(
            "Points awarded for funding & bias, in the range -15 to 5 "
            "(defaults to a neutral +2 when funding isn't mentioned at "
            "all; negative values penalize only actively-indicated "
            "industry-biased/suspicious funding; see the rubric's "
            "funding_bias category)."
        )
    )
    total_score: int = Field(description="Sum of the four category scores above (funding_score may be negative), clamped to 0-100.")
    summary_notes: str = Field(description="A concise 2-sentence rationale for the overall evaluation.")


@lru_cache
def _get_client() -> genai.Client:
    """Cached Gemini client — see research_keywords.py's `_get_client` for
    why this isn't shared with the other Gemini-using services directly
    (equivalent client, separate `@lru_cache` entry per module).
    """
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key)


@lru_cache
def _load_rubric() -> Dict[str, Any]:
    """Reads and caches docs/paper_grading_rubric.json for the lifetime
    of the process — unlike paper_search.py's docs/paperApis.json (which
    re-reads every call so `enabled: false` takes effect without a
    restart), the rubric's categories/grade_bands aren't meant to be
    toggled live, and re-parsing this larger structured file on every
    single paper graded would be wasteful.

    Raises:
        PaperGradingError: if the rubric file is missing or malformed —
            there's no sensible default rubric to fall back to, so
            grading simply can't proceed without it.
    """
    try:
        with RUBRIC_PATH.open("r", encoding="utf-8") as handle:
            rubric = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise PaperGradingError(
            f"Could not read paper grading rubric at {RUBRIC_PATH}: {exc}"
        ) from exc

    if not isinstance(rubric, dict) or "categories" not in rubric or "grade_bands" not in rubric:
        raise PaperGradingError(
            f"Paper grading rubric at {RUBRIC_PATH} is missing 'categories' or 'grade_bands'."
        )
    return rubric


def _format_rubric_for_prompt(rubric: Dict[str, Any]) -> str:
    """Renders the rubric's categories/score tiers as readable text to
    embed in the Gemini system prompt, so the actual scoring criteria
    live in docs/paper_grading_rubric.json (data-driven, editable without
    a code change) rather than being hardcoded into SYSTEM_PROMPT below.
    """
    lines: List[str] = []
    for category in rubric.get("categories", []):
        min_score = category.get("min_score", 0)
        max_score = category.get("max_score")
        range_desc = (
            f"score range {min_score} to {max_score} points"
            if min_score
            else f"max {max_score} points"
        )
        lines.append(
            f"- {category.get('label')} (\"{category.get('id')}\") — "
            f"{range_desc}. {category.get('description')}"
        )
        for tier in category.get("score_tiers", []):
            lines.append(f"    * {tier.get('range')} points: {tier.get('example')}")
    return "\n".join(lines)


def _score_to_grade(total_score: int, rubric: Dict[str, Any]) -> str:
    """Maps a clamped 0-100 total score onto a letter grade via the
    rubric's `grade_bands`. Falls back to the lowest configured grade
    (or "E" if grade_bands is somehow empty) if no band's range covers
    the score — defensive only, shouldn't happen with a well-formed
    rubric covering 0-100 contiguously.
    """
    for band in rubric.get("grade_bands", []):
        if band.get("min_score", 0) <= total_score <= band.get("max_score", 100):
            return str(band.get("grade"))
    return str(rubric.get("grade_bands", [{}])[-1].get("grade", "E")) if rubric.get("grade_bands") else "E"


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _build_prompt(
    paper_metadata: Dict[str, Optional[str]], rubric: Dict[str, Any], ingredient_name: str
) -> str:
    title = paper_metadata.get("title") or "Unknown title"
    abstract = paper_metadata.get("abstract") or "No abstract available."
    authors = paper_metadata.get("authors") or "Not specified."
    journal = paper_metadata.get("journal") or "Not specified."
    publication_date = paper_metadata.get("publication_date") or "Not specified."

    return (
        f"This paper was found while researching the ingredient/compound "
        f"\"{ingredient_name}\". Before scoring the rubric below, first "
        f"strictly determine `is_relevant_to_ingredient`: does the title "
        f"and/or abstract explicitly study, test, or analyze "
        f"\"{ingredient_name}\" itself — or a direct synonym of it, or "
        f"one of its specific active/chemical compounds — and not a "
        f"different, unrelated ingredient or an entirely different "
        f"topic? For example, if the target is \"Vitamin C\" and the "
        f"paper is actually about Vitamin D, `is_relevant_to_ingredient` "
        f"must be false. Judge relevance on topic alone, never on "
        f"quality: a low-quality but genuinely on-topic paper is still "
        f"relevant; a high-quality but off-topic paper is still not. "
        f"Briefly justify your determination in `relevance_reasoning`. "
        f"Still fill in every rubric field below as best you can "
        f"regardless of your relevance determination — they'll simply be "
        f"disregarded downstream for a paper judged irrelevant.\n\n"
        "Evaluate the following scientific paper against the rubric below. "
        "Be strict and evidence-based — only award points the abstract/"
        "metadata actually supports; when information for a category is "
        "missing, score conservatively in that category's lower tiers "
        "rather than assuming the best case. The one exception is "
        "`funding_score` (\"funding_bias\") — see the note below, where "
        "funding not being mentioned at all defaults to a neutral +2, not "
        "that category's lower (negative) tiers. (`journal_score` "
        "(\"journal_reputation\") can also go negative, but only when a "
        "publisher is actively identified as predatory/blacklisted, not "
        "merely unidentified — see that note below too.)\n\n"
        f"Title: {title}\n"
        f"Abstract: {abstract}\n"
        f"Authors: {authors}\n"
        f"Journal/Publisher: {journal}\n"
        f"Publication info: {publication_date}\n\n"
        "Rubric categories:\n"
        f"{_format_rubric_for_prompt(rubric)}\n\n"
        "Note: `funding_score` (\"funding_bias\") and `journal_score` "
        "(\"journal_reputation\") are the two exceptions to the usual "
        "0-to-max scoring — both can go negative.\n\n"
        "`funding_score` ranges from -15 to 5. Award a positive value "
        "(up to +5) for independent/well-disclosed funding; when funding "
        "isn't mentioned in the abstract/metadata at all, default to a "
        "neutral **+2** (do not treat missing funding information as a "
        "reason to penalize an otherwise strong paper); and award a "
        "negative value (down to -15) only when the abstract/metadata "
        "actively indicates industry-biased, undisclosed-conflict, or "
        "suspicious commercial funding — never as a penalty for silence "
        "alone.\n\n"
        "`journal_score` ranges from -5 to 15. Award a positive value "
        "(up to +15) scaled to the journal's reputation/rigor; when the "
        "publisher simply isn't identified in the metadata, score near "
        "the neutral 0-1 tier (not the negative range — an unidentified "
        "publisher is not the same as a known-bad one); and award a "
        "negative value (down to -5) only when the abstract/metadata "
        "actively identifies a predatory publisher, vanity press, "
        "unvetted pay-to-publish outlet, or fake/absent peer review — "
        "never merely for the publisher being unnamed.\n\n"
        "`study_type_score` must be a non-negative integer between 0 and "
        "40. `sample_score` must be a non-negative integer between 0 and "
        "40.\n\n"
        "Return your evaluation as the required JSON object. `total_score` "
        "must equal the sum of the four category scores (`funding_score` "
        "and `journal_score` may each be negative)."
    )


def grade_paper(paper_metadata: Dict[str, Optional[str]], ingredient_name: str) -> GradeResult:
    """Evaluates one paper against docs/paper_grading_rubric.json *and*
    checks its relevance to `ingredient_name` (Phase 6), via a single
    Gemini call, and returns both.

    Args:
        paper_metadata: `{"title", "abstract", "authors", "journal",
            "publication_date"}` — any value may be None/missing; Gemini
            is instructed to score conservatively where metadata is
            absent rather than assume the best case.
        ingredient_name: The canonical Ingredient name this paper was
            found under (e.g. "Vitamin C") — passed explicitly into the
            prompt so Gemini can judge whether the paper is actually
            about it, rather than a different ingredient/topic that
            happened to surface under the same search keywords.

    Returns:
        `{"grade": "A"-"E", "grade_score": 0-100, "rubric_evaluation": {...},
        "is_relevant_to_ingredient": bool, "relevance_reasoning": str}`
        — see RubricEvaluation above for the breakdown shape. Every
        category score is clamped to that category's `(min_score,
        max_score)` range — plain `0` to `max_score` for `study_type`
        (`0`-`40` as of v1.5) / `sample_methodology`, but `funding_bias`
        (`-15` to `5`) and `journal_reputation` (`-5` to `15` as of
        v1.5, previously `-5` to `20`) are both penalty scales rather
        than a plain 0-to-max score — in case Gemini's raw output
        overshoots either bound. `total_score` is the sum of
        those clamped category scores (funding's and/or journal's
        contribution may be negative), then clamped again to 0-100 —
        `grade` is derived from that final clamped total, not from
        Gemini's raw output. `is_relevant_to_ingredient`/
        `relevance_reasoning` are passed through from Gemini's response
        unmodified (a plain bool/str, nothing to clamp or re-derive).

    Raises:
        PaperGradingError: if the rubric can't be loaded, the Gemini
            request itself fails, or the response can't be parsed against
            the expected schema at all.
    """
    rubric = _load_rubric()
    client = _get_client()
    settings = get_settings()

    prompt = _build_prompt(paper_metadata, rubric, ingredient_name)

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_RubricEvaluationSchema,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surface as a clean service error
        raise PaperGradingError(f"Gemini request failed: {exc}") from exc

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, _RubricEvaluationSchema):
        raw_text = getattr(response, "text", None)
        if not raw_text:
            raise PaperGradingError("Gemini returned an empty response.")
        try:
            parsed = _RubricEvaluationSchema.model_validate_json(raw_text)
        except Exception as exc:  # noqa: BLE001
            raise PaperGradingError(
                f"Gemini response did not match the expected schema: {exc}"
            ) from exc

    # (min_score, max_score) per category — min_score defaults to 0 for
    # categories that don't set one explicitly (study_type and
    # sample_methodology). Two categories are penalty scales rather than
    # plain 0-to-max scores: funding_bias ranges -15 to 5 ((sharpened,
    # v1.2) penalty for industry-biased/suspicious funding; the prompt
    # also defaults this one to a neutral +2 — not 0 — when funding
    # simply isn't mentioned, so undisclosed funding no longer drags an
    # otherwise strong paper down — see _build_prompt above), and
    # journal_reputation ranges -5 to 15 as of v1.5 (previously -5 to 20,
    # with the 5-point difference moved to study_type's max, 35 -> 40 —
    # penalty for an actively identified predatory/blacklisted publisher;
    # an unidentified one still scores near-neutral 0-1, not negative —
    # see _build_prompt).
    category_bounds = {
        category["id"]: (category.get("min_score", 0), category["max_score"])
        for category in rubric.get("categories", [])
    }

    study_min, study_max = category_bounds.get("study_type", (0, 40))
    journal_min, journal_max = category_bounds.get("journal_reputation", (-5, 15))
    sample_min, sample_max = category_bounds.get("sample_methodology", (0, 40))
    funding_min, funding_max = category_bounds.get("funding_bias", (-15, 5))

    study_type_score = _clamp(parsed.study_type_score, study_min, study_max)
    journal_score = _clamp(parsed.journal_score, journal_min, journal_max)
    sample_score = _clamp(parsed.sample_score, sample_min, sample_max)
    funding_score = _clamp(parsed.funding_score, funding_min, funding_max)

    # Recomputed from the (now clamped) category scores rather than
    # trusting Gemini's own `total_score` — guarantees the number always
    # actually equals the sum shown in the breakdown, even if Gemini's
    # arithmetic (or an out-of-range category score) didn't add up.
    total_score = _clamp(
        study_type_score + journal_score + sample_score + funding_score, 0, 100
    )
    grade = _score_to_grade(total_score, rubric)

    rubric_evaluation: RubricEvaluation = {
        "study_type": parsed.study_type,
        "study_type_score": study_type_score,
        "journal_reputation": parsed.journal_reputation,
        "journal_score": journal_score,
        "sample_info": parsed.sample_info,
        "sample_score": sample_score,
        "funding_status": parsed.funding_status,
        "funding_score": funding_score,
        "total_score": total_score,
        "summary_notes": parsed.summary_notes,
    }

    return {
        "grade": grade,
        "grade_score": total_score,
        "rubric_evaluation": rubric_evaluation,
        "is_relevant_to_ingredient": parsed.is_relevant_to_ingredient,
        "relevance_reasoning": parsed.relevance_reasoning,
    }


def grade_single_paper(session: Session, paper: ResearchPaper) -> ResearchPaper:
    """Grades and persists exactly one already-stored `ResearchPaper` row
    — the on-demand counterpart to the automatic per-paper grading
    `app/services/paper_analysis_pipeline.py::analyze_ingredient_papers`
    does over every stored paper for an ingredient. Backs
    `POST /api/v1/papers/{paper_id}/grade` (app/api/routes.py), triggered
    by tapping a gray "(-)" ungraded badge in the frontend's StudiesList.

    Args:
        session: An open SQLModel session. `paper` must already be
            attached to it (e.g. via `session.get(ResearchPaper, id)` —
            the caller is responsible for the 404 lookup; this function
            assumes the row already exists).
        paper: The row to grade.

    Returns:
        `paper`, unchanged if it was already graded (idempotent — no
        Gemini call, no commit — per spec: re-tapping an already-graded
        badge, or a race between two taps, is a safe no-op), otherwise
        with `grade`/`grade_score`/`rubric_evaluation`/`status` freshly
        set and committed. `status` becomes `PAPER_STATUS_DISCARDED_IRRELEVANT`
        (Phase 6) if Gemini determines this paper isn't actually about
        its own `ingredient_id`'s Ingredient, otherwise stays/becomes
        `PAPER_STATUS_ACTIVE`.

    Raises:
        PaperGradingError: if the paper's Ingredient can't be found (a
            broken FK — shouldn't happen, but relevance-checking needs a
            name to check against), grading fails (propagated from
            `grade_paper` or `_load_rubric`), or the commit fails — in
            every case the session is rolled back so a failed attempt
            doesn't leave a half-updated row.

    Known limitation: unlike ingestion-time grading, this has no
    `journal` name to pass along — `PaperRecord.journal` (see
    paper_search.py) exists only transiently during the initial
    paper-search fan-out and was never added as its own `ResearchPaper`
    column (see that module's docstring for why). `grade_paper` still
    scores "Journal / Publisher Rigor" conservatively without one (same
    handling as any other missing-metadata field), just with slightly
    less signal to work with than the paper's original, ingestion-time
    grade would have had.
    """
    if paper.grade is not None:
        return paper

    ingredient = session.get(Ingredient, paper.ingredient_id)
    if ingredient is None:
        raise PaperGradingError(
            f"Could not find Ingredient id={paper.ingredient_id} for paper "
            f"id={paper.id} — cannot relevance-check without a target "
            "ingredient name."
        )

    result = grade_paper(
        {
            "title": paper.title,
            "abstract": paper.abstract,
            "authors": paper.authors,
            "journal": None,
            "publication_date": paper.publication_date,
        },
        ingredient.name,
    )

    paper.grade = result["grade"]
    paper.grade_score = result["grade_score"]
    paper.rubric_evaluation = dict(result["rubric_evaluation"])
    paper.status = (
        PAPER_STATUS_ACTIVE
        if result["is_relevant_to_ingredient"]
        else PAPER_STATUS_DISCARDED_IRRELEVANT
    )
    session.add(paper)

    try:
        session.commit()
        session.refresh(paper)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise PaperGradingError(f"Failed to save grading result: {exc}") from exc

    if paper.status == PAPER_STATUS_DISCARDED_IRRELEVANT:
        logger.info(
            "Paper id=%s (%r) graded but marked %s relative to ingredient "
            "%r: %s",
            paper.id,
            paper.title,
            PAPER_STATUS_DISCARDED_IRRELEVANT,
            ingredient.name,
            result["relevance_reasoning"],
        )

    return paper
