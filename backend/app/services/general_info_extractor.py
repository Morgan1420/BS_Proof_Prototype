"""General Information extraction (Phase 33) — Ingredient Description +
Daily Dosage (Healthy Adult), each resolved under a STRICT, Grade-A/B-only
source hierarchy:

  1. **Primary priority — verified online resources.** Every
     `VerifiedResource` for the ingredient with `grade in ("A", "B")`,
     highest grade first (A before B), ties broken by `score` descending.
  2. **Secondary priority — peer-reviewed papers.** Only consulted if NO
     Grade A/B resource yields the field. Every `ResearchPaper` with
     `grade in ("A", "B")`, same A-before-B/score-descending ordering.
  3. **Fallback — unavailable.** If neither collection has anything
     usable for a field, that field is marked `is_available=False` with
     every other value `None` — the frontend renders a fixed notice ("No
     high-grade (Grade A or B) source available containing this
     information.") for that case; this module has no notice-text
     concept of its own (see `extract_general_info`'s return shape).

**NEVER accepts Grade C, D, or E sources for either field** — per the
task's own hard constraint. This is enforced BEFORE Gemini ever sees a
single row: the candidate lists built by `_build_candidates` only ever
include rows already filtered to `grade in ("A", "B")` in the DB query
itself (see `extract_general_info`'s `select(...).where(...)` clauses) —
Gemini physically cannot cite a Grade C/D/E source because none is ever
included in its prompt, not because it was asked nicely not to.

**One Gemini call for both fields, not `resolve_field_fallback()` called
twice per the task's literal reference sketch.** The task's reference
implementation describes two independent `await resolve_field_fallback(...)`
calls (one for `description`, one for `daily_dosage`), each presumably
walking its own candidate list one Gemini call at a time until something
usable turns up. This module instead builds ONE combined, already-
priority-ordered candidate list (resources first, papers second — see
`_build_candidates`) and makes a SINGLE Gemini call asking it to resolve
BOTH fields against that list, each field independently allowed to pick a
different (or no) winning candidate. This is a deliberate, documented
deviation for the same reason `conclusion_grader.py::synthesize_ingredient_summary`
(Stage 2) is one call over every paper/resource rather than one call per
source: this codebase's established rate-limit-consciousness (see
`app/services/paper_analysis_pipeline.py`'s "Rate Limiting & Execution
Order" docstring section) treats "one small call per ingredient-level
step" as the norm, not "one call per candidate source considered." The
prompt itself (`_build_prompt`) still encodes the fallback hierarchy
explicitly — candidates are presented in strict priority order with an
instruction to prefer the earliest usable one — so the *outcome* matches
the spec's per-field waterfall even though the mechanism differs.

**Every `source_name`/`source_type`/`source_grade` is server-derived, never
trusted from Gemini's own text.** Gemini only ever returns, per field,
whether usable information was found and (if so) the integer index of the
winning candidate in the list it was given (`_GeneralInfoFieldSchema`
below) — `extract_general_info` then looks that index up in the same
`candidates` list it built server-side and copies `source_name`/
`source_type`/`source_grade`/`text` straight from the real DB row, never
from anything Gemini generated about the source itself. Same "never trust
the model's own bound-following" convention as every other rubric-based
grader in this codebase (`paper_grader.py`, `conclusion_grader.py`) — here
applied to source attribution rather than a numeric score.

**Deliberately synchronous, not `async def`.** The task's reference
sketch declares `async def extract_general_info(...)`, but every
Gemini-calling service in this codebase is a plain, blocking sync function
— see `app/services/gemini_rate_limit.py`'s module docstring for the full
"why synchronous" reasoning (no genuine `await`-able I/O in the call
itself; always invoked from a FastAPI `run_in_threadpool` worker thread,
never the event loop). Same deviation `html_resource_extractor.py::
extract_conclusions_from_webpage` already made for this exact reason.

**Never raises.** Any Gemini request/parse failure degrades to a fully
"unavailable" result for both fields (logged, not propagated) — same
best-effort philosophy as every other step in
`app/services/paper_analysis_pipeline.py`. A failure here should never be
able to fail the grade request that triggered it.

**Field-name correction from the task's literal spec.** The task's
reference pseudocode filters candidates via
`getattr(r, 'confidence_grade', None)` for both resources and papers —
but neither `VerifiedResource` nor `ResearchPaper` has a `confidence_grade`
attribute (that field only exists on the unrelated `PaperConclusion`/
`ScientificConclusion` models). Both actually expose their letter grade as
plain `.grade` (see `app/models/research.py`) — this module filters on
that real field name instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Literal, Optional, TypedDict

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models.research import ResearchPaper, VerifiedResource
from app.services.gemini_rate_limit import call_gemini_with_retry, throttle_gemini_call

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[GeneralInfo]"

# Only these two grades are ever eligible — enforced both in the DB
# queries below (`extract_general_info`) and again here as a shared
# constant, so there's exactly one place that defines "what counts as
# high-grade" for this feature.
ELIGIBLE_GRADES = ("A", "B")

# Fixed, server-owned notice text for an unavailable field — per spec,
# verbatim. Not stored on the persisted `general_info` dict itself (see
# `GeneralInfoFieldResult` below — that's just `is_available: bool` plus
# `None`s); this constant exists so the backend and any future non-mobile
# consumer can render the identical sentence the task specifies, without
# duplicating the literal string at each call site.
UNAVAILABLE_NOTICE = "No high-grade (Grade A or B) source available containing this information."


class GeneralInfoFieldResult(TypedDict):
    """One resolved field (`description` or `daily_dosage`) — mirrors the
    task's literal per-field dict shape field-for-field."""

    text: Optional[str]
    source_name: Optional[str]
    source_type: Optional[str]
    source_grade: Optional[str]
    is_available: bool


class GeneralInfoResult(TypedDict):
    """Return shape of `extract_general_info()` — always both fields,
    never a partial result (see module docstring)."""

    description: GeneralInfoFieldResult
    daily_dosage: GeneralInfoFieldResult


def _unavailable_field() -> GeneralInfoFieldResult:
    return {
        "text": None,
        "source_name": None,
        "source_type": None,
        "source_grade": None,
        "is_available": False,
    }


def _unavailable_result() -> GeneralInfoResult:
    return {"description": _unavailable_field(), "daily_dosage": _unavailable_field()}


@dataclass
class _Candidate:
    """One Grade A/B source offered to Gemini, in priority order — see
    `_build_candidates`. `text` is the evidence block Gemini actually
    reads; every other field is real DB data copied straight onto the
    winning `GeneralInfoFieldResult` afterward, never re-derived from
    anything Gemini says.
    """

    source_type: Literal["verified_resource", "paper"]
    source_name: str
    source_grade: str
    text: str


def _grade_sort_key(grade: Optional[str], score: Optional[int]) -> tuple:
    """A before B; within a grade, higher score first; a missing score
    sorts last within its grade tier. Mirrors the "best available
    Grade A/B source first" ordering the task's hierarchy describes."""
    grade_rank = 0 if grade == "A" else 1
    return (grade_rank, -(score if score is not None else -1))


def _format_paper_source_name(paper: ResearchPaper) -> str:
    """Best-effort short citation label, e.g. "Smith et al. (2023)" — per
    the task's own example. Falls back to the paper's title whenever
    `authors`/`publication_date` aren't usable for a clean citation (both
    are free-text fields from the paper-search APIs, not guaranteed to
    parse), so this never raises or produces an empty label."""
    authors = (paper.authors or "").strip()
    year = ""
    if paper.publication_date:
        # publication_date is free-text (varies by source API) — pull the
        # first 4-digit run as a best-effort year, ignore if none found.
        digits = "".join(ch if ch.isdigit() else " " for ch in paper.publication_date).split()
        year = next((token for token in digits if len(token) == 4), "")

    if authors:
        first_author = authors.split(",")[0].split(" and ")[0].strip()
        label = f"{first_author} et al." if ("," in authors or " and " in authors) else first_author
        return f"{label} ({year})" if year else label

    return paper.title


def _build_candidates(
    resources: List[VerifiedResource], papers: List[ResearchPaper]
) -> List[_Candidate]:
    """Resources first (already Grade A/B-only, A-before-B/score-desc
    sorted by the caller), then papers, same ordering — see module
    docstring for why "resources entirely before papers" rather than an
    interleaved/merged ranking: the task's hierarchy is strictly
    two-tiered (verified resources are the primary priority; papers are
    only a fallback when resources have nothing), not a single combined
    ranking across both source types.
    """
    candidates: List[_Candidate] = []

    for resource in resources:
        pieces = [resource.title, resource.publisher]
        if resource.summary:
            pieces.append(resource.summary)
        if resource.extracted_conclusions:
            pieces.extend(resource.extracted_conclusions)
        text = " | ".join(piece for piece in pieces if piece and piece.strip())
        if not text:
            continue
        candidates.append(
            _Candidate(
                source_type="verified_resource",
                source_name=resource.publisher or resource.title,
                source_grade=resource.grade or "B",
                text=text,
            )
        )

    for paper in papers:
        pieces = [paper.title]
        if paper.abstract:
            pieces.append(paper.abstract)
        text = " | ".join(piece for piece in pieces if piece and piece.strip())
        if not text:
            continue
        candidates.append(
            _Candidate(
                source_type="paper",
                source_name=_format_paper_source_name(paper),
                source_grade=paper.grade or "B",
                text=text,
            )
        )

    return candidates


# --- Structured Gemini response schema ---


class _GeneralInfoFieldSchema(BaseModel):
    found: bool = Field(
        description=(
            "True iff at least one candidate source below actually states "
            "this information. False if none of them do — do not guess or "
            "infer from general knowledge; only use what the candidate "
            "text below actually says."
        )
    )
    text: Optional[str] = Field(
        default=None,
        description=(
            "The extracted information itself, written as a short, direct "
            "sentence (not a quote/citation) — null if `found` is false."
        ),
    )
    source_index: Optional[int] = Field(
        default=None,
        description=(
            "The 0-based index (from the numbered candidate list below) "
            "of the HIGHEST-PRIORITY candidate that actually contains "
            "this information — null if `found` is false. Must be the "
            "earliest candidate in the list that works, never a "
            "lower-priority one when an earlier candidate would do."
        ),
    )


class _GeneralInfoExtractionSchema(BaseModel):
    description: _GeneralInfoFieldSchema = Field(
        description="A general description of what this ingredient is and/or what it's commonly used for."
    )
    daily_dosage: _GeneralInfoFieldSchema = Field(
        description="The recommended/typical daily dosage of this ingredient for a healthy adult."
    )


def _build_prompt(ingredient_name: str, candidates: List[_Candidate]) -> str:
    candidate_lines = []
    for index, candidate in enumerate(candidates):
        candidate_lines.append(
            f"[{index}] ({candidate.source_type}, grade {candidate.source_grade}, "
            f"source: {candidate.source_name}): {candidate.text}"
        )
    candidates_text = "\n".join(candidate_lines)

    return (
        "You are extracting two specific pieces of general information "
        f"about the dietary supplement ingredient '{ingredient_name}' from "
        "a strictly ordered list of high-grade (Grade A or B only) "
        "sources.\n\n"
        "SOURCES (already ordered from HIGHEST to LOWEST priority — "
        "verified official/regulatory resources first, peer-reviewed "
        "papers second):\n"
        f"{candidates_text}\n\n"
        "FIELDS TO EXTRACT:\n"
        "1. `description` — a general description of what this ingredient "
        "is and/or what it's commonly used for.\n"
        "2. `daily_dosage` — the recommended/typical daily dosage of this "
        "ingredient for a healthy adult.\n\n"
        "INSTRUCTIONS:\n"
        "1. For EACH field independently, scan the sources above IN "
        "ORDER (index 0 first) and use the FIRST (highest-priority) "
        "source that actually states that information. Do not skip ahead "
        "to a lower-priority source just because it's more detailed — "
        "only move past a source if it genuinely doesn't contain the "
        "information at all.\n"
        "2. Only extract information a source ACTUALLY states — never "
        "infer, estimate, or fill in from general knowledge outside the "
        "sources above. If no source contains a field's information, set "
        "`found` to false and leave `text`/`source_index` null for that "
        "field.\n"
        "3. `source_index` must be the exact bracketed number of the "
        "source you used, e.g. `2` for `[2] (...)` above.\n"
        "4. The two fields may come from different sources — resolve "
        "each independently.\n\n"
        "Return your extraction as the required JSON object."
    )


@lru_cache
def _get_client() -> genai.Client:
    """Cached Gemini client — separate `@lru_cache` entry from every other
    Gemini-using service's own `_get_client` (same "one client per
    module" convention as paper_grader.py/resource_grader.py/
    conclusion_grader.py/html_resource_extractor.py/research_keywords.py)."""
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key)


def _resolve_field(
    field: _GeneralInfoFieldSchema, candidates: List[_Candidate]
) -> GeneralInfoFieldResult:
    """Builds a `GeneralInfoFieldResult` from Gemini's per-field verdict —
    every value except `text` is copied from the real `_Candidate` at
    `field.source_index`, never from anything Gemini generated about the
    source itself (see module docstring). Defensively falls back to
    "unavailable" if `found` is true but `source_index` is missing/out of
    range (a malformed response shouldn't fabricate a source)."""
    if not field.found or field.source_index is None:
        return _unavailable_field()
    if not (0 <= field.source_index < len(candidates)):
        logger.warning(
            "%s Gemini returned an out-of-range source_index=%s for %d "
            "candidate(s) — treating field as unavailable.",
            _LOG_PREFIX,
            field.source_index,
            len(candidates),
        )
        return _unavailable_field()

    candidate = candidates[field.source_index]
    text = (field.text or "").strip()
    if not text:
        return _unavailable_field()

    return {
        "text": text,
        "source_name": candidate.source_name,
        "source_type": candidate.source_type,
        "source_grade": candidate.source_grade,
        "is_available": True,
    }


def extract_general_info(
    session: Session, ingredient_id: int, ingredient_name: str
) -> GeneralInfoResult:
    """Resolves `description`/`daily_dosage` for `ingredient_id` under the
    strict Grade A/B-only source hierarchy — see module docstring.

    Always returns a full `GeneralInfoResult` (never `None`, unlike
    `conclusion_grader.py::synthesize_ingredient_summary`'s "skip
    entirely on zero evidence" convention) — an ingredient with zero
    Grade A/B sources for either field gets an honest, persisted
    "unavailable" result rather than nothing at all, so the frontend
    always has something concrete to render (see
    `general_info_extractor.py`'s own `_unavailable_result` for that
    shape). Safe to call repeatedly for the same ingredient — each call
    re-derives fresh from whatever Grade A/B evidence currently exists,
    the same idempotent-overwrite convention `summary_description`/
    `scientific_conclusions` already use.

    Makes NO Gemini call at all if there are zero Grade A/B candidates
    (resources + papers combined) — nothing to extract from, so this
    short-circuits straight to `_unavailable_result()` (same "don't call
    Gemini with nothing to work with" cost-saving as
    `synthesize_ingredient_summary`'s own zero-evidence check).

    Never raises: any Gemini request/parse failure is logged and degrades
    to `_unavailable_result()` — this is a best-effort enrichment step,
    not one that should be able to fail the grade request that triggered
    it (same philosophy as every other step in
    `app/services/paper_analysis_pipeline.py`).

    Args:
        session: An open SQLModel session.
        ingredient_id: The canonical Ingredient to extract for.
        ingredient_name: That Ingredient's `name` (used directly in the
            Gemini prompt).

    Returns:
        A `GeneralInfoResult` — always both fields, each independently
        either resolved (`is_available=True` + real text/source metadata)
        or unavailable (`is_available=False`, everything else `None`).
    """
    # Grade A/B filter applied here, in the query itself — see module
    # docstring's "NEVER accepts Grade C, D, or E" section for why this is
    # the actual enforcement point, not merely a prompt instruction.
    resources = session.exec(
        select(VerifiedResource)
        .where(VerifiedResource.ingredient_id == ingredient_id)
        .where(VerifiedResource.grade.in_(ELIGIBLE_GRADES))
    ).all()
    papers = session.exec(
        select(ResearchPaper)
        .where(ResearchPaper.ingredient_id == ingredient_id)
        .where(ResearchPaper.grade.in_(ELIGIBLE_GRADES))
    ).all()

    resources_sorted = sorted(resources, key=lambda r: _grade_sort_key(r.grade, r.score))
    papers_sorted = sorted(papers, key=lambda p: _grade_sort_key(p.grade, p.grade_score))

    candidates = _build_candidates(resources_sorted, papers_sorted)
    if not candidates:
        logger.info(
            "%s No Grade A/B verified resources or papers available for "
            "ingredient id=%s (%r) — General Information stays unavailable "
            "this run.",
            _LOG_PREFIX,
            ingredient_id,
            ingredient_name,
        )
        return _unavailable_result()

    client = _get_client()
    settings = get_settings()
    prompt = _build_prompt(ingredient_name, candidates)

    def _call_gemini():
        throttle_gemini_call()
        return client.models.generate_content(
            model=settings.gemini_model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_GeneralInfoExtractionSchema,
            ),
        )

    try:
        response = call_gemini_with_retry(
            _call_gemini, label=f"General Information extraction for ingredient id={ingredient_id}"
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, see module docstring
        logger.warning(
            "%s Gemini request failed for ingredient id=%s (%r): %s",
            _LOG_PREFIX,
            ingredient_id,
            ingredient_name,
            exc,
        )
        return _unavailable_result()

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, _GeneralInfoExtractionSchema):
        raw_text = getattr(response, "text", None)
        if not raw_text:
            logger.warning(
                "%s Gemini returned an empty response for ingredient id=%s (%r).",
                _LOG_PREFIX,
                ingredient_id,
                ingredient_name,
            )
            return _unavailable_result()
        try:
            parsed = _GeneralInfoExtractionSchema.model_validate_json(raw_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s Gemini response did not match the expected schema for "
                "ingredient id=%s (%r): %s",
                _LOG_PREFIX,
                ingredient_id,
                ingredient_name,
                exc,
            )
            return _unavailable_result()

    result: GeneralInfoResult = {
        "description": _resolve_field(parsed.description, candidates),
        "daily_dosage": _resolve_field(parsed.daily_dosage, candidates),
    }

    logger.info(
        "%s Resolved General Information for ingredient id=%s (%r): "
        "description=%s (source=%s), daily_dosage=%s (source=%s).",
        _LOG_PREFIX,
        ingredient_id,
        ingredient_name,
        result["description"]["is_available"],
        result["description"]["source_name"],
        result["daily_dosage"]["is_available"],
        result["daily_dosage"]["source_name"],
    )
    return result
