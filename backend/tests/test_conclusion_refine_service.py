"""Unit tests for the Phase 40 conclusion refinement service
(`app/services/conclusion_refine_service.py`).

**Scope note — why every test here is skip-guarded.** Unlike
`app/services/resource_parser.py` (see `test_nih_extraction.py`),
`conclusion_refine_service.py` imports `pydantic` and `google.genai` at
module level (same as every other Gemini-calling service in this
codebase — see that module's own docstring for why it mirrors
`html_resource_extractor.py`'s design). Neither package is installed in
this sandbox's `backend/venv` (confirmed: `python3 -c "import pydantic"`
raises `ModuleNotFoundError` here — this environment is evidently a
lighter editing/`py_compile`-only setup, not a fully installed
`requirements.txt` environment). Per this project's CLAUDE.md rule ("DO
NOT execute package installation commands automatically"), these tests
can't install either package themselves, so the whole module can't even
be imported here. All test classes below are wrapped in
`unittest.skipUnless(...)` (mirroring `test_nih_extraction.py`'s
`NihDomainDetectionTests` pattern exactly) so this file still runs
cleanly to completion in this sandbox, and runs its real assertions once
the operator installs `backend/requirements.txt` in a real environment.

Run via (from `backend/`, after `pip install -r requirements.txt`):

    python3 -m unittest discover -s tests -p "test_*.py" -v
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_DEPS_AVAILABLE = (
    importlib.util.find_spec("pydantic") is not None
    and importlib.util.find_spec("google.genai") is not None
)
_SKIP_REASON = (
    "pydantic and/or google-genai are not installed in this environment "
    "— conclusion_refine_service.py (which imports both at module level) "
    "can't be imported. Run `pip install -r requirements.txt` (see "
    "backend/requirements.txt) to enable these tests."
)

if _DEPS_AVAILABLE:
    from app.services import conclusion_refine_service as refine_service


@unittest.skipUnless(_DEPS_AVAILABLE, _SKIP_REASON)
class RefineConclusionsGuardTests(unittest.TestCase):
    """Fast-path guards that never touch Gemini at all — cheapest, most
    important behavior to lock down since a bug here would either waste
    Gemini calls on trivial input or (worse) silently drop conclusions.
    """

    def test_returns_input_unchanged_when_ingredient_name_missing(self) -> None:
        raw = ["A long enough conclusion sentence.", "Another one here too."]
        self.assertEqual(refine_service.refine_conclusions(raw, ""), raw)
        self.assertEqual(refine_service.refine_conclusions(raw, None), raw)  # type: ignore[arg-type]

    def test_returns_input_unchanged_when_raw_list_empty(self) -> None:
        self.assertEqual(refine_service.refine_conclusions([], "Vitamin C"), [])

    def test_skips_gemini_call_below_min_items_threshold(self) -> None:
        raw = ["Only one item, well below the merge/dedupe threshold."]
        with patch.object(refine_service, "_get_client") as mock_get_client:
            result = refine_service.refine_conclusions(raw, "Vitamin C")
        mock_get_client.assert_not_called()
        self.assertEqual(result, raw)


@unittest.skipUnless(_DEPS_AVAILABLE, _SKIP_REASON)
class RefineConclusionsGeminiPathTests(unittest.TestCase):
    """Exercises the real Gemini call path with a mocked client/response,
    so no network access or API key is needed.
    """

    def _raw_conclusions(self) -> list:
        return [
            "Vitamin C helps the body absorb iron from plant-based foods.",
            "Consult your doctor before taking any dietary supplement.",
            "Vitamin C helps the body absorb iron from plant sources.",
            "Each vitamin has specific jobs within the body.",
        ]

    def _mock_response(self, conclusions: list):
        parsed = refine_service._RefinedConclusionsSchema(conclusions=conclusions)
        response = MagicMock()
        response.parsed = parsed
        return response

    def test_successful_refinement_dedupes_and_sanitizes(self) -> None:
        raw = self._raw_conclusions()
        # Gemini already did the noise removal/merge in this mocked
        # response; the service's own dedup + is_valid_human_conclusion
        # pass should still apply on top (defense in depth — "never trust
        # the model's own bound-following").
        mocked_conclusions = [
            "Vitamin C helps the body absorb iron from plant-based foods.",
            "Vitamin C helps the body absorb iron from plant-based foods.",  # exact dup
            "x",  # too short — must be dropped by is_valid_human_conclusion
        ]
        with patch.object(refine_service, "_get_client", return_value=MagicMock()), patch.object(
            refine_service, "throttle_gemini_call"
        ), patch.object(
            refine_service,
            "call_gemini_with_retry",
            return_value=self._mock_response(mocked_conclusions),
        ):
            result = refine_service.refine_conclusions(raw, "Vitamin C")

        self.assertEqual(
            result, ["Vitamin C helps the body absorb iron from plant-based foods."]
        )

    def test_gemini_failure_falls_back_to_original_list(self) -> None:
        raw = self._raw_conclusions()
        with patch.object(refine_service, "_get_client", return_value=MagicMock()), patch.object(
            refine_service, "throttle_gemini_call"
        ), patch.object(
            refine_service,
            "call_gemini_with_retry",
            side_effect=RuntimeError("simulated Gemini outage"),
        ):
            result = refine_service.refine_conclusions(raw, "Vitamin C")
        self.assertEqual(result, raw)

    def test_total_wipeout_falls_back_to_original_list(self) -> None:
        # Gemini (mocked) claims nothing survives — the service should
        # distrust a single pass's total-wipeout judgment and keep the
        # original, already-sanitized items rather than leaving the
        # resource with zero conclusions.
        raw = self._raw_conclusions()
        with patch.object(refine_service, "_get_client", return_value=MagicMock()), patch.object(
            refine_service, "throttle_gemini_call"
        ), patch.object(
            refine_service,
            "call_gemini_with_retry",
            return_value=self._mock_response([]),
        ):
            result = refine_service.refine_conclusions(raw, "Vitamin C")
        self.assertEqual(result, raw)

    def test_result_capped_at_max_refined_conclusions(self) -> None:
        raw = [f"Distinct scientific statement number {i} about Vitamin C." for i in range(70)]
        mocked_conclusions = [f"Distinct scientific statement number {i} about Vitamin C." for i in range(70)]
        with patch.object(refine_service, "_get_client", return_value=MagicMock()), patch.object(
            refine_service, "throttle_gemini_call"
        ), patch.object(
            refine_service,
            "call_gemini_with_retry",
            return_value=self._mock_response(mocked_conclusions),
        ):
            result = refine_service.refine_conclusions(raw, "Vitamin C")
        self.assertLessEqual(len(result), refine_service._MAX_REFINED_CONCLUSIONS)
        self.assertEqual(len(result), refine_service._MAX_REFINED_CONCLUSIONS)


@unittest.skipUnless(_DEPS_AVAILABLE, _SKIP_REASON)
class BuildRefinementPromptTests(unittest.TestCase):
    def test_prompt_includes_ingredient_name_and_numbered_items(self) -> None:
        raw = ["First conclusion text.", "Second conclusion text."]
        prompt = refine_service._build_refinement_prompt("Zinc", raw)
        self.assertIn("Zinc", prompt)
        self.assertIn("1. First conclusion text.", prompt)
        self.assertIn("2. Second conclusion text.", prompt)
        self.assertIn("REMOVE NOISE", prompt)
        self.assertIn("MERGE NEAR-DUPLICATES", prompt)


if __name__ == "__main__":
    unittest.main()
