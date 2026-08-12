import { Platform } from 'react-native';

/**
 * Base URL for the FastAPI backend (see backend/app/main.py).
 *
 * `localhost` means a different machine depending on where the JS is
 * actually running, so one value can't cover every target automatically.
 * Set API_BASE_URL to match your current setup:
 *
 * - iOS Simulator: the simulator shares your Mac's network stack, so
 *   `localhost` refers to your dev machine directly.
 *     http://localhost:8000
 *
 * - Android Emulator: the emulator has its own virtual network. `10.0.2.2`
 *   is a special alias Android provides that routes to your host machine's
 *   `localhost`. Plain `localhost` inside the emulator means the emulator
 *   itself, not your dev machine, and requests will fail to connect.
 *     http://10.0.2.2:8000
 *
 * - Physical device (Expo Go / dev build) or Expo web: the device is a
 *   separate machine on your Wi-Fi network, so neither of the above works.
 *   Use your dev machine's LAN IP instead — the same host Metro prints when
 *   you run `npx expo start` (`Metro: exp://<LAN-IP>:8081`). The backend's
 *   CORS config already allows local 192.168.x.x / 10.x.x.x / 172.16-31.x.x
 *   addresses (see backend/app/main.py), so no backend changes are needed
 *   when switching to this option.
 *     http://<YOUR-LAN-IP>:8000
 */
export const API_BASE_URL = 'http://192.168.31.229:8000'; // TODO: set for your environment (see comments above)

const SCAN_ENDPOINT = `${API_BASE_URL}/api/v1/scan`;

/** Shape of the JSON body returned by POST /api/v1/scan on success. */
export interface ScanResponse {
  message: string;
}

/** Shape of a FastAPI error body, e.g. { "detail": "..." }. */
interface ApiErrorBody {
  detail?: string;
}

/** The { uri, name, type } object React Native's FormData expects for files. */
interface UploadableImage {
  uri: string;
  name: string;
  type: string;
}

const MIME_TYPE_BY_EXTENSION: Readonly<Record<string, string>> = {
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  png: 'image/png',
  webp: 'image/webp',
};

/**
 * Derives a filename and MIME type from a local file URI so the multipart
 * upload has the metadata the backend's MIME-type validation checks for.
 */
function inferImagePayload(uri: string): UploadableImage {
  const filename = uri.split('/').pop() || `upload-${Date.now()}.jpg`;
  const extensionMatch = /\.(\w+)$/.exec(filename);
  const extension = (extensionMatch?.[1] ?? 'jpg').toLowerCase();
  const type = MIME_TYPE_BY_EXTENSION[extension] ?? 'image/jpeg';

  return { uri, name: filename, type };
}

/**
 * Uploads a supplement label photo to POST /api/v1/scan and returns the
 * parsed JSON response.
 *
 * @param uri Local file URI of the image, as returned by expo-image-picker.
 * @throws Error with a human-readable message on network failure or a
 *   non-2xx response (using the backend's `detail` field when available).
 */
export async function uploadSupplementImage(
  uri: string
): Promise<ScanResponse> {
  const image = inferImagePayload(uri);

  const formData = new FormData();

  if (Platform.OS === 'web') {
    // On web, React Native's `FormData` IS the browser's native FormData,
    // which requires a real Blob/File for a file field — a plain
    // { uri, name, type } object just gets stringified into a text field,
    // so the backend never receives an actual file (this is what caused
    // the 422 "Unprocessable Content" response). The image `uri` here is a
    // blob: URL from expo-image-picker, so we re-fetch it to get the
    // underlying Blob before attaching it.
    const blobResponse = await fetch(image.uri);
    const blob = await blobResponse.blob();
    formData.append('file', blob, image.name);
  } else {
    // On iOS/Android, React Native's FormData polyfill expects this
    // { uri, name, type } shape for file fields instead of a real Blob.
    // The DOM FormData typings don't model it, hence the cast.
    formData.append('file', {
      uri: image.uri,
      name: image.name,
      type: image.type,
    } as unknown as Blob);
  }

  let response: Response;
  try {
    response = await fetch(SCAN_ENDPOINT, {
      method: 'POST',
      body: formData,
      headers: {
        Accept: 'application/json',
        // Do NOT set Content-Type manually — fetch derives the multipart
        // boundary automatically from the FormData instance. Setting it
        // explicitly breaks the boundary and the backend will fail to
        // parse the upload.
      },
    });
  } catch (networkError) {
    const reason =
      networkError instanceof Error
        ? networkError.message
        : String(networkError);
    throw new Error(
      `Could not reach the server at ${API_BASE_URL}. Check API_BASE_URL in ` +
        `src/services/api.ts and confirm the backend is running. (${reason})`
    );
  }

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`;
    try {
      const errorBody = (await response.json()) as ApiErrorBody;
      if (errorBody.detail) {
        detail = errorBody.detail;
      }
    } catch {
      // Response body wasn't JSON; fall back to the generic message above.
    }
    throw new Error(detail);
  }

  return (await response.json()) as ScanResponse;
}

// ---------------------------------------------------------------------
// Supplement search / suggest (GET /api/v1/supplements/*)
// ---------------------------------------------------------------------

/** Mirrors the backend's FilterType enum (app/schemas/search.py). */
export type FilterType = 'all' | 'products' | 'ingredients';

/** Mirrors the backend's ResultType enum (app/schemas/search.py). */
export type ResultType = 'product' | 'ingredient';

/** Shape of the JSON body returned by GET /api/v1/supplements/suggest. */
export interface SuggestResponse {
  query: string;
  suggestions: string[];
}

/**
 * A single Ingredient joined with one Product's specific dosage for it —
 * mirrors the backend's LinkedIngredientResponse (app/schemas/supplement.py).
 * `amount`/`unit`/`daily_value_percentage` come from the
 * ProductIngredientLink junction row; `recommended_daily_dosage`/
 * `scientific_data` come from the canonical Ingredient row.
 */
export interface LinkedIngredient {
  id: number;
  name: string;
  amount?: string | null;
  unit?: string | null;
  daily_value_percentage?: string | null;
  recommended_daily_dosage?: string | null;
  scientific_data?: string | null;
}

/**
 * A single search/browse result, matching the backend's SearchResultItem.
 * `type` determines which of the optional fields are populated: `brand`
 * and `ingredients` for products; `recommended_daily_dosage`/
 * `scientific_data`/`product_count` for ingredients.
 *
 * As of the backend's Many-to-Many schema refactor (Product <->
 * Ingredient via a ProductIngredientLink junction table), an ingredient
 * result no longer carries a product-specific dosage (amount/unit/
 * daily_value) or a single parent product name — Ingredient is now
 * canonical/shared data that can belong to zero, one, or many products.
 * Ingredient results instead surface that canonical metadata.
 *
 * `ingredients` is populated for product results via an explicit
 * server-side join (see app/services/search.py::get_linked_ingredients) —
 * always `[]` for ingredient-type results.
 */
export interface SearchResultItem {
  id: number;
  type: ResultType;
  name: string;
  brand?: string | null;
  ingredients?: LinkedIngredient[];
  recommended_daily_dosage?: string | null;
  scientific_data?: string | null;
  product_count?: number | null;
}

/** Shape of the JSON body returned by GET /api/v1/products/{id}. */
export interface ProductDetailResponse {
  id: number;
  name: string;
  brand?: string | null;
  serving_size?: string | null;
  created_at?: string | null;
  ingredients: LinkedIngredient[];
}

/** Shape of the JSON body returned by GET /api/v1/supplements/search. */
export interface SearchResponse {
  query?: string | null;
  filter_type: FilterType;
  count: number;
  results: SearchResultItem[];
}

/**
 * GETs `url` and parses the JSON body as `T`, with the same network-error
 * and non-2xx handling as uploadSupplementImage above (including FastAPI's
 * validation-error shape, where `detail` is an array of objects rather
 * than a plain string).
 */
async function getJson<T>(url: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
    });
  } catch (networkError) {
    const reason =
      networkError instanceof Error
        ? networkError.message
        : String(networkError);
    throw new Error(
      `Could not reach the server at ${API_BASE_URL}. Check API_BASE_URL in ` +
        `src/services/api.ts and confirm the backend is running. (${reason})`
    );
  }

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`;
    try {
      const errorBody = (await response.json()) as { detail?: unknown };
      if (typeof errorBody.detail === 'string') {
        detail = errorBody.detail;
      } else if (errorBody.detail) {
        // FastAPI's 422 validation errors return `detail` as an array of
        // { loc, msg, type } objects rather than a string.
        detail = JSON.stringify(errorBody.detail);
      }
    } catch {
      // Response body wasn't JSON; fall back to the generic message above.
    }
    throw new Error(detail);
  }

  return (await response.json()) as T;
}

/**
 * Fetches up to `limit` live autocomplete suggestions for `query` from
 * GET /api/v1/supplements/suggest. The backend itself no-ops (returns an
 * empty list) for queries shorter than 3 characters, so callers don't
 * strictly need to guard that — but the Library screen still debounces
 * and gates on length client-side to avoid firing a request per keystroke.
 */
export async function fetchSuggestions(
  query: string,
  limit = 5
): Promise<SuggestResponse> {
  const params = new URLSearchParams({ query, limit: String(limit) });
  return getJson<SuggestResponse>(
    `${API_BASE_URL}/api/v1/supplements/suggest?${params.toString()}`
  );
}

/**
 * Searches or browses products/ingredients via
 * GET /api/v1/supplements/search. Omitting `query` browses all rows of
 * `filterType` instead of filtering by name (used by the Library screen's
 * "Products" / "Ingredients" explore cards).
 */
export async function searchSupplements(params: {
  query?: string;
  filterType?: FilterType;
  limit?: number;
}): Promise<SearchResponse> {
  const searchParams = new URLSearchParams();
  if (params.query) {
    searchParams.set('query', params.query);
  }
  if (params.filterType) {
    searchParams.set('filter_type', params.filterType);
  }
  searchParams.set('limit', String(params.limit ?? 20));

  return getJson<SearchResponse>(
    `${API_BASE_URL}/api/v1/supplements/search?${searchParams.toString()}`
  );
}

/**
 * Fetches a single product with its full linked-ingredient list from
 * GET /api/v1/products/{id}. Not currently called anywhere in the app —
 * ResultsScreen/ProductCard get their ingredient data straight from
 * GET /api/v1/supplements/search's nested `ingredients` field (see
 * SearchResultItem above) — but this is available for a future
 * dedicated product-detail screen without another backend round trip.
 */
export async function fetchProductDetail(
  productId: number
): Promise<ProductDetailResponse> {
  return getJson<ProductDetailResponse>(
    `${API_BASE_URL}/api/v1/products/${productId}`
  );
}

// ---------------------------------------------------------------------
// Dev-only debug tools (GET /api/v1/dev/*)
// ---------------------------------------------------------------------

/** Shape of the JSON body returned by DELETE /api/v1/dev/mock-data. */
export interface MockDataResetResponse {
  status: string;
  message: string;
}

/**
 * Dev/debug only: completely wipes every Product/Ingredient/link row on
 * the backend (not just is_mock=True ones). See NavBar's "Reset DB"
 * button. Not authenticated — this hits an unauthenticated backend
 * endpoint intended for local development only (see
 * backend/app/api/routes.py::delete_mock_data, which now calls
 * storage.delete_all_data under the hood).
 */
export async function resetDatabase(): Promise<MockDataResetResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/v1/dev/mock-data`, {
      method: 'DELETE',
      headers: { Accept: 'application/json' },
    });
  } catch (networkError) {
    const reason =
      networkError instanceof Error
        ? networkError.message
        : String(networkError);
    throw new Error(
      `Could not reach the server at ${API_BASE_URL}. Check API_BASE_URL in ` +
        `src/services/api.ts and confirm the backend is running. (${reason})`
    );
  }

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`;
    try {
      const errorBody = (await response.json()) as { detail?: unknown };
      if (typeof errorBody.detail === 'string') {
        detail = errorBody.detail;
      } else if (errorBody.detail) {
        detail = JSON.stringify(errorBody.detail);
      }
    } catch {
      // Response body wasn't JSON; fall back to the generic message above.
    }
    throw new Error(detail);
  }

  return (await response.json()) as MockDataResetResponse;
}
