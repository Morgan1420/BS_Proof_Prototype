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
    │   └── routes.py       # `router` (POST /scan), `search_router` (/supplements/*), `products_router` (/products/{id}), `dev_router` (/dev/*)
    ├── schemas/
    │   ├── supplement.py   # Ingredient, SupplementAnalysis — Pydantic I/O models (Gemini + API response)
    │   ├── search.py       # FilterType, ResultType, SearchResultItem (now with nested ingredients), SearchResponse, SuggestResponse
    │   ├── dev.py           # MockDataResetResponse
    │   └── (supplement.py adds LinkedIngredientResponse, ProductDetailResponse)
    ├── models/
    │   ├── schemas.py      # ScanResponse (superseded by schemas/supplement.py; unused)
    │   └── supplement.py   # Product, Ingredient, ProductIngredientLink — SQLModel ORM tables (M2M)
    └── services/
        ├── vision.py       # Gemini API calls for label parsing
        ├── storage.py      # save_scan() (M2M find-or-create), delete_all_data(), delete_mock_data() (legacy, unused by the route)
        └── search.py       # suggest() / search() queries, get_linked_ingredients()/get_product_detail() (explicit joins)
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

## Frontend Structure

```
frontend/
├── App.tsx                     # Thin re-export of src/App.tsx (keeps index.ts's import path stable)
├── index.ts                    # Expo entry point, registers App as the root component
└── src/
    ├── App.tsx                 # Root: SafeAreaProvider + NavigationContainer + persistent NavBar + Stack.Navigator
    ├── theme.ts                 # Strict color palette, typography, spacing, layout (20% screen inset) tokens
    ├── navigation/
    │   ├── types.ts             # RootStackParamList (Home/Scan/Library/ResultsScreen), FilterType
    │   └── navigationRef.ts     # Imperative nav ref, used by NavBar (which renders outside the Stack tree)
    ├── components/
    │   ├── NavBar.tsx            # Persistent top bar: "BSProof" logo (-> Home), Scan / Library links, debug Reset DB
    │   ├── Footer.tsx             # Persistent footer, reused on every screen
    │   ├── ImageUploader.tsx     # Upload button + image preview (styled to palette)
    │   ├── ProductCard.tsx       # Expandable product card (metadata + nested Ingredient accordion)
    │   └── IngredientCard.tsx    # Accordion card: dosage/%DV or canonical RDA/research + placeholder
    ├── screens/
    │   ├── HomeScreen.tsx        # Marketing hero (full-width) + "Why BSProof?" info section (20% inset) + Footer
    │   ├── ScanScreen.tsx        # ImageUploader + Analyze button + raw-JSON Results section + Footer
    │   ├── LibraryScreen.tsx     # Search (live suggestions) + Explore (Products/Ingredients cards) + Footer
    │   └── ResultsScreen.tsx     # Back button + title/filter row, ProductCard/IngredientCard list, + Footer
    └── services/
        └── api.ts                # API_BASE_URL, uploadSupplementImage(), fetchSuggestions(), searchSupplements(), fetchProductDetail(), resetDatabase()
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

### Expandable cards (`ProductCard`, `IngredientCard`)

`ProductCard` (collapsed: name + brand + chevron; expanded: a metadata
block — full name, brand, serving size, scan date — plus its ingredients
rendered as `IngredientCard`s) and `IngredientCard` (collapsed: name +
quick dose summary; expanded: dosage info plus a research/metadata
placeholder box) both use **controlled** expansion — the parent owns an
`expandedId` state and passes each child `isExpanded`/`onToggle`, giving
single-expansion ("accordion") behavior for free within any group that
shares one state value.

This is used at two levels: `ProductCard` owns `expandedIngredientId` for
*its own* nested ingredient list, and `ResultsScreen` separately owns
`expandedIngredientId` for standalone ingredient results in its flat list
(ingredients not nested under any product card) — these are two
independent accordions, not one shared across the whole screen.

`IngredientCard`'s `Ingredient` type carries two distinct optional field
sets, and the component renders whichever is present:
- **Product-specific** (`amount`/`unit`/`dailyValue`/`productName`) —
  populated when nested inside a `ProductCard`, representing that
  product's own dosage of the ingredient (once available — see the gap
  below).
- **Canonical** (`recommendedDailyDosage`/`scientificData`/
  `productCount`) — populated for standalone ingredient results on
  `ResultsScreen`, since the backend's M2M refactor made `Ingredient` a
  shared row with no single product's dosage attached. The header's dose
  summary falls back to `"RDA: {recommendedDailyDosage}"`, the dosage row
  falls back to a "Recommended Daily Dosage" label, and the research
  placeholder box shows `scientificData` (currently always the backend's
  own placeholder value, `"n/a"`) instead of the hardcoded coming-soon text
  when it's present.

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
`src/services/api.ts` and stores the raw response in local state, rendered
in a "Result" container as pretty-printed JSON (rather than only surfacing
it via `Alert.alert`, as the old combined Home/Scan screen did). Errors
still go through `Alert.alert`. An `isLoading` state disables the Analyze
button and shows a spinner while the request is in flight. On web, `api.ts`
fetches the picker's `blob:` URI into a real `Blob` before attaching it to
`FormData`, since React Native Web's `FormData` requires a `Blob`/`File`
rather than the `{ uri, name, type }` object shape used on iOS/Android.

Note: the frontend's `ScanResponse` type (`{ message: string }`) in
`api.ts` still reflects the old stub response shape. `ScanScreen` treats
the result as `unknown` for display purposes so it renders correctly
either way, but `api.ts` should eventually be updated to type the response
as `SupplementAnalysis` to match what the backend now actually returns.

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
