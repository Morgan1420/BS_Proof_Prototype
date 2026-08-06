# Supplement & Health Product Automated Grading System — Architecture Specification

This document provides a complete technical handover of the multi-tiered health product evaluation pipeline established so far. Use this context to continue algorithm design or pass to downstream development systems.

---

## 1. System Overview & Core Philosophy

The system evaluates health and beauty products (primarily dietary supplements) using a evidence-based, four-phase processing pipeline.

### Key Architectural Decisions

* **Decoupled Knowledge Base:** Scientific literature retrieval and paper grading operate asynchronously per ingredient. Results are cached globally in a **Standardized Ingredient Grade Schema (SIFG)** to prevent redundant execution and keep latency low.
* **Non-Blocking Fallbacks:** OCR image noise or missing database matches yield *Draft Records* rather than hard user errors, allowing live grading to proceed uninterrupted using extracted OCR data as primary payload.
* **Objective Evidence Weighting:** Scientific claims are weighted strictly by study design quality (Systematic Reviews > RCTs > Observational > Animal/In-vitro), sample size, journal impact factor, and conflict-of-interest penalties.

---

## Phase 1: Identification & Payload Structuring

```
[ User Scan / Upload ]
         │
         ▼
[ Vision-LLM Parsing ] ──> Extract Raw Metadata & Ingredients
         │
         ▼
[ Primary Identifier Lookup ] (UPC Barcode / Composite Name Hash)
         │
  ┌──────┴────────────────┐
  ▼                       ▼
[ Similarity ≥ 80% ]    [ Similarity < 80% ]
  │                       │
  │                       ▼
  │             [ External API / Web Search ]
  │                       │
  │                 ┌─────┴──────────────────┐
  │                 ▼                        ▼
  │           [ Product Found ]      [ Not Found / Version Mismatch ]
  │                 │                        │
  └────────┬────────┘                        ▼
           │                       [ Create User Draft Payload ]
           ▼                                 │
 [ Structure Normalized JSON Payload ] <──────┘

```

### JSON Schema: Structured Product Payload

```json
{
  "product_metadata": {
    "product_id": "prod_987654321",
    "upc": "012345678905",
    "brand_name": "Example Labs",
    "product_name": "Daily Focus Boost",
    "formula_version": 2,
    "serving_size": "2 capsules",
    "servings_per_container": 30,
    "certifications": ["NSF", "GMP"]
  },
  "product_ingredients": [
    {
      "raw_name": "KSM-66 Ashwagandha",
      "normalized_id": "ing_ashwagandha_01",
      "dose_amount": 600,
      "dose_unit": "mg",
      "standardization": "5% Withanolides",
      "is_proprietary_blend": false
    },
    {
      "raw_name": "Energy Blend",
      "normalized_id": "blend_prop_01",
      "dose_amount": 400,
      "dose_unit": "mg",
      "standardization": null,
      "is_proprietary_blend": true,
      "blend_components": ["Caffeine Anhydrous", "L-Theanine"]
    }
  ]
}

```

---

## Phase 2: Single Ingredient Scientific Grading

```
[ For Each Ingredient in Payload ]
                 │
                 ▼
     [ DB Cache: Graded Before? ]
         ┌───────┴───────┐
       (YES)            (NO)
         │               │
         │               ▼
         │     [ PubMed API Search ] ──> Retrieve Top 10 High-Impact Papers
         │               │
         │               ▼
         │     [ LLM Paper Evaluator ] ──> Extract Risk of Bias & Quantitative Data
         │               │
         │               ▼
         │     [ Consensus Engine ] ──> Synthesize Evidence & Dose Mapping
         │               │
         │               ▼
         │     [ Store SIFG JSON in Database Cache ]
         └───────┬───────┘
                 │
                 ▼
[ Pass SIFG Data to Combination Engine ]

```

### 1. Risk of Bias & Quality Weighting Matrix

Each paper is assigned an evidentiary weight $W_{paper}$ based on methodological rigor:

* **Evidence Hierarchy ($W_{tier}$):**
* *Tier 1 (1.0x):* Meta-Analyses & Systematic Reviews (AMSTAR 2 evaluated).
* *Tier 2 (0.85x):* Double-Blind Randomized Controlled Trials (RCTs).
* *Tier 3 (0.50x):* Open-Label / Cohort / Observational Human Studies.
* *Tier 4 (0.15x):* Animal Models (*in-vivo*).
* *Tier 5 (0.05x):* Cell Culture (*in-vitro*).


* **Rigor Modifiers:**
* *Sample Size ($n$):* Penalty if $n < 30$.
* *Journal Quality:* Filtered via SCImago SJR / DOAJ predatory check.
* *Conflict of Interest (COI):* -30% penalty if directly funded by ingredient patent-holder/brand.



### 2. Consensus Score Formula

Claims are assigned directional scores ($+1.0$ positive, $0.0$ neutral, $-1.0$ adverse). The final score per claim is:

$$\text{Consensus Score} = \frac{\sum (\text{Directional Value} \times W_{\text{paper}})}{\sum W_{\text{paper}}}$$

### 3. Composite Score, Grade & Confidence (implementation note, Step 4)

This spec originally left several calibration constants unspecified. `app/services/consensus_engine.py` fills them in as follows; treat these as tunable, not canonical:

* *Sample Size penalty magnitude:* the spec says "penalty if $n < 30$" but not by how much -- implemented as a **0.75x** multiplier on $W_{paper}$ (25% reduction). A paper with no detectable sample size is not penalized.
* *Journal Quality (SCImago/DOAJ) filtering:* **not implemented** -- out of scope for Step 4; `W_{paper}` is currently just $W_{tier} \times$ (sample-size modifier) $\times$ (COI modifier).
* *Composite score (0-100):* the ingredient-level Consensus Score (pooled across all evaluated papers, range $[-1, 1]$) is linearly rescaled: $\text{composite\_score} = (\text{Consensus Score} + 1) / 2 \times 100$.
* *Evidence grade (A-F):* thresholded directly from `composite_score` using conventional bands (A $\geq$ 90, B $\geq$ 80, C $\geq$ 70, D $\geq$ 60, else F).
* *Evidence confidence score (0-1):* distinct from the composite score -- reflects evidence *volume and quality*, not directionality: $\text{confidence} = \text{avg}(W_{paper}) \times \min(1, \text{papers\_evaluated} / 10)$, where 10 mirrors the "Top 10 Papers" retrieval target from Phase 2.
* *Per-claim evidence_level (High/Moderate/Low):* thresholded from the claim group's average $W_{paper}$ (High $\geq$ 0.75, Moderate $\geq$ 0.40, else Low).
* *No evaluable papers* (empty PubMed result, or the LLM Paper Evaluator call fails outright): degrades to `composite_score: 0`, `overall_confidence_score: 0`, `evidence_grade: "F"`, `validated_claims: []`, rather than raising -- consistent with this project's Non-Blocking Fallbacks decision.
* *`dosage_benchmarks` / `safety_and_side_effects` (implementation note, Step 5):* the Consensus Engine does not compute these -- dose-response and adverse-event extraction is a separate, not-yet-built step. Both fields are nullable on `IngredientGradeSchema` and are `None` (not a fabricated placeholder) until that step exists.

### API Surface (implementation note, Step 5)

`app/main.py` + `app/api/v1/endpoints/scan.py` expose the Phase 1 -> Phase 2 pipeline over HTTP:

* `POST /api/v1/scan` -- accepts a label image upload, runs Vision Parsing (Phase 1) synchronously, and returns a `job_id` plus the raw ingredient list immediately (HTTP 202). PubMed retrieval + Consensus Engine scoring (Phase 2) for every ingredient then runs as a FastAPI `BackgroundTasks` job.
* `GET /api/v1/ingredients/{ingredient_id}` -- returns the `IngredientGradeSchema` once background grading completes (200), a lightweight `{status: "processing"}` body while it's still running (202), a `{status: "failed", error}` body if grading errored for that ingredient (200), or 404 if the id is unknown.
* Job/ingredient state is held in a process-local in-memory store (`app/services/pipeline.py`) -- **not persistent, not multi-worker safe**. This stands in for the "Store SIFG JSON in Database Cache" step in the Phase 2 diagram until real persistence (e.g. Postgres via SQLAlchemy, per `database_url` in `Settings`) is added.

### JSON Schema: Standardized Ingredient Grade Schema (SIFG)

```json
{
  "ingredient_id": "ing_ashwagandha_01",
  "canonical_name": "Withania Somnifera",
  "evidence_summary": {
    "total_papers_analyzed": 10,
    "composite_score": 91.5,
    "evidence_grade": "A",
    "overall_confidence_score": 0.88
  },
  "dosage_benchmarks": {
    "minimum_effective_dose_mg": 300,
    "optimal_dose_range_mg": "600-900",
    "maximum_safe_daily_dose_mg": 1200,
    "unit": "mg"
  },
  "validated_claims": [
    {
      "claim": "Stress & Anxiety Reduction",
      "consensus_score": 0.91,
      "evidence_level": "High",
      "supporting_studies_count": 7
    }
  ],
  "safety_and_side_effects": {
    "safety_rating": "Safe",
    "known_interactions": ["Thyroid Medications", "Sedatives"],
    "common_side_effects": ["Mild Drowsiness", "Gastrointestinal Upset"]
  }
}

```

---
## Phase 3: Ingredient Combination Grading (Draft Specs)

!! This phase is still in a draft phase, avoid trying to build anything from this unless specified by the master. 

* **Pairwise Synergies (+10 to +25%):** Positive bioavailability boosts (e.g., Piperine + Curcumin, Vitamin D3 + K2).
* **Negative Interactions (-15 to -40%):** Competition for intestinal transporters (e.g., High-dose Zinc + Copper, Calcium + Iron) or mechanism cancellation.
* **Proprietary Blend Penalty:**

$$\text{Penalty Multiplier} = 1 - \left(0.35 \times \frac{\text{Undisclosed Blend Mass}}{\text{Total Product Mass}}\right)$$

