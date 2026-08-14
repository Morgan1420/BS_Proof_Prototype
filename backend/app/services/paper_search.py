"""Scientific paper search across Europe PMC, PubMed, Semantic Scholar,
and OpenAlex, for the Phase 2 ingredient-grading pipeline.

`docs/paperApis.json` (repo root, sibling of `backend/`) configures the
four keyless REST APIs this module queries — each entry gives an `id`
(mapped below to this module's parser for that source), `domain`,
`endpoint`, `query_param`, and any static `extra_params` needed beyond
the search keyword itself. This supersedes the earlier
`docs/paperWebsites.json` (a ~50-site curated reference list with only a
handful of real APIs mixed in, used purely as an allow-list) — every
entry in `paperApis.json` is a genuine, directly-queryable REST endpoint,
and `enabled: false` (or removing an entry) turns off querying that
source, same as before.

All four are free/open and require no API key:
- **Europe PMC** (`europepmc.org`) — single JSON REST call.
- **PubMed** (`pubmed.ncbi.nlm.nih.gov`) — NCBI E-utilities; the
  configured `esearch` endpoint only returns matching PMIDs, so a second,
  internal `efetch` call (XML) fetches the actual title/abstract/authors/
  date per batch of PMIDs.
- **Semantic Scholar** (`semanticscholar.org`) — Graph API, public tier
  (shared rate limit).
- **OpenAlex** (`openalex.org`) — Works API; abstracts come back as an
  inverted index rather than plain text and are reconstructed here.

Every (keyword, source) query runs concurrently via `httpx.AsyncClient` +
`asyncio.gather`, each with its own short timeout — with up to 5 keywords
across 4 sources, running these sequentially could take a couple of
minutes in the worst case; concurrently, the whole batch takes roughly as
long as the single slowest call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional
from xml.etree import ElementTree

import httpx
from sqlmodel import Session, select

from app.models.research import ResearchPaper, parse_keywords, serialize_keywords
from app.services.paper_grader import PaperGradingError, grade_paper

logger = logging.getLogger(__name__)

# backend/app/services/paper_search.py -> parents[2] == backend/ ->
# parents[3] == repo root (same absolute-path-resolution reasoning as
# app/core/config.py's .env lookup and app/db.py's database path — don't
# rely on the process's current working directory).
_REPO_ROOT = Path(__file__).resolve().parents[3]
PAPER_APIS_PATH = _REPO_ROOT / "docs" / "paperApis.json"

# How many results to request per (keyword, source) pair. Kept modest —
# with up to 5 keywords x 4 sources, this already means up to 20 HTTP
# calls per grade request; a higher per-call limit would multiply the
# amount of (mostly-to-be-deduplicated) data further without much benefit
# for a debug-stage feature that only surfaces a raw count today.
DEFAULT_MAX_RESULTS_PER_SOURCE = 3

# Per the task spec: keep individual external calls short so a slow API
# never blocks the request for long — a single flaky source is skipped
# (see _safe_query_async) rather than waited on.
_HTTP_TIMEOUT_SECONDS = 5.0

# Not part of paperApis.json — this is a second, internal call PubMed's
# parser makes after esearch (which only returns PMIDs), not a
# independently-configurable "source" of its own. See docs/paperApis.json's
# "pubmed" entry description.
PUBMED_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


@dataclass
class PaperRecord:
    """One paper result from a source query, before it's turned into a
    ResearchPaper row (which needs an `ingredient_id` this layer doesn't
    know about)."""

    title: str
    abstract: Optional[str]
    authors: Optional[str]
    publication_date: Optional[str]
    source_url: str
    source_domain: str
    # The single Gemini-generated search keyword (see
    # app/services/research_keywords.py) that produced this specific
    # (keyword, source) query result — every parser function below sets
    # this to the same `keyword` argument it queried the source with.
    # search_papers_for_ingredient() accumulates these per unique paper
    # into ResearchPaper.keywords, since the same paper commonly turns up
    # under more than one keyword/source combination.
    keyword: str
    # The publishing journal/venue name, where the source API provides
    # one (Europe PMC's journalInfo, PubMed's Journal/Title, Semantic
    # Scholar's `venue`, OpenAlex's primary_location source) — distinct
    # from `source_domain` above, which is the *platform* that surfaced
    # the paper (e.g. "pubmed.ncbi.nlm.nih.gov"), not the journal that
    # actually published it. Fed into app/services/paper_grader.py's
    # "Journal / Publisher Rigor" evaluation; not persisted as its own
    # ResearchPaper column — the grader's `journal_reputation` text
    # captures the assessment, so storing the raw name separately isn't
    # needed for anything the app does today. None if the source doesn't
    # expose one.
    journal: Optional[str] = None


# A source parser: given an httpx.AsyncClient, that source's config entry
# (endpoint/query_param/extra_params), a keyword, and a max-results cap,
# returns the papers found. Async so every (keyword, source) pair can run
# concurrently under one asyncio.gather (see _search_all_records_async).
_QueryFn = Callable[[httpx.AsyncClient, Dict[str, object], str, int], Awaitable[List[PaperRecord]]]


def _load_paper_apis() -> List[Dict[str, object]]:
    """Reads docs/paperApis.json. Returns an empty list (rather than
    raising) if the file is missing or malformed — a config-file problem
    shouldn't take down the whole grading pipeline; it just means no
    sources get queried, which surfaces as "0 papers found" rather than a
    500.
    """
    try:
        with PAPER_APIS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read paper API config at %s: %s", PAPER_APIS_PATH, exc)
        return []

    if not isinstance(data, list):
        logger.warning(
            "Paper API config at %s did not contain a JSON array; ignoring.",
            PAPER_APIS_PATH,
        )
        return []
    return data


def _reconstruct_openalex_abstract(
    inverted_index: Optional[Dict[str, List[int]]]
) -> Optional[str]:
    """OpenAlex doesn't return abstracts as plain text — for copyright
    reasons it returns an `abstract_inverted_index` mapping each word to
    the list of positions it occupies, and expects API consumers to
    reconstruct the text themselves. Returns None if absent/malformed.
    """
    if not inverted_index:
        return None
    try:
        positions: Dict[int, str] = {}
        for word, word_positions in inverted_index.items():
            for position in word_positions:
                positions[position] = word
        if not positions:
            return None
        return " ".join(positions[i] for i in sorted(positions))
    except (TypeError, AttributeError) as exc:
        logger.warning("Could not reconstruct OpenAlex abstract: %s", exc)
        return None


async def _query_europe_pmc(
    client: httpx.AsyncClient, config: Dict[str, object], keyword: str, max_results: int
) -> List[PaperRecord]:
    """Europe PMC REST search API — a single JSON call."""
    params = {
        **config.get("extra_params", {}),
        config["query_param"]: keyword,
        "pageSize": max_results,
    }
    response = await client.get(str(config["endpoint"]), params=params)
    response.raise_for_status()
    payload = response.json()

    records: List[PaperRecord] = []
    for result in payload.get("resultList", {}).get("result", []):
        title = result.get("title")
        if not title:
            continue

        source_id = result.get("id")
        source_code = result.get("source", "MED")
        source_url = (
            f"https://europepmc.org/article/{source_code}/{source_id}"
            if source_id
            else "https://europepmc.org/"
        )
        journal = ((result.get("journalInfo") or {}).get("journal") or {}).get("title")

        records.append(
            PaperRecord(
                title=title,
                abstract=result.get("abstractText"),
                authors=result.get("authorString"),
                publication_date=result.get("firstPublicationDate"),
                source_url=source_url,
                source_domain="europepmc.org",
                keyword=keyword,
                journal=journal,
            )
        )
    return records


async def _query_pubmed(
    client: httpx.AsyncClient, config: Dict[str, object], keyword: str, max_results: int
) -> List[PaperRecord]:
    """PubMed via NCBI E-utilities: the configured esearch endpoint for
    matching PMIDs, then an internal efetch call (XML, rettype=abstract)
    for title/abstract/authors/date — esearch alone doesn't include the
    abstract text (see docs/paperApis.json's "pubmed" entry).
    """
    search_params = {
        **config.get("extra_params", {}),
        config["query_param"]: keyword,
        "retmax": max_results,
    }
    search_response = await client.get(str(config["endpoint"]), params=search_params)
    search_response.raise_for_status()
    pmids: List[str] = search_response.json().get("esearchresult", {}).get("idlist", [])
    if not pmids:
        return []

    fetch_response = await client.get(
        PUBMED_EFETCH_URL,
        params={
            "db": "pubmed",
            "id": ",".join(pmids),
            "rettype": "abstract",
            "retmode": "xml",
        },
    )
    fetch_response.raise_for_status()

    root = ElementTree.fromstring(fetch_response.text)

    records: List[PaperRecord] = []
    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext(".//PMID")
        title_el = article.find(".//ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else None
        if not pmid or not title:
            continue

        abstract_parts = [
            "".join(node.itertext()).strip()
            for node in article.findall(".//AbstractText")
        ]
        abstract = " ".join(part for part in abstract_parts if part) or None

        author_names: List[str] = []
        for author_el in article.findall(".//AuthorList/Author"):
            last_name = author_el.findtext("LastName")
            initials = author_el.findtext("Initials")
            collective_name = author_el.findtext("CollectiveName")
            if last_name and initials:
                author_names.append(f"{last_name} {initials}")
            elif last_name:
                author_names.append(last_name)
            elif collective_name:
                author_names.append(collective_name)
        authors = ", ".join(author_names) or None

        publication_date = article.findtext(".//PubDate/Year") or article.findtext(
            ".//PubDate/MedlineDate"
        )
        journal = article.findtext(".//Journal/Title")

        records.append(
            PaperRecord(
                title=title,
                abstract=abstract,
                authors=authors,
                publication_date=publication_date,
                source_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                source_domain="pubmed.ncbi.nlm.nih.gov",
                keyword=keyword,
                journal=journal,
            )
        )
    return records


async def _query_semantic_scholar(
    client: httpx.AsyncClient, config: Dict[str, object], keyword: str, max_results: int
) -> List[PaperRecord]:
    """Semantic Scholar Graph API — public/unauthenticated tier, subject
    to a fairly aggressive shared rate limit (handled by
    _safe_query_async's 429 handling below)."""
    params = {
        **config.get("extra_params", {}),
        config["query_param"]: keyword,
        "limit": max_results,
    }
    response = await client.get(str(config["endpoint"]), params=params)
    response.raise_for_status()
    payload = response.json()

    records: List[PaperRecord] = []
    for paper in payload.get("data", []) or []:
        title = paper.get("title")
        if not title:
            continue

        authors = (
            ", ".join(
                author.get("name", "")
                for author in paper.get("authors", []) or []
                if author.get("name")
            )
            or None
        )
        publication_date = paper.get("publicationDate") or (
            str(paper["year"]) if paper.get("year") else None
        )

        records.append(
            PaperRecord(
                title=title,
                abstract=paper.get("abstract"),
                authors=authors,
                publication_date=publication_date,
                source_url=paper.get("url") or "https://www.semanticscholar.org/",
                source_domain="semanticscholar.org",
                keyword=keyword,
                journal=paper.get("venue") or None,
            )
        )
    return records


async def _query_openalex(
    client: httpx.AsyncClient, config: Dict[str, object], keyword: str, max_results: int
) -> List[PaperRecord]:
    """OpenAlex Works API — a single JSON call; abstracts need
    reconstructing from an inverted index (see
    _reconstruct_openalex_abstract)."""
    params = {
        **config.get("extra_params", {}),
        config["query_param"]: keyword,
        "per_page": max_results,
    }
    response = await client.get(str(config["endpoint"]), params=params)
    response.raise_for_status()
    payload = response.json()

    records: List[PaperRecord] = []
    for work in payload.get("results", []) or []:
        title = work.get("title") or work.get("display_name")
        if not title:
            continue

        authorships = work.get("authorships", []) or []
        authors = (
            ", ".join(
                authorship.get("author", {}).get("display_name", "")
                for authorship in authorships
                if authorship.get("author", {}).get("display_name")
            )
            or None
        )
        publication_date = work.get("publication_date") or (
            str(work["publication_year"]) if work.get("publication_year") else None
        )
        source_url = work.get("id") or work.get("doi") or "https://openalex.org/"
        # `primary_location.source` is OpenAlex's current shape; `host_venue`
        # is an older/deprecated one kept as a fallback in case a response
        # still uses it. Both may be present-but-null in the JSON (not just
        # absent), hence the `or {}` guards rather than a plain `.get(...,  {})`.
        primary_source = (work.get("primary_location") or {}).get("source") or {}
        host_venue = work.get("host_venue") or {}
        journal = primary_source.get("display_name") or host_venue.get("display_name")

        records.append(
            PaperRecord(
                title=title,
                abstract=_reconstruct_openalex_abstract(work.get("abstract_inverted_index")),
                authors=authors,
                publication_date=publication_date,
                source_url=source_url,
                source_domain="openalex.org",
                keyword=keyword,
                journal=journal,
            )
        )
    return records


# Maps each recognized `id` in docs/paperApis.json to (display label,
# parser function).
_SOURCE_QUERY_FUNCTIONS: Dict[str, "tuple[str, _QueryFn]"] = {
    "europe_pmc": ("Europe PMC", _query_europe_pmc),
    "pubmed": ("PubMed", _query_pubmed),
    "semantic_scholar": ("Semantic Scholar", _query_semantic_scholar),
    "openalex": ("OpenAlex", _query_openalex),
}


def _enabled_api_configs() -> List[Dict[str, object]]:
    """Every entry in docs/paperApis.json with `enabled` not explicitly
    `false` and a recognized `id` this module has a parser for."""
    configs: List[Dict[str, object]] = []
    for entry in _load_paper_apis():
        api_id = entry.get("id")
        if entry.get("enabled") is False:
            continue
        if api_id not in _SOURCE_QUERY_FUNCTIONS:
            logger.warning("Skipping unrecognized paper API config id %r.", api_id)
            continue
        configs.append(entry)
    return configs


async def _safe_query_async(
    query_fn: _QueryFn,
    client: httpx.AsyncClient,
    config: Dict[str, object],
    source_label: str,
    keyword: str,
    max_results: int,
) -> List[PaperRecord]:
    """Runs one source query, converting any failure (timeout, rate
    limit, network error, malformed response) into a logged warning and
    an empty result instead of letting it propagate — one flaky/rate-
    limited/slow source should never fail the whole grading request when
    the others might still return useful results.
    """
    try:
        return await query_fn(client, config, keyword, max_results)
    except httpx.TimeoutException:
        logger.warning(
            "%s timed out (>%ss) for keyword %r — skipping.",
            source_label,
            _HTTP_TIMEOUT_SECONDS,
            keyword,
        )
        return []
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 429:
            logger.warning(
                "%s rate-limited the request for keyword %r — skipping this source "
                "for this keyword.",
                source_label,
                keyword,
            )
        else:
            logger.warning(
                "%s returned HTTP %s for keyword %r: %s",
                source_label,
                status_code,
                keyword,
                exc,
            )
        return []
    except httpx.RequestError as exc:
        logger.warning(
            "Network error querying %s for keyword %r: %s", source_label, keyword, exc
        )
        return []
    except (ElementTree.ParseError, ValueError, KeyError) as exc:
        # Malformed/unexpected response shape from a source — same
        # "don't let one source's hiccup kill the request" reasoning.
        logger.warning(
            "Could not parse %s response for keyword %r: %s", source_label, keyword, exc
        )
        return []
    except Exception as exc:  # noqa: BLE001 - final safety net, see docstring
        logger.warning(
            "Unexpected error querying %s for keyword %r: %s", source_label, keyword, exc
        )
        return []


async def _search_all_records_async(
    keywords: List[str], max_results_per_source: int
) -> List[PaperRecord]:
    """Fans out every (keyword, source) query concurrently over one
    shared httpx.AsyncClient, each individually guarded by
    _safe_query_async, and flattens the results.
    """
    configs = _enabled_api_configs()
    if not configs or not keywords:
        return []

    timeout = httpx.Timeout(_HTTP_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = [
            _safe_query_async(
                _SOURCE_QUERY_FUNCTIONS[config["id"]][1],
                client,
                config,
                _SOURCE_QUERY_FUNCTIONS[config["id"]][0],
                keyword,
                max_results_per_source,
            )
            for keyword in keywords
            for config in configs
        ]
        results_per_task = await asyncio.gather(*tasks)

    all_records: List[PaperRecord] = []
    for records in results_per_task:
        all_records.extend(records)
    return all_records


def search_papers_for_ingredient(
    session: Session,
    ingredient_id: int,
    keywords: List[str],
    max_results_per_source: int = DEFAULT_MAX_RESULTS_PER_SOURCE,
) -> List[ResearchPaper]:
    """Queries every available source (see module docstring) for each
    keyword concurrently, deduplicates against both this batch and what's
    already stored for `ingredient_id`, and persists new ResearchPaper
    rows.

    Stays a synchronous function — internally it runs the actual network
    fan-out via `asyncio.run(_search_all_records_async(...))` over
    `httpx.AsyncClient`, so its call sites
    (app/services/grading.py, and ultimately the FastAPI route via
    `run_in_threadpool`) don't need to change. `asyncio.run()` is safe to
    call here specifically because this always executes inside a worker
    thread (via `run_in_threadpool`), never on FastAPI's own event loop
    thread — each thread gets its own event loop, so there's no
    "asyncio.run() cannot be called from a running event loop" conflict.

    Deliberately `session.flush()`s rather than `session.commit()`s — the
    caller (app/services/grading.py) commits once, together with the
    ingredient's `is_graded`/`grade_badge_text` update, so a grade
    request either fully succeeds or fully rolls back rather than leaving
    papers persisted with the ingredient still marked ungraded.

    Args:
        session: An open SQLModel session.
        ingredient_id: The canonical Ingredient this search is for.
        keywords: Search queries to run against each source (typically
            from app/services/research_keywords.py).
        max_results_per_source: Cap per (keyword, source) pair.

    Returns:
        The newly-created ResearchPaper rows (already added + flushed,
        so they have ids) — does NOT include rows that already existed
        for this ingredient before this call.
    """
    all_records = asyncio.run(_search_all_records_async(keywords, max_results_per_source))

    if not all_records:
        return []

    existing = session.exec(
        select(ResearchPaper).where(ResearchPaper.ingredient_id == ingredient_id)
    ).all()
    existing_by_url = {paper.source_url: paper for paper in existing}
    existing_by_title = {paper.title.strip().lower(): paper for paper in existing}

    # Groups this batch's *new* (not-already-stored) records by identity
    # (source_url, falling back to normalized title — same dedup key the
    # code used before keyword tracking existed), accumulating every
    # keyword that turned each one up. A paper commonly surfaces under
    # several keyword/source combinations in one grade request; this
    # ensures it becomes exactly one ResearchPaper row with a merged,
    # deduplicated keyword list, not one row per (keyword, source) hit.
    batch_order: List[str] = []
    batch_records: Dict[str, PaperRecord] = {}
    batch_keywords: Dict[str, List[str]] = {}

    for record in all_records:
        normalized_title = record.title.strip().lower()
        existing_paper = existing_by_url.get(record.source_url) or existing_by_title.get(
            normalized_title
        )
        if existing_paper is not None:
            # Already persisted from an earlier grade request for this
            # ingredient — not a new row, but if this run found it via a
            # keyword that wasn't recorded before (e.g. a re-grade with
            # different Gemini-generated terms), merge that keyword onto
            # the existing row instead of silently dropping it.
            _merge_keyword_onto(existing_paper, record.keyword)
            continue

        key = record.source_url or normalized_title
        if key not in batch_records:
            batch_records[key] = record
            batch_keywords[key] = []
            batch_order.append(key)
        if record.keyword and record.keyword not in batch_keywords[key]:
            batch_keywords[key].append(record.keyword)

    new_papers: List[ResearchPaper] = []
    for key in batch_order:
        record = batch_records[key]
        paper = ResearchPaper(
            ingredient_id=ingredient_id,
            title=record.title,
            abstract=record.abstract,
            authors=record.authors,
            publication_date=record.publication_date,
            source_url=record.source_url,
            source_domain=record.source_domain,
            keywords=serialize_keywords(batch_keywords[key]),
        )
        _apply_grade(paper, record)
        session.add(paper)
        new_papers.append(paper)

    if new_papers:
        session.flush()  # assigns ids without committing — see docstring

    return new_papers


def _apply_grade(paper: ResearchPaper, record: PaperRecord) -> None:
    """Grades `paper` via app/services/paper_grader.py (Phase 3) and sets
    its `grade`/`grade_score`/`rubric_evaluation` fields — called exactly
    once, right when a paper is first persisted, never on subsequent
    re-grade runs (an already-graded paper's evaluation doesn't change).

    Makes one blocking Gemini call per new paper, so grading a batch with
    many newly-found papers adds proportionally to this request's total
    latency (on top of the paper-search calls already made) — acceptable
    for this debug-stage feature's request volume, but a candidate for a
    future concurrent/batched pass if paper counts grow.

    Resilient by design: a single paper's grading failure (Gemini error,
    malformed response, missing/unreadable rubric file) is logged and
    left as an ungraded row (`grade`/`grade_score`/`rubric_evaluation`
    all stay `None`) rather than failing the whole ingestion batch — same
    "one flaky piece shouldn't sink the request" philosophy as
    _safe_query_async's per-source handling above.
    """
    try:
        result = grade_paper(
            {
                "title": record.title,
                "abstract": record.abstract,
                "authors": record.authors,
                "journal": record.journal,
                "publication_date": record.publication_date,
            }
        )
    except PaperGradingError as exc:
        logger.warning("Could not grade paper %r: %s", record.title, exc)
        return

    paper.grade = result["grade"]
    paper.grade_score = result["grade_score"]
    paper.rubric_evaluation = dict(result["rubric_evaluation"])


def _merge_keyword_onto(paper: ResearchPaper, keyword: Optional[str]) -> None:
    """Adds `keyword` to `paper.keywords` (parsed, deduplicated,
    re-serialized) if it isn't already recorded. No-op if `keyword` is
    falsy or already present. Mutating an already-persistent ResearchPaper
    instance here is enough for SQLAlchemy to pick it up as dirty and
    include it in the caller's eventual commit — no explicit `session.add`
    needed for an object the session already tracks.
    """
    if not keyword:
        return
    current = parse_keywords(paper.keywords)
    if keyword in current:
        return
    current.append(keyword)
    paper.keywords = serialize_keywords(current)
