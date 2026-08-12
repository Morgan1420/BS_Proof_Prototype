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
│   └── scanned_ingredients.json  # Append-only log of parsed scans (gitignored)
└── app/
    ├── main.py             # FastAPI app instance, CORS config, router mounting
    ├── core/
    │   └── config.py       # Settings (GEMINI_API_KEY, GEMINI_MODEL) via pydantic-settings
    ├── api/
    │   └── routes.py       # Route handlers (currently: POST /api/v1/scan)
    ├── schemas/
    │   └── supplement.py   # Ingredient, SupplementAnalysis Pydantic models
    ├── models/
    │   └── schemas.py      # ScanResponse (superseded by schemas/supplement.py; unused)
    └── services/
        ├── vision.py       # Gemini API calls for label parsing
        └── storage.py      # Persistence to scanned_ingredients.json
```

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
- **Note:** if persistence to `scanned_ingredients.json` fails (e.g. disk
  issue), the request still succeeds and returns the analysis — the failure
  is only logged server-side, so a storage hiccup doesn't lose an otherwise
  successful Gemini result.

## Pipeline

1. **Image Upload** — Expo client sends image to `POST /api/v1/scan`.
2. **Vision Processing** — `app/services/vision.py::analyze_supplement_label`
   sends the image bytes to Gemini (`google-genai` SDK) with a strict system
   prompt and `response_schema=SupplementAnalysis`, so the model returns
   JSON that maps directly onto the Pydantic model (`response.parsed`, with
   a manual `model_validate_json` fallback).
3. **Data Persistence** — `app/services/storage.py::save_scan` appends the
   validated payload to `backend/data/scanned_ingredients.json`, adding a
   `scan_id` (UUID4) and UTC `scanned_at` timestamp. A `threading.Lock`
   guards concurrent writes within the process (not across multiple worker
   processes — fine for this single-process prototype).

Both `analyze_supplement_label` (blocking network call) and `save_scan`
(blocking file I/O) are synchronous functions run via
`starlette.concurrency.run_in_threadpool` from the async route handler, so
they don't block the event loop.

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
    ├── theme.ts                 # Strict color palette, typography, spacing tokens
    ├── navigation/
    │   ├── types.ts             # RootStackParamList (HomeScreen, ScanScreen, LibraryScreen)
    │   └── navigationRef.ts     # Imperative nav ref, used by NavBar (which renders outside the Stack tree)
    ├── components/
    │   ├── NavBar.tsx            # Persistent top bar: "BSProof" logo (-> Home), Scan / Library links
    │   ├── Footer.tsx             # Persistent footer, reused on every screen
    │   └── ImageUploader.tsx     # Upload button + image preview (styled to palette)
    ├── screens/
    │   ├── HomeScreen.tsx        # Marketing hero + "Why BSProof?" info section + Footer
    │   ├── ScanScreen.tsx        # ImageUploader + Analyze button + raw-JSON Results section + Footer
    │   └── LibraryScreen.tsx     # Placeholder ("Content Library - Coming Soon.") + Footer
    └── services/
        └── api.ts                # API_BASE_URL, uploadSupplementImage(), ScanResponse-shaped types
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
