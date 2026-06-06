from datetime import date

import pytest

from fitatu_mcp.models import DailyNutrition, MealItem, MealNutrition
from fitatu_mcp.server import _cache_counts


def _insert_day(session, user_id="user1", day_date=date(2026, 6, 6)):
    day = DailyNutrition(
        user_id=user_id, day_date=day_date,
        total_energy=0.0, total_protein=0.0, total_fat=0.0,
        total_carbohydrate=0.0, total_fiber=0.0, total_sugars=0.0, total_salt=0.0,
    )
    session.add(day)
    session.flush()
    return day


def _insert_meal(session, daily_id, meal_key="breakfast"):
    meal = MealNutrition(
        daily_id=daily_id, meal_key=meal_key, meal_name=meal_key.title(),
        total_energy=0.0, total_protein=0.0, total_fat=0.0,
        total_carbohydrate=0.0, total_fiber=0.0, total_sugars=0.0, total_salt=0.0,
        item_count=0,
    )
    session.add(meal)
    session.flush()
    return meal


def _insert_item(session, meal_id, name="Apple"):
    item = MealItem(
        meal_id=meal_id, name=name,
        energy=0.0, protein=0.0, fat=0.0, carbohydrate=0.0,
        fiber=0.0, sugars=0.0, salt=0.0, measure_quantity=0.0, weight=0.0,
    )
    session.add(item)
    session.flush()
    return item


class TestCacheCounts:
    def test_missing_day_returns_zeros(self, db_session):
        assert _cache_counts(db_session, "user1", "2026-06-06") == (0, 0)

    def test_day_with_no_meals_returns_zeros(self, db_session):
        _insert_day(db_session)
        assert _cache_counts(db_session, "user1", "2026-06-06") == (0, 0)

    def test_counts_meals_and_items(self, db_session):
        day = _insert_day(db_session)
        m1 = _insert_meal(db_session, day.id, "breakfast")
        m2 = _insert_meal(db_session, day.id, "lunch")
        _insert_item(db_session, m1.id, "Oats")
        _insert_item(db_session, m2.id, "Rice")
        _insert_item(db_session, m2.id, "Chicken")

        meals, items = _cache_counts(db_session, "user1", "2026-06-06")
        assert meals == 2
        assert items == 3

    def test_does_not_count_other_users_data(self, db_session):
        day = _insert_day(db_session, user_id="other_user")
        meal = _insert_meal(db_session, day.id)
        _insert_item(db_session, meal.id)

        assert _cache_counts(db_session, "user1", "2026-06-06") == (0, 0)
