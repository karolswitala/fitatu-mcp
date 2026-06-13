"""Unit tests for SessionPool — multi-user FitatuClient pooling."""

from __future__ import annotations

import asyncio
import base64
import time
from unittest.mock import patch

import pytest


def _mk_pool(**kwargs):
    from mcp_server.auth import SessionPool

    return SessionPool(**kwargs)


def _b64(email: str, password: str) -> str:
    return base64.b64encode(f"{email}:{password}".encode("utf-8")).decode("ascii")


def _login_stub_setter(user_id: str):
    """Returns a function suitable for monkeypatching FitatuClient.login.

    The patched login() runs on self (no args beyond the bound instance),
    stamps token + user_id, mirroring real behavior.
    """
    def _login(self):
        self.token = f"token-for-{user_id}"
        self.user_id = user_id
    return _login


def test_session_pool_get_or_login_caches_by_user_id():
    pool = _mk_pool()

    with patch("mcp_server.fitatu_client.FitatuClient.login", _login_stub_setter("42")):
        c1 = asyncio.run(pool.get_or_login("alice@x.com", "pw1"))
        c2 = asyncio.run(pool.get_or_login("alice@x.com", "pw1"))

    assert c1 is c2
    assert c1.user_id == "42"
    assert len(pool) == 1


def test_session_pool_register_session_returns_unique_sid():
    pool = _mk_pool()

    with patch("mcp_server.fitatu_client.FitatuClient.login", _login_stub_setter("42")):
        client = asyncio.run(pool.get_or_login("alice@x.com", "pw1"))
        sid1 = asyncio.run(pool.register_session(client))
        sid2 = asyncio.run(pool.register_session(client))

    assert sid1 != sid2
    assert len(sid1) >= 40


def test_session_pool_resolve_session_returns_same_client():
    pool = _mk_pool()

    with patch("mcp_server.fitatu_client.FitatuClient.login", _login_stub_setter("42")):
        client = asyncio.run(pool.get_or_login("alice@x.com", "pw1"))
        sid = asyncio.run(pool.register_session(client))

    resolved = pool.resolve_session(sid)
    assert resolved is client


def test_session_pool_basic_cache_short_circuits_second_login():
    pool = _mk_pool()
    call_count = {"n": 0}

    def _login(self):
        call_count["n"] += 1
        self.token = "tok"
        self.user_id = "42"

    with patch("mcp_server.fitatu_client.FitatuClient.login", _login):
        asyncio.run(pool.get_or_login("alice@x.com", "pw1"))
        asyncio.run(pool.get_or_login("alice@x.com", "pw1"))

    assert call_count["n"] == 1


def test_session_pool_convergence_basic_and_session_share_client():
    """Same user_id via Basic and Session path -> single client entry."""
    pool = _mk_pool()

    with patch("mcp_server.fitatu_client.FitatuClient.login", _login_stub_setter("42")):
        client_via_basic = asyncio.run(pool.get_or_login("alice@x.com", "pw1"))
        sid = asyncio.run(pool.register_session(client_via_basic))

    via_session = pool.resolve_session(sid)
    via_basic_cached = pool.resolve_basic_cached(_b64("alice@x.com", "pw1"))

    assert via_session is client_via_basic
    assert via_basic_cached is client_via_basic
    assert len(pool) == 1


def test_session_pool_concurrent_basic_login_calls_login_once():
    """Race-condition guard: asyncio.Lock around get_or_login serializes cold-start."""
    pool = _mk_pool()
    call_count = {"n": 0}

    async def _slow_login(self):
        await asyncio.sleep(0.01)
        call_count["n"] += 1
        self.token = "tok"
        self.user_id = "42"

    def _sync_login(self):
        # Patched login() must be sync (real one is). Bridge to async via to_thread inside pool.
        time.sleep(0.01)
        call_count["n"] += 1
        self.token = "tok"
        self.user_id = "42"

    async def _runner():
        with patch("mcp_server.fitatu_client.FitatuClient.login", _sync_login):
            results = await asyncio.gather(
                pool.get_or_login("alice@x.com", "pw1"),
                pool.get_or_login("alice@x.com", "pw1"),
                pool.get_or_login("alice@x.com", "pw1"),
            )
        return results

    results = asyncio.run(_runner())
    assert call_count["n"] == 1
    assert all(r is results[0] for r in results)


def test_session_pool_evict_idle_removes_stale():
    pool = _mk_pool(idle_ttl_seconds=0)

    with patch("mcp_server.fitatu_client.FitatuClient.login", _login_stub_setter("42")):
        c1 = asyncio.run(pool.get_or_login("alice@x.com", "pw1"))

    # First user backdated; second login triggers idle eviction
    pool._by_user["42"].last_used = time.monotonic() - 3600

    with patch("mcp_server.fitatu_client.FitatuClient.login", _login_stub_setter("99")):
        c2 = asyncio.run(pool.get_or_login("bob@x.com", "pw2"))

    assert "42" not in pool._by_user
    assert "99" in pool._by_user


def test_session_pool_drop_user_clears_all_indirection_maps():
    pool = _mk_pool()

    with patch("mcp_server.fitatu_client.FitatuClient.login", _login_stub_setter("42")):
        client = asyncio.run(pool.get_or_login("alice@x.com", "pw1"))
        sid = asyncio.run(pool.register_session(client))

    assert "42" in pool._by_user
    assert any(uid == "42" for uid in pool._sessions.values())
    assert any(uid == "42" for uid in pool._basic.values())

    pool._drop_user_locked("42")

    assert "42" not in pool._by_user
    assert sid not in pool._sessions
    assert not any(uid == "42" for uid in pool._basic.values())


def test_decode_basic_header_roundtrip():
    from mcp_server.auth import decode_basic_header

    b64 = base64.b64encode(b"alice@x.com:hunter2").decode("ascii")
    email, pw = decode_basic_header(b64)
    assert email == "alice@x.com"
    assert pw == "hunter2"


def test_decode_basic_header_rejects_malformed():
    from mcp_server.auth import decode_basic_header

    with pytest.raises(ValueError):
        decode_basic_header("not-base64-!!!")
    with pytest.raises(ValueError):
        decode_basic_header(base64.b64encode(b"no-colon-here").decode("ascii"))
