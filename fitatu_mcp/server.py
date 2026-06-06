import os
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

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

MAX_RANGE_DAYS_COMPACT = 31   # sync_day, get_day_macros, get_cache_stats
MAX_RANGE_DAYS_VERBOSE = 7    # get_day_summary, get_day_meals


def _parse_date(day_date: str) -> date:
    if not _DATE_RE.match(day_date):
        raise ValueError(f"Invalid date '{day_date}': must be YYYY-MM-DD (e.g. 2024-01-31)")
    try:
        return datetime.strptime(day_date, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Invalid date '{day_date}': date does not exist in the calendar")


def _validate_day_date(day_date: str) -> None:
    _parse_date(day_date)


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


def _cache_counts(db: Session, user_id: str, day_date: str) -> tuple[int, int]:
    day_row = (
        db.query(DailyNutrition)
        .options(joinedload(DailyNutrition.meals).joinedload(MealNutrition.items))
        .filter(DailyNutrition.user_id == user_id, DailyNutrition.day_date == day_date)
        .one_or_none()
    )

    if not day_row:
        return 0, 0

    meals_count = len(day_row.meals)
    items_count = sum(len(meal.items) for meal in day_row.meals)
    return meals_count, items_count


def _load_or_sync_day(db: Session, user_id: str, day_date: str) -> DailyNutrition:
    day_row = _load_day(db, user_id, day_date)

    if day_row and not _is_today_stale(day_row, day_date):
        return day_row

    if day_row:
        logger.info("Stale today cache for day_date=%s user_id=%s; triggering re-sync", day_date, user_id)
    else:
        logger.info("Cache miss for day_date=%s user_id=%s; triggering auto-sync", day_date, user_id)

    summary = sync_day_from_fitatu(db, client, day_date)
    day_row = _load_day(db, summary.user_id, day_date)
    if not day_row:
        raise ValueError("Day data not found after auto-sync. Check Fitatu source data.")
    return day_row


def _is_today_stale(day_row: DailyNutrition, day_date: str) -> bool:
    if day_date != date.today().isoformat():
        return False
    if day_row.updated_at is None:
        return True
    age_seconds = (datetime.now(timezone.utc) - day_row.updated_at.replace(tzinfo=timezone.utc)).total_seconds()
    return age_seconds > TODAY_TTL_SECONDS


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@mcp.tool(
    name="sync_day",
    description=(
        "Sync daily nutrition from Fitatu into SQLite for a date range. "
        "start_date is required (YYYY-MM-DD). end_date defaults to start_date. "
        "Maximum range: 31 days."
    ),
)
def mcp_sync_day(start_date: str, end_date: str = "") -> dict:
    end_date = end_date or start_date
    logger.info("Tool sync_day called start_date=%s end_date=%s", start_date, end_date)
    start, end = _validate_date_range(start_date, end_date, MAX_RANGE_DAYS_COMPACT)
    days = []
    with SessionLocal() as db:
        user_id = _ensure_user_id()
        for day_date in _iter_date_range(start, end):
            before_meals, before_items = _cache_counts(db, user_id, day_date)
            summary = sync_day_from_fitatu(db, client, day_date)
            after_meals, after_items = _cache_counts(db, summary.user_id, day_date)
            days.append({
                "status": "synced",
                "user_id": summary.user_id,
                "day_date": summary.day_date,
                "totals": summary.totals.model_dump(),
                "cache": {
                    "meals_before": before_meals,
                    "meals_after": after_meals,
                    "items_before": before_items,
                    "items_after": after_items,
                },
            })
            logger.info("sync_day synced day_date=%s meals=%s items=%s", day_date, after_meals, after_items)
    return _range_envelope(start_date, end_date, days)


@mcp.tool(
    name="get_day_summary",
    description=(
        "Get full daily nutrition summary including meals and items for a date range. "
        "start_date is required (YYYY-MM-DD). end_date defaults to start_date. "
        "Maximum range: 7 days."
    ),
)
def mcp_get_day_summary(start_date: str, end_date: str = "") -> dict:
    end_date = end_date or start_date
    logger.info("Tool get_day_summary called start_date=%s end_date=%s", start_date, end_date)
    start, end = _validate_date_range(start_date, end_date, MAX_RANGE_DAYS_VERBOSE)
    days = []
    with SessionLocal() as db:
        user_id = _ensure_user_id()
        for day_date in _iter_date_range(start, end):
            try:
                day_row = _load_or_sync_day(db, user_id, day_date)
                days.append(db_day_to_schema(day_row).model_dump())
            except Exception as exc:
                logger.warning("get_day_summary failed for day_date=%s: %s", day_date, exc)
                days.append({"day_date": day_date, "error": str(exc)})
    return _range_envelope(start_date, end_date, days)


@mcp.tool(
    name="get_day_macros",
    description=(
        "Get macro totals for a date range. "
        "start_date is required (YYYY-MM-DD). end_date defaults to start_date. "
        "Maximum range: 31 days."
    ),
)
def mcp_get_day_macros(start_date: str, end_date: str = "") -> dict:
    end_date = end_date or start_date
    logger.info("Tool get_day_macros called start_date=%s end_date=%s", start_date, end_date)
    start, end = _validate_date_range(start_date, end_date, MAX_RANGE_DAYS_COMPACT)
    days = []
    with SessionLocal() as db:
        user_id = _ensure_user_id()
        for day_date in _iter_date_range(start, end):
            try:
                day_row = _load_or_sync_day(db, user_id, day_date)
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


@mcp.tool(
    name="get_day_meals",
    description=(
        "Get meal summaries and meal items for a date range. "
        "start_date is required (YYYY-MM-DD). end_date defaults to start_date. "
        "Maximum range: 7 days."
    ),
)
def mcp_get_day_meals(start_date: str, end_date: str = "") -> dict:
    end_date = end_date or start_date
    logger.info("Tool get_day_meals called start_date=%s end_date=%s", start_date, end_date)
    start, end = _validate_date_range(start_date, end_date, MAX_RANGE_DAYS_VERBOSE)
    days = []
    with SessionLocal() as db:
        user_id = _ensure_user_id()
        for day_date in _iter_date_range(start, end):
            try:
                day_row = _load_or_sync_day(db, user_id, day_date)
                summary = db_day_to_schema(day_row)
                days.append({
                    "day_date": summary.day_date,
                    "user_id": summary.user_id,
                    "meals": [m.model_dump() for m in summary.meals],
                })
            except Exception as exc:
                logger.warning("get_day_meals failed for day_date=%s: %s", day_date, exc)
                days.append({"day_date": day_date, "error": str(exc)})
    return _range_envelope(start_date, end_date, days)


@mcp.tool(
    name="get_cache_stats",
    description=(
        "Get cached meal/item counts and macro totals for a date range. "
        "start_date is required (YYYY-MM-DD). end_date defaults to start_date. "
        "Maximum range: 31 days."
    ),
)
def mcp_get_cache_stats(start_date: str, end_date: str = "") -> dict:
    end_date = end_date or start_date
    logger.info("Tool get_cache_stats called start_date=%s end_date=%s", start_date, end_date)
    start, end = _validate_date_range(start_date, end_date, MAX_RANGE_DAYS_COMPACT)
    days = []
    with SessionLocal() as db:
        user_id = _ensure_user_id()
        for day_date in _iter_date_range(start, end):
            try:
                day_row = _load_or_sync_day(db, user_id, day_date)
                days.append({
                    "day_date": day_row.day_date.isoformat(),
                    "user_id": day_row.user_id,
                    "updated_at": day_row.updated_at.isoformat() if day_row.updated_at else None,
                    "totals": {
                        "energy": day_row.total_energy,
                        "protein": day_row.total_protein,
                        "fat": day_row.total_fat,
                        "carbohydrate": day_row.total_carbohydrate,
                        "fiber": day_row.total_fiber,
                        "sugars": day_row.total_sugars,
                        "salt": day_row.total_salt,
                    },
                    "cache": {
                        "meals": len(day_row.meals),
                        "items": sum(len(meal.items) for meal in day_row.meals),
                        "per_meal": [
                            {"meal_key": meal.meal_key, "meal_name": meal.meal_name, "items": len(meal.items)}
                            for meal in day_row.meals
                        ],
                    },
                })
            except Exception as exc:
                logger.warning("get_cache_stats failed for day_date=%s: %s", day_date, exc)
                days.append({"day_date": day_date, "error": str(exc)})
    return _range_envelope(start_date, end_date, days)


app.mount("/mcp", mcp_app)
