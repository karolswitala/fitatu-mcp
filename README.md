# Fitatu Nutrition MCP Server (FastAPI)

This server exposes daily nutrition data (meals and macros) through MCP HTTP Streamable transport.
SQLite is used as a cache layer.
Sync is additive: only new meal items are inserted; existing cached items are preserved.

## Endpoints

- `GET /health`
- MCP Streamable HTTP endpoint: `/mcp`

## MCP tools (HTTP Streamable)

All tools accept `start_date` (required, YYYY-MM-DD) and `end_date` (optional, defaults to `start_date`).
Tools other than `sync_day` auto-sync from Fitatu if the requested day is not cached or is stale.

| Tool | Max range | Description |
|------|-----------|-------------|
| `sync_day` | 31 days | Explicitly sync days from Fitatu into SQLite |
| `get_day_summary` | 7 days | Full nutrition summary including all meals and items |
| `get_day_macros` | 31 days | Macro totals only (energy, protein, fat, carbs, fiber, sugars, salt) |
| `get_day_meals` | 7 days | Meal summaries and items without day-level totals |
| `get_cache_stats` | 31 days | Cached meal/item counts and macro totals |

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

Use MCP tool `sync_day` first, then read data with the remaining tools.

## n8n MCP integration

Configure MCP client in n8n to use HTTP Streamable transport with URL:

- `http://<host>:8000/mcp/`

Use MCP tools listed above directly in n8n flows.
