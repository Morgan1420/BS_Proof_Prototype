// Central place for environment-specific config.
//
// The FastAPI backend must be reachable from wherever this app is running:
//   - Web (`expo start --web`): http://localhost:8000 works directly.
//   - iOS Simulator: http://localhost:8000 also works.
//   - Android Emulator: use http://10.0.2.2:8000 instead -- "localhost" on
//     the emulator refers to the emulator itself, not your machine.
//   - A physical phone (Expo Go): use your computer's LAN IP, e.g.
//     http://192.168.1.23:8000, and start the backend bound to all
//     interfaces: `uvicorn app.main:app --host 0.0.0.0 --reload`.
//
// Override at build/start time with EXPO_PUBLIC_API_BASE_URL if none of
// the above defaults fit (e.g. a physical device): Expo inlines any env
// var prefixed with EXPO_PUBLIC_ into the JS bundle automatically.
import { Platform } from "react-native";

const DEFAULT_HOST = Platform.OS === "android" ? "10.0.2.2" : "localhost";

export const API_BASE_URL =
  process.env.EXPO_PUBLIC_API_BASE_URL || `http://${DEFAULT_HOST}:8000/api/v1`;

// How often (ms) to poll GET /ingredients/{id} while a scan is in progress.
export const POLL_INTERVAL_MS = 2500;

// Give up polling a single ingredient after this long (ms) and surface a timeout.
export const POLL_TIMEOUT_MS = 120000; // 2 minutes
