# Fitatu Nutrition MCP Server (FastAPI)

This server exposes daily nutrition data (meals and macros) through MCP HTTP Streamable transport.
SQLite is used as a cache layer.
Sync is additive: only new meal items are inserted; existing cached items are preserved.

## Endpoints

- `GET /health`
- MCP Streamable HTTP endpoint: `/mcp`

## MCP tools (HTTP Streamable)

All tools accept `start_date` (required, YYYY-MM-DD), `end_date` (optional, defaults to `start_date`)
and `force_refresh` (optional, default `false`). Days are read from the SQLite cache and re-fetched
from Fitatu automatically when missing or stale; `force_refresh=true` bypasses the cache and re-fetches now.
There is no separate sync tool — the cache is an implementation detail.

| Tool | Max range | Description |
|------|-----------|-------------|
| `get_day_summary` | 7 days | Full nutrition summary including all meals and items |
| `get_day_macros` | 31 days | Macro totals only (energy, protein, fat, carbs, fiber, sugars, salt) |

## MCP tools — body measurements (read-only)

Weight, body sizes, body-fat percentage and derived BMI are cached as per-metric points
(one row per `user_id`/`metric`/`date`). The cache is an implementation detail: tools
refresh from Fitatu automatically when it is empty or older than `MEASUREMENTS_TTL_SECONDS`
(default 3600), and every tool takes `force_refresh=False` to bypass the TTL and re-fetch now.
BMI is folded into the dashboard and single-date outputs. Metric names accept both the
friendly tool name and the raw Fitatu API key, e.g. `abdomen` or `stomach`, `body_fat` or
`fatPercentage`. Allowed metrics: `weight`, `neck`, `chest`, `waist`, `abdomen`, `hips`,
`thigh`, `calf`, `biceps`, `body_fat`.

| Tool | Signature | Description |
|------|-----------|-------------|
| `get_body_measurements` | `get_body_measurements(force_refresh=False)` | Dashboard: per-metric first/latest value, total change, latest date, plus current BMI |
| `get_measurement_history` | `get_measurement_history(metric, from_date="", to_date="", force_refresh=False)` | One metric's time series (newest-first), optionally date-bounded; omit dates for full history |
| `get_measurements_on_date` | `get_measurements_on_date(date, force_refresh=False)` | All metrics recorded on one date, with derived BMI and units |

These tools are strictly read-only; they never issue a `PUT`/`POST`/`DELETE` to Fitatu.

## Local run

Set credentials:

- `FITATU_USERNAME`
- `FITATU_PASSWORD`
- `FITATU_API_SECRET` — can be obtained by inspecting network requests in the Fitatu web app (e.g. via browser DevTools); look for the `api-secret` (or similar) header in authenticated API calls

Then run:

**PowerShell:**
```powershell
pip install -r requirements.txt
$env:FITATU_USERNAME="your_email"
$env:FITATU_PASSWORD="your_password"
$env:FITATU_API_SECRET="your_api_secret"
python -m uvicorn fitatu_mcp.server:app --host 0.0.0.0 --port 8000
```

**bash/zsh:**
```bash
pip install -r requirements.txt
export FITATU_USERNAME="your_email"
export FITATU_PASSWORD="your_password"
export FITATU_API_SECRET="your_api_secret"
python -m uvicorn fitatu_mcp.server:app --host 0.0.0.0 --port 8000
```

## Docker

Build image:

```bash
docker build -t fitatu-mcp-server .
```

Run container (username/password passed at runtime):

```bash
docker run --rm -p 8000:8000 \
  -e FITATU_USERNAME="your_email" \
  -e FITATU_PASSWORD="your_password" \
  -e FITATU_API_SECRET="your_api_secret" \
  -e FITATU_DB_FILE="/data/fitatu_nutrition.db" \
  -v "${PWD}/data:/data" \
  fitatu-mcp-server
```

## n8n MCP integration

Configure MCP client in n8n to use HTTP Streamable transport with URL:

- `http://<host>:8000/mcp/`

Use MCP tools listed above directly in n8n flows.
