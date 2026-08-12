"""Persistence for parsed supplement label scans (SQLite, via SQLModel).

As of the Many-to-Many refactor, a single canonical Ingredient row is
shared across every Product that contains it (deduplicated by
case-insensitive name match). Product-specific dosage data
(amount/unit/daily_value) now lives on ProductIngredientLink, not on
Ingredient itself — see the "strict rule" in app/models/supplement.py.
"""

from __future__ import annotations

from typing import Dict

from sqlalchemy import func
from sqlmodel import Session, delete, select

from app.models.supplement import Ingredient as IngredientRow
from app.models.supplement import Product, ProductIngredientLink
from app.schemas.supplement import SupplementAnalysis

# Gemini's SupplementAnalysis doesn't produce a product_name in every
# case (label not legible) and never produces a brand at all — Product.name
# and Product.brand are both non-nullable `str` in the new schema, so we
# need fallbacks.
DEFAULT_PRODUCT_NAME = "Unnamed product"
DEFAULT_BRAND = "Unknown"


def _clean_ingredient_name(raw_name: str) -> str:
    """Defensive normalization of an ingredient name before storage/lookup.

    The Gemini prompt (see vision.py's SYSTEM_PROMPT) is responsible for
    actually producing a clean, canonical English name with percentages/
    ratios/translations stripped out — this just collapses stray
    whitespace as a safety net, it is not a substitute for that.
    """
    return " ".join(raw_name.split())


def _escape_like_pattern(value: str) -> str:
    """Escapes SQL LIKE/ILIKE wildcard characters so an *exact*-match
    lookup via `.ilike()` can't accidentally turn into a wildcard search.

    This matters specifically because ingredient names extracted from
    messy labels can legitimately contain a literal '%' if the cleaning
    step upstream ever falls short (e.g. "Magnesium 11%..." slipping
    through) — without escaping, `.ilike("Magnesium 11%")` would match
    "Magnesium 11" followed by ANYTHING, silently matching or failing to
    match the wrong rows instead of doing a real equality check.
    """
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _find_or_create_ingredient(session: Session, raw_name: str) -> IngredientRow:
    """Looks up an existing canonical Ingredient by exact, case-insensitive
    name match, incrementing its `product_count`; creates a new one
    (`is_mock=False`, `product_count=1`) if none matches.

    Ingredient.name is unique at the DB level; this find-first avoids a
    UNIQUE constraint violation when the same compound is scanned with
    different capitalization across products (e.g. "vitamin d3" vs.
    "Vitamin D3") — cleaning/escaping the name (see helpers above) makes
    that match reliable even if a name isn't perfectly clean.
    """
    clean_name = _clean_ingredient_name(raw_name)
    lookup_pattern = _escape_like_pattern(clean_name)

    existing = session.exec(
        select(IngredientRow).where(
            IngredientRow.name.ilike(lookup_pattern, escape="\\")
        )
    ).first()

    if existing is not None:
        existing.product_count += 1
        session.add(existing)
        return existing

    ingredient = IngredientRow(
        name=clean_name,
        is_mock=False,
        product_count=1,
    )
    session.add(ingredient)
    session.flush()  # assigns ingredient.id without committing yet
    return ingredient


def save_scan(session: Session, analysis: SupplementAnalysis) -> Product:
    """Persists a parsed SupplementAnalysis as a Product row, linked to
    (find-or-created) canonical Ingredient rows via ProductIngredientLink.

    Steps:
      1. Create the Product row and `flush()` (not `commit()`) so
         `product.id` is assigned. We deliberately don't commit here:
         doing so would leave a half-saved Product with no ingredient
         links permanently in the DB if anything below fails. `flush()`
         gets the auto-assigned id within the still-open transaction,
         and everything commits together at the end — or rolls back
         together on error (see the try/except below).
      2. For each parsed ingredient, find-or-create its canonical
         Ingredient row (`_find_or_create_ingredient`, which also
         increments `product_count` on a match).
      3. Create a ProductIngredientLink row explicitly tying that
         Product + Ingredient together with this scan's amount/unit/
         daily_value.
      4. Commit everything as one transaction.

    Known gaps (see docs/Architecture.md for the full list):
      - `analysis.serving_size` has no column to live in on `Product` and
        is silently dropped here.
      - Gemini's extraction schema doesn't produce a brand, so
        `Product.brand` is always saved as "Unknown".
      - `Product.is_mock` is left at its model default (`True`) even for
        real scans — intentional, not an oversight: it keeps "Reset DB"
        useful for clearing scan/product history during development,
        while the canonical Ingredient dictionary (now `is_mock=False`
        for real scans) survives resets and keeps accumulating. Flag if
        this isn't the desired behavior.

    Args:
        session: An open SQLModel session (see app/db.py::get_session).
        analysis: The validated result from
            app.services.vision.analyze_supplement_label.

    Returns:
        The persisted Product, with its `ingredients` (link) relationship
        loaded.

    Raises:
        Exception: re-raises any database error after rolling back the
            session, so the session isn't left in a broken state.
    """
    product = Product(
        name=analysis.product_name or DEFAULT_PRODUCT_NAME,
        brand=DEFAULT_BRAND,
    )

    try:
        # 1. Get/create Product.
        session.add(product)
        session.flush()  # assigns product.id, needed for the links below

        for item in analysis.ingredients:
            # 2. Find-or-create the canonical Ingredient.
            ingredient = _find_or_create_ingredient(session, item.name)

            # 3. Explicitly create the link row carrying this scan's dosage.
            link = ProductIngredientLink(
                product_id=product.id,
                ingredient_id=ingredient.id,
                amount=item.amount,
                unit=item.unit,
                daily_value_percentage=item.daily_value,
            )
            session.add(link)

        # 4. Commit the Product, every (new-or-reused) Ingredient, and
        # every ProductIngredientLink together as one transaction.
        session.commit()
        session.refresh(product)
    except Exception:
        session.rollback()
        raise

    return product


def delete_mock_data(session: Session) -> Dict[str, int]:
    """Deletes every Product/Ingredient/ProductIngredientLink row flagged
    is_mock=True. Used by DELETE /api/v1/dev/mock-data.

    Mock Products are deleted via the ORM (`session.delete`, not a bulk
    `delete()` statement) so the `cascade="all, delete-orphan"` on
    Product.ingredients actually fires and removes their
    ProductIngredientLink rows too. Mock Ingredients are only deleted if
    no link row still references them afterwards — an ingredient flagged
    is_mock=True that's also linked to a *non-mock* product (once that
    becomes possible) is left alone rather than deleted out from under a
    real product.

    Returns:
        A dict of how many rows were deleted per table:
        {"products": N, "links": N, "ingredients": N}.
    """
    mock_products = session.exec(
        select(Product).where(Product.is_mock)
    ).all()
    deleted_links = sum(len(product.ingredients) for product in mock_products)
    deleted_products = len(mock_products)

    for product in mock_products:
        session.delete(product)

    session.flush()  # apply the cascade-deletes above before the query below

    mock_ingredients = session.exec(
        select(IngredientRow).where(IngredientRow.is_mock)
    ).all()
    deleted_ingredients = 0
    for ingredient in mock_ingredients:
        session.refresh(ingredient)
        if not ingredient.product_links:
            session.delete(ingredient)
            deleted_ingredients += 1

    session.commit()

    return {
        "products": deleted_products,
        "links": deleted_links,
        "ingredients": deleted_ingredients,
    }


def delete_all_data(session: Session) -> Dict[str, int]:
    """Unconditionally wipes every row from every supplement table.

    This is the function DELETE /api/v1/dev/mock-data actually calls now.
    `delete_mock_data` (above) is scoped to is_mock=True rows only — but
    `_find_or_create_ingredient` sets `is_mock=False` on every real
    Ingredient created from an actual scan, so that path was never able
    to clear those rows out. This function does a real full reset instead:
    every Product, Ingredient, and ProductIngredientLink, regardless of
    the is_mock flag.

    Uses bulk `delete()` statements rather than per-object ORM deletes, in
    explicit dependency order (links, then products, then ingredients) —
    child rows before parents — because app/db.py now enables
    `PRAGMA foreign_keys=ON` for every SQLite connection, so deleting a
    Product/Ingredient while a ProductIngredientLink still references it
    would raise an IntegrityError rather than silently leaving an orphan.

    After committing, re-queries each table's row count and raises if any
    of them is nonzero, so a partially-applied wipe (e.g. a driver-level
    issue swallowing part of the statement) surfaces as a loud error
    instead of a silent "Database wiped successfully" lie to the caller.

    Returns:
        A dict of how many rows were deleted per table:
        {"products": N, "links": N, "ingredients": N}.
    """
    deleted_links = session.exec(select(func.count()).select_from(ProductIngredientLink)).one()
    deleted_products = session.exec(select(func.count()).select_from(Product)).one()
    deleted_ingredients = session.exec(select(func.count()).select_from(IngredientRow)).one()

    try:
        # Child rows first, then parents — required now that FK
        # enforcement is on (see app/db.py).
        session.exec(delete(ProductIngredientLink))
        session.exec(delete(Product))
        session.exec(delete(IngredientRow))
        session.commit()
    except Exception:
        session.rollback()
        raise

    remaining_links = session.exec(select(func.count()).select_from(ProductIngredientLink)).one()
    remaining_products = session.exec(select(func.count()).select_from(Product)).one()
    remaining_ingredients = session.exec(select(func.count()).select_from(IngredientRow)).one()

    if remaining_links or remaining_products or remaining_ingredients:
        raise RuntimeError(
            "Database wipe did not take effect: "
            f"{remaining_links} link(s), {remaining_products} product(s), "
            f"{remaining_ingredients} ingredient(s) still remain."
        )

    return {
        "products": deleted_products,
        "links": deleted_links,
        "ingredients": deleted_ingredients,
    }
