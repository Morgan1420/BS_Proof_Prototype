"""Tests for app.services.literature_search -- the 4-source retrieval/dedup/ranking pipeline.

Layered like the module itself:
  - per-provider HTTP + parsing tests (httpx mocked, same FakeAsyncClient
    pattern as test_pubmed_client.py, one provider at a time)
  - pure-function tests for dedup, scoring, and top-N selection
  - orchestration tests for aggregate_literature, mocking each provider
    function directly (so these don't re-test HTTP parsing, just the
    parallel-fetch / dedup / rank / select / error-isolation wiring)
"""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.core.config import Settings
from app.services import literature_search as ls


def run(coro):
    return asyncio.run(coro)


def make_settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, gemini_api_key="test-key")
    defaults.update(overrides)
    return Settings(**defaults)


def make_paper(**overrides) -> ls.RawPaper:
    defaults = dict(source="PubMed", title="A Study", abstract="Abstract text.")
    defaults.update(overrides)
    return ls.RawPaper(**defaults)


class FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]


class FakeAsyncClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def patch_client(fake_client):
    return patch("app.services.literature_search.httpx.AsyncClient", return_value=fake_client)


# -- Europe PMC ----------------------------------------------------------------


EUROPEPMC_RESPONSE = {
    "resultList": {
        "result": [
            {
                "title": "Ashwagandha RCT",
                "abstractText": "A randomized trial.",
                "doi": "10.1000/abc",
                "pmid": "111",
                "citedByCount": 42,
                "pubYear": "2022",
                "pubTypeList": {"pubType": ["Randomized Controlled Trial", "Journal Article"]},
            },
            {
                "title": "Ashwagandha Observational Study",
                "abstractText": "An observational cohort.",
                "doi": None,
                "pmid": "222",
                "citedByCount": 5,
                "pubYear": "2010",
                "pubTypeList": {"pubType": ["Observational Study"]},
            },
        ]
    }
}


class TestEuropePMCProvider:
    def test_parses_results_into_raw_papers(self):
        fake_client = FakeAsyncClient(FakeResponse(json_data=EUROPEPMC_RESPONSE))

        with patch_client(fake_client):
            papers, queries = run(ls._search_europepmc("ashwagandha supplementation", make_settings()))

        assert len(papers) == 2
        assert papers[0].source == ls.EUROPE_PMC
        assert papers[0].title == "Ashwagandha RCT"
        assert papers[0].doi == "10.1000/abc"
        assert papers[0].pmid == "111"
        assert papers[0].citation_count == 42
        assert papers[0].publication_year == 2022
        assert "Randomized Controlled Trial" in papers[0].study_type
        assert queries == ["ashwagandha supplementation"]

    def test_calls_the_real_europepmc_endpoint(self):
        fake_client = FakeAsyncClient(FakeResponse(json_data={"resultList": {"result": []}}))

        with patch_client(fake_client):
            run(ls._search_europepmc("q", make_settings()))

        url, params, _ = fake_client.calls[0]
        assert url == ls.EUROPEPMC_SEARCH_URL
        assert params["query"] == "q"

    def test_empty_results_return_empty_list(self):
        fake_client = FakeAsyncClient(FakeResponse(json_data={"resultList": {"result": []}}))

        with patch_client(fake_client):
            papers, _ = run(ls._search_europepmc("q", make_settings()))

        assert papers == []

    def test_missing_resultlist_key_treated_as_empty(self):
        fake_client = FakeAsyncClient(FakeResponse(json_data={}))

        with patch_client(fake_client):
            papers, _ = run(ls._search_europepmc("q", make_settings()))

        assert papers == []

    def test_raises_literature_provider_error_on_http_failure(self):
        fake_client = FakeAsyncClient(httpx.ConnectError("refused"))

        with patch_client(fake_client):
            with pytest.raises(ls.LiteratureProviderError) as exc_info:
                run(ls._search_europepmc("q", make_settings()))
        assert exc_info.value.source == ls.EUROPE_PMC

    def test_raises_literature_provider_error_on_non_2xx(self):
        fake_client = FakeAsyncClient(FakeResponse(status_code=500))

        with patch_client(fake_client):
            with pytest.raises(ls.LiteratureProviderError):
                run(ls._search_europepmc("q", make_settings()))

    def test_one_malformed_record_does_not_drop_the_whole_batch(self):
        response = {
            "resultList": {
                "result": [
                    {"title": None, "pubTypeList": "not-a-dict-or-list-of-strings"},  # should still parse (title None)
                    EUROPEPMC_RESPONSE["resultList"]["result"][0],
                ]
            }
        }
        fake_client = FakeAsyncClient(FakeResponse(json_data=response))

        with patch_client(fake_client):
            papers, _ = run(ls._search_europepmc("q", make_settings()))

        assert len(papers) == 2


# -- OpenAlex --------------------------------------------------------------------


OPENALEX_RESPONSE = {
    "results": [
        {
            "title": "Ashwagandha and Cognition",
            "abstract_inverted_index": {"This": [0], "is": [1], "an": [2], "abstract": [3]},
            "doi": "https://doi.org/10.2000/xyz",
            "cited_by_count": 100,
            "publication_year": 2023,
            "type": "article",
            "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/333"},
        }
    ]
}


class TestOpenAlexProvider:
    def test_parses_results_into_raw_papers(self):
        fake_client = FakeAsyncClient(FakeResponse(json_data=OPENALEX_RESPONSE))

        with patch_client(fake_client):
            papers, queries = run(ls._search_openalex("ashwagandha supplementation", make_settings()))

        assert len(papers) == 1
        paper = papers[0]
        assert paper.source == ls.OPENALEX
        assert paper.title == "Ashwagandha and Cognition"
        assert paper.abstract == "This is an abstract"
        assert paper.doi == "10.2000/xyz"  # https://doi.org/ prefix stripped
        assert paper.pmid == "333"  # extracted from the pmid URL
        assert paper.citation_count == 100
        assert paper.publication_year == 2023
        assert queries == ["ashwagandha supplementation"]

    def test_calls_the_real_openalex_endpoint(self):
        fake_client = FakeAsyncClient(FakeResponse(json_data={"results": []}))

        with patch_client(fake_client):
            run(ls._search_openalex("q", make_settings()))

        url, params, _ = fake_client.calls[0]
        assert url == ls.OPENALEX_WORKS_URL
        assert params["search"] == "q"

    def test_missing_abstract_inverted_index_yields_none_abstract(self):
        response = {"results": [{"title": "T", "doi": None, "ids": {}}]}
        fake_client = FakeAsyncClient(FakeResponse(json_data=response))

        with patch_client(fake_client):
            papers, _ = run(ls._search_openalex("q", make_settings()))

        assert papers[0].abstract is None

    def test_raises_literature_provider_error_on_http_failure(self):
        fake_client = FakeAsyncClient(httpx.ReadTimeout("timed out"))

        with patch_client(fake_client):
            with pytest.raises(ls.LiteratureProviderError) as exc_info:
                run(ls._search_openalex("q", make_settings()))
        assert exc_info.value.source == ls.OPENALEX


class TestReconstructAbstract:
    def test_reconstructs_words_in_order(self):
        inv = {"This": [0], "is": [1], "an": [2], "abstract": [3]}
        assert ls._reconstruct_abstract(inv) == "This is an abstract"

    def test_handles_repeated_words(self):
        inv = {"the": [0, 3], "cat": [1], "and": [2], "dog": [4]}
        assert ls._reconstruct_abstract(inv) == "the cat and the dog"

    def test_none_input_returns_none(self):
        assert ls._reconstruct_abstract(None) is None

    def test_empty_dict_returns_none(self):
        assert ls._reconstruct_abstract({}) is None


# -- Semantic Scholar --------------------------------------------------------------


SEMANTIC_SCHOLAR_RESPONSE = {
    "data": [
        {
            "title": "Ashwagandha Meta-Analysis",
            "abstract": "A meta-analysis of trials.",
            "year": 2021,
            "citationCount": 77,
            "externalIds": {"DOI": "10.3000/def", "PubMed": "444"},
            "publicationTypes": ["MetaAnalysis", "Review"],
        }
    ]
}


class TestSemanticScholarProvider:
    def test_parses_results_into_raw_papers(self):
        fake_client = FakeAsyncClient(FakeResponse(json_data=SEMANTIC_SCHOLAR_RESPONSE))

        with patch_client(fake_client):
            papers, queries = run(ls._search_semantic_scholar("ashwagandha supplementation", make_settings()))

        assert len(papers) == 1
        paper = papers[0]
        assert paper.source == ls.SEMANTIC_SCHOLAR
        assert paper.doi == "10.3000/def"
        assert paper.pmid == "444"
        assert paper.citation_count == 77
        assert paper.publication_year == 2021
        assert "MetaAnalysis" in paper.study_type
        assert queries == ["ashwagandha supplementation"]

    def test_calls_the_real_semantic_scholar_endpoint(self):
        fake_client = FakeAsyncClient(FakeResponse(json_data={"data": []}))

        with patch_client(fake_client):
            run(ls._search_semantic_scholar("q", make_settings()))

        url, params, _ = fake_client.calls[0]
        assert url == ls.SEMANTIC_SCHOLAR_SEARCH_URL
        assert params["query"] == "q"

    def test_includes_api_key_header_when_configured(self):
        fake_client = FakeAsyncClient(FakeResponse(json_data={"data": []}))
        settings = make_settings(semantic_scholar_api_key="s2-key")

        with patch_client(fake_client):
            run(ls._search_semantic_scholar("q", settings))

        _, _, headers = fake_client.calls[0]
        assert headers["x-api-key"] == "s2-key"

    def test_omits_api_key_header_when_not_configured(self):
        fake_client = FakeAsyncClient(FakeResponse(json_data={"data": []}))

        with patch_client(fake_client):
            run(ls._search_semantic_scholar("q", make_settings()))

        _, _, headers = fake_client.calls[0]
        assert headers == {}

    def test_raises_literature_provider_error_on_http_failure(self):
        fake_client = FakeAsyncClient(httpx.ConnectError("refused"))

        with patch_client(fake_client):
            with pytest.raises(ls.LiteratureProviderError) as exc_info:
                run(ls._search_semantic_scholar("q", make_settings()))
        assert exc_info.value.source == ls.SEMANTIC_SCHOLAR


# -- Study type classification / year extraction --------------------------------------


class TestClassifyStudyType:
    def test_prefers_authoritative_hint_over_text_scan(self):
        assert ls._classify_study_type("nothing relevant here", hint="Randomized Controlled Trial") == (
            "Randomized Controlled Trial"
        )

    def test_falls_back_to_text_scan_when_no_hint(self):
        result = ls._classify_study_type("This is a systematic review of ashwagandha trials.")
        assert "systematic review" in result

    def test_returns_none_when_nothing_matches(self):
        assert ls._classify_study_type("a completely unrelated sentence about nothing") is None


class TestExtractYear:
    def test_extracts_a_four_digit_year(self):
        assert ls._extract_year("J Med. 2019 Sep;25(9):899-908.") == 2019

    def test_returns_none_with_no_year(self):
        assert ls._extract_year("no year here") is None

    def test_returns_none_for_empty_text(self):
        assert ls._extract_year(None) is None
        assert ls._extract_year("") is None


# -- Deduplication ------------------------------------------------------------------


class TestDeduplication:
    def test_dedups_by_doi(self):
        a = make_paper(source="PubMed", doi="10.1/ABC", title="T1")
        b = make_paper(source="Europe PMC", doi="10.1/abc", title="T1 (dup)")

        result = ls._deduplicate([a, b])

        assert len(result) == 1

    def test_dedups_by_pmid_when_no_doi(self):
        a = make_paper(source="PubMed", doi=None, pmid="123", title="T1")
        b = make_paper(source="OpenAlex", doi=None, pmid="123", title="T1 (dup)")

        result = ls._deduplicate([a, b])

        assert len(result) == 1

    def test_dedups_by_normalized_title_when_no_ids(self):
        a = make_paper(doi=None, pmid=None, title="Effects of Ashwagandha!")
        b = make_paper(doi=None, pmid=None, title="effects of ashwagandha")

        result = ls._deduplicate([a, b])

        assert len(result) == 1

    def test_keeps_distinct_papers(self):
        a = make_paper(doi="10.1/a", title="A")
        b = make_paper(doi="10.1/b", title="B")

        result = ls._deduplicate([a, b])

        assert len(result) == 2

    def test_prefers_the_more_complete_record_on_collision(self):
        sparse = make_paper(doi="10.1/x", pmid=None, abstract=None, citation_count=None, publication_year=None)
        rich = make_paper(
            doi="10.1/x", pmid="999", abstract="Full abstract", citation_count=10, publication_year=2020
        )

        result = ls._deduplicate([sparse, rich])

        assert len(result) == 1
        assert result[0].citation_count == 10

    def test_papers_with_no_identifying_info_are_all_kept(self):
        a = ls.RawPaper(source="X", title=None, abstract=None)
        b = ls.RawPaper(source="Y", title=None, abstract=None)

        result = ls._deduplicate([a, b])

        assert len(result) == 2


# -- Scoring ------------------------------------------------------------------------


class TestScoreStudyType:
    def test_rct_scores_max(self):
        assert ls._score_study_type("Randomized Controlled Trial") == ls.STUDY_TYPE_MAX_POINTS

    def test_meta_analysis_scores_max(self):
        assert ls._score_study_type("Meta-Analysis") == ls.STUDY_TYPE_MAX_POINTS

    def test_general_review_scores_less_than_max(self):
        score = ls._score_study_type("Review")
        assert 0 < score < ls.STUDY_TYPE_MAX_POINTS

    def test_observational_scores_between_review_and_zero(self):
        score = ls._score_study_type("Observational Study")
        assert 0 < score < ls._score_study_type("Review")

    def test_none_scores_zero(self):
        assert ls._score_study_type(None) == 0.0

    def test_unclassified_type_scores_low_but_nonzero(self):
        score = ls._score_study_type("Journal Article")
        assert 0 < score < ls._score_study_type("Observational Study")


class TestScoreCitationCount:
    def test_zero_or_none_scores_zero(self):
        assert ls._score_citation_count(0) == 0.0
        assert ls._score_citation_count(None) == 0.0

    def test_score_increases_with_citations(self):
        assert ls._score_citation_count(100) > ls._score_citation_count(10)

    def test_score_is_capped_at_max(self):
        assert ls._score_citation_count(10_000_000) == ls.CITATION_MAX_POINTS


class TestScoreRecency:
    CURRENT_YEAR = 2026

    def test_within_5_years_scores_max(self):
        assert ls._score_recency(2023, self.CURRENT_YEAR) == ls.RECENCY_MAX_POINTS

    def test_exactly_10_years_scores_zero(self):
        assert ls._score_recency(2016, self.CURRENT_YEAR) == 0.0

    def test_between_5_and_10_years_tapers(self):
        score_6y = ls._score_recency(2020, self.CURRENT_YEAR)
        score_9y = ls._score_recency(2017, self.CURRENT_YEAR)
        assert 0 < score_9y < score_6y < ls.RECENCY_MAX_POINTS

    def test_older_than_10_years_scores_zero(self):
        assert ls._score_recency(1990, self.CURRENT_YEAR) == 0.0

    def test_none_scores_zero(self):
        assert ls._score_recency(None, self.CURRENT_YEAR) == 0.0


class TestScoreKeywordMatch:
    def test_form_match_scores_half(self):
        score = ls._score_keyword_match("Effects of KSM-66 Root Extract", "KSM-66 Root Extract", [])
        assert score == ls.KEYWORD_MATCH_MAX_POINTS / 2

    def test_dose_match_scores_half(self):
        score = ls._score_keyword_match("A study of 600mg dosing", None, ["600mg"])
        assert score == ls.KEYWORD_MATCH_MAX_POINTS / 2

    def test_both_match_scores_max(self):
        score = ls._score_keyword_match("KSM-66 Root Extract at 600mg", "KSM-66 Root Extract", ["600mg"])
        assert score == ls.KEYWORD_MATCH_MAX_POINTS

    def test_no_match_scores_zero(self):
        score = ls._score_keyword_match("An unrelated title", "KSM-66 Root Extract", ["600mg"])
        assert score == 0.0

    def test_no_title_scores_zero(self):
        assert ls._score_keyword_match(None, "form", ["600"]) == 0.0


class TestBuildDoseTerms:
    def test_builds_terms_from_amount_and_unit(self):
        terms = ls._build_dose_terms(600.0, "mg")
        assert any("600" in t for t in terms)
        assert any("mg" in t for t in terms)

    def test_no_amount_returns_empty(self):
        assert ls._build_dose_terms(None, "mg") == []


class TestSelectTopPapers:
    def test_selects_highest_scoring_first(self):
        low = ls.ScoredPaper(paper=make_paper(title="Low"), score=10.0, breakdown={})
        high = ls.ScoredPaper(paper=make_paper(title="High"), score=90.0, breakdown={})

        selected = ls.select_top_papers([low, high], limit=20)

        assert selected[0].paper.title == "High"

    def test_respects_the_limit(self):
        scored = [ls.ScoredPaper(paper=make_paper(title=str(i)), score=float(i), breakdown={}) for i in range(30)]

        selected = ls.select_top_papers(scored, limit=20)

        assert len(selected) == 20

    def test_returns_fewer_than_limit_if_not_enough_papers(self):
        scored = [ls.ScoredPaper(paper=make_paper(), score=1.0, breakdown={}) for _ in range(3)]

        selected = ls.select_top_papers(scored, limit=20)

        assert len(selected) == 3

    def test_ties_broken_by_citation_count(self):
        low_cited = ls.ScoredPaper(paper=make_paper(title="A", citation_count=1), score=50.0, breakdown={})
        high_cited = ls.ScoredPaper(paper=make_paper(title="B", citation_count=100), score=50.0, breakdown={})

        selected = ls.select_top_papers([low_cited, high_cited], limit=20)

        assert selected[0].paper.title == "B"


# -- aggregate_literature orchestration ------------------------------------------------


def _async_result(papers, queries):
    return AsyncMock(return_value=(papers, queries))


class TestAggregateLiterature:
    def test_merges_results_from_every_provider(self):
        with patch.object(ls, "_search_pubmed", _async_result([make_paper(source="PubMed", doi="10.1/a")], ["q"])), \
             patch.object(ls, "_search_europepmc", _async_result([make_paper(source="Europe PMC", doi="10.1/b")], ["q"])), \
             patch.object(ls, "_search_openalex", _async_result([make_paper(source="OpenAlex", doi="10.1/c")], ["q"])), \
             patch.object(ls, "_search_semantic_scholar", _async_result([make_paper(source="Semantic Scholar", doi="10.1/d")], ["q"])):
            result = run(ls.aggregate_literature("Ashwagandha", "KSM-66", 600, "mg", settings=make_settings()))

        assert result.papers_found == 4
        assert result.provider_counts == {
            "PubMed": 1, "Europe PMC": 1, "OpenAlex": 1, "Semantic Scholar": 1,
        }
        assert result.provider_errors == {}

    def test_deduplicates_across_providers(self):
        shared = make_paper(source="PubMed", doi="10.1/shared", title="Shared Paper")
        with patch.object(ls, "_search_pubmed", _async_result([shared], ["q"])), \
             patch.object(ls, "_search_europepmc", _async_result([make_paper(source="Europe PMC", doi="10.1/shared")], ["q"])), \
             patch.object(ls, "_search_openalex", _async_result([], ["q"])), \
             patch.object(ls, "_search_semantic_scholar", _async_result([], ["q"])):
            result = run(ls.aggregate_literature("Ashwagandha", None, None, None, settings=make_settings()))

        assert result.papers_found == 1  # deduplicated down from 2 raw hits
        assert result.provider_counts["PubMed"] == 1
        assert result.provider_counts["Europe PMC"] == 1

    def test_one_provider_failing_does_not_block_the_others(self):
        with patch.object(ls, "_search_pubmed", AsyncMock(side_effect=ls.LiteratureProviderError("PubMed", "boom"))), \
             patch.object(ls, "_search_europepmc", _async_result([make_paper(source="Europe PMC", doi="10.1/b")], ["q"])), \
             patch.object(ls, "_search_openalex", _async_result([], ["q"])), \
             patch.object(ls, "_search_semantic_scholar", _async_result([], ["q"])):
            result = run(ls.aggregate_literature("Ashwagandha", None, None, None, settings=make_settings()))

        assert result.provider_counts["PubMed"] == 0
        assert "PubMed" in result.provider_errors
        assert result.papers_found == 1  # Europe PMC's paper still made it through

    def test_all_providers_failing_yields_zero_papers_and_every_error_recorded(self):
        error_mocks = {
            name: AsyncMock(side_effect=ls.LiteratureProviderError(name, "down"))
            for name in ("_search_pubmed", "_search_europepmc", "_search_openalex", "_search_semantic_scholar")
        }
        with patch.object(ls, "_search_pubmed", error_mocks["_search_pubmed"]), \
             patch.object(ls, "_search_europepmc", error_mocks["_search_europepmc"]), \
             patch.object(ls, "_search_openalex", error_mocks["_search_openalex"]), \
             patch.object(ls, "_search_semantic_scholar", error_mocks["_search_semantic_scholar"]):
            result = run(ls.aggregate_literature("Ashwagandha", None, None, None, settings=make_settings()))

        assert result.papers_found == 0
        assert len(result.provider_errors) == 4

    def test_selects_only_the_configured_top_n(self):
        many_papers = [make_paper(source="PubMed", doi=f"10.1/{i}", title=str(i)) for i in range(30)]
        with patch.object(ls, "_search_pubmed", _async_result(many_papers, ["q"])), \
             patch.object(ls, "_search_europepmc", _async_result([], ["q"])), \
             patch.object(ls, "_search_openalex", _async_result([], ["q"])), \
             patch.object(ls, "_search_semantic_scholar", _async_result([], ["q"])):
            result = run(
                ls.aggregate_literature(
                    "Ashwagandha", None, None, None, settings=make_settings(literature_top_papers_limit=20)
                )
            )

        assert result.papers_found == 30
        assert result.papers_analyzed == 20
        assert len(result.studies) == 20

    def test_result_studies_are_ranked_highest_score_first(self):
        weak = make_paper(source="PubMed", doi="10.1/weak", title="Weak", study_type=None)
        strong = make_paper(
            source="PubMed", doi="10.1/strong", title="Strong", study_type="Randomized Controlled Trial",
            citation_count=500, publication_year=2025,
        )
        with patch.object(ls, "_search_pubmed", _async_result([weak, strong], ["q"])), \
             patch.object(ls, "_search_europepmc", _async_result([], ["q"])), \
             patch.object(ls, "_search_openalex", _async_result([], ["q"])), \
             patch.object(ls, "_search_semantic_scholar", _async_result([], ["q"])):
            result = run(ls.aggregate_literature("Ashwagandha", None, None, None, settings=make_settings()))

        assert result.studies[0].title == "Strong"


class TestLogRetrievalSummary:
    def test_prints_the_exact_requested_format(self, capsys):
        result = ls.AggregatedLiteratureResult(
            studies=[make_paper()] * 3,
            queries_used=["q"],
            provider_counts={"PubMed": 3, "Europe PMC": 5, "OpenAlex": 2, "Semantic Scholar": 4},
            provider_errors={},
            papers_found=9,
            papers_analyzed=3,
        )

        ls.log_retrieval_summary(2, 5, 20, result)

        out = capsys.readouterr().out
        assert "[GRADING STEP 2/5] API Queries Completed:" in out
        assert "  - PubMed: 3 papers" in out
        assert "  - Europe PMC: 5 papers" in out
        assert "  - OpenAlex: 2 papers" in out
        assert "  - Semantic Scholar: 4 papers" in out
        assert "[GRADING STEP 2/5] Total Unique Papers Found: 9" in out
        assert "[GRADING STEP 2/5] Ranked and Selected Top 20 Papers for Gemini Analysis (3)" in out

    def test_marks_a_failed_provider_in_the_breakdown(self, capsys):
        result = ls.AggregatedLiteratureResult(
            provider_counts={"PubMed": 0, "Europe PMC": 5, "OpenAlex": 2, "Semantic Scholar": 4},
            provider_errors={"PubMed": "boom"},
            papers_found=11,
            papers_analyzed=11,
        )

        ls.log_retrieval_summary(2, 5, 20, result)

        out = capsys.readouterr().out
        assert "  - PubMed: 0 papers (FAILED)" in out
