"""Standalone script: list every Gemini model available to this project's API key.

Usage (from backend/):
    source venv/bin/activate
    python scripts/list_gemini_models.py

Reads GEMINI_API_KEY the same way the rest of the app does -- via
app.core.config.get_settings() (backend/.env, or a real environment
variable which takes precedence) -- rather than relying on google-genai's
own default GOOGLE_API_KEY lookup, so this always matches whatever key
app/services/gemini_client.py would actually use.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `app.*` importable when this script is run directly (e.g.
# `python scripts/list_gemini_models.py`) rather than as an installed package.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from google import genai  # noqa: E402

from app.core.config import get_settings  # noqa: E402


def main() -> None:
    settings = get_settings()
    api_key = (settings.gemini_api_key or "").strip()
    if not api_key:
        print("GEMINI_API_KEY is not set -- check backend/.env.", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    for m in client.models.list():
        print(m.name)


if __name__ == "__main__":
    main()
