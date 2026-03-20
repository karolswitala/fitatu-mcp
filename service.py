from datetime import datetime
import logging

from .fitatu_client import FitatuClient
from .models import DailyNutrition, MealItem, MealNutrition
from .schemas import DaySummarySchema, MacroTotals, MealItemSchema, MealSummarySchema


logger = logging.getLogger(__name__)


def safe_float(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def aggregate_day_summary(user_id: str, day_date: str, diet_plan: dict) -> DaySummarySchema:
    logger.info("Aggregating day summary user_id=%s day_date=%s meals=%s", user_id, day_date, len(diet_plan or {}))
    meals: list[MealSummarySchema] = []
    day_totals = MacroTotals()

    for meal_key, meal_data in diet_plan.items():
        items_raw = meal_data.get("items", [])
        meal_items: list[MealItemSchema] = []
        meal_totals = MacroTotals()

        for item in items_raw:
            meal_item = MealItemSchema(
            id=None,
            plan_day_diet_item_id=item.get("planDayDietItemId"),
            product_id=item.get("productId"),
                name=item.get("name", "Unknown"),
                brand=item.get("brand"),
                measure_name=item.get("measureName"),
                measure_quantity=safe_float(item.get("measureQuantity")),
                weight=safe_float(item.get("weight")),
                energy=safe_float(item.get("energy")),
                protein=safe_float(item.get("protein")),
                fat=safe_float(item.get("fat")),
                carbohydrate=safe_float(item.get("carbohydrate")),
                fiber=safe_float(item.get("fiber")),
                sugars=safe_float(item.get("sugars")),
                salt=safe_float(item.get("salt")),
                eaten=bool(item.get("eaten", False)),
            )
            meal_items.append(meal_item)

            meal_totals.energy += meal_item.energy
            meal_totals.protein += meal_item.protein
            meal_totals.fat += meal_item.fat
            meal_totals.carbohydrate += meal_item.carbohydrate
            meal_totals.fiber += meal_item.fiber
            meal_totals.sugars += meal_item.sugars
            meal_totals.salt += meal_item.salt

        meals.append(
            MealSummarySchema(
                meal_key=meal_key,
                meal_name=meal_data.get("mealName") or meal_key,
                meal_time=meal_data.get("mealTime"),
                recommended_percent=meal_data.get("recommendedPercent"),
                item_count=len(meal_items),
                totals=meal_totals,
                items=meal_items,
            )
        )

        day_totals.energy += meal_totals.energy
        day_totals.protein += meal_totals.protein
        day_totals.fat += meal_totals.fat
        day_totals.carbohydrate += meal_totals.carbohydrate
        day_totals.fiber += meal_totals.fiber
        day_totals.sugars += meal_totals.sugars
        day_totals.salt += meal_totals.salt

    return DaySummarySchema(user_id=user_id, day_date=day_date, totals=day_totals, meals=meals)


def persist_day_summary(db, summary: DaySummarySchema) -> None:
    logger.info("Persisting day summary user_id=%s day_date=%s", summary.user_id, summary.day_date)
    summary_date = datetime.strptime(summary.day_date, "%Y-%m-%d").date()
    day_row = (
        db.query(DailyNutrition)
        .filter(DailyNutrition.user_id == summary.user_id, DailyNutrition.day_date == summary_date)
        .one_or_none()
    )

    if not day_row:
        logger.info("No existing day row found; creating new row")
        day_row = DailyNutrition(
            user_id=summary.user_id,
            day_date=summary_date,
            total_energy=0.0,
            total_protein=0.0,
            total_fat=0.0,
            total_carbohydrate=0.0,
            total_fiber=0.0,
            total_sugars=0.0,
            total_salt=0.0,
        )
        db.add(day_row)
        db.flush()

    existing_meals = {meal.meal_key: meal for meal in day_row.meals}

    for meal in summary.meals:
        meal_row = existing_meals.get(meal.meal_key)
        if not meal_row:
            logger.info("Creating new meal row meal_key=%s", meal.meal_key)
            meal_row = MealNutrition(
                daily_id=day_row.id,
                meal_key=meal.meal_key,
                meal_name=meal.meal_name,
                meal_time=meal.meal_time,
                recommended_percent=meal.recommended_percent,
                total_energy=0.0,
                total_protein=0.0,
                total_fat=0.0,
                total_carbohydrate=0.0,
                total_fiber=0.0,
                total_sugars=0.0,
                total_salt=0.0,
                item_count=0,
            )
            db.add(meal_row)
            db.flush()
            existing_meals[meal.meal_key] = meal_row

        meal_row.meal_name = meal.meal_name
        meal_row.meal_time = meal.meal_time
        meal_row.recommended_percent = meal.recommended_percent

        existing_item_keys = {_item_key_from_db(existing_item) for existing_item in meal_row.items}

        for item in meal.items:
            item_key = _item_key_from_schema(item)
            if item_key in existing_item_keys:
                continue

            db_item = MealItem(
                meal_id=meal_row.id,
                plan_day_diet_item_id=item.plan_day_diet_item_id,
                product_id=item.product_id,
                name=item.name,
                brand=item.brand,
                measure_name=item.measure_name,
                measure_quantity=item.measure_quantity,
                weight=item.weight,
                energy=item.energy,
                protein=item.protein,
                fat=item.fat,
                carbohydrate=item.carbohydrate,
                fiber=item.fiber,
                sugars=item.sugars,
                salt=item.salt,
                eaten=item.eaten,
            )
            db.add(db_item)
            existing_item_keys.add(item_key)

    db.flush()

    for meal_row in day_row.meals:
        _recalculate_meal_totals(meal_row)

    day_row.total_energy = sum(meal.total_energy for meal in day_row.meals)
    day_row.total_protein = sum(meal.total_protein for meal in day_row.meals)
    day_row.total_fat = sum(meal.total_fat for meal in day_row.meals)
    day_row.total_carbohydrate = sum(meal.total_carbohydrate for meal in day_row.meals)
    day_row.total_fiber = sum(meal.total_fiber for meal in day_row.meals)
    day_row.total_sugars = sum(meal.total_sugars for meal in day_row.meals)
    day_row.total_salt = sum(meal.total_salt for meal in day_row.meals)

    db.commit()
    logger.info("Persist complete user_id=%s day_date=%s total_meals=%s", summary.user_id, summary.day_date, len(day_row.meals))


def _item_key_from_db(item: MealItem) -> tuple:
    if item.plan_day_diet_item_id:
        return ("plan", item.plan_day_diet_item_id)
    return (
        "fallback",
        item.name,
        item.product_id,
        round(item.measure_quantity, 6),
        round(item.weight, 6),
        round(item.energy, 6),
    )


def _item_key_from_schema(item: MealItemSchema) -> tuple:
    if item.plan_day_diet_item_id:
        return ("plan", item.plan_day_diet_item_id)
    return (
        "fallback",
        item.name,
        item.product_id,
        round(item.measure_quantity, 6),
        round(item.weight, 6),
        round(item.energy, 6),
    )


def _recalculate_meal_totals(meal_row: MealNutrition) -> None:
    meal_row.item_count = len(meal_row.items)
    meal_row.total_energy = sum(item.energy for item in meal_row.items)
    meal_row.total_protein = sum(item.protein for item in meal_row.items)
    meal_row.total_fat = sum(item.fat for item in meal_row.items)
    meal_row.total_carbohydrate = sum(item.carbohydrate for item in meal_row.items)
    meal_row.total_fiber = sum(item.fiber for item in meal_row.items)
    meal_row.total_sugars = sum(item.sugars for item in meal_row.items)
    meal_row.total_salt = sum(item.salt for item in meal_row.items)


def sync_day_from_fitatu(db, client: FitatuClient, day_date: str) -> DaySummarySchema:
    logger.info("Sync start day_date=%s user_id=%s", day_date, client.user_id)
    payload = client.get_day(day_date)
    summary = aggregate_day_summary(client.user_id or "", day_date, payload.get("dietPlan", {}))
    persist_day_summary(db, summary)
    persisted_day = (
        db.query(DailyNutrition)
        .filter(DailyNutrition.user_id == (client.user_id or ""), DailyNutrition.day_date == datetime.strptime(day_date, "%Y-%m-%d").date())
        .one()
    )
    result = db_day_to_schema(persisted_day)
    logger.info("Sync complete day_date=%s user_id=%s meals=%s", day_date, result.user_id, len(result.meals))
    return result


def db_day_to_schema(day_row: DailyNutrition) -> DaySummarySchema:
    meals: list[MealSummarySchema] = []
    for meal in day_row.meals:
        meal_items = [
            MealItemSchema(
                id=item.id,
                plan_day_diet_item_id=item.plan_day_diet_item_id,
                product_id=item.product_id,
                name=item.name,
                brand=item.brand,
                measure_name=item.measure_name,
                measure_quantity=item.measure_quantity,
                weight=item.weight,
                energy=item.energy,
                protein=item.protein,
                fat=item.fat,
                carbohydrate=item.carbohydrate,
                fiber=item.fiber,
                sugars=item.sugars,
                salt=item.salt,
                eaten=item.eaten,
            )
            for item in meal.items
        ]

        meals.append(
            MealSummarySchema(
                meal_key=meal.meal_key,
                meal_name=meal.meal_name,
                meal_time=meal.meal_time,
                recommended_percent=meal.recommended_percent,
                item_count=meal.item_count,
                totals=MacroTotals(
                    energy=meal.total_energy,
                    protein=meal.total_protein,
                    fat=meal.total_fat,
                    carbohydrate=meal.total_carbohydrate,
                    fiber=meal.total_fiber,
                    sugars=meal.total_sugars,
                    salt=meal.total_salt,
                ),
                items=meal_items,
            )
        )

    return DaySummarySchema(
        user_id=day_row.user_id,
        day_date=day_row.day_date.isoformat(),
        totals=MacroTotals(
            energy=day_row.total_energy,
            protein=day_row.total_protein,
            fat=day_row.total_fat,
            carbohydrate=day_row.total_carbohydrate,
            fiber=day_row.total_fiber,
            sugars=day_row.total_sugars,
            salt=day_row.total_salt,
        ),
        meals=meals,
    )
