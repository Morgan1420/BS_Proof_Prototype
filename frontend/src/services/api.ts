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

/** A single ingredient row from POST /api/v1/scan's response — mirrors
 * the backend's app/schemas/supplement.py::Ingredient (the per-scan
 * parsed shape Gemini returns, not the canonical DB Ingredient row —
 * see that file's module docstring for the distinction). */
export interface ScannedIngredient {
  name: string;
  amount: string;
  unit: string;
  daily_value?: string | null;
}

/** Shape of the JSON body returned by POST /api/v1/scan on success —
 * mirrors the backend's SupplementAnalysis (app/schemas/supplement.py).
 * Replaces the old `{ message: string }` stub, which never actually
 * matched what the backend returns (a long-flagged "Known gap" in
 * docs/Architecture.md — ScanScreen previously worked around it by
 * treating the response as `unknown`). */
export interface SupplementAnalysis {
  product_name?: string | null;
  serving_size?: string | null;
  ingredients: ScannedIngredient[];
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
): Promise<SupplementAnalysis> {
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

  return (await response.json()) as SupplementAnalysis;
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
// Ingredient detail + grading (GET/POST /api/v1/ingredients/{id}...) — Phase 2
// ---------------------------------------------------------------------

/** Recognized paper letter grades (Phase 3 automated grading) — mirrors
 * docs/paper_grading_rubric.json's `grade_bands`. */
export type PaperGrade = 'A' | 'B' | 'C' | 'D' | 'E';

/**
 * ResearchPaper.status lifecycle values (Phase 6 ingredient relevance
 * verification) — mirrors the backend's PAPER_STATUS_ACTIVE /
 * PAPER_STATUS_DISCARDED_IRRELEVANT constants (app/models/research.py).
 * Named constants rather than bare string literals, same reasoning as
 * the backend side: IngredientCard.tsx compares against
 * PAPER_STATUS_DISCARDED_IRRELEVANT rather than the literal string.
 */
export const PAPER_STATUS_ACTIVE = 'ACTIVE';
export const PAPER_STATUS_DISCARDED_IRRELEVANT = 'DISCARDED_IRRELEVANT';

/**
 * Structured per-category rubric breakdown for a graded paper — mirrors
 * the backend's RubricEvaluationResponse (app/schemas/research.py),
 * which itself mirrors app/services/paper_grader.py's RubricEvaluation
 * shape and, in turn, ResearchPaper.rubric_evaluation's stored JSON.
 * Rendered by StudiesList's Rubric Breakdown modal.
 */
export interface RubricEvaluation {
  study_type: string;
  study_type_score: number;
  journal_reputation: string;
  journal_score: number;
  sample_info: string;
  sample_score: number;
  funding_status: string;
  funding_score: number;
  total_score: number;
  summary_notes: string;
}

/**
 * A single stored research paper — mirrors the backend's
 * ResearchPaperResponse (app/schemas/research.py), which itself mirrors
 * the ResearchPaper table (app/models/research.py). Rendered by
 * StudiesList.tsx.
 */
export interface ResearchPaper {
  id: number;
  title: string;
  abstract?: string | null;
  authors?: string | null;
  publication_date?: string | null;
  source_url: string;
  source_domain: string;
  ingredient_id: number;
  /** Every Gemini-generated search keyword that surfaced this paper
   * (backend: ResearchPaper.keywords, comma-separated string parsed into
   * this array server-side). Rendered as "Matched Keywords" pill tags in
   * StudiesList's paper info modal. Always present as an array (possibly
   * empty), never undefined. */
  keywords: string[];
  /** Letter grade from the Phase 3 automated paper-grading pipeline
   * (app/services/paper_grader.py) — null until that pipeline has
   * successfully graded this paper (grading is best-effort at ingestion
   * time; a Gemini/parsing failure leaves a paper permanently
   * ungraded). Typed loosely as `string` rather than `PaperGrade` at
   * this API boundary since the backend's `Optional[str]` column isn't
   * enforced beyond "A"-"E" by a DB constraint — StudiesList validates
   * against PaperGrade before rendering a badge. */
  grade?: string | null;
  /** Total rubric score, 0-100. Null iff `grade` is null. */
  grade_score?: number | null;
  /** Full per-category breakdown backing `grade`/`grade_score`. Null iff
   * `grade` is null. */
  rubric_evaluation?: RubricEvaluation | null;
  /** One of PAPER_STATUS_ACTIVE / PAPER_STATUS_DISCARDED_IRRELEVANT
   * above (Phase 6). In practice a DISCARDED_IRRELEVANT paper never
   * comes back from GET /api/v1/ingredients/{id} or the grade-ingredient
   * endpoint — the backend filters those out server-side (see
   * app/services/search.py::get_ingredient_papers) — but the on-demand
   * single-paper-grade endpoint (gradePaper below) returns the
   * just-graded paper regardless of outcome, so IngredientCard checks
   * this field to remove a just-discarded paper from local state — see
   * handlePaperGraded in IngredientCard.tsx. */
  status: string;
  /** 2-4 short, factual study-level findings extracted by the same
   * Gemini call that produces `grade`/`rubric_evaluation` (Phase 19 —
   * see backend/app/services/paper_grader.py). Null until this paper is
   * graded (same "null iff grade is null" convention as grade_score/
   * rubric_evaluation above) — StudiesList's paper info modal renders a
   * "No specific conclusions extracted for this source yet." fallback
   * for that case. */
  extracted_conclusions?: string[] | null;
}

/**
 * Structured per-category rubric breakdown backing a conclusion's
 * `confidence_score`/`confidence_grade` — mirrors the backend's
 * PaperConclusionResponse.rubric_evaluation (app/schemas/research.py),
 * which is itself a loose `Dict[str, Any]` server-side (not a strict
 * schema, unlike RubricEvaluation above) — see that field's docstring
 * for why: a merged conclusion's stored evaluation is built by spreading
 * a previous dict and overwriting a few keys
 * (conclusion_grader.py::process_paper_conclusions), so its exact key
 * set is less rigidly fixed than a freshly-graded paper's. Every field
 * here is optional for the same reason.
 */
export interface ConclusionRubricEvaluation {
  evidence_strength?: string;
  evidence_strength_score?: number;
  cross_paper_consensus?: string;
  cross_paper_consensus_score?: number;
  claim_specificity?: string;
  claim_specificity_score?: number;
  total_score?: number;
  summary_notes?: string;
}

/**
 * A single synthesized cross-paper conclusion/claim for an ingredient
 * (Phase 5) — mirrors the backend's PaperConclusionResponse
 * (app/schemas/research.py), which itself mirrors the PaperConclusion
 * table (app/models/research.py). Rendered by RecommendedUsesList.tsx.
 */
export interface PaperConclusion {
  id: number;
  ingredient_id: number;
  claim_summary: string;
  detailed_conclusion?: string | null;
  dosage_mentioned?: string | null;
  rubric_evaluation?: ConclusionRubricEvaluation | null;
  confidence_score: number;
  /** Loosely typed as `string` rather than `PaperGrade` at this API
   * boundary, same reasoning as ResearchPaper.grade above. */
  confidence_grade: string;
  cross_paper_consensus: number;
  supporting_paper_ids: number[];
  contradicting_paper_ids: number[];
}

/**
 * A single official government/regulatory reference link for an
 * ingredient (Phase 7) — mirrors the backend's VerifiedResourceResponse
 * (app/schemas/research.py), which itself mirrors the VerifiedResource
 * table (app/models/research.py). Rendered by VerifiedResourcesList.tsx.
 *
 * Every resource the backend returns has already cleared its strict
 * domain allow-list (`.gov`, `.europa.eu`, `ncbi.nlm.nih.gov`,
 * `efsa.europa.eu` — see backend/app/services/resource_fetcher.py) before
 * ever being persisted, so the frontend never needs to re-validate
 * `domain` itself — only display it (and derive an "NIH"/"USDA"/"EFSA"
 * -style authority badge from it, see VerifiedResourcesList.tsx).
 *
 * `grade`/`score`/`reasoning_summary` (Phase 8 — see
 * backend/app/services/resource_grader.py) are a separate quality signal
 * layered on top of that domain gate — all three are `null` until the
 * backend successfully grades this resource (best-effort at fetch time;
 * a Gemini/parsing failure leaves a resource permanently ungraded rather
 * than retried, same convention as ResearchPaper.grade), so the frontend
 * must handle a null `grade` as a normal, expected state (render no
 * badge), not an error.
 */
export interface VerifiedResource {
  id: number;
  ingredient_id: number;
  title: string;
  publisher: string;
  url: string;
  domain: string;
  summary?: string | null;
  /** Typed loosely as `string` rather than `PaperGrade` at this API
   * boundary, same reasoning as ResearchPaper.grade/PaperConclusion.
   * confidence_grade — the backend's `Optional[str]` column isn't
   * enforced beyond "A"-"E" by a DB constraint, so VerifiedResourcesList
   * validates against `PaperGrade` (via utils/grades.ts's `isPaperGrade`/
   * `GRADE_COLORS`, same A-E scale reused across every graded entity in
   * this app — see that file's module docstring) before rendering a
   * badge. */
  grade?: string | null;
  /** Total rubric score, 0-100 (strictly clamped server-side — see
   * resource_grader.py's "Score Calculation Guard"). Null iff `grade` is
   * null. */
  score?: number | null;
  /** Concise 1-2 sentence rationale for the overall evaluation. Null iff
   * `grade` is null. */
  reasoning_summary?: string | null;
  /** 2-4 short, factual conclusions extracted using this provider's own
   * extraction_instructions (docs/verified_resource_apis.json) — Phase
   * 19, see backend/app/services/resource_extractor.py. Null until Stage
   * 1 extraction runs for this resource (independent of `grade` — this
   * comes from the extraction pipeline, not the grading one).
   * VerifiedResourcesList's info modal renders a "No specific
   * conclusions extracted for this source yet." fallback when empty. */
  extracted_conclusions?: string[] | null;
}

/** Shape of the JSON body returned by GET /api/v1/ingredients/{id}. */
export interface IngredientDetailResponse {
  id: number;
  name: string;
  recommended_daily_dosage?: string | null;
  scientific_data?: string | null;
  product_count: number;
  is_graded: boolean;
  grade_badge_text?: string | null;
  /** Gemini-synthesized 1-2 sentence overview combining BOTH graded
   * ResearchPaper findings and VerifiedResource official guidance
   * (NIH/USDA/EFSA/Health Canada/...) for this ingredient — see
   * backend/app/services/conclusion_grader.py::synthesize_ingredient_summary.
   * `null`/`undefined` until a grade request has both evidence to
   * synthesize from and a successful Gemini call — IngredientCard.tsx
   * falls back to a client-computed heuristic sentence in that case (see
   * its `scientificSummary`). Not yet included in GradeIngredientResponse
   * below — same "frontend re-fetches ingredient detail to see it" caveat
   * as `conclusions`/`verified_resources`. */
  summary_description?: string | null;
  papers: ResearchPaper[];
  /** Every *active* synthesized PaperConclusion for this ingredient,
   * highest-confidence first (see app/services/search.py::
   * get_ingredient_conclusions on the backend). Not yet included in
   * GradeIngredientResponse below — see that interface's docstring. */
  conclusions: PaperConclusion[];
  /** Every stored VerifiedResource for this ingredient (Phase 7 — see
   * app/services/search.py::get_ingredient_resources on the backend).
   * Same "not yet included in GradeIngredientResponse" caveat as
   * `conclusions` above. */
  verified_resources: VerifiedResource[];
}

/** Shape of the JSON body returned by
 * POST /api/v1/ingredients/{id}/grade. Deliberately has no `conclusions`
 * field — the backend's GradeIngredientResponse only refreshes `papers`
 * (see app/schemas/research.py's docstring) — so IngredientCard follows
 * up this call with a plain fetchIngredientDetail() to pick up anything
 * the Phase 5 pipeline just synthesized. */
export interface GradeIngredientResponse {
  status: string;
  ingredient_id: number;
  is_graded: boolean;
  grade_badge_text?: string | null;
  papers_found: number;
  papers: ResearchPaper[];
}

/**
 * Fetches a single canonical ingredient plus its full stored research-
 * paper list from GET /api/v1/ingredients/{id}. Used by standalone
 * IngredientCard to populate its "List of Studies" panel (papers already
 * persisted by a prior grade request — this never triggers a new paper
 * search itself).
 */
export async function fetchIngredientDetail(
  ingredientId: number
): Promise<IngredientDetailResponse> {
  return getJson<IngredientDetailResponse>(
    `${API_BASE_URL}/api/v1/ingredients/${ingredientId}`
  );
}

/**
 * Runs the backend's Phase 2 debug grading pipeline for a single
 * canonical ingredient (see backend/app/services/grading.py): Gemini
 * generates search keywords, the backend queries PubMed/Europe PMC/
 * Semantic Scholar for each one, persists paper metadata, and assigns a
 * debug grade (the stored paper count formatted as "N / N / N"). Can
 * take several seconds — it's several sequential external network calls
 * server-side — which is why standalone IngredientCard shows a loading
 * spinner on the grade button while this is in flight.
 */
export async function gradeIngredient(
  ingredientId: number
): Promise<GradeIngredientResponse> {
  let response: Response;
  try {
    response = await fetch(
      `${API_BASE_URL}/api/v1/ingredients/${ingredientId}/grade`,
      {
        method: 'POST',
        headers: { Accept: 'application/json' },
      }
    );
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

  return (await response.json()) as GradeIngredientResponse;
}

// ---------------------------------------------------------------------
// Single-paper grading (POST /api/v1/papers/{id}/grade) — on-demand
// ---------------------------------------------------------------------

/** Shape of the JSON body returned by
 * POST /api/v1/papers/{paper_id}/grade. */
export interface GradePaperResponse {
  status: string;
  paper: ResearchPaper;
}

/**
 * Grades exactly one already-stored research paper on demand (see
 * backend/app/services/paper_grader.py::grade_single_paper) — triggered
 * by tapping a paper's gray "(-)" ungraded badge in StudiesList, rather
 * than waiting for the next full ingredient re-grade
 * (POST /api/v1/ingredients/{id}/grade above, which grades every
 * newly-found paper automatically but doesn't retroactively grade
 * papers that failed grading on an earlier run). Idempotent server-side:
 * calling this on an already-graded paper just returns it unchanged, no
 * extra Gemini call.
 */
export async function gradePaper(paperId: number): Promise<GradePaperResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/v1/papers/${paperId}/grade`, {
      method: 'POST',
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

  return (await response.json()) as GradePaperResponse;
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
