"""Tests for app.services.pubmed_service.

Mocks Bio.Entrez's esearch/efetch/read calls directly at the module
level (``app.services.pubmed_service.Entrez``), so these tests never hit
the live NCBI API and don't depend on network access or a real
NCBI_ENTREZ_EMAIL.
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import Settings
from app.services.pubmed_service import (
    PubMedPaper,
    PubMedService,
    PubMedServiceError,
    RateLimiter,
)


def run(coro):
    """Run an async test coroutine without requiring pytest-asyncio."""
    return asyncio.run(coro)


class AttrString(str):
    """A str subclass carrying an ``.attributes`` dict, mimicking Bio.Entrez's StringElement.

    Real Entrez.read() output uses StringElement (a str subclass) with an
    `.attributes` dict for structured-abstract labels like "METHODS" /
    "RESULTS". Plain test dicts use ordinary str where the label doesn't
    matter, and AttrString where a test needs to exercise that branch.
    """

    def __new__(cls, value, attributes=None):
        obj = super().__new__(cls, value)
        obj.attributes = attributes or {}
        return obj


@pytest.fixture
def settings_with_email() -> Settings:
    return Settings(_env_file=None, ncbi_entrez_email="test@example.com", ncbi_api_key=None)


@pytest.fixture
def service(settings_with_email: Settings) -> PubMedService:
    return PubMedService(settings=settings_with_email)


SEARCH_RESULT = {"IdList": ["11111111", "22222222"], "Count": "2"}

FETCH_RESULT = {
    "PubmedArticle": [
        {
            "MedlineCitation": {
                "PMID": "11111111",
                "Article": {
                    "ArticleTitle": "Ashwagandha and Stress: A Randomized Controlled Trial",
                    "Abstract": {
                        "AbstractText": [
                            AttrString(
                                "This randomized controlled trial enrolled n = 120 adults.",
                                {"Label": "METHODS"},
                            ),
                            AttrString(
                                "Ashwagandha significantly reduced perceived stress.",
                                {"Label": "RESULTS"},
                            ),
                        ]
                    },
                    "Journal": {"JournalIssue": {"PubDate": {"Year": "2021", "Month": "Jun"}}},
                    "PublicationTypeList": ["Randomized Controlled Trial", "Journal Article"],
                },
                "CoiStatement": "The authors declare no conflict of interest.",
            }
        },
        {
            "MedlineCitation": {
                "PMID": "22222222",
                "Article": {
                    "ArticleTitle": "Ashwagandha: A Systematic Review",
                    "Abstract": {"AbstractText": ["We reviewed 340 participants across 8 trials."]},
                    "Journal": {"JournalIssue": {"PubDate": {"MedlineDate": "2022 Jan-Feb"}}},
                    "PublicationTypeList": ["Systematic Review"],
                },
            }
        },
    ]
}


def patch_entrez(esearch_return=None, efetch_return=None, read_side_effect=None):
    """Patch Entrez.esearch/efetch (to dummy handles) and Entrez.read (with the given side_effect)."""
    return (
        patch("app.services.pubmed_service.Entrez.esearch", return_value=esearch_return or MagicMock()),
        patch("app.services.pubmed_service.Entrez.efetch", return_value=efetch_return or MagicMock()),
        patch("app.services.pubmed_service.Entrez.read", side_effect=read_side_effect),
    )


class TestSearchIngredientSuccess:
    def test_returns_parsed_papers_for_valid_response(self, service):
        p1, p2, p3 = patch_entrez(read_side_effect=[SEARCH_RESULT, FETCH_RESULT])
        with p1 as mock_esearch, p2 as mock_efetch, p3:
            papers = run(service.search_ingredient("Ashwagandha"))

        assert len(papers) == 2
        assert all(isinstance(p, PubMedPaper) for p in papers)

        first = papers[0]
        assert first.pmid == "11111111"
        assert first.title == "Ashwagandha and Stress: A Randomized Controlled Trial"
        assert "METHODS: This randomized controlled trial enrolled n = 120 adults." in first.abstract
        assert "RESULTS: Ashwagandha significantly reduced perceived stress." in first.abstract
        assert first.publication_date == "2021-Jun"
        assert "Randomized Controlled Trial" in first.publication_types
        assert first.sample_size == 120
        assert first.coi_statement == "The authors declare no conflict of interest."

        second = papers[1]
        assert second.pmid == "22222222"
        assert second.publication_date == "2022 Jan-Feb"
        assert second.sample_size == 340
        assert second.coi_statement is None

        mock_esearch.assert_called_once()
        mock_efetch.assert_called_once()

    def test_query_includes_all_publication_type_filters(self, service):
        p1, p2, p3 = patch_entrez(read_side_effect=[{"IdList": []}, {"PubmedArticle": []}])
        with p1 as mock_esearch, p2, p3:
            run(service.search_ingredient("Ashwagandha"))

        _, kwargs = mock_esearch.call_args
        term = kwargs["term"]
        assert "Ashwagandha" in term
        assert "Randomized Controlled Trial[pt]" in term
        assert "Clinical Trial[pt]" in term
        assert "Meta-Analysis[pt]" in term
        assert "Systematic Review[pt]" in term

    def test_respects_custom_retmax(self, service):
        p1, p2, p3 = patch_entrez(read_side_effect=[{"IdList": []}, {"PubmedArticle": []}])
        with p1 as mock_esearch, p2, p3:
            run(service.search_ingredient("Ashwagandha", retmax=5))

        _, kwargs = mock_esearch.call_args
        assert kwargs["retmax"] == 5


class TestSearchIngredientNoResults:
    def test_no_pmids_returns_empty_list_without_fetching(self, service):
        p1, p2, p3 = patch_entrez(read_side_effect=[{"IdList": []}])
        with p1, p2 as mock_efetch, p3:
            papers = run(service.search_ingredient("NonexistentIngredientXYZ"))

        assert papers == []
        mock_efetch.assert_not_called()


class TestSearchIngredientGracefulDegradation:
    """search_ingredient must never raise -- every failure mode degrades to a (partial) list."""

    def test_esearch_failure_returns_empty_list(self, service):
        with patch("app.services.pubmed_service.Entrez.esearch", side_effect=RuntimeError("network down")):
            papers = run(service.search_ingredient("Ashwagandha"))

        assert papers == []

    def test_efetch_failure_returns_empty_list(self, service):
        p1, _unused_p2, p3 = patch_entrez(read_side_effect=[{"IdList": ["11111111"]}])
        with p1, patch(
            "app.services.pubmed_service.Entrez.efetch", side_effect=RuntimeError("network down")
        ), p3:
            papers = run(service.search_ingredient("Ashwagandha"))

        assert papers == []

    def test_single_malformed_article_is_skipped_not_fatal(self, service):
        malformed_and_valid = {
            "PubmedArticle": [
                {"MedlineCitation": {}},  # missing PMID/Article -> should be skipped, not fatal
                FETCH_RESULT["PubmedArticle"][0],
            ]
        }
        p1, p2, p3 = patch_entrez(
            read_side_effect=[{"IdList": ["1", "11111111"]}, malformed_and_valid]
        )
        with p1, p2, p3:
            papers = run(service.search_ingredient("Ashwagandha"))

        assert len(papers) == 1
        assert papers[0].pmid == "11111111"


class TestConfiguration:
    def test_missing_email_raises_service_error(self):
        blank_email_settings = Settings.model_construct(
            app_name="BS Proof - Supplement Grading API",
            environment="development",
            debug=False,
            gemini_api_key=None,
            ncbi_entrez_email="   ",
            ncbi_api_key=None,
            database_url="sqlite:///./bs_proof.db",
        )

        with pytest.raises(PubMedServiceError, match="NCBI_ENTREZ_EMAIL is missing or invalid."):
            PubMedService(settings=blank_email_settings)

    def test_sets_entrez_email_from_settings(self, settings_with_email):
        from Bio import Entrez

        PubMedService(settings=settings_with_email)
        assert Entrez.email == "test@example.com"

    def test_sets_entrez_api_key_when_provided(self):
        from Bio import Entrez

        settings = Settings(_env_file=None, ncbi_entrez_email="test@example.com", ncbi_api_key="test-ncbi-key")
        PubMedService(settings=settings)
        assert Entrez.api_key == "test-ncbi-key"

    def test_leaves_entrez_api_key_unset_when_not_provided(self, settings_with_email):
        from Bio import Entrez

        PubMedService(settings=settings_with_email)
        assert Entrez.api_key is None


class TestSampleSizeExtraction:
    @pytest.mark.parametrize(
        "abstract,expected",
        [
            ("A trial with n = 45 participants.", 45),
            ("We enrolled 200 patients in this cohort.", 200),
            ("No sample size mentioned here.", None),
            ("n=12 healthy volunteers were recruited.", 12),
            ("A total of 88 women completed the study.", 88),
        ],
    )
    def test_extracts_expected_sample_size(self, abstract, expected):
        assert PubMedService._extract_sample_size(abstract) == expected


class TestRateLimiter:
    def test_enforces_minimum_interval_between_calls(self):
        limiter = RateLimiter(max_per_second=5)  # min interval 0.2s

        async def scenario() -> float:
            start = time.monotonic()
            await limiter.wait()
            await limiter.wait()
            return time.monotonic() - start

        elapsed = run(scenario())
        assert elapsed >= 0.18  # small slack below the 0.2s floor for scheduling jitter

    def test_first_call_does_not_wait(self):
        limiter = RateLimiter(max_per_second=1)  # min interval 1s -- would fail the test if it waited

        async def scenario() -> float:
            start = time.monotonic()
            await limiter.wait()
            return time.monotonic() - start

        elapsed = run(scenario())
        assert elapsed < 0.5

    def test_higher_rate_limit_when_api_key_present(self):
        from app.services.pubmed_service import (
            AUTHENTICATED_RATE_LIMIT_PER_SEC,
            UNAUTHENTICATED_RATE_LIMIT_PER_SEC,
        )

        with_key = PubMedService(
            settings=Settings(
                _env_file=None, ncbi_entrez_email="test@example.com", ncbi_api_key="a-key"
            )
        )
        without_key = PubMedService(
            settings=Settings(_env_file=None, ncbi_entrez_email="test@example.com", ncbi_api_key=None)
        )

        assert with_key._rate_limiter._min_interval == 1.0 / AUTHENTICATED_RATE_LIMIT_PER_SEC
        assert without_key._rate_limiter._min_interval == 1.0 / UNAUTHENTICATED_RATE_LIMIT_PER_SEC
