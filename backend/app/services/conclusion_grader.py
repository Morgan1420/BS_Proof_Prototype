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
`main_consensus`/`recommended_uses`, returned for observability/future
use — see that function's docstring for exactly what's persisted).
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
"""

from __future__ import annotations

import json
import logging
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


class _RecommendedUseSchema(BaseModel):
    """One synthesized recommended-use claim, as returned inside
    `_IngredientSummarySchema.recommended_uses` — mirrors the task's
    `PROMPT_TEMPLATE` output schema exactly (`claim`/`confidence_grade`/
    `supporting_study_count`/`supporting_resource_count`/`notes`).

    NOT persisted anywhere today (see `synthesize_ingredient_summary`'s
    docstring for what IS persisted) — this is intentionally a separate,
    coarser-grained shape from the existing per-claim `PaperConclusion`
    table above, since it additionally counts *resource* support
    (`supporting_resource_count`), a dimension PaperConclusion has never
    tracked. Returned from `synthesize_ingredient_summary` for
    observability/future use rather than dropped, even though only
    `summary_description` is written to the DB by this pass.
    """

    claim: str = Field(description="A short, specific recommended-use or effect claim.")
    confidence_grade: Literal["A", "B", "C", "D", "E"] = Field(
        description="Overall confidence grade for this claim, combining paper + resource evidence."
    )
    supporting_study_count: int = Field(
        default=0, description="How many of the provided papers support this claim."
    )
    supporting_resource_count: int = Field(
        default=0, description="How many of the provided verified resources support this claim."
    )
    notes: str = Field(description="One concise sentence of supporting rationale.")


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
    recommended_uses: List[_RecommendedUseSchema] = Field(default_factory=list)


class IngredientSummaryResult(TypedDict):
    """Return shape of `synthesize_ingredient_summary()`."""

    summary_description: str
    main_consensus: str
    recommended_uses: List[Dict[str, Any]]


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
    section of the Phase 17 prompt — one structured block per resource:
    publisher, title, authority grade/score (Phase 8), and its Stage 1
    `extracted_data` (official stance / recommended dose / upper-limit
    warning / key takeaways — app/services/resource_extractor.py),
    rendered as compact, uniformly-shaped bullet lines so this section
    carries evidence density comparable to `_format_papers_for_prompt`'s
    per-paper lines below, rather than a raw, unevenly-detailed snippet —
    see module docstring's "Phase 17" paragraph for why this replaced the
    old raw-summary rendering.

    Falls back to the pre-Phase-17 raw-`summary` rendering for a resource
    whose `extracted_data` is still `None` — expected only for a resource
    Stage 1 hasn't reached yet (a brand-new resource fetched but not yet
    extraction-attempted this run) or one whose Stage 1 Gemini call
    itself failed (see paper_analysis_pipeline.py's Stage 1 loop); never
    for a resource Stage 1 successfully ran on, since even its "nothing
    extractable" outcome is stored as a real, non-None dict with empty
    fields (see resource_extractor.py's short-snippet guard) rather than
    a bare `None`.
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

        extracted = resource.extracted_data
        if extracted:
            official_stance = extracted.get("official_stance") or "Not stated by this source."
            recommended_dose = extracted.get("recommended_dose") or "Not stated by this source."
            upper_limit_warning = (
                extracted.get("upper_limit_warning") or "Not stated by this source."
            )
            key_takeaways = [t for t in (extracted.get("key_takeaways") or []) if t]

            lines.append(f"    Official stance: {official_stance}")
            lines.append(f"    Recommended dose: {recommended_dose}")
            lines.append(f"    Upper limit warning: {upper_limit_warning}")
            if key_takeaways:
                lines.append("    Key takeaways:")
                for takeaway in key_takeaways:
                    lines.append(f"      * {takeaway}")
            else:
                lines.append("    Key takeaways: (none extracted)")
        else:
            # Stage 1 hasn't successfully extracted this one yet — fall
            # back to its raw summary rather than dropping it from the
            # prompt entirely.
            snippet = resource.summary or "No summary available."
            lines.append(f"    (Not yet processed by Stage 1 extraction.) Raw snippet: {snippet}")

    return "\n".join(lines)


def _build_summary_prompt(
    ingredient_name: str,
    papers: List[ResearchPaper],
    resources: List[VerifiedResource],
    conclusions_by_paper_id: Dict[int, List[PaperConclusion]],
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
    """
    resources_text = _format_resources_for_prompt(resources)
    papers_text = _format_papers_for_prompt(papers, conclusions_by_paper_id)

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
        "5. `recommended_uses` should list each distinct, specific "
        "recommended-use or effect claim the evidence supports — "
        "synthesized claims should combine paper-support counts AND "
        "official resource endorsements where both exist, each with a "
        "confidence grade and accurate supporting study/resource counts "
        "(counting only sources actually listed above) — return an empty "
        "list if the evidence doesn't support any specific claim yet.\n\n"
        "Return your evaluation as the required JSON object."
    )


def synthesize_ingredient_summary(
    session: Session, ingredient_id: int, ingredient_name: str
) -> Optional[IngredientSummaryResult]:
    """Phase 11: one ingredient-level Gemini call synthesizing a
    `summary_description` (plus `main_consensus`/`recommended_uses`) from
    BOTH every currently-graded, non-discarded ResearchPaper AND every
    VerifiedResource stored for `ingredient_id` — see the module
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

    Args:
        session: An open SQLModel session.
        ingredient_id: The canonical Ingredient to synthesize for.
        ingredient_name: That Ingredient's `name` (used directly in the
            prompt).

    Returns:
        An `IngredientSummaryResult`, or `None` if there was no evidence
        to synthesize from at all (see above) — this function does NOT
        persist anything itself; the caller (paper_analysis_pipeline.py)
        is responsible for writing `summary_description` onto the
        Ingredient row. `main_consensus`/`recommended_uses` are returned
        for observability/future use but are not currently persisted
        anywhere — only `summary_description` is, per spec.

    Raises:
        ConclusionGradingError: if the Gemini request fails or its
            response can't be parsed against the expected schema.
            Callers should catch this the same "log and skip, don't fail
            the whole grade request" way every other best-effort Gemini
            call in this pipeline is handled.
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

    client = _get_client()
    settings = get_settings()
    prompt = _build_summary_prompt(ingredient_name, papers, resources, conclusions_by_paper_id)

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
    resources_with_extracted_data = sum(1 for r in resources if r.extracted_data is not None)
    logger.info(
        "[ConclusionGrader Debug] Papers count: %d",
        len(papers),
    )
    logger.info(
        "[ConclusionGrader Debug] Resources count: %d (%d with Stage 1 "
        "extracted_data, %d falling back to raw summary text)",
        len(resources),
        resources_with_extracted_data,
        len(resources) - resources_with_extracted_data,
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

    recommended_uses = [
        {
            "claim": use.claim.strip(),
            "confidence_grade": use.confidence_grade,
            "supporting_study_count": max(0, use.supporting_study_count),
            "supporting_resource_count": max(0, use.supporting_resource_count),
            "notes": use.notes.strip(),
        }
        for use in parsed.recommended_uses
    ]

    return {
        "summary_description": summary_description,
        "main_consensus": (parsed.main_consensus or "").strip(),
        "recommended_uses": recommended_uses,
    }
