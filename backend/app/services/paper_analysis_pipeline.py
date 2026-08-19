"""Per-ingredient analysis pipeline (Phase 5), with ingredient relevance
verification (Phase 6), the Two-Stage Extraction Pipeline's Stage 1 step
(Phase 17), and rate-limit-aware execution ordering + a per-run paper
cap (Phase 18 — see "Rate Limiting & Execution Order" below).

Runs after app/services/paper_search.py has found/persisted new papers,
and app/services/resource_fetcher.py has found/persisted new verified
resources, for an ingredient (see app/services/grading.py::grade_ingredient).
As of Phase 18, in this order:

  1. **Stage 1 — resource claims extraction** (moved ahead of paper
     grading this phase — see "Rate Limiting & Execution Order" below).
  2. **Paper grading + conclusion synthesis**, for at most
     `MAX_PAPERS_PER_GRADED_INGREDIENT` papers this run (Phase 18 cap —
     see below), for each:
     a. Grade it (app/services/paper_grader.py::grade_single_paper) — a
        no-op, no Gemini call, if it's already graded. The same Gemini
        call that grades also relevance-checks the paper against its
        ingredient (Phase 6) and sets `paper.status` accordingly.
     b. If the paper was just marked `PAPER_STATUS_DISCARDED_IRRELEVANT`
        (i.e. Gemini determined it isn't actually about the target
        ingredient — e.g. a Vitamin D paper found during a Vitamin C
        search), log a warning and skip straight to the next paper —
        conclusion synthesis never runs for it.
     c. Otherwise, if (and only if) its grade_score ends up > 50,
        synthesize/merge its findings into the ingredient's running
        PaperConclusion set
        (app/services/conclusion_grader.py::process_paper_conclusions).
  3. **Stage 2 — unified conclusion synthesis** (unchanged position,
     still last — see "Multi-source ingredient summary synthesis"
     below).

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
pipeline runs too — the initial paper query below filters it out before
ranking/capping even happens (Phase 18), so a
re-grade request never re-relevance-checks (or re-logs a warning for) a
paper already known to be off-topic. It's also excluded from every
paper-list/summary query in app/services/search.py (`get_ingredient_papers`),
so it no longer counts toward "Total studies"/"Average grade" or shows
up in the "List of Studies"/recommendations panels on the frontend.

**Multi-source ingredient summary synthesis (Phase 11).** After the
per-paper grade/conclusion loop below finishes, this module makes one
additional, ingredient-level call to
`app/services/conclusion_grader.py::synthesize_ingredient_summary` —
considering every currently-graded paper AND every VerifiedResource
(Phase 7/8 — official NIH/USDA/EFSA/Health Canada/etc. guidance)
together — and persists the resulting `summary_description` onto the
`Ingredient` row (`app/models/supplement.py`). This is a single call per
`analyze_ingredient_papers()` run, not per paper — see that function's
own docstring, and conclusion_grader.py's module docstring, for why this
doesn't reintroduce the "one huge batched call" problem the per-paper
design above avoids. Same best-effort philosophy as every other step
here: a failure is logged and skipped, never allowed to fail the whole
grade request.

**Precondition audited (Phase 16): verified-resource fetch MUST already
have run and committed before `analyze_ingredient_papers()` is called.**
This module does not fetch verified resources itself — that's
`app/services/resource_fetcher.py::fetch_verified_resources_for_ingredient`,
called (and committed) by `app/services/grading.py::grade_ingredient`
*before* it calls `analyze_ingredient_papers()` (see that function's own
docstring for the exact ordering: search papers -> fetch verified
resources -> commit -> `analyze_ingredient_papers()`). By the time
`synthesize_ingredient_summary()` below queries `VerifiedResource` fresh
from the DB, that fetch has already been flushed (at minimum) or fully
committed (in `grade_ingredient`'s normal path) earlier in the same
session, so the query always sees it. If `analyze_ingredient_papers()`
is ever called from a new call site that *doesn't* first fetch verified
resources, ingredient-summary synthesis will silently run with zero
resources for that request (not fail — `synthesize_ingredient_summary`
treats "zero resources" as a normal, evidence-still-available-from-papers
case, not an error) — the log line right before that call, below, makes
that visible.

**Two-Stage Extraction Pipeline (Phase 17).** Replaces the single-step
"dump every paper and every resource into one synthesis prompt" approach
audited in Phase 16 above with two distinct steps, specifically to fix a
"lost-in-the-middle" effect where Gemini consistently favored dense
paper abstracts over thin web-resource snippets when both were mixed
into one prompt (see app/services/resource_extractor.py's module
docstring for the full investigation/reasoning):

  - **Stage 1 (per-resource, this module).** Right after the per-paper
    loop above and right before the final synthesis call, this function
    queries every VerifiedResource currently stored for this ingredient
    and, for each one still missing `extracted_data`, calls
    `app/services/resource_extractor.py::extract_claims_from_resource`
    to distill that resource's own title/publisher/summary into a
    compact, structured `{official_stance, recommended_dose,
    upper_limit_warning, key_takeaways}` payload, persisted directly onto
    that VerifiedResource row. A resource that already has
    `extracted_data` (from an earlier run) is left alone — extraction, like
    grading, doesn't change once it's succeeded. This also means a
    resource fetched *before* this feature existed gets backfilled the
    next time its ingredient is re-graded, with no separate migration/
    backfill script needed.
  - **Stage 2 (ingredient-level,
    app/services/conclusion_grader.py::synthesize_ingredient_summary,
    called immediately after).** Consumes the now-structured
    `extracted_data` for every resource (rather than each resource's raw
    `summary` text) alongside the papers' own already-extracted
    PaperConclusion findings — see that function's module docstring for
    the updated, resources-first prompt structure.

Same best-effort philosophy as everything else in this pipeline: a
Stage 1 extraction failure for one resource (Gemini request failure,
unparsable response) is logged and skipped — that resource's
`extracted_data` simply stays `None` and Stage 2 falls back to its raw
`summary` text for that one resource (see conclusion_grader.py's
`_format_resources_for_prompt`) — never allowed to abort the loop or
block Stage 2 from running with whatever data IS available.

**Rate Limiting & Execution Order (Phase 18).** Under sustained grading
traffic this app was hitting the Gemini free-tier's requests-per-minute
ceiling mid-run — a burst of newly-found papers graded back-to-back,
with no spacing between calls, could all land in the same one-minute
quota window and start failing together. Two changes address this:

  - **Stage 1 now runs BEFORE paper grading, not after.** Previously,
    the per-paper grading loop ran first and Stage 1 resource extraction
    ran only once every paper had had its turn — meaning a quota
    exhausted by a large batch of papers left nothing for Stage 1 (and,
    in turn, Stage 2) to work with even though verified resources had
    already been fetched (see the Phase 16 precondition note above —
    fetching itself, in app/services/resource_fetcher.py, was already
    running before this function is even called; only *this function's
    own* Stage 1 extraction step was, until now, sequenced after paper
    grading rather than before it). Reordering Stage 1 ahead of the
    paper loop means resource extraction gets first claim on whatever
    quota is available in a given run, rather than leftovers.
  - **`MAX_PAPERS_PER_GRADED_INGREDIENT = 6`** caps how many papers this
    function actually processes (grades + attempts conclusion synthesis
    for) in one run, regardless of how many are stored for the
    ingredient — seeing a burst of 20+ newly-found papers in one grade
    request was the single biggest source of rate-limit pressure. Papers
    are ranked by the best available relevance/quality signal before the
    cap is applied — see `_paper_relevance_sort_key` below — rather than
    an arbitrary/random 6; an ingredient with more than 6 stored papers
    will simply need additional grade requests (e.g. re-clicking
    "Assign Grade") to eventually work through the rest, since the same
    top-ranked papers win the cap every run. This trade-off (a
    lower-ranked paper could in principle be starved indefinitely if 6+
    higher-ranked ones persist) is a known, deliberate limitation of
    this simple a cap — flagged here rather than silently accepted.
  - **Every individual Gemini call this pipeline triggers** (via
    `paper_grader.py`'s `grade_paper()` and `resource_extractor.py`'s
    `extract_claims_from_resource()`) is now paced (~4.5s minimum
    spacing, process-wide) and retried with exponential backoff on a
    429/`RESOURCE_EXHAUSTED` response — see
    `app/services/gemini_rate_limit.py`'s module docstring for the full
    mechanism, including two deliberate deviations from how a rate-limit
    retry helper is sometimes written (synchronous rather than
    `async def`; and catching `google.genai.errors.ClientError`/
    `APIError` rather than `google.api_core.exceptions.ResourceExhausted`,
    which this app's actual Gemini dependency — `google-genai` — never
    raises).

**Known gap, out of scope this phase:** `conclusion_grader.py`'s own two
Gemini calls (`process_paper_conclusions`, invoked once per paper inside
the loop below, and `synthesize_ingredient_summary`, Stage 2 at the
bottom of this function) are NOT wired through `gemini_rate_limit.py` —
that file wasn't part of this task's authorized scope. A rate-limit hit
on either of those still surfaces as an ordinary, unretried
`ConclusionGradingError`, caught and logged exactly as before (see the
per-paper loop and Stage 2 call below) — never a hard crash, but also
never retried/backed-off. Flagged rather than silently left
undocumented; extending `gemini_rate_limit.py`'s use into
`conclusion_grader.py` would close this gap the same way it was closed
here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import Session, select

from app.models.research import (
    PAPER_STATUS_DISCARDED_IRRELEVANT,
    ResearchPaper,
    VerifiedResource,
    parse_keywords,
)
from app.models.supplement import Ingredient
from app.services.conclusion_grader import (
    ConclusionGradingError,
    process_paper_conclusions,
    synthesize_ingredient_summary,
)
from app.services.paper_grader import PaperGradingError, grade_single_paper
from app.services.resource_extractor import (
    ResourceExtractionError,
    extract_claims_from_resource,
)

logger = logging.getLogger(__name__)

# Phase 18: caps how many papers a single analyze_ingredient_papers() run
# actually grades/processes, regardless of how many are stored for the
# ingredient — see module docstring's "Rate Limiting & Execution Order"
# section for why (a burst of many newly-found papers graded back-to-back
# was the single biggest source of 429/RESOURCE_EXHAUSTED pressure).
# Papers are ranked by _paper_relevance_sort_key before this cap is
# applied, so the top 6 by best-available relevance/quality win, not an
# arbitrary/random 6.
MAX_PAPERS_PER_GRADED_INGREDIENT = 20


@dataclass
class PipelineResult:
    """Summary of one analyze_ingredient_papers() run — informational
    only (nothing currently branches on it beyond logging), since the
    whole point of this pipeline's error handling is that individual
    paper failures don't need to be surfaced as a hard error to the
    caller; whatever succeeded is already durably saved.
    """

    papers_considered: int = 0
    # Phase 18: how many otherwise-eligible papers this run did NOT
    # process at all because MAX_PAPERS_PER_GRADED_INGREDIENT was
    # already reached by higher-ranked ones — see
    # _paper_relevance_sort_key and the module docstring's "Rate
    # Limiting & Execution Order" section. 0 whenever the ingredient has
    # at most MAX_PAPERS_PER_GRADED_INGREDIENT active papers stored.
    papers_excluded_by_cap: int = 0
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
    # Phase 17 (Stage 1): how many VerifiedResource rows for this
    # ingredient were considered/extracted-from this run. `_attempted`
    # counts resources that were actually missing `extracted_data` and so
    # had extract_claims_from_resource() called for them (whether or not
    # that call itself hit the short-snippet guard) — a resource that
    # already had `extracted_data` from an earlier run isn't counted
    # here, mirroring how `papers_conclusions_attempted` above only
    # counts papers Gemini was actually called for.
    resources_considered: int = 0
    resources_extraction_attempted: int = 0
    resources_extraction_failed: int = 0
    # Phase 11: True iff synthesize_ingredient_summary() ran successfully
    # this call and its summary_description was saved onto the
    # Ingredient row. False either because there was no evidence to
    # synthesize from (see that function's docstring) or because the
    # Gemini call/commit failed — both are logged separately, this flag
    # alone doesn't distinguish which.
    ingredient_summary_generated: bool = False


def _paper_relevance_sort_key(paper: ResearchPaper) -> tuple:
    """Best-available PRE-grading relevance/quality signal — used only
    to decide which papers win the `MAX_PAPERS_PER_GRADED_INGREDIENT`
    cap when an ingredient has more stored (active, non-discarded)
    papers than one run can afford to process (Phase 18).

    Two tiers, expressed as a tuple so Python's default lexicographic
    tuple comparison handles the fallback between them for free — sorted
    descending (see call site: `reverse=True`):

    1. An already-graded paper's real, measured `grade_score` (Phase 3)
       — the strongest signal available, since Gemini has already judged
       both its quality AND whether it's actually on-topic (Phase 6).
       The leading `1` (vs. `0` for an ungraded paper below) means every
       already-graded paper outranks every ungraded one, regardless of
       score — grading is a one-time, idempotent operation
       (`grade_single_paper` no-ops with no Gemini call for an
       already-graded row), so keeping already-graded papers in the
       processed set costs nothing extra towards this run's rate-limit
       budget, while still giving them priority for continued
       conclusion-synthesis consideration.
    2. An ungraded paper has no measured signal yet — falls back to how
       many distinct Gemini-generated search keywords matched it (see
       `parse_keywords`, imported above) as a rough pre-grading proxy: a
       paper that multiple different search angles converged on is
       plausibly more central to the ingredient than one a single
       keyword happened to surface.

    Both tiers tie-break on `created_at` (most recently found first).

    **Known limitation** (documented in the module docstring's "Rate
    Limiting & Execution Order" section too): since the same top-ranked
    papers win this ordering on every run, a lower-ranked paper can in
    principle never get its turn if 6+ higher-ranked ones persist for
    the same ingredient — a deliberate simplicity trade-off, not an
    oversight.
    """
    if paper.grade_score is not None:
        return (1, paper.grade_score, paper.created_at)
    return (0, len(parse_keywords(paper.keywords)), paper.created_at)


def analyze_ingredient_papers(
    session: Session, ingredient_id: int, ingredient_name: str
) -> PipelineResult:
    """Runs Stage 1 resource-claims extraction over every VerifiedResource
    still missing `extracted_data` (Phase 17), then the grade ->
    relevance-check -> (maybe) synthesize-conclusions loop over at most
    `MAX_PAPERS_PER_GRADED_INGREDIENT` currently-active ResearchPaper rows
    stored for `ingredient_id` (Phase 18 cap, ranked by
    `_paper_relevance_sort_key`), then makes one final, ingredient-level
    Stage 2 call to synthesize `summary_description` from every graded
    paper AND every (now structured) VerifiedResource together (Phase 11,
    prompt restructured Phase 17, execution order changed Phase 18 — see
    module docstring).

    Safe to call repeatedly for the same ingredient (e.g. once per grade
    request, even if some papers were already processed by an earlier
    call): grading is idempotent per paper (grade_single_paper no-ops on
    an already-graded row), conclusion merging is idempotent per (paper,
    conclusion) pair (a paper's id is only ever appended to a
    supporting/contradicting list once — see conclusion_grader.py), and
    an already-discarded paper is excluded from the initial query
    entirely (see module docstring's "Discard logic"). The Phase 11
    summary step simply overwrites `summary_description` with a fresh
    synthesis each time it runs — there's no partial/incremental state to
    worry about there, unlike per-paper conclusion merging.

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
    all_papers = session.exec(
        select(ResearchPaper)
        .where(ResearchPaper.ingredient_id == ingredient_id)
        .where(ResearchPaper.status != PAPER_STATUS_DISCARDED_IRRELEVANT)
    ).all()

    # Phase 18: rank by best-available relevance/quality signal, then cap
    # to MAX_PAPERS_PER_GRADED_INGREDIENT — see _paper_relevance_sort_key
    # and the module docstring's "Rate Limiting & Execution Order"
    # section for why.
    papers_ranked = sorted(all_papers, key=_paper_relevance_sort_key, reverse=True)
    papers = papers_ranked[:MAX_PAPERS_PER_GRADED_INGREDIENT]
    papers_excluded = papers_ranked[MAX_PAPERS_PER_GRADED_INGREDIENT:]

    result = PipelineResult(
        papers_considered=len(papers),
        papers_excluded_by_cap=len(papers_excluded),
    )
    if papers_excluded:
        logger.info(
            "[Pipeline Debug] MAX_PAPERS_PER_GRADED_INGREDIENT=%d reached "
            "for ingredient id=%s (%r) — processing top %d of %d active "
            "paper(s) this run; %d excluded (will be reconsidered on a "
            "future grade request).",
            MAX_PAPERS_PER_GRADED_INGREDIENT,
            ingredient_id,
            ingredient_name,
            len(papers),
            len(all_papers),
            len(papers_excluded),
        )

    # --- Phase 17, Stage 1: per-resource structured claims extraction ---
    # Phase 18: moved AHEAD of paper grading below (previously ran after
    # it) — see module docstring's "Rate Limiting & Execution Order"
    # section for why: giving Stage 1 first claim on whatever Gemini
    # quota is available this run, rather than leftovers after a
    # potentially-large batch of paper grading calls, is what actually
    # fixes "verified resources never get analyzed" under sustained rate
    # pressure. Every VerifiedResource currently stored for this
    # ingredient is considered, not just ones found by this particular
    # grade request, so a resource fetched under an earlier run (before
    # this feature existed, or before it was ever extracted for some
    # other reason) gets backfilled here too. See module docstring's
    # "Two-Stage Extraction Pipeline" section.
    resources_available = session.exec(
        select(VerifiedResource).where(VerifiedResource.ingredient_id == ingredient_id)
    ).all()
    result.resources_considered = len(resources_available)

    resources_extracted_this_run = 0
    for resource in resources_available:
        if resource.extracted_data is not None:
            # Already extracted (or already recorded as a real,
            # non-None "insufficient data" result — see
            # resource_extractor.py's short-snippet guard) on an earlier
            # run — never re-extracted, same "doesn't change once
            # assigned" convention as grade/score/reasoning_summary.
            continue

        result.resources_extraction_attempted += 1
        try:
            # Phase 19: also passes resource.domain so
            # extract_claims_from_resource can look up this provider's own
            # extraction_instructions (docs/verified_resource_apis.json)
            # and fold provider-specific guidance into the same call.
            extracted = extract_claims_from_resource(
                resource.title, resource.publisher, resource.summary, resource.domain
            )
        except ResourceExtractionError as exc:
            logger.warning(
                "Skipping Stage 1 claims extraction for resource id=%s "
                "(%r) on ingredient %s — leaving extracted_data null; "
                "Stage 2 will fall back to this resource's raw summary "
                "text: %s",
                resource.id,
                resource.title,
                ingredient_id,
                exc,
            )
            result.resources_extraction_failed += 1
            continue

        # Phase 19: extracted_conclusions is split off into its own column
        # rather than nested inside extracted_data — Stage 2 synthesis
        # (conclusion_grader.py::_format_resources_for_prompt) reads
        # extracted_data's four fixed keys directly and would need
        # reshaping if a fifth, list-shaped key were added to it; keeping
        # extracted_data's shape untouched avoids that. See
        # VerifiedResource.extracted_conclusions's docstring in
        # app/models/research.py for the full reasoning.
        resource.extracted_data = {
            "official_stance": extracted["official_stance"],
            "recommended_dose": extracted["recommended_dose"],
            "upper_limit_warning": extracted["upper_limit_warning"],
            "key_takeaways": extracted["key_takeaways"],
        }
        resource.extracted_conclusions = extracted["extracted_conclusions"]
        session.add(resource)
        resources_extracted_this_run += 1

    if resources_extracted_this_run:
        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001 - same "log, don't fail" reasoning as elsewhere
            session.rollback()
            logger.warning(
                "Failed to save Stage 1 extracted_data for ingredient %s "
                "(%r) — %d resource(s) extracted this run were rolled "
                "back, Stage 2 will proceed using whatever extracted_data "
                "was already committed from earlier runs: %s",
                ingredient_id,
                ingredient_name,
                resources_extracted_this_run,
                exc,
            )
        else:
            logger.info(
                "[Pipeline Debug] Stage 1 extraction complete for "
                "ingredient id=%s (%r) — %d/%d resource(s) newly "
                "extracted this run (%d failed, left null).",
                ingredient_id,
                ingredient_name,
                resources_extracted_this_run,
                result.resources_extraction_attempted,
                result.resources_extraction_failed,
            )

    # --- Paper grading + conclusion synthesis ---
    # Phase 18: now runs AFTER Stage 1 resource extraction above
    # (previously ran first) — see module docstring's "Rate Limiting &
    # Execution Order" section. `papers` here is already capped to
    # MAX_PAPERS_PER_GRADED_INGREDIENT and ranked by
    # _paper_relevance_sort_key (see above).
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

    # --- Phase 11 / Phase 17 Stage 2: one ingredient-level synthesis
    # call, after every paper AND every resource above has had its
    # chance to grade/extract — not inside either loop (see module
    # docstring's "Multi-source ingredient summary synthesis" / "Two-
    # Stage Extraction Pipeline" sections for why this is deliberately a
    # single call per run).
    # Phase 16 audit: logged immediately before the call so the server
    # log itself is direct evidence of the precondition documented in
    # the module docstring above — that verified resources for this
    # ingredient have already been fetched/committed (and, as of Phase
    # 17, extraction-attempted) by this point in the request.
    logger.info(
        "[Pipeline Debug] About to synthesize ingredient summary for "
        "ingredient id=%s (%r) — %d paper(s) considered this run, %d "
        "verified resource(s) currently stored.",
        ingredient_id,
        ingredient_name,
        len(papers),
        len(resources_available),
    )

    try:
        summary_result = synthesize_ingredient_summary(session, ingredient_id, ingredient_name)
    except ConclusionGradingError as exc:
        logger.warning(
            "Skipping ingredient summary synthesis for ingredient %s (%r) — "
            "previously completed grades/conclusions remain saved: %s",
            ingredient_id,
            ingredient_name,
            exc,
        )
        summary_result = None

    if summary_result is not None:
        ingredient = session.get(Ingredient, ingredient_id)
        if ingredient is None:
            # Shouldn't happen — every caller already has a valid
            # ingredient_id in hand (see app/services/grading.py) — but
            # defensive rather than crashing the whole grade request over
            # a row that vanished mid-run.
            logger.warning(
                "Could not find Ingredient id=%s to save summary_description "
                "onto — skipping.",
                ingredient_id,
            )
        else:
            ingredient.summary_description = summary_result["summary_description"]
            session.add(ingredient)
            try:
                session.commit()
                result.ingredient_summary_generated = True
            except Exception as exc:  # noqa: BLE001 - same "log, don't fail" reasoning as above
                session.rollback()
                logger.warning(
                    "Failed to save summary_description for ingredient %s (%r): %s",
                    ingredient_id,
                    ingredient_name,
                    exc,
                )

    return result
