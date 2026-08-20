"""HTML scraping + Gemini extraction fallback for VerifiedResource rows
(Phase 27) — a narrow, targeted fallback used ONLY when
`app/services/resource_parser.py`'s deterministic, zero-LLM extraction
(Phase 21) comes back completely empty for a resource. Most sources'
searchable JSON API payloads are thin (a title, a URL, maybe a short
summary) — this module fetches that resource's own live webpage, strips
it down to plain readable text, and makes ONE Gemini call against that
richer text to see if a genuine conclusion can still be recovered.

**Why this is a fallback, not the primary extraction path.** Fetching
and cleaning a full webpage, then calling Gemini against it, is slower
(a real HTTP round-trip to a third-party site) and costs a Gemini call
per resource — both things `resource_parser.py`'s whole design exists to
avoid for the common case (see that module's own docstring: rate limits,
latency, hallucination risk). This module is deliberately reserved for
the minority of resources where the fast path already came back empty
(`VerifiedResource.extracted_conclusions == []`) — see
`app/services/paper_analysis_pipeline.py`'s own "Phase 27" docstring
section for exactly where/how it's wired in, including the per-run cap
that keeps this from becoming an unbounded source of latency/Gemini
quota pressure.

**Two-step design, deliberately split into two functions:**

1. `fetch_and_clean_html(url, max_chars)` — genuinely `async def`, real
   network I/O via `httpx.AsyncClient`. Reuses
   `app/services/resource_fetcher.py`'s `fetch_with_resilience` (Phase
   25) rather than hand-rolling a second bespoke retry/timeout/backoff
   implementation — this module's fetch gets the exact same hardening
   (isolated `asyncio.wait_for` timeout, exponential backoff on
   500/502/503/504, linear backoff on 429, no crash on an unexpected
   payload) for free, and any future improvement to that shared helper
   benefits this module too without a second edit. Called with
   `parse_mode="raw"` since a webpage is HTML, not JSON — that mode
   returns `response.text` unconditionally on a 200, skipping
   `fetch_with_resilience`'s JSON content-type check entirely (see that
   function's own docstring).
2. `extract_conclusions_from_webpage(url, publisher, ingredient_name)` —
   **deliberately synchronous**, not `async def`, despite the task's own
   reference sketch — matching every other Gemini-calling service in
   this codebase (`paper_grader.py`, `resource_grader.py`,
   `conclusion_grader.py`, `resource_extractor.py`; see
   `app/services/gemini_rate_limit.py`'s module docstring for the full
   "why synchronous" reasoning, restated here since it now also governs
   this module). Internally wraps the one genuinely-async step
   (`fetch_and_clean_html`) in its own `asyncio.run(...)` — same pattern
   `resource_fetcher.py::fetch_verified_resources_for_ingredient` already
   uses to bridge async HTTP fan-out into a sync caller, and safe for the
   exact same reason: this function is always invoked from
   `paper_analysis_pipeline.py::analyze_ingredient_papers`, itself always
   run inside a FastAPI `run_in_threadpool` worker thread (never on the
   event loop), so there is never an already-running loop for
   `asyncio.run()` to collide with. Making the whole function `async def`
   around a Gemini call that's never actually `await`-ed anywhere would
   be a false-async signature — the same reasoning `resource_extractor.py`
   and `gemini_rate_limit.py` already documented for their own
   `client.models.generate_content(...)` call sites.

**Never raises.** Both the short-cleaned-text guard and every genuine
failure mode (fetch failure, malformed HTML, Gemini request/parse
failure) degrade to an empty `[]` result, logged at `warning`/`info`
rather than raised — this module exists specifically to fill in a gap
best-effort, so a failure here should never be able to fail (or even
surface an error for) the grade request that triggered it. Callers
should treat an empty list exactly like "the deterministic parser also
found nothing," not as a distinct error case needing its own handling.

**Every conclusion is guaranteed to carry its `publisher:` prefix.** The
prompt below instructs Gemini to prefix every conclusion itself (per
spec), but per this codebase's established "never trust the model's own
bound-following, enforce it server-side" convention (the same reasoning
every rubric-based grader's score-clamping elsewhere in this codebase
follows), `_ensure_publisher_prefix` re-checks and, if necessary, adds
the prefix itself after Gemini responds — so a downstream consumer can
always rely on the prefix being present, not merely requested.

**`beautifulsoup4` handled safely (not a hard dependency at import
time).** `bs4` is now listed in `backend/requirements.txt`, but this
module's own `import` is wrapped in a `try/except ImportError` — if an
operator hasn't run the install command yet, the rest of this app still
starts and runs normally; only this specific fallback path degrades to
"unavailable" (logged once, per call, at `warning`) rather than crashing
the whole process at import time. See `_BS4_AVAILABLE` below.
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import lru_cache
from typing import List, Optional

import httpx
from pydantic import BaseModel, Field

from google import genai
from google.genai import types

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - see module docstring
    BeautifulSoup = None  # type: ignore[assignment,misc]

from app.core.config import get_settings
from app.services.gemini_rate_limit import call_gemini_with_retry, throttle_gemini_call
from app.services.resource_fetcher import DEFAULT_HEADERS, fetch_with_resilience

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[HTMLExtractor]"

_BS4_AVAILABLE = BeautifulSoup is not None

# Same short-timeout-and-skip philosophy as every other outbound HTTP call
# in this app (see resource_fetcher.py's _HTTP_TIMEOUT_SECONDS) — a slow
# third-party page should never meaningfully stall a grade request. Kept
# slightly more generous than resource_fetcher.py's 5.0s default (a full
# webpage fetch is inherently heavier than a small JSON API response) but
# still bounded — per spec.
_HTML_FETCH_TIMEOUT_SECONDS = 8.0

# HTML tags stripped entirely before text extraction — navigation chrome,
# scripts/styles, and other non-content elements that would otherwise
# pollute the cleaned text Gemini sees (and, per the module docstring's
# "no generic navigation statements" requirement, risk being extracted as
# a fake "conclusion").
_JUNK_TAGS = ("script", "style", "nav", "footer", "header", "aside", "form", "svg")

_WHITESPACE_RE = re.compile(r"\s+")

# Below this many cleaned characters, there simply isn't enough real page
# content to extract a genuine conclusion from (e.g. a mostly-JS page that
# rendered almost nothing server-side, or a fetch that technically
# succeeded but returned a near-empty error/interstitial page) — same
# "don't call Gemini on a handful of words" reasoning as
# resource_extractor.py's own _MIN_SNIPPET_LENGTH_FOR_EXTRACTION (Phase
# 17), just with a per-spec threshold appropriate to a full webpage rather
# than a short API snippet.
_MIN_CLEANED_TEXT_LENGTH_FOR_EXTRACTION = 200

# Cap on how many conclusions this module returns per resource — same
# "don't trust the model's own bound-following" server-side enforcement
# every other Gemini list output in this codebase applies (e.g.
# resource_extractor.py's extracted_conclusions capped at 4).
_MAX_WEBPAGE_CONCLUSIONS = 6

# Phase 39 — NIH resource extraction overhaul. NIH/NLM fact sheets (both
# Health Professional and Consumer versions) are deliberately dense —
# distinct RDA/AI/UL values across several population contexts, a
# separate discrete finding per health topic/outcome under "What does the
# science say?", plus safety/interaction notes — genuinely more than 6
# independent facts is the norm, not the exception, for a good NIH page.
# The generic 6-item cap above exists to bound an UNKNOWN, possibly-thin
# source; for a confirmed NIH/NLM domain (see
# resource_fetcher.py::is_nih_domain) this phase raises it instead of
# leaving genuine, distinct, on-page findings capped away — still a real
# cap (never literally unbounded — "no trust in the model's own bound-
# following" still applies), just sized for what this source category
# actually contains.
_MAX_WEBPAGE_CONCLUSIONS_NIH = 25


async def fetch_and_clean_html(url: str, max_chars: int = 8000) -> Optional[str]:
    """Fetches `url` and converts its HTML into clean, junk-free plain
    text, capped at `max_chars` — or `None` on any failure (unreachable
    host, non-200/non-retryable response, empty/junk-only page, `bs4`
    unavailable). Never raises — see module docstring.

    Uses `resource_fetcher.py::fetch_with_resilience` (Phase 25) for the
    actual HTTP GET rather than a bespoke `httpx` call — see module
    docstring for why reusing that shared, already-hardened helper is
    preferred over duplicating its retry/timeout/backoff logic here.
    """
    if not _BS4_AVAILABLE:
        logger.warning(
            "%s beautifulsoup4 is not installed — HTML fallback extraction "
            "is unavailable for %r. Run `pip install -r requirements.txt` "
            "(or `pip install beautifulsoup4` directly) to enable it.",
            _LOG_PREFIX,
            url,
        )
        return None

    if not url or not url.strip():
        return None

    async with httpx.AsyncClient(follow_redirects=True, headers=DEFAULT_HEADERS) as client:
        raw_html = await fetch_with_resilience(
            client, url, timeout=_HTML_FETCH_TIMEOUT_SECONDS, parse_mode="raw"
        )

    if not raw_html or not raw_html.strip():
        return None

    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(_JUNK_TAGS):
            tag.decompose()
        text = soup.get_text(separator=" ")
        clean_text = _WHITESPACE_RE.sub(" ", text).strip()
    except Exception as exc:  # noqa: BLE001 - a malformed/unusual page
        # shouldn't crash the pipeline that called this — degrade to "no
        # usable text" instead, same fail-open philosophy as every other
        # best-effort step in this app.
        logger.warning("%s Failed to parse HTML for %r: %s", _LOG_PREFIX, url, exc)
        return None

    if not clean_text:
        return None
    return clean_text[:max_chars]


class _WebpageConclusionsSchema(BaseModel):
    """Structured output schema handed to Gemini as `response_schema` —
    a single field, since this prompt asks for exactly one thing (a flat
    list of prefixed conclusion strings), unlike the richer multi-field
    schemas `resource_extractor.py`/`resource_grader.py` use for their
    own, differently-shaped extractions.
    """

    conclusions: List[str] = Field(
        default_factory=list,
        description=(
            "All distinct, factual scientific conclusions, approved "
            "health uses, dosage guidelines (RDAs), and safety/toxicity "
            "warnings this webpage's own text actually states about the "
            "ingredient — each one prefixed with the source name exactly "
            "as instructed. Empty list if the page's text doesn't "
            "contain anything extractable (do not invent one to avoid "
            "returning an empty list)."
        ),
    )


def _build_webpage_prompt(ingredient_name: str, publisher: str, cleaned_text: str) -> str:
    return (
        f"You are analyzing the full official webpage text for the "
        f"dietary ingredient '{ingredient_name}' from official source: "
        f"{publisher}.\n\n"
        f"WEBPAGE CONTENT:\n{cleaned_text}\n\n"
        "INSTRUCTIONS:\n"
        "Extract all distinct, factual scientific conclusions, approved "
        f"health uses, dosage guidelines (RDAs), and safety/toxicity "
        f"warnings regarding '{ingredient_name}'.\n\n"
        "CRITICAL REQUIREMENTS:\n"
        "1. Do NOT include generic navigation statements or unverified "
        "web text — only extract what this specific page's own content "
        "actually states.\n"
        "2. Prefix every extracted conclusion with the source name: "
        f'"{publisher}: [Conclusion Detail]".\n'
        "3. Return your extraction as the required JSON object — an "
        "empty `conclusions` list if nothing extractable is present, "
        "never an invented entry.\n"
    )


def _build_nih_webpage_prompt(ingredient_name: str, publisher: str, cleaned_text: str) -> str:
    """Phase 39 — NIH-specific variant of `_build_webpage_prompt` above,
    used only when the caller (`paper_analysis_pipeline.py`, via
    `is_nih_domain`) confirms this resource's URL resolves to an official
    NIH/NLM domain. Implements the "Strict NIH Extraction & Parsing
    Guidelines" this phase's task spec asked for: exhaustive, per-section
    scanning of an NIH fact sheet (Health Professional or Consumer
    version) rather than the generic prompt's looser "extract what you
    find" instruction — explicitly enumerating the same four sections
    that spec named (recommended intakes, description/mechanism, health
    effects/scientific conclusions, safety/interactions) so Gemini
    doesn't stop after the first section it happens to notice.

    Still returns the same flat `conclusions: List[str]` shape as the
    generic prompt (`_WebpageConclusionsSchema`) — this module only ever
    feeds `VerifiedResource.extracted_conclusions`, a flat list; a
    resource's own `description`/`daily_dosage` for
    `Ingredient.general_info` are derived separately, downstream, by
    `general_info_extractor.py` reading this same `extracted_conclusions`
    list (plus `VerifiedResource.summary`) once the resource is graded
    A/B — see that module's `_build_candidates`. There's no separate
    "general_info" schema to fill in here; asking for exhaustive,
    individually-itemized dosage/description/health-effect/safety
    statements in THIS list is what makes them available to that
    downstream step at all.
    """
    return (
        "You are extracting scientific evidence from an official NIH "
        "(National Institutes of Health) resource page for the dietary "
        f"ingredient '{ingredient_name}', published by: {publisher}.\n\n"
        f"PAGE CONTENT:\n{cleaned_text}\n\n"
        "Your objective is to extract AS MANY DISCRETE, INDIVIDUAL "
        "CONCLUSIONS AS POSSIBLE without omitting any specific finding, "
        "dosage, or context this page actually states. Explicitly scan "
        "for and extract from ALL of the following sections, if present:\n\n"
        "1. RECOMMENDED INTAKES / DAILY DOSAGE — exact RDA (Recommended "
        "Dietary Allowance), AI (Adequate Intake), and UL (Tolerable "
        "Upper Intake Level) values with precise units (mg, mcg, IU), "
        "each as its own conclusion, including the specific population "
        "context (e.g. healthy adults, pregnant individuals, elderly, "
        "smokers) whenever the page states one.\n"
        "2. DESCRIPTION & MECHANISM — the biological definition, natural "
        "form, and biochemical/physiological function.\n"
        "3. HEALTH EFFECTS & SCIENTIFIC CONCLUSIONS (\"What does the "
        "science say?\") — every distinct health outcome or biological "
        "marker this page discusses (e.g. cardiovascular disease, bone "
        "health, cognition, immune function, cancer, diabetes) MUST "
        "become its OWN separate, standalone conclusion item — do NOT "
        "summarize or merge multiple health topics into a single "
        "sentence. Include specific context in the text itself where "
        "the page provides it (e.g. \"In postmenopausal women...\", \"At "
        "doses above 500mg...\", \"Reduces risk of X by Y%...\").\n"
        "4. SAFETY, SIDE EFFECTS & INTERACTIONS — toxicity thresholds, "
        "side effects at high doses, and drug/nutrient interactions, "
        "each as its own discrete safety conclusion.\n\n"
        "CRITICAL REQUIREMENTS:\n"
        "1. Do NOT include generic navigation statements or unverified "
        "web text — only extract what this specific page's own content "
        "actually states; never invent a value the page doesn't contain.\n"
        "2. Prefix every extracted conclusion with the source name: "
        f'"{publisher}: [Conclusion Detail]".\n'
        "3. Return your extraction as the required JSON object — an "
        "empty `conclusions` list if nothing extractable is present, "
        "never an invented entry.\n"
    )


def _ensure_publisher_prefix(conclusion: str, publisher: str) -> str:
    """Guarantees `conclusion` starts with `"{publisher}: "` — see module
    docstring's "never trust the model's own bound-following" note.
    Case-insensitively checks whether Gemini already complied (so a
    correctly-prefixed conclusion isn't double-prefixed) before
    prepending.
    """
    stripped = conclusion.strip()
    publisher_stripped = publisher.strip()
    if not publisher_stripped:
        return stripped
    if stripped.lower().startswith(f"{publisher_stripped.lower()}:"):
        return stripped
    return f"{publisher_stripped}: {stripped}"


@lru_cache
def _get_client() -> genai.Client:
    """Cached Gemini client — separate `@lru_cache` entry from every
    other Gemini-using service's own `_get_client` (paper_grader.py,
    resource_grader.py, conclusion_grader.py, resource_extractor.py,
    research_keywords.py), same "one client per module" reasoning as
    those.
    """
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key)


def extract_conclusions_from_webpage(
    url: str, publisher: str, ingredient_name: str, *, is_nih: bool = False
) -> List[str]:
    """Fetches `url`'s live webpage, cleans it, and asks Gemini to
    extract every distinct factual conclusion it actually contains about
    `ingredient_name` — the Phase 27 HTML fallback (see module
    docstring). Deliberately synchronous (see module docstring for why),
    and NEVER raises: any failure at any stage (fetch, parse, short
    text, Gemini request, schema mismatch) returns `[]`, logged but not
    propagated — this is a best-effort enrichment step, not a required
    one.

    Args:
        url: The VerifiedResource's own `url` — the live page fetched.
        publisher: The VerifiedResource's `publisher` — used both in the
            Gemini prompt and as the prefix guaranteed on every returned
            conclusion (see `_ensure_publisher_prefix`).
        ingredient_name: The ingredient this extraction is for.
        is_nih: Phase 39 — True when the caller (`paper_analysis_pipeline.py`,
            via `resource_fetcher.py::is_nih_domain(resource.domain)`) has
            confirmed `url` resolves to an official NIH/NLM domain. Swaps
            in `_build_nih_webpage_prompt`'s exhaustive, per-section
            extraction instructions (see that function's own docstring)
            in place of the generic `_build_webpage_prompt`, and raises
            the result cap from `_MAX_WEBPAGE_CONCLUSIONS` (6) to
            `_MAX_WEBPAGE_CONCLUSIONS_NIH` (25) — an NIH/NLM fact sheet
            genuinely contains more independent, extractable facts than
            the generic cap was sized for (see that constant's own
            docstring). Defaults to `False` so every other/non-NIH caller
            (there are none today — this is currently only ever called
            for `is_nih_domain`-verified resources per the pipeline
            wiring, but the flag defaults safely regardless) keeps the
            original generic behavior unchanged.

    Returns:
        A `list[str]` of publisher-prefixed conclusions — `[]` (never
        `None`) whenever nothing could be extracted for any reason,
        capped (at `_MAX_WEBPAGE_CONCLUSIONS_NIH` when `is_nih=True`,
        `_MAX_WEBPAGE_CONCLUSIONS` otherwise) and de-duplicated
        (preserving first-seen order), same conventions as every other
        Gemini-derived conclusion list in this codebase.
    """
    if not url or not url.strip() or not publisher or not ingredient_name:
        return []

    try:
        cleaned_text = asyncio.run(fetch_and_clean_html(url))
    except Exception as exc:  # noqa: BLE001 - best-effort fallback, see docstring
        logger.warning("%s Failed to fetch/clean %r: %s", _LOG_PREFIX, url, exc)
        return []

    if not cleaned_text or len(cleaned_text) < _MIN_CLEANED_TEXT_LENGTH_FOR_EXTRACTION:
        logger.info(
            "%s Skipping Gemini extraction for %r — cleaned page text is "
            "too short (%d character(s), need >= %d) to extract anything "
            "meaningful.",
            _LOG_PREFIX,
            url,
            len(cleaned_text or ""),
            _MIN_CLEANED_TEXT_LENGTH_FOR_EXTRACTION,
        )
        return []

    client = _get_client()
    settings = get_settings()
    prompt_builder = _build_nih_webpage_prompt if is_nih else _build_webpage_prompt
    prompt = prompt_builder(ingredient_name, publisher, cleaned_text)
    max_conclusions = _MAX_WEBPAGE_CONCLUSIONS_NIH if is_nih else _MAX_WEBPAGE_CONCLUSIONS

    def _call_gemini():
        throttle_gemini_call()
        return client.models.generate_content(
            model=settings.gemini_model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_WebpageConclusionsSchema,
            ),
        )

    try:
        response = call_gemini_with_retry(
            _call_gemini, label=f"HTML fallback extraction for {url!r}"
        )
    except Exception as exc:  # noqa: BLE001 - best-effort fallback, see docstring
        logger.warning("%s Gemini extraction request failed for %r: %s", _LOG_PREFIX, url, exc)
        return []

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, _WebpageConclusionsSchema):
        raw_text = getattr(response, "text", None)
        if not raw_text:
            logger.warning("%s Gemini returned an empty response for %r.", _LOG_PREFIX, url)
            return []
        try:
            parsed = _WebpageConclusionsSchema.model_validate_json(raw_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s Gemini response did not match the expected schema for %r: %s",
                _LOG_PREFIX,
                url,
                exc,
            )
            return []

    conclusions = [
        _ensure_publisher_prefix(item, publisher) for item in parsed.conclusions if item and item.strip()
    ]
    # Dedup (preserving first-seen order) + cap — same conventions as
    # every other Gemini-derived conclusion list in this codebase.
    conclusions = list(dict.fromkeys(conclusions))[:max_conclusions]

    if conclusions:
        logger.info(
            "%s SUCCESS — extracted %d conclusion(s) from %r via HTML fallback.",
            _LOG_PREFIX,
            len(conclusions),
            url,
        )
        # Phase 39 — verbose, explicitly-named observability logging for
        # confirmed NIH/NLM sources, per the task's own requested log
        # line. Additive to (not a replacement for) the generic SUCCESS
        # line above.
        if is_nih:
            logger.info(
                "[NIH Extractor] Parsed %d discrete conclusion(s) from NIH source: %s",
                len(conclusions),
                url,
            )
    else:
        logger.info(
            "%s No conclusions extracted from %r via HTML fallback (page fetched "
            "and parsed successfully, but Gemini found nothing extractable).",
            _LOG_PREFIX,
            url,
        )
    return conclusions
