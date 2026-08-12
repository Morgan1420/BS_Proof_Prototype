"""Pydantic models for structured supplement label analysis.

These models double as the JSON schema handed to Gemini for structured
output (see app/services/vision.py) and as the API response model for
POST /api/v1/scan.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class Ingredient(BaseModel):
    """A single row from a Supplement Facts / Nutrition Facts panel."""

    name: str = Field(..., description="Ingredient name as printed on the label.")
    amount: str = Field(
        ...,
        description=(
            "Amount per serving as printed on the label, excluding the "
            "unit. Kept as a string since labels use varied formats "
            "(e.g. '500', '1.5', '250-300')."
        ),
    )
    unit: str = Field(
        ...,
        description="Unit for `amount`, e.g. 'mg', 'g', 'mcg', 'IU', '%'.",
    )
    daily_value: Optional[str] = Field(
        default=None,
        description="Percent Daily Value if printed, e.g. '25%'. Omitted/null if not shown.",
    )


class SupplementAnalysis(BaseModel):
    """Structured result of analyzing a supplement label image."""

    product_name: Optional[str] = Field(
        default=None, description="Product name if visible on the packaging."
    )
    serving_size: Optional[str] = Field(
        default=None,
        description="Serving size exactly as printed, e.g. '1 capsule', '2 scoops (10 g)'.",
    )
    ingredients: List[Ingredient] = Field(
        default_factory=list,
        description="Every ingredient/nutrient row listed in the Supplement Facts panel, in printed order.",
    )
