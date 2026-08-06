"""Phase 2 PubMed / NCBI Entrez Retrieval Service.

Searches PubMed for high-rigor clinical literature on a given supplement
ingredient and extracts structured paper metadata, matching the
"PubMed API Search -> Retrieve Top 10 High-Impact Papers" step in
docs/Architecture.md (Phase 2). Downstream steps (LLM Paper Evaluator,
Consensus Engine, SIFG caching) are not implemented here -- this service
is purely the retrieval boundary.

Scope notes:
    * Retrieval is restricted to Randomized Controlled Trials, (other)
      Clinical Trials, Meta-Analyses, and Systematic Reviews via NCBI's
      Publication Type ([pt]) field -- i.e. Tier 1/Tier 2 of the
      Architecture.md Risk of Bias & Quality Weighting Matrix. Journal
      quality (SCImago SJR / DOAJ) and COI-based penalties are Rigor
      Modifiers applied by a later scoring step, not by this service.
    * "High-impact" is approximated via PubMed's relevance sort; true
      impact-factor ranking is out of scope for the retrieval step.
    * Sample size (n) extraction from the abstract is a best-effort
      regex heuristic, not a guarantee -- abstracts don't always state N
      plainly, and some state it only in the full text.

Uses ``Bio.Entrez`` per NCBI_ENTREZ_EMAIL/NCBI_API_KEY in
``app.core.config.Settings``. Per CLAUDE.md's "Asynchronous Execution"
standard, the blocking Bio.Entrez calls are offloaded to a thread via
``asyncio.to_thread`` so this service is awaitable from an async
background job / task queue at the API layer (not implemented here).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import List, Optional
from urllib.error import HTTPError

from Bio import Entrez
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Phase 2 evidence hierarchy per docs/Architecture.md: restrict retrieval to
# Tier 1 (systematic reviews / meta-analyses) and Tier 2 (RCTs / clinical
# trials) publication types, via NCBI's Publication Type ([pt]) field.
PUBLICATION_TYPE_FILTERS = [
    "Randomized Controlled Trial[pt]",
    "Clinical Trial[pt]",
    "Meta-Analysis[pt]",
    "Systematic Review[pt]",
]

DEFAULT_RETMAX = 10  # "Retrieve Top 10 High-Impact Papers" per Architecture.md Phase 2 flow

# NCBI Entrez rate limits: 3 req/sec without an API key, 10 req/sec with one.
# See https://www.ncbi.nlm.nih.gov/books/NBK25497/
UNAUTHENTICATED_RATE_LIMIT_PER_SEC = 3.0
AUTHENTICATED_RATE_LIMIT_PER_SEC = 10.0

MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


class PubMedPaper(BaseModel):
    """Extracted metadata for a single retrieved PubMed record."""

    pmid: str = Field(..., description="PubMed ID.")
    title: str = Field(..., description="Article title.")
    abstract: Optional[str] = Field(default=None, description="Full abstract text, sections joined.")
    publication_date: Optional[str] = Field(
        default=None, description="Publication date as printed by PubMed (granularity varies)."
    )
    publication_types: List[str] = Field(
        default_factory=list, description="MeSH Publication Type labels, e.g. 'Randomized Controlled Trial'."
    )
    sample_size: Optional[int] = Field(
        default=None, description="Best-effort heuristic sample size (n) parsed from the abstract."
    )
    coi_statement: Optional[str] = Field(
        default=None, description="Conflict-of-interest statement text, if present in the record."
    )


class PubMedServiceError(Exception):
    """Raised for configuration/setup failures (e.g. missing Entrez email).

    Retrieval-time failures (network errors, malformed records) are
    caught inside ``search_ingredient`` and degrade to an empty result
    list rather than raising -- an ingredient with no retrievable
    literature is valid application state, not an error.
    """


class RateLimiter:
    """Async rate limiter enforcing a minimum interval between calls.

    A simple mutex-guarded "last call time" tracker rather than a token
    bucket: sufficient for throttling a single service instance's
    sequential Entrez calls to NCBI's per-second cap.
    """

    def __init__(self, max_per_second: float) -> None:
        self._min_interval = 1.0 / max_per_second
        self._lock = asyncio.Lock()
        self._last_call: Optional[float] = None

    async def wait(self) -> None:
        """Block (async) until at least ``1/max_per_second`` has elapsed since the last call."""
        async with self._lock:
            now = time.monotonic()
            if self._last_call is not None:
                elapsed = now - self._last_call
                remaining = self._min_interval - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_call = time.monotonic()


class PubMedService:
    """Searches PubMed and retrieves structured paper metadata for an ingredient."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        """Configure the service and set module-level ``Bio.Entrez`` credentials.

        Args:
            settings: Injected ``Settings``; defaults to ``get_settings()``.

        Raises:
            PubMedServiceError: If ``NCBI_ENTREZ_EMAIL`` is missing/blank.
        """
        self._settings = settings or get_settings()
        Entrez.email = self._validate_email(self._settings.ncbi_entrez_email)

        api_key = (self._settings.ncbi_api_key or "").strip() or None
        Entrez.api_key = api_key

        rate = AUTHENTICATED_RATE_LIMIT_PER_SEC if api_key else UNAUTHENTICATED_RATE_LIMIT_PER_SEC
        self._rate_limiter = RateLimiter(rate)

    @staticmethod
    def _validate_email(email: Optional[str]) -> str:
        """Ensure the Entrez contact email is present and non-empty.

        Checked explicitly rather than relying on Bio.Entrez to fail, so
        a misconfigured deployment surfaces immediately as a clear
        ``PubMedServiceError`` at construction time.
        """
        if not email or not email.strip():
            raise PubMedServiceError("NCBI_ENTREZ_EMAIL is missing or invalid.")
        return email.strip()

    # -- Public API -------------------------------------------------------

    async def search_ingredient(
        self,
        ingredient_name: str,
        retmax: int = DEFAULT_RETMAX,
    ) -> List[PubMedPaper]:
        """Search PubMed for high-rigor evidence on an ingredient and fetch metadata.

        Restricted to clinical trials, RCTs, systematic reviews, and
        meta-analyses (see ``PUBLICATION_TYPE_FILTERS``).

        Args:
            ingredient_name: Ingredient to search for, e.g. "Ashwagandha".
            retmax: Max records to retrieve (default 10, per Architecture.md
                Phase 2's "Retrieve Top 10 High-Impact Papers").

        Returns:
            A list of ``PubMedPaper``. Never raises: search failures,
            fetch failures, or individual unparseable records all degrade
            to an empty (or partial) list, logged as warnings.
        """
        try:
            pmids = await self._esearch(ingredient_name, retmax)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all fallback boundary
            logger.warning("PubMed search failed for %r: %s: %s", ingredient_name, type(exc).__name__, exc)
            return []

        if not pmids:
            return []

        try:
            articles = await self._efetch(pmids)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "PubMed fetch failed for %r (pmids=%s): %s: %s",
                ingredient_name,
                pmids,
                type(exc).__name__,
                exc,
            )
            return []

        papers: List[PubMedPaper] = []
        for article in articles:
            try:
                papers.append(self._parse_article(article))
            except Exception as exc:  # noqa: BLE001 - one bad record shouldn't drop the rest
                logger.warning(
                    "Failed to parse a PubMed article for %r: %s: %s", ingredient_name, type(exc).__name__, exc
                )
                continue
        return papers

    # -- Query construction -------------------------------------------------

    @staticmethod
    def _build_query(ingredient_name: str) -> str:
        type_filter = " OR ".join(PUBLICATION_TYPE_FILTERS)
        return f'"{ingredient_name}"[Title/Abstract] AND ({type_filter})'

    # -- Entrez calls (blocking, offloaded to a thread) ----------------------

    async def _esearch(self, ingredient_name: str, retmax: int) -> List[str]:
        query = self._build_query(ingredient_name)

        def _search() -> dict:
            handle = Entrez.esearch(db="pubmed", term=query, retmax=retmax, sort="relevance")
            try:
                return Entrez.read(handle)
            finally:
                handle.close()

        result = await self._call_with_retries(_search)
        return list(result.get("IdList", []))

    async def _efetch(self, pmids: List[str]) -> List[dict]:
        def _fetch() -> dict:
            handle = Entrez.efetch(db="pubmed", id=",".join(pmids), rettype="abstract", retmode="xml")
            try:
                return Entrez.read(handle)
            finally:
                handle.close()

        result = await self._call_with_retries(_fetch)
        return list(result.get("PubmedArticle", []))

    async def _call_with_retries(self, blocking_fn):
        """Run a blocking Entrez call in a thread, rate-limited and retried on transient errors."""
        attempt = 0
        while True:
            await self._rate_limiter.wait()
            try:
                return await asyncio.to_thread(blocking_fn)
            except HTTPError as exc:
                attempt += 1
                if attempt > MAX_RETRIES or exc.code not in RETRYABLE_HTTP_STATUS_CODES:
                    raise
                backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "NCBI Entrez request failed (HTTP %s); retrying in %.1fs (attempt %d/%d).",
                    exc.code,
                    backoff,
                    attempt,
                    MAX_RETRIES,
                )
                await asyncio.sleep(backoff)

    # -- Record parsing -------------------------------------------------------

    def _parse_article(self, article: dict) -> PubMedPaper:
        medline = article["MedlineCitation"]
        article_data = medline["Article"]

        pmid = str(medline["PMID"])
        title = str(article_data.get("ArticleTitle", "")).strip() or "Untitled"
        abstract = self._extract_abstract(article_data)

        return PubMedPaper(
            pmid=pmid,
            title=title,
            abstract=abstract,
            publication_date=self._extract_publication_date(article_data),
            publication_types=[str(pt) for pt in article_data.get("PublicationTypeList", [])],
            sample_size=self._extract_sample_size(abstract) if abstract else None,
            coi_statement=self._extract_coi_statement(medline),
        )

    @staticmethod
    def _extract_abstract(article_data: dict) -> Optional[str]:
        abstract_block = article_data.get("Abstract")
        if not abstract_block:
            return None
        texts = abstract_block.get("AbstractText", [])
        if not texts:
            return None

        parts = []
        for text in texts:
            label = None
            attributes = getattr(text, "attributes", None)
            if attributes:
                label = attributes.get("Label")
            segment = str(text)
            parts.append(f"{label}: {segment}" if label else segment)
        return "\n".join(parts).strip() or None

    @staticmethod
    def _extract_publication_date(article_data: dict) -> Optional[str]:
        journal_issue = article_data.get("Journal", {}).get("JournalIssue", {})
        pub_date = journal_issue.get("PubDate", {})
        if not pub_date:
            return None

        year = pub_date.get("Year")
        month = pub_date.get("Month")
        day = pub_date.get("Day")
        parts = [str(p) for p in (year, month, day) if p]
        if parts:
            return "-".join(parts)
        # Some records only carry a free-text date, e.g. "2022 Jan-Feb".
        medline_date = pub_date.get("MedlineDate")
        return str(medline_date) if medline_date else None

    @staticmethod
    def _extract_coi_statement(medline: dict) -> Optional[str]:
        coi = medline.get("CoiStatement")
        text = str(coi).strip() if coi else ""
        return text or None

    _SAMPLE_SIZE_PATTERN = re.compile(
        r"\bn\s*=\s*(\d{1,6})\b|\b(\d{1,6})\s+(?:participants|subjects|patients|volunteers|adults|women|men)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _extract_sample_size(cls, abstract: str) -> Optional[int]:
        """Best-effort heuristic: look for 'n = 123' or '123 participants/subjects/...'.

        Returns the first match, or ``None`` if nothing looks like a
        sample size. Downstream consumers (Phase 2 Rigor Modifiers, which
        penalize n < 30) should treat this as a hint, not ground truth.
        """
        match = cls._SAMPLE_SIZE_PATTERN.search(abstract)
        if not match:
            return None
        value = match.group(1) or match.group(2)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
