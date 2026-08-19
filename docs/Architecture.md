# Architecture

## Overview

Supplement label scanner: a React Native (Expo) client uploads a photo of
supplement packaging to a FastAPI backend, which extracts structured
ingredient/dosage data via the Google Gemini API and persists it locally.

## Backend Structure

```
backend/
├── .env                    # GEMINI_API_KEY, GEMINI_MODEL (not committed)
├── requirements.txt
├── data/
│   └── app.db               # SQLite database (gitignored), created by init_db() on startup
└── app/
    ├── main.py             # FastAPI app instance, CORS config, router mounting, init_db() on startup
    ├── db.py                # SQLModel engine (PRAGMA foreign_keys=ON per connection), init_db(), reset_database(), get_session() dependency
    ├── core/
    │   └── config.py       # Settings (GEMINI_API_KEY, GEMINI_MODEL) via pydantic-settings
    ├── api/
    │   └── routes.py       # `router` (POST /scan), `search_router` (/supplements/*), `products_router` (/products/{id}), `ingredients_router` (GET /ingredients/{id}, POST /ingredients/{id}/grade), `papers_router` (POST /papers/{id}/grade — Phase 4), `dev_router` (/dev/*)
    ├── schemas/
    │   ├── supplement.py   # Ingredient, SupplementAnalysis — Pydantic I/O models (Gemini + API response)
    │   ├── search.py       # FilterType, ResultType, SearchResultItem (now with nested ingredients), SearchResponse, SuggestResponse
    │   ├── dev.py           # MockDataResetResponse
    │   ├── research.py      # RubricEvaluationResponse (Phase 3), ResearchPaperResponse (now with status — Phase 6), PaperConclusionResponse (Phase 5), VerifiedResourceResponse (Phase 7; now with grade/score/reasoning_summary — Phase 8), IngredientDetailResponse (now with conclusions + verified_resources + summary_description — Phase 11), GradeIngredientResponse (Phase 2), GradePaperResponse (Phase 4)
    │   └── (supplement.py adds LinkedIngredientResponse, ProductDetailResponse)
    ├── models/
    │   ├── schemas.py      # ScanResponse (superseded by schemas/supplement.py; unused)
    │   ├── supplement.py   # Product, Ingredient (now with is_graded/grade_badge_text/papers/summary_description — Phase 11; now with verified_resources relationship — Phase 16), ProductIngredientLink — SQLModel ORM tables (M2M)
    │   └── research.py     # ResearchPaper — SQLModel ORM table (Phase 2; now with keywords + grade/grade_score/rubric_evaluation from Phase 3, status from Phase 6), FK'd to Ingredient. Also: serialize_keywords()/parse_keywords(), PAPER_STATUS_ACTIVE/PAPER_STATUS_DISCARDED_IRRELEVANT (Phase 6). PaperConclusion (Phase 5) — one row per synthesized cross-paper claim, FK'd to Ingredient (no ORM relationship — queried directly, see search.py). VerifiedResource (Phase 7; now with grade/score/reasoning_summary — Phase 8, extracted_data — Phase 17) — one row per official government/regulatory reference link, FK'd to Ingredient; now has an `ingredient` ORM relationship back to Ingredient.verified_resources for parity with ResearchPaper (Phase 16 — still queried directly by every actual read path, see conclusion_grader.py)
    └── services/
        ├── vision.py       # Gemini API calls for label parsing
        ├── storage.py      # save_scan() (M2M find-or-create), delete_all_data(), delete_mock_data() (legacy, unused by the route)
        ├── search.py       # suggest() / search() queries, get_linked_ingredients()/get_product_detail() (explicit joins), get_ingredient_papers() (excludes DISCARDED_IRRELEVANT papers — Phase 6)/get_ingredient_detail()/to_research_paper_response() (shared ORM->response mapper), get_ingredient_conclusions() (Phase 5), get_ingredient_resources() (Phase 7, now includes grade/score/reasoning_summary — Phase 8)
        ├── research_keywords.py  # Gemini: generate_ingredient_keywords() (Phase 2)
        ├── paper_search.py       # Europe PMC/PubMed/Semantic Scholar/OpenAlex (async, concurrent): search_papers_for_ingredient() (Phase 2; search-only as of Phase 5 — grading moved to paper_analysis_pipeline.py)
        ├── paper_grader.py       # Gemini: grade_paper() — evaluates one paper against docs/paper_grading_rubric.json AND relevance-checks it against its target ingredient in the same call (Phase 3 grading, Phase 6 relevance); grade_single_paper() — on-demand/idempotent DB-aware wrapper for one already-stored paper, also sets ResearchPaper.status (Phase 4; also the per-paper grading step of the Phase 5 pipeline)
        ├── conclusion_grader.py  # Gemini: process_paper_conclusions() — extracts one graded paper's findings and merges/creates PaperConclusion rows against docs/conclusion_grading_rubric.json (Phase 5); defensively gates on paper.status != DISCARDED_IRRELEVANT (Phase 6). Also: synthesize_ingredient_summary() — one ingredient-level call combining every graded paper AND every VerifiedResource into a single summary_description/main_consensus/recommended_uses (Phase 11)
        ├── paper_analysis_pipeline.py  # analyze_ingredient_papers() — sequential per-paper grade + relevance-check + conclusion-synthesis loop with per-paper error isolation (Phase 5); discards/skips conclusion synthesis for DISCARDED_IRRELEVANT papers (Phase 6); after the loop, calls synthesize_ingredient_summary() once and persists summary_description onto Ingredient (Phase 11)
        ├── resource_fetcher.py   # Plain HTTP: fetch_verified_resources_for_ingredient() queries docs/verified_resource_apis.json's official gov/regulatory APIs by ingredient name, strictly domain-filters results, persists VerifiedResource rows (Phase 7); also grades each new one via resource_grader.py, sequentially (Phase 8)
        ├── resource_grader.py    # Gemini: grade_resource() — evaluates one already-fetched, already-domain-verified resource against docs/resource_grading_rubric.json (Phase 8); pure, no DB — called directly by resource_fetcher.py, no separate on-demand endpoint/pipeline module (unlike papers)
        ├── resource_extractor.py # Gemini: extract_claims_from_resource() — Two-Stage Extraction Pipeline Stage 1 (Phase 17); distills one VerifiedResource's title/publisher/summary into structured {official_stance, recommended_dose, upper_limit_warning, key_takeaways}; pure, no DB — called per-resource from paper_analysis_pipeline.py, persisted onto VerifiedResource.extracted_data; Gemini call paced/retried via gemini_rate_limit.py (Phase 18)
        ├── gemini_rate_limit.py  # Shared: throttle_gemini_call() (process-wide ~4.5s inter-call pacing) + call_gemini_with_retry() (exponential backoff on 429/RESOURCE_EXHAUSTED) — used by paper_grader.py and resource_extractor.py (Phase 18)
        └── grading.py             # Orchestrates keyword-gen + paper-search + verified-resource lookup/grading + the Phase 5/6 grade/relevance/conclusion-synthesis pipeline + debug grade assignment: grade_ingredient() (Phase 2, updated Phase 5/6/7/8)
```

Note: `app/schemas/supplement.py` and `app/models/supplement.py` both define an
`Ingredient` class — one is a plain Pydantic model (Gemini/API I/O shape),
the other a SQLModel table (DB row shape). They're intentionally separate;
`app/services/storage.py` imports the table one aliased as `IngredientRow`
to keep them straight.

## Configuration

`app/core/config.py` defines a `Settings` (pydantic-settings) model that
reads `GEMINI_API_KEY` and `GEMINI_MODEL` from `backend/.env`, resolved by
absolute path so it loads correctly regardless of the working directory the
server is started from. `get_settings()` is `lru_cache`d — the file is only
parsed once per process. Missing `GEMINI_API_KEY` raises a validation error
at first use.

## Data Schemas (`app/schemas/supplement.py`)

- **`Ingredient`**: `name` (str), `amount` (str), `unit` (str), `daily_value`
  (optional str). `amount` is kept as a string because labels use varied
  formats (ranges, decimals).
- **`SupplementAnalysis`**: `product_name` (optional str), `serving_size`
  (optional str), `ingredients` (list of `Ingredient`).

These models are used both as the response model for `POST /api/v1/scan`
and as the `response_schema` handed to Gemini for structured output.

- **`LinkedIngredientResponse`**: `id` (int), `name` (str), `amount`
  (optional str), `unit` (optional str), `daily_value_percentage`
  (optional str) — from a `ProductIngredientLink` row — plus
  `recommended_daily_dosage` (default `"x"`) and `scientific_data`
  (default `"n/a"`) from the canonical `Ingredient` row. Used to build the
  nested `ingredients` list on `SearchResultItem` (product results) and
  `ProductDetailResponse`.
- **`ProductDetailResponse`**: `id`, `name`, `brand` (default
  `"Unknown"`), `serving_size` (default `"Not available"` — `Product` has
  no such column yet, see Database gaps), `created_at` (optional ISO
  string), `ingredients` (`List[LinkedIngredientResponse]`). Response
  model for `GET /api/v1/products/{id}`.

## Database (`app/db.py`, `app/models/supplement.py`)

SQLite via SQLModel. As of the Many-to-Many refactor, `Product` and
`Ingredient` are related through a `ProductIngredientLink` junction table
rather than `Ingredient` holding a direct `product_id` FK:

- **`Product`** (table `products`): `id` (PK), `name` (str), `brand`
  (str), `is_mock` (bool, default `True`), `created_at` (UTC datetime,
  defaulted). `ingredients` relationship — despite the name, this returns
  `ProductIngredientLink` rows (each carrying *this product's* dosage for
  one ingredient), not bare `Ingredient` rows; use `link.ingredient` to
  reach the canonical ingredient. Deleting a `Product` cascades to its
  link rows (but not to the `Ingredient` rows those links point to).
- **`Ingredient`** (table `ingredients`): `id` (PK), `name` (str,
  **unique**), `recommended_daily_dosage` (str, default `"x"`),
  `product_count` (int, default `0`), `scientific_data` (str, default
  `"n/a"`), `is_mock` (bool, default `True`). **Strict rule:** this table
  holds only canonical/shared compound data — no product-specific
  dosage, percentage, or serving size. `recommended_daily_dosage` and
  `product_count` are explicitly *placeholders*: nothing in the app
  currently computes or updates them (see gaps below).
- **`ProductIngredientLink`** (table `product_ingredient_links`): `id`
  (PK), `product_id` (FK -> `products.id`), `ingredient_id` (FK ->
  `ingredients.id`), `amount` (str — kept as a string rather than float,
  since Gemini's extracted amounts can be ranges/decimals like
  "250-300"), `unit` (str), `daily_value_percentage` (optional str).
  This is where a product's specific dosage of an ingredient lives.

`app/db.py` points the engine at `backend/data/app.db`, resolved to an
absolute path (same reasoning as `.env` loading in `core/config.py`).
`init_db()` (non-destructive: creates `backend/data/` + any missing
tables) runs on every startup via `app/main.py`'s `lifespan` handler.
`reset_database()` is a separate, destructive, one-time migration
utility — it deletes `app.db` outright and recreates it from the current
schema, since `SQLModel.metadata.drop_all()` only drops tables it
currently recognizes by name, and this refactor renamed
`product`/`ingredient` to `products`/`ingredients`, which `drop_all()`
would silently leave behind. Run manually if migrating an existing DB:

```bash
cd backend && python -c "from app.db import reset_database; reset_database()"
```

**Additive column migrations:** `SQLModel.metadata.create_all()` (what
`init_db()` actually calls) only creates tables that don't exist *by
name* yet — it never adds columns to a table that's already there. When
the Phase 2 grading feature added `is_graded`/`grade_badge_text` to the
`Ingredient` model, any database created before that change ended up
schema-stale: the `ingredients` table existed (so `create_all()` left it
alone) but was missing both columns, and every query touching
`Ingredient` — e.g. `GET /api/v1/supplements/search` — failed with
`sqlite3.OperationalError: no such column: ingredients.is_graded`.
`init_db()` now also calls
`_migrate_ingredient_grading_columns()` right after `create_all()`: it
checks `PRAGMA table_info(ingredients)` and runs an `ALTER TABLE
ingredients ADD COLUMN ...` for each of `is_graded`/`grade_badge_text`
only if that column is actually missing. This runs on every startup,
is a no-op on an already-up-to-date database (including a freshly
created one, where `create_all()` included both columns from the
start), and — unlike `reset_database()` — doesn't touch any existing
row, so scanned Product/Ingredient/link data survives. This is a
lightweight, hand-rolled pattern rather than a real migration tool
(no Alembic in this project); if the schema keeps evolving, a proper
migration framework would be worth introducing instead of adding more
one-off `_migrate_*` functions here.

Same pattern again for `ResearchPaper.keywords` (added after
`research_papers` already existed in deployed databases — see "Matched
keyword tracking" in the Phase 2 section below), later `grade`/
`grade_score`/`rubric_evaluation` (Phase 3 automated paper grading — see
"Automated paper grading" below), and now `status` (Phase 6 ingredient
relevance verification — see "Ingredient Relevance Verification (Phase 6)"
below): `init_db()` also calls `_migrate_research_paper_columns()` right
after `_migrate_ingredient_grading_columns()`, checking `PRAGMA
table_info(research_papers)` and adding whichever of `keywords VARCHAR`,
`grade VARCHAR`, `grade_score INTEGER`, `rubric_evaluation JSON`,
`status VARCHAR DEFAULT 'ACTIVE' NOT NULL` are missing. No `DEFAULT`
needed for the first four (nullable columns) — but `status` mirrors
`is_graded` in needing one, since `ResearchPaper.status` is a
non-Optional `str` field (`Field(default=PAPER_STATUS_ACTIVE)`), so
`create_all()` generates it `NOT NULL` on a fresh database and the
`ALTER TABLE` here has to match that on a migrated one.

One more additive migration, `_migrate_verified_resource_columns()`
(also called from `init_db()`): `VerifiedResource.grade`/`score`/
`reasoning_summary` (Phase 8 automated resource grading — see "Automated
Resource Grading (Phase 8)" below) were added after `verified_resources`
already existed in deployed (Phase 7) databases — unlike
`verified_resources` itself, which needed no migration when it was
first introduced (a brand-new table, not new columns on an existing
one — see that model's docstring). Checks `PRAGMA
table_info(verified_resources)` and adds whichever of `grade VARCHAR`,
`score INTEGER`, `reasoning_summary TEXT` are missing; all three are
nullable, so no `DEFAULT` is needed.

`get_session()` is a FastAPI dependency yielding one `Session` per
request.

### `app/services/storage.py`

- **`save_scan(session, analysis)`**: creates the `Product` row and
  `flush()`es (not `commit()`s — see the function's docstring) to obtain
  `product.id` within the still-open transaction. For each parsed
  ingredient it calls `_find_or_create_ingredient`, then explicitly
  builds a `ProductIngredientLink` row carrying that scan's amount/unit/
  daily value. Everything — the Product, any newly-created Ingredients,
  and all the links — commits together as one transaction at the end, or
  rolls back together on error.
- **`_find_or_create_ingredient(session, raw_name)`**: normalizes the
  name (`_clean_ingredient_name` — collapses stray whitespace; the real
  cleaning happens in the Gemini prompt, see below) and looks it up with
  an *exact*, case-insensitive match. LIKE/ILIKE wildcard characters
  (`%`, `_`) in the name are escaped first (`_escape_like_pattern`) so a
  name that happens to still contain a literal `%` can't turn the lookup
  into an unintended wildcard search. On a match, increments
  `product_count` on the existing row; otherwise creates a new
  `Ingredient` with `is_mock=False`, `product_count=1`.
- **`delete_mock_data(session)`**: deletes every `is_mock=True` `Product`
  (via ORM `session.delete`, so the relationship cascade removes its
  link rows too — a bulk SQL `delete()` would bypass that cascade), then
  deletes any `is_mock=True` `Ingredient` that has no remaining links
  afterwards (so an ingredient still referenced by a surviving non-mock
  product isn't deleted out from under it). **No longer called by any
  route** — kept for reference, superseded by `delete_all_data` below.
- **`delete_all_data(session)`**: unconditionally wipes every row from
  `ProductIngredientLink`, `Product`, and `Ingredient` — this is what
  `DELETE /api/v1/dev/mock-data` actually calls now. `delete_mock_data`
  above was scoped to `is_mock=True` rows, but real `Ingredient` rows
  created from an actual scan are `is_mock=False` (see
  `_find_or_create_ingredient`), so that path could never clear them —
  this was the root cause of the "Reset DB" button leaving dirty
  ingredient data behind. Deletes via bulk `delete()` statements in
  explicit dependency order (links, then products, then ingredients),
  required now that `app/db.py` enables `PRAGMA foreign_keys=ON` for
  every SQLite connection — deleting a parent row before its children
  would raise an `IntegrityError` instead of silently orphaning them.
  After committing, re-queries each table's row count and raises
  `RuntimeError` if any is still nonzero, so a partial wipe surfaces as
  an error instead of a false "success" response.

**Root cause of the "Found in 0 products" / "links not created" bug:**
two compounding issues, both fixed here. First, `Ingredient.product_count`
was previously never incremented at all (intentionally, per the prior
task's framing of it as an unmanaged placeholder) — every ingredient
showed `product_count=0` regardless of how many products actually linked
to it, which is what "Found in 0 products" in the UI was actually
reporting. Second, Gemini was returning noisy, multi-language raw label
text as the ingredient `name` (percentages, elemental breakdowns,
translations all concatenated in) — since deduplication matches on exact
name, two scans of visually "the same" ingredient with slightly different
raw text never matched each other, so counts and links never
accumulated the way a shared canonical ingredient should. The
`ProductIngredientLink` rows themselves were, as far as we can tell,
already being created correctly — the M2M write path wasn't silently
failing, it just wasn't doing what "linked ingredient" implies once
`product_count` is actually surfaced in the UI and names don't
deduplicate.

**Known gaps:**
- `SupplementAnalysis.serving_size` (from Gemini) has no column on
  `Product` and is silently dropped by `storage.save_scan`.
- `Product.brand` is always saved as `"Unknown"` — the Gemini extraction
  schema doesn't produce a brand field, and `Product.brand` is now a
  required `str` (not optional), so a fallback is used.
- `Product.is_mock` is left at its model default (`True`) even for real
  scans — deliberate: it keeps "Reset DB" useful for clearing scan/
  product history during development, while the canonical `Ingredient`
  dictionary (now `is_mock=False` for real scans, see above) survives
  resets and keeps accumulating. `Ingredient.recommended_daily_dosage`
  and `scientific_data` remain unmanaged placeholders — nothing populates
  them from a scan.
- The API response (`SupplementAnalysis`) doesn't currently include the
  persisted `Product.id`, so a client can't yet correlate a scan response
  with its database row.
- SQLite's default journal mode can raise "database is locked" under
  concurrent writes; fine for a single-process dev prototype, but worth
  revisiting (e.g. WAL mode) before this sees real concurrent traffic.
- `DELETE /api/v1/dev/mock-data` (see below) is unauthenticated and
  destructive — fine for local development, a real risk if this backend
  is ever exposed beyond localhost/LAN. It now wipes the *entire*
  database (all rows, not just `is_mock=True` ones), so it also clears
  the canonical `Ingredient` dictionary that used to survive resets.
- Route-level: `POST /api/v1/scan` already logs persistence failures via
  `logger.exception` (full traceback) but doesn't surface them to the
  client — if scans still silently fail to persist after this fix, check
  the server log for a traceback rather than assuming the write path
  itself is untouched.

## API Routes

### `GET /health`
Liveness check. Returns `{"status": "ok"}`.

### `POST /api/v1/scan`
Accepts a single image upload (`multipart/form-data`, field name `file`),
sends it to Gemini for parsing, persists the result, and returns the
structured analysis.

- **Request:** `UploadFile`, MIME type must be one of `image/jpeg`,
  `image/png`, `image/jpg`, `image/webp`.
- **Response (200):** `SupplementAnalysis` JSON (see schema above).
- **Errors:**
  - `400` — unsupported MIME type, empty file, or unreadable upload.
  - `502` — Gemini request failed, or its response didn't match the
    expected schema (`VisionServiceError` in `app/services/vision.py`).
- **Note:** if persisting to SQLite fails (e.g. disk issue), the request
  still succeeds and returns the analysis — the failure is only logged
  server-side (`storage.save_scan` rolls back the session and re-raises;
  the route catches and logs it), so a storage hiccup doesn't lose an
  otherwise successful Gemini result.

### `GET /api/v1/supplements/suggest`
Live autocomplete: returns up to `limit` (default 5) matching product/
ingredient names for a partial `query`. Returns an empty list (not an
error) if `query` is shorter than 3 characters — see
`app/services/search.py::MIN_SUGGEST_QUERY_LENGTH`. Names are deduplicated
case-insensitively across both tables (products checked first).

- **Params:** `query` (required, str), `limit` (int, default 5, max 25).
- **Response (200):** `SuggestResponse` — `{ query, suggestions: string[] }`.

### `GET /api/v1/supplements/search`
Search or browse `Product`/`Ingredient` rows. If `query` is omitted, this
browses all rows of the selected `filter_type` instead of filtering by
name — used by the Library screen's "Products"/"Ingredients" explore
cards. When `filter_type=all`, products are fetched first (up to `limit`),
then ingredients fill any remaining slots (a simple, deterministic split,
not an even one).

- **Params:** `query` (optional str), `filter_type` (optional enum:
  `all` | `products` | `ingredients`, default `all`), `limit` (int,
  default 20, max 20).
- **Response (200):** `SearchResponse` — `{ query, filter_type, count,
  results: SearchResultItem[] }`. Each `SearchResultItem` has `id`, `type`
  (`product` | `ingredient`), `name`, plus `brand` + `ingredients`
  (products) or `recommended_daily_dosage`/`scientific_data`/
  `product_count` (ingredients) — fields not applicable to the item's
  `type` are `null`/`[]`. As of the M2M refactor, ingredient results no
  longer carry a product-specific dosage or single parent product name
  (an ingredient can now belong to zero, many, or many products) — they
  surface the canonical `Ingredient` row's metadata instead.
- **`ingredients` (product results only):** a `LinkedIngredientResponse[]`
  built by `app/services/search.py::get_linked_ingredients`, which runs
  an explicit `ProductIngredientLink` + `Ingredient` join per product
  rather than reading `Product.ingredients` — SQLModel's lazy-loaded
  relationship serializes as `[]` inside Pydantic response models
  regardless of what's actually linked in the DB, which was the root
  cause of `ProductCard` always showing "No ingredient data available
  for this product yet" on the frontend even for correctly-persisted
  scans.

### `GET /api/v1/products/{id}`
Returns a single `Product` with its full linked-ingredient list, via the
same explicit-join approach as `/supplements/search` above
(`app/services/search.py::get_product_detail`).

- **Params:** `id` (path, int).
- **Response (200):** `ProductDetailResponse` — `{ id, name, brand,
  serving_size, created_at, ingredients: LinkedIngredientResponse[] }`.
- **Errors:** `404` if no `Product` with that id exists.
- **Note:** not currently called by the frontend — `ResultsScreen` gets
  everything it needs from `/supplements/search`'s nested `ingredients`
  field — but available for a future dedicated product-detail screen.

### `GET /api/v1/ingredients/{id}`
Returns a single canonical `Ingredient` plus every currently-*active*
`ResearchPaper` stored for it, (Phase 5) every synthesized
`PaperConclusion`, and (Phase 7) every stored `VerifiedResource`
(`app/services/search.py::get_ingredient_detail`). Added to back
standalone `IngredientCard`'s "List of Studies" panel
(`src/components/StudiesList.tsx`) — this is a pure read, it never
triggers a new paper search (or verified-resource lookup) itself;
`papers`/`conclusions`/`verified_resources` are just whatever's already
been persisted by a prior `POST .../grade` call (`[]` for all three if
the ingredient hasn't been graded yet).

- **Params:** `id` (path, int).
- **Response (200):** `IngredientDetailResponse` — `{ id, name,
  recommended_daily_dosage, scientific_data, product_count, is_graded,
  grade_badge_text, papers: ResearchPaperResponse[], conclusions:
  PaperConclusionResponse[], verified_resources: VerifiedResourceResponse[]
  }`. Each `ResearchPaperResponse` is `{ id,
  title, abstract, authors, publication_date, source_url, source_domain,
  ingredient_id, keywords: string[], grade, grade_score,
  rubric_evaluation, status }` — a direct mirror of the `ResearchPaper`
  table columns, except `keywords` (parsed from the stored
  comma-separated string via `parse_keywords()`) and `rubric_evaluation`
  (the stored JSON dict, validated straight into
  `RubricEvaluationResponse`). `grade`/`grade_score`/`rubric_evaluation`
  are `null` for a paper that hasn't been graded yet (Phase 3 — see
  "Automated paper grading" below). `status` (Phase 6 — see "Ingredient
  Relevance Verification" below) is always `"ACTIVE"` here — a paper
  Gemini determines is off-topic for this ingredient is flipped to
  `"DISCARDED_IRRELEVANT"` and excluded from this list entirely
  (`app/services/search.py::get_ingredient_papers`), so it never counts
  toward `grade_badge_text`/the "Total studies" or "Average grade" the
  frontend derives from `papers`. Each `PaperConclusionResponse` (Phase 5
  — see "Cross-Paper Conclusion Synthesis" below) is `{ id,
  ingredient_id, claim_summary, detailed_conclusion, dosage_mentioned,
  rubric_evaluation, confidence_score, confidence_grade,
  cross_paper_consensus, supporting_paper_ids: number[],
  contradicting_paper_ids: number[] }`, ordered
  highest-`confidence_score`-first. Each `VerifiedResourceResponse`
  (Phase 7 — see "Verified Online Resources" below) is `{ id,
  ingredient_id, title, publisher, url, domain, summary, grade, score,
  reasoning_summary }`, most recently added first — every row already
  cleared the backend's strict domain allow-list at fetch time, so
  (unlike `papers`) there's no further status/relevance field to check
  before displaying one. `grade`/`score`/`reasoning_summary` (Phase 8 —
  see "Automated Resource Grading" below) are `null` until
  `app/services/resource_fetcher.py` successfully grades that resource
  (best-effort at fetch time, never retried) — the frontend renders no
  grade badge for a `null` grade, same convention as `papers`.
- **Errors:** `404` if no `Ingredient` with that id exists.

### `POST /api/v1/ingredients/{id}/grade`
**[Phase 2, debug]** Runs the research-paper search pipeline for a single
canonical `Ingredient`, grades and relevance-checks every stored active
paper (Phase 6 — discarding any Gemini determines aren't actually about
this ingredient), synthesizes/merges cross-paper conclusions from the
rest (Phase 5), queries the official government/regulatory APIs
configured in `docs/verified_resource_apis.json` for new verified
resource links and grades each one against
`docs/resource_grading_rubric.json` (Phase 7/8), and assigns a debug
grade — see "Research Paper Search & Ingredient Grading (Phase 2)",
"Cross-Paper Conclusion Synthesis (Phase 5)", "Ingredient Relevance
Verification (Phase 6)", "Verified Online Resources (Phase 7)", and
"Automated Resource Grading (Phase 8)" below for the full pipeline.

- **Params:** `ingredient_id` (path, int).
- **Response (200):** `GradeIngredientResponse` — `{ status,
  ingredient_id, is_graded, grade_badge_text, papers_found, papers:
  ResearchPaperResponse[] }`, e.g. `{"status": "success", "ingredient_id":
  1, "is_graded": true, "grade_badge_text": "14 / 14 / 14",
  "papers_found": 14, "papers": [...]}`. `papers` is the same shape as
  `GET /api/v1/ingredients/{id}` above (added so the frontend's
  `StudiesList` can refresh immediately off this response, without a
  follow-up GET) — `papers_found` is kept as a separate plain int for
  backwards compatibility with the existing grade-badge text logic.
- **Errors:** `404` if no `Ingredient` with that id exists; `502` if
  Gemini keyword generation fails outright or the final DB commit fails
  (`app/services/grading.py::GradingError`) — individual paper-search
  source failures (rate limits, network errors) are handled internally
  and do NOT surface as an error here, they just mean fewer papers found.
- **Note:** can take several seconds — it's a Gemini call followed by up
  to 15 sequential external HTTP calls (5 keywords × 3 sources). The
  frontend shows a loading spinner on the grade button for the duration.

### `POST /api/v1/papers/{paper_id}/grade`
**[Phase 4, on-demand]** Grades exactly one already-stored `ResearchPaper`
row — the counterpart to the automatic per-paper grading
`POST /api/v1/ingredients/{id}/grade` already does for every *newly*-found
paper (above); this endpoint exists for papers that were persisted before
grading existed, or whose automatic grading attempt failed (see "Per-paper
resilience" in the Phase 3 section below) and were left permanently
ungraded. Backs the frontend's gray "(-)" ungraded badge in `StudiesList`
— tapping it calls this instead of waiting for (or forcing) a full
ingredient re-grade.

- **Params:** `paper_id` (path, int).
- **Response (200):** `GradePaperResponse` — `{ status, paper:
  ResearchPaperResponse }`. If the paper was already graded, `paper` is
  returned **unchanged** — no Gemini call is made
  (`app/services/paper_grader.py::grade_single_paper` treats this as a
  safe, idempotent no-op, not an error), so re-tapping an already-graded
  badge or a duplicate request from a slow double-tap can't double-charge
  a Gemini call or corrupt the stored grade. Unlike every other
  paper-bearing endpoint, `paper.status` here **can** come back
  `"DISCARDED_IRRELEVANT"` (Phase 6) — this is the one place a discarded
  paper is ever returned to the frontend at all, since it's the freshly-
  graded row itself, not a list query that already filters discards out.
  The frontend checks this and removes the paper from local state instead
  of leaving it displayed — see `IngredientCard.tsx::handlePaperGraded`.
- **Errors:** `404` if no `ResearchPaper` with that id exists; `502` if
  grading fails (`PaperGradingError` — Gemini request failure, unparsable
  response, or a DB commit failure) or the rubric file can't be loaded.
- **Known limitation:** unlike ingestion-time grading, this call has no
  `journal` name to pass to Gemini — `PaperRecord.journal` (see
  `app/services/paper_search.py`) exists only transiently during the
  original paper-search fan-out and isn't persisted on `ResearchPaper`
  — so "Journal / Publisher Rigor" scoring here has slightly less signal
  to work with than that paper's original, ingestion-time grade would
  have had (still scored conservatively, same as any other missing
  metadata — see the Phase 3 section's "Prompting" bullet).

### `DELETE /api/v1/dev/mock-data`
Dev-only: unconditionally wipes every `Product`/`Ingredient`/
`ProductIngredientLink` row in the database (see `storage.delete_all_data`
above for the exact deletion logic — bulk deletes in dependency order,
with a post-commit verification query). **Unauthenticated.**

- **Response (200):** `MockDataResetResponse` — `{"status": "success", "message": "Database completely wiped"}`.
- **Errors:** `500` if the post-commit verification finds any rows still
  remaining (wraps the `RuntimeError` from `storage.delete_all_data`).

## Pipeline

1. **Image Upload** — Expo client sends image to `POST /api/v1/scan`.
2. **Vision Processing** — `app/services/vision.py::analyze_supplement_label`
   sends the image bytes to Gemini (`google-genai` SDK) with a strict system
   prompt and `response_schema=SupplementAnalysis`, so the model returns
   JSON that maps directly onto the Pydantic model (`response.parsed`, with
   a manual `model_validate_json` fallback). The prompt's `name` field rules
   are deliberately strict and include a worked few-shot example: translate
   non-English labels, strip percentages/elemental breakdowns/ratios/
   multi-language repeats out of the ingredient name entirely (they're
   dropped, not moved elsewhere), and never let a "% elemental" composition
   figure leak into `daily_value` (that's specifically the label's %DV
   column). This matters beyond output tidiness — `storage.py`'s ingredient
   deduplication matches on exact name, so noisy/inconsistent names
   previously meant the same real-world compound never matched itself
   across scans.
3. **Data Persistence** — `app/services/storage.py::save_scan` builds a
   `Product` row, finds-or-creates a canonical `Ingredient` row per parsed
   ingredient (case-insensitive name match), and links them via
   `ProductIngredientLink` rows carrying that scan's dosage — all
   committed via the request's SQLModel `Session` (see Database section
   below).

Both `analyze_supplement_label` (blocking network call) and `save_scan`
(blocking DB calls) are synchronous functions run via
`starlette.concurrency.run_in_threadpool` from the async route handler, so
they don't block the event loop. `get_session()` itself is also a sync
generator dependency, which FastAPI runs in its worker threadpool
automatically.

## CORS

`app/main.py` allows local Expo/React Native dev origins (`localhost` on
ports 8081, 19000–19002, 19006) plus a regex for LAN addresses
(`192.168.x.x`, `10.x.x.x`, `172.16-31.x.x`) so the app is reachable from
physical devices and Expo web previews during development.

## Research Paper Search & Ingredient Grading (Phase 2)

First pass at automated scientific-literature backing for ingredients,
triggered by the frontend's "Assign Grade" button on standalone
`IngredientCard`s (product-level grading is still local/placeholder-only —
see the frontend section below). Three new backend pieces, orchestrated
by a fourth:

**1. Data model — `app/models/research.py`.** A new `ResearchPaper`
SQLModel table (`research_papers`), one row per paper found for one
`Ingredient`: `id`, `ingredient_id` (FK -> `ingredients.id`, indexed),
`title`, `abstract`, `authors` (comma-separated), `publication_date` (kept
as a free-text `str` — the three source APIs return wildly inconsistent
date formats/granularity, not worth normalizing for a debug-stage
feature), `source_url`, `source_domain`, `keywords` (comma-separated,
added after this table already existed — see "Matched keyword tracking"
below), `created_at`. `serialize_keywords(keywords: List[str]) ->
Optional[str]` and `parse_keywords(value: Optional[str]) -> List[str]`
(also in this module) are the shared round-trip helpers for that column
— `paper_search.py` writes through `serialize_keywords`, `search.py`
reads through `parse_keywords` when building the API response; both
sides need to agree on the exact comma/whitespace handling, hence
sharing one pair of functions rather than each re-implementing it.
`Ingredient` (`app/models/supplement.py`) gained three new fields: `is_graded: bool`
(default `False`), `grade_badge_text: Optional[str]` (default `None`),
and a `papers: List["ResearchPaper"]` relationship (back-populated from
`ResearchPaper.ingredient`). The `"ResearchPaper"` reference on
`Ingredient` is a quoted forward-ref, not a real import — `research.py`
already imports `supplement.py` (for its own relationship), so a real
import the other way would be circular; `app/db.py` now imports both
`app.models.supplement` and `app.models.research` at module load
specifically so the forward-ref resolves and both tables register for
`create_all()`.

**2. Gemini keyword generation — `app/services/research_keywords.py`.**
`generate_ingredient_keywords(ingredient_name)` sends a Gemini prompt
(structured `response_schema` output, same `.parsed`-with-raw-text-
fallback pattern as `vision.py`) asking for 3–5 targeted search queries
covering bioavailability/absorption, clinical efficacy, safety, and
mechanism-of-action angles — e.g. `"Magnesium Bisglycinate"` ->
`["Magnesium Bisglycinate bioavailability", "Magnesium Bisglycinate
clinical trial", "magnesium absorption mechanism", ...]`. Results are
cleaned (trimmed, deduplicated case-insensitively) and capped at 5
regardless of what Gemini returns. If Gemini's response parses but is
empty after cleaning, this falls back to a single query built from the
raw ingredient name rather than failing outright; if the Gemini request
itself fails or the response can't be parsed at all, it raises
`KeywordGenerationError`.

**3. Paper search — `app/services/paper_search.py`.** Reads
`docs/paperApis.json` (repo root, sibling of `backend/` — resolved by
absolute path, same reasoning as `.env`/`app.db` path resolution
elsewhere), which **replaced** the earlier `docs/paperWebsites.json` (a
~50-site curated reference list where only a handful of entries had a
real API — used purely as an allow-list). `paperApis.json` is a genuine
config file: each entry has an `id` (mapped in code to that source's
parser), `domain`, `endpoint`, `query_param`, and any static
`extra_params` the request needs beyond the keyword itself, plus an
`enabled` flag — set to `false` (or remove the entry) to turn off
querying that source without touching code. Four **keyless** REST APIs
are configured/queried:
- **Europe PMC** (`europepmc.org`) — one JSON call;
  `resultType=core` (an `extra_params` entry beyond the base spec) is
  required for `abstractText` to actually be included.
- **PubMed** (`pubmed.ncbi.nlm.nih.gov`) — the configured `esearch`
  endpoint only returns matching PMIDs as JSON; `_query_pubmed` follows
  up with a second, internal `efetch` call (XML, not itself in the
  config) per batch of PMIDs to get title/abstract/authors/date.
- **Semantic Scholar** (`semanticscholar.org`) — Graph API, public/
  unauthenticated tier (shared, fairly aggressive rate limit).
- **OpenAlex** (`openalex.org`) — Works API; its abstracts come back as
  an `abstract_inverted_index` (word -> position list, for copyright
  reasons) rather than plain text, so `_reconstruct_openalex_abstract`
  rebuilds the plain-text abstract from it before storing.

Every (keyword, source) pair is queried **concurrently**: `httpx.AsyncClient`
+ `asyncio.gather` fan out all the requests for one grading pass at once
(each individually wrapped by `_safe_query_async`, so one slow/failing
source doesn't hold up the others), each with its own 5-second timeout
(`_HTTP_TIMEOUT_SECONDS`) — with up to 5 keywords × 4 sources (20 calls),
running these sequentially could take minutes in the worst case;
concurrently, the whole batch takes roughly as long as the single
slowest call. `search_papers_for_ingredient(session, ingredient_id,
keywords)` itself stays a **synchronous** function — it internally calls
`asyncio.run(_search_all_records_async(...))` — so its callers
(`grading.py`, and ultimately the route via `run_in_threadpool`) didn't
need to change. `asyncio.run()` is safe here specifically because this
function only ever executes inside a `run_in_threadpool` worker thread,
never on FastAPI's own event loop thread — each thread gets its own
event loop.

Once all records are collected, they're deduplicated against both this
batch and whatever's already stored for that ingredient (by
`source_url`, falling back to a normalized `title` match), and persisted
via `session.add()` + `session.flush()` — deliberately **not**
`session.commit()`; the caller (`grading.py`) commits once together with
the ingredient's grade update, so a request either fully succeeds or
fully rolls back. `_safe_query_async` catches timeouts, rate limits (HTTP
429), other HTTP errors, network errors, and malformed responses per
source/keyword — logged and treated as "0 results from this source for
this keyword" rather than failing the whole request.

**Matched keyword tracking.** Every `PaperRecord` (the pre-persistence
shape each source parser returns) now carries the single `keyword` it
was queried with — each of `_query_europe_pmc`/`_query_pubmed`/
`_query_semantic_scholar`/`_query_openalex` sets it to the same
`keyword` argument used to build that request. Since one paper commonly
turns up under several (keyword, source) combinations in a single grade
request, `search_papers_for_ingredient` groups this batch's new records
by their dedup key (`source_url`/normalized `title`, same as before)
*before* creating `ResearchPaper` rows, accumulating every distinct
keyword seen for that key into one deduplicated list —
`serialize_keywords(...)` — so a paper found 4 times across different
keywords/sources ends up as one row with 4 keywords, not 4 rows or a
row that only remembers the first keyword. If a record instead matches
a paper *already stored* from an earlier grade request (a re-grade run,
typically with a different set of Gemini-generated keywords),
`_merge_keyword_onto` appends the newly-seen keyword onto that existing
row's `keywords` (deduplicated) instead of dropping it — mutating an
already-persistent `ResearchPaper` the session already tracks is enough
for SQLAlchemy to pick it up as dirty and include it in the caller's
eventual commit, no separate `session.add()` needed. Rendered as
"Matched Keywords" pill tags in the frontend's paper info modal (see
`StudiesList.tsx` below).

**Automated paper grading (Phase 3) — `app/services/paper_grader.py`.**
**As of Phase 5, grading no longer happens inline during paper search** —
`search_papers_for_ingredient` used to call a private `_apply_grade`
helper on each newly-built `ResearchPaper` before flushing it; that
helper was removed and its responsibility folded into
`app/services/paper_analysis_pipeline.py::analyze_ingredient_papers`
(see "Cross-Paper Conclusion Synthesis (Phase 5)" below), which now
grades every *stored* paper for an ingredient (new ones this run, plus
any left ungraded by an earlier partial run) via
`paper_grader.grade_single_paper` — the same idempotent, DB-aware
function Phase 4's on-demand single-paper endpoint already used. A
paper's evaluation still never changes once assigned, only its
`keywords` list grows on a re-match (`_merge_keyword_onto`, still in
`paper_search.py`). The rubric/prompting/scoring mechanics described
below (rubric file, category clamping, server-side grade derivation) are
unchanged — only *when* and *from where* grading is triggered moved.

- **Rubric — `docs/paper_grading_rubric.json`** (repo root, same
  absolute-path resolution as `paperApis.json`; currently `version:
  "1.5"`). Defines four weighted categories whose *positive* maximums
  sum to 100 — `study_type` **40** (raised from 35 in v1.5),
  `journal_reputation` **15** (lowered from 20 in v1.5 — the 5 points
  moved directly to `study_type`, unlike v1.4's transfer to
  `sample_methodology`), `sample_methodology` 40, `funding_bias` 5 —
  each with a human-readable `description` and a handful of
  `score_tiers` (a point range + a worked example of what earns it),
  plus `grade_bands` mapping contiguous 0-100 score ranges onto letters
  A-E (A: 80-100 down to E: 0-24, per v1.3). `_load_rubric()`
  reads and `@lru_cache`s this file for the process lifetime — unlike
  `paperApis.json` (re-read every call so `enabled: false` takes effect
  live), the rubric isn't meant to be hot-swapped, and it's read once
  per *paper* rather than once per *request*, so re-parsing it every
  time would add up.
  - **Two categories are penalty scales, not plain 0-to-max scores.**
    - `funding_bias` — `min_score: -15`, `max_score: 5` — independent/
      well-disclosed funding earns up to +5, and industry-biased,
      undisclosed-conflict, or "suspicious commercial interference"
      funding is *heavily penalized* down to -15. Funding that simply
      isn't mentioned in the abstract/metadata at all doesn't score a
      neutral 0 — it defaults to **+2** (see "Prompting" below): the
      intent is specifically that an otherwise-strong paper shouldn't be
      dragged toward a lower grade purely because its abstract happens
      not to discuss funding, as opposed to actively indicating a
      conflict of interest.
    - **`journal_reputation` — `min_score: -5`, `max_score: 15` (lowered
      from `20` in v1.5, which itself was new in v1.4, transferred down
      from a plain `0`-`25` scale).** Highly reputable journals still
      earn up to +15 (down from the v1.4 +20 ceiling — v1.5 moved those 5
      points to `study_type`'s max, 35 -> 40, not to
      `sample_methodology` as v1.4's own transfer did), but a publisher
      *actively identified* as predatory, a vanity press, an unvetted
      pay-to-publish outlet, or one using fake/absent peer review is
      still penalized down to -5, same floor as v1.4. Critically, an
      *unidentified* publisher (metadata just doesn't say) still scores
      near-neutral (0-1), not negative — same "penalty only for a
      publisher actively flagged as disreputable" rule v1.4 introduced,
      mirroring how `funding_bias`'s own negative range is reserved for
      actively indicated bias, not silence (see "Prompting" below for
      both).
    - A paper can therefore land well below what its other categories
      alone would suggest when funding *and/or* the journal are flagged
      as bad — e.g. a methodologically excellent but transparently
      marketing-driven study in a predatory journal now loses points on
      two fronts at once rather than merely forfeiting positive credit
      on each. This is why the categories' positive maximums
      (40+15+40+5=100, per v1.5) sum to exactly 100 while the
      theoretical floor is -20 (-15 funding + -5 journal), not 0 —
      `grade_paper` clamps the final total back to 0-100 (see
      "Structured output" below), so that floor never surfaces as a
      negative `grade_score` in the API/UI.
  - **A grade threshold + score-tier calibration (v1.3, unchanged by
    v1.4/v1.5).** The A threshold was lowered from 85 to 80 (B/C/D
    shifted down to match: B 65-79, C 45-64, D 25-44, E 0-24, still
    contiguous/covering 0-100 with no gaps), and
    `study_type`/`sample_methodology`'s score-tier text was nudged so
    best-in-class study designs/sample sizes sit closer to each
    category's ceiling — both changes were prompted by top-tier
    systematic reviews/meta-analyses landing around 74 pts ('B') purely
    because their abstract didn't happen to mention funding. v1.4/v1.5
    continue that same "don't penalize a strong paper for what its
    metadata is silent on" direction, first extended to
    `journal_reputation` in v1.4; v1.5 is purely a point-allocation
    rebalance (`journal_reputation`'s max down 5, `study_type`'s max up
    the same 5) with no new scoring behavior.
- **Prompting.** `_format_rubric_for_prompt` renders every category's
  label/max score/description/score tiers as plain text and embeds it in
  the Gemini prompt alongside the paper's title/abstract/authors/
  journal/publication info — the actual scoring criteria live in the
  JSON file (editable without a code change), not hardcoded into a
  Python string. Gemini is explicitly told to score conservatively in
  the lower tiers of a category when the given metadata doesn't cover
  it — **except `funding_bias` and, as of v1.4, `journal_reputation`,
  both called out in `_build_prompt` as deliberate exceptions**:
  - `funding_score`: funding not being mentioned at all defaults to a
    neutral **+2**, not that category's lower (negative) tiers; a
    negative score is only awarded when the abstract/metadata *actively*
    indicates industry-biased/undisclosed-conflict/suspicious commercial
    funding — never as a penalty for the metadata simply being silent.
  - `journal_score`: an unidentified publisher scores near the neutral
    0-1 tier (not the negative range); a negative score (down to -5) is
    only awarded when the abstract/metadata *actively* identifies a
    predatory publisher, vanity press, unvetted pay-to-publish outlet,
    or fake/absent peer review — again, never merely for the publisher
    being unnamed.
- **Structured output — `_RubricEvaluationSchema`.** Same
  `response_schema` + `.parsed`-with-raw-text-fallback pattern as
  `research_keywords.py`/`vision.py`. Deliberately does **not** ask
  Gemini for a letter grade directly — only the four category scores,
  their descriptive text, `total_score`, and `summary_notes`; the prompt
  explicitly calls out that `funding_score` (-15 to 5, defaulting to +2
  when funding goes unmentioned) and `journal_score` (-5 to 15 as of
  v1.5, previously -5 to 20, defaulting to 0-1 when the publisher goes
  unidentified) are the two fields allowed to go negative — see
  "Prompting" above for both — `study_type_score` (0 to 40 as of v1.5)
  and `sample_score` must be non-negative. Each category score is
  clamped to that category's own `(min_score, max_score)` bounds from
  the rubric (`category_bounds`, built from each category's
  `min_score`/`max_score` — `0` by default, `-15` for `funding_bias`,
  `-5` for `journal_reputation`) in case Gemini's raw output overshoots
  either side. The final
  `total_score` is then recomputed server-side as the sum of those
  clamped category scores (funding's and/or journal's contribution may
  be negative) rather than trusted from Gemini's own arithmetic, clamped
  again to 0-100, and `grade` is derived purely from that final total via
  `grade_bands` (`_score_to_grade`). This guarantees `grade` and
  `grade_score` can never disagree with each other or with the category
  breakdown — a real risk if Gemini were asked to independently pick
  both a score and a letter — and that a heavily-penalized paper's total
  never surfaces as a negative or out-of-range `grade_score`.
- **Per-paper resilience.** `app/services/paper_analysis_pipeline.py::analyze_ingredient_papers`
  (Phase 5) catches `PaperGradingError` (raised for a failed Gemini call,
  an empty/unparseable response, or a missing/malformed rubric file)
  around each paper's grading step and logs a warning rather than
  propagating — a single paper's grading failure leaves that one row
  ungraded (`grade`/`grade_score`/`rubric_evaluation` stay `None`)
  without stopping the loop or rolling back progress already committed
  for earlier papers, same philosophy as `_safe_query_async`'s
  per-source handling.
- **Journal name capture (now unused downstream).** `PaperRecord` still
  captures a `journal: Optional[str]` field per search result (Europe
  PMC's `journalInfo.journal.title`, PubMed's `Journal/Title` XML
  element, Semantic Scholar's `venue` field, OpenAlex's
  `primary_location.source.display_name`/`host_venue.display_name`) —
  but as of Phase 5, nothing reads it: grading happens later, via
  `grade_single_paper`, which has no `journal` parameter (see that
  function's "Known limitation" docstring, same gap Phase 4's on-demand
  endpoint already had). Kept captured per source anyway since it's
  effectively free to extract, in case a future pass wires it back into
  grading.
- **Cost/latency tradeoff.** One blocking Gemini call per paper graded,
  now made from `analyze_ingredient_papers` (Phase 5) after paper search
  completes and its results are committed, rather than inline during the
  search loop — for a grade request that turns up many new papers (or
  re-processes previously ungraded ones), this still adds meaningfully
  to total request time (sequential, not concurrent, deliberately — see
  "Cross-Paper Conclusion Synthesis (Phase 5)" below for why). Acceptable
  for this debug-stage feature's volume; a candidate for a future
  concurrent/batched pass if paper counts grow.
- **API exposure.** `ResearchPaperResponse` (`app/schemas/research.py`)
  gained `grade: Optional[str]`, `grade_score: Optional[int]`,
  `rubric_evaluation: Optional[RubricEvaluationResponse]`, built by the
  shared `app/services/search.py::to_research_paper_response` mapper
  straight from the DB row (`paper.rubric_evaluation` is already a plain
  `dict`, since SQLAlchemy's `JSON` column type deserializes it
  automatically; Pydantic validates it against
  `RubricEvaluationResponse`'s fields, which mirror `paper_grader.py`'s
  `RubricEvaluation` shape exactly). All three are `None` for a paper
  that hasn't been graded yet or whose grading failed — the frontend
  treats that as a normal, expected state: as of Phase 4 (below), it
  renders a gray "(-)" badge for it (not "no badge", as in the initial
  Phase 3 pass) that the user can tap to grade that one paper on demand.
- **On-demand single-paper grading (Phase 4) —
  `grade_single_paper(session, paper)`.** The DB-aware counterpart to
  the pure `grade_paper()` above: given an already-fetched `ResearchPaper`
  ORM instance, returns it unchanged if `paper.grade` is already set (no
  Gemini call — grading is a one-time, idempotent operation once it
  succeeds), otherwise calls `grade_paper()` with that row's own stored
  fields as `paper_metadata` (`journal` is always `None` here — see the
  route doc's "Known limitation" above) and commits the result directly
  (`session.commit()` + `session.refresh()`), rolling back and raising
  `PaperGradingError` on failure. Backs
  `POST /api/v1/papers/{paper_id}/grade` (see API Routes above), which
  the route itself keeps thin: 404 lookup, `run_in_threadpool`-wrapped
  call, 502 on `PaperGradingError`.

**4. Orchestration — `app/services/grading.py`.**
`grade_ingredient(session, ingredient)` runs the full pipeline: generate
keywords -> search + persist papers -> **commit** (durable checkpoint,
see "Cross-Paper Conclusion Synthesis (Phase 5)" below for why this
changed from one single end-to-end transaction) -> run the Phase 5
grade + conclusion-synthesis pipeline over every stored paper for the
ingredient -> count total stored papers -> **debug grade assignment**:
`ingredient.is_graded = True` and `ingredient.grade_badge_text =
f"{paper_count} / {paper_count} / {paper_count}"` — there's no real
grading algorithm yet, this is purely so the badge shows something
derived from real (paper-count) data rather than a static placeholder ->
final commit. Raises `GradingError` (not paper-search's own exceptions,
and not the Phase 5 pipeline's per-paper exceptions, which it never lets
escape — see below) for keyword-generation failures or either commit
failing, so the route only needs to catch one exception type.

**5. Route — `POST /api/v1/ingredients/{id}/grade`** (see API Routes
above) — thin: looks up the `Ingredient` (404 if missing), runs
`grading.grade_ingredient` via `run_in_threadpool` (blocking from the
event loop's point of view — one Gemini call, then the concurrent
paper-search batch described above), and returns a
`GradeIngredientResponse` (`app/schemas/research.py`).

**New dependency:** `httpx` (added to `requirements.txt`) for the paper
search HTTP calls. Not auto-installed per this project's rule against
running install commands — install manually:

```bash
cd backend
pip install -r requirements.txt
```

**Known gaps:**
- `GET /api/v1/supplements/search` still doesn't return
  `is_graded`/`grade_badge_text` on ingredient results — a freshly-graded
  ingredient's state lives only in that one `IngredientCard`'s local React
  state, so navigating away and back to the Results screen currently
  shows it as ungraded again even though the grade (and its papers) are
  still persisted in the DB. Fixing this means adding those two fields to
  `SearchResultItem`/`toIngredient()` — not done in this pass, scoped
  tightly to "click grade, see a result," per the task. (The papers list
  itself doesn't have this gap — `IngredientCard` fetches
  `GET /api/v1/ingredients/{id}` directly on first expand, independent of
  `/supplements/search`'s response, so `StudiesList` always shows real
  persisted data regardless of this gap; only the grade *badge* resets
  visually on revisit.)
- No pagination/dedup-across-requests limit on `ResearchPaper` growth —
  re-grading the same ingredient re-runs the whole search and only adds
  genuinely new papers (via the dedup in `search_papers_for_ingredient`),
  but there's no cap on how many times a user can trigger that.
- Semantic Scholar's unauthenticated tier has a fairly aggressive shared
  rate limit; expect `_safe_query_async` to log 429s there under any real
  usage. An API key (free to request) would raise that limit — not
  wired up here.
- OpenAlex has no documented rate limit for casual/unauthenticated use
  but does ask (via its "polite pool" convention) for a `mailto` param or
  `User-Agent` identifying the caller for better throughput/support —
  not set here.
- This endpoint is unauthenticated, same caveat as `/dev/mock-data`.

## Cross-Paper Conclusion Synthesis (Phase 5)

Where Phase 3/4 grade individual papers in isolation, Phase 5 synthesizes
*claims* — e.g. "Improves deep sleep duration" — across every graded
paper an ingredient has, tracking which papers support vs. contradict
each claim and how confident the evidence makes it. Three new pieces,
plus changes to `paper_search.py`/`grading.py` to wire them in.

**1. Data model — `PaperConclusion` (`app/models/research.py`).** A new
table (`paper_conclusions`), one row per synthesized claim (not per
paper): `id`, `ingredient_id` (FK -> `ingredients.id`, indexed, **no**
`Relationship()` back to `Ingredient` — every consumer queries this
table directly by `ingredient_id`, same "avoid lazy-loaded relationships
in API responses" reasoning as `get_linked_ingredients`), `claim_summary`,
`detailed_conclusion`, `dosage_mentioned` (the specific dosage *this
claim* pertains to — distinct from `Ingredient.recommended_daily_dosage`),
`rubric_evaluation` (JSON — same column pattern as
`ResearchPaper.rubric_evaluation`), `confidence_score` (0-100),
`confidence_grade` (A-E, server-derived, never trusted from Gemini —
same philosophy as Phase 3's `grade`), `cross_paper_consensus` (int,
duplicated out of `rubric_evaluation` as its own column since it's the
one category re-evaluated on every merge), `supporting_paper_ids`/
`contradicting_paper_ids` (JSON arrays of `ResearchPaper.id` — a
lightweight tag, not a join table, since nothing needs more than the
ids/counts), `is_active` (bool, default `True` — reserved for a future
"merge duplicate conclusions" cleanup pass; nothing deactivates a row
today, but every read already filters on it), `created_at`/`updated_at`
(the latter bumped manually on every merge, not an ORM `onupdate` hook).
Being a **brand-new table** (not new columns on an existing one), it
needs no hand-rolled `_migrate_*` function like the additive-column
cases elsewhere in this doc — `SQLModel.metadata.create_all()` (already
called by `init_db()` on every startup) creates any table missing *by
name*, which covers this case directly, as long as `app.models.research`
is imported before `create_all()` runs (it already is).

**2. Rubric — `docs/conclusion_grading_rubric.json`.** Same shape as
`paper_grading_rubric.json` (`grade_bands` A-E over 0-100,
`categories` with `id`/`label`/`max_score`/`description`/`score_tiers`),
but scores a *claim's aggregate evidence*, not one paper: `evidence_strength`
(40 — how strong the paper(s) backing this claim are, by their own
Phase 3 `grade_score`), `cross_paper_consensus` (40 — how many papers
support vs. contradict it, and how consistent the finding is; the *one*
category re-evaluated every time a new paper is merged into an existing
claim), `claim_specificity` (20 — how precisely dosage/population/outcome
are specified, i.e. how clinically actionable the claim is). Maximums
sum to exactly 100.

**3. Synthesis service — `app/services/conclusion_grader.py`.**
`process_paper_conclusions(session, ingredient_id, paper)` is gatekept by
`MIN_GRADE_SCORE_FOR_CONCLUSIONS = 50`: returns `False` immediately (no
Gemini call) if `paper.grade_score` is `None` or `<= 50` — an ungraded or
low-quality paper never gets to influence an ingredient's synthesized
conclusions. Otherwise it:
- Fetches every *active* `PaperConclusion` already stored for the
  ingredient.
- Makes **one** Gemini call (structured `response_schema`, same
  `.parsed`-with-raw-text-fallback pattern as every other Gemini service
  in this app) that does extraction + merging + grading together —
  deliberately not three separate calls, to keep the per-paper request
  count (and therefore free-tier rate-limit exposure) as low as possible.
  The prompt includes the paper's title/abstract/grade plus a compact
  summary of every existing conclusion (`id`, `claim_summary`,
  `confidence_score`, supporting/contradicting counts), and asks Gemini
  to return two lists:
  - `merged_conclusions`: findings that match an existing claim —
    `existing_conclusion_id`, `relationship` (`SUPPORTS`/`CONTRADICTS`),
    reasoning, and a **re-evaluated `cross_paper_consensus` score**.
  - `new_conclusions`: findings that don't match anything existing —
    fully graded against the rubric (all three category scores + tier
    labels + notes).
- For each merge: appends `paper.id` to that conclusion's
  `supporting_paper_ids` or `contradicting_paper_ids` (deduplicated — a
  paper already recorded on a conclusion is never appended twice, which
  is what makes re-running this function on the same paper safe), clamps
  the new `cross_paper_consensus` score to the rubric's bounds, and
  **recomputes total confidence** as `evidence_strength_score` (unchanged
  from when the claim was first created — a later paper's own quality
  doesn't retroactively change it) + the new `cross_paper_consensus` +
  `claim_specificity_score` (also unchanged), clamped to 0-100, with
  `confidence_grade` re-derived from that total via `grade_bands` — never
  trusted from Gemini directly, same "derive the letter server-side"
  philosophy as `paper_grader.py`. Every field is **reassigned to a new
  value** (`conclusion.supporting_paper_ids = [*old, paper.id]`, not
  `.append()`), not mutated in place — required for SQLAlchemy's
  dirty-tracking to notice the change on a JSON column.
- For each new finding: creates a `PaperConclusion` row, category scores
  clamped to the rubric's bounds, `confidence_score` computed as their
  sum (not trusted from Gemini), `confidence_grade` derived the same way,
  `supporting_paper_ids=[paper.id]`.
- Commits its own work (`process_paper_conclusions` is the transaction
  boundary, not its caller) and raises `ConclusionGradingError` — with a
  rollback first — on any failure (Gemini request, response parsing, or
  the commit itself).

**4. Pipeline — `app/services/paper_analysis_pipeline.py`.**
`analyze_ingredient_papers(session, ingredient_id, ingredient_name)` is
the sequential, per-paper loop the task's "replace bulk/batch evaluation
with a sequential pipeline" requirement asks for: it fetches **every
currently-active** `ResearchPaper` stored for the ingredient — excluding
any already `DISCARDED_IRRELEVANT` from a previous run (Phase 6 — see
"Ingredient Relevance Verification (Phase 6)" below) — not just ones
from the current request (this also catches papers left
ungraded/unsynthesized by an earlier, partially-failed run) and, for each
one in turn:
1. Calls `paper_grader.grade_single_paper` (Phase 4's idempotent,
   DB-aware grader — a no-op, no Gemini call, if already graded; as of
   Phase 6 this same call also relevance-checks the paper and sets its
   `status`).
2. If the paper's `status` just came back `DISCARDED_IRRELEVANT`, logs
   `[Pipeline] Discarded Paper ID #{id}: Unrelated to target ingredient
   '{ingredient_name}'` and moves straight to the next paper —
   `process_paper_conclusions` is never called for it (Phase 6).
3. Otherwise, calls `conclusion_grader.process_paper_conclusions` for the
   now-graded, confirmed-relevant paper.

Every step is wrapped in its own `try/except`, and a failure in grading
or conclusion synthesis (rate limiting, a transient network error, a
malformed Gemini response) is logged and the loop **moves on to the next
paper** rather than aborting — the whole point being that
`grade_single_paper` and `process_paper_conclusions` each commit their
own work independently, so whatever succeeded before a mid-loop failure
is already durably saved, never rolled back by a later paper's problem.
`analyze_ingredient_papers` itself never raises for a single paper's
failure; it only would if the initial paper-lookup query itself broke (a
sign of a dead DB connection, not a transient API issue). Returns a
`PipelineResult` (papers considered/graded/failed, discarded-irrelevant,
conclusions attempted/failed) — informational only, nothing branches on
it today beyond logging.

Deliberately **per-paper and sequential, never bulk/batched into fewer,
larger Gemini calls** — both to stay within free-tier rate limits (many
small calls beat one huge one) and because a single call trying to
grade+synthesize an entire ingredient's paper set at once would also
risk exceeding practical context/output-length limits well before it hit
a rate limit.

**5. Wiring — `paper_search.py` / `grading.py`.** `search_papers_for_ingredient`
(Phase 2) is now **search-only** again — the `_apply_grade` call it used
to make per new paper (Phase 3) was removed; grading is consolidated
into the pipeline above so it happens in exactly one place. To make sure
newly-found papers survive even if the pipeline fails outright,
`grade_ingredient` (`app/services/grading.py`) now **commits twice**
before its own final commit: once right after `search_papers_for_ingredient`
returns (durably persisting new papers before grading starts at all),
and then implicitly again on every paper `analyze_ingredient_papers`
successfully processes (since `grade_single_paper`/
`process_paper_conclusions` each call `session.commit()` on the same
shared session — each of those commits also flushes through any other
pending session state, including the newly-found papers if the very
first commit somehow hadn't happened yet). This is the mechanism behind
the "if rate limiting occurs mid-loop, previously completed paper grades
and conclusion updates remain saved without rolling back whole batches"
requirement — nothing in this pipeline defers its persistence to one
big transaction at the end the way the pre-Phase-5 `grade_ingredient`
did.

**API exposure.** `IngredientDetailResponse` (`app/schemas/research.py`)
gained `conclusions: List[PaperConclusionResponse]`, populated by
`app/services/search.py::get_ingredient_conclusions` (every *active*
`PaperConclusion` for the ingredient, highest-`confidence_score`-first)
and returned from `GET /api/v1/ingredients/{id}` (see API Routes above).
Not yet added to `GradeIngredientResponse`/`GradePaperResponse` — those
still only refresh `papers`; the frontend would need a follow-up
`GET .../ingredients/{id}` call to see freshly-synthesized conclusions
after a grade request. **Not yet rendered anywhere in the frontend** —
`StudiesList.tsx` still only shows per-paper data; a "Conclusions" panel
consuming this new field is unbuilt.

**Known gaps:**
- No frontend UI consumes `conclusions` yet — this pass is backend-only,
  scoped to the pipeline/model/API per the task.
- `evidence_strength`/`claim_specificity` are only ever scored once, when
  a claim is first created — if the *original* supporting paper(s)'
  grades were somehow wrong, or a much stronger paper later supports the
  same claim without prompting a full re-score of those two categories,
  `confidence_score` won't reflect that; only `cross_paper_consensus` is
  re-evaluated on every merge, per the task's explicit scope.
- No "supersede/deactivate a stale conclusion" pass exists yet —
  `is_active` is defined and every read filters on it, but nothing ever
  sets it `False`. Two near-duplicate claims from slightly different
  Gemini phrasing could end up as two separate `PaperConclusion` rows
  rather than merging, since matching relies entirely on Gemini
  recognizing the paraphrase against the existing-conclusions list in
  the prompt, not on any server-side similarity check.
- Same unauthenticated-endpoint caveat as the rest of this API surface.

## Ingredient Relevance Verification (Phase 6)

Papers found by `paper_search.py` are keyword-matched, not
topic-verified — a Vitamin C search can surface a Vitamin D paper that
merely happens to share a keyword (e.g. both call themselves an
"antioxidant" or "immune support" study). Phase 6 adds a strict
relevance gate so an off-topic paper never contaminates an ingredient's
grade badge, "Total studies"/"Average grade" figures, or synthesized
conclusions.

**1. Data model — `app/models/research.py`.** Two module-level string
constants, `PAPER_STATUS_ACTIVE = "ACTIVE"` and
`PAPER_STATUS_DISCARDED_IRRELEVANT = "DISCARDED_IRRELEVANT"`, are the
only two values `ResearchPaper.status: str = Field(default=
PAPER_STATUS_ACTIVE)` ever takes. Every module that sets or compares
against paper status (`paper_grader.py`, `conclusion_grader.py`,
`paper_analysis_pipeline.py`, `search.py`) imports these rather than
re-typing the literal, so a future rename can't silently desync one call
site from another. `status` is non-Optional (unlike `grade`/
`grade_score`/`rubric_evaluation`), so a fresh `create_all()` generates
it `NOT NULL DEFAULT` — see `app/db.py::_migrate_research_paper_columns`
above for the matching `ALTER TABLE` DDL on already-deployed databases.
Every paper starts `"ACTIVE"` at ingestion (`paper_search.py` doesn't
grade or relevance-check — see Phase 2 above) and is only ever flipped
to `"DISCARDED_IRRELEVANT"`, never back.

**2. Relevance check — `app/services/paper_grader.py`.** Rather than a
third Gemini call per paper (on top of the existing grading call), the
relevance check is folded into the *same* Gemini call that already
grades a paper against `docs/paper_grading_rubric.json` — consistent
with this pipeline's "minimize Gemini calls per paper" philosophy (see
Phase 2/5 above on rate-limit-driven sequential design).
`_RubricEvaluationSchema` gained two fields, listed *first* (before
`study_type`) since relevance is conceptually a gating question prior
to quality scoring: `is_relevant_to_ingredient: bool` and
`relevance_reasoning: str` (one sentence explaining the call).
`_build_prompt` now takes the target `ingredient_name` and instructs
Gemini to strictly judge whether the paper's title/abstract explicitly
studies, tests, or analyzes that ingredient itself (or a direct synonym
/ specific active compound of it) — not a different ingredient, and not
merely a broader category it happens to belong to — with a worked
Vitamin C/Vitamin D example, and an explicit instruction to judge
relevance on topic alone, never on quality, and to still fill in every
rubric field regardless of the relevance outcome (so a `false` paper
still gets a real, if moot, grade). `grade_paper()`'s return dict gained
`is_relevant_to_ingredient`/`relevance_reasoning` alongside the existing
`grade`/`grade_score`/`rubric_evaluation` keys. Unlike the numeric rubric
scores (always clamped/re-derived server-side — see Phase 3's "never
trust Gemini's own aggregate" rule), the boolean is trusted directly:
there's nothing to clamp on a plain `true`/`false`.

**3. Persisting the result — `paper_grader.py::grade_single_paper`.**
This function (Phase 4's idempotent, DB-aware single-paper grader — also
the per-paper grading step of the Phase 5 pipeline) now also fetches the
paper's `Ingredient` row (`session.get(Ingredient, paper.ingredient_id)`
— raising `PaperGradingError` if somehow missing), passes
`ingredient.name` into `grade_paper()`, and — alongside setting
`grade`/`grade_score`/`rubric_evaluation` as before — sets `paper.status
= PAPER_STATUS_ACTIVE if result["is_relevant_to_ingredient"] else
PAPER_STATUS_DISCARDED_IRRELEVANT`. Status-setting lives here (not in
the pipeline) specifically because `grade_single_paper` is also called
directly by the on-demand `POST /api/v1/papers/{paper_id}/grade` route,
outside the Phase 5/6 pipeline entirely — putting it here means both
callers get uniform, guaranteed status-setting rather than needing the
pipeline to duplicate it.

**4. Defensive gate — `app/services/conclusion_grader.py`.**
`process_paper_conclusions` checks `paper.status ==
PAPER_STATUS_DISCARDED_IRRELEVANT` and returns `False` (no-op, not an
error) as its very first check, *before* the existing
`grade_score <= MIN_GRADE_SCORE_FOR_CONCLUSIONS` gate. This is
defense-in-depth: the pipeline (below) already never calls this function
for a discarded paper, but a defensive check here means the business
rule ("never synthesize conclusions from an irrelevant paper") holds
even for a hypothetical future caller that skips the pipeline.

**5. Pipeline discard logic — `app/services/paper_analysis_pipeline.py`.**
`analyze_ingredient_papers` gained a required `ingredient_name: str`
parameter (needed for the exact log message format below) and now
excludes `PAPER_STATUS_DISCARDED_IRRELEVANT` papers from its initial
query (`.where(ResearchPaper.status != PAPER_STATUS_DISCARDED_IRRELEVANT)`)
so an already-discarded paper is never re-relevance-checked or re-logged
on a subsequent re-grade. After `grade_single_paper` succeeds for a
paper, if its `status` just came back `DISCARDED_IRRELEVANT`, the
pipeline logs `logger.warning("[Pipeline] Discarded Paper ID #%s:
Unrelated to target ingredient %r", ...)`, increments the new
`PipelineResult.papers_discarded_irrelevant` counter, and `continue`s —
skipping `process_paper_conclusions` for that paper entirely (requirement
#2.1: "Skip conclusion extraction and grading for this paper
completely"). `app/services/grading.py::grade_ingredient` was updated to
pass `ingredient.name` through to this new parameter.

**6. Exclusion from summaries — `app/services/search.py`.**
`get_ingredient_papers` (backing both `GET /api/v1/ingredients/{id}` and
`POST /api/v1/ingredients/{id}/grade`) gained a
`.where(ResearchPaper.status != PAPER_STATUS_DISCARDED_IRRELEVANT)`
filter, so a discarded paper is excluded from the `papers` list the
frontend's "Total studies"/"Average grade"/"List of Studies" all derive
from (`StudiesAnalysisBar.tsx`/`StudiesList.tsx`) — satisfying
requirement #2.3 without either table needing its own separate
count/average query. `to_research_paper_response` also now includes
`status` on every response (see API exposure below), and
`ResearchPaperResponse` (`app/schemas/research.py`) gained a matching
non-Optional `status: str` field.

**API exposure & frontend.** `status` is now present on every
`ResearchPaperResponse` (default `PAPER_STATUS_ACTIVE`), mirrored on the
frontend's `ResearchPaper` interface (`src/services/api.ts`), which also
exports `PAPER_STATUS_ACTIVE`/`PAPER_STATUS_DISCARDED_IRRELEVANT`
constants matching the backend's. In practice a `DISCARDED_IRRELEVANT`
paper is never returned by `GET /api/v1/ingredients/{id}` or
`POST /api/v1/ingredients/{id}/grade` (both go through the now-filtered
`get_ingredient_papers`) — but `POST /api/v1/papers/{paper_id}/grade`
(on-demand single-paper grading) *does* return the just-graded paper
regardless of outcome, since it's not a list query. `IngredientCard.tsx`'s
`handlePaperGraded` (passed to `StudiesList` as `onPaperGraded`) checks
`updatedPaper.status` and, if `DISCARDED_IRRELEVANT`, **filters the
paper out of local state** instead of splicing it back in — otherwise a
stale "irrelevant" paper would stay visible in `StudiesList` and counted
in `StudiesAnalysisBar` until the next full `fetchIngredientDetail()`
call.

**Known gaps:**
- No UI surfaces *why* a paper was discarded — `relevance_reasoning` is
  returned by `grade_paper()` internally but never persisted on
  `ResearchPaper` or exposed via any API response; a discarded paper
  simply stops appearing.
- A `DISCARDED_IRRELEVANT` row is kept in the database, not hard-deleted
  (requirement #2.2 offered either "flag or delete" — flagging was
  chosen, consistent with this codebase's general preference for
  soft-state over destructive deletes, e.g. `PaperConclusion.is_active`).
  There's no admin/debug view of discarded papers, so this is currently
  a one-way, silent trip to invisible-but-present state.
- Same unauthenticated-endpoint caveat as the rest of this API surface.

## Verified Online Resources (Phase 7)

Every paper-derived signal so far (Phase 2-6) comes from academic search
APIs and an LLM's own judgment of quality/relevance. Phase 7 adds a
second, independent category of evidence: direct links to official
government and regulatory reference pages (NIH, USDA, EFSA) — no Gemini
call involved at all, just plain HTTP requests to public APIs plus a
strict domain allow-list.

**1. Config — `docs/verified_resource_apis.json`.** Same
config-file-driven-source pattern as `docs/paperApis.json`
(Phase 2/Research Paper Search below): a JSON array of `{id, name,
domain, endpoint, query_param, extra_params, auth, access_type,
description}` entries. As shipped in Phase 7, four free/keyless sources
were configured (MedlinePlus, PubChem, USDA FoodData Central, OpenEFSA);
as of Phase 10 this expanded to six — see "Verified Resource Fetcher
Expansion (Phase 10)" below for the current source list and what changed.
`enabled: false` (or removing an entry) disables querying that source,
same convention as `paperApis.json`.

**2. Data model — `app/models/research.py`.** A new `VerifiedResource`
SQLModel table (`verified_resources`) — `id`, `ingredient_id` (FK to
`ingredients.id`), `title`, `publisher`, `url`, `domain`, `summary`
(optional), `created_at`. No ORM `Relationship()` back to `Ingredient` —
same convention as `PaperConclusion`, queried directly by
`ingredient_id` in `search.py`. Unlike every other Phase 2-6 addition to
`research_papers` (`keywords`, `grade`, `status`, ...), this is a
brand-new table rather than new columns on an existing one, so it needs
no additive `_migrate_*` step in `app/db.py` —
`SQLModel.metadata.create_all()` alone creates it on both a fresh
database and one upgraded from a pre-Phase-7 version.

**3. Query + strict domain filtering —
`app/services/resource_fetcher.py`.** `fetch_verified_resources_for_ingredient(session,
ingredient_id, ingredient_name)` queries every enabled source in
`docs/verified_resource_apis.json` by `ingredient_name`, concurrently
(one `httpx.AsyncClient` + `asyncio.gather`, same fan-out pattern as
`paper_search.py`), each individually guarded against timeouts/HTTP
errors/malformed responses so one flaky government API never fails the
whole request. As shipped in Phase 7, three of the four sources
(MedlinePlus, USDA, EFSA) were queried through a schema-tolerant generic
extractor (`_extract_generic_records`); as of Phase 10, every source
except Health Canada LNHPD has its own precise, shape-verified parser
instead — see "Verified Resource Fetcher Expansion (Phase 10)" below for
why (the generic extractor turned out not to actually match either
MedlinePlus's or USDA's real response shape, which silently produced
zero results from both).

**Every candidate result, regardless of source or parser, is checked
against `_is_verified_domain` before it's ever turned into a
`VerifiedResource` row** — this is the actual safety mechanism, not the
per-source parsing — even a malformed or unexpected response shape can
only ever produce *fewer* results, never an unverified one, since the
domain check runs on every extracted URL unconditionally. The allow-list
itself was widened in Phase 10 — see below for the current list.

Deduplicated per-ingredient by `url` (same convention as
`ResearchPaper.source_url`); `session.flush()`s rather than
`session.commit()`s, so its work lands in the same first commit
`grade_ingredient` already does right after paper search.

**4. Pipeline wiring — `app/services/grading.py`.** `grade_ingredient`
calls `fetch_verified_resources_for_ingredient` right after
`search_papers_for_ingredient`, before the first commit — wrapped in its
own `try`/`except` (logged, not re-raised) since this is an independent
subsystem (no Gemini call, no shared state with paper grading): a
resource-lookup failure should never fail the whole grade request, same
"one subsystem's hiccup isn't everyone's failure" philosophy as
paper-search's own per-source error handling.

**5. API exposure — `app/schemas/research.py` /
`app/services/search.py`.** `VerifiedResourceResponse` mirrors the
`VerifiedResource` table field-for-field, including the Phase 8
`grade`/`score`/`reasoning_summary` columns (see "Automated Resource
Grading (Phase 8)" below). `get_ingredient_resources` (new) returns
every stored resource for an ingredient, most recently added first — no
status/relevance filter needed here (unlike `get_ingredient_papers`'s
Phase 6 filter), since every row already cleared the domain allow-list
before being persisted in the first place. `IngredientDetailResponse`
gained a `verified_resources` field, populated by `get_ingredient_detail`
alongside `papers`/`conclusions`. Not added to
`GradeIngredientResponse`/`GradePaperResponse` — same convention as
`conclusions`: the frontend re-fetches ingredient detail after a grade
request to pick up anything Phase 7/8 just found/graded.

**6. Frontend — `src/components/VerifiedResourcesList.tsx`.** Renders
the "Verified Online Resources" panel: header, the subheading
"Authoritative reference sheets and official health agency
documentation.", and one row per resource — title (bold) + a derived
authority pill badge ("NIH"/"USDA"/"EFSA"/"GOV", from the resource's
already-verified `domain`) on the left, a Phase 8 grade/score badge
(circular A-E letter badge + "N/100" score text, same visual treatment
as `RecommendedUsesList`'s conclusion grade badge — rendered only when
`resource.grade` is non-null) plus a "View Resource ↗" link on the
right, dashed separators between rows (same visual convention as
`RecommendedUsesList`'s rows). The empty state renders the spec's exact
copy: "No verified government or regulatory reference pages found for
this ingredient." The container uses a 2px `colors.orange` (`#E85D04`)
border — the "active card outline theme" — distinguishing it visually
from its border-less siblings (`RecommendedUsesList`/`StudiesAnalysisBar`
only use a tinted background).

The "View Resource" link renders differently per platform:
on web, a genuine `<a href={url} target="_blank" rel="noopener
noreferrer">` via a raw `React.createElement('a', ...)` call (not RN's
`Text`/`Pressable`, which don't reliably pass `href`/`target`/`rel`
through react-native-web to the underlying DOM node) — this is the one
place in the app with an explicit `target`/`rel` requirement to satisfy
literally; on native (iOS/Android), a `Pressable` calling
`Linking.openURL`, since `target`/`rel` have no native equivalent and
`Linking.openURL` already hands the URL to the system browser/app.

Wired into `IngredientCard.tsx`'s "Scientific information" composite
section directly between `RecommendedUsesList` ("Recomended uses list")
and `StudiesAnalysisBar` ("Studies Analisis"), per spec. Its data
(`verifiedResources` state) follows the exact same
undefined/loading/error/fetch-once-per-mount lifecycle as `papers`/
`conclusions` — populated by the same `GET /api/v1/ingredients/{id}`
call on first expand, and refreshed by a follow-up
`fetchIngredientDetail()` call after a grade request completes (since
`GradeIngredientResponse` doesn't carry it — see point 5 above).

**Known gaps (as shipped in Phase 7 — see "Verified Resource Fetcher
Expansion (Phase 10)" below for what was subsequently fixed):**
- The generic extractor (`_extract_generic_records`) is a best-effort
  heuristic, not a guaranteed-correct parser for MedlinePlus/USDA/EFSA's
  actual response shapes — it may under-extract (miss a resource whose
  fields don't match any of the tried key names) or, less likely thanks
  to the "must have a title/publisher/summary-ish field too" guard,
  over-extract an incidental URL-bearing object that isn't really a
  standalone reference page. The domain allow-list bounds the *risk*
  (nothing unverified ever gets through) but not the *completeness* or
  *precision* of what's found. **(Phase 10: this was, in fact, the actual
  bug — see below.)**
- No retry/backoff on a failed source — same fail-open, log-and-skip
  behavior as `paper_search.py`'s sources; a transient failure just
  means fewer resources found this run, not a queued retry. **(Phase 10
  adds one same-source retry using a chemical/systematic name — still no
  cross-source retry/backoff.)**
- Same unauthenticated-endpoint caveat as the rest of this API surface.

## Automated Resource Grading (Phase 8)

Phase 7 guarantees every `VerifiedResource` comes from an official
government/regulatory *domain* — but domain authority alone doesn't say
whether one particular page is actually any good (thin content, no
citations, an outdated entry, or even a stray commercial page hosted
under an otherwise-official domain). Phase 8 adds a second, independent
quality signal on top: an automated 0-100 rubric score, graded by
Gemini, using `docs/resource_grading_rubric.json`.

**1. Rubric — `docs/resource_grading_rubric.json`.** Same shape/tooling
as `docs/paper_grading_rubric.json` (`categories` + `grade_bands`,
verified via the same sum-to-100 / contiguous-bands sanity checks used
for every other rubric in this codebase). Four categories: `publisher_authority`
(0-35 — domain/institutional authority), `evidence_citations` (0-30 —
scientific citations & primary sources), `comprehensiveness_currency`
(0-20 — content breadth & recency), and `transparency_bias` (-10 to 15 —
the one category that can go negative, penalizing commercial/sales
motives). Category maxes sum to exactly 100; `grade_bands` map a clamped
0-100 total onto A (80-100) through E (0-24), contiguous and covering
the full range, same convention as every other rubric here.

**2. Grading service — `app/services/resource_grader.py`.**
`grade_resource(resource_metadata)` is pure (no DB access) — it takes
`{"resource_title", "url", "publisher", "page_snippet_or_text"}` (any
value may be `None`; Gemini is instructed to score conservatively where
content is missing, same "don't assume the best case" philosophy as
`paper_grader.py`) and returns `{"total_score", "grade",
"category_scores", "reasoning_summary"}` via a single Gemini call with a
structured `response_schema`.

Same "never trust the model's own aggregate" rule as every other
Gemini-graded entity in this codebase: `_ResourceEvaluationSchema` asks
Gemini for the four `category_scores` and a `reasoning_summary` only —
deliberately NOT `total_score`/`grade`, even though the task's own
example Gemini output includes them, because asking for both risks the
two disagreeing (e.g. category scores summing to 72 paired with a
returned "A"). `grade_resource` instead clamps each category score to
its rubric bounds (`0` to `max_score` for three categories;
`transparency_bias` clamped to its `-10` to `15` penalty range), sums
the clamped scores into `total_score`, clamps that sum to `0`-`100` once
more (the task's explicit "Score Calculation Guard" — the four category
bounds alone already keep the raw sum within `-10` to `100`, so this
final clamp only ever has to catch the low end), and derives `grade`
from that final total via the rubric's `grade_bands` — identical
`_clamp`/`_score_to_grade` logic to `paper_grader.py`, just applied to a
resource's four categories instead of a paper's.

Unlike `paper_grader.py`, there is no DB-aware
`grade_single_resource()`/on-demand grading endpoint here — this task's
scope doesn't include a "tap a badge to grade this one resource" UI
(unlike papers' `POST /api/v1/papers/{paper_id}/grade`). Keeping this
module DB-agnostic means `resource_fetcher.py` — which already owns
every `VerifiedResource` ORM object's construction/`session.add()`/flush
lifecycle — doesn't have to hand a half-built row back and forth across
a module boundary just to get it graded.

**3. Wiring — `app/services/resource_fetcher.py`.** Per the task's
explicit instruction ("when resource_fetcher.py retrieves online
reference pages... evaluate each resource"), grading happens inline,
directly inside `fetch_verified_resources_for_ingredient`'s loop — right
after each new `VerifiedResource` row is built, before it's added to the
session. One Gemini call per resource, sequentially (never batched, same
rate-limit-friendly philosophy as every other Gemini-calling loop in
this codebase). `page_snippet_or_text` is populated from the resource's
own `summary` field (itself optional — not every source provides one;
see Phase 7). A grading failure for one resource
(`ResourceGradingError` — a Gemini request/parsing error) is caught and
logged, not re-raised: that resource is still persisted, just with
`grade`/`score`/`reasoning_summary` left at their default `None`
(permanently ungraded, never retried — same best-effort convention as
`ResearchPaper.grade`), so one flaky Gemini call never costs an
otherwise-good, already-domain-verified link its spot in the list.

**4. Data model — `app/models/research.py`.** `VerifiedResource` gained
three nullable columns: `grade` (`Optional[str]`, one of "A"-"E"),
`score` (`Optional[int]`, 0-100), `reasoning_summary` (`Optional[str]`).
All three `None` until grading succeeds. Deliberately no separate
per-category JSON breakdown column (contrast with
`ResearchPaper.rubric_evaluation`) — the task spec for this feature only
calls for `grade`/`score`/`reasoning_summary` on this table; the four
`category_scores` Gemini also returns are used to compute `score` but
aren't separately persisted. Since `verified_resources` already existed
in deployed (Phase 7) databases before these three columns were added,
they need the same additive `ALTER TABLE` migration treatment as
`ResearchPaper`'s own `grade`/`grade_score` — see
`app/db.py::_migrate_verified_resource_columns` (Database section
above).

**5. API exposure — `app/schemas/research.py` /
`app/services/search.py`.** `VerifiedResourceResponse` gained matching
`grade`/`score`/`reasoning_summary` fields (all `Optional`, default
`None`). `get_ingredient_resources`/`to_...` mapping in `search.py`
passes all three through from the ORM row — no filtering logic needed
(unlike Phase 6's relevance filter), since an ungraded resource is still
shown, just without a grade badge.

**6. Frontend — `src/components/VerifiedResourcesList.tsx`.** Each
resource row gained a grade/score badge — a circular A-E letter badge
(`GRADE_COLORS`, same green-to-red mapping and `isPaperGrade` type guard
shared across `ResearchPaper.grade`/`PaperConclusion.confidence_grade`/
`VerifiedResource.grade`, see `src/utils/grades.ts`) plus its "N/100"
score text, rendered on the row's right side alongside the "View
Resource ↗" link, and omitted entirely (no badge at all, not a
placeholder) for a `null` grade — same "null grade = normal ungraded
state, not an error" convention as `StudiesList`/`RecommendedUsesList`.
`VerifiedResource` (`src/services/api.ts`) gained matching
`grade`/`score`/`reasoning_summary` fields. `IngredientCard.tsx` itself
needed no changes for this — the `verified_resources` data it already
fetches/passes through to `VerifiedResourcesList` picks up the new
fields automatically, since the type-level plumbing was already in place
from Phase 7.

**Known gaps:**
- `reasoning_summary` is fetched and persisted but not yet surfaced
  anywhere in the UI beyond being available on the `VerifiedResource`
  object — no info-modal/tooltip exposes it yet (contrast with
  `RecommendedUsesList`'s conclusion detail modal, which does show its
  own rubric reasoning).
- No admin/debug view of ungraded (`grade: null`) resources — a
  permanently-failed grading attempt is silent and indistinguishable
  from "grading hasn't run yet" from the frontend's point of view.
- Same unauthenticated-endpoint caveat as the rest of this API surface.

## Scientific Information Redesign (Phase 9)

Unifies the visual/interaction design of the three "Scientific
Information" list panels (`RecommendedUsesList`, `VerifiedResourcesList`,
`StudiesList`) and adds a consistent two-modal pattern (rubric breakdown
vs. general metadata) to all three — frontend-only, no backend or schema
changes.

**1. Outer section — `src/components/IngredientCard.tsx`.** The
"Scientific information" block is now "Scientific Information": wrapped
in a bordered card (`borderWidth: 1`, `borderColor: colors.orange`
i.e. `#E85D04`, `borderRadius: 12`, `padding: spacing.md`), with a
centered, bold, 22px title (`typography.sectionTitle`) and a synthesized
one-sentence summary directly beneath it (e.g. *"Analyzed 12 studies
across databases. Average score: B (78/100). Primary consensus
indicates: '...'"*). The summary is built client-side in a `useMemo`
(`scientificSummary`) from data already fetched for the three lists — no
new endpoint. Its average-grade math (`computeAverageGrade`, moved into
`src/utils/grades.ts`) is the same band table (`grade_bands`: A 85-100
down to E 0-29) `StudiesAnalysisBar.tsx` used to compute; the top
synthesized claim comes from `conclusions[0]`, which
`IngredientDetailResponse` already documents as sorted
highest-confidence-first server-side.

**2. Removed — `StudiesAnalysisBar.tsx`.** The standalone "Studies
Analisis" metrics block (total studies / average grade / placeholder
"Rating: XX") is no longer imported or rendered anywhere. Its total-count
metric now lives inside `StudiesList`'s own collapsible title bar
("List of Studies (Total: N)"); its average-grade computation was ported
to `computeAverageGrade` (`src/utils/grades.ts`) and now feeds
`IngredientCard.tsx`'s summary sentence instead. The file itself is left
in place, unused, rather than deleted (no delete tooling in this pass) —
docs/Architecture.md's file-structure listing below reflects that it is
no longer part of the render tree.

**3. Shared components (new):**
- `src/components/CollapsibleSection.tsx` — the common
  click-title-bar-to-toggle wrapper (chevron `▲`/`▼`, `isOpen` state
  defaulting to `true`) and container border (`1px solid`
  `colors.neutralBorder` i.e. `#E0E0E0`, `borderRadius: 8`) shared by all
  three list panels, replacing three near-identical hand-copied
  container/header implementations. `colors.neutralBorder` is a new
  `theme.ts` palette entry, added specifically so this `#E0E0E0` never
  has to appear as an inline hex value in a component's `StyleSheet`,
  honoring that file's own "every color used anywhere in the UI should
  come from this file" rule. Deliberately distinct from `colors.orange`
  — each list's own border is this subtler gray, while the bolder orange
  is now reserved for the outer "Scientific Information" card only (see
  point 1) — so `VerifiedResourcesList`'s previous orange container
  border (Phase 7's "active card outline theme") changed to this gray.
- `src/components/GradeCircleBadge.tsx` — the shared round A-E
  letter-grade badge (`GRADE_COLORS` fill, white bold letter,
  palette-orange border, optional `onPress`/`large` variants), factored
  out of `StudiesList.tsx`'s previously-local `PaperGradeBadge` so
  `RecommendedUsesList`/`VerifiedResourcesList` render an identical badge
  for `PaperConclusion.confidence_grade`/`VerifiedResource.grade`.
- `src/components/ExternalLinkIconButton.tsx` — the shared "🌐" row
  action (Ionicons `globe-outline`), factored out of `StudiesList.tsx`'s
  inline globe icon so `VerifiedResourcesList` renders the same
  icon/interaction for `VerifiedResource.url` instead of its old
  separate "View Resource ↗" text link. Preserves the dual web/native
  open-in-new-tab behavior (`<a target="_blank" rel="noopener
  noreferrer">` on web via `React.createElement`, `Linking.openURL` on
  native) that `VerifiedResourcesList`'s old `ViewResourceLink`
  established in Phase 7. `RecommendedUsesList` never renders this — a
  `PaperConclusion` is a synthesized cross-paper claim, not a single
  external page, so per spec its rows have no website icon.

**4. Standard row layout, all three lists:**
`flexDirection: 'row'`, `justifyContent: 'space-between'`,
`alignItems: 'center'` — title/claim text on the left, up to three
action icons on the right in a fixed order: grade badge (if graded) →
info `(i)` icon → website `🌐` icon (`StudiesList`/`VerifiedResourcesList`
only). Pagination unchanged (`src/components/Pagination.tsx`, already
matched the `← Previous [1][2] Next →` spec exactly) but now capped at
`PAGE_SIZE = 5` on every list — `RecommendedUsesList` was previously 3;
`VerifiedResourcesList` had no pagination at all before this pass (every
resource rendered on one unbroken page).

**5. Two-modal-per-list-item pattern, all three lists.** Each list keeps
two separate `useState<T | null>` selections,
`activeRubricModalItem`/`activeInfoModalItem`, replacing any prior single
combined modal:
- **Rubric & Comments Modal** (tap the grade badge) — total score/grade
  plus a per-category breakdown and AI reviewer notes, using whichever
  categories that item type actually has: `StudiesList` shows the paper
  rubric's four categories (Study Design, Journal Rigor, Methodology &
  Sample, Funding & Bias — unchanged from Phase 3/4, already matched this
  spec exactly); `RecommendedUsesList` shows the *conclusion* rubric's
  own three categories (Evidence Strength, Cross-Paper Consensus, Claim
  Specificity — a different rubric shape, `ConclusionRubricEvaluation`,
  not the paper's); `VerifiedResourcesList` shows only the total
  score/grade and `reasoning_summary` with an explicit "category
  breakdown not available for this source" note, since
  `VerifiedResource` deliberately persists only one summary column, not
  per-category scores (Phase 8's `resource_grader.py` design decision).
- **General Info Modal** (tap the `(i)` icon) — general metadata, per
  type: Studies show title/authors/publication date/`source_domain` (the
  closest available stand-in for "journal" — no dedicated column exists)
  /abstract/`rubric_evaluation.sample_info` (closest available stand-in
  for "sample size") /Matched Keywords; Recommended Uses show the claim/
  detailed conclusion plus confidence score, dosage notes, and
  supporting/contradicting paper counts; Resources show publisher,
  a derived "domain authority" rating (`deriveAuthorityBadge`), and
  summary — with "citation count" shown as an explicit "not tracked for
  this resource type" rather than a fabricated number, since
  `VerifiedResource` has no such field.

**6. File-level changes:**
- `src/theme.ts` — added `colors.neutralBorder` (`#E0E0E0`).
- `src/utils/grades.ts` — added `computeAverageGrade()` +
  `AverageGradeResult` (ported from `StudiesAnalysisBar.tsx`).
- `src/components/StudiesList.tsx` — least changed of the three (it
  already matched most of this spec): now wrapped in
  `CollapsibleSection`, title includes `(Total: N)`, modal state renamed
  to `activeRubricModalItem`/`activeInfoModalItem`, grade badge/globe
  icon now use the shared `GradeCircleBadge`/`ExternalLinkIconButton`.
- `src/components/RecommendedUsesList.tsx` — `PAGE_SIZE` 3 → 5, wrapped
  in `CollapsibleSection`, grade badge made pressable, single combined
  modal split into separate rubric/info modals.
- `src/components/VerifiedResourcesList.tsx` — most changed: added
  pagination (previously none), wrapped in `CollapsibleSection` (border
  orange → `colors.neutralBorder`), added an info `(i)` icon and its
  modal (neither existed before), grade badge made pressable with a new
  rubric modal, "View Resource ↗" text link replaced by
  `ExternalLinkIconButton`.

**Known gaps:**
- `VerifiedResource`'s "citation count" and precise numeric "domain
  authority rating" remain unavailable by design (no backing column) —
  the General Info Modal shows honest placeholders rather than
  fabricated values; adding real citation-count tracking would require a
  new backend field/data source.
- `StudiesAnalysisBar.tsx` is orphaned (unused but not deleted) — no file
  delete tooling was available in this pass.

## Verified Resource Fetcher Expansion (Phase 10)

Fixes a bug where `fetch_verified_resources_for_ingredient` effectively
only ever returned PubChem results, and expands
`app/services/resource_fetcher.py` to properly support all six sources
listed in `docs/verified_resource_apis.json`. Backend-only — no schema,
model, or API-shape changes; `VerifiedResource`/`VerifiedResourceResponse`
and `fetch_verified_resources_for_ingredient`'s call signature are
untouched, so nothing else in the pipeline (grading.py, search.py, the
frontend) needed to change.

**Root cause.** Three of the four Phase 7 sources (MedlinePlus, USDA,
EFSA) shared one schema-tolerant generic extractor
(`_extract_generic_records`) that assumes a flat `{"url": "...", "title":
"..."}`-shaped dict somewhere in the response. Two of those never
actually matched it: MedlinePlus was pointed at the wrong service
entirely (`connect.medlineplus.gov`'s Connect API takes a standardized
ICD/RxNorm *code*, not a free-text ingredient name — it could never
resolve "Vitamin C" to anything), and USDA FoodData Central's
`/foods/search` response has no URL field at all, only an `fdcId` a
caller has to build a link from. Both silently produced zero results on
every call, leaving PubChem — which already had its own precise parser —
as the only source that ever actually worked.

**1. Precise, shape-verified parsers replace the generic extractor for
every source except Health Canada.** Real response shapes were confirmed
against each provider's live API/documentation before writing its
parser (not assumed):
- **MedlinePlus (`_query_medlineplus`)** — `docs/verified_resource_apis.json`'s
  `medlineplus_api` entry now points at the real free-text health topic
  search (`wsearch.nlm.nih.gov/ws/query?db=healthTopics&term=...`), which
  returns XML: `<nlmSearchResult><list><document url="...">
  <content name="title">...</content>...</document></list></nlmSearchResult>`.
  `_query_medlineplus` parses this via `xml.etree.ElementTree`, stripping
  the embedded highlight-span HTML MedlinePlus's own search-term
  highlighting leaves inside `<content>` text (`_strip_html`). It also
  handles a JSON response defensively (falls back to
  `_extract_generic_records` if the response's content-type/body looks
  like JSON instead) — satisfying "parse both JSON and XML cleanly."
- **USDA (`_query_usda`)** — parses the real `{"foods": [{"fdcId",
  "description", "dataType", "foodCategory"}]}` shape and *constructs* a
  detail-page URL from `fdcId`
  (`https://fdc.nal.usda.gov/food-details/{fdcId}/nutrients`), since the
  response itself carries no link. `api_key` is resolved fresh per
  request from the `USDA_API_KEY` environment variable when set,
  overriding the checked-in `DEMO_KEY` default in the config file — a
  403 response is logged with an explicit
  `USDA FoodData Central: 403 error - invalid or rate-limited api_key`
  line before the generic HTTP-status handler would otherwise catch it.
- **DailyMed (`_query_dailymed`, new source)** — queries
  `/dailymed/services/v2/spls.json?drug_name=...` (confirmed against
  NLM's own API docs: `{"data": [{"setid", "title", "published_date"}]}`)
  and constructs the SPL detail page URL from `setid`
  (`https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}`).
- **Europe PMC (`_query_europe_pmc`, new source)** — builds its own query
  string as `"{ingredient_name} AND (monograph OR review)"` (not a bare
  ingredient-name search — overriding what `_resolve_endpoint` would
  otherwise substitute), keeps only `isOpenAccess == "Y"` results, and
  prefers an HTML link from `fullTextUrlList.fullTextUrl[]`, falling back
  to Europe PMC's own canonical article page
  (`https://europepmc.org/article/{source}/{id}`) when the full-text-URL
  list is missing one. Uses `resultType: core` (not the original `lite`)
  specifically to get `abstractText` back for the resource's summary.
- **Health Canada LNHPD (`_query_health_canada`, new source)** — still
  uses the schema-tolerant `_extract_generic_records` path, because its
  real JSON response envelope could not be confirmed at implementation
  time (a live test request against the configured endpoint returned an
  empty body) — extended with LNHPD-plausible field names
  (`product_name`/`licence_number`/`company_name`) added to the generic
  extractor's key lists. Flagged in code as the one source that should be
  upgraded to a precise parser once its real shape is confirmed, the same
  way the other four were in this pass.
- **PubChem** — unchanged; it already had a precise parser.

**2. Parallel execution — `_search_all_records_async`.** Now uses
`asyncio.gather(*tasks, return_exceptions=True)` (previously
`asyncio.gather(*tasks)` with no `return_exceptions`). Every per-source
coroutine was, and still is, independently wrapped in its own
try/except (`_run_source_query`) so nothing should actually raise past
it — `return_exceptions=True` is kept as a defense-in-depth guarantee on
top of that, and the aggregation loop explicitly checks for a stray
exception object and logs+skips it rather than propagating, so a timeout
or error in one source's coroutine categorically cannot cancel or block
another's in-flight request. Verified with an offline test harness
(stubbed sources: one timing out, one raising an unexpected exception,
one succeeding, one needing its fallback retry — all four ran to
completion independently, see point 4).

**3. Common-name -> chemical-name fallback retry —
`_CHEMICAL_NAME_FALLBACKS`/`_fallback_name_for`/`_safe_query_async`.** A
small, hand-curated table maps common supplement-label names to their
primary chemical/systematic name (e.g. "Vitamin C" -> "Ascorbic acid",
"Vitamin D3" -> "Cholecalciferol", "CoQ10" -> "Ubiquinone" — about 20
entries covering the standard vitamin/coenzyme naming pairs, not a
general chemistry name-resolution service). `_safe_query_async` runs a
source's query with the ingredient's common name first; if that comes
back with zero results (not necessarily an error) and a fallback name
exists, it retries that same source once with the chemical name before
giving up — each attempt still goes through the same per-request 5s
timeout and independent try/except. An ingredient not in the table
simply isn't retried.

**4. Standardized result shape — `VerifiedResourceSchema`.** Every
per-source parser now returns `List[VerifiedResourceSchema]` (a Pydantic
`BaseModel` — `title: str`, `publisher: str`, `url: str`, `domain: str`,
`summary: Optional[str]`) instead of the old plain `ResourceRecord`
dataclass — same two-stage "parser output -> DB row" split as before,
just with Pydantic's construction-time type validation catching a parser
bug immediately instead of it surfacing later as a confusing DB-layer
error.

**5. Widened domain allow-list — `_VERIFIED_DOMAIN_SUFFIXES`.** Now
`.gov`, `.europa.eu`, `.org`, `ebi.ac.uk`, `canada.ca` (plus
`ncbi.nlm.nih.gov`/`efsa.europa.eu`, kept for readability even though
both are now redundant with `.gov`/`.europa.eu`) — widened specifically
to admit `europepmc.org` (`.org`), `www.ebi.ac.uk` (Europe PMC's actual
API host), and `health-products.canada.ca` (`canada.ca`, Health Canada
LNHPD). **`.org` is a deliberately broad suffix** — it admits any `.org`
domain whatsoever, not just EMBL-EBI's — accepted here only because it
was an explicit requirement; narrowing it to specific known-good hosts
would meaningfully tighten this without losing any currently-configured
source, if ever revisited.

**6. Logging convention.** Every log line in this module is now prefixed
`[ResourceFetcher]` and names the source's display label — an explicit
per-provider status line is logged on every outcome
(`[ResourceFetcher] MedlinePlus (NIH/NLM): 2 resource(s) found for
'Vitamin C'.`, `[ResourceFetcher] USDA FoodData Central: 403 error -
invalid or rate-limited api_key.`, a fallback-retry notice, etc.) so a
single ingredient's grade-request log output makes it immediately
obvious which of the six sources actually contributed results.

**Verification.** Since this sandbox has no installed `httpx`/
`pydantic`/`sqlmodel` (and package installation isn't run automatically
per this repo's CLAUDE.md), correctness was verified with a standalone
offline test harness: lightweight stub modules stood in for
`httpx`/`pydantic`/`sqlmodel`/the `app.models`/`app.services` import
graph so the *real* `resource_fetcher.py` could be imported and exercised
directly, then: (1) the domain allow-list was checked against every
provider's real hostname plus a rejected example; (2) `_parse_medlineplus_xml`
was run against the actual XML payload captured from a live MedlinePlus
wsearch request; (3) `_query_usda`/`_query_dailymed`/`_query_europe_pmc`
were each run against a synthetic payload built from that provider's
documented/confirmed real response shape, with a fake `httpx.AsyncClient`
standing in for the network call; (4) `_fallback_name_for` was checked
against known and unknown ingredient names; (5)
`docs/verified_resource_apis.json` was confirmed to parse into exactly
the six expected, all-enabled source ids; (6) a full
`_search_all_records_async` fan-out was run against four stubbed sources
(one timeout, one unexpected exception, one immediate success, one
needing its fallback retry) confirming none of the four blocked or
cancelled another and the fallback retry fired correctly. All checks
passed. `python3 -m py_compile app/services/resource_fetcher.py` also
passes.

**Known gaps:**
- Health Canada LNHPD's real response shape is still unconfirmed —
  `_query_health_canada` relies on the schema-tolerant generic extractor
  with plausible field-name guesses, not a verified parser (see point 1).
- The `_CHEMICAL_NAME_FALLBACKS` table is small and hand-curated (~20
  entries, common vitamins/coenzymes only) — an ingredient outside that
  set gets no fallback retry even if its common name genuinely doesn't
  resolve on a given source.
- `.org`'s breadth (point 5) remains a known, accepted tradeoff, not an
  oversight — see that point for the narrowing option if ever revisited.

## Multi-Source Ingredient Summary Synthesis (Phase 11)

Every prior synthesis step (Phase 5's `PaperConclusion`s) only ever
considered peer-reviewed papers. Phase 11 adds one additional,
ingredient-level Gemini call that considers BOTH graded `ResearchPaper`
findings AND official `VerifiedResource` guidance (NIH/USDA/EFSA/Health
Canada/etc. — Phase 7/8) together, producing a single synthesized
`summary_description` — the sentence rendered directly under the
"Scientific Information" section title on a standalone `IngredientCard`
(see "Scientific Information Redesign (Phase 9)" above for where that
title/summary slot came from).

**1. Data model — `app/models/supplement.py` / `app/db.py`.** `Ingredient`
gained a nullable `summary_description: Optional[str]` column. Additive/
migrated the same way `is_graded`/`grade_badge_text` were —
`_migrate_ingredient_grading_columns()` (app/db.py) now also adds
`summary_description` to a pre-Phase-11 `ingredients` table via
`ALTER TABLE`, same idempotent "check `PRAGMA table_info` first" pattern
as every other additive column in this app.

**2. Synthesis service — `app/services/conclusion_grader.py::
synthesize_ingredient_summary()`.** A *different kind* of operation from
that same file's `process_paper_conclusions()` (Phase 5): where that
function runs once per newly-graded paper and incrementally merges
findings into the running `PaperConclusion` set,
`synthesize_ingredient_summary()` runs once per grade request (called by
`paper_analysis_pipeline.py` after the per-paper loop finishes, not
inside it) and makes exactly one additional Gemini call for the whole
ingredient. Being ingredient-level rather than per-paper, this doesn't
reintroduce the "one huge batched call" problem the per-paper design
deliberately avoids (see that module's docstring) — it's still one
small call, just added once per grade request instead of once per paper.

- **Evidence gathering.** Queries every `ResearchPaper` for the
  ingredient that is both non-discarded
  (`status != PAPER_STATUS_DISCARDED_IRRELEVANT`, Phase 6) and graded
  (`grade IS NOT NULL`) — an ungraded paper has no grade/score to
  usefully include — plus every `VerifiedResource` for the ingredient
  regardless of grade (an ungraded resource is still real official
  guidance worth citing, same "null grade ≠ excluded" convention as
  `VerifiedResourcesList.tsx`). Each paper's "key extracted
  conclusions" (per the task spec) are pulled from the *existing*
  active `PaperConclusion` rows that list it in
  `supporting_paper_ids`/`contradicting_paper_ids` — reusing Phase 5's
  already-synthesized findings rather than re-deriving them from the raw
  abstract a second time.
- **Prompt — `_build_summary_prompt()`.** Follows the task's
  `PROMPT_TEMPLATE` structure: an "EVIDENCE SOURCES PROVIDED" section
  listing papers (title, grade/score, study design from
  `rubric_evaluation.study_type`, and linked conclusions — see
  `_format_papers_for_prompt`) and resources (publisher, title,
  authority grade/score, summary — see `_format_resources_for_prompt`),
  followed by explicit instructions to synthesize a consensus spanning
  both evidence types, note points of agreement/conflict, and produce
  `summary_description`/`main_consensus`/`recommended_uses`.
- **Structured output — `_IngredientSummarySchema`/
  `_RecommendedUseSchema`.** Mirrors the task's JSON schema field-for-
  field: `summary_description: str`, `main_consensus: str`,
  `recommended_uses: [{claim, confidence_grade (A-E, `Literal`-
  constrained), supporting_study_count, supporting_resource_count,
  notes}]`. `supporting_study_count`/`supporting_resource_count` are
  clamped to `>= 0` server-side (never trusted raw from Gemini, same
  "derive/clamp, don't trust" philosophy as every other rubric-based
  grader in this app) — a negative count from a model hallucination
  becomes `0`, not a validation error.
- **Strict zero-evidence handling (per spec).** If there are ZERO
  qualifying papers AND ZERO verified resources, `synthesize_ingredient_summary`
  makes **no Gemini call at all** and returns `None` — there's nothing
  to synthesize, and calling Gemini with two empty evidence sections
  would just invite a fabricated answer. If exactly one collection is
  empty, synthesis still runs (one real evidence source is enough for a
  genuine summary), but the prompt includes an explicit note telling
  Gemini which evidence type is unavailable and instructing it to say so
  plainly rather than inventing the missing source type (study findings
  it never saw, or regulatory guidance that was never fetched).
- **Persistence:** deliberately none, inside this function — it's pure
  (query + one Gemini call + validate/clamp the response), returning an
  `IngredientSummaryResult` for the caller to act on. Only
  `summary_description` is currently written to the DB (see point 3);
  `main_consensus`/`recommended_uses` are returned for observability/
  future use but not persisted anywhere yet — per spec, only
  `summary_description` needed a DB column and API exposure this pass.

**3. Pipeline wiring — `app/services/paper_analysis_pipeline.py::
analyze_ingredient_papers`.** After the existing per-paper grade/
relevance-check/conclusion-synthesis loop finishes, this function now:
fetches the `Ingredient` row (`session.get`), calls
`synthesize_ingredient_summary()` once, and — if it returned a result —
sets `ingredient.summary_description` and commits, all wrapped in the
same "log and skip, never fail the whole grade request" error handling
every other step in this pipeline already uses (`ConclusionGradingError`
is caught, not re-raised). `PipelineResult` gained an
`ingredient_summary_generated: bool` field for observability (mirrors
`papers_conclusions_attempted`/`_failed`).

**4. API exposure — `app/schemas/research.py` / `app/services/search.py`.**
`IngredientDetailResponse` gained `summary_description: Optional[str]`,
populated in `get_ingredient_detail()` straight from the `Ingredient`
row. Not added to `GradeIngredientResponse` — same "frontend re-fetches
ingredient detail to see it" convention `conclusions`/`verified_resources`
already follow, since the Phase 11 step (like Phase 5's conclusion
synthesis and Phase 7's resource lookup) can run as a side effect of a
grade request without that response itself needing to carry the result.

**5. Frontend — `src/components/IngredientCard.tsx`.** The
`scientificSummary` sentence (already rendered under the "Scientific
Information" title as of Phase 9) now has a priority order: (1) the
backend's `summaryDescription` state (seeded from
`ingredient.summary_description`, refreshed by both the initial
`fetchIngredientDetail()` effect and the post-grade follow-up fetch in
`handleGradeRequest`) whenever it's a non-empty string — preferred
since it's strictly richer than anything computed client-side; (2) the
pre-Phase-11 client-computed heuristic ("average grade + top
conclusion") as a fallback whenever the backend hasn't produced one yet
(no grade request has run, the pipeline had zero papers/resources to
synthesize from, or the Phase 11 Gemini call failed). This keeps the
summary sentence from ever going blank while the richer multi-source
version isn't available. `frontend/src/services/api.ts`'s
`IngredientDetailResponse` gained the matching
`summary_description?: string | null` field.

**Verification.** Since this sandbox has no installed `google-genai`/
`pydantic`/`sqlmodel`, correctness was verified with the same offline
stub-module approach used for the Phase 10 resource-fetcher work:
lightweight stand-ins for those packages plus minimal plain-Python
model classes let the *real* `conclusion_grader.py`/
`paper_analysis_pipeline.py` be imported and exercised directly. Checks
covered: prompt-formatting helpers against synthetic paper/resource
data (including the empty-papers/empty-resources text and the
one-collection-empty prompt note); the zero-evidence early return
(asserted no Gemini client is even constructed); a full non-empty
synthesis path with a mocked Gemini response, including
negative-count clamping; an empty `summary_description` from Gemini
correctly raising `ConclusionGradingError`; and the pipeline's tail
wiring — `summary_description` gets persisted onto the `Ingredient` row
and `ingredient_summary_generated` gets set when synthesis succeeds,
both stay untouched when synthesis returns `None`, and a raised
`ConclusionGradingError` is swallowed without propagating. All checks
passed. `python3 -m py_compile` and `tsc --noEmit` both pass on every
changed file.

**Known gaps:**
- `main_consensus`/`recommended_uses` are computed and returned by
  `synthesize_ingredient_summary()` but not persisted or exposed via the
  API yet — only `summary_description` is, per spec. A future pass could
  add DB columns/API fields for these if the frontend wants to render
  them directly (e.g. a richer "Regulatory vs. Literature Consensus"
  panel) rather than folding everything into one sentence.
- `recommended_uses`' `supporting_resource_count` dimension has no
  equivalent on the existing per-claim `PaperConclusion` table — the two
  "recommended uses" concepts (this Gemini call's coarser, ephemeral
  list vs. `PaperConclusion`'s persisted, incrementally-merged one) are
  intentionally not unified this pass.
- Same unauthenticated-endpoint and best-effort-Gemini-call caveats as
  every other synthesis/grading step in this app.

## Global Layout Refactor & Section Standardization (Phase 12)

> **Reverted in part by Phase 13** (see that section below): item 1
> below (hide-on-scroll NavBar, `src/utils/scrollDirection.ts`) was
> fully undone at the requester's follow-up instruction — NavBar is
> always visible again, `scrollDirection.ts` was deleted, and every
> screen's `onScroll`/`reportScrollOffset` wiring was removed. Items 2-5
> below (Footer/flex audit, ResultsScreen's header-scrolls-with-content
> fix, grade-based list sorting, StandaloneInfoSection) were **not**
> reverted and remain exactly as described here — Phase 13 only refined
> item 3's padding, not its ListHeaderComponent structure. This section
> is left intact as a historical record of what Phase 12 built and why;
> read it alongside Phase 13 for the current state.

A frontend-only pass covering three things: a hide-on-scroll NavBar,
confirming/finishing the Footer's already-flex-based bottom-pinning, a
`ResultsScreen` header that scrolls with its content instead of sitting
permanently above it, grade-based sorting on all three Scientific
Information lists, and a shared bordered/collapsible card treatment for
`IngredientCard`'s four top-level standalone sections (including a
rebuilt "Related Products").

**Adapting a web spec to React Native.** The request that drove this
phase was written in web CSS terms — `transform: translateY(-100%)` on a
`position: fixed` NavBar, `position: sticky` headers to remove. This
app is React Native (Expo, react-native-web on web — see the Tech Stack
section), not a DOM app, so those instructions don't translate literally
in every case; each one below notes where and why the implementation
deliberately diverges from the literal CSS instruction while still
delivering the same visual/interactive outcome.

**1. Hide-on-scroll NavBar (`src/utils/scrollDirection.ts` +
`src/components/NavBar.tsx`).** `scrollDirection.ts` is a small
module-level pub/sub store — modeled on `navigation/navigationRef.ts`'s
existing "imperative module, not React Context" convention, for the
same reason that file exists: `NavBar` is rendered once in `App.tsx`
*above* the Stack Navigator, as a sibling to it rather than a descendant
of any one screen, so there's no React tree connecting a screen's own
scroll container down to `NavBar` for Context to flow through. Every
screen's scrollable container (`HomeScreen`/`LibraryScreen`/
`ScanScreen`'s `ScrollView`s, `ResultsScreen`'s `FlatList`) calls
`reportScrollOffset(offsetY)` from its own `onScroll` (throttled to
`scrollEventThrottle={16}`, ~60fps). The module tracks direction with a
small dead-zone (`DIRECTION_CHANGE_THRESHOLD`, 4px) to avoid flicker
from sub-pixel jitter, and always reports "up" (shown) below
`offsetY < 20` regardless of recent direction, matching the spec's "at
the top of the page" rule. `resetScrollDirection()` is called on every
navigation route change (from `NavBar`'s existing route-tracking effect)
so a freshly-opened screen always starts with the bar shown rather than
inheriting whatever scrolled-down state the previous screen left it in.

`NavBar` subscribes and drives one shared `Animated.Value`
(`shownProgress`, 1 = shown, 0 = hidden) via `Animated.timing`
(300ms, `Easing.inOut(Easing.ease)` — matching the spec's `transition:
transform 0.3s ease-in-out`). Two different visual treatments apply
depending on which of NavBar's two existing variants (see Phase pre-9's
"Make NavBar a transparent overlay on Home only") is active:

- **HomeScreen** (`safeAreaHome` — already `position: 'absolute'`,
  floating over the Hero, reserving no layout space): animates
  `transform: translateY` from `0` to `-(measuredHeight + 24)`, paired
  with an opacity fade. This is the one variant where the spec's literal
  `translateY(-100%)` technique is actually correct, since the bar
  doesn't push anything else's layout around.
- **Every other screen** (`safeArea` — normal document flow, pushing
  screen content down by its own height, same as before this phase):
  animates the wrapping `Animated.View`'s own `height` (0 to its
  measured natural height) instead of `translateY`. Translating an
  in-flow element off-screen without shrinking the space it reserves
  would leave a blank gap where it used to be, not the smooth
  "content slides up to fill the space" effect the spec is going for —
  animating height is RN's correct equivalent for an in-flow element,
  without requiring every non-Home screen to switch to
  `position: absolute` and carry a matching top-inset padding (a much
  larger, riskier structural change this phase deliberately avoided).
  `overflow: 'hidden'` on that wrapper is what makes the shrinking
  height actually clip content instead of squashing it.

Both variants' height/measurement come from an `onLayout` callback on
the bar's own content (`onBarLayout`), seeded with a reasonable
`ESTIMATED_BAR_HEIGHT` (64) for the one frame before the real
measurement lands, so there's no visible "jump". The whole animation is
`useNativeDriver: false` — deliberate, not an oversight: the same
`shownProgress` value also drives `height` on non-Home screens, and
`height` is a layout property the native driver can't animate; splitting
drivers per-variant on one shared value risks a native/JS desync, so
everything stays JS-driven.

**2. Footer / flex-column layout (`src/components/Footer.tsx`,
`src/App.tsx`, every screen).** Audited against the spec's "remove
fixed/sticky positioning, wrap the app in a flex column, main content
`flex: 1`" requirement — this was already the case going into this
phase (see Phase pre-9's "LibraryScreen: footer pinning" and
"ScanScreen: center empty state" work): `Footer.tsx` has never used
`position: fixed`/`sticky`/`absolute`; `App.tsx`'s root `View` is
already `flex: 1`; and `HomeScreen`/`LibraryScreen`/`ScanScreen` each
already give their main content container `flexGrow: 1` (paired with
`justifyContent: 'space-between'` or an inner `flex: 1` body) so Footer
naturally lands at the bottom of the viewport on short content and
scrolls normally below it on tall content. One real gap this phase did
fix: `ResultsScreen`'s `FlatList` had no `style` of its own (only a
`contentContainerStyle`), so it wasn't actually claiming the screen's
remaining flex space — added `style={styles.flatList}` (`flex: 1`) so
Footer pins to the bottom there too, matching the other three screens.

**3. `ResultsScreen` header — no longer effectively sticky
(`src/screens/ResultsScreen.tsx`).** The spec asked to remove
`position: sticky`/`top: 0` from the "All Ingredients"/"All Products"
header bars — this codebase has no `pages/IngredientsPage.tsx` or
`pages/ProductsPage.tsx` (this is a Stack-Navigator screens app, not a
multi-page site); `ResultsScreen.tsx` is the one screen that renders
both of those headers, via `getHeaderText()`'s `filterType` branch (see
that function). It never used literal `position: sticky` CSS, but its
back-button/title/filter row *was* a sibling rendered above the
`FlatList` rather than inside it — meaning it never scrolled away with
list content, which is the same practical effect as a sticky header
even without that CSS property. Fixed by moving that whole block into
the `FlatList`'s own `ListHeaderComponent` (`listHeader`, computed once
per render and reused across the loading/error/list branches) so it's
now genuine list content that scrolls with everything else, per the
"headers scroll naturally with page content" requirement.

**4. Grade-based sorting, all three Scientific Information lists
(`src/utils/grades.ts::sortByGradeThenScore`, `StudiesList.tsx`,
`RecommendedUsesList.tsx`, `VerifiedResourcesList.tsx`).**
`StudiesList.tsx` already had a local, paper-only `sortPapersByGrade`
(Phase pre-9's "grade-based sort + tie-break") — generalized into
`grades.ts::sortByGradeThenScore<T>(items, getGrade, getScore)`, a
generic A-to-E-then-score-descending-then-original-order sort usable
across `ResearchPaper` (`grade`/`grade_score`), `PaperConclusion`
(`confidence_grade`/`confidence_score`), and `VerifiedResource`
(`grade`/`score`) via small accessor callbacks, rather than three
near-identical hand-copied comparators. All three lists now call it
immediately before paginating — `RecommendedUsesList` sorts right after
its existing "confidence_grade C or better" filter;
`VerifiedResourcesList` (which has no such filter — every stored
`VerifiedResource` already cleared the domain allow-list at fetch time)
sorts its full list, ungraded resources simply falling to the end via
`UNGRADED_RANK`; `StudiesList` is functionally unchanged, just now
calling the shared helper instead of its own local copy.

**5. Section visual standardization
(`src/components/StandaloneInfoSection.tsx`,
`IngredientCard.tsx`).** New shared component,
`StandaloneInfoSection` — a bordered/collapsible card (`1px solid
#E85D04`, 12px radius, 16px padding, 16px bottom margin; centered bold
20px title; tap-anywhere-on-the-header chevron toggle, `▼`/`▲`) — is
deliberately a *different* component from the pre-existing
`CollapsibleSection.tsx`, not a reskin of it: `CollapsibleSection` is
the smaller, subtler wrapper for the three individual list panels
*inside* Scientific Information (`#E0E0E0` border, left-aligned label,
Ionicons chevron); `StandaloneInfoSection` is the bolder, section-level
chrome those lists — and the other three standalone blocks — all sit
inside. `IngredientCard.tsx`'s standalone body now wraps all four
top-level sections in it: "General Information" and "Grade Info" (still
placeholder body copy, per docs/Architecture.md's pre-existing
"Expandable cards" follow-up — only their outer chrome changed here),
"Scientific Information" (previously its own one-off bordered `View`
with a non-collapsible title — now genuinely collapsible like the other
three, wrapping the same summary sentence + `RecommendedUsesList` +
`VerifiedResourcesList` + `StudiesList` as before), and a rebuilt
"Related Products".

**Related Products rebuild.** Per spec: a summary line ("This ingredient
appears in N products.", using the existing `Ingredient.productCount`
field), and a bordered (`#E0E0E0`, 8px radius) box containing a
5-per-page paginated (`Pagination.tsx`, same component every other list
uses) row list — each row: thumbnail (when available) + name + brand on
the left, a magnifying-glass (`Ionicons name="search"`) placeholder
action button on the right, per spec. **Known gap, stated honestly
rather than faked:** no backend endpoint currently returns *which*
products an ingredient appears in — `GET /api/v1/ingredients/{id}`
exposes only the bare `productCount` number (Ingredient is canonical/
shared M2M data; see the "Many-to-Many refactor" section above), not a
product list. A new `RelatedProduct` type
(`{ id, name, brand?, thumbnailUrl? }`) and an optional
`Ingredient.relatedProducts?: RelatedProduct[]` field were added so the
full pagination/row-layout UI is real and ready to receive data, but
every current caller leaves it `undefined` — the box renders an honest
"Product list not available yet." empty state in that case (distinct
from an empty *array*, which would mean "confirmed zero products" once
a real data source exists) rather than fabricating rows. Wiring an
actual `GET /api/v1/ingredients/{id}/products`-shaped endpoint is
tracked as follow-up work, not attempted in this pass (this phase's
scope was frontend components only — see the section's own
constraints).

**Verification.** `npx tsc --noEmit` passes cleanly across every changed
file (`NavBar.tsx`, `Footer.tsx` — unchanged, `App.tsx` — unchanged,
`HomeScreen.tsx`, `LibraryScreen.tsx`, `ScanScreen.tsx`,
`ResultsScreen.tsx`, `StudiesList.tsx`, `RecommendedUsesList.tsx`,
`VerifiedResourcesList.tsx`, `IngredientCard.tsx`, plus the three new/
extended files: `scrollDirection.ts`, `StandaloneInfoSection.tsx`,
`grades.ts`). No backend files were touched this phase.

## NavBar Revert & Header/List Alignment Fix (Phase 13)

A follow-up correction pass, driven by an explicit request to undo part
of Phase 12 and fix a padding regression it left behind.

**1. NavBar hide-on-scroll — fully reverted
(`src/components/NavBar.tsx`).** Removed everything Phase 12 added: the
`scrollDirection.ts` subscription, the `shownProgress` `Animated.Value`
and its `animateTo`/interpolated `homeTranslateY`/`flowHeight` styles,
`onBarLayout`/`measuredHeight` measurement, and the conditional
`Animated.View` wrapper branching on `isHomeScreen`. `NavBar` now
renders unconditionally — no scroll listeners, no scroll-direction
state, no animated transforms — the same shape it had before Phase 12,
with the `safeArea`/`safeAreaHome` variant styling (unchanged since
before Phase 9's "Make NavBar a transparent overlay on Home only")
being the only thing that varies it by route. `src/utils/scrollDirection.ts`
itself was deleted outright (nothing else in the app used it), and the
`onScroll={(event) => reportScrollOffset(...)}` /
`scrollEventThrottle={16}` wiring was removed from every screen that had
it — `HomeScreen.tsx`, `LibraryScreen.tsx`, `ScanScreen.tsx`'s
`ScrollView`s, and `ResultsScreen.tsx`'s `FlatList`.

The revert request again described the desired end state in web CSS
terms (`position: 'sticky'`, `top: 0`, `zIndex: 1000`). Those don't map
onto this component literally: `NavBar` is rendered once in `App.tsx` as
a sibling *above* `<Stack.Navigator>`, never inside any screen's own
scrolling container, so it was never capable of scrolling out of view on
its default (non-Home) variant in the first place — "always visible" on
that variant falls out of normal document flow alone, no `position`
trick needed. The Home variant already used (and still uses)
`position: 'absolute'` + `zIndex: 100`/`elevation: 100` to float,
permanently, over the Hero video — the closest RN equivalent of the
requested `zIndex: 1000` always-on-top behavior — and this phase left
that variant's positioning untouched, only removing its (now former)
scroll-driven animation.

**2. Footer / flex-column layout — audited, no changes needed
(`src/components/Footer.tsx`, `src/App.tsx`).** Re-checked against the
same "outer container flex column + `minHeight: 100vh`, main content
`flex: 1`, footer in normal document flow" requirement Phase 12 already
satisfied (see that section's item 2): `App.tsx`'s root `View` is
`flex: 1` with a `flex: 1` `stackWrapper` around the Stack Navigator;
`Footer.tsx` has never used `position: fixed`/`sticky`/`absolute`; every
screen's main scrollable content already claims `flex: 1`/`flexGrow: 1`
so Footer naturally sits at the bottom of short content and scrolls
normally below tall content. Nothing was changed here — Phase 12's prior
audit (and the `ResultsScreen` `FlatList` `flex: 1` fix that came out of
it) already covers this; this pass just re-verified it rather than
re-doing it.

**3. `ResultsScreen` header/list padding alignment
(`src/screens/ResultsScreen.tsx`).** This app has no
`pages/IngredientsPage.tsx`/`pages/ProductsPage.tsx` — same mapping
noted in Phase 12 — `ResultsScreen.tsx` is the one screen rendering both
the "All Ingredients" and "All Products" views (see `getHeaderText()`),
so its header is what this fix targets. The misalignment: `body` (the
header container, wrapping the back button and title/filter row) and
`listContent` (the FlatList's `contentContainerStyle`, wrapping the
card grid) both already read the exact same
`layout.screenHorizontalPadding` token (20%) for their horizontal inset
— that part was already correctly shared, not duplicated/hardcoded. The
actual bug was one level deeper: `backButton`'s style carried its own
extra `padding: spacing.xs` (4px) left over from this screen's very
first version, which shifted the back arrow's rendered glyph 4px to the
right of `body`'s own inset — 4px inward of where a card's left border
lines up directly below it. The filter icon (a bare `Ionicons`, no
wrapping padding) had no such offset and was already flush on the
right. Fix: removed `backButton`'s `padding`, keeping `hitSlop={8}` to
preserve a comfortable tap target without shifting the button's visual
position. Left and right edges of the header now line up exactly with
the card grid's edges.

This codebase doesn't use a pixel `maxWidth` cap (e.g. the requested
`1200px`) anywhere — every screen's horizontal inset is the same
percentage-based `layout.screenHorizontalPadding` token (`theme.ts`),
responsive to any viewport width by design (see that file's own
docstring). Introducing a one-off pixel `maxWidth` here would have
diverged from that existing, consistent responsive-layout convention
rather than fixed the actual bug, which was the stray 4px padding above,
not a missing width cap — the header and the list already share the
same responsive width source.

**Verification.** `npx tsc --noEmit` passes cleanly across every changed
file (`NavBar.tsx`, `HomeScreen.tsx`, `LibraryScreen.tsx`,
`ScanScreen.tsx`, `ResultsScreen.tsx`) and the deletion of
`scrollDirection.ts`. No backend files were touched this phase.

## ResultsScreen Header/Footer Layout Fix (Phase 14)

A follow-up bugfix pass — Phase 13's `ListHeaderComponent` restructure
(see that section) fixed the "header doesn't scroll with content" issue
but introduced two new, purely visual regressions of its own, both
reported from a rendered screenshot: the header rendering as a narrow,
centered box next to full-width cards, and the Footer overlapping/
cutting off the last card instead of appearing after it.

**Root cause 1 — double horizontal padding on the header.** Before
Phase 13, `body` (the back-arrow/title/filter row) was a sibling
rendered directly in `screen`'s own flex column, so its own
`paddingHorizontal: layout.screenHorizontalPadding` was the *only*
horizontal inset applied to it. Phase 13 moved that same `body` View to
be the FlatList's `ListHeaderComponent` — which made it a *child* of
`contentContainerStyle` (`listContent`), a container that *also* had
`paddingHorizontal: layout.screenHorizontalPadding` applied for the
cards. The header ended up padded twice (once by its own style, once
again by its new parent), effectively insetting it by double the
intended margin on each side — exactly the "narrow, centered box"
regression. The cards (rendered via `renderItem`, siblings of
`ListHeaderComponent` within the same content container) only ever had
the one, correct level of padding, so they stayed full width relative
to the header.

**Fix 1.** Moved `paddingHorizontal` off `listContent`
(`contentContainerStyle`) entirely and onto a new per-item wrapper,
`itemWrapper`, applied inside `renderItem` around each `ProductCard`/
`IngredientCard`. `body` keeps its own `paddingHorizontal` unchanged —
now the *only* source of inset for the header, same single-source
guarantee `itemWrapper` gives each card. Both read the exact same
`layout.screenHorizontalPadding` token, so the header's back
arrow/filter icon and every card's left/right border land on identical
pixel edges, with no hardcoded `maxWidth` needed to keep two separately-
maintained widths in sync.

**Root cause 2 — Footer overlapping the last card.** Before this pass,
`<Footer />` was rendered as a plain sibling *after* the FlatList inside
`screen`'s flex column, with the FlatList itself given `style={{ flex: 1
}}` (added in Phase 12 to make Footer pin to the viewport bottom on
short result lists). That combination — a `flex: 1` FlatList sitting
next to a separate, non-flex Footer sibling — creates two independently-
sized layout regions: the FlatList becomes its own bounded scrolling
viewport (sized to whatever's left after Footer's natural height is
subtracted), scrolling its row content *inside* that bounded box, while
Footer sits fixed immediately after it in the outer flex column. Rather
than behaving like a normal trailing element in one continuous page
scroll, this let Footer end up visually anchored at the bottom of the
screen's remaining space regardless of how far the list itself had been
scrolled — overlapping or crowding out the last card instead of only
appearing once the user scrolls past it.

**Fix 2.** `<Footer />` is now rendered as the FlatList's own
`ListFooterComponent` — genuine list content, the same content-container
child every card already is, rather than a sibling with its own
independent flex sizing. This guarantees Footer is always the true last
row of the *one* scrollable region containing the header, every card,
and Footer together — it becomes visible if and only if the user has
actually scrolled past everything above it, with no separate bounded
viewport for it to get pinned against. The `flex: 1`/`contentContainerStyle`
`flexGrow: 1` combination from Phase 12 is kept (now describing the
FlatList's own single scroll region, not a Footer-pinning trick) so a
short results list still stretches to fill the screen and Footer still
visually anchors at the bottom in that case, exactly as before — only
the tall-list, "scroll past the last card to see it" case actually
needed fixing.

Making Footer a child of `contentContainerStyle` reintroduces the
question Fix 1 already resolved: wouldn't it now also inherit
`listContent`'s horizontal padding, breaking its established full-bleed
(edge-to-edge) width — the same rule NavBar/Footer follow on every other
screen (see `theme.ts`'s `layout.screenHorizontalPadding` docstring)?
This is exactly why Fix 1 moved padding off `listContent` and onto
`itemWrapper`/`body` individually instead of leaving it on the shared
container — with `listContent` itself unpadded, `Footer` (now a direct,
unwrapped child of it) renders full width automatically, no negative-
margin cancellation trick required. The two fixes are deliberately the
same change (stop padding the shared container, pad each piece that
actually needs it) applied for two different reasons.

The loading and error branches (no `FlatList` rendered in either case)
keep `<Footer />` as a plain trailing sibling after their centered
spinner/error message — there's no competing `flex: 1` element in those
branches, so the original overlap bug never applied to them; each
branch now renders its own `<Footer />` rather than sharing one
common one after the whole loading/error/list conditional, since that
shared position no longer exists now that the list branch's Footer
lives inside the FlatList instead.

**Verification.** `npx tsc --noEmit` passes cleanly. No backend files
were touched this phase.

## Collapsible Section Header Chevron Fix (Phase 15)

Small follow-up bugfix: in the four `StandaloneInfoSection`-wrapped
headers ("General Information", "Grade Info", "Scientific Information",
"Related Products" — see Phase 12), the `▼`/`▲` chevron was rendering on
its own line *below* the centered title instead of beside it. Root
cause: `StandaloneInfoSection.tsx`'s `headerRow` style set `alignItems`
and `gap` but never set `flexDirection: 'row'` — and unlike web
Flexbox, React Native's own default `flexDirection` is `'column'`, not
`'row'`. The title `Text` and the chevron `Text` were therefore stacking
vertically as two rows, not sitting side by side as one.

Fixed in `StandaloneInfoSection.tsx` (not `IngredientCard.tsx` itself —
that file only *uses* this shared component, the header markup and
styling live here) by adding `flexDirection: 'row'` and
`justifyContent: 'center'` to `headerRow`. `justifyContent: 'center'`
(rather than `space-between`) keeps the title itself centered per the
original spec, with the chevron sitting immediately to its right as
part of the same centered (title + chevron) unit, rather than snapping
to the container's far edge.

The three inner list headers this request also named — "Recommended
Uses List", "Verified Online Resources List", "List of Studies" — use
the separate `CollapsibleSection.tsx` wrapper (see Phase 9), whose
`headerRow` already had `flexDirection: 'row'` (paired with
`justifyContent: 'space-between'`, title left-aligned/`flex: 1`, chevron
pinned right) — those were never affected by this bug and needed no
change.

**Verification.** `npx tsc --noEmit` passes cleanly. No backend files
were touched this phase.

## Verified-Resource Data-Flow Audit (Phase 16)

Triggered by a report that ingredient summaries/recommendations were
"ignoring Verified Online Resources." Audited the three files named in
the request — `paper_analysis_pipeline.py`, `conclusion_grader.py`,
`resource_fetcher.py` — plus their call site, `grading.py`.

**Finding: the described bug does not reproduce in the current code.**
`grading.py::grade_ingredient` already runs
`fetch_verified_resources_for_ingredient` and commits *before* calling
`analyze_ingredient_papers` (which is what eventually calls
`synthesize_ingredient_summary`) — so by the time that synthesis query
runs, any newly-fetched `VerifiedResource` rows are already durably
committed, not just sitting in an uncommitted, possibly-stale in-memory
collection. `conclusion_grader.py`'s prompt builder
(`_build_summary_prompt`) already formats `resources` into readable
title/publisher/summary blocks and includes them in the Gemini prompt
alongside the papers. Execution order and prompt payload were both
already correct.

**Concrete fixes made anyway, since they're real, valuable gaps
independent of whether the reported bug reproduces:**

1. **ORM relationship parity (`app/models/supplement.py`,
   `app/models/research.py`).** `Ingredient` had a `papers` relationship
   to `ResearchPaper` but no equivalent relationship to
   `VerifiedResource` — the only way to reach a `VerifiedResource` row
   from code was a manual `select(...).where(ingredient_id == ...)`
   query. Added `Ingredient.verified_resources` (back-populated by a new
   `VerifiedResource.ingredient`), mirroring the existing `papers`
   relationship. Deliberately **not** `lazy="selectin"` — every actual
   read path in this codebase (`synthesize_ingredient_summary`,
   `search.py::get_ingredient_resources`) already queries
   `VerifiedResource` directly rather than via ORM lazy-loading, by
   design (see those functions' own docstrings: a direct query in the
   same session always sees rows already `flush()`ed earlier in that
   request, with no stale-collection risk) — this relationship is
   additive/for-parity, not something existing code needed to start
   using.
2. **Debug logging (`conclusion_grader.py`, `resource_fetcher.py`,
   `paper_analysis_pipeline.py`).** None of the three files had any
   logging around the papers/resources counts actually reaching
   synthesis. Added `logger.info` lines (not `logger.debug` — this
   process's default config may not surface DEBUG records, and the
   whole point is visibility without a logging-config change first;
   not bare `print()` — matches this codebase's established
   `logging`-module convention) at three points: inside
   `resource_fetcher.py`, reporting each ingredient's total verified
   resource count after every fetch; inside
   `paper_analysis_pipeline.py::analyze_ingredient_papers`, immediately
   before the `synthesize_ingredient_summary` call, reporting exactly
   how many papers and resources are about to feed into it; inside
   `conclusion_grader.py::synthesize_ingredient_summary` itself,
   reporting papers count, resources count, and the fully-formatted
   resources payload being sent to Gemini. Together these make it
   possible to confirm from server logs alone, at every hop, that
   `VerifiedResource` rows are actually flowing through — rather than
   needing to re-read code to check.
3. **Strengthened prompt instruction (`conclusion_grader.py`).** Added
   an explicit instruction as the first bullet of
   `_build_summary_prompt`'s INSTRUCTIONS section: Gemini **must** cite
   and incorporate the VERIFIED ONLINE RESOURCES section alongside the
   papers, not synthesize from papers alone when resources are present,
   and must reflect official agency guidance (NIH/EFSA/USDA/Health
   Canada) directly in `main_consensus`/`recommended_uses` when present
   among the resources. This doesn't change what data reaches the
   prompt (it was already there) but makes the model less likely to
   underweight it.

**Real gap identified, NOT fixed this phase — flagged for a scope
decision.** The frontend's "Recommended Uses List"
(`RecommendedUsesList.tsx`) is sourced entirely from the `PaperConclusion`
table (`process_paper_conclusions`, Phase 5) — a paper-only pipeline that
never reads `VerifiedResource` at all. Meanwhile,
`synthesize_ingredient_summary()`'s richer `main_consensus` and
`recommended_uses` fields — the ones actually built from both papers
*and* resources together — are computed but never persisted anywhere
(only `summary_description`, a short prose sentence, is saved onto
`Ingredient`). This mismatch is the most likely real source of "ignoring
Verified Online Resources": not a broken data pipeline, but a
recommendations UI that was never wired to the multi-source synthesis
output in the first place. Fixing it would require a DB migration (new
columns to persist `main_consensus`/`recommended_uses`), an API schema
change, and a frontend change to either add a new section or replace
`RecommendedUsesList`'s data source — outside the three files this task
authorized, so left as a follow-up rather than silently expanded into.

**Verification.** `python3 -m py_compile` on all five touched files
(`models/research.py`, `models/supplement.py`,
`services/conclusion_grader.py`, `services/resource_fetcher.py`,
`services/paper_analysis_pipeline.py`) passes cleanly. No frontend files
touched this phase.

## Two-Stage Resource Extraction Pipeline (Phase 17)

Follow-up to the Phase 16 audit. That audit found the reported bug (
"synthesis ignores Verified Online Resources") didn't reproduce
mechanically — execution order and prompt payload were already correct
— but didn't explain why resources still felt under-represented in
practice. This phase addresses the actual root cause: a
**lost-in-the-middle effect**. Mixing dense, information-rich paper
abstracts and short, thin web-resource snippets into one single
synthesis prompt caused Gemini to consistently over-weight the papers,
not because resource text was missing from the prompt, but because it
was out-matched, evidence-density-wise, by the papers sitting next to
it.

**The fix: split synthesis into two stages.**

- **Stage 1 — per-resource extraction (new:
  `backend/app/services/resource_extractor.py`).** A new, focused Gemini
  service, `extract_claims_from_resource(resource_title, publisher,
  snippet_or_text)`, mirrors `resource_grader.py`'s existing
  structured-output pattern (cached client, `response_schema`, `.parsed`
  with raw-text fallback) and distills one resource's own
  title/publisher/summary into `{official_stance, recommended_dose,
  upper_limit_warning, key_takeaways}`. **Short-snippet guard:** if the
  resource's summary is under 20 characters (or missing — many source
  APIs don't provide one at all), no Gemini call is made; a real,
  non-`None` result with every field empty is returned instead, logged
  at `info` — calling Gemini on a handful of words risks it fabricating
  a plausible-sounding dose the source never actually stated, which is
  worse than honestly recording "nothing extractable."
  **Deliberate deviation from the task's literal `async def` signature:**
  implemented as a synchronous function instead, matching every other
  Gemini-calling service in this codebase (`paper_grader.py`,
  `resource_grader.py`, `conclusion_grader.py`) — its caller is itself a
  plain sync function always run inside a `run_in_threadpool` worker
  thread, so an `async def` here would wrap a call that's never actually
  awaited; see that module's own docstring for the full reasoning.
- **Stage 2 — unified synthesis (`conclusion_grader.py`, existing
  function, prompt rewritten).** `synthesize_ingredient_summary()`'s
  Gemini call is unchanged in cadence/output shape (still one call per
  grade request, still the same `_IngredientSummarySchema`), but its
  prompt now presents two clearly-labeled, comparably-dense blocks —
  `--- SOURCE 1: OFFICIAL REGULATORY & HEALTH AGENCY STANDS ---`
  (resources, rendered from each one's Stage 1 `extracted_data` — falls
  back to the old raw-summary rendering only for a resource Stage 1
  hasn't reached yet) followed by `--- SOURCE 2: PEER-REVIEWED
  SCIENTIFIC PAPERS ---` — rather than one continuous, uneven-density
  wall of text. Resources are deliberately listed first: with Stage 1
  now making both blocks comparably compact, order shouldn't matter much
  on its own, but leading with the previously-crowded-out source is a
  small additional nudge.

**Orchestration (`paper_analysis_pipeline.py`).** Stage 1 runs inside
`analyze_ingredient_papers()`, after the per-paper grade/conclusion loop
and immediately before the Stage 2 synthesis call: every `VerifiedResource`
currently stored for the ingredient is queried, and any one still
missing `extracted_data` gets `extract_claims_from_resource()` called
and its result persisted directly onto that row, committed once as a
batch. Because this queries *every* stored resource (not just ones found
by this specific grade request), a resource fetched before this feature
existed gets backfilled automatically the next time its ingredient is
re-graded — no separate migration/backfill script needed. A resource
that already has `extracted_data` is skipped (never re-extracted, same
"doesn't change once assigned" convention as `grade`/`score`). A Stage 1
failure for one resource is logged and skipped — that resource's
`extracted_data` stays `None` and Stage 2 falls back to its raw summary
for that one entry, never blocking Stage 2 from running with whatever
IS available.

**Schema (`models/research.py` / `db.py`).** `VerifiedResource` gained
`extracted_data: Optional[dict]` (native JSON column, same pattern as
`ResearchPaper.rubric_evaluation`) — additive on a table that already
existed in deployed (Phase 7/8) databases, so `db.py::
_migrate_verified_resource_columns` gained a fourth column
(`extracted_data JSON`) alongside `grade`/`score`/`reasoning_summary`.
`None` is a normal, expected state for two distinct reasons a caller
must not conflate: not yet extraction-attempted, or extraction attempted
but genuinely nothing to report — the second case is stored as a real
dict with every field null/empty, not a bare `None`, so it's
distinguishable if that ever matters.

**Verification.** `python3 -m py_compile` on all five touched/new files
(`models/research.py`, `db.py`, `services/resource_extractor.py`,
`services/paper_analysis_pipeline.py`, `services/conclusion_grader.py`)
passes cleanly. No frontend files touched this phase.

## Gemini Rate Limiting & Execution Reordering (Phase 18)

Follow-up to a reported `429 ResourceExhausted` problem during paper
grading: under sustained traffic, a burst of newly-found papers graded
back-to-back with no spacing could all land in the same one-minute quota
window and start failing together, effectively starving the later steps
(Stage 1 resource extraction, Stage 2 synthesis) of any remaining quota.

**Corrected exception type — a real bug in the task's own reference
code.** The task's reference implementation imports
`google.api_core.exceptions.ResourceExhausted`. This backend's Gemini
dependency is `google-genai` (`from google import genai` —
`backend/requirements.txt`: `google-genai>=1.0,<2.0`), the newer unified
Gen AI SDK — not `google-generativeai`/Vertex AI, which are what
actually raise `ResourceExhausted`. Verified against the `google-genai`
SDK's own source/issue tracker: a 429 quota error there surfaces as
`google.genai.errors.ClientError` (a subclass of `APIError`, carrying a
`.code: int` and `.status: Optional[str] == "RESOURCE_EXHAUSTED"`).
Using the task's literal import would have silently defeated the whole
feature — that `except` clause would never match anything `google-genai`
actually raises, and every 429 would fall straight through unretried.
`backend/app/services/gemini_rate_limit.py` (new) checks for the correct
type.

**New shared module: `backend/app/services/gemini_rate_limit.py`.**
Deliberately factored out as its own small module — not duplicated
inside both `paper_grader.py` and `resource_extractor.py` (the two files
this task named for the change) — since retry/backoff pacing logic is
pure infrastructure, identical no matter which Gemini call it wraps,
unlike this codebase's established "small graders may duplicate their
own rubric-loading logic until a third one shows up" convention (see
`conclusion_grader.py`'s docstring), which is about domain-specific
scoring logic, not generic retry plumbing. Two functions:

- **`throttle_gemini_call()`** — called immediately before every
  `client.models.generate_content(...)` in `paper_grader.py` and
  `resource_extractor.py`. Enforces a minimum ~4.5s gap since the *last*
  Gemini call made anywhere in this process, via one lock-guarded,
  process-wide timestamp — not a per-loop local sleep. This is stricter
  (and more correct) than the task's literal "sleep 4.5s in this one
  loop": a per-loop sleep does nothing to stop two concurrent grade
  requests from each independently pacing to 13 RPM while jointly still
  exceeding the actual account-wide limit; a single shared timestamp
  bounds the real aggregate rate regardless of how many requests are in
  flight (same "fine for a single-process dev/prototype app, revisit
  under real concurrent load" caveat as `app/db.py`'s SQLite locking
  discussion).
- **`call_gemini_with_retry(prompt_func, max_retries=4, label=...)`** —
  wraps one Gemini call with exponential backoff (5s/10s/20s/40s)
  specifically on a 429/`RESOURCE_EXHAUSTED` response; every other
  exception propagates immediately, unretried. Small refinement over the
  task's literal reference: does NOT sleep again after a final,
  still-failing attempt — sleeping 40s only to immediately give up
  afterward would be pure wasted latency.

**Deliberate deviation: synchronous, not `async def`.** Same reasoning
already applied to `resource_extractor.py` in Phase 17 — every
Gemini-calling service in this codebase makes a blocking, synchronous
call from a plain sync function always run inside a `run_in_threadpool`
worker thread, never from an `async def` caller; an async retry wrapper
around a call that's never actually awaited would be a false-async
signature. `time.sleep()` blocks the worker thread the same way
`await asyncio.sleep()` would block an event-loop task.

**Execution reordering (`paper_analysis_pipeline.py`).** Stage 1
resource-claims extraction now runs **before** the per-paper grading
loop (previously ran after it). Verified-resource *fetching* itself
(`resource_fetcher.py`, including its own Phase 8 `grade_resource()`
calls) already ran before `analyze_ingredient_papers()` even started —
see the Phase 16 audit — but Stage 1 *extraction* (Phase 17, inside this
function) was still sequenced after paper grading, meaning a quota
exhausted by a large batch of papers left nothing for it. Reordering
gives Stage 1 first claim on whatever quota is available each run.

**`MAX_PAPERS_PER_GRADED_INGREDIENT = 6`.** Caps how many papers one
`analyze_ingredient_papers()` run actually grades/processes, regardless
of how many are stored. Papers are ranked by `_paper_relevance_sort_key`
before the cap applies — a two-tier heuristic: already-graded papers
first (ordered by real `grade_score`, since re-processing them costs no
Gemini call), then ungraded papers ordered by how many distinct
Gemini-generated search keywords matched them (a rough pre-grading
relevance proxy — no per-paper relevance score exists before grading
happens). **Known, documented trade-off:** since the same top-ranked
papers win every run, a lower-ranked paper can in principle never get
processed if 6+ higher-ranked ones persist for the same ingredient —
accepted as a deliberate simplicity trade-off for this debug-stage
feature, not silently overlooked.

**Known gap, out of scope this phase.** `conclusion_grader.py`'s own two
Gemini calls (`process_paper_conclusions`, `synthesize_ingredient_summary`)
are NOT wired through `gemini_rate_limit.py` — that file wasn't part of
this task's authorized 3-file scope (`paper_analysis_pipeline.py`,
`paper_grader.py`, `resource_extractor.py`). A rate-limit hit there still
surfaces as an ordinary, unretried `ConclusionGradingError`, caught and
logged exactly as before — never a hard crash, but also never
retried/backed-off.

**Verification.** `python3 -m py_compile` on all four touched/new files
(`services/gemini_rate_limit.py`, `services/paper_grader.py`,
`services/resource_extractor.py`, `services/paper_analysis_pipeline.py`)
passes cleanly. No frontend files touched this phase.

## Extracted Conclusions for Papers & Verified Resources (Phase 19)

Adds a short, factual `extracted_conclusions: List[str]` (2-4 items) to
both `ResearchPaper` and `VerifiedResource`, rendered under a new
"Extracted Conclusions" heading in each item's "(i)" info modal.

**Where the info modals actually live.** The task's own description
pointed at `src/components/IngredientCard.tsx` for this UI. That file is
a pure passthrough — it hands `papers`/`verified_resources` straight to
`StudiesList`/`VerifiedResourcesList` and owns no `Modal` markup of its
own (confirmed by grep: zero matches for `Modal`/`infoModal` in that
file). The two actual `activeInfoModalItem` `Modal` implementations live
in `StudiesList.tsx` (papers) and `VerifiedResourcesList.tsx` (verified
resources) — this is where the new section was actually added.
`IngredientCard.tsx` itself only got a doc-comment clarifying this, since
the new field flows through its existing prop passthrough with no
transformation code needed.

**Deliberate deviation: one Gemini call per item, not two.** The task's
reference prompt template sketched `extracted_conclusions` as its own
dedicated Gemini call, separate from existing extraction/grading. Both
`paper_grader.py::grade_paper()` and
`resource_extractor.py::extract_claims_from_resource()` instead fold it
into their SAME existing structured-output call (one extra
`extracted_conclusions: List[str]` field on each call's response schema,
one extra prompt paragraph) rather than issuing a second Gemini request
per paper/resource. Doubling Gemini calls here would work directly
against Phase 18's rate-limiting pass (`gemini_rate_limit.py`), which
exists specifically to reduce how many Gemini requests this pipeline
makes per grade run. Both cap the model's own list length server-side
(`extracted_conclusions[:4]`) rather than trusting Gemini to honor the
stated "2 to 4" bound — same philosophy as every rubric score clamp and
`key_takeaways[:3]` elsewhere in this codebase.

**Source-specific extraction (`resource_extractor.py`).** Every entry in
`docs/verified_resource_apis.json` already had a populated
`extraction_instructions` string (no config changes needed this phase).
A new `_find_extraction_instructions(domain)` helper matches a
`VerifiedResource.domain` against each config entry's `domain` by suffix
(same convention as `resource_fetcher.py::_is_verified_domain`, since a
resource's real host can be a subdomain of the configured one, e.g.
`connect.medlineplus.gov` vs. `medlineplus.gov`) and folds the matched
provider's instructions into `_build_prompt()`, so Gemini extracts
conclusions following that specific provider's own guidance (e.g.
PubChem's "focus on biological role... and documented toxicity/safety
baseline" vs. DailyMed's "focus on INDICATIONS & USAGE/DOSAGE AND
ADMINISTRATION/WARNINGS"). The config file is read fresh on every call
via a new, non-`@lru_cache`d `_load_resource_api_configs()` — matching
`resource_fetcher.py`'s own established "config edits take effect
without a restart" convention for this same file — rather than reusing
`resource_fetcher.py`'s private loader, since this JSON is shared config,
not owned by either module.

**`extracted_data` vs. `extracted_conclusions` on `VerifiedResource`.**
Both come out of the same Stage 1 Gemini call but are stored in two
separate columns. `extracted_data` keeps its pre-existing four-field
shape (`official_stance`/`recommended_dose`/`upper_limit_warning`/
`key_takeaways`) unchanged, since Stage 2 synthesis
(`conclusion_grader.py::_format_resources_for_prompt`) reads those four
keys directly and would need reshaping if a fifth, list-shaped key were
merged in. `extracted_conclusions` is a separate, flat, display-only list
purpose-built for the frontend info modal — `paper_analysis_pipeline.py`
splits the single dict `extract_claims_from_resource()` returns into
`resource.extracted_data` (the original four fields) and
`resource.extracted_conclusions` (the new list) before committing.

**Paper grading (`paper_grader.py`).** `grade_paper()`'s existing
structured-output call (already producing `grade`/`grade_score`/
`rubric_evaluation`/relevance) now also returns `extracted_conclusions` —
2-4 short findings the paper's own title/abstract actually states (a
measured effect size, a tolerability/safety finding, a studied dosage),
explicitly instructed to return an empty list rather than fabricate
anything when the abstract is missing or too sparse. Persisted onto
`ResearchPaper.extracted_conclusions` by `grade_single_paper()`.

**API exposure.** `extracted_conclusions: Optional[List[str]] = None`
added to both `ResearchPaperResponse` and `VerifiedResourceResponse`
(`backend/app/schemas/research.py`); `search.py`'s
`to_research_paper_response()` and `get_ingredient_resources()` both pass
the ORM column straight through with no reshaping.

**Frontend.** `ResearchPaper`/`VerifiedResource` (`src/services/api.ts`)
each gained an `extracted_conclusions?: string[] | null` field.
`StudiesList.tsx`'s paper info modal gained an "Extracted Conclusions"
section (bulleted, between the abstract and the "View Source" button);
`VerifiedResourcesList.tsx`'s resource info modal gained the same section
as its new last `modalSection` (after Summary). Both render the required
"No specific conclusions extracted for this source yet." fallback text
when the field is null/empty, rather than hiding the section — an honest
signal that extraction/grading hasn't produced a result yet, not an
error state.

**Migrations.** `extracted_conclusions` (`JSON`, nullable) added to both
`_RESEARCH_PAPER_COLUMNS` and `_VERIFIED_RESOURCE_COLUMNS` in
`backend/app/db.py`, applied via the existing idempotent
`ALTER TABLE ... ADD COLUMN` migration helpers — no new migration
mechanism needed.

**Verification.** `python3 -m py_compile` on every touched backend file
(`models/research.py`, `db.py`, `services/paper_grader.py`,
`services/resource_extractor.py`, `services/paper_analysis_pipeline.py`,
`schemas/research.py`, `services/search.py`) and `npx tsc --noEmit` on
the frontend (`services/api.ts`, `components/StudiesList.tsx`,
`components/VerifiedResourcesList.tsx`, `components/IngredientCard.tsx`)
both pass cleanly.

## Frontend Structure

```
frontend/
├── App.tsx                     # Thin re-export of src/App.tsx (keeps index.ts's import path stable)
├── index.ts                    # Expo entry point, registers App as the root component
└── src/
    ├── App.tsx                 # Root: SafeAreaProvider + NavigationContainer + persistent NavBar + Stack.Navigator
    ├── theme.ts                 # Strict color palette, typography, spacing, layout (20% screen inset) tokens
    ├── assets/
    │   ├── 7685212-hd_1920_1080_24fps.mp4    # Hero background video (HD, used by default)
    │   ├── 7710495-uhd_4096_2160_25fps.mp4   # Hero background video (UHD alt — larger file, not used by default)
    │   ├── products.png                       # LibraryScreen "Products" explore card background
    │   └── ingredients.png                    # LibraryScreen "Ingredients" explore card background
    ├── navigation/
    │   ├── types.ts             # RootStackParamList (Home/Scan/Library/ResultsScreen), FilterType
    │   └── navigationRef.ts     # Imperative nav ref, used by NavBar (which renders outside the Stack tree)
    ├── components/
    │   ├── NavBar.tsx            # Persistent top bar: "BSProof" logo (-> Home), Scan / Library links, debug Reset DB; always visible, no scroll-driven show/hide (Phase 12 added hide-on-scroll, reverted Phase 13)
    │   ├── Footer.tsx             # Persistent footer, reused on every screen — normal document flow (never fixed/sticky), pinned to viewport bottom via each screen's own flex layout (Phase 12)
    │   ├── ImageUploader.tsx     # Upload button + image preview (styled to palette)
    │   ├── ProductCard.tsx       # Expandable product card (metadata + nested Ingredient accordion + grade badge + orange-on-expand text)
    │   ├── IngredientCard.tsx    # Accordion card, two variants: 'nested' (dosage/%DV/research) and 'standalone' (grade badge + General Information/Grade Info/Scientific Information/Related Products, each its own collapsible StandaloneInfoSection card — Phase 12)
    │   ├── StandaloneInfoSection.tsx  # Shared bordered/collapsible card wrapper (#E85D04 border, centered bold title, chevron toggle) for IngredientCard's four top-level standalone sections (Phase 12)
    │   ├── CollapsibleSection.tsx     # Shared collapsible/bordered list wrapper (chevron toggle, #E0E0E0 border) used by StudiesList/RecommendedUsesList/VerifiedResourcesList (Phase 9)
    │   ├── GradeCircleBadge.tsx       # Shared round A-E letter-grade badge (row + modal-header variants) used by all three lists (Phase 9)
    │   ├── ExternalLinkIconButton.tsx # Shared "🌐" open-in-new-tab row action used by StudiesList/VerifiedResourcesList (Phase 9)
    │   ├── StudiesList.tsx       # Paginated (5/page) "List of Studies (Total: N)" panel — ResearchPaper rows, rubric + info modals, external-link button (Phase 2, unified Phase 9)
    │   ├── RecommendedUsesList.tsx    # "Recommended Uses List" — paginated (5/page) PaperConclusion list, graded C+, sorted A->E then score before paginating (Phase 12), rubric + info modals (Phase 5, unified Phase 9)
    │   ├── VerifiedResourcesList.tsx  # "Verified Online Resources" — official government/regulatory reference links (Phase 7), grade/score badges (Phase 8), sorted A->E then score before paginating (Phase 12), paginated (5/page) with rubric + info modals (Phase 9)
    │   ├── StudiesAnalysisBar.tsx     # UNUSED as of Phase 9 (no longer imported by IngredientCard.tsx) — its total-count/average-grade metrics moved into StudiesList's title bar and IngredientCard's summary sentence, respectively; left in place, not deleted
    │   ├── Pagination.tsx        # Shared prev/next + page indicator, used by all three Scientific Information lists
    │   └── GradeBadge.tsx        # Shared top-right grade pill/button (graded vs. ungraded), used by ProductCard + standalone IngredientCard
    ├── screens/
    │   ├── HomeScreen.tsx        # Marketing hero (full-width, looping video background via expo-video) + "Why BSProof?" info section (20% inset) + Footer
    │   ├── ScanScreen.tsx        # ImageUploader + Analyze button + raw-JSON Results section + Footer
    │   ├── LibraryScreen.tsx     # Search (live suggestions) + Explore (Products/Ingredients cards) + Footer
    │   └── ResultsScreen.tsx     # Back button + title/filter row is the FlatList's own ListHeaderComponent (scrolls with content, not sticky — Phase 12); header's horizontal inset shares `layout.screenHorizontalPadding` with the card list below it, flush left/right (Phase 13), ProductCard/IngredientCard list, + Footer
    ├── services/
    │   └── api.ts                # API_BASE_URL, uploadSupplementImage(), fetchSuggestions(), searchSupplements(), fetchProductDetail(), fetchIngredientDetail(), gradeIngredient(), resetDatabase()
    └── utils/
        ├── animations.ts          # animateCardToggle() — shared LayoutAnimation helper for accordion cards
        └── grades.ts               # GRADE_COLORS, GRADE_RANK, getGradeRank(), isPaperGrade(), sortByGradeThenScore() (Phase 12) — shared grade-letter helpers (Phase 5)
```

### Navigation

`src/App.tsx` renders `NavBar` as a persistent sibling above
`<Stack.Navigator>` (native stack, headers disabled) inside a single
`NavigationContainer`, so the bar stays mounted across every screen rather
than being redefined per-screen. Because `NavBar` sits outside the
navigator's own screen tree, it can't use the `navigation` prop or
`useNavigation()` hook the way in-stack screens (Home/Scan/Library) can —
it navigates via the imperative `navigationRef` / `navigateTo()` helper in
`src/navigation/navigationRef.ts` instead (the documented pattern for
"navigating without the navigation prop").

**Transparent overlay on Home:** for the same reason (`NavBar` has no
Navigator context), it can't use `useRoute()`/`useNavigationState()`
either to know which screen is active — it tracks this itself via
`navigationRef.addListener('state', ...)` + `navigationRef.getCurrentRoute()`,
defaulting to `'HomeScreen'` (matching the Stack's `initialRouteName`) so
the first render is already correct before the container reports ready.
When the active route is `'HomeScreen'`, `NavBar`'s outer `SafeAreaView`
switches to `position: 'absolute'` (`top/left/right: 0`, `zIndex: 100`,
`elevation: 100` for Android) with a semi-transparent `rgba(53, 90, 53,
0.8)` background (dark green @ 80%), floating over the top of the Hero
instead of pushing it down — removing it from normal flow is what lets
Hero's `minHeight: windowHeight` fill the true full viewport behind it,
with no HomeScreen-side layout change needed. On every other screen,
`NavBar` renders in its original normal-flow, fully opaque form.

### Color Palette (`src/theme.ts`)

All seven palette colors (brown `#8C3703`, orange `#E85D04`, yellow
`#FFBA08`, light yellow `#FBD569`, off-white `#F7EFCA`, olive `#899536`,
dark green `#355A35`) are centralized in `theme.ts`; components import
`colors` from there rather than hardcoding hex values. Disabled/muted UI
states use `opacity` on an existing palette color rather than introducing a
new gray, to stay strictly within the mapping.

### Global layout padding

`theme.ts` exports `layout.screenHorizontalPadding = '20%'`, applied as
`paddingHorizontal` to each screen's main body container: HomeScreen's
info section, ScanScreen's `body`, LibraryScreen's `body`, and
ResultsScreen's header + `FlatList` content. `NavBar`, `Footer`, and
HomeScreen's Hero are explicitly exempt and stay full-width — Hero's own
small internal padding (for breathing room around its title/buttons) is
separate from this global inset and unaffected by it. Because 20% is
taken off *each* side, note this significantly narrows content on phone-
width screens (roughly 60% of screen width remains) — worth revisiting if
it reads as too cramped on real devices.

Vertical rhythm was tightened up in a later pass: `HomeScreen`'s info
section, `ScanScreen`'s body, `LibraryScreen`'s body, and `ResultsScreen`'s
header/`FlatList` content all use `paddingVertical: spacing.xl` (32) on
their main container, with inter-section/inter-card gaps bumped to
`spacing.md`/`spacing.xl` depending on context — up from the original
`spacing.lg`/`spacing.sm` values. `NavBar` and `Footer` are explicitly
excluded, same as the horizontal rule above.

### Hero video background (`HomeScreen.tsx`)

The Hero section's background is a looping, muted local video instead of
a flat color, via **`expo-video`** — not `expo-av`: `expo-av`'s Video/Audio
APIs are deprecated (no further patches, removed entirely in SDK 55) and
this project is on SDK 57, so `expo-video` is the only supported option.
Install it manually:

```bash
cd frontend
npx expo install expo-video
```

Implementation:
- `useVideoPlayer(HERO_VIDEO, setup)` creates the player; the setup
  callback sets `loop = true`, `muted = true`, `volume = 0`. **`play()` is
  deliberately called from a separate `useEffect(() => player.play(),
  [player])` afterward, not inside the setup callback.** On web, the
  underlying `<video>` element isn't attached to the DOM yet when the
  setup callback runs, so calling `play()` there silently no-ops — you
  get a static poster frame instead of playback (a known `expo-video` web
  issue, [expo/expo#36350](https://github.com/expo/expo/issues/36350)).
  Deferring to a post-mount effect is the workaround; native (iOS/Android)
  plays either way.
- `<VideoView player={player} contentFit="cover" nativeControls={false} />`
  is absolutely positioned (`top/left/right/bottom: 0`, with
  `style.pointerEvents: 'none'` — not the standalone `pointerEvents` prop,
  which RN now warns is deprecated) to fill the Hero container, which is
  given `position: 'relative'` + `overflow: 'hidden'` so the video is
  clipped to it rather than the whole screen.
- A `rgba(0, 0, 0, 0.35)` overlay `View` sits above the video (also
  `style.pointerEvents: 'none'`), for contrast.
- The title/buttons render as later JSX siblings (so they stack on top by
  default) plus explicit `zIndex: 2` as a safeguard. `heroTitle`'s color
  changed from `brown` to `offWhite` (still an existing palette color —
  just the higher-contrast one against a video + dark overlay instead of
  the old flat `lightYellow` background) with a small text shadow for
  extra legibility against busy video frames — split by `Platform.OS`:
  the unified `textShadow` shorthand (cast past `@types/react-native`,
  which doesn't know about it yet) on web, since react-native-web warns
  the classic `textShadowColor`/`Offset`/`Radius` props are deprecated;
  those same classic (and, on native, non-deprecated) props on iOS/Android.
- Two source clips live in `src/assets/`: an HD 1920x1080 file (used by
  default — smaller, no visible quality loss as a background loop) and a
  UHD 4096x2160 alternative (~2.5x the size). Swap the `HERO_VIDEO`
  `require()` in `HomeScreen.tsx` to switch.
- The Hero container's `lightYellow` `backgroundColor` is kept as a
  fallback, visible behind/around the video while it's loading.
- The screen's `ScrollView` sets `bounces={false}` (iOS) and
  `overScrollMode="never"` (Android) to remove the elastic
  overscroll/recoil effect at the top/bottom bounds.

### Expandable cards (`ProductCard`, `IngredientCard`)

`ProductCard` (collapsed: name + brand + chevron; expanded: a metadata
block — full name, brand, serving size, scan date — plus its ingredients
rendered as `IngredientCard`s) and `IngredientCard` (collapsed: name +
quick dose summary; expanded: dosage info plus a research/metadata
placeholder box) both use **controlled** expansion — the parent owns an
`expandedId` state and passes each child `isExpanded`/`onToggle`, giving
single-expansion ("accordion") behavior for free within any group that
shares one state value. `ProductCard` used to be a partial exception
(self-owned `isExpanded` + a `defaultExpanded`/`onExpandChange` pair) —
it's now fully controlled too, mirroring `IngredientCard`'s
`IngredientCardProps` exactly (`isExpanded: boolean`, `onToggle: () =>
void`), so both card types follow one consistent pattern.

This is used at three levels now: `ResultsScreen` owns `expandedProductId`
across all top-level `ProductCard` rows in its list (only one product open
at a time), `ProductCard` owns `expandedIngredientId` for *its own* nested
ingredient list (only one ingredient open per expanded product), and
`ResultsScreen` separately owns `expandedIngredientId` for standalone
ingredient results in its flat list (ingredients not nested under any
product card) — three independent accordions, not one shared across the
whole screen. `ScanScreen` follows the same controlled pattern for its
single `ProductCard` with a plain `isProductExpanded` boolean (there's
only ever one product to track, so no id is needed) — see "Scan flow"
below.

**`variant` prop — two distinct internal layouts:** `IngredientCard` now
takes an optional `variant?: 'nested' | 'standalone'` prop (defaults to
`'nested'`), rather than inferring its layout purely from which
`Ingredient` fields happen to be populated:
- **`'nested'`** (default — `ProductCard`'s usage doesn't pass `variant`
  at all, so it's unaffected) is the original compact dosage/
  product-relation card: header shows `"{name} — {doseSummary}"`,
  expanded body shows a `doseBlock` (dosage/%DV/product/found-in rows,
  whichever of `amount`/`unit`/`dailyValue`/`productName`/`productCount`
  are present) plus a research placeholder box. `IngredientCard`'s
  `Ingredient` type still carries two distinct optional field sets here,
  and this variant renders whichever is present: **product-specific**
  (`amount`/`unit`/`dailyValue`/`productName`, populated when nested
  inside a `ProductCard`) or **canonical** (`recommendedDailyDosage`/
  `scientificData`/`productCount`, populated for standalone results
  before this variant split existed — no longer used in practice now
  that standalone results pass `variant="standalone"` instead, but the
  fallback rendering logic is left in place for a nested card that
  somehow lacks product-specific fields).
- **`'standalone'`** (`ResultsScreen` passes this explicitly on its
  top-level, not-nested-under-a-product `IngredientCard` usage) is a new
  wireframe-driven layout: the header is `{name}` on the left and a
  **grade badge** (see "Grading UI" below) paired with the expand/
  collapse chevron on the right; the expanded body renders four
  stacked info blocks (`STANDALONE_INFO_BLOCKS` — General Information,
  Grade Info, Science Info, Related Products), each an
  olive-tinted rounded container with a dark-green bold label and
  italic placeholder body text — **except `Science Info`**, which as of
  the "List of Studies" feature (below) renders the real, database-backed
  `StudiesList` component instead of placeholder text (it supplies its
  own header/container, so the generic block wrapper and label are
  skipped for that one entry — see the `block.label === 'Science Info'`
  branch in `IngredientCard`'s render). The other three blocks (General
  Information, Grade Info, Related Products) remain static placeholder
  text — that content doesn't exist on the backend yet.

**"List of Studies" (`src/components/StudiesList.tsx`).** Replaces the
old `Science Info` placeholder text with a paginated list of the
ingredient's stored `ResearchPaper` rows:
- **Data loading.** `IngredientCard` lazily fetches
  `GET /api/v1/ingredients/{id}` (`fetchIngredientDetail` in
  `src/services/api.ts`) the first time a standalone card is expanded —
  guarded by a `papersFetchAttemptedRef` so it only fires once per mount,
  not on every re-expand. If the card is handed `ingredient.papers`
  directly (not currently done by any caller, but supported), the fetch
  is skipped entirely. A fresh `POST .../grade` response's `papers` field
  (see "Research Paper Search & Ingredient Grading" above) overwrites
  local `papers` state directly instead of triggering a second GET.
  `papers === undefined` renders `StudiesList`'s loading text;
  `papers === []` (fetched, genuinely empty — ingredient never graded, or
  graded but zero results found) renders `"No studies available yet.
  Click 'Grade' to fetch research."`; a failed fetch renders the caught
  error message instead.
- **Pagination.** Purely client-side — `StudiesList` receives the full
  unpaginated `papers` array and slices it into pages of 5
  (`PAGE_SIZE`) locally; no server-side paging, since a single
  ingredient's paper count from the Phase 2 search pipeline is small.
  Footer shows `← Previous` / numbered page badges (orange-filled for the
  active page, per palette) / `Next →`, with `Previous`/`Next` disabled
  (dimmed) on the first/last page respectively. Hidden entirely when
  everything fits on one page.
- **Per-row actions.** Each row shows the paper's `title` (2-line
  truncated) with, on the right: a round letter-grade badge (below, only
  if the paper has been graded), an info icon that opens a centered modal
  showing `authors` / `publication_date` / `source_domain` (joined with
  `·`, skipping any that are missing), a **"Matched Keywords"** section
  (below), and the full `abstract`, with its own "View Source" button;
  and a globe icon that calls `Linking.openURL(paper.source_url)`
  directly from the row (wrapped in a `.catch` that alerts if the
  platform can't open the URL, e.g. a malformed link). Rows are
  separated by a dashed bottom border.
- **Grade-based sorting (before pagination).** `sortPapersByGrade`
  (module-level, pure, strictly typed `(papers: readonly ResearchPaper[])
  => ResearchPaper[]`) ranks every paper by grade — A (1) through E (5),
  with `null`/`undefined`/any unrecognized value sharing `UNGRADED_RANK`
  (6, always last) via the same `isPaperGrade()` guard used for badge
  rendering. Papers with the same letter grade are tie-broken by
  `grade_score` descending; papers sharing `UNGRADED_RANK` (or, in
  principle, an exact score tie) fall back to their original index in
  the *unsorted* input array, threaded through explicitly via a
  `{ paper, index }` wrapper rather than relying on `Array.prototype.sort`'s
  spec-guaranteed stability — deliberately explicit so "preserve original
  retrieval order" holds regardless of engine/refactor. `StudiesList`
  computes `sortedPapers = useMemo(() => papers && sortPapersByGrade(papers), [papers])`
  and derives `totalPages`/`pageItems`/the empty-state check from
  `sortedPapers`, never the raw `papers` prop directly — so sorting
  always happens *before* the 5-item pagination chunking, per spec, and
  re-runs automatically any time `papers` changes (including after an
  on-demand single-paper grade, below — no separate "re-sort" call
  needed, the `useMemo` dependency does it).
- **Round letter-grade badge, gray "(-)" ungraded badge, and Rubric
  Breakdown modal (Phase 3/4 — automated paper grading, see the backend
  section above).** A small (26px) circular badge sits leftmost in each
  row's action group, in one of three states:
  - **Graded** (`isPaperGrade(paper.grade)` true): `PaperGradeBadge`
    filled with a fixed grade->color mapping (`GRADE_COLORS`, local to
    `StudiesList.tsx` — deliberately not sourced from `theme.ts`, since
    these are semantic traffic-light colors, not brand palette colors):
    `A` `#1E7E34`, `B` `#28A745`, `C` `#D39E00`, `D` `#FD7E14`, `E`
    `#DC3545`. Tapping it opens the Rubric Breakdown modal (below).
  - **Ungraded, idle:** a `Pressable` badge filled `UNGRADED_BADGE_COLOR`
    (`#6C757D`) showing `"-"` — tapping it calls
    `handleGradePaperPress`, which POSTs
    `/api/v1/papers/{id}/grade` (`gradePaper()` in `src/services/api.ts`)
    and, on success, calls the `onPaperGraded` prop with the updated
    paper (see below); on failure, shows an `Alert` (same pattern as
    `handleOpenSource`/ingredient-level grading) and the badge reverts to
    idle so the user can retry.
  - **Ungraded, grading in flight:** the same circular/bordered footprint
    (no row reflow) with the fill swapped to transparent and an
    `ActivityIndicator` in place of the `"-"`, tracked via a single
    `gradingPaperId: number | null` state value (guards against a second
    tap firing a duplicate request for the same or a different paper
    while one is already in flight — simple enough for this component's
    realistic usage pattern of grading one paper at a time).
  Every state shares the same circular shape and palette-orange border
  (per the "expanded card = orange, active-selected border rule" above)
  — only the fill/content differs, so the row's layout never shifts as a
  badge moves between states.
  - **`onPaperGraded` — who owns `papers`.** `StudiesList` doesn't own
    the `papers` array itself (it's a prop, ultimately state living in
    `IngredientCard`) — so a successful on-demand grade can't just be
    set locally. `StudiesListProps.onPaperGraded?: (paper: ResearchPaper)
    => void` is how the result gets back to whoever does own it;
    `IngredientCard`'s `handlePaperGraded` implements it by mapping its
    `papers` state, replacing the entry with a matching `id`. Once that
    prop updates, `sortedPapers`'s `useMemo` (above) picks up the change
    automatically and the newly-graded paper re-sorts into its correct
    rank — including possibly jumping onto a different page, since
    sorting happens before chunking.
  Tapping a **graded** badge opens a second modal (independent of the info
  modal's `activePaper` state — this one tracked as
  `activeRubricPaper`) showing: a header with the paper title, a larger
  (44px) version of the same badge, and `total_score / 100`; then four
  dashed-divider-separated sections pulled straight from
  `rubric_evaluation` — "Study Design" (`study_type` +
  `study_type_score`), "Journal Rigor" (`journal_reputation` +
  `journal_score` — as of rubric v1.4 this one can also be negative,
  down to -5, for an actively-flagged predatory/blacklisted publisher),
  "Methodology & Sample" (`sample_info` + `sample_score`), "Funding &
  Bias" (`funding_status` + `funding_score` — as of rubric v1.2 this one
  can be negative down to -15 (sharpened from v1.1's -10 floor), e.g.
  "-12 pts" for penalized industry-biased funding; both negative-capable
  fields render as plain signed text, no special-casing needed since
  `total_score` (shown at the top) is already the post-penalty,
  0-100-clamped figure)
  — and a final "AI Summary Note" section showing `summary_notes` in
  italics. `PaperGradeBadge` renders as a plain (non-`Pressable`) `View`
  when used without an `onPress` (the modal-header usage) rather than
  wrapping an already-tapped badge in another dead button for screen
  readers. Both modals use the same `Modal` + backdrop-`Pressable` +
  `stopPropagation`-on-card pattern — plain component props and
  callbacks throughout, no `findNodeHandle` or other ref-based DOM API,
  so both are equally functional on web and native.
- **"Matched Keywords" (info modal).** Renders `paper.keywords` (the
  Gemini-generated search terms that surfaced this paper — see "Matched
  keyword tracking" above) as a wrapping row of pill tags, each an
  orange-bordered, orange-tinted-background (`${colors.orange}18`)
  rounded chip with orange text. Omitted entirely if `keywords` is empty
  (a paper found before keyword tracking existed, or found through a
  source/keyword this logic somehow didn't attach — shouldn't happen in
  practice, but handled gracefully rather than showing an empty
  section).
- **Palette: orange-only, no exceptions.** `StudiesList` only ever
  renders while its parent `IngredientCard` is expanded (it's nested
  inside standalone `IngredientCard`'s `{isExpanded && ... && (...)}`
  block), so every text/icon/border color in this component — the "LIST
  OF STUDIES" header, paper titles, the `(i)`/globe icons, row dividers,
  pagination text and page-number badge borders, and the info modal's
  border/title/metadata/abstract/"View Source" button/keyword pills —
  is hardcoded to the palette orange (`colors.orange`), not conditioned
  on an `isExpanded` prop. There is no "collapsed" rendering of this
  component to also support, so a static orange palette is simpler and
  equally correct. The only non-orange colors left are backgrounds
  (the olive-tinted container, the off-white modal card) — the "no green
  visible" rule targets `#355A35` specifically, not every non-orange
  hex in the file.

### Grading UI (`GradeBadge.tsx`, `ProductCard.tsx`, standalone `IngredientCard.tsx`)

First pass at surfacing grading in the UI. As of Phase 2 (see "Research
Paper Search & Ingredient Grading" above), standalone `IngredientCard`'s
grade button is backed by a real endpoint; `ProductCard`'s is still
local/placeholder-only (there's no product-level grading pipeline yet).

**1. Grade badge/button (`src/components/GradeBadge.tsx`).** A single
shared component, used in the header's top-right corner by both
`ProductCard` and standalone `IngredientCard` (nested `IngredientCard`
rows still don't render any badge at all — out of scope, unchanged) so
both render an identical pill shape (`borderWidth: 1.5`, `borderColor:
colors.darkGreen`, `borderRadius: 15`, `paddingHorizontal: 10`,
`${colors.olive}18` background) regardless of graded state:
- **Ungraded, idle** (`isGraded: false`, `isLoading: false`): renders as
  a `Pressable` reading "Assign Grade".
- **Ungraded, loading** (`isGraded: false`, `isLoading: true`): the same
  pill, `disabled`, showing a small `ActivityIndicator` (colored to match
  the current text color — dark green normally, orange while the card is
  expanded) instead of the label. Only standalone `IngredientCard` ever
  sets this — `ProductCard`'s "grading" is still an instant local state
  flip with nothing to wait on.
- **Graded** (`isGraded: true`): the same-shaped pill, no longer
  pressable, showing whatever `gradeValue` the caller passes — a required
  prop as of Phase 2 (previously a hardcoded constant baked into
  `GradeBadge` itself). `ProductCard` passes the fixed
  `PLACEHOLDER_GRADE_VALUE = '8 / 10 / 9'` (still exported from
  `GradeBadge.tsx`); standalone `IngredientCard` passes the real
  `grade_badge_text` returned by `POST /api/v1/ingredients/{id}/grade`
  (falling back to `PLACEHOLDER_GRADE_VALUE` before any request has ever
  completed).

**`ProductCard`'s `handleGradeRequest`** is unchanged from the pre-Phase-2
version: `animateCardToggle()` then `setIsGraded(true)`, a purely local,
instant flip with nothing persisted anywhere.

**Standalone `IngredientCard`'s `handleGradeRequest`** is now async: it
guards against a request already in flight, sets `isRequestingGrade`
(true), calls `gradeIngredient(ingredient.id)`
(`src/services/api.ts`, `POST /api/v1/ingredients/{id}/grade`), and on
success calls `animateCardToggle()` before setting `isGraded`/
`gradeBadgeText` from the response (so the pill's resize — "Assign
Grade" vs. a loading spinner vs. a real, possibly-different-width grade
string — animates smoothly either way). On failure, shows
`Alert.alert('Grading failed', message)` and leaves the card ungraded —
the user can retry. `isRequestingGrade` is reset in a `finally` either
way. Reverting an already-graded card back to ungraded isn't exposed in
either component — only forward, "request a grade," is wired up.

**`is_graded` / `grade_badge_text` on the data types:** `Product`
(`ProductCard.tsx`) and `Ingredient` (`IngredientCard.tsx`) both carry
optional `is_graded?: boolean` fields; `Ingredient` additionally carries
`grade_badge_text?: string`. Every mapping function that builds a
`Product`/`Ingredient` still explicitly sets `is_graded: false`
(`ResultsScreen.tsx`'s `toProduct`/`toIngredient`, `ScanScreen.tsx`'s
`toScannedProduct`) since the backend's search endpoint doesn't return
real grading data yet (see the Phase 2 section's "Known gaps" above) —
`toLinkedIngredient`, which builds *nested* ingredient rows, deliberately
leaves both fields unset, since nested rows never render a badge at all.
Each card's `useState` initializers read these props once on mount but
don't re-sync if the prop changes identity later — acceptable since a
real grade, once fetched via the API response, immediately overwrites
the initial value anyway.

**2. Orange text on expansion.** Both `ProductCard` and standalone
`IngredientCard` now force every text element they own to the palette
orange (`colors.orange`) while `isExpanded` is `true` — name, brand,
metadata labels/values, the "Ingredients" section title, the empty-
ingredients message (`ProductCard`); name and the four info blocks'
labels/placeholder bodies (standalone `IngredientCard`); `GradeBadge`'s
own label text too, via an `isExpanded` prop passed down to it. The
mechanism is a shared `expandedTextColor: { color: colors.orange }`
style, appended as the last element of each `Text`'s style array (e.g.
`[styles.name, isExpanded && styles.expandedTextColor]`) — RN style
arrays merge left-to-right, so the later, conditional entry wins once
`isExpanded` is `true` and is simply `false` (a no-op) otherwise. Applied
**only** to `ProductCard`'s own text and the `'standalone'` variant of
`IngredientCard` — nested `IngredientCard` rows are completely
unaffected (no `expandedTextColor` style exists there at all), since a
nested row's `isExpanded` is per-ingredient and unrelated to its parent
`ProductCard`'s own expanded state. The card's own background and border
colors (including the existing dark-green → orange border swap on
expand) are untouched by this — only text color. The `Ionicons` chevron
glyphs are also left as-is (`colors.brown`) — the spec calls out "text
elements" specifically, and an icon glyph isn't a `Text` component.

**Visual/interaction redesign:**
- Both cards' outer border is now `borderWidth: 3` (was 1), defaulting to
  `colors.darkGreen` and switching to `colors.orange` while expanded via a
  conditional `cardExpanded` style appended to the base `card` style
  (`style={[styles.card, isExpanded && styles.cardExpanded]}`) — a
  direct, per-card visual "this one's open" signal.
- Titles/tags/labels use three `theme.ts` typography tokens scoped to
  result cards — `resultCardTitle` (20, up from `body`'s 16),
  `resultCardTag` (15, up from 13), `resultCardLabel` (13, up from the
  original 12 — nudged back down slightly from an intermediate 14 in a
  later pass, to keep secondary body details like dosage values and
  metadata labels compact) — kept separate from other screens' tokens
  for the same reason `sectionTitleLarge` is (LibraryScreen section) so
  bumping them doesn't cascade elsewhere. Header row padding went from
  `spacing.md`/mixed vertical-horizontal to a uniform `spacing.lg` (24);
  `expandedSection`'s gap and the nested
  `metadataBlock`/`doseBlock`/`ingredientsList` spacing all moved up a
  step on the `spacing` scale (e.g. `sm`→`md`, `md`→`lg`) for more
  breathing room between the header, metadata block, and ingredient
  list. Card `borderRadius` increased too — `ProductCard` to 20 (was
  12), `IngredientCard` to 16 (was 10), the outer card intentionally a
  touch rounder than what's nested inside it. `ProductCard`'s
  "Ingredients" subheading (`ingredientsTitle`) was bumped from
  `resultCardTag`/700 to `sectionTitle`(22)/800 so it reads as a clear
  divider between the metadata block above it and the ingredient list
  below — there's no equivalent literal "Products" heading anywhere in
  the current per-card hierarchy to apply the same treatment to.
- **Auto-scroll on top-level expansion:** `ResultsScreen` holds a
  `flatListRef` and a `scrollToItemIndex(index)` helper that calls the
  `FlatList`'s `scrollToIndex({ index, animated: true, viewPosition: 0
  })`, deferred one `requestAnimationFrame` so it doesn't fight the
  in-flight `LayoutAnimation` the expand itself triggered. It's called
  whenever a **top-level** row (the `FlatList`'s own item — a
  `ProductCard` or a standalone `IngredientCard`, per `renderItem`'s `{
  item, index }`) transitions to expanded: for `ProductCard`, inline in
  the `onToggle` handler that owns `expandedProductId` (collapsing any
  previously-open product first, single-expansion); for the standalone
  `IngredientCard`, inline in the `onToggle` handler that owns
  `expandedIngredientId`. An `onScrollToIndexFailed` handler on the
  `FlatList` is a defensive fallback (retries after a short delay) for
  the rare case the target row isn't measured yet — expected to
  essentially never fire, since the tapped card is always already
  visible on screen.
- **Auto-scroll on nested ingredient expansion:** tapping an
  `IngredientCard` *inside* an already-expanded `ProductCard` also
  triggers a scroll, so the newly revealed dosage/placeholder content
  isn't clipped by the viewport edge. This used to go through
  `findNodeHandle` + `measureLayout` against a supplied native node —
  **`findNodeHandle` is unsupported on React Native Web** and threw
  `[Error: findNodeHandle is not supported on web. Use the ref property
  on the component instead.]` the moment a card was expanded there,
  which crashed the whole Results Screen. It's been replaced with a
  `Platform.OS`-branched approach that never touches a native node
  handle at all:
  - **Web:** `IngredientCard` now forwards its `ref` (`React.forwardRef`)
    straight to its own outer `View`. `ProductCard` keeps one such ref
    per rendered ingredient row (`ingredientRowRefs`, keyed by ingredient
    id) and, in its nested `onToggle` handler
    (`handleIngredientToggle`), calls `rowNode.scrollIntoView({ behavior:
    'smooth', block: 'nearest' })` directly on the tapped row — React
    Native Web forwards `View` refs to the underlying DOM node, which
    supports `scrollIntoView` natively, so no knowledge of the parent
    scroll container (or its native node) is needed at all.
  - **Native (iOS/Android):** there's no cross-platform-safe way to
    measure a nested row's pixel offset without a native node handle, so
    this instead calls an optional `onNestedIngredientExpand` prop
    supplied by the parent screen, which just re-runs the *same*
    top-level scroll already used for the product's own expansion:
    `ResultsScreen` passes `() => scrollToItemIndex(index)` (re-pinning
    the product row to the top of the `FlatList`); `ScanScreen` passes a
    `handleNestedIngredientExpand` that calls `scrollViewRef.current.
    scrollTo({ y, animated: true })`, where `y` is `resultsContainer`'s
    own Y offset — tracked via a single `onLayout` on that `View` (it's a
    direct child of `body`, which is itself the `ScrollView`'s first
    child, so no cross-boundary measurement is needed there either).
    Re-pinning the card's top to the viewport top maximizes the space
    left below to show the newly revealed content; it's not pixel-exact
    like the web path, but needs no native node handle of any kind.

  Both paths run inside a `requestAnimationFrame` (deferred one frame so
  they don't race the in-flight `LayoutAnimation` the expand itself
  triggered) and are guarded with `if (rowNode) …` / optional chaining, so
  a not-yet-mounted or unmounted ref is a silent no-op rather than a
  crash. `ProductCard`'s prop surface reflects this: the old
  `nestedScrollSupport` object (`{ scrollContainerNode, scrollToY }`) is
  gone, replaced by the single optional `onNestedIngredientExpand: () =>
  void` callback described above (unused on web, where `ProductCard`
  handles it internally).
- Expand/collapse now animates via `LayoutAnimation.configureNext(
  LayoutAnimation.Presets.easeInEaseOut)`, wrapped as
  `animateCardToggle()` in a new shared helper,
  `src/utils/animations.ts`. It must be called synchronously,
  immediately before the state update that changes what's rendered — so
  it's called at every call site that actually owns expand/collapse
  state: `ProductCard`'s own `handleToggle`, the nested `IngredientCard`'s
  `onToggle` inside `ProductCard`, and the standalone `IngredientCard`'s
  `onToggle` in `ResultsScreen`. `IngredientCard` itself doesn't call it,
  since it's a controlled component that doesn't own the state it's
  toggling (see "controlled expansion" above). The helper also flips
  Android's `UIManager.setLayoutAnimationEnabledExperimental(true)` once
  at module load (guarded on the method existing, since some RN versions
  under the Fabric/new-architecture renderer have removed it — calling it
  unconditionally could throw on those; it's largely a no-op there anyway
  since LayoutAnimation is supported natively under Fabric).

**Fixed:** `SearchResultItem` now carries a nested `ingredients` list for
product results (`app/services/search.py::get_linked_ingredients` does an
explicit join over `ProductIngredientLink` + `Ingredient` per product,
rather than relying on `Product.ingredients`' lazy-loaded relationship,
which serialized as `[]` inside Pydantic even when link rows existed in
the DB — this was the root cause of `ProductCard` always showing "No
ingredient data available for this product yet"). `ResultsScreen`'s
`toProduct()` now maps `item.ingredients` through `toLinkedIngredient()`
instead of hardcoding `[]`.

**Remaining known gap:** `GET /api/v1/supplements/search` still doesn't
return a product's serving size or scan date (`Product` has no
`serving_size` column, and `SupplementAnalysis.serving_size` from Gemini
is silently dropped on save — see the Database section's gaps). Those two
fields on `ProductCard` still render "Not available" until a
`serving_size` column is added to `Product`. A dedicated detail endpoint,
`GET /api/v1/products/{id}` (`ProductDetailResponse`), now exists and
returns the same nested ingredient list in one request — not currently
called anywhere in the app (search already returns what `ResultsScreen`
needs), but available for a future dedicated product-detail screen.

### Scan flow

`ScanScreen.handleAnalyze` calls `uploadSupplementImage` from
`src/services/api.ts` and stores the response in local state. Errors go
through `Alert.alert`. An `isLoading` state disables the Analyze button
and shows a spinner while the request is in flight. On web, `api.ts`
fetches the picker's `blob:` URI into a real `Blob` before attaching it to
`FormData`, since React Native Web's `FormData` requires a `Blob`/`File`
rather than the `{ uri, name, type }` object shape used on iOS/Android.

**Typed response (closed gap):** `api.ts` now exports `SupplementAnalysis`
(`{ product_name, serving_size, ingredients: ScannedIngredient[] }`),
mirroring the backend's actual `app/schemas/supplement.py::SupplementAnalysis`
response shape, and `uploadSupplementImage()`/`ScanScreen`'s `result` state
are typed against it. This replaces the old `ScanResponse` stub
(`{ message: string }`), which never matched what the backend actually
returns — a long-flagged "Known gap" that's now resolved.

**Result rendering:** the raw pretty-printed-JSON debug output is gone.
`ScanScreen` now maps the response onto a `Product` (via a local
`toScannedProduct()` helper) and renders it through the **same
`ProductCard`** component `ResultsScreen` uses — same width bounds
(`resultsContainer` no longer caps `maxWidth`; it stretches to fill the
20%-inset `body`, same as a `ResultsScreen` list row), same padding,
rounded borders, and orange focus-state border transition, since it's
literally the same component rendered the same controlled way. `ScanScreen`
tracks a local `isProductExpanded` boolean (there's only ever one product
on this screen, so no id is needed, unlike `ResultsScreen`'s
`expandedProductId`), passed to `ProductCard` as `isExpanded`/`onToggle`;
it starts (and is reset on every new scan result to) `true`, so the
just-scanned details are visible immediately rather than requiring a tap.
`ProductCard` also receives an `onNestedIngredientExpand` callback (see
"Expandable cards" above) so tapping a nested ingredient here
auto-scrolls too — on web, `ProductCard` handles that itself directly via
the tapped row's own `scrollIntoView`; on native, this screen's callback
scrolls its own `scrollViewRef` to `resultsContainer`'s tracked `onLayout`
offset. Since the scan response has no persisted `Product.id`
(see the "Known gaps" list above) and its per-scan `Ingredient` rows have
no canonical `Ingredient.id` either, `toScannedProduct()` synthesizes
placeholder ids (product `0`, ingredient = array index) purely so
`ProductCard`/`IngredientCard` have something to key/track expansion by —
these aren't real database ids and don't correlate to anything.

**Empty-state centering:** when nothing's been picked and there's no
result yet (`isEmptyState`), `body`'s `justifyContent` switches from
`'flex-start'` to `'center'` via a conditional `bodyCentered` style, so
the upload card/prompt/button sit vertically centered in the viewport
rather than pinned to the top. `body` is `flex: 1` in *both* states now
(replacing the old separate `footerSpacer` `View`) — that alone both
centers the idle state (by giving `body` the full leftover height to
center within) and keeps Footer pinned to the bottom on short content;
once real content (the `ProductCard`) makes `body` taller than the
available space, `flex: 1` simply has no effect and it renders normally,
top-aligned and scrollable.

### Search / browse flow

`LibraryScreen` has a Search section (text input + live autocomplete) and
an Explore section (Products / Ingredients browse cards):

- Typing debounces (300ms) and, once the query is longer than 3
  characters, calls `fetchSuggestions()` (`GET /supplements/suggest`) and
  renders the result as an absolutely-positioned dropdown under the search
  bar (`zIndex`/`elevation` set explicitly, since Android needs both to
  stack correctly). A `requestIdRef` guards against a slow earlier request
  overwriting a faster later one. Suggestion fetch failures are logged via
  `console.warn` and silently clear the dropdown, rather than
  interrupting typing with an `Alert`.
- Submitting the search (button or return key) or tapping a suggestion
  navigates to `ResultsScreen` with `{ query, filterType: 'all' }`.
- Tapping a Products/Ingredients explore card navigates to `ResultsScreen`
  with only `{ filterType }` set (no `query`), which the backend
  interprets as "browse all rows of this type."

`ResultsScreen` reads `query`/`filterType` from its route params, calls
`searchSupplements()` (`GET /supplements/search`, capped at 20 results) on
mount/param-change, and renders an `ActivityIndicator` while loading, an
error message on failure, or a `FlatList` of results. Each row renders as
a `ProductCard` or `IngredientCard` depending on `item.type` (see
"Expandable cards" above). A back arrow (`navigation.goBack()`, guarded by
`canGoBack()`) sits above a title/filter icon row — the filter icon is a
static visual placeholder, not wired to any behavior yet. Both LibraryScreen
and ResultsScreen rely on the NavBar already being persistent
(`src/App.tsx`) rather than rendering their own.

**LibraryScreen visual redesign:**
- `ScrollView`'s `contentContainerStyle` uses `flexGrow: 1` +
  `justifyContent: 'space-between'` (replacing an old fixed
  `footerSpacer` `View`) so `Footer` pins to the viewport bottom when
  content is shorter than the screen, while still behaving like normal
  scrollable content once it's taller.
- The Search/Explore section titles use a new, larger typography token,
  `typography.sectionTitleLarge` (28px) — kept separate from the shared
  `typography.sectionTitle` (22px, still used by Home/Results) so bumping
  it doesn't cascade to screens that didn't ask for bigger titles. Section
  titles and subtitles are center-aligned (`textAlign: 'center'`); the
  search bar and card row are separate non-`Text` elements with an
  explicit `alignSelf: 'stretch'` so they aren't shrunk by the section
  container's now-centering `alignItems`. The gap between the Search and
  Explore sections increased to `spacing.xl * 1.75` (56, up from 40).
- The search bar is now a 56px-tall, `borderRadius: 28` pill with no icon
  inside the text field. A separate 44×44 circular button (`borderRadius:
  22`, `backgroundColor: colors.darkGreen`) sits at the pill's right edge
  with a centered `Ionicons name="search"` glyph — no "Search" label text.
  The pill's own outer border was thickened to `borderWidth: 3` (was a
  thin, translucent-brown 1px border) to match the button and NavBar. The
  `TextInput` also sets `selectionColor` (`colors.darkGreen`) and
  `underlineColorAndroid="transparent"`, and — web only —
  `outlineStyle: 'none'` to suppress the browser's default focus ring on
  the underlying `<input>`. That last one needs an `as unknown as
  TextStyle` cast: `@types/react-native` already defines an
  `outlineStyle` key, but typed for a *native* border-style meaning
  (`'solid' | 'dotted' | 'dashed'`) that doesn't include `'none'` — a
  naming collision with react-native-web's separate, web-only usage of
  the same prop name, not a real type error.
- **Focus state:** a dedicated `isFocused` boolean (separate from
  `suggestionsVisible`, which drives the autocomplete dropdown and has
  its own blur-delay timing) is set on the `TextInput`'s `onFocus`/
  `onBlur`. While focused, `searchBarFocused` (`borderColor:
  colors.orange`) and `searchButtonFocused` (`backgroundColor:
  colors.orange`) are appended to the bar/button's style arrays,
  swapping both from dark green to orange; they revert on blur.
- The Products/Ingredients explore cards are sized to **15% of the
  actual window width** each (`useWindowDimensions().width * 0.15`,
  applied inline as `{ width: exploreCardSize, height: exploreCardSize
  }` per card) — computed from the window rather than set as a `'15%'`
  string in the `StyleSheet`, since a percentage there would resolve
  against the card's parent (`cardsRow`, narrower than the screen because
  of the 20% horizontal screen inset), not the true screen width the
  spec asks for. `useWindowDimensions()` (already used on HomeScreen) is
  reactive to resize/rotation, unlike a one-off `Dimensions.get()` call.
  `cardsRow` centers the two cards (`justifyContent: 'center'`) with a
  `spacing.xl` (32px) gap between them. Each card is an `ImageBackground`
  (`src/assets/products.png` / `ingredients.png`, `blurRadius={2}`) with
  a `rgba(0, 0, 0, 0.4)` dark overlay `View` beneath the (uniformly
  `offWhite`) label text for legibility, and no border
  (`borderWidth: 0`) — the two cards are distinguished purely by their
  photo.

### Debug tools

`NavBar` has a trash-icon button on the far right (inside the same
right-aligned `links` row as Scan/Supplement Library), visible on every
screen since NavBar is persistent. Tapping it shows a confirm dialog
(`Alert.alert` with Cancel/destructive-Delete buttons — React Native has
no `Alert.confirm`, so this is the platform's standard confirm-dialog
pattern) asking "Are you sure you want to delete all mock database
entries?". On confirm, it calls `resetMockData()`
(`DELETE /api/v1/dev/mock-data`) and shows the result via `Alert.alert`,
with an `ActivityIndicator` swapped in for the icon while the request is
in flight. This is a development convenience, not a user-facing feature —
it should be removed (or hidden behind a build flag) before this app ever
ships anywhere real, since the endpoint it calls is unauthenticated and
destructive.
