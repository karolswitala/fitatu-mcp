import os
import logging
from contextlib import asynccontextmanager

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


def _load_day(db: Session, day_date: str) -> DailyNutrition | None:
    return (
        db.query(DailyNutrition)
        .options(joinedload(DailyNutrition.meals).joinedload(MealNutrition.items))
        .filter(DailyNutrition.day_date == day_date)
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@mcp.tool(name="sync_day", description="Sync daily nutrition from Fitatu into SQLite for a given YYYY-MM-DD date")
def mcp_sync_day(day_date: str) -> dict:
    logger.info("Tool sync_day called day_date=%s", day_date)
    with SessionLocal() as db:
        if not client.user_id:
            client.login()
        if not client.user_id:
            raise ValueError("Could not determine user_id after login")

        before_meals, before_items = _cache_counts(db, client.user_id, day_date)
        summary = sync_day_from_fitatu(db, client, day_date)
        after_meals, after_items = _cache_counts(db, summary.user_id, day_date)

        result = {
            "status": "synced",
            "user_id": summary.user_id,
            "day_date": summary.day_date,
            "totals": summary.totals.model_dump(),
            "cache_delta": {
                "meals_added": max(after_meals - before_meals, 0),
                "items_added": max(after_items - before_items, 0),
            },
            "cache_totals": {
                "meals": after_meals,
                "items": after_items,
            },
        }
        logger.info(
            "Tool sync_day completed day_date=%s user_id=%s meals_added=%s items_added=%s",
            summary.day_date,
            summary.user_id,
            result["cache_delta"]["meals_added"],
            result["cache_delta"]["items_added"],
        )
        return result


@mcp.tool(name="get_day_summary", description="Get full daily nutrition summary including meals and items")
def mcp_get_day_summary(day_date: str) -> dict:
    logger.info("Tool get_day_summary called day_date=%s", day_date)
    with SessionLocal() as db:
        day_row = _load_day(db, day_date)
        if not day_row:
            raise ValueError("Day data not found. Call sync_day first.")
        return db_day_to_schema(day_row).model_dump()


@mcp.tool(name="get_day_macros", description="Get macro totals for a day")
def mcp_get_day_macros(day_date: str) -> dict:
    logger.info("Tool get_day_macros called day_date=%s", day_date)
    with SessionLocal() as db:
        day_row = db.query(DailyNutrition).filter(DailyNutrition.day_date == day_date).one_or_none()
        if not day_row:
            raise ValueError("Day data not found. Call sync_day first.")

        return MacroTotals(
            energy=day_row.total_energy,
            protein=day_row.total_protein,
            fat=day_row.total_fat,
            carbohydrate=day_row.total_carbohydrate,
            fiber=day_row.total_fiber,
            sugars=day_row.total_sugars,
            salt=day_row.total_salt,
        ).model_dump()


@mcp.tool(name="get_day_meals", description="Get meal summaries and meal items for a day")
def mcp_get_day_meals(day_date: str) -> dict:
    logger.info("Tool get_day_meals called day_date=%s", day_date)
    with SessionLocal() as db:
        day_row = _load_day(db, day_date)
        if not day_row:
            raise ValueError("Day data not found. Call sync_day first.")

        summary = db_day_to_schema(day_row)
        return {"day_date": summary.day_date, "user_id": summary.user_id, "meals": [m.model_dump() for m in summary.meals]}


@mcp.tool(name="get_cache_stats", description="Get cached meal/item counts and macro totals for a day")
def mcp_get_cache_stats(day_date: str) -> dict:
    logger.info("Tool get_cache_stats called day_date=%s", day_date)
    with SessionLocal() as db:
        day_row = _load_day(db, day_date)
        if not day_row:
            raise ValueError("Day data not found in cache. Call sync_day first.")

        return {
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
                    {
                        "meal_key": meal.meal_key,
                        "meal_name": meal.meal_name,
                        "items": len(meal.items),
                    }
                    for meal in day_row.meals
                ],
            },
        }


app.mount("/mcp", mcp_app)
