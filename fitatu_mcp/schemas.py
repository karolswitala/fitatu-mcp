from pydantic import BaseModel, Field


class MacroTotals(BaseModel):
    energy: float = 0.0
    protein: float = 0.0
    fat: float = 0.0
    carbohydrate: float = 0.0
    fiber: float = 0.0
    sugars: float = 0.0
    salt: float = 0.0


class MealItemSchema(BaseModel):
    id: int | None = None
    plan_day_diet_item_id: str | None = None
    product_id: int | None = None
    name: str
    brand: str | None = None
    measure_name: str | None = None
    measure_quantity: float = 0.0
    weight: float = 0.0
    energy: float = 0.0
    protein: float = 0.0
    fat: float = 0.0
    carbohydrate: float = 0.0
    fiber: float = 0.0
    sugars: float = 0.0
    salt: float = 0.0
    eaten: bool = False


class MealSummarySchema(BaseModel):
    meal_key: str
    meal_name: str
    meal_time: str | None = None
    recommended_percent: int | None = None
    item_count: int = 0
    totals: MacroTotals = Field(default_factory=MacroTotals)
    items: list[MealItemSchema] = Field(default_factory=list)


class DaySummarySchema(BaseModel):
    user_id: str
    day_date: str
    totals: MacroTotals = Field(default_factory=MacroTotals)
    meals: list[MealSummarySchema] = Field(default_factory=list)
