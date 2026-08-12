"""Local JSON persistence for parsed supplement label scans."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List

from app.schemas.supplement import SupplementAnalysis

logger = logging.getLogger(__name__)

# backend/app/services/storage.py -> parents[2] == backend/
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DATA_FILE = DATA_DIR / "scanned_ingredients.json"

# Guards read-modify-write of DATA_FILE against concurrent requests within
# this process. Note: this does NOT protect against multiple server
# processes (e.g. multiple uvicorn workers) writing to the same file — fine
# for this single-process prototype, but worth revisiting before scaling.
_write_lock = Lock()


def _read_all() -> List[Dict[str, Any]]:
    """Reads all existing scan records, tolerating a missing/corrupt file."""
    if not DATA_FILE.exists():
        return []
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            content = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s, starting fresh: %s", DATA_FILE, exc)
        return []
    return content if isinstance(content, list) else []


def save_scan(analysis: SupplementAnalysis) -> Dict[str, Any]:
    """Appends a parsed analysis to backend/data/scanned_ingredients.json.

    Adds a unique `scan_id` and UTC `scanned_at` timestamp to the stored
    record.

    Returns:
        The full stored record (including scan_id / scanned_at), in case a
        caller wants to surface those to the client.

    Raises:
        OSError: if the data directory/file cannot be written to.
    """
    record: Dict[str, Any] = {
        "scan_id": str(uuid.uuid4()),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        **analysis.model_dump(),
    }

    with _write_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        records = _read_all()
        records.append(record)
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)

    return record
