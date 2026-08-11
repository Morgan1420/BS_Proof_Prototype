"""Tests for app.services.gemini_client -- the single-attempt, env-driven-model, no-retry Gemini caller.

Exercises generate_content against a bare mock `client` object (not a
real google.genai.Client), so these tests run without requiring the
google-genai SDK to be installed. Every test patches asyncio.sleep so the
mandatory 60s pause doesn't actually slow the suite down, while still
letting tests assert it was requested.

Every test passes an explicit `settings=` (built via `make_settings()`,
always with `_env_file=None`) rather than relying on generate_content's
`get_settings()` default, so these tests stay hermetic regardless of
whatever GEMINI_MODEL happens to be set in the real backend/.env.

Note: generate_content() calls the SDK's synchronous
client.models.generate_content via asyncio.to_thread, so mocked
side_effect callables run on a worker thread, not the main event loop
thread -- the one test that needs cross-call ordering guarantees
(TestMandatoryPause's lock test) uses threading.Event for that reason,
not asyncio.Event.
"""

import asyncio
import importlib.util
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.services.gemini_client import (
    DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS,
    MAX_RATE_LIMIT_RETRIES,
    RATE_LIMIT_PAUSE_SECONDS,
    GeminiCallError,
    generate_content,
)

GENAI_AVAILABLE = importlib.util.find_spec("google.genai") is not None
if GENAI_AVAILABLE:
    from google.genai import errors as genai_errors


def run(coro):
    """Run an async test coroutine without requiring pytest-asyncio."""
    return asyncio.run(coro)


def make_settings(model: str = "gemini-2.5-flash-lite") -> Settings:
    return Settings(_env_file=None, gemini_api_key="test-key", gemini_model=model)


def make_client(generate_content_side_effect):
    """A bare mock exposing the SYNCHRONOUS client.models.generate_content method.

    Not client.aio.models... -- generate_content() is expected to call
    the plain sync SDK method (via asyncio.to_thread), per the "standard
    synchronous SDK call" rule (see gemini_client's module docstring).
    """
    client = MagicMock()
    client.models.generate_content = MagicMock(side_effect=generate_content_side_effect)
    return client


def patched_sleep():
    # Real waits are 60s -- patch asyncio.sleep so these tests run instantly
    # while still letting us assert on what it was called with.
    return patch("app.services.gemini_client.asyncio.sleep", new_callable=AsyncMock)


SENTINEL_RESPONSE = object()


class TestDynamicModelFromSettings:
    """Model selection is fully dynamic -- read from Settings.gemini_model, no hardcoded constant."""

    def test_default_gemini_model_setting_is_gemini_2_0_flash(self):
        # Settings' own field default, per app.core.config -- GEMINI_MODEL
        # env var overrides this, but the baked-in default is 2.0 Flash.
        settings = Settings(_env_file=None, gemini_api_key="test-key")
        assert settings.gemini_model == "gemini-2.0-flash"

    def test_generate_content_uses_the_model_from_settings(self):
        settings = make_settings(model="some-custom-model-name")
        client = make_client(generate_content_side_effect=[SENTINEL_RESPONSE])

        with patched_sleep():
            run(generate_content(client, contents=["x"], config={}, settings=settings))

        client.models.generate_content.assert_called_once_with(
            model="some-custom-model-name", contents=["x"], config={}
        )

    def test_a_leading_models_prefix_is_stripped(self):
        settings = make_settings(model="models/gemini-2.0-flash")
        client = make_client(generate_content_side_effect=[SENTINEL_RESPONSE])

        with patched_sleep():
            run(generate_content(client, contents=["x"], config={}, settings=settings))

        client.models.generate_content.assert_called_once_with(
            model="gemini-2.0-flash", contents=["x"], config={}
        )

    def test_a_bare_model_name_without_prefix_is_unaffected(self):
        settings = make_settings(model="gemini-2.0-flash")
        client = make_client(generate_content_side_effect=[SENTINEL_RESPONSE])

        with patched_sleep():
            run(generate_content(client, contents=["x"], config={}, settings=settings))

        client.models.generate_content.assert_called_once_with(
            model="gemini-2.0-flash", contents=["x"], config={}
        )

    def test_defaults_to_get_settings_when_no_settings_argument_given(self):
        fallback_settings = make_settings(model="model-from-get-settings")
        client = make_client(generate_content_side_effect=[SENTINEL_RESPONSE])

        with patched_sleep(), patch(
            "app.services.gemini_client.get_settings", return_value=fallback_settings
        ):
            run(generate_content(client, contents=["x"], config={}))

        client.models.generate_content.assert_called_once_with(
            model="model-from-get-settings", contents=["x"], config={}
        )


class TestStandardSdkCall:
    def test_calls_the_synchronous_client_models_generate_content_method(self):
        settings = make_settings()
        client = make_client(generate_content_side_effect=[SENTINEL_RESPONSE])

        with patched_sleep():
            result = run(generate_content(client, contents=["x"], config={}, settings=settings))

        assert result is SENTINEL_RESPONSE
        client.models.generate_content.assert_called_once_with(
            model=settings.gemini_model, contents=["x"], config={}
        )


class TestSingleAttemptNoRetry:
    def test_successful_call_returns_the_response(self):
        client = make_client(generate_content_side_effect=[SENTINEL_RESPONSE])

        with patched_sleep():
            result = run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        assert result is SENTINEL_RESPONSE
        assert client.models.generate_content.call_count == 1

    def test_a_single_failure_raises_immediately_without_any_retry(self):
        client = make_client(generate_content_side_effect=[RuntimeError("boom")])

        with patched_sleep():
            with pytest.raises(GeminiCallError, match="boom"):
                run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        # Exactly one attempt -- no retry loop of any kind.
        assert client.models.generate_content.call_count == 1

    def test_a_429_like_error_that_is_not_a_genai_clienterror_is_not_retried(self):
        # Only a real google.genai.errors.ClientError triggers the 429 retry
        # path (see TestRateLimitRetry below) -- a lookalike exception that
        # merely has a `.code = 429` attribute, but isn't that SDK type,
        # must still fail immediately with no retry.
        class FakeRateLimitError(Exception):
            code = 429

        client = make_client(generate_content_side_effect=[FakeRateLimitError("RESOURCE_EXHAUSTED")])

        with patched_sleep():
            with pytest.raises(GeminiCallError):
                run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        assert client.models.generate_content.call_count == 1

    def test_a_404_style_error_does_not_fall_back_to_any_other_model(self):
        class FakeNotFoundError(Exception):
            code = 404

        client = make_client(generate_content_side_effect=[FakeNotFoundError("model not found")])

        with patched_sleep():
            with pytest.raises(GeminiCallError):
                run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        assert client.models.generate_content.call_count == 1

    def test_failure_wraps_the_original_exception(self):
        original = RuntimeError("network is down")
        client = make_client(generate_content_side_effect=[original])

        with patched_sleep():
            with pytest.raises(GeminiCallError) as exc_info:
                run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        assert exc_info.value.__cause__ is original


def make_client_error(status: str = "RESOURCE_EXHAUSTED", code: int = 429, retry_delay: str = None):
    """Build a real google.genai.errors.ClientError shaped like a live 429 response.

    `retry_delay`, if given, is a protobuf Duration string like "19s" --
    embedded in the error body the same way Google's own RetryInfo detail
    is, so _extract_retry_delay_seconds() can find it. Pass None to build
    a 429 with no RetryInfo at all (exercises the default-delay fallback).
    """
    details = [] if retry_delay is None else [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
    ]
    response_json = {
        "error": {
            "code": code,
            "status": status,
            "message": "Resource has been exhausted (e.g. check quota).",
            "details": details,
        }
    }
    return genai_errors.ClientError(code, response_json)


@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai SDK not installed")
class TestRateLimitRetry:
    """A real 429/RESOURCE_EXHAUSTED ClientError gets retried in place -- the one
    exception to this module's otherwise zero-retry rule.
    """

    def test_succeeds_after_one_429_retry(self):
        error = make_client_error(retry_delay="19s")
        client = make_client(generate_content_side_effect=[error, SENTINEL_RESPONSE])

        with patched_sleep() as mock_sleep:
            result = run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        assert result is SENTINEL_RESPONSE
        assert client.models.generate_content.call_count == 2
        # First sleep is the retryDelay from the 429; second is the mandatory
        # post-call pause.
        mock_sleep.assert_any_await(19.0)
        mock_sleep.assert_any_await(RATE_LIMIT_PAUSE_SECONDS)
        assert mock_sleep.await_count == 2

    def test_retries_up_to_max_rate_limit_retries_then_succeeds(self):
        errors_then_success = [make_client_error(retry_delay="1s") for _ in range(MAX_RATE_LIMIT_RETRIES)]
        errors_then_success.append(SENTINEL_RESPONSE)
        client = make_client(generate_content_side_effect=errors_then_success)

        with patched_sleep():
            result = run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        assert result is SENTINEL_RESPONSE
        # MAX_RATE_LIMIT_RETRIES failures + 1 final success = MAX_RATE_LIMIT_RETRIES + 1 attempts.
        assert client.models.generate_content.call_count == MAX_RATE_LIMIT_RETRIES + 1

    def test_gives_up_after_exhausting_max_rate_limit_retries(self):
        # One more 429 than the retry budget allows -- MAX_RATE_LIMIT_RETRIES + 1
        # total attempts, all of which fail.
        error = make_client_error(retry_delay="1s")
        client = make_client(generate_content_side_effect=[error] * (MAX_RATE_LIMIT_RETRIES + 1))

        with patched_sleep():
            with pytest.raises(GeminiCallError):
                run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        assert client.models.generate_content.call_count == MAX_RATE_LIMIT_RETRIES + 1

    def test_uses_the_retry_delay_from_the_error_details(self):
        error = make_client_error(retry_delay="42s")
        client = make_client(generate_content_side_effect=[error, SENTINEL_RESPONSE])

        with patched_sleep() as mock_sleep:
            run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        mock_sleep.assert_any_await(42.0)

    def test_falls_back_to_the_default_delay_when_no_retry_info_is_present(self):
        error = make_client_error(retry_delay=None)
        client = make_client(generate_content_side_effect=[error, SENTINEL_RESPONSE])

        with patched_sleep() as mock_sleep:
            run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        mock_sleep.assert_any_await(DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS)

    def test_falls_back_to_the_default_delay_when_retry_delay_is_unparseable(self):
        error = make_client_error(retry_delay="not-a-duration")
        client = make_client(generate_content_side_effect=[error, SENTINEL_RESPONSE])

        with patched_sleep() as mock_sleep:
            run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        mock_sleep.assert_any_await(DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS)

    def test_a_non_429_clienterror_is_not_retried(self):
        # A real ClientError, but 404 rather than 429 -- must still fail on
        # the first attempt, same as any other non-rate-limit error.
        not_found = genai_errors.ClientError(
            404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "model not found"}}
        )
        client = make_client(generate_content_side_effect=[not_found])

        with patched_sleep():
            with pytest.raises(GeminiCallError):
                run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        assert client.models.generate_content.call_count == 1

    def test_final_failure_still_pays_the_mandatory_pause_once(self):
        error = make_client_error(retry_delay="1s")
        client = make_client(generate_content_side_effect=[error] * (MAX_RATE_LIMIT_RETRIES + 1))

        with patched_sleep() as mock_sleep:
            with pytest.raises(GeminiCallError):
                run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        # MAX_RATE_LIMIT_RETRIES retry-delay sleeps + exactly one final
        # RATE_LIMIT_PAUSE_SECONDS sleep before raising.
        assert mock_sleep.await_count == MAX_RATE_LIMIT_RETRIES + 1
        mock_sleep.assert_awaited_with(RATE_LIMIT_PAUSE_SECONDS)


class TestLoggingAndStdout:
    def test_logs_the_model_being_sent_before_the_request(self, capsys):
        settings = make_settings(model="gemini-2.5-flash-lite")
        client = make_client(generate_content_side_effect=[SENTINEL_RESPONSE])

        with patched_sleep():
            run(generate_content(client, contents=["x"], config={}, settings=settings))

        captured = capsys.readouterr()
        assert "model='gemini-2.5-flash-lite'" in captured.out

    def test_logged_model_has_the_models_prefix_already_stripped(self, capsys):
        settings = make_settings(model="models/gemini-2.0-flash")
        client = make_client(generate_content_side_effect=[SENTINEL_RESPONSE])

        with patched_sleep():
            run(generate_content(client, contents=["x"], config={}, settings=settings))

        captured = capsys.readouterr()
        assert "model='gemini-2.0-flash'" in captured.out
        assert "models/gemini-2.0-flash" not in captured.out

    def test_failure_prints_the_full_exception_message_and_traceback_to_stdout(self, capsys):
        client = make_client(generate_content_side_effect=[ValueError("very specific failure reason")])

        with patched_sleep():
            with pytest.raises(GeminiCallError):
                run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        captured = capsys.readouterr()
        assert "very specific failure reason" in captured.out
        assert "ValueError" in captured.out
        # A real stack trace, not just the message -- Python tracebacks
        # always include this literal header line.
        assert "Traceback (most recent call last)" in captured.out

    def test_success_does_not_print_a_traceback(self, capsys):
        client = make_client(generate_content_side_effect=[SENTINEL_RESPONSE])

        with patched_sleep():
            run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        captured = capsys.readouterr()
        assert "Traceback" not in captured.out


class TestMandatoryPause:
    def test_pause_is_60_seconds(self):
        assert RATE_LIMIT_PAUSE_SECONDS == 60.0

    def test_successful_call_pauses_before_returning(self):
        client = make_client(generate_content_side_effect=[SENTINEL_RESPONSE])

        with patched_sleep() as mock_sleep:
            run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        mock_sleep.assert_awaited_once_with(RATE_LIMIT_PAUSE_SECONDS)

    def test_failed_call_also_pauses_before_raising(self):
        client = make_client(generate_content_side_effect=[RuntimeError("boom")])

        with patched_sleep() as mock_sleep:
            with pytest.raises(GeminiCallError):
                run(generate_content(client, contents=["x"], config={}, settings=make_settings()))

        mock_sleep.assert_awaited_once_with(RATE_LIMIT_PAUSE_SECONDS)

    def test_second_call_cannot_start_until_first_call_releases_the_shared_lock(self):
        """The module-level lock should serialize two concurrent calls end-to-end.

        asyncio.sleep is mocked away (as in every other test here), but the
        lock itself is real: the second call's generate_content must not
        fire until the first call's entire generate_content (request +
        mandatory pause) has finished and released the lock -- proving the
        pause is a process-wide gate, not just a delay local to one
        caller's own await chain. Uses threading.Event (not asyncio.Event)
        because the mocked SDK call runs on a worker thread via
        asyncio.to_thread, not the main event loop thread.
        """
        first_call_started = threading.Event()
        release_first_call = threading.Event()
        call_order = []

        def _first_call(*_args, **_kwargs):
            call_order.append("first_call_start")
            first_call_started.set()
            release_first_call.wait(timeout=2.0)
            call_order.append("first_call_end")
            return SENTINEL_RESPONSE

        def _second_call(*_args, **_kwargs):
            call_order.append("second_call_start")
            return SENTINEL_RESPONSE

        client_one = make_client(generate_content_side_effect=_first_call)
        client_two = make_client(generate_content_side_effect=_second_call)

        async def scenario():
            loop = asyncio.get_running_loop()
            first_task = asyncio.create_task(
                generate_content(client_one, contents=["x"], config={}, settings=make_settings())
            )
            # Wait (off the event loop thread) for the first call to actually
            # start and be holding _rate_limit_lock, before racing the second.
            await loop.run_in_executor(None, first_call_started.wait, 2.0)

            second_task = asyncio.create_task(
                generate_content(client_two, contents=["y"], config={}, settings=make_settings())
            )
            # A genuine cross-thread yield (not asyncio.sleep, which is
            # mocked in this block) -- lets the event loop actually run the
            # second task up to (and block on) the lock before we check it.
            await loop.run_in_executor(None, lambda: None)
            assert "second_call_start" not in call_order  # still queued behind the first call

            release_first_call.set()
            await first_task
            await second_task

        with patched_sleep():
            run(scenario())

        assert call_order == ["first_call_start", "first_call_end", "second_call_start"]
