"""Pydantic models shared across API routes."""

from pydantic import BaseModel, Field


class ScanResponse(BaseModel):
    """Response returned once an uploaded label image has been received.

    This is a connectivity-check response only; it does not yet carry
    parsed ingredient/dosage data. That will be added once vision.py
    (Gemini integration) and storage.py are wired in.
    """

    message: str = Field(..., description="Human-readable status message.")

    class Config:
        json_schema_extra = {
            "example": {"message": "Image received"},
        }
