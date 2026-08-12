"""API route definitions for the supplement label scanner."""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.schemas.supplement import SupplementAnalysis
from app.services import storage
from app.services.vision import VisionServiceError, analyze_supplement_label

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scan"])

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
async def scan_label(file: UploadFile = File(...)) -> SupplementAnalysis:
    """Analyze a supplement label photo and persist the structured result.

    1. Validates the upload is an accepted image MIME type.
    2. Sends the image bytes to Gemini (app/services/vision.py) to extract
       product name, serving size, and ingredient/dosage data.
    3. Appends the parsed result to backend/data/scanned_ingredients.json
       (app/services/storage.py).
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
        await run_in_threadpool(storage.save_scan, analysis)
    except OSError:
        # The analysis itself succeeded — don't fail the request just
        # because the local save didn't work. Log it and move on.
        logger.exception("Failed to persist scan result to local storage.")

    return analysis
