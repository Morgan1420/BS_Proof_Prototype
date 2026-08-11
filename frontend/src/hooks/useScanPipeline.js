import { useCallback, useState } from "react";
import { scanLabel } from "../api/client";

/**
 * Owns the whole "pick an image -> POST /api/scan -> show the result" flow.
 *
 * There's no polling here -- the backend performs one Gemini vision call
 * synchronously and returns the full result immediately (see
 * app/api/routes.py), so this hook is just a thin async wrapper around
 * scanLabel() with loading/error state.
 *
 * phase: "idle" | "scanning" | "done" | "error"
 * scanResult: the raw ScanResult from POST /api/scan (or null before the
 *   first scan) -- { scan_id, scanned_at, product, ingredients }
 */
export function useScanPipeline() {
  const [phase, setPhase] = useState("idle");
  const [scanResult, setScanResult] = useState(null);
  const [errorMessage, setErrorMessage] = useState(null);

  const startScan = useCallback(async (image) => {
    setErrorMessage(null);
    setScanResult(null);
    setPhase("scanning");

    try {
      const result = await scanLabel(image);
      setScanResult(result);
      setPhase("done");
    } catch (err) {
      setErrorMessage(
        err.response?.data?.detail || err.message || "Something went wrong scanning the label."
      );
      setPhase("error");
    }
  }, []);

  const reset = useCallback(() => {
    setPhase("idle");
    setScanResult(null);
    setErrorMessage(null);
  }, []);

  return { phase, scanResult, errorMessage, startScan, reset };
}
