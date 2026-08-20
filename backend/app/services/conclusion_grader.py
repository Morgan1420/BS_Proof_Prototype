"""Gemini-backed cross-paper conclusion synthesis (Phase 5), extended
with multi-source ingredient-level summary synthesis (Phase 11).

For a single already-graded ResearchPaper (grade_score > 50 — see
MIN_GRADE_SCORE_FOR_CONCLUSIONS below), extracts the paper's scientific
findings and reconciles them against every existing, active
PaperConclusion already stored for the same ingredient: a finding that
matches an existing conclusion updates that conclusion's
supporting/contradicting paper list and re-evaluated cross-paper
consensus score; a genuinely new finding becomes a new PaperConclusion
row, graded against docs/conclusion_grading_rubric.json.

One Gemini call does extraction + merging + grading together (see
_ConclusionSynthesisSchema) rather than three separate calls, to stay
within free-tier rate limits — same "one small call per paper, not one
huge batched call" design app/services/paper_analysis_pipeline.py's
module docstring describes.

Mirrors app/services/paper_grader.py's rubric-loading/clamping/
grade-derivation pattern (see that module for the reasoning behind never
trusting Gemini's own total/grade directly) — kept as its own,
self-contained copy here rather than a shared import, since these are
the only two rubric-based graders in the app today; worth factoring out
into a shared helper module if a third one shows up.

**Phase 11 — `synthesize_ingredient_summary()`** (bottom of this file) is
a *different kind* of operation from `process_paper_conclusions` above:
where that function runs once per newly-graded paper and incrementally
merges its findings into the running PaperConclusion set,
`synthesize_ingredient_summary` runs once per grade request (called by
app/services/paper_analysis_pipeline.py after that per-paper loop
finishes) and makes exactly one additional Gemini call for the whole
ingredient, considering every currently-graded ResearchPaper AND every
VerifiedResource (Phase 7/8 — official government/regulatory guidance)
together, to produce a single synthesized `summary_description` (plus
`main_consensus`/`scientific_conclusions`, returned for observability/
future use — see that function's docstring for exactly what's persisted).
Being ingredient-level rather than per-paper, this doesn't reintroduce
the "one huge batched call" problem the per-paper design above
deliberately avoids — it's still just one small call, now added once
per grade request rather than once per paper.

**Phase 16 — audit.** A report that this synthesis step was "ignoring
Verified Online Resources" turned out not to reproduce: execution order
was already correct (resources fetched/committed before this runs) and
the prompt already included formatted resource text. See
docs/Architecture.md's Phase 16 section for the full audit. What the
audit DID surface, though, was a real quality problem, addressed below.

**Phase 17 — Two-Stage Extraction Pipeline.** The single prompt built by
`_build_summary_prompt` used to interleave two very different kinds of
evidence text: each paper's own dense, information-rich abstract-derived
findings, and each resource's raw, often much shorter/thinner `summary`
snippet. In practice this caused a "lost-in-the-middle" effect — Gemini
consistently over-weighted the papers and under-weighted (or
effectively ignored) the resources, not because the resource text was
missing from the prompt (it wasn't — see the Phase 16 audit above), but
because it was outmatched, evidence-density-wise, by the papers sitting
alongside it.

The fix moves resource-claims extraction into its own dedicated step,
run independently per resource, *before* this synthesis prompt is ever
built — see app/services/resource_extractor.py (Stage 1) and
app/services/paper_analysis_pipeline.py::analyze_ingredient_papers's
Stage 1 loop, which populates each VerifiedResource's `extracted_data`
column ahead of every call into this module. `_format_resources_for_prompt`
below now renders that pre-distilled, uniformly-structured
`{official_stance, recommended_dose, upper_limit_warning,
key_takeaways}` payload (falling back to the old raw-`summary`
rendering only for a resource extraction hasn't reached yet), and
`_build_summary_prompt` now presents the two evidence sources as two
clearly-labeled, comparably-dense blocks — resources first, papers
second — rather than one continuous wall of uneven-density text. This
module (Stage 2) is unchanged in every other respect: same one-call-per-
grade-request cadence, same `_IngredientSummarySchema` output shape,
same zero-evidence handling.

**Phase 21/22 — `extracted_data` retired, replaced by
`extracted_conclusions`/`aligned_conclusions`.** `resource_extractor.py`
(the Gemini-based Stage 1 that populated `VerifiedResource.extracted_data`
above) was retired in Phase 21 in favor of `resource_parser.py`'s
deterministic, zero-LLM extraction into `extracted_conclusions` — which
means `extracted_data` is `None` for every resource fetched after Phase
21, and `_format_resources_for_prompt`'s Phase 17 rendering above had
silently been falling back to raw `summary` text for all of them ever
since (a known, accepted regression at the time — see
docs/Architecture.md's Phase 21 section). Phase 23 (below) finally closes
that gap: `_format_resources_for_prompt` now renders
`extracted_conclusions` (Phase 19/21, uncapped as of Phase 22) plus each
conclusion's `aligned_conclusions` classification (Phase 22 —
AGREES/CONTRADICTS/DISTINCT_NEW against existing paper claims) when
present, giving the `official_authority_backing`/`multi_source_consensus`
rubric categories below real per-conclusion alignment signal to work
from, instead of an almost-always-empty `extracted_data` dict.

**Phase 23 — Multi-Source Confidence Rubric for `scientific_conclusions`
(named `recommended_uses` through Phase 23, renamed Phase 24 — see
below).** Through Phase 22, these items were graded by Gemini directly
picking a `confidence_grade` letter with no server-derived scoring behind
it, and were never persisted (returned from `synthesize_ingredient_summary`
for observability only — only `summary_description` was written to the
`Ingredient` row). Phase 23 replaces that with a real, four-category,
100-point rubric — `docs/multi_source_confidence_rubric.json`, loaded by
`_load_multi_source_rubric()` below, deliberately a SEPARATE file/loader
from `docs/conclusion_grading_rubric.json`/`_load_rubric()` above, which
still governs the per-paper `PaperConclusion` table via
`process_paper_conclusions` — that per-paper synthesis pipeline is
untouched this phase; only the ingredient-level claims below get the new
rubric, since those (not `PaperConclusion`) are what already models "one
synthesized claim combining paper AND regulatory evidence together,"
which is what Phase 23's task asked to rescore.

Same "never trust Gemini's own bound-following" convention as every
other rubric-based grader in this codebase (paper_grader.py,
process_paper_conclusions above): Gemini emits four raw per-category
scores (`paper_evidence_quality`, `official_authority_backing`,
`multi_source_consensus`, `claim_specificity`) per claim, clamped
server-side to each category's `max_score`, summed into a clamped 0-100
`total_score`, and mapped to a `confidence_grade` letter via
`_score_to_grade` against the new rubric's own `grade_bands` — Gemini's
own judgment shapes the four inputs, but never gets to hand back a
grade/total directly. The full per-claim shape (`score_breakdown`,
`supporting_study_count`/`supporting_resource_count`, `sources_summary`,
`grade_justification`) is now persisted onto a new
`Ingredient.recommended_uses` JSON column (see
app/models/supplement.py), not just returned for observability —
app/services/paper_analysis_pipeline.py writes it alongside
`summary_description` in the same commit.

**Phase 24 — Terminology rename + Direct Injection Safety Net.** Two
unrelated changes landed together:

1. **Rename.** `recommended_uses` -> `scientific_conclusions` everywhere
   this module touches it: `_RecommendedUseSchema` -> `_ScientificConclusionSchema`,
   `_IngredientSummarySchema.recommended_uses` -> `.scientific_conclusions`,
   `IngredientSummaryResult`'s `recommended_uses` key ->
   `scientific_conclusions`, the synthesis prompt's instruction text, and
   (outside this module) `Ingredient.recommended_uses` ->
   `Ingredient.scientific_conclusions` (app/models/supplement.py — see
   that column's own "Phase 24 rename, backward-compat" docstring
   paragraph for the additive-column-plus-one-time-backfill migration
   strategy, since this is a rename, not a new concept, and existing
   synthesized data shouldn't be silently lost), and
   `RecommendedUseResponse`/`RecommendedUseScoreBreakdown` ->
   `ScientificConclusionResponse`/`ScientificConclusionScoreBreakdown`
   (app/schemas/research.py). Purely a naming change — the underlying
   rubric, scoring pipeline, and JSON shape are otherwise identical to
   Phase 23.

2. **Direct Injection Safety Net (`GUARANTEED INCLUSION`).** Despite the
   Phase 23 prompt already including every resource's `extracted_conclusions`
   in SOURCE 1, Gemini was still observed silently dropping some of them
   from its synthesized output — the prompt only *asks*, it can't
   *enforce*. Phase 24 adds a Python-side enforcement pass, run AFTER
   Gemini's response is parsed and scored (see
   `synthesize_ingredient_summary`'s own docstring for exactly where):
   for every string in every VerifiedResource's `extracted_conclusions`,
   `_is_conclusion_represented()` checks whether that string is already
   reasonably reflected somewhere in Gemini's synthesized
   `scientific_conclusions` list (normalized substring match, falling
   back to a significant-word-overlap heuristic to tolerate Gemini having
   paraphrased/merged it into a broader claim rather than quoting it
   verbatim — see that function's own docstring for the exact thresholds;
   deliberately simple/deterministic text matching, no NLP dependency,
   same "no external NLP libraries" posture as resource_parser.py's own
   free-text extraction). Any resource conclusion NOT represented is
   force-appended as its own standalone claim
   (`supporting_resource_count=1`, `supporting_study_count=0`, a fixed
   `score_breakdown` reflecting "real official-source claim, zero paper
   backing, not otherwise cross-referenced" — clamped to the rubric's own
   category bounds and put through the exact same `_clamp`/
   `_score_to_grade` derivation as every Gemini-scored claim, so an
   injected claim's `confidence_grade`/`total_score` are computed
   identically to a synthesized one, never hardcoded). This guarantees,
   by construction rather than by hoping the prompt is obeyed, that 100%
   of parsed online-resource conclusions appear somewhere in the final
   `scientific_conclusions` array for every grade request.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models.research import (
    PAPER_STATUS_DISCARDED_IRRELEVANT,
    PaperConclusion,
    ResearchPaper,
    VerifiedResource,
)

logger = logging.getLogger(__name__)

# backend/app/services/conclusion_grader.py -> parents[2] == backend/ ->
# parents[3] == repo root — same absolute-path-resolution reasoning as
# paper_grader.py's docs/paper_grading_rubric.json lookup.
_REPO_ROOT = Path(__file__).resolve().parents[3]
RUBRIC_PATH = _REPO_ROOT / "docs" / "conclusion_grading_rubric.json"

# A paper must score above this on paper_grader.py's 0-100 scale before
# its findings are extracted into conclusions at all — a paper that
# hasn't been graded yet, or graded too low, shouldn't get to influence
# the ingredient's synthesized conclusions.
MIN_GRADE_SCORE_FOR_CONCLUSIONS = 50


class ConclusionGradingError(RuntimeError):
    """Raised when Gemini fails to return a usable conclusion synthesis,
    or the DB commit of the result fails."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@lru_cache
def _get_client() -> genai.Client:
    """Cached Gemini client — see research_keywords.py's `_get_client`
    for why this isn't shared with the other Gemini-using services
    directly (equivalent client, separate `@lru_cache` entry per
    module)."""
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key)


@lru_cache
def _load_rubric() -> Dict[str, Any]:
    """Reads and caches docs/conclusion_grading_rubric.json for the
    lifetime of the process — see paper_grader.py::_load_rubric for the
    same reasoning (categories/grade_bands aren't meant to be toggled
    live).

    Raises:
        ConclusionGradingError: if the rubric file is missing or
            malformed — there's no sensible default to fall back to.
    """
    try:
        with RUBRIC_PATH.open("r", encoding="utf-8") as handle:
            rubric = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConclusionGradingError(
            f"Could not read conclusion grading rubric at {RUBRIC_PATH}: {exc}"
        ) from exc

    if not isinstance(rubric, dict) or "categories" not in rubric or "grade_bands" not in rubric:
        raise ConclusionGradingError(
            f"Conclusion grading rubric at {RUBRIC_PATH} is missing 'categories' or 'grade_bands'."
        )
    return rubric


# Phase 23 — docs/multi_source_confidence_rubric.json, a SEPARATE file
# from RUBRIC_PATH above. See module docstring's "Phase 23" paragraph for
# why: RUBRIC_PATH/_load_rubric() above still exclusively govern the
# per-paper PaperConclusion table (process_paper_conclusions); this one
# governs only the ingredient-level `scientific_conclusions` claims
# produced by synthesize_ingredient_summary() at the bottom of this file.
MULTI_SOURCE_RUBRIC_PATH = _REPO_ROOT / "docs" / "multi_source_confidence_rubric.json"


@lru_cache
def _load_multi_source_rubric() -> Dict[str, Any]:
    """Reads and caches docs/multi_source_confidence_rubric.json — same
    reasoning/shape/error-handling as `_load_rubric()` above, just a
    different file and a separate `@lru_cache` entry (so this app can
    have two independently-cached rubrics loaded at once without one
    call clobbering the other's cache).

    Raises:
        ConclusionGradingError: if the rubric file is missing or
            malformed — there's no sensible default to fall back to.
    """
    try:
        with MULTI_SOURCE_RUBRIC_PATH.open("r", encoding="utf-8") as handle:
            rubric = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConclusionGradingError(
            f"Could not read multi-source confidence rubric at {MULTI_SOURCE_RUBRIC_PATH}: {exc}"
        ) from exc

    if not isinstance(rubric, dict) or "categories" not in rubric or "grade_bands" not in rubric:
        raise ConclusionGradingError(
            f"Multi-source confidence rubric at {MULTI_SOURCE_RUBRIC_PATH} "
            "is missing 'categories' or 'grade_bands'."
        )
    return rubric


def _format_rubric_for_prompt(rubric: Dict[str, Any]) -> str:
    """Renders the rubric's categories/score tiers as readable text to
    embed in the Gemini prompt — see paper_grader.py's identically-named
    helper for the same "data-driven, not hardcoded" reasoning."""
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


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _score_to_grade(total_score: int, rubric: Dict[str, Any]) -> str:
    """Maps a clamped 0-100 total onto a letter grade via the rubric's
    `grade_bands` — see paper_grader.py::_score_to_grade for the same
    logic/fallback reasoning."""
    for band in rubric.get("grade_bands", []):
        if band.get("min_score", 0) <= total_score <= band.get("max_score", 100):
            return str(band.get("grade"))
    bands = rubric.get("grade_bands", [])
    return str(bands[-1].get("grade", "E")) if bands else "E"


# --- Structured Gemini response schema ---


class _MergedConclusionSchema(BaseModel):
    existing_conclusion_id: int = Field(
        description="The id of the existing conclusion this paper's finding matches."
    )
    relationship: Literal["SUPPORTS", "CONTRADICTS"] = Field(
        description="Whether this paper's finding agrees with or contradicts the existing claim."
    )
    updated_consensus_reasoning: str = Field(
        description="One or two sentences explaining how this paper changes the consensus."
    )
    new_consensus_score: int = Field(
        description=(
            "Re-evaluated cross_paper_consensus category score (see rubric) "
            "given this paper's addition to the evidence."
        )
    )


class _NewConclusionSchema(BaseModel):
    claim_summary: str = Field(description="A short (<=15 word) summary of the new finding.")
    detailed_conclusion: str = Field(description="A fuller 2-4 sentence explanation of the finding.")
    dosage_mentioned: Optional[str] = Field(
        default=None, description="The specific dosage this finding pertains to, if any."
    )
    evidence_strength: str = Field(description="Evaluated evidence_strength tier (see rubric).")
    evidence_strength_score: int = Field(description="Points for evidence_strength, per rubric.")
    cross_paper_consensus: str = Field(
        description=(
            "Evaluated cross_paper_consensus tier (see rubric) — for a "
            "brand-new claim, this reflects a single supporting paper."
        )
    )
    cross_paper_consensus_score: int = Field(description="Points for cross_paper_consensus, per rubric.")
    claim_specificity: str = Field(description="Evaluated claim_specificity tier (see rubric).")
    claim_specificity_score: int = Field(description="Points for claim_specificity, per rubric.")
    summary_notes: str = Field(description="A concise 1-2 sentence rationale for the overall confidence grade.")


class _ConclusionSynthesisSchema(BaseModel):
    merged_conclusions: List[_MergedConclusionSchema] = Field(default_factory=list)
    new_conclusions: List[_NewConclusionSchema] = Field(default_factory=list)


def _build_prompt(
    paper: ResearchPaper, existing: List[PaperConclusion], rubric: Dict[str, Any]
) -> str:
    existing_lines: List[str] = []
    for conclusion in existing:
        existing_lines.append(
            f"- id={conclusion.id}: \"{conclusion.claim_summary}\" "
            f"(confidence_score={conclusion.confidence_score}, "
            f"supporting_papers={len(conclusion.supporting_paper_ids)}, "
            f"contradicting_papers={len(conclusion.contradicting_paper_ids)})"
        )
    existing_text = "\n".join(existing_lines) if existing_lines else "(none yet)"

    return (
        "You are extracting scientific findings from a research paper's "
        "abstract and reconciling them against a running list of "
        "previously extracted findings for the same dietary supplement "
        "ingredient.\n\n"
        f"Paper title: {paper.title}\n"
        f"Paper abstract: {paper.abstract or 'No abstract available.'}\n"
        f"Paper quality grade: {paper.grade} ({paper.grade_score}/100)\n\n"
        "Existing conclusions already recorded for this ingredient:\n"
        f"{existing_text}\n\n"
        "Instructions:\n"
        "1. Identify each distinct scientific finding/claim in this "
        "paper's abstract relevant to the ingredient's effects, safety, "
        "or dosage.\n"
        "2. For each finding, check whether it matches (is the same "
        "underlying claim as) one of the existing conclusions listed "
        "above. If it does, add it to `merged_conclusions` with that "
        "conclusion's exact `existing_conclusion_id`, whether this paper "
        "SUPPORTS or CONTRADICTS it, and a re-evaluated "
        "`new_consensus_score` for the cross_paper_consensus rubric "
        "category reflecting the updated evidence.\n"
        "3. If a finding does NOT match any existing conclusion, add it "
        "to `new_conclusions` instead, fully graded against the rubric "
        "below.\n"
        "4. If the abstract contains no extractable, ingredient-relevant "
        "finding, return empty lists for both.\n\n"
        "Rubric categories (for new_conclusions' category scores, and "
        "for re-evaluating cross_paper_consensus on merged_conclusions):\n"
        f"{_format_rubric_for_prompt(rubric)}\n\n"
        "Return your evaluation as the required JSON object."
    )


def process_paper_conclusions(
    session: Session, ingredient_id: int, paper: ResearchPaper
) -> bool:
    """Extracts findings from `paper` and merges them into the
    ingredient's running PaperConclusion set — see module docstring.

    Gatekept two ways, both returning False immediately (no Gemini call):
    - `paper.status == PAPER_STATUS_DISCARDED_IRRELEVANT` (Phase 6 —
      app/services/paper_grader.py determined this paper isn't actually
      about the target ingredient). This is a defense-in-depth check —
      app/services/paper_analysis_pipeline.py already skips calling this
      function at all for a discarded paper — but costs nothing to
      assert here too, so this function is self-contained-safe
      regardless of caller discipline.
    - MIN_GRADE_SCORE_FOR_CONCLUSIONS: `paper.grade_score` is missing or
      <= 50, per spec — a paper that hasn't been graded yet, or graded
      too low, shouldn't contribute to the ingredient's synthesized
      conclusions.

    Commits its own changes (new PaperConclusion rows + updates to
    existing ones) — callers (app/services/paper_analysis_pipeline.py)
    don't need to commit afterward for this step, and per that module's
    error-handling design, a failure here leaves the session rolled back
    to its last commit, not partially applied.

    Returns:
        True if Gemini was actually called and its result (possibly
        empty merged/new lists) was committed; False if skipped entirely
        by either gatekeeper check above.

    Raises:
        ConclusionGradingError: if the rubric can't load, the Gemini
            request fails, the response can't be parsed, or the DB
            commit fails (session is rolled back first in that case).
    """
    if paper.status == PAPER_STATUS_DISCARDED_IRRELEVANT:
        return False
    if paper.grade_score is None or paper.grade_score <= MIN_GRADE_SCORE_FOR_CONCLUSIONS:
        return False

    rubric = _load_rubric()
    bounds = {
        category["id"]: (category.get("min_score", 0), category["max_score"])
        for category in rubric.get("categories", [])
    }
    evidence_min, evidence_max = bounds.get("evidence_strength", (0, 40))
    consensus_min, consensus_max = bounds.get("cross_paper_consensus", (0, 40))
    specificity_min, specificity_max = bounds.get("claim_specificity", (0, 20))

    existing = session.exec(
        select(PaperConclusion)
        .where(PaperConclusion.ingredient_id == ingredient_id)
        .where(PaperConclusion.is_active.is_(True))
    ).all()
    existing_by_id = {conclusion.id: conclusion for conclusion in existing}

    client = _get_client()
    settings = get_settings()
    prompt = _build_prompt(paper, existing, rubric)

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_ConclusionSynthesisSchema,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surface as a clean service error
        raise ConclusionGradingError(f"Gemini request failed: {exc}") from exc

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, _ConclusionSynthesisSchema):
        raw_text = getattr(response, "text", None)
        if not raw_text:
            raise ConclusionGradingError("Gemini returned an empty response.")
        try:
            parsed = _ConclusionSynthesisSchema.model_validate_json(raw_text)
        except Exception as exc:  # noqa: BLE001
            raise ConclusionGradingError(
                f"Gemini response did not match the expected schema: {exc}"
            ) from exc

    # --- Apply merges onto existing conclusions ---
    for merge in parsed.merged_conclusions:
        conclusion = existing_by_id.get(merge.existing_conclusion_id)
        if conclusion is None:
            logger.warning(
                "Gemini referenced unknown existing_conclusion_id=%s for "
                "ingredient %s — skipping.",
                merge.existing_conclusion_id,
                ingredient_id,
            )
            continue

        # Reassigned to a new list (never .append()'d in place) so
        # SQLAlchemy's dirty-tracking picks up the change on a JSON
        # column — see PaperConclusion's docstring in app/models/research.py.
        if merge.relationship == "SUPPORTS":
            if paper.id not in conclusion.supporting_paper_ids:
                conclusion.supporting_paper_ids = [*conclusion.supporting_paper_ids, paper.id]
        else:  # CONTRADICTS
            if paper.id not in conclusion.contradicting_paper_ids:
                conclusion.contradicting_paper_ids = [*conclusion.contradicting_paper_ids, paper.id]

        new_consensus = _clamp(merge.new_consensus_score, consensus_min, consensus_max)
        conclusion.cross_paper_consensus = new_consensus

        # Recompute total confidence from the (possibly updated)
        # cross_paper_consensus plus the claim's original
        # evidence_strength/claim_specificity components, stored in its
        # rubric_evaluation — those don't get re-scored on every merge,
        # only cross_paper_consensus does (per spec: "re-evaluate
        # 'cross_paper_consensus' score, and recalculate total
        # confidence").
        stored_eval = conclusion.rubric_evaluation or {}
        evidence_score = _clamp(
            int(stored_eval.get("evidence_strength_score", 0)), evidence_min, evidence_max
        )
        specificity_score = _clamp(
            int(stored_eval.get("claim_specificity_score", 0)), specificity_min, specificity_max
        )
        total = _clamp(evidence_score + new_consensus + specificity_score, 0, 100)

        conclusion.confidence_score = total
        conclusion.confidence_grade = _score_to_grade(total, rubric)
        conclusion.rubric_evaluation = {
            **stored_eval,
            "cross_paper_consensus_score": new_consensus,
            "total_score": total,
            "summary_notes": merge.updated_consensus_reasoning,
        }
        conclusion.updated_at = _utcnow()
        session.add(conclusion)

    # --- Create brand-new conclusions ---
    for new_item in parsed.new_conclusions:
        evidence_score = _clamp(new_item.evidence_strength_score, evidence_min, evidence_max)
        consensus_score = _clamp(new_item.cross_paper_consensus_score, consensus_min, consensus_max)
        specificity_score = _clamp(new_item.claim_specificity_score, specificity_min, specificity_max)
        total = _clamp(evidence_score + consensus_score + specificity_score, 0, 100)
        grade = _score_to_grade(total, rubric)

        conclusion = PaperConclusion(
            ingredient_id=ingredient_id,
            claim_summary=new_item.claim_summary,
            detailed_conclusion=new_item.detailed_conclusion,
            dosage_mentioned=new_item.dosage_mentioned,
            rubric_evaluation={
                "evidence_strength": new_item.evidence_strength,
                "evidence_strength_score": evidence_score,
                "cross_paper_consensus": new_item.cross_paper_consensus,
                "cross_paper_consensus_score": consensus_score,
                "claim_specificity": new_item.claim_specificity,
                "claim_specificity_score": specificity_score,
                "total_score": total,
                "summary_notes": new_item.summary_notes,
            },
            confidence_score=total,
            confidence_grade=grade,
            cross_paper_consensus=consensus_score,
            supporting_paper_ids=[paper.id],
            contradicting_paper_ids=[],
        )
        session.add(conclusion)

    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise ConclusionGradingError(f"Failed to save conclusion updates: {exc}") from exc

    return True


# =====================================================================
# Phase 11: multi-source ingredient-level summary synthesis
# =====================================================================
#
# Distinct from everything above this line — see the module docstring's
# "Phase 11" paragraph for how this differs from process_paper_conclusions.


class _ScientificConclusionSchema(BaseModel):
    """One synthesized scientific conclusion claim, as returned inside
    `_IngredientSummarySchema.scientific_conclusions` — Phase 23 shape,
    renamed Phase 24 (was `_RecommendedUseSchema`).

    Deliberately does NOT include a `confidence_grade`/`total_score`
    field: per this module's "never trust Gemini's own bound-following"
    convention (see module docstring's "Phase 23" paragraph), Gemini only
    ever supplies the four raw category scores below; the server derives
    `total_score`/`confidence_grade` from them afterward (see
    `synthesize_ingredient_summary`'s post-processing), the same way
    `process_paper_conclusions` above derives `PaperConclusion.
    confidence_score`/`confidence_grade` from Gemini-supplied category
    scores rather than trusting a Gemini-picked letter directly. Every
    claim gets this same derivation regardless of whether Gemini
    synthesized it or Phase 24's Direct Injection Safety Net force-
    appended it afterward (see that section of the module docstring) —
    this schema only ever describes what Gemini itself returns.

    Now persisted (as a plain dict, via `IngredientSummaryResult`'s
    `scientific_conclusions` below) onto `Ingredient.scientific_conclusions`
    — see that column's docstring in app/models/supplement.py — rather
    than only returned for observability, as it was through Phase 22.
    """

    claim: str = Field(description="A short, specific scientific conclusion or effect claim.")
    paper_evidence_quality_score: int = Field(
        description=(
            "0 to the rubric's paper_evidence_quality max_score — quality "
            "of supporting peer-reviewed studies. 0 if no papers support "
            "this claim at all (see module docstring's graceful "
            "single-source handling)."
        )
    )
    official_authority_backing_score: int = Field(
        description=(
            "0 to the rubric's official_authority_backing max_score — "
            "degree of official regulatory/health-agency authorization. 0 "
            "if no verified resources support this claim at all."
        )
    )
    multi_source_consensus_score: int = Field(
        description=(
            "0 to the rubric's multi_source_consensus max_score — "
            "consistency of this finding across independent papers AND "
            "official agency guidelines (agreement vs. contradiction)."
        )
    )
    claim_specificity_score: int = Field(
        description=(
            "0 to the rubric's claim_specificity max_score — how "
            "precisely the claim states an effective dosage range, target "
            "population, and clinical outcome measure."
        )
    )
    supporting_study_count: int = Field(
        default=0, description="How many of the provided papers support this claim."
    )
    supporting_resource_count: int = Field(
        default=0, description="How many of the provided verified resources support this claim."
    )
    sources_summary: List[str] = Field(
        default_factory=list,
        description=(
            "Short (2-4 word) labels for the specific sources backing "
            "this claim, e.g. '3 RCTs', 'Health Canada Monograph', 'USDA "
            "FDC' — only sources actually present in the evidence above, "
            "never fabricated."
        ),
    )
    grade_justification: str = Field(
        description=(
            "One or two concise sentences explaining the confidence "
            "assessment for this claim, referencing the actual evidence "
            "(or lack thereof) behind each of the four category scores."
        )
    )


class _IngredientSummarySchema(BaseModel):
    """Structured output schema for the Phase 11 ingredient-level
    synthesis call — mirrors the task's `PROMPT_TEMPLATE` JSON schema
    field-for-field.
    """

    summary_description: str = Field(
        description=(
            "A 1-2 sentence synthesized overview combining paper and "
            "official-resource evidence, suitable for display directly "
            "under a UI section title."
        )
    )
    main_consensus: str = Field(
        description="A fuller sentence or two summarizing the overall scientific and regulatory baseline."
    )
    scientific_conclusions: List[_ScientificConclusionSchema] = Field(default_factory=list)


class IngredientSummaryResult(TypedDict):
    """Return shape of `synthesize_ingredient_summary()`. `scientific_conclusions`
    (Phase 23, renamed Phase 24 — was `recommended_uses`) is the fully-
    scored shape (see that function's docstring for the exact per-item
    dict fields, and for the Phase 24 Direct Injection Safety Net that
    can append additional, non-Gemini-synthesized entries onto this list
    before it's returned) — persisted verbatim onto
    `Ingredient.scientific_conclusions` by the caller
    (app/services/paper_analysis_pipeline.py), not just returned for
    observability as it was through Phase 22."""

    summary_description: str
    main_consensus: str
    scientific_conclusions: List[Dict[str, Any]]


def _format_papers_for_prompt(
    papers: List[ResearchPaper], conclusions_by_paper_id: Dict[int, List[PaperConclusion]]
) -> str:
    """Renders the "SOURCE 2: PEER-REVIEWED SCIENTIFIC PAPERS" section of
    the Phase 17 prompt (Phase 11 originally) — one line per paper: title, grade/score, study design
    (from that paper's own `rubric_evaluation.study_type`, when graded),
    and its "key extracted conclusions" (every already-synthesized
    PaperConclusion claim this paper supports or contradicts, per
    `conclusions_by_paper_id` — built by the caller from the SAME
    PaperConclusion rows process_paper_conclusions() maintains, so this
    reuses existing synthesis work rather than re-deriving findings from
    the raw abstract a second time).
    """
    if not papers:
        return "(No graded peer-reviewed papers are available for this ingredient yet.)"

    lines: List[str] = []
    for paper in papers:
        study_design = "Not yet assessed"
        if paper.rubric_evaluation:
            study_design = paper.rubric_evaluation.get("study_type") or study_design

        related = conclusions_by_paper_id.get(paper.id, [])
        if related:
            claim_bits = []
            for conclusion in related:
                stance = "supports" if paper.id in conclusion.supporting_paper_ids else "contradicts"
                claim_bits.append(f'{stance} "{conclusion.claim_summary}"')
            findings_text = "; ".join(claim_bits)
        else:
            findings_text = "no extracted conclusions linked yet"

        lines.append(
            f'- "{paper.title}" — grade {paper.grade or "ungraded"} '
            f"({paper.grade_score if paper.grade_score is not None else 'n/a'}/100), "
            f"study design: {study_design}. Key extracted conclusions: {findings_text}."
        )
    return "\n".join(lines)


def _format_resources_for_prompt(resources: List[VerifiedResource]) -> str:
    """Renders the "SOURCE 1: OFFICIAL REGULATORY & HEALTH AGENCY STANDS"
    section of the prompt — one structured block per resource: publisher,
    title, authority grade/score (Phase 8), and its extracted
    conclusions.

    **Phase 23 rewrite.** Through Phase 22 this rendered
    `VerifiedResource.extracted_data` (the old Gemini-based Stage 1
    structured shape) — which has been `None` for every resource fetched
    since Phase 21 retired that extractor, silently degrading every one
    of these blocks to the raw-`summary` fallback branch (see module
    docstring's "Phase 21/22" paragraph). This now renders
    `extracted_conclusions` (Phase 19/21 — deterministic, uncapped as of
    Phase 22) instead, one bullet per conclusion, each annotated with its
    `aligned_conclusions` classification (Phase 22 —
    AGREES/CONTRADICTS/DISTINCT_NEW against this ingredient's existing
    paper claims) when available — directly useful signal for this
    module's `official_authority_backing`/`multi_source_consensus` rubric
    categories, since an AGREES/CONTRADICTS tag is exactly the kind of
    cross-source alignment those categories score.

    Falls back to the raw `summary` snippet only for a resource whose
    `extracted_conclusions` is still empty/`None` — expected for a
    resource `resource_parser.py` genuinely found nothing extractable in
    (see `VerifiedResource.extraction_failure_reason`), not a "not yet
    processed" state (extraction now runs synchronously at fetch time,
    Phase 21 — there's no separate step left to not have reached yet).
    """
    if not resources:
        return "(No official verified government/regulatory resources are available for this ingredient yet.)"

    lines: List[str] = []
    for resource in resources:
        grade_text = (
            f'{resource.grade} ({resource.score}/100)'
            if resource.grade and resource.score is not None
            else "ungraded"
        )
        lines.append(
            f'- {resource.publisher} — "{resource.title}" (authority grade {grade_text}):'
        )

        conclusions = resource.extracted_conclusions or []
        if conclusions:
            aligned_by_text = {
                item.get("text"): item
                for item in (resource.aligned_conclusions or [])
                if isinstance(item, dict)
            }
            for conclusion_text in conclusions:
                aligned = aligned_by_text.get(conclusion_text)
                if aligned and aligned.get("alignment"):
                    lines.append(
                        f"    * {conclusion_text} [{aligned['alignment']} vs. existing "
                        f"paper claims]"
                    )
                else:
                    lines.append(f"    * {conclusion_text}")
        else:
            # resource_parser.py found nothing extractable — fall back to
            # the raw summary rather than dropping this resource from the
            # prompt entirely.
            snippet = resource.summary or "No summary available."
            lines.append(f"    (No conclusions extracted.) Raw snippet: {snippet}")

    return "\n".join(lines)


def _build_summary_prompt(
    ingredient_name: str,
    papers: List[ResearchPaper],
    resources: List[VerifiedResource],
    conclusions_by_paper_id: Dict[int, List[PaperConclusion]],
    multi_source_rubric: Dict[str, Any],
) -> str:
    """Builds the Phase 17 ingredient-level synthesis prompt (Stage 2) —
    same overall shape as the task's `PROMPT_TEMPLATE`: two clearly
    labeled, comparably-dense evidence blocks (resources first, papers
    second — see module docstring's "Phase 17" paragraph for why this
    order and this structure specifically), with real evidence text
    interpolated via
    `_format_resources_for_prompt`/`_format_papers_for_prompt`.

    Resources are presented first deliberately: with Stage 1 extraction
    now making the resource block just as compact/structured as the
    paper block, source order shouldn't matter much either way — but
    leading with the source that was previously being crowded out is a
    small, free nudge in the right direction on top of the density fix
    itself.

    `multi_source_rubric` (Phase 23 —
    docs/multi_source_confidence_rubric.json) is rendered via the same
    `_format_rubric_for_prompt` helper `process_paper_conclusions` above
    uses for its own (different) rubric, so `scientific_conclusions`' four
    category scores are grounded in the same real, data-driven tier
    descriptions rather than instructions #5 alone trying to describe the
    scoring scheme inline.

    **Phase 24 — instruction #6 below asks Gemini to account for every
    resource claim, but does NOT itself guarantee it.** This is
    best-effort/complementary to the real guarantee, which is
    `synthesize_ingredient_summary`'s own Python-side Direct Injection
    Safety Net (see that function's docstring and this module's own
    "Phase 24" docstring paragraph) — the instruction alone was already
    shown not to be reliably followed (Gemini was observed still
    dropping resource claims even with SOURCE 1 fully populated), which
    is exactly why Phase 24 added an enforcement pass that doesn't depend
    on Gemini's compliance at all. Kept anyway since a prompt that
    correctly primes Gemini toward the desired behavior means the safety
    net has fewer claims to force-append in practice, even though it's
    not what makes the guarantee true.
    """
    resources_text = _format_resources_for_prompt(resources)
    papers_text = _format_papers_for_prompt(papers, conclusions_by_paper_id)
    rubric_text = _format_rubric_for_prompt(multi_source_rubric)

    zero_evidence_note = ""
    if not papers and resources:
        zero_evidence_note = (
            "\nNOTE: No peer-reviewed papers are available yet — base your synthesis "
            "entirely on the official resources above, and say so plainly in "
            "`summary_description`/`main_consensus` rather than inventing study findings.\n"
        )
    elif papers and not resources:
        zero_evidence_note = (
            "\nNOTE: No official verified resources are available yet — base your "
            "synthesis entirely on the papers below, and say so plainly in "
            "`summary_description`/`main_consensus` rather than inventing regulatory "
            "guidance.\n"
        )

    return (
        "You are an expert scientific synthesizer evaluating evidence for "
        f"the ingredient: '{ingredient_name}'.\n\n"
        "Synthesize an overarching consensus using TWO DISTINCT SOURCES, "
        "given equal weight and consideration below — treat neither as "
        "more authoritative by default; let the actual evidence content "
        "decide.\n\n"
        f"--- SOURCE 1: OFFICIAL REGULATORY & HEALTH AGENCY STANDS "
        f"({len(resources)} total) ---\n"
        f"{resources_text}\n\n"
        f"--- SOURCE 2: PEER-REVIEWED SCIENTIFIC PAPERS ({len(papers)} total) ---\n"
        f"{papers_text}\n"
        f"{zero_evidence_note}\n"
        "INSTRUCTIONS:\n"
        "1. Merge official agency guidelines from SOURCE 1 (RDAs, safety/"
        "upper-limit warnings, approved health claims/official stance) "
        "with the empirical study findings from SOURCE 2 — you MUST "
        "incorporate SOURCE 1 whenever it's non-empty; do not synthesize "
        "your answer from SOURCE 2 alone just because its entries read as "
        "more detailed. When a SOURCE 1 entry states a `recommended_dose` "
        "or `upper_limit_warning`, that figure should generally anchor "
        "`main_consensus`'s own dosage/safety statement unless SOURCE 2 "
        "gives a specific, well-supported reason to state it differently.\n"
        "2. Highlight points where the peer-reviewed literature (SOURCE "
        "2) agrees or conflicts with the official agency stance (SOURCE "
        "1), when both are available.\n"
        "3. Generate a 1-2 sentence `summary_description` for the top of "
        "the UI card (e.g. \"Analyzed 12 studies and 4 official "
        "resources. Official RDA is 90mg/day, supported by moderate "
        "clinical trial evidence for immune health.\") — only state "
        "counts/doses/grades that are actually present in the evidence "
        "above; never fabricate a source count, agency name, or dosage "
        "figure not present in SOURCE 1 or SOURCE 2.\n"
        "4. `main_consensus` should be a fuller sentence or two "
        "summarizing the overall scientific and regulatory baseline, "
        "combining both sources.\n"
        "5. `scientific_conclusions` should list each distinct, specific "
        "scientific conclusion or effect claim the evidence supports — "
        "synthesized claims should combine paper-support counts AND "
        "official resource endorsements where both exist. Score EACH "
        "claim against the four rubric categories below "
        "(paper_evidence_quality_score, official_authority_backing_score, "
        "multi_source_consensus_score, claim_specificity_score) — do NOT "
        "pick a confidence_grade or total_score yourself, only the four "
        "raw category scores; those are derived server-side from your "
        "scores afterward. Count only sources actually listed above for "
        "supporting_study_count/supporting_resource_count, and list only "
        "real sources (never fabricated) in sources_summary. Return an "
        "empty list if the evidence doesn't support any specific claim "
        "yet.\n"
        "6. You MUST attempt to map EACH individual item in SOURCE 1's "
        "resource claims to one of your synthesized `scientific_conclusions` "
        "entries — either by merging it into a broader claim it supports, "
        "or, if it doesn't fit any broader claim, by preserving it as its "
        "own standalone regulatory claim. Do not silently omit a SOURCE 1 "
        "item just because it seems minor or narrow — a specific RDA "
        "figure or safety warning is exactly the kind of claim that "
        "belongs in `scientific_conclusions` on its own if nothing else "
        "covers it.\n\n"
        "GRACEFUL SINGLE-SOURCE HANDLING: a claim backed solely by "
        "SOURCE 2 papers (no SOURCE 1 resource endorses it) should score "
        "official_authority_backing_score at or near 0 — this is "
        "expected and normal, not a penalty to compensate for elsewhere; "
        "conversely a claim backed solely by SOURCE 1 resources (no "
        "paper studies it directly) should score paper_evidence_quality_score "
        "at or near 0. Never inflate one category's score to make up for "
        "a genuinely absent source, and never fabricate paper/resource "
        "support that isn't listed above just to avoid a low score in "
        "either category.\n\n"
        "MULTI-SOURCE CONFIDENCE RUBRIC (score scientific_conclusions "
        "claims against this):\n"
        f"{rubric_text}\n\n"
        "Return your evaluation as the required JSON object."
    )


# --- Phase 24: Direct Injection Safety Net matching helpers ---
#
# Deliberately simple, deterministic text matching — no external NLP/
# fuzzy-matching dependency, same "no external NLP libraries" posture as
# resource_parser.py's own free-text extraction (see that module's
# docstring). This only has to answer one question well enough: "did
# Gemini's synthesized output already account for this specific resource
# conclusion, even if paraphrased/merged into a broader claim?" — not
# reproduce genuine semantic-similarity matching.

# A resource conclusion sharing at least this fraction of its own
# "significant" (4+ letter) words with a synthesized claim's text is
# treated as already represented — tuned loose enough to catch a
# claim that merged/paraphrased the resource text, tight enough that an
# unrelated claim mentioning one or two overlapping words doesn't
# false-positive.
_WORD_OVERLAP_MATCH_THRESHOLD = 0.6
_SIGNIFICANT_WORD_MIN_LENGTH = 4


def _normalize_for_matching(text: str) -> str:
    """Lowercase + collapse whitespace — used for both the substring
    check and as the input to `_significant_words` below."""
    return " ".join(text.lower().split())


def _significant_words(text: str) -> set[str]:
    """Every alphanumeric "word" in `text` at least
    `_SIGNIFICANT_WORD_MIN_LENGTH` characters long, lowercased —
    deliberately excludes short connector words (\"the\", \"and\", \"for\",
    \"a\"...) without needing an explicit stopword list, so overlap scoring
    isn't dominated by words carrying no real distinguishing content."""
    return {
        word
        for word in re.findall(r"[a-z0-9]+", text.lower())
        if len(word) >= _SIGNIFICANT_WORD_MIN_LENGTH
    }


def _is_conclusion_represented(conclusion_text: str, claims: List[Dict[str, Any]]) -> bool:
    """True iff `conclusion_text` (one string from some
    VerifiedResource.extracted_conclusions) is already reasonably
    reflected somewhere in `claims` (the scored `scientific_conclusions`
    list built so far) — checked against each claim's `claim` text AND
    `grade_justification` (Gemini sometimes explains a merge in the
    justification even when the claim text itself is phrased more
    broadly), via two passes:

    1. Normalized substring match, either direction — catches both "the
       claim quotes the resource conclusion near-verbatim" and "the
       resource conclusion is itself a short summary of a longer claim".
    2. Significant-word overlap >= `_WORD_OVERLAP_MATCH_THRESHOLD` of the
       resource conclusion's own significant words — catches a claim that
       paraphrased/merged the resource conclusion into different wording
       without quoting it directly.

    An empty/whitespace-only `conclusion_text` is treated as vacuously
    represented (nothing to guarantee inclusion of); a `conclusion_text`
    with zero significant words falls back to the substring check alone.
    """
    conclusion_norm = _normalize_for_matching(conclusion_text)
    if not conclusion_norm:
        return True
    conclusion_words = _significant_words(conclusion_text)

    for claim in claims:
        haystack = _normalize_for_matching(
            f"{claim.get('claim', '')} {claim.get('grade_justification', '')}"
        )
        if not haystack:
            continue
        if conclusion_norm in haystack or haystack in conclusion_norm:
            return True
        if not conclusion_words:
            continue
        haystack_words = _significant_words(haystack)
        if not haystack_words:
            continue
        overlap = conclusion_words & haystack_words
        if len(overlap) / len(conclusion_words) >= _WORD_OVERLAP_MATCH_THRESHOLD:
            return True
    return False


def synthesize_ingredient_summary(
    session: Session, ingredient_id: int, ingredient_name: str
) -> Optional[IngredientSummaryResult]:
    """Phase 11: one ingredient-level Gemini call synthesizing a
    `summary_description` (plus `main_consensus`/`scientific_conclusions`)
    from BOTH every currently-graded, non-discarded ResearchPaper AND
    every VerifiedResource stored for `ingredient_id` — see the module
    docstring for how this differs from `process_paper_conclusions`
    above.

    Called once per grade request, after the per-paper grade/conclusion
    loop finishes (see
    app/services/paper_analysis_pipeline.py::analyze_ingredient_papers),
    not once per paper.

    **Strict zero-evidence handling** (per spec):
    - If there are ZERO qualifying papers AND ZERO verified resources,
      this makes no Gemini call at all — there is nothing to synthesize
      — and returns `None`. Callers must treat `None` as "nothing to
      persist this run", not as an error.
    - If exactly one of the two collections is empty, synthesis still
      runs (one real evidence source is enough to write a genuine
      summary), but the prompt explicitly tells Gemini which evidence
      type is unavailable so it doesn't fabricate the other (see
      `_build_summary_prompt`'s `zero_evidence_note`), and instructs it
      to say so plainly in the output text rather than silently
      pretending both sources were considered.

    **Phase 24 — Direct Injection Safety Net (`GUARANTEED INCLUSION`).**
    AFTER Gemini's response is parsed and every returned claim is scored
    (see the per-claim scoring loop below), this function makes a second,
    Python-only pass over every VerifiedResource's `extracted_conclusions`
    and force-appends a standalone claim for any conclusion string that
    isn't already reasonably represented somewhere in the scored
    `scientific_conclusions` list — see `_is_conclusion_represented()`'s
    own docstring for the exact matching heuristic. This is a hard
    guarantee, not a best-effort prompt instruction (the prompt DOES also
    ask Gemini to account for every resource claim — see
    `_build_summary_prompt`'s instruction #6 — but that alone was
    observed to still let some claims through unaddressed, which is
    exactly why this enforcement pass exists and doesn't depend on
    Gemini's compliance at all). An injected claim gets a fixed default
    `score_breakdown` (real official-source backing, zero paper evidence,
    not otherwise cross-referenced) run through the exact same
    `_clamp`/`_score_to_grade` derivation as a Gemini-scored claim, so its
    `total_score`/`confidence_grade` are computed consistently, never
    hardcoded.

    Args:
        session: An open SQLModel session.
        ingredient_id: The canonical Ingredient to synthesize for.
        ingredient_name: That Ingredient's `name` (used directly in the
            prompt).

    Returns:
        An `IngredientSummaryResult`, or `None` if there was no evidence
        to synthesize from at all (see above) — this function does NOT
        persist anything itself; the caller (paper_analysis_pipeline.py)
        is responsible for writing `summary_description` AND (Phase 23)
        `scientific_conclusions` onto the Ingredient row. `main_consensus`
        is still returned for observability/future use only — it's the
        one field of this result that remains unpersisted, per spec
        (`summary_description` and, as of Phase 23,
        `scientific_conclusions` are both written to the DB by the
        caller).

    Raises:
        ConclusionGradingError: if the Gemini request fails or its
            response can't be parsed against the expected schema.
            Callers should catch this the same "log and skip, don't fail
            the whole grade request" way every other best-effort Gemini
            call in this pipeline is handled. Note the Phase 24 safety
            net only ever runs AFTER a successful Gemini response, so a
            request that fails here never reaches the injection pass —
            an ingredient with a failed synthesis call still has no
            `scientific_conclusions` update at all this run, exactly as
            before Phase 24.
    """
    papers = session.exec(
        select(ResearchPaper)
        .where(ResearchPaper.ingredient_id == ingredient_id)
        .where(ResearchPaper.status != PAPER_STATUS_DISCARDED_IRRELEVANT)
        .where(ResearchPaper.grade.is_not(None))
        .order_by(ResearchPaper.created_at.desc())
    ).all()

    resources = session.exec(
        select(VerifiedResource)
        .where(VerifiedResource.ingredient_id == ingredient_id)
        .order_by(VerifiedResource.created_at.desc())
    ).all()

    if not papers and not resources:
        logger.info(
            "No graded papers or verified resources available for ingredient "
            "%r (id=%s) — skipping ingredient summary synthesis.",
            ingredient_name,
            ingredient_id,
        )
        return None

    # Map paper.id -> every active PaperConclusion that lists it as
    # supporting or contradicting — reused as each paper's "key extracted
    # conclusions" in the prompt (see _format_papers_for_prompt) rather
    # than re-deriving findings from the raw abstract a second time.
    conclusions_by_paper_id: Dict[int, List[PaperConclusion]] = {}
    if papers:
        all_conclusions = session.exec(
            select(PaperConclusion)
            .where(PaperConclusion.ingredient_id == ingredient_id)
            .where(PaperConclusion.is_active.is_(True))
        ).all()
        for conclusion in all_conclusions:
            for paper_id in (*conclusion.supporting_paper_ids, *conclusion.contradicting_paper_ids):
                conclusions_by_paper_id.setdefault(paper_id, []).append(conclusion)

    multi_source_rubric = _load_multi_source_rubric()

    client = _get_client()
    settings = get_settings()
    prompt = _build_summary_prompt(
        ingredient_name, papers, resources, conclusions_by_paper_id, multi_source_rubric
    )

    # Debug visibility into exactly what evidence is (and isn't) reaching
    # this prompt — added specifically to make it trivial to confirm, from
    # the server logs alone, that VerifiedResource rows are actually
    # flowing into ingredient-summary synthesis rather than silently being
    # dropped somewhere upstream. Deliberately `logger.info` (not
    # `logger.debug`) for all three lines, not just the counts — this
    # process's default logging config may not surface DEBUG-level
    # records, and the whole point of these lines is to be visible
    # without needing a logging-config change first. Uses this module's
    # own `logger` (stdlib `logging`, same convention as every other
    # service in this app — see e.g. resource_fetcher.py's
    # `[ResourceFetcher]`-prefixed lines) rather than bare `print()` calls,
    # so these lines are still visible in the console (uvicorn/FastAPI
    # sends both to the same stream) while also going through normal log
    # formatting/level filtering/handlers like everything else here.
    # Phase 23: this used to count VerifiedResource.extracted_data (the
    # retired Gemini Stage 1 field, permanently None since Phase 21 — see
    # module docstring's "Phase 21/22" paragraph); now counts
    # extracted_conclusions (Phase 19/21, what _format_resources_for_prompt
    # actually renders as of this phase) so the log line reflects reality.
    resources_with_extracted_conclusions = sum(
        1 for r in resources if r.extracted_conclusions
    )
    logger.info(
        "[ConclusionGrader Debug] Papers count: %d",
        len(papers),
    )
    logger.info(
        "[ConclusionGrader Debug] Resources count: %d (%d with "
        "extracted_conclusions, %d falling back to raw summary text)",
        len(resources),
        resources_with_extracted_conclusions,
        len(resources) - resources_with_extracted_conclusions,
    )
    logger.info(
        "[ConclusionGrader Debug] Formatted Resources Payload (SOURCE 1):\n%s",
        _format_resources_for_prompt(resources),
    )

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_IngredientSummarySchema,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surface as a clean service error
        raise ConclusionGradingError(f"Gemini request failed: {exc}") from exc

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, _IngredientSummarySchema):
        raw_text = getattr(response, "text", None)
        if not raw_text:
            raise ConclusionGradingError("Gemini returned an empty response.")
        try:
            parsed = _IngredientSummarySchema.model_validate_json(raw_text)
        except Exception as exc:  # noqa: BLE001
            raise ConclusionGradingError(
                f"Gemini response did not match the expected schema: {exc}"
            ) from exc

    summary_description = (parsed.summary_description or "").strip()
    if not summary_description:
        raise ConclusionGradingError("Gemini returned an empty summary_description.")

    # Phase 23: server-derived scoring — see module docstring's "Phase
    # 23" paragraph for why Gemini only supplies the four raw category
    # scores below (`_ScientificConclusionSchema`) and never a grade/total
    # directly, same "never trust Gemini's own bound-following"
    # convention as `process_paper_conclusions` above and paper_grader.py.
    # Category bounds are also reused by the Phase 24 safety net below, so
    # an injected claim's fixed default scores get clamped against the
    # exact same rubric bounds as a Gemini-scored one.
    category_bounds = {
        category["id"]: (category.get("min_score", 0), category["max_score"])
        for category in multi_source_rubric.get("categories", [])
    }
    paper_min, paper_max = category_bounds.get("paper_evidence_quality", (0, 30))
    authority_min, authority_max = category_bounds.get("official_authority_backing", (0, 25))
    consensus_min, consensus_max = category_bounds.get("multi_source_consensus", (0, 25))
    specificity_min, specificity_max = category_bounds.get("claim_specificity", (0, 20))

    scientific_conclusions: List[Dict[str, Any]] = []
    for use in parsed.scientific_conclusions:
        claim = use.claim.strip()
        if not claim:
            # Defensive — an empty claim string isn't a usable
            # scientific-conclusion item; skip rather than persist a
            # blank row.
            continue

        paper_score = _clamp(use.paper_evidence_quality_score, paper_min, paper_max)
        authority_score = _clamp(
            use.official_authority_backing_score, authority_min, authority_max
        )
        consensus_score = _clamp(use.multi_source_consensus_score, consensus_min, consensus_max)
        specificity_score = _clamp(use.claim_specificity_score, specificity_min, specificity_max)
        total_score = _clamp(
            paper_score + authority_score + consensus_score + specificity_score, 0, 100
        )
        confidence_grade = _score_to_grade(total_score, multi_source_rubric)

        # sources_summary: short labels only, empty/whitespace entries
        # dropped, capped at 6 — same "clamp Gemini's own list length"
        # convention as resource_parser.py's item caps elsewhere in this
        # codebase (though here the cap is generous/defensive rather than
        # the main point — sources_summary is meant to be a handful of
        # labels, not a restatement of every source).
        sources_summary = [s.strip() for s in use.sources_summary if s and s.strip()][:6]

        scientific_conclusions.append(
            {
                "claim": claim,
                "confidence_grade": confidence_grade,
                "total_score": total_score,
                "score_breakdown": {
                    "paper_evidence_quality": paper_score,
                    "official_authority_backing": authority_score,
                    "multi_source_consensus": consensus_score,
                    "claim_specificity": specificity_score,
                },
                "supporting_study_count": max(0, use.supporting_study_count),
                "supporting_resource_count": max(0, use.supporting_resource_count),
                "sources_summary": sources_summary,
                "grade_justification": use.grade_justification.strip(),
            }
        )

    # --- Phase 24: Direct Injection Safety Net (GUARANTEED INCLUSION) —
    # see this function's own docstring's "Phase 24" paragraph and the
    # module docstring's "Phase 24" section for the full design. Runs
    # AFTER every Gemini-synthesized claim above has been scored, so
    # `_is_conclusion_represented` checks against the complete, final
    # Gemini-derived list before deciding anything needs injecting. Fixed
    # default score_breakdown per the task spec: real official-source
    # backing (official_authority_backing near its max), zero paper
    # evidence, moderate-but-not-verified consensus/specificity — clamped
    # to the rubric's own bounds (defensive: covers a hypothetical future
    # rubric revision narrowing these categories below the task's literal
    # 20/12/14 defaults) and put through the exact same
    # `_clamp`/`_score_to_grade` derivation as every Gemini-scored claim
    # above, so total_score/confidence_grade are computed identically,
    # never hardcoded.
    injected_count = 0
    for resource in resources:
        source_label = resource.publisher or resource.title or "Official Source"
        for conclusion_text in resource.extracted_conclusions or []:
            if _is_conclusion_represented(conclusion_text, scientific_conclusions):
                continue

            paper_score = _clamp(0, paper_min, paper_max)
            authority_score = _clamp(20, authority_min, authority_max)
            consensus_score = _clamp(12, consensus_min, consensus_max)
            specificity_score = _clamp(14, specificity_min, specificity_max)
            total_score = _clamp(
                paper_score + authority_score + consensus_score + specificity_score, 0, 100
            )
            confidence_grade = _score_to_grade(total_score, multi_source_rubric)

            injected_claim = {
                "claim": conclusion_text,
                "confidence_grade": confidence_grade,
                "total_score": total_score,
                "score_breakdown": {
                    "paper_evidence_quality": paper_score,
                    "official_authority_backing": authority_score,
                    "multi_source_consensus": consensus_score,
                    "claim_specificity": specificity_score,
                },
                "supporting_study_count": 0,
                "supporting_resource_count": 1,
                "sources_summary": [source_label],
                "grade_justification": (
                    f"Direct regulatory conclusion extracted from official source: "
                    f"{source_label}."
                ),
            }
            scientific_conclusions.append(injected_claim)
            injected_count += 1

    if injected_count:
        logger.info(
            "[ConclusionGrader Debug] Phase 24 safety net injected %d "
            "omitted resource conclusion(s) as standalone claim(s) for "
            "ingredient %r (id=%s).",
            injected_count,
            ingredient_name,
            ingredient_id,
        )

    return {
        "summary_description": summary_description,
        "main_consensus": (parsed.main_consensus or "").strip(),
        "scientific_conclusions": scientific_conclusions,
    }
