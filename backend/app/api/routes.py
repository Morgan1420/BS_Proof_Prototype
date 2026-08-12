"""API route definitions for the supplement label scanner."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from app.db import get_session
from app.schemas.dev import MockDataResetResponse
from app.schemas.search import FilterType, SearchResponse, SuggestResponse
from app.schemas.supplement import ProductDetailResponse, SupplementAnalysis
from app.services import search as search_service
from app.services import storage
from app.services.vision import VisionServiceError, analyze_supplement_label

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scan"])
search_router = APIRouter(prefix="/supplements", tags=["supplements"])
products_router = APIRouter(prefix="/products", tags=["products"])
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
