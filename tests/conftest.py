import os

# Must be set before any project module is imported.
os.environ.setdefault("FITATU_USERNAME", "test@example.com")
os.environ.setdefault("FITATU_PASSWORD", "testpassword")
os.environ.setdefault("FITATU_API_SECRET", "test-secret")
os.environ.setdefault("MCP_API_KEY", "test-mcp-api-key-32chars-padding!!")
os.environ.setdefault("FITATU_DB_FILE", ":memory:")
