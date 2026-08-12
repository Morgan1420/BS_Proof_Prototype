"""Search and browse queries over the Product/Ingredient tables."""

from __future__ import annotations

from typing import List, Optional

from sqlmodel import Session, select

from app.models.supplement import Ingredient as IngredientRow
from app.models.supplement import Product, ProductIngredientLink
from app.schemas.search import FilterType, ResultType, SearchResultItem
from app.schemas.supplement import LinkedIngredientResponse, ProductDetailResponse

# Below this length we don't query the DB at all — matches the frontend's
# own "only fetch suggestions once the user has typed more than 3
# characters" behavior, but enforced here too since this is a public
# endpoint that shouldn't have to trust the caller.
MIN_SUGGEST_QUERY_LENGTH = 3


def get_linked_ingredients(
    session: Session, product_id: int
) -> List[LinkedIngredientResponse]:
    """Returns every Ingredient linked to `product_id`, each carrying that
    product's specific amount/unit/daily_value_percentage.

    This is the fix for the "No ingredient data available for this
    product yet" bug: SQLModel's `Product.ingredients` relationship is
    lazy-loaded, and building `SearchResultItem`/`ProductDetailResponse`
    from a bare `Product` instance (without touching the relationship)
    always serialized as an empty list. An explicit join query, executed
    while the session is open, sidesteps that entirely — no reliance on
    lazy-loading inside Pydantic serialization.
    """
    rows = session.exec(
        select(ProductIngredientLink, IngredientRow)
        .where(ProductIngredientLink.product_id == product_id)
        .where(ProductIngredientLink.ingredient_id == IngredientRow.id)
    ).all()

    return [
        LinkedIngredientResponse(
            id=ingredient.id,
            name=ingredient.name,
            amount=link.amount,
            unit=link.unit,
            daily_value_percentage=link.daily_value_percentage,
            recommended_daily_dosage=ingredient.recommended_daily_dosage,
            scientific_data=ingredient.scientific_data,
        )
        for link, ingredient in rows
    ]


def get_product_detail(
    session: Session, product_id: int
) -> Optional[ProductDetailResponse]:
    """Returns a single Product with its full linked-ingredient list, or
    None if no Product with that id exists. Backs GET /api/v1/products/{id}.
    """
    product = session.get(Product, product_id)
    if product is None:
        return None

    return ProductDetailResponse(
        id=product.id,
        name=product.name,
        brand=product.brand,
        serving_size=getattr(product, "serving_size", "Not available"),
        created_at=product.created_at.isoformat() if product.created_at else None,
        ingredients=get_linked_ingredients(session, product.id),
    )


def suggest(session: Session, query: str, limit: int) -> List[str]:
    """Returns up to `limit` distinct product/ingredient names matching
    `query` (case-insensitive substring match), or an empty list if `query`
    is shorter than MIN_SUGGEST_QUERY_LENGTH.

    Product names are tried first, then ingredient names fill any
    remaining slots. Names are deduplicated case-insensitively (e.g. a
    product and an ingredient both named "Creatine Monohydrate" only
    produce one suggestion).
    """
    normalized = (query or "").strip()
    if len(normalized) < MIN_SUGGEST_QUERY_LENGTH:
        return []

    pattern = f"%{normalized}%"

    product_names = session.exec(
        select(Product.name).where(Product.name.ilike(pattern)).limit(limit)
    ).all()

    ingredient_names = session.exec(
        select(IngredientRow.name).where(IngredientRow.name.ilike(pattern)).limit(limit)
    ).all()

    seen: set[str] = set()
    suggestions: List[str] = []
    for name in [*product_names, *ingredient_names]:
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        suggestions.append(name)
        if len(suggestions) >= limit:
            break

    return suggestions


def search(
    session: Session,
    query: Optional[str],
    filter_type: FilterType,
    limit: int,
) -> List[SearchResultItem]:
    """Returns up to `limit` SearchResultItems from Product and/or
    Ingredient rows, depending on `filter_type`.

    If `query` is given, results are restricted to names containing it
    (case-insensitive). If `query` is omitted, this browses all rows of
    the selected type(s) — used by the Library screen's "Products" /
    "Ingredients" explore cards.

    When `filter_type` is `all`, products are fetched first (up to
    `limit`), then ingredients fill any remaining slots — this is a
    simple, deterministic split rather than an even/interleaved one.
    """
    normalized_query = (query or "").strip() or None
    pattern = f"%{normalized_query}%" if normalized_query else None

    results: List[SearchResultItem] = []

    if filter_type in (FilterType.all, FilterType.products):
        stmt = select(Product)
        if pattern:
            stmt = stmt.where(Product.name.ilike(pattern))
        stmt = stmt.order_by(Product.created_at.desc()).limit(limit)

        for product in session.exec(stmt).all():
            results.append(
                SearchResultItem(
                    id=product.id,
                    type=ResultType.product,
                    name=product.name,
                    brand=product.brand,
                    # Explicit join per product — see get_linked_ingredients'
                    # docstring for why this can't just read
                    # product.ingredients here.
                    ingredients=get_linked_ingredients(session, product.id),
                )
            )

    if filter_type in (FilterType.all, FilterType.ingredients) and len(results) < limit:
        remaining = limit - len(results)
        # No join needed anymore: Ingredient is canonical/self-contained
        # now (its general metadata, not any one product's dosage — see
        # the M2M schema's "strict rule").
        stmt = select(IngredientRow)
        if pattern:
            stmt = stmt.where(IngredientRow.name.ilike(pattern))
        stmt = stmt.order_by(IngredientRow.id.desc()).limit(remaining)

        for ingredient in session.exec(stmt).all():
            results.append(
                SearchResultItem(
                    id=ingredient.id,
                    type=ResultType.ingredient,
                    name=ingredient.name,
                    recommended_daily_dosage=ingredient.recommended_daily_dosage,
                    scientific_data=ingredient.scientific_data,
                    product_count=ingredient.product_count,
                )
            )

    return results[:limit]
