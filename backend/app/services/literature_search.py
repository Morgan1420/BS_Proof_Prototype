"""Multi-source literature retrieval for single-ingredient grading.

Step 2 of ``POST /api/ingredients/{ingredient_id}/grade`` (see
``app.services.grading_service``): instead of PubMed alone, this queries
four public paper-search APIs IN PARALLEL for one ingredient's exact
name and form, merges the results, deduplicates them, ranks what's left
by a weighted quality score, and hands Gemini only the top
``Settings.literature_top_papers_limit`` (default 20) -- not a raw dump
of everything found.

Sources (each isolated -- see ``_run_providers``):

* **PubMed** (``app.services.pubmed_client``) -- NCBI E-utilities,
  biomedical/clinical trial literature. Reused as-is; this module wraps
  its ``LiteratureStudy`` results into the richer ``RawPaper`` shape
  used here.
* **Europe PMC** (``https://www.ebi.ac.uk/europepmc/webservices/rest/search``)
  -- comprehensive open-access bio/medical literature, including
  citation counts and structured publication types.
* **OpenAlex** (``https://api.openalex.org/works``) -- a broad scholarly
  catalog with citation counts; abstracts come back as an "inverted
  index" (word -> positions) rather than plain text, reconstructed here
  (see ``_reconstruct_abstract``).
* **Semantic Scholar** (``https://api.semanticscholar.org/graph/v1/paper/search``)
  -- AI-indexed academic papers with citation counts and publication
  types.

**Resilience:** the four providers run via ``asyncio.gather(...,
return_exceptions=True)`` -- one failing (network error, timeout, a
response shape that doesn't parse) never blocks or fails the others.
``aggregate_literature`` itself never raises; if every provider fails,
it returns a result with ``papers_found=0`` and every provider's error
recorded, and it's ``IngredientGradingService``'s job to decide that's a
"search failed" condition for the Gemini prompt (see that module).

**Deduplication** (``_deduplicate``) keys each paper by DOI, then PMID,
then a normalized title, in that order of preference -- keeping
whichever duplicate has the most complete metadata when the same paper
turns up from more than one source.

**Ranking** (``score_paper``) is a 0-100 weighted sum:

* Study type (up to 40 pts) -- systematic reviews / meta-analyses /
  RCTs score highest; a general review or observational study scores
  lower; anything unclassifiable scores lowest but non-zero.
* Citation count (up to 30 pts, log-scaled) -- ``min(30, log10(n+1) * 10)``.
* Recency (up to 20 pts) -- full marks within 5 years, tapering linearly
  to zero by 10 years old.
* Keyword match (up to 10 pts) -- the ingredient's form and/or dose
  appearing in the paper's own title.

Study type is inferred from whatever authoritative field a source
provides (Europe PMC's ``pubTypeList``, Semantic Scholar's
``publicationTypes``) when present, falling back to a best-effort
keyword scan of the paper's own title/abstract text otherwise (see
``_classify_study_type``) -- this is a ranking SIGNAL, never asserted as
fact, and it's never presented to Gemini as anything more certain than
what it is.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from app.core.config import Settings, get_settings
from app.services import pubmed_client

logger = logging.getLogger(__name__)

EUROPEPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

# Exact display names/order for the "API Queries Completed" log block (see
# app.services.grading_service) and for GradingStats.provider_counts keys.
PUBMED = "PubMed"
EUROPE_PMC = "Europe PMC"
OPENALEX = "OpenAlex"
SEMANTIC_SCHOLAR = "Semantic Scholar"
PROVIDER_NAMES: Tuple[str, ...] = (PUBMED, EUROPE_PMC, OPENALEX, SEMANTIC_SCHOLAR)

# Same bound pubmed_client applies to its own abstracts -- kept consistent
# across every source so no single paper can dominate the prompt.
MAX_ABSTRACT_CHARS = 1500

# Ranking weights (module docstring) -- these four always sum to a
# paper's total score, each capped at its own maximum.
STUDY_TYPE_MAX_POINTS = 40.0
CITATION_MAX_POINTS = 30.0
RECENCY_MAX_POINTS = 20.0
KEYWORD_MATCH_MAX_POINTS = 10.0

_STRONG_STUDY_TYPE_KEYWORDS = (
    "systematic review",
    "meta-analysis",
    "meta analysis",
    "randomized controlled trial",
    "randomised controlled trial",
    "randomized clinical trial",
    "clinical trial",
    "rct",
)
_MODERATE_STUDY_TYPE_KEYWORDS = ("cohort", "observational", "case-control", "case control")
_WEAK_STUDY_TYPE_KEYWORDS = ("review",)

_DOI_PREFIX_PATTERN = re.compile(r"^(https?://)?(dx\.)?doi\.org/", re.IGNORECASE)
_NON_ALNUM_PATTERN = re.compile(r"[^a-z0-9\s]")
_WHITESPACE_PATTERN = re.compile(r"\s+")


class LiteratureProviderError(Exception):
    """Raised internally by one provider's HTTP call; caught per-provider by ``_run_providers``.

    Never escapes ``aggregate_literature`` -- a provider failing just
    means that provider contributes zero papers, recorded in the
    result's ``provider_errors``.
    """

    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"{source}: {message}")
        self.source = source


@dataclass(frozen=True)
class RawPaper:
    """One paper, normalized to a common shape regardless of which API returned it."""

    source: str
    title: Optional[str]
    abstract: Optional[str]
    doi: Optional[str] = None
    pmid: Optional[str] = None
    citation_count: Optional[int] = None
    publication_year: Optional[int] = None
    study_type: Optional[str] = None


@dataclass(frozen=True)
class ScoredPaper:
    """One ``RawPaper`` plus its ranking score and the per-criterion breakdown that produced it."""

    paper: RawPaper
    score: float
    breakdown: Dict[str, float]


@dataclass(frozen=True)
class AggregatedLiteratureResult:
    """Everything one ``aggregate_literature`` call produced.

    ``studies`` is already the ranked, top-N selection -- the only
    papers actually handed to Gemini. ``papers_found`` is the unique
    count across every source AFTER deduplication (before the top-N
    cut); ``papers_analyzed`` is ``len(studies)``.
    """

    studies: List[RawPaper] = field(default_factory=list)
    queries_used: List[str] = field(default_factory=list)
    provider_counts: Dict[str, int] = field(default_factory=dict)
    provider_errors: Dict[str, str] = field(default_factory=dict)
    papers_found: int = 0
    papers_analyzed: int = 0


def _safe_int(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _truncate_abstract(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return text[:MAX_ABSTRACT_CHARS]


# -- Study-type inference -----------------------------------------------------


def _classify_study_type(text: str, hint: Optional[str] = None) -> Optional[str]:
    """Best-effort study-type label used only for ranking, never asserted to Gemini as verified fact.

    Prefers an authoritative ``hint`` from the source's own metadata
    (Europe PMC's ``pubTypeList``, Semantic Scholar's
    ``publicationTypes``) when one was given; otherwise scans the
    paper's own title/abstract text for common study-type phrases.
    Returns ``None`` if nothing could be determined either way.
    """
    if hint:
        return hint
    lowered = (text or "").lower()
    matched = [
        kw
        for kw in (*_STRONG_STUDY_TYPE_KEYWORDS, *_MODERATE_STUDY_TYPE_KEYWORDS, *_WEAK_STUDY_TYPE_KEYWORDS)
        if kw in lowered
    ]
    return ", ".join(matched) if matched else None


_YEAR_PATTERN = re.compile(r"\b(19|20)\d{2}\b")


def _extract_year(text: Optional[str]) -> Optional[int]:
    """Best-effort 4-digit year extraction from PubMed's free-text citation chunk."""
    if not text:
        return None
    match = _YEAR_PATTERN.search(text)
    return int(match.group(0)) if match else None


# -- Provider: PubMed (wraps app.services.pubmed_client) ----------------------


async def _search_pubmed(name: str, form: Optional[str], settings: Settings) -> Tuple[List[RawPaper], List[str]]:
    try:
        result = await pubmed_client.search_literature(name, form, settings=settings)
    except pubmed_client.PubMedSearchError as exc:
        raise LiteratureProviderError(PUBMED, str(exc)) from exc

    papers = [
        RawPaper(
            source=PUBMED,
            title=study.title,
            abstract=study.abstract,
            doi=None,
            pmid=study.pmid,
            citation_count=None,  # not available from E-utilities without a separate call
            publication_year=_extract_year(study.abstract),
            study_type=_classify_study_type(f"{study.title or ''} {study.abstract or ''}"),
        )
        for study in result.studies
    ]
    return papers, result.queries_used


# -- Provider: Europe PMC ------------------------------------------------------


def _parse_europepmc_result(item: dict) -> RawPaper:
    pub_type_list = item.get("pubTypeList")
    pub_types = pub_type_list.get("pubType") if isinstance(pub_type_list, dict) else pub_type_list
    type_hint = ", ".join(pub_types) if isinstance(pub_types, list) and pub_types else None
    title = item.get("title")
    abstract = _truncate_abstract(item.get("abstractText"))
    return RawPaper(
        source=EUROPE_PMC,
        title=title,
        abstract=abstract,
        doi=item.get("doi"),
        pmid=item.get("pmid"),
        citation_count=_safe_int(item.get("citedByCount")),
        publication_year=_safe_int(item.get("pubYear")),
        study_type=_classify_study_type(f"{title or ''} {abstract or ''}", hint=type_hint),
    )


async def _search_europepmc(query: str, settings: Settings) -> Tuple[List[RawPaper], List[str]]:
    params = {
        "query": query,
        "format": "json",
        "pageSize": settings.literature_max_papers_per_source,
        "resultType": "core",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.pubmed_timeout_seconds) as client:
            response = await client.get(EUROPEPMC_SEARCH_URL, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise LiteratureProviderError(EUROPE_PMC, f"{type(exc).__name__}: {exc}") from exc
    except ValueError as exc:
        raise LiteratureProviderError(EUROPE_PMC, f"Unexpected response shape: {exc}") from exc

    results = ((data.get("resultList") or {}).get("result")) or []
    papers: List[RawPaper] = []
    for item in results:
        try:
            papers.append(_parse_europepmc_result(item))
        except Exception as exc:  # noqa: BLE001 - one malformed record shouldn't drop the whole batch
            logger.debug("Skipping unparseable Europe PMC result: %s", exc)
    return papers, [query]


# -- Provider: OpenAlex ---------------------------------------------------------


def _reconstruct_abstract(inverted_index: Optional[dict]) -> Optional[str]:
    """OpenAlex returns abstracts as {word: [positions]} rather than plain text -- rebuild it."""
    if not inverted_index:
        return None
    positions: Dict[int, str] = {}
    max_position = -1
    for word, occurrences in inverted_index.items():
        for position in occurrences or []:
            positions[position] = word
            max_position = max(max_position, position)
    if max_position < 0:
        return None
    text = " ".join(positions.get(i, "") for i in range(max_position + 1)).strip()
    return text or None


def _extract_pmid_from_openalex_ids(ids: dict) -> Optional[str]:
    pmid_url = (ids or {}).get("pmid")
    if not pmid_url:
        return None
    match = re.search(r"(\d+)\s*$", str(pmid_url))
    return match.group(1) if match else None


def _parse_openalex_result(item: dict) -> RawPaper:
    title = item.get("title") or item.get("display_name")
    abstract = _truncate_abstract(_reconstruct_abstract(item.get("abstract_inverted_index")))
    doi = item.get("doi")
    if doi:
        doi = _DOI_PREFIX_PATTERN.sub("", doi)
    ids = item.get("ids") or {}
    return RawPaper(
        source=OPENALEX,
        title=title,
        abstract=abstract,
        doi=doi,
        pmid=_extract_pmid_from_openalex_ids(ids),
        citation_count=_safe_int(item.get("cited_by_count")),
        publication_year=_safe_int(item.get("publication_year")),
        study_type=_classify_study_type(f"{title or ''} {abstract or ''}", hint=item.get("type")),
    )


async def _search_openalex(query: str, settings: Settings) -> Tuple[List[RawPaper], List[str]]:
    params = {"search": query, "per_page": settings.literature_max_papers_per_source}
    try:
        async with httpx.AsyncClient(timeout=settings.pubmed_timeout_seconds) as client:
            response = await client.get(OPENALEX_WORKS_URL, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise LiteratureProviderError(OPENALEX, f"{type(exc).__name__}: {exc}") from exc
    except ValueError as exc:
        raise LiteratureProviderError(OPENALEX, f"Unexpected response shape: {exc}") from exc

    results = data.get("results") or []
    papers: List[RawPaper] = []
    for item in results:
        try:
            papers.append(_parse_openalex_result(item))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipping unparseable OpenAlex result: %s", exc)
    return papers, [query]


# -- Provider: Semantic Scholar -------------------------------------------------


def _parse_semantic_scholar_result(item: dict) -> RawPaper:
    title = item.get("title")
    abstract = _truncate_abstract(item.get("abstract"))
    external_ids = item.get("externalIds") or {}
    pub_types = item.get("publicationTypes") or []
    type_hint = ", ".join(pub_types) if pub_types else None
    return RawPaper(
        source=SEMANTIC_SCHOLAR,
        title=title,
        abstract=abstract,
        doi=external_ids.get("DOI"),
        pmid=external_ids.get("PubMed"),
        citation_count=_safe_int(item.get("citationCount")),
        publication_year=_safe_int(item.get("year")),
        study_type=_classify_study_type(f"{title or ''} {abstract or ''}", hint=type_hint),
    )


async def _search_semantic_scholar(query: str, settings: Settings) -> Tuple[List[RawPaper], List[str]]:
    params = {
        "query": query,
        "limit": settings.literature_max_papers_per_source,
        "fields": "title,abstract,year,citationCount,externalIds,publicationTypes",
    }
    headers = {"x-api-key": settings.semantic_scholar_api_key} if settings.semantic_scholar_api_key else {}
    try:
        async with httpx.AsyncClient(timeout=settings.pubmed_timeout_seconds) as client:
            response = await client.get(SEMANTIC_SCHOLAR_SEARCH_URL, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise LiteratureProviderError(SEMANTIC_SCHOLAR, f"{type(exc).__name__}: {exc}") from exc
    except ValueError as exc:
        raise LiteratureProviderError(SEMANTIC_SCHOLAR, f"Unexpected response shape: {exc}") from exc

    results = data.get("data") or []
    papers: List[RawPaper] = []
    for item in results:
        try:
            papers.append(_parse_semantic_scholar_result(item))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipping unparseable Semantic Scholar result: %s", exc)
    return papers, [query]


# -- Deduplication --------------------------------------------------------------


def _normalize_doi(doi: str) -> str:
    return _DOI_PREFIX_PATTERN.sub("", doi).strip().lower()


def _normalize_title(title: str) -> str:
    lowered = _NON_ALNUM_PATTERN.sub("", title.lower())
    return _WHITESPACE_PATTERN.sub(" ", lowered).strip()


def _dedup_key(paper: RawPaper) -> Optional[str]:
    if paper.doi:
        return f"doi:{_normalize_doi(paper.doi)}"
    if paper.pmid:
        return f"pmid:{str(paper.pmid).strip()}"
    if paper.title:
        normalized = _normalize_title(paper.title)
        if normalized:
            return f"title:{normalized}"
    return None


def _completeness(paper: RawPaper) -> int:
    """How many optional metadata fields this record actually has -- used to pick the
    richer of two records found for the same paper via different sources."""
    return sum(
        1
        for value in (paper.doi, paper.pmid, paper.abstract, paper.citation_count, paper.publication_year, paper.study_type)
        if value is not None
    )


def _deduplicate(papers: List[RawPaper]) -> List[RawPaper]:
    """Merge duplicates found across sources, keyed by DOI, then PMID, then normalized title.

    When the same paper is found more than once (e.g. via both PubMed
    and Europe PMC), the record with the most complete metadata wins --
    no field-by-field merging, just "keep the richer single record", to
    stay simple and avoid ever combining mismatched data from two
    different sources into one synthetic record.
    """
    kept: Dict[str, RawPaper] = {}
    unkeyed: List[RawPaper] = []
    for paper in papers:
        key = _dedup_key(paper)
        if key is None:
            unkeyed.append(paper)
            continue
        existing = kept.get(key)
        if existing is None or _completeness(paper) > _completeness(existing):
            kept[key] = paper
    return list(kept.values()) + unkeyed


# -- Ranking ----------------------------------------------------------------------


def _score_study_type(study_type: Optional[str]) -> float:
    if not study_type:
        return 0.0
    lowered = study_type.lower()
    if any(keyword in lowered for keyword in _STRONG_STUDY_TYPE_KEYWORDS):
        return STUDY_TYPE_MAX_POINTS
    if any(keyword in lowered for keyword in _WEAK_STUDY_TYPE_KEYWORDS):
        return STUDY_TYPE_MAX_POINTS / 2
    if any(keyword in lowered for keyword in _MODERATE_STUDY_TYPE_KEYWORDS):
        return STUDY_TYPE_MAX_POINTS * 0.375  # 15 pts -- a recognized study design, just not top-tier evidence
    return STUDY_TYPE_MAX_POINTS / 8  # some other classified type (e.g. "journal article") -- weak signal, not zero


def _score_citation_count(citation_count: Optional[int]) -> float:
    if not citation_count or citation_count <= 0:
        return 0.0
    return min(CITATION_MAX_POINTS, math.log10(citation_count + 1) * 10.0)


def _score_recency(publication_year: Optional[int], current_year: int) -> float:
    if not publication_year:
        return 0.0
    age = max(0, current_year - publication_year)
    if age <= 5:
        return RECENCY_MAX_POINTS
    if age <= 10:
        return RECENCY_MAX_POINTS * (10 - age) / 5.0
    return 0.0


def _score_keyword_match(title: Optional[str], form: Optional[str], dose_terms: List[str]) -> float:
    if not title:
        return 0.0
    lowered = title.lower()
    half = KEYWORD_MATCH_MAX_POINTS / 2
    score = 0.0
    if form and form.strip() and form.strip().lower() in lowered:
        score += half
    if any(term and term.lower() in lowered for term in dose_terms):
        score += half
    return score


def _build_dose_terms(amount: Optional[float], unit: Optional[str]) -> List[str]:
    if amount is None:
        return []
    amount_str = str(amount).rstrip("0").rstrip(".") if isinstance(amount, float) else str(amount)
    terms = [amount_str]
    if unit:
        terms.append(f"{amount_str}{unit}")
        terms.append(f"{amount_str} {unit}")
    return terms


def score_paper(paper: RawPaper, form: Optional[str], dose_terms: List[str], current_year: int) -> ScoredPaper:
    breakdown = {
        "study_type": _score_study_type(paper.study_type),
        "citation_count": _score_citation_count(paper.citation_count),
        "recency": _score_recency(paper.publication_year, current_year),
        "keyword_match": _score_keyword_match(paper.title, form, dose_terms),
    }
    return ScoredPaper(paper=paper, score=sum(breakdown.values()), breakdown=breakdown)


def select_top_papers(scored: List[ScoredPaper], limit: int) -> List[ScoredPaper]:
    """Highest score first; ties broken by citation count, then title, for deterministic ordering."""
    ranked = sorted(
        scored,
        key=lambda s: (-s.score, -(s.paper.citation_count or 0), s.paper.title or ""),
    )
    return ranked[:limit]


# -- Orchestration -----------------------------------------------------------------


async def _run_providers(
    name: str, form: Optional[str], settings: Settings
) -> Tuple[List[RawPaper], List[str], Dict[str, int], Dict[str, str]]:
    query = pubmed_client.build_query(name, form)

    provider_calls: Dict[str, Callable[[], Awaitable[Tuple[List[RawPaper], List[str]]]]] = {
        PUBMED: lambda: _search_pubmed(name, form, settings),
        EUROPE_PMC: lambda: _search_europepmc(query, settings),
        OPENALEX: lambda: _search_openalex(query, settings),
        SEMANTIC_SCHOLAR: lambda: _search_semantic_scholar(query, settings),
    }

    results = await asyncio.gather(*(call() for call in provider_calls.values()), return_exceptions=True)

    all_papers: List[RawPaper] = []
    queries_used: List[str] = []
    provider_counts: Dict[str, int] = {}
    provider_errors: Dict[str, str] = {}

    for source, result in zip(provider_calls.keys(), results):
        if isinstance(result, BaseException):
            provider_counts[source] = 0
            provider_errors[source] = str(result)
            logger.warning("Literature provider %s failed: %s", source, result)
            continue
        papers, provider_queries = result
        provider_counts[source] = len(papers)
        all_papers.extend(papers)
        for provider_query in provider_queries:
            if provider_query not in queries_used:
                queries_used.append(provider_query)

    return all_papers, queries_used, provider_counts, provider_errors


async def aggregate_literature(
    name: str,
    form: Optional[str],
    amount: Optional[float],
    unit: Optional[str],
    settings: Optional[Settings] = None,
) -> AggregatedLiteratureResult:
    """Query PubMed, Europe PMC, OpenAlex, and Semantic Scholar in parallel, then dedup + rank + select.

    Never raises -- a provider that fails outright just contributes zero
    papers (recorded in ``provider_errors``); if every provider fails,
    the result simply has ``papers_found=0`` and all four errors
    recorded, which ``IngredientGradingService`` treats as a total
    search failure for the Gemini prompt.

    Args:
        name: Ingredient name as printed on the label.
        form: Ingredient form as printed, if any.
        amount: Dose amount as printed, if any -- used only for the
            ranking step's keyword-match score (see module docstring),
            not encoded into the API queries themselves.
        unit: Dose unit as printed, if any.
        settings: Injected ``Settings``; defaults to ``get_settings()``.

    Returns:
        An ``AggregatedLiteratureResult`` whose ``studies`` are already
        the ranked top ``settings.literature_top_papers_limit`` papers.
    """
    settings = settings or get_settings()

    all_papers, queries_used, provider_counts, provider_errors = await _run_providers(name, form, settings)

    deduped = _deduplicate(all_papers)
    dose_terms = _build_dose_terms(amount, unit)
    current_year = datetime.now(timezone.utc).year
    scored = [score_paper(paper, form, dose_terms, current_year) for paper in deduped]
    selected = select_top_papers(scored, limit=settings.literature_top_papers_limit)

    return AggregatedLiteratureResult(
        studies=[s.paper for s in selected],
        queries_used=queries_used,
        provider_counts=provider_counts,
        provider_errors=provider_errors,
        papers_found=len(deduped),
        papers_analyzed=len(selected),
    )


def log_retrieval_summary(step: int, total_steps: int, top_limit: int, result: AggregatedLiteratureResult) -> None:
    """Print the exact `[GRADING STEP x/y] ...` retrieval-summary block (see app.services.grading_service).

    Deliberately plain-formatted (no ingredient_id prefix, unlike
    ``grading_service.log_grading_step``) to match the specific
    requested layout: one "API Queries Completed" line with an indented
    per-provider breakdown, then the total unique count, then the
    top-N-selected count. ``top_limit`` is the configured target (e.g.
    20, ``Settings.literature_top_papers_limit``) -- shown even if fewer
    papers were actually found/selected than that.
    """
    lines = [f"[GRADING STEP {step}/{total_steps}] API Queries Completed:"]
    for source in PROVIDER_NAMES:
        count = result.provider_counts.get(source, 0)
        suffix = " (FAILED)" if source in result.provider_errors else ""
        lines.append(f"  - {source}: {count} papers{suffix}")
    lines.append(f"[GRADING STEP {step}/{total_steps}] Total Unique Papers Found: {result.papers_found}")
    lines.append(
        f"[GRADING STEP {step}/{total_steps}] Ranked and Selected Top {top_limit} Papers for Gemini "
        f"Analysis ({result.papers_analyzed})"
    )
    text = "\n".join(lines)
    print(text, file=sys.stdout, flush=True)
    logger.info(text)
