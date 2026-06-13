"""Bootstrap smoke tests: verify test harness loads env + DB tables."""

from __future__ import annotations

import os


def test_env_stubs_loaded():
    """Root conftest must populate the API secret before pytest collection."""
    assert os.environ["FITATU_API_SECRET"]
    assert os.environ["FITATU_DB_FILE"]


def test_in_memory_db_has_tables(in_memory_engine):
    """Base.metadata.create_all on the in-memory engine yields the existing tables."""
    from mcp_server.models import Base

    table_names = set(Base.metadata.tables.keys())
    # Existing read-side tables (per models.py current state)
    assert "daily_nutrition" in table_names
    assert "meal_nutrition" in table_names
    assert "meal_item" in table_names
