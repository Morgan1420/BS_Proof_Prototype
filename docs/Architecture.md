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
    │   ├── research.py      # RubricEvaluationResponse (Phase 3), ResearchPaperResponse, IngredientDetailResponse, GradeIngredientResponse (Phase 2), GradePaperResponse (Phase 4)
    │   └── (supplement.py adds LinkedIngredientResponse, ProductDetailResponse)
    ├── models/
    │   ├── schemas.py      # ScanResponse (superseded by schemas/supplement.py; unused)
    │   ├── supplement.py   # Product, Ingredient (now with is_graded/grade_badge_text/papers), ProductIngredientLink — SQLModel ORM tables (M2M)
    │   └── research.py     # ResearchPaper — SQLModel ORM table (Phase 2; now with keywords + grade/grade_score/rubric_evaluation from Phase 3), FK'd to Ingredient. Also: serialize_keywords()/parse_keywords()
    └── services/
        ├── vision.py       # Gemini API calls for label parsing
        ├── storage.py      # save_scan() (M2M find-or-create), delete_all_data(), delete_mock_data() (legacy, unused by the route)
        ├── search.py       # suggest() / search() queries, get_linked_ingredients()/get_product_detail() (explicit joins), get_ingredient_papers()/get_ingredient_detail()/to_research_paper_response() (shared ORM->response mapper)
        ├── research_keywords.py  # Gemini: generate_ingredient_keywords() (Phase 2)
        ├── paper_search.py       # Europe PMC/PubMed/Semantic Scholar/OpenAlex (async, concurrent): search_papers_for_ingredient() (Phase 2; now also grades each new paper — Phase 3)
        ├── paper_grader.py       # Gemini: grade_paper() — evaluates one paper against docs/paper_grading_rubric.json (Phase 3); grade_single_paper() — on-demand DB-aware wrapper for one already-stored paper (Phase 4)
        └── grading.py             # Orchestrates keyword-gen + paper-search (which itself now grades) + debug grade assignment: grade_ingredient() (Phase 2)
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
keyword tracking" in the Phase 2 section below), and later `grade`/
`grade_score`/`rubric_evaluation` (Phase 3 automated paper grading — see
"Automated paper grading" below): `init_db()` also calls
`_migrate_research_paper_columns()` right after
`_migrate_ingredient_grading_columns()`, checking `PRAGMA
table_info(research_papers)` and adding whichever of `keywords VARCHAR`,
`grade VARCHAR`, `grade_score INTEGER`, `rubric_evaluation JSON` are
missing. No `DEFAULT` needed for any of these (unlike `is_graded`) since
all four columns are nullable.

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
Returns a single canonical `Ingredient` plus every `ResearchPaper` stored
for it (`app/services/search.py::get_ingredient_detail`). Added to back
standalone `IngredientCard`'s "List of Studies" panel
(`src/components/StudiesList.tsx`) — this is a pure read, it never
triggers a new paper search itself; `papers` is just whatever's already
been persisted by a prior `POST .../grade` call (`[]` if the ingredient
hasn't been graded yet).

- **Params:** `id` (path, int).
- **Response (200):** `IngredientDetailResponse` — `{ id, name,
  recommended_daily_dosage, scientific_data, product_count, is_graded,
  grade_badge_text, papers: ResearchPaperResponse[] }`. Each
  `ResearchPaperResponse` is `{ id, title, abstract, authors,
  publication_date, source_url, source_domain, ingredient_id, keywords:
  string[], grade, grade_score, rubric_evaluation }` — a direct mirror of
  the `ResearchPaper` table columns, except `keywords` (parsed from the
  stored comma-separated string via `parse_keywords()`) and
  `rubric_evaluation` (the stored JSON dict, validated straight into
  `RubricEvaluationResponse`). `grade`/`grade_score`/`rubric_evaluation`
  are `null` for a paper that hasn't been graded yet (Phase 3 — see
  "Automated paper grading" below).
- **Errors:** `404` if no `Ingredient` with that id exists.

### `POST /api/v1/ingredients/{id}/grade`
**[Phase 2, debug]** Runs the research-paper search pipeline for a single
canonical `Ingredient` and assigns a debug grade — see "Research Paper
Search & Ingredient Grading (Phase 2)" below for the full pipeline.

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
  a Gemini call or corrupt the stored grade.
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
Every newly-persisted `ResearchPaper` row is graded automatically, right
when it's created — `search_papers_for_ingredient`'s new-paper loop
calls a private `_apply_grade(paper, record)` helper immediately after
building each `ResearchPaper` (before it's flushed), which calls
`paper_grader.grade_paper({title, abstract, authors, journal,
publication_date})` and sets `paper.grade`/`paper.grade_score`/
`paper.rubric_evaluation` from the result. Papers matched to an
*already-stored* paper (the `_merge_keyword_onto` path above) are never
re-graded — a paper's evaluation doesn't change once assigned, only its
`keywords` list grows.

- **Rubric — `docs/paper_grading_rubric.json`** (repo root, same
  absolute-path resolution as `paperApis.json`; currently `version:
  "1.1"`). Defines four weighted categories whose *positive* maximums
  sum to 100 — `study_type` 35, `journal_reputation` 25,
  `sample_methodology` 30, `funding_bias` **10** — each with a
  human-readable `description` and a handful of `score_tiers` (a point
  range + a worked example of what earns it), plus `grade_bands` mapping
  contiguous 0-100 score ranges onto letters A-E (A: 85-100 down to E:
  0-29). `_load_rubric()` reads and `@lru_cache`s this file for the
  process lifetime — unlike `paperApis.json` (re-read every call so
  `enabled: false` takes effect live), the rubric isn't meant to be
  hot-swapped, and it's read once per *paper* rather than once per
  *request*, so re-parsing it every time would add up.
  - **`funding_bias` is a penalty scale, not a plain 0-to-max score.**
    It's the one category with an explicit `min_score` (`-10`, alongside
    `max_score: 10`) — independent/well-disclosed funding earns up to
    +10, neutral/undisclosed funding scores near 0, and industry-biased,
    undisclosed-conflict, or "suspicious commercial interference" funding
    is *penalized* down to -10. A paper can therefore land below what its
    other three categories alone would suggest — e.g. a methodologically
    excellent but transparently marketing-driven study loses points
    overall rather than merely forfeiting a category's positive credit.
    This is why the categories' positive maximums (35+25+30+10=100) sum
    to exactly 100 while the theoretical floor is -10, not 0 —
    `grade_paper` clamps the final total back to 0-100 (see "Structured
    output" below), so that floor never surfaces as a negative
    `grade_score` in the API/UI.
- **Prompting.** `_format_rubric_for_prompt` renders every category's
  label/max score/description/score tiers as plain text and embeds it in
  the Gemini prompt alongside the paper's title/abstract/authors/
  journal/publication info — the actual scoring criteria live in the
  JSON file (editable without a code change), not hardcoded into a
  Python string. Gemini is explicitly told to score conservatively in
  the lower tiers of a category when the given metadata doesn't cover it
  (e.g. no funding info in an abstract), rather than assuming the best
  case.
- **Structured output — `_RubricEvaluationSchema`.** Same
  `response_schema` + `.parsed`-with-raw-text-fallback pattern as
  `research_keywords.py`/`vision.py`. Deliberately does **not** ask
  Gemini for a letter grade directly — only the four category scores,
  their descriptive text, `total_score`, and `summary_notes`; the prompt
  explicitly calls out that `funding_score` is the one field allowed to
  go negative (-10 to 10), every other category score must be
  non-negative. Each category score is clamped to that category's own
  `(min_score, max_score)` bounds from the rubric (`category_bounds`,
  built from each category's `min_score`/`max_score` — `0` by default,
  `-10` for `funding_bias`) in case Gemini's raw output overshoots either
  side. The final `total_score` is then recomputed server-side as the
  sum of those clamped category scores (funding's contribution may be
  negative) rather than trusted from Gemini's own arithmetic, clamped
  again to 0-100, and `grade` is derived purely from that final total via
  `grade_bands` (`_score_to_grade`). This guarantees `grade` and
  `grade_score` can never disagree with each other or with the category
  breakdown — a real risk if Gemini were asked to independently pick
  both a score and a letter — and that a heavily-penalized paper's total
  never surfaces as a negative or out-of-range `grade_score`.
- **Per-paper resilience.** `_apply_grade` catches `PaperGradingError`
  (raised for a failed Gemini call, an empty/unparseable response, or a
  missing/malformed rubric file) and logs a warning rather than
  propagating — a single paper's grading failure leaves that one row
  ungraded (`grade`/`grade_score`/`rubric_evaluation` stay `None`)
  without failing the rest of the ingestion batch, same philosophy as
  `_safe_query_async`'s per-source handling.
- **Journal name capture.** `PaperRecord` gained a `journal: Optional[str]`
  field (not persisted as its own `ResearchPaper` column — only used
  transiently to build the grading prompt) so "Journal / Publisher
  Rigor" grading has something better than the *platform* domain
  (`source_domain`, e.g. `"pubmed.ncbi.nlm.nih.gov"`) to go on: Europe
  PMC's `journalInfo.journal.title`, PubMed's `Journal/Title` XML
  element, Semantic Scholar's `venue` field, and OpenAlex's
  `primary_location.source.display_name` (falling back to the older
  `host_venue.display_name` shape). `None` if the source doesn't expose
  one for that result.
- **Cost/latency tradeoff.** One additional blocking Gemini call per
  *newly-found* paper, on top of the keyword-generation call and the
  paper-search HTTP fan-out — for a grade request that turns up many new
  papers, this adds meaningfully to total request time (sequential, not
  concurrent, in this pass). Acceptable for this debug-stage feature's
  volume; a candidate for a future concurrent/batched grading pass if
  paper counts grow.
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
keywords -> search + persist papers -> count total stored papers for
that ingredient -> **debug grade assignment**: `ingredient.is_graded =
True` and `ingredient.grade_badge_text = f"{paper_count} / {paper_count}
/ {paper_count}"` — there's no real grading algorithm yet, this is
purely so the badge shows something derived from real (paper-count) data
rather than a static placeholder. Commits everything as one transaction;
rolls back and raises `GradingError` on failure. Raises `GradingError`
(not paper-search's own exceptions) for keyword-generation failures too,
so the route only needs to catch one exception type.

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
    │   ├── NavBar.tsx            # Persistent top bar: "BSProof" logo (-> Home), Scan / Library links, debug Reset DB
    │   ├── Footer.tsx             # Persistent footer, reused on every screen
    │   ├── ImageUploader.tsx     # Upload button + image preview (styled to palette)
    │   ├── ProductCard.tsx       # Expandable product card (metadata + nested Ingredient accordion + grade badge + orange-on-expand text)
    │   ├── IngredientCard.tsx    # Accordion card, two variants: 'nested' (dosage/%DV/research) and 'standalone' (grade badge + 3 placeholder info blocks + real StudiesList "Science Info" block + orange-on-expand text)
    │   ├── StudiesList.tsx       # Paginated (5/page) "List of Studies" panel — ResearchPaper rows, info modal, external-link button (Phase 2)
    │   └── GradeBadge.tsx        # Shared top-right grade pill/button (graded vs. ungraded), used by ProductCard + standalone IngredientCard
    ├── screens/
    │   ├── HomeScreen.tsx        # Marketing hero (full-width, looping video background via expo-video) + "Why BSProof?" info section (20% inset) + Footer
    │   ├── ScanScreen.tsx        # ImageUploader + Analyze button + raw-JSON Results section + Footer
    │   ├── LibraryScreen.tsx     # Search (live suggestions) + Explore (Products/Ingredients cards) + Footer
    │   └── ResultsScreen.tsx     # Back button + title/filter row, ProductCard/IngredientCard list, + Footer
    ├── services/
    │   └── api.ts                # API_BASE_URL, uploadSupplementImage(), fetchSuggestions(), searchSupplements(), fetchProductDetail(), fetchIngredientDetail(), gradeIngredient(), resetDatabase()
    └── utils/
        └── animations.ts          # animateCardToggle() — shared LayoutAnimation helper for accordion cards
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
  `journal_score`), "Methodology & Sample" (`sample_info` +
  `sample_score`), "Funding & Bias" (`funding_status` + `funding_score`
  — as of rubric v1.1 this one can be negative, e.g. "-6 pts" for
  penalized industry-biased funding; rendered as plain signed text, no
  special-casing needed since `total_score` (shown at the top) is
  already the post-penalty, 0-100-clamped figure)
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
