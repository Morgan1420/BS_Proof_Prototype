"""Pydantic schemas for the single-step vision scan pipeline, plus per-ingredient grading.

A scan itself still does ONE thing: send the label image to Gemini once
and extract exactly what's printed on it (product metadata + each
ingredient's name, form, amount/dosage, and % Daily Value). Full
multi-ingredient scientific grading (the old always-on PubMed retrieval
+ consensus scoring pass over every ingredient in a scan) is still gone
-- see ``docs/Architecture.md``'s history note. What's back, as of the
single-ingredient grading feature, is an explicit, on-demand
``POST /api/ingredients/{ingredient_id}/grade`` a user can trigger for
one ingredient at a time (see ``app.services.grading_service`` and
``app.services.pubmed_client``); ``ScannedIngredient`` below carries that
grade's result fields, all nullable and defaulted to "not graded yet"
until that endpoint actually runs for this ingredient.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

GradeStatus = Literal["pending", "graded", "failed"]


def generate_ingredient_id() -> str:
    """A short, unique, stable-once-assigned id for one ingredient line.

    Assigned server-side (never by Gemini's label extraction -- see
    ``app.services.vision_parser``'s separate, narrower extraction
    schema) so it has nothing to do with what's printed on the label.
    Used to address one ingredient for grading, independent of which
    scan/product it came from.
    """
    return f"ing_{uuid.uuid4().hex[:12]}"


class ScannedIngredient(BaseModel):
    """One ingredient line as extracted from the label image, plus its (optional) grading state.

    The label-derived fields (everything except ``name``) are nullable
    and left ``None`` rather than guessed when the label doesn't print
    them -- this project never fabricates supplement dosage/safety data.
    The grading fields are equally honest about absence: a fresh
    ingredient is ``grade_status="pending"`` with every other grading
    field ``None`` until ``POST /api/ingredients/{ingredient_id}/grade``
    actually runs and succeeds for it (see
    ``app.services.grading_service.IngredientGradingService``). Nothing
    here is ever pre-filled with a plausible-looking placeholder grade.
    """

    ingredient_id: str = Field(
        default_factory=generate_ingredient_id,
        description="Server-assigned id used to address this one ingredient for grading, e.g. via "
        "POST /api/ingredients/{ingredient_id}/grade. Stable once persisted to "
        "data/scanned_ingredients.json -- see app.services.storage.ScanStorage.backfill_ingredient_ids "
        "for how older, pre-existing records get one assigned.",
    )
    name: str = Field(..., description="Ingredient name as printed on the label, e.g. 'Ashwagandha'.")
    form: Optional[str] = Field(
        default=None,
        description="Form as printed, e.g. a specific extract name ('KSM-66 Root Extract'), a chemical "
        "form ('Citrate', 'Chelate'), or a delivery form ('Capsule'). Null if not stated.",
    )
    amount: Optional[float] = Field(default=None, ge=0, description="Dose amount per serving, e.g. 600.")
    unit: Optional[str] = Field(default=None, description="Unit for `amount`, e.g. 'mg', 'mcg', 'IU'.")
    percent_daily_value: Optional[str] = Field(
        default=None,
        description="% Daily Value as printed next to this ingredient, e.g. '150%'. Null if the label "
        "doesn't print one (common for ingredients with no established Daily Value, often shown as a "
        "dagger symbol instead of a percentage).",
    )

    # --- Single-ingredient grading (see app.services.grading_service) ---
    grade_status: GradeStatus = Field(
        default="pending",
        description="'pending' until POST /api/ingredients/{ingredient_id}/grade is run for this "
        "ingredient; 'graded' once it succeeds; 'failed' if the last attempt errored (still re-gradable "
        "-- the UI should offer a retry, not a dead end).",
    )
    sifg_grade: Optional[str] = Field(
        default=None,
        description="Overall Supplement Ingredient Fact Grade from the last successful grading pass, "
        "e.g. 'B+' or 'Insufficient Evidence'. Null until grade_status='graded'.",
    )
    sifg_score: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        description="Numeric 0-100 companion to sifg_grade from the last successful grading pass. Null "
        "until grade_status='graded'.",
    )
    efficacy_safety_evaluation: Optional[str] = Field(
        default=None,
        description="Gemini's efficacy/safety evaluation from the last successful grading pass, grounded "
        "in the PubMed excerpts that were actually found for this ingredient/form/dose. Null until "
        "grade_status='graded'.",
    )
    dosage_appropriateness: Optional[str] = Field(
        default=None,
        description="Gemini's assessment of this label's printed dose against typical studied dosing. "
        "Null until grade_status='graded'.",
    )
    evidence_summary: Optional[str] = Field(
        default=None,
        description="Short plain-language summary of the evidence considered -- explicitly says so if no "
        "relevant PubMed literature was found, rather than fabricating support. Null until "
        "grade_status='graded'.",
    )
    raw_consensus: Optional[Dict[str, Any]] = Field(
        default=None,
        description="The full raw JSON Gemini returned for this grading pass (see "
        "app.services.grading_service.SifgConsensus), kept as-is for the UI's expandable 'raw JSON' "
        "view. Null until grade_status='graded'.",
    )
    grading_stats: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Backend-computed metadata about the last grading attempt -- NOT part of Gemini's "
        "own output (see app.services.grading_service.GradingStats): papers_found, papers_analyzed, "
        "search_queries, grading_duration_seconds, model_used. Deliberately kept separate from "
        "raw_consensus so it's never mistaken for something Gemini itself reported. Populated for both "
        "grade_status='graded' and 'failed' (a failed Gemini call still ran a literature search, whose "
        "stats are worth keeping) -- null only before any grading attempt has been made.",
    )
    graded_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp of the last grading attempt (successful or failed). Null until at "
        "least one grading attempt has been made.",
    )


class ScannedProductMetadata(BaseModel):
    """Top-level product identification as printed on the label."""

    model_config = ConfigDict(str_strip_whitespace=True)

    brand_name: Optional[str] = Field(default=None, description="Brand or manufacturer name as printed.")
    product_name: Optional[str] = Field(default=None, description="Product display name as printed.")
    serving_size: Optional[str] = Field(default=None, description="Serving size as printed, e.g. '2 capsules'.")
    servings_per_container: Optional[int] = Field(
        default=None,
        description="Servings per container as printed. Null if not legible or not a single fixed count "
        "(e.g. non-US labels with a variable dosing range) -- never a fabricated guess.",
    )


class ScanResult(BaseModel):
    """Response body for ``POST /api/scan``, and one entry of ``GET /api/ingredients``'s list.

    This is exactly what gets appended to ``data/scanned_ingredients.json``
    (see ``app.services.storage.ScanStorage``) -- one record per scan,
    holding the product metadata plus every ingredient extracted from
    that single label image.
    """

    scan_id: str = Field(..., description="Unique id for this scan, e.g. 'scan_a1b2c3d4e5f6'.")
    scanned_at: datetime = Field(..., description="UTC timestamp the scan was performed.")
    product: ScannedProductMetadata
    ingredients: List[ScannedIngredient] = Field(default_factory=list)
