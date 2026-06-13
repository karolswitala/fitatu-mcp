import json
import os
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Mapping

from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.orm import Session, joinedload
from .auth import SessionPool, decode_basic_header
from .database import SessionLocal, init_db
from .fitatu_client import FitatuClient
from .models import DailyNutrition, MealNutrition
from .schemas import MacroTotals
from . import service
from .service import db_day_to_schema, sync_day_from_fitatu

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


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


def _is_today_stale(day_row: DailyNutrition, day_date: str, today_ttl_seconds: int) -> bool:
    if day_date != date.today().isoformat():
        return False
    if day_row.updated_at is None:
        return True
    age_seconds = (datetime.now(timezone.utc) - day_row.updated_at.replace(tzinfo=timezone.utc)).total_seconds()
    return age_seconds > today_ttl_seconds


def build_app(env: Mapping[str, str] | None = None) -> tuple[FastAPI, FastMCP]:
    """Construct (FastAPI, FastMCP) pair from env mapping.

    Per-user auth — each MCP request resolves to its own FitatuClient via header:
      - X-Fitatu-Session: <session_id>            (from fitatu_login tool)
      - Authorization: Basic base64(email:pwd)    (static client config)

    Env keys consumed (all optional except where noted):
      - FITATU_API_SECRET (optional; built-in default)
      - FITATU_BASE_URL_READ, FITATU_BASE_URL_WRITE (optional cluster overrides)
      - FITATU_ALLOW_DELETE (default false; gates destructive tools)
      - FITATU_TODAY_TTL_SECONDS, FITATU_SESSION_TTL_SECONDS, FITATU_SESSION_POOL_MAX
      - MCP_ENABLE_DNS_REBINDING_PROTECTION, MCP_ALLOWED_HOSTS
    """
    env = env if env is not None else os.environ

    today_ttl = int(env.get("FITATU_TODAY_TTL_SECONDS", "300"))
    session_ttl = int(env.get("FITATU_SESSION_TTL_SECONDS", str(30 * 24 * 3600)))
    pool_max = int(env.get("FITATU_SESSION_POOL_MAX", "64"))
    dns_rebind = (env.get("MCP_ENABLE_DNS_REBINDING_PROTECTION", "false").lower() in {"1", "true", "yes", "on"})
    allowed_hosts_csv = env.get(
        "MCP_ALLOWED_HOSTS",
        "localhost,localhost:*,127.0.0.1,127.0.0.1:*,fitatu-mcp,fitatu-mcp:*,host.docker.internal,host.docker.internal:*",
    )
    allow_delete = env.get("FITATU_ALLOW_DELETE", "false").lower() in {"1", "true", "yes", "on"}

    pool = SessionPool(
        base_url_read=env.get("FITATU_BASE_URL_READ"),
        base_url_write=env.get("FITATU_BASE_URL_WRITE"),
        idle_ttl_seconds=session_ttl,
        max_size=pool_max,
    )

    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=dns_rebind,
        allowed_hosts=[h.strip() for h in allowed_hosts_csv.split(",") if h.strip()],
    )

    mcp = FastMCP(
        name="fitatu-nutrition-mcp",
        instructions=(
            "Use tools to sync, read, and write daily nutrition. "
            "Meal items (add/update/delete) log what the user actually ate; "
            "products (create/get/delete/search) manage the user's reusable food catalog. "
            "Authentication: call fitatu_login(email, password) to mint a session, then send "
            "X-Fitatu-Session header. Or configure Authorization: Basic at the client level."
        ),
        streamable_http_path="/",
        transport_security=transport_security,
    )

    async def _resolve_client(ctx: Context) -> FitatuClient:
        """Resolve the FitatuClient for the current MCP request.

        Inspects the inbound Starlette Request headers in precedence order:
          1. X-Fitatu-Session header (preferred — after fitatu_login)
          2. Authorization: Basic base64(email:password)  (static client config)
        Raises ValueError with a friendly message when neither is present or valid.
        """
        req = None
        try:
            if ctx is not None:
                rc = ctx.request_context
                req = rc.request if rc is not None else None
        except Exception:
            # FastMCP raises when no MCP request context is active
            # (e.g. in-process pytest call_tool). Fall through to test seam.
            pass
        if req is None:
            # Test seam: in-process tool calls without a real HTTP request
            # short-circuit to a pool-configured default client (set by tests).
            if pool._test_default_client is not None:
                return pool._test_default_client
            raise ValueError(
                "auth_required: no HTTP request context. "
                "Pass X-Fitatu-Session or Authorization: Basic."
            )
        sid = req.headers.get("X-Fitatu-Session")
        if sid:
            client = pool.resolve_session(sid)
            if client is None:
                raise ValueError(
                    "auth_invalid: X-Fitatu-Session is unknown or expired; "
                    "call fitatu_login again to mint a fresh session."
                )
            return client
        auth = req.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            b64 = auth[6:].strip()
            cached = pool.resolve_basic_cached(b64)
            if cached is not None:
                return cached
            email, password = decode_basic_header(b64)
            return await pool.get_or_login(email, password)
        raise ValueError(
            "auth_required: pass X-Fitatu-Session (from fitatu_login) "
            "or Authorization: Basic base64(email:password)"
        )

    async def _load_or_sync_day(db: Session, client: FitatuClient, user_id: str, day_date: str) -> DailyNutrition:
        day_row = _load_day(db, user_id, day_date)
        if day_row and not _is_today_stale(day_row, day_date, today_ttl):
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

    # -- Auth tool --

    @mcp.tool(
        name="fitatu_login",
        description=(
            "Authenticate against Fitatu with your account email and password, returning a "
            "session_id to pass as the X-Fitatu-Session header on subsequent tool calls. "
            "SECURITY: this writes your password into chat history; prefer configuring "
            "Authorization: Basic at the MCP client level when possible. The server NEVER "
            "persists credentials to disk; they live only in process memory."
        ),
    )
    async def mcp_fitatu_login(email: str, password: str, ctx: Context = None) -> dict:
        if not email or not password:
            raise ValueError("email and password are required")
        client = await pool.get_or_login(email, password)
        sid = await pool.register_session(client)
        return {
            "ok": True,
            "session_id": sid,
            "user_id": client.user_id,
            "next_step": (
                "Send subsequent requests with header `X-Fitatu-Session: <session_id>`. "
                "Session expires after 30 days of inactivity."
            ),
        }

    # -- Existing read tools --

    @mcp.tool(
        name="sync_day",
        description=(
            "Sync daily nutrition from Fitatu into SQLite for a date range. "
            "start_date is required (YYYY-MM-DD). end_date defaults to start_date. Maximum range: 31 days."
        ),
    )
    async def mcp_sync_day(start_date: str, end_date: str = "", ctx: Context = None) -> dict:
        client = await _resolve_client(ctx)
        end_date = end_date or start_date
        logger.info("Tool sync_day called start_date=%s end_date=%s", start_date, end_date)
        start, end = _validate_date_range(start_date, end_date, MAX_RANGE_DAYS_COMPACT)
        days = []
        with SessionLocal() as db:
            user_id = client.user_id
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
        return _range_envelope(start_date, end_date, days)

    @mcp.tool(
        name="get_day_summary",
        description=(
            "Get full daily nutrition summary including meals and items for a date range. "
            "start_date is required (YYYY-MM-DD). end_date defaults to start_date. Maximum range: 7 days."
        ),
    )
    async def mcp_get_day_summary(start_date: str, end_date: str = "", ctx: Context = None) -> dict:
        client = await _resolve_client(ctx)
        end_date = end_date or start_date
        start, end = _validate_date_range(start_date, end_date, MAX_RANGE_DAYS_VERBOSE)
        days = []
        with SessionLocal() as db:
            user_id = client.user_id
            for day_date in _iter_date_range(start, end):
                try:
                    day_row = await _load_or_sync_day(db, client, user_id, day_date)
                    days.append(db_day_to_schema(day_row).model_dump())
                except Exception as exc:
                    logger.warning("get_day_summary failed for day_date=%s: %s", day_date, exc)
                    days.append({"day_date": day_date, "error": str(exc)})
        return _range_envelope(start_date, end_date, days)

    @mcp.tool(
        name="get_day_macros",
        description=(
            "Get macro totals for a date range. "
            "start_date is required (YYYY-MM-DD). end_date defaults to start_date. Maximum range: 31 days."
        ),
    )
    async def mcp_get_day_macros(start_date: str, end_date: str = "", ctx: Context = None) -> dict:
        client = await _resolve_client(ctx)
        end_date = end_date or start_date
        start, end = _validate_date_range(start_date, end_date, MAX_RANGE_DAYS_COMPACT)
        days = []
        with SessionLocal() as db:
            user_id = client.user_id
            for day_date in _iter_date_range(start, end):
                try:
                    day_row = await _load_or_sync_day(db, client, user_id, day_date)
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
            "start_date is required (YYYY-MM-DD). end_date defaults to start_date. Maximum range: 7 days."
        ),
    )
    async def mcp_get_day_meals(start_date: str, end_date: str = "", ctx: Context = None) -> dict:
        client = await _resolve_client(ctx)
        end_date = end_date or start_date
        start, end = _validate_date_range(start_date, end_date, MAX_RANGE_DAYS_VERBOSE)
        days = []
        with SessionLocal() as db:
            user_id = client.user_id
            for day_date in _iter_date_range(start, end):
                try:
                    day_row = await _load_or_sync_day(db, client, user_id, day_date)
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
            "start_date is required (YYYY-MM-DD). end_date defaults to start_date. Maximum range: 31 days."
        ),
    )
    async def mcp_get_cache_stats(start_date: str, end_date: str = "", ctx: Context = None) -> dict:
        client = await _resolve_client(ctx)
        end_date = end_date or start_date
        start, end = _validate_date_range(start_date, end_date, MAX_RANGE_DAYS_COMPACT)
        days = []
        with SessionLocal() as db:
            user_id = client.user_id
            for day_date in _iter_date_range(start, end):
                try:
                    day_row = await _load_or_sync_day(db, client, user_id, day_date)
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

    # -- Product write tools (Group 5) --

    @mcp.tool(
        name="create_custom_product",
        description=(
            "Create a user-owned product in the Fitatu catalog. Returns the product id and a local cache row. "
            "Macros are per 100g. Use -1 sentinel for optional macros to skip them."
        ),
    )
    async def mcp_create_custom_product(
        name: str,
        energy: float,
        protein: float,
        fat: float,
        carbohydrate: float,
        brand: str = "",
        fiber: float = -1,
        sodium: float = -1,
        salt: float = -1,
        saturated_fat: float = -1,
        sugars: float = -1,
        cholesterol: float = -1,
        ctx: Context = None,
    ) -> dict:
        client = await _resolve_client(ctx)
        name_clean = (name or "").strip()
        if not name_clean:
            raise ValueError("name must not be empty")
        if len(name_clean) > 200:
            raise ValueError("name must be 200 characters or fewer")
        for label, value in (("energy", energy), ("protein", protein), ("fat", fat), ("carbohydrate", carbohydrate)):
            if value < 0:
                raise ValueError(f"{label} must be >= 0 (got {value})")

        payload: dict = {
            "name": name_clean,
            "energy": energy,
            "protein": protein,
            "fat": fat,
            "carbohydrate": carbohydrate,
        }
        if brand and brand.strip():
            payload["brand"] = brand.strip()
        for src, dest in (
            ("fiber", "fiber"),
            ("sodium", "sodium"),
            ("salt", "salt"),
            ("saturated_fat", "saturatedFat"),
            ("sugars", "sugars"),
            ("cholesterol", "cholesterol"),
        ):
            v = locals()[src]
            if v is not None and v != -1:
                payload[dest] = v

        created = client.create_product(payload)
        product_id = created.get("id")
        if product_id is None:
            raise RuntimeError(f"create_product returned no id: {created}")

        user_id = client.user_id
        with SessionLocal() as db:
            local_cached = True
            try:
                full = client.get_product(int(product_id))
            except RuntimeError as exc:
                logger.warning("Post-create get_product failed: %s", exc)
                full = {"id": int(product_id), "name": name_clean, **{k: v for k, v in payload.items() if k != "name"}}
                local_cached = False
            product = service.upsert_product(db, full, source="custom", user_id=user_id)
            db.commit()
            schema = service.product_to_schema(product)
        return {"ok": True, "product": schema.model_dump(mode="json"), "local_cached": local_cached}

    @mcp.tool(
        name="get_product",
        description="Get a single product by id. Reads local cache first; falls through to Fitatu on miss.",
    )
    async def mcp_get_product(product_id: int, ctx: Context = None) -> dict:
        client = await _resolve_client(ctx)
        if product_id <= 0:
            raise ValueError("product_id must be a positive integer")
        with SessionLocal() as db:
            local = service.get_product_local(db, product_id)
            if local is not None:
                return {"ok": True, "product": service.product_to_schema(local).model_dump(mode="json"), "from_cache": True}
            full = client.get_product(product_id)
            product = service.upsert_product(db, full, source="catalog")
            db.commit()
            return {"ok": True, "product": service.product_to_schema(product).model_dump(mode="json"), "from_cache": False}

    @mcp.tool(
        name="search_products",
        description=(
            "Search products. scope='custom' = local LIKE over cached/created products. "
            "scope='catalog' = live Fitatu search via GET /api/search/new/food (the endpoint the "
            "Fitatu web app uses). scope='all' = custom first, then catalog (dedup by id). "
            "type_filter: 'PRODUCT' (default) | 'RECIPE' | 'ANY'. "
            "Optional macro filters (per 100g) — pass -1 (sentinel for 'unset'): "
            "min_energy/max_energy/min_protein/max_protein/min_fat/max_fat/min_carbohydrate/max_carbohydrate. "
            "Any active macro filter sets hasFilters=true upstream. "
            "Results include `score` (0..5, higher = better name match) and `index` "
            "('SEARCH' = name match, 'LAST_USED' = your history). Short distinctive phrases "
            "(brand or product name) work best."
        ),
    )
    async def mcp_search_products(
        query: str,
        scope: str = "custom",
        limit: int = 20,
        type_filter: str = "PRODUCT",
        min_energy: float = -1,
        max_energy: float = -1,
        min_protein: float = -1,
        max_protein: float = -1,
        min_fat: float = -1,
        max_fat: float = -1,
        min_carbohydrate: float = -1,
        max_carbohydrate: float = -1,
        ctx: Context = None,
    ) -> dict:
        client = await _resolve_client(ctx)
        user_id = client.user_id
        q = (query or "").strip()
        type_filter_u = (type_filter or "PRODUCT").upper().strip()

        if len(q) < 2:
            raise ValueError("query must be at least 2 characters")
        if scope not in {"custom", "catalog", "all"}:
            raise ValueError("scope must be one of custom|catalog|all")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if type_filter_u not in {"PRODUCT", "RECIPE", "ANY"}:
            raise ValueError("type_filter must be PRODUCT|RECIPE|ANY")

        def _macro(value: float) -> float | None:
            return None if value == -1 else value

        custom_results: list[dict] = []
        if scope in {"custom", "all"}:
            with SessionLocal() as db:
                rows = service.search_products_local(db, q, "custom", limit, user_id=user_id)
                custom_results = [
                    {
                        "id": r.id,
                        "name": r.name,
                        "brand": r.brand,
                        "energy": r.energy,
                        "source": r.source,
                        "type": "PRODUCT",
                        "score": None,
                        "index": None,
                    }
                    for r in rows
                ]

        catalog_results: list[dict] = []
        warnings: list[str] = []
        if scope in {"catalog", "all"}:
            try:
                hits = client.search_food(
                    phrase=q,
                    page=1,
                    limit=max(limit * 2, 40),
                    min_energy=_macro(min_energy),
                    max_energy=_macro(max_energy),
                    min_protein=_macro(min_protein),
                    max_protein=_macro(max_protein),
                    min_fat=_macro(min_fat),
                    max_fat=_macro(max_fat),
                    min_carbohydrate=_macro(min_carbohydrate),
                    max_carbohydrate=_macro(max_carbohydrate),
                )
            except RuntimeError as exc:
                logger.warning("search_food upstream failed: %s", exc)
                hits = []
                warnings.append(f"upstream catalog search failed: {exc}")

            seen_ids = {r["id"] for r in custom_results}
            kept = []
            for hit in hits:
                fid = hit.get("foodId")
                if fid is None or fid in seen_ids:
                    continue
                hit_type = (hit.get("type") or "").upper()
                if type_filter_u != "ANY" and hit_type != type_filter_u:
                    continue
                kept.append({
                    "id": fid,
                    "name": hit.get("name"),
                    "brand": hit.get("brand") or None,
                    "manufacturer": hit.get("manufacturer") or None,
                    "energy": hit.get("energy"),
                    "source": "catalog",
                    "type": hit_type or None,
                    "score": hit.get("score"),
                    "index": hit.get("index"),
                    "verified": bool(hit.get("verified")),
                })
                seen_ids.add(fid)
            # Preserve upstream order (Fitatu already ranks SEARCH-hit brand products well).
            catalog_results = kept

        if scope == "custom":
            results = custom_results
        elif scope == "catalog":
            results = catalog_results[:limit]
        else:
            results = (custom_results + catalog_results)[:limit]

        envelope: dict = {
            "ok": True,
            "query": q,
            "scope": scope,
            "type_filter": type_filter_u,
            "results": results,
        }
        if warnings:
            envelope["warnings"] = warnings
        if not results and scope in {"catalog", "all"}:
            envelope["hint"] = (
                "No catalog matches. Try a shorter, more distinctive phrase (a brand or core word), "
                "or use create_custom_product with values from the package label."
            )
        return envelope

    # -- Meal-item write tools (Group 9, spec §15 — PRIMARY user goal) --

    @mcp.tool(
        name="add_meal_item",
        description=(
            "Log a meal item: 'I ate X amount of product/recipe Y for breakfast on date Z'. "
            "Pass date (YYYY-MM-DD), meal_key (breakfast|second_breakfast|lunch|dinner|snack|supper), "
            "product_id (from search_products / create_custom_product / create_recipe — "
            "treat as recipe id when food_type='RECIPE'), "
            "measure_id (from product/recipe's measures[].id; for recipes typically 39='porcja' or 1='g'), "
            "measure_quantity (number of measures, e.g. 1 porcja or 100 g). "
            "food_type='PRODUCT' (default) or 'RECIPE'."
        ),
    )
    async def mcp_add_meal_item(
        date: str,
        meal_key: str,
        product_id: int,
        measure_id: int,
        measure_quantity: float,
        food_type: str = "PRODUCT",
        ctx: Context = None,
    ) -> dict:
        client = await _resolve_client(ctx)
        if product_id <= 0:
            raise ValueError("product_id must be a positive integer")
        with SessionLocal() as db:
            result = service.add_meal_item(
                db, client, date, meal_key, product_id, measure_id, measure_quantity,
                food_type=food_type,
            )
            db.commit()
            return result

    @mcp.tool(
        name="update_meal_item",
        description=(
            "Update an existing meal item's quantity. Internally posts a new item then deletes the old one "
            "(no PUT endpoint exists). If cleanup of the old item fails, the response includes "
            "`cleanup_failed: true` and a warning."
        ),
    )
    async def mcp_update_meal_item(
        date: str,
        meal_key: str,
        plan_day_diet_item_id: str,
        new_measure_quantity: float,
        ctx: Context = None,
    ) -> dict:
        client = await _resolve_client(ctx)
        with SessionLocal() as db:
            result = service.update_meal_item(
                db, client, date, meal_key, plan_day_diet_item_id, new_measure_quantity,
            )
            db.commit()
            return result

    # -- Recipes --

    @mcp.tool(
        name="get_recipe_tags",
        description=(
            "List the recipe tags Fitatu supports (cuisines, diet types, popular categories, "
            "meal characters). Each entry is {name, category, translation}. Pass full entries "
            "back to create_recipe via the `tags` argument."
        ),
    )
    async def mcp_get_recipe_tags(ctx: Context = None) -> dict:
        client = await _resolve_client(ctx)
        tags = client.get_recipe_tags()
        return {"ok": True, "count": len(tags), "tags": tags}

    @mcp.tool(
        name="create_recipe",
        description=(
            "Create a user recipe in the Fitatu catalog. items_json is a JSON-encoded list of "
            "{type:'PRODUCT'|'RECIPE', itemId:int, measureId:int, measureQuantity:float}. "
            "meal_schema_csv is a comma-separated subset of "
            "breakfast/second_breakfast/lunch/dinner/snack/supper (empty = all). "
            "tags_json is an optional JSON array of full tag dicts from get_recipe_tags "
            "(plus optional user tags as {name, category:'RECIPE_TAG_USERS_TYPE', translation}). "
            "Server returns id + computed macros (energy/protein/fat/carbohydrate per serving)."
        ),
    )
    async def mcp_create_recipe(
        name: str,
        items_json: str,
        serving: str = "1",
        cooking_time: int = 0,
        preparation_time: str = "",
        recipe_description: str = "",
        meal_schema_csv: str = "",
        tags_json: str = "",
        shared: bool = False,
        ctx: Context = None,
    ) -> dict:
        client = await _resolve_client(ctx)
        name_clean = (name or "").strip()
        if not name_clean:
            raise ValueError("name must be non-empty")
        if len(name_clean) > 200:
            raise ValueError("name must be 200 characters or fewer")

        try:
            items = json.loads(items_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"items_json must be valid JSON: {exc}") from exc
        if not isinstance(items, list) or not items:
            raise ValueError("items_json must be a non-empty JSON array")
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                raise ValueError(f"items_json[{i}] must be an object")
            it_type = (it.get("type") or "").upper()
            if it_type not in {"PRODUCT", "RECIPE"}:
                raise ValueError(f"items_json[{i}].type must be PRODUCT or RECIPE (got {it_type!r})")
            for k in ("itemId", "measureId", "measureQuantity"):
                if k not in it:
                    raise ValueError(f"items_json[{i}] missing required key {k!r}")
            it["type"] = it_type

        valid_keys = service.MEAL_KEYS_VALID
        meal_schema = [k.strip() for k in meal_schema_csv.split(",") if k.strip()] if meal_schema_csv else list(valid_keys)
        for mk in meal_schema:
            if mk not in valid_keys:
                raise ValueError(f"meal_schema_csv contains invalid key {mk!r}; valid: {sorted(valid_keys)}")

        tags: list[dict] = []
        if tags_json:
            try:
                tags = json.loads(tags_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"tags_json must be valid JSON: {exc}") from exc
            if not isinstance(tags, list):
                raise ValueError("tags_json must be a JSON array of tag objects")
            for i, t in enumerate(tags):
                if not isinstance(t, dict) or not {"name", "category"}.issubset(t):
                    raise ValueError(f"tags_json[{i}] must include 'name' and 'category'")
                t.setdefault("translation", t["name"])

        # Some callers double-escape newlines, sending literal "\\n" / "\\r" sequences
        # instead of real control characters. Fitatu's web client renders the raw string,
        # so we normalize here before posting.
        recipe_description_clean = (
            recipe_description
            .replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\r", "\n")
            .replace("\\t", "\t")
        )
        preparation_time_clean = preparation_time.replace("\\n", "\n").replace("\\r", "\n")

        payload: dict = {
            "categories": None,
            "cookingTime": cooking_time,
            "items": items,
            "mealSchema": meal_schema,
            "name": name_clean,
            "preparationTime": preparation_time_clean,
            "recipeDescription": recipe_description_clean,
            "serving": str(serving),
            "shared": bool(shared),
            "tags": tags,
        }

        created = client.create_recipe(payload)
        return {"ok": True, "recipe": created}

    if allow_delete:
        @mcp.tool(
            name="delete_custom_product",
            description="Delete a user-owned product from the Fitatu catalog (and local cache). Requires FITATU_ALLOW_DELETE=true.",
        )
        async def mcp_delete_custom_product(product_id: int, ctx: Context = None) -> dict:
            client = await _resolve_client(ctx)
            if product_id <= 0:
                raise ValueError("product_id must be a positive integer")
            client.delete_product(product_id)
            with SessionLocal() as db:
                service.delete_product(db, product_id, user_id=client.user_id)
                db.commit()
            return {"ok": True, "deleted": True, "product_id": product_id}

        @mcp.tool(
            name="delete_meal_item",
            description=(
                "Soft-delete a logged meal item (marks deletedAt server-side). "
                "Requires FITATU_ALLOW_DELETE=true."
            ),
        )
        async def mcp_delete_meal_item(
            date: str,
            meal_key: str,
            plan_day_diet_item_id: str,
            ctx: Context = None,
        ) -> dict:
            client = await _resolve_client(ctx)
            with SessionLocal() as db:
                result = service.delete_meal_item(
                    db, client, date, meal_key, plan_day_diet_item_id,
                )
                db.commit()
                return result

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

    # Expose for testing introspection
    app.state.session_pool = pool
    app.state.mcp = mcp

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/mcp", mcp_app)
    return app, mcp


# Module-level app/mcp for uvicorn entrypoint.
app, mcp = build_app()
