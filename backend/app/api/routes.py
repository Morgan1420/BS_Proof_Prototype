"""API route definitions for the supplement label scanner."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from app.db import get_session
from app.models.research import ResearchPaper
from app.models.supplement import Ingredient as IngredientRow
from app.schemas.dev import MockDataResetResponse
from app.schemas.research import (
    GradeIngredientResponse,
    GradePaperResponse,
    IngredientDetailResponse,
)
from app.schemas.search import FilterType, SearchResponse, SuggestResponse
from app.schemas.supplement import ProductDetailResponse, SupplementAnalysis
from app.services import grading as grading_service
from app.services import search as search_service
from app.services import storage
from app.services.paper_grader import PaperGradingError, grade_single_paper
from app.services.vision import VisionServiceError, analyze_supplement_label

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scan"])
search_router = APIRouter(prefix="/supplements", tags=["supplements"])
products_router = APIRouter(prefix="/products", tags=["products"])
ingredients_router = APIRouter(prefix="/ingredients", tags=["ingredients"])
papers_router = APIRouter(prefix="/papers", tags=["papers"])
dev_router = APIRouter(prefix="/dev", tags=["dev"])

# MIME types accepted for a supplement label photo upload.
ALLOWED_IMAGE_MIME_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/jpg", "image/webp"}
)


@router.post(
    "/scan",
    response_model=SupplementAnalysis,
    status_code=status.HTTP_200_OK,
    summary="Upload a supplement label image and extract structured ingredient data",
)
async def scan_label(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> SupplementAnalysis:
    """Analyze a supplement label photo and persist the structured result.

    1. Validates the upload is an accepted image MIME type.
    2. Sends the image bytes to Gemini (app/services/vision.py) to extract
       product name, serving size, and ingredient/dosage data.
    3. Commits the result as a Product row with related Ingredient rows to
       the SQLite database (app/services/storage.py, app/models/supplement.py).
    4. Returns the structured SupplementAnalysis payload to the caller.
    """
    if file.content_type not in ALLOWED_IMAGE_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file type '{file.content_type}'. "
                f"Expected one of: {', '.join(sorted(ALLOWED_IMAGE_MIME_TYPES))}."
            ),
        )

    try:
        contents = await file.read()
    except Exception as exc:  # noqa: BLE001 - surface as a clean 400 to the client
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to read uploaded file.",
        ) from exc

    if not contents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    try:
        # analyze_supplement_label() makes a blocking network call to the
        # Gemini API; run it off the event loop so other requests aren't
        # blocked while waiting on it.
        analysis = await run_in_threadpool(
            analyze_supplement_label, contents, file.content_type
        )
    except VisionServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Label analysis failed: {exc}",
        ) from exc

    try:
        # save_scan() makes blocking DB calls (session.add/commit/refresh);
        # keep it off the event loop, same as the Gemini call above.
        await run_in_threadpool(storage.save_scan, session, analysis)
    except Exception:  # noqa: BLE001
        # The analysis itself succeeded — don't fail the request just
        # because the DB write didn't work. Log it and move on.
        logger.exception("Failed to persist scan result to the database.")

    return analysis


@search_router.get(
    "/suggest",
    response_model=SuggestResponse,
    summary="Live autocomplete suggestions for product/ingredient names",
)
async def suggest_supplements(
    query: str = Query(..., min_length=1, description="Partial name to match."),
    limit: int = Query(5, ge=1, le=25),
    session: Session = Depends(get_session),
) -> SuggestResponse:
    """Returns up to `limit` matching product/ingredient names.

    No DB query is made if `query` is shorter than 3 characters (see
    app/services/search.py::MIN_SUGGEST_QUERY_LENGTH) — an empty
    suggestion list is returned instead.
    """
    suggestions = await run_in_threadpool(
        search_service.suggest, session, query, limit
    )
    return SuggestResponse(query=query, suggestions=suggestions)


@search_router.get(
    "/search",
    response_model=SearchResponse,
    summary="Search or browse products and ingredients",
)
async def search_supplements(
    query: Optional[str] = Query(
        None, description="Optional name filter (case-insensitive substring)."
    ),
    filter_type: FilterType = Query(
        FilterType.all, description="Restrict results to products, ingredients, or all."
    ),
    limit: int = Query(20, ge=1, le=20),
    session: Session = Depends(get_session),
) -> SearchResponse:
    """Returns up to `limit` (max 20) matching Product/Ingredient rows.

    If `query` is omitted, this browses all rows of the selected
    `filter_type` instead of filtering by name — used by the Library
    screen's "Products" / "Ingredients" explore cards.
    """
    results = await run_in_threadpool(
        search_service.search, session, query, filter_type, limit
    )
    return SearchResponse(
        query=query,
        filter_type=filter_type,
        count=len(results),
        results=results,
    )


@products_router.get(
    "/{product_id}",
    response_model=ProductDetailResponse,
    summary="Get a single product with its full linked-ingredient list",
)
async def get_product(
    product_id: int,
    session: Session = Depends(get_session),
) -> ProductDetailResponse:
    """Returns one Product plus every Ingredient linked to it (via an
    explicit join — see app/services/search.py::get_linked_ingredients for
    why this can't just rely on the lazy-loaded relationship).
    """
    detail = await run_in_threadpool(search_service.get_product_detail, session, product_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No product with id {product_id}.",
        )
    return detail


@ingredients_router.get(
    "/{ingredient_id}",
    response_model=IngredientDetailResponse,
    summary="Get a single canonical ingredient with its full stored research-paper list",
)
async def get_ingredient(
    ingredient_id: int,
    session: Session = Depends(get_session),
) -> IngredientDetailResponse:
    """Returns one canonical Ingredient plus every ResearchPaper stored
    for it (see app/models/research.py), for the standalone
    IngredientCard's "List of Studies" panel
    (src/components/StudiesList.tsx). Papers are whatever's already been
    persisted by a prior POST .../grade call — this endpoint itself never
    triggers new paper searches, it just reads what's there (an empty
    `papers` list if the ingredient hasn't been graded yet).
    """
    detail = await run_in_threadpool(
        search_service.get_ingredient_detail, session, ingredient_id
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No ingredient with id {ingredient_id}.",
        )
    return detail


@ingredients_router.post(
    "/{ingredient_id}/grade",
    response_model=GradeIngredientResponse,
    summary="[Phase 2, debug] Generate search keywords, fetch research papers, and assign a debug grade",
)
async def grade_ingredient(
    ingredient_id: int,
    session: Session = Depends(get_session),
) -> GradeIngredientResponse:
    """Runs the Phase 2 grading pipeline for a single ingredient:

    1. Looks up the Ingredient by id (404 if it doesn't exist).
    2. Asks Gemini for 3-5 targeted search keywords
       (app/services/research_keywords.py).
    3. Queries PubMed / Europe PMC / Semantic Scholar for each keyword and
       persists new, deduplicated ResearchPaper rows
       (app/services/paper_search.py).
    4. Sets `is_graded=True` and `grade_badge_text` to the total stored
       paper count formatted as "N / N / N" — a debug placeholder, not a
       real grading algorithm yet (see app/services/grading.py).

    This can take several seconds (it makes multiple external network
    calls server-side, sequentially) — the frontend shows a loading
    spinner on the grade button for the duration.
    """
    ingredient = session.get(IngredientRow, ingredient_id)
    if ingredient is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No ingredient with id {ingredient_id}.",
        )

    try:
        # The Gemini call and every paper-search HTTP call are blocking;
        # run the whole pipeline off the event loop, same reasoning as
        # /scan's analyze_supplement_label call above.
        paper_count = await run_in_threadpool(
            grading_service.grade_ingredient, session, ingredient
        )
    except grading_service.GradingError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Grading failed: {exc}",
        ) from exc

    # Full paper list (not just the count) so the frontend can refresh its
    # StudiesList panel immediately off this response, without a
    # follow-up GET /api/v1/ingredients/{id} call.
    papers = await run_in_threadpool(
        search_service.get_ingredient_papers, session, ingredient.id
    )

    return GradeIngredientResponse(
        status="success",
        ingredient_id=ingredient.id,
        is_graded=ingredient.is_graded,
        grade_badge_text=ingredient.grade_badge_text,
        papers_found=paper_count,
        papers=papers,
    )


@papers_router.post(
    "/{paper_id}/grade",
    response_model=GradePaperResponse,
    summary="[Phase 4, on-demand] Grade a single already-stored research paper",
)
async def grade_paper_route(
    paper_id: int,
    session: Session = Depends(get_session),
) -> GradePaperResponse:
    """Grades exactly one already-stored `ResearchPaper` row on demand —
    backs the frontend's gray "(-)" ungraded badge in StudiesList
    (tapping it calls this instead of waiting for the next full
    ingredient re-grade).

    1. Looks up the ResearchPaper by id (404 if it doesn't exist).
    2. If it's already graded, returns it unchanged (no Gemini call) —
       see app/services/paper_grader.py::grade_single_paper's docstring
       for why this is a safe, idempotent no-op rather than an error.
    3. Otherwise runs it through the same rubric-based Gemini evaluation
       as ingestion-time grading (app/services/paper_grader.py::grade_paper)
       and persists the result.
    """
    paper = session.get(ResearchPaper, paper_id)
    if paper is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No research paper with id {paper_id}.",
        )

    try:
        graded_paper = await run_in_threadpool(grade_single_paper, session, paper)
    except PaperGradingError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Grading failed: {exc}",
        ) from exc

    return GradePaperResponse(
        status="success",
        paper=search_service.to_research_paper_response(graded_paper),
    )


@dev_router.delete(
    "/mock-data",
    response_model=MockDataResetResponse,
    summary="[dev] Completely wipe all Product/Ingredient/link rows",
)
async def delete_mock_data(
    session: Session = Depends(get_session),
) -> MockDataResetResponse:
    """Dev-only endpoint: unconditionally wipes the entire database.

    Calls storage.delete_all_data, which deletes every row from every
    supplement table (not just is_mock=True ones — see that function's
    docstring for why the old is_mock-scoped version left real scan data
    behind) and verifies the tables are actually empty afterwards, raising
    if not.

    WARNING: this is a prototype convenience endpoint. It is NOT
    authenticated or gated behind an environment check — anyone who can
    reach the backend can call it. Do not ship this as-is to a
    production-facing deployment; see docs/Architecture.md.
    """
    try:
        await run_in_threadpool(storage.delete_all_data, session)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Database wipe failed.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database wipe failed: {exc}",
        ) from exc
    return MockDataResetResponse(status="success", message="Database completely wiped")
