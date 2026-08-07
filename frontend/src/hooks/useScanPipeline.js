import { useCallback, useEffect, useRef, useState } from "react";
import { getIngredientGrade, scanLabel } from "../api/client";
import { POLL_INTERVAL_MS, POLL_TIMEOUT_MS } from "../config";

/**
 * Owns the whole "pick an image -> POST /scan -> poll each ingredient" flow.
 *
 * phase: "idle" | "uploading" | "polling" | "done" | "error"
 * job: the raw ScanResponse from POST /scan (or null before the first scan)
 * ingredientResults: keyed by ingredient_id ->
 *   { raw_name, dose_amount, dose_unit,
 *     status: "pending" | "processing" | "completed" | "failed",
 *     grade?: <IngredientGradeSchema>, error?: string }
 */
export function useScanPipeline() {
  const [phase, setPhase] = useState("idle");
  const [job, setJob] = useState(null);
  const [ingredientResults, setIngredientResults] = useState({});
  const [errorMessage, setErrorMessage] = useState(null);

  const pollTimers = useRef({});
  const pollStartedAt = useRef({});

  const clearAllTimers = useCallback(() => {
    Object.values(pollTimers.current).forEach(clearTimeout);
    pollTimers.current = {};
  }, []);

  // Stop any in-flight polling if the component unmounts mid-scan.
  useEffect(() => () => clearAllTimers(), [clearAllTimers]);

  const pollOne = useCallback(async (ingredientId) => {
    try {
      const result = await getIngredientGrade(ingredientId);

      setIngredientResults((prev) => ({
        ...prev,
        [ingredientId]: {
          ...prev[ingredientId],
          status: result.done ? (result.failed ? "failed" : "completed") : result.status,
          grade: result.grade ?? prev[ingredientId]?.grade,
          error: result.error ?? prev[ingredientId]?.error,
        },
      }));

      if (result.done) {
        delete pollTimers.current[ingredientId];
        return;
      }

      const startedAt = pollStartedAt.current[ingredientId] || Date.now();
      if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
        setIngredientResults((prev) => ({
          ...prev,
          [ingredientId]: { ...prev[ingredientId], status: "failed", error: "Timed out waiting for grading." },
        }));
        delete pollTimers.current[ingredientId];
        return;
      }

      pollTimers.current[ingredientId] = setTimeout(() => pollOne(ingredientId), POLL_INTERVAL_MS);
    } catch (err) {
      setIngredientResults((prev) => ({
        ...prev,
        [ingredientId]: {
          ...prev[ingredientId],
          status: "failed",
          error: err.message || "Network error while polling for results.",
        },
      }));
      delete pollTimers.current[ingredientId];
    }
  }, []);

  const startScan = useCallback(
    async (image) => {
      clearAllTimers();
      setErrorMessage(null);
      setJob(null);
      setIngredientResults({});
      setPhase("uploading");

      try {
        const scanResponse = await scanLabel(image);
        setJob(scanResponse);

        const initialResults = {};
        scanResponse.ingredients.forEach((ing) => {
          initialResults[ing.ingredient_id] = {
            raw_name: ing.raw_name,
            dose_amount: ing.dose_amount,
            dose_unit: ing.dose_unit,
            status: ing.status,
          };
        });
        setIngredientResults(initialResults);

        if (scanResponse.ingredients.length === 0) {
          setPhase("done");
          return;
        }

        setPhase("polling");
        const now = Date.now();
        scanResponse.ingredients.forEach((ing) => {
          pollStartedAt.current[ing.ingredient_id] = now;
          pollOne(ing.ingredient_id);
        });
      } catch (err) {
        setErrorMessage(
          err.response?.data?.detail || err.message || "Something went wrong uploading the image."
        );
        setPhase("error");
      }
    },
    [clearAllTimers, pollOne]
  );

  // Flip to "done" once every ingredient has settled (completed or failed).
  useEffect(() => {
    if (phase !== "polling") return;
    const entries = Object.values(ingredientResults);
    if (entries.length === 0) return;
    const allSettled = entries.every((e) => e.status === "completed" || e.status === "failed");
    if (allSettled) setPhase("done");
  }, [ingredientResults, phase]);

  const reset = useCallback(() => {
    clearAllTimers();
    setPhase("idle");
    setJob(null);
    setIngredientResults({});
    setErrorMessage(null);
  }, [clearAllTimers]);

  return { phase, job, ingredientResults, errorMessage, startScan, reset };
}
