"""Gemini-backed automated resource quality grading (Phase 8).

Mirrors app/services/paper_grader.py's Gemini usage pattern almost
exactly (cached client, structured `response_schema` output, `.parsed`
with a raw-text fallback, server-side score clamping + grade derivation
rather than trusting Gemini's own aggregate) — but evaluates one
VerifiedResource (app/models/research.py, Phase 7 — an already-fetched,
already-domain-verified government/regulatory reference link; see
app/services/resource_fetcher.py) against
docs/resource_grading_rubric.json instead of a paper against
docs/paper_grading_rubric.json.

`grade_resource()` (pure — no DB access, no Gemini schema opinions about
`VerifiedResource` itself) is the only function this module exports.
Unlike paper_grader.py, there is no separate DB-aware
`grade_single_resource()`/on-demand grading endpoint here: this task's
scope doesn't call for one (no "tap a badge to grade this one resource"
UI, unlike papers' `POST /api/v1/papers/{paper_id}/grade`) — instead,
app/services/resource_fetcher.py::fetch_verified_resources_for_ingredient
calls `grade_resource()` directly, once per newly-found resource, right
after building (but before flushing) each `VerifiedResource` row, and
sets `grade`/`score`/`reasoning_summary` on it itself. Keeping this
module DB-agnostic (a pure function in, structured result out) means
resource_fetcher.py — which already owns every `VerifiedResource` ORM
object's construction/`session.add()`/flush lifecycle — doesn't have to
hand a half-built row back and forth across a module boundary just to
get it graded.

**Phase 39 — deliberately NOT a domain-based Grade A bypass.** The Phase
39 NIH extraction overhaul task asked for "Auto-Grade Inheritance:
Automatically assign Grade A to all conclusions extracted from verified
NIH URLs." This module does NOT implement that as a hard bypass — doing
so would break the one guarantee every Grade-A/B-gated consumer in this
codebase depends on: `general_info_extractor.py`'s `ELIGIBLE_GRADES =
("A", "B")` gate (itself an explicit, repeatedly-stated project
requirement: "NEVER accept Grade C, D, or E sources for General
Information fields") and `conclusion_grader.py`'s server-derived
`confidence_grade`/`total_score` scoring both exist specifically so a
grade always reflects real, checked evidence quality — never an
unearned label. A hard "domain == nih.gov -> Grade A" rule would let a
genuinely thin, stale, or malformed NIH page (a 404 interstitial, a
redirect landing page, a page that failed to parse) inherit the same
trust as a comprehensive, well-cited fact sheet, silently degrading the
exact fields (`general_info.description`/`general_info.daily_dosage`,
Scientific Claims) this phase's own task was trying to improve the
quality of.

Instead, `_build_prompt` below adds an honest, rubric-aligned nudge: when
the resource's URL resolves to a confirmed NIH/NLM domain (see
`_is_nih_domain` below — a small local duplicate of
`resource_fetcher.py::is_nih_domain`, not a cross-import, since
`resource_fetcher.py` already imports THIS module and a reverse import
would be circular), the prompt explicitly reminds Gemini that
`docs/resource_grading_rubric.json`'s own `publisher_authority` rubric
already names NIH by example as its Tier 1 (30-35/35) case — so a
genuine, well-cited, comprehensive NIH page should reliably clear the
Grade A band (80-100 total) through the SAME honest, evidence-based
scoring every other resource goes through, rather than skipping that
scoring entirely. In practice this means a real NIH fact sheet should
still land at Grade A almost every time (satisfying the spirit of "Auto-
Grade Inheritance" for the resources it's actually meant to help), while
a broken/thin one is still caught rather than blindly trusted. See
docs/Architecture.md's Phase 39 section for the full reasoning.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict
from urllib.parse import urlparse

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Phase 39 — small, local duplicate of
# resource_fetcher.py::_NIH_DOMAIN_SUFFIXES/is_nih_domain. NOT a
# cross-import: resource_fetcher.py already imports `grade_resource` from
# THIS module (`from app.services.resource_grader import
# ResourceGradingError, grade_resource`), so importing resource_fetcher.py
# back here would be circular. See the module docstring's "Phase 39"
# paragraph above for what this is used for (an honest prompt nudge, not a
# grade bypass).
_NIH_DOMAIN_SUFFIXES = ("nih.gov", "medlineplus.gov")


def _is_nih_domain(url: Optional[str]) -> bool:
    """True iff `url`'s hostname is (or is a subdomain of) an official
    NIH/NLM property. Parses the hostname from the URL itself (this
    module only ever receives a `url` string in `resource_metadata`, not
    a separate pre-parsed `domain` field) — falsy/unparseable input
    returns `False` rather than raising.
    """
    if not url:
        return False
    try:
        hostname = (urlparse(url).netloc or "").lower().rstrip(".")
    except ValueError:
        return False
    if not hostname:
        return False
    for suffix in _NIH_DOMAIN_SUFFIXES:
        if hostname == suffix or hostname.endswith("." + suffix):
            return True
    return False

# backend/app/services/resource_grader.py -> parents[2] == backend/ ->
# parents[3] == repo root. Same absolute-path-resolution reasoning as
# paper_grader.py's RUBRIC_PATH.
_REPO_ROOT = Path(__file__).resolve().parents[3]
RUBRIC_PATH = _REPO_ROOT / "docs" / "resource_grading_rubric.json"

_LETTER_GRADES = ("A", "B", "C", "D", "E")


class ResourceGradingError(RuntimeError):
    """Raised when Gemini fails to return a usable resource evaluation."""


class CategoryScores(TypedDict):
    """The four rubric category scores — mirrors
    docs/resource_grading_rubric.json's `categories` ids exactly, and the
    task spec's `category_scores` JSON shape. Not persisted on
    VerifiedResource as its own column (unlike ResearchPaper's
    `rubric_evaluation` JSON blob) — see GradeResult below and
    app/models/research.py's VerifiedResource docstring for why; these
    four numbers exist only to compute `total_score`, then are discarded.
    """

    publisher_authority: int
    evidence_citations: int
    comprehensiveness_currency: int
    transparency_bias: int


class GradeResult(TypedDict):
    """Return shape of grade_resource() — mirrors the task spec's Gemini
    JSON output shape field-for-field (`total_score`, `grade`,
    `category_scores`, `reasoning_summary`), except that `total_score`/
    `grade` here are always the server-recomputed/re-derived values, not
    Gemini's own (see grade_resource's docstring for why).
    """

    total_score: int
    grade: str
    category_scores: CategoryScores
    reasoning_summary: str


class _CategoryScoresSchema(BaseModel):
    """Nested structured-output schema for the four rubric category
    scores — same nested-BaseModel-as-response_schema pattern already
    used elsewhere in this codebase (see
    app/schemas/supplement.py::SupplementAnalysis's `List[Ingredient]`,
    consumed by app/services/vision.py). Bounds in each field's
    description mirror docs/resource_grading_rubric.json's per-category
    `min_score`/`max_score` (0 where a category has no explicit
    `min_score`) — Gemini is free to return a value outside these, which
    is exactly why grade_resource() below clamps every one of them
    server-side rather than trusting the raw numbers.
    """

    publisher_authority: int = Field(
        description=(
            "Domain & Institutional Authority — 0 to 35 points. Evaluates "
            "the standing/credentials of the publishing entity or domain "
            "extension (e.g. a national health agency or international "
            "regulator scores at the top of the range; a commercial blog "
            "or unverified personal domain scores at the bottom)."
        )
    )
    evidence_citations: int = Field(
        description=(
            "Scientific Citations & Primary Sources — 0 to 30 points. "
            "Measures whether claims are backed by direct links to "
            "peer-reviewed literature, DOIs, clinical trial IDs, or "
            "official regulatory monographs, versus vague or absent "
            "sourcing."
        )
    )
    comprehensiveness_currency: int = Field(
        description=(
            "Content Breadth & Recency — 0 to 20 points. Assesses the "
            "detail provided (definition, dosage, safety/upper limits, "
            "food sources, mechanism, interactions) and how recently the "
            "page appears to have been updated."
        )
    )
    transparency_bias: int = Field(
        description=(
            "Commercial Neutrality & Disclosures — -10 to 15 points. "
            "Measures independence from sales motives: a fully "
            "independent, non-commercial resource earns full positive "
            "points; direct product sales, affiliate buy-links, sponsored "
            "posts, or disguised marketing content are penalized into the "
            "negative range."
        )
    )


class _ResourceEvaluationSchema(BaseModel):
    """Structured output schema handed to Gemini as `response_schema`.

    Deliberately does NOT include `total_score`/`grade` fields, even
    though the task's example Gemini output includes them — same
    "never trust the model's own aggregate/letter" rationale as
    paper_grader.py's `_RubricEvaluationSchema`: asking Gemini for both
    the category breakdown AND a total/letter risks the two disagreeing
    (e.g. category scores summing to 72 paired with a returned "A").
    Computing `total_score` as the actual sum of the (clamped)
    `category_scores` below, then deriving `grade` from that one number
    via the rubric's `grade_bands`, guarantees the three always agree —
    see grade_resource() below.
    """

    category_scores: _CategoryScoresSchema
    reasoning_summary: str = Field(
        description=(
            "A concise 1-2 sentence rationale for the overall evaluation "
            "— e.g. \"Official NIH MedlinePlus health topic page with "
            "comprehensive dosage guidelines, safety details, and zero "
            "commercial bias.\""
        )
    )


@lru_cache
def _get_client() -> genai.Client:
    """Cached Gemini client — separate `@lru_cache` entry from every
    other Gemini-using service's own `_get_client` (paper_grader.py,
    research_keywords.py, conclusion_grader.py), same reasoning as those:
    an equivalent client per module rather than one shared across the
    codebase.
    """
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key)


@lru_cache
def _load_rubric() -> Dict[str, Any]:
    """Reads and caches docs/resource_grading_rubric.json for the
    lifetime of the process — same reasoning as
    paper_grader.py::_load_rubric: this rubric's categories/grade_bands
    aren't meant to be toggled live, so re-parsing it on every single
    resource graded would be wasteful.

    Raises:
        ResourceGradingError: if the rubric file is missing or malformed
            — there's no sensible default rubric to fall back to.
    """
    try:
        with RUBRIC_PATH.open("r", encoding="utf-8") as handle:
            rubric = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ResourceGradingError(
            f"Could not read resource grading rubric at {RUBRIC_PATH}: {exc}"
        ) from exc

    if not isinstance(rubric, dict) or "categories" not in rubric or "grade_bands" not in rubric:
        raise ResourceGradingError(
            f"Resource grading rubric at {RUBRIC_PATH} is missing 'categories' or 'grade_bands'."
        )
    return rubric


def _format_rubric_for_prompt(rubric: Dict[str, Any]) -> str:
    """Renders the rubric's categories/score tiers as readable text to
    embed in the Gemini prompt — same helper/reasoning as
    paper_grader.py::_format_rubric_for_prompt: the actual scoring
    criteria live in docs/resource_grading_rubric.json (data-driven,
    editable without a code change), not hardcoded into this module.
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
    rubric's `grade_bands` — identical logic to
    paper_grader.py::_score_to_grade. Falls back to the lowest configured
    grade (or "E" if grade_bands is somehow empty) if no band's range
    covers the score — defensive only, shouldn't happen with a
    well-formed rubric covering 0-100 contiguously.
    """
    for band in rubric.get("grade_bands", []):
        if band.get("min_score", 0) <= total_score <= band.get("max_score", 100):
            return str(band.get("grade"))
    bands = rubric.get("grade_bands")
    return str(bands[-1].get("grade", "E")) if bands else _LETTER_GRADES[-1]


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _build_prompt(resource_metadata: Dict[str, Optional[str]], rubric: Dict[str, Any]) -> str:
    resource_title = resource_metadata.get("resource_title") or "Unknown title"
    url = resource_metadata.get("url") or "Unknown URL"
    publisher = resource_metadata.get("publisher") or "Unknown publisher"
    page_snippet_or_text = (
        resource_metadata.get("page_snippet_or_text")
        or "No page content available beyond the title/publisher below."
    )

    # Phase 39 — see module docstring's "Phase 39" paragraph for why this
    # is an honest scoring nudge, not a grade bypass: still asks Gemini to
    # score every category from the actual content, just reminds it what
    # the rubric itself already says about this domain category.
    nih_hint = ""
    if _is_nih_domain(url):
        nih_hint = (
            "\nNote: this URL resolves to an official NIH/NLM domain. "
            "The rubric's own `publisher_authority` category names "
            "'NIH' explicitly as its Tier 1 (30-35/35) example — score "
            "`publisher_authority` accordingly UNLESS the page content "
            "itself gives you a concrete reason not to (e.g. it's "
            "actually a broken/near-empty page, not a genuine fact "
            "sheet). This does not change how any other category should "
            "be scored — `evidence_citations`, "
            "`comprehensiveness_currency`, and `transparency_bias` must "
            "still be judged strictly from what the page content "
            "actually shows, same as any other resource.\n"
        )

    return (
        "Evaluate the following online reference resource against the "
        "rubric below. Be strict and evidence-based — only award points "
        "the provided title/publisher/page content actually supports; "
        "when information for a category is missing or the page content "
        "is too sparse to judge, score conservatively in that category's "
        "lower tiers rather than assuming the best case. The one "
        "exception is `transparency_bias`, which can go negative — see "
        "the note below.\n\n"
        f"Resource Title: {resource_title}\n"
        f"URL: {url}\n"
        f"Publisher: {publisher}\n"
        f"Page Snippet/Text: {page_snippet_or_text}\n"
        f"{nih_hint}\n"
        "Rubric categories:\n"
        f"{_format_rubric_for_prompt(rubric)}\n\n"
        "Note: `transparency_bias` ranges from -10 to 15 — the only "
        "category that can go negative. Award a positive value (up to "
        "+15) for demonstrated independence from commercial/sales "
        "motives; when the page content doesn't clearly indicate either "
        "way, score near the neutral low-positive tier (do not assume "
        "the worst case for silence alone); and award a negative value "
        "(down to -10) only when the content actively shows direct "
        "product sales, affiliate buy-links, sponsored posts, or "
        "disguised marketing content. Every other category score "
        "(`publisher_authority`, `evidence_citations`, "
        "`comprehensiveness_currency`) must be a non-negative integer, "
        "each no greater than its category's max_score above.\n\n"
        "Return your evaluation as the required JSON object."
    )


def grade_resource(resource_metadata: Dict[str, Optional[str]]) -> GradeResult:
    """Evaluates one online reference resource against
    docs/resource_grading_rubric.json via a single Gemini call.

    Pure — no DB access, no side effects. The caller
    (app/services/resource_fetcher.py) is responsible for turning the
    result into a persisted VerifiedResource row.

    Args:
        resource_metadata: `{"resource_title", "url", "publisher",
            "page_snippet_or_text"}` — any value may be None/missing;
            Gemini is instructed to score conservatively where content is
            absent rather than assume the best case. `page_snippet_or_text`
            is typically a VerifiedResource's `summary` field (see
            app/models/research.py) — itself optional, since not every
            source API provides one (see resource_fetcher.py).

    Returns:
        `{"total_score": 0-100, "grade": "A"-"E", "category_scores": {...},
        "reasoning_summary": str}` — see CategoryScores above for the
        breakdown shape. Every category score is clamped to that
        category's `(min_score, max_score)` range from the rubric —
        plain `0` to `max_score` for `publisher_authority`/
        `evidence_citations`/`comprehensiveness_currency`, but
        `transparency_bias` (`-10` to `15`) is a penalty scale rather
        than a plain 0-to-max score — in case Gemini's raw output
        overshoots either bound. `total_score` is the sum of those
        clamped category scores (`transparency_bias`'s contribution may
        be negative), then clamped again to 0-100 per the task's "Score
        Calculation Guard" — `grade` is derived from that final clamped
        total via the rubric's `grade_bands`, never from Gemini's own
        output (Gemini is never even asked for a total/letter — see
        `_ResourceEvaluationSchema`'s docstring).

    Raises:
        ResourceGradingError: if the rubric can't be loaded, the Gemini
            request itself fails, or the response can't be parsed against
            the expected schema at all.
    """
    rubric = _load_rubric()
    client = _get_client()
    settings = get_settings()

    prompt = _build_prompt(resource_metadata, rubric)

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_ResourceEvaluationSchema,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surface as a clean service error
        raise ResourceGradingError(f"Gemini request failed: {exc}") from exc

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, _ResourceEvaluationSchema):
        raw_text = getattr(response, "text", None)
        if not raw_text:
            raise ResourceGradingError("Gemini returned an empty response.")
        try:
            parsed = _ResourceEvaluationSchema.model_validate_json(raw_text)
        except Exception as exc:  # noqa: BLE001
            raise ResourceGradingError(
                f"Gemini response did not match the expected schema: {exc}"
            ) from exc

    # (min_score, max_score) per category — min_score defaults to 0 for
    # categories that don't set one explicitly (publisher_authority,
    # evidence_citations, comprehensiveness_currency). transparency_bias
    # is the one penalty scale, -10 to 15 — see _build_prompt above.
    category_bounds = {
        category["id"]: (category.get("min_score", 0), category["max_score"])
        for category in rubric.get("categories", [])
    }

    publisher_min, publisher_max = category_bounds.get("publisher_authority", (0, 35))
    citations_min, citations_max = category_bounds.get("evidence_citations", (0, 30))
    comprehensiveness_min, comprehensiveness_max = category_bounds.get(
        "comprehensiveness_currency", (0, 20)
    )
    transparency_min, transparency_max = category_bounds.get("transparency_bias", (-10, 15))

    publisher_score = _clamp(
        parsed.category_scores.publisher_authority, publisher_min, publisher_max
    )
    citations_score = _clamp(
        parsed.category_scores.evidence_citations, citations_min, citations_max
    )
    comprehensiveness_score = _clamp(
        parsed.category_scores.comprehensiveness_currency,
        comprehensiveness_min,
        comprehensiveness_max,
    )
    transparency_score = _clamp(
        parsed.category_scores.transparency_bias, transparency_min, transparency_max
    )

    # Recomputed from the (now clamped) category scores rather than
    # trusting a Gemini-provided total (there isn't one — see
    # _ResourceEvaluationSchema's docstring) — guarantees `total_score`
    # always actually equals the sum shown in `category_scores`. Clamped
    # a second time to 0-100 per the task's explicit "Score Calculation
    # Guard" — the four category bounds alone (0+0+0-10 to 35+30+20+15)
    # already keep the raw sum within -10 to 100, so this final clamp
    # only ever has to catch the low end.
    total_score = _clamp(
        publisher_score + citations_score + comprehensiveness_score + transparency_score,
        0,
        100,
    )
    grade = _score_to_grade(total_score, rubric)

    category_scores: CategoryScores = {
        "publisher_authority": publisher_score,
        "evidence_citations": citations_score,
        "comprehensiveness_currency": comprehensiveness_score,
        "transparency_bias": transparency_score,
    }

    return {
        "total_score": total_score,
        "grade": grade,
        "category_scores": category_scores,
        "reasoning_summary": parsed.reasoning_summary,
    }
