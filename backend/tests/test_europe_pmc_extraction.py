"""Unit tests for the Phase 42 Europe PMC extraction overhaul
(`app/services/resource_parser.py`'s `_parse_europe_pmc` and its
helpers).

Deliberately stdlib `unittest`, no skip-guard needed: `resource_parser.py`
has zero third-party dependencies (only `re`/`logging`/`xml.etree`/
`difflib`/`html`/`typing`, all standard library), same as
`test_nih_extraction.py`'s coverage of the same module — see that file's
own docstring for why this module in particular is fully testable in a
sandbox missing `httpx`/`pydantic`/`google-genai`.

Run via (from `backend/`):

    python3 -m unittest discover -s tests -p "test_*.py" -v
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.services import resource_parser  # noqa: E402


def _single_result_payload(title, abstract_text=None):
    entry = {"title": title}
    if abstract_text is not None:
        entry["abstractText"] = abstract_text
    return {"resultList": {"result": [entry]}}


class CleanHtmlTextTests(unittest.TestCase):
    """`_clean_html_text` — unescape-then-strip ordering."""

    def test_unescapes_entity_escaped_tags_then_strips_them(self) -> None:
        raw = "Uptake of [&lt;sup&gt;18&lt;/sup&gt;F]FDG increased significantly."
        self.assertEqual(
            resource_parser._clean_html_text(raw),
            "Uptake of [18F]FDG increased significantly.",
        )

    def test_strips_real_tags(self) -> None:
        self.assertEqual(
            resource_parser._clean_html_text("A <b>bold</b> claim about <i>vitamin C</i>."),
            "A bold claim about vitamin C.",
        )

    def test_does_not_treat_mathematical_less_than_as_a_tag(self) -> None:
        raw = "The result was significant (p<0.05) and risk dropped to <50%."
        self.assertEqual(
            resource_parser._clean_html_text(raw),
            "The result was significant (p<0.05) and risk dropped to <50%.",
        )

    def test_combined_entity_and_literal_less_than_and_real_tag(self) -> None:
        raw = (
            "Uptake of [&lt;sup&gt;18&lt;/sup&gt;F]FDG rose (p<0.05). "
            "CONCLUSIONS: promising as a <b>tracer</b>."
        )
        cleaned = resource_parser._clean_html_text(raw)
        self.assertIn("[18F]FDG", cleaned)
        self.assertIn("(p<0.05)", cleaned)
        self.assertIn("CONCLUSIONS: promising as a tracer.", cleaned)

    def test_normalizes_whitespace(self) -> None:
        self.assertEqual(
            resource_parser._clean_html_text("Too   much\n\nwhitespace   here."),
            "Too much whitespace here.",
        )

    def test_non_string_or_empty_input_returns_empty_string(self) -> None:
        self.assertEqual(resource_parser._clean_html_text(None), "")
        self.assertEqual(resource_parser._clean_html_text(""), "")
        self.assertEqual(resource_parser._clean_html_text(12345), "")  # type: ignore[arg-type]


class IsNearDuplicateOfTitleTests(unittest.TestCase):
    def test_exact_match_is_a_duplicate(self) -> None:
        title = "Creatine supplementation improves muscle strength"
        self.assertTrue(resource_parser._is_near_duplicate_of_title(title, title))

    def test_case_insensitive_match_is_a_duplicate(self) -> None:
        self.assertTrue(
            resource_parser._is_near_duplicate_of_title(
                "CREATINE SUPPLEMENTATION IMPROVES MUSCLE STRENGTH",
                "Creatine supplementation improves muscle strength",
            )
        )

    def test_near_duplicate_with_trailing_punctuation_is_a_duplicate(self) -> None:
        self.assertTrue(
            resource_parser._is_near_duplicate_of_title(
                "Creatine supplementation improves muscle strength.",
                "Creatine supplementation improves muscle strength",
            )
        )

    def test_genuinely_distinct_sentence_is_not_a_duplicate(self) -> None:
        self.assertFalse(
            resource_parser._is_near_duplicate_of_title(
                "Daily intake of 5g creatine increased lean mass by 8% over 12 weeks.",
                "Creatine supplementation improves muscle strength",
            )
        )

    def test_empty_candidate_or_title_is_never_a_duplicate(self) -> None:
        self.assertFalse(resource_parser._is_near_duplicate_of_title("", "Some title"))
        self.assertFalse(resource_parser._is_near_duplicate_of_title("Some text", ""))


class SplitEuropePmcSectionsTests(unittest.TestCase):
    def test_splits_structured_abstract_into_named_sections(self) -> None:
        abstract = (
            "BACKGROUND: Zinc is an essential trace mineral. "
            "METHODS: We conducted a randomized trial in 200 adults. "
            "RESULTS: Zinc supplementation reduced cold duration by 33%. "
            "CONCLUSIONS: Zinc is effective for reducing cold duration."
        )
        sections = resource_parser._split_europe_pmc_sections(abstract)
        self.assertEqual(
            set(sections.keys()), {"BACKGROUND", "METHODS", "RESULTS", "CONCLUSIONS"}
        )
        self.assertIn("Zinc supplementation reduced cold duration by 33%.", sections["RESULTS"])
        self.assertIn("Zinc is effective for reducing cold duration.", sections["CONCLUSIONS"])

    def test_unstructured_abstract_returns_empty_dict(self) -> None:
        abstract = "Vitamin D helps calcium absorption and supports bone density over time."
        self.assertEqual(resource_parser._split_europe_pmc_sections(abstract), {})

    def test_empty_string_returns_empty_dict(self) -> None:
        self.assertEqual(resource_parser._split_europe_pmc_sections(""), {})


class EuropePmcSentencesTests(unittest.TestCase):
    def test_prioritizes_results_and_conclusions_sections(self) -> None:
        abstract = (
            "BACKGROUND: Zinc is an essential trace mineral for immune function. "
            "METHODS: We conducted a randomized controlled trial in 200 adults. "
            "RESULTS: Zinc supplementation at 30mg daily reduced cold duration by 33%. "
            "CONCLUSIONS: Zinc supplementation is effective for reducing common cold duration."
        )
        conclusions = resource_parser._europe_pmc_sentences("Zinc and immune function", abstract)
        joined = " ".join(conclusions)
        self.assertIn("Zinc supplementation at 30mg daily reduced cold duration by 33%.", joined)
        self.assertIn(
            "Zinc supplementation is effective for reducing common cold duration.", joined
        )
        # BACKGROUND/METHODS setup text must NOT survive the section-priority filter.
        self.assertNotIn("essential trace mineral", joined)
        self.assertNotIn("randomized controlled trial", joined)

    def test_falls_back_to_whole_abstract_when_unstructured(self) -> None:
        abstract = (
            "Daily intake of 800 IU vitamin D reduced fracture risk by 20% in "
            "postmenopausal women. Vitamin D also plays a role in immune "
            "modulation in vitro."
        )
        conclusions = resource_parser._europe_pmc_sentences("Vitamin D and bone health", abstract)
        self.assertEqual(len(conclusions), 2)
        self.assertTrue(all(c.startswith("Europe PMC ('Vitamin D and bone health'): ") for c in conclusions))

    def test_drops_sentences_that_are_near_duplicates_of_the_title(self) -> None:
        title = "Vitamin C reduces oxidative stress markers in athletes"
        abstract = (
            "Vitamin C reduces oxidative stress markers in athletes. "
            "Supplementation with 1000mg daily lowered malondialdehyde levels by 18% "
            "after eight weeks of training."
        )
        conclusions = resource_parser._europe_pmc_sentences(title, abstract)
        self.assertEqual(len(conclusions), 1)
        self.assertIn("malondialdehyde levels by 18%", conclusions[0])

    def test_empty_abstract_returns_empty_list(self) -> None:
        self.assertEqual(resource_parser._europe_pmc_sentences("Some Title", ""), [])


class ParseEuropePmcTests(unittest.TestCase):
    """`_parse_europe_pmc` end to end — the actual reported bug and its
    fix.
    """

    def test_missing_abstract_text_produces_no_title_leak(self) -> None:
        # This is the literal reported bug: "Europe PMC ('Title'): Title."
        # Through Phase 41, a missing abstractText fell back to using the
        # title as the abstract. It must now produce nothing at all.
        payload = _single_result_payload(
            "Creatine supplementation improves muscle strength: a review"
        )
        conclusions = resource_parser._parse_europe_pmc(payload)
        self.assertEqual(conclusions, [])

    def test_empty_string_abstract_text_produces_no_title_leak(self) -> None:
        payload = _single_result_payload(
            "Creatine supplementation improves muscle strength: a review",
            abstract_text="   ",
        )
        self.assertEqual(resource_parser._parse_europe_pmc(payload), [])

    def test_no_result_ever_equals_provider_title_prefix_pattern(self) -> None:
        # Guards against ANY regression back to the "('Title'): Title."
        # shape, not just the exact missing-field case above.
        payload = _single_result_payload(
            "Omega-3 fatty acids and cardiovascular health",
        )
        conclusions = resource_parser._parse_europe_pmc(payload)
        title_leak = "Europe PMC ('Omega-3 fatty acids and cardiovascular health'): Omega-3 fatty acids and cardiovascular health."
        self.assertNotIn(title_leak, conclusions)

    def test_html_entities_and_real_tags_are_cleaned(self) -> None:
        payload = _single_result_payload(
            "Radiolabeled uptake study",
            abstract_text=(
                "RESULTS: Uptake of [&lt;sup&gt;18&lt;/sup&gt;F]FDG increased by "
                "15% after treatment in the <b>treatment</b> group compared with controls."
            ),
        )
        conclusions = resource_parser._parse_europe_pmc(payload)
        joined = " ".join(conclusions)
        self.assertIn("[18F]FDG", joined)
        self.assertNotIn("&lt;", joined)
        self.assertNotIn("&gt;", joined)
        self.assertNotIn("<b>", joined)
        self.assertNotIn("</b>", joined)

    def test_multiple_results_each_yield_their_own_conclusions(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "Study A",
                        "abstractText": "Study A found a 20% reduction in inflammation markers after 8 weeks.",
                    },
                    {
                        "title": "Study B",
                        "abstractText": "Study B observed improved insulin sensitivity in the treatment arm.",
                    },
                ]
            }
        }
        conclusions = resource_parser._parse_europe_pmc(payload)
        self.assertTrue(any(c.startswith("Europe PMC ('Study A'): ") for c in conclusions))
        self.assertTrue(any(c.startswith("Europe PMC ('Study B'): ") for c in conclusions))

    def test_missing_title_falls_back_to_untitled_label(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "abstractText": "This finding describes a genuine result with no title present at all.",
                    }
                ]
            }
        }
        conclusions = resource_parser._parse_europe_pmc(payload)
        self.assertTrue(
            all(c.startswith(f"Europe PMC ('{resource_parser._EUROPE_PMC_UNTITLED_LABEL}'): ") for c in conclusions)
        )

    def test_no_abstract_conclusions_log_line_fires_per_spec(self) -> None:
        payload = _single_result_payload("A Study With No Abstract At All")
        with self.assertLogs("app.services.resource_parser", level="INFO") as captured:
            resource_parser._parse_europe_pmc(payload)
        self.assertTrue(
            any(
                "[Europe PMC] No abstract conclusions found for title: A Study With No Abstract At All"
                in message
                for message in captured.output
            )
        )

    def test_malformed_payload_shapes_return_empty_list_not_an_exception(self) -> None:
        self.assertEqual(resource_parser._parse_europe_pmc({}), [])
        self.assertEqual(resource_parser._parse_europe_pmc({"resultList": {}}), [])
        self.assertEqual(resource_parser._parse_europe_pmc(None), [])
        self.assertEqual(resource_parser._parse_europe_pmc("not a dict"), [])
        self.assertEqual(
            resource_parser._parse_europe_pmc({"resultList": {"result": "not a list"}}), []
        )
        self.assertEqual(
            resource_parser._parse_europe_pmc({"resultList": {"result": ["not a dict"]}}), []
        )


class ParseResourceConclusionsEuropePmcDispatchTests(unittest.TestCase):
    """`parse_resource_conclusions`'s end-to-end behavior for
    `api_id="europe_pmc"` — confirms the Tier 3 generic-envelope-
    stringify fallback (Phase 30) is excluded for this provider, and that
    a genuinely empty result stays honestly empty rather than resurfacing
    raw envelope key/value text.
    """

    def test_no_abstract_does_not_fall_through_to_generic_envelope_stringify(self) -> None:
        payload = _single_result_payload("Creatine and strength: a systematic review")
        conclusions, failure_reason = resource_parser.parse_resource_conclusions(
            "europe_pmc", payload
        )
        self.assertEqual(conclusions, [])
        self.assertIsNotNone(failure_reason)
        # The Tier 3 bug this closes: stringifying {"resultList": {"result":
        # [{"title": ...}]}} produces telltale "resultList" / "result :"
        # envelope-key text — must never appear anywhere in the reason or
        # (redundantly, since conclusions is already asserted empty) the
        # conclusions list.
        self.assertNotIn("resultList", failure_reason)

    def test_real_abstract_is_extracted_and_sanitizer_still_applies(self) -> None:
        payload = _single_result_payload(
            "Iron deficiency in adolescents",
            abstract_text=(
                "RESULTS: Iron supplementation at 60mg daily improved hemoglobin "
                "levels by 1.2 g/dL over twelve weeks in the treatment group."
            ),
        )
        conclusions, failure_reason = resource_parser.parse_resource_conclusions(
            "europe_pmc", payload
        )
        self.assertIsNone(failure_reason)
        self.assertEqual(len(conclusions), 1)
        self.assertIn("hemoglobin levels by 1.2 g/dL", conclusions[0])
        for conclusion in conclusions:
            self.assertTrue(resource_parser.is_valid_human_conclusion(conclusion))


class MedlineplusSentencesSharedCleanerTests(unittest.TestCase):
    """Confirms `_medlineplus_sentences` picked up the same unescape fix
    via the shared `_clean_html_text` helper (Phase 42 bonus fix — same
    class of bug, same upstream XML-sourced field shape, not previously
    reported broken but fixed alongside its sibling parser rather than
    left with a latent version of the same issue).
    """

    def test_unescapes_entities_before_stripping_tags(self) -> None:
        conclusions = resource_parser._medlineplus_sentences(
            "Iodine",
            "Iodine deficiency during pregnancy [&lt;i&gt;in utero&lt;/i&gt;] can impair fetal brain development significantly.",
        )
        self.assertEqual(len(conclusions), 1)
        self.assertIn("[in utero]", conclusions[0])
        self.assertNotIn("&lt;", conclusions[0])


if __name__ == "__main__":
    unittest.main()
