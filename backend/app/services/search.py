"""Search and browse queries over the Product/Ingredient tables."""

from __future__ import annotations

from typing import List, Optional

from sqlmodel import Session, select

from app.models.research import (
    PAPER_STATUS_DISCARDED_IRRELEVANT,
    PaperConclusion,
    ResearchPaper,
    VerifiedResource,
    parse_keywords,
)
from app.models.supplement import Ingredient as IngredientRow
from app.models.supplement import Product, ProductIngredientLink
from app.schemas.research import (
    IngredientDetailResponse,
    PaperConclusionResponse,
    ResearchPaperResponse,
    VerifiedResourceResponse,
)
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


def to_research_paper_response(paper: ResearchPaper) -> ResearchPaperResponse:
    """Builds a ResearchPaperResponse from one ResearchPaper ORM row —
    shared by get_ingredient_papers() below and
    app/api/routes.py's single-paper grade endpoint, so both build this
    shape identically rather than duplicating the field mapping.
    """
    return ResearchPaperResponse(
        id=paper.id,
        title=paper.title,
        abstract=paper.abstract,
        authors=paper.authors,
        publication_date=paper.publication_date,
        source_url=paper.source_url,
        source_domain=paper.source_domain,
        ingredient_id=paper.ingredient_id,
        keywords=parse_keywords(paper.keywords),
        grade=paper.grade,
        grade_score=paper.grade_score,
        # paper.rubric_evaluation is already a plain dict (SQLAlchemy's
        # JSON column type deserializes it automatically) whose keys
        # match RubricEvaluationResponse field-for-field — pydantic
        # validates it straight through. None stays None.
        rubric_evaluation=paper.rubric_evaluation,
        status=paper.status,
        # Phase 19 — paper.extracted_conclusions is already a plain
        # list[str] (or None); passes straight through.
        extracted_conclusions=paper.extracted_conclusions,
    )


def get_ingredient_papers(
    session: Session, ingredient_id: int
) -> List[ResearchPaperResponse]:
    """Returns every stored, *relevant* ResearchPaper row for
    `ingredient_id`, most recently added first (see
    ResearchPaper.created_at) — backs the `papers` field on both
    GET /api/v1/ingredients/{id} and POST /api/v1/ingredients/{id}/grade's
    response.

    Excludes PAPER_STATUS_DISCARDED_IRRELEVANT rows (Phase 6 — see
    app/models/research.py) so a paper Gemini determined isn't actually
    about this ingredient never counts toward "Total studies"/"Average
    grade" or appears in the "List of Studies"/recommendations panels.
    """
    stmt = (
        select(ResearchPaper)
        .where(ResearchPaper.ingredient_id == ingredient_id)
        .where(ResearchPaper.status != PAPER_STATUS_DISCARDED_IRRELEVANT)
        .order_by(ResearchPaper.created_at.desc())
    )
    return [to_research_paper_response(paper) for paper in session.exec(stmt).all()]


def get_ingredient_conclusions(
    session: Session, ingredient_id: int
) -> List[PaperConclusionResponse]:
    """Returns every *active* synthesized PaperConclusion for
    `ingredient_id`, highest-confidence first — backs the `conclusions`
    field on GET /api/v1/ingredients/{id} (see
    app/services/conclusion_grader.py for how these are built/updated).
    """
    stmt = (
        select(PaperConclusion)
        .where(PaperConclusion.ingredient_id == ingredient_id)
        .where(PaperConclusion.is_active.is_(True))
        .order_by(PaperConclusion.confidence_score.desc())
    )
    return [
        PaperConclusionResponse(
            id=conclusion.id,
            ingredient_id=conclusion.ingredient_id,
            claim_summary=conclusion.claim_summary,
            detailed_conclusion=conclusion.detailed_conclusion,
            dosage_mentioned=conclusion.dosage_mentioned,
            rubric_evaluation=conclusion.rubric_evaluation,
            confidence_score=conclusion.confidence_score,
            confidence_grade=conclusion.confidence_grade,
            cross_paper_consensus=conclusion.cross_paper_consensus,
            supporting_paper_ids=conclusion.supporting_paper_ids,
            contradicting_paper_ids=conclusion.contradicting_paper_ids,
        )
        for conclusion in session.exec(stmt).all()
    ]


def get_ingredient_resources(
    session: Session, ingredient_id: int
) -> List[VerifiedResourceResponse]:
    """Returns every stored VerifiedResource for `ingredient_id`, most
    recently added first — backs the `verified_resources` field on
    GET /api/v1/ingredients/{id} (Phase 7 — see
    app/services/resource_fetcher.py for how these are found/persisted;
    Phase 8 — app/services/resource_grader.py for how `grade`/`score`/
    `reasoning_summary` are assigned).

    Unlike get_ingredient_papers above, there's no status/relevance
    filter to apply here — every VerifiedResource row already cleared
    the strict domain allow-list at fetch time (see
    resource_fetcher.py::_is_verified_domain), so every stored row is,
    by construction, one worth showing, regardless of whether it's been
    graded yet (`grade`/`score`/`reasoning_summary` may still be `None`
    — see VerifiedResourceResponse's docstring).
    """
    stmt = (
        select(VerifiedResource)
        .where(VerifiedResource.ingredient_id == ingredient_id)
        .order_by(VerifiedResource.created_at.desc())
    )
    return [
        VerifiedResourceResponse(
            id=resource.id,
            ingredient_id=resource.ingredient_id,
            title=resource.title,
            publisher=resource.publisher,
            url=resource.url,
            domain=resource.domain,
            summary=resource.summary,
            grade=resource.grade,
            score=resource.score,
            reasoning_summary=resource.reasoning_summary,
            # Phase 19 — resource.extracted_conclusions is already a plain
            # list[str] (or None); passes straight through.
            extracted_conclusions=resource.extracted_conclusions,
            # Phase 20 — same straight pass-through.
            extraction_failure_reason=resource.extraction_failure_reason,
            # Phase 22 — resource.aligned_conclusions is a plain
            # list[dict] (or None); Pydantic validates each dict into an
            # AlignedConclusionResponse on the way out.
            aligned_conclusions=resource.aligned_conclusions,
        )
        for resource in session.exec(stmt).all()
    ]


def get_ingredient_detail(
    session: Session, ingredient_id: int
) -> Optional[IngredientDetailResponse]:
    """Returns a single canonical Ingredient plus its full stored paper
    list, synthesized conclusion list, and verified-resource list, or
    None if no Ingredient with that id exists. Backs
    GET /api/v1/ingredients/{id}.
    """
    ingredient = session.get(IngredientRow, ingredient_id)
    if ingredient is None:
        return None

    return IngredientDetailResponse(
        id=ingredient.id,
        name=ingredient.name,
        recommended_daily_dosage=ingredient.recommended_daily_dosage,
        scientific_data=ingredient.scientific_data,
        product_count=ingredient.product_count,
        is_graded=ingredient.is_graded,
        grade_badge_text=ingredient.grade_badge_text,
        summary_description=ingredient.summary_description,
        # Phase 23, renamed Phase 24 — ingredient.scientific_conclusions is
        # a plain list[dict] (or None); Pydantic validates each dict into a
        # ScientificConclusionResponse on the way out, same pass-through
        # convention as VerifiedResourceResponse.aligned_conclusions above.
        scientific_conclusions=ingredient.scientific_conclusions or [],
        papers=get_ingredient_papers(session, ingredient.id),
        conclusions=get_ingredient_conclusions(session, ingredient.id),
        verified_resources=get_ingredient_resources(session, ingredient.id),
        # Phase 33 — ingredient.general_info is already a plain dict (or
        # None); Pydantic validates it into a GeneralInfoResponse on the
        # way out, same pass-through convention as scientific_conclusions
        # above.
        general_info=ingredient.general_info,
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
                    # Phase 38 — surface the real, persisted grading status
                    # here too (previously only get_ingredient_detail did),
                    # so a browse/search result reflects the same DB state
                    # as GET /api/v1/ingredients/{id} instead of silently
                    # looking "ungraded" until the card is individually
                    # expanded.
                    is_graded=ingredient.is_graded,
                    grade_badge_text=ingredient.grade_badge_text,
                )
            )

    return results[:limit]
