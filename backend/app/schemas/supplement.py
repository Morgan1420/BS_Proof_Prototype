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


class LinkedIngredientResponse(BaseModel):
    """A canonical Ingredient joined with one Product's specific dosage
    for it (i.e. one ProductIngredientLink row + its Ingredient).

    `amount`/`unit`/`daily_value_percentage` come from the junction row;
    `recommended_daily_dosage`/`scientific_data` come from the canonical
    Ingredient row (see the "strict rule" in app/models/supplement.py —
    dosage never lives on Ingredient itself). Used to build the nested
    `ingredients` list on SearchResultItem (product results) and on
    ProductDetailResponse.
    """

    id: int
    name: str
    amount: Optional[str] = None
    unit: Optional[str] = None
    daily_value_percentage: Optional[str] = None
    # General metadata placeholders — unmanaged, see docs/Architecture.md.
    recommended_daily_dosage: Optional[str] = "x"
    scientific_data: Optional[str] = "n/a"


class ProductDetailResponse(BaseModel):
    """A single Product with its full linked-ingredient list attached.

    Response model for GET /api/v1/products/{id}.
    """

    id: int
    name: str
    brand: Optional[str] = "Unknown"
    # Product has no serving_size column today (see docs/Architecture.md's
    # "Known gaps" — Gemini's serving_size is currently dropped on save),
    # so this is always the fallback until that column is added.
    serving_size: Optional[str] = "Not available"
    created_at: Optional[str] = None
    ingredients: List[LinkedIngredientResponse] = Field(default_factory=list)
