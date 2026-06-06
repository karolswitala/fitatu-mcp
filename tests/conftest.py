import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Must be set before any project module is imported.
os.environ.setdefault("FITATU_USERNAME", "test@example.com")
os.environ.setdefault("FITATU_PASSWORD", "testpassword")
os.environ.setdefault("FITATU_API_SECRET", "test-secret")
os.environ.setdefault("MCP_API_KEY", "test-mcp-api-key-32chars-padding!!")
os.environ.setdefault("FITATU_DB_FILE", ":memory:")


@pytest.fixture
def db_session():
    from fitatu_mcp.models import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()
