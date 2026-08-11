"""Shared FastAPI dependencies for the API layer."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.services.grading_service import IngredientGradingService
from app.services.storage import ScanStorage
from app.services.vision_parser import VisionParserService


def get_vision_parser(request: Request) -> VisionParserService:
    """Resolve the app-wide ``VisionParserService`` singleton set up in ``app.main``'s lifespan.

    Raises a clean 503 (instead of an ``AttributeError`` deep inside a
    route) if the service couldn't be constructed at startup -- e.g.
    ``GEMINI_API_KEY`` isn't configured. See ``app.main.lifespan``.
    """
    vision_parser = getattr(request.app.state, "vision_parser", None)
    if vision_parser is None:
        raise HTTPException(
            status_code=503,
            detail="Vision parsing is not configured. Set GEMINI_API_KEY.",
        )
    return vision_parser


def get_storage(request: Request) -> ScanStorage:
    """Resolve the app-wide ``ScanStorage`` singleton set up in ``app.main``'s lifespan.

    Unlike ``get_vision_parser``, this never needs external configuration
    (just a filesystem path), so it's always available once the app has
    started -- no 503 case here.
    """
    return request.app.state.scan_storage


def get_grading_service(request: Request) -> IngredientGradingService:
    """Resolve the app-wide ``IngredientGradingService`` singleton set up in ``app.main``'s lifespan.

    Same pattern as ``get_vision_parser``: raises a clean 503 (instead of
    an ``AttributeError`` deep inside a route) if the service couldn't be
    constructed at startup -- grading reuses the same ``GEMINI_API_KEY``
    requirement as scanning. See ``app.main.lifespan``.
    """
    grading_service = getattr(request.app.state, "grading_service", None)
    if grading_service is None:
        raise HTTPException(
            status_code=503,
            detail="Ingredient grading is not configured. Set GEMINI_API_KEY.",
        )
    return grading_service
