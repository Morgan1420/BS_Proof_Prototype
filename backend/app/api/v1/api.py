"""Aggregates all v1 API endpoint routers under a single APIRouter.

``app.main`` mounts this at the ``/api/v1`` prefix.
"""

from fastapi import APIRouter

from app.api.v1.endpoints import scan

api_router = APIRouter()
api_router.include_router(scan.router, tags=["scan"])
