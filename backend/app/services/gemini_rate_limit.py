"""Shared Gemini rate-limit handling (Phase 18) — used by every
Gemini-calling service that needs it: currently
`app/services/paper_grader.py` (paper quality grading, Phase 3/6).
`app/services/resource_extractor.py` (Stage 1 resource claims
extraction, Phase 17) used this too, through Phase 20 — as of Phase 21
that module is deprecated and no longer called by anything (see its own
module docstring), so this is effectively its only live caller now.

**Why this exists.** Under sustained grading traffic, this app was
hitting the Gemini free-tier's requests-per-minute ceiling mid-pipeline
— a `429 RESOURCE_EXHAUSTED` error partway through grading a batch of
newly-found papers. Each individual Gemini call site already converts
that into its own service-specific error (`PaperGradingError`,
`ResourceExtractionError`) and the per-paper/per-resource loops in
`app/services/paper_analysis_pipeline.py` already catch and log those
per-item rather than aborting the whole run — so a single rate-limit hit
was never actually a hard crash. In practice, though, an entire *burst*
of papers graded back-to-back with no spacing would all land in the same
one-minute quota window and fail together, one after another, with no
chance for the quota to recover — which is what this module fixes, with
two complementary mechanisms:

1. **`throttle_gemini_call()`** — call this immediately before every
   `client.models.generate_content(...)` invocation. Enforces a minimum
   `GEMINI_CALL_SPACING_SECONDS` (4.5s) gap since the *last* Gemini call
   made anywhere in this process (a process-wide, lock-guarded
   timestamp — not a per-loop/per-function-local sleep), so the
   aggregate call rate across every concurrent grade request stays under
   ~13 RPM, safely below a 15 RPM ceiling. Deliberately global rather
   than "sleep 4.5s after every call in this one loop": a per-loop sleep
   only bounds that one loop's own internal pacing — it does nothing to
   stop two *concurrent* grade requests (e.g. two ingredients graded
   around the same time) from each independently pacing themselves to
   13 RPM while jointly still exceeding the actual account-wide limit.
   A single shared timestamp, protected by one lock, correctly bounds
   the real aggregate rate regardless of how many requests are in
   flight — still a reasonable simplification for this single-process
   dev/prototype app (same "fine for now, revisit under real concurrent
   load" caveat already noted elsewhere in this codebase, e.g.
   `app/db.py`'s SQLite locking discussion), but a strictly more correct
   one than scoping the pacing per-call-site.
2. **`call_gemini_with_retry()`** — wraps one Gemini call (as a zero-
   argument callable) with exponential-backoff retries (5s, 10s, 20s,
   40s) specifically when the underlying error is a 429/
   `RESOURCE_EXHAUSTED` response — see `_is_rate_limit_error` below.
   Every other exception (a malformed request, an auth failure, an
   unparsable response) propagates immediately, unretried, straight back
   to the caller's own existing error handling — retrying those would
   just waste time reproducing the same failure.

**Two deliberate deviations from the task's literal reference
implementation**, both documented here since they affect every call site
that uses this module:

1. **Synchronous, not `async def`.** Every Gemini-calling service in
   this codebase (`paper_grader.py`, `resource_grader.py`,
   `resource_extractor.py`, `conclusion_grader.py`,
   `research_keywords.py`) makes a blocking, synchronous
   `client.models.generate_content(...)` call from a plain sync
   function — always invoked inside a FastAPI `run_in_threadpool` worker
   thread (see `app/services/paper_search.py`'s docstring for why
   `asyncio.run()` is safe there for the *other* kind of async work this
   app does), never from the event loop, never from an `async def`
   caller. An `async def call_gemini_with_retry` wrapping a call that's
   never actually `await`-ed anywhere would be a false-async signature;
   `time.sleep()` here blocks the worker thread the same way
   `await asyncio.sleep()` would block an event-loop task, with none of
   the genuine concurrency `async`/`await` implies elsewhere in this
   codebase (e.g. `resource_fetcher.py`'s real concurrent HTTP fan-out
   via `httpx.AsyncClient` + `asyncio.gather`). Same reasoning already
   applied to `resource_extractor.py`'s own `extract_claims_from_resource`
   in Phase 17 — restated here since it now also governs this module.
2. **Catches `google.genai.errors.ClientError`/`APIError`, not
   `google.api_core.exceptions.ResourceExhausted`.** This backend's
   Gemini dependency is `google-genai` (`from google import genai` —
   see `backend/requirements.txt`: `google-genai>=1.0,<2.0`), the newer
   unified Gen AI SDK — not `google-generativeai` or
   `google-cloud-aiplatform`/Vertex AI's client, which are what actually
   raise `google.api_core.exceptions.ResourceExhausted`. Verified against
   the `google-genai` SDK's own source and issue tracker
   (googleapis/python-genai): a 429 quota error surfaces there as
   `google.genai.errors.ClientError` (a subclass of `APIError`, which
   carries a `.code: int` HTTP status and a `.status: Optional[str]`
   value of `"RESOURCE_EXHAUSTED"`) — see `_is_rate_limit_error` below.
   Importing the task's literal `google.api_core.exceptions.ResourceExhausted`
   would have silently defeated this entire feature: that `except`
   clause would never match anything `google-genai` actually raises
   (unless that package happens to be installed transitively for an
   unrelated reason, `google.api_core` isn't even a guaranteed
   dependency of this app at all), and every 429 would fall straight
   through as an ordinary, unretried failure.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, TypeVar

from google.genai import errors as genai_errors

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[RateLimit]"

T = TypeVar("T")

# Minimum spacing enforced between consecutive Gemini calls anywhere in
# this process, via throttle_gemini_call() below — 4.5s keeps a
# sequential stream of calls under ~13 requests/minute, safely under a
# 15 RPM ceiling with some headroom for calls this module doesn't pace
# (see module docstring's "known gap" — conclusion_grader.py's own
# Gemini calls aren't wired through this module).
GEMINI_CALL_SPACING_SECONDS = 4.5

# Exponential backoff schedule for a 429 retry: (2**attempt) * 5 =
# 5s, 10s, 20s, 40s for attempts 0-3 — matches the task's own reference
# schedule exactly.
DEFAULT_MAX_RETRIES = 4
_BACKOFF_BASE_SECONDS = 5

_throttle_lock = threading.Lock()
_last_call_at: Optional[float] = None


def throttle_gemini_call() -> None:
    """Blocks the calling thread, if necessary, until at least
    `GEMINI_CALL_SPACING_SECONDS` has elapsed since the last Gemini call
    made anywhere in this process via this function — call this
    immediately before every `client.models.generate_content(...)`
    invocation (see module docstring for why this is process-wide rather
    than scoped to one call site's own loop).

    A no-op (no sleep) for the very first call this process ever makes,
    and for any call that happens to arrive more than
    `GEMINI_CALL_SPACING_SECONDS` after the previous one anyway (e.g.
    because the previous call itself took a while, or other work
    happened in between) — this only ever sleeps for the *remaining*
    gap, never a flat 4.5s regardless of elapsed time.
    """
    global _last_call_at
    with _throttle_lock:
        now = time.monotonic()
        if _last_call_at is not None:
            remaining = GEMINI_CALL_SPACING_SECONDS - (now - _last_call_at)
            if remaining > 0:
                time.sleep(remaining)
        _last_call_at = time.monotonic()


def _is_rate_limit_error(exc: BaseException) -> bool:
    """True iff `exc` represents a 429/`RESOURCE_EXHAUSTED` response from
    the Gemini API.

    Primary check: `isinstance(exc, genai_errors.APIError)` (covers both
    its `ClientError` and `ServerError` subclasses — see module
    docstring) with `.code == 429` or `.status == "RESOURCE_EXHAUSTED"`.
    Falls back to a plain substring check on `str(exc)` when `exc` isn't
    an `APIError` at all — defensive in case a future SDK version raises
    something else for this, or some other layer wraps the original
    error before it reaches here. See module docstring for why the
    task's literal `google.api_core.exceptions.ResourceExhausted` is not
    the right exception type to check for this codebase's actual
    `google-genai` dependency.
    """
    if isinstance(exc, genai_errors.APIError):
        if exc.code == 429 or str(exc.status or "").upper() == "RESOURCE_EXHAUSTED":
            return True
    return "RESOURCE_EXHAUSTED" in str(exc).upper()


def call_gemini_with_retry(
    prompt_func: Callable[[], T],
    max_retries: int = DEFAULT_MAX_RETRIES,
    *,
    label: str = "Gemini request",
) -> T:
    """Calls `prompt_func()` — a zero-argument callable that performs and
    returns the result of one Gemini API call (typically
    `client.models.generate_content(...)`, wrapped in a small lambda by
    the caller so it can be retried as a unit) — retrying with
    exponential backoff specifically when the failure is a 429/
    `RESOURCE_EXHAUSTED` response (see `_is_rate_limit_error`).

    Any other exception `prompt_func()` raises propagates immediately,
    on the first attempt, completely unretried: a malformed request
    (400), an auth failure (401/403), or an unparsable response isn't
    going to succeed just because we waited and tried again, so retrying
    those would only add latency without ever fixing anything — callers'
    own existing `except Exception as exc: raise <ServiceError>(...)`
    wrapping around this call handles those exactly as before.

    Args:
        prompt_func: Zero-arg callable performing one Gemini API call.
        max_retries: Total number of ATTEMPTS (not additional retries
            beyond a first try) — `max_retries=4` (the default, matching
            the task's own reference) means up to 4 attempts total,
            waiting 5s/10s/20s between the first three failures before a
            4th and final attempt. Deliberately does NOT sleep again
            after a 4th attempt that also fails — sleeping 40s only to
            immediately give up afterward would be pure wasted latency
            with no further attempt to justify it.
        label: Short, human-readable description of what's being
            retried (e.g. `"grading paper id=42"`,
            `"extracting resource id=17"`), included in every log line
            so a busy console makes clear which of several potentially
            in-flight rate-limit backoffs a given log line belongs to.

    Returns:
        Whatever `prompt_func()` eventually returned on its first
        successful attempt.

    Raises:
        Whatever non-rate-limit exception `prompt_func()` raised, if any
        (propagated immediately, unretried — see above).
        RuntimeError: if every one of `max_retries` attempts failed with
            a rate-limit error.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries):
        try:
            return prompt_func()
        except Exception as exc:  # noqa: BLE001 - inspected immediately below
            if not _is_rate_limit_error(exc):
                raise
            last_exc = exc
            is_final_attempt = attempt == max_retries - 1
            wait_time = (2**attempt) * _BACKOFF_BASE_SECONDS
            if is_final_attempt:
                logger.warning(
                    "%s 429 RESOURCE_EXHAUSTED received for %s "
                    "(attempt %d/%d) — no attempts remaining.",
                    _LOG_PREFIX,
                    label,
                    attempt + 1,
                    max_retries,
                )
                break
            logger.warning(
                "%s 429 RESOURCE_EXHAUSTED received for %s. Retrying in "
                "%ss (attempt %d/%d)...",
                _LOG_PREFIX,
                label,
                wait_time,
                attempt + 1,
                max_retries,
            )
            time.sleep(wait_time)

    raise RuntimeError(
        f"{_LOG_PREFIX} Max Gemini retries exceeded due to rate limits "
        f"while {label} ({max_retries} attempt(s) exhausted). "
        f"Last error: {last_exc}"
    )
