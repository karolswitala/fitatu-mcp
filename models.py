from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class DailyNutrition(Base):
    __tablename__ = "daily_nutrition"
    __table_args__ = (UniqueConstraint("user_id", "day_date", name="uq_daily_nutrition_user_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    day_date: Mapped[date] = mapped_column(Date, index=True)

    total_energy: Mapped[float] = mapped_column(Float, default=0.0)
    total_protein: Mapped[float] = mapped_column(Float, default=0.0)
    total_fat: Mapped[float] = mapped_column(Float, default=0.0)
    total_carbohydrate: Mapped[float] = mapped_column(Float, default=0.0)
    total_fiber: Mapped[float] = mapped_column(Float, default=0.0)
    total_sugars: Mapped[float] = mapped_column(Float, default=0.0)
    total_salt: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    meals: Mapped[list["MealNutrition"]] = relationship(
        back_populates="daily",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MealNutrition(Base):
    __tablename__ = "meal_nutrition"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    daily_id: Mapped[int] = mapped_column(ForeignKey("daily_nutrition.id", ondelete="CASCADE"), index=True)

    meal_key: Mapped[str] = mapped_column(String(64), index=True)
    meal_name: Mapped[str] = mapped_column(String(128))
    meal_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    recommended_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)

    total_energy: Mapped[float] = mapped_column(Float, default=0.0)
    total_protein: Mapped[float] = mapped_column(Float, default=0.0)
    total_fat: Mapped[float] = mapped_column(Float, default=0.0)
    total_carbohydrate: Mapped[float] = mapped_column(Float, default=0.0)
    total_fiber: Mapped[float] = mapped_column(Float, default=0.0)
    total_sugars: Mapped[float] = mapped_column(Float, default=0.0)
    total_salt: Mapped[float] = mapped_column(Float, default=0.0)
    item_count: Mapped[int] = mapped_column(Integer, default=0)

    daily: Mapped[DailyNutrition] = relationship(back_populates="meals")
    items: Mapped[list["MealItem"]] = relationship(
        back_populates="meal",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MealItem(Base):
    __tablename__ = "meal_item"
    __table_args__ = (
        UniqueConstraint("meal_id", "plan_day_diet_item_id", name="uq_meal_item_plan_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meal_id: Mapped[int] = mapped_column(ForeignKey("meal_nutrition.id", ondelete="CASCADE"), index=True)

    plan_day_diet_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)

    measure_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    measure_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    weight: Mapped[float] = mapped_column(Float, default=0.0)

    energy: Mapped[float] = mapped_column(Float, default=0.0)
    protein: Mapped[float] = mapped_column(Float, default=0.0)
    fat: Mapped[float] = mapped_column(Float, default=0.0)
    carbohydrate: Mapped[float] = mapped_column(Float, default=0.0)
    fiber: Mapped[float] = mapped_column(Float, default=0.0)
    sugars: Mapped[float] = mapped_column(Float, default=0.0)
    salt: Mapped[float] = mapped_column(Float, default=0.0)
    eaten: Mapped[bool] = mapped_column(Boolean, default=False)

    meal: Mapped[MealNutrition] = relationship(back_populates="items")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_user_source", "user_id", "source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255), index=True)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    energy: Mapped[float] = mapped_column(Float)
    protein: Mapped[float] = mapped_column(Float)
    fat: Mapped[float] = mapped_column(Float)
    carbohydrate: Mapped[float] = mapped_column(Float)
    fiber: Mapped[float | None] = mapped_column(Float, nullable=True)
    sodium: Mapped[float | None] = mapped_column(Float, nullable=True)
    salt: Mapped[float | None] = mapped_column(Float, nullable=True)
    saturated_fat: Mapped[float | None] = mapped_column(Float, nullable=True)
    sugars: Mapped[float | None] = mapped_column(Float, nullable=True)
    cholesterol: Mapped[float | None] = mapped_column(Float, nullable=True)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    raw: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="custom")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
