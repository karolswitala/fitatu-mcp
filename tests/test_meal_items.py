"""Group 9 tests: meal-item write tools (add/update/delete).

Spec §15 / discovery 2026-05-22:
The Fitatu web client persists day mutations through ONE endpoint:
  POST /api/diet-plan/{userId}/days  with body {"<date>": <day_envelope>}

Add = append item. Update = mutate measureQuantity + bump updatedAt.
Delete = mark deletedAt (soft). Server is source of truth for nutrition.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest


def _base_env(**overrides) -> dict:
    env = {
        "FITATU_API_SECRET": "s",
        "FITATU_DB_FILE": ":memory:",
        "FITATU_ALLOW_DELETE": "false",
    }
    env.update(overrides)
    return env


def _tool_names(mcp) -> list[str]:
    tools = asyncio.run(mcp.list_tools())
    return [t.name for t in tools]


# ---- helpers shared across tests ----


def _make_client():
    from mcp_server.fitatu_client import FitatuClient

    client = FitatuClient("u", "p")
    client.token = "tok"
    client.user_id = "42"
    return client


def _existing_day_payload() -> dict:
    """One pre-existing PRODUCT item + one RECIPE item across two slots."""
    return {
        "dietPlan": {
            "second_breakfast": {
                "items": [
                    {
                        "planDayDietItemId": "old-product-uuid",
                        "foodType": "PRODUCT",
                        "measureId": 1,
                        "measureQuantity": 70,
                        "ingredientsServing": None,
                        "mealNumber": None,
                        "numberOfMeals": None,
                        "eaten": False,
                        "productId": 555,
                        "source": "API",
                    },
                ]
            },
            "dinner": {
                "items": [
                    {
                        "planDayDietItemId": "old-recipe-uuid",
                        "foodType": "RECIPE",
                        "measureId": 39,
                        "measureQuantity": 1,
                        "ingredientsServing": 8,
                        "eaten": False,
                        "recipeId": 99999,
                        "source": "API",
                    },
                ]
            },
        },
        "toiletItems": [],
        "note": None,
        "tagsIds": [],
    }


def _make_product(id_=555, measures=None):
    return {
        "id": id_,
        "name": "Banana",
        "energy": 89,
        "protein": 1.1,
        "fat": 0.3,
        "carbohydrate": 22.8,
        "measures": measures or [{"id": 1, "name": "100g", "weightPerUnit": 100.0}],
    }


def _mock_sync_returning_empty(db, client, day):
    from mcp_server.schemas import DaySummarySchema
    return DaySummarySchema(user_id="42", day_date=day, meals=[])


# ---- 9.1 add: appends to existing items + posts whole day ----


def test_add_meal_item_appends_and_posts_whole_day():
    from mcp_server import service

    client = _make_client()
    posted: dict = {}

    def fake_post_day(date, envelope):
        posted["date"] = date
        posted["envelope"] = envelope
        resp = MagicMock(); resp.status_code = 200; resp.json.return_value = {}
        return resp

    with patch.object(client, "get_day", return_value=_existing_day_payload()), \
         patch.object(client, "get_product", return_value=_make_product(555)), \
         patch.object(client, "post_day", side_effect=fake_post_day), \
         patch("mcp_server.service.sync_day_from_fitatu", side_effect=_mock_sync_returning_empty):
        db = MagicMock()
        db.get.return_value = None
        result = service.add_meal_item(
            db, client,
            date="2026-05-22",
            meal_key="second_breakfast",
            product_id=555,
            measure_id=1,
            measure_quantity=2.0,
        )

    assert result["ok"] is True
    assert result["date"] == "2026-05-22"
    assert result["meal_key"] == "second_breakfast"
    # UUID v1 emitted
    parsed = uuid.UUID(result["plan_day_diet_item_id"])
    assert parsed.version == 1

    # Posted envelope is wrapped { "<date>": {...} } at the client layer; our service
    # just hands it to post_day. We verify the envelope contents instead.
    assert posted["date"] == "2026-05-22"
    env = posted["envelope"]
    sb_items = env["dietPlan"]["second_breakfast"]["items"]
    # Existing item preserved
    assert any(i["planDayDietItemId"] == "old-product-uuid" for i in sb_items)
    # New item appended with EXACT shape (no nutrition fields)
    new = next(i for i in sb_items if i["planDayDietItemId"] == result["plan_day_diet_item_id"])
    assert new["foodType"] == "PRODUCT"
    assert new["productId"] == 555
    assert new["measureId"] == 1
    assert new["measureQuantity"] == 2.0
    assert new["eaten"] is False
    assert new["source"] == "API"
    assert new["ingredientsServing"] is None
    assert "updatedAt" in new
    # Nutrition fields MUST NOT leak into the write payload
    for forbidden in ("energy", "protein", "fat", "carbohydrate", "weight", "meal", "type", "itemId"):
        assert forbidden not in new, f"unexpected field {forbidden!r} in write payload"
    # Other meals preserved untouched
    assert env["dietPlan"]["dinner"]["items"][0]["recipeId"] == 99999


def test_add_meal_item_rejects_invalid_meal_key():
    from mcp_server import service

    client = _make_client()
    db = MagicMock()
    with pytest.raises(ValueError, match="meal_key"):
        service.add_meal_item(db, client, "2026-05-22", "brunch", 555, 1, 1.0)


def test_add_meal_item_rejects_unknown_measure():
    from mcp_server import service

    client = _make_client()
    with patch.object(client, "get_product", return_value=_make_product(555, measures=[{"id": 1, "name": "100g", "weightPerUnit": 100}])):
        db = MagicMock()
        db.get.return_value = None
        with pytest.raises(ValueError, match="measure_id 99"):
            service.add_meal_item(db, client, "2026-05-22", "lunch", 555, 99, 1.0)


def test_add_meal_item_recipe_uses_get_recipe_and_writes_recipe_shape():
    """food_type=RECIPE → look up recipe.measures[], write recipeId + ingredientsServing."""
    from mcp_server import service

    client = _make_client()
    posted: dict = {}

    def fake_post_day(date, envelope):
        posted["envelope"] = envelope
        resp = MagicMock(); resp.status_code = 200; resp.json.return_value = {}
        return resp

    fake_recipe = {
        "id": 145394529,
        "name": "Pyry z gzikiem",
        "serving": 2,
        "measures": [
            {"id": 1, "name": "g", "weightPerUnit": 1},
            {"id": 39, "name": "porcja", "weightPerUnit": 650},
        ],
    }
    with patch.object(client, "get_day", return_value=_existing_day_payload()), \
         patch.object(client, "get_recipe", return_value=fake_recipe) as mock_get_recipe, \
         patch.object(client, "post_day", side_effect=fake_post_day), \
         patch("mcp_server.service.sync_day_from_fitatu", side_effect=_mock_sync_returning_empty):
        db = MagicMock()
        db.get.return_value = None
        result = service.add_meal_item(
            db, client,
            date="2026-05-22",
            meal_key="supper",
            product_id=145394529,
            measure_id=39,
            measure_quantity=1.0,
            food_type="RECIPE",
        )

    assert result["ok"] is True
    mock_get_recipe.assert_called_once_with(145394529)
    new_pid = result["plan_day_diet_item_id"]
    items = posted["envelope"]["dietPlan"]["supper"]["items"]
    new = next(i for i in items if i["planDayDietItemId"] == new_pid)
    assert new["foodType"] == "RECIPE"
    assert new["recipeId"] == 145394529
    assert "productId" not in new
    assert new["measureId"] == 39
    assert new["measureQuantity"] == 1.0
    assert new["ingredientsServing"] == 2  # mirrors recipe.serving
    assert new["eaten"] is False
    assert new["source"] == "API"


def test_add_meal_item_recipe_rejects_unknown_measure():
    from mcp_server import service

    client = _make_client()
    fake_recipe = {"id": 1, "name": "R", "serving": 1, "measures": [{"id": 39, "name": "porcja", "weightPerUnit": 200}]}
    with patch.object(client, "get_recipe", return_value=fake_recipe):
        db = MagicMock()
        with pytest.raises(ValueError, match=r"measure_id 99 not present in recipe 1"):
            service.add_meal_item(db, client, "2026-05-22", "supper", 1, 99, 1.0, food_type="RECIPE")


def test_add_meal_item_rejects_invalid_food_type():
    from mcp_server import service

    client = _make_client()
    db = MagicMock()
    with pytest.raises(ValueError, match="food_type"):
        service.add_meal_item(db, client, "2026-05-22", "supper", 1, 1, 1.0, food_type="MEAL")


# ---- 9.2 update: mutates in place, same planDayDietItemId, bumps updatedAt ----


def test_update_meal_item_mutates_in_place():
    from mcp_server import service

    client = _make_client()
    posted: dict = {}

    def fake_post_day(date, envelope):
        posted["envelope"] = envelope
        resp = MagicMock(); resp.status_code = 200; resp.json.return_value = {}
        return resp

    with patch.object(client, "get_day", return_value=_existing_day_payload()), \
         patch.object(client, "post_day", side_effect=fake_post_day), \
         patch("mcp_server.service.sync_day_from_fitatu", side_effect=_mock_sync_returning_empty):
        db = MagicMock()
        result = service.update_meal_item(
            db, client,
            date="2026-05-22",
            meal_key="second_breakfast",
            plan_day_diet_item_id="old-product-uuid",
            new_measure_quantity=42.0,
        )

    # Same UUID
    assert result["plan_day_diet_item_id"] == "old-product-uuid"
    items = posted["envelope"]["dietPlan"]["second_breakfast"]["items"]
    assert len(items) == 1
    updated = items[0]
    assert updated["measureQuantity"] == 42.0
    assert "updatedAt" in updated
    assert updated["productId"] == 555  # unchanged


def test_update_meal_item_404_when_id_missing():
    from mcp_server import service

    client = _make_client()
    with patch.object(client, "get_day", return_value=_existing_day_payload()):
        db = MagicMock()
        with pytest.raises(RuntimeError, match="not found"):
            service.update_meal_item(
                db, client, "2026-05-22", "second_breakfast", "does-not-exist", 1.0,
            )


# ---- 9.3 delete: marks deletedAt, item stays in array ----


def test_delete_meal_item_marks_deleted_at():
    from mcp_server import service

    client = _make_client()
    posted: dict = {}

    def fake_post_day(date, envelope):
        posted["envelope"] = envelope
        resp = MagicMock(); resp.status_code = 200; resp.json.return_value = {}
        return resp

    with patch.object(client, "get_day", return_value=_existing_day_payload()), \
         patch.object(client, "post_day", side_effect=fake_post_day), \
         patch("mcp_server.service.sync_day_from_fitatu", side_effect=_mock_sync_returning_empty):
        db = MagicMock()
        result = service.delete_meal_item(
            db, client, "2026-05-22", "second_breakfast", "old-product-uuid",
        )

    assert result["ok"] is True
    assert result["deleted_plan_day_diet_item_id"] == "old-product-uuid"
    items = posted["envelope"]["dietPlan"]["second_breakfast"]["items"]
    # Item still in array (soft delete) with deletedAt set
    assert len(items) == 1
    assert items[0]["planDayDietItemId"] == "old-product-uuid"
    assert "deletedAt" in items[0]


def test_delete_meal_item_idempotent_when_already_deleted():
    from mcp_server import service

    client = _make_client()
    day = _existing_day_payload()
    day["dietPlan"]["second_breakfast"]["items"][0]["deletedAt"] = "2026-05-22 19:00:00"

    with patch.object(client, "get_day", return_value=day), \
         patch.object(client, "post_day") as mock_post, \
         patch("mcp_server.service.sync_day_from_fitatu", side_effect=_mock_sync_returning_empty):
        db = MagicMock()
        result = service.delete_meal_item(
            db, client, "2026-05-22", "second_breakfast", "old-product-uuid",
        )
    # Idempotent: no POST issued, returns already_deleted flag
    mock_post.assert_not_called()
    assert result["already_deleted"] is True


# ---- 9.4 tool registrations gated correctly ----


def test_delete_meal_item_unregistered_when_flag_false():
    from mcp_server.server import build_app

    _, mcp = build_app(_base_env(FITATU_ALLOW_DELETE="false"))
    names = _tool_names(mcp)
    assert "delete_meal_item" not in names


def test_delete_meal_item_registered_when_flag_true():
    from mcp_server.server import build_app

    _, mcp = build_app(_base_env(FITATU_ALLOW_DELETE="true"))
    names = _tool_names(mcp)
    assert "delete_meal_item" in names


def test_add_meal_item_and_update_meal_item_registered():
    """Both add and update are unconditional (not gated)."""
    from mcp_server.server import build_app

    _, mcp = build_app(_base_env(FITATU_ALLOW_DELETE="false"))
    names = _tool_names(mcp)
    assert "add_meal_item" in names
    assert "update_meal_item" in names
