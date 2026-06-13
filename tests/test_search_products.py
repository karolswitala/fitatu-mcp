"""Group 6 tests: search_products — local (custom) + live Fitatu (catalog) wiring."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest


def _base_env(**overrides) -> dict:
    env = {
        "FITATU_API_SECRET": "s",
        "FITATU_DB_FILE": ":memory:",
        "FITATU_ALLOW_DELETE": "false",
    }
    env.update(overrides)
    return env


async def _call_tool(mcp, name: str, args: dict):
    return await mcp.call_tool(name, args)


def _call_tool_sync(mcp, name: str, args: dict) -> dict:
    """Call a FastMCP tool and decode the JSON envelope from the first TextContent."""
    result = asyncio.run(_call_tool(mcp, name, args))
    # FastMCP returns list[TextContent] or tuple (list[TextContent], structured)
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return result[1]
    items = result[0] if isinstance(result, tuple) else result
    text = items[0].text
    return json.loads(text)


@pytest.fixture
def app_mcp(monkeypatch, stub_fitatu_client):
    """Build app/mcp with a SessionLocal bound to an in-memory engine seeded with products.

    Pool is configured with stub_fitatu_client as the test default so
    in-process call_tool() short-circuits the per-request auth resolver.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from mcp_server import database, server
    from mcp_server.models import Base

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(database, "SessionLocal", TestSession)
    monkeypatch.setattr(server, "SessionLocal", TestSession)

    app, mcp = server.build_app(_base_env())
    # Wire stub client as the default for in-process tool calls (no real HTTP request).
    app.state.session_pool._test_default_client = stub_fitatu_client

    # Seed products belonging to the stub user (user_id="42") for custom search tests.
    from mcp_server.service import upsert_product

    with TestSession() as db:
        upsert_product(db, {"id": 1, "name": "Homemade Hummus", "energy": 200, "protein": 8, "fat": 12, "carbohydrate": 15}, source="custom", user_id="42")
        upsert_product(db, {"id": 2, "name": "Greek Yogurt", "energy": 60, "protein": 10, "fat": 0, "carbohydrate": 4}, source="custom", user_id="42")
        upsert_product(db, {"id": 3, "name": "Catalog Apple", "energy": 50, "protein": 0, "fat": 0, "carbohydrate": 12}, source="catalog")
        db.commit()

    return app, mcp


def test_search_scope_custom_returns_local_like_matches(app_mcp):
    app, mcp = app_mcp
    envelope = _call_tool_sync(mcp, "search_products", {"query": "Yogurt", "scope": "custom", "limit": 10})
    assert envelope["ok"] is True
    assert envelope["scope"] == "custom"
    assert len(envelope["results"]) == 1
    assert envelope["results"][0]["name"] == "Greek Yogurt"


def test_search_scope_catalog_calls_fitatu_search_food(app_mcp):
    app, mcp = app_mcp
    fake_hits = [
        {"foodId": 555, "name": "Apple PRODUCT", "brand": "Acme", "energy": 52, "type": "PRODUCT", "score": 0.8},
        {"foodId": 777, "name": "Apple pie RECIPE", "brand": "", "energy": 250, "type": "RECIPE", "score": 0.7},
    ]
    with patch("mcp_server.fitatu_client.FitatuClient.search_food", return_value=fake_hits) as mock_search:
        envelope = _call_tool_sync(mcp, "search_products", {"query": "Apple", "scope": "catalog", "limit": 10, "type_filter": "ANY"})

    mock_search.assert_called_once()
    assert envelope["ok"] is True
    assert envelope["scope"] == "catalog"
    ids = [r["id"] for r in envelope["results"]]
    assert 555 in ids and 777 in ids
    apple = next(r for r in envelope["results"] if r["id"] == 555)
    assert apple["type"] == "PRODUCT"
    assert apple["source"] == "catalog"


def test_search_macro_filters_passed_through(app_mcp):
    app, mcp = app_mcp
    fake_hits = [{"foodId": 1, "name": "Match", "brand": "", "energy": 100, "type": "PRODUCT", "score": 0.0}]
    with patch("mcp_server.fitatu_client.FitatuClient.search_food", return_value=fake_hits) as mock_search:
        _call_tool_sync(mcp, "search_products", {
            "query": "truskawki", "scope": "catalog",
            "min_energy": 1, "max_energy": 200, "min_protein": 3, "max_protein": 10,
        })
    kwargs = mock_search.call_args.kwargs
    assert kwargs["min_energy"] == 1
    assert kwargs["max_energy"] == 200
    assert kwargs["min_protein"] == 3
    assert kwargs["max_protein"] == 10
    # sentinel for unset
    assert kwargs["min_fat"] is None
    assert kwargs["max_carbohydrate"] is None


def test_search_type_filter_recipe_drops_products(app_mcp):
    app, mcp = app_mcp
    fake_hits = [
        {"foodId": 1, "name": "Apple PRODUCT", "brand": "", "energy": 50, "type": "PRODUCT", "score": 0.8},
        {"foodId": 2, "name": "Apple pie RECIPE", "brand": "", "energy": 250, "type": "RECIPE", "score": 0.6},
    ]
    with patch("mcp_server.fitatu_client.FitatuClient.search_food", return_value=fake_hits):
        envelope = _call_tool_sync(mcp, "search_products", {"query": "Apple", "scope": "catalog", "type_filter": "RECIPE"})
    ids = [r["id"] for r in envelope["results"]]
    assert ids == [2]


def test_search_scope_all_merges_custom_then_catalog_dedup(app_mcp):
    app, mcp = app_mcp
    fake_hits = [
        {"foodId": 2, "name": "Greek Yogurt (catalog dup)", "brand": "", "energy": 60, "type": "PRODUCT", "score": 0.9},
        {"foodId": 999, "name": "Greek Yogurt Drink", "brand": "Brand", "energy": 75, "type": "PRODUCT", "score": 0.8},
    ]
    with patch("mcp_server.fitatu_client.FitatuClient.search_food", return_value=fake_hits):
        envelope = _call_tool_sync(mcp, "search_products", {"query": "Yogurt", "scope": "all", "limit": 10})

    assert envelope["ok"] is True
    assert envelope["scope"] == "all"
    ids = [r["id"] for r in envelope["results"]]
    assert ids.count(2) == 1
    assert 999 in ids
    assert envelope["results"][0]["id"] == 2
    assert envelope["results"][0]["source"] == "custom"


def test_search_scope_catalog_upstream_failure_returns_empty(app_mcp):
    app, mcp = app_mcp
    with patch("mcp_server.fitatu_client.FitatuClient.search_food", side_effect=RuntimeError("upstream 500")):
        envelope = _call_tool_sync(mcp, "search_products", {"query": "Apple", "scope": "catalog", "limit": 10})
    assert envelope["ok"] is True
    assert envelope["results"] == []
    assert "warnings" in envelope


def test_search_query_too_short_raises(app_mcp):
    app, mcp = app_mcp
    with pytest.raises(Exception) as exc_info:
        _call_tool_sync(mcp, "search_products", {"query": "a", "scope": "custom", "limit": 10})
    assert "at least 2 characters" in str(exc_info.value)
