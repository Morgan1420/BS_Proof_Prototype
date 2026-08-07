"""Integration tests for the FastAPI app: POST /api/v1/scan and GET /api/v1/ingredients/{id}.

Uses FastAPI's TestClient against the real `app` from app.main, with the
GradingPipeline dependency (app.api.deps.get_pipeline) overridden to use
mocked VisionParserService / PubMedService / ConsensusEngine -- so these
tests never call Gemini or PubMed and don't require real API keys.

TestClient runs FastAPI's BackgroundTasks synchronously as part of the
request/response cycle, so polling GET /ingredients/{id} immediately
after POST /scan reliably sees the background-graded result rather than
a "still processing" state -- one test exercises that pending state
directly, by calling pipeline.start_scan() without running the
background grading step.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_pipeline
from app.main import app
from app.schemas.ingredient_grade import EvidenceGrade, EvidenceLevel, EvidenceSummary, ValidatedClaim
from app.schemas.product import MatchStatus, ProductIngredient, ProductMetadata, StructuredProductPayload
from app.services.consensus_engine import ConsensusEvaluationResult
from app.services.pipeline import GradingPipeline
from app.services.pubmed_service import PubMedPaper


def make_payload(ingredients=None) -> StructuredProductPayload:
    return StructuredProductPayload(
        match_status=MatchStatus.DRAFT,
        similarity_score=None,
        product_metadata=ProductMetadata(
            product_id="prod_test",
            brand_name="Example Labs",
            product_name="Daily Focus Boost",
            serving_size="2 capsules",
            servings_per_container=30,
        ),
        product_ingredients=ingredients if ingredients is not None else [],
    )


def make_evaluation_result(ingredient_id: str, canonical_name: str, composite_score: float = 85.0) -> ConsensusEvaluationResult:
    return ConsensusEvaluationResult(
        ingredient_id=ingredient_id,
        canonical_name=canonical_name,
        evidence_summary=EvidenceSummary(
            total_papers_analyzed=3,
            composite_score=composite_score,
            evidence_grade=EvidenceGrade.B,
            overall_confidence_score=0.5,
        ),
        validated_claims=[
            ValidatedClaim(
                claim="Test Claim", consensus_score=0.8, evidence_level=EvidenceLevel.MODERATE, supporting_studies_count=3
            )
        ],
    )


@pytest.fixture
def mock_vision_parser() -> AsyncMock:
    mock = AsyncMock()
    mock.parse_label_image.return_value = make_payload(
        ingredients=[ProductIngredient(raw_name="Ashwagandha", dose_amount=600, dose_unit="mg")]
    )
    return mock


@pytest.fixture
def mock_pubmed_service() -> AsyncMock:
    mock = AsyncMock()
    mock.search_ingredient.return_value = [
        PubMedPaper(pmid="1", title="A trial", abstract="An abstract.", publication_types=["Randomized Controlled Trial"])
    ]
    return mock


@pytest.fixture
def mock_consensus_engine() -> AsyncMock:
    mock = AsyncMock()

    async def _evaluate(ingredient_id, canonical_name, papers):
        return make_evaluation_result(ingredient_id, canonical_name)

    mock.evaluate_ingredient.side_effect = _evaluate
    return mock


@pytest.fixture
def pipeline(mock_vision_parser, mock_pubmed_service, mock_consensus_engine) -> GradingPipeline:
    return GradingPipeline(
        vision_parser=mock_vision_parser,
        pubmed_service=mock_pubmed_service,
        consensus_engine=mock_consensus_engine,
    )


@pytest.fixture
def client(pipeline):
    app.dependency_overrides[get_pipeline] = lambda: pipeline
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestScanEndpoint:
    def test_scan_returns_202_with_job_and_pending_ingredient(self, client):
        response = client.post("/api/v1/scan", files={"file": ("label.png", b"fake-image-bytes", "image/png")})

        assert response.status_code == 202
        body = response.json()
        assert body["job_id"].startswith("job_")
        # The response body is serialized from the job's state at the moment the
        # handler returns -- i.e. right after start_scan(), before the background
        # task (scheduled via BackgroundTasks.add_task) has run at all. So this is
        # "processing"/"pending", not "completed", even though TestClient will have
        # fully run that background task by the time THIS request returns control
        # to the test (see test_returns_completed_grade_after_scan below, which
        # confirms completion via a follow-up GET).
        assert body["status"] == "processing"
        assert body["product_metadata"]["brand_name"] == "Example Labs"
        assert len(body["ingredients"]) == 1
        assert body["ingredients"][0]["raw_name"] == "Ashwagandha"
        assert body["ingredients"][0]["dose_amount"] == 600
        assert body["ingredients"][0]["dose_unit"] == "mg"
        assert body["ingredients"][0]["status"] == "pending"
        assert body["ingredients"][0]["ingredient_id"].startswith("ing_")

    def test_scan_rejects_non_image_content_type(self, client):
        response = client.post("/api/v1/scan", files={"file": ("label.pdf", b"%PDF-1.4", "application/pdf")})
        assert response.status_code == 400

    def test_scan_rejects_empty_file(self, client):
        response = client.post("/api/v1/scan", files={"file": ("label.png", b"", "image/png")})
        assert response.status_code == 400

    def test_scan_with_no_ingredients_is_immediately_completed(self, client, mock_vision_parser):
        mock_vision_parser.parse_label_image.return_value = make_payload(ingredients=[])

        response = client.post("/api/v1/scan", files={"file": ("label.png", b"bytes", "image/png")})

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "completed"
        assert body["ingredients"] == []

    def test_scan_calls_vision_parser_with_uploaded_bytes(self, client, mock_vision_parser):
        client.post("/api/v1/scan", files={"file": ("label.png", b"specific-bytes-here", "image/png")})

        mock_vision_parser.parse_label_image.assert_called_once()
        called_image = mock_vision_parser.parse_label_image.call_args.args[0]
        assert called_image == b"specific-bytes-here"


class TestGetIngredientEndpoint:
    def test_returns_404_for_unknown_ingredient(self, client):
        response = client.get("/api/v1/ingredients/ing_does_not_exist")
        assert response.status_code == 404

    def test_returns_completed_grade_after_scan(self, client):
        scan_response = client.post("/api/v1/scan", files={"file": ("label.png", b"bytes", "image/png")})
        ingredient_id = scan_response.json()["ingredients"][0]["ingredient_id"]

        response = client.get(f"/api/v1/ingredients/{ingredient_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["ingredient_id"] == ingredient_id
        assert body["evidence_summary"]["composite_score"] == 85.0
        assert body["dosage_benchmarks"] is None
        assert body["safety_and_side_effects"] is None
        assert body["validated_claims"][0]["claim"] == "Test Claim"

    def test_returns_202_pending_before_background_grading_runs(self, client, pipeline):
        # Register the ingredient via start_scan directly, without invoking
        # run_grading_job, to observe the "pending" window an API client
        # would see between POST /scan returning and grading finishing in
        # a real (non-TestClient) deployment.
        job = asyncio.run(pipeline.start_scan(b"fake-bytes"))
        ingredient_id = job.ingredients[0].ingredient_id

        response = client.get(f"/api/v1/ingredients/{ingredient_id}")

        assert response.status_code == 202
        assert response.json()["status"] == "pending"

    def test_returns_failed_status_when_grading_errors(self, client, mock_pubmed_service):
        mock_pubmed_service.search_ingredient.side_effect = RuntimeError("PubMed is down")

        scan_response = client.post("/api/v1/scan", files={"file": ("label.png", b"bytes", "image/png")})
        ingredient_id = scan_response.json()["ingredients"][0]["ingredient_id"]

        response = client.get(f"/api/v1/ingredients/{ingredient_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert "PubMed is down" in body["error"]


class TestGetPipelineDependency:
    """Unit tests for app.api.deps.get_pipeline in isolation (no TestClient/app lifecycle)."""

    def test_raises_503_when_pipeline_not_configured(self):
        fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pipeline=None)))

        with pytest.raises(HTTPException) as exc_info:
            get_pipeline(fake_request)  # type: ignore[arg-type]

        assert exc_info.value.status_code == 503

    def test_returns_pipeline_when_configured(self, pipeline):
        fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pipeline=pipeline)))

        assert get_pipeline(fake_request) is pipeline  # type: ignore[arg-type]


class TestHealthCheck:
    def test_health_check_does_not_require_pipeline(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
