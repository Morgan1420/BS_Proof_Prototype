"""``POST /api/v1/scan`` and ``GET /api/v1/ingredients/{ingredient_id}``.

See ``docs/Architecture.md`` Phase 1 (Vision-LLM Parsing) and Phase 2
(PubMed retrieval -> LLM Paper Evaluator -> Consensus Engine) for the
pipeline these routes expose, and ``app/services/pipeline.py`` for the
orchestration logic. Vision parsing runs synchronously within the POST
request (a single, fast Gemini call); PubMed retrieval and consensus
scoring for every extracted ingredient runs as a FastAPI background
task, so the endpoint returns immediately with a job id and the raw
ingredient list -- callers poll GET /ingredients/{id} for results.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.api.deps import get_pipeline
from app.services.pipeline import GradingPipeline, IngredientStatus, ScanJobStatus, ScanResponse

router = APIRouter()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


@router.post("/scan", response_model=ScanResponse, status_code=202)
async def scan_product_label(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="A photo of the product's label."),
    pipeline: GradingPipeline = Depends(get_pipeline),
) -> ScanResponse:
    """Accept a label image, extract ingredients immediately, grade literature in the background.

    Returns HTTP 202 with a ``job_id`` and the raw ingredient list as
    soon as vision parsing finishes. Poll
    ``GET /api/v1/ingredients/{ingredient_id}`` for each ingredient's
    SIFG grade once background grading completes.
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

    job = await pipeline.start_scan(image_bytes)
    if job.status == ScanJobStatus.PROCESSING:
        background_tasks.add_task(pipeline.run_grading_job, job.job_id)

    return ScanResponse.from_job(job)


@router.get("/ingredients/{ingredient_id}")
async def get_ingredient_grade(
    ingredient_id: str,
    pipeline: GradingPipeline = Depends(get_pipeline),
):
    """Return an ingredient's SIFG grade once its background grading has completed.

    - 200 + the full ``IngredientGradeSchema`` once grading is complete.
    - 202 + ``{"status": "pending" | "processing"}`` while grading is still running.
    - 200 + ``{"status": "failed", "error": ...}`` if grading errored for this ingredient.
    - 404 if this ingredient_id has never been part of any scan.
    """
    record = await pipeline.ingredient_results.get(ingredient_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No such ingredient: {ingredient_id!r}.")

    if record.status == IngredientStatus.COMPLETED and record.grade is not None:
        return record.grade

    if record.status == IngredientStatus.FAILED:
        return JSONResponse(
            status_code=200,
            content={"ingredient_id": ingredient_id, "status": record.status.value, "error": record.error},
        )

    return JSONResponse(
        status_code=202,
        content={"ingredient_id": ingredient_id, "status": record.status.value},
    )
