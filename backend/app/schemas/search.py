"""Pydantic response models for the supplement search/browse endpoints."""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.supplement import LinkedIngredientResponse


class FilterType(str, Enum):
    """Which table(s) a search/browse request should cover."""

    all = "all"
    products = "products"
    ingredients = "ingredients"


class ResultType(str, Enum):
    """What kind of row a single search result represents."""

    product = "product"
    ingredient = "ingredient"


class SuggestResponse(BaseModel):
    """Response body for GET /api/v1/supplements/suggest."""

    query: str
    suggestions: List[str] = Field(default_factory=list)


class SearchResultItem(BaseModel):
    """A single row in a search/browse result — either a Product or an
    Ingredient, distinguished by `type`. Fields that don't apply to a given
    `type` are left null (e.g. `brand` on an ingredient result).

    Note: as of the Many-to-Many schema refactor (see
    app/models/supplement.py), an ingredient result no longer carries a
    product-specific dosage (amount/unit/daily_value) or a single parent
    product name — Ingredient is now canonical/shared data that can
    belong to zero, one, or many products via ProductIngredientLink.
    Ingredient results instead surface that canonical metadata
    (`recommended_daily_dosage`, `scientific_data`, `product_count`).

    `ingredients` is populated (via an explicit join in
    app/services/search.py::search) only for `type == "product"` results
    — it's each linked Ingredient plus that product's specific
    amount/unit/daily_value_percentage from ProductIngredientLink. Left
    as an empty list for `type == "ingredient"` results, since a
    standalone ingredient result isn't tied to one product's dosage.
    """

    id: int
    type: ResultType
    name: str

    # Product-specific
    brand: Optional[str] = None
    ingredients: List[LinkedIngredientResponse] = Field(default_factory=list)

    # Ingredient-specific (canonical metadata, not any one product's
    # dosage — see the "strict rule" in app/models/supplement.py)
    recommended_daily_dosage: Optional[str] = None
    scientific_data: Optional[str] = None
    product_count: Optional[int] = Field(
        default=None,
        description="How many products currently link to this ingredient.",
    )


class SearchResponse(BaseModel):
    """Response body for GET /api/v1/supplements/search."""

    query: Optional[str] = None
    filter_type: FilterType = FilterType.all
    count: int
    results: List[SearchResultItem] = Field(default_factory=list)
