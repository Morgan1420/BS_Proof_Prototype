# Supplement & Health Product Scan — Architecture Specification

This document is the technical handover for the current pipeline: a single-step vision scan, plus optional, on-demand, single-ingredient scientific grading.

---

## 1. System Overview & Core Philosophy

This project previously planned a four-phase always-on pipeline (vision extraction, PubMed-backed scientific grading, ingredient combination analysis, and a final product grade). That always-on multi-ingredient grading pass, along with combination analysis and product-grade synthesis, was removed entirely (see "History" at the bottom of this document) in favor of a system that does one thing automatically: turn a photo of a supplement label into a structured list of its ingredients, and save that list locally. Scientific grading has since been reintroduced, but in a deliberately narrower, opt-in form: a user can trigger a real multi-source literature search (PubMed, Europe PMC, OpenAlex, and Semantic Scholar) + one Gemini SIFG evaluation for exactly one ingredient at a time, on demand, via a "Grade" button -- there is still no automatic, always-on, whole-scan grading pass, and no combination/product-grade synthesis of any kind.

### Key Architectural Decisions

* **Single-Step Vision Pipeline:** A scan is exactly one Gemini call. `POST /api/scan` sends the label image, gets back the extracted product metadata and ingredient list, and returns it to the caller synchronously -- no job ids, no background tasks, no polling.
* **Never Fabricate Label Data:** Any field Gemini can't read off the label (a missing form, a dose with no printed unit, an ingredient with no stated % Daily Value) is left `null`, never guessed or defaulted to a plausible-looking value. See `app/services/vision_parser.py`'s `EXTRACTION_SYSTEM_PROMPT` and every field's docstring in `app/schemas/scan.py`.
* **Fail Loud, Not Silent:** Unlike the old pipeline's "Non-Blocking Fallback" (degrade to a fabricated draft record), a failed scan now raises a clear error (`VisionParsingError` -> HTTP 502) instead of silently persisting garbage. Nothing is written to storage for a failed scan.
* **Fully Dynamic, Env-Driven Model -- No Hardcoded Constant, No Fallbacks:** Which Gemini model gets called is read fresh, on every call, from `Settings.gemini_model` (env var `GEMINI_MODEL`, see `backend/.env`; field default `"gemini-2.0-flash"` in `app/core/config.py`). `app/services/gemini_client.py` has no `MODEL` constant of any kind -- `generate_content(..., settings=...)` resolves the model via `_resolve_model(settings)`, which reads `settings.gemini_model` and strips any leading `"models/"` prefix (so both `"gemini-2.0-flash"` and `"models/gemini-2.0-flash"` work identically; the prefixed form is what `client.models.list()` itself returns). `VisionParserService` threads its own `self._settings` down into every `generate_content` call, so a single `Settings` instance is the one source of truth for both the API key and the model. Switching models is now a one-line edit to `backend/.env`'s `GEMINI_MODEL` and a process restart -- no code change. There is still no dynamic model discovery (`client.models.list()`) used at call time, no candidate list, and no rotation/fallback to any other model string within a single call -- only ever the one model `settings.gemini_model` currently names. History of models tried here as free-tier availability/quota shifted: `"gemini-2.5-flash"` (404'd -- no longer available to new users) -> `"gemini-2.0-flash"` (429'd -- free-tier quota set to 0) -> `"gemini-2.5-flash-lite"` (worked, confirmed active on the project dashboard). Run `backend/scripts/list_gemini_models.py` to check what a given API key currently has access to.
* **No Retries, Except a Bounded, Graceful Retry on 429s:** A Gemini call is attempted exactly ONCE (`gemini_client.generate_content`) for any non-429 failure -- auth, malformed request, 404, network error, etc. all fail immediately and raise `GeminiCallError` (wrapped as `VisionParsingError` by the vision layer, and surfaced to the caller as HTTP 502). No "model not found" fallback, no same-model retry for these, no candidate rotation. The one exception is a 429 / RESOURCE_EXHAUSTED `google.genai.errors.ClientError`: `generate_content` retries it in place -- same model, same request -- up to `MAX_RATE_LIMIT_RETRIES` (3) additional times (so up to 4 attempts total), waiting the `retryDelay` Google's own error response suggests (parsed from the `RetryInfo` detail in the error body) before each retry, or `DEFAULT_RATE_LIMIT_RETRY_DELAY_SECONDS` (15s) if that detail is missing or unparseable. If the 429 is still failing after all retries, or the error isn't recognizably a `ClientError` (e.g. the SDK isn't installed, or it's some other 4xx), it fails the same as any other error. The full raw exception -- message and stack trace -- is printed directly to stdout at the point of a terminal failure, in addition to the structured log line, so a failure is visible without digging through log formatting; each 429 retry attempt is also logged to stdout before its wait. This intentionally replaces a much more elaborate prior version of `gemini_client.py` (multi-model candidate rotation, dynamic model discovery, 429-aware backoff with per-model and shared retry budgets) -- that machinery is gone; only this narrow, single-model 429 retry remains.
* **Mandatory 60-Second Pause, Process-Wide:** To keep this service under Gemini's free-tier per-minute rate limit, every call -- however it resolves (success, exhausted 429 retries, or an immediate non-429 failure) -- is followed by a hard `RATE_LIMIT_PAUSE_SECONDS` (60s) pause, held under a single module-level `asyncio.Lock` shared by every call the process makes. That lock is held for the entire call, including any 429 retry waits, so those retry-delay sleeps happen *inside* this same 60s-gated window, not on top of it. The lock makes the pause a genuinely global "at most one request in flight, retrying, or cooling down at a time" gate: two concurrent scan requests can't both fire a Gemini call within 60 seconds of each other, even though each is handled by its own independent request/task. A scan issues at most 1 Gemini request (plus up to 3 in-place 429 retries of that same request) total.
* **Standard Synchronous SDK Call:** `generate_content` calls the plain synchronous `client.models.generate_content(model=..., contents=..., config=...)` method (not `client.aio.models...`), run via `asyncio.to_thread` so the async function doesn't block the server's event loop for the duration of the request. The Gemini call itself is the ordinary, documented synchronous google-genai usage; the `to_thread` wrapper is purely so it doesn't freeze the rest of the app while in flight.
* **Grading Is Single-Ingredient, On-Demand, Never Automatic:** `POST /api/ingredients/{ingredient_id}/grade` runs for exactly one ingredient, only when a user explicitly requests it (the frontend's "Grade" button) -- there is no background job, scheduled task, or automatic trigger that grades an ingredient as a side effect of scanning it. Grading one ingredient never reads or writes any other ingredient, in that scan or any other. `app/services/grading_service.IngredientGradingService` reuses the exact same `gemini_client.generate_content` machinery scanning uses (same dynamic `Settings.gemini_model`, same zero-retry-except-429 behavior, same process-wide 60s pause/lock -- see the bullets above), so a grading call and a scan call anywhere in the process still can't fire within 60 seconds of each other.
* **Multi-Source Literature Retrieval, Ranked and Cut to a Top-N Before Gemini Ever Sees It:** `app/services/literature_search.aggregate_literature` queries four public paper-search APIs IN PARALLEL for the ingredient's exact name/form -- PubMed (via `app.services.pubmed_client`, NCBI E-utilities), Europe PMC (`https://www.ebi.ac.uk/europepmc/webservices/rest/search`), OpenAlex (`https://api.openalex.org/works`), and Semantic Scholar (`https://api.semanticscholar.org/graph/v1/paper/search`) -- via `asyncio.gather(..., return_exceptions=True)`, so each source is isolated: one failing (network, timeout, unparseable response) never blocks or fails the others. Results are merged, deduplicated (by DOI, then PMID, then normalized title -- keeping whichever duplicate has the more complete metadata when the same paper turns up from more than one source), then ranked by a weighted 0-100 quality score -- study type up to 40 pts (systematic reviews/meta-analyses/RCTs score highest, recognized-but-lower-tier designs score less, unclassifiable types score lowest-but-nonzero), citation count up to 30 pts (log-scaled), recency up to 20 pts (full marks within 5 years, tapering linearly to zero by 10 years old), and title keyword match (form and/or dose appearing in the paper's own title) up to 10 pts -- and only the top `Settings.literature_top_papers_limit` (default 20) papers are selected as Gemini's context. Dosage is deliberately not encoded into any of the four API queries themselves (free-text search doesn't reliably filter on numeric doses); it instead contributes to the keyword-match ranking score and is given directly to Gemini as prompt context for `dosage_appropriateness`. Study-type classification is a ranking SIGNAL ONLY (inferred from a source's own metadata when available, or a best-effort keyword scan otherwise) -- it is never presented to Gemini as a verified fact.
* **Grading Degrades Gracefully on a Dead Literature Source, Never on a Dead Gemini:** If a single provider (PubMed, Europe PMC, OpenAlex, or Semantic Scholar) is slow, unreachable, or returns something unparseable, that provider simply contributes zero papers -- the others still feed the ranked result normally. Only when literally EVERY provider fails does `IngredientGradingService.grade_ingredient` treat the run as `search_failed=True` and downgrade to "zero studies" for the Gemini prompt; Gemini is told explicitly that the search failed (not just that it found nothing), so `evidence_summary` can say so honestly. Only a failure in the Gemini evaluation step itself (`GeminiCallError`) raises `GradingError`, surfaced as HTTP 502, with the ingredient's `grade_status` persisted as `"failed"` (not silently reverted) so the UI can offer a retry.
* **Never Fabricate Grading Evidence:** `GRADING_SYSTEM_PROMPT` (`app/services/grading_service.py`) instructs Gemini to cite only the study excerpts actually included in that call's prompt, and to say so explicitly (grading conservatively, typically `sifg_grade="Insufficient Evidence"`) whenever no studies were found or the search failed -- never to invent a citation, identifier, or finding. Same "never fabricate" principle as label extraction, applied to evidence instead of dosage data.
* **ingredient_id Is Server-Assigned and Stable:** Every `ScannedIngredient` carries a server-generated `ingredient_id` (`app.schemas.scan.generate_ingredient_id`), used to address it for grading -- Gemini never sees or fills this field during label extraction (see `app/services/vision_parser.py`'s separate, narrower `_ExtractedIngredient` schema, which deliberately excludes every grading field so the vision call's `response_schema` can't be asked to hallucinate a grade from a photo). `ScanStorage.backfill_ingredient_ids()` runs once at startup to assign a persisted id to any ingredient record written before this field existed -- without it, an old record's id would be regenerated fresh on every `GET /api/ingredients` response (from the schema's `default_factory`) and never match what's actually on disk, making grading it 404 forever.
* **Every Grading Phase Is Logged in Real Time:** `app/services/grading_service.log_grading_step` prints (and logs) one `[GRADING STEP x/5] ingredient_id=... -- ...` line per phase, straight to stdout, so a grading run's progress is visible in the terminal as it happens rather than only after the fact: (1) the target ingredient's name/form/dose, (2) the literature-retrieval summary -- printed by the separate `app.services.literature_search.log_retrieval_summary` in a fixed format with no `ingredient_id=...` prefix: each provider's raw paper count (marked `(FAILED)` if that provider errored), the total unique papers found after dedup, and how many were ranked/selected for Gemini, (3) the full system + user prompt sent to Gemini, (4) Gemini's raw JSON response plus its own stated reasoning (`evidence_summary` / `efficacy_safety_evaluation` / `dosage_appropriateness` -- explicitly labeled as Gemini's own response fields, not a separate hidden chain-of-thought trace, since structured-output mode doesn't expose one), and (5) the final grade and confirmation it was persisted, logged by `app/api/routes.grade_ingredient` after the actual disk write. A failed grade still logs a step 5 line (marked FAILED) instead of silently stopping at step 4.
* **Grading Stats Are Backend-Computed, Kept Separate From Gemini's Output:** Every grading attempt (`app.services.grading_service.IngredientGradingService.grade_ingredient`) returns a `GradingResult` -- the Gemini `SifgConsensus` plus a `GradingStats` (`papers_found`, `papers_analyzed`, `provider_counts`, `search_queries`, `grading_duration_seconds`, `model_used`), timed via `time.monotonic()` around the whole literature-retrieval-plus-evaluation run. These numbers come from this codebase's own bookkeeping, not from Gemini, so they're persisted to a separate `grading_stats` field on `ScannedIngredient` rather than folded into `raw_consensus` -- keeping "what Gemini said" and "what we measured about the run" honestly distinct. A failed Gemini call still attaches whatever `GradingStats` were computable up to that point to `GradingError.stats`, so even a `grade_status="failed"` record can retain useful diagnostics (e.g. "the literature search worked and found 12 papers across 3 sources, but Gemini itself then failed").

---

## 2. Pipeline

```
[ User Scan / Upload ]
         │
         ▼
[ Vision-LLM Parsing ] ──> Extract product metadata & ingredient list
         │                 (name, form, amount, unit, % Daily Value)
         ▼
[ Append to data/scanned_ingredients.json ]        each ingredient starts
         │                                          grade_status="pending"
         ▼
[ Return ScanResult to caller ]


[ User taps "Grade" on ONE ingredient ]   (never automatic -- see Key Architectural Decisions)
         │
         ▼
[ 4-source literature search, in parallel ] ──> PubMed, Europe PMC, OpenAlex,
         │                                       Semantic Scholar for that ingredient's
         │                                       exact name/form (one provider failing
         │                                       doesn't block the others)
         ▼
[ Dedup + weighted-quality ranking + top-N cut ] ──> top ~20 papers by study type,
         │                                            citations, recency, keyword match
         │                        (degrades to "0 studies, search failed" only if
         │                         EVERY provider fails -- never fails the grade)
         ▼
[ One Gemini SIFG evaluation call ] ──> grade, score, efficacy/safety evaluation,
         │                              dosage assessment, evidence summary
         ▼
[ Update that ingredient's record in data/scanned_ingredients.json ]
         │
         ▼
[ Return the updated ingredient to caller ]
```

Scanning still has no matching against a product database, no similarity scoring, and no draft-vs-matched distinction. Grading still has no ingredient-combination synergy/interaction analysis and no product-grade synthesis -- it's a per-ingredient evaluation, nothing more, and it only ever runs for the one ingredient a user explicitly requests.

### JSON Schema: ScanResult / ScannedIngredient

Mirrored by `app/schemas/scan.py` (`ScanResult` / `ScannedProductMetadata` / `ScannedIngredient`). This is exactly what `POST /api/scan` returns, and exactly what one entry of `GET /api/ingredients`'s list looks like -- also exactly what's appended to `data/scanned_ingredients.json`. A freshly scanned ingredient's grading fields are all `null`/`"pending"`, as below; see "Single-Ingredient Grading" further down for what they look like once `POST /api/ingredients/{ingredient_id}/grade` has run.

```json
{
  "scan_id": "scan_a1b2c3d4e5f6",
  "scanned_at": "2026-08-07T12:34:56.789Z",
  "product": {
    "brand_name": "Example Labs",
    "product_name": "Daily Focus Boost",
    "serving_size": "2 capsules",
    "servings_per_container": 30
  },
  "ingredients": [
    {
      "ingredient_id": "ing_a1b2c3d4e5f6",
      "name": "Ashwagandha",
      "form": "KSM-66 Root Extract",
      "amount": 600,
      "unit": "mg",
      "percent_daily_value": null,
      "grade_status": "pending",
      "sifg_grade": null,
      "sifg_score": null,
      "efficacy_safety_evaluation": null,
      "dosage_appropriateness": null,
      "evidence_summary": null,
      "raw_consensus": null,
      "grading_stats": null,
      "graded_at": null
    },
    {
      "ingredient_id": "ing_f6e5d4c3b2a1",
      "name": "Vitamin D3",
      "form": "Cholecalciferol",
      "amount": 25,
      "unit": "mcg",
      "percent_daily_value": "125%",
      "grade_status": "pending",
      "sifg_grade": null,
      "sifg_score": null,
      "efficacy_safety_evaluation": null,
      "dosage_appropriateness": null,
      "evidence_summary": null,
      "raw_consensus": null,
      "grading_stats": null,
      "graded_at": null
    }
  ]
}
```

`servings_per_container` (implementation note): nullable, no minimum-value constraint. Many non-US labels print a variable dosing range instead of one fixed count, and Gemini's structured-output schema validator has rejected Pydantic's `gt=0` (translated to JSON Schema `exclusiveMinimum`) in practice. The extraction prompt instructs the model to return `null` rather than `0` when the count isn't determinable; the schema itself accepts either.

`ingredient_id` (implementation note): server-assigned, never something Gemini's label-extraction call sees or fills in -- see `app/services/vision_parser.py`'s separate `_ExtractedIngredient` schema. `grade_status` is one of `"pending"` (default, never graded) / `"graded"` (last attempt succeeded) / `"failed"` (last attempt errored -- still re-gradable, not a dead end).

### Single-Ingredient Grading

`POST /api/ingredients/{ingredient_id}/grade` grades exactly the one ingredient named in the URL and returns its updated `ScannedIngredient` record:

```json
{
  "ingredient_id": "ing_a1b2c3d4e5f6",
  "name": "Ashwagandha",
  "form": "KSM-66 Root Extract",
  "amount": 600,
  "unit": "mg",
  "percent_daily_value": null,
  "grade_status": "graded",
  "sifg_grade": "B+",
  "sifg_score": 78,
  "efficacy_safety_evaluation": "Generally well tolerated across the reviewed studies, with mild GI effects the most commonly reported issue at this dose.",
  "dosage_appropriateness": "600mg/day is within the range used in the reviewed clinical trials.",
  "evidence_summary": "Based on 12 studies aggregated from PubMed, Europe PMC, OpenAlex, and Semantic Scholar for 'Ashwagandha KSM-66 Root Extract supplementation'.",
  "raw_consensus": { "sifg_grade": "B+", "sifg_score": 78, "...": "full raw Gemini JSON, unmodified" },
  "grading_stats": {
    "papers_found": 17,
    "papers_analyzed": 12,
    "provider_counts": { "PubMed": 5, "Europe PMC": 6, "OpenAlex": 4, "Semantic Scholar": 3 },
    "search_queries": ["Ashwagandha KSM-66 Root Extract supplementation"],
    "grading_duration_seconds": 4.217,
    "model_used": "gemini-2.0-flash"
  },
  "graded_at": "2026-08-07T12:40:02.123Z"
}
```

`grading_stats` (implementation note): computed by this codebase (`app.services.grading_service.GradingStats`), not returned by Gemini -- kept as a sibling of `raw_consensus` rather than inside it so the two are never conflated. `papers_found` is the unique count across all four sources AFTER deduplication but BEFORE the top-N ranking cut; `papers_analyzed` is how many of those were actually selected (ranked highest) for the Gemini prompt -- at most `Settings.literature_top_papers_limit` (default 20). `provider_counts` is each source's RAW count before dedup, so it can sum to more than `papers_found` when the same paper is found by more than one source; a source that failed outright contributes `0`. `search_queries` has one entry per distinct query actually executed across every provider -- typically one (the shared query built from name + form), plus a second if PubMed's own broader name-only fallback query was also tried (see "Single-Ingredient Grading" below).

Implementation, end to end (see `app/api/routes.grade_ingredient` and `app/services/grading_service.IngredientGradingService.grade_ingredient`, which logs one `[GRADING STEP x/5]` line per phase to stdout -- see "Every Grading Phase Is Logged in Real Time" above):

1. `ScanStorage.get_ingredient(ingredient_id)` finds the ingredient across every saved scan (404 if not found). *(logged as step 1: target ingredient)*
2. `app/services/literature_search.aggregate_literature(name, form, amount, unit)` -- queries PubMed (via `app.services.pubmed_client.search_literature`, itself an `esearch.fcgi` call for matching PMIDs plus one `efetch.fcgi` call for titles/abstracts, with its own broader name-only fallback query if the primary query finds nothing and a form was given), Europe PMC, OpenAlex, and Semantic Scholar all IN PARALLEL (`asyncio.gather(..., return_exceptions=True)`), so one provider erroring never blocks the others. Results are merged, deduplicated by DOI/PMID/normalized-title, and ranked by the weighted quality score described above (study type, citation count, recency, keyword match), keeping only the top `Settings.literature_top_papers_limit` (default 20) for the Gemini prompt. Dosage is deliberately not encoded into any provider's query itself (free-text search doesn't reliably filter on numeric doses); the label's printed dose instead feeds the keyword-match ranking score and is given directly to Gemini as context for `dosage_appropriateness`. Only a TOTAL failure across every provider degrades to zero studies with an explicit "search failed" flag -- a single provider failing just means it contributes zero papers, without aborting the grade. *(logged as step 2: `literature_search.log_retrieval_summary`'s fixed-format per-provider counts, total unique papers, and ranked/selected count)*
3. `app/services/grading_service.IngredientGradingService._evaluate_with_gemini` -- one `gemini_client.generate_content` call (same dynamic model / zero-retry-except-429 / 60s-pause machinery scanning uses) with `response_schema=SifgConsensus`, instructed (`GRADING_SYSTEM_PROMPT`) to cite only the studies actually provided and to grade conservatively (`"Insufficient Evidence"`) rather than fabricate support when none were found. *(logged as steps 3-4: full prompt sent, then Gemini's raw output + stated reasoning)*
4. `ScanStorage.update_ingredient(ingredient_id, {...})` persists the result -- `grade_status="graded"` plus every `SifgConsensus` field (including the full raw JSON in `raw_consensus`) plus the run's `GradingStats` (as `grading_stats`) -- into that one ingredient's record. On a Gemini-side failure, `grade_status="failed"` plus a fresh `graded_at` (and `grading_stats`, if any could be computed before the failure) is persisted instead of silently discarded, and the endpoint returns HTTP 502. *(logged as step 5: final grade + persisted status, or FAILED)*

### API Surface

`app/main.py` + `app/api/routes.py` expose the entire API over HTTP:

* `POST /api/scan` -- accepts a label image upload (multipart, field name `file`), runs the single Gemini vision call synchronously, and returns the full `ScanResult` (HTTP 201) as soon as extraction finishes. On success, the same result is appended to `data/scanned_ingredients.json`. On failure (bad image, Gemini error, malformed response), returns HTTP 502 with a `detail` message and writes nothing to storage. Returns 400 for a non-image content type or an empty file, 413 if the upload exceeds 10 MB, and 503 if `GEMINI_API_KEY` isn't configured.
* `GET /api/ingredients` -- returns every scan saved so far, oldest first, read straight from `data/scanned_ingredients.json` (`ScanResult[]`). Backs the frontend's "Saved Ingredients" tab, including each ingredient's current `grade_status`.
* `POST /api/ingredients/{ingredient_id}/grade` -- grades exactly the one ingredient named in the path (see "Single-Ingredient Grading" above) and returns its updated `ScannedIngredient` (HTTP 200). Returns 404 if no ingredient with that id exists, 502 (with `grade_status` persisted as `"failed"`) if the Gemini evaluation call fails, and 503 if `GEMINI_API_KEY` isn't configured. A literature-search failure (even a total one, across every provider) never produces an error response here -- see "Single-Ingredient Grading" above.
* `GET /health` -- liveness check, no configuration required.

### Local Storage

`app/services/storage.py`'s `ScanStorage` appends each successful scan to a flat JSON array on disk at `backend/data/scanned_ingredients.json` (path resolved relative to the `backend/` package root, git-ignored -- it's local scan history, not source). Writes are protected by an `asyncio.Lock` (safe for concurrent requests within one process) and use an atomic temp-file-then-rename so a crash mid-write can't corrupt the file. This is **not** designed for multi-process/multi-worker concurrent writes, and there is no database, ORM, or migration layer -- intentional, for a local prototype at this scope. A corrupted or non-array JSON file is treated as empty (logged, not raised) rather than taking the API down.

**Mock Data Seeding:** `ScanStorage.seed_if_missing()` is called once at app startup (`app.main`'s `lifespan`). If `data/scanned_ingredients.json` doesn't exist yet, it's created and seeded with one realistic mock scan -- a "Daily Essentials Multivitamin" containing Vitamin C (1000mg), Zinc (15mg), and Ashwagandha (500mg) -- so the "Saved Ingredients" tab has something to show immediately on a fresh install, without depending on a live Gemini call ever having succeeded. The seed record uses the exact same `ScanResult` shape a real scan produces, so nothing about the API or frontend needs to know it's fake. It's a no-op (never overwrites) once the file exists, whether that file holds only the seed record or real scan history.

**`ingredient_id` Backfill:** `ScanStorage.backfill_ingredient_ids()` also runs once at startup, right after `seed_if_missing()`. It assigns a stable, persisted `ingredient_id` to any ingredient record that predates that field (old seed/scan data). Without this, `ingredient_id` would be regenerated fresh on every `GET /api/ingredients` response instead of matching what's actually on disk, and grading any pre-existing ingredient would 404 forever. A no-op once every ingredient already has one.

* `ScanStorage.get_ingredient(ingredient_id)` / `update_ingredient(ingredient_id, updates)` -- the read/write primitives grading uses to find and persist one ingredient's record, searching/updating across every saved scan without touching any other ingredient.

---

## History: Removed, Then Partially Reintroduced

**Removed (still gone):** The original design specified three further phases beyond a single-ingredient grade: an ingredient-combination synergy/interaction/proprietary-blend-penalty engine, and a final whole-product-grade synthesis step, plus an *always-on, automatic* grading pass over every ingredient in a scan (via a PubMed retrieval service + an LLM Paper Evaluator + a Consensus Engine producing a composite score/evidence grade/confidence score). All of that original code (`app/services/consensus_engine.py`, `app/services/pubmed_service.py`, `app/services/pipeline.py`'s job orchestration, `app/schemas/ingredient_grade.py`) was deleted outright, along with the NCBI Entrez configuration it depended on -- none of it was dormant/flagged-off, and none of it survived. Combination analysis and product-grade synthesis remain fully removed; there is no dormant code path for either.

**Reintroduced, narrower:** Single-ingredient, on-demand SIFG grading is back, rebuilt from scratch rather than resurrected -- `app/services/pubmed_client.py` (real NCBI E-utilities calls), `app/services/literature_search.py` (the multi-source aggregation/dedup/ranking layer added on top of PubMed and three other public APIs), and `app/services/grading_service.py` (the Gemini SIFG evaluation call) are new modules with no code in common with the deleted ones. The key difference from the original design: grading now runs for exactly one ingredient, only when a user explicitly requests it via `POST /api/ingredients/{ingredient_id}/grade` -- never automatically as part of a scan, never for a whole product at once, and with no combination or product-grade step downstream of it. Literature retrieval itself started single-source (PubMed only) and was later expanded to the current four-source aggregation with weighted ranking -- see `app/services/literature_search.py`'s module docstring for the full retrieval/dedup/ranking design.
