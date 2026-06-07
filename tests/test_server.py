import pytest

from fitatu_mcp.server import _cache_counts
from tests.helpers import insert_day, insert_item, insert_meal


class TestCacheCounts:
    def test_missing_day_returns_zeros(self, db_session):
        assert _cache_counts(db_session, "user1", "2026-06-06") == (0, 0)

    def test_day_with_no_meals_returns_zeros(self, db_session):
        insert_day(db_session)
        assert _cache_counts(db_session, "user1", "2026-06-06") == (0, 0)

    def test_counts_meals_and_items(self, db_session):
        day = insert_day(db_session)
        m1 = insert_meal(db_session, day.id, "breakfast")
        m2 = insert_meal(db_session, day.id, "lunch")
        insert_item(db_session, m1.id, "Oats")
        insert_item(db_session, m2.id, "Rice")
        insert_item(db_session, m2.id, "Chicken")

        meals, items = _cache_counts(db_session, "user1", "2026-06-06")
        assert meals == 2
        assert items == 3

    def test_does_not_count_other_users_data(self, db_session):
        day = insert_day(db_session, user_id="other_user")
        meal = insert_meal(db_session, day.id)
        insert_item(db_session, meal.id)

        assert _cache_counts(db_session, "user1", "2026-06-06") == (0, 0)
