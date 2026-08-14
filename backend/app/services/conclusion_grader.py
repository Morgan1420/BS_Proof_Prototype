"""Gemini-backed cross-paper conclusion synthesis (Phase 5).

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
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models.research import PaperConclusion, ResearchPaper

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

    Gatekept by MIN_GRADE_SCORE_FOR_CONCLUSIONS: returns False
    immediately (no Gemini call) if `paper.grade_score` is missing or
    <= 50, per spec — a paper that hasn't been graded yet, or graded too
    low, shouldn't contribute to the ingredient's synthesized
    conclusions.

    Commits its own changes (new PaperConclusion rows + updates to
    existing ones) — callers (app/services/paper_analysis_pipeline.py)
    don't need to commit afterward for this step, and per that module's
    error-handling design, a failure here leaves the session rolled back
    to its last commit, not partially applied.

    Returns:
        True if Gemini was actually called and its result (possibly
        empty merged/new lists) was committed; False if skipped entirely
        by the gatekeeper check above.

    Raises:
        ConclusionGradingError: if the rubric can't load, the Gemini
            request fails, the response can't be parsed, or the DB
            commit fails (session is rolled back first in that case).
    """
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
