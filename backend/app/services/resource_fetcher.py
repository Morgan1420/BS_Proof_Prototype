"""Verified official-resource lookup for canonical ingredients (Phase 7,
expanded Phase 9).

`docs/verified_resource_apis.json` (repo root, sibling of `backend/`)
configures six free government/regulatory REST APIs — NIH PubChem, the
real MedlinePlus free-text health topic search, USDA FoodData Central,
NLM DailyMed, Europe PMC (EMBL-EBI), and Health Canada's Licensed
Natural Health Products Database (LNHPD) — this module queries by
`ingredient.name`, one HTTP call per enabled entry, same
config-file-driven-source pattern as app/services/paper_search.py's
`docs/paperApis.json`.

**Root cause of the "only PubChem ever returns anything" bug this phase
fixes:** every source other than PubChem was previously routed through
one schema-tolerant generic extractor (`_extract_generic_records`) that
assumes a flat `{"url": "...", "title": "..."}`-shaped dict somewhere in
the response. Two of the three previously-generic sources never actually
matched that assumption — MedlinePlus Connect's real payload nests its
link inside `"link": [{"href": ...}]` (a list, not a bare string) and
was, on top of that, pointed at the wrong MedlinePlus service entirely
(Connect takes a standardized ICD/RxNorm *code*, not a free-text
ingredient name — it could never resolve "Vitamin C" to anything);
USDA FoodData Central's `/foods/search` response has no URL field at
all, only an `fdcId` a caller has to build a link from. Both silently
produced zero records every time, leaving PubChem (which already had a
precise, shape-aware parser) as the only source that ever worked. Every
source now gets a precise, shape-aware parser (see `_query_medlineplus`/
`_query_usda`/`_query_dailymed`/`_query_europe_pmc` below) built against
each API's actual documented response shape; only Health Canada's LNHPD
still falls back to the schema-tolerant generic extractor, since its
real JSON response shape could not be confirmed at implementation time
(the public endpoint returned an empty body during testing) — see
`_query_health_canada`'s docstring.

**Strict domain filtering is the actual safety mechanism here, not the
per-source parsing.** Every candidate result — regardless of which source
produced it — is required to resolve to a URL whose hostname clears
`_is_verified_domain` *before* it's ever turned into a `VerifiedResourceSchema`
record. Unlike ResearchPaper (any source domain is accepted at ingestion
time, then relevance/quality-judged afterward by Gemini — see Phase 3/6),
there is no downstream grading step here to catch a bad link after the
fact: a generic blog post, unverified news site, or user-edited page
either resolves to an allow-listed hostname, or it is discarded outright
and never persisted. As of this phase the allow-list is
(`.gov`, `.europa.eu`, `.org`, `ebi.ac.uk`, `canada.ca`) — widened from
the original four-pattern list specifically to admit `europepmc.org`
(`.org`) and `www.ebi.ac.uk` (`ebi.ac.uk`, Europe PMC's actual API host)
and `health-products.canada.ca` (`canada.ca`, Health Canada LNHPD).
**`.org` is a deliberately broad suffix** — it admits any `.org` domain
whatsoever, not just EMBL-EBI's — accepted here only because it was
explicitly specified as a requirement; if this list is ever revisited,
narrowing `.org` down to specific known-good hosts (`europepmc.org`
itself, rather than the bare TLD) would meaningfully tighten this
without losing any currently-configured source.

**Parallel execution.** Every enabled source is queried concurrently via
one `asyncio.gather(..., return_exceptions=True)` call
(`_search_all_sources_async`) over a single shared `httpx.AsyncClient` —
`return_exceptions=True` specifically so that if a per-source coroutine
somehow still raises (every one of them is already independently
try/except-wrapped in `_safe_query_async`, so this is a defense-in-depth
guarantee, not the primary safety net), the exception comes back as a
plain object in the results list instead of aborting every other
in-flight source's request. Each request is bounded by a strict 5-second
timeout (`_HTTP_TIMEOUT_SECONDS`) applied uniformly via the shared
client's `httpx.Timeout` — one flaky or slow source can never block or
fail the others.

**Common-name -> chemical-name fallback.** If a source's first query (the
ingredient's common name, e.g. "Vitamin C") comes back with zero results,
`_safe_query_async` automatically retries that same source once with the
ingredient's known chemical/systematic name (e.g. "Ascorbic acid") when
one is available — see `_CHEMICAL_NAME_FALLBACKS`. This is a small,
hand-curated table of common supplement-label names, not a general
chemistry name-resolution service — an ingredient not in the table simply
isn't retried (its zero-result outcome for that source is left as-is).

**Quality grading (Phase 8), unchanged by this pass.** Every newly-found
resource is still graded against docs/resource_grading_rubric.json via
app/services/resource_grader.py::grade_resource, one Gemini call per
resource, sequentially, right here in
`fetch_verified_resources_for_ingredient`. A grading failure for one
resource is caught and logged, not re-raised — see that function's
docstring.

**Logging convention.** Every log line in this module is prefixed
`[ResourceFetcher]` and names the source's display label, e.g.
`[ResourceFetcher] MedlinePlus (NIH/NLM): 2 resources found` on success,
or `[ResourceFetcher] USDA FoodData Central: 403 error - invalid key` on
failure — so a single ingredient's grade-request log output makes it
immediately obvious which of the six sources actually contributed
results and which didn't (and why).

**Deterministic conclusion extraction, at fetch time (Phase 21).** Every
`_query_*` function below now returns its raw, already-deserialized API
response alongside the parsed `VerifiedResourceSchema` records (see the
updated `_QueryFn` type alias) — this raw payload never used to be kept
around past the point where it was parsed into the standardized schema.
`fetch_verified_resources_for_ingredient` now calls
`app/services/resource_parser.py::parse_resource_conclusions(api_id,
raw_payload)` once per source (immediately after that source's raw
response is fetched, since one raw payload is shared by every resource
that source contributes this call — a source can produce more than one
`VerifiedResourceSchema` record) and stores the resulting
`(conclusions, failure_reason)` pair directly onto every new
`VerifiedResource` row that source contributes, plus `api_id` itself
(the config entry's `id`, e.g. `"pubchem_pug_rest"`) so it's recorded
per-row for later reference/debugging. This replaces the Gemini-based
Stage 1 extraction step that used to run later, separately, in
`app/services/paper_analysis_pipeline.py` (Phase 17/19/20,
`app/services/resource_extractor.py`) — see that module's own docstring
for why it no longer has a Stage 1 step at all, and
`resource_parser.py`'s own module docstring for the full "why
deterministic, not Gemini" reasoning (rate limits, latency,
hallucination risk). Resource *quality* grading
(`app/services/resource_grader.py::grade_resource`, Phase 8, right below
in this same function) is unaffected — that's a distinct concern (an
A-E badge on how authoritative/well-sourced a resource is) from
conclusion extraction, and still makes its one Gemini call per resource
exactly as before.

**Phase 25 — fault-tolerant, resilient API client upgrade.** Addresses
four reliability problems observed in production against these
government endpoints (mostly NIH/NLM-hosted — PubChem, MedlinePlus,
DailyMed):

1. **Header rejection.** Every request now carries realistic consumer
   headers (`DEFAULT_HEADERS` — `User-Agent`/`Accept`/`Accept-Language`),
   set once on the shared `httpx.AsyncClient` in
   `_search_all_sources_async` rather than repeated per-call. Some
   NIH/NLM endpoints reject or heavily throttle requests that look
   script-generated (missing/generic `User-Agent`).
2. **Name ambiguity.** `resolve_ingredient_search_terms` returns a
   prioritized `[common name, chemical/Latin name, alternate synonyms,
   ...]` list (`_SYNONYM_MAP`, merged with the pre-existing single-name
   `_CHEMICAL_NAME_FALLBACKS` table for backward compatibility) — a
   source expecting a chemical/systematic/Latin name (e.g. "Ascorbic
   Acid", "Withania somnifera") gets a real chance to resolve a
   consumer-facing supplement label name it would otherwise never match.
   `_safe_query_async` tries these terms in order (bounded by
   `_MAX_SEARCH_TERM_ATTEMPTS`), stopping at the first that yields a
   result.
3. **Unexpected payload formats.** Every source now goes through the new
   shared `fetch_with_resilience` helper instead of a raw
   `client.get(...); response.raise_for_status(); response.json()`
   sequence — it only ever calls `.json()` when the response actually
   looks like JSON (content-type or body-prefix check), degrading to
   `None` instead of raising when a source returns an HTML error page or
   other unexpected format under load.
4. **Hanging requests.** Every `fetch_with_resilience` attempt is wrapped
   in its own `asyncio.wait_for(..., timeout=...)`, independent of the
   shared client-level `httpx.Timeout` already in place — plus up to
   `_MAX_FETCH_ATTEMPTS` (2) attempts with exponential backoff on a
   transient `500`/`502`/`503`/`504` and linear backoff on `429`, so one
   dead or overloaded endpoint can never stall the rest of the pipeline.

Per-provider hardening on top of the shared `fetch_with_resilience`
layer: PubChem now falls back to a name -> CIDs -> description-by-CID
lookup chain when the direct name->description call comes back empty
(`_query_pubchem`); MedlinePlus's primary query is now the (JSON,
code-oriented) Connect service per spec, with an automatic fallback to
the real free-text wsearch endpoint when Connect's coded-lookup
requirement yields nothing for a plain ingredient name
(`_query_medlineplus`); DailyMed caps results to the top 3 active SPL
matches (`_DAILYMED_MAX_ACTIVE_MATCHES`); USDA FoodData Central always
explicitly sends `pageSize=5` (`_USDA_PAGE_SIZE`) rather than relying on
USDA's own server default; Europe PMC's query was widened to also match
`"dietary supplement"`-labeled literature, not just `monograph`/`review`.

**Logging convention, extended.** Every source now logs one of three
explicit top-level outcomes once its full term-fallback loop settles —
`SUCCESS` (first term, i.e. the ingredient's own name, worked),
`FALLBACK_USED` (a later synonym/chemical-name term worked), or `FAILED`
(every term attempt came back empty) — on top of whatever richer,
provider-specific detail each `_query_*` function already logs for its
own internal attempt(s) (e.g. PubChem's own `FALLBACK_USED` line when its
CID-lookup chain, not a different search term, is what actually
succeeded).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel
from sqlmodel import Session, select

from app.models.research import VerifiedResource
from app.services.resource_grader import ResourceGradingError, grade_resource
from app.services.resource_parser import parse_resource_conclusions

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ResourceFetcher]"

# backend/app/services/resource_fetcher.py -> parents[2] == backend/ ->
# parents[3] == repo root — same absolute-path-resolution reasoning as
# app/services/paper_search.py's PAPER_APIS_PATH.
_REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFIED_RESOURCE_APIS_PATH = _REPO_ROOT / "docs" / "verified_resource_apis.json"

# Kept modest — same reasoning as paper_search.py's
# DEFAULT_MAX_RESULTS_PER_SOURCE: with up to 6 sources, this already means
# up to 6 HTTP calls per grade request, and each returning a handful of
# links is plenty for a "reference sheets" panel, not a comprehensive index.
DEFAULT_MAX_RESULTS_PER_SOURCE = 3

# Same short-timeout-and-skip philosophy as paper_search.py — one flaky
# government API should never block (or fail) the whole grade request.
_HTTP_TIMEOUT_SECONDS = 5.0

# --- Phase 25: fault-tolerant HTTP client hardening ---

# Government/NIH endpoints (especially NLM-hosted ones — PubChem,
# MedlinePlus, DailyMed) reject or heavily rate-limit requests that look
# like a generic script rather than a real consumer — no `User-Agent` at
# all, or one of httpx's own default values. Sent on every outbound
# request via the shared `httpx.AsyncClient(headers=DEFAULT_HEADERS, ...)`
# constructed in `_search_all_sources_async`, rather than repeated on each
# individual `client.get(...)` call.
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": "BSProof-DigitalWellbeing/1.0 (contact@bsproof.app; research-tool)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# HTTP statuses treated as transient/worth retrying inside
# fetch_with_resilience — a momentary upstream outage, not a real "this
# request will never succeed" failure (unlike a 404/400/403, which is
# retried zero times since retrying an identical request produces an
# identical failure).
_RETRYABLE_STATUS_CODES: Tuple[int, ...] = (500, 502, 503, 504)

# Two attempts total per fetch_with_resilience call (one retry) — kept
# small so one persistently-unreachable source can't multiply this
# module's worst-case per-ingredient latency; combined with the
# per-search-term retry loop in _safe_query_async (see
# resolve_ingredient_search_terms/_MAX_SEARCH_TERM_ATTEMPTS below), a
# single source can still make a handful of total attempts across
# different search terms without any one of them individually hanging.
_MAX_FETCH_ATTEMPTS = 2

# 429 (rate limited) backs off linearly — 1.5s, 3.0s, ... — per spec.
_RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.5

# 500/502/503/504 (transient server-side failure) back off
# exponentially — 1.0s, 2.0s, 4.0s, ... — distinct from the 429 case
# since a genuinely overloaded/misbehaving server benefits from a more
# aggressive back-off curve than a simple per-request rate limit does.
_TRANSIENT_ERROR_BACKOFF_BASE_SECONDS = 1.0

# USDA FoodData Central's checked-in DEMO_KEY (see
# docs/verified_resource_apis.json) is shared/rate-limited across every
# DEMO_KEY user worldwide — operators can set a real, higher-quota key via
# this environment variable without editing the checked-in config file.
_USDA_API_KEY_ENV_VAR = "USDA_API_KEY"

# Phase 25: explicitly sent on every USDA FoodData Central request
# (`_query_usda`) rather than relying on the API's own server-side
# default page size — see that function's docstring.
_USDA_PAGE_SIZE = 5

# Requirement: validate every returned resource URL against an allow-list
# of official domain extensions/hosts: `.gov`, `.europa.eu`, `.org`,
# `ebi.ac.uk`, `canada.ca`. Widened from the original four-pattern list
# (`.gov`, `.europa.eu`, `ncbi.nlm.nih.gov`, `efsa.europa.eu`) — the two
# specific-hostname entries are kept even though they're now redundant
# with `.gov`/`.europa.eu` (ncbi.nlm.nih.gov and efsa.europa.eu both
# already end in one of those two suffixes), purely for readability/
# self-documentation. See the module docstring for the `.org` breadth
# caveat.
_VERIFIED_DOMAIN_SUFFIXES: Tuple[str, ...] = (
    ".gov",
    ".europa.eu",
    ".org",
    "ebi.ac.uk",
    "canada.ca",
    "ncbi.nlm.nih.gov",
    "efsa.europa.eu",
)


def _is_verified_domain(domain: Optional[str]) -> bool:
    """True iff `domain` is (or is a subdomain of) one of
    _VERIFIED_DOMAIN_SUFFIXES above. Case-insensitive; a falsy/empty
    domain is never verified.
    """
    if not domain:
        return False
    normalized = domain.strip().lower().rstrip(".")
    if not normalized:
        return False
    for suffix in _VERIFIED_DOMAIN_SUFFIXES:
        bare = suffix[1:] if suffix.startswith(".") else suffix
        if normalized == bare or normalized.endswith("." + bare):
            return True
    return False


# Phase 39 — NIH resource extraction overhaul. A narrower subset of
# _VERIFIED_DOMAIN_SUFFIXES specifically for "is this domain an official
# NIH/NLM property" (as opposed to "is this domain verified/allow-listed
# at all", which _is_verified_domain answers). Every domain this app's
# three currently-configured NIH-affiliated sources
# (docs/verified_resource_apis.json's `pubchem_pug_rest`/`medlineplus_api`/
# `dailymed_api`) actually resolve to is listed explicitly, plus a
# `nih.gov` suffix check to also catch any future source added under that
# domain (e.g. ods.od.nih.gov, nccih.nih.gov — the "NIH Office of Dietary
# Supplements"/"NCCIH" resources named in the Phase 39 task spec, which
# this app does NOT currently fetch from at all — no configured API entry
# in docs/verified_resource_apis.json queries either domain today; see
# this module's own "Phase 39" docstring section and docs/Architecture.md
# for that scope note).
#
# Used by resource_parser.py (verbose "[NIH Extractor]" logging),
# html_resource_extractor.py (NIH-specific exhaustive extraction prompt +
# higher conclusions cap), resource_grader.py (an honest, rubric-aligned
# authority hint — NOT a grade bypass, see that module's own "Phase 39"
# docstring for why), and paper_analysis_pipeline.py (threading the flag
# into the HTML fallback call).
_NIH_DOMAIN_SUFFIXES: Tuple[str, ...] = (
    "nih.gov",
    "medlineplus.gov",
)


def is_nih_domain(domain: Optional[str]) -> bool:
    """True iff `domain` is (or is a subdomain of) an official NIH/NLM
    property — `nih.gov` (covers `pubchem.ncbi.nlm.nih.gov`,
    `dailymed.nlm.nih.gov`, `ncbi.nlm.nih.gov`, `ods.od.nih.gov`,
    `nccih.nih.gov`, ...) or `medlineplus.gov` (NIH/NLM-operated but not
    itself under the `nih.gov` suffix). Case-insensitive; a falsy/empty
    domain is never NIH. See `_NIH_DOMAIN_SUFFIXES` above.
    """
    if not domain:
        return False
    normalized = domain.strip().lower().rstrip(".")
    if not normalized:
        return False
    for suffix in _NIH_DOMAIN_SUFFIXES:
        if normalized == suffix or normalized.endswith("." + suffix):
            return True
    return False


async def fetch_with_resilience(
    client: httpx.AsyncClient,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 6.0,
    *,
    parse_mode: str = "json",
    on_status: Optional[Callable[[int], None]] = None,
    max_attempts: int = _MAX_FETCH_ATTEMPTS,
) -> Optional[Any]:
    """Generic, resilient GET used by every per-source `_query_*` function
    below (Phase 25) — replaces each provider's old bespoke `client.get(...)
    ; response.raise_for_status(); response.json()` sequence with one
    shared implementation that isolates a single slow/flaky/misbehaving
    endpoint so it can never hang or crash the whole fetch pass.

    - **Hanging requests (Problem #4).** Every attempt is wrapped in its
      own `asyncio.wait_for(..., timeout=timeout)`, independent of the
      shared client-level `httpx.Timeout` already applied in
      `_search_all_sources_async` — belt-and-suspenders against a
      connection that technically stays open but never sends a response.
    - **Transient failures.** Up to `max_attempts` (default
      `_MAX_FETCH_ATTEMPTS` = 2) attempts total. A `429` backs off
      linearly (`_RATE_LIMIT_BACKOFF_BASE_SECONDS * (attempt + 1)`); a
      `500`/`502`/`503`/`504` (`_RETRYABLE_STATUS_CODES`) backs off
      exponentially (`_TRANSIENT_ERROR_BACKOFF_BASE_SECONDS * 2 **
      attempt`) before the next attempt. Any other non-200 status (404,
      400, 403, ...) is treated as non-retryable — an identical request
      would just fail identically again — and returns `None` immediately
      without burning a second attempt.
    - **Unexpected payload formats (Problem #3).** On a `200`, the
      response is only ever treated as JSON if its `content-type` header
      mentions `json` or its stripped body starts with `{`/`[` — an HTML
      error page or an unexpected XML body (which some of these
      government APIs are known to return instead of the requested JSON
      under load) is logged and degrades to `None` rather than raising
      inside `response.json()`. `parse_mode="raw"` skips this check
      entirely and returns `response.text` unconditionally — used by
      MedlinePlus's wsearch XML fallback (see `_query_medlineplus`),
      which wants the raw body regardless of content-type so its own XML
      parser can handle it.
    - **Network errors/timeouts.** `httpx.RequestError` and
      `asyncio.TimeoutError` are caught per-attempt and logged, moving on
      to the next attempt (or returning `None` once attempts are
      exhausted) rather than propagating — same fail-open philosophy as
      every other layer of this module.
    - `on_status`, if given, is called with the raw HTTP status code
      immediately after every non-exception response, before this
      function's own retry/parse handling — lets a caller react to a
      specific status (e.g. `_query_usda` logging its own distinct "403 -
      invalid or rate-limited api_key" message) without duplicating the
      retry loop itself.

    Returns the parsed JSON (`dict`/`list`) or raw text (`parse_mode=
    "raw"`) on success, or `None` on any failure — callers must treat
    `None` as "this attempt produced nothing usable," not as an
    exception to handle themselves.
    """
    last_exc: Optional[BaseException] = None

    for attempt in range(max_attempts):
        try:
            response = await asyncio.wait_for(
                client.get(url, params=params),
                timeout=timeout,
            )
        except (httpx.RequestError, asyncio.TimeoutError) as exc:
            last_exc = exc
            logger.warning(
                "%s Timeout/Error querying %s (attempt %d/%d): %s",
                _LOG_PREFIX,
                url,
                attempt + 1,
                max_attempts,
                exc,
            )
            continue

        if on_status is not None:
            on_status(response.status_code)

        if response.status_code == 200:
            if parse_mode == "raw":
                return response.text

            content_type = response.headers.get("content-type", "").lower()
            body = response.text.strip()
            if "json" in content_type or body.startswith("{") or body.startswith("["):
                try:
                    return response.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.warning(
                        "%s %s returned malformed JSON despite a JSON-like "
                        "content-type/body: %s",
                        _LOG_PREFIX,
                        url,
                        exc,
                    )
                    return None
            logger.warning(
                "%s %s returned a non-JSON payload (content-type=%r) — "
                "likely an HTML error page or unexpected format; treating "
                "as failure rather than risking a parse crash.",
                _LOG_PREFIX,
                url,
                content_type,
            )
            return None

        if response.status_code == 429:
            backoff = _RATE_LIMIT_BACKOFF_BASE_SECONDS * (attempt + 1)
            logger.warning(
                "%s %s rate-limited (429) — backing off %.1fs before "
                "attempt %d/%d.",
                _LOG_PREFIX,
                url,
                backoff,
                attempt + 2,
                max_attempts,
            )
            await asyncio.sleep(backoff)
            continue

        if response.status_code in _RETRYABLE_STATUS_CODES:
            backoff = _TRANSIENT_ERROR_BACKOFF_BASE_SECONDS * (2**attempt)
            logger.warning(
                "%s %s returned transient HTTP %s — backing off %.1fs "
                "before attempt %d/%d.",
                _LOG_PREFIX,
                url,
                response.status_code,
                backoff,
                attempt + 2,
                max_attempts,
            )
            await asyncio.sleep(backoff)
            continue

        # Non-retryable status (404, 400, 403, ...) — no point spending a
        # second attempt on a request that will fail identically again.
        logger.warning(
            "%s %s returned non-retryable HTTP %s.",
            _LOG_PREFIX,
            url,
            response.status_code,
        )
        return None

    if last_exc is not None:
        logger.warning(
            "%s Giving up on %s after %d attempt(s): %s",
            _LOG_PREFIX,
            url,
            max_attempts,
            last_exc,
        )
    return None


class VerifiedResourceSchema(BaseModel):
    """Standardized shape every per-source parser below must produce,
    before it's turned into a VerifiedResource row (which needs an
    `ingredient_id` this layer doesn't know about) — same two-stage shape
    as paper_search.py's PaperRecord/ResearchPaper split, just a Pydantic
    model rather than a plain dataclass so a parser bug (wrong type,
    missing required field) is caught immediately at construction time
    instead of surfacing later as a confusing DB-layer error.
    """

    title: str
    publisher: str
    url: str
    domain: str
    summary: Optional[str] = None


# A source query function: given an httpx.AsyncClient, that source's
# config entry (endpoint/query_param/extra_params), the ingredient name to
# search for, and a max-results cap, returns the verified resources found
# AND (Phase 21) that source's raw, already-deserialized API response —
# `Any` since it's a dict for five of the six sources but plain response
# text (str) for MedlinePlus's real XML-returning endpoint — see
# app/services/resource_parser.py::parse_resource_conclusions, which is
# what actually consumes this raw payload. Async so every enabled source
# can be queried concurrently under one asyncio.gather (see
# _search_all_sources_async) — same convention as paper_search.py's
# `_QueryFn`.
_QueryFn = Callable[
    [httpx.AsyncClient, Dict[str, Any], str, int],
    Awaitable[Tuple[List[VerifiedResourceSchema], Any]],
]


def _load_resource_apis() -> List[Dict[str, Any]]:
    """Reads docs/verified_resource_apis.json. Returns an empty list
    (rather than raising) if the file is missing or malformed — a
    config-file problem shouldn't take down the whole grading pipeline;
    it just means no verified-resource sources get queried, same
    fail-open behavior as paper_search.py::_load_paper_apis.
    """
    try:
        with VERIFIED_RESOURCE_APIS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "%s Could not read verified resource API config at %s: %s",
            _LOG_PREFIX,
            VERIFIED_RESOURCE_APIS_PATH,
            exc,
        )
        return []

    if not isinstance(data, list):
        logger.warning(
            "%s Verified resource API config at %s did not contain a JSON array; ignoring.",
            _LOG_PREFIX,
            VERIFIED_RESOURCE_APIS_PATH,
        )
        return []
    return data


def _resolve_endpoint(config: Dict[str, Any], ingredient_name: str) -> Tuple[str, Dict[str, Any]]:
    """Builds the (url, params) to request for `ingredient_name` against
    one docs/verified_resource_apis.json entry.

    Most entries put the ingredient name in a query string parameter
    (`query_param`, e.g. "term"/"query"/"drug_name") alongside any static
    `extra_params`. `pubchem_pug_rest` is the exception: its `endpoint`
    has a `{ingredient_name}` placeholder baked into the URL *path*
    (`.../compound/name/{ingredient_name}/description/JSON`) and its
    `query_param` is the literal string `"path"` — a marker meaning "this
    source takes no query-string parameter for the ingredient name at
    all", not an actual parameter name to set. URL-encodes the substituted
    name (ingredient names can contain spaces/punctuation that aren't
    valid unescaped in a URL path segment).

    `europe_pmc`'s query function builds its own, different `query` value
    (`"{ingredient_name} AND (monograph OR review)"`, see
    `_query_europe_pmc`) rather than calling this helper for that
    parameter — this function is still used there for the endpoint URL
    and the rest of `extra_params` (format/resultType).
    """
    endpoint_template = str(config["endpoint"])
    extra_params = dict(config.get("extra_params") or {})

    if "{ingredient_name}" in endpoint_template:
        url = endpoint_template.format(ingredient_name=quote(ingredient_name, safe=""))
        return url, extra_params

    query_param = config.get("query_param")
    params = extra_params
    if query_param and query_param != "path":
        params = {**extra_params, str(query_param): ingredient_name}
    return endpoint_template, params


# --- Best-effort common-name -> chemical/systematic-name fallback ---
#
# Phase 25: no longer consulted directly by name — folded into
# `resolve_ingredient_search_terms` below (see that function's docstring)
# as the second-priority lookup, behind the newer, richer `_SYNONYM_MAP`.
# Kept as its own table (rather than merged data into `_SYNONYM_MAP`
# itself) purely so every entry that predates this phase stays reviewable
# on its own, unchanged.

# Small, hand-curated table of common supplement-label names -> their
# primary chemical/systematic name — see
# `resolve_ingredient_search_terms`'s docstring for how this is now used.
# Deliberately NOT exhaustive or a general name-resolution service: an
# ingredient not listed here just doesn't get this particular fallback
# term (it may still get one from `_SYNONYM_MAP`, or none at all).
# Keys are matched case-insensitively.
_CHEMICAL_NAME_FALLBACKS: Dict[str, str] = {
    "vitamin a": "Retinol",
    "vitamin b1": "Thiamine",
    "thiamin": "Thiamine",
    "vitamin b2": "Riboflavin",
    "vitamin b3": "Niacin",
    "vitamin b5": "Pantothenic acid",
    "vitamin b6": "Pyridoxine",
    "vitamin b7": "Biotin",
    "vitamin b9": "Folic acid",
    "folate": "Folic acid",
    "vitamin b12": "Cobalamin",
    "vitamin c": "Ascorbic acid",
    "vitamin d": "Cholecalciferol",
    "vitamin d2": "Ergocalciferol",
    "vitamin d3": "Cholecalciferol",
    "vitamin e": "Tocopherol",
    "vitamin k": "Phylloquinone",
    "vitamin k1": "Phylloquinone",
    "vitamin k2": "Menaquinone",
    "coenzyme q10": "Ubiquinone",
    "coq10": "Ubiquinone",
    "niacinamide": "Nicotinamide",
}


# Phase 25: hand-curated common-name -> [chemical/Latin/alternate names]
# table, richer than the single-fallback `_CHEMICAL_NAME_FALLBACKS` above
# (kept, not replaced — merged into `resolve_ingredient_search_terms`
# below so no existing coverage is lost) — some sources index compounds
# almost exclusively under their chemical/systematic name (PubChem) or a
# botanical Latin binomial (Health Canada LNHPD monographs), which a
# consumer-facing supplement label's common name will never match on its
# own. Same "small, hand-curated, not a general chemistry name-resolution
# service" caveat as `_CHEMICAL_NAME_FALLBACKS` — an ingredient not listed
# here just falls through to the generic two-term default. Keys are
# matched case-insensitively.
_SYNONYM_MAP: Dict[str, List[str]] = {
    "vitamin c": ["Vitamin C", "Ascorbic Acid", "L-Ascorbic Acid"],
    "ashwagandha": ["Ashwagandha", "Withania somnifera", "Indian Ginseng"],
    "turmeric": ["Turmeric", "Curcumin", "Curcuma longa"],
    "vitamin d3": ["Vitamin D3", "Cholecalciferol"],
}


def resolve_ingredient_search_terms(ingredient_name: str) -> List[str]:
    """Returns a prioritized list of search terms to try, in order, for
    `ingredient_name`: `[Common Name, Chemical/Latin Name, Alternate
    Synonyms, ...]` (Phase 25 — see module docstring's "Name Ambiguity"
    problem statement).

    Lookup order (case-insensitive against the stripped input):

    1. `_SYNONYM_MAP` — a curated multi-term entry (common name, chemical/
       Latin name, plus any further alternates) when one exists for this
       ingredient.
    2. `_CHEMICAL_NAME_FALLBACKS` — the older, single-alternate table this
       module already had (Phase 9) — kept and folded in here rather than
       replaced, so ingredients only covered by that table (e.g. "vitamin
       b12" -> "Cobalamin") don't lose their existing fallback coverage.
    3. Neither — falls back to `[ingredient_name, lowered]`, i.e. the
       original casing plus a lowercased variant, per spec (some sources'
       free-text search is case-sensitive in practice even though it
       shouldn't be).

    The original `ingredient_name` (in its original casing) always leads
    the returned list — callers try it first and only fall back to later
    entries when it produces zero results (see `_safe_query_async`). The
    result is de-duplicated (case-insensitive, first-seen order
    preserved) since a synonym occasionally collides with the original
    input verbatim (e.g. `_SYNONYM_MAP`'s "vitamin c" entry lists "Vitamin
    C" first, identical to a caller who already passed exactly that).
    """
    stripped = ingredient_name.strip()
    lowered = stripped.lower()

    if not lowered:
        return [ingredient_name]

    synonyms = _SYNONYM_MAP.get(lowered)
    if synonyms:
        candidate_terms = [stripped, *synonyms]
    else:
        single_fallback = _CHEMICAL_NAME_FALLBACKS.get(lowered)
        if single_fallback:
            candidate_terms = [stripped, single_fallback]
        else:
            candidate_terms = [stripped, lowered]

    seen: set = set()
    resolved: List[str] = []
    for term in candidate_terms:
        term = term.strip()
        if not term:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        resolved.append(term)
    return resolved or [ingredient_name]


# --- Generic, schema-tolerant extraction (fallback path — currently only
# used for Health Canada LNHPD, see _query_health_canada) ---

# Tried in order against each candidate dict — the first key present with a
# non-empty string value wins. Deliberately broad/duplicated (rather than
# one fixed key per source) since this module can't pin down every
# government API's exact field naming ahead of time — see module
# docstring. Extended with plausible Health Canada LNHPD field names
# (product_name/licence_number/company_name and their camelCase variants)
# on top of the original MedlinePlus/USDA/EFSA-oriented key guesses.
_TITLE_KEYS: Tuple[str, ...] = (
    "title",
    "name",
    "topic",
    "healthTopic",
    "display_name",
    "displayName",
    "claim",
    "product_name",
    "productName",
    "brand_name",
    "brandName",
)
_PUBLISHER_KEYS: Tuple[str, ...] = (
    "publisher",
    "organization",
    "org",
    "agency",
    "authority",
    "source",
    "sourceName",
    "company_name",
    "companyName",
)
_URL_KEYS: Tuple[str, ...] = (
    "url",
    "link",
    "href",
    "resource_url",
    "resourceUrl",
    "source_url",
    "sourceUrl",
    "descriptionUrl",
)
_SUMMARY_KEYS: Tuple[str, ...] = (
    "summary",
    "snippet",
    "abstract",
    "description",
    "overview",
    "fullSummary",
    "licence_number",
    "licenceNumber",
)

# Depth guard for _iter_candidate_dicts below — JSON parsed via
# `json.loads` can't contain cycles, but an extremely deeply nested or
# huge payload from a misbehaving source shouldn't be walked unbounded.
_MAX_WALK_DEPTH = 12


def _first_str(entry: Dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _iter_candidate_dicts(node: Any, depth: int = 0) -> Iterable[Dict[str, Any]]:
    """Recursively yields every dict found in a parsed JSON payload (lists
    are descended into, dicts are yielded and then descended into via
    their values), bounded by _MAX_WALK_DEPTH.

    A source's response might be a flat list of results, a `{"results":
    [...]}` / `{"data": {"items": [...]}}` envelope, or something else
    entirely — rather than hardcoding each government API's exact
    envelope shape (which this module doesn't control and isn't
    guaranteed to stay stable), this walks the whole structure and lets
    `_extract_generic_records` below decide which dicts actually look
    like a resource entry.
    """
    if depth > _MAX_WALK_DEPTH:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_candidate_dicts(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_candidate_dicts(item, depth + 1)


def _extract_generic_records(
    payload: Any, publisher_fallback: str, max_results: int
) -> List[VerifiedResourceSchema]:
    """Schema-tolerant extraction — currently only used for Health Canada
    LNHPD (see module docstring: every other source now has a precise,
    shape-aware parser). A dict qualifies as a candidate resource entry
    only if it has BOTH a URL-ish field with a plain string value
    (checked against the domain allow-list right here, not deferred) AND
    at least one of a title/publisher/summary-ish field — the second
    requirement specifically filters out incidental URL-bearing objects
    that aren't actual resource entries (e.g. a bare `{"next":
    "https://api.../page=2"}` pagination link, which has a URL but
    nothing else resource-like).
    """
    records: List[VerifiedResourceSchema] = []
    seen_urls: set = set()

    for entry in _iter_candidate_dicts(payload):
        url = _first_str(entry, _URL_KEYS)
        if not url or url in seen_urls:
            continue
        if not any(key in entry for key in (*_TITLE_KEYS, *_PUBLISHER_KEYS, *_SUMMARY_KEYS)):
            continue

        domain = urlparse(url).netloc.lower()
        if not _is_verified_domain(domain):
            continue

        title = _first_str(entry, _TITLE_KEYS) or publisher_fallback
        publisher = _first_str(entry, _PUBLISHER_KEYS) or publisher_fallback
        summary = _first_str(entry, _SUMMARY_KEYS)

        records.append(
            VerifiedResourceSchema(
                title=title,
                publisher=publisher,
                url=url,
                domain=domain,
                summary=summary,
            )
        )
        seen_urls.add(url)
        if len(records) >= max_results:
            break

    return records


async def _query_generic(
    client: httpx.AsyncClient,
    config: Dict[str, Any],
    ingredient_name: str,
    max_results: int,
    *,
    publisher_fallback: str,
) -> Tuple[List[VerifiedResourceSchema], Any]:
    """Shared query implementation for sources without their own precise
    parser — as of this phase, only `health_canada_lnhpd` (see module
    docstring). `publisher_fallback` is bound per-source via
    functools.partial in _SOURCE_QUERY_FUNCTIONS below.

    Routed through `fetch_with_resilience` (Phase 25) like every other
    source below — retried/backed-off/timeout-guarded the same way, and
    degrades to an empty result rather than raising on a malformed
    payload.
    """
    url, params = _resolve_endpoint(config, ingredient_name)
    payload = await fetch_with_resilience(client, url, params, timeout=_HTTP_TIMEOUT_SECONDS)
    if payload is None:
        logger.info(
            "%s %s: FAILED (no usable response for %r).",
            _LOG_PREFIX,
            publisher_fallback,
            ingredient_name,
        )
        return [], None

    records = _extract_generic_records(payload, publisher_fallback, max_results)
    if records:
        logger.info(
            "%s %s: SUCCESS (%d record(s) found for %r).",
            _LOG_PREFIX,
            publisher_fallback,
            len(records),
            ingredient_name,
        )
    else:
        logger.info(
            "%s %s: FAILED (0 records extracted from response for %r).",
            _LOG_PREFIX,
            publisher_fallback,
            ingredient_name,
        )
    return records, payload


# --- Precise parser: PubChem PUG REST (well-known, stable JSON shape) ---


def _pubchem_records_from_payload(
    payload: Any, max_results: int
) -> Tuple[List[VerifiedResourceSchema], List[Dict[str, Any]]]:
    """Shared record-building logic for both PubChem attempts below
    (direct name->description, and the CID-fallback chain's final
    cid->description call) — both hit the same `.../description/JSON`
    endpoint shape, `{"InformationList": {"Information": [{"CID", "Title",
    "Description", "DescriptionSourceName", "DescriptionURL"}, ...]}}`,
    one entry per data source PubChem has a description from for that
    compound (there's often more than one — e.g. PubChem's own curated
    summary plus a Wikipedia mirror — which is exactly why this still
    needs the domain filter below rather than trusting every entry).
    Returns `(records, information)` — `information` (the raw list) is
    handed back too purely so callers can log e.g. the first CID found.
    """
    information = ((payload or {}).get("InformationList") or {}).get("Information") or []
    if not isinstance(information, list):
        return [], []

    records: List[VerifiedResourceSchema] = []
    for info in information:
        if len(records) >= max_results:
            break
        if not isinstance(info, dict):
            continue

        source_url = info.get("DescriptionURL")
        if not source_url:
            # Some entries (notably PubChem's own) omit DescriptionURL —
            # fall back to the canonical PubChem compound page for that
            # CID, which is still an ncbi.nlm.nih.gov URL and so still
            # clears the domain filter below.
            cid = info.get("CID")
            source_url = f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}" if cid else None
        if not source_url:
            continue

        domain = urlparse(source_url).netloc.lower()
        if not _is_verified_domain(domain):
            continue

        compound_title = info.get("Title") or "Compound"
        records.append(
            VerifiedResourceSchema(
                title=f"PubChem Compound Summary — {compound_title}",
                publisher=info.get("DescriptionSourceName") or "National Institutes of Health (PubChem)",
                url=source_url,
                domain=domain,
                summary=info.get("Description") or None,
            )
        )

    return records, information


async def _query_pubchem(
    client: httpx.AsyncClient, config: Dict[str, Any], ingredient_name: str, max_results: int
) -> Tuple[List[VerifiedResourceSchema], Any]:
    """NIH PubChem PUG REST — two-step, fault-tolerant lookup (Phase 25).

    **Attempt 1 — direct name lookup.**
    `.../compound/name/{name}/description/JSON` in one call, per the
    endpoint already configured in `docs/verified_resource_apis.json`.
    PubChem's name-matching for this endpoint is fairly strict (near-exact
    string match against a compound's registered synonyms) — a name that
    doesn't resolve returns a 404 (via `fetch_with_resilience`, which
    already turns that into a non-retryable `None` rather than raising).

    **Attempt 2 — CID search, then description-by-CID (fallback).** If
    attempt 1 produced zero usable records (a 404, or a 200 with an empty
    `Information` list), retries via PubChem's two-step CID-resolution
    path instead: `.../compound/name/{name}/cids/JSON` (looser matching
    than the description endpoint — returns a `IdentifierList.CID` array
    of candidate compound ids), then
    `.../compound/cid/{first_cid}/description/JSON` for that CID's actual
    description payload (reusing `_pubchem_records_from_payload`, the
    exact same shape as attempt 1's response). This mirrors PubChem's own
    documented "resolve by name however you can, then always work from
    the CID" pattern, and gives a genuine second chance at names the
    strict description-by-name lookup rejects outright.

    Both attempts are independently routed through `fetch_with_resilience`
    (timeout/retry/backoff/malformed-payload guarding) rather than a raw
    `client.get`.
    """
    url, params = _resolve_endpoint(config, ingredient_name)
    payload = await fetch_with_resilience(client, url, params, timeout=_HTTP_TIMEOUT_SECONDS)
    records, information = _pubchem_records_from_payload(payload, max_results)

    if records:
        first_cid = information[0].get("CID") if information else None
        logger.info(
            "%s PubChem: SUCCESS (Found CID %s for %r).",
            _LOG_PREFIX,
            first_cid,
            ingredient_name,
        )
        return records, payload

    # Fallback chain: name -> CIDs -> description-by-CID. Only attempted
    # when the direct name->description call above came back empty (see
    # docstring) — this is a distinct fallback dimension from the
    # search-term synonym loop in _safe_query_async: it retries the SAME
    # search term via a different PubChem endpoint/strategy, not a
    # different term.
    cids_url = url.replace("/description/JSON", "/cids/JSON")
    cids_payload = await fetch_with_resilience(client, cids_url, params, timeout=_HTTP_TIMEOUT_SECONDS)
    cids = ((cids_payload or {}).get("IdentifierList") or {}).get("CID") if isinstance(cids_payload, dict) else None

    if isinstance(cids, list) and cids:
        first_cid = cids[0]
        cid_description_url = (
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{first_cid}/description/JSON"
        )
        cid_payload = await fetch_with_resilience(client, cid_description_url, timeout=_HTTP_TIMEOUT_SECONDS)
        records, information = _pubchem_records_from_payload(cid_payload, max_results)
        if records:
            logger.info(
                "%s PubChem: FALLBACK_USED (CID search found CID %s for %r).",
                _LOG_PREFIX,
                first_cid,
                ingredient_name,
            )
            return records, cid_payload

    logger.info(
        "%s PubChem: FAILED (no description found via name or CID lookup for %r).",
        _LOG_PREFIX,
        ingredient_name,
    )
    return [], payload


# --- Precise parser: MedlinePlus free-text health topic search (XML,
# with a JSON fallback path in case the configured endpoint is ever
# pointed at a JSON-emitting variant) ---

# Strips any embedded HTML markup MedlinePlus's search-term-highlighting
# leaves inside <content name="title">/<content name="FullSummary">
# text (e.g. `<span class="qt0">Vitamin</span>`) — this module only wants
# the plain text, not the highlight spans.
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = _HTML_TAG_RE.sub("", value).strip()
    return cleaned or None


def _parse_medlineplus_xml(xml_text: str, max_results: int) -> List[VerifiedResourceSchema]:
    """Parses the MedlinePlus wsearch response — real shape confirmed
    against a live request:

        <nlmSearchResult>
          <list ...>
            <document rank="0" url="https://medlineplus.gov/vitaminc.html">
              <content name="title">...</content>
              <content name="organizationName">National Library of Medicine</content>
              <content name="FullSummary">...</content>
              <content name="snippet">...</content>
            </document>
            ...
          </list>
        </nlmSearchResult>

    Every `<content>` child's text may contain embedded highlight-span
    HTML (see `_strip_html` above) — stripped before use.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"MedlinePlus response was not valid XML: {exc}") from exc

    records: List[VerifiedResourceSchema] = []
    for document in root.iter("document"):
        if len(records) >= max_results:
            break

        url = document.get("url")
        if not url:
            continue
        domain = urlparse(url).netloc.lower()
        if not _is_verified_domain(domain):
            continue

        title = None
        publisher = None
        summary = None
        for content in document.findall("content"):
            name = content.get("name")
            text = _strip_html(content.text)
            if name == "title" and text:
                title = text
            elif name == "organizationName" and text:
                publisher = text
            elif name in ("FullSummary", "snippet") and text and not summary:
                summary = text

        if not title:
            continue

        records.append(
            VerifiedResourceSchema(
                title=title,
                publisher=publisher or "National Library of Medicine (MedlinePlus)",
                url=url,
                domain=domain,
                summary=summary,
            )
        )

    return records


# --- MedlinePlus Connect (primary, Phase 25) — the JSON-returning,
# code-based lookup service configured in docs/verified_resource_apis.json
# (endpoint https://connect.medlineplus.gov/service). Its Atom-derived JSON
# envelope wraps most text fields in a `{"_value": "..."}` dict rather
# than a bare string. ---


def _atom_value(node: Any) -> Optional[str]:
    """Extracts the text content of one MedlinePlus Connect Atom-JSON
    field — either a bare string, or (the more common case in this feed)
    a `{"_value": "...", ...}` dict. Returns `None` for anything else
    (missing field, unexpected shape) rather than raising.
    """
    if isinstance(node, str):
        return node.strip() or None
    if isinstance(node, dict):
        value = node.get("_value")
        if isinstance(value, str):
            return value.strip() or None
    return None


def _parse_medlineplus_connect_json(
    payload: Dict[str, Any], max_results: int
) -> List[VerifiedResourceSchema]:
    """Parses a MedlinePlus Connect response: `{"feed": {"entry": [{"title":
    {"_value": ...}, "summary": {"_value": "<div>...html...</div>"},
    "link": [{"href": "https://medlineplus.gov/...", "rel": "alternate"},
    ...]}, ...]}}`. `summary._value` commonly contains embedded HTML (a
    `<div>`-wrapped health topic blurb) — stripped via `_strip_html` per
    spec ("Clean XML/HTML tags from returned summary._value blocks").
    """
    entries = ((payload.get("feed") or {}).get("entry")) or []
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return []

    records: List[VerifiedResourceSchema] = []
    for entry in entries:
        if len(records) >= max_results:
            break
        if not isinstance(entry, dict):
            continue

        links = entry.get("link") or []
        if isinstance(links, dict):
            links = [links]
        url = None
        if isinstance(links, list):
            for link in links:
                href = link.get("href") if isinstance(link, dict) else None
                if href:
                    url = href
                    break
        if not url:
            continue

        domain = urlparse(url).netloc.lower()
        if not _is_verified_domain(domain):
            continue

        title = _atom_value(entry.get("title"))
        if not title:
            continue
        summary = _strip_html(_atom_value(entry.get("summary")))

        records.append(
            VerifiedResourceSchema(
                title=title,
                publisher="National Library of Medicine (MedlinePlus Connect)",
                url=url,
                domain=domain,
                summary=summary,
            )
        )

    return records


# --- MedlinePlus wsearch (Phase 25 fallback) ---
#
# MedlinePlus Connect's `mainSearchCriteria.v.c` parameter is documented
# to expect a standardized code (ICD-9/10-CM, RxNorm, SNOMED CT — the
# `mainSearchCriteria.v.cs` OID selects which coding system), not a
# free-text ingredient name — a plain supplement-label name like
# "Ashwagandha" may well resolve to zero Connect entries even on a
# perfectly healthy request/response. Rather than let that mean "this
# source never works for supplements," this function automatically falls
# back to MedlinePlus's real free-text health-topic search
# (wsearch.nlm.nih.gov) when Connect comes back empty — giving this
# source a genuine chance to resolve a common name even though the
# primary, spec'd endpoint is code-oriented. See `_parse_medlineplus_xml`
# for that endpoint's real (XML) response shape.
_MEDLINEPLUS_WSEARCH_URL = "https://wsearch.nlm.nih.gov/ws/query"


async def _query_medlineplus(
    client: httpx.AsyncClient, config: Dict[str, Any], ingredient_name: str, max_results: int
) -> Tuple[List[VerifiedResourceSchema], Any]:
    """MedlinePlus, Phase 25: primary + fallback, both routed through
    `fetch_with_resilience`.

    **Primary — MedlinePlus Connect** (`docs/verified_resource_apis.json`'s
    configured endpoint/params — JSON, per spec). Parsed by
    `_parse_medlineplus_connect_json` above.

    **Fallback — MedlinePlus wsearch** (real free-text health-topic
    search, XML) — only attempted when Connect yields zero records (see
    `_MEDLINEPLUS_WSEARCH_URL`'s docstring for why that's a real,
    expected outcome for a plain ingredient name against Connect).
    Requested with `parse_mode="raw"` since this endpoint returns XML, not
    JSON, and parsed via the existing `_parse_medlineplus_xml`.
    """
    url, params = _resolve_endpoint(config, ingredient_name)
    payload = await fetch_with_resilience(client, url, params, timeout=_HTTP_TIMEOUT_SECONDS)

    records: List[VerifiedResourceSchema] = []
    if isinstance(payload, dict):
        records = _parse_medlineplus_connect_json(payload, max_results)

    if records:
        logger.info(
            "%s MedlinePlus: SUCCESS (Connect returned %d entr%s for %r).",
            _LOG_PREFIX,
            len(records),
            "y" if len(records) == 1 else "ies",
            ingredient_name,
        )
        return records, payload

    wsearch_params = {"db": "healthTopics", "term": ingredient_name}
    xml_text = await fetch_with_resilience(
        client, _MEDLINEPLUS_WSEARCH_URL, wsearch_params, timeout=_HTTP_TIMEOUT_SECONDS, parse_mode="raw"
    )
    if xml_text:
        try:
            records = _parse_medlineplus_xml(xml_text, max_results)
        except ValueError as exc:
            logger.warning(
                "%s MedlinePlus: wsearch fallback returned unparseable XML for %r: %s",
                _LOG_PREFIX,
                ingredient_name,
                exc,
            )
            records = []

    if records:
        logger.info(
            "%s MedlinePlus: FALLBACK_USED (%r yielded %d entr%s via wsearch free-text search).",
            _LOG_PREFIX,
            ingredient_name,
            len(records),
            "y" if len(records) == 1 else "ies",
        )
        return records, xml_text

    logger.info(
        "%s MedlinePlus: FAILED (no entries from Connect or wsearch fallback for %r).",
        _LOG_PREFIX,
        ingredient_name,
    )
    return [], payload


# --- Precise parser: USDA FoodData Central `/foods/search` ---


async def _query_usda(
    client: httpx.AsyncClient, config: Dict[str, Any], ingredient_name: str, max_results: int
) -> Tuple[List[VerifiedResourceSchema], Any]:
    """USDA FoodData Central `/fdc/v1/foods/search` — real response shape:
    `{"totalHits": ..., "foods": [{"fdcId": ..., "description": ...,
    "dataType": ..., "foodCategory": ...}, ...]}`. There is no URL field
    in the response at all — this builds one from `fdcId` using the
    official FoodData Central app's food-detail URL pattern
    (`https://fdc.nal.usda.gov/food-details/{fdcId}/nutrients`), which is
    always an `fdc.nal.usda.gov` (`.gov`) URL and so always clears the
    domain filter regardless of the specific fdcId.

    `api_key` is resolved fresh on every call from the `USDA_API_KEY`
    environment variable when set, overriding whatever static value (the
    rate-limited `DEMO_KEY`) is checked into
    docs/verified_resource_apis.json's `extra_params` — see
    `_USDA_API_KEY_ENV_VAR`. `pageSize` is always explicitly set to
    `_USDA_PAGE_SIZE` (Phase 25 — per spec, "explicitly specify
    pageSize=5"), overriding anything already in `extra_params`, so this
    call never silently relies on USDA's own server-side default.
    """
    url, params = _resolve_endpoint(config, ingredient_name)
    params = dict(params)
    env_api_key = os.environ.get(_USDA_API_KEY_ENV_VAR)
    if env_api_key:
        params["api_key"] = env_api_key
    elif "api_key" not in params:
        params["api_key"] = "DEMO_KEY"
    params["pageSize"] = _USDA_PAGE_SIZE

    def _log_status(status_code: int) -> None:
        if status_code == 403:
            # Distinct, explicit log line per the "USDA: 403 error -
            # invalid key" example — fetch_with_resilience's own generic
            # non-retryable-status log still fires too, but this gives a
            # clearer, source-specific message on top of it.
            logger.warning(
                "%s USDA FoodData Central: 403 error - invalid or rate-limited api_key.",
                _LOG_PREFIX,
            )

    payload = await fetch_with_resilience(
        client, url, params, timeout=_HTTP_TIMEOUT_SECONDS, on_status=_log_status
    )
    if payload is None:
        logger.info("%s USDA FoodData Central: FAILED (no usable response for %r).", _LOG_PREFIX, ingredient_name)
        return [], None

    foods = payload.get("foods") or []
    records: List[VerifiedResourceSchema] = []
    for food in foods:
        if len(records) >= max_results:
            break

        fdc_id = food.get("fdcId")
        description = food.get("description")
        if not fdc_id or not description:
            continue

        source_url = f"https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients"
        domain = urlparse(source_url).netloc.lower()
        if not _is_verified_domain(domain):
            continue

        data_type = food.get("dataType")
        food_category = food.get("foodCategory")
        summary_parts = [part for part in (data_type, food_category) if part]
        summary = " — ".join(summary_parts) if summary_parts else None

        records.append(
            VerifiedResourceSchema(
                title=str(description),
                publisher="U.S. Department of Agriculture (FoodData Central)",
                url=source_url,
                domain=domain,
                summary=summary,
            )
        )

    if records:
        logger.info(
            "%s USDA FoodData Central: SUCCESS (%d record(s) found for %r).",
            _LOG_PREFIX,
            len(records),
            ingredient_name,
        )
    else:
        logger.info(
            "%s USDA FoodData Central: FAILED (0 records found for %r).",
            _LOG_PREFIX,
            ingredient_name,
        )
    return records, payload


# --- Precise parser: DailyMed `/spls.json?drug_name=...` ---

# Phase 25: hard cap on active SPL matches returned, per spec — see
# _query_dailymed's docstring.
_DAILYMED_MAX_ACTIVE_MATCHES = 3


def _dailymed_entry_is_active(entry: Dict[str, Any]) -> bool:
    """True unless `entry` carries an explicit falsy active/status field —
    see _query_dailymed's docstring for the "fail open, schema not
    confirmed" reasoning.
    """
    for active_field in ("active", "is_active", "status"):
        if active_field in entry and not entry.get(active_field):
            return False
    return True


async def _query_dailymed(
    client: httpx.AsyncClient, config: Dict[str, Any], ingredient_name: str, max_results: int
) -> Tuple[List[VerifiedResourceSchema], Any]:
    """DailyMed `/dailymed/services/v2/spls.json?drug_name=...` — real
    response shape (confirmed against NLM's own API documentation):
    `{"metadata": {...}, "data": [{"setid": ..., "title": ...,
    "published_date": ..., "spl_version": ..., "active": ...}, ...]}`.
    The response itself has no direct web link either — this builds the
    canonical SPL detail page URL from `setid`
    (`https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}`),
    always a `dailymed.nlm.nih.gov` (`.gov`) URL.

    Phase 25: capped to the top `_DAILYMED_MAX_ACTIVE_MATCHES` (3) active
    matches, regardless of the caller-supplied `max_results` — per spec
    ("Limit returned SPL list to top 3 active matches"). "Active" is
    determined defensively: an entry is skipped only if it carries an
    explicit falsy `active`/`is_active`/`status` field (DailyMed's public
    docs don't guarantee one of these field names is always present) —
    an entry with none of those fields is treated as active by default
    (fail open, same "don't lose real data over an unconfirmed schema
    detail" reasoning as `_query_health_canada`'s generic fallback).
    """
    url, params = _resolve_endpoint(config, ingredient_name)
    payload = await fetch_with_resilience(client, url, params, timeout=_HTTP_TIMEOUT_SECONDS)
    if payload is None:
        logger.info("%s DailyMed: FAILED (no usable response for %r).", _LOG_PREFIX, ingredient_name)
        return [], None

    entries = payload.get("data") or []
    effective_max = min(max_results, _DAILYMED_MAX_ACTIVE_MATCHES)
    records: List[VerifiedResourceSchema] = []
    for entry in entries:
        if len(records) >= effective_max:
            break
        if not isinstance(entry, dict) or not _dailymed_entry_is_active(entry):
            continue

        setid = entry.get("setid")
        title = entry.get("title")
        if not setid or not title:
            continue

        source_url = f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"
        domain = urlparse(source_url).netloc.lower()
        if not _is_verified_domain(domain):
            continue

        published_date = entry.get("published_date")
        summary = f"Structured Product Label (SPL), published {published_date}." if published_date else None

        records.append(
            VerifiedResourceSchema(
                title=str(title),
                publisher="U.S. National Library of Medicine (DailyMed)",
                url=source_url,
                domain=domain,
                summary=summary,
            )
        )

    if records:
        logger.info(
            "%s DailyMed: SUCCESS (%d active SPL label(s) found for %r).",
            _LOG_PREFIX,
            len(records),
            ingredient_name,
        )
    else:
        logger.info(
            "%s DailyMed: FAILED (0 active SPL labels found for %r).",
            _LOG_PREFIX,
            ingredient_name,
        )
    return records, payload


# --- Precise parser: Europe PMC (open-access monographs/reviews) ---


async def _query_europe_pmc(
    client: httpx.AsyncClient, config: Dict[str, Any], ingredient_name: str, max_results: int
) -> Tuple[List[VerifiedResourceSchema], Any]:
    """Europe PMC REST search — builds the query as `'{ingredient_name} AND
    (monograph OR "dietary supplement" OR review)'` (Phase 25 — widened
    from `"(monograph OR review)"` per spec, to also directly target
    dietary-supplement-labeled literature) rather than a bare
    ingredient-name search, and keeps only `isOpenAccess == "Y"` results
    ("official open-access review links"). Real response shape:
    `{"resultList": {"result": [{"id", "source", "title", "journalTitle",
    "isOpenAccess", "abstractText", "fullTextUrlList": {"fullTextUrl":
    [{"availability", "documentStyle", "url"}, ...]}}, ...]}}`. Prefers
    an HTML full-text URL from `fullTextUrlList` when present; falls back
    to Europe PMC's own canonical article page
    (`https://europepmc.org/article/{source}/{id}`, an `.org` URL) when a
    result is flagged open-access but the full-text-URL list is
    missing/doesn't contain a usable link.
    """
    endpoint_url, base_params = _resolve_endpoint(config, ingredient_name)
    params = dict(base_params)
    params["query"] = f'{ingredient_name} AND (monograph OR "dietary supplement" OR review)'
    params["pageSize"] = max(max_results * 2, max_results)

    payload = await fetch_with_resilience(client, endpoint_url, params, timeout=_HTTP_TIMEOUT_SECONDS)
    if payload is None:
        logger.info("%s Europe PMC: FAILED (no usable response for %r).", _LOG_PREFIX, ingredient_name)
        return [], None

    results = ((payload.get("resultList") or {}).get("result")) or []
    records: List[VerifiedResourceSchema] = []
    for result in results:
        if len(records) >= max_results:
            break

        if result.get("isOpenAccess") != "Y":
            continue

        title = result.get("title")
        if not title:
            continue

        source_url = None
        full_text_urls = ((result.get("fullTextUrlList") or {}).get("fullTextUrl")) or []
        for candidate in full_text_urls:
            if candidate.get("documentStyle") == "html" and candidate.get("url"):
                source_url = candidate["url"]
                break
        if not source_url and full_text_urls:
            source_url = full_text_urls[0].get("url")
        if not source_url:
            source = result.get("source")
            result_id = result.get("id")
            if source and result_id:
                source_url = f"https://europepmc.org/article/{source}/{result_id}"
        if not source_url:
            continue

        domain = urlparse(source_url).netloc.lower()
        if not _is_verified_domain(domain):
            continue

        abstract = result.get("abstractText")
        summary = abstract.strip()[:600] if isinstance(abstract, str) and abstract.strip() else None

        records.append(
            VerifiedResourceSchema(
                title=str(title),
                publisher=result.get("journalTitle") or "Europe PMC (EMBL-EBI)",
                url=source_url,
                domain=domain,
                summary=summary,
            )
        )

    if records:
        logger.info(
            "%s Europe PMC: SUCCESS (%d open-access record(s) found for %r).",
            _LOG_PREFIX,
            len(records),
            ingredient_name,
        )
    else:
        logger.info(
            "%s Europe PMC: FAILED (0 open-access records found for %r).",
            _LOG_PREFIX,
            ingredient_name,
        )
    return records, payload


# --- Health Canada LNHPD — schema-tolerant (see docstring) ---


async def _query_health_canada(
    client: httpx.AsyncClient, config: Dict[str, Any], ingredient_name: str, max_results: int
) -> Tuple[List[VerifiedResourceSchema], Any]:
    """Health Canada's Licensed Natural Health Products Database
    (LNHPD) ingredient endpoint — queried for authorized monograph/
    licence details for `ingredient_name`.

    Unlike every other source in this module, this one does NOT have a
    hand-verified precise parser: the public LNHPD API's exact JSON
    response envelope could not be confirmed at implementation time (a
    live test request against the configured endpoint returned an empty
    body), so this defers to the same schema-tolerant
    `_extract_generic_records` extraction used for every source before
    this phase's fix, with LNHPD-plausible field names
    (`product_name`/`licence_number`/`company_name` and their camelCase
    variants) added to `_TITLE_KEYS`/`_PUBLISHER_KEYS`/`_SUMMARY_KEYS`
    above to give it a reasonable chance of matching real LNHPD field
    naming if/when the live API responds with data. If LNHPD's actual
    schema is confirmed later, this should be upgraded to a precise
    parser the same way MedlinePlus/USDA/DailyMed/Europe PMC were in this
    same pass.
    """
    return await _query_generic(
        client,
        config,
        ingredient_name,
        max_results,
        publisher_fallback="Health Canada (Licensed Natural Health Products Database)",
    )


# Maps each recognized `id` in docs/verified_resource_apis.json to
# (display label, query function) — same lookup-table pattern as
# paper_search.py's _SOURCE_QUERY_FUNCTIONS.
_SOURCE_QUERY_FUNCTIONS: Dict[str, Tuple[str, _QueryFn]] = {
    "pubchem_pug_rest": ("PubChem (NIH)", _query_pubchem),
    "medlineplus_api": ("MedlinePlus (NIH/NLM)", _query_medlineplus),
    "usda_fooddata": ("USDA FoodData Central", _query_usda),
    "dailymed_api": ("DailyMed (NIH/NLM)", _query_dailymed),
    "europe_pmc": ("Europe PMC (EMBL-EBI)", _query_europe_pmc),
    "health_canada_lnhpd": ("Health Canada LNHPD", _query_health_canada),
}


def _enabled_resource_apis() -> List[Dict[str, Any]]:
    """Every entry in docs/verified_resource_apis.json with `enabled` not
    explicitly `false` and a recognized `id` this module has a query
    function for — same convention as paper_search.py::_enabled_api_configs.
    """
    configs: List[Dict[str, Any]] = []
    for entry in _load_resource_apis():
        api_id = entry.get("id")
        if entry.get("enabled") is False:
            continue
        if api_id not in _SOURCE_QUERY_FUNCTIONS:
            logger.warning(
                "%s Skipping unrecognized verified resource API config id %r.",
                _LOG_PREFIX,
                api_id,
            )
            continue
        configs.append(entry)
    return configs


async def _run_source_query(
    query_fn: _QueryFn,
    client: httpx.AsyncClient,
    config: Dict[str, Any],
    source_label: str,
    ingredient_name: str,
    max_results: int,
) -> Tuple[List[VerifiedResourceSchema], Any]:
    """Runs one source query for one search term, converting any failure
    (timeout, rate limit, network error, malformed response) into a
    logged warning and an empty result instead of letting it propagate —
    same reasoning as paper_search.py::_safe_query_async: one flaky/slow
    government API should never fail the whole grade request when the
    others might still return useful results. Wrapped in its own
    try/except independent of every other source, per spec.

    Returns `(records, raw_data)` (Phase 21) — `raw_data` is `None` on
    every failure branch below (there's nothing to hand
    resource_parser.py when the request itself never produced a usable
    response) and is otherwise whatever `query_fn` itself returned.
    """
    try:
        return await query_fn(client, config, ingredient_name, max_results)
    except httpx.TimeoutException:
        logger.warning(
            "%s %s timed out (>%ss) for %r — skipping.",
            _LOG_PREFIX,
            source_label,
            _HTTP_TIMEOUT_SECONDS,
            ingredient_name,
        )
        return [], None
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        logger.warning(
            "%s %s returned HTTP %s for %r: %s",
            _LOG_PREFIX,
            source_label,
            status_code,
            ingredient_name,
            exc,
        )
        return [], None
    except httpx.RequestError as exc:
        logger.warning(
            "%s Network error querying %s for %r: %s",
            _LOG_PREFIX,
            source_label,
            ingredient_name,
            exc,
        )
        return [], None
    except (ValueError, KeyError, TypeError, ET.ParseError) as exc:
        # Malformed/unexpected response shape from a source — same
        # "don't let one source's hiccup kill the request" reasoning.
        logger.warning(
            "%s Could not parse %s response for %r: %s",
            _LOG_PREFIX,
            source_label,
            ingredient_name,
            exc,
        )
        return [], None
    except Exception as exc:  # noqa: BLE001 - final safety net, see docstring
        logger.warning(
            "%s Unexpected error querying %s for %r: %s",
            _LOG_PREFIX,
            source_label,
            ingredient_name,
            exc,
        )
        return [], None


# Phase 25: caps how many of resolve_ingredient_search_terms()'s
# prioritized terms a single source will actually try — bounds worst-case
# latency/request count per source (each term attempt is itself already
# bounded by fetch_with_resilience's own _MAX_FETCH_ATTEMPTS retries) even
# for an ingredient with a long synonym list.
_MAX_SEARCH_TERM_ATTEMPTS = 3


async def _safe_query_async(
    query_fn: _QueryFn,
    client: httpx.AsyncClient,
    config: Dict[str, Any],
    source_label: str,
    ingredient_name: str,
    max_results: int,
) -> Tuple[List[VerifiedResourceSchema], Any]:
    """One source's full query lifecycle (Phase 25): tries
    `resolve_ingredient_search_terms(ingredient_name)`'s terms in order —
    common name first, then chemical/Latin/synonym alternates — stopping
    at the first term that produces at least one record, up to
    `_MAX_SEARCH_TERM_ATTEMPTS`. A term producing zero results is not
    necessarily an error — a perfectly healthy request can just have zero
    matches for that specific wording — so this keeps trying the next
    term rather than giving up after the first empty result. Logs a
    single, explicit `SUCCESS` / `FALLBACK_USED` / `FAILED` status line
    for the source once the whole term loop settles, on top of whatever
    provider-specific detail each `_query_*` function already logged for
    its own attempt(s) — per spec.

    Returns `(records, raw_data)` — `raw_data` is whichever term attempt
    actually produced the final `records`; the LAST attempt's raw_data
    when `records` stays empty throughout (there's nothing for
    resource_parser.py to attach conclusions to in that case anyway, but
    keeping it non-None when available costs nothing and preserves
    whatever debugging value the last raw response has).
    """
    search_terms = resolve_ingredient_search_terms(ingredient_name)[:_MAX_SEARCH_TERM_ATTEMPTS]
    if not search_terms:
        search_terms = [ingredient_name]

    records: List[VerifiedResourceSchema] = []
    raw_data: Any = None
    used_term = ingredient_name

    for index, term in enumerate(search_terms):
        records, raw_data = await _run_source_query(
            query_fn, client, config, source_label, term, max_results
        )
        used_term = term
        if records:
            break
        if index + 1 < len(search_terms):
            logger.info(
                "%s %s: no results for %r — retrying with %r.",
                _LOG_PREFIX,
                source_label,
                term,
                search_terms[index + 1],
            )

    if records and used_term.strip().lower() == ingredient_name.strip().lower():
        logger.info(
            "%s %s: SUCCESS (%d resource(s) found for %r).",
            _LOG_PREFIX,
            source_label,
            len(records),
            ingredient_name,
        )
    elif records:
        logger.info(
            "%s %s: FALLBACK_USED (%r yielded %d resource(s)).",
            _LOG_PREFIX,
            source_label,
            used_term,
            len(records),
        )
    else:
        logger.info(
            "%s %s: FAILED (0 resources found for %r after %d search term "
            "attempt(s)).",
            _LOG_PREFIX,
            source_label,
            ingredient_name,
            len(search_terms),
        )

    return records, raw_data


async def _search_all_sources_async(
    ingredient_name: str, max_results_per_source: int
) -> List[Tuple[str, List[VerifiedResourceSchema], Any]]:
    """Fans out every enabled source concurrently over one shared
    httpx.AsyncClient via `asyncio.gather(..., return_exceptions=True)` —
    each source is already individually guarded by `_safe_query_async`
    (so nothing should actually raise), but `return_exceptions=True` is
    kept as a defense-in-depth guarantee: a timeout or error in one
    source's coroutine can never cancel or block the others' in-flight
    requests, per spec.

    The shared client is constructed with `headers=DEFAULT_HEADERS`
    (Phase 25) — set once here, applied to every request any `_query_*`
    function below makes through it, rather than repeated per-call. See
    `DEFAULT_HEADERS`'s own comment for why these headers matter (NIH/NLM
    endpoints in particular reject or throttle requests that look
    script-generated rather than from a real consumer client).

    Unlike its pre-Phase-21 predecessor (`_search_all_records_async`,
    which flattened every source's records into one combined list), this
    returns one `(api_id, records, raw_data)` tuple PER source rather
    than a flattened list — `fetch_verified_resources_for_ingredient`
    below needs `raw_data` grouped by source (not flattened away) so it
    can call `resource_parser.py::parse_resource_conclusions(api_id,
    raw_data)` once per source and apply the result to every
    `VerifiedResourceSchema` record that source contributed.
    """
    configs = _enabled_resource_apis()
    if not configs or not ingredient_name.strip():
        return []

    timeout = httpx.Timeout(_HTTP_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout, headers=DEFAULT_HEADERS) as client:
        tasks = [
            _safe_query_async(
                _SOURCE_QUERY_FUNCTIONS[config["id"]][1],
                client,
                config,
                _SOURCE_QUERY_FUNCTIONS[config["id"]][0],
                ingredient_name,
                max_results_per_source,
            )
            for config in configs
        ]
        results_per_task = await asyncio.gather(*tasks, return_exceptions=True)

    results_by_source: List[Tuple[str, List[VerifiedResourceSchema], Any]] = []
    for config, result in zip(configs, results_per_task):
        api_id = config["id"]
        if isinstance(result, BaseException):
            source_label = _SOURCE_QUERY_FUNCTIONS[api_id][0]
            logger.warning(
                "%s %s: unexpected exception escaped _safe_query_async — treating as "
                "zero results: %s",
                _LOG_PREFIX,
                source_label,
                result,
            )
            continue
        records, raw_data = result
        results_by_source.append((api_id, records, raw_data))
    return results_by_source


def fetch_verified_resources_for_ingredient(
    session: Session,
    ingredient_id: int,
    ingredient_name: str,
    max_results_per_source: int = DEFAULT_MAX_RESULTS_PER_SOURCE,
) -> List[VerifiedResource]:
    """Queries every enabled source in docs/verified_resource_apis.json
    for `ingredient_name`, applies the strict domain allow-list (see
    module docstring), deduplicates against both this batch and what's
    already stored for `ingredient_id`, and persists new
    VerifiedResource rows.

    Stays a synchronous function — internally runs the actual network
    fan-out via `asyncio.run(_search_all_sources_async(...))`, same
    reasoning and same safety guarantee as
    paper_search.py::search_papers_for_ingredient: this is always called
    from inside a worker thread (via FastAPI's `run_in_threadpool`, see
    app/services/grading.py::grade_ingredient's call site), never on the
    event loop thread, so `asyncio.run()` here can't collide with an
    already-running loop.

    Also grades each newly-found resource against
    docs/resource_grading_rubric.json (Phase 8 — see
    app/services/resource_grader.py::grade_resource), one Gemini call per
    resource, sequentially. A per-resource grading failure is caught and
    logged, not raised — that resource is still returned/persisted, just
    permanently ungraded (`grade`/`score`/`reasoning_summary` stay
    `None`) — see module docstring.

    Also (Phase 21) deterministically extracts `extracted_conclusions`/
    `extraction_failure_reason` for every newly-found resource via
    app/services/resource_parser.py::parse_resource_conclusions — no
    Gemini call, no rate limits, executes essentially instantly. Called
    once per source (not once per resulting resource row — see that
    function's own docstring for why) using that source's raw API
    response, which `_search_all_sources_async` now returns alongside the
    parsed records specifically so this can happen right here, at fetch
    time, rather than in a separate later pipeline pass.

    Deliberately `session.flush()`s rather than `session.commit()`s —
    same convention as search_papers_for_ingredient: the caller
    (app/services/grading.py::grade_ingredient) commits once, alongside
    newly-found papers, right after both search steps run.

    Args:
        session: An open SQLModel session.
        ingredient_id: The canonical Ingredient this lookup is for.
        ingredient_name: That Ingredient's `name` — the primary search
            term sent to every source, with automatic per-source retries
            against prioritized chemical/Latin/synonym alternates on a
            zero-result first attempt (Phase 25 — see
            `resolve_ingredient_search_terms`/`_safe_query_async`).
        max_results_per_source: Cap per source.

    Returns:
        The newly-created VerifiedResource rows (already added + flushed,
        so they have ids) — does NOT include rows that already existed
        for this ingredient before this call.
    """
    results_by_source = asyncio.run(
        _search_all_sources_async(ingredient_name, max_results_per_source)
    )

    # Queried unconditionally (even when `results_by_source` below turns
    # out empty) so the debug log at the bottom of this function always
    # reports an accurate total — including the "found nothing new this
    # call, but N were already there from an earlier run" case, which the
    # old early-return (before this row was moved up) would have skipped
    # silently.
    existing = session.exec(
        select(VerifiedResource).where(VerifiedResource.ingredient_id == ingredient_id)
    ).all()
    existing_urls = {resource.url for resource in existing}

    if not results_by_source:
        logger.info(
            "%s Ingredient id=%s (%r) now has %d verified resource(s) total "
            "(%d existing + 0 newly found this call — no source returned "
            "any new results).",
            _LOG_PREFIX,
            ingredient_id,
            ingredient_name,
            len(existing),
            len(existing),
        )
        return []

    new_resources: List[VerifiedResource] = []
    seen_this_batch: set = set()
    for api_id, records, raw_data in results_by_source:
        if not records:
            continue

        # Phase 21: deterministic, zero-Gemini conclusion extraction —
        # run once per source (not once per resulting resource row),
        # since `raw_data` here is that source's one whole API response
        # for this ingredient, shared by every resource this source
        # contributes this call. See resource_parser.py's module
        # docstring for the full "why deterministic, not Gemini"
        # reasoning (this replaces the old Gemini-based Stage 1 step that
        # used to run later, separately, in
        # app/services/paper_analysis_pipeline.py — see that module's own
        # docstring for why it no longer has one).
        # Phase 39: `resource_url` is purely cosmetic (see
        # parse_resource_conclusions's own docstring) — this call is
        # per-SOURCE, shared by every resource `records` contributes this
        # batch, so there's no single "the" URL to attribute a source-level
        # parse to. `records[0].url` (the first/highest-ranked result) is
        # passed just so the `[NIH Extractor]` log line below names an
        # actual live resource rather than only the shared `api_id` string.
        conclusions, failure_reason = parse_resource_conclusions(
            api_id, raw_data, resource_url=records[0].url
        )

        for record in records:
            if record.url in existing_urls or record.url in seen_this_batch:
                continue
            seen_this_batch.add(record.url)

            resource = VerifiedResource(
                ingredient_id=ingredient_id,
                title=record.title,
                publisher=record.publisher,
                url=record.url,
                domain=record.domain,
                summary=record.summary,
                api_id=api_id,
                extracted_conclusions=conclusions,
                extraction_failure_reason=failure_reason,
            )

            # Phase 8: grade this resource against
            # docs/resource_grading_rubric.json before it's ever added to
            # the session — a failure here just means `grade`/`score`/
            # `reasoning_summary` stay at their default `None` (the
            # resource itself is still kept and persisted, domain-
            # verified but ungraded) rather than losing the resource
            # entirely — see module docstring. Distinct from (and
            # unaffected by) the deterministic conclusion extraction
            # above — quality grading is still one Gemini call per
            # resource, same as before this phase.
            try:
                grade_result = grade_resource(
                    {
                        "resource_title": resource.title,
                        "url": resource.url,
                        "publisher": resource.publisher,
                        "page_snippet_or_text": resource.summary,
                    }
                )
            except ResourceGradingError as exc:
                logger.warning(
                    "%s Resource grading failed for %r (%s) — leaving ungraded: %s",
                    _LOG_PREFIX,
                    resource.title,
                    resource.url,
                    exc,
                )
            else:
                resource.grade = grade_result["grade"]
                resource.score = grade_result["total_score"]
                resource.reasoning_summary = grade_result["reasoning_summary"]

            session.add(resource)
            new_resources.append(resource)

    if new_resources:
        session.flush()  # assigns ids without committing — see docstring

    # Debug visibility for the downstream data-flow audit described in
    # app/services/conclusion_grader.py::synthesize_ingredient_summary's
    # own debug lines: confirms, right at the source, how many
    # VerifiedResource rows this ingredient now has in total (existing +
    # newly found this call) — since that function queries VerifiedResource
    # fresh from the DB rather than trusting a possibly-stale in-memory
    # collection, this total is exactly what it will see once this
    # session's flush/commit lands (see this function's own docstring for
    # why this flushes rather than commits).
    logger.info(
        "%s Ingredient id=%s (%r) now has %d verified resource(s) total "
        "(%d existing + %d newly found this call).",
        _LOG_PREFIX,
        ingredient_id,
        ingredient_name,
        len(existing) + len(new_resources),
        len(existing),
        len(new_resources),
    )

    return new_resources
