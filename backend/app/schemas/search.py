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

    **Phase 38 fix.** `is_graded`/`grade_badge_text` were missing from
    this schema entirely — GET /api/v1/supplements/search (which backs
    both the Library screen's "Ingredients" explore card and every
    standalone IngredientCard rendered from ResultsScreen) had no way to
    report an ingredient's real, already-persisted grading status, so the
    frontend had nothing to read for it and defaulted every result to
    "ungraded" (see frontend/src/screens/ResultsScreen.tsx::toIngredient's
    old hardcoded `is_graded: false`). This was misdiagnosed as a
    backend/DB persistence bug — `Ingredient.is_graded` was in fact being
    correctly committed and never reset (see
    app/services/grading.py::grade_ingredient,
    app/db.py's non-destructive init_db()) — but the search/browse
    endpoint simply never surfaced it, so every reload/browse looked like
    a fresh revert to ungraded even though GET /api/v1/ingredients/{id}
    (used by the ingredient-detail fetch) already returned the correct
    value the whole time.
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
    # Phase 38 — real, persisted grading status (app/models/supplement.py
    # Ingredient.is_graded/grade_badge_text), same fields
    # IngredientDetailResponse already exposes. `None`/absent for
    # `type == "product"` results, same convention as
    # recommended_daily_dosage/scientific_data/product_count above.
    is_graded: Optional[bool] = None
    grade_badge_text: Optional[str] = None


class SearchResponse(BaseModel):
    """Response body for GET /api/v1/supplements/search."""

    query: Optional[str] = None
    filter_type: FilterType = FilterType.all
    count: int
    results: List[SearchResultItem] = Field(default_factory=list)
