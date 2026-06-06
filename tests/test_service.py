from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fitatu_mcp.service import _item_key, sync_day_from_fitatu


def _item(plan_id=None, name="Apple", product_id=1, measure_quantity=100.0, weight=100.0, energy=52.0):
    return SimpleNamespace(
        plan_day_diet_item_id=plan_id,
        name=name,
        product_id=product_id,
        measure_quantity=measure_quantity,
        weight=weight,
        energy=energy,
    )


class TestItemKey:
    def test_plan_id_returns_plan_key(self):
        assert _item_key(_item(plan_id="abc123")) == ("plan", "abc123")

    def test_plan_id_takes_priority_over_other_fields(self):
        assert _item_key(_item(plan_id="xyz", name="Banana")) == ("plan", "xyz")

    def test_no_plan_id_returns_fallback_key(self):
        key = _item_key(_item(plan_id=None))
        assert key[0] == "fallback"
        assert key[1] == "Apple"

    def test_fallback_key_rounds_floats_to_six_places(self):
        item = _item(plan_id=None, measure_quantity=100.1234567, weight=99.9876543, energy=52.3333333)
        key = _item_key(item)
        assert key[3] == round(100.1234567, 6)
        assert key[4] == round(99.9876543, 6)
        assert key[5] == round(52.3333333, 6)

    def test_same_result_for_db_and_schema_like_objects(self):
        # Both MealItem and MealItemSchema have the same attributes — duck typing must work for both
        db_item = _item(plan_id=None, name="Rice", product_id=42, measure_quantity=150.0, weight=150.0, energy=195.0)
        schema_item = _item(plan_id=None, name="Rice", product_id=42, measure_quantity=150.0, weight=150.0, energy=195.0)
        assert _item_key(db_item) == _item_key(schema_item)


class TestSyncDayFromFitatu:
    def test_raises_when_user_id_none_after_get_day(self):
        client = MagicMock()
        client.user_id = None
        client.get_day.return_value = {"dietPlan": {}}
        db = MagicMock()

        with pytest.raises(ValueError, match="user_id"):
            sync_day_from_fitatu(db, client, "2026-06-06")

    def test_raises_when_user_id_empty_string(self):
        client = MagicMock()
        client.user_id = ""
        client.get_day.return_value = {"dietPlan": {}}
        db = MagicMock()

        with pytest.raises(ValueError, match="user_id"):
            sync_day_from_fitatu(db, client, "2026-06-06")
