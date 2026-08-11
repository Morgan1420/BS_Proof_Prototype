"""Literature search for single-ingredient grading -- real NCBI PubMed E-utilities calls.

Used only by ``app.services.grading_service.IngredientGradingService`` as
Step 2/3 of ``POST /api/ingredients/{ingredient_id}/grade``. Two plain
HTTP calls against NCBI's public E-utilities API (no SDK, no API key
required, though ``Settings.pubmed_api_key`` is sent if configured):

1. ``esearch.fcgi`` -- turn a query built from the ingredient's name/form
   into a list of matching PMIDs.
2. ``efetch.fcgi`` (``rettype=abstract&retmode=text``) -- fetch each
   PMID's title + abstract as plain text, which is simpler and more
   robust to parse here than the full XML record.

``search_literature`` tries up to two queries per call: a primary query
(name + form + "supplementation") and, only if that returns zero PMIDs
and a form was given, a broader fallback query (name only). PubMed's
indexing of specific proprietary extract names (e.g. "KSM-66") is
inconsistent, so a name-only query can find studies the form-specific
one misses -- the fallback is skipped entirely if the primary query
already found something, to avoid a second round-trip for no reason.
Every query actually sent (one or two) is returned in
``LiteratureSearchResult.queries_used``, so the caller can log/report
exactly what was searched.

Deliberately best-effort: PubMed's plain-text abstract dump has no
formal schema, so parsing it (``_parse_abstract_text``) is a pragmatic
"split on numbered entries, grab the PMID line" approach, not a full
citation parser. If NCBI is slow, unreachable, or returns something this
can't parse, ``search_literature`` raises ``PubMedSearchError`` (which
carries whatever queries were attempted before the failure, in
``queries_attempted``) -- it's ``IngredientGradingService``'s job (not
this module's) to decide that a literature-search failure shouldn't
block grading and should degrade to "zero studies found" instead (see
that module's docstring). This module itself never silently swallows a
failure into an empty list, so that decision stays visible to, and owned
by, the caller.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

NCBI_EUTILS_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# How much of one study's raw abstract text to keep -- bounds the size of
# the prompt IngredientGradingService eventually builds from these.
MAX_ABSTRACT_CHARS = 1500

_ENTRY_SPLIT_PATTERN = re.compile(r"\n(?=\d+\.\s)")
_PMID_PATTERN = re.compile(r"PMID:\s*(\d+)")


class PubMedSearchError(Exception):
    """Raised when a PubMed E-utilities call fails outright (network, timeout, non-2xx, unparseable JSON).

    Never raised for "zero results found" -- that's a normal, successful
    outcome (see ``search_literature``'s empty-results return), not an
    error.
    """

    def __init__(self, message: str, queries_attempted: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.queries_attempted: List[str] = list(queries_attempted or [])


@dataclass(frozen=True)
class LiteratureStudy:
    """One PubMed study's title + best-effort abstract text, for one grading pass."""

    pmid: str
    title: Optional[str]
    abstract: Optional[str]


@dataclass(frozen=True)
class LiteratureSearchResult:
    """Everything one ``search_literature`` call found, plus exactly what was searched.

    ``papers_found`` is the number of PMIDs the (successful) esearch call
    returned -- ``len(studies)`` can be slightly lower if an entry
    couldn't be parsed out of efetch's plain-text response (see
    ``_parse_abstract_text``), which is why both numbers are kept
    separately for ``IngredientGradingService``'s grading-stats output.
    """

    studies: List[LiteratureStudy] = field(default_factory=list)
    queries_used: List[str] = field(default_factory=list)
    papers_found: int = 0


def build_query(name: str, form: Optional[str]) -> str:
    """Build the primary search term from the ingredient's exact name and form.

    Shared with ``app.services.literature_search`` so every provider
    (PubMed, Europe PMC, OpenAlex, Semantic Scholar) searches on
    consistent terms. Dosage is deliberately NOT encoded into the search
    query -- free-text search on these APIs doesn't reliably filter on
    numeric dose amounts, and a query like "600 mg" mostly just
    narrows/misses results rather than finding dose-specific studies.
    Dosage instead contributes to ``literature_search``'s keyword-match
    ranking score (does a candidate paper's own title mention the dose?)
    and is given directly to Gemini as prompt context for
    ``dosage_appropriateness`` -- see that module and
    ``IngredientGradingService``.
    """
    terms = [name.strip()]
    if form and form.strip():
        terms.append(form.strip())
    terms.append("supplementation")
    return " ".join(terms)


def _build_fallback_query(name: str) -> str:
    """A broader, name-only query -- see module docstring for when this is used."""
    return f"{name.strip()} supplementation clinical trial"


def _parse_abstract_text(raw_text: str) -> List[LiteratureStudy]:
    """Best-effort parse of efetch's plain-text `rettype=abstract` output.

    NCBI's plain-text format numbers each entry ("1. <Journal citation>.")
    with no other strict structure, but consistently follows that first
    citation line with the article title as its own line, then authors,
    then the abstract body, then a trailing "PMID: <digits>" line -- so
    this splits on the entry numbering and treats the *second*
    non-empty line of each chunk as the title. The full chunk, truncated
    to MAX_ABSTRACT_CHARS, is kept as the "abstract" -- in practice this
    includes the citation/author lines around the actual abstract body
    too, which is fine context for Gemini's grading prompt even if it's
    not a clean abstract-only excerpt.
    """
    studies: List[LiteratureStudy] = []
    for chunk in _ENTRY_SPLIT_PATTERN.split(raw_text.strip()):
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue
        title = lines[1] if len(lines) >= 2 else re.sub(r"^\d+\.\s*", "", lines[0]).strip() or None
        pmid_match = _PMID_PATTERN.search(chunk)
        if not pmid_match:
            # Not a recognizable entry (e.g. a stray header/footer line) -- skip it
            # rather than fabricating a PMID.
            continue
        abstract = chunk[: MAX_ABSTRACT_CHARS]
        studies.append(LiteratureStudy(pmid=pmid_match.group(1), title=title, abstract=abstract))
    return studies


async def _run_esearch(client: httpx.AsyncClient, query: str, settings: Settings) -> List[str]:
    params = {"db": "pubmed", "retmode": "json", "retmax": settings.pubmed_max_studies, "term": query}
    if settings.pubmed_api_key:
        params["api_key"] = settings.pubmed_api_key

    response = await client.get(f"{NCBI_EUTILS_BASE_URL}/esearch.fcgi", params=params)
    response.raise_for_status()
    try:
        return response.json()["esearchresult"]["idlist"]
    except (KeyError, ValueError) as exc:
        raise PubMedSearchError(f"Unexpected esearch response shape: {exc}") from exc


async def _run_efetch(client: httpx.AsyncClient, pmids: List[str], settings: Settings) -> str:
    params = {"db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "text"}
    if settings.pubmed_api_key:
        params["api_key"] = settings.pubmed_api_key

    response = await client.get(f"{NCBI_EUTILS_BASE_URL}/efetch.fcgi", params=params)
    response.raise_for_status()
    return response.text


async def search_literature(
    name: str,
    form: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> LiteratureSearchResult:
    """Search PubMed for studies related to one ingredient's exact name and form.

    Tries the primary (name + form) query first; if that finds zero
    PMIDs and a form was given, tries one broader (name-only) fallback
    query before giving up -- see module docstring. At most two queries
    are ever sent per call.

    Args:
        name: Ingredient name as printed on the label, e.g. 'Ashwagandha'.
        form: Ingredient form as printed, if any, e.g. 'KSM-66 Root Extract'.
        settings: Injected ``Settings``; defaults to ``get_settings()``.
            Reads ``pubmed_api_key`` / ``pubmed_max_studies`` /
            ``pubmed_timeout_seconds``.

    Returns:
        A ``LiteratureSearchResult`` with up to ``settings.pubmed_max_studies``
        studies, most-relevant first (NCBI's own default sort), plus
        exactly which query/queries were executed. Zero studies found is
        a normal, successful outcome, not an error.

    Raises:
        PubMedSearchError: If an E-utilities call fails outright (network
            error, timeout, non-2xx response, or a response body that
            doesn't parse as expected). Carries whatever queries were
            already attempted (``.queries_attempted``) for logging. Does
            NOT raise for zero search results.
    """
    settings = settings or get_settings()
    candidate_queries = [build_query(name, form)]
    if form and form.strip():
        candidate_queries.append(_build_fallback_query(name))

    queries_used: List[str] = []
    pmids: List[str] = []

    try:
        async with httpx.AsyncClient(timeout=settings.pubmed_timeout_seconds) as client:
            for query in candidate_queries:
                queries_used.append(query)
                pmids = await _run_esearch(client, query, settings)
                if pmids:
                    break  # good enough -- no need to also try the broader fallback
                logger.info("PubMed query %r returned no results.", query)

            if not pmids:
                return LiteratureSearchResult(studies=[], queries_used=queries_used, papers_found=0)

            efetch_text = await _run_efetch(client, pmids, settings)
    except PubMedSearchError as exc:
        exc.queries_attempted = queries_used
        raise
    except httpx.HTTPError as exc:
        raise PubMedSearchError(
            f"PubMed E-utilities call failed: {type(exc).__name__}: {exc}", queries_attempted=queries_used
        ) from exc

    studies = _parse_abstract_text(efetch_text)
    logger.info(
        "PubMed search (queries=%r) found %d PMIDs, parsed %d studies.", queries_used, len(pmids), len(studies)
    )
    return LiteratureSearchResult(studies=studies, queries_used=queries_used, papers_found=len(pmids))
