// Thin API client for the FastAPI backend (see backend/app/api/v1/endpoints/scan.py).
import axios from "axios";
import { Platform } from "react-native";
import { API_BASE_URL } from "../config";

const client = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
});

/**
 * Upload a label image and kick off Phase 1 (vision parsing, synchronous)
 * plus Phase 2 (PubMed retrieval + consensus scoring, background) grading.
 *
 * Returns the backend's ScanResponse:
 *   { job_id, status, product_metadata, match_status, ingredients }
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
 * Fetch the current status/grade for one ingredient
 * (GET /api/v1/ingredients/{ingredient_id}).
 *
 * Normalizes the backend's three possible response shapes into one:
 *   - { done: true,  failed: false, grade: <IngredientGradeSchema> }
 *   - { done: true,  failed: true,  error: string }
 *   - { done: false, status: "pending" | "processing" }
 */
export async function getIngredientGrade(ingredientId) {
  const response = await client.get(`/ingredients/${ingredientId}`, {
    validateStatus: (status) => status === 200 || status === 202 || status === 404,
  });

  if (response.status === 404) {
    return { done: true, failed: true, error: "Ingredient not found." };
  }
  if (response.status === 202) {
    return { done: false, status: response.data.status };
  }
  // 200: either a completed IngredientGradeSchema, or a {status:"failed", error} body.
  if (response.data && response.data.status === "failed") {
    return { done: true, failed: true, error: response.data.error || "Grading failed." };
  }
  return { done: true, failed: false, grade: response.data };
}

function guessMimeType(fileName) {
  const ext = (fileName.split(".").pop() || "").toLowerCase();
  if (ext === "png") return "image/png";
  if (ext === "webp") return "image/webp";
  if (ext === "heic" || ext === "heif") return "image/heic";
  return "image/jpeg";
}
