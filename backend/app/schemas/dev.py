"""Pydantic response models for internal/dev-only endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class MockDataResetResponse(BaseModel):
    """Response body for DELETE /api/v1/dev/mock-data."""

    status: str
    message: str
