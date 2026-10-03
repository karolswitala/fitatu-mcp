import asyncio
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import fitatu_mcp.server as server
from fitatu_mcp.server import (
    _validate_metric,
    mcp_get_body_measurements,
    mcp_get_measurement_history,
    mcp_get_measurements_on_date,
)
from fitatu_mcp.measurements_service import ALLOWED_METRICS
from tests.helpers import insert_body_profile, insert_measurement_point


def _mock_client():
    client = MagicMock()
    client.user_id = "user1"
    client.get_weight_chart.return_value = {"weights": {"2026-03-15": 70.0, "2026-03-01": 71.2}}
    client.get_metric_series.return_value = [{"date": "2026-03-01", "value": 96}]
    client.get_height.return_value = {"height_cm": 175, "weight_unit": "KG", "size_unit": "CM"}
    return client


def _patch_server(db_session, client):
    session_local = MagicMock()
    session_local.return_value.__enter__.return_value = db_session
    session_local.return_value.__exit__.return_value = False
    return patch.multiple(server, client=client, SessionLocal=session_local)


def _fresh_point(db_session, metric, measured_date, value, unit):
    insert_measurement_point(
        db_session, metric=metric, measured_date=measured_date, value=value, unit=unit,
        updated_at=datetime.now(timezone.utc),
    )


class TestValidateMetric:
    def test_invalid_metric_lists_allowed(self):
        with pytest.raises(ValueError) as exc:
            _validate_metric("foobar")
        message = str(exc.value)
        assert "allowed are" in message
        for name in ALLOWED_METRICS:
            assert name in message

    def test_empty_metric_raises(self):
        with pytest.raises(ValueError):
            _validate_metric("")

    def test_api_key_normalized_to_tool_name(self):
        assert _validate_metric("stomach") == "abdomen"
        assert _validate_metric("chest") == "chest"


class TestDateValidation:
    def test_invalid_date_raises(self, db_session):
        with _patch_server(db_session, _mock_client()):
            with pytest.raises(ValueError, match="Invalid date"):
                asyncio.run(mcp_get_measurements_on_date("not-a-date"))

    def test_reversed_range_raises(self, db_session):
        with _patch_server(db_session, _mock_client()):
            with pytest.raises(ValueError, match="must not be before"):
                asyncio.run(mcp_get_measurement_history("chest", "2026-03-15", "2026-01-01"))

    def test_over_long_range_raises(self, db_session):
        with _patch_server(db_session, _mock_client()):
            with pytest.raises(ValueError, match="maximum allowed"):
                asyncio.run(mcp_get_measurement_history("chest", "2000-01-01", "2026-01-01"))


class TestMeasurementHistory:
    def test_envelope_shape(self, db_session):
        _fresh_point(db_session, "chest", date(2026, 3, 1), 96, "CM")
        with _patch_server(db_session, _mock_client()):
            result = asyncio.run(mcp_get_measurement_history("chest"))
        assert set(result) == {"metric", "unit", "count", "points"}
        assert result["metric"] == "chest"
        assert result["count"] == 1

    def test_auto_sync_on_empty_cache(self, db_session):
        client = _mock_client()
        with _patch_server(db_session, client):
            asyncio.run(mcp_get_measurement_history("chest"))
        client.get_weight_chart.assert_called_once()

    def test_no_sync_when_fresh(self, db_session):
        _fresh_point(db_session, "chest", date(2026, 3, 1), 96, "CM")
        client = _mock_client()
        with _patch_server(db_session, client):
            asyncio.run(mcp_get_measurement_history("chest"))
        client.get_weight_chart.assert_not_called()


class TestBodyMeasurements:
    def test_returns_all_metrics_and_bmi(self, db_session):
        _fresh_point(db_session, "chest", date(2025, 11, 1), 100, "CM")
        _fresh_point(db_session, "chest", date(2026, 3, 1), 96, "CM")
        _fresh_point(db_session, "weight", date(2026, 3, 15), 70.0, "KG")
        insert_body_profile(db_session)
        with _patch_server(db_session, _mock_client()):
            result = asyncio.run(mcp_get_body_measurements())
        by_metric = {m["metric"]: m for m in result["metrics"]}
        assert by_metric["chest"]["start_value"] == 100
        assert by_metric["chest"]["end_value"] == 96
        assert by_metric["chest"]["difference"] == -4
        assert result["bmi"]["bmi"] == 22.9
        assert result["bmi"]["date"] == "2026-03-15"

    def test_bmi_none_when_missing_data(self, db_session):
        _fresh_point(db_session, "chest", date(2026, 3, 1), 96, "CM")
        with _patch_server(db_session, _mock_client()):
            result = asyncio.run(mcp_get_body_measurements())
        assert result["bmi"] is None


class TestMeasurementsOnDate:
    def test_day_envelope_with_bmi_and_units(self, db_session):
        _fresh_point(db_session, "weight", date(2026, 3, 1), 71.2, "KG")
        _fresh_point(db_session, "chest", date(2026, 3, 1), 96, "CM")
        insert_body_profile(db_session)
        with _patch_server(db_session, _mock_client()):
            result = asyncio.run(mcp_get_measurements_on_date("2026-03-01"))
        assert result["date"] == "2026-03-01"
        assert result["metrics"]["weight"] == 71.2
        assert result["weight_unit"] == "KG"
        assert result["bmi"] is not None


class TestForceRefresh:
    def test_force_refresh_resyncs_even_when_fresh(self, db_session):
        _fresh_point(db_session, "chest", date(2026, 3, 1), 96, "CM")
        client = _mock_client()
        with _patch_server(db_session, client):
            asyncio.run(mcp_get_body_measurements(force_refresh=True))
        # bypasses the TTL and re-fetches from Fitatu
        client.get_weight_chart.assert_called_once()
