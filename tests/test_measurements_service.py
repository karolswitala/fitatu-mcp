import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fitatu_mcp.measurements_service import (
    API_METRIC,
    METRIC_API,
    bmi_from_db,
    compute_bmi,
    day_from_db,
    ensure_fresh,
    measurements_are_stale,
    normalize_metric,
    series_from_db,
    summary_from_db,
    sync_measurements,
)
from fitatu_mcp.models import MeasurementPoint, UserBodyProfile
from tests.helpers import insert_body_profile, insert_measurement_point

FIXTURES = Path(__file__).parent / "fixtures" / "measurements"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def _seed_chest(session):
    for point in _load("series_chest.json"):
        insert_measurement_point(
            session, metric="chest",
            measured_date=date.fromisoformat(point["date"]),
            value=point["value"], unit="CM",
        )


def _seed_day(session, day="2026-03-01"):
    detail = _load(f"day_{day}.json")
    d = date.fromisoformat(day)
    mapping = {
        "weight": ("weight", "KG"),
        "neck": ("neck", "CM"), "chest": ("chest", "CM"), "waist": ("waist", "CM"),
        "stomach": ("abdomen", "CM"), "hips": ("hips", "CM"), "thigh": ("thigh", "CM"),
        "calf": ("calf", "CM"), "biceps": ("biceps", "CM"), "fatPercentage": ("body_fat", "%"),
    }
    for api_key, (metric, unit) in mapping.items():
        insert_measurement_point(session, metric=metric, measured_date=d,
                                 value=detail[api_key], unit=unit)


class TestNormalizeMetric:
    def test_api_key_maps_to_tool_name(self):
        assert normalize_metric("stomach") == "abdomen"
        assert normalize_metric("fatPercentage") == "body_fat"

    def test_tool_name_passthrough(self):
        assert normalize_metric("abdomen") == "abdomen"
        assert normalize_metric("body_fat") == "body_fat"
        assert normalize_metric("weight") == "weight"

    def test_shared_name_passthrough(self):
        assert normalize_metric("chest") == "chest"

    def test_metric_api_mapping_table(self):
        assert METRIC_API["abdomen"] == "stomach"
        assert METRIC_API["body_fat"] == "fatPercentage"
        assert API_METRIC["stomach"] == "abdomen"

    def test_unknown_metric_raises_listing_allowed(self):
        with pytest.raises(ValueError, match="Unknown metric"):
            normalize_metric("foobar")


class TestComputeBmi:
    def test_known_oracle(self):
        assert compute_bmi(70.0, 175) == 22.9

    def test_missing_inputs_return_none(self):
        assert compute_bmi(None, 175) is None
        assert compute_bmi(70.0, None) is None
        assert compute_bmi(70.0, 0) is None


class TestSyncMeasurements:
    def _client(self):
        client = MagicMock()
        client.get_weight_chart.return_value = _load("weight_chart.json")  # 6 points
        client.get_metric_series.return_value = [
            {"date": "2026-03-01", "value": 50.0},
            {"date": "2025-11-01", "value": 40.0},
        ]
        client.get_height.return_value = {
            "height_cm": 175, "weight_unit": "KG", "size_unit": "CM",
        }
        return client

    def test_upserts_expected_point_count(self, db_session):
        count = sync_measurements(db_session, self._client(), "user1")
        # 6 weight points + 9 size parts * 2 points each
        assert count == 6 + 9 * 2
        assert db_session.query(MeasurementPoint).count() == 24

    def test_writes_body_profile_height(self, db_session):
        sync_measurements(db_session, self._client(), "user1")
        profile = db_session.get(UserBodyProfile, "user1")
        assert profile is not None
        assert profile.height_cm == 175

    def test_idempotent_no_dupes_values_refreshed(self, db_session):
        client = self._client()
        sync_measurements(db_session, client, "user1")
        # change weight on second sync
        client.get_weight_chart.return_value = {"weights": {"2026-03-15": 69.5,
                                                            "2026-03-14": 70.0,
                                                            "2026-03-13": 70.2,
                                                            "2026-03-12": 70.2,
                                                            "2026-03-10": 70.5,
                                                            "2026-03-01": 71.2}}
        sync_measurements(db_session, client, "user1")
        assert db_session.query(MeasurementPoint).count() == 24
        row = (
            db_session.query(MeasurementPoint)
            .filter_by(user_id="user1", metric="weight", measured_date=date(2026, 3, 15))
            .one()
        )
        assert row.value == 69.5


class TestSummaryFromDb:
    def test_reproduces_chest_summary_oracle(self, db_session):
        _seed_chest(db_session)
        summaries = summary_from_db(db_session, "user1", ["chest"])
        assert len(summaries) == 1
        chest = summaries[0]
        assert chest.start_value == 100
        assert chest.end_value == 96
        assert chest.difference == -4
        assert chest.latest_date == "2026-03-01"
        assert chest.unit == "CM"

    def test_skips_metrics_without_points(self, db_session):
        _seed_chest(db_session)
        summaries = summary_from_db(db_session, "user1", ["chest", "waist"])
        assert [s.metric for s in summaries] == ["chest"]

    def test_all_metrics_when_none_requested(self, db_session):
        _seed_chest(db_session)
        insert_measurement_point(db_session, metric="weight",
                                 measured_date=date(2026, 3, 15), value=70.0, unit="KG")
        metrics = {s.metric for s in summary_from_db(db_session, "user1", None)}
        assert metrics == {"chest", "weight"}


class TestDayFromDb:
    def test_reproduces_detail_dict_and_units(self, db_session):
        _seed_day(db_session)
        insert_body_profile(db_session)
        result = day_from_db(db_session, "user1", date(2026, 3, 1))

        assert result.metrics == {
            "weight": 71.2, "neck": 37, "chest": 96, "waist": 82,
            "abdomen": 88, "hips": 95, "thigh": 55, "calf": 37.5,
            "biceps": 32, "body_fat": 20.4,
        }
        assert result.weight_unit == "KG"
        assert result.size_unit == "CM"
        assert result.bmi == compute_bmi(71.2, 175)
        assert result.date == "2026-03-01"


class TestBmiFromDb:
    def test_picks_latest_weight_when_no_date(self, db_session):
        insert_body_profile(db_session)
        insert_measurement_point(db_session, metric="weight",
                                 measured_date=date(2026, 3, 1), value=71.2, unit="KG")
        insert_measurement_point(db_session, metric="weight",
                                 measured_date=date(2026, 3, 15), value=70.0, unit="KG")
        result = bmi_from_db(db_session, "user1", None)
        assert result is not None
        assert result.date == "2026-03-15"
        assert result.weight_kg == 70.0
        assert result.height_cm == 175
        assert result.bmi == 22.9

    def test_specific_date(self, db_session):
        insert_body_profile(db_session)
        insert_measurement_point(db_session, metric="weight",
                                 measured_date=date(2026, 3, 1), value=71.2, unit="KG")
        insert_measurement_point(db_session, metric="weight",
                                 measured_date=date(2026, 3, 15), value=70.0, unit="KG")
        result = bmi_from_db(db_session, "user1", date(2026, 3, 1))
        assert result.weight_kg == 71.2

    def test_none_without_height(self, db_session):
        insert_measurement_point(db_session, metric="weight",
                                 measured_date=date(2026, 3, 15), value=70.0, unit="KG")
        assert bmi_from_db(db_session, "user1", None) is None

    def test_none_without_weight(self, db_session):
        insert_body_profile(db_session)
        assert bmi_from_db(db_session, "user1", None) is None


class TestMeasurementsAreStale:
    def test_empty_is_stale(self, db_session):
        assert measurements_are_stale(db_session, "user1", 3600) is True

    def test_fresh_is_not_stale(self, db_session):
        insert_measurement_point(db_session, updated_at=datetime.now(timezone.utc))
        assert measurements_are_stale(db_session, "user1", 3600) is False

    def test_old_is_stale(self, db_session):
        old = datetime.now(timezone.utc) - timedelta(seconds=7200)
        insert_measurement_point(db_session, updated_at=old)
        assert measurements_are_stale(db_session, "user1", 3600) is True


class TestEnsureFresh:
    def test_syncs_when_stale(self, db_session):
        client = MagicMock()
        client.get_weight_chart.return_value = {"weights": {"2026-03-15": 70.0}}
        client.get_metric_series.return_value = []
        client.get_height.return_value = {"height_cm": 175}
        synced = ensure_fresh(db_session, client, "user1", 3600)
        assert synced is True
        client.get_weight_chart.assert_called_once()

    def test_skips_when_fresh(self, db_session):
        insert_measurement_point(db_session, updated_at=datetime.now(timezone.utc))
        client = MagicMock()
        synced = ensure_fresh(db_session, client, "user1", 3600)
        assert synced is False
        client.get_weight_chart.assert_not_called()


class TestSeriesFromDb:
    def test_newest_first_ordering(self, db_session):
        _seed_chest(db_session)
        series = series_from_db(db_session, "user1", "chest")
        dates = [p.date for p in series.points]
        assert dates == sorted(dates, reverse=True)
        assert series.metric == "chest"
        assert series.count == len(series.points)
        assert series.unit == "CM"

    def test_respects_limit(self, db_session):
        _seed_chest(db_session)
        series = series_from_db(db_session, "user1", "chest", limit=2)
        assert series.count == 2
        assert series.points[0].date == "2026-03-01"

    def test_respects_start_and_end(self, db_session):
        _seed_chest(db_session)
        series = series_from_db(
            db_session, "user1", "chest",
            start=date(2026, 1, 1), end=date(2026, 2, 20),
        )
        dates = {p.date for p in series.points}
        assert dates == {"2026-01-15", "2026-02-01", "2026-02-15"}
