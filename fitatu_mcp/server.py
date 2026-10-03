import os
import logging
import re
import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.orm import Session, joinedload
from .database import SessionLocal, init_db
from .fitatu_client import FitatuClient
from .models import DailyNutrition, MealNutrition
from .schemas import MacroTotals
from .service import db_day_to_schema, sync_day_from_fitatu
from .measurements_service import (
    ALLOWED_METRICS,
    bmi_from_db,
    day_from_db,
    ensure_fresh,
    normalize_metric,
    series_from_db,
    summary_from_db,
    sync_measurements as sync_measurements_service,
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

FITATU_USERNAME = os.getenv("FITATU_USERNAME")
FITATU_PASSWORD = os.getenv("FITATU_PASSWORD")
MCP_API_KEY = os.getenv("MCP_API_KEY")
MCP_ENABLE_DNS_REBINDING_PROTECTION = os.getenv("MCP_ENABLE_DNS_REBINDING_PROTECTION", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MCP_ALLOWED_HOSTS = os.getenv(
    "MCP_ALLOWED_HOSTS",
    "localhost,localhost:*,127.0.0.1,127.0.0.1:*,fitatu-mcp,fitatu-mcp:*,host.docker.internal,host.docker.internal:*",
)

if not FITATU_USERNAME or not FITATU_PASSWORD:
    raise RuntimeError("FITATU_USERNAME and FITATU_PASSWORD must be set")
if not MCP_API_KEY:
    raise RuntimeError("MCP_API_KEY must be set")

TODAY_TTL_SECONDS = int(os.getenv("FITATU_TODAY_TTL_SECONDS", "300"))
MEASUREMENTS_TTL_SECONDS = int(os.getenv("MEASUREMENTS_TTL_SECONDS", "3600"))

client = FitatuClient(FITATU_USERNAME, FITATU_PASSWORD)

transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=MCP_ENABLE_DNS_REBINDING_PROTECTION,
    allowed_hosts=[h.strip() for h in MCP_ALLOWED_HOSTS.split(",") if h.strip()],
)

mcp = FastMCP(
    name="fitatu-nutrition-mcp",
    instructions=(
        "Use tools to sync and read daily nutrition, macros, and meals from Fitatu-backed SQLite storage."
    ),
    streamable_http_path="/",
    transport_security=transport_security,
)

mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server startup: initializing DB and MCP session manager")
    init_db()
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="Fitatu Nutrition MCP Server",
    version="1.0.0",
    description="MCP server exposing daily meals and macro nutrient information",
    lifespan=lifespan,
)


@app.middleware("http")
async def bearer_auth(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != MCP_API_KEY:
            logger.warning(
                "Unauthorized MCP request path=%s client=%s auth_prefix=%s",
                request.url.path,
                request.client.host if request.client else "unknown",
                auth[:16],
            )
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

MAX_RANGE_DAYS_COMPACT = 31   # get_day_macros
MAX_RANGE_DAYS_VERBOSE = 7    # get_day_summary
MAX_RANGE_DAYS_MEASUREMENTS = 3660   # get_measurement_history (~10y; measurements span years)


def _parse_date(day_date: str) -> date:
    if not _DATE_RE.match(day_date):
        raise ValueError(f"Invalid date '{day_date}': must be YYYY-MM-DD (e.g. 2024-01-31)")
    try:
        return datetime.strptime(day_date, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Invalid date '{day_date}': date does not exist in the calendar")


def _validate_date_range(start_date: str, end_date: str, max_days: int) -> tuple[date, date]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if end < start:
        raise ValueError(f"end_date '{end_date}' must not be before start_date '{start_date}'")
    span = (end - start).days + 1
    if span > max_days:
        raise ValueError(f"Date range spans {span} days; maximum allowed is {max_days}")
    return start, end


def _iter_date_range(start: date, end: date):
    current = start
    while current <= end:
        yield current.isoformat()
        current = date.fromordinal(current.toordinal() + 1)


def _range_envelope(start_date: str, end_date: str, days: list) -> dict:
    return {
        "start_date": start_date,
        "end_date": end_date,
        "day_count": len(days),
        "days": days,
    }


def _ensure_user_id() -> str:
    if not client.user_id:
        client.login()
    if not client.user_id:
        raise ValueError("Could not determine user_id after login")
    return client.user_id


def _load_day(db: Session, user_id: str, day_date: str) -> DailyNutrition | None:
    return (
        db.query(DailyNutrition)
        .options(joinedload(DailyNutrition.meals).joinedload(MealNutrition.items))
        .filter(DailyNutrition.user_id == user_id, DailyNutrition.day_date == day_date)
        .one_or_none()
    )


def _load_or_sync_day(db: Session, user_id: str, day_date: str, force_refresh: bool = False) -> DailyNutrition:
    day_row = _load_day(db, user_id, day_date)

    if day_row and not force_refresh and not _is_stale(day_row, day_date):
        return day_row

    if force_refresh:
        logger.info("Force refresh for day_date=%s user_id=%s; re-syncing", day_date, user_id)
    elif day_row:
        logger.info("Stale cache for day_date=%s user_id=%s; triggering re-sync", day_date, user_id)
    else:
        logger.info("Cache miss for day_date=%s user_id=%s; triggering auto-sync", day_date, user_id)

    summary = sync_day_from_fitatu(db, client, day_date)
    day_row = _load_day(db, summary.user_id, day_date)
    if not day_row:
        raise ValueError("Day data not found after auto-sync. Check Fitatu source data.")
    return day_row


def _is_stale(day_row: DailyNutrition, day_date: str) -> bool:
    today = date.today()
    parsed = date.fromisoformat(day_date)

    if parsed == today:
        if day_row.updated_at is None:
            return True
        age_seconds = (datetime.now(timezone.utc) - day_row.updated_at.replace(tzinfo=timezone.utc)).total_seconds()
        return age_seconds > TODAY_TTL_SECONDS

    if parsed == today - timedelta(days=1):
        if day_row.updated_at is None:
            return True
        return day_row.updated_at.replace(tzinfo=timezone.utc).date() < today

    return False


def _validate_metric(metric: str) -> str:
    if not metric or not metric.strip():
        raise ValueError(f"Metric is required; allowed are {', '.join(ALLOWED_METRICS)}")
    try:
        return normalize_metric(metric.strip())
    except ValueError:
        raise ValueError(f"Invalid metric '{metric}': allowed are {', '.join(ALLOWED_METRICS)}")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@mcp.tool(
    name="get_day_summary",
    description=(
        "Get full daily nutrition summary including meals and items for a date range. "
        "start_date is required (YYYY-MM-DD). end_date defaults to start_date. "
        "Maximum range: 7 days. Refreshes from Fitatu automatically when a day is missing "
        "or stale; set force_refresh=true to re-fetch now."
    ),
)
async def mcp_get_day_summary(start_date: str, end_date: str = "", force_refresh: bool = False) -> dict:
    end_date = end_date or start_date
    logger.info(
        "Tool get_day_summary called start_date=%s end_date=%s force_refresh=%s",
        start_date, end_date, force_refresh,
    )
    start, end = _validate_date_range(start_date, end_date, MAX_RANGE_DAYS_VERBOSE)

    def _run() -> dict:
        days = []
        with SessionLocal() as db:
            user_id = _ensure_user_id()
            for day_date in _iter_date_range(start, end):
                try:
                    day_row = _load_or_sync_day(db, user_id, day_date, force_refresh)
                    days.append(db_day_to_schema(day_row).model_dump())
                except Exception as exc:
                    logger.warning("get_day_summary failed for day_date=%s: %s", day_date, exc)
                    days.append({"day_date": day_date, "error": str(exc)})
        return _range_envelope(start_date, end_date, days)

    return await asyncio.to_thread(_run)


@mcp.tool(
    name="get_day_macros",
    description=(
        "Get macro totals for a date range. "
        "start_date is required (YYYY-MM-DD). end_date defaults to start_date. "
        "Maximum range: 31 days. Refreshes from Fitatu automatically when a day is missing "
        "or stale; set force_refresh=true to re-fetch now."
    ),
)
async def mcp_get_day_macros(start_date: str, end_date: str = "", force_refresh: bool = False) -> dict:
    end_date = end_date or start_date
    logger.info(
        "Tool get_day_macros called start_date=%s end_date=%s force_refresh=%s",
        start_date, end_date, force_refresh,
    )
    start, end = _validate_date_range(start_date, end_date, MAX_RANGE_DAYS_COMPACT)

    def _run() -> dict:
        days = []
        with SessionLocal() as db:
            user_id = _ensure_user_id()
            for day_date in _iter_date_range(start, end):
                try:
                    day_row = _load_or_sync_day(db, user_id, day_date, force_refresh)
                    macros = MacroTotals(
                        energy=day_row.total_energy,
                        protein=day_row.total_protein,
                        fat=day_row.total_fat,
                        carbohydrate=day_row.total_carbohydrate,
                        fiber=day_row.total_fiber,
                        sugars=day_row.total_sugars,
                        salt=day_row.total_salt,
                    ).model_dump()
                    days.append({"day_date": day_date, **macros})
                except Exception as exc:
                    logger.warning("get_day_macros failed for day_date=%s: %s", day_date, exc)
                    days.append({"day_date": day_date, "error": str(exc)})
        return _range_envelope(start_date, end_date, days)

    return await asyncio.to_thread(_run)



def _refresh_measurements(db: Session, user_id: str, force_refresh: bool) -> None:
    """Freshen the measurement cache: force a full re-fetch, else sync only when stale."""
    if force_refresh:
        logger.info("Force-refreshing measurements user_id=%s", user_id)
        sync_measurements_service(db, client, user_id)
    else:
        ensure_fresh(db, client, user_id, MEASUREMENTS_TTL_SECONDS)


@mcp.tool(
    name="get_body_measurements",
    description=(
        "Current body-measurements dashboard: for every metric (weight, neck, chest, waist, "
        "abdomen, hips, thigh, calf, biceps, body_fat) the first value, latest value, total "
        "change, and latest date, plus current BMI. Reads from the Fitatu-backed cache and "
        "refreshes automatically when stale; set force_refresh=true to re-fetch from Fitatu now."
    ),
)
async def mcp_get_body_measurements(force_refresh: bool = False) -> dict:
    logger.info("Tool get_body_measurements called force_refresh=%s", force_refresh)

    def _run() -> dict:
        with SessionLocal() as db:
            user_id = _ensure_user_id()
            _refresh_measurements(db, user_id, force_refresh)
            summaries = summary_from_db(db, user_id, None)
            bmi = bmi_from_db(db, user_id, None)
            return {
                "metrics": [s.model_dump() for s in summaries],
                "bmi": bmi.model_dump() if bmi else None,
            }

    return await asyncio.to_thread(_run)


@mcp.tool(
    name="get_measurement_history",
    description=(
        "Time series (newest-first) for one body metric, for trends and charts. "
        "metric is required (one of: weight, neck, chest, waist, abdomen, hips, thigh, calf, "
        "biceps, body_fat). Optional from_date/to_date (YYYY-MM-DD) bound the window; omit both "
        "for full history. Refreshes automatically when stale; set force_refresh=true to re-fetch now."
    ),
)
async def mcp_get_measurement_history(
    metric: str, from_date: str = "", to_date: str = "", force_refresh: bool = False
) -> dict:
    logger.info(
        "Tool get_measurement_history called metric=%s from_date=%s to_date=%s force_refresh=%s",
        metric, from_date, to_date, force_refresh,
    )
    canonical = _validate_metric(metric)
    start = end = None
    if from_date and to_date:
        start, end = _validate_date_range(from_date, to_date, MAX_RANGE_DAYS_MEASUREMENTS)
    elif from_date:
        start = _parse_date(from_date)
    elif to_date:
        end = _parse_date(to_date)

    def _run() -> dict:
        with SessionLocal() as db:
            user_id = _ensure_user_id()
            _refresh_measurements(db, user_id, force_refresh)
            return series_from_db(db, user_id, canonical, start, end, None).model_dump()

    return await asyncio.to_thread(_run)


@mcp.tool(
    name="get_measurements_on_date",
    description=(
        "All body metrics recorded on a single date, plus derived BMI and units. "
        "date is required (YYYY-MM-DD). Refreshes automatically when stale; "
        "set force_refresh=true to re-fetch from Fitatu now."
    ),
)
async def mcp_get_measurements_on_date(date: str, force_refresh: bool = False) -> dict:
    logger.info("Tool get_measurements_on_date called date=%s force_refresh=%s", date, force_refresh)
    parsed = _parse_date(date)

    def _run() -> dict:
        with SessionLocal() as db:
            user_id = _ensure_user_id()
            _refresh_measurements(db, user_id, force_refresh)
            return day_from_db(db, user_id, parsed).model_dump()

    return await asyncio.to_thread(_run)


app.mount("/mcp", mcp_app)
