"""SQLModel ORM tables for scanned products and their canonical
ingredients, related through a Many-to-Many junction table.

Schema (as of the M2M refactor):
  - Product: one row per scanned product (name, brand, is_mock, created_at).
  - Ingredient: one row PER CANONICAL COMPOUND, deduplicated by name (e.g.
    "Creatine Monohydrate" appears once here even if it shows up in five
    different scanned products). Holds only general/canonical metadata
    (recommended_daily_dosage, scientific_data placeholders) — see the
    strict rule below.
  - ProductIngredientLink: the junction table. Holds the PRODUCT-SPECIFIC
    dosage (amount, unit, daily_value_percentage) for one
    product/ingredient pairing.

STRICT RULE: do not add product-specific dosage/percentage/serving-size
columns to Ingredient. That data belongs on ProductIngredientLink only —
Ingredient must stay canonical/shared data.

Distinct from the Pydantic I/O models in app/schemas/supplement.py, which
shape Gemini's structured output and also happen to define an
`Ingredient` class (for the per-scan shape Gemini returns — not this
canonical DB row). Import with an alias where both are in scope (see
app/services/storage.py) to keep them straight.
"""


# Deliberately NOT using `from __future__ import annotations` here: it
# turns every annotation (including the Relationship() ones below) into a
# plain string, which trips a strict check in newer SQLAlchemy versions —
# "expression ... seems to be using a generic class as the argument to
# relationship()" — since it can no longer distinguish a real `List[...]`
# generic from an unparsed string. Forward references stay as quoted
# strings ("Ingredient") instead, which SQLAlchemy resolves normally.

from datetime import datetime, timezone
from typing import List, Optional

from sqlmodel import Field, Relationship, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Product(SQLModel, table=True):
    """A single scanned supplement product."""

    __tablename__ = "products"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    brand: str
    is_mock: bool = Field(
        default=True,
        description=(
            "Flags test/scanned data so it can be bulk-deleted via "
            "DELETE /api/v1/dev/mock-data."
        ),
    )
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)

    # NOTE: despite the field name, this returns ProductIngredientLink
    # rows (each carrying THIS product's specific amount/unit/%DV for one
    # ingredient) — not bare Ingredient rows. Use `link.ingredient` to
    # reach the canonical Ingredient from each item. Named `ingredients`
    # to match the task's field-naming spec for this model.
    ingredients: List["ProductIngredientLink"] = Relationship(
        back_populates="product",
        # A product's dosage links only make sense attached to that
        # product: deleting a Product should delete its link rows too.
        # The linked *Ingredient* rows are NOT cascade-deleted here —
        # they're canonical/shared and may still be referenced by other
        # products (see app/services/storage.py::delete_mock_data for how
        # mock ingredients are cleaned up without orphaning real ones).
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Ingredient(SQLModel, table=True):
    """A single canonical ingredient/compound, deduplicated by name and
    shared across every Product that contains it. Product-specific
    dosage lives on ProductIngredientLink, NOT here — see the module
    docstring's strict rule.
    """

    __tablename__ = "ingredients"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)

    # General metadata placeholders — not derived from any scan; intended
    # to be populated separately later (e.g. from a reference database).
    # Deliberately NOT auto-maintained by storage.py right now (see
    # docs/Architecture.md for the reasoning).
    recommended_daily_dosage: str = Field(default="x")
    product_count: int = Field(default=0)
    scientific_data: str = Field(default="n/a")

    is_mock: bool = Field(
        default=True,
        description=(
            "Flags test/scanned data so it can be bulk-deleted via "
            "DELETE /api/v1/dev/mock-data."
        ),
    )

    # --- Phase 2: research paper search / debug grading ---
    # See app/services/grading.py for what sets these. There's no real
    # grading algorithm yet — `grade_badge_text` is currently just the
    # stored paper count formatted three times (e.g. "14 / 14 / 14"),
    # purely so the frontend badge has something non-placeholder to show
    # while the actual grading pipeline is built out.
    is_graded: bool = Field(default=False)
    grade_badge_text: Optional[str] = Field(default=None)

    # --- Multi-source ingredient summary synthesis
    # (app/services/conclusion_grader.py::synthesize_ingredient_summary) ---
    # A single Gemini-synthesized 1-2 sentence overview combining BOTH
    # graded ResearchPaper findings and VerifiedResource official
    # guidance (NIH/USDA/EFSA/Health Canada/...) for this ingredient —
    # e.g. "Analyzed 12 studies and 4 official resources. Average score:
    # B (78/100). Primary consensus confirms efficacy for X with strong
    # support from NIH/EFSA guidelines." Rendered directly beneath the
    # "Scientific Information" section title on the frontend (see
    # src/components/IngredientCard.tsx). None until
    # app/services/paper_analysis_pipeline.py::analyze_ingredient_papers
    # successfully generates one (best-effort — a Gemini failure here is
    # logged and skipped, same "leave it null, don't fail the whole
    # grade request" convention as ResearchPaper.grade/
    # VerifiedResource.grade), and also None if the ingredient had zero
    # graded papers AND zero verified resources to synthesize from at
    # generation time (see that function's docstring) — the frontend
    # falls back to a client-computed heuristic sentence in that case
    # rather than showing nothing (see IngredientCard.tsx's
    # `scientificSummary`). Nullable/added after `ingredients` already
    # existed in deployed databases — same additive-migration story as
    # `is_graded`/`grade_badge_text` above; see
    # app/db.py::_migrate_ingredient_grading_columns.
    summary_description: Optional[str] = Field(default=None)

    product_links: List["ProductIngredientLink"] = Relationship(
        back_populates="ingredient",
    )
    # Forward-referenced as a string ("ResearchPaper") rather than
    # imported directly — app/models/research.py imports *this* module
    # (for its own `ingredient: Optional[Ingredient]` relationship), so a
    # real import here would be circular. SQLAlchemy resolves the string
    # against the shared SQLModel registry once ResearchPaper has been
    # imported anywhere (see app/db.py, which imports both modules at
    # startup specifically for this).
    papers: List["ResearchPaper"] = Relationship(back_populates="ingredient")
    # Same forward-reference/circular-import reasoning as `papers` above,
    # now mirrored for VerifiedResource (see app/models/research.py) —
    # added so `Ingredient <-> VerifiedResource` has the same explicit,
    # queryable ORM relationship `Ingredient <-> ResearchPaper` already
    # has, rather than only being reachable via a manual `select(...)
    # .where(VerifiedResource.ingredient_id == ...)` query. Every current
    # read path (app/services/conclusion_grader.py::
    # synthesize_ingredient_summary, app/services/search.py::
    # get_ingredient_resources) already queries VerifiedResource directly
    # rather than through this relationship — see synthesize_ingredient_summary's
    # own docstring for why that's deliberate (a direct query in the same
    # session always sees rows already `flush()`ed earlier in the same
    # request, with no risk of a stale/cached relationship collection) —
    # so this relationship is additive/for-parity rather than something
    # existing code needs to start using. `lazy="selectin"` is intentionally
    # NOT set here: this app's established convention (see `papers` above,
    # and app/services/search.py's own docstring) is explicit per-request
    # queries over ORM lazy-loading for anything an API response needs to
    # serialize, to avoid N+1s and DetachedInstanceError surprises outside
    # an active session.
    verified_resources: List["VerifiedResource"] = Relationship(back_populates="ingredient")


class ProductIngredientLink(SQLModel, table=True):
    """Junction table: one row per (product, ingredient) pairing, holding
    that pairing's product-specific dosage.
    """

    __tablename__ = "product_ingredient_links"

    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="products.id")
    ingredient_id: int = Field(foreign_key="ingredients.id")

    # Kept as `str` rather than `float`: label amounts from Gemini
    # extraction are already strings, to accommodate ranges/decimals as
    # printed on the label (e.g. "250-300", "1.5") — see
    # app/schemas/supplement.py::Ingredient.amount. Coercing to float
    # here would fail on those non-numeric-but-valid label values.
    amount: str
    unit: str
    daily_value_percentage: Optional[str] = Field(default=None)

    product: Optional[Product] = Relationship(back_populates="ingredients")
    ingredient: Optional[Ingredient] = Relationship(back_populates="product_links")
