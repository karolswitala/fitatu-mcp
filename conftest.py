"""Root conftest: set env stubs and register repo as `mcp_server` package alias.

Production deploy (Dockerfile) copies this repo into `/app/mcp_server/`, so
imports use the `mcp_server.X` form (e.g. `from .database import Base`).
For local pytest, we register an alias so the same `mcp_server.X` imports work
from the repo without renaming the dir or symlinking.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Set env stubs BEFORE any package import. Multi-user auth no longer reads
# FITATU_USERNAME/PASSWORD/MCP_API_KEY — clients authenticate per-request.
os.environ.setdefault("FITATU_API_SECRET", "test-secret")
os.environ.setdefault("FITATU_DB_FILE", ":memory:")

# Register the repo dir as `mcp_server` so production-style imports work.
# Parent dir must be on sys.path; the package name is taken from the dir name,
# but since "fitatu-mcp" contains a dash (illegal), we register a sys.modules entry.
parent = REPO_ROOT.parent
if str(parent) not in sys.path:
    sys.path.insert(0, str(parent))

import importlib.util
import types

if "mcp_server" not in sys.modules:
    pkg = types.ModuleType("mcp_server")
    pkg.__path__ = [str(REPO_ROOT)]
    sys.modules["mcp_server"] = pkg
