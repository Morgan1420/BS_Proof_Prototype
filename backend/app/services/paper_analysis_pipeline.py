"""Per-ingredient analysis pipeline (Phase 5), with ingredient relevance
verification (Phase 6) and rate-limit-aware execution ordering + a
per-run paper cap (Phase 18 — see "Rate Limiting & Execution Order"
below).

**No more Stage 1 here as of Phase 21 — read this first.** This module
used to run a "Stage 1" step (Phase 17) that made a Gemini call per
VerifiedResource to extract structured claims from it, ahead of the
per-paper grading loop below. That step doesn't exist here anymore: the
task that removed it ("replace the LLM-based resource extraction service
with a fast, zero-LLM deterministic parser") named this file as the
integration point, but the raw API payloads that parser needs only ever
existed inside `app/services/resource_fetcher.py`'s per-source query
functions — by the time control reaches this module, a VerifiedResource
row's raw API response is long gone, only its already-parsed
title/publisher/summary remain. So extraction now happens synchronously,
deterministically, and for free, right at fetch time in
`resource_fetcher.py::fetch_verified_resources_for_ingredient` (see that
function's own Phase 21 docstring paragraph, and
`app/services/resource_parser.py`'s module docstring for the full "why
deterministic, not Gemini" reasoning) — there's nothing left for a later
pipeline pass to do. `app/services/resource_extractor.py` (the old
Gemini-based module) is no longer imported or called anywhere in this
codebase; it's left in place, unused, purely as historical reference (its
own docstring has been updated to say so) rather than deleted outright.
One real, accepted side effect: `VerifiedResource.extracted_data` (the
old Stage 1's four-field structured shape, distinct from
`extracted_conclusions`) is no longer populated for any resource fetched
after this phase — `conclusion_grader.py`'s Stage 2 synthesis already had
its own fallback to a resource's raw `summary` text for exactly this
"never extracted" case, so Stage 2 still runs fine, just working from
slightly less structured input per resource than it did under Phase 17.

Runs after app/services/paper_search.py has found/persisted new papers,
and app/services/resource_fetcher.py has found/persisted new verified
resources (now already conclusion-extracted, as of Phase 21), for an
ingredient (see app/services/grading.py::grade_ingredient). In this
order:

**Phase 41 note — this module needs no changes for "Grade Again".**
When `grade_ingredient` is re-run for an ingredient that was already
graded (the standalone IngredientCard's "Grade Again" affordance, see
that function's own "Phase 41" docstring paragraph), every
ResearchPaper/PaperConclusion/VerifiedResource row for it is purged —
and committed — BEFORE paper_search.py/resource_fetcher.py run again,
i.e. before this module's analyze_ingredient_papers() is ever called.
So by the time control reaches here, "fresh papers" and "fresh
resources" always simply mean *every* stored paper/resource for this
run, first-time grade or tenth re-grade alike — this module doesn't need
to know or care which one it is, the same way it's never needed to know
that today.

  1. **Paper grading + conclusion synthesis**, for at most
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
  2. **HTML scraping + Gemini extraction fallback (Phase 27)** — for any
     VerifiedResource whose deterministic (Phase 21) extraction came back
     completely empty, tries recovering conclusions from that resource's
     own live webpage instead — see "HTML fallback extraction" below.
     Runs after the paper loop (papers still get first claim on this
     run's Gemini quota) but before Stage 2, so Stage 2 sees whatever
     this step recovered.
  2b. **Conclusion refinement (Phase 40)** — for every VerifiedResource
     with a non-empty `extracted_conclusions` at this point (deterministic
     + HTML fallback both already applied), runs
     `conclusion_refine_service.py::refine_conclusions` to strip
     boilerplate/disclaimers/off-topic items and merge near-duplicate
     paraphrases WITHIN that one resource's own list — see that module's
     own docstring for why this is scoped per-resource rather than
     merging across resources. Runs after step 2 (so it also cleans up
     anything the HTML fallback just recovered) and before Stage 2, so
     Stage 2 synthesizes from the cleaned-up input.
  3. **Stage 2 — unified conclusion synthesis** (unchanged position,
     still last of the two conclusion_grader.py stages — see "Multi-
     source ingredient summary synthesis" below; still numbered "Stage 2"
     for continuity with conclusion_grader.py's own naming, even though
     there's no longer a "Stage 1" in this particular module).
  4. **Resource claim alignment (Phase 22)** — after Stage 2 above has
     run (so this always classifies against the freshest possible set of
     `PaperConclusion` rows, including any this very run's per-paper loop
     just merged in), classifies every `VerifiedResource.extracted_conclusions`
     string for the ingredient as AGREES/CONTRADICTS/DISTINCT_NEW against
     those `PaperConclusion` rows, via
     `app/services/resource_aligner.py::align_resource_conclusions_for_ingredient`
     — see that module's own docstring for the one-call-per-ingredient
     batching, index-based mapping, deterministic short-circuit, and
     strict-fallback design. Persists the result onto each resource's own
     `aligned_conclusions` column. Same best-effort philosophy as every
     other step here: never allowed to fail the whole grade request.

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
together — and persists the resulting `summary_description` AND (Phase
23, renamed `scientific_conclusions` Phase 24) the fully rubric-scored
claims array onto the `Ingredient` row (`app/models/supplement.py`) in
the same commit — see that column's own docstring, and
conclusion_grader.py's module docstring's "Phase 23"/"Phase 24"
paragraphs, for the Multi-Source Confidence Rubric
(`docs/multi_source_confidence_rubric.json`) each `scientific_conclusions`
claim is now scored against, plus Phase 24's Direct Injection Safety Net
guaranteeing every parsed VerifiedResource conclusion appears somewhere
in that array. This is a single call per
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

**HTML fallback extraction (Phase 27).** `resource_parser.py`'s
deterministic extraction (Phase 21) is fast and free, but it's only as
good as the raw API payload it's given — some sources' searchable JSON
endpoints return little more than a title and a URL, with no summary/
snippet text at all, leaving `VerifiedResource.extracted_conclusions`
genuinely empty even though the resource's own live webpage likely has
real content. This step (right after the paper loop above, before Stage
2 below) queries `resources_available` (already fetched above) for every
resource with an empty `extracted_conclusions`, and for up to
`MAX_HTML_FALLBACK_RESOURCES_PER_RUN` of them (same "cap it, don't let
one grade request run an unbounded number of these" reasoning as
`MAX_PAPERS_PER_GRADED_INGREDIENT` below), calls
`app/services/html_resource_extractor.py::extract_conclusions_from_webpage`
— which fetches that resource's own `url`, strips it down to clean text,
and makes one Gemini call against the richer page content. Any recovered
conclusions are written directly onto
`resource.extracted_conclusions` (clearing `extraction_failure_reason`
back to `None`) and committed immediately, so Stage 2 below — and the
Phase 24 Direct Injection Safety Net inside
`synthesize_ingredient_summary` itself, which loops over every
resource's `extracted_conclusions` — both see the enriched data. Same
best-effort philosophy as every other step here: a fetch/parse/Gemini
failure for one resource is logged and skipped, never allowed to fail
the whole grade request; a resource still stuck at zero conclusions
after this step simply falls back to Stage 2's existing raw-`summary`-
text fallback, exactly as it did before this phase existed. See
`html_resource_extractor.py`'s own module docstring for the full fetch/
clean/extract design, including why it's a synchronous function
internally bridging one `asyncio.run()` call for its HTTP fetch, not an
`async def` all the way through.

**Two-Stage Extraction Pipeline (Phase 17, Stage 1 retired Phase 21).**
Phase 17 replaced the single-step "dump every paper and every resource
into one synthesis prompt" approach audited in Phase 16 above with two
distinct steps, specifically to fix a "lost-in-the-middle" effect where
Gemini consistently favored dense paper abstracts over thin web-resource
snippets when both were mixed into one prompt. Stage 2 (below) still
exists and still gets that same benefit — every VerifiedResource this
function sees by the time it calls `synthesize_ingredient_summary()` has
already had `extracted_conclusions` computed, just via
`resource_parser.py`'s deterministic rules at fetch time (Phase 21)
rather than via a Gemini call in a "Stage 1" step that used to live
right here. See this module's own docstring intro above for the full
story of why Stage 1 moved out of this file entirely, and
app/services/resource_parser.py's module docstring for the extraction
logic itself.

  - **Stage 2 (ingredient-level,
    app/services/conclusion_grader.py::synthesize_ingredient_summary,
    called at the end of this function, below).** Consumes each
    resource's `extracted_conclusions` (Phase 19/20, deterministic since
    Phase 21) alongside the papers' own already-extracted PaperConclusion
    findings — see that function's module docstring for the resources-
    first prompt structure. A resource whose deterministic extraction
    came back empty (see `VerifiedResource.extraction_failure_reason`)
    falls back to that resource's raw `summary` text, same as any
    resource that was never extracted at all — see conclusion_grader.py's
    `_format_resources_for_prompt`.

**Rate Limiting & Execution Order (Phase 18, resource-extraction
reordering point retired Phase 21).** Under sustained grading traffic
this app was hitting the Gemini free-tier's requests-per-minute ceiling
mid-run — a burst of newly-found papers graded back-to-back, with no
spacing between calls, could all land in the same one-minute quota
window and start failing together. Phase 18 addressed this with two
changes; the first no longer applies as written (Stage 1 doesn't exist
in this module anymore to reorder), but is kept below for historical
context:

  - ~~Stage 1 now runs BEFORE paper grading, not after~~ — moot as of
    Phase 21: resource conclusion extraction happens at fetch time in
    `resource_fetcher.py`, before `analyze_ingredient_papers()` (this
    function) is even called at all, so there's no ordering decision to
    make between it and the paper-grading loop below anymore. This
    module's paper-grading loop simply gets first (and only) claim on
    whatever Gemini quota this run has, by construction.
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
    `paper_grader.py`'s `grade_paper()` — `resource_extractor.py`'s
    `extract_claims_from_resource()` no longer runs at all, see this
    module's docstring intro — plus, as of Phase 27,
    `html_resource_extractor.py`'s `extract_conclusions_from_webpage()`
    for the capped set of resources needing the HTML fallback, see that
    section above) is still paced (~4.5s minimum spacing, process-wide)
    and retried with exponential backoff on a 429/`RESOURCE_EXHAUSTED`
    response — see `app/services/gemini_rate_limit.py`'s module docstring
    for the full mechanism, including two deliberate deviations from how
    a rate-limit retry helper is sometimes written (synchronous rather
    than `async def`; and catching `google.genai.errors.ClientError`/
    `APIError` rather than `google.api_core.exceptions.ResourceExhausted`,
    which this app's actual Gemini dependency — `google-genai` — never
    raises).
  - **`MAX_HTML_FALLBACK_RESOURCES_PER_RUN = 3` (Phase 27)** caps how
    many resources a single run attempts the HTML fallback for — the
    same rate-limit-pressure reasoning as
    `MAX_PAPERS_PER_GRADED_INGREDIENT` above, just scoped to this new
    step: an ingredient whose resources mostly failed deterministic
    extraction would otherwise trigger a full webpage fetch + Gemini call
    for every single one, sequentially, adding real latency and Gemini
    quota pressure to every grade request. Resources needing the
    fallback are attempted in whatever order `resources_available` was
    queried in (no additional ranking — unlike the paper cap, there's no
    equivalent "best available quality signal" to rank ungraded
    resources by) — an ingredient with more than 3 resources needing the
    fallback in one run simply picks up the rest on a future grade
    request.

**General Information extraction (Phase 33).** After every step above
(paper grading, HTML fallback, Stage 2 synthesis, AND resource claim
alignment) has finished for this run, this module makes one final call to
`app/services/general_info_extractor.py::extract_general_info` — which
resolves a short `description` and `daily_dosage` for the ingredient,
each independently sourced from the single highest-graded Grade A/B
`VerifiedResource` if one exists, falling back to the single highest-
graded Grade A/B `ResearchPaper` if no resource has it, or marked
honestly unavailable if neither does (see that module's own docstring for
the full hierarchy and why Grade C/D/E sources are never even visible to
it). Persists the resulting two-field JSON block onto
`Ingredient.general_info` (`app/models/supplement.py`) in its own commit,
separate from the Stage 2 `summary_description`/`scientific_conclusions`
commit above — a General Information failure should never roll back an
already-successful Stage 2 synthesis, or vice versa. Placed last
(after resource alignment, not right after Stage 2) specifically so it
sees the freshest possible Grade A/B state for both papers and
resources — resource alignment doesn't change any `grade` value itself,
but placing this absolute last keeps the ordering simple and future-proof
against a later step that might. Same best-effort philosophy as every
other step here: a Gemini/commit failure is logged and skipped, never
allowed to fail the whole grade request — see that module's own "never
raises" docstring guarantee.

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
from app.services.resource_aligner import align_resource_conclusions_for_ingredient
from app.services.html_resource_extractor import extract_conclusions_from_webpage
from app.services.general_info_extractor import extract_general_info
from app.services.resource_fetcher import is_nih_domain
from app.services.conclusion_refine_service import refine_conclusions

# Phase 21: app/services/resource_extractor.py (the old Gemini-based
# Stage 1 extraction module) is deliberately NOT imported here anymore —
# see this module's own docstring intro for why resource conclusion
# extraction moved to app/services/resource_parser.py, called from
# app/services/resource_fetcher.py, and no longer has any step in this
# file at all. `extract_conclusions_from_webpage` above is a DIFFERENT,
# Phase 27 addition — a narrow, targeted webpage-scraping fallback used
# only when that deterministic parser came back completely empty for a
# resource — see this module's own "Phase 27" docstring section below
# and app/services/html_resource_extractor.py's module docstring.

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

# Phase 27: caps how many VerifiedResource rows a single
# analyze_ingredient_papers() run attempts the HTML scraping + Gemini
# extraction fallback for — see module docstring's "HTML fallback
# extraction" and "Rate Limiting & Execution Order" sections for why.
MAX_HTML_FALLBACK_RESOURCES_PER_RUN = 3


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
    # How many VerifiedResource rows for this ingredient exist at Stage 2
    # synthesis time, below — purely informational (see this dataclass's
    # own docstring). Through Phase 20 this also doubled as a Stage 1
    # "how many did we consider extracting from" count; as of Phase 21
    # there's no Stage 1 in this module anymore (see module docstring
    # intro) — every VerifiedResource already has its
    # extracted_conclusions/extraction_failure_reason set by
    # resource_fetcher.py at fetch time, well before this function ever
    # sees it, so this field is now simply "how many resources exist."
    resources_considered: int = 0
    # Phase 27: how many VerifiedResource rows (out of those with an empty
    # extracted_conclusions) this run actually attempted the HTML scraping
    # + Gemini extraction fallback for — capped by
    # MAX_HTML_FALLBACK_RESOURCES_PER_RUN, see module docstring's "HTML
    # fallback extraction" section. 0 whenever every resource already had
    # a non-empty extracted_conclusions from the Phase 21 deterministic
    # parser (the common case).
    resources_html_fallback_attempted: int = 0
    # Phase 27: of those attempted above, how many actually came back with
    # at least one recovered conclusion (i.e. extract_conclusions_from_webpage
    # returned a non-empty list and resource.extracted_conclusions was
    # updated). Always <= resources_html_fallback_attempted; a gap between
    # the two just means the webpage fetch/Gemini call didn't recover
    # anything for that resource — not itself an error, see
    # html_resource_extractor.py's own "never raises" design.
    resources_html_fallback_enriched: int = 0
    # Phase 40 — how many raw extracted_conclusions strings existed
    # across every VerifiedResource for this ingredient right before the
    # conclusion_refine_service.py pass ran (i.e. after Phase 21/39
    # deterministic extraction AND the Phase 27 HTML fallback above have
    # both finished, so this reflects the true "raw pool" the task
    # described as "up to 100+ items"), and how many remained afterward.
    # `conclusions_refined_after <= conclusions_refined_before` is the
    # common case but not guaranteed — see
    # conclusion_refine_service.py::refine_conclusions's "falls back to
    # the original list unchanged" paths (too few items to bother, a
    # Gemini failure, or a total-wipeout safety net), each of which
    # leaves a resource's count exactly as it was. Both 0 whenever this
    # ingredient has no VerifiedResource rows with any extracted_conclusions
    # at all yet.
    conclusions_refined_before: int = 0
    conclusions_refined_after: int = 0
    # Phase 11: True iff synthesize_ingredient_summary() ran successfully
    # this call and its summary_description was saved onto the
    # Ingredient row. False either because there was no evidence to
    # synthesize from (see that function's docstring) or because the
    # Gemini call/commit failed — both are logged separately, this flag
    # alone doesn't distinguish which.
    ingredient_summary_generated: bool = False
    # Phase 22 — see app/services/resource_aligner.py::AlignmentResult,
    # which these four mirror directly (this dataclass just flattens that
    # one onto PipelineResult rather than nesting it, same "flat,
    # informational-only" style as every other field here).
    resources_aligned: int = 0
    alignment_conclusions_considered: int = 0
    alignment_gemini_call_made: bool = False
    alignment_fallback_used: bool = False
    # Phase 33: True iff general_info_extractor.py::extract_general_info
    # ran (it always runs — unlike ingredient_summary_generated above,
    # there's no "skip entirely" path once paper/resource grading has
    # happened at all this run) and its result was successfully committed
    # onto the Ingredient row. False only on a commit failure — see the
    # module docstring's "Phase 33" section below.
    general_info_generated: bool = False


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
    """Runs the grade -> relevance-check -> (maybe) synthesize-conclusions
    loop over at most `MAX_PAPERS_PER_GRADED_INGREDIENT` currently-active
    ResearchPaper rows stored for `ingredient_id` (Phase 18 cap, ranked by
    `_paper_relevance_sort_key`), then makes one final, ingredient-level
    Stage 2 call to synthesize `summary_description` from every graded
    paper AND every VerifiedResource together (Phase 11, prompt
    restructured Phase 17). No Stage 1 step runs here as of Phase 21 —
    every VerifiedResource already has its `extracted_conclusions` set by
    the time this function is even called (see module docstring intro).

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

    # --- VerifiedResource lookup (Phase 21: no Stage 1 extraction loop
    # here anymore — every VerifiedResource already has its
    # extracted_conclusions/extraction_failure_reason set by
    # app/services/resource_fetcher.py at fetch time, well before this
    # function is ever called; see module docstring intro). Queried here
    # purely so `result.resources_considered` and the Stage 2 debug log
    # below can report an accurate count. ---
    resources_available = session.exec(
        select(VerifiedResource).where(VerifiedResource.ingredient_id == ingredient_id)
    ).all()
    result.resources_considered = len(resources_available)

    # --- Paper grading + conclusion synthesis ---
    # `papers` here is already capped to MAX_PAPERS_PER_GRADED_INGREDIENT
    # and ranked by _paper_relevance_sort_key (see above). Gets first (and
    # only) claim on this run's Gemini quota — as of Phase 21 there's no
    # Stage 1 resource extraction step left to compete with (see module
    # docstring intro).
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

    # --- Phase 27: HTML scraping + Gemini extraction fallback ---
    # Runs after the paper loop above (papers keep first claim on this
    # run's Gemini quota, per the existing Phase 18 ordering principle)
    # but before Stage 2 below, so Stage 2 — and the Phase 24 Direct
    # Injection Safety Net inside synthesize_ingredient_summary — both see
    # whatever this step recovers. See module docstring's "HTML fallback
    # extraction" section for the full design/rationale.
    resources_needing_fallback = [
        resource
        for resource in resources_available
        if not resource.extracted_conclusions
    ][:MAX_HTML_FALLBACK_RESOURCES_PER_RUN]

    if resources_needing_fallback:
        logger.info(
            "[Pipeline Debug] MAX_HTML_FALLBACK_RESOURCES_PER_RUN=%d — "
            "attempting HTML fallback extraction for %d resource(s) with "
            "empty extracted_conclusions, for ingredient id=%s (%r).",
            MAX_HTML_FALLBACK_RESOURCES_PER_RUN,
            len(resources_needing_fallback),
            ingredient_id,
            ingredient_name,
        )

    for resource in resources_needing_fallback:
        result.resources_html_fallback_attempted += 1
        try:
            # Phase 39 — is_nih_domain(resource.domain) selects the
            # NIH-specific exhaustive extraction prompt + raised
            # conclusions cap inside extract_conclusions_from_webpage
            # (see that function's own docstring) whenever this resource
            # is a confirmed NIH/NLM property. A resource only reaches
            # this HTML fallback at all when resource_parser.py's Phase
            # 21 deterministic pass already found nothing for it — for an
            # NIH source specifically, that pass now includes the Phase
            # 39 structured MedlinePlus parser too (see
            # resource_parser.py::_parse_medlineplus), so this fallback
            # is genuinely the second-chance path, not the primary one,
            # even for NIH resources.
            recovered_conclusions = extract_conclusions_from_webpage(
                resource.url,
                resource.publisher,
                ingredient_name,
                is_nih=is_nih_domain(resource.domain),
            )
        except Exception as exc:  # noqa: BLE001 - same "log, don't fail" reasoning as every other step here
            logger.warning(
                "Skipping HTML fallback extraction for resource id=%s "
                "(url=%r) on ingredient %s — previously completed "
                "progress remains saved: %s",
                resource.id,
                resource.url,
                ingredient_id,
                exc,
            )
            continue

        if not recovered_conclusions:
            continue

        resource.extracted_conclusions = recovered_conclusions
        resource.extraction_failure_reason = None
        session.add(resource)
        result.resources_html_fallback_enriched += 1

    if resources_needing_fallback:
        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001 - same "log, don't fail" reasoning as above
            session.rollback()
            logger.warning(
                "Failed to commit HTML fallback extraction results for "
                "ingredient %s (%r) — falling back to Stage 2's existing "
                "raw-summary-text fallback for these resource(s) instead: %s",
                ingredient_id,
                ingredient_name,
                exc,
            )
            result.resources_html_fallback_enriched = 0

    # --- Phase 40: post-extraction conclusion refinement pass ---
    # Runs after BOTH the Phase 21/39 deterministic parser (already
    # applied at fetch time, well before this function is ever called —
    # see resource_fetcher.py) and the Phase 27 HTML fallback loop just
    # above have had their chance to populate/enrich each
    # VerifiedResource.extracted_conclusions, and BEFORE Stage 2 below —
    # so synthesize_ingredient_summary (and its Phase 24 Direct Injection
    # Safety Net) both see the cleaned-up, deduplicated input, not the
    # noisier raw extraction. See conclusion_refine_service.py's own
    # module docstring for why this refines each resource's own
    # extracted_conclusions list in place (removing boilerplate/fluff/
    # off-topic items and merging near-duplicate paraphrases WITHIN that
    # one resource) rather than writing a separately-shaped result
    # straight to `Ingredient.scientific_conclusions` — that field's
    # existing, richer shape (server-derived `confidence_grade`/
    # `total_score`/`score_breakdown`, already read by real frontend
    # components) is preserved untouched; Stage 2's existing cross-
    # resource merging + scoring engine is what actually produces it,
    # same as before this phase, just fed cleaner input now.
    for resource in resources_available:
        if not resource.extracted_conclusions:
            continue
        before_count = len(resource.extracted_conclusions)
        result.conclusions_refined_before += before_count
        try:
            refined = refine_conclusions(resource.extracted_conclusions, ingredient_name)
        except Exception as exc:  # noqa: BLE001 - same "log, don't fail" reasoning as every other step here
            logger.warning(
                "Skipping conclusion refinement for resource id=%s "
                "(url=%r) on ingredient %s — previously extracted "
                "conclusions remain saved unrefined: %s",
                resource.id,
                resource.url,
                ingredient_id,
                exc,
            )
            result.conclusions_refined_after += before_count
            continue

        result.conclusions_refined_after += len(refined)
        if refined != resource.extracted_conclusions:
            resource.extracted_conclusions = refined
            session.add(resource)

    if result.conclusions_refined_before:
        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001 - same "log, don't fail" reasoning as above
            session.rollback()
            logger.warning(
                "Failed to commit conclusion refinement results for "
                "ingredient %s (%r) — Stage 2 below will synthesize from "
                "the original, unrefined extracted_conclusions instead: %s",
                ingredient_id,
                ingredient_name,
                exc,
            )
            result.conclusions_refined_after = result.conclusions_refined_before

        # Requirement: log the before/after counts for this refinement
        # pass, ingredient-level (summed across every resource touched
        # this run) — matches the task's own requested log line, adapted
        # to this pipeline's real per-resource, per-run granularity
        # rather than one global "raw list" that doesn't otherwise exist
        # as a single variable in this codebase (see
        # conclusion_refine_service.py's module docstring).
        logger.info(
            "%s Consolidated conclusions from %d down to %d clean "
            "item(s) for ingredient id=%s (%r).",
            "[ConclusionRefine]",
            result.conclusions_refined_before,
            result.conclusions_refined_after,
            ingredient_id,
            ingredient_name,
        )

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
            # Phase 23 — Multi-Source Confidence Rubric: persist the fully
            # scored scientific_conclusions array (renamed Phase 24 from
            # recommended_uses — see conclusion_grader.py's module
            # docstring "Phase 23"/"Phase 24" paragraphs and
            # Ingredient.scientific_conclusions's own docstring in
            # app/models/supplement.py). Written in the same commit as
            # summary_description, not a separate one — both fields come
            # from the same single Gemini call/result, so either both land
            # or (on a commit failure below) neither does. Note that by
            # this point summary_result["scientific_conclusions"] already
            # includes any Phase 24 Direct Injection Safety Net claims —
            # the injection pass runs inside synthesize_ingredient_summary
            # itself, before this function ever sees the result.
            ingredient.scientific_conclusions = summary_result["scientific_conclusions"]
            session.add(ingredient)
            try:
                session.commit()
                result.ingredient_summary_generated = True
            except Exception as exc:  # noqa: BLE001 - same "log, don't fail" reasoning as above
                session.rollback()
                logger.warning(
                    "Failed to save summary_description/scientific_conclusions for "
                    "ingredient %s (%r): %s",
                    ingredient_id,
                    ingredient_name,
                    exc,
                )

    # --- Phase 22: resource claim alignment — runs AFTER Stage 2 above so
    # it always sees the freshest PaperConclusion set for this run (see
    # module docstring's "Resource claim alignment" step, and
    # resource_aligner.py's own docstring for the full design). Never
    # raises — align_resource_conclusions_for_ingredient applies its own
    # DISTINCT_NEW-with-note fallback internally on any Gemini failure,
    # same best-effort philosophy as every other step in this function.
    logger.info(
        "[Pipeline Debug] About to align resource conclusions for "
        "ingredient id=%s (%r).",
        ingredient_id,
        ingredient_name,
    )
    alignment_result = align_resource_conclusions_for_ingredient(
        session, ingredient_id, ingredient_name
    )
    result.resources_aligned = alignment_result.resources_updated
    result.alignment_conclusions_considered = alignment_result.conclusions_considered
    result.alignment_gemini_call_made = alignment_result.gemini_call_made
    result.alignment_fallback_used = alignment_result.fallback_used

    # --- Phase 33: General Information (Description + Daily Dosage) —
    # runs LAST, after every other step above, so it sees the freshest
    # possible Grade A/B paper/resource state — see module docstring's
    # "General Information extraction (Phase 33)" section. Always makes
    # (or at least attempts) this call, unlike Stage 2's "skip entirely if
    # there's zero evidence at all" — extract_general_info itself already
    # short-circuits internally (no Gemini call) when there are zero
    # Grade A/B candidates, and always returns a full, persistable result
    # either way (see that function's own docstring), so there's no
    # analogous "skip this whole step" branch needed here.
    logger.info(
        "[Pipeline Debug] About to extract General Information for "
        "ingredient id=%s (%r).",
        ingredient_id,
        ingredient_name,
    )
    general_info_result = extract_general_info(session, ingredient_id, ingredient_name)
    ingredient = session.get(Ingredient, ingredient_id)
    if ingredient is None:
        # Shouldn't happen — same defensive reasoning as the Stage 2 write
        # above (every caller already has a valid ingredient_id in hand).
        logger.warning(
            "Could not find Ingredient id=%s to save general_info onto — "
            "skipping.",
            ingredient_id,
        )
    else:
        ingredient.general_info = general_info_result
        session.add(ingredient)
        try:
            session.commit()
            result.general_info_generated = True
        except Exception as exc:  # noqa: BLE001 - same "log, don't fail" reasoning as every other step here
            session.rollback()
            logger.warning(
                "Failed to save general_info for ingredient %s (%r): %s",
                ingredient_id,
                ingredient_name,
                exc,
            )

    return result
