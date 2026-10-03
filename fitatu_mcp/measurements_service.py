import logging
from datetime import date, datetime, timezone

from sqlalchemy import func

from .fitatu_client import FitatuClient
from .models import MeasurementPoint, UserBodyProfile
from .schemas import (
    BmiSchema,
    DayMeasurementsSchema,
    MeasurementPointSchema,
    MetricSeriesSchema,
    MetricSummarySchema,
)
from .service import safe_float

logger = logging.getLogger(__name__)


# Canonical tool-facing metric name -> Fitatu API key.
METRIC_API = {
    "weight": "weight",
    "neck": "neck",
    "chest": "chest",
    "waist": "waist",
    "abdomen": "stomach",
    "hips": "hips",
    "thigh": "thigh",
    "calf": "calf",
    "biceps": "biceps",
    "body_fat": "fatPercentage",
}
# Reverse map: API key (or tool name) -> canonical tool name.
API_METRIC = {api_key: metric for metric, api_key in METRIC_API.items()}

# Size metrics (everything except weight) carry a per-part series endpoint.
SIZE_METRICS = {metric: api_key for metric, api_key in METRIC_API.items() if metric != "weight"}

ALLOWED_METRICS = sorted(METRIC_API)


def normalize_metric(name: str) -> str:
    """Return the canonical tool name for a tool name or raw API key; raise on unknown."""
    if name in METRIC_API:
        return name
    if name in API_METRIC:
        return API_METRIC[name]
    raise ValueError(f"Unknown metric '{name}'. Allowed: {', '.join(ALLOWED_METRICS)}")


def metric_unit(metric: str) -> str:
    if metric == "weight":
        return "KG"
    if metric == "body_fat":
        return "%"
    return "CM"


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _upsert_point(db, user_id: str, metric: str, measured_date: date, value: float, unit: str | None) -> None:
    row = (
        db.query(MeasurementPoint)
        .filter(
            MeasurementPoint.user_id == user_id,
            MeasurementPoint.metric == metric,
            MeasurementPoint.measured_date == measured_date,
        )
        .one_or_none()
    )
    now = datetime.now(timezone.utc)
    if row:
        row.value = value
        row.unit = unit
        row.updated_at = now
    else:
        db.add(
            MeasurementPoint(
                user_id=user_id,
                metric=metric,
                measured_date=measured_date,
                value=value,
                unit=unit,
            )
        )


def _upsert_profile(db, user_id: str, profile_data: dict) -> None:
    row = db.get(UserBodyProfile, user_id)
    now = datetime.now(timezone.utc)
    if row is None:
        row = UserBodyProfile(user_id=user_id)
        db.add(row)
    row.height_cm = profile_data.get("height_cm")
    if profile_data.get("weight_unit") is not None:
        row.weight_unit = profile_data.get("weight_unit")
    if profile_data.get("size_unit") is not None:
        row.size_unit = profile_data.get("size_unit")
    row.updated_at = now


def sync_measurements(db, client: FitatuClient, user_id: str) -> int:
    """Full re-sync of all metrics + height. Idempotent. Returns number of points upserted."""
    logger.info("Measurements sync start user_id=%s", user_id)
    count = 0

    chart = client.get_weight_chart() or {}
    weights = chart.get("weights") or {}
    for day_str, kg in weights.items():
        _upsert_point(db, user_id, "weight", _parse_date(day_str), safe_float(kg), "KG")
        count += 1

    for metric, api_key in SIZE_METRICS.items():
        unit = metric_unit(metric)
        series = client.get_metric_series(api_key, 500) or []
        for point in series:
            day_str = point.get("date")
            if not day_str:
                continue
            _upsert_point(db, user_id, metric, _parse_date(day_str), safe_float(point.get("value")), unit)
            count += 1

    profile_data = client.get_height(date.today().isoformat())
    _upsert_profile(db, user_id, profile_data)

    db.commit()
    logger.info("Measurements sync complete user_id=%s points=%s", user_id, count)
    return count


def measurements_are_stale(db, user_id: str, ttl: int) -> bool:
    newest = (
        db.query(func.max(MeasurementPoint.updated_at))
        .filter(MeasurementPoint.user_id == user_id)
        .scalar()
    )
    if newest is None:
        return True
    age_seconds = (datetime.now(timezone.utc) - newest.replace(tzinfo=timezone.utc)).total_seconds()
    return age_seconds > ttl


def ensure_fresh(db, client: FitatuClient, user_id: str, ttl: int) -> bool:
    if measurements_are_stale(db, user_id, ttl):
        logger.info("Measurements stale/empty user_id=%s; triggering sync", user_id)
        sync_measurements(db, client, user_id)
        return True
    return False


def series_from_db(
    db,
    user_id: str,
    metric: str,
    start: date | None = None,
    end: date | None = None,
    limit: int = 500,
) -> MetricSeriesSchema:
    query = db.query(MeasurementPoint).filter(
        MeasurementPoint.user_id == user_id,
        MeasurementPoint.metric == metric,
    )
    if start is not None:
        query = query.filter(MeasurementPoint.measured_date >= start)
    if end is not None:
        query = query.filter(MeasurementPoint.measured_date <= end)
    rows = query.order_by(MeasurementPoint.measured_date.desc()).limit(limit).all()

    unit = rows[0].unit if rows else metric_unit(metric)
    points = [
        MeasurementPointSchema(date=row.measured_date.isoformat(), value=row.value, unit=row.unit)
        for row in rows
    ]
    return MetricSeriesSchema(metric=metric, unit=unit, count=len(points), points=points)


def summary_from_db(db, user_id: str, metrics: list[str] | None = None) -> list[MetricSummarySchema]:
    target = metrics if metrics else list(METRIC_API)
    summaries: list[MetricSummarySchema] = []
    for metric in target:
        oldest = (
            db.query(MeasurementPoint)
            .filter(MeasurementPoint.user_id == user_id, MeasurementPoint.metric == metric)
            .order_by(MeasurementPoint.measured_date.asc())
            .first()
        )
        newest = (
            db.query(MeasurementPoint)
            .filter(MeasurementPoint.user_id == user_id, MeasurementPoint.metric == metric)
            .order_by(MeasurementPoint.measured_date.desc())
            .first()
        )
        if oldest is None or newest is None:
            continue
        difference = round(newest.value - oldest.value, 6)
        summaries.append(
            MetricSummarySchema(
                metric=metric,
                start_value=oldest.value,
                end_value=newest.value,
                difference=difference,
                latest_date=newest.measured_date.isoformat(),
                unit=newest.unit or metric_unit(metric),
            )
        )
    return summaries


def compute_bmi(weight_kg: float | None, height_cm: float | None) -> float | None:
    if not weight_kg or not height_cm or height_cm <= 0:
        return None
    return round(weight_kg / (height_cm / 100) ** 2, 1)


def bmi_from_db(db, user_id: str, measured_date: date | None = None) -> BmiSchema | None:
    profile = db.get(UserBodyProfile, user_id)
    height_cm = profile.height_cm if profile else None
    if not height_cm:
        return None

    query = db.query(MeasurementPoint).filter(
        MeasurementPoint.user_id == user_id,
        MeasurementPoint.metric == "weight",
    )
    if measured_date is not None:
        row = query.filter(MeasurementPoint.measured_date == measured_date).one_or_none()
    else:
        row = query.order_by(MeasurementPoint.measured_date.desc()).first()
    if row is None:
        return None

    bmi = compute_bmi(row.value, height_cm)
    if bmi is None:
        return None
    return BmiSchema(
        date=row.measured_date.isoformat(),
        weight_kg=row.value,
        height_cm=height_cm,
        bmi=bmi,
    )


def day_from_db(db, user_id: str, measured_date: date) -> DayMeasurementsSchema:
    rows = (
        db.query(MeasurementPoint)
        .filter(
            MeasurementPoint.user_id == user_id,
            MeasurementPoint.measured_date == measured_date,
        )
        .all()
    )
    metrics: dict[str, float] = {}
    weight_unit: str | None = None
    size_unit: str | None = None
    for row in rows:
        metrics[row.metric] = row.value
        if row.metric == "weight":
            weight_unit = row.unit or weight_unit
        elif row.metric != "body_fat":
            size_unit = row.unit or size_unit

    bmi_obj = bmi_from_db(db, user_id, measured_date)
    return DayMeasurementsSchema(
        date=measured_date.isoformat(),
        metrics=metrics,
        bmi=bmi_obj.bmi if bmi_obj else None,
        weight_unit=weight_unit,
        size_unit=size_unit,
    )
