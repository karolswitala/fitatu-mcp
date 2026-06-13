"""Server-level auth tests: fitatu_login tool, _resolve_client routing."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
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


def _call_tool_sync(mcp, name: str, args: dict) -> dict:
    result = asyncio.run(mcp.call_tool(name, args))
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return result[1]
    items = result[0] if isinstance(result, tuple) else result
    return json.loads(items[0].text)


def _login_stub(user_id: str):
    def _login(self):
        self.token = f"tok-{user_id}"
        self.user_id = user_id
    return _login


def test_no_session_no_basic_raises_auth_required():
    """When pool has no _test_default_client and no HTTP request -> auth_required.

    Use search_products (no required args beyond `query`) so schema validation
    passes and _resolve_client is the first thing to run.
    """
    from mcp_server.server import build_app

    app, mcp = build_app(_base_env())
    assert app.state.session_pool._test_default_client is None
    with pytest.raises(Exception, match="auth_required"):
        _call_tool_sync(mcp, "search_products", {"query": "anything"})


def test_fitatu_login_tool_returns_session_id():
    from mcp_server.server import build_app

    app, mcp = build_app(_base_env())
    with patch("mcp_server.fitatu_client.FitatuClient.login", _login_stub("42")):
        envelope = _call_tool_sync(mcp, "fitatu_login", {
            "email": "alice@x.com", "password": "pw1",
        })
    assert envelope["ok"] is True
    assert envelope["user_id"] == "42"
    assert isinstance(envelope["session_id"], str)
    assert len(envelope["session_id"]) >= 40
    # Pool was mutated
    assert "42" in app.state.session_pool._by_user


def test_fitatu_login_rejects_empty_creds():
    from mcp_server.server import build_app

    _, mcp = build_app(_base_env())
    with pytest.raises(Exception, match="email and password"):
        _call_tool_sync(mcp, "fitatu_login", {"email": "", "password": "pw"})


def test_fitatu_login_does_not_log_password(caplog):
    """Sentinel password must not appear in any log record."""
    from mcp_server.server import build_app

    sentinel_pw = "CANARY-12345-DO-NOT-LOG"
    _, mcp = build_app(_base_env())
    caplog.set_level(logging.DEBUG)
    with patch("mcp_server.fitatu_client.FitatuClient.login", _login_stub("42")):
        _call_tool_sync(mcp, "fitatu_login", {
            "email": "canary@x.com", "password": sentinel_pw,
        })

    for record in caplog.records:
        assert sentinel_pw not in record.getMessage()
        for arg in (record.args or ()):
            assert sentinel_pw not in str(arg)


def test_two_users_isolation_via_default_client_swap(monkeypatch, stub_fitatu_client):
    """Custom products created by user A stay invisible to user B."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from mcp_server import database, server
    from mcp_server.fitatu_client import FitatuClient
    from mcp_server.models import Base
    from mcp_server.server import build_app

    # Shared in-memory SQLite across the test
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(database, "SessionLocal", TestSession)
    monkeypatch.setattr(server, "SessionLocal", TestSession)

    app, mcp = build_app(_base_env())

    # Stub upstream Fitatu create_product so it returns a payload locally.
    counter = {"n": 1000}

    def _fake_create_product(self, payload):
        counter["n"] += 1
        return {
            "id": counter["n"],
            "name": payload["name"],
            "energy": payload["energy"],
            "protein": payload["protein"],
            "fat": payload["fat"],
            "carbohydrate": payload["carbohydrate"],
        }

    monkeypatch.setattr(FitatuClient, "create_product", _fake_create_product)

    # User Alice (user_id=42 via stub)
    app.state.session_pool._test_default_client = stub_fitatu_client
    _call_tool_sync(mcp, "create_custom_product", {
        "name": "Alice cookies", "energy": 400, "protein": 5, "fat": 20, "carbohydrate": 50,
    })

    # Switch to Bob (user_id=99)
    bob = FitatuClient("bob", "pw")
    bob.token = "tok-99"
    bob.user_id = "99"
    app.state.session_pool._test_default_client = bob

    _call_tool_sync(mcp, "create_custom_product", {
        "name": "Bob brownies", "energy": 350, "protein": 4, "fat": 15, "carbohydrate": 45,
    })

    # Bob sees only his own custom
    envelope = _call_tool_sync(mcp, "search_products", {
        "query": "brownies", "scope": "custom", "limit": 10,
    })
    names = [r["name"] for r in envelope["results"]]
    assert names == ["Bob brownies"]

    envelope = _call_tool_sync(mcp, "search_products", {
        "query": "cookies", "scope": "custom", "limit": 10,
    })
    assert envelope["results"] == []

    # Swap back to Alice
    app.state.session_pool._test_default_client = stub_fitatu_client
    envelope = _call_tool_sync(mcp, "search_products", {
        "query": "cookies", "scope": "custom", "limit": 10,
    })
    names = [r["name"] for r in envelope["results"]]
    assert names == ["Alice cookies"]


def test_resolve_client_uses_session_header():
    """End-to-end: register a session, then routing a synthetic ctx resolves it."""
    from mcp_server.server import build_app

    app, mcp = build_app(_base_env())
    with patch("mcp_server.fitatu_client.FitatuClient.login", _login_stub("42")):
        envelope = _call_tool_sync(mcp, "fitatu_login", {
            "email": "alice@x.com", "password": "pw",
        })
    sid = envelope["session_id"]

    # Verify pool can resolve the session id we just got back.
    pool = app.state.session_pool
    client = pool.resolve_session(sid)
    assert client is not None
    assert client.user_id == "42"


def test_resolve_client_unknown_session_returns_none():
    from mcp_server.server import build_app

    app, _ = build_app(_base_env())
    pool = app.state.session_pool
    assert pool.resolve_session("bogus-session-id") is None


def test_resolve_basic_cached_uncached_returns_none():
    from mcp_server.server import build_app

    app, _ = build_app(_base_env())
    pool = app.state.session_pool
    b64 = base64.b64encode(b"never-seen@x.com:pw").decode("ascii")
    assert pool.resolve_basic_cached(b64) is None
