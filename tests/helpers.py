from datetime import date

from fitatu_mcp.models import (
    DailyNutrition,
    MealItem,
    MealNutrition,
    MeasurementPoint,
    UserBodyProfile,
)


def insert_day(session, user_id="user1", day_date=date(2026, 6, 6)):
    day = DailyNutrition(
        user_id=user_id, day_date=day_date,
        total_energy=0.0, total_protein=0.0, total_fat=0.0,
        total_carbohydrate=0.0, total_fiber=0.0, total_sugars=0.0, total_salt=0.0,
    )
    session.add(day)
    session.flush()
    return day


def insert_meal(session, daily_id, meal_key="breakfast"):
    meal = MealNutrition(
        daily_id=daily_id, meal_key=meal_key, meal_name=meal_key.title(),
        total_energy=0.0, total_protein=0.0, total_fat=0.0,
        total_carbohydrate=0.0, total_fiber=0.0, total_sugars=0.0, total_salt=0.0,
        item_count=0,
    )
    session.add(meal)
    session.flush()
    return meal


def insert_item(session, meal_id, name="Apple", plan_id=None, product_id=1,
                measure_quantity=100.0, weight=100.0, energy=52.0):
    item = MealItem(
        meal_id=meal_id,
        plan_day_diet_item_id=plan_id,
        product_id=product_id,
        name=name,
        energy=energy, protein=0.0, fat=0.0, carbohydrate=0.0,
        fiber=0.0, sugars=0.0, salt=0.0,
        measure_quantity=measure_quantity, weight=weight,
    )
    session.add(item)
    session.flush()
    return item


def insert_measurement_point(session, user_id="user1", metric="weight",
                             measured_date=date(2026, 3, 15), value=70.0, unit="KG",
                             updated_at=None):
    point = MeasurementPoint(
        user_id=user_id, metric=metric, measured_date=measured_date,
        value=value, unit=unit,
    )
    if updated_at is not None:
        point.updated_at = updated_at
    session.add(point)
    session.flush()
    return point


def insert_body_profile(session, user_id="user1", height_cm=175.0,
                        weight_unit="KG", size_unit="CM"):
    profile = UserBodyProfile(
        user_id=user_id, height_cm=height_cm,
        weight_unit=weight_unit, size_unit=size_unit,
    )
    session.add(profile)
    session.flush()
    return profile
