"""Deterministic, zero-LLM conclusion extraction for VerifiedResource rows
(Phase 21) — replaces the Gemini-based Stage 1 extraction pipeline
(`app/services/resource_extractor.py`, Phase 17/19/20).

**Why this exists.** The Gemini-based approach had three real costs in
production: it was subject to the free tier's requests-per-minute quota
(`429 RESOURCE_EXHAUSTED` — see `app/services/gemini_rate_limit.py`,
built specifically to work around this), it added several seconds of
latency per resource even when it succeeded, and — the most fundamental
issue — it could hallucinate a plausible-sounding dose/claim the source
text never actually stated. Every one of the six configured providers
(`docs/verified_resource_apis.json`) has a well-known, stable response
shape (`pubchem_pug_rest`'s `InformationList.Information`, `usda_fooddata`'s
`foods[].foodNutrients`, etc.) — well-known enough that a handful of
direct JSON key lookups extracts the same category of information Gemini
was asked to, instantly, for free, and without any risk of inventing
something not actually in the payload. Every one of the six configured
providers now has its own dedicated structured parser (Phase 26 promoted
`dailymed_api`, Phase 28 promoted `europe_pmc`, Phase 39 promoted
`medlineplus_api` — the last one still routed through a generic
stringify-and-keyword-match fallback, `_parse_free_text_fallback`, now
retired/unreachable — see that function's own docstring).

**Where this is called from.** `parse_resource_conclusions()` below is
pure — no DB access, no I/O, no external calls of any kind — same "pure
function in, structured result out" design `resource_extractor.py` used.
It is called from `app/services/resource_fetcher.py::fetch_verified_resources_for_ingredient`,
once per enabled source per ingredient fetch, immediately after that
source's raw API response is fetched — NOT from
`app/services/paper_analysis_pipeline.py` (see that module's own
docstring for why the "Stage 1" extraction step it used to run no longer
exists at all: there's nothing left for a later pipeline pass to do once
extraction happens synchronously, deterministically, and for free at
fetch time). This mirrors the same "task named one file, the actual
integration point turned out to be another" situation this codebase has
hit before — see `docs/Architecture.md`'s Phase 19/20 notes on
`IngredientCard.tsx` vs. `StudiesList.tsx`/`VerifiedResourcesList.tsx` for
the precedent.

**One call per source, not per resource.** `raw_data` here is one
source's *entire* raw API response for one ingredient search — not a
single resource's own snippet. A single source query can produce more
than one `VerifiedResource` row (up to `DEFAULT_MAX_RESULTS_PER_SOURCE`
in resource_fetcher.py), and every one of those rows shares the same
underlying raw payload — so `resource_fetcher.py` calls this function
once per source and applies the resulting `(conclusions, failure_reason)`
pair to every resource that source contributed that call, rather than
re-running the same parse once per resulting row.

**Per-provider rules** (dispatched on `api_id`, the `id` field from
`docs/verified_resource_apis.json`):

- `pubchem_pug_rest` — reads `InformationList.Information[].Description`
  (PubChem's compound description payload) — every entry PubChem
  returns, not just the first few, since a compound can have
  descriptions from several data sources (PubChem's own curated summary,
  a Wikipedia mirror, etc.), each potentially covering distinct
  biological-role/toxicity/pharmacology ground. Every conclusion is
  prefixed with that entry's own `Title` (Phase 26 — see below).
- `usda_fooddata` — reads the top `_USDA_MAX_FOOD_MATCHES` (2) food
  matches' `foodNutrients[]` (value/amount + unitName + nutrientName),
  ALL of them (subject to the Phase 26 zero-value filter below), plus a
  `percentDailyValue` conclusion per nutrient when the payload includes
  one. Every conclusion is prefixed with that food's own
  `description`/`brandOwner` (Phase 26).
- `dailymed_api` — (Phase 26, promoted out of the free-text fallback
  below into its own structured parser) reads `data[]` (setid/title/
  published_date/spl_version) — every conclusion is prefixed with that
  entry's own `title` (the actual drug/product name). See `_parse_dailymed`'s
  own docstring for why this deliberately does NOT fabricate an
  "Indicated for ..." style claim the way a generic example might imply.
- `health_canada_lnhpd` — reads `licences[]` (or the raw payload itself,
  if it's already a bare list) for `purpose_name`/`approved_subclause`/
  `dose_subclause`/`risk_statement` — every licence entry, every
  populated clause, each prefixed with that licence's own product/brand
  name when the payload provides one (Phase 26 — see
  `_health_canada_product_label`).
- `medlineplus_api` — (Phase 39, promoted out of the free-text fallback
  below into its own structured parser — `europe_pmc` got the same
  treatment in Phase 28, `dailymed_api` in Phase 26) reads `feed.entry[]`
  (MedlinePlus Connect JSON — the primary path) or each wsearch XML
  `<document>` (the fallback path, only reached when Connect returns zero
  entries — see `resource_fetcher.py::_query_medlineplus`), HTML-strips
  each entry's own summary text, and SENTENCE-SPLITS it into every
  individual statement over a length floor — one conclusion per sentence,
  not one merged excerpt, per the Phase 39 "do not summarize or merge
  separate health topics into a single sentence" requirement. Every
  conclusion is prefixed with that entry's own `title` (Phase 26-style
  topic context — see `_parse_medlineplus`).
- `europe_pmc` — reads `resultList.result[]`'s own `abstractText` (title
  is used ONLY as a per-conclusion label prefix — Phase 42 removed the
  old title-as-abstract fallback entirely, see `_parse_europe_pmc`'s own
  docstring), HTML-entity-unescapes and tag-strips it, then
  SENTENCE-SPLITS it into every individual statement (prioritizing
  inline RESULTS/CONCLUSIONS section text when present), same one-
  conclusion-per-sentence treatment `medlineplus_api` got in Phase 39.
- Any other/unrecognized `api_id` — no rule set exists for it; returns
  `([], <explanit reason>)` rather than raising, same "unknown input
  degrades to an honest empty result, never a crash" philosophy as every
  other fail-open path in this module.

**No cap on result count (Phase 22).** Earlier phases capped every
branch at 4 results, mirroring the old Gemini-based extractor's own
`[:4]` — Phase 22 removed that cap entirely (maximize extraction depth:
pull every RDA/safety/clinical statement a payload actually contains,
not just the first few) or the fallback path's early-exit-after-4-matches
behavior. The only thing still enforced is de-duplication
(`dict.fromkeys`, preserving first-seen order) — an identical string
appearing twice in one payload (e.g. the same nutrient listed under two
different food matches) is still collapsed to one entry, but every
genuinely distinct statement is kept, however many there are.

**Phase 26 — explicit product/subject context + zero-value filtering.**
Fixes two quality problems observed in production output:

1. **Missing subject/product context.** A conclusion like `"Contains 0.0
   UG of Folic acid per standard reference serving"` reads as an
   unattributed, disembodied fact — nothing ties it back to which food,
   drug label, or compound it actually describes. Every structured
   parser below (`_parse_pubchem`/`_parse_usda`/`_parse_dailymed`/
   `_parse_health_canada`) now prefixes every conclusion string with the
   specific product/food/compound name it came from — `"USDA Food
   Reference ('Spinach, raw'): Contains ..."`, `"DailyMed Label
   ('Ascorbic Acid Injection'): ..."`, `"PubChem Compound ('Ascorbic
   Acid'): ..."`, `"Health Canada LNHPD ('...'): ..."` — rather than a
   bare, contextless sentence fragment.
2. **Zero-value/negligible statements.** `_is_positive_number` (used by
   `_parse_usda` for both its `value`/`amount` and `percentDailyValue`
   fields) rejects `None`, non-numeric strings, and any value that
   parses to `<= 0` — a nutrient genuinely measured/reported as zero (or
   a garbage/placeholder value) is dropped rather than surfaced as a
   fake-looking "fact" with no scientific value to the user.

**Phase 28 — universal sanitizer against raw metadata/boilerplate.**
Two further quality problems surfaced in production, distinct from
Phase 26's (which were about *missing* context, not *wrong* content):

1. **Raw API metadata leaking through as a "conclusion."**
   `_parse_free_text_fallback` (used by `europe_pmc` through Phase 27)
   worked by `str()`-ing the *entire* raw payload dict and sentence-
   splitting the result — for Europe PMC, whose response envelope is
   `{"resultList": {"result": [...]}, "hitCount": ..., "nextCursorMark":
   ..., "version": ...}`, that stringified-dict text itself (e.g.
   `"{'version': '6.9', 'hitCount': 72292, 'nextCursorMark': ...}"`) could
   itself get kept as a "sentence" if it happened to contain a keyword
   substring. Fixed by giving `europe_pmc` its own structured parser,
   `_parse_europe_pmc` (below) — it drills explicitly into
   `resultList.result[]` and reads each result's own `abstractText`/
   `title` field, never touching the envelope's pagination/versioning
   keys at all. `europe_pmc` moved from `_FREE_TEXT_PROVIDERS` into
   `_STRUCTURED_PARSERS`; only `medlineplus_api` still uses the
   sentence-splitting fallback.
2. **Generic registration boilerplate masquerading as a finding.**
   `_parse_dailymed`'s Phase 26 design deliberately emitted a factual-
   but-contentless "Structured Product Label on file with the U.S.
   National Library of Medicine" line for every entry, specifically to
   avoid *fabricating* an indication claim the searchable `/spls.json`
   endpoint's payload never actually contains (see that function's own
   docstring). In practice this line is still noise from the user's
   perspective — a label existing on file isn't itself a scientific
   conclusion. `_parse_dailymed` no longer emits it: an entry is now
   only turned into a conclusion when the payload actually contains
   genuine indication/usage text (which the current searchable endpoint
   never does, so this parser now commonly returns `[]` for it — an
   honest empty result, same fail-open philosophy as everywhere else in
   this module, rather than padding output with a boilerplate sentence
   just to have something to show).

Both fixes are backed by a new universal, provider-agnostic safety net:
`is_valid_human_conclusion(text)` (public — used by
`parse_resource_conclusions` below, but plain enough to reuse/test on
its own) rejects any string under 25 characters, anything that looks
like a stringified JSON object/array (`text.strip()` starting with `{`
or `[`), and anything matching one of `BOILERPLATE_PATTERNS` — API
pagination/metadata keys (`hitCount`, `nextPageUrl`, `nextCursorMark`,
a `version':`-style dict key), DailyMed's retired boilerplate phrase,
`SPL Image`/`Set ID:` labels, a raw `application/json` content-type
string, or a bare URL. `parse_resource_conclusions` runs every parser's
output through this filter as a final pass, regardless of which
provider or parser produced it — a genuinely new/misbehaving parser
added in the future gets this same protection for free, rather than
needing its own bespoke cleanup logic. If every candidate conclusion
gets filtered out this way, the result is the same honest `([],
failure_reason)` shape as any other "found nothing" case — see
`_REASON_NO_READABLE_CONCLUSIONS`.

**Phase 30 — general description fallback for keyword/structured
misses.** The Phase 21 per-provider parsers above are precise (they key
off a specific JSON field, or — for `medlineplus_api` — a fixed
`_FALLBACK_KEYWORDS` list) specifically to avoid inventing anything, but
that precision has a real cost: an official payload can easily contain
a genuine, on-topic scientific/biological description that just never
happens to use one of the hardcoded keywords ("dosage", "RDA", "safety
limit", etc.) or land in the exact structured field a provider's parser
keys off — PubChem/MedlinePlus in particular often return general
compound/health-topic prose (mechanism-of-action, biological role,
general health-topic overviews) that's genuinely useful context but
wouldn't satisfy the original keyword/structured checks. Previously that
meant a real, on-topic payload could still end up reported as "no
conclusions extracted."

`extract_general_description_fallback(provider_id, raw_data)` (public,
same "small enough to reuse/test on its own" reasoning as
`is_valid_human_conclusion`) is a second, more permissive extraction
pass — called from `parse_resource_conclusions` below ONLY when the
primary parser (structured or free-text) produced zero conclusions after
dedup + sanitizing, never as a replacement for it. Three tiers, tried in
order, stopping at the first that produces anything:

1. `pubchem_pug_rest` — re-reads `InformationList.Information[].Description`
   (the same field `_parse_pubchem` already reads) with a much lower bar:
   any description over 30 characters, prefixed `"PubChem Reference: "`
   rather than requiring it to have already passed as a full structured
   conclusion.
2. `medlineplus_api` — reads `feed.entry[].summary._value` (falling back
   to `entry[].title._value`) directly from the MedlinePlus Connect JSON
   shape (see `resource_fetcher.py::_parse_medlineplus_connect_json` for
   the same shape), HTML-stripped, prefixed `"MedlinePlus Guidance: "`.
3. **Generic text-splitter, any provider.** If neither provider-specific
   tier above applies or found anything, stringifies the whole raw
   payload, strips obvious JSON punctuation artifacts (braces/brackets/
   quotes/underscores) before sentence-splitting so a stray `{`/`_` from
   a dict key doesn't glue two real words together, and keeps at most the
   first two resulting sentences that independently clear
   `is_valid_human_conclusion` — deliberately capped at 2 (unlike Phase
   22's "no cap" for the precise parsers above) since this tier has no
   field-level guarantee of relevance the way a named JSON key does, only
   sentence-level plausibility.

Every conclusion this function returns is still run through
`is_valid_human_conclusion` before it's returned (both inside this
function, so the guarantee holds even if something calls it directly,
and again — redundantly, harmlessly — by `parse_resource_conclusions`
alongside the primary parser's own output) — this is an easier-to-clear
bar than the primary parsers' field-level precision, not a bypass of the
Phase 28 sanitizer. If a provider's primary parser AND this fallback
both come back empty, `parse_resource_conclusions` still returns the
same honest `([], failure_reason)` shape as before — see
`_REASON_NO_CONCLUSIONS_FOUND`/`_REASON_NO_READABLE_CONCLUSIONS`, both
reworded this phase to reflect that the fallback was also attempted.
"""

from __future__ import annotations

import difflib
import html
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Phase 39 — api_id values whose source is an official NIH/NLM property
# (mirrors resource_fetcher.py::is_nih_domain's domain-based check, applied
# here at the api_id level since this module's functions are pure and don't
# receive a resource's `domain` — only `api_id`/`raw_data`; every one of
# these three api_ids always resolves to an NIH/NLM domain, see
# docs/verified_resource_apis.json). Used only for the verbose
# "[NIH Extractor]" observability logging in parse_resource_conclusions()
# below — every one of these three already had a real per-provider rule set
# before this phase (PubChem/DailyMed were already structured parsers;
# MedlinePlus is promoted to one this phase — see _parse_medlineplus below),
# so this constant doesn't change WHAT gets extracted, only adds a named log
# line confirming how much was.
_NIH_API_IDS: Tuple[str, ...] = ("pubchem_pug_rest", "medlineplus_api", "dailymed_api")

_LOG_PREFIX = "[ResourceParser]"

# Sentence-length floor for the regex/keyword fallback path — mirrors
# resource_extractor.py's old _MIN_SNIPPET_LENGTH_FOR_EXTRACTION
# reasoning: a handful of words isn't a genuine conclusion, just noise
# that happened to contain a keyword.
_MIN_FALLBACK_SENTENCE_LENGTH = 20

# Cap on how many characters of one sentence get kept in the fallback
# path — matches the reference implementation's own cap, to avoid an
# unusually long "sentence" (e.g. a run-on abstract with missed
# punctuation) blowing out the frontend's bullet-point layout.
_MAX_FALLBACK_SENTENCE_LENGTH = 200

# Keywords the regex/keyword fallback path looks for — deliberately broad
# rather than an exact-phrase match, since MedlinePlus/Europe PMC prose
# phrases the same underlying facts many different ways. Expanded
# in Phase 22 (maximize extraction depth) to also catch contraindication/
# mechanism/efficacy/interaction statements, not just dosage/safety ones.
_FALLBACK_KEYWORDS: Tuple[str, ...] = (
    "recommended",
    "dosage",
    "rda",
    "safety",
    "warning",
    "indicated",
    "indication",
    "contraindication",
    "benefit",
    "upper limit",
    "mechanism",
    "efficacy",
    "interaction",
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?]) +")

# Phase 30: both reworded to reflect that extract_general_description_fallback()
# is now also attempted (and also came back empty) before either of these
# is returned — see parse_resource_conclusions() below and the module
# docstring's "Phase 30" paragraph.
_REASON_NO_CONCLUSIONS_FOUND = (
    "No structured nutrient values, RDA limits, or safety keywords were "
    "found in the official payload (or every candidate value was zero, "
    "non-numeric, or otherwise filtered as contentless), and the general "
    "description fallback found no readable descriptive text either."
)
_REASON_NO_RAW_DATA = "No raw API payload was available to parse."
_REASON_UNRECOGNIZED_PROVIDER = (
    "No deterministic parser is configured for this provider yet."
)
# Phase 28: used whenever a provider's parser DID produce candidate
# strings, but every single one of them was filtered out by
# is_valid_human_conclusion() below (raw JSON metadata, a pagination/
# versioning artifact, a bare URL, or generic registration boilerplate)
# — distinct from _REASON_NO_CONCLUSIONS_FOUND above, which covers the
# parser producing zero candidates in the first place. Both are
# "honest empty result" cases from the caller's point of view (see
# VerifiedResource.extraction_failure_reason in app/models/research.py),
# just with a more specific, user-facing explanation for this one.
_REASON_NO_READABLE_CONCLUSIONS = (
    "No readable scientific statements found; provider returned metadata "
    "or generic label filings, and the general description fallback "
    "found no additional descriptive text either."
)

# Phase 28: regex patterns that flag a candidate conclusion string as raw
# API metadata / pagination-envelope noise / generic registration
# boilerplate rather than a genuine, human-readable scientific statement
# — see is_valid_human_conclusion() below and the module docstring's
# "Phase 28" paragraph for the two production failure modes this closes.
BOILERPLATE_PATTERNS: Tuple[str, ...] = (
    r"hitCount",
    r"nextPageUrl",
    r"nextCursorMark",
    r"version['\"]?\s*:",
    r"Structured Product Label on file",
    r"SPL Image",
    r"Set ID:",
    r"application/json",
    r"https?://",
    r"^\{.*\}$",
    r"^\[.*\]$",
    # Phase 30: catches "key : value , key : value"-shaped dict soup —
    # found during testing that extract_general_description_fallback's
    # tier-3 generic text-splitter could turn a metadata-only payload
    # (e.g. DailyMed's bare {"setid": ..., "title": ...} entry, exactly
    # the kind of contentless record Phase 26/28 deliberately stop
    # `_parse_dailymed` from resurfacing) into something like `"data :
    # setid : x , title : Foo"` — long enough and free of every other
    # boilerplate pattern above to otherwise slip through. Two or more
    # `word:`-style labels separated by a comma is a strong signal of
    # serialized key-value pairs, not natural prose — genuine prefixed
    # conclusions in this module (`"USDA Food Reference ('...'): ..."`,
    # `"MedlinePlus Guidance: ..."`, etc.) only ever contain ONE colon,
    # so this never matches them (see is_valid_human_conclusion's own
    # test coverage).
    r"\b[A-Za-z_]+\s*:\s*[^,:]+,\s*[A-Za-z_]+\s*:",
)
_BOILERPLATE_RE: Tuple[re.Pattern, ...] = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in BOILERPLATE_PATTERNS
)

# Phase 28: floor below which a string is too short to be a genuine
# standalone scientific statement, regardless of content — mirrors the
# reasoning behind _MIN_FALLBACK_SENTENCE_LENGTH above, just applied
# universally (to every provider's output, not just the free-text
# fallback path) as part of is_valid_human_conclusion().
_MIN_HUMAN_CONCLUSION_LENGTH = 25

# Shared HTML-tag stripper — used by both _parse_europe_pmc's
# abstractText (Phase 28) and the Phase 30 general description fallback's
# MedlinePlus summary._value (which commonly arrives <div>-wrapped).
#
# Phase 42: tightened from the original `r"<[^>]+>"` to require a letter
# or slash immediately after `<` — i.e. an actual tag opener, not just
# any literal `<`. The original pattern is too permissive for scientific
# prose specifically: text like "p<0.05" or "reduced risk by <50%" (both
# common in abstracts) starts with a literal `<` that isn't a tag at all,
# and `[^>]+` is greedy — it would keep consuming everything up to the
# NEXT real `>` anywhere later in the string (e.g. the closing `>` of an
# unrelated `<b>` tag several sentences later), silently deleting
# everything in between, including real sentence content, section
# headers, and punctuation. Confirmed during Phase 42 testing on
# synthetic Europe PMC abstract text combining both patterns
# ("...(p<0.05). CONCLUSIONS: ...promise as a <b>tracer</b>..." lost the
# entire "). CONCLUSIONS: ...as a" span). Requiring `[A-Za-z]` right
# after the optional `/` still matches every real tag this module
# encounters (`<sup>`, `</sup>`, `<b>`, `<i>`, `<p>`, `<div>`, ...) since
# no real HTML/XML tag name starts with a digit or punctuation, while no
# longer treating a bare numeric/mathematical `<` as a tag opener at all.
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")

# Phase 42: title-similarity floor for the Europe PMC "STRICT TITLE
# GUARD" check below — a candidate sentence whose difflib.SequenceMatcher
# ratio against the paper's own title is at or above this is treated as
# "basically just the title again", not a genuine finding, even if it
# technically differs by a word or trailing punctuation.
_EUROPE_PMC_TITLE_SIMILARITY_THRESHOLD = 0.90


def _clean_html_text(raw_text: Optional[str]) -> str:
    """`html.unescape()` THEN `_HTML_TAG_RE.sub("", ...)` THEN whitespace
    normalization — in that specific order, which matters.

    Phase 42 bugfix. Some providers' text fields (confirmed for Europe
    PMC's `abstractText`, e.g. isotope notation like
    `[<sup>18</sup>F]FDG`) arrive from their upstream XML source already
    HTML-entity-escaped — i.e. the JSON string literally contains the six
    characters `&lt;sup&gt;`, not a real `<` character — rather than
    genuine live tags. `_HTML_TAG_RE` alone can never match that: there's
    no literal `<` for the regex to see, so the escaped tag soup
    (`&lt;sup&gt;18&lt;/sup&gt;F`) leaked straight through to the stored
    conclusion, byte for byte. Calling `html.unescape()` FIRST turns those
    entities into real `<sup>`/`</sup>` characters, which `_HTML_TAG_RE`
    then actually strips, leaving clean `18F`. Doing it in the other
    order (or skipping unescape entirely) reproduces the bug.

    Used by `_parse_europe_pmc` (below) and `_medlineplus_sentences`
    (above `_parse_medlineplus`) — the latter wasn't reported broken, but
    reads from the same class of upstream API response shape (an
    XML-sourced summary field) and had the exact same unescape-ordering
    gap, so it's fixed here too rather than leaving one sibling parser
    with a latent version of a bug just fixed in the other.
    """
    if not isinstance(raw_text, str) or not raw_text:
        return ""
    unescaped = html.unescape(raw_text)
    stripped = _HTML_TAG_RE.sub("", unescaped)
    return re.sub(r"\s+", " ", stripped).strip()


def _is_near_duplicate_of_title(candidate: str, title: str) -> bool:
    """Phase 42 "STRICT TITLE GUARD" checklist item — True if `candidate`
    is exactly the title, or close enough (>= 90% `difflib`
    SequenceMatcher ratio, case-insensitive) that it reads as the title
    restated rather than a genuine finding. Deliberately uses stdlib
    `difflib` rather than pulling in a fuzzy-matching dependency (e.g.
    `rapidfuzz`) — this codebase's convention (see
    `backend/tests/test_conclusion_refine_service.py`'s own docstring on
    the sandbox's missing third-party packages) is to prefer the standard
    library wherever it's genuinely sufficient, and a single similarity
    ratio check is well within what `difflib` is for.
    """
    if not candidate or not title:
        return False
    ratio = difflib.SequenceMatcher(
        None, candidate.strip().lower(), title.strip().lower()
    ).ratio()
    return ratio >= _EUROPE_PMC_TITLE_SIMILARITY_THRESHOLD

# Phase 30: length floor / character cap for the two provider-specific
# tiers of extract_general_description_fallback() below — deliberately a
# lower bar (30, not _MIN_HUMAN_CONCLUSION_LENGTH's 25-with-boilerplate-
# checks) since these are raw field reads, not yet the sanitized-and-
# structured conclusions the primary parsers produce; is_valid_human_conclusion
# still gets the final say on every candidate before it's returned.
_GENERAL_FALLBACK_MIN_LENGTH = 30
_GENERAL_FALLBACK_MAX_CHARS = 250

# Phase 30: the generic text-splitter tier (any provider, last resort)
# keeps at most this many sentences — deliberately capped, unlike the
# precise parsers' uncapped Phase 22 extraction, since a sentence merely
# clearing a keyword-free plausibility check has no field-level
# relevance guarantee behind it.
_GENERIC_FALLBACK_MIN_SENTENCE_LENGTH = 35
_GENERIC_FALLBACK_MAX_SENTENCES = 2

# Phase 30: strips characters that make a stringified dict/list
# unreadable as prose (braces, brackets, quotes, underscores) before the
# generic text-splitter tier sentence-splits the payload — without this,
# a raw key like `"nutrient_name"` would glue into "nutrient name" only
# after the underscore is removed; punctuation that actually matters for
# sentence-splitting (periods/commas) is left untouched.
_JSON_ARTIFACT_RE = re.compile(r"[{}\[\]\"'_]")


def is_valid_human_conclusion(text: Any) -> bool:
    """Phase 28 universal sanitizer — True iff `text` reads like a
    genuine, human-readable scientific/regulatory statement rather than
    raw API metadata, a stringified JSON object/array, a bare URL, or
    generic registration boilerplate (see `BOILERPLATE_PATTERNS` and the
    module docstring's "Phase 28" paragraph for the two concrete
    production failures this was built to close).

    Applied as a final, provider-agnostic filter over every parser's
    output in `parse_resource_conclusions()` below — never raises;
    anything that isn't a non-empty string (including `None`) is treated
    as invalid rather than erroring.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if len(stripped) < _MIN_HUMAN_CONCLUSION_LENGTH:
        return False
    # Reject stringified JSON objects/arrays outright — a real
    # conclusion sentence never starts with a bare "{" or "[".
    if stripped.startswith("{") or stripped.startswith("["):
        return False
    for pattern in _BOILERPLATE_RE:
        if pattern.search(stripped):
            return False
    return True


def _as_dict(raw_data: Any) -> dict:
    """Best-effort coercion to a plain dict for the structured
    (JSON-key-lookup) providers below — returns `{}` (never raises) for
    anything that isn't already a dict, so a provider whose response
    shape doesn't match expectations degrades to "found nothing" via the
    normal empty-conclusions path rather than an unhandled exception.
    """
    return raw_data if isinstance(raw_data, dict) else {}


def _is_positive_number(value: Any) -> bool:
    """Phase 26: True iff `value` can be parsed as a `float` that is
    strictly greater than zero — the shared zero-value/negligible-value
    filter every numeric-measure conclusion below is gated on (currently
    `_parse_usda`'s `value`/`amount` and `percentDailyValue` fields, the
    only numeric-measure fields any structured parser here extracts).
    `None`, an empty/non-numeric string, and `0`/`0.0`/negative values
    all return `False` — never raises, so a malformed numeric field
    degrades to "skip this entry" rather than crashing the parser.
    """
    if value is None:
        return False
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _parse_pubchem(raw_data: Any) -> List[str]:
    """PubChem PUG REST — `{"InformationList": {"Information": [{"Title":
    ..., "Description": ...}, ...]}}` (see
    app/services/resource_fetcher.py::_query_pubchem for the same shape,
    confirmed against a live request when that parser was built).

    Phase 26: every conclusion is now prefixed with that entry's own
    `Title` (the specific compound name PubChem's own data source used —
    "Ascorbic Acid", "Vitamin C", etc.) rather than a generic, unattributed
    "Biological role: ..." fragment — falls back to the literal label
    `"Compound"` only when `Title` itself is missing/blank. The old,
    separate `"Classification: {title}"` conclusion (previously emitted
    per entry purely from `Title` alone) is dropped: once `Title` is the
    prefix on every real conclusion, a standalone line that just restates
    the same name with no additional fact is exactly the kind of
    contentless statement this phase's "no contextless conclusions"
    requirement targets.
    """
    conclusions: List[str] = []
    information = (_as_dict(raw_data).get("InformationList") or {}).get("Information") or []
    if not isinstance(information, list):
        return conclusions
    for info in information:
        if not isinstance(info, dict):
            continue
        title = info.get("Title")
        compound_label = title.strip() if isinstance(title, str) and title.strip() else "Compound"
        description = info.get("Description")
        if isinstance(description, str) and description.strip():
            conclusions.append(f"PubChem Compound ('{compound_label}'): {description.strip()}")
    return conclusions


# Phase 26: top N food matches considered (previously only the single
# highest-ranked match) — a food search can rank a less-specific match
# first even when a more relevant one is right behind it, so pulling
# nutrients from a couple of top candidates (each clearly labeled with
# its own food_title — see `_parse_usda`) gives a better chance at
# genuinely useful data without unbounded growth.
_USDA_MAX_FOOD_MATCHES = 2


def _parse_usda(raw_data: Any) -> List[str]:
    """USDA FoodData Central — `{"foods": [{"description", "brandOwner",
    "foodNutrients": [{"value"/"amount", "unitName", "nutrientName",
    "percentDailyValue"}, ...]}, ...]}` — the top `_USDA_MAX_FOOD_MATCHES`
    (2, Phase 26 — previously only the single first/highest-ranked food)
    matches' nutrients are used, ALL of them per food (no cap, Phase 22),
    plus a separate `percentDailyValue` conclusion per nutrient whenever
    the payload actually includes one (some USDA data types omit it).

    Phase 26 fixes two quality problems:

    - **Missing context.** Every conclusion is now prefixed with that
      food's own `description` (falling back to `brandOwner`, then the
      literal label `"Standard Reference Item"` if neither is present) —
      `"USDA Food Reference ('Spinach, raw'): Contains ..."` — rather
      than a bare `"Contains ..."` fragment with no indication of which
      food it's even describing.
    - **Zero-value noise.** Both the raw value/amount and the
      percentDailyValue are now run through `_is_positive_number` —
      a nutrient reported at `0`/`0.0` (or a non-numeric/garbage value)
      is skipped rather than surfaced as a fake-looking "Contains 0.0 UG
      of X" statement with no scientific value.
    """
    conclusions: List[str] = []
    foods = _as_dict(raw_data).get("foods")
    if not isinstance(foods, list) or not foods:
        return conclusions

    for food in foods[:_USDA_MAX_FOOD_MATCHES]:
        if not isinstance(food, dict):
            continue

        food_title = food.get("description") or food.get("brandOwner") or "Standard Reference Item"

        nutrients = food.get("foodNutrients")
        if not isinstance(nutrients, list):
            continue

        for nutrient in nutrients:
            if not isinstance(nutrient, dict):
                continue

            value = nutrient.get("value")
            if value is None:
                value = nutrient.get("amount")
            unit = nutrient.get("unitName", "")
            name = nutrient.get("nutrientName", "")

            if name and unit and _is_positive_number(value):
                conclusions.append(
                    f"USDA Food Reference ('{food_title}'): Contains {value} "
                    f"{unit} of {name} per standard serving."
                )

            percent_dv = nutrient.get("percentDailyValue")
            if name and _is_positive_number(percent_dv):
                conclusions.append(
                    f"USDA Food Reference ('{food_title}'): {name} provides "
                    f"{percent_dv}% of the Daily Value per standard serving."
                )
    return conclusions


# Phase 26: candidate product/brand-name fields tried, in order, for
# _health_canada_product_label below — LNHPD's real field naming was
# never confirmed live (see _parse_health_canada's own docstring), so
# this mirrors the same plausible-field-name-guessing approach
# resource_fetcher.py's own generic extractor already uses for this
# provider's title/publisher fields.
_HEALTH_CANADA_PRODUCT_NAME_KEYS: Tuple[str, ...] = (
    "product_name",
    "productName",
    "brand_name",
    "brandName",
    "licence_number",
    "licenceNumber",
)


def _health_canada_product_label(item: Dict[str, Any]) -> str:
    """First non-empty string found in `item` across
    `_HEALTH_CANADA_PRODUCT_NAME_KEYS`, or the literal fallback label
    `"Health Canada Licensed Product"` if none of them are present —
    used to prefix every conclusion `_parse_health_canada` emits for this
    licence entry (Phase 26).
    """
    for key in _HEALTH_CANADA_PRODUCT_NAME_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Health Canada Licensed Product"


def _parse_health_canada(raw_data: Any) -> List[str]:
    """Health Canada LNHPD — expected `{"licences": [{"purpose_name",
    "approved_subclause", "dose_subclause", "risk_statement"}, ...]}`,
    but this provider's real response envelope was never confirmed live
    (see resource_fetcher.py::_query_health_canada's own docstring) —
    also tolerates the payload itself already being a bare list of
    licence entries, in case the real API responds that way instead.
    Every licence entry is parsed (not just the first), and every one of
    the four monograph clauses is captured when present (Phase 22 added
    `approved_subclause`, alongside the pre-existing three).

    Phase 26: every conclusion is now prefixed with
    `_health_canada_product_label(item)` — that licence's own product/
    brand name when the payload provides one, or a generic "Health Canada
    Licensed Product" label when it doesn't — per this phase's "always
    prefix with the specific product title" requirement.
    """
    conclusions: List[str] = []
    if isinstance(raw_data, list):
        licences: Any = raw_data
    else:
        licences = _as_dict(raw_data).get("licences")
    if not isinstance(licences, list):
        return conclusions
    for item in licences:
        if not isinstance(item, dict):
            continue
        product_label = _health_canada_product_label(item)
        purpose = item.get("purpose_name")
        if isinstance(purpose, str) and purpose.strip():
            conclusions.append(
                f"Health Canada LNHPD ('{product_label}'): Approved purpose — {purpose.strip()}"
            )
        approved_subclause = item.get("approved_subclause")
        if isinstance(approved_subclause, str) and approved_subclause.strip():
            conclusions.append(
                f"Health Canada LNHPD ('{product_label}'): Approved subclause — "
                f"{approved_subclause.strip()}"
            )
        dose = item.get("dose_subclause")
        if isinstance(dose, str) and dose.strip():
            conclusions.append(
                f"Health Canada LNHPD ('{product_label}'): Authorized dosage — {dose.strip()}"
            )
        risk = item.get("risk_statement")
        if isinstance(risk, str) and risk.strip():
            conclusions.append(
                f"Health Canada LNHPD ('{product_label}'): Safety note — {risk.strip()}"
            )
    return conclusions


def _medlineplus_atom_value(node: Any) -> Optional[str]:
    """Extracts the text content of one MedlinePlus Connect Atom-JSON
    field — either a bare string, or (the more common case in this feed)
    a `{"_value": "...", ...}` dict. Returns `None` for anything else.

    Deliberately a small, local duplicate of
    `resource_fetcher.py::_atom_value` rather than a cross-module import —
    this module's whole design (see module docstring) is to stay a
    self-contained, dependency-free pure-function layer; resource_fetcher.py
    already owns the "how MedlinePlus Connect's JSON is shaped" knowledge
    for its own (different) purpose of building a VerifiedResourceSchema's
    title/summary at fetch time, and this module needs the exact same
    three-line unwrap for a different purpose (sentence-level conclusion
    extraction) at parse time.
    """
    if isinstance(node, str):
        return node.strip() or None
    if isinstance(node, dict):
        value = node.get("_value")
        if isinstance(value, str):
            return value.strip() or None
    return None


_MEDLINEPLUS_UNTITLED_LABEL = "Health Topic"


def _medlineplus_sentences(title: str, summary_text: Optional[str]) -> List[str]:
    """Shared by both `_parse_medlineplus`'s Connect-JSON and wsearch-XML
    branches — HTML-strips `summary_text` and splits it into every
    individual sentence over `_MIN_FALLBACK_SENTENCE_LENGTH`, each
    returned as its own `"MedlinePlus ('{title}'): {sentence}"` string.

    This is the Phase 39 fix for "do NOT summarize or merge separate
    health topics into a single sentence" — one health-topic summary
    genuinely covering several distinct findings (e.g. bone health,
    cardiovascular risk, and a dosage note in the same paragraph) now
    becomes several standalone conclusions, one per sentence, instead of
    being collapsed into whichever single sentence the old keyword
    fallback happened to keep (or losing everything if none of its
    sentences happened to contain a `_FALLBACK_KEYWORDS` term at all).

    Phase 42: HTML-cleaning now goes through the shared `_clean_html_text`
    (unescape-then-strip, not just strip) — see that helper's own
    docstring for the entity-escaped-tag bug this closes here too, not
    just in the Europe PMC parser it was written for.
    """
    if not summary_text:
        return []
    clean_summary = _clean_html_text(summary_text)
    if not clean_summary:
        return []
    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT_RE.split(clean_summary)
        if len(sentence.strip()) > _MIN_FALLBACK_SENTENCE_LENGTH
    ]
    return [f"MedlinePlus ('{title}'): {sentence}" for sentence in sentences]


def _parse_medlineplus_connect(payload: Dict[str, Any]) -> List[str]:
    """MedlinePlus Connect JSON branch of `_parse_medlineplus` below —
    `{"feed": {"entry": [{"title": {"_value": ...}|str, "summary":
    {"_value": "<div>...html...</div>"}|str}, ...]}}`, same shape
    `resource_fetcher.py::_parse_medlineplus_connect_json` parses for its
    own (different) purpose.
    """
    conclusions: List[str] = []
    entries = ((payload.get("feed") or {}).get("entry")) or []
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return conclusions

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = _medlineplus_atom_value(entry.get("title")) or _MEDLINEPLUS_UNTITLED_LABEL
        summary = _medlineplus_atom_value(entry.get("summary"))
        conclusions.extend(_medlineplus_sentences(title, summary))
    return conclusions


def _parse_medlineplus_wsearch(xml_text: str) -> List[str]:
    """MedlinePlus wsearch XML branch of `_parse_medlineplus` below —
    `<nlmSearchResult><list><document url="..."><content
    name="title">...</content><content name="FullSummary">...</content>
    ...</document>...</list></nlmSearchResult>`, same shape
    `resource_fetcher.py::_parse_medlineplus_xml` parses for its own
    (different) purpose. Malformed XML degrades to `[]` (never raises) —
    same fail-open philosophy as every other parser in this module.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    conclusions: List[str] = []
    for document in root.iter("document"):
        title: Optional[str] = None
        summary: Optional[str] = None
        for content in document.findall("content"):
            name = content.get("name")
            text = content.text.strip() if content.text else None
            if name == "title" and text:
                title = _HTML_TAG_RE.sub("", text).strip()
            elif name in ("FullSummary", "snippet") and text and not summary:
                summary = text
        conclusions.extend(_medlineplus_sentences(title or _MEDLINEPLUS_UNTITLED_LABEL, summary))
    return conclusions


def _parse_medlineplus(raw_data: Any) -> List[str]:
    """MedlinePlus (NIH/NLM) — Phase 39, promoted out of the generic
    free-text/keyword fallback (`_parse_free_text_fallback`, now retired —
    see that function's own updated docstring) into its own structured
    parser, mirroring the Phase 26 (DailyMed)/Phase 28 (Europe PMC)
    promotion pattern.

    `raw_data` is EITHER of two real shapes, depending on which of
    `resource_fetcher.py::_query_medlineplus`'s two request paths produced
    it for this fetch — a `dict` (MedlinePlus Connect, the primary path —
    see `_parse_medlineplus_connect` above) or a `str` (the wsearch XML
    fallback, only reached when Connect returned zero entries — see
    `_parse_medlineplus_wsearch` above).

    **The actual Phase 39 fix.** Through Phase 38, MedlinePlus was the one
    remaining provider routed through `_parse_free_text_fallback`, which
    `str()`-ed the *entire* raw payload (envelope included) and kept only
    the handful of sentences that happened to mention a
    `_FALLBACK_KEYWORDS` term — collapsing a health topic's dosage detail,
    mechanism, and several distinct findings into whichever one or two
    sentences passed that filter (or losing the whole entry if none did).
    This function instead reads each entry/document's own summary text
    directly (never the raw envelope) and sentence-splits it into every
    individual statement (see `_medlineplus_sentences`) — the task's "do
    NOT summarize or merge separate health topics into a single sentence;
    create individual, standalone conclusion items" requirement. Every
    conclusion is prefixed `"MedlinePlus ('{title}'): "` (Phase 26-style
    topic context), falling back to the literal label `"Health Topic"`
    only when a title itself is missing/blank.

    Still subject to the shared `is_valid_human_conclusion()` post-filter
    in `parse_resource_conclusions()` below, same as every other
    provider — a stray fragment (e.g. a sentence that's really a section
    heading with no real content) is caught there, not here.
    """
    if isinstance(raw_data, str):
        return _parse_medlineplus_wsearch(raw_data)
    return _parse_medlineplus_connect(_as_dict(raw_data))


def _parse_dailymed(raw_data: Any) -> List[str]:
    """DailyMed `/dailymed/services/v2/spls.json` — `{"metadata": {...},
    "data": [{"setid", "title", "published_date", "spl_version"}, ...]}`
    (see app/services/resource_fetcher.py::_query_dailymed for the same
    shape).

    Phase 26: promoted out of the generic free-text fallback
    (`_parse_free_text_fallback`) into its own structured parser
    specifically so every conclusion can be prefixed with the actual
    drug/product name (`title`) rather than a generic, unattributed
    sentence — per this phase's "explicit product context in every
    conclusion" requirement.

    **Phase 28 — no longer fabricates a generic "on file" placeholder,
    either.** Through Phase 26/27 this function emitted a factual-but-
    contentless "Structured Product Label on file with the U.S. National
    Library of Medicine" line for every entry, specifically to avoid
    *fabricating* an indication claim the searchable `/spls.json` list
    endpoint's payload never actually contains (only label metadata —
    setid/title/published_date/spl_version — never the actual SPL
    section text: INDICATIONS & USAGE, DOSAGE AND ADMINISTRATION,
    WARNINGS/PRECAUTIONS). In production that "on file" line turned out
    to be exactly the kind of registration boilerplate this module now
    filters out everywhere (see `is_valid_human_conclusion` and the
    module docstring's "Phase 28" paragraph) — a label existing on file
    isn't itself a scientific conclusion. This function now only emits a
    conclusion when the payload actually contains genuine indication/
    usage text (checked via a defensive, best-guess set of field names,
    in case a future/differently-shaped DailyMed payload ever includes
    one); an entry with only the metadata fields the current searchable
    endpoint actually returns is skipped entirely, same "an honest empty
    result beats a padded, contentless one" philosophy as every other
    fail-open path in this module — still never inventing text the
    payload doesn't contain.
    """
    conclusions: List[str] = []
    entries = _as_dict(raw_data).get("data")
    if not isinstance(entries, list):
        return conclusions

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        product_label = title.strip()

        # Phase 28: only ever emit a conclusion when the payload itself
        # contains genuine indication/usage prose — never the generic
        # "SPL on file" placeholder this function used to fabricate for
        # every entry regardless of what data was actually present. The
        # current searchable /spls.json endpoint never populates any of
        # these fields (see docstring above), so this loop commonly
        # produces nothing at all for this provider today — an honest
        # empty result, not a crash or a padded fake statement.
        indication_text = (
            entry.get("indication_and_usage")
            or entry.get("indications_and_usage")
            or entry.get("indication")
        )
        if isinstance(indication_text, str) and indication_text.strip():
            conclusions.append(
                f"DailyMed Label ('{product_label}'): {indication_text.strip()}"
            )
    return conclusions


_EUROPE_PMC_UNTITLED_LABEL = "Untitled Study"

# Phase 42 — inline structured-abstract section headers Europe PMC
# results commonly use (e.g. "BACKGROUND: ... METHODS: ... RESULTS: ...
# CONCLUSIONS: ..." all within one abstractText string). Matches an
# ALL-CAPS run of 2-30 letters/spaces/slashes/hyphens immediately
# followed by a colon — covers "RESULTS:", "CONCLUSIONS:",
# "CONCLUSIONS AND RELEVANCE:", "MAIN OUTCOME MEASURES:", etc. A
# heuristic, not a real parser of PMC's underlying structured-abstract
# XML (this module only ever sees the flattened REST search response,
# not that XML) — same "best-effort, never raises, degrades to the
# broader-but-still-correct behavior" philosophy as every other regex in
# this file. False positives (a short all-caps phrase mid-sentence that
# happens to precede a colon) just mean that phrase's text lands in its
# own bucket instead of being merged into a neighboring one — harmless,
# since `_europe_pmc_sentences` below only special-cases a small,
# specific set of section names and falls back to the whole abstract
# whenever no header matches at all.
_EUROPE_PMC_SECTION_HEADER_RE = re.compile(r"\b([A-Z][A-Z/ \-]{1,29}):\s*")

# Section-name stems (matched via substring, not exact-equality — see
# _europe_pmc_sentences below) that the "STRICT Europe PMC Extraction
# Rules" spec asks to prioritize: RESULTS/CONCLUSIONS, including their
# many real-world header variants ("RESULTS AND DISCUSSION:",
# "CONCLUSIONS AND RELEVANCE:", "MAIN FINDINGS:", ...).
_EUROPE_PMC_PRIORITY_SECTION_STEMS: Tuple[str, ...] = ("RESULT", "CONCLUSION", "FINDING")


def _split_europe_pmc_sections(clean_abstract: str) -> Dict[str, str]:
    """Splits an already-HTML-cleaned, structured-abstract-style Europe
    PMC abstract into `{SECTION_NAME: section_text}` by its own inline
    ALL-CAPS section headers (see `_EUROPE_PMC_SECTION_HEADER_RE` above).

    Returns `{}` — not an error — for an abstract that isn't written in
    this structured style at all (common for many journals/older
    entries); `_europe_pmc_sentences` below falls back to sentence-
    splitting the whole abstract in that case.
    """
    matches = list(_EUROPE_PMC_SECTION_HEADER_RE.finditer(clean_abstract))
    if not matches:
        return {}
    sections: Dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group(1).strip().upper()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(clean_abstract)
        section_text = clean_abstract[start:end].strip()
        if section_text:
            sections[name] = section_text
    return sections


def _europe_pmc_sentences(title_label: str, clean_abstract: str) -> List[str]:
    """Splits one Europe PMC result's already-HTML-cleaned abstract text
    into every individual sentence over `_MIN_FALLBACK_SENTENCE_LENGTH`,
    each returned as its own `"Europe PMC ('{title}'): {sentence}"`
    string — same one-conclusion-per-discrete-finding shape as
    `_medlineplus_sentences` above (Phase 39), applied here for the
    task's "Each distinct finding MUST be its own standalone claim
    string" requirement, entirely deterministically (see this module's
    own top-of-file docstring for why it never calls Gemini — that
    reasoning applies here too; this parser stays zero-LLM).

    **Section prioritization.** If `_split_europe_pmc_sections` finds
    real inline section headers, only sentences from sections whose name
    contains a RESULTS/CONCLUSION(S)/FINDINGS stem
    (`_EUROPE_PMC_PRIORITY_SECTION_STEMS`) are used — the task's
    "Prioritize extracting sentences from the RESULTS and CONCLUSIONS
    sections" requirement — discarding BACKGROUND/METHODS/OBJECTIVE-style
    boilerplate setup text. If no such sections are found at all (an
    unstructured abstract, still the common case for many journals), this
    falls back to sentence-splitting the entire cleaned abstract, so an
    unstructured abstract still yields multiple discrete findings rather
    than nothing.

    **STRICT TITLE GUARD.** Any candidate sentence that's a near-duplicate
    of the title (`_is_near_duplicate_of_title`, >=90% similarity) is
    dropped — belt-and-suspenders on top of `_parse_europe_pmc` no longer
    ever using the title as `abstractText`'s fallback value at all (the
    actual root cause of the reported "Europe PMC ('Title'): Title." bug
    — see that function's own docstring).
    """
    sections = _split_europe_pmc_sections(clean_abstract)
    priority_text = " ".join(
        text
        for name, text in sections.items()
        if any(stem in name for stem in _EUROPE_PMC_PRIORITY_SECTION_STEMS)
    )
    source_text = priority_text if priority_text else clean_abstract

    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT_RE.split(source_text)
        if len(sentence.strip()) > _MIN_FALLBACK_SENTENCE_LENGTH
    ]

    return [
        f"Europe PMC ('{title_label}'): {sentence}"
        for sentence in sentences
        if not _is_near_duplicate_of_title(sentence, title_label)
    ]


def _parse_europe_pmc(raw_data: Any) -> List[str]:
    """Europe PMC REST search — `{"resultList": {"result": [{"title",
    "abstractText", ...}, ...]}, "hitCount": ..., "nextCursorMark": ...,
    "version": ...}`.

    Phase 28 — replaces the old approach of routing `europe_pmc` through
    `_parse_free_text_fallback`, which `str()`-ed the *entire* raw
    payload (envelope included) before sentence-splitting it — meaning
    the envelope's own pagination/versioning fields (`hitCount`,
    `nextCursorMark`, `version`) could themselves leak through as a
    "conclusion" if that stringified-dict text happened to contain a
    keyword substring. This parser drills explicitly into
    `resultList.result[]` instead and only ever reads each individual
    result's own `abstractText` — the envelope's own keys are never
    touched.

    **Phase 42 — two real production bugs fixed, plus richer extraction.**

    1. **The "Europe PMC ('Title'): Title." bug.** Through Phase 41 this
       function read `item.get("abstractText") or item.get("title")` —
       when `abstractText` was missing/empty, it silently fell back to
       using the TITLE as the "abstract" text, producing a conclusion
       that was just the title again, prefixed with itself. Fixed by
       removing that fallback entirely: `abstractText` is the only field
       ever used as source text now — the task's "STRICT TITLE GUARD:
       NEVER use the paper title as the scientific conclusion" — and a
       result with no usable `abstractText` is skipped outright (logged,
       not silently dropped — see below), never a discard-or-fallback
       decision made further down the pipeline. As additional
       belt-and-suspenders, `_europe_pmc_sentences` also drops any
       resulting sentence that's a near-duplicate of the title even if
       it came from a genuine (if degenerate) abstract.
    2. **Unescaped HTML entity leakage** (e.g. `[&lt;sup&gt;18&lt;/sup&gt;F]`
       appearing verbatim in a saved conclusion). `abstractText` sometimes
       arrives with its tags HTML-entity-escaped rather than as real `<...>`
       tags — the old `_HTML_TAG_RE.sub("", abstract)` call could never
       match escaped entities (there's no literal `<` character), so they
       passed straight through untouched. Fixed by routing through the new
       shared `_clean_html_text()` helper (unescape, THEN strip tags) — see
       that function's own docstring.

    On top of both fixes, extraction is also now genuinely per-finding
    rather than per-paper: each result's cleaned abstract is split into
    every individual sentence via `_europe_pmc_sentences` (prioritizing
    RESULTS/CONCLUSIONS section text when the abstract has inline section
    headers), so one abstract discussing several distinct findings now
    yields several standalone conclusions instead of one giant blob — the
    task's "extract AS MANY discrete scientific conclusions ... as
    possible" / "Each distinct finding MUST be its own standalone claim
    string" requirements. This is implemented as plain sentence-splitting
    + regex section detection, NOT a new Gemini call — see this module's
    top-of-file docstring for why every parser here is deliberately
    zero-LLM; reintroducing an LLM call specifically for this one
    provider would reopen a design decision (Phase 21) this task didn't
    ask to revisit, and would run a Gemini request per Europe PMC result
    at fetch time with no rate-limit budget allocated for it (see
    `app/services/gemini_rate_limit.py` / Phase 18's per-run paper cap).

    Every conclusion is prefixed with that result's own `title`
    (Phase 26-style product/subject context), falling back to the literal
    label `"Untitled Study"` only when `title` itself is missing/blank.
    Still subject to the shared `is_valid_human_conclusion()` post-filter
    in `parse_resource_conclusions()` below, same as every other provider.

    Per the task's "Post-Parsing Validation Checklist," a result with no
    valid abstract conclusions logs `"[Europe PMC] No abstract
    conclusions found for title: {title}"` and is simply omitted, rather
    than falling back to title-boilerplate.
    """
    conclusions: List[str] = []
    results = (_as_dict(raw_data).get("resultList") or {}).get("result") or []
    if not isinstance(results, list):
        return conclusions
    for item in results:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        title_label = title.strip() if isinstance(title, str) and title.strip() else _EUROPE_PMC_UNTITLED_LABEL

        abstract = item.get("abstractText")
        clean_abstract = _clean_html_text(abstract) if isinstance(abstract, str) else ""
        item_conclusions = _europe_pmc_sentences(title_label, clean_abstract) if clean_abstract else []

        if not item_conclusions:
            logger.info(
                "[Europe PMC] No abstract conclusions found for title: %s",
                title_label,
            )
            continue
        conclusions.extend(item_conclusions)
    return conclusions


def _parse_free_text_fallback(raw_data: Any) -> List[str]:
    """**Retired Phase 39 — no longer called from anywhere.** Through
    Phase 38 this was MedlinePlus's primary parser (the last provider
    still routed through `_FREE_TEXT_PROVIDERS`, now an empty tuple — see
    below); Phase 39 promoted MedlinePlus to its own structured parser,
    `_parse_medlineplus` (above), for the same reason `dailymed_api` was
    promoted in Phase 26 and `europe_pmc` in Phase 28: this function
    stringifies the *entire* raw payload (envelope included) before
    sentence-splitting it, which both risks leaking raw envelope/metadata
    text as a "conclusion" (the exact Phase 28 failure mode
    `_parse_europe_pmc`'s own docstring describes) and — the specific
    Phase 39 complaint — collapses a health topic's several distinct
    findings down to whichever one or two sentences happened to contain a
    `_FALLBACK_KEYWORDS` term, discarding the rest.

    Kept in the module (not deleted) purely for historical/reference
    reasoning, same "don't silently delete, document the retirement"
    convention as `app/services/resource_extractor.py`'s own Phase 21
    deprecation. `_FALLBACK_KEYWORDS`/`_MAX_FALLBACK_SENTENCE_LENGTH` are
    kept alongside it for the same reason and are likewise unused
    elsewhere; `_MIN_FALLBACK_SENTENCE_LENGTH` is the one exception — it's
    reused (on purpose, same sentence-length floor) by
    `_medlineplus_sentences` above, `_parse_medlineplus`'s new shared
    sentence-splitting helper.
    """
    conclusions: List[str] = []
    text_content = str(raw_data)
    sentences = _SENTENCE_SPLIT_RE.split(text_content)
    for sentence in sentences:
        clean_sentence = sentence.strip()
        if len(clean_sentence) > _MIN_FALLBACK_SENTENCE_LENGTH and any(
            keyword in clean_sentence.lower() for keyword in _FALLBACK_KEYWORDS
        ):
            conclusions.append(clean_sentence[:_MAX_FALLBACK_SENTENCE_LENGTH])
    return conclusions


def extract_general_description_fallback(provider_id: Optional[str], raw_data: Any) -> List[str]:
    """Phase 30 — second-chance extraction, tried only when the primary
    per-provider parser (structured or free-text/keyword) came back with
    zero conclusions after dedup + `is_valid_human_conclusion` filtering
    (see `parse_resource_conclusions` below, and the module docstring's
    "Phase 30" paragraph for why this exists: an official payload can
    contain a genuine, on-topic description that just never happens to
    use one of `_FALLBACK_KEYWORDS` or land in a field the structured
    parsers key off).

    Three tiers, tried in order, stopping at the first that produces
    anything:

    1. `pubchem_pug_rest` — re-reads the same `InformationList.
       Information[].Description` field `_parse_pubchem` already reads,
       but with a much lower bar (over `_GENERAL_FALLBACK_MIN_LENGTH`
       characters, no requirement to already read like a full structured
       conclusion) — prefixed `"PubChem Reference: "` rather than
       `_parse_pubchem`'s `"PubChem Compound ('{title}'): "`, so a
       resource's own info modal can tell which extraction path produced
       a given line.
    2. `medlineplus_api` — reads `feed.entry[].summary._value` (falling
       back to `entry[].title._value` when no summary is present) from
       the MedlinePlus Connect JSON shape (see
       `resource_fetcher.py::_parse_medlineplus_connect_json` for the
       same shape) — HTML-stripped via the shared `_HTML_TAG_RE`,
       prefixed `"MedlinePlus Guidance: "`. Gracefully finds nothing (not
       an error) if `raw_data` is instead the wsearch XML fallback text
       (a `str`, not this dict shape) — falls through to tier 3 below.
    3. **Generic text-splitter, any provider (including
       `provider_id=None`/unrecognized).** Only reached if neither tier
       above applies or found anything. Stringifies the whole raw
       payload, strips JSON punctuation artifacts (braces/brackets/
       quotes/underscores — see `_JSON_ARTIFACT_RE`) so a raw dict key
       doesn't glue two real words together, sentence-splits the result,
       and keeps at most `_GENERIC_FALLBACK_MAX_SENTENCES` (2) sentences
       that independently clear `is_valid_human_conclusion` — capped,
       unlike the precise parsers' uncapped Phase 22 extraction, since a
       sentence merely clearing a keyword-free plausibility check has no
       field-level relevance guarantee behind it.

    Every conclusion returned is filtered through
    `is_valid_human_conclusion` before this function returns — a
    self-contained guarantee (per the task's explicit constraint) that
    holds even if something calls this function directly, not just via
    `parse_resource_conclusions`'s own redundant pass over the combined
    result.

    Never raises on a malformed/unexpected `raw_data` shape — degrades to
    `[]` for that tier and falls through to the next one, same fail-open
    philosophy as every other parser in this module. A genuine bug here
    still surfaces as `parse_resource_conclusions`'s own
    `"Parser error processing payload: ..."` result, since that function
    calls this one from inside its own try/except.
    """
    conclusions: List[str] = []

    if provider_id == "pubchem_pug_rest":
        information = (_as_dict(raw_data).get("InformationList") or {}).get("Information")
        if isinstance(information, list):
            for info in information:
                if not isinstance(info, dict):
                    continue
                description = info.get("Description")
                if (
                    isinstance(description, str)
                    and len(description.strip()) > _GENERAL_FALLBACK_MIN_LENGTH
                ):
                    conclusions.append(
                        f"PubChem Reference: {description.strip()[:_GENERAL_FALLBACK_MAX_CHARS]}"
                    )

    elif provider_id == "medlineplus_api":
        feed = _as_dict(raw_data).get("feed")
        entries = _as_dict(feed).get("entry") if isinstance(feed, dict) else None
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue

                summary_field = entry.get("summary")
                if isinstance(summary_field, dict):
                    raw_text: Any = summary_field.get("_value")
                elif isinstance(summary_field, str):
                    raw_text = summary_field
                else:
                    raw_text = None

                if not raw_text:
                    title_field = entry.get("title")
                    if isinstance(title_field, dict):
                        raw_text = title_field.get("_value")
                    elif isinstance(title_field, str):
                        raw_text = title_field

                if isinstance(raw_text, str) and raw_text.strip():
                    clean_text = _HTML_TAG_RE.sub("", raw_text).strip()
                    if len(clean_text) > _GENERAL_FALLBACK_MIN_LENGTH:
                        conclusions.append(
                            f"MedlinePlus Guidance: {clean_text[:_GENERAL_FALLBACK_MAX_CHARS]}"
                        )

    # Tier 3 — generic text-splitter, any provider, only reached if
    # nothing above produced a candidate. Strips HTML/XML tags FIRST
    # (found necessary during testing: MedlinePlus's wsearch XML fallback
    # response, if it ever reaches this tier as a raw `str`, would
    # otherwise leave bare `<tag>` markup sitting in the "sentence" —
    # is_valid_human_conclusion checks for JSON/boilerplate artifacts,
    # not XML markup specifically, so this has to be handled here) before
    # stripping JSON punctuation artifacts and sentence-splitting.
    #
    # Phase 42: explicitly excludes `europe_pmc` — `_parse_europe_pmc`'s
    # own empty result for a paper with no abstractText is a deliberate
    # decision (the STRICT TITLE GUARD: omit rather than fabricate a
    # conclusion from something else), not a "this parser doesn't know
    # how to read this payload yet" gap Tier 3 exists to paper over. Left
    # enabled, this generic stringifier would `str()` europe_pmc's whole
    # `{"resultList": {"result": [{"title": ..., ...}]}, "hitCount": ...}`
    # envelope, and the resulting "resultList : result : title : ..."
    # colon-chain (no comma between the fake key/value pairs, so
    # `is_valid_human_conclusion`'s comma-gated key-value BOILERPLATE
    # pattern doesn't catch it) would slip through as a "conclusion" —
    # precisely the envelope-key-leak failure mode Phase 28 built
    # `_parse_europe_pmc` to eliminate in the first place, and precisely
    # the kind of "boilerplate" the task's own validation checklist says
    # to omit rather than return.
    if not conclusions and raw_data is not None and provider_id != "europe_pmc":
        text_no_tags = _HTML_TAG_RE.sub(" ", str(raw_data))
        clean_str = _JSON_ARTIFACT_RE.sub(" ", text_no_tags)
        sentences = [
            sentence.strip()
            for sentence in _SENTENCE_SPLIT_RE.split(clean_str)
            if len(sentence.strip()) > _GENERIC_FALLBACK_MIN_SENTENCE_LENGTH
        ]
        for sentence in sentences:
            if is_valid_human_conclusion(sentence):
                conclusions.append(sentence[:_GENERAL_FALLBACK_MAX_CHARS])
            if len(conclusions) >= _GENERIC_FALLBACK_MAX_SENTENCES:
                break

    # Final, self-contained guarantee (see docstring above) — redundant
    # for tier 3 (already gated per-sentence), but tiers 1/2 only checked
    # a length floor, not the full sanitizer.
    return [c for c in conclusions if is_valid_human_conclusion(c)]


_STRUCTURED_PARSERS = {
    "pubchem_pug_rest": _parse_pubchem,
    "usda_fooddata": _parse_usda,
    "dailymed_api": _parse_dailymed,
    "health_canada_lnhpd": _parse_health_canada,
    "europe_pmc": _parse_europe_pmc,
    "medlineplus_api": _parse_medlineplus,
}

# Phase 26: dailymed_api moved into _STRUCTURED_PARSERS above (see
# _parse_dailymed's own docstring). Phase 28: europe_pmc moved into
# _STRUCTURED_PARSERS too (see _parse_europe_pmc's own docstring). Phase
# 39: medlineplus_api — the last remaining member — moved in too (see
# _parse_medlineplus's own docstring), leaving this tuple empty. Kept
# (not deleted) so `parse_resource_conclusions`'s `elif api_id in
# _FREE_TEXT_PROVIDERS` branch below stays syntactically meaningful
# rather than needing its own separate removal — it's simply unreachable
# now, same "empty but present, not silently deleted" reasoning as
# `_parse_free_text_fallback`'s own retirement note above.
_FREE_TEXT_PROVIDERS: Tuple[str, ...] = ()


def parse_resource_conclusions(
    api_id: Optional[str], raw_data: Any, resource_url: Optional[str] = None
) -> Tuple[List[str], Optional[str]]:
    """Deterministically parses one source's raw API payload into every
    short, factual conclusion it actually contains about the target
    ingredient — no Gemini call, no network access, executes essentially
    instantly, and (Phase 22) no artificial cap on how many it returns.

    Args:
        api_id: The `id` field from the matching `docs/verified_resource_apis.json`
            entry (e.g. `"pubchem_pug_rest"`) — selects which provider-
            specific rule set below to apply. `None`/unrecognized values
            are handled gracefully (see Returns below), never raised.
        raw_data: That source's raw, already-deserialized API response
            for this ingredient search — a `dict` for the five JSON
            sources, or the raw response text (a `str`) for MedlinePlus's
            real XML-returning endpoint. `None` (no raw payload captured)
            is also handled gracefully.
        resource_url: Optional (Phase 39) — the specific `VerifiedResource.url`
            this parse is for, used only to make the `[NIH Extractor]` log
            line below name the actual resource rather than just the
            shared source-level `api_id`. Purely cosmetic; parsing
            behavior is identical whether or not this is provided (see
            `resource_fetcher.py`'s call site for why a per-source call
            can't always supply one specific URL — one raw payload commonly
            yields several `VerifiedResource` rows, each with its own URL).

    Returns:
        A `(conclusions, failure_reason)` tuple. `conclusions` is ALWAYS
        a valid `list[str]` (never `None`, uncapped, deduplicated
        preserving first-seen order, and — Phase 28 — filtered through
        `is_valid_human_conclusion()` so raw JSON metadata, pagination/
        versioning artifacts, and generic registration boilerplate never
        reach the caller) — even on a parser error, `conclusions` is
        `[]`, never a partial/malformed result. `failure_reason` is
        `None` iff `conclusions` is non-empty; otherwise a short,
        human-readable explanation of why nothing was extracted, suitable
        for direct display in the frontend's resource info modal (see
        `VerifiedResource.extraction_failure_reason`'s docstring in
        app/models/research.py).
    """
    if raw_data is None:
        return [], _REASON_NO_RAW_DATA

    try:
        if api_id in _STRUCTURED_PARSERS:
            conclusions = _STRUCTURED_PARSERS[api_id](raw_data)
        elif api_id in _FREE_TEXT_PROVIDERS:
            conclusions = _parse_free_text_fallback(raw_data)
        else:
            logger.warning(
                "%s No deterministic parser configured for api_id=%r — "
                "returning an empty result.",
                _LOG_PREFIX,
                api_id,
            )
            return [], _REASON_UNRECOGNIZED_PROVIDER

        # Dedup only (preserving first-seen order) — Phase 22 removed the
        # old [:4] cap entirely to maximize extraction depth; an
        # identical string appearing twice is still collapsed to one
        # entry, but every distinct statement is kept, however many.
        conclusions = list(dict.fromkeys(conclusions))
        parser_produced_candidates = bool(conclusions)

        # Phase 28: universal sanitizer pass — applied to every
        # provider's output, structured or free-text alike, as the final
        # gate before anything is returned to the caller. Catches raw API
        # metadata/pagination artifacts and generic registration
        # boilerplate that slipped through an individual parser (see
        # is_valid_human_conclusion() and the module docstring's "Phase
        # 28" paragraph for the two concrete production cases this
        # closes).
        conclusions = [c for c in conclusions if is_valid_human_conclusion(c)]

        # Phase 30: general description fallback — only tried when the
        # primary parser (structured or free-text/keyword) produced
        # nothing usable above, never as a replacement for it. See
        # extract_general_description_fallback's own docstring and the
        # module docstring's "Phase 30" paragraph for the three tiers
        # this attempts. `used_general_fallback` is purely for the log
        # line below — the (conclusions, failure_reason) return shape
        # doesn't otherwise distinguish which path produced the result.
        used_general_fallback = False
        if not conclusions:
            fallback_conclusions = extract_general_description_fallback(api_id, raw_data)
            # Same dedup + sanitizer treatment as the primary parser's
            # own output above — extract_general_description_fallback
            # already self-filters, but re-applying here costs nothing
            # and keeps this one code path as the single source of truth
            # for "what actually gets returned to the caller."
            fallback_conclusions = list(dict.fromkeys(fallback_conclusions))
            fallback_conclusions = [
                c for c in fallback_conclusions if is_valid_human_conclusion(c)
            ]
            if fallback_conclusions:
                conclusions = fallback_conclusions
                used_general_fallback = True

    except Exception as exc:  # noqa: BLE001 - fail-open: a parser bug or
        # an unexpectedly-shaped payload should degrade to an honest
        # empty result with a specific reason, never crash the resource
        # fetch this is called from.
        logger.warning(
            "%s Parser error processing payload for api_id=%r: %s",
            _LOG_PREFIX,
            api_id,
            exc,
        )
        return [], f"Parser error processing payload: {exc}"

    if conclusions:
        if used_general_fallback:
            logger.info(
                "%s FALLBACK_USED — general description fallback recovered "
                "%d conclusion(s) for api_id=%r after the primary parser "
                "found none.",
                _LOG_PREFIX,
                len(conclusions),
                api_id,
            )
        # Phase 39 — verbose, explicitly-named observability logging for
        # NIH/NLM sources specifically, per the task's own requested log
        # line. Deliberately separate from (and in addition to) the
        # generic FALLBACK_USED line above, not a replacement for it —
        # this fires regardless of which path (primary structured parser
        # or general description fallback) produced the result, since the
        # task's concern is "how many discrete conclusions came out of
        # this NIH source," not which internal code path got them there.
        if api_id in _NIH_API_IDS:
            logger.info(
                "[NIH Extractor] Parsed %d discrete conclusion(s) from NIH source: %s",
                len(conclusions),
                resource_url or f"api_id={api_id}",
            )
        return conclusions, None
    if parser_produced_candidates:
        # Phase 28: the parser DID find candidate strings, but every one
        # of them was raw metadata/boilerplate that is_valid_human_conclusion()
        # rejected — a more specific, user-facing explanation than "found
        # nothing at all" (see _REASON_NO_READABLE_CONCLUSIONS's own
        # comment above). Phase 30: the general description fallback was
        # also attempted (see above) and also came back empty — that
        # constant's wording was updated this phase to reflect that.
        return [], _REASON_NO_READABLE_CONCLUSIONS
    # Phase 30: _REASON_NO_CONCLUSIONS_FOUND's wording was also updated
    # this phase — by this point both the primary parser AND the general
    # description fallback have been tried and both came back empty.
    return [], _REASON_NO_CONCLUSIONS_FOUND
