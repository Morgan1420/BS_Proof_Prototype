"""Tests for app.services.storage.ScanStorage -- the local JSON-file record store."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.schemas.scan import ScannedIngredient, ScannedProductMetadata, ScanResult
from app.services.storage import SEED_SCAN_ID, ScanStorage


def run(coro):
    """Run an async test coroutine without requiring pytest-asyncio."""
    return asyncio.run(coro)


def make_result(scan_id: str = "scan_test001", brand_name: str = "Example Labs") -> ScanResult:
    return ScanResult(
        scan_id=scan_id,
        scanned_at=datetime.now(timezone.utc),
        product=ScannedProductMetadata(
            brand_name=brand_name,
            product_name="Daily Focus Boost",
            serving_size="2 capsules",
            servings_per_container=30,
        ),
        ingredients=[
            ScannedIngredient(name="Ashwagandha", form="KSM-66 Root Extract", amount=600, unit="mg"),
        ],
    )


@pytest.fixture
def storage_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "scanned_ingredients.json"


@pytest.fixture
def storage(storage_path: Path) -> ScanStorage:
    return ScanStorage(path=storage_path)


class TestListAllOnEmptyStore:
    def test_returns_empty_list_when_file_does_not_exist(self, storage):
        assert run(storage.list_all()) == []


class TestAppend:
    def test_creates_the_file_and_parent_directory(self, storage, storage_path):
        assert not storage_path.exists()
        run(storage.append(make_result()))
        assert storage_path.exists()

    def test_appended_record_is_returned_by_list_all(self, storage):
        run(storage.append(make_result()))

        records = run(storage.list_all())

        assert len(records) == 1
        assert records[0]["scan_id"] == "scan_test001"
        assert records[0]["product"]["brand_name"] == "Example Labs"
        assert records[0]["ingredients"][0]["name"] == "Ashwagandha"

    def test_multiple_appends_accumulate_in_insertion_order(self, storage):
        run(storage.append(make_result(scan_id="scan_one", brand_name="Brand One")))
        run(storage.append(make_result(scan_id="scan_two", brand_name="Brand Two")))
        run(storage.append(make_result(scan_id="scan_three", brand_name="Brand Three")))

        records = run(storage.list_all())

        assert [r["scan_id"] for r in records] == ["scan_one", "scan_two", "scan_three"]

    def test_written_file_is_valid_json_matching_scan_result_shape(self, storage, storage_path):
        run(storage.append(make_result()))

        with storage_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        assert isinstance(data, list)
        assert len(data) == 1
        # Round-trips cleanly back through the schema.
        ScanResult.model_validate(data[0])


class TestSeedIfMissing:
    """ScanStorage.seed_if_missing -- creates data/scanned_ingredients.json
    with realistic mock data if it doesn't exist yet (see app.main's
    lifespan, which calls this once at startup).
    """

    def test_creates_the_file_when_missing(self, storage, storage_path):
        assert not storage_path.exists()
        run(storage.seed_if_missing())
        assert storage_path.exists()

    def test_seeded_data_is_returned_by_list_all(self, storage):
        run(storage.seed_if_missing())

        records = run(storage.list_all())

        assert len(records) == 1
        assert records[0]["scan_id"] == SEED_SCAN_ID

    def test_seeded_scan_contains_vitamin_c_zinc_and_ashwagandha(self, storage):
        run(storage.seed_if_missing())

        records = run(storage.list_all())
        ingredient_names = {ing["name"] for ing in records[0]["ingredients"]}

        assert ingredient_names == {"Vitamin C", "Zinc", "Ashwagandha"}
        by_name = {ing["name"]: ing for ing in records[0]["ingredients"]}
        assert by_name["Vitamin C"]["amount"] == 1000
        assert by_name["Vitamin C"]["unit"] == "mg"
        assert by_name["Zinc"]["amount"] == 15
        assert by_name["Zinc"]["unit"] == "mg"
        assert by_name["Ashwagandha"]["amount"] == 500
        assert by_name["Ashwagandha"]["unit"] == "mg"

    def test_seeded_record_round_trips_through_scan_result_schema(self, storage):
        run(storage.seed_if_missing())

        records = run(storage.list_all())

        ScanResult.model_validate(records[0])

    def test_does_not_overwrite_an_existing_file(self, storage):
        run(storage.append(make_result(scan_id="scan_real_001")))

        run(storage.seed_if_missing())  # should be a no-op -- file already exists

        records = run(storage.list_all())
        assert len(records) == 1
        assert records[0]["scan_id"] == "scan_real_001"

    def test_calling_it_twice_on_a_missing_file_only_seeds_once(self, storage):
        run(storage.seed_if_missing())
        run(storage.seed_if_missing())

        records = run(storage.list_all())
        assert len(records) == 1

    def test_real_scans_can_still_be_appended_after_seeding(self, storage):
        run(storage.seed_if_missing())
        run(storage.append(make_result(scan_id="scan_real_001")))

        records = run(storage.list_all())

        assert [r["scan_id"] for r in records] == [SEED_SCAN_ID, "scan_real_001"]


class TestBackfillIngredientIds:
    """ScanStorage.backfill_ingredient_ids -- one-time migration for pre-existing
    records that predate the ingredient_id field (see app.main's lifespan,
    which calls this once at startup, right after seed_if_missing).
    """

    def test_assigns_an_id_to_an_ingredient_missing_one(self, storage, storage_path):
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_text(
            json.dumps(
                [
                    {
                        "scan_id": "scan_old",
                        "scanned_at": datetime.now(timezone.utc).isoformat(),
                        "product": {"brand_name": "Old Brand"},
                        "ingredients": [{"name": "Zinc", "form": None, "amount": 15, "unit": "mg"}],
                    }
                ]
            ),
            encoding="utf-8",
        )

        run(storage.backfill_ingredient_ids())

        records = run(storage.list_all())
        assert records[0]["ingredients"][0]["ingredient_id"]
        assert records[0]["ingredients"][0]["ingredient_id"].startswith("ing_")

    def test_the_backfilled_id_is_stable_across_repeated_calls(self, storage, storage_path):
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_text(
            json.dumps(
                [
                    {
                        "scan_id": "scan_old",
                        "scanned_at": datetime.now(timezone.utc).isoformat(),
                        "product": {},
                        "ingredients": [{"name": "Zinc"}],
                    }
                ]
            ),
            encoding="utf-8",
        )

        run(storage.backfill_ingredient_ids())
        first_id = run(storage.list_all())[0]["ingredients"][0]["ingredient_id"]

        run(storage.backfill_ingredient_ids())
        second_id = run(storage.list_all())[0]["ingredients"][0]["ingredient_id"]

        assert first_id == second_id

    def test_does_not_touch_an_ingredient_that_already_has_an_id(self, storage):
        run(storage.append(make_result()))
        original_id = run(storage.list_all())[0]["ingredients"][0]["ingredient_id"]

        run(storage.backfill_ingredient_ids())

        assert run(storage.list_all())[0]["ingredients"][0]["ingredient_id"] == original_id

    def test_no_op_on_an_empty_store(self, storage):
        run(storage.backfill_ingredient_ids())  # should not raise
        assert run(storage.list_all()) == []


class TestGetIngredient:
    def test_finds_an_ingredient_by_id_across_scans(self, storage):
        run(storage.append(make_result(scan_id="scan_one")))
        run(storage.append(make_result(scan_id="scan_two")))
        records = run(storage.list_all())
        target_id = records[1]["ingredients"][0]["ingredient_id"]

        found = run(storage.get_ingredient(target_id))

        assert found is not None
        assert found["ingredient_id"] == target_id
        assert found["name"] == "Ashwagandha"

    def test_returns_none_for_an_unknown_id(self, storage):
        run(storage.append(make_result()))

        assert run(storage.get_ingredient("ing_does_not_exist")) is None

    def test_returns_none_when_store_is_empty(self, storage):
        assert run(storage.get_ingredient("ing_anything")) is None


class TestUpdateIngredient:
    def test_merges_updates_into_the_matching_ingredient_only(self, storage):
        run(storage.append(make_result(scan_id="scan_one")))
        run(storage.append(make_result(scan_id="scan_two")))
        records = run(storage.list_all())
        target_id = records[0]["ingredients"][0]["ingredient_id"]
        other_id = records[1]["ingredients"][0]["ingredient_id"]

        updated = run(storage.update_ingredient(target_id, {"grade_status": "graded", "sifg_grade": "A"}))

        assert updated["grade_status"] == "graded"
        assert updated["sifg_grade"] == "A"

        records = run(storage.list_all())
        by_id = {
            ing["ingredient_id"]: ing for scan in records for ing in scan["ingredients"]
        }
        assert by_id[target_id]["grade_status"] == "graded"
        assert by_id[other_id]["grade_status"] == "pending"  # untouched

    def test_persists_across_a_fresh_read(self, storage):
        run(storage.append(make_result()))
        target_id = run(storage.list_all())[0]["ingredients"][0]["ingredient_id"]

        run(storage.update_ingredient(target_id, {"grade_status": "graded", "sifg_score": 91.5}))

        reloaded = run(storage.list_all())
        assert reloaded[0]["ingredients"][0]["sifg_score"] == 91.5

    def test_returns_none_for_an_unknown_id(self, storage):
        run(storage.append(make_result()))

        assert run(storage.update_ingredient("ing_does_not_exist", {"grade_status": "graded"})) is None

    def test_does_not_write_to_disk_when_id_is_unknown(self, storage, storage_path):
        run(storage.append(make_result()))
        before = storage_path.read_text(encoding="utf-8")

        run(storage.update_ingredient("ing_does_not_exist", {"grade_status": "graded"}))

        assert storage_path.read_text(encoding="utf-8") == before


class TestReadResilience:
    def test_corrupted_json_file_is_treated_as_empty_rather_than_raising(self, storage, storage_path):
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_text("not valid json{{{", encoding="utf-8")

        assert run(storage.list_all()) == []

    def test_non_list_json_file_is_treated_as_empty(self, storage, storage_path):
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

        assert run(storage.list_all()) == []

    def test_append_after_corrupted_file_recovers_by_overwriting(self, storage, storage_path):
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_text("not valid json{{{", encoding="utf-8")

        run(storage.append(make_result()))

        records = run(storage.list_all())
        assert len(records) == 1
