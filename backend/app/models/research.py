"""SQLModel ORM tables for scientific research: ResearchPaper (one row
per paper linked to a canonical Ingredient — Phase 2, automated paper
scraping/grading) and PaperConclusion (one row per synthesized claim
built up across those papers — Phase 5, see
app/services/conclusion_grader.py).

ResearchPaper rows are populated by app/services/paper_search.py, which
queries PubMed, Europe PMC, Semantic Scholar, and OpenAlex per
Gemini-generated keyword (see app/services/research_keywords.py) and
persists deduplicated results here. PaperConclusion rows are populated
by app/services/conclusion_grader.py, driven by
app/services/paper_analysis_pipeline.py. See app/services/grading.py for
the full pipeline both feed into.

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

from sqlalchemy import JSON, Column
from sqlmodel import Field, Relationship, SQLModel

from app.models.supplement import Ingredient


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
