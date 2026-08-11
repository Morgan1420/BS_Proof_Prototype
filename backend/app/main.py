"""FastAPI application entry point.

Wires together CORS middleware, exception handlers, and the API router,
and (via the lifespan context manager) constructs the shared
``VisionParserService`` / ``ScanStorage`` instances backing
``POST /api/scan`` and ``GET /api/ingredients``. See
``docs/Architecture.md`` for the (now single-step) pipeline this API
exposes, ``app/services/vision_parser.py`` for the Gemini call itself,
and ``app/services/storage.py`` for the local JSON-file persistence.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.services.grading_service import GradingError, IngredientGradingService
from app.services.storage import ScanStorage
from app.services.vision_parser import VisionParserService, VisionParsingError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Construct the shared ``VisionParserService`` / ``IngredientGradingService`` / ``ScanStorage`` at startup.

    If ``GEMINI_API_KEY`` isn't configured, the app still starts (rather
    than crashing on boot) but ``app.state.vision_parser`` /
    ``app.state.grading_service`` are left ``None``;
    ``app.api.deps.get_vision_parser`` / ``get_grading_service`` turn
    that into a clean 503 on ``POST /api/scan`` /
    ``POST /api/ingredients/{ingredient_id}/grade`` instead of an
    unhandled error deep in a request. ``ScanStorage`` needs no external
    configuration (just a filesystem path), so it's always constructed
    successfully -- its ``seed_if_missing()`` guarantees
    ``data/scanned_ingredients.json`` exists with realistic mock data on
    first run, and ``backfill_ingredient_ids()`` guarantees every
    ingredient in it (old seed/scan data included) has a stable,
    persisted ``ingredient_id`` before any request can read or grade one
    -- see that method's docstring for why this matters.
    """
    settings = get_settings()
    app.state.scan_storage = ScanStorage()
    await app.state.scan_storage.seed_if_missing()
    await app.state.scan_storage.backfill_ingredient_ids()
    try:
        app.state.vision_parser = VisionParserService(settings=settings)
    except VisionParsingError as exc:
        logger.warning(
            "Vision parser not fully configured at startup (%s: %s); "
            "POST /api/scan will return 503 until GEMINI_API_KEY is set.",
            type(exc).__name__,
            exc,
        )
        app.state.vision_parser = None
    try:
        app.state.grading_service = IngredientGradingService(settings=settings)
    except GradingError as exc:
        logger.warning(
            "Grading service not fully configured at startup (%s: %s); "
            "POST /api/ingredients/{id}/grade will return 503 until GEMINI_API_KEY is set.",
            type(exc).__name__,
            exc,
        )
        app.state.grading_service = None
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
async def configuration_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map a stray ``VisionParsingError`` to a clean 503.

    Only ever expected at startup (see ``lifespan`` above) -- routes
    handle ``VisionParsingError`` from an in-request scan call
    themselves (as a 502, see ``app/api/routes.py``). This handler is a
    defense-in-depth backstop in case one somehow escapes that boundary
    instead.
    """
    logger.error(
        "Service configuration error on %s %s: %s: %s", request.method, request.url.path, type(exc).__name__, exc
    )
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(GradingError)
async def grading_configuration_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map a stray ``GradingError`` to a clean 503.

    Same role as ``configuration_error_handler`` above, for grading's
    equivalent startup-time misconfiguration case. In-request grading
    failures (e.g. the Gemini call itself failing for an already-running
    service) are handled directly in ``app/api/routes.py`` as a 502, not
    here -- this is only a defense-in-depth backstop.
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


app.include_router(api_router, prefix="/api")


@app.get("/health")
def health_check() -> dict:
    return {"status": "ok"}
