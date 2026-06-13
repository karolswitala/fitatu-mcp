# Fitatu Nutrition MCP Server

An [MCP](https://modelcontextprotocol.io) server that mirrors users' daily Fitatu nutrition (meals + macros) into a local SQLite cache and exposes it over MCP Streamable HTTP. **Multi-user** — one server instance can serve many Fitatu accounts; each request authenticates with its own credentials. Server is zero-knowledge: it never persists Fitatu passwords or tokens to disk.

- Transport: **MCP Streamable HTTP** (FastMCP, mounted under `/mcp`).
- Inbound auth: per-request, either `Authorization: Basic base64(email:password)` (static client config) or `X-Fitatu-Session: <session_id>` (after the `fitatu_login` tool).
- Outbound auth: Fitatu username/password from the client request → JWT cached in process memory per Fitatu user.
- Cache: SQLite at `${FITATU_DB_FILE:-/data/fitatu_nutrition.db}`. Cached data is scoped by Fitatu numeric user_id; users see only their own meals and custom products.

> ⚠️ **DEPLOY BEHIND HTTPS.** Both ingress paths carry credentials (Basic) or live sessions (X-Fitatu-Session). On `localhost` over plain HTTP this is fine for personal use; on any network-reachable deployment you **MUST** terminate TLS in front (caddy, traefik, nginx, Cloudflare Tunnel, …). The shipped `compose.yml` exposes plain HTTP on `:8000` — that is for `localhost` only.

## Endpoints

- `GET /health` — public, returns `{"status": "ok"}`.
- `POST /mcp/` — MCP entry point (requires `Authorization: Basic …` or `X-Fitatu-Session: …`).

## MCP tools

All date params are `YYYY-MM-DD`. Read tools accept a `start_date` and optional `end_date` (defaults to `start_date`).

### Auth tool

| Tool | Description |
|------|-------------|
| `fitatu_login(email, password)` | Authenticate against Fitatu, returning a `session_id` to use in subsequent `X-Fitatu-Session` headers. Server never persists credentials. Session expires after 30 days idle. |

### Read tools

| Tool | Description | Max range |
|------|-------------|-----------|
| `sync_day(start_date, end_date?)` | Pull from Fitatu into SQLite. Reports `cache_before/after` per day. | 31 d |
| `get_day_summary(start_date, end_date?)` | Full day: meals + items + totals. Auto-syncs on cache miss / stale today. | 7 d |
| `get_day_macros(start_date, end_date?)` | Macro totals only (energy/protein/fat/carbs/fiber/sugars/salt). | 31 d |
| `get_day_meals(start_date, end_date?)` | Meal summaries + items. | 7 d |
| `get_cache_stats(start_date, end_date?)` | Cached counts (meals/items, per-meal breakdown) and totals. | 31 d |

`sync_day` is the only read tool that always hits Fitatu. The rest read SQLite and only trigger a sync if today's row is older than `FITATU_TODAY_TTL_SECONDS` (default 300 s) or the day is missing.

### Write tools — meal items (log what you ate)

| Tool | Description |
|------|-------------|
| `add_meal_item(date, meal_key, product_id, measure_id, measure_quantity)` | Log "I ate X servings of product Y for breakfast on Z". `meal_key` ∈ `breakfast`, `second_breakfast`, `lunch`, `dinner`, `snack`, `supper`. Returns the new `plan_day_diet_item_id` and a fresh day envelope. |
| `update_meal_item(date, meal_key, plan_day_diet_item_id, new_measure_quantity)` | Change a logged item's quantity. Internally POSTs a new item then DELETEs the old (no PUT exists). On cleanup failure, response includes `cleanup_failed: true` + warning. |
| `delete_meal_item(date, meal_key, plan_day_diet_item_id, delete_all_related_meals=false)` | Remove a logged item. **Gated by `FITATU_ALLOW_DELETE=true`** (default false → tool not registered). |

### Write tools — products (manage your reusable food catalog)

| Tool | Description |
|------|-------------|
| `create_custom_product(name, energy, protein, fat, carbohydrate, ...)` | Create a user-owned product. Macros per 100g. Returns the new product id + local cache row. |
| `get_product(product_id)` | Fetch a product by id. Reads local cache first; falls through to Fitatu on miss. |
| `search_products(query, scope="custom", limit=20)` | Search by name. `scope="custom"` = local LIKE over cached/custom products. `scope="catalog"` = live Fitatu search (PRODUCT + RECIPE hits via `GET /api/search/food/user/{uid}`). `scope="all"` = custom first, then catalog, deduplicated by product id. |
| `delete_custom_product(product_id)` | Remove a user-owned product from both Fitatu and the local cache. **Gated by `FITATU_ALLOW_DELETE=true`**. |

### Write tools — recipes

| Tool | Description |
|------|-------------|
| `get_recipe_tags()` | List recipe tags Fitatu supports (cuisines, diet types, popular categories, meal characters). Pass entries verbatim into `create_recipe`. |
| `create_recipe(name, items_json, ...)` | Create a user recipe. `items_json` = list of `{type:'PRODUCT'\|'RECIPE', itemId, measureId, measureQuantity}`. Optional `serving`, `cooking_time`, `preparation_time`, `recipe_description`, `meal_schema_csv`, `tags_json`, `shared`. Server returns `{id, name, energy, protein, fat, carbohydrate}` (per serving). |

Writes hit `https://www.fitatu.com/api` (the canonical web app cluster). Reads still go to `https://pl-pl.fitatu.com`. Both can be overridden with `FITATU_BASE_URL_READ` / `FITATU_BASE_URL_WRITE`.

## Quick start (docker compose)

```bash
cp .env.example .env
# No Fitatu credentials needed here — each MCP client authenticates per-request.
docker compose up -d --build
```

This brings up the MCP server at `http://localhost:8000/mcp/`. The SQLite cache lives in the `fitatu_data` volume. Each MCP client (Claude Desktop, gateway, …) authenticates per-request — see the **Authentication** section below.

To pull the pre-built image instead of building locally, set `FITATU_MCP_IMAGE` in `.env` (e.g. `ghcr.io/pawelharacz/fitatu-mcp:latest`) and run `docker compose pull && docker compose up -d`.

## About `FITATU_API_SECRET`

The Fitatu mobile/web client signs requests with a static `api-secret` header. This value is **public** — it's baked into the JavaScript bundle served to every browser at `https://www.fitatu.com/app/`, identical for all users, and has been stable for years. It identifies the client app, not the user; the actual user auth is the JWT issued by `POST /api/login`.

Because it's a public client identifier rather than a real secret, a working default ships in `fitatu_client.py` and **you do not need to set `FITATU_API_SECRET`** unless Fitatu rotates the value. If they do, grab the new one from any authenticated XHR in the Fitatu web app DevTools (Network tab → request headers → `api-secret`) and set it in `.env`.

## Authentication

The server supports two ingress paths. Both converge on the same per-user `FitatuClient` cached in memory.

### Path A — Static `Authorization: Basic` (recommended for single-user clients)

Configure your MCP client to send Basic auth with your Fitatu email + password:

```jsonc
// Claude Desktop / similar MCP-client config snippet
{
  "transport": "streamable-http",
  "url": "http://localhost:8000/mcp/",
  "headers": {
    "Authorization": "Basic <base64(email:password)>"
  }
}
```

The server logs you into Fitatu on the first request, caches the FitatuClient by your Fitatu user_id, and re-uses it for subsequent requests with the same Basic header.

### Path B — `fitatu_login` tool + `X-Fitatu-Session` header

If your client can't pass arbitrary headers, or you'd rather not put a password in a config file, log in interactively from chat:

```
LLM:  Calling fitatu_login(email="you@example.com", password="...")
MCP:  {"ok": true, "session_id": "AbCd...43chars...", "user_id": "41130303", ...}
```

Then either:
- paste the `session_id` into your client config as `X-Fitatu-Session: <session_id>`, or
- let the LLM include the session id with every follow-up tool call (some clients can route headers based on prior tool responses).

Sessions live in process memory only. They expire after 30 days of idle time (configurable via `FITATU_SESSION_TTL_SECONDS`), or when the server restarts.

### MCP client setup

- Transport: **HTTP Streamable**
- URL: `http://localhost:8000/mcp/` (or `http://fitatu-mcp:8000/mcp/` from inside the compose network)
- Headers: `Authorization: Basic …` **or** `X-Fitatu-Session: …`

Typical flow: call `sync_day` once for the date range you care about, then chain `get_day_macros` / `get_day_summary` for downstream work.

## Local run (without Docker)

The Python files use relative imports, so the package must be importable as `mcp_server`. The simplest layout is to keep this repo as that package (clone into a directory named `mcp_server`, or `pip install -e` it).

```bash
pip install -r requirements.txt
# No Fitatu credentials in env — clients pass them per-request.
# FITATU_API_SECRET is optional — a working default is built in.
export FITATU_DB_FILE=./fitatu_nutrition.db
python -m uvicorn mcp_server.server:app --host 0.0.0.0 --port 8000
```

## Publishing the image

`.github/workflows/docker.yml` builds multi-arch (`linux/amd64`, `linux/arm64`) images and pushes to GHCR on:

- pushes to `main` (tagged `latest` and `sha-<short>`)
- tags matching `v*.*.*` (semver tags)
- manual `workflow_dispatch`

Pull with:

```bash
docker pull ghcr.io/pawelharacz/fitatu-mcp:latest
```

The image runs as UID 1000, exposes 8000, declares a `/health` HEALTHCHECK, and honors `--proxy-headers` so it's safe behind a reverse proxy that terminates TLS.

## Configuration reference

| Variable | Default | Purpose |
|----------|---------|---------|
| `FITATU_API_SECRET` | `PYRXtfs88UDJMuCCrNpLV` (built-in) | Public client identifier; override only if Fitatu rotates it. See "About `FITATU_API_SECRET`" below. |
| `FITATU_DB_FILE` | `/data/fitatu_nutrition.db` | SQLite path inside the container. |
| `FITATU_TODAY_TTL_SECONDS` | `300` | How long today's row is treated as fresh. |
| `FITATU_SESSION_TTL_SECONDS` | `2592000` (30 days) | Idle TTL for cached FitatuClients in the session pool. |
| `FITATU_SESSION_POOL_MAX` | `64` | Max concurrent logged-in Fitatu users cached in memory. |
| `FITATU_BASE_URL_READ` | `https://pl-pl.fitatu.com` | Override Fitatu cluster for reads. |
| `FITATU_BASE_URL_WRITE` | `https://www.fitatu.com` | Override Fitatu cluster for writes (no `/api` suffix; client appends it). |
| `FITATU_ALLOW_DELETE` | `false` | Set to `true` to register `delete_custom_product` and `delete_meal_item`. |
| `LOG_LEVEL` | `INFO` | Python logging level. |
| `MCP_ENABLE_DNS_REBINDING_PROTECTION` | `false` | Toggle DNS-rebinding guard. |
| `MCP_ALLOWED_HOSTS` | (compose-friendly list) | Allowlist used when rebinding protection is on. |

## Running tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest tests/
```

Tests use in-memory SQLite and mock all Fitatu HTTP. No live credentials needed.

## License

This project is released under the [PolyForm Noncommercial License 1.0.0](LICENSE). You may use, modify, and share it freely for personal, research, educational, or other noncommercial purposes. **Commercial use is not permitted** without a separate agreement.

This repository is a fork of [karolswitala/fitatu-mcp](https://github.com/karolswitala/fitatu-mcp), which is published without an explicit license. The PolyForm terms apply to modifications and additions contributed in this fork; the unmodified upstream code remains subject to the upstream author's rights.
