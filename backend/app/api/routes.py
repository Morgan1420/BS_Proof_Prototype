"""``POST /api/scan``, ``GET /api/ingredients``, and ``POST /api/ingredients/{ingredient_id}/grade``.

A scan still performs ONE Gemini vision call
(``app.services.vision_parser.VisionParserService``) and appends the
result straight to ``data/scanned_ingredients.json``
(``app.services.storage.ScanStorage``); there are no background jobs and
no polling for that endpoint. Full always-on multi-ingredient scientific
grading is still gone -- see ``docs/Architecture.md``'s history note.
What's back is on-demand, single-ingredient grading: a user can trigger
``POST /api/ingredients/{ingredient_id}/grade`` for exactly one
ingredient at a time (real PubMed literature search + one Gemini SIFG
evaluation call, see ``app.services.grading_service``), which updates
just that one ingredient's record in place and returns it -- every other
ingredient, in this scan or any other, is untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.deps import get_grading_service, get_storage, get_vision_parser
from app.schemas.scan import ScannedIngredient, ScanResult
from app.services.grading_service import GradingError, IngredientGradingService, TOTAL_GRADING_STEPS, log_grading_step
from app.services.storage import ScanStorage
from app.services.vision_parser import VisionParserService, VisionParsingError

router = APIRouter()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


@router.post("/scan", response_model=ScanResult, status_code=201)
async def scan_product_label(
    file: UploadFile = File(..., description="A photo of the product's label."),
    vision_parser: VisionParserService = Depends(get_vision_parser),
    storage: ScanStorage = Depends(get_storage),
) -> ScanResult:
    """Accept a label image, extract its ingredients in one Gemini call, and save the result.

    Returns the full ``ScanResult`` (product metadata + ingredients)
    immediately -- there's no job id / polling here, since there's
    nothing left running in the background after this call returns. The
    same result is appended to ``data/scanned_ingredients.json``, so it
    also shows up in a subsequent ``GET /api/ingredients``.
    """
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported content type {file.content_type!r}; expected an image.",
        )

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Uploaded file exceeds the 10 MB limit.")

    try:
        result = await vision_parser.scan_label(image_bytes)
    except VisionParsingError as exc:
        raise HTTPException(status_code=502, detail=f"Vision parsing failed: {exc}") from exc

    await storage.append(result)
    return result


@router.get("/ingredients", response_model=List[ScanResult])
async def list_saved_ingredients(
    storage: ScanStorage = Depends(get_storage),
) -> List[Dict[str, Any]]:
    """Return every scan saved so far, oldest first, straight from ``data/scanned_ingredients.json``."""
    return await storage.list_all()


@router.post("/ingredients/{ingredient_id}/grade", response_model=ScannedIngredient)
async def grade_ingredient(
    ingredient_id: str,
    storage: ScanStorage = Depends(get_storage),
    grading_service: IngredientGradingService = Depends(get_grading_service),
) -> Dict[str, Any]:
    """Grade exactly one saved ingredient: PubMed literature search + one Gemini SIFG evaluation call.

    Runs ONLY for ``ingredient_id`` -- no other ingredient, in this scan
    or any other, is read or modified. Persists the result (or a
    ``grade_status="failed"`` marker on failure) to the specific
    ingredient's record in ``data/scanned_ingredients.json`` via
    ``ScanStorage.update_ingredient``, and returns the updated ingredient
    so the UI can replace its "Grade" button with the new grade in place.
    """
    existing = await storage.get_ingredient(ingredient_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"No ingredient found with ingredient_id={ingredient_id!r}.")

    ingredient = ScannedIngredient.model_validate(existing)

    try:
        grading_result = await grading_service.grade_ingredient(ingredient)
    except GradingError as exc:
        # Persist the failure (not just raise it) so the UI can show a clear
        # "grading failed" state -- with a graded_at timestamp, a retry path
        # via grade_status="failed", and whatever grading_stats could still
        # be computed (e.g. a literature search that succeeded before the
        # Gemini call itself failed) -- rather than the row silently
        # reverting to looking untouched.
        failure_updates: Dict[str, Any] = {
            "grade_status": "failed",
            "graded_at": datetime.now(timezone.utc).isoformat(),
        }
        if exc.stats is not None:
            failure_updates["grading_stats"] = exc.stats.model_dump(mode="json")
        await storage.update_ingredient(ingredient_id, failure_updates)
        log_grading_step(TOTAL_GRADING_STEPS, ingredient_id, "Grading FAILED, not saved as a grade", body=str(exc))
        raise HTTPException(status_code=502, detail=f"Grading failed: {exc}") from exc

    consensus = grading_result.consensus
    stats = grading_result.stats
    updates = {
        "grade_status": "graded",
        "sifg_grade": consensus.sifg_grade,
        "sifg_score": consensus.sifg_score,
        "efficacy_safety_evaluation": consensus.efficacy_safety_evaluation,
        "dosage_appropriateness": consensus.dosage_appropriateness,
        "evidence_summary": consensus.evidence_summary,
        "raw_consensus": consensus.model_dump(mode="json"),
        "grading_stats": stats.model_dump(mode="json"),
        "graded_at": datetime.now(timezone.utc).isoformat(),
    }
    updated = await storage.update_ingredient(ingredient_id, updates)
    if updated is None:
        # Vanishingly unlikely (the ingredient existed moments ago, above) but
        # not impossible if data/scanned_ingredients.json was hand-edited
        # mid-request -- surface it as a 404 rather than a confusing 500.
        raise HTTPException(
            status_code=404, detail=f"Ingredient {ingredient_id!r} was removed while grading was in progress."
        )

    log_grading_step(
        TOTAL_GRADING_STEPS,
        ingredient_id,
        "Saved",
        body=(
            f"grade_status=graded sifg_grade={consensus.sifg_grade!r} sifg_score={consensus.sifg_score!r} "
            f"duration={stats.grading_duration_seconds}s -- persisted to data/scanned_ingredients.json"
        ),
    )
    return updated
