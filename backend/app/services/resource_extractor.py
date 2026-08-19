"""Gemini-backed per-resource structured claims extraction — Stage 1 of
the Two-Stage Extraction Pipeline (Phase 17).

**Why this exists.** Feeding Gemini one single prompt that mixed dense,
information-rich peer-reviewed paper abstracts alongside short, thin
verified-resource snippets caused a "lost-in-the-middle" effect: the
model consistently favored the academic abstracts and effectively
ignored the web resources, even when the resource text was explicitly
included and instructed to be cited (see
app/services/conclusion_grader.py's module docstring, and the Phase 16
audit section of docs/Architecture.md, for the investigation that led
here). Dumping raw, uneven-length snippets into one shared prompt meant
the two source types never competed on equal footing.

**The fix: extract each resource's claims independently, first.** This
module's `extract_claims_from_resource()` is called once per
VerifiedResource (app/models/research.py), before the final synthesis
call — see app/services/paper_analysis_pipeline.py::analyze_ingredient_papers's
Stage 1 step, which calls this for every resource still missing
`extracted_data` and persists the result onto that column. By the time
Stage 2 (app/services/conclusion_grader.py::synthesize_ingredient_summary)
runs, every resource contributes a compact, uniformly-structured,
already-distilled block (`official_stance`/`recommended_dose`/
`upper_limit_warning`/`key_takeaways`) — comparable in information
density to a paper's own already-extracted PaperConclusion rows, rather
than a raw snippet of wildly varying length and specificity.

Mirrors app/services/resource_grader.py's Gemini usage pattern almost
exactly (cached client, structured `response_schema` output, `.parsed`
with a raw-text fallback) — same reasoning, restated here: keeping every
Gemini-calling module in this codebase on one consistent calling
convention. One deliberate structural difference from the task spec that
requested this module: `extract_claims_from_resource()` is a
**synchronous** function, not `async def`. Every other Gemini-calling
service in this codebase (`paper_grader.py`, `resource_grader.py`,
`conclusion_grader.py`, `research_keywords.py`) calls
`client.models.generate_content` synchronously and is invoked from a
synchronous context — this module's own caller
(`paper_analysis_pipeline.py::analyze_ingredient_papers`) is itself a
plain sync function, always run inside a `run_in_threadpool` worker
thread (see that module's docstring), same as every other blocking
Gemini call in this pipeline. Making just this one function `async def`
around a call that isn't actually awaited anywhere would be a
false-async signature — a real async version would need the `genai`
SDK's separate `client.aio.models.generate_content` client and an
`asyncio.run()` wrapper at the call site (the same pattern
`resource_fetcher.py` already uses for genuinely concurrent HTTP I/O),
which isn't warranted here: extraction calls are already sequential and
best-effort, one per resource, the same way `resource_grader.grade_resource`
is already called sequentially per resource in `resource_fetcher.py`.

Pure — no DB access, no side effects, same "pure function in, structured
result out" design as `resource_grader.grade_resource`. The caller is
responsible for persisting the result onto `VerifiedResource.extracted_data`.

**Rate limiting (Phase 18).** The Gemini call below is now paced and
retried via `app/services/gemini_rate_limit.py` — `throttle_gemini_call()`
right before the request (process-wide ~4.5s minimum spacing from the
previous Gemini call made anywhere in this app, not just other
extraction calls) and `call_gemini_with_retry()` around it (exponential
backoff specifically on a 429/`RESOURCE_EXHAUSTED` response). See that
module's docstring for the full reasoning, including why it's
synchronous rather than `async def` (same reasoning already applied to
this function's own signature, above) and why it checks for
`google.genai.errors.ClientError`/`APIError` rather than the task's
originally-suggested `google.api_core.exceptions.ResourceExhausted`,
which this app's actual `google-genai` dependency never raises.

**Source-specific extraction + `extracted_conclusions` (Phase 19).**
Each entry in `docs/verified_resource_apis.json` already carries its own
`extraction_instructions` string (added when that config file was first
built out — e.g. PubChem's says to focus on "biological role,
physiological action, and documented toxicity/safety baseline",
DailyMed's says to focus on "INDICATIONS & USAGE"/"DOSAGE AND
ADMINISTRATION"/"WARNINGS"). `_find_extraction_instructions()` below
looks that up by matching a VerifiedResource's own `domain` column
against each config entry's `domain` (suffix match, same convention as
`resource_fetcher.py::_is_verified_domain` — a resource's actual host
can be a subdomain of the configured one), and folds it into the SAME
prompt/call `extract_claims_from_resource()` already makes, asking
Gemini to also return `extracted_conclusions` — 2-4 short, factual
conclusions specifically informed by that provider's own extraction
guidance. Persisted onto `VerifiedResource.extracted_conclusions`
(distinct from `extracted_data` above — see that column's own docstring
in app/models/research.py for why they're kept separate), rendered under
an "Extracted Conclusions" heading in the frontend's resource info modal
(`src/components/VerifiedResourcesList.tsx`).

**Deliberate deviation: one call, not two.** The task describing this
feature sketched `extracted_conclusions` as its own dedicated Gemini
prompt/call, separate from the existing structured-claims extraction
above. This module instead folds it into the SAME call (one extra field
on `_ExtractedClaimsSchema`, one extra paragraph in the prompt) —
doubling the Gemini calls made per resource would directly work against
Phase 18's rate-limiting pass (`app/services/gemini_rate_limit.py`),
which exists specifically to reduce how many Gemini requests this
pipeline makes per grade request. `key_takeaways` (existing) and
`extracted_conclusions` (new) can read as similar in spirit — both are
short factual bullet lists — but serve different consumers:
`key_takeaways` is internal to Stage 2 synthesis
(`conclusion_grader.py`'s prompt), while `extracted_conclusions` is a
provider-instruction-driven, display-oriented list purpose-built for the
frontend info modal. Being somewhat redundant in content is an accepted
trade-off for not doubling API calls.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.gemini_rate_limit import call_gemini_with_retry, throttle_gemini_call

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ResourceExtractor]"

# backend/app/services/resource_extractor.py -> parents[2] == backend/ ->
# parents[3] == repo root — same absolute-path-resolution reasoning as
# resource_fetcher.py's own VERIFIED_RESOURCE_APIS_PATH. Read directly
# here too (rather than importing resource_fetcher.py's private loader)
# since docs/verified_resource_apis.json is shared config, not something
# either module exclusively owns — same "each module loads its own
# config/rubric file" convention as paper_grader.py/resource_grader.py
# each loading their own rubric JSON independently.
_REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFIED_RESOURCE_APIS_PATH = _REPO_ROOT / "docs" / "verified_resource_apis.json"

# Below this many characters of cleaned snippet/text, there simply isn't
# enough content to extract a genuine official_stance/dose/warning from —
# calling Gemini on a handful of words risks it inventing plausible-
# sounding but unsupported claims rather than reporting "not enough
# information," which is exactly the kind of fabrication the rest of this
# pipeline (see conclusion_grader.py's zero_evidence_note handling)
# deliberately avoids elsewhere. A resource this short still gets a
# result — just a real, non-None dict with every field explicitly empty
# — rather than a wasted/misleading Gemini call. See
# extract_claims_from_resource()'s docstring.
_MIN_SNIPPET_LENGTH_FOR_EXTRACTION = 20


class ResourceExtractionError(RuntimeError):
    """Raised when Gemini fails to return a usable claims extraction."""


class ExtractedResourceClaims(TypedDict):
    """Return shape of extract_claims_from_resource() — mirrors
    VerifiedResource.extracted_data's stored JSON shape field-for-field
    (see app/models/research.py). `key_takeaways` is always a list (never
    None) with at most 3 entries, even when nothing else could be
    extracted.
    """

    official_stance: Optional[str]
    recommended_dose: Optional[str]
    upper_limit_warning: Optional[str]
    key_takeaways: List[str]
    # Phase 19 — see module docstring's "Source-specific extraction"
    # paragraph. 2-4 entries, capped server-side; always a list, never
    # None, even when nothing could be extracted (mirrors key_takeaways's
    # own "empty list, not null" convention).
    extracted_conclusions: List[str]


class _ExtractedClaimsSchema(BaseModel):
    """Structured output schema handed to Gemini as `response_schema` —
    mirrors the task spec's extraction JSON schema exactly. Every field
    is explicitly optional/defaulted since a legitimate, well-formed
    resource can still lack a stated dose or upper limit (e.g. a page
    that only discusses food sources, or a licensing record with no
    dosage information at all) — Gemini is instructed (see
    `_build_prompt`) to return null/empty rather than guess.
    """

    official_stance: Optional[str] = Field(
        default=None,
        description=(
            "A concise summary of this source's official stance, "
            "authorization, or regulatory classification for the "
            "ingredient — e.g. \"Authorized dietary supplement "
            "ingredient for immune support\" — or null if the text "
            "doesn't state one."
        ),
    )
    recommended_dose: Optional[str] = Field(
        default=None,
        description=(
            "The recommended daily intake / RDA this specific source "
            "states, if any — e.g. \"75-90 mg/day (RDA)\" — or null if "
            "not mentioned. Do not infer or estimate one from general "
            "knowledge; only report what this text actually states."
        ),
    )
    upper_limit_warning: Optional[str] = Field(
        default=None,
        description=(
            "The tolerable upper intake level or safety warning this "
            "specific source states, if any — e.g. \"2000 mg/day "
            "maximum\" — or null if not mentioned."
        ),
    )
    key_takeaways: List[str] = Field(
        default_factory=list,
        description=(
            "2-3 short, specific bullet-point takeaways from this "
            "source, covering whatever it actually discusses (efficacy, "
            "safety, mechanism, food sources, population-specific "
            "guidance, etc.) — empty list if the text is too sparse to "
            "extract anything specific."
        ),
    )
    extracted_conclusions: List[str] = Field(
        default_factory=list,
        description=(
            "2 to 4 concise, factual conclusions regarding the "
            "ingredient, extracted following the EXTRACTION "
            "INSTRUCTIONS FOR THIS PROVIDER given in the prompt (when "
            "provided) — e.g. \"RDA is 90mg/day\", \"Reduces duration of "
            "cold symptoms when taken early\". Extract ONLY what the "
            "provided text actually states; empty list if too sparse to "
            "extract anything specific."
        ),
    )


def _load_resource_api_configs() -> List[Dict[str, Any]]:
    """Reads docs/verified_resource_apis.json fresh on every call —
    deliberately NOT `@lru_cache`d, matching resource_fetcher.py's own
    `_load_resource_apis()` convention for this exact file (see that
    function's docstring): an operator editing `extraction_instructions`
    should see it take effect on the next resource extracted, without
    needing a process restart. Returns an empty list (not a raised
    error) if the file is missing/malformed — same fail-open behavior as
    resource_fetcher.py, since a config-file problem here should degrade
    to "no provider-specific instructions available" (see
    `_find_extraction_instructions` below), not break extraction
    entirely.
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
    return data if isinstance(data, list) else []


def _find_extraction_instructions(domain: Optional[str]) -> Optional[str]:
    """Looks up the `extraction_instructions` string for whichever
    docs/verified_resource_apis.json entry `domain` belongs to — matched
    by suffix (a VerifiedResource's actual `domain` column can be a
    subdomain of the configured one, e.g. `connect.medlineplus.gov`
    vs. a config entry's bare `medlineplus.gov`), same matching
    convention as resource_fetcher.py::_is_verified_domain. Returns
    `None` (not an error) if `domain` is falsy, no config entry matches,
    or the matching entry has no `extraction_instructions` set —
    `_build_prompt` below renders an honest "no provider-specific
    guidance available" fallback in that case rather than failing.
    """
    if not domain:
        return None
    normalized = domain.strip().lower().rstrip(".")
    if not normalized:
        return None

    for config in _load_resource_api_configs():
        config_domain = str(config.get("domain") or "").strip().lower().rstrip(".")
        if not config_domain:
            continue
        if normalized == config_domain or normalized.endswith("." + config_domain):
            instructions = config.get("extraction_instructions")
            return instructions if isinstance(instructions, str) and instructions.strip() else None
    return None


@lru_cache
def _get_client() -> genai.Client:
    """Cached Gemini client — separate `@lru_cache` entry from every
    other Gemini-using service's own `_get_client` (paper_grader.py,
    resource_grader.py, conclusion_grader.py, research_keywords.py), same
    reasoning as those: an equivalent client per module rather than one
    shared across the codebase.
    """
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key)


def _build_prompt(
    resource_title: str,
    publisher: str,
    snippet_or_text: str,
    provider_instructions: Optional[str],
) -> str:
    instructions_block = (
        provider_instructions.strip()
        if provider_instructions and provider_instructions.strip()
        else (
            "(No provider-specific extraction guidance is configured for "
            "this source — extract based on general best practices for "
            "an official reference source: focus on regulatory stance, "
            "dosage, and safety information the text actually states.)"
        )
    )

    return (
        "You are analyzing an official reference payload from: "
        f"{publisher} ({resource_title}).\n\n"
        "Extract ONLY what this specific text actually states — do not "
        "fill in gaps from general knowledge, and do not restate the "
        "ingredient's common-knowledge uses if this text itself doesn't "
        "mention them. Where the text doesn't address a field, return "
        "null (or an empty list for key_takeaways/extracted_conclusions) "
        "rather than guessing.\n\n"
        "EXTRACTION INSTRUCTIONS FOR THIS PROVIDER:\n"
        f"{instructions_block}\n\n"
        f"RESOURCE DATA / SNIPPET:\n{snippet_or_text}\n\n"
        "Based ONLY on the provider data and instructions above, also "
        "extract `extracted_conclusions`: 2 to 4 concise, factual "
        "conclusions regarding the ingredient, following the provider "
        "instructions when given.\n\n"
        "Return your extraction as the required JSON object."
    )


def extract_claims_from_resource(
    resource_title: str,
    publisher: str,
    snippet_or_text: Optional[str],
    domain: Optional[str] = None,
) -> ExtractedResourceClaims:
    """Extracts normalized official stance, recommended dose, upper-limit
    warning, key takeaways, AND (Phase 19) provider-instruction-driven
    `extracted_conclusions` from one VerifiedResource's own
    title/publisher/summary/domain — Stage 1 of the Two-Stage Extraction
    Pipeline (Phase 17). See module docstring for why this runs
    independently per resource rather than as part of the final
    multi-source synthesis prompt, and for why `extracted_conclusions`
    rides along on this same call rather than a second one.

    **Short-snippet guard.** If `snippet_or_text` (after stripping) is
    shorter than `_MIN_SNIPPET_LENGTH_FOR_EXTRACTION` characters — either
    `None`/empty (many source APIs don't provide a summary at all — see
    app/models/research.py's `VerifiedResource.summary` docstring) or
    just a few words — this makes NO Gemini call and returns a real
    (non-None) result with every field explicitly empty/null instead.
    This is treated as a normal, expected outcome (logged at `info`, not
    raised as an error): calling Gemini on a handful of words risks it
    fabricating a plausible-sounding dose or stance the source text never
    actually stated, which is worse than honestly recording "nothing
    extractable here."

    Args:
        resource_title: The VerifiedResource's `title`.
        publisher: The VerifiedResource's `publisher`.
        snippet_or_text: The VerifiedResource's `summary` — the only text
            content this module has to work with; optional/may be None.

    Returns:
        An `ExtractedResourceClaims` dict — every field defaults to
        None/empty on either the short-snippet path above or when Gemini
        genuinely finds nothing to report for a field; never raises for
        "nothing found," only for an actual request/parsing failure (see
        Raises below).

    Raises:
        ResourceExtractionError: if the Gemini request itself fails, or
            its response can't be parsed against the expected schema at
            all. Callers should catch this the same "log and skip, leave
            extracted_data null" way every other best-effort Gemini call
            in this pipeline is handled (see
            app/services/paper_analysis_pipeline.py's Stage 1 step).
    """
    cleaned_snippet = (snippet_or_text or "").strip()

    if len(cleaned_snippet) < _MIN_SNIPPET_LENGTH_FOR_EXTRACTION:
        logger.info(
            "%s Skipping claims extraction for %r (%s) — snippet/text is "
            "too short (%d character(s), need >= %d) to extract anything "
            "meaningful; storing an empty/insufficient-data result "
            "instead of calling Gemini.",
            _LOG_PREFIX,
            resource_title,
            publisher,
            len(cleaned_snippet),
            _MIN_SNIPPET_LENGTH_FOR_EXTRACTION,
        )
        return {
            "official_stance": None,
            "recommended_dose": None,
            "upper_limit_warning": None,
            "key_takeaways": [],
            "extracted_conclusions": [],
        }

    client = _get_client()
    settings = get_settings()
    provider_instructions = _find_extraction_instructions(domain)
    prompt = _build_prompt(resource_title, publisher, cleaned_snippet, provider_instructions)

    def _call_gemini():
        throttle_gemini_call()
        return client.models.generate_content(
            model=settings.gemini_model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_ExtractedClaimsSchema,
            ),
        )

    try:
        response = call_gemini_with_retry(
            _call_gemini, label=f"extracting claims for resource {resource_title!r}"
        )
    except Exception as exc:  # noqa: BLE001 - surface as a clean service error
        raise ResourceExtractionError(f"Gemini request failed: {exc}") from exc

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, _ExtractedClaimsSchema):
        raw_text = getattr(response, "text", None)
        if not raw_text:
            raise ResourceExtractionError("Gemini returned an empty response.")
        try:
            parsed = _ExtractedClaimsSchema.model_validate_json(raw_text)
        except Exception as exc:  # noqa: BLE001
            raise ResourceExtractionError(
                f"Gemini response did not match the expected schema: {exc}"
            ) from exc

    official_stance = (parsed.official_stance or "").strip() or None
    recommended_dose = (parsed.recommended_dose or "").strip() or None
    upper_limit_warning = (parsed.upper_limit_warning or "").strip() or None
    # Capped at 3 (per spec's "2-3 core bullet points") even if Gemini's
    # raw output overshoots that — same "don't trust the model's own
    # bound-following, enforce it server-side" philosophy as every
    # rubric-based grader's score clamping elsewhere in this codebase.
    key_takeaways = [item.strip() for item in parsed.key_takeaways if item and item.strip()][:3]
    # Phase 19: "2 to 4" per spec — capped server-side, same "don't trust
    # the model's own bound-following" reasoning as key_takeaways above.
    extracted_conclusions = [
        item.strip() for item in parsed.extracted_conclusions if item and item.strip()
    ][:4]

    return {
        "official_stance": official_stance,
        "recommended_dose": recommended_dose,
        "upper_limit_warning": upper_limit_warning,
        "key_takeaways": key_takeaways,
        "extracted_conclusions": extracted_conclusions,
    }
