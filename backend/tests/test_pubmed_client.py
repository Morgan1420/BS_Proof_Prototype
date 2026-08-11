"""Tests for app.services.pubmed_client -- real NCBI E-utilities calls, mocked at the httpx level.

No real network calls: `httpx.AsyncClient` is replaced with a small fake
that returns pre-scripted responses to each `.get()` call in order, so
these tests exercise the real esearch -> (optional fallback) -> efetch ->
parse pipeline without touching the network.
"""

import asyncio
from unittest.mock import patch

import httpx
import pytest

from app.core.config import Settings
from app.services.pubmed_client import (
    NCBI_EUTILS_BASE_URL,
    LiteratureSearchResult,
    LiteratureStudy,
    PubMedSearchError,
    _parse_abstract_text,
    search_literature,
)


def run(coro):
    return asyncio.run(coro)


def make_settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, gemini_api_key="test-key")
    defaults.update(overrides)
    return Settings(**defaults)


SAMPLE_ABSTRACT_TEXT = """1. J Altern Complement Med. 2019 Sep;25(9):899-908.

Efficacy and Safety of Ashwagandha Root Extract on Cognitive Function.

Author One A, Author Two B.

This randomized controlled trial evaluated the effects of standardized
ashwagandha root extract on cognitive function in adults.

PMID: 12345678

2. Phytomedicine. 2015;22(1):100-106.

Ashwagandha for Anxiety: A Systematic Review.

Author Three C.

A review of clinical trials assessing ashwagandha's effect on anxiety.

PMID: 87654321
"""


class FakeResponse:
    def __init__(self, json_data=None, text_data="", status_code=200):
        self._json_data = json_data
        self.text = text_data
        self.status_code = status_code

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]


class FakeAsyncClient:
    """Stands in for httpx.AsyncClient -- returns scripted responses to .get(), in call order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, params=None):
        self.calls.append((url, params))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def patch_async_client(fake_client: FakeAsyncClient):
    return patch("app.services.pubmed_client.httpx.AsyncClient", return_value=fake_client)


ESEARCH_TWO_RESULTS = FakeResponse(json_data={"esearchresult": {"idlist": ["12345678", "87654321"]}})
ESEARCH_ZERO_RESULTS = FakeResponse(json_data={"esearchresult": {"idlist": []}})
EFETCH_SUCCESS = FakeResponse(text_data=SAMPLE_ABSTRACT_TEXT)


class TestSearchLiteratureSuccess:
    def test_returns_parsed_studies_for_each_pmid(self):
        fake_client = FakeAsyncClient([ESEARCH_TWO_RESULTS, EFETCH_SUCCESS])

        with patch_async_client(fake_client):
            result = run(search_literature("Ashwagandha", "KSM-66 Root Extract", settings=make_settings()))

        assert isinstance(result, LiteratureSearchResult)
        assert len(result.studies) == 2
        assert result.studies[0].pmid == "12345678"
        assert "Cognitive Function" in result.studies[0].title
        assert result.studies[1].pmid == "87654321"
        assert "Anxiety" in result.studies[1].title
        assert result.papers_found == 2

    def test_returns_empty_result_when_esearch_finds_no_pmids_and_no_form_given(self):
        fake_client = FakeAsyncClient([ESEARCH_ZERO_RESULTS])

        with patch_async_client(fake_client):
            result = run(search_literature("Some Obscure Ingredient", settings=make_settings()))

        assert result.studies == []
        assert result.papers_found == 0
        # No form -- no fallback query, no second call.
        assert len(fake_client.calls) == 1
        assert len(result.queries_used) == 1

    def test_primary_query_includes_name_and_form(self):
        fake_client = FakeAsyncClient([ESEARCH_ZERO_RESULTS, ESEARCH_ZERO_RESULTS])

        with patch_async_client(fake_client):
            result = run(search_literature("Ashwagandha", "KSM-66 Root Extract", settings=make_settings()))

        _, params = fake_client.calls[0]
        assert "Ashwagandha" in params["term"]
        assert "KSM-66 Root Extract" in params["term"]
        assert result.queries_used[0] == params["term"]

    def test_omits_form_from_query_when_not_given(self):
        fake_client = FakeAsyncClient([ESEARCH_ZERO_RESULTS])

        with patch_async_client(fake_client):
            run(search_literature("Zinc", form=None, settings=make_settings()))

        _, params = fake_client.calls[0]
        assert params["term"].startswith("Zinc")

    def test_uses_pubmed_max_studies_setting_as_retmax(self):
        fake_client = FakeAsyncClient([ESEARCH_ZERO_RESULTS])
        settings = make_settings(pubmed_max_studies=2)

        with patch_async_client(fake_client):
            run(search_literature("Zinc", settings=settings))

        _, params = fake_client.calls[0]
        assert params["retmax"] == 2

    def test_includes_api_key_when_configured(self):
        fake_client = FakeAsyncClient([ESEARCH_TWO_RESULTS, EFETCH_SUCCESS])
        settings = make_settings(pubmed_api_key="my-ncbi-key")

        with patch_async_client(fake_client):
            run(search_literature("Ashwagandha", settings=settings))

        _, esearch_params = fake_client.calls[0]
        _, efetch_params = fake_client.calls[1]
        assert esearch_params["api_key"] == "my-ncbi-key"
        assert efetch_params["api_key"] == "my-ncbi-key"

    def test_omits_api_key_when_not_configured(self):
        fake_client = FakeAsyncClient([ESEARCH_ZERO_RESULTS])

        with patch_async_client(fake_client):
            run(search_literature("Zinc", settings=make_settings()))

        _, params = fake_client.calls[0]
        assert "api_key" not in params

    def test_calls_the_real_ncbi_eutils_endpoints(self):
        fake_client = FakeAsyncClient([ESEARCH_TWO_RESULTS, EFETCH_SUCCESS])

        with patch_async_client(fake_client):
            run(search_literature("Ashwagandha", settings=make_settings()))

        esearch_url, _ = fake_client.calls[0]
        efetch_url, _ = fake_client.calls[1]
        assert esearch_url == f"{NCBI_EUTILS_BASE_URL}/esearch.fcgi"
        assert efetch_url == f"{NCBI_EUTILS_BASE_URL}/efetch.fcgi"


class TestFallbackQuery:
    """A form-specific primary query that finds nothing falls back to one broader, name-only query."""

    def test_falls_back_to_a_second_query_when_primary_finds_nothing_and_form_was_given(self):
        fake_client = FakeAsyncClient([ESEARCH_ZERO_RESULTS, ESEARCH_TWO_RESULTS, EFETCH_SUCCESS])

        with patch_async_client(fake_client):
            result = run(search_literature("Ashwagandha", "KSM-66 Root Extract", settings=make_settings()))

        assert len(result.queries_used) == 2
        assert result.papers_found == 2
        assert len(fake_client.calls) == 3  # primary esearch, fallback esearch, efetch

    def test_fallback_query_is_broader_and_form_only_omitted(self):
        fake_client = FakeAsyncClient([ESEARCH_ZERO_RESULTS, ESEARCH_ZERO_RESULTS])

        with patch_async_client(fake_client):
            result = run(search_literature("Ashwagandha", "KSM-66 Root Extract", settings=make_settings()))

        primary_query, fallback_query = result.queries_used
        assert "KSM-66 Root Extract" in primary_query
        assert "KSM-66 Root Extract" not in fallback_query
        assert "Ashwagandha" in fallback_query

    def test_no_fallback_when_no_form_was_given(self):
        fake_client = FakeAsyncClient([ESEARCH_ZERO_RESULTS])

        with patch_async_client(fake_client):
            result = run(search_literature("Zinc", form=None, settings=make_settings()))

        assert len(result.queries_used) == 1
        assert len(fake_client.calls) == 1

    def test_no_fallback_when_primary_query_already_found_results(self):
        fake_client = FakeAsyncClient([ESEARCH_TWO_RESULTS, EFETCH_SUCCESS])

        with patch_async_client(fake_client):
            result = run(search_literature("Ashwagandha", "KSM-66 Root Extract", settings=make_settings()))

        assert len(result.queries_used) == 1
        assert len(fake_client.calls) == 2  # esearch + efetch, no second esearch

    def test_still_zero_results_after_both_queries_fail_to_find_anything(self):
        fake_client = FakeAsyncClient([ESEARCH_ZERO_RESULTS, ESEARCH_ZERO_RESULTS])

        with patch_async_client(fake_client):
            result = run(search_literature("Ashwagandha", "KSM-66 Root Extract", settings=make_settings()))

        assert result.studies == []
        assert result.papers_found == 0
        assert len(result.queries_used) == 2


class TestSearchLiteratureFailure:
    def test_raises_pubmed_search_error_on_network_failure(self):
        fake_client = FakeAsyncClient([httpx.ConnectError("connection refused")])

        with patch_async_client(fake_client):
            with pytest.raises(PubMedSearchError):
                run(search_literature("Ashwagandha", settings=make_settings()))

    def test_raises_pubmed_search_error_on_timeout(self):
        fake_client = FakeAsyncClient([httpx.ReadTimeout("timed out")])

        with patch_async_client(fake_client):
            with pytest.raises(PubMedSearchError):
                run(search_literature("Ashwagandha", settings=make_settings()))

    def test_raises_pubmed_search_error_on_non_2xx_status(self):
        fake_client = FakeAsyncClient([FakeResponse(status_code=500)])

        with patch_async_client(fake_client):
            with pytest.raises(PubMedSearchError):
                run(search_literature("Ashwagandha", settings=make_settings()))

    def test_raises_pubmed_search_error_on_unexpected_esearch_shape(self):
        fake_client = FakeAsyncClient([FakeResponse(json_data={"unexpected": "shape"})])

        with patch_async_client(fake_client):
            with pytest.raises(PubMedSearchError):
                run(search_literature("Ashwagandha", settings=make_settings()))

    def test_efetch_failure_after_a_successful_esearch_still_raises(self):
        fake_client = FakeAsyncClient([ESEARCH_TWO_RESULTS, FakeResponse(status_code=503)])

        with patch_async_client(fake_client):
            with pytest.raises(PubMedSearchError):
                run(search_literature("Ashwagandha", settings=make_settings()))

    def test_error_carries_the_queries_attempted_before_failure(self):
        fake_client = FakeAsyncClient([ESEARCH_TWO_RESULTS, FakeResponse(status_code=503)])

        with patch_async_client(fake_client):
            with pytest.raises(PubMedSearchError) as exc_info:
                run(search_literature("Ashwagandha", "KSM-66 Root Extract", settings=make_settings()))

        assert len(exc_info.value.queries_attempted) == 1
        assert "Ashwagandha" in exc_info.value.queries_attempted[0]

    def test_error_on_a_failed_fallback_query_carries_both_queries_attempted(self):
        fake_client = FakeAsyncClient([ESEARCH_ZERO_RESULTS, FakeResponse(status_code=503)])

        with patch_async_client(fake_client):
            with pytest.raises(PubMedSearchError) as exc_info:
                run(search_literature("Ashwagandha", "KSM-66 Root Extract", settings=make_settings()))

        assert len(exc_info.value.queries_attempted) == 2


class TestParseAbstractText:
    def test_parses_two_entries_with_pmids_and_titles(self):
        studies = _parse_abstract_text(SAMPLE_ABSTRACT_TEXT)

        assert len(studies) == 2
        assert all(isinstance(s, LiteratureStudy) for s in studies)
        assert studies[0].pmid == "12345678"
        assert studies[1].pmid == "87654321"

    def test_skips_entries_with_no_pmid(self):
        text = "1. Some Title With No PMID Line\n\nJust some text with no PMID at all.\n"

        studies = _parse_abstract_text(text)

        assert studies == []

    def test_empty_text_returns_empty_list(self):
        assert _parse_abstract_text("") == []

    def test_abstract_is_truncated_to_max_length(self):
        long_body = "word " * 1000
        text = f"1. Title\n\n{long_body}\n\nPMID: 11111111\n"

        studies = _parse_abstract_text(text)

        assert len(studies) == 1
        assert len(studies[0].abstract) <= 1500
