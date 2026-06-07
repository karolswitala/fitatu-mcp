# Fitatu MCP Server — Architecture

## Overview

A FastAPI application that wraps the Fitatu nutrition API and exposes it as an MCP (Model Context Protocol) server. SQLite acts as a local cache so that repeated reads don't hit Fitatu's API every time.

```
n8n (MCP client)
     │  HTTP Streamable  Bearer token
     ▼
FastAPI app  (server.py)
     │
     ├── /health         — liveness probe
     └── /mcp            — MCP endpoint (mounted sub-app)
              │
              └── MCP tools (4 tools, all defined in server.py)
                       │
                       ├── FitatuClient  (fitatu_client.py)   — talks to Fitatu API
                       ├── service.py                         — sync + persist logic
                       └── SQLite via SQLAlchemy              — cache
```

---

## Files

| File | Responsibility |
|------|---------------|
| `fitatu_mcp/server.py` | App wiring, auth middleware, all 4 MCP tool definitions, date validation helpers |
| `fitatu_mcp/service.py` | Business logic: aggregate API response → schema, persist to DB, sync orchestration |
| `fitatu_mcp/fitatu_client.py` | HTTP client for Fitatu API (login, token refresh, fetch day data) |
| `fitatu_mcp/models.py` | SQLAlchemy ORM models (3 tables) |
| `fitatu_mcp/schemas.py` | Pydantic models used as in-memory data transfer objects |
| `fitatu_mcp/database.py` | SQLAlchemy engine + session factory, `init_db()` |
| `tests/helpers.py` | Shared DB insert helpers for tests |
| `tests/test_staleness.py` | Unit tests for cache staleness logic |
| `tests/test_service.py` | Unit tests for service layer functions |
| `tests/test_server.py` | Integration tests for server-level helpers (cache counts) |
| `tests/test_models.py` | DB constraint tests (unique indexes) |

---

## Data Model

Three tables, parent → child hierarchy:

```
daily_nutrition          (one row per user+day)
  │  id, user_id, day_date
  │  total_energy/protein/fat/carbohydrate/fiber/sugars/salt
  │  created_at, updated_at
  │
  └── meal_nutrition     (one row per meal slot per day, e.g. "breakfast")
        │  id, daily_id, meal_key, meal_name, meal_time
        │  total_energy/protein/...  item_count
        │
        └── meal_item    (one row per food item in a meal)
               id, meal_id, plan_day_diet_item_id, product_id
               name, brand, measure_name, measure_quantity, weight
               energy/protein/fat/carbohydrate/fiber/sugars/salt
               eaten
```

**Unique indexes:**

| Table | Index | Columns | Notes |
|-------|-------|---------|-------|
| `daily_nutrition` | `uq_daily_nutrition_user_day` | `(user_id, day_date)` | UniqueConstraint |
| `meal_nutrition` | `uq_meal_nutrition_daily_meal` | `(daily_id, meal_key)` | Unique Index |
| `meal_item` | `uq_meal_item_plan_id` | `(meal_id, plan_day_diet_item_id)` | UniqueConstraint; not enforced when `plan_day_diet_item_id` is NULL (SQLite behaviour) |
| `meal_item` | `uq_meal_item_fallback` | `(meal_id, name, product_id, measure_quantity, weight, energy)` | Partial unique index; `WHERE plan_day_diet_item_id IS NULL` only |

Both `meal_nutrition` and `meal_item` indexes use `Index(unique=True)` rather than `UniqueConstraint` so SQLAlchemy emits `CREATE UNIQUE INDEX IF NOT EXISTS`, which applies to existing tables without a manual migration.

**Totals are denormalised:** `meal_nutrition.total_*` = sum of its items; `daily_nutrition.total_*` = sum of its meals. Recalculated on every sync.

---

## Pydantic Schemas (in-memory DTOs)

Mirror the DB structure but used only during sync, never persisted directly:

```
DaySummarySchema
  totals: MacroTotals
  meals: list[MealSummarySchema]
               totals: MacroTotals
               items:  list[MealItemSchema]
```

`MacroTotals` is a flat bag of 7 floats (energy, protein, fat, carbohydrate, fiber, sugars, salt).

---

## Request / Data Flow

### Tool handler pattern

All tool handlers are `async def` and use `asyncio.to_thread` to avoid blocking the event loop. Date validation runs on the event loop (pure CPU). Everything from the DB session onwards runs in a thread pool:

```python
async def mcp_get_day_macros(start_date, end_date=""):
    start, end = _validate_date_range(...)   # on event loop

    def _run():
        with SessionLocal() as db:           # in thread
            ...                              # DB + HTTP work
        return _range_envelope(...)

    return await asyncio.to_thread(_run)
```

Each `_run()` creates its own `SessionLocal` context so sessions are fully contained within their thread.

### Happy path — cached day

```
n8n calls get_day_macros("2026-06-04")
  → _validate_date_range          (event loop)
  → asyncio.to_thread(_run)       (thread pool)
      → SessionLocal (open DB session)
      → _ensure_user_id  (login if no active session)
      → _load_or_sync_day
          → _load_day (joinedload query)
          → _is_stale? No → return cached row
      → build MacroTotals from row fields
      → return dict
```

### Cache miss or stale — triggers sync

```
_load_or_sync_day
  → _load_day returns None (or stale)
  → sync_day_from_fitatu(db, client, day_date)
      → client.get_day(day_date)         ← HTTP GET to Fitatu (blocking, in thread)
      → aggregate_day_summary()          ← raw dict → DaySummarySchema
      → persist_day_summary(db, summary) ← upsert into SQLite; returns day_row
  → return day_row
```

### Sync / persist detail (persist_day_summary)

1. Load existing `DailyNutrition` row (with meals+items eager-loaded)
2. Delete meals no longer in API response
3. For each meal in response:
   - Insert `MealNutrition` if new; update name/time fields if existing
   - Delete items no longer in meal; upsert items by `_item_key`
4. Flush, recalculate all meal totals via `_recalculate_meal_totals`
5. Roll up meal totals into day totals
6. Commit, return `day_row`

`_item_key` deduplicates items by `plan_day_diet_item_id` when present, or by a fallback tuple of `(name, product_id, measure_quantity, weight, energy)` when not. Works via duck typing on both `MealItem` (ORM) and `MealItemSchema` (Pydantic).

---

## Cache Staleness Logic (`_is_stale`)

| Day | Behaviour |
|-----|-----------|
| Today | Re-sync if `updated_at` is older than `TODAY_TTL_SECONDS` (default 5 min) |
| Yesterday | Re-sync once on the following day — stale if `updated_at` date (UTC) is before today |
| Older days | Never re-synced once cached |

The yesterday rule ensures that edits made late in the day (adding/removing meals) are picked up the next morning on first access, without continuous polling.

Configured via env var `FITATU_TODAY_TTL_SECONDS`.

---

## MCP Tools

| Tool | Max range | Auto-syncs? | Returns |
|------|-----------|-------------|---------|
| `sync_day` | 31 days | Always (explicit sync) | cache before/after delta |
| `get_day_summary` | 7 days | On miss/stale | full meals + items + day totals |
| `get_day_macros` | 31 days | On miss/stale | macro totals only |
| `get_cache_stats` | 31 days | Never (read-only) | cache counts + totals, or `cached: false` |

All responses are wrapped in `_range_envelope`:
```json
{ "start_date": "...", "end_date": "...", "day_count": N, "days": [...] }
```

`get_cache_stats` uses `_load_day` directly (no auto-sync). Uncached days return `{"day_date": "...", "cached": false}`. Safe to call as a diagnostic without side effects.

---

## Authentication

**Fitatu API** — username/password login on first use; JWT stored in memory; auto-refreshes on 401 (tries `refresh_token` first, falls back to re-login). Credentials from env vars `FITATU_USERNAME` / `FITATU_PASSWORD` / `FITATU_API_SECRET`.

**MCP endpoint** — Bearer token middleware in FastAPI checks `Authorization: Bearer <MCP_API_KEY>` on all `/mcp*` paths. Returns 401 otherwise.

---

## Configuration (env vars)

| Var | Default | Purpose |
|-----|---------|---------|
| `FITATU_USERNAME` | — | Fitatu account email |
| `FITATU_PASSWORD` | — | Fitatu account password |
| `FITATU_API_SECRET` | — | API secret header for Fitatu HTTP calls |
| `MCP_API_KEY` | — | Bearer token protecting the MCP endpoint |
| `FITATU_DB_FILE` | `fitatu_nutrition.db` | SQLite file path |
| `FITATU_TODAY_TTL_SECONDS` | `300` | How old today's cache can be before re-sync |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `MCP_ENABLE_DNS_REBINDING_PROTECTION` | `false` | Transport security setting |
| `MCP_ALLOWED_HOSTS` | `localhost,...` | Allowed hosts for transport security |
