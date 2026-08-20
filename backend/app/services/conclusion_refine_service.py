"""Post-processing refinement pass for deterministically-extracted
VerifiedResource conclusions (Phase 40).

**The problem this solves.** `resource_parser.py`'s Phase 21/39
deterministic extraction (and its Phase 27 HTML-fallback companion) is
precise about WHERE it reads text from, but neither has any real
understanding of whether a given sentence is actually a useful,
ingredient-specific scientific statement — a MedlinePlus health-topic
summary genuinely can contain a medical disclaimer ("Talk to your doctor
before taking any supplement"), generic dietary filler ("The best way to
get vitamins is from a balanced diet"), or a near-duplicate paraphrase of
a sentence already kept from the same page. `is_valid_human_conclusion`
(resource_parser.py) catches raw API metadata/boilerplate, but it's a
cheap regex/length check — it was never meant to judge whether a
grammatically valid sentence is scientifically substantive or on-topic
for the specific ingredient being graded.

**What this module does — and, importantly, what it does NOT do.**
`refine_conclusions()` makes ONE Gemini call per resource to (1) drop
generic/off-topic/boilerplate items and (2) merge near-duplicate
paraphrases *within that one resource's own extracted conclusions* into a
single, clearer statement — returning a plain `list[str]`, the exact same
shape `VerifiedResource.extracted_conclusions` already stores. It does
**NOT** merge conclusions ACROSS different resources, and it does NOT
assign a grade/category/source label to anything it returns. Both of
those were in the task's original reference prompt/output schema, and
both were deliberately dropped after tracing the real pipeline:

- **Cross-resource merging is already `conclusion_grader.py`
  ::synthesize_ingredient_summary`'s job** (Phase 23/24's Multi-Source
  Confidence Rubric) — it already reads every `VerifiedResource` for an
  ingredient in one pass and merges/scores overlapping claims across
  sources. Re-implementing that same merge here, in a second, separate
  Gemini call with no visibility into the rubric or the other module's
  own scoring, would produce a second, competing (and un-graded) opinion
  about which claims are "the same finding" — redundant at best,
  contradictory at worst.
- **A Gemini-assigned grade would violate this codebase's one
  consistently-enforced integrity rule**, stated identically in
  `resource_grader.py`, `paper_grader.py`, and `conclusion_grader.py`:
  grades are always server-derived from a rubric, never taken directly
  from the model's own output. `Ingredient.scientific_conclusions` (the
  field the original task asked this service to write to) already has an
  established, richer shape — `{claim, confidence_grade, total_score,
  score_breakdown, supporting_study_count, supporting_resource_count,
  sources_summary, grade_justification}` — built exclusively by
  `synthesize_ingredient_summary`'s own server-side scoring and read by
  real, already-shipped frontend components
  (`ScientificConclusionsList.tsx`, `IngredientCard.tsx`'s Scientific
  Claims card, which specifically filters on `confidence_grade === 'A' ||
  'B'`). Writing this service's own `{conclusion_text, grade, ...}` shape
  directly into that field would silently break every one of those
  consumers (wrong field names) and hand out letter grades no rubric ever
  computed.

Instead, `refine_conclusions()`'s cleaned-up output is written back onto
`VerifiedResource.extracted_conclusions` itself — see
`app/services/paper_analysis_pipeline.py`'s "Phase 40" section, run
immediately after the Phase 21/39 deterministic extraction + Phase 27
HTML fallback have both finished populating that column, and committed
BEFORE Stage 2 (`synthesize_ingredient_summary`) runs. Stage 2 (unchanged
by this phase) then reads this cleaner, less redundant per-resource input
and does the real cross-resource merging + server-side grading exactly as
before — so `Ingredient.scientific_conclusions` genuinely does end up
less noisy and less redundant as a result of this pass, achieved through
the existing, already-correct synthesis engine rather than a parallel,
schema-incompatible one. See docs/Architecture.md's Phase 40 section for
the full reasoning.

**Never raises.** Same "best-effort enrichment, not a required step"
philosophy as `html_resource_extractor.py`: any failure (Gemini request
error, malformed/unparsable response, empty result) returns the ORIGINAL
`raw_conclusions` list unchanged, logged but not propagated — a
refinement hiccup should never cause a resource to lose conclusions it
already had, and should never fail the grade request that triggered it.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import List

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.gemini_rate_limit import call_gemini_with_retry, throttle_gemini_call
from app.services.resource_parser import is_valid_human_conclusion

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ConclusionRefine]"

# Below this many raw items there's nothing meaningful to deduplicate or
# merge — skip the Gemini call entirely and return `raw_conclusions`
# unchanged, same "don't spend a Gemini call on trivial input" reasoning
# as html_resource_extractor.py's own _MIN_CLEANED_TEXT_LENGTH_FOR_EXTRACTION
# guard.
_MIN_ITEMS_TO_REFINE = 2

# Server-side cap on how many refined conclusions this module will ever
# return for one resource — same "don't trust the model's own bound-
# following" enforcement every other Gemini list output in this codebase
# applies (e.g. html_resource_extractor.py's _MAX_WEBPAGE_CONCLUSIONS).
# Deliberately generous — this is a MERGE/filter pass, so a resource that
# came in with, say, 40 raw items should still be allowed a healthy
# refined count if genuinely that many distinct, substantive findings
# survive; this only guards against a pathological/misbehaving response.
_MAX_REFINED_CONCLUSIONS = 60


class _RefinedConclusionsSchema(BaseModel):
    """Structured output schema handed to Gemini as `response_schema` —
    a flat list of strings, deliberately the SAME shape as
    `VerifiedResource.extracted_conclusions` already has (see module
    docstring for why no grade/category/source fields are requested).
    """

    conclusions: List[str] = Field(
        default_factory=list,
        description=(
            "The cleaned, deduplicated list of genuinely useful, "
            "ingredient-specific scientific conclusions that survive "
            "after removing boilerplate/disclaimers/generic fluff and "
            "merging near-duplicate restatements of the same finding. "
            "Empty list if nothing in the input actually qualifies — "
            "never invent a conclusion the input didn't already state."
        ),
    )


def _build_refinement_prompt(ingredient_name: str, raw_conclusions: List[str]) -> str:
    numbered_items = "\n".join(
        f"{index}. {text}" for index, text in enumerate(raw_conclusions, start=1)
    )
    return (
        "You are an expert scientific editor refining a raw list of "
        f"extracted evidence statements for the dietary ingredient "
        f"'{ingredient_name}'. All {len(raw_conclusions)} items below "
        "came from the SAME single source, already confirmed to be about "
        f"'{ingredient_name}' — your job is quality control on that one "
        "source's own extracted text, not a cross-source literature "
        "review.\n\n"
        f"RAW EXTRACTED ITEMS (1-{len(raw_conclusions)}):\n{numbered_items}\n\n"
        "Perform exactly these steps:\n"
        "1. REMOVE NOISE — exclude any item that is:\n"
        "   - A generic medical disclaimer (e.g. \"Consult your doctor "
        "before taking any supplement\", \"Talk to a healthcare "
        "provider\").\n"
        "   - Trivial or generic fluff with no actual scientific content "
        "(e.g. \"Each vitamin has specific jobs\", \"Some vitamins help "
        "prevent problems\").\n"
        "   - Generic dietary advice not itself a scientific finding "
        f"(e.g. \"The best way to get {ingredient_name} is a balanced "
        "diet\").\n"
        f"   - Not actually about '{ingredient_name}' specifically (an "
        "item about a different, unrelated ingredient/topic that doesn't "
        f"mention or clearly concern '{ingredient_name}').\n"
        "2. MERGE NEAR-DUPLICATES — when two or more of the remaining "
        "items are just different phrasings of the SAME underlying "
        "finding, mechanism, dosage figure, or safety note, combine them "
        "into ONE clear, self-contained statement rather than keeping "
        "both. Do not merge items that describe genuinely different "
        "findings just because they're topically related.\n"
        "3. Every surviving/merged item must remain a faithful "
        "restatement of what the raw input actually said — never invent "
        "a new fact, number, or claim the input didn't already contain.\n\n"
        "Return the cleaned list as the required JSON object — an empty "
        "`conclusions` list if nothing survives filtering, never a "
        "padded or invented entry."
    )


@lru_cache
def _get_client() -> genai.Client:
    """Cached Gemini client — separate `@lru_cache` entry from every
    other Gemini-using service's own `_get_client` (paper_grader.py,
    resource_grader.py, conclusion_grader.py, resource_extractor.py,
    html_resource_extractor.py, research_keywords.py), same "one client
    per module" reasoning as those.
    """
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key)


def refine_conclusions(raw_conclusions: List[str], ingredient_name: str) -> List[str]:
    """Cleans up, deduplicates, and merges near-duplicate items within
    one resource's own `extracted_conclusions` list — see module
    docstring for the full design and for why this is scoped to a single
    resource's own items rather than merging across resources.

    Args:
        raw_conclusions: One `VerifiedResource.extracted_conclusions`
            list (or any flat list of extracted conclusion strings) for
            `ingredient_name`.
        ingredient_name: The ingredient these conclusions are about —
            used both for the relevance-filtering instruction and to
            keep the prompt self-contained.

    Returns:
        A `list[str]` — the refined/deduplicated conclusions, capped at
        `_MAX_REFINED_CONCLUSIONS` and re-validated through
        `is_valid_human_conclusion` (the same Phase 28 sanitizer every
        other conclusion in this codebase passes through, as a final
        safety net in case a rewritten/merged sentence drifted into
        something that reads like leftover metadata). Falls back to
        `raw_conclusions` UNCHANGED (never `[]`, never raises) whenever
        there are too few items to bother refining, or refinement fails
        for any reason — see module docstring's "Never raises" note.
    """
    if not ingredient_name or not raw_conclusions:
        return raw_conclusions

    # Nothing meaningful for Gemini to dedupe/merge below this size —
    # skip the call entirely rather than spend a Gemini request refining
    # a single item into itself.
    if len(raw_conclusions) < _MIN_ITEMS_TO_REFINE:
        return raw_conclusions

    client = _get_client()
    settings = get_settings()
    prompt = _build_refinement_prompt(ingredient_name, raw_conclusions)

    def _call_gemini():
        throttle_gemini_call()
        return client.models.generate_content(
            model=settings.gemini_model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_RefinedConclusionsSchema,
            ),
        )

    try:
        response = call_gemini_with_retry(
            _call_gemini,
            label=f"Conclusion refinement for ingredient {ingredient_name!r}",
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, see module docstring
        logger.warning(
            "%s Refinement request failed for ingredient %r (%d raw "
            "item(s)) — keeping the original, unrefined list: %s",
            _LOG_PREFIX,
            ingredient_name,
            len(raw_conclusions),
            exc,
        )
        return raw_conclusions

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, _RefinedConclusionsSchema):
        raw_text = getattr(response, "text", None)
        if not raw_text:
            logger.warning(
                "%s Gemini returned an empty response for ingredient %r "
                "— keeping the original, unrefined list.",
                _LOG_PREFIX,
                ingredient_name,
            )
            return raw_conclusions
        try:
            parsed = _RefinedConclusionsSchema.model_validate_json(raw_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s Gemini response did not match the expected schema for "
                "ingredient %r — keeping the original, unrefined list: %s",
                _LOG_PREFIX,
                ingredient_name,
                exc,
            )
            return raw_conclusions

    # Dedup (preserving first-seen order) + the same Phase 28 sanitizer
    # every other conclusion in this codebase is subject to + cap — same
    # "never trust the model's own bound-following" enforcement pattern
    # as every other Gemini list output here.
    refined = [item.strip() for item in parsed.conclusions if item and item.strip()]
    refined = list(dict.fromkeys(refined))
    refined = [item for item in refined if is_valid_human_conclusion(item)]
    refined = refined[:_MAX_REFINED_CONCLUSIONS]

    if not refined:
        logger.info(
            "%s Refinement removed every item for ingredient %r (%d raw "
            "item(s) in, 0 out) — keeping the original, unrefined list "
            "rather than leaving this resource with zero conclusions "
            "over a single refinement pass's judgment call.",
            _LOG_PREFIX,
            ingredient_name,
            len(raw_conclusions),
        )
        return raw_conclusions

    return refined
