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
(`_search_all_records_async`) over a single shared `httpx.AsyncClient` —
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

# USDA FoodData Central's checked-in DEMO_KEY (see
# docs/verified_resource_apis.json) is shared/rate-limited across every
# DEMO_KEY user worldwide — operators can set a real, higher-quota key via
# this environment variable without editing the checked-in config file.
_USDA_API_KEY_ENV_VAR = "USDA_API_KEY"

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
# search for, and a max-results cap, returns the verified resources found.
# Async so every enabled source can be queried concurrently under one
# asyncio.gather (see _search_all_records_async) — same convention as
# paper_search.py's `_QueryFn`.
_QueryFn = Callable[
    [httpx.AsyncClient, Dict[str, Any], str, int], Awaitable[List[VerifiedResourceSchema]]
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

# Small, hand-curated table of common supplement-label names -> their
# primary chemical/systematic name, used only to retry a source once when
# its first query (the common name) returns zero results — see module
# docstring. Deliberately NOT exhaustive or a general name-resolution
# service: an ingredient not listed here just doesn't get a retry.
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


def _fallback_name_for(ingredient_name: str) -> Optional[str]:
    """Returns the known chemical/systematic name for `ingredient_name`
    (case-insensitive lookup against `_CHEMICAL_NAME_FALLBACKS`), or
    `None` if there isn't one — either because the ingredient isn't in
    the table, or because the table's value is identical to the input
    (nothing meaningfully different to retry with).
    """
    fallback = _CHEMICAL_NAME_FALLBACKS.get(ingredient_name.strip().lower())
    if fallback and fallback.strip().lower() != ingredient_name.strip().lower():
        return fallback
    return None


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
) -> List[VerifiedResourceSchema]:
    """Shared query implementation for sources without their own precise
    parser — as of this phase, only `health_canada_lnhpd` (see module
    docstring). `publisher_fallback` is bound per-source via
    functools.partial in _SOURCE_QUERY_FUNCTIONS below.
    """
    url, params = _resolve_endpoint(config, ingredient_name)
    response = await client.get(url, params=params)
    response.raise_for_status()
    payload = response.json()
    return _extract_generic_records(payload, publisher_fallback, max_results)


# --- Precise parser: PubChem PUG REST (well-known, stable JSON shape) ---


async def _query_pubchem(
    client: httpx.AsyncClient, config: Dict[str, Any], ingredient_name: str, max_results: int
) -> List[VerifiedResourceSchema]:
    """NIH PubChem PUG REST `.../compound/name/{name}/description/JSON` —
    a single JSON call returning `{"InformationList": {"Information":
    [{"CID", "Title", "Description", "DescriptionSourceName",
    "DescriptionURL"}, ...]}}`, one entry per data source PubChem has a
    description from for that compound (there's often more than one —
    e.g. PubChem's own curated summary plus a Wikipedia mirror — which is
    exactly why this still needs the domain filter below rather than
    trusting every entry).
    """
    url, params = _resolve_endpoint(config, ingredient_name)
    response = await client.get(url, params=params)
    response.raise_for_status()
    payload = response.json()

    information = ((payload.get("InformationList") or {}).get("Information")) or []

    records: List[VerifiedResourceSchema] = []
    for info in information:
        if len(records) >= max_results:
            break

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

        compound_title = info.get("Title") or ingredient_name
        records.append(
            VerifiedResourceSchema(
                title=f"PubChem Compound Summary — {compound_title}",
                publisher=info.get("DescriptionSourceName") or "National Institutes of Health (PubChem)",
                url=source_url,
                domain=domain,
                summary=info.get("Description") or None,
            )
        )

    return records


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


async def _query_medlineplus(
    client: httpx.AsyncClient, config: Dict[str, Any], ingredient_name: str, max_results: int
) -> List[VerifiedResourceSchema]:
    """Queries the MedlinePlus free-text health topic search
    (wsearch.nlm.nih.gov) and parses its response, handling both the XML
    it actually returns and, defensively, JSON in case the configured
    endpoint is ever pointed at a JSON-emitting variant — per the
    requirement to "parse both JSON and XML responses cleanly, extracting
    topic title and URL."
    """
    url, params = _resolve_endpoint(config, ingredient_name)
    response = await client.get(url, params=params)
    response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    body = response.text.strip()

    if "json" in content_type.lower() or body.startswith("{") or body.startswith("["):
        payload = response.json()
        return _extract_generic_records(
            payload, "National Library of Medicine (MedlinePlus)", max_results
        )

    return _parse_medlineplus_xml(body, max_results)


# --- Precise parser: USDA FoodData Central `/foods/search` ---


async def _query_usda(
    client: httpx.AsyncClient, config: Dict[str, Any], ingredient_name: str, max_results: int
) -> List[VerifiedResourceSchema]:
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
    `_USDA_API_KEY_ENV_VAR`.
    """
    url, params = _resolve_endpoint(config, ingredient_name)
    params = dict(params)
    env_api_key = os.environ.get(_USDA_API_KEY_ENV_VAR)
    if env_api_key:
        params["api_key"] = env_api_key
    elif "api_key" not in params:
        params["api_key"] = "DEMO_KEY"

    response = await client.get(url, params=params)
    if response.status_code == 403:
        # Distinct, explicit log line per the "USDA: 403 error - invalid
        # key" example — raised as an HTTPStatusError so
        # _safe_query_async's shared handler still logs/absorbs it, but
        # with a clearer message than the generic HTTP-status branch
        # would produce on its own.
        logger.warning(
            "%s USDA FoodData Central: 403 error - invalid or rate-limited api_key.",
            _LOG_PREFIX,
        )
    response.raise_for_status()
    payload = response.json()

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

    return records


# --- Precise parser: DailyMed `/spls.json?drug_name=...` ---


async def _query_dailymed(
    client: httpx.AsyncClient, config: Dict[str, Any], ingredient_name: str, max_results: int
) -> List[VerifiedResourceSchema]:
    """DailyMed `/dailymed/services/v2/spls.json?drug_name=...` — real
    response shape (confirmed against NLM's own API documentation):
    `{"metadata": {...}, "data": [{"setid": ..., "title": ...,
    "published_date": ..., "spl_version": ...}, ...]}`. The response
    itself has no direct web link either — this builds the canonical SPL
    detail page URL from `setid`
    (`https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}`),
    always a `dailymed.nlm.nih.gov` (`.gov`) URL.
    """
    url, params = _resolve_endpoint(config, ingredient_name)
    response = await client.get(url, params=params)
    response.raise_for_status()
    payload = response.json()

    entries = payload.get("data") or []
    records: List[VerifiedResourceSchema] = []
    for entry in entries:
        if len(records) >= max_results:
            break

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

    return records


# --- Precise parser: Europe PMC (open-access monographs/reviews) ---


async def _query_europe_pmc(
    client: httpx.AsyncClient, config: Dict[str, Any], ingredient_name: str, max_results: int
) -> List[VerifiedResourceSchema]:
    """Europe PMC REST search — builds the query as
    `"{ingredient_name} AND (monograph OR review)"` per spec (rather than
    a bare ingredient-name search) to target official review/monograph
    content specifically, and keeps only `isOpenAccess == "Y"` results
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
    params["query"] = f"{ingredient_name} AND (monograph OR review)"
    params["pageSize"] = max(max_results * 2, max_results)

    response = await client.get(endpoint_url, params=params)
    response.raise_for_status()
    payload = response.json()

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

    return records


# --- Health Canada LNHPD — schema-tolerant (see docstring) ---


async def _query_health_canada(
    client: httpx.AsyncClient, config: Dict[str, Any], ingredient_name: str, max_results: int
) -> List[VerifiedResourceSchema]:
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
) -> List[VerifiedResourceSchema]:
    """Runs one source query for one search term, converting any failure
    (timeout, rate limit, network error, malformed response) into a
    logged warning and an empty result instead of letting it propagate —
    same reasoning as paper_search.py::_safe_query_async: one flaky/slow
    government API should never fail the whole grade request when the
    others might still return useful results. Wrapped in its own
    try/except independent of every other source, per spec.
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
        return []
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
        return []
    except httpx.RequestError as exc:
        logger.warning(
            "%s Network error querying %s for %r: %s",
            _LOG_PREFIX,
            source_label,
            ingredient_name,
            exc,
        )
        return []
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
        return []
    except Exception as exc:  # noqa: BLE001 - final safety net, see docstring
        logger.warning(
            "%s Unexpected error querying %s for %r: %s",
            _LOG_PREFIX,
            source_label,
            ingredient_name,
            exc,
        )
        return []


async def _safe_query_async(
    query_fn: _QueryFn,
    client: httpx.AsyncClient,
    config: Dict[str, Any],
    source_label: str,
    ingredient_name: str,
    max_results: int,
) -> List[VerifiedResourceSchema]:
    """One source's full query lifecycle: try the ingredient's common
    name first; if that comes back empty (not necessarily an error — a
    perfectly healthy request can just have zero matches) and a known
    chemical/systematic name exists for it (`_fallback_name_for`), retry
    once with that name before giving up. Logs an explicit per-provider
    status line on every outcome (success count, fallback attempt,
    or — via `_run_source_query` — the specific failure reason), per
    spec.
    """
    records = await _run_source_query(
        query_fn, client, config, source_label, ingredient_name, max_results
    )

    if not records:
        fallback_name = _fallback_name_for(ingredient_name)
        if fallback_name:
            logger.info(
                "%s %s: no results for %r — retrying with chemical name %r.",
                _LOG_PREFIX,
                source_label,
                ingredient_name,
                fallback_name,
            )
            records = await _run_source_query(
                query_fn, client, config, source_label, fallback_name, max_results
            )

    if records:
        logger.info(
            "%s %s: %d resource(s) found for %r.",
            _LOG_PREFIX,
            source_label,
            len(records),
            ingredient_name,
        )
    else:
        logger.info(
            "%s %s: 0 resources found for %r.",
            _LOG_PREFIX,
            source_label,
            ingredient_name,
        )

    return records


async def _search_all_records_async(
    ingredient_name: str, max_results_per_source: int
) -> List[VerifiedResourceSchema]:
    """Fans out every enabled source concurrently over one shared
    httpx.AsyncClient via `asyncio.gather(..., return_exceptions=True)` —
    each source is already individually guarded by `_safe_query_async`
    (so nothing should actually raise), but `return_exceptions=True` is
    kept as a defense-in-depth guarantee: a timeout or error in one
    source's coroutine can never cancel or block the others' in-flight
    requests, per spec. Flattens the results, treating any (unexpected)
    exception object that does slip through as "that source contributed
    nothing" rather than propagating it.
    """
    configs = _enabled_resource_apis()
    if not configs or not ingredient_name.strip():
        return []

    timeout = httpx.Timeout(_HTTP_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
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

    all_records: List[VerifiedResourceSchema] = []
    for config, result in zip(configs, results_per_task):
        if isinstance(result, BaseException):
            source_label = _SOURCE_QUERY_FUNCTIONS[config["id"]][0]
            logger.warning(
                "%s %s: unexpected exception escaped _safe_query_async — treating as "
                "zero results: %s",
                _LOG_PREFIX,
                source_label,
                result,
            )
            continue
        all_records.extend(result)
    return all_records


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
    fan-out via `asyncio.run(_search_all_records_async(...))`, same
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

    Deliberately `session.flush()`s rather than `session.commit()`s —
    same convention as search_papers_for_ingredient: the caller
    (app/services/grading.py::grade_ingredient) commits once, alongside
    newly-found papers, right after both search steps run.

    Args:
        session: An open SQLModel session.
        ingredient_id: The canonical Ingredient this lookup is for.
        ingredient_name: That Ingredient's `name` — the search term sent
            to every source (with an automatic chemical-name retry per
            source on a zero-result first attempt — see
            `_fallback_name_for`).
        max_results_per_source: Cap per source.

    Returns:
        The newly-created VerifiedResource rows (already added + flushed,
        so they have ids) — does NOT include rows that already existed
        for this ingredient before this call.
    """
    all_records = asyncio.run(
        _search_all_records_async(ingredient_name, max_results_per_source)
    )
    if not all_records:
        return []

    existing = session.exec(
        select(VerifiedResource).where(VerifiedResource.ingredient_id == ingredient_id)
    ).all()
    existing_urls = {resource.url for resource in existing}

    new_resources: List[VerifiedResource] = []
    seen_this_batch: set = set()
    for record in all_records:
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
        )

        # Phase 8: grade this resource against
        # docs/resource_grading_rubric.json before it's ever added to the
        # session — a failure here just means `grade`/`score`/
        # `reasoning_summary` stay at their default `None` (the resource
        # itself is still kept and persisted, domain-verified but
        # ungraded) rather than losing the resource entirely — see
        # module docstring.
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

    return new_resources
