"""Resource-conclusion claim alignment (Phase 22).

For one ingredient, classifies every string already sitting in each of
its `VerifiedResource.extracted_conclusions` (Phase 21's deterministic
parser output, maximized in Phase 22 — see `resource_parser.py`) against
the ingredient's existing `PaperConclusion.claim_summary` rows, as one
of three labels:

  - `AGREES` — the resource conclusion directly reinforces/aligns with
    an existing paper claim (e.g. an RDA statement matching a study's
    own recommended dosage).
  - `CONTRADICTS` — the resource conclusion rebuts, reduces confidence
    in, or flags a safety concern conflicting with an existing paper
    claim (e.g. a regulatory upper-limit warning where the papers make
    no such caveat).
  - `DISTINCT_NEW` — the resource conclusion introduces a regulatory
    statement, RDA boundary, or observation the papers don't cover at
    all — neither agreeing nor conflicting, just new information.

Result is stored on each `VerifiedResource.aligned_conclusions` — see
that field's own docstring in `app/models/research.py` for the exact
per-item shape (`text`/`alignment`/`target_claim`/`notes`).

**One Gemini call per INGREDIENT, not per resource.** Every resource's
`extracted_conclusions` for the ingredient are pooled into a single
prompt and classified together in one request — mirrors
`conclusion_grader.py::synthesize_ingredient_summary`'s own "one call
per grade request, not one per paper/resource" reasoning (see that
module's docstring), for the same reason: this app runs on a Gemini
free-tier rate limit (see `gemini_rate_limit.py`), and a resource-conclusion
count that can now run arbitrarily high per resource (Phase 22 removed
the old `_MAX_CONCLUSIONS = 4` cap in `resource_parser.py` — see that
module's docstring) makes "one call per resource" a much worse multiplier
than it would have been under the old cap. A single ingredient-level call
covering every resource's conclusions at once keeps this at the same
one-call-per-step cadence every other Gemini-backed step in this pipeline
already uses.

**Index-based mapping, never trusting echoed text.** The prompt assigns
every resource conclusion and every existing paper claim a stable integer
index; Gemini's structured response references conclusions/claims by
index only (`conclusion_index`, `target_claim_index`) — never asked to
reproduce the conclusion or claim text itself. The `text` and
`target_claim` fields actually persisted onto `aligned_conclusions` are
always looked up from the server's own original strings by that index
afterward, never taken from whatever Gemini's response happened to
contain. This avoids the same paraphrase-drift risk
`resource_parser.py`'s "deterministic, not Gemini" rationale and this
codebase's Phase 19 extraction schemas were already built around —
Gemini classifies, it doesn't get to rewrite the record.

**Deterministic short-circuit: no existing claims means everything is
DISTINCT_NEW, by definition — no Gemini call needed.** If an ingredient
has zero active `PaperConclusion` rows (e.g. no papers graded yet, or all
graded papers scored too low to produce a conclusion), there is nothing
for a resource conclusion to agree or contradict — every one of them is
trivially, correctly `DISTINCT_NEW` without needing a model's judgment at
all. This is both a rate-limit-friendly optimization (skips a Gemini call
entirely for an ingredient still early in its evidence-gathering
lifecycle) and the only *correct* answer for that case — asking Gemini to
classify against an empty claim list would just be an expensive way of
producing the same result with a chance of hallucinating a
comparison that doesn't exist.

**Strict fallback on failure — task requirement.** If the batched Gemini
call itself fails (rate limit exhausted past retry, malformed response,
network error) every conclusion for the ingredient falls back to
`DISTINCT_NEW` with an explanatory `notes` string
(`"Alignment classification unavailable: <reason>"`) — never guessed into
`AGREES`/`CONTRADICTS` without real model evidence backing that specific
label, and never silently dropped (a resource with `extracted_conclusions`
always ends up with an equal-length `aligned_conclusions`, classified or
fallback-classified, never left as `None`/mismatched after this function
runs). Same "log and degrade gracefully, never abort the whole grade
request over one step" philosophy as every other best-effort step in
`app/services/paper_analysis_pipeline.py`.

Wired through `app/services/gemini_rate_limit.py` (`throttle_gemini_call`
+ `call_gemini_with_retry`) exactly like `paper_grader.py` — this is a new
Gemini call site, so unlike `conclusion_grader.py`'s two call sites (a
documented, pre-existing gap — see `paper_analysis_pipeline.py`'s "Known
gap" section), there's no reason to skip rate-limit protection here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Literal, Optional, Tuple

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models.research import PaperConclusion, VerifiedResource
from app.services.gemini_rate_limit import call_gemini_with_retry, throttle_gemini_call

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ResourceAligner]"


class AlignmentError(RuntimeError):
    """Raised when Gemini fails to return a usable alignment
    classification for an ingredient's pooled resource conclusions.

    Callers (currently only `align_resource_conclusions_for_ingredient`
    below) catch this and apply the DISTINCT_NEW-with-explanatory-note
    fallback described in this module's docstring — this is never allowed
    to propagate out and abort the caller's grade request, same as
    `ConclusionGradingError`/`PaperGradingError`/`ResourceExtractionError`
    elsewhere in this codebase.
    """


@lru_cache
def _get_client() -> genai.Client:
    """Cached Gemini client — see research_keywords.py's `_get_client`
    for why this isn't shared with the other Gemini-using services
    directly (equivalent client, separate `@lru_cache` entry per
    module)."""
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key)


# --- Structured Gemini response schema ---


class _AlignedConclusionSchema(BaseModel):
    conclusion_index: int = Field(
        description="The index (from the numbered list below) of the resource conclusion being classified."
    )
    alignment: Literal["AGREES", "CONTRADICTS", "DISTINCT_NEW"] = Field(
        description=(
            "AGREES if this reinforces an existing paper claim, "
            "CONTRADICTS if it rebuts/conflicts with or raises a safety "
            "concern against one, DISTINCT_NEW if it's regulatory/RDA/"
            "observational information the papers don't cover at all."
        )
    )
    target_claim_index: Optional[int] = Field(
        default=None,
        description=(
            "The index of the specific existing paper claim this AGREES "
            "or CONTRADICTS with. Omit/null for DISTINCT_NEW."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description="One short sentence explaining the classification.",
    )


class _AlignmentResponseSchema(BaseModel):
    classifications: List[_AlignedConclusionSchema] = Field(default_factory=list)


# --- Flat, indexed data shapes passed between the orchestration function
# and the pure classification function below ---


@dataclass
class _IndexedConclusion:
    index: int
    resource_id: int
    text: str


@dataclass
class _IndexedClaim:
    index: int
    text: str


def _build_prompt(
    conclusions: List[_IndexedConclusion], claims: List[_IndexedClaim], ingredient_name: str
) -> str:
    """Renders the pooled, indexed conclusion/claim lists into one prompt
    — indices only are ever asked to be echoed back (see module docstring
    for why: avoids trusting Gemini to reproduce text verbatim)."""
    conclusion_lines = [f"{item.index}. {item.text}" for item in conclusions]
    claim_lines = [f"{item.index}. {item.text}" for item in claims]

    return (
        f"You are cross-referencing official/regulatory resource statements "
        f"about the supplement ingredient {ingredient_name!r} against a "
        f"running list of scientific claims already synthesized from "
        f"peer-reviewed papers about the same ingredient.\n\n"
        f"EXISTING PAPER CLAIMS (indexed):\n" + "\n".join(claim_lines) + "\n\n"
        f"RESOURCE CONCLUSIONS TO CLASSIFY (indexed):\n" + "\n".join(conclusion_lines) + "\n\n"
        "For EVERY resource conclusion listed above, classify it as exactly "
        "one of:\n"
        "- AGREES: it directly aligns with or reinforces one of the "
        "existing paper claims (set target_claim_index to that claim's "
        "index).\n"
        "- CONTRADICTS: it rebuts, reduces confidence in, or flags a "
        "safety concern conflicting with one of the existing paper claims "
        "(set target_claim_index to that claim's index).\n"
        "- DISTINCT_NEW: it introduces a regulatory statement, RDA/upper-"
        "limit boundary, or observation not covered by any existing paper "
        "claim (leave target_claim_index unset).\n\n"
        "Return one classification entry per resource conclusion index, "
        "referencing conclusions and claims ONLY by their numeric index — "
        "do not restate their text. Include a short one-sentence 'notes' "
        "explanation for each."
    )


def _classify_conclusions(
    conclusions: List[_IndexedConclusion],
    claims: List[_IndexedClaim],
    ingredient_name: str,
) -> Dict[int, _AlignedConclusionSchema]:
    """Pure Gemini-calling classification step — makes exactly ONE
    request covering every conclusion in `conclusions` at once (see
    module docstring). Returns a dict keyed by `conclusion_index` for
    every entry Gemini classified; a caller-side index missing from the
    result (e.g. Gemini only classified some of them) is the caller's
    responsibility to fall back on — see
    `align_resource_conclusions_for_ingredient` below.

    Raises:
        AlignmentError: on any request failure, empty response, or
            schema-validation failure — see this module's docstring for
            the fallback behavior callers apply in response.
    """
    client = _get_client()
    settings = get_settings()
    prompt = _build_prompt(conclusions, claims, ingredient_name)

    throttle_gemini_call()
    try:
        response = call_gemini_with_retry(
            lambda: client.models.generate_content(
                model=settings.gemini_model,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_AlignmentResponseSchema,
                ),
            ),
            label=f"aligning resource conclusions for ingredient {ingredient_name!r}",
        )
    except Exception as exc:  # noqa: BLE001 - surface as a clean service error
        raise AlignmentError(f"Gemini request failed: {exc}") from exc

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, _AlignmentResponseSchema):
        raw_text = getattr(response, "text", None)
        if not raw_text:
            raise AlignmentError("Gemini returned an empty response.")
        try:
            parsed = _AlignmentResponseSchema.model_validate_json(raw_text)
        except Exception as exc:  # noqa: BLE001
            raise AlignmentError(
                f"Gemini response did not match the expected schema: {exc}"
            ) from exc

    return {item.conclusion_index: item for item in parsed.classifications}


def _fallback_entry(text: str, reason: str) -> dict:
    """The strict-fallback shape used whenever a conclusion couldn't be
    classified by Gemini — always DISTINCT_NEW, never a guessed AGREES/
    CONTRADICTS (see module docstring's "Strict fallback on failure")."""
    return {
        "text": text,
        "alignment": "DISTINCT_NEW",
        "target_claim": None,
        "notes": f"Alignment classification unavailable: {reason}",
    }


def _deterministic_new_entry(text: str) -> dict:
    """The short-circuit shape used when there are simply no existing
    paper claims yet to compare against — also DISTINCT_NEW, but with a
    notes string that reflects this is the *correct* answer, not a
    degraded fallback (see module docstring's "Deterministic
    short-circuit")."""
    return {
        "text": text,
        "alignment": "DISTINCT_NEW",
        "target_claim": None,
        "notes": "No existing paper claims yet to compare against for this ingredient.",
    }


@dataclass
class AlignmentResult:
    """Summary of one align_resource_conclusions_for_ingredient() run —
    informational only, mirrors PipelineResult in
    paper_analysis_pipeline.py (nothing branches on this beyond logging;
    whatever succeeded is already durably saved)."""

    resources_considered: int = 0
    conclusions_considered: int = 0
    existing_claims_considered: int = 0
    # True iff a real Gemini classification call ran (i.e. existing_claims
    # was non-empty) — False for the deterministic short-circuit case
    # (existing_claims_considered == 0) or when there were zero
    # conclusions to classify in the first place.
    gemini_call_made: bool = False
    # True iff the Gemini call was attempted but failed, so every
    # conclusion this run fell back to the DISTINCT_NEW-with-note default
    # rather than a real classification.
    fallback_used: bool = False
    resources_updated: int = 0


def align_resource_conclusions_for_ingredient(
    session: Session, ingredient_id: int, ingredient_name: str
) -> AlignmentResult:
    """Classifies every `VerifiedResource.extracted_conclusions` string
    for `ingredient_id` as AGREES/CONTRADICTS/DISTINCT_NEW against the
    ingredient's active `PaperConclusion` rows, and persists the result
    onto each resource's own `aligned_conclusions` column.

    Intended call site: `app/services/paper_analysis_pipeline.py`'s
    `analyze_ingredient_papers()`, AFTER both the per-paper conclusion
    loop and the Stage 2 `synthesize_ingredient_summary()` call have run
    — so this always classifies against the most up-to-date set of paper
    claims available for this grade request, not a stale set from before
    this run's own paper grading updated them.

    Safe to call repeatedly for the same ingredient: every call recomputes
    and overwrites `aligned_conclusions` fresh from current
    `extracted_conclusions`/`PaperConclusion` state — no partial/
    incremental merging to worry about, unlike per-paper conclusion
    synthesis.

    Never raises for a Gemini-call failure — see module docstring's
    "Strict fallback on failure". Only propagates if something outside
    the classification step itself breaks (e.g. the initial resource/
    claim lookup queries failing), which would indicate a broken session/
    DB connection rather than a transient Gemini issue.

    Args:
        session: An open SQLModel session.
        ingredient_id: The canonical Ingredient whose VerifiedResource
            rows should be classified.
        ingredient_name: That same Ingredient's `name` — used only for
            the Gemini prompt and log lines (see `_build_prompt`).

    Returns:
        An AlignmentResult summarizing what happened — see its docstring.
    """
    resources = session.exec(
        select(VerifiedResource).where(VerifiedResource.ingredient_id == ingredient_id)
    ).all()
    resources_with_conclusions = [r for r in resources if r.extracted_conclusions]

    result = AlignmentResult(resources_considered=len(resources_with_conclusions))

    if not resources_with_conclusions:
        logger.info(
            "%s No VerifiedResource rows with extracted_conclusions for "
            "ingredient id=%s (%r) — nothing to align.",
            _LOG_PREFIX,
            ingredient_id,
            ingredient_name,
        )
        return result

    # --- Flatten every resource's extracted_conclusions into one
    # globally-indexed list, remembering which resource each came from so
    # results can be mapped back afterward (see module docstring's
    # "Index-based mapping" section). ---
    indexed_conclusions: List[_IndexedConclusion] = []
    conclusions_by_resource: Dict[int, List[Tuple[int, str]]] = {}
    running_index = 0
    for resource in resources_with_conclusions:
        per_resource: List[Tuple[int, str]] = []
        for text in resource.extracted_conclusions or []:
            indexed_conclusions.append(
                _IndexedConclusion(index=running_index, resource_id=resource.id, text=text)
            )
            per_resource.append((running_index, text))
            running_index += 1
        conclusions_by_resource[resource.id] = per_resource

    result.conclusions_considered = len(indexed_conclusions)

    existing_claims = session.exec(
        select(PaperConclusion)
        .where(PaperConclusion.ingredient_id == ingredient_id)
        .where(PaperConclusion.is_active.is_(True))
    ).all()
    result.existing_claims_considered = len(existing_claims)

    indexed_claims = [
        _IndexedClaim(index=i, text=claim.claim_summary) for i, claim in enumerate(existing_claims)
    ]
    claim_text_by_index = {item.index: item.text for item in indexed_claims}

    # --- Deterministic short-circuit: zero existing claims means every
    # conclusion is trivially DISTINCT_NEW — no Gemini call needed (see
    # module docstring). ---
    if not indexed_claims:
        logger.info(
            "%s No active PaperConclusion rows for ingredient id=%s (%r) — "
            "classifying all %d resource conclusion(s) as DISTINCT_NEW "
            "without a Gemini call.",
            _LOG_PREFIX,
            ingredient_id,
            ingredient_name,
            len(indexed_conclusions),
        )
        classifications_by_index: Dict[int, dict] = {
            item.index: _deterministic_new_entry(item.text) for item in indexed_conclusions
        }
    else:
        try:
            classified = _classify_conclusions(indexed_conclusions, indexed_claims, ingredient_name)
            result.gemini_call_made = True
            classifications_by_index = {}
            for item in indexed_conclusions:
                match = classified.get(item.index)
                if match is None:
                    # Gemini omitted this index from its response — strict
                    # fallback, never guessed.
                    classifications_by_index[item.index] = _fallback_entry(
                        item.text, "not classified in model response"
                    )
                    continue
                target_claim_text = (
                    claim_text_by_index.get(match.target_claim_index)
                    if match.target_claim_index is not None
                    else None
                )
                classifications_by_index[item.index] = {
                    "text": item.text,
                    "alignment": match.alignment,
                    "target_claim": target_claim_text,
                    "notes": match.notes,
                }
        except AlignmentError as exc:
            logger.warning(
                "%s Alignment classification failed for ingredient id=%s "
                "(%r) — falling back to DISTINCT_NEW for all %d resource "
                "conclusion(s): %s",
                _LOG_PREFIX,
                ingredient_id,
                ingredient_name,
                len(indexed_conclusions),
                exc,
            )
            result.fallback_used = True
            classifications_by_index = {
                item.index: _fallback_entry(item.text, str(exc)) for item in indexed_conclusions
            }

    # --- Map classifications back onto each resource's own
    # aligned_conclusions column, preserving that resource's original
    # extracted_conclusions order. ---
    for resource in resources_with_conclusions:
        per_resource_indices = conclusions_by_resource[resource.id]
        resource.aligned_conclusions = [
            classifications_by_index[index] for index, _text in per_resource_indices
        ]
        session.add(resource)

    try:
        session.commit()
        result.resources_updated = len(resources_with_conclusions)
    except Exception as exc:  # noqa: BLE001 - same "log, don't fail" reasoning as callers elsewhere
        session.rollback()
        logger.warning(
            "%s Failed to save aligned_conclusions for ingredient id=%s "
            "(%r): %s",
            _LOG_PREFIX,
            ingredient_id,
            ingredient_name,
            exc,
        )

    return result
