"""Integration tests for the FastAPI app: POST /api/scan and GET /api/ingredients.

Uses FastAPI's TestClient against the real `app` from app.main, with the
`get_vision_parser` / `get_storage` dependencies (app.api.deps) overridden
to use a mocked VisionParserService and a ScanStorage pointed at a tmp_path
file -- so these tests never call Gemini and never touch the real
data/scanned_ingredients.json.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_grading_service, get_storage, get_vision_parser
from app.main import app
from app.schemas.scan import ScannedIngredient, ScannedProductMetadata, ScanResult
from app.services.grading_service import GradingError, GradingResult, GradingStats, SifgConsensus
from app.services.storage import ScanStorage
from app.services.vision_parser import VisionParsingError


def make_scan_result(scan_id: str = "scan_test001") -> ScanResult:
    return ScanResult(
        scan_id=scan_id,
        scanned_at=datetime.now(timezone.utc),
        product=ScannedProductMetadata(
            brand_name="Example Labs",
            product_name="Daily Focus Boost",
            serving_size="2 capsules",
            servings_per_container=30,
        ),
        ingredients=[
            ScannedIngredient(name="Ashwagandha", form="KSM-66 Root Extract", amount=600, unit="mg"),
        ],
    )


@pytest.fixture
def mock_vision_parser() -> AsyncMock:
    mock = AsyncMock()
    mock.scan_label.return_value = make_scan_result()
    return mock


@pytest.fixture
def storage(tmp_path: Path) -> ScanStorage:
    return ScanStorage(path=tmp_path / "scanned_ingredients.json")


SAMPLE_CONSENSUS = SifgConsensus(
    sifg_grade="B+",
    sifg_score=78.0,
    efficacy_safety_evaluation="Generally well tolerated in the reviewed studies.",
    dosage_appropriateness="Within the range studied.",
    evidence_summary="Based on the provided studies.",
    studies_considered=["12345678"],
)

SAMPLE_STATS = GradingStats(
    papers_found=2,
    papers_analyzed=2,
    search_queries=["Ashwagandha KSM-66 Root Extract supplementation"],
    grading_duration_seconds=1.234,
    model_used="gemini-2.0-flash",
)

SAMPLE_GRADING_RESULT = GradingResult(consensus=SAMPLE_CONSENSUS, stats=SAMPLE_STATS)


@pytest.fixture
def mock_grading_service() -> AsyncMock:
    mock = AsyncMock()
    mock.grade_ingredient.return_value = SAMPLE_GRADING_RESULT
    return mock


@pytest.fixture
def client(mock_vision_parser, storage, mock_grading_service):
    app.dependency_overrides[get_vision_parser] = lambda: mock_vision_parser
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_grading_service] = lambda: mock_grading_service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestScanEndpoint:
    def test_scan_returns_201_with_full_result(self, client):
        response = client.post("/api/scan", files={"file": ("label.png", b"fake-image-bytes", "image/png")})

        assert response.status_code == 201
        body = response.json()
        assert body["scan_id"] == "scan_test001"
        assert body["product"]["brand_name"] == "Example Labs"
        assert len(body["ingredients"]) == 1
        assert body["ingredients"][0]["name"] == "Ashwagandha"
        assert body["ingredients"][0]["form"] == "KSM-66 Root Extract"
        assert body["ingredients"][0]["amount"] == 600
        assert body["ingredients"][0]["unit"] == "mg"

    def test_scan_calls_vision_parser_with_uploaded_bytes(self, client, mock_vision_parser):
        client.post("/api/scan", files={"file": ("label.png", b"specific-bytes-here", "image/png")})

        mock_vision_parser.scan_label.assert_called_once()
        called_image = mock_vision_parser.scan_label.call_args.args[0]
        assert called_image == b"specific-bytes-here"

    def test_scan_appends_result_to_storage(self, client, storage):
        client.post("/api/scan", files={"file": ("label.png", b"bytes", "image/png")})

        records = asyncio.run(storage.list_all())
        assert len(records) == 1
        assert records[0]["scan_id"] == "scan_test001"

    def test_scan_rejects_non_image_content_type(self, client):
        response = client.post("/api/scan", files={"file": ("label.pdf", b"%PDF-1.4", "application/pdf")})
        assert response.status_code == 400

    def test_scan_rejects_empty_file(self, client):
        response = client.post("/api/scan", files={"file": ("label.png", b"", "image/png")})
        assert response.status_code == 400

    def test_scan_returns_502_when_vision_parsing_fails(self, client, mock_vision_parser):
        mock_vision_parser.scan_label.side_effect = VisionParsingError("Gemini call failed: boom")

        response = client.post("/api/scan", files={"file": ("label.png", b"bytes", "image/png")})

        assert response.status_code == 502
        assert "Gemini call failed" in response.json()["detail"]

    def test_failed_scan_is_not_saved_to_storage(self, client, mock_vision_parser, storage):
        mock_vision_parser.scan_label.side_effect = VisionParsingError("boom")

        client.post("/api/scan", files={"file": ("label.png", b"bytes", "image/png")})

        records = asyncio.run(storage.list_all())
        assert records == []


class TestListIngredientsEndpoint:
    """GET /api/ingredients -- everything saved in data/scanned_ingredients.json so far."""

    def test_returns_empty_list_when_nothing_scanned_yet(self, client):
        response = client.get("/api/ingredients")

        assert response.status_code == 200
        assert response.json() == []

    def test_returns_saved_scan_after_a_scan(self, client):
        client.post("/api/scan", files={"file": ("label.png", b"bytes", "image/png")})

        response = client.get("/api/ingredients")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["scan_id"] == "scan_test001"
        assert body[0]["product"]["brand_name"] == "Example Labs"
        assert body[0]["ingredients"][0]["name"] == "Ashwagandha"

    def test_multiple_scans_all_appear_in_order(self, client, mock_vision_parser):
        mock_vision_parser.scan_label.side_effect = [
            make_scan_result("scan_one"),
            make_scan_result("scan_two"),
            make_scan_result("scan_three"),
        ]

        for _ in range(3):
            client.post("/api/scan", files={"file": ("label.png", b"bytes", "image/png")})

        response = client.get("/api/ingredients")

        scan_ids = [entry["scan_id"] for entry in response.json()]
        assert scan_ids == ["scan_one", "scan_two", "scan_three"]


class TestGradeIngredientEndpoint:
    """POST /api/ingredients/{ingredient_id}/grade -- grades exactly one saved ingredient."""

    def _seed_one_ingredient(self, client) -> str:
        client.post("/api/scan", files={"file": ("label.png", b"bytes", "image/png")})
        body = client.get("/api/ingredients").json()
        return body[0]["ingredients"][0]["ingredient_id"]

    def test_returns_404_for_an_unknown_ingredient_id(self, client):
        response = client.post("/api/ingredients/ing_does_not_exist/grade")

        assert response.status_code == 404

    def test_grades_the_ingredient_and_returns_the_updated_object(self, client, mock_grading_service):
        ingredient_id = self._seed_one_ingredient(client)

        response = client.post(f"/api/ingredients/{ingredient_id}/grade")

        assert response.status_code == 200
        body = response.json()
        assert body["ingredient_id"] == ingredient_id
        assert body["grade_status"] == "graded"
        assert body["sifg_grade"] == "B+"
        assert body["sifg_score"] == 78.0
        assert body["evidence_summary"] == "Based on the provided studies."
        assert body["raw_consensus"]["sifg_grade"] == "B+"
        assert body["graded_at"] is not None
        assert body["grading_stats"]["papers_found"] == 2
        assert body["grading_stats"]["papers_analyzed"] == 2
        assert body["grading_stats"]["search_queries"] == ["Ashwagandha KSM-66 Root Extract supplementation"]
        assert body["grading_stats"]["grading_duration_seconds"] == 1.234
        assert body["grading_stats"]["model_used"] == "gemini-2.0-flash"
        mock_grading_service.grade_ingredient.assert_called_once()

    def test_grading_persists_to_storage(self, client, storage):
        ingredient_id = self._seed_one_ingredient(client)

        client.post(f"/api/ingredients/{ingredient_id}/grade")

        records = asyncio.run(storage.list_all())
        graded = records[0]["ingredients"][0]
        assert graded["grade_status"] == "graded"
        assert graded["sifg_grade"] == "B+"

    def test_only_the_requested_ingredient_is_graded(self, client, mock_vision_parser, storage):
        # A scan with two ingredients -- only one gets graded.
        mock_vision_parser.scan_label.return_value = ScanResult(
            scan_id="scan_two_ingredients",
            scanned_at=datetime.now(timezone.utc),
            product=ScannedProductMetadata(brand_name="Example Labs"),
            ingredients=[
                ScannedIngredient(name="Ashwagandha", amount=600, unit="mg"),
                ScannedIngredient(name="Zinc", amount=15, unit="mg"),
            ],
        )
        client.post("/api/scan", files={"file": ("label.png", b"bytes", "image/png")})
        body = client.get("/api/ingredients").json()
        ashwagandha_id = body[0]["ingredients"][0]["ingredient_id"]
        zinc_id = body[0]["ingredients"][1]["ingredient_id"]

        client.post(f"/api/ingredients/{ashwagandha_id}/grade")

        records = asyncio.run(storage.list_all())
        by_id = {ing["ingredient_id"]: ing for ing in records[0]["ingredients"]}
        assert by_id[ashwagandha_id]["grade_status"] == "graded"
        assert by_id[zinc_id]["grade_status"] == "pending"  # untouched

    def test_returns_502_and_persists_failed_status_when_grading_errors(
        self, client, mock_grading_service, storage
    ):
        ingredient_id = self._seed_one_ingredient(client)
        mock_grading_service.grade_ingredient.side_effect = GradingError("Gemini call failed: boom")

        response = client.post(f"/api/ingredients/{ingredient_id}/grade")

        assert response.status_code == 502
        assert "Gemini call failed" in response.json()["detail"]

        records = asyncio.run(storage.list_all())
        assert records[0]["ingredients"][0]["grade_status"] == "failed"
        assert records[0]["ingredients"][0]["graded_at"] is not None

    def test_failure_with_partial_stats_persists_grading_stats_too(self, client, mock_grading_service, storage):
        ingredient_id = self._seed_one_ingredient(client)
        partial_stats = GradingStats(
            papers_found=3,
            papers_analyzed=3,
            search_queries=["Ashwagandha KSM-66 Root Extract supplementation"],
            grading_duration_seconds=0.5,
            model_used="gemini-2.0-flash",
        )
        mock_grading_service.grade_ingredient.side_effect = GradingError(
            "Gemini call failed: boom", stats=partial_stats
        )

        client.post(f"/api/ingredients/{ingredient_id}/grade")

        records = asyncio.run(storage.list_all())
        graded = records[0]["ingredients"][0]
        assert graded["grade_status"] == "failed"
        assert graded["grading_stats"]["papers_found"] == 3
        assert graded["grading_stats"]["model_used"] == "gemini-2.0-flash"

    def test_failure_without_stats_leaves_grading_stats_null(self, client, mock_grading_service, storage):
        ingredient_id = self._seed_one_ingredient(client)
        mock_grading_service.grade_ingredient.side_effect = GradingError("boom")  # no stats attached

        client.post(f"/api/ingredients/{ingredient_id}/grade")

        records = asyncio.run(storage.list_all())
        assert records[0]["ingredients"][0]["grading_stats"] is None

    def test_a_freshly_scanned_ingredient_starts_as_pending(self, client):
        ingredient_id = self._seed_one_ingredient(client)

        body = client.get("/api/ingredients").json()

        ingredient = body[0]["ingredients"][0]
        assert ingredient["ingredient_id"] == ingredient_id
        assert ingredient["grade_status"] == "pending"
        assert ingredient["sifg_grade"] is None
        assert ingredient["raw_consensus"] is None
        assert ingredient["grading_stats"] is None


class TestGetGradingServiceDependency:
    """Unit tests for app.api.deps.get_grading_service in isolation (no TestClient/app lifecycle)."""

    def test_raises_503_when_grading_service_not_configured(self):
        fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(grading_service=None)))

        with pytest.raises(HTTPException) as exc_info:
            get_grading_service(fake_request)  # type: ignore[arg-type]

        assert exc_info.value.status_code == 503

    def test_returns_grading_service_when_configured(self, mock_grading_service):
        fake_request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(grading_service=mock_grading_service))
        )

        assert get_grading_service(fake_request) is mock_grading_service  # type: ignore[arg-type]


class TestGetVisionParserDependency:
    """Unit tests for app.api.deps.get_vision_parser in isolation (no TestClient/app lifecycle)."""

    def test_raises_503_when_vision_parser_not_configured(self):
        fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(vision_parser=None)))

        with pytest.raises(HTTPException) as exc_info:
            get_vision_parser(fake_request)  # type: ignore[arg-type]

        assert exc_info.value.status_code == 503

    def test_returns_vision_parser_when_configured(self, mock_vision_parser):
        fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(vision_parser=mock_vision_parser)))

        assert get_vision_parser(fake_request) is mock_vision_parser  # type: ignore[arg-type]


class TestHealthCheck:
    def test_health_check_does_not_require_configuration(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
