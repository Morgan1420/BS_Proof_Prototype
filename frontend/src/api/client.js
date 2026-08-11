// Thin API client for the FastAPI backend (see backend/app/api/routes.py).
import axios from "axios";
import { Platform } from "react-native";
import { API_BASE_URL } from "../config";

const client = axios.create({
  baseURL: API_BASE_URL,
  // A single Gemini vision call can still legitimately take a while once
  // the backend's rate-limit handling kicks in (see gemini_client.py's
  // RATE_LIMIT_MAX_RETRIES / instant model rotation). 10 minutes gives
  // real headroom without disabling the timeout outright (timeout: 0),
  // which would leave the UI stuck forever on a truly dead connection.
  timeout: 600000,
});

/**
 * Upload a label image and get back the extracted ScanResult immediately.
 *
 * The backend now does ONE thing per scan -- a single Gemini vision call
 * (POST /api/scan) -- and returns the full result synchronously; there's
 * no job id, no background grading, and nothing to poll for.
 *
 * `image` is an asset object from expo-image-picker's launch*Async result
 * (`result.assets[0]`) -- needs `.uri`, and ideally `.fileName` / `.mimeType`
 * (both best-effort since not every platform/picker result provides them).
 */
export async function scanLabel(image) {
  const formData = new FormData();
  const uri = image.uri;
  const fileName = image.fileName || uri.split("/").pop() || "label.jpg";
  const mimeType = image.mimeType || guessMimeType(fileName);

  if (Platform.OS === "web") {
    // On web, `uri` is typically a blob:/data: URL -- fetch it back into a
    // real Blob so FormData sends binary image content, not a bare string.
    const fetched = await fetch(uri);
    const blob = await fetched.blob();
    formData.append("file", blob, fileName);
  } else {
    // React Native's FormData accepts this {uri, name, type} shape directly.
    formData.append("file", { uri, name: fileName, type: mimeType });
  }

  const response = await client.post("/scan", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return response.data;
}

/**
 * Fetch every scan saved so far (GET /api/ingredients), straight from
 * the backend's data/scanned_ingredients.json.
 *
 * Returns an array of ScanResult objects:
 *   [{ scan_id, scanned_at, product: {...}, ingredients: [...] }, ...]
 */
export async function getIngredients() {
  const response = await client.get("/ingredients");
  return response.data;
}

/**
 * Grade exactly one saved ingredient (POST /api/ingredients/{id}/grade):
 * a real PubMed literature search + one Gemini SIFG evaluation call for
 * that ingredient only. Can legitimately take a while (same Gemini
 * rate-limit pause as a scan), which is why this shares the client's
 * long default timeout.
 *
 * Returns the updated ingredient object -- `grade_status: "graded"` plus
 * `sifg_grade` / `sifg_score` / `efficacy_safety_evaluation` /
 * `dosage_appropriateness` / `evidence_summary` / `raw_consensus` /
 * `graded_at` -- ready to replace the ingredient's row in place.
 */
export async function gradeIngredient(ingredientId) {
  const response = await client.post(`/ingredients/${ingredientId}/grade`);
  return response.data;
}

function guessMimeType(fileName) {
  const ext = (fileName.split(".").pop() || "").toLowerCase();
  if (ext === "png") return "image/png";
  if (ext === "webp") return "image/webp";
  if (ext === "heic" || ext === "heif") return "image/heic";
  return "image/jpeg";
}
