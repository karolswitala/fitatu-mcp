from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fitatu_mcp.server import TODAY_TTL_SECONDS, _is_stale


def _row(updated_at):
    return SimpleNamespace(updated_at=updated_at)


TODAY = date(2026, 6, 6)
YESTERDAY = TODAY - timedelta(days=1)
TWO_DAYS_AGO = TODAY - timedelta(days=2)


@pytest.fixture(autouse=True)
def freeze_today():
    with patch("fitatu_mcp.server.date") as mock_date:
        mock_date.today.return_value = TODAY
        mock_date.fromisoformat.side_effect = date.fromisoformat
        yield mock_date


class TestIsStaleToday:
    def test_none_updated_at_is_stale(self):
        assert _is_stale(_row(None), TODAY.isoformat()) is True

    def test_updated_within_ttl_is_not_stale(self):
        recent = datetime.now(timezone.utc).replace(tzinfo=None)
        assert _is_stale(_row(recent), TODAY.isoformat()) is False

    def test_updated_beyond_ttl_is_stale(self):
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=TODAY_TTL_SECONDS + 60)
        assert _is_stale(_row(old), TODAY.isoformat()) is True


class TestIsStaleYesterday:
    def test_none_updated_at_is_stale(self):
        assert _is_stale(_row(None), YESTERDAY.isoformat()) is True

    def test_last_synced_before_today_is_stale(self):
        before_midnight = datetime(2026, 6, 5, 23, 59, 59)
        assert _is_stale(_row(before_midnight), YESTERDAY.isoformat()) is True

    def test_last_synced_today_is_not_stale(self):
        after_midnight = datetime(2026, 6, 6, 0, 0, 1)
        assert _is_stale(_row(after_midnight), YESTERDAY.isoformat()) is False


class TestIsStaleOlderDays:
    def test_two_days_ago_is_never_stale(self):
        assert _is_stale(_row(None), TWO_DAYS_AGO.isoformat()) is False

    def test_arbitrary_past_day_is_never_stale(self):
        assert _is_stale(_row(datetime(2025, 1, 1)), "2025-01-01") is False
