"""Step 5: Pipeline Integration.

Orchestrates the three services built in Steps 2-4 into the end-to-end
flow from docs/Architecture.md:

    Vision Parser (Phase 1)  ->  PubMed Service (Phase 2 retrieval)
                             ->  Consensus Engine (Phase 2 scoring)

Vision parsing is a single, fast Gemini call, so ``GradingPipeline.start_scan``
runs it synchronously and returns a job immediately. PubMed retrieval +
consensus scoring for *each* ingredient is comparatively slow (multiple
network calls per ingredient, rate-limited), so that work
(``run_grading_job``) is meant to be handed to FastAPI's ``BackgroundTasks``
by the API layer -- see ``app/api/v1/endpoints/scan.py`` -- rather than
awaited in the request/response cycle, per CLAUDE.md's "Asynchronous
Execution" standard.

Persistence: job and ingredient-result state lives in process-local
in-memory stores (``InMemoryJobStore`` / ``InMemoryIngredientResultStore``).
This is NOT persistent (lost on restart) and NOT safe across multiple
worker processes -- it stands in for the "Store SIFG JSON in Database
Cache" step in Architecture.md's Phase 2 diagram until real persistence
(e.g. Postgres via SQLAlchemy, per ``Settings.database_url``) is built.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel

from app.schemas.ingredient_grade import IngredientGradeSchema
from app.schemas.product import MatchStatus, ProductIngredient, ProductMetadata
from app.services.consensus_engine import ConsensusEngine
from app.services.pubmed_service import DEFAULT_RETMAX, PubMedService
from app.services.vision_parser import ImageInput, VisionParserService

logger = logging.getLogger(__name__)


class IngredientStatus(str, Enum):
    """Lifecycle of a single ingredient's Phase 2 grading, both within a job and globally."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ScanJobStatus(str, Enum):
    """Lifecycle of a scan job as a whole.

    Only two states: a job is PROCESSING until every ingredient has
    finished (successfully or not), then COMPLETED. Per-ingredient
    success/failure is tracked at the ingredient level (``IngredientStatus``),
    not rolled up into a job-level failure state -- a job where 2 of 3
    ingredients graded successfully is still a "completed" job with one
    failed ingredient, not a failed job.
    """

    PROCESSING = "processing"
    COMPLETED = "completed"


class IngredientJobEntry(BaseModel):
    """A single ingredient's status within one scan job.

    ``dose_amount`` / ``dose_unit`` are carried straight through from the
    Phase 1 vision extraction (``ProductIngredient``) -- this is what was
    actually printed on the label, not an evidence-based recommendation,
    so it's safe to surface as-is (unlike ``dosage_benchmarks`` on the
    SIFG grade, which this pipeline deliberately never fabricates).
    """

    ingredient_id: str
    raw_name: str
    dose_amount: Optional[float] = None
    dose_unit: Optional[str] = None
    status: IngredientStatus = IngredientStatus.PENDING
    error: Optional[str] = None


class ScanJob(BaseModel):
    """A single label-scan job: the Phase 1 payload plus each ingredient's Phase 2 progress."""

    job_id: str
    status: ScanJobStatus
    product_metadata: ProductMetadata
    match_status: MatchStatus
    ingredients: List[IngredientJobEntry]
    created_at: datetime
    updated_at: datetime


class ScanResponse(BaseModel):
    """Response body for ``POST /api/v1/scan``."""

    job_id: str
    status: ScanJobStatus
    product_metadata: ProductMetadata
    match_status: MatchStatus
    ingredients: List[IngredientJobEntry]

    @classmethod
    def from_job(cls, job: ScanJob) -> "ScanResponse":
        return cls(
            job_id=job.job_id,
            status=job.status,
            product_metadata=job.product_metadata,
            match_status=job.match_status,
            ingredients=job.ingredients,
        )


class IngredientRecord(BaseModel):
    """Latest known state for one ingredient_id, independent of any single job.

    A given ingredient (e.g. "Ashwagandha") may appear in many scans
    across many products; this is the global "have we graded this
    already" lookup the GET /ingredients/{id} endpoint reads from, and
    (per Architecture.md's "Decoupled Knowledge Base" decision) the seam
    where a real cache/database would prevent redundant re-grading.
    """

    ingredient_id: str
    status: IngredientStatus
    grade: Optional[IngredientGradeSchema] = None
    error: Optional[str] = None


class InMemoryJobStore:
    """Process-local job tracker. See module docstring re: persistence caveats."""

    def __init__(self) -> None:
        self._jobs: Dict[str, ScanJob] = {}
        self._lock = asyncio.Lock()

    async def create(self, job: ScanJob) -> None:
        async with self._lock:
            self._jobs[job.job_id] = job

    async def get(self, job_id: str) -> Optional[ScanJob]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def update(self, job: ScanJob) -> None:
        async with self._lock:
            self._jobs[job.job_id] = job


class InMemoryIngredientResultStore:
    """Process-local ingredient status/result index. See module docstring re: persistence caveats."""

    def __init__(self) -> None:
        self._records: Dict[str, IngredientRecord] = {}
        self._lock = asyncio.Lock()

    async def set_status(self, ingredient_id: str, status: IngredientStatus, error: Optional[str] = None) -> None:
        async with self._lock:
            existing = self._records.get(ingredient_id)
            grade = existing.grade if existing else None
            self._records[ingredient_id] = IngredientRecord(
                ingredient_id=ingredient_id, status=status, grade=grade, error=error
            )

    async def set_completed(self, ingredient_id: str, grade: IngredientGradeSchema) -> None:
        async with self._lock:
            self._records[ingredient_id] = IngredientRecord(
                ingredient_id=ingredient_id, status=IngredientStatus.COMPLETED, grade=grade
            )

    async def get(self, ingredient_id: str) -> Optional[IngredientRecord]:
        async with self._lock:
            return self._records.get(ingredient_id)


class GradingPipeline:
    """Ties Vision Parser -> PubMed Service -> Consensus Engine together end-to-end."""

    def __init__(
        self,
        vision_parser: VisionParserService,
        pubmed_service: PubMedService,
        consensus_engine: ConsensusEngine,
        job_store: Optional[InMemoryJobStore] = None,
        ingredient_store: Optional[InMemoryIngredientResultStore] = None,
        papers_per_ingredient: int = DEFAULT_RETMAX,
    ) -> None:
        self._vision_parser = vision_parser
        self._pubmed_service = pubmed_service
        self._consensus_engine = consensus_engine
        self.jobs = job_store or InMemoryJobStore()
        self.ingredient_results = ingredient_store or InMemoryIngredientResultStore()
        self._papers_per_ingredient = papers_per_ingredient

    # -- Phase 1: synchronous ---------------------------------------------------

    async def start_scan(self, image: ImageInput) -> ScanJob:
        """Run vision parsing synchronously and register a new job.

        Does NOT run the Phase 2 grading itself -- call ``run_grading_job``
        with the returned job's id to do that (typically scheduled via
        FastAPI ``BackgroundTasks`` by the caller, so this method can
        return as soon as the (fast) vision parsing step finishes).

        Never raises: ``VisionParserService.parse_label_image`` already
        degrades to a draft payload (possibly with zero ingredients) on
        any failure, so a job is always created.
        """
        payload = await self._vision_parser.parse_label_image(image)

        ingredients = [
            IngredientJobEntry(
                ingredient_id=self._resolve_ingredient_id(ing),
                raw_name=ing.raw_name,
                dose_amount=ing.dose_amount,
                dose_unit=ing.dose_unit,
            )
            for ing in payload.product_ingredients
        ]

        now = datetime.now(timezone.utc)
        job = ScanJob(
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            status=ScanJobStatus.PROCESSING if ingredients else ScanJobStatus.COMPLETED,
            product_metadata=payload.product_metadata,
            match_status=payload.match_status,
            ingredients=ingredients,
            created_at=now,
            updated_at=now,
        )
        await self.jobs.create(job)

        # Pre-register every ingredient as PENDING immediately (synchronously,
        # before this method returns) so a GET /ingredients/{id} issued right
        # after POST /scan sees "pending" rather than a false 404.
        for entry in ingredients:
            await self.ingredient_results.set_status(entry.ingredient_id, IngredientStatus.PENDING)

        return job

    @staticmethod
    def _resolve_ingredient_id(ingredient: ProductIngredient) -> str:
        """Use the DB-matched normalized_id if we have one; otherwise slugify raw_name.

        A Draft Record (see Architecture.md Phase 1) won't have a
        normalized_id yet -- this gives it a stable-enough id to key
        Phase 2 grading and caching on regardless.
        """
        if ingredient.normalized_id:
            return ingredient.normalized_id
        slug = re.sub(r"[^a-z0-9]+", "_", ingredient.raw_name.strip().lower()).strip("_")
        return f"ing_{slug or 'unknown'}"

    # -- Phase 2: background ------------------------------------------------------

    async def run_grading_job(self, job_id: str) -> None:
        """Run PubMed retrieval + consensus scoring for every ingredient in a job.

        Intended to be scheduled via FastAPI ``BackgroundTasks`` (or
        awaited directly in tests). Ingredients are graded sequentially
        rather than concurrently -- PubMedService and the Gemini free
        tier both have per-second rate limits that concurrent ingredient
        processing would blow through faster.
        """
        job = await self.jobs.get(job_id)
        if job is None:
            logger.warning("run_grading_job called for unknown job_id=%s", job_id)
            return

        for entry in job.ingredients:
            await self._grade_one_ingredient(entry)
            await self.jobs.update(job)

        job.status = ScanJobStatus.COMPLETED
        job.updated_at = datetime.now(timezone.utc)
        await self.jobs.update(job)

    async def _grade_one_ingredient(self, entry: IngredientJobEntry) -> None:
        """Grade a single ingredient, mutating `entry` and the shared ingredient store in place.

        Never raises: PubMedService.search_ingredient and
        ConsensusEngine.evaluate_ingredient already degrade to an empty
        list / insufficient-evidence result on failure, so the only
        remaining failure mode is something unexpected -- caught here so
        one bad ingredient can't sink the rest of the job.
        """
        entry.status = IngredientStatus.PROCESSING
        await self.ingredient_results.set_status(entry.ingredient_id, IngredientStatus.PROCESSING)

        try:
            papers = await self._pubmed_service.search_ingredient(
                entry.raw_name, retmax=self._papers_per_ingredient
            )
            evaluation = await self._consensus_engine.evaluate_ingredient(
                entry.ingredient_id, entry.raw_name, papers
            )
            grade = IngredientGradeSchema(
                ingredient_id=entry.ingredient_id,
                canonical_name=entry.raw_name,
                evidence_summary=evaluation.evidence_summary,
                dosage_benchmarks=None,
                validated_claims=evaluation.validated_claims,
                safety_and_side_effects=None,
            )
            await self.ingredient_results.set_completed(entry.ingredient_id, grade)
            entry.status = IngredientStatus.COMPLETED
        except Exception as exc:  # noqa: BLE001 - one ingredient's failure shouldn't sink the job
            error_message = f"{type(exc).__name__}: {exc}"
            logger.warning("Grading failed for ingredient_id=%s: %s", entry.ingredient_id, error_message)
            entry.status = IngredientStatus.FAILED
            entry.error = error_message
            await self.ingredient_results.set_status(entry.ingredient_id, IngredientStatus.FAILED, error=error_message)
