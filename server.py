import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from sqlalchemy.orm import Session, joinedload

from .database import SessionLocal, init_db
from .fitatu_client import FitatuClient
from .models import DailyNutrition, MealNutrition
from .schemas import MacroTotals
from .service import db_day_to_schema, sync_day_from_fitatu

FITATU_USERNAME = os.getenv("FITATU_USERNAME")
FITATU_PASSWORD = os.getenv("FITATU_PASSWORD")

if not FITATU_USERNAME or not FITATU_PASSWORD:
    raise RuntimeError("FITATU_USERNAME and FITATU_PASSWORD must be set")

client = FitatuClient(FITATU_USERNAME, FITATU_PASSWORD)

mcp = FastMCP(
    name="fitatu-nutrition-mcp",
    instructions=(
        "Use tools to sync and read daily nutrition, macros, and meals from Fitatu-backed SQLite storage."
    ),
    streamable_http_path="/",
)

mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="Fitatu Nutrition MCP Server",
    version="1.0.0",
    description="MCP server exposing daily meals and macro nutrient information",
    lifespan=lifespan,
)


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
    with SessionLocal() as db:
        if not client.user_id:
            client.login()
        if not client.user_id:
            raise ValueError("Could not determine user_id after login")

        before_meals, before_items = _cache_counts(db, client.user_id, day_date)
        summary = sync_day_from_fitatu(db, client, day_date)
        after_meals, after_items = _cache_counts(db, summary.user_id, day_date)

        return {
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


@mcp.tool(name="get_day_summary", description="Get full daily nutrition summary including meals and items")
def mcp_get_day_summary(day_date: str) -> dict:
    with SessionLocal() as db:
        day_row = _load_day(db, day_date)
        if not day_row:
            raise ValueError("Day data not found. Call sync_day first.")
        return db_day_to_schema(day_row).model_dump()


@mcp.tool(name="get_day_macros", description="Get macro totals for a day")
def mcp_get_day_macros(day_date: str) -> dict:
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
    with SessionLocal() as db:
        day_row = _load_day(db, day_date)
        if not day_row:
            raise ValueError("Day data not found. Call sync_day first.")

        summary = db_day_to_schema(day_row)
        return {"day_date": summary.day_date, "user_id": summary.user_id, "meals": [m.model_dump() for m in summary.meals]}


@mcp.tool(name="get_cache_stats", description="Get cached meal/item counts and macro totals for a day")
def mcp_get_cache_stats(day_date: str) -> dict:
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
