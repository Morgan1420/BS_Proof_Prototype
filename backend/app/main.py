"""FastAPI application entry point.

Wires together CORS middleware, exception handlers, and the v1 API
router, and (via the lifespan context manager) constructs a single
shared ``GradingPipeline`` instance backing the ``/api/v1/scan`` and
``/api/v1/ingredients`` endpoints. See ``docs/Architecture.md`` for the
end-to-end pipeline this API exposes, and ``app/services/pipeline.py``
for the orchestration logic itself.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.api import api_router
from app.core.config import get_settings
from app.services.consensus_engine import ConsensusEngine, ConsensusEngineError
from app.services.pipeline import GradingPipeline
from app.services.pubmed_service import PubMedService, PubMedServiceError
from app.services.vision_parser import VisionParserService, VisionParsingError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Construct the shared ``GradingPipeline`` at startup.

    If ``GEMINI_API_KEY`` / ``NCBI_ENTREZ_EMAIL`` aren't configured, the
    app still starts (rather than crashing on boot) but
    ``app.state.pipeline`` is left ``None``; ``app.api.deps.get_pipeline``
    turns that into a clean 503 on the affected routes instead of an
    unhandled error deep in a request.
    """
    settings = get_settings()
    try:
        app.state.pipeline = GradingPipeline(
            vision_parser=VisionParserService(settings=settings),
            pubmed_service=PubMedService(settings=settings),
            consensus_engine=ConsensusEngine(settings=settings),
        )
    except (VisionParsingError, PubMedServiceError, ConsensusEngineError) as exc:
        logger.warning(
            "Grading pipeline not fully configured at startup (%s: %s); "
            "/api/v1/scan and /api/v1/ingredients will return 503 until "
            "GEMINI_API_KEY and NCBI_ENTREZ_EMAIL are set.",
            type(exc).__name__,
            exc,
        )
        app.state.pipeline = None
    yield


settings = get_settings()

app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Wildcard origins ("*", the default) and allow_credentials=True is an invalid
    # combination per the CORS spec -- browsers reject it outright. Only enable
    # credentials once cors_origins is locked down to specific origins.
    allow_credentials=settings.cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(VisionParsingError)
@app.exception_handler(PubMedServiceError)
@app.exception_handler(ConsensusEngineError)
async def configuration_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map our services' configuration errors to a clean 503.

    These are only ever expected at startup (see ``lifespan`` above);
    this handler is a defense-in-depth backstop in case one somehow
    escapes a service boundary during a request instead.
    """
    logger.error(
        "Service configuration error on %s %s: %s: %s", request.method, request.url.path, type(exc).__name__, exc
    )
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all so unexpected errors return clean JSON instead of leaking a stack trace."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "An unexpected error occurred."})


app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
def health_check() -> dict:
    return {"status": "ok"}
