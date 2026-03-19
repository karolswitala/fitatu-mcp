# Fitatu Nutrition MCP Server (FastAPI)

This server exposes daily nutrition data (meals and macros) through MCP HTTP Streamable transport.
SQLite is used as a cache layer.
Sync is additive: only new meal items are inserted; existing cached items are preserved.

## Endpoints

- `GET /health`
- MCP Streamable HTTP endpoint: `/mcp`

## MCP tools (HTTP Streamable)

- `sync_day(day_date)`
- `get_day_summary(day_date)`
- `get_day_macros(day_date)`
- `get_day_meals(day_date)`
- `get_cache_stats(day_date)`

`sync_day` also returns:
- `cache_delta`: newly added meals/items in this sync run
- `cache_totals`: total cached meals/items for that day

Parameter format: `day_date = "YYYY-MM-DD"`

## Local run

Set credentials:

- `FITATU_USERNAME`
- `FITATU_PASSWORD`
- `FITATU_API_SECRET`

Then run:

```powershell
pip install -r mcp_server/requirements.txt
$env:FITATU_USERNAME="your_email"
$env:FITATU_PASSWORD="your_password"
$env:FITATU_API_SECRET="your_api_secret"
python -m uvicorn mcp_server.server:app --host 0.0.0.0 --port 8000
```

## Docker

Build image:

```powershell
docker build -t fitatu-mcp-server ./mcp_server
```

Run container (username/password passed at runtime):

```powershell
docker run --rm -p 8000:8000 ^
  -e FITATU_USERNAME="your_email" ^
  -e FITATU_PASSWORD="your_password" ^
  -e FITATU_API_SECRET="your_api_secret" ^
  -e FITATU_DB_FILE="/data/fitatu_nutrition.db" ^
  -v ${PWD}/data:/data ^
  fitatu-mcp-server
```

Use MCP tool `sync_day` first, then read data with the remaining tools.

## n8n MCP integration

Configure MCP client in n8n to use HTTP Streamable transport with URL:

- `http://<host>:8000/mcp/`

Use MCP tools listed above directly in n8n flows.
