"""Single, explicit Gemini call -- no fallbacks, fully env-driven model, graceful 429 retry only.

This module makes exactly ONE kind of call: a request to whatever model
``Settings.gemini_model`` (env var ``GEMINI_MODEL``) currently says -- see
``app.core.config``. There is no candidate list, no
``client.models.list()`` discovery, no iteration/rotation loop, and no
fallback to a different model ever. The ONE exception to "no retry" is a
429 / RESOURCE_EXHAUSTED rate-limit error, which is retried in place (same
model, same request) a bounded number of times using Google's own
suggested backoff. Every other failure still fails immediately, and the
full raw exception (message + stack trace) is logged straight to stdout
so it's visible without digging through structured log output.

Rules:

1. **Model is fully dynamic, driven by one environment variable.** There
   is no hardcoded model constant in this module. Every call reads
   ``settings.gemini_model`` fresh (default injected via
   ``app.core.config.get_settings()``, or an explicit ``Settings``
   instance passed in) and passes it straight through to
   ``client.models.generate_content``. Changing which model gets called
   is changing ``GEMINI_MODEL`` in the environment / ``backend/.env`` and
   restarting the process -- no code change, no redeploy of this module.
   Any leading ``"models/"`` prefix is stripped first, so both
   ``"gemini-2.0-flash"`` and ``"models/gemini-2.0-flash"`` work
   identically (Gemini's own API sometimes returns/expects the prefixed
   form, e.g. from ``client.models.list()``).
2. **No retries, except a bounded, graceful retry on 429 rate limits.**
   Every non-429 failure (404, auth, malformed request, network error,
   etc.) still fails on the first attempt -- no backoff, no "model not
   found" handling, no same-model retry, no fallback to a different
   model. A 429 / RESOURCE_EXHAUSTED ``google.genai.errors.ClientError``
   is the one exception: it's retried, against the same model, up to
   ``MAX_RATE_LIMIT_RETRIES`` (3) additional times. Before each retry,
   this waits however long Google's own error response says to
   (``RetryInfo.retryDelay`` in the error details), falling back to
   ``DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS`` (15s) if that detail isn't
   present or isn't parseable. If the SDK isn't installed, or the error
   isn't recognizably a 429 ``ClientError``, this behaves exactly as
   before: fail immediately, no retry.
3. **Mandatory 60s pause between calls, process-wide.** Once a call
   resolves -- successfully, or by exhausting its 429 retries, or by
   failing outright on a non-429 error -- it's followed by a hard
   ``RATE_LIMIT_PAUSE_SECONDS`` (60s) pause, held under a single
   module-level lock shared by every call this process makes, so the
   next call anywhere in the process can't start until 60s after the
   previous one finished. This is separate from, and in addition to, any
   429 retry-delay waits above -- those happen *between* attempts of the
   same logical call, still inside this same 60s-gated window.
4. **Standard synchronous SDK call.** Uses the plain
   ``client.models.generate_content(model=..., contents=..., config=...)``
   method (not ``client.aio.models...``), run via ``asyncio.to_thread`` so
   this async function doesn't block the server's event loop while the
   request is in flight. The actual google-genai call being made is the
   ordinary synchronous one, just executed on a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import traceback
from typing import Any, Optional

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

try:
    # Optional dependency, same as elsewhere in this codebase (see
    # app.services.vision_parser's local `from google import genai` import) --
    # only needed to recognize a 429 ClientError specifically. If it's not
    # installed, _is_rate_limit_error() below always returns False and this
    # module falls back to its original zero-retry behavior for everything.
    from google.genai import errors as _genai_errors
except ImportError:  # pragma: no cover - exercised via tests when SDK isn't installed
    _genai_errors = None  # type: ignore[assignment]

# Mandatory pause enforced after every executed API request (success or
# failure) before any subsequent call -- anywhere in the process -- is
# allowed to start. See module docstring point 3.
RATE_LIMIT_PAUSE_SECONDS = 60.0

# How many additional attempts a 429/RESOURCE_EXHAUSTED error gets, on top
# of the initial attempt -- so up to 1 + MAX_RATE_LIMIT_RETRIES = 4 total
# attempts against the same model before this gives up. See module
# docstring point 2.
MAX_RATE_LIMIT_RETRIES = 3

# Used only when a 429's error body doesn't carry a parseable
# RetryInfo.retryDelay. See module docstring point 2.
DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS = 15.0

# Shared across every call this process makes -- see module docstring
# point 3. Held for the duration of the request (including any 429
# retries) AND its post-request pause, so the next call literally cannot
# begin until 60s after the previous one finished.
_rate_limit_lock = asyncio.Lock()

_RETRY_DELAY_PATTERN = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*s\s*$")


class GeminiCallError(Exception):
    """Raised when a Gemini call ultimately fails -- either a non-429 error on
    the first attempt, or a 429 that's still failing after
    ``MAX_RATE_LIMIT_RETRIES`` retries.

    Wraps whatever the underlying SDK/network error was; callers should
    treat this as a terminal failure for the current request, not
    something to loop on themselves.
    """


def _resolve_model(settings: Settings) -> str:
    """Read ``settings.gemini_model`` and strip any leading ``"models/"`` prefix.

    So both ``"gemini-2.0-flash"`` and ``"models/gemini-2.0-flash"`` (the
    latter being the form ``client.models.list()`` itself returns) work
    seamlessly as a ``GEMINI_MODEL`` value.
    """
    return settings.gemini_model.replace("models/", "")


def _is_rate_limit_error(exc: Exception) -> bool:
    """True if ``exc`` is a 429 / RESOURCE_EXHAUSTED ``google.genai.errors.ClientError``.

    Deliberately narrow: this must NOT match a bare 404, auth failure, or
    any other 4xx ``ClientError`` -- those still fail immediately with no
    retry, per module docstring point 2.
    """
    if _genai_errors is None or not isinstance(exc, _genai_errors.ClientError):
        return False
    return getattr(exc, "code", None) == 429 or getattr(exc, "status", None) == "RESOURCE_EXHAUSTED"


def _extract_retry_delay_seconds(exc: Exception) -> float:
    """Best-effort extraction of Google's suggested ``RetryInfo.retryDelay`` from a 429.

    The error body (``exc.details``) is normally shaped like::

        {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "19s"}
        ]}}

    ``retryDelay`` is a protobuf ``Duration`` string, e.g. ``"19s"`` or
    ``"0.5s"``. Falls back to ``DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS`` if
    that detail is missing, malformed, or the error body has an
    unexpected shape -- this must never itself raise.
    """
    try:
        details = getattr(exc, "details", None)
        if not isinstance(details, dict):
            return DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS
        error_body = details.get("error", details)
        if not isinstance(error_body, dict):
            return DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS

        for detail in error_body.get("details", None) or []:
            if not isinstance(detail, dict):
                continue
            if "RetryInfo" not in str(detail.get("@type", "")):
                continue
            match = _RETRY_DELAY_PATTERN.match(str(detail.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    except Exception:  # noqa: BLE001 - defensive: a parsing bug must never break the retry path
        pass
    return DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS


async def generate_content(
    client: Any,
    contents: Any,
    config: Any,
    settings: Optional[Settings] = None,
) -> Any:
    """Call ``client.models.generate_content`` against ``settings.gemini_model``.

    A single attempt for any non-429 outcome. A 429 / RESOURCE_EXHAUSTED
    ``ClientError`` is retried in place -- same model, same request -- up
    to ``MAX_RATE_LIMIT_RETRIES`` additional times, waiting Google's own
    suggested ``retryDelay`` (or ``DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS``)
    between attempts. See module docstring points 1-2.

    Always waits ``RATE_LIMIT_PAUSE_SECONDS`` once the call resolves
    (success, exhausted 429 retries, or an immediate non-429 failure)
    before returning or re-raising, and holds ``_rate_limit_lock`` the
    entire time -- including through any 429 retry waits -- so no other
    call anywhere in the process can start until that pause is over
    (module docstring point 3).

    Args:
        client: A ``google.genai.Client`` (or any object exposing the
            same synchronous ``client.models.generate_content`` method --
            tests use a bare mock). Called via ``asyncio.to_thread``, see
            module docstring point 4.
        contents: Passed straight through to ``generate_content``.
        config: Passed straight through to ``generate_content``.
        settings: Which model to call is read from ``settings.gemini_model``
            (with any ``"models/"`` prefix stripped). Defaults to
            ``get_settings()`` if omitted -- pass an explicit ``Settings``
            instance (e.g. from a caller that already holds one, or in
            tests) to pin the model without touching the environment.

    Raises:
        GeminiCallError: If the call ultimately fails -- either a non-429
            error on the first attempt, or a 429 still failing after all
            retries. The original exception's full message and stack
            trace are printed to stdout before this is raised.
    """
    settings = settings or get_settings()
    model = _resolve_model(settings)

    logger.info("Sending Gemini request with model=%r", model)
    print(f"[gemini_client] Sending Gemini request with model={model!r}", file=sys.stdout, flush=True)

    async with _rate_limit_lock:
        attempt = 0
        while True:
            attempt += 1
            try:
                # Standard synchronous google-genai call -- client.models.generate_content,
                # not client.aio.models... -- run off the event loop thread so this async
                # function doesn't block the whole server for the duration of the request.
                response = await asyncio.to_thread(
                    client.models.generate_content, model=model, contents=contents, config=config
                )
            except Exception as exc:  # noqa: BLE001 - deliberate: log raw; retry only for 429s
                if _is_rate_limit_error(exc) and attempt <= MAX_RATE_LIMIT_RETRIES:
                    retry_delay = _extract_retry_delay_seconds(exc)
                    print(
                        f"[gemini_client] 429 RESOURCE_EXHAUSTED on model={model!r} "
                        f"(attempt {attempt}/{MAX_RATE_LIMIT_RETRIES + 1}); "
                        f"retrying in {retry_delay:.1f}s: {exc}",
                        file=sys.stdout,
                        flush=True,
                    )
                    logger.warning(
                        "Gemini call to model=%r hit 429 RESOURCE_EXHAUSTED (attempt %d/%d); "
                        "retrying in %.1fs",
                        model,
                        attempt,
                        MAX_RATE_LIMIT_RETRIES + 1,
                        retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    continue

                # Terminal failure -- either not a 429 at all, or a 429 that's
                # still failing after every retry. Full raw exception message +
                # stack trace, straight to stdout -- no further retry, no
                # secondary model lookup, nothing hidden behind log formatting/levels.
                print(
                    f"[gemini_client] Gemini call to model={model!r} failed: {type(exc).__name__}: {exc}",
                    file=sys.stdout,
                    flush=True,
                )
                traceback.print_exc(file=sys.stdout)
                sys.stdout.flush()
                logger.error("Gemini call to model=%r failed: %s: %s", model, type(exc).__name__, exc)

                await asyncio.sleep(RATE_LIMIT_PAUSE_SECONDS)
                raise GeminiCallError(
                    f"Gemini call to model={model!r} failed: {type(exc).__name__}: {exc}"
                ) from exc

            logger.info(
                "Gemini call to model=%r succeeded; pausing %.0fs before any subsequent call is allowed.",
                model,
                RATE_LIMIT_PAUSE_SECONDS,
            )
            await asyncio.sleep(RATE_LIMIT_PAUSE_SECONDS)
            return response
