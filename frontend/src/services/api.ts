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
