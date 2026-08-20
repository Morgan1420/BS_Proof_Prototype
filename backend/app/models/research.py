"""SQLModel ORM tables for scientific research: ResearchPaper (one row
per paper linked to a canonical Ingredient — Phase 2, automated paper
scraping/grading), PaperConclusion (one row per synthesized claim built
up across those papers — Phase 5, see
app/services/conclusion_grader.py), and VerifiedResource (one row per
official government/regulatory reference link — Phase 7, see
app/services/resource_fetcher.py). VerifiedResource also carries
`extracted_conclusions`/`extraction_failure_reason` (Phase 19/20,
deterministic since Phase 21 — see app/services/resource_parser.py), a
per-resource list of short factual conclusions extracted independently
of every other resource — see that field's own docstring below.
`extracted_data` (Phase 17, deprecated Phase 21 — see that field's own
docstring) is the now-unused predecessor shape, kept only for backward
compatibility with rows persisted before Phase 21.

ResearchPaper rows are populated by app/services/paper_search.py, which
queries PubMed, Europe PMC, Semantic Scholar, and OpenAlex per
Gemini-generated keyword (see app/services/research_keywords.py) and
persists deduplicated results here. PaperConclusion rows are populated
by app/services/conclusion_grader.py, driven by
app/services/paper_analysis_pipeline.py. VerifiedResource rows are
populated by app/services/resource_fetcher.py, which queries the
official APIs configured in docs/verified_resource_apis.json. See
app/services/grading.py for the full pipeline all three feed into.

Deliberately its own module (not folded into app/models/supplement.py):
these are distinct, independently-growing tables with no direct
dependency on Product/ProductIngredientLink, and keeping them separate
avoids bloating the M2M schema module with an unrelated concern.
"""

# Deliberately NOT using `from __future__ import annotations` — same
# reasoning as app/models/supplement.py's module docstring: it would turn
# the `Relationship()` annotation into a plain string, which trips a
# strict SQLAlchemy check that can no longer tell a real `Optional[...]`
# generic apart from an unparsed string.

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import JSON, Column, String
from sqlmodel import Field, Relationship, SQLModel

from app.models.supplement import Ingredient


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Phase 6: ingredient relevance verification ---
# ResearchPaper.status lifecycle values (see that field's docstring
# below, and app/services/paper_grader.py::grade_single_paper, which is
# what actually sets it). Named constants rather than bare string
# literals scattered across paper_grader.py/conclusion_grader.py/
# paper_analysis_pipeline.py/search.py — every one of those modules
# imports from here rather than re-typing the literal, so a rename can't
# silently desync one call site from another.
PAPER_STATUS_ACTIVE = "ACTIVE"
PAPER_STATUS_DISCARDED_IRRELEVANT = "DISCARDED_IRRELEVANT"


# `ResearchPaper.keywords` is stored as a single comma-separated string
# column (same convention as `authors` on this model) rather than a JSON
# column or a separate keywords table — SQLite's JSON support is limited
# and per-paper keyword counts are small (a handful at most, bounded by
# how many Gemini-generated search terms surfaced that paper), so a flat
# string round-trips fine through these two helpers without needing real
# relational structure. Shared by app/services/paper_search.py (writes)
# and app/services/search.py (reads, for the API response) so both sides
# agree on the exact separator/whitespace handling.
_KEYWORDS_SEPARATOR = ", "


def serialize_keywords(keywords: List[str]) -> Optional[str]:
    """Joins a deduplicated, order-preserved keyword list into the flat
    string stored on `ResearchPaper.keywords`. Returns None (not "") for
    an empty list, matching the column's nullable/Optional type.
    """
    cleaned = [keyword.strip() for keyword in keywords if keyword and keyword.strip()]
    return _KEYWORDS_SEPARATOR.join(cleaned) if cleaned else None


def parse_keywords(value: Optional[str]) -> List[str]:
    """Inverse of serialize_keywords — splits the stored string back into
    a list, tolerating stray whitespace. Returns [] for None/empty.
    """
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


class ResearchPaper(SQLModel, table=True):
    """A single scientific paper/article found for one canonical
    Ingredient, sourced from PubMed / Europe PMC / Semantic Scholar.

    Deduplicated per-ingredient by `source_url` (falling back to a
    normalized `title` match) in app/services/paper_search.py — the same
    paper turning up again for a different keyword shouldn't create a
    second row.
    """

    __tablename__ = "research_papers"

    id: Optional[int] = Field(default=None, primary_key=True)
    ingredient_id: int = Field(foreign_key="ingredients.id", index=True)

    title: str
    abstract: Optional[str] = Field(default=None)
    # Comma-separated list of author names — kept as a flat string rather
    # than a separate table/relationship since nothing in this app needs
    # to query "papers by a given author" yet; revisit if that changes.
    authors: Optional[str] = Field(default=None)
    # Kept as `str` rather than `date`: the three source APIs return
    # publication dates in inconsistent formats/granularity (a bare
    # year, "YYYY Mon", a full ISO date, or a free-text MedlineDate range
    # for PubMed specifically) — normalizing all of that to a single
    # `date` type isn't worth the complexity for a debug-stage feature
    # that only displays this, never sorts/filters by it.
    publication_date: Optional[str] = Field(default=None)
    source_url: str
    # e.g. "pubmed.ncbi.nlm.nih.gov", "europepmc.org", "semanticscholar.org".
    source_domain: str

    # Which Gemini-generated search keyword(s) (see
    # app/services/research_keywords.py) turned this paper up, stored via
    # serialize_keywords() above — a comma-separated string, same
    # convention as `authors`. Deduplicated: if the same paper is found
    # again (later in the same grade request via a different
    # keyword/source, or in a subsequent re-grade of this ingredient),
    # the new keyword is merged in rather than creating a duplicate row
    # or overwriting what's already recorded — see
    # app/services/paper_search.py::search_papers_for_ingredient.
    # Nullable/added after `research_papers` already existed in deployed
    # databases — see app/db.py::_migrate_research_paper_columns for the
    # additive migration this needs, same reasoning as the Phase 2
    # is_graded/grade_badge_text columns on Ingredient.
    keywords: Optional[str] = Field(default=None)

    # --- Phase 3: automated paper grading (app/services/paper_grader.py) ---
    # Set together, once, right after a paper is first persisted by
    # search_papers_for_ingredient() — never re-graded on subsequent
    # re-grade runs (a paper's evaluation doesn't change once assigned).
    # All three are nullable/added after `research_papers` already
    # existed in deployed databases — same additive-migration story as
    # `keywords` above; see app/db.py::_migrate_research_paper_columns.
    #
    # `grade` is always one of "A"/"B"/"C"/"D"/"E", derived server-side
    # from `grade_score` via docs/paper_grading_rubric.json's
    # `grade_bands` (not trusted directly from Gemini's own output) — see
    # paper_grader.py::grade_paper for why.
    grade: Optional[str] = Field(default=None)
    grade_score: Optional[int] = Field(default=None)
    # Structured per-category breakdown (study_type/study_type_score,
    # journal_reputation/journal_score, sample_info/sample_score,
    # funding_status/funding_score, total_score, summary_notes) — see
    # paper_grader.py's _RubricEvaluationSchema for the exact shape.
    # Stored as a native JSON column (SQLAlchemy's `JSON` type, which
    # SQLite persists as TEXT under the hood) rather than another
    # hand-rolled comma-string like `authors`/`keywords`, since this is
    # genuinely structured/nested data, not a flat list.
    rubric_evaluation: Optional[dict] = Field(default=None, sa_column=Column(JSON))

    # --- Phase 6: ingredient relevance verification
    # (app/services/paper_grader.py) ---
    # One of PAPER_STATUS_ACTIVE / PAPER_STATUS_DISCARDED_IRRELEVANT
    # (defined above). Every paper starts "ACTIVE" at ingestion time
    # (search_papers_for_ingredient doesn't grade or relevance-check
    # anymore — see paper_search.py) and stays that way until it's
    # graded: grade_single_paper's same Gemini call that produces
    # `grade`/`grade_score`/`rubric_evaluation` also asks whether the
    # paper is actually about the target ingredient it was found under,
    # and flips this to "DISCARDED_IRRELEVANT" if not (e.g. a Vitamin D
    # paper that turned up during a Vitamin C search). Nullable/added
    # after `research_papers` already existed in deployed databases —
    # same additive-migration story as `keywords`/`grade` above; see
    # app/db.py::_migrate_research_paper_columns. Every paper-list/
    # summary query in app/services/search.py excludes
    # DISCARDED_IRRELEVANT rows — see get_ingredient_papers.
    status: str = Field(default=PAPER_STATUS_ACTIVE)

    # --- Phase 19: extracted conclusions
    # (app/services/paper_grader.py::grade_paper) ---
    # 2-4 short, factual, study-level findings Gemini extracts during the
    # SAME grading call that produces `grade`/`grade_score`/
    # `rubric_evaluation`/relevance (see grade_paper's `_RubricEvaluationSchema`)
    # — deliberately folded into that one existing call rather than a
    # second, separate Gemini request per paper: this codebase's grading
    # pipeline already went through a rate-limiting pass (Phase 18,
    # app/services/gemini_rate_limit.py) specifically to reduce Gemini
    # call volume, so adding a second call per paper here would directly
    # work against that. e.g. `["Demonstrated 18% reduction in
    # inflammation markers", "Well-tolerated at 500mg daily"]`. Rendered
    # under an "Extracted Conclusions" heading in the frontend's paper
    # info modal (src/components/StudiesList.tsx) — None until this
    # paper is graded (same "None until grade is None" convention as
    # `rubric_evaluation` above), and the frontend renders a "No specific
    # conclusions extracted for this source yet." fallback line for that
    # case rather than an empty section. Stored as a native JSON column
    # (list of strings) — same convention as
    # PaperConclusion.supporting_paper_ids elsewhere in this module.
    # Nullable/added after `research_papers` already existed in deployed
    # databases — same additive-migration story as every other column on
    # this table; see app/db.py::_migrate_research_paper_columns.
    extracted_conclusions: Optional[List[str]] = Field(default=None, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=_utcnow, nullable=False)

    ingredient: Optional[Ingredient] = Relationship(back_populates="papers")


class PaperConclusion(SQLModel, table=True):
    """A single synthesized scientific conclusion/claim for one canonical
    Ingredient, built up incrementally across every graded ResearchPaper
    (grade_score > 50) whose findings support or contradict it — Phase 5,
    see app/services/conclusion_grader.py::process_paper_conclusions.

    Unlike ResearchPaper (one row per paper found), this is one row per
    *distinct claim* — e.g. "Improves deep sleep duration" — regardless
    of how many papers discuss it; `supporting_paper_ids`/
    `contradicting_paper_ids` track which ResearchPaper rows (by id)
    agree or disagree, and `confidence_score`/`confidence_grade` reflect
    the aggregate evidence across all of them, re-evaluated every time a
    new qualifying paper is merged into it.

    Deliberately no `Relationship()` back to Ingredient (unlike
    ResearchPaper.ingredient above): every consumer of this table
    (conclusion_grader.py, app/services/search.py's API response) always
    queries it directly by `ingredient_id` — see
    app/services/search.py::get_linked_ingredients' docstring for why
    this codebase generally prefers explicit queries over lazy-loaded
    relationships when building API responses.
    """

    __tablename__ = "paper_conclusions"

    id: Optional[int] = Field(default=None, primary_key=True)
    ingredient_id: int = Field(foreign_key="ingredients.id", index=True)

    claim_summary: str
    detailed_conclusion: Optional[str] = Field(default=None)
    # The specific dosage this claim pertains to, if the source paper(s)
    # specify one (e.g. "300mg") — distinct from
    # Ingredient.recommended_daily_dosage, which is general/canonical,
    # not tied to any one finding.
    dosage_mentioned: Optional[str] = Field(default=None)

    # Structured breakdown backing confidence_score/confidence_grade —
    # see app/services/conclusion_grader.py's rubric-category shape
    # (evidence_strength[_score], cross_paper_consensus[_score],
    # claim_specificity[_score], total_score, summary_notes). Same JSON
    # column pattern as ResearchPaper.rubric_evaluation above.
    rubric_evaluation: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    confidence_score: int = Field(default=0)
    confidence_grade: str

    # Duplicated out of rubric_evaluation as its own column (rather than
    # only living inside the JSON blob) specifically because it's the
    # one rubric category re-evaluated on every merge — see
    # conclusion_grader.py::process_paper_conclusions — so having it
    # directly queryable/sortable without unpacking JSON is worth the
    # small duplication, same reasoning as ResearchPaper keeping
    # `grade`/`grade_score` alongside its own `rubric_evaluation` JSON.
    cross_paper_consensus: int = Field(default=0)

    # ResearchPaper.id values, not a relationship/join table —
    # deliberately: a "supports/contradicts" edge here is a lightweight
    # tag, not a first-class row needing its own table, and every
    # consumer only ever needs the ids/counts, never a full join back to
    # ResearchPaper. Always reassigned to a new list (never mutated
    # in-place with .append()) wherever updated, so SQLAlchemy's change
    # tracking picks up the new value — see conclusion_grader.py.
    supporting_paper_ids: List[int] = Field(default_factory=list, sa_column=Column(JSON))
    contradicting_paper_ids: List[int] = Field(default_factory=list, sa_column=Column(JSON))

    # Reserved for a future "supersede/merge duplicate conclusions"
    # cleanup pass — every conclusion created today is active by default
    # and nothing currently deactivates one, but queries already filter
    # on it (see app/services/search.py::get_ingredient_conclusions) so
    # that pass can land later without an API/query-shape change.
    is_active: bool = Field(default=True)

    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    # Bumped manually wherever a merge updates this row (not an ORM
    # `onupdate` hook) — see conclusion_grader.py.
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)


class VerifiedResource(SQLModel, table=True):
    """A single official, government/regulatory reference link for one
    canonical Ingredient — Phase 7, see
    app/services/resource_fetcher.py::fetch_verified_resources_for_ingredient.
    Quality-graded against docs/resource_grading_rubric.json — Phase 8,
    see app/services/resource_grader.py.

    Unlike ResearchPaper (any source domain, relevance/quality judged
    afterward by Gemini — see Phase 3/6), every row here has already
    cleared a strict domain allow-list (`.gov`, `.europa.eu`,
    `ncbi.nlm.nih.gov`, `efsa.europa.eu` — see
    resource_fetcher.py::_is_verified_domain) *before* it's ever
    persisted — a generic blog post, unverified news site, or
    user-edited page (e.g. a wiki) can never end up in this table, so
    nothing downstream needs to re-check `domain` before displaying it.
    `grade`/`score`/`reasoning_summary` (Phase 8) are a *separate*,
    independent quality signal on top of that domain gate — a resource
    can be from an unquestionably official domain and still score poorly
    on the rubric (e.g. thin content, no citations, outdated), so these
    columns are never used to decide whether a row exists, only how it's
    badged once displayed. Deduplicated per-ingredient by `url`, same
    convention as ResearchPaper's `source_url` dedup — see
    resource_fetcher.py.

    This is a brand-new table (not columns added to an existing one), so
    unlike ResearchPaper's `keywords`/`grade`/`status` columns it needed
    no additive `ALTER TABLE` migration in app/db.py when it was first
    introduced (Phase 7) — `SQLModel.metadata.create_all()` alone was
    sufficient. The Phase 8 `grade`/`score`/`reasoning_summary` columns
    below, however, DO need one — `verified_resources` now already
    exists in deployed (Phase 7) databases, so these three are additive
    columns on an existing table, same story as ResearchPaper's own
    `grade`/`grade_score` — see app/db.py::_migrate_verified_resource_columns.
    """

    __tablename__ = "verified_resources"

    id: Optional[int] = Field(default=None, primary_key=True)
    ingredient_id: int = Field(foreign_key="ingredients.id", index=True)

    # e.g. "MedlinePlus Vitamin C Health Topic" — either lifted directly
    # from the source API's own title/name field, or (for sources that
    # don't reliably expose one) synthesized from the source's display
    # name + ingredient name — see resource_fetcher.py's per-source
    # parsers for exactly which.
    title: str
    # e.g. "National Institutes of Health" — the human-readable agency/
    # organization name, distinct from `domain` (the machine hostname)
    # below. Falls back to the configured API's own display name (see
    # docs/verified_resource_apis.json's `name` field) when the source's
    # response doesn't carry its own publisher/organization field.
    publisher: str
    url: str
    # The verified hostname the link resolved to (e.g.
    # "medlineplus.gov", "pubchem.ncbi.nlm.nih.gov") — always one that
    # already passed `_is_verified_domain` at fetch time (see class
    # docstring). Kept as its own column (rather than re-parsed from
    # `url` on every read) so the frontend's authority badge
    # (`src/components/VerifiedResourcesList.tsx`) can derive "NIH" /
    # "USDA" / "EFSA" without needing a URL-parsing dependency.
    domain: str
    # Optional 1-2 sentence overview snippet, where the source API
    # provides one (e.g. PubChem's compound `Description` field). None
    # for sources/entries that don't expose a summary — the frontend
    # simply omits the snippet for those rather than showing an empty
    # or placeholder string.
    summary: Optional[str] = Field(default=None)
    # --- Phase 21: deterministic conclusion parsing
    # (app/services/resource_parser.py::parse_resource_conclusions) ---
    # The `id` of whichever docs/verified_resource_apis.json entry
    # produced this row — e.g. "pubchem_pug_rest", "usda_fooddata" —
    # recorded at fetch time in app/services/resource_fetcher.py.
    # `domain` alone isn't a reliable enough key for this: it identifies
    # the *hostname a link resolved to*, not which of the six configured
    # sources actually fetched it (in principle two different config
    # entries could resolve to overlapping domains). `api_id` is the
    # authoritative dispatch key resource_parser.py's
    # parse_resource_conclusions() switches on to pick the right
    # provider-specific parsing rules, so it's stored directly rather
    # than re-derived from `domain` on every read. Nullable — `None` for
    # any row persisted before this column existed (Phase 7-21 rows);
    # such a row simply never got api_id-driven parsing and keeps
    # whatever extracted_conclusions/extraction_failure_reason it already
    # had (or lacks) from an earlier phase.
    api_id: Optional[str] = Field(default=None, sa_column=Column(String))

    # --- Phase 8: automated resource grading
    # (app/services/resource_grader.py) ---
    # Set together, once, right after a resource is first persisted by
    # fetch_verified_resources_for_ingredient() — never re-graded on a
    # subsequent re-grade run (same "a paper's evaluation doesn't change
    # once assigned" convention as ResearchPaper.grade). All three are
    # nullable — None until resource_grader.py successfully grades this
    # row (best-effort: a Gemini failure for one resource is logged and
    # skipped rather than failing the whole fetch, leaving that one row
    # permanently ungraded rather than retried — see resource_fetcher.py),
    # so the frontend must handle a null `grade` (no badge rendered) as a
    # normal, expected state, not an error — same convention as
    # ResearchPaper.grade.
    #
    # `grade` is always one of "A"/"B"/"C"/"D"/"E", derived server-side
    # from `score` via docs/resource_grading_rubric.json's `grade_bands`
    # (not trusted directly from Gemini's own output) — see
    # resource_grader.py::grade_resource for why, same "derive the
    # letter ourselves" philosophy as paper_grader.py.
    grade: Optional[str] = Field(default=None)
    # 0-100, strictly clamped — see resource_grader.py::grade_resource's
    # "Score Calculation Guard".
    score: Optional[int] = Field(default=None)
    # A concise rationale for the overall evaluation, straight from
    # Gemini's response (nothing to clamp/re-derive, unlike `grade`/
    # `score`). Deliberately just this one summary column rather than a
    # full per-category JSON breakdown (contrast with ResearchPaper.
    # rubric_evaluation) — the task spec for this feature only calls for
    # `grade`/`score`/`reasoning_summary` on this table; the four
    # category_scores Gemini also returns (see resource_grader.py) are
    # used to compute `score` but aren't separately persisted.
    reasoning_summary: Optional[str] = Field(default=None)

    # --- Phase 17: Two-Stage Extraction Pipeline
    # (app/services/resource_extractor.py) ---
    # **Deprecated as of Phase 21 — see that phase's note on
    # `extracted_conclusions` below.** `resource_extractor.py`'s Gemini
    # call (the only thing that ever populated this column) was retired
    # in favor of `app/services/resource_parser.py`'s deterministic
    # parser, which does not produce this four-field shape at all (only
    # `extracted_conclusions`/`extraction_failure_reason`). This column
    # is kept, unmigrated, purely for backward compatibility with rows
    # persisted before Phase 21 — `conclusion_grader.py`'s Stage 2
    # synthesis (`_format_resources_for_prompt`) still reads it when
    # present and still has its own pre-existing fallback to a resource's
    # raw `summary` text when it's `None`, so an old row with real
    # `extracted_data` keeps benefiting from it, while every resource
    # fetched after Phase 21 simply has this stay `None` forever (falling
    # back to raw `summary` text in Stage 2, same as any other
    # never-extracted row always could). Original Phase 17 documentation
    # preserved below for historical context on why this shape existed.
    #
    # Why this existed: feeding Gemini one single prompt mixing dense,
    # information-rich paper abstracts alongside short, thin web-resource
    # snippets caused it to consistently favor the papers and effectively
    # ignore the resources (a "lost-in-the-middle" context-dilution
    # effect) — see app/services/conclusion_grader.py's module docstring.
    # Extracting each resource's claims independently, *before* the final
    # synthesis call, meant Stage 2 received a compact, uniformly-
    # structured, already-distilled block per resource — comparable in
    # information density to a paper's own extracted PaperConclusion
    # rows, so the two source types competed on equal footing rather than
    # the resource's raw, verbose (or, just as often, sparse) snippet
    # text competing directly against a paper's dense abstract.
    #
    # Stored as a native JSON column (SQLAlchemy's `JSON` type, same
    # convention as ResearchPaper.rubric_evaluation above) with exactly
    # four keys — `official_stance` (str | None), `recommended_dose`
    # (str | None), `upper_limit_warning` (str | None), `key_takeaways`
    # (list[str]).
    extracted_data: Optional[dict] = Field(default=None, sa_column=Column(JSON))

    # --- Phase 19: extracted conclusions; Phase 21: now populated
    # deterministically, not by Gemini
    # (app/services/resource_parser.py::parse_resource_conclusions) ---
    # 2-4 short, factual conclusions about the ingredient, extracted from
    # this resource's provider's raw API response via fast, rule-based
    # JSON-key lookups and regex/keyword matching — see
    # resource_parser.py's module docstring for the full per-provider
    # rule set (dispatched on `api_id` above). Computed once per source
    # per ingredient fetch, in app/services/resource_fetcher.py::
    # fetch_verified_resources_for_ingredient (immediately after that
    # source's raw payload is fetched, since every resource that same
    # source contributes this call shares one underlying raw response —
    # see that function's own docstring), NOT by a separate later
    # pipeline pass — there is no more "Stage 1" extraction step in
    # app/services/paper_analysis_pipeline.py as of Phase 21 (see that
    # module's docstring for why the Gemini-based version documented
    # under `extracted_data` above was retired: rate limits, latency, and
    # a class of failures this rule-based replacement can't have at all —
    # hallucinated output). Rendered under an "Extracted Conclusions"
    # heading in the frontend's resource info modal
    # (`src/components/VerifiedResourcesList.tsx`). Always set (never
    # left `None`) for any resource fetched after Phase 21 — either a
    # real, non-empty list, or an empty list paired with a reason in
    # `extraction_failure_reason` below. Stays `None` only for resources
    # persisted before Phase 21 that were never backfilled (the old
    # Gemini-based Stage 1 backfill loop that used to do this on a later
    # re-grade request no longer exists — see
    # paper_analysis_pipeline.py's module docstring).
    extracted_conclusions: Optional[List[str]] = Field(default=None, sa_column=Column(JSON))

    # --- Phase 20: extraction failure reason; Phase 21: now set by the
    # deterministic parser, not Gemini
    # (app/services/resource_parser.py::parse_resource_conclusions) ---
    # A short, human-readable explanation for why `extracted_conclusions`
    # came back empty, set whenever parse_resource_conclusions() returns
    # an empty list — e.g. "No structured nutrient values, RDA limits, or
    # safety keywords were found in the official payload.", or a
    # `"Parser error processing payload: ..."` message if the raw
    # response didn't match the shape that provider's parsing rules
    # expect. Distinct from `extracted_conclusions` being merely absent
    # because this resource predates Phase 21 and was never backfilled —
    # this column stays `None` in that case too, same as
    # `extracted_conclusions` itself, but gets a real string value
    # specifically when parsing was attempted and came back empty, so the
    # frontend's info modal (see
    # src/components/VerifiedResourcesList.tsx's "Extracted Conclusions"
    # section) can show an honest, specific reason instead of a generic
    # "no conclusions yet" message. Plain `String` column (not
    # `JSON`, unlike its siblings above) since it only ever holds one
    # short sentence, never a structured/list value.
    extraction_failure_reason: Optional[str] = Field(default=None, sa_column=Column(String))

    # --- Phase 22: claim alignment / cross-referencing
    # (app/services/resource_aligner.py::align_resource_conclusions_for_ingredient) ---
    # One entry per string in `extracted_conclusions` above, classifying
    # how that specific conclusion relates to this ingredient's existing
    # paper evidence (`PaperConclusion.claim_summary` rows) — computed
    # once per ingredient re-grade, AFTER paper conclusions and the
    # Stage 2 ingredient summary have both been synthesized (see
    # app/services/paper_analysis_pipeline.py's docstring for exactly
    # where in the run this happens), by a single Gemini call per
    # ingredient covering every resource's conclusions at once (batched,
    # not one call per resource — see resource_aligner.py's module
    # docstring for why).
    #
    # Stored as a native JSON array of plain dicts (not a stricter
    # sub-model — same "loose dict, not a strict schema" convention as
    # PaperConclusion.rubric_evaluation elsewhere in this module), each
    # shaped:
    #   {
    #     "text": str,              # the conclusion itself (verbatim
    #                                # from extracted_conclusions — never
    #                                # regenerated/paraphrased by Gemini)
    #     "alignment": str,         # "AGREES" | "CONTRADICTS" | "DISTINCT_NEW"
    #     "target_claim": str|None, # the specific existing paper claim
    #                                # this agrees/contradicts with; None
    #                                # for DISTINCT_NEW
    #     "notes": str|None,        # 1 short sentence explaining the
    #                                # classification
    #   }
    #
    # `None` until alignment has run at least once for this resource
    # (same "None = not attempted yet" convention as `extracted_data`/
    # `extracted_conclusions` above) — the frontend's info modal treats a
    # missing/unclassified conclusion as an honest "not yet classified"
    # state, not an error. An empty list `[]` is a real, valid result
    # (this resource had no `extracted_conclusions` to classify in the
    # first place). See resource_aligner.py's module docstring for the
    # fallback behavior when the classification Gemini call itself fails
    # (every conclusion defaults to DISTINCT_NEW with an explanatory
    # note, never silently dropped and never guessed into AGREES/
    # CONTRADICTS without real evidence).
    aligned_conclusions: Optional[List[dict]] = Field(default=None, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=_utcnow, nullable=False)

    # Added for parity with ResearchPaper.ingredient above (same
    # back_populates pattern, matched by Ingredient.verified_resources in
    # app/models/supplement.py) — this table previously had no
    # Relationship() back to Ingredient at all, unlike ResearchPaper.
    # Every current caller (conclusion_grader.py::
    # synthesize_ingredient_summary, search.py::get_ingredient_resources)
    # queries VerifiedResource directly by ingredient_id rather than via
    # this relationship — see PaperConclusion's own docstring above for
    # why this codebase generally prefers that — so this doesn't change
    # any existing code path, it only makes `ingredient.verified_resources`
    # a working, real relationship for any future/defensive use.
    ingredient: Optional[Ingredient] = Relationship(back_populates="verified_resources")
