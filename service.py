import json
from datetime import datetime
import logging
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, joinedload, object_session

from .fitatu_client import FitatuClient
from .models import DailyNutrition, MealItem, MealNutrition, Product
from .schemas import DaySummarySchema, MacroTotals, MealItemSchema, MealSummarySchema, ProductSchema


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
        .options(joinedload(DailyNutrition.meals).joinedload(MealNutrition.items))
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
    summary_meal_keys = {meal.meal_key for meal in summary.meals}

    for meal_key, existing_meal in list(existing_meals.items()):
        if meal_key not in summary_meal_keys:
            logger.info("Deleting removed meal row meal_key=%s", meal_key)
            db.delete(existing_meal)
            existing_meals.pop(meal_key, None)

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

        existing_items_by_key = {_item_key_from_db(existing_item): existing_item for existing_item in meal_row.items}
        summary_item_keys = {_item_key_from_schema(item) for item in meal.items}

        for item_key, existing_item in list(existing_items_by_key.items()):
            if item_key not in summary_item_keys:
                logger.info("Deleting removed meal item meal_key=%s item_key=%s", meal.meal_key, item_key)
                db.delete(existing_item)
                existing_items_by_key.pop(item_key, None)

        for item in meal.items:
            item_key = _item_key_from_schema(item)
            existing_item = existing_items_by_key.get(item_key)
            if existing_item:
                # Keep cache additive, but refresh nutrient values so stale zero rows are corrected.
                existing_item.name = item.name
                existing_item.brand = item.brand
                existing_item.measure_name = item.measure_name
                existing_item.measure_quantity = item.measure_quantity
                existing_item.weight = item.weight
                existing_item.energy = item.energy
                existing_item.protein = item.protein
                existing_item.fat = item.fat
                existing_item.carbohydrate = item.carbohydrate
                existing_item.fiber = item.fiber
                existing_item.sugars = item.sugars
                existing_item.salt = item.salt
                existing_item.eaten = item.eaten
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
            existing_items_by_key[item_key] = db_item

    db.flush()

    meal_rows = db.query(MealNutrition).filter(MealNutrition.daily_id == day_row.id).all()
    for meal_row in meal_rows:
        _recalculate_meal_totals(meal_row)

    day_row.total_energy = sum(meal.total_energy for meal in meal_rows)
    day_row.total_protein = sum(meal.total_protein for meal in meal_rows)
    day_row.total_fat = sum(meal.total_fat for meal in meal_rows)
    day_row.total_carbohydrate = sum(meal.total_carbohydrate for meal in meal_rows)
    day_row.total_fiber = sum(meal.total_fiber for meal in meal_rows)
    day_row.total_sugars = sum(meal.total_sugars for meal in meal_rows)
    day_row.total_salt = sum(meal.total_salt for meal in meal_rows)

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
    session = object_session(meal_row)
    if session is None:
        items = list(meal_row.items)
    else:
        items = session.query(MealItem).filter(MealItem.meal_id == meal_row.id).all()

    meal_row.item_count = len(items)
    meal_row.total_energy = sum(item.energy for item in items)
    meal_row.total_protein = sum(item.protein for item in items)
    meal_row.total_fat = sum(item.fat for item in items)
    meal_row.total_carbohydrate = sum(item.carbohydrate for item in items)
    meal_row.total_fiber = sum(item.fiber for item in items)
    meal_row.total_sugars = sum(item.sugars for item in items)
    meal_row.total_salt = sum(item.salt for item in items)


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


# -- Product (custom catalog) helpers --

_PRODUCT_COLUMN_KEYS = {
    "name", "brand", "energy", "protein", "fat", "carbohydrate",
    "fiber", "sodium", "salt", "saturated_fat", "sugars", "cholesterol",
    "barcode",
}

_FITATU_TO_LOCAL = {
    "saturatedFat": "saturated_fat",
}


def upsert_product(
    db: Session,
    payload: dict,
    source: str = "custom",
    user_id: str | None = None,
) -> Product:
    """Insert or update a Product row keyed by Fitatu product id.

    Maps Fitatu's response keys (camelCase) to local snake_case columns.
    Stores the full payload as a JSON string in `raw` for forensics.
    When `source == "custom"` and `user_id` is provided, the row is stamped
    with that owner so multi-user search can scope custom products correctly.
    Catalog rows keep `user_id=None` (globally shared cache).
    """
    product_id = payload.get("id")
    if product_id is None:
        raise ValueError("upsert_product payload missing 'id'")

    column_values: dict = {}
    for src_key, value in payload.items():
        target_key = _FITATU_TO_LOCAL.get(src_key, src_key)
        if target_key in _PRODUCT_COLUMN_KEYS:
            column_values[target_key] = value

    raw_json = json.dumps(payload, default=str)

    existing = db.get(Product, product_id)
    if existing is None:
        product = Product(
            id=product_id,
            source=source,
            raw=raw_json,
            user_id=user_id if source == "custom" else None,
            **column_values,
        )
        db.add(product)
        return product

    for k, v in column_values.items():
        setattr(existing, k, v)
    existing.raw = raw_json
    # Source is sticky: don't downgrade a custom row to catalog by re-fetching.
    if existing.source != source and source == "custom":
        existing.source = source
    # Stamp ownership when a custom row is upserted with a user_id (e.g. user
    # re-creates an unowned legacy row).
    if source == "custom" and user_id is not None and existing.user_id is None:
        existing.user_id = user_id
    return existing


def get_product_local(db: Session, product_id: int) -> Product | None:
    return db.get(Product, product_id)


def delete_product(db: Session, product_id: int, user_id: str) -> bool:
    """Delete a custom product. Refuses to delete a row owned by another user."""
    existing = db.get(Product, product_id)
    if existing is None:
        return False
    if existing.source == "custom" and existing.user_id is not None and existing.user_id != user_id:
        raise ValueError(
            f"product {product_id} belongs to a different user; refusing to delete"
        )
    db.delete(existing)
    return True


def search_products_local(
    db: Session,
    query: str,
    scope: str,
    limit: int,
    user_id: str | None = None,
) -> list[Product]:
    stmt = select(Product).where(Product.name.ilike(f"%{query}%"))
    if scope == "custom":
        if user_id is None:
            raise ValueError("user_id is required when scope='custom'")
        stmt = stmt.where(Product.source == "custom", Product.user_id == user_id)
    elif scope == "catalog":
        stmt = stmt.where(Product.source == "catalog")
    elif scope == "all":
        if user_id is None:
            raise ValueError("user_id is required when scope='all'")
        stmt = stmt.where(
            or_(
                and_(Product.source == "custom", Product.user_id == user_id),
                Product.source == "catalog",
            )
        )
    else:
        raise ValueError(f"scope must be one of custom|catalog|all (got {scope!r})")
    stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars())


def product_to_schema(p: Product) -> ProductSchema:
    return ProductSchema.model_validate(p)


# -- Meal-item write helpers (spec §15) --

MEAL_KEYS_VALID: frozenset[str] = frozenset({
    "breakfast", "second_breakfast", "lunch", "dinner", "snack", "supper",
})

_DAY_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_meal_key(meal_key: str) -> None:
    if meal_key not in MEAL_KEYS_VALID:
        raise ValueError(
            f"meal_key {meal_key!r} not in {sorted(MEAL_KEYS_VALID)}"
        )


def _validate_day_date(day_date: str) -> None:
    if not _DAY_DATE_RE.match(day_date or ""):
        raise ValueError(f"date must be YYYY-MM-DD (got {day_date!r})")


def _resolve_product_for_meal_item(db: Session, client: FitatuClient, product_id: int) -> dict:
    """Cache-first product lookup; falls through to client.get_product.

    Returns the full product dict (with measures[]) — used to verify that the
    chosen measure_id actually exists on the product before we send a write.
    """
    local = get_product_local(db, product_id)
    if local is not None and local.raw:
        try:
            return json.loads(local.raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupt raw JSON for product %s; refetching", product_id)

    try:
        full = client.get_product(product_id)
    except RuntimeError as exc:
        raise RuntimeError(f"Product {product_id} not found in Fitatu") from exc
    upsert_product(db, full, source="catalog" if local is None else local.source)
    db.flush()
    return full


def _now_updated_at() -> str:
    """Mirror the format Fitatu's web client uses: 'YYYY-MM-DD H:M:S' (no zero pad)."""
    from datetime import datetime
    now = datetime.now()
    return f"{now.year:04d}-{now.month:02d}-{now.day:02d} {now.hour}:{now.minute}:{now.second}"


_WRITE_ITEM_KEYS = (
    "planDayDietItemId",
    "foodType",
    "measureId",
    "measureQuantity",
    "ingredientsServing",
    "mealNumber",
    "numberOfMeals",
    "eaten",
    "productId",
    "recipeId",
    "source",
    "updatedAt",
    "deletedAt",
)


def _strip_to_write_shape(item: dict) -> dict:
    """Keep only the fields Fitatu accepts on write; drop computed/server fields."""
    out: dict = {}
    for k in _WRITE_ITEM_KEYS:
        if k in item:
            out[k] = item[k]
    return out


def _load_day_envelope_for_write(client: FitatuClient, date: str) -> dict:
    """Fetch current day, strip to write shape per meal/item.

    Returns an envelope ready to be passed to client.post_day(date, envelope).
    Preserves all existing meals + items (the write endpoint replaces the whole day).
    """
    day_payload = client.get_day(date)
    diet_plan = (day_payload.get("dietPlan") or {}).copy()
    new_diet_plan: dict = {}
    for meal_key, meal in diet_plan.items():
        items_raw = (meal or {}).get("items") or []
        new_diet_plan[meal_key] = {"items": [_strip_to_write_shape(i) for i in items_raw]}
    return {
        "dietPlan": new_diet_plan,
        "toiletItems": day_payload.get("toiletItems") or [],
        "note": day_payload.get("note"),
        "tagsIds": day_payload.get("tagsIds") or [],
    }


def _ensure_meal_slot(envelope: dict, meal_key: str) -> list[dict]:
    """Ensure dietPlan[meal_key] exists; return the items list (mutable)."""
    diet_plan = envelope.setdefault("dietPlan", {})
    slot = diet_plan.setdefault(meal_key, {})
    items = slot.setdefault("items", [])
    return items


def add_meal_item(
    db: Session,
    client: FitatuClient,
    date: str,
    meal_key: str,
    product_id: int,
    measure_id: int,
    measure_quantity: float,
    food_type: str = "PRODUCT",
) -> dict:
    """Append a meal item via the whole-day POST endpoint.

    `food_type` is `"PRODUCT"` (default) or `"RECIPE"`. When `RECIPE`, `product_id`
    is interpreted as a recipe id; the item is written with `foodType="RECIPE"`,
    `recipeId=<id>`, and `ingredientsServing=<recipe.serving>` so server-side
    nutrition math matches Fitatu's web app.
    """
    _validate_day_date(date)
    _validate_meal_key(meal_key)
    if measure_quantity <= 0:
        raise ValueError(f"measure_quantity must be > 0 (got {measure_quantity})")
    food_type_u = (food_type or "PRODUCT").upper()
    if food_type_u not in {"PRODUCT", "RECIPE"}:
        raise ValueError(f"food_type must be PRODUCT or RECIPE (got {food_type!r})")

    if food_type_u == "PRODUCT":
        # Verify product + measure exist (catches typos early; server is lenient).
        product = _resolve_product_for_meal_item(db, client, product_id)
        measures = product.get("measures") or []
        if not any(m.get("id") == measure_id for m in measures):
            raise ValueError(
                f"measure_id {measure_id} not present in product {product_id}.measures[]"
            )
        recipe_serving: int | None = None
    else:
        try:
            recipe = client.get_recipe(product_id)
        except RuntimeError as exc:
            raise ValueError(f"recipe {product_id} not found: {exc}") from exc
        measures = recipe.get("measures") or []
        if not any(m.get("id") == measure_id for m in measures):
            raise ValueError(
                f"measure_id {measure_id} not present in recipe {product_id}.measures[]"
            )
        recipe_serving = recipe.get("serving") or recipe.get("numberOfPortions")

    envelope = _load_day_envelope_for_write(client, date)
    items = _ensure_meal_slot(envelope, meal_key)

    plan_id = client._gen_uuid()
    new_item: dict = {
        "planDayDietItemId": plan_id,
        "foodType": food_type_u,
        "measureId": measure_id,
        "measureQuantity": measure_quantity,
        "ingredientsServing": recipe_serving if food_type_u == "RECIPE" else None,
        "mealNumber": None,
        "numberOfMeals": None,
        "eaten": False,
        "source": "API",
        "updatedAt": _now_updated_at(),
    }
    if food_type_u == "PRODUCT":
        new_item["productId"] = product_id
    else:
        new_item["recipeId"] = product_id
    items.append(new_item)

    response = client.post_day(date, envelope)
    if response.status_code not in (200, 201, 202, 204):
        raise RuntimeError(
            f"post_day failed: {response.status_code} {getattr(response, 'text', '')[:200]}"
        )

    day_summary = sync_day_from_fitatu(db, client, date)
    return {
        "ok": True,
        "plan_day_diet_item_id": plan_id,
        "date": date,
        "meal_key": meal_key,
        "day": day_summary.model_dump(mode="json") if hasattr(day_summary, "model_dump") else day_summary,
    }


def update_meal_item(
    db: Session,
    client: FitatuClient,
    date: str,
    meal_key: str,
    plan_day_diet_item_id: str,
    new_measure_quantity: float,
) -> dict:
    """Update an item's measure_quantity in place; same planDayDietItemId."""
    _validate_day_date(date)
    _validate_meal_key(meal_key)
    if new_measure_quantity <= 0:
        raise ValueError(f"new_measure_quantity must be > 0 (got {new_measure_quantity})")

    envelope = _load_day_envelope_for_write(client, date)
    items = _ensure_meal_slot(envelope, meal_key)
    existing = next((i for i in items if str(i.get("planDayDietItemId")) == str(plan_day_diet_item_id)), None)
    if existing is None:
        raise RuntimeError(
            f"plan_day_diet_item_id {plan_day_diet_item_id!r} not found in {meal_key} of {date}"
        )
    if existing.get("deletedAt"):
        raise RuntimeError(
            f"plan_day_diet_item_id {plan_day_diet_item_id!r} is soft-deleted; cannot update"
        )

    existing["measureQuantity"] = new_measure_quantity
    existing["updatedAt"] = _now_updated_at()
    # If the item is a RECIPE with ingredientsServing, scale it (mirror web client: portions × qty).
    # We don't have access to recipe portions here without an extra fetch, so leave
    # ingredientsServing untouched for v1 — server keeps the old number-of-portions allocation.

    response = client.post_day(date, envelope)
    if response.status_code not in (200, 201, 202, 204):
        raise RuntimeError(
            f"post_day (update) failed: {response.status_code} {getattr(response, 'text', '')[:200]}"
        )

    day_summary = sync_day_from_fitatu(db, client, date)
    return {
        "ok": True,
        "plan_day_diet_item_id": plan_day_diet_item_id,
        "date": date,
        "meal_key": meal_key,
        "day": day_summary.model_dump(mode="json") if hasattr(day_summary, "model_dump") else day_summary,
    }


def delete_meal_item(
    db: Session,
    client: FitatuClient,
    date: str,
    meal_key: str,
    plan_day_diet_item_id: str,
    delete_all_related_meals: bool = False,
) -> dict:
    """Soft-delete: mark `deletedAt` on the item and POST the whole day.

    `delete_all_related_meals` is accepted for API parity but has no equivalent
    in the whole-day-POST contract; the param is ignored (single-item soft-delete).
    """
    _validate_day_date(date)
    _validate_meal_key(meal_key)

    envelope = _load_day_envelope_for_write(client, date)
    items = _ensure_meal_slot(envelope, meal_key)
    existing = next((i for i in items if str(i.get("planDayDietItemId")) == str(plan_day_diet_item_id)), None)
    if existing is None:
        raise RuntimeError(
            f"plan_day_diet_item_id {plan_day_diet_item_id!r} not found in {meal_key} of {date}"
        )
    if existing.get("deletedAt"):
        # Already deleted — idempotent no-op
        return {
            "ok": True,
            "deleted_plan_day_diet_item_id": plan_day_diet_item_id,
            "date": date,
            "meal_key": meal_key,
            "already_deleted": True,
        }

    # Format matches web client capture: "YYYY-MM-DD HH:MM:SS" (zero-padded here; server tolerates either).
    from datetime import datetime
    now = datetime.now()
    existing["deletedAt"] = now.strftime("%Y-%m-%d %H:%M:%S")

    response = client.post_day(date, envelope)
    if response.status_code not in (200, 201, 202, 204):
        raise RuntimeError(
            f"post_day (delete) failed: {response.status_code} {getattr(response, 'text', '')[:200]}"
        )

    day_summary = sync_day_from_fitatu(db, client, date)
    return {
        "ok": True,
        "deleted_plan_day_diet_item_id": plan_day_diet_item_id,
        "date": date,
        "meal_key": meal_key,
        "day": day_summary.model_dump(mode="json") if hasattr(day_summary, "model_dump") else day_summary,
    }
