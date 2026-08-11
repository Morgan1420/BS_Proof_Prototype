"""Local JSON-file storage for scanned ingredients.

Replaces the old process-local in-memory job/ingredient stores (and the
"Store SIFG JSON in Database Cache" step) now that scientific grading is
gone -- a scan's result is appended directly to a flat JSON file
(``data/scanned_ingredients.json``, relative to the ``backend/`` package
root) rather than any in-memory or database structure. This is
deliberately simple: no ORM, no migrations, just a JSON array on disk.

Not designed for high-concurrency or multi-process safety: writes go
through an ``asyncio.Lock`` (safe for concurrent requests within a
single process) and an atomic write-then-rename (safe against a crash
mid-write corrupting the file), but two separate backend processes
writing at once could still race. Fine for a local prototype; swap for
a real database if this needs to run with multiple workers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.schemas.scan import ScannedIngredient, ScannedProductMetadata, ScanResult, generate_ingredient_id

logger = logging.getLogger(__name__)

# backend/app/services/storage.py -> backend/ -> backend/data/scanned_ingredients.json
DEFAULT_STORAGE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "scanned_ingredients.json"

# Fixed id for the seeded mock scan (see ScanStorage.seed_if_missing) --
# stable rather than randomly generated so it's easy to recognize/filter
# out in the UI or in tests, and so re-running seed_if_missing against an
# already-seeded (but otherwise still-only-seed-data) file is idempotent
# in spirit even though it never actually re-writes an existing file.
SEED_SCAN_ID = "scan_seed_mock_001"


def _build_seed_scan() -> ScanResult:
    """One realistic mock scan -- a multivitamin with three ingredients.

    Exists so the "Saved Ingredients" tab has something real-looking to
    show immediately on a fresh install, without waiting on a live Gemini
    call. Uses the exact same ``ScanResult`` schema a real scan produces,
    so it round-trips through the API identically to real data -- nothing
    about the frontend needs to know this record is fake.
    """
    return ScanResult(
        scan_id=SEED_SCAN_ID,
        scanned_at=datetime.now(timezone.utc),
        product=ScannedProductMetadata(
            brand_name="Example Wellness Co.",
            product_name="Daily Essentials Multivitamin",
            serving_size="1 tablet",
            servings_per_container=60,
        ),
        ingredients=[
            ScannedIngredient(
                name="Vitamin C", form="Ascorbic Acid", amount=1000, unit="mg", percent_daily_value="1111%"
            ),
            ScannedIngredient(name="Zinc", form="Zinc Citrate", amount=15, unit="mg", percent_daily_value="136%"),
            ScannedIngredient(
                name="Ashwagandha", form="KSM-66 Root Extract", amount=500, unit="mg", percent_daily_value=None
            ),
        ],
    )


class ScanStorage:
    """Append-only JSON-file store for ``ScanResult`` records."""

    def __init__(self, path: Path = DEFAULT_STORAGE_PATH) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def seed_if_missing(self) -> None:
        """Create the storage file with realistic mock data if it doesn't exist yet.

        Called once at app startup (see ``app.main``'s ``lifespan``) so
        the "Saved Ingredients" tab / ``GET /api/ingredients`` has
        something to show immediately -- a real-looking multivitamin scan
        (Vitamin C, Zinc, Ashwagandha) -- without depending on a live
        Gemini call ever having happened. A no-op if the file already
        exists, so this never overwrites real scan history -- safe to
        call unconditionally on every startup.
        """
        async with self._lock:
            await asyncio.to_thread(self._seed_if_missing_sync)

    def _seed_if_missing_sync(self) -> None:
        if self._path.exists():
            return
        logger.info("%s does not exist yet; creating it and seeding realistic mock data.", self._path)
        seed_record = json.loads(_build_seed_scan().model_dump_json())
        self._write_all([seed_record])

    async def append(self, result: ScanResult) -> None:
        """Append one scan's result to the JSON file, preserving everything already there."""
        async with self._lock:
            records = await self._read_all_locked()
            records.append(json.loads(result.model_dump_json()))
            await asyncio.to_thread(self._write_all, records)

    async def list_all(self) -> List[Dict[str, Any]]:
        """Return every scan record saved so far, oldest first (insertion order)."""
        async with self._lock:
            return await self._read_all_locked()

    async def backfill_ingredient_ids(self) -> None:
        """Assign a stable ``ingredient_id`` to any ingredient record that doesn't have one yet.

        Called once at startup (see ``app.main``'s ``lifespan``), right
        after ``seed_if_missing``. Exists because ``ingredient_id`` is a
        newer field than this file's on-disk format -- without this, an
        ingredient read via ``GET /api/ingredients`` would get a
        *freshly generated* id on every single request (from
        ``ScannedIngredient``'s ``default_factory``, applied during
        response-model validation), which would never match anything
        actually persisted here, and every
        ``POST /api/ingredients/{ingredient_id}/grade`` against
        pre-existing data would 404. This makes the id these dicts
        return via ``list_all``/``get_ingredient`` the same, real,
        already-persisted id every time -- a one-time migration, not
        something that runs on every read. A no-op (no write) if every
        ingredient already has one, so it's safe to call unconditionally
        on every startup.
        """
        async with self._lock:
            records = await self._read_all_locked()
            changed = False
            for scan in records:
                for ingredient in scan.get("ingredients", []):
                    if not ingredient.get("ingredient_id"):
                        ingredient["ingredient_id"] = generate_ingredient_id()
                        changed = True
            if changed:
                logger.info("Backfilled ingredient_id for one or more pre-existing ingredient records.")
                await asyncio.to_thread(self._write_all, records)

    async def get_ingredient(self, ingredient_id: str) -> Optional[Dict[str, Any]]:
        """Find one ingredient dict by ``ingredient_id``, searching across every saved scan.

        Returns ``None`` if no ingredient with that id exists. Read-only
        -- see ``update_ingredient`` for persisting a grading result.
        """
        async with self._lock:
            records = await self._read_all_locked()
        for scan in records:
            for ingredient in scan.get("ingredients", []):
                if ingredient.get("ingredient_id") == ingredient_id:
                    return ingredient
        return None

    async def update_ingredient(self, ingredient_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Merge ``updates`` into the one ingredient dict matching ``ingredient_id``, and persist it.

        Used by ``POST /api/ingredients/{ingredient_id}/grade`` (see
        ``app.api.routes``) to write a grading result -- ``grade_status``,
        ``sifg_grade``, ``raw_consensus``, etc. -- into the specific
        ingredient inside its parent scan record, leaving every other
        ingredient (in this scan or any other) completely untouched.

        Returns the updated ingredient dict, or ``None`` if no ingredient
        with that id exists (the caller should treat that as a 404 --
        this method never creates a new record).
        """
        async with self._lock:
            records = await self._read_all_locked()
            updated_ingredient: Optional[Dict[str, Any]] = None
            for scan in records:
                for ingredient in scan.get("ingredients", []):
                    if ingredient.get("ingredient_id") == ingredient_id:
                        ingredient.update(updates)
                        updated_ingredient = ingredient
                        break
                if updated_ingredient is not None:
                    break

            if updated_ingredient is None:
                return None

            await asyncio.to_thread(self._write_all, records)
            return updated_ingredient

    async def _read_all_locked(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._read_all_sync)

    def _read_all_sync(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read %s (%s: %s); treating as empty rather than crashing the request.",
                self._path,
                type(exc).__name__,
                exc,
            )
            return []
        if not isinstance(data, list):
            logger.warning("%s did not contain a JSON array; treating as empty.", self._path)
            return []
        return data

    def _write_all(self, records: List[Dict[str, Any]]) -> None:
        """Write the full record list atomically (temp file + rename).

        Rewriting the whole file on every append is fine at this scale
        (a local prototype's scan history) and avoids ever leaving the
        file in a half-written, unparseable state if the process dies
        mid-write -- the rename is atomic on POSIX filesystems.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, default=str)
        tmp_path.replace(self._path)
