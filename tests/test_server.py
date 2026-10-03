import asyncio
from datetime import date
from unittest.mock import MagicMock, patch

import fitatu_mcp.server as server
from fitatu_mcp.server import (
    _load_or_sync_day,
    mcp_get_day_macros,
    mcp_get_day_summary,
)
from tests.helpers import insert_day

# Older than yesterday, so never stale per _is_stale.
FRESH_DAY = "2026-06-06"
MISS_DAY = "2026-06-07"


def _patch_server(db_session):
    client = MagicMock()
    client.user_id = "user1"
    session_local = MagicMock()
    session_local.return_value.__enter__.return_value = db_session
    session_local.return_value.__exit__.return_value = False
    return patch.multiple(server, client=client, SessionLocal=session_local)


def _stub_sync(db_session):
    """Sync stub that inserts the requested day if missing and returns a user1 summary."""
    def _sync(db, client, day_date):
        d = date.fromisoformat(day_date)
        if not server._load_day(db_session, "user1", day_date):
            insert_day(db_session, day_date=d)
        return MagicMock(user_id="user1")
    return patch.object(server, "sync_day_from_fitatu", side_effect=_sync)


class TestLoadOrSyncDay:
    def test_fresh_cached_row_does_not_sync(self, db_session):
        insert_day(db_session, day_date=date.fromisoformat(FRESH_DAY))
        with _patch_server(db_session), _stub_sync(db_session) as sync:
            row = _load_or_sync_day(db_session, "user1", FRESH_DAY)
        sync.assert_not_called()
        assert row.day_date == date.fromisoformat(FRESH_DAY)

    def test_force_refresh_syncs_fresh_cached_row(self, db_session):
        insert_day(db_session, day_date=date.fromisoformat(FRESH_DAY))
        with _patch_server(db_session), _stub_sync(db_session) as sync:
            row = _load_or_sync_day(db_session, "user1", FRESH_DAY, force_refresh=True)
        sync.assert_called_once()
        assert sync.call_args.args[2] == FRESH_DAY
        assert row is not None

    def test_cache_miss_triggers_sync(self, db_session):
        with _patch_server(db_session), _stub_sync(db_session) as sync:
            row = _load_or_sync_day(db_session, "user1", MISS_DAY)
        sync.assert_called_once()
        assert row.day_date == date.fromisoformat(MISS_DAY)


class TestGetDayMacros:
    def test_default_does_not_resync_fresh_day(self, db_session):
        insert_day(db_session, day_date=date.fromisoformat(FRESH_DAY))
        with _patch_server(db_session), _stub_sync(db_session) as sync:
            result = asyncio.run(mcp_get_day_macros(FRESH_DAY))
        sync.assert_not_called()
        assert result["start_date"] == FRESH_DAY
        assert result["end_date"] == FRESH_DAY
        assert result["day_count"] == 1
        assert result["days"][0]["day_date"] == FRESH_DAY
        assert "error" not in result["days"][0]

    def test_force_refresh_propagates(self, db_session):
        insert_day(db_session, day_date=date.fromisoformat(FRESH_DAY))
        with _patch_server(db_session), _stub_sync(db_session) as sync:
            result = asyncio.run(mcp_get_day_macros(FRESH_DAY, force_refresh=True))
        sync.assert_called_once()
        assert result["day_count"] == 1
        assert "error" not in result["days"][0]

    def test_range_envelope(self, db_session):
        with _patch_server(db_session), _stub_sync(db_session) as sync:
            result = asyncio.run(mcp_get_day_macros(FRESH_DAY, MISS_DAY))
        assert sync.call_count == 2
        assert result["start_date"] == FRESH_DAY
        assert result["end_date"] == MISS_DAY
        assert result["day_count"] == 2
        assert [d["day_date"] for d in result["days"]] == [FRESH_DAY, MISS_DAY]


class TestGetDaySummary:
    def test_default_does_not_resync_fresh_day(self, db_session):
        insert_day(db_session, day_date=date.fromisoformat(FRESH_DAY))
        with _patch_server(db_session), _stub_sync(db_session) as sync:
            result = asyncio.run(mcp_get_day_summary(FRESH_DAY))
        sync.assert_not_called()
        assert result["start_date"] == FRESH_DAY
        assert result["end_date"] == FRESH_DAY
        assert result["day_count"] == 1
        assert "error" not in result["days"][0]

    def test_force_refresh_propagates(self, db_session):
        insert_day(db_session, day_date=date.fromisoformat(FRESH_DAY))
        with _patch_server(db_session), _stub_sync(db_session) as sync:
            result = asyncio.run(mcp_get_day_summary(FRESH_DAY, force_refresh=True))
        sync.assert_called_once()
        assert result["day_count"] == 1
        assert "error" not in result["days"][0]


class TestToolRegistry:
    def test_registered_tools_exact(self):
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert names == {
            "get_day_summary",
            "get_day_macros",
            "get_body_measurements",
            "get_measurement_history",
            "get_measurements_on_date",
        }
        assert "sync_day" not in names
