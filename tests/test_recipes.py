"""Tests for recipe tools: get_recipe_tags + create_recipe."""

from __future__ import annotations

import asyncio
import json
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


def _call_tool_sync(mcp, name: str, args: dict) -> dict:
    result = asyncio.run(mcp.call_tool(name, args))
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return result[1]
    items = result[0] if isinstance(result, tuple) else result
    return json.loads(items[0].text)


@pytest.fixture
def app_mcp(stub_fitatu_client):
    from mcp_server import server

    app, mcp = server.build_app(_base_env())
    app.state.session_pool._test_default_client = stub_fitatu_client
    return app, mcp


# -- client-layer --


def _mock_response(status: int, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    resp.text = json.dumps(body) if body is not None else ""
    return resp


def test_client_get_recipe_tags_returns_list():
    from mcp_server.fitatu_client import FitatuClient

    client = FitatuClient("u", "p")
    client.token = "t"
    client.user_id = "42"
    tags = [{"name": "tag.vegan", "translation": "wegańskie", "category": "RECIPE_TAG_CATEGORY_DIET_TYPE"}]
    with patch("mcp_server.fitatu_client.requests.request", return_value=_mock_response(200, tags)) as mock_req:
        result = client.get_recipe_tags()
        assert result == tags
        call = mock_req.call_args
        url = call.args[1] if len(call.args) > 1 else call.kwargs.get("url")
        method = call.args[0] if call.args else call.kwargs.get("method")
        assert method == "GET"
        assert url.endswith("/api/resources/food-tags/recipe")


def test_client_create_recipe_posts_payload_to_api_recipes():
    from mcp_server.fitatu_client import FitatuClient

    client = FitatuClient("u", "p")
    client.token = "t"
    client.user_id = "42"
    server_resp = {"id": 999, "name": "Test", "energy": 100, "protein": 5, "fat": 2, "carbohydrate": 10}
    with patch("mcp_server.fitatu_client.requests.request", return_value=_mock_response(200, server_resp)) as mock_req:
        result = client.create_recipe({"name": "Test", "items": [], "tags": []})
        assert result == server_resp
        call = mock_req.call_args
        method = call.args[0] if call.args else call.kwargs.get("method")
        url = call.args[1] if len(call.args) > 1 else call.kwargs.get("url")
        body = call.kwargs.get("json")
        assert method == "POST"
        assert url.endswith("/api/recipes")
        assert body == {"name": "Test", "items": [], "tags": []}


def test_client_create_recipe_raises_on_non_2xx():
    from mcp_server.fitatu_client import FitatuClient

    client = FitatuClient("u", "p")
    client.token = "t"
    client.user_id = "42"
    with patch("mcp_server.fitatu_client.requests.request", return_value=_mock_response(400, {"err": "bad"})):
        with pytest.raises(RuntimeError, match="create_recipe failed"):
            client.create_recipe({"name": "X"})


# -- MCP tool layer --


def test_mcp_get_recipe_tags_wraps_client_response(app_mcp):
    app, mcp = app_mcp
    tags = [
        {"name": "tag.vegan", "translation": "wegańskie", "category": "RECIPE_TAG_CATEGORY_DIET_TYPE"},
        {"name": "tag.polish", "translation": "polska", "category": "RECIPE_TAG_CATEGORY_CUISINES_OF_THE_WORLD_TYPE"},
    ]
    with patch("mcp_server.fitatu_client.FitatuClient.get_recipe_tags", return_value=tags):
        envelope = _call_tool_sync(mcp, "get_recipe_tags", {})
    assert envelope["ok"] is True
    assert envelope["count"] == 2
    assert envelope["tags"] == tags


def test_mcp_create_recipe_happy_path_minimal(app_mcp):
    app, mcp = app_mcp
    server_resp = {"id": 145394529, "name": "test", "energy": 93.2, "protein": 2.5, "fat": 0.8, "carbohydrate": 13.2}
    items = [{"type": "PRODUCT", "itemId": 116807915, "measureId": 1, "measureQuantity": 100}]
    with patch("mcp_server.fitatu_client.FitatuClient.create_recipe", return_value=server_resp) as mock_create:
        envelope = _call_tool_sync(mcp, "create_recipe", {
            "name": "test",
            "items_json": json.dumps(items),
        })
    assert envelope["ok"] is True
    assert envelope["recipe"] == server_resp
    payload = mock_create.call_args.args[0]
    assert payload["name"] == "test"
    assert payload["items"] == items
    assert payload["serving"] == "1"
    assert payload["shared"] is False
    assert set(payload["mealSchema"]) == {"breakfast", "second_breakfast", "lunch", "dinner", "snack", "supper"}
    assert payload["tags"] == []
    assert payload["categories"] is None


def test_mcp_create_recipe_validates_items_type(app_mcp):
    app, mcp = app_mcp
    bad_items = json.dumps([{"type": "INGREDIENT", "itemId": 1, "measureId": 1, "measureQuantity": 1}])
    with pytest.raises(Exception, match="PRODUCT or RECIPE"):
        _call_tool_sync(mcp, "create_recipe", {"name": "test", "items_json": bad_items})


def test_mcp_create_recipe_rejects_invalid_meal_schema_key(app_mcp):
    app, mcp = app_mcp
    items = json.dumps([{"type": "PRODUCT", "itemId": 1, "measureId": 1, "measureQuantity": 1}])
    with pytest.raises(Exception, match="invalid key"):
        _call_tool_sync(mcp, "create_recipe", {
            "name": "x",
            "items_json": items,
            "meal_schema_csv": "breakfast,brunch",
        })


def test_mcp_create_recipe_forwards_tags(app_mcp):
    app, mcp = app_mcp
    items = json.dumps([{"type": "PRODUCT", "itemId": 1, "measureId": 1, "measureQuantity": 1}])
    tags = json.dumps([
        {"name": "tag.vegan", "category": "RECIPE_TAG_CATEGORY_DIET_TYPE", "translation": "wegańskie"},
        {"name": "test", "category": "RECIPE_TAG_USERS_TYPE", "translation": "test"},
    ])
    with patch("mcp_server.fitatu_client.FitatuClient.create_recipe", return_value={"id": 1, "name": "x", "energy": 0, "protein": 0, "fat": 0, "carbohydrate": 0}) as mock_create:
        _call_tool_sync(mcp, "create_recipe", {"name": "x", "items_json": items, "tags_json": tags})
    payload = mock_create.call_args.args[0]
    assert len(payload["tags"]) == 2
    assert payload["tags"][1]["category"] == "RECIPE_TAG_USERS_TYPE"


def test_mcp_create_recipe_normalizes_literal_backslash_n_in_description(app_mcp):
    """Callers that JSON-double-escape newlines send '\\n' literals; we must convert them."""
    app, mcp = app_mcp
    items = json.dumps([{"type": "PRODUCT", "itemId": 1, "measureId": 1, "measureQuantity": 1}])
    # Literal backslash-n (2 chars) — what we want to be normalized into a real newline
    desc_in = "Krok 1: gotuj.\\n\\nKrok 2: podawaj."
    with patch("mcp_server.fitatu_client.FitatuClient.create_recipe", return_value={"id": 1, "name": "x", "energy": 0, "protein": 0, "fat": 0, "carbohydrate": 0}) as mock_create:
        _call_tool_sync(mcp, "create_recipe", {
            "name": "x",
            "items_json": items,
            "recipe_description": desc_in,
        })
    payload = mock_create.call_args.args[0]
    assert "\\n" not in payload["recipeDescription"]
    assert "\n\n" in payload["recipeDescription"]
    assert payload["recipeDescription"] == "Krok 1: gotuj.\n\nKrok 2: podawaj."


def test_mcp_create_recipe_preserves_real_newlines(app_mcp):
    """Callers sending real newline characters should be passed through unchanged."""
    app, mcp = app_mcp
    items = json.dumps([{"type": "PRODUCT", "itemId": 1, "measureId": 1, "measureQuantity": 1}])
    desc_in = "Krok 1.\n\nKrok 2."  # real newline
    with patch("mcp_server.fitatu_client.FitatuClient.create_recipe", return_value={"id": 1, "name": "x", "energy": 0, "protein": 0, "fat": 0, "carbohydrate": 0}) as mock_create:
        _call_tool_sync(mcp, "create_recipe", {
            "name": "x",
            "items_json": items,
            "recipe_description": desc_in,
        })
    payload = mock_create.call_args.args[0]
    assert payload["recipeDescription"] == desc_in


def test_mcp_create_recipe_rejects_empty_name(app_mcp):
    app, mcp = app_mcp
    items = json.dumps([{"type": "PRODUCT", "itemId": 1, "measureId": 1, "measureQuantity": 1}])
    with pytest.raises(Exception, match="name"):
        _call_tool_sync(mcp, "create_recipe", {"name": "  ", "items_json": items})
