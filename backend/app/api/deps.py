"""Shared FastAPI dependencies for the API layer."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.services.pipeline import GradingPipeline


def get_pipeline(request: Request) -> GradingPipeline:
    """Resolve the app-wide ``GradingPipeline`` singleton set up in ``app.main``'s lifespan.

    Raises a clean 503 (instead of an ``AttributeError`` deep inside a
    route) if the pipeline couldn't be constructed at startup -- e.g.
    ``GEMINI_API_KEY`` / ``NCBI_ENTREZ_EMAIL`` aren't configured. See
    ``app.main.lifespan``.
    """
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:
        raise HTTPException(
            status_code=503,
            detail="Grading pipeline is not configured. Set GEMINI_API_KEY and NCBI_ENTREZ_EMAIL.",
        )
    return pipeline
