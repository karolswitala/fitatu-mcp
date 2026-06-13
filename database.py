import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from .models import Base

DB_FILE = os.getenv("FITATU_DB_FILE", "fitatu_nutrition.db")
DB_URL = f"sqlite:///{DB_FILE}"

engine = create_engine(DB_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _apply_additive_migrations()


def _apply_additive_migrations() -> None:
    insp = inspect(engine)
    if "products" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("products")}
    with engine.begin() as conn:
        if "barcode" not in cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN barcode VARCHAR(64)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_products_barcode ON products(barcode)"))
        if "user_id" not in cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN user_id VARCHAR(64)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_products_user_id ON products(user_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_products_user_source ON products(user_id, source)"))
