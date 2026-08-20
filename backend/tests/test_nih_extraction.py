"""Unit tests for the Phase 39 NIH resource extraction overhaul.

Deliberately written against Python's stdlib `unittest` rather than
pytest: this repo's `backend/venv` (and the sandbox this was authored in)
doesn't have pytest installed, and per this project's CLAUDE.md rule
("DO NOT execute package installation commands automatically"), these
tests can't install it themselves. `unittest` needs nothing beyond the
standard library, so `python3 -m unittest discover -s tests` (run from
`backend/`) works out of the box.

**Scope note.** `app/services/resource_parser.py` has zero third-party
dependencies (only `re`/`logging`/`xml.etree`/`typing`), so its NIH-
related functions (`_parse_medlineplus`, `parse_resource_conclusions`,
the `_NIH_API_IDS` constant) are fully covered here with real, executable
tests. `resource_fetcher.py::is_nih_domain` and
`resource_grader.py::_is_nih_domain` are pure/dependency-free
*themselves*, but importing the modules they live in transitively pulls
in `httpx` / `google-genai` respectively — neither is installed in this
sandbox's `backend/venv` (confirmed via `python3 -c "import httpx"`
failing with `ModuleNotFoundError`). Rather than skip them entirely,
`NihDomainDetectionTests` below attempts the import and uses
`unittest.skipUnless` to skip gracefully (with a clear reason) if it's
not available, so this file still runs cleanly end-to-end in an
environment missing those packages, and runs the real assertions in one
that has them (e.g. after the operator runs
`pip install -r requirements.txt` per this project's standard convention
of providing, not auto-running, install commands).
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

# backend/tests/test_nih_extraction.py -> parents[1] == backend/ — added to
# sys.path so `import app...` resolves the same way it does for every
# other script in this repo run from `backend/` as the working directory
# (e.g. `python3 -c "from app.db import reset_database; ..."` in
# db.py::reset_database's own docstring), without requiring the package to
# be pip-installed.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.services import resource_parser  # noqa: E402


class ParseMedlineplusConnectJsonTests(unittest.TestCase):
    """`_parse_medlineplus`'s primary (MedlinePlus Connect JSON) branch."""

    def test_splits_summary_into_discrete_sentence_conclusions(self) -> None:
        payload = {
            "feed": {
                "entry": [
                    {
                        "title": {"_value": "Vitamin D"},
                        "summary": {
                            "_value": (
                                "<div>Vitamin D helps the body absorb calcium "
                                "properly. It plays an important role in bone "
                                "health and density. Most healthy adults need "
                                "600 to 800 IU per day.</div>"
                            )
                        },
                    }
                ]
            }
        }

        conclusions = resource_parser._parse_medlineplus(payload)

        # Phase 39's core requirement: one discrete conclusion per
        # sentence, not one merged blob covering all three facts.
        self.assertEqual(len(conclusions), 3)
        for conclusion in conclusions:
            self.assertTrue(conclusion.startswith("MedlinePlus ('Vitamin D'): "))
        self.assertTrue(any("absorb calcium" in c for c in conclusions))
        self.assertTrue(any("bone health" in c for c in conclusions))
        self.assertTrue(any("600 to 800 IU" in c for c in conclusions))
        # HTML wrapper must be stripped, not leaked into a conclusion.
        self.assertFalse(any("<div>" in c or "</div>" in c for c in conclusions))

    def test_falls_back_to_untitled_label_when_title_missing(self) -> None:
        payload = {
            "feed": {
                "entry": [
                    {
                        "summary": {
                            "_value": "This is a long enough sentence to pass the floor."
                        }
                    }
                ]
            }
        }
        conclusions = resource_parser._parse_medlineplus(payload)
        self.assertEqual(len(conclusions), 1)
        self.assertTrue(conclusions[0].startswith("MedlinePlus ('Health Topic'): "))

    def test_handles_single_entry_not_wrapped_in_a_list(self) -> None:
        # Real Atom-JSON feeds sometimes collapse a single-item array to a
        # bare object — _parse_medlineplus_connect must tolerate both.
        payload = {
            "feed": {
                "entry": {
                    "title": {"_value": "Zinc"},
                    "summary": {"_value": "Zinc supports normal immune system function."},
                }
            }
        }
        conclusions = resource_parser._parse_medlineplus(payload)
        self.assertEqual(len(conclusions), 1)
        self.assertIn("Zinc supports normal immune system function.", conclusions[0])

    def test_empty_or_malformed_payload_returns_empty_list(self) -> None:
        self.assertEqual(resource_parser._parse_medlineplus({}), [])
        self.assertEqual(resource_parser._parse_medlineplus({"feed": {}}), [])
        self.assertEqual(resource_parser._parse_medlineplus(None), [])
        self.assertEqual(resource_parser._parse_medlineplus(12345), [])


class ParseMedlineplusWsearchXmlTests(unittest.TestCase):
    """`_parse_medlineplus`'s fallback (wsearch XML) branch."""

    def test_splits_full_summary_into_discrete_sentences(self) -> None:
        xml_text = (
            "<nlmSearchResult>"
            "<list>"
            '<document rank="0" url="https://medlineplus.gov/vitaminc.html">'
            '<content name="title">Vitamin C</content>'
            '<content name="organizationName">National Library of Medicine</content>'
            '<content name="FullSummary">Vitamin C is an antioxidant that the body '
            "needs to form blood vessels and cartilage. It also helps the body "
            "absorb iron from plant-based foods.</content>"
            "</document>"
            "</list>"
            "</nlmSearchResult>"
        )

        conclusions = resource_parser._parse_medlineplus(xml_text)

        self.assertEqual(len(conclusions), 2)
        for conclusion in conclusions:
            self.assertTrue(conclusion.startswith("MedlinePlus ('Vitamin C'): "))
        self.assertTrue(any("antioxidant" in c for c in conclusions))
        self.assertTrue(any("absorb iron" in c for c in conclusions))

    def test_malformed_xml_returns_empty_list_not_an_exception(self) -> None:
        self.assertEqual(resource_parser._parse_medlineplus("<not valid xml"), [])

    def test_empty_string_returns_empty_list(self) -> None:
        self.assertEqual(resource_parser._parse_medlineplus(""), [])


class ParseResourceConclusionsNihDispatchTests(unittest.TestCase):
    """`parse_resource_conclusions`'s NIH-specific behavior — dispatch,
    logging, and the general sanitizer/dedup pipeline all still applying
    to the newly-structured medlineplus_api path.
    """

    def test_medlineplus_api_now_routes_through_structured_parser(self) -> None:
        payload = {
            "feed": {
                "entry": [
                    {
                        "title": {"_value": "Magnesium"},
                        "summary": {
                            "_value": (
                                "Magnesium is involved in over 300 enzyme "
                                "reactions in the human body. Low magnesium "
                                "levels have been linked to muscle cramps."
                            )
                        },
                    }
                ]
            }
        }
        conclusions, failure_reason = resource_parser.parse_resource_conclusions(
            "medlineplus_api", payload
        )
        self.assertIsNone(failure_reason)
        self.assertEqual(len(conclusions), 2)
        self.assertTrue(all(c.startswith("MedlinePlus ('Magnesium'): ") for c in conclusions))

    def test_nih_api_ids_cover_the_three_configured_nih_sources(self) -> None:
        self.assertEqual(
            set(resource_parser._NIH_API_IDS),
            {"pubchem_pug_rest", "medlineplus_api", "dailymed_api"},
        )

    def test_nih_extractor_log_line_fires_for_nih_sources(self) -> None:
        payload = {
            "feed": {
                "entry": [
                    {
                        "title": {"_value": "Iron"},
                        "summary": {
                            "_value": "Iron deficiency is the most common nutritional deficiency worldwide."
                        },
                    }
                ]
            }
        }
        with self.assertLogs("app.services.resource_parser", level="INFO") as captured:
            conclusions, _ = resource_parser.parse_resource_conclusions(
                "medlineplus_api", payload, resource_url="https://medlineplus.gov/iron.html"
            )
        self.assertEqual(len(conclusions), 1)
        self.assertTrue(
            any(
                "[NIH Extractor]" in message and "medlineplus.gov" in message
                for message in captured.output
            )
        )

    def test_non_nih_source_does_not_emit_nih_extractor_log_line(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "A Study",
                        "abstractText": "This is a sufficiently long abstract sentence for the parser.",
                    }
                ]
            }
        }
        with self.assertLogs("app.services.resource_parser", level="INFO") as captured:
            # europe_pmc isn't NIH — still need at least one INFO log to
            # satisfy assertLogs, so trigger the FALLBACK/other info path
            # indirectly isn't guaranteed; instead just assert the NIH tag
            # never appears among whatever (if anything) was logged by
            # logging a sentinel line ourselves as a baseline.
            logging.getLogger("app.services.resource_parser").info("sentinel")
            conclusions, _ = resource_parser.parse_resource_conclusions(
                "europe_pmc", payload
            )
        self.assertEqual(len(conclusions), 1)
        self.assertFalse(any("[NIH Extractor]" in message for message in captured.output))

    def test_still_subject_to_is_valid_human_conclusion_sanitizer(self) -> None:
        # A "sentence" that's really just a stringified metadata fragment
        # must still be rejected even though it now comes from the
        # structured MedlinePlus parser rather than the old free-text
        # fallback — the Phase 28 sanitizer is provider-agnostic.
        payload = {
            "feed": {
                "entry": [
                    {
                        "title": {"_value": "Test"},
                        "summary": {"_value": "https://example.com/some/boilerplate/link/here"},
                    }
                ]
            }
        }
        conclusions, failure_reason = resource_parser.parse_resource_conclusions(
            "medlineplus_api", payload
        )
        self.assertEqual(conclusions, [])
        self.assertIsNotNone(failure_reason)


@unittest.skipUnless(
    (lambda: (__import__("importlib").util.find_spec("httpx") is not None))(),
    "httpx is not installed in this environment — resource_fetcher.py "
    "(and therefore is_nih_domain) can't be imported. Run `pip install "
    "-r requirements.txt` (see backend/requirements.txt) to enable this "
    "test.",
)
class NihDomainDetectionTests(unittest.TestCase):
    """`resource_fetcher.py::is_nih_domain` — skipped in environments
    without `httpx` installed (see module docstring).
    """

    def test_recognizes_official_nih_and_medlineplus_domains(self) -> None:
        from app.services.resource_fetcher import is_nih_domain

        self.assertTrue(is_nih_domain("ods.od.nih.gov"))
        self.assertTrue(is_nih_domain("pubchem.ncbi.nlm.nih.gov"))
        self.assertTrue(is_nih_domain("dailymed.nlm.nih.gov"))
        self.assertTrue(is_nih_domain("nccih.nih.gov"))
        self.assertTrue(is_nih_domain("medlineplus.gov"))
        self.assertTrue(is_nih_domain("MEDLINEPLUS.GOV"))  # case-insensitive

    def test_rejects_non_nih_domains(self) -> None:
        from app.services.resource_fetcher import is_nih_domain

        self.assertFalse(is_nih_domain("fdc.nal.usda.gov"))
        self.assertFalse(is_nih_domain("health-products.canada.ca"))
        self.assertFalse(is_nih_domain("example.com"))
        self.assertFalse(is_nih_domain(None))
        self.assertFalse(is_nih_domain(""))


if __name__ == "__main__":
    unittest.main()
