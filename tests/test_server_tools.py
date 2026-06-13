"""Group 5 tests: build_app factory + new tool registrations.

These tests exercise the build_app(env) factory so we can flip FITATU_ALLOW_DELETE
between True/False in a single pytest invocation without importlib.reload.
"""

from __future__ import annotations

import asyncio

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
    """Return list of tool names. FastMCP stores tools internally."""
    tools = asyncio.run(mcp.list_tools())
    return [t.name for t in tools]


def test_build_app_returns_fastapi_and_mcp():
    from mcp_server.server import build_app

    app, mcp = build_app(_base_env())
    assert app is not None
    assert mcp is not None


def test_existing_read_tools_still_registered():
    from mcp_server.server import build_app

    _, mcp = build_app(_base_env())
    names = _tool_names(mcp)
    for expected in ("sync_day", "get_day_summary", "get_day_macros", "get_day_meals", "get_cache_stats"):
        assert expected in names, f"expected tool {expected!r} in {names}"


def test_fitatu_login_tool_is_registered():
    from mcp_server.server import build_app

    _, mcp = build_app(_base_env())
    names = _tool_names(mcp)
    assert "fitatu_login" in names


def test_create_custom_product_registered_unconditionally():
    from mcp_server.server import build_app

    _, mcp = build_app(_base_env())
    names = _tool_names(mcp)
    assert "create_custom_product" in names
    assert "get_product" in names
    assert "search_products" in names


def test_delete_custom_product_absent_when_flag_false():
    from mcp_server.server import build_app

    _, mcp = build_app(_base_env(FITATU_ALLOW_DELETE="false"))
    names = _tool_names(mcp)
    assert "delete_custom_product" not in names


def test_delete_custom_product_present_when_flag_true():
    from mcp_server.server import build_app

    _, mcp = build_app(_base_env(FITATU_ALLOW_DELETE="true"))
    names = _tool_names(mcp)
    assert "delete_custom_product" in names


def test_build_app_threads_base_urls_to_pool():
    """FITATU_BASE_URL_READ / FITATU_BASE_URL_WRITE env vars reach the pool."""
    from mcp_server.server import build_app

    env = _base_env(
        FITATU_BASE_URL_READ="https://read.example.com",
        FITATU_BASE_URL_WRITE="https://write.example.com",
    )
    app, _ = build_app(env)
    pool = app.state.session_pool
    assert pool._base_url_read == "https://read.example.com"
    assert pool._base_url_write == "https://write.example.com"


# -- Strategic validation gap tests (Group 8) --


def test_create_custom_product_rejects_empty_name(stub_fitatu_client):
    """create_custom_product with whitespace-only name → ValueError."""
    from mcp_server.server import build_app

    app, mcp = build_app(_base_env())
    app.state.session_pool._test_default_client = stub_fitatu_client
    with pytest.raises(Exception, match="name must not be empty"):
        asyncio.run(mcp.call_tool("create_custom_product", {
            "name": "   ",
            "energy": 10, "protein": 1, "fat": 0, "carbohydrate": 2,
        }))


def test_create_custom_product_rejects_negative_macro(stub_fitatu_client):
    """Negative required macro → ValueError."""
    from mcp_server.server import build_app

    app, mcp = build_app(_base_env())
    app.state.session_pool._test_default_client = stub_fitatu_client
    with pytest.raises(Exception, match=">= 0"):
        asyncio.run(mcp.call_tool("create_custom_product", {
            "name": "Bad", "energy": -5, "protein": 1, "fat": 0, "carbohydrate": 2,
        }))


def test_create_custom_product_rejects_name_too_long(stub_fitatu_client):
    """Name > 200 chars → ValueError."""
    from mcp_server.server import build_app

    app, mcp = build_app(_base_env())
    app.state.session_pool._test_default_client = stub_fitatu_client
    with pytest.raises(Exception, match="200 characters or fewer"):
        asyncio.run(mcp.call_tool("create_custom_product", {
            "name": "x" * 201,
            "energy": 10, "protein": 1, "fat": 0, "carbohydrate": 2,
        }))
