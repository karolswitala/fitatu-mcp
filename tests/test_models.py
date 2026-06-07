from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from tests.helpers import insert_day, insert_item, insert_meal


class TestMealNutritionUniqueConstraint:
    def test_duplicate_meal_key_same_day_raises(self, db_session):
        day = insert_day(db_session)
        insert_meal(db_session, day.id, "breakfast")
        with pytest.raises(IntegrityError):
            insert_meal(db_session, day.id, "breakfast")

    def test_same_meal_key_different_day_is_allowed(self, db_session):
        day1 = insert_day(db_session, day_date=date(2026, 6, 6))
        day2 = insert_day(db_session, day_date=date(2026, 6, 7))
        insert_meal(db_session, day1.id, "breakfast")
        insert_meal(db_session, day2.id, "breakfast")

    def test_different_meal_keys_same_day_is_allowed(self, db_session):
        day = insert_day(db_session)
        insert_meal(db_session, day.id, "breakfast")
        insert_meal(db_session, day.id, "lunch")


class TestMealItemPlanIdConstraint:
    def test_duplicate_plan_id_same_meal_raises(self, db_session):
        day = insert_day(db_session)
        meal = insert_meal(db_session, day.id)
        insert_item(db_session, meal.id, plan_id="p1")
        with pytest.raises(IntegrityError):
            insert_item(db_session, meal.id, plan_id="p1")

    def test_same_plan_id_different_meal_is_allowed(self, db_session):
        day = insert_day(db_session)
        meal1 = insert_meal(db_session, day.id, "breakfast")
        meal2 = insert_meal(db_session, day.id, "lunch")
        insert_item(db_session, meal1.id, plan_id="p1")
        insert_item(db_session, meal2.id, plan_id="p1")


class TestMealItemFallbackIndex:
    def test_duplicate_fallback_fields_with_null_plan_id_raises(self, db_session):
        day = insert_day(db_session)
        meal = insert_meal(db_session, day.id)
        insert_item(db_session, meal.id, name="Apple", plan_id=None)
        with pytest.raises(IntegrityError):
            insert_item(db_session, meal.id, name="Apple", plan_id=None)

    def test_different_names_with_null_plan_id_is_allowed(self, db_session):
        day = insert_day(db_session)
        meal = insert_meal(db_session, day.id)
        insert_item(db_session, meal.id, name="Apple", plan_id=None)
        insert_item(db_session, meal.id, name="Banana", plan_id=None)

    def test_fallback_index_does_not_apply_when_plan_id_is_set(self, db_session):
        # Items with a plan_id are deduplicated by the plan_id constraint, not fallback fields
        day = insert_day(db_session)
        meal = insert_meal(db_session, day.id)
        insert_item(db_session, meal.id, name="Apple", plan_id="p1")
        insert_item(db_session, meal.id, name="Apple", plan_id="p2")
