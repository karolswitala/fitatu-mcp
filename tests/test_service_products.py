"""Group 3 tests: Product table + service helpers."""

from __future__ import annotations

import time

import pytest


def test_product_table_created_by_create_all():
    """Base.metadata.create_all picks up new Product table without migration tooling."""
    from mcp_server.models import Base

    assert "products" in Base.metadata.tables


def test_upsert_product_insert_then_update_idempotent(db_session):
    from mcp_server.models import Product
    from mcp_server.service import upsert_product

    payload = {
        "id": 12345,
        "name": "Greek Yogurt 200g",
        "energy": 120,
        "protein": 10,
        "fat": 5,
        "carbohydrate": 8,
        "brand": "Olympus",
    }

    p1 = upsert_product(db_session, payload, source="custom", user_id="42")
    db_session.flush()
    first_created = p1.created_at

    time.sleep(0.01)
    payload["name"] = "Greek Yogurt PLAIN 200g"
    payload["energy"] = 125
    p2 = upsert_product(db_session, payload, source="custom", user_id="42")
    db_session.flush()

    rows = db_session.query(Product).all()
    assert len(rows) == 1
    assert rows[0].name == "Greek Yogurt PLAIN 200g"
    assert rows[0].energy == 125
    assert rows[0].user_id == "42"
    assert rows[0].created_at == first_created
    assert rows[0].updated_at >= first_created


def test_search_products_local_case_insensitive_substring(db_session):
    from mcp_server.service import upsert_product, search_products_local

    upsert_product(db_session, {"id": 1, "name": "Homemade Hummus", "energy": 200, "protein": 8, "fat": 12, "carbohydrate": 15}, source="custom", user_id="42")
    upsert_product(db_session, {"id": 2, "name": "Greek Yogurt", "energy": 60, "protein": 10, "fat": 0, "carbohydrate": 4}, source="custom", user_id="42")
    db_session.flush()

    results = search_products_local(db_session, "HUM", "all", 10, user_id="42")
    assert len(results) == 1
    assert results[0].name == "Homemade Hummus"


def test_search_products_local_respects_scope_custom(db_session):
    from mcp_server.service import upsert_product, search_products_local

    upsert_product(db_session, {"id": 1, "name": "Custom Yogurt", "energy": 60, "protein": 10, "fat": 0, "carbohydrate": 4}, source="custom", user_id="42")
    upsert_product(db_session, {"id": 2, "name": "Catalog Yogurt", "energy": 60, "protein": 10, "fat": 0, "carbohydrate": 4}, source="catalog")
    db_session.flush()

    results = search_products_local(db_session, "Yogurt", "custom", 10, user_id="42")
    assert len(results) == 1
    assert results[0].source == "custom"


def test_search_products_local_respects_limit(db_session):
    from mcp_server.service import upsert_product, search_products_local

    for i in range(5):
        upsert_product(db_session, {"id": 100 + i, "name": f"Apple {i}", "energy": 50, "protein": 0, "fat": 0, "carbohydrate": 12}, source="custom", user_id="42")
    db_session.flush()

    results = search_products_local(db_session, "Apple", "all", 2, user_id="42")
    assert len(results) == 2


def test_delete_product_removes_row(db_session):
    from mcp_server.models import Product
    from mcp_server.service import upsert_product, delete_product

    upsert_product(db_session, {"id": 999, "name": "Disposable", "energy": 0, "protein": 0, "fat": 0, "carbohydrate": 0}, source="custom", user_id="42")
    db_session.flush()

    ok = delete_product(db_session, 999, user_id="42")
    assert ok is True
    db_session.flush()
    assert db_session.query(Product).filter_by(id=999).one_or_none() is None

    assert delete_product(db_session, 999, user_id="42") is False


# -- Multi-user isolation --


def test_custom_product_scoped_to_user(db_session):
    """upsert_product stamps user_id on custom rows."""
    from mcp_server.models import Product
    from mcp_server.service import upsert_product

    upsert_product(db_session, {"id": 1, "name": "Alice's Hummus", "energy": 200, "protein": 8, "fat": 12, "carbohydrate": 15}, source="custom", user_id="alice")
    db_session.flush()
    row = db_session.query(Product).filter_by(id=1).one()
    assert row.user_id == "alice"
    assert row.source == "custom"


def test_user_a_does_not_see_user_b_custom_products(db_session):
    """search_products_local filters custom rows by user_id."""
    from mcp_server.service import upsert_product, search_products_local

    upsert_product(db_session, {"id": 1, "name": "Alice's Yogurt", "energy": 60, "protein": 10, "fat": 0, "carbohydrate": 4}, source="custom", user_id="alice")
    upsert_product(db_session, {"id": 2, "name": "Bob's Yogurt", "energy": 70, "protein": 11, "fat": 0, "carbohydrate": 5}, source="custom", user_id="bob")
    db_session.flush()

    alice_results = search_products_local(db_session, "Yogurt", "custom", 10, user_id="alice")
    assert len(alice_results) == 1
    assert alice_results[0].name == "Alice's Yogurt"

    bob_results = search_products_local(db_session, "Yogurt", "custom", 10, user_id="bob")
    assert len(bob_results) == 1
    assert bob_results[0].name == "Bob's Yogurt"


def test_catalog_row_user_id_null_visible_to_all(db_session):
    """Catalog rows have user_id NULL; visible in scope='catalog' or scope='all' for any user."""
    from mcp_server.service import upsert_product, search_products_local

    upsert_product(db_session, {"id": 3, "name": "Catalog Apple", "energy": 50, "protein": 0, "fat": 0, "carbohydrate": 12}, source="catalog")
    db_session.flush()

    alice_cat = search_products_local(db_session, "Apple", "catalog", 10, user_id="alice")
    assert len(alice_cat) == 1
    bob_cat = search_products_local(db_session, "Apple", "catalog", 10, user_id="bob")
    assert len(bob_cat) == 1


def test_delete_product_refuses_other_users_row(db_session):
    """delete_product raises ValueError when row owned by another user_id."""
    from mcp_server.service import upsert_product, delete_product

    upsert_product(db_session, {"id": 10, "name": "Alice's Cake", "energy": 300, "protein": 5, "fat": 10, "carbohydrate": 50}, source="custom", user_id="alice")
    db_session.flush()

    with pytest.raises(ValueError, match="different user"):
        delete_product(db_session, 10, user_id="bob")
