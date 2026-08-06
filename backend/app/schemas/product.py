"""Pydantic schemas for Phase 1: Identification & Payload Structuring.

Mirrors the "Structured Product Payload" JSON schema defined in
``docs/Architecture.md`` (Phase 1). These models represent a product as
extracted by the Vision-LLM OCR step and normalized against the Primary
Identifier Lookup (UPC barcode / composite name hash), including the
non-blocking "Draft Record" fallback path used when no confident DB match
is found.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MatchStatus(str, Enum):
    """Outcome of the Primary Identifier Lookup step in Phase 1.

    ``MATCHED`` corresponds to the "Similarity >= 80%" / "Product Found"
    branch; ``DRAFT`` corresponds to the "Not Found / Version Mismatch"
    branch that yields a non-blocking Draft Record built from raw OCR data.
    """

    MATCHED = "matched"
    DRAFT = "draft"


class ProductMetadata(BaseModel):
    """Top-level identifying metadata for a scanned/uploaded product.

    Matches the ``product_metadata`` object in the Architecture.md
    Structured Product Payload schema.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    product_id: str = Field(
        ..., description="Internal unique product identifier, e.g. 'prod_987654321'."
    )
    upc: Optional[str] = Field(
        default=None, description="UPC barcode, if captured via OCR or matched in the DB."
    )
    brand_name: str = Field(..., description="Brand or manufacturer name.")
    product_name: str = Field(..., description="Product display name.")
    formula_version: int = Field(
        default=1, ge=1, description="Formula revision number, used to detect version mismatches."
    )
    serving_size: str = Field(..., description="Serving size as printed on the label, e.g. '2 capsules'.")
    servings_per_container: int = Field(..., gt=0, description="Number of servings per container.")
    certifications: List[str] = Field(
        default_factory=list, description="Third-party certifications, e.g. ['NSF', 'GMP']."
    )


class ProductIngredient(BaseModel):
    """A single ingredient line item, standalone or part of a proprietary blend.

    Matches one entry of the ``product_ingredients`` array in the
    Architecture.md Structured Product Payload schema.
    """

    raw_name: str = Field(..., description="Ingredient name as printed on the label.")
    normalized_id: Optional[str] = Field(
        default=None,
        description="Canonical ingredient/blend ID once resolved against the ingredient DB "
        "(e.g. 'ing_ashwagandha_01'); null until Phase 1 normalization succeeds.",
    )
    dose_amount: float = Field(..., ge=0, description="Dose quantity per serving.")
    dose_unit: str = Field(..., description="Unit for dose_amount, e.g. 'mg', 'mcg', 'IU'.")
    standardization: Optional[str] = Field(
        default=None, description="Standardization spec, e.g. '5% Withanolides'."
    )
    is_proprietary_blend: bool = Field(
        default=False, description="True if this line represents an undisclosed-dose proprietary blend."
    )
    blend_components: Optional[List[str]] = Field(
        default=None, description="Disclosed component names within a proprietary blend, if any."
    )

    @model_validator(mode="after")
    def validate_blend_consistency(self) -> "ProductIngredient":
        """Reject payloads where blend_components is set without the blend flag.

        Prevents ambiguous state from reaching the Phase 3 Combination
        Engine, which relies on ``is_proprietary_blend`` to trigger the
        Proprietary Blend Penalty calculation.
        """
        if self.blend_components and not self.is_proprietary_blend:
            raise ValueError("blend_components provided but is_proprietary_blend is False")
        return self


class StructuredProductPayload(BaseModel):
    """Full normalized payload produced at the end of Phase 1.

    This is the object handed to Phase 2 (per-ingredient scientific
    grading) once the Primary Identifier Lookup has either resolved to a
    DB match or fallen back to a user Draft Record. ``match_status`` and
    ``similarity_score`` are not part of the literal Architecture.md JSON
    example but are implied by the Phase 1 flow diagram and are included
    here so downstream services can branch on lookup confidence; drop
    them if you'd rather track that state elsewhere.
    """

    match_status: MatchStatus = Field(
        default=MatchStatus.DRAFT,
        description="Whether this payload resolved to a confident DB match or fell back to a draft.",
    )
    similarity_score: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        description="Similarity percentage from the Primary Identifier Lookup step (match threshold: 80%).",
    )
    product_metadata: ProductMetadata
    product_ingredients: List[ProductIngredient] = Field(default_factory=list)
