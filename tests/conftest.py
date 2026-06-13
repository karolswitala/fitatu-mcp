"""Shared test fixtures. Env stubs live in root conftest.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def in_memory_engine():
    """Fresh in-memory SQLite engine with all tables created via Base.metadata.create_all."""
    from mcp_server.models import Base

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(in_memory_engine):
    """SessionLocal bound to the in-memory engine; rolls back at teardown."""
    SessionLocal = sessionmaker(bind=in_memory_engine, autoflush=False, autocommit=False, future=True)
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture
def fake_session():
    """MagicMock(spec=requests.Session) for FitatuClient injection."""
    return MagicMock(spec=requests.Session)


# -- Multi-user auth fixtures (post-2026-05-23 refactor) --

@pytest.fixture
def stub_fitatu_client():
    """A fully-stubbed FitatuClient: token + user_id set, ready for pool seeding."""
    from mcp_server.fitatu_client import FitatuClient

    client = FitatuClient("test-user", "test-pass")
    client.token = "stub-token"
    client.refresh_token = "stub-refresh"
    client.user_id = "42"
    return client


@pytest.fixture
def seeded_pool(stub_fitatu_client):
    """SessionPool pre-seeded with a stub FitatuClient (user_id=42)."""
    from mcp_server.auth import SessionPool, _PoolEntry

    pool = SessionPool()
    pool._by_user[stub_fitatu_client.user_id] = _PoolEntry(client=stub_fitatu_client)
    sid = "test-session-id-deterministic"
    pool._sessions[sid] = stub_fitatu_client.user_id
    return pool, stub_fitatu_client, sid
