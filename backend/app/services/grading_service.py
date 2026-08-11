"""Single-ingredient SIFG grading -- multi-source literature retrieval + one Gemini evaluation call.

The whole "Grade" button on the ingredients list, backend-side:
``POST /api/ingredients/{ingredient_id}/grade`` calls
``IngredientGradingService.grade_ingredient`` for exactly the one
requested ``ScannedIngredient`` -- nothing else in
``data/scanned_ingredients.json`` is touched or re-graded.

Two steps, five logged phases (``[GRADING STEP x/5]``, printed to stdout
AND logged -- see ``log_grading_step`` / ``literature_search.log_retrieval_summary``):

1. **Literature retrieval** (step 2; ``app.services.literature_search.aggregate_literature``):
   PubMed, Europe PMC, OpenAlex, and Semantic Scholar are queried in
   parallel for this ingredient's exact name/form, merged, deduplicated,
   ranked by a weighted quality score (study type, citation count,
   recency, keyword match), and cut down to the top
   ``Settings.literature_top_papers_limit`` (default 20) papers. Any
   single provider failing (network, timeout, unparseable response)
   does NOT fail the grade -- ``aggregate_literature`` isolates each
   provider so the others still contribute; only a TOTAL failure across
   every source degrades to "zero studies found", and Gemini is told
   explicitly that the search failed (not just that it found nothing)
   so it can factor that into ``evidence_summary`` rather than silently
   grading as if literature were checked and came up empty.
2. **Gemini evaluation** (steps 3-4; ``app.services.gemini_client.generate_content``,
   reusing the exact same dynamic ``Settings.gemini_model`` / zero-retry
   / 429-retry / 60s-pause machinery the scan endpoint uses -- see that
   module): one structured-output call producing a ``SifgConsensus`` --
   overall grade/score, efficacy & safety evaluation, dosage
   appropriateness, and an evidence summary. The prompt instructs Gemini
   to cite ONLY the studies actually passed in and to say so plainly if
   none were found, rather than inventing citations -- this project never
   fabricates supplement data, and a made-up citation would be exactly
   that.

Step 1 (this ingredient's context) is logged at the start of
``grade_ingredient``; step 5 (final grade + persisted status) is logged
by the caller (``app.api.routes.grade_ingredient``) after it actually
writes the result to disk, using the same ``log_grading_step`` helper --
see that module. Every call also returns a ``GradingStats`` alongside
its ``SifgConsensus`` (wrapped together as ``GradingResult``): how many
papers were found vs. actually selected for Gemini, the per-provider
paper counts, which query/queries were run, how long the whole pass
took, and which Gemini model answered. These are OUR OWN
backend-computed numbers, not anything Gemini itself reported -- kept
deliberately separate from ``SifgConsensus``/``raw_consensus`` so that
distinction stays honest (see
``app.schemas.scan.ScannedIngredient.grading_stats``'s docstring).

Persistence (updating the specific ingredient record in
``data/scanned_ingredients.json``) is the caller's job (see
``app.api.routes.grade_ingredient`` and
``app.services.storage.ScanStorage.update_ingredient``), not this
module's -- this module only ever computes a grade, it never writes
anything to disk.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.schemas.scan import ScannedIngredient
from app.services.gemini_client import GeminiCallError, generate_content
from app.services.literature_search import PROVIDER_NAMES, RawPaper, aggregate_literature, log_retrieval_summary

logger = logging.getLogger(__name__)

TOTAL_GRADING_STEPS = 5
RETRIEVAL_STEP = 2  # both the "API Queries Completed" and "Total Unique Papers Found" log lines

GRADING_SYSTEM_PROMPT = (
    "You are a rigorous, conservative supplement-science reviewer producing a single ingredient's "
    "Supplement Ingredient Fact Grade (SIFG). You will be given one ingredient's label-printed name, "
    "form, dose, and % Daily Value, plus a set of study excerpts aggregated from PubMed, Europe PMC, "
    "OpenAlex, and Semantic Scholar (which may be empty). Base your efficacy/safety evaluation and "
    "evidence summary ONLY on the studies actually provided below -- never invent, assume, or reference "
    "a study, finding, identifier, or citation that was not given to you. If the provided studies list "
    "is empty, or the literature search itself failed, say so explicitly in evidence_summary (e.g. 'No "
    "literature was found for this exact ingredient/form during this grading pass' or 'The literature "
    "search failed and no studies could be retrieved') rather than grading as if evidence was reviewed "
    "and simply absent. In that case grade conservatively (typically sifg_grade='Insufficient Evidence') "
    "using only well-established general nutritional-science knowledge, and note clearly that this is "
    "not literature-backed. For dosage_appropriateness, compare the label's printed dose against "
    "typical/well-established dosing for this ingredient and form. Respond only with JSON matching the "
    "provided response schema."
)


def log_grading_step(step: int, ingredient_id: str, title: str, body: str = "") -> None:
    """Print + log one ``[GRADING STEP x/5]`` line (see module docstring).

    Printed to stdout (in addition to the structured logger call) so the
    whole grading run's progress and reasoning are visible directly in
    the terminal in real time, matching ``gemini_client.py``'s existing
    stdout-logging convention -- not just buried in structured log
    output. The literature-retrieval step (step ``RETRIEVAL_STEP``) uses
    ``literature_search.log_retrieval_summary`` instead, which prints a
    fixed-format block without this function's ``ingredient_id=...``
    prefix -- see that function's docstring for why.
    """
    header = f"[GRADING STEP {step}/{TOTAL_GRADING_STEPS}] ingredient_id={ingredient_id!r} -- {title}"
    print(header, file=sys.stdout, flush=True)
    logger.info(header)
    if body:
        print(body, file=sys.stdout, flush=True)
        logger.info(body)


class SifgConsensus(BaseModel):
    """Gemini's structured-output contract for one ingredient's grading pass.

    Everything here is exactly what ends up in the ingredient's
    ``sifg_grade`` / ``sifg_score`` / ``efficacy_safety_evaluation`` /
    ``dosage_appropriateness`` / ``evidence_summary`` fields (see
    ``app.schemas.scan.ScannedIngredient``) plus a full copy stored in
    ``raw_consensus`` for the UI's expandable raw-JSON view. Contains
    only what Gemini itself returned -- see ``GradingStats`` for the
    separate, backend-computed numbers (papers found, timing, etc.).
    """

    sifg_grade: str = Field(
        ...,
        description="Overall letter grade, e.g. 'A+' through 'F', or 'Insufficient Evidence' if the "
        "available literature (or lack thereof) doesn't support a confident grade.",
    )
    sifg_score: Optional[float] = Field(
        default=None, ge=0, le=100, description="Numeric 0-100 companion to sifg_grade."
    )
    efficacy_safety_evaluation: str = Field(
        ..., description="Evaluation of efficacy and safety, grounded only in the provided studies."
    )
    dosage_appropriateness: str = Field(
        ..., description="Assessment of the label's printed dose against typical/studied dosing."
    )
    evidence_summary: str = Field(
        ...,
        description="Plain-language summary of the evidence considered -- must say so explicitly if no "
        "relevant literature was found or the search failed.",
    )
    studies_considered: List[str] = Field(
        default_factory=list,
        description="Identifiers (PMID, DOI, or title -- whichever the source provided) of the studies, "
        "from the ones actually given, that materially informed this grade. Empty if none were provided "
        "or none were materially relevant.",
    )


class GradingStats(BaseModel):
    """Backend-computed metadata about one grading run -- NOT part of Gemini's own output.

    Kept as a sibling of ``SifgConsensus`` (see ``GradingResult``)
    rather than folded into it, so it's never confused with something
    Gemini itself reported -- these numbers come from
    ``app.services.literature_search`` and this module's own
    timing/config, computed whether or not the Gemini call itself
    ultimately succeeds (see ``GradingError.stats``).
    """

    papers_found: int = Field(
        default=0,
        description="Total unique papers found across PubMed, Europe PMC, OpenAlex, and Semantic "
        "Scholar combined, after deduplication (by DOI, then PMID, then normalized title) -- before the "
        "top-N ranking cut.",
    )
    papers_analyzed: int = Field(
        default=0,
        description="Number of papers actually selected (after ranking) and included in the Gemini "
        "prompt -- at most Settings.literature_top_papers_limit (default 20).",
    )
    provider_counts: Dict[str, int] = Field(
        default_factory=dict,
        description="Raw paper count returned by each source BEFORE deduplication/ranking, e.g. "
        "{'PubMed': 3, 'Europe PMC': 5, 'OpenAlex': 2, 'Semantic Scholar': 4}. A source that failed "
        "outright contributes 0 here (see app.services.literature_search.AggregatedLiteratureResult.provider_errors).",
    )
    search_queries: List[str] = Field(
        default_factory=list,
        description="Every distinct search string actually executed for this grading run, across every "
        "provider (see app.services.literature_search / app.services.pubmed_client's query-building).",
    )
    grading_duration_seconds: float = Field(
        default=0.0,
        description="Wall-clock time for the full grading run (literature retrieval + Gemini "
        "evaluation), measured from the start of grade_ingredient to when it returns or raises.",
    )
    model_used: str = Field(
        default="", description="The actual Gemini model that answered this call (settings.gemini_model, "
        "with any leading 'models/' prefix stripped) -- see app.services.gemini_client._resolve_model."
    )


class GradingResult(BaseModel):
    """What ``IngredientGradingService.grade_ingredient`` returns on success: the grade plus its stats."""

    consensus: SifgConsensus
    stats: GradingStats


class GradingError(Exception):
    """Raised when single-ingredient grading fails: bad API key at construction, or the Gemini
    evaluation call itself fails/returns an unparseable response.

    Note a literature-retrieval failure (even a total one, across every
    provider) does NOT raise this -- see module docstring point 1; only
    the Gemini step is fatal to a grading attempt. Carries ``stats``
    (whatever ``GradingStats`` could still be computed before the
    failure -- papers found, queries run, duration up to that point,
    model attempted) so the caller can persist useful diagnostics even
    for a failed grade; ``None`` only if the failure happened before any
    stats were computable at all (e.g. at construction time, before
    ``grade_ingredient`` ever runs).
    """

    def __init__(self, message: str, stats: Optional[GradingStats] = None) -> None:
        super().__init__(message)
        self.stats = stats


class IngredientGradingService:
    """Computes one ``GradingResult`` per call, for one ``ScannedIngredient`` at a time."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        """Configure the service.

        Args:
            settings: Injected ``Settings``; defaults to ``get_settings()``.

        Raises:
            GradingError: If ``GEMINI_API_KEY`` is missing, blank, or whitespace-only --
                grading reuses the same Gemini API key as scanning.
        """
        self._settings = settings or get_settings()
        self._api_key = self._validate_api_key(self._settings.gemini_api_key)

    @staticmethod
    def _validate_api_key(api_key: Optional[str]) -> str:
        if not api_key or not api_key.strip():
            raise GradingError("GEMINI_API_KEY is missing or invalid.")
        return api_key.strip()

    def _resolve_model(self) -> str:
        """Same resolution gemini_client.generate_content itself applies -- see that module's _resolve_model."""
        return self._settings.gemini_model.replace("models/", "")

    # -- Public API -------------------------------------------------------

    async def grade_ingredient(self, ingredient: ScannedIngredient) -> GradingResult:
        """Run the full grading pass (literature retrieval + Gemini evaluation) for one ingredient.

        Args:
            ingredient: The specific ingredient to grade -- only its own
                name/form/amount/unit/percent_daily_value are used as
                context; nothing else in its parent scan is touched.

        Returns:
            A ``GradingResult`` (the ``SifgConsensus`` plus a
            ``GradingStats`` describing this run).

        Raises:
            GradingError: If the Gemini evaluation call fails, or its
                response doesn't match ``SifgConsensus``. Carries a
                best-effort ``GradingStats`` in ``.stats`` even on
                failure. A literature retrieval failure alone does NOT
                raise -- see module docstring.
        """
        started_at = time.monotonic()
        ingredient_id = ingredient.ingredient_id
        model_used = self._resolve_model()

        log_grading_step(
            1,
            ingredient_id,
            "Target ingredient",
            body=(
                f"name={ingredient.name!r} form={ingredient.form!r} "
                f"dose={ingredient.amount!r} {ingredient.unit or ''} "
                f"percent_daily_value={ingredient.percent_daily_value!r}"
            ),
        )

        retrieval = await aggregate_literature(
            ingredient.name, ingredient.form, ingredient.amount, ingredient.unit, settings=self._settings
        )
        log_retrieval_summary(
            RETRIEVAL_STEP, TOTAL_GRADING_STEPS, self._settings.literature_top_papers_limit, retrieval
        )

        # "Search failed" (as opposed to "searched and found nothing") only when
        # literally every provider errored out -- see aggregate_literature's docstring.
        search_failed = len(retrieval.provider_errors) == len(PROVIDER_NAMES)

        try:
            consensus = await self._evaluate_with_gemini(ingredient, retrieval.studies, search_failed)
        except GradingError as exc:
            exc.stats = self._build_stats(retrieval, model_used, started_at)
            raise

        stats = self._build_stats(retrieval, model_used, started_at)
        return GradingResult(consensus=consensus, stats=stats)

    # -- Internals ----------------------------------------------------------

    @staticmethod
    def _build_stats(retrieval, model_used: str, started_at: float) -> GradingStats:
        return GradingStats(
            papers_found=retrieval.papers_found,
            papers_analyzed=retrieval.papers_analyzed,
            provider_counts=retrieval.provider_counts,
            search_queries=retrieval.queries_used,
            grading_duration_seconds=round(time.monotonic() - started_at, 3),
            model_used=model_used,
        )

    async def _evaluate_with_gemini(
        self,
        ingredient: ScannedIngredient,
        studies: List[RawPaper],
        search_failed: bool,
    ) -> SifgConsensus:
        from google import genai  # local import: optional dependency, only needed here
        from google.genai import types

        api_key = self._validate_api_key(self._api_key)
        client = genai.Client(api_key=api_key)

        prompt = self._build_prompt(ingredient, studies, search_failed)
        ingredient_id = ingredient.ingredient_id

        log_grading_step(
            3,
            ingredient_id,
            f"Sending prompt to Gemini (model={self._resolve_model()!r})",
            body=f"--- system prompt ---\n{GRADING_SYSTEM_PROMPT}\n--- user prompt ---\n{prompt}",
        )

        try:
            response = await generate_content(
                client,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    system_instruction=GRADING_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=SifgConsensus,
                ),
                settings=self._settings,
            )
        except GeminiCallError as exc:
            log_grading_step(4, ingredient_id, "Gemini call FAILED", body=str(exc))
            raise GradingError(f"Gemini grading call failed: {exc}") from exc

        if not response.text:
            log_grading_step(4, ingredient_id, "Gemini call FAILED", body="Response had no structured output text.")
            raise GradingError("Gemini grading response did not include any structured output text.")

        try:
            consensus = SifgConsensus.model_validate(json.loads(response.text))
        except Exception as exc:  # noqa: BLE001 - normalize any parse/validation failure to GradingError
            log_grading_step(4, ingredient_id, "Raw Gemini output (failed schema validation)", body=response.text)
            raise GradingError(f"Gemini grading response did not match the expected schema: {exc}") from exc

        # Gemini's structured-output mode doesn't expose a separate hidden
        # chain-of-thought trace -- "reasoning" here is its own stated
        # reasoning (the evidence_summary / efficacy_safety_evaluation
        # fields it just returned), logged explicitly as such rather than
        # implied to be something more than that.
        log_grading_step(
            4,
            ingredient_id,
            "Raw Gemini output & stated reasoning",
            body=(
                f"--- raw output ---\n{response.text}\n"
                f"--- reasoning (from Gemini's own response fields, not a separate hidden trace) ---\n"
                f"efficacy_safety_evaluation: {consensus.efficacy_safety_evaluation}\n"
                f"dosage_appropriateness: {consensus.dosage_appropriateness}\n"
                f"evidence_summary: {consensus.evidence_summary}"
            ),
        )
        return consensus

    @staticmethod
    def _build_prompt(ingredient: ScannedIngredient, studies: List[RawPaper], search_failed: bool) -> str:
        dose = f"{ingredient.amount} {ingredient.unit}" if ingredient.amount is not None else None
        lines = [
            f"Ingredient: {ingredient.name}",
            f"Form: {ingredient.form or 'not stated on label'}",
            f"Dose per serving (as printed on label): {dose or 'not stated on label'}",
            f"% Daily Value (as printed on label): {ingredient.percent_daily_value or 'not stated on label'}",
            "",
        ]
        if search_failed:
            lines.append(
                "Literature search: FAILED across every source (PubMed, Europe PMC, OpenAlex, Semantic "
                "Scholar) -- no studies could be retrieved for this grading pass; this is NOT the same "
                "as a search that succeeded and found zero results."
            )
        elif not studies:
            lines.append(
                "Literature search: succeeded, but found no studies matching this ingredient/form across "
                "any source."
            )
        else:
            lines.append(
                f"Literature search: found {len(studies)} study excerpt(s) below, aggregated and ranked "
                f"from PubMed, Europe PMC, OpenAlex, and Semantic Scholar."
            )
            lines.append("")
            for study in studies:
                identifier = study.pmid or study.doi or "no id available"
                lines.append(f"--- {study.source} | {identifier} ---")
                if study.title:
                    lines.append(f"Title: {study.title}")
                meta_bits = []
                if study.publication_year:
                    meta_bits.append(f"Year: {study.publication_year}")
                if study.citation_count is not None:
                    meta_bits.append(f"Citations: {study.citation_count}")
                if study.study_type:
                    meta_bits.append(f"Type (inferred): {study.study_type}")
                if meta_bits:
                    lines.append(" | ".join(meta_bits))
                if study.abstract:
                    lines.append(study.abstract)
                lines.append("")
        return "\n".join(lines)
