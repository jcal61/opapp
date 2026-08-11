from datetime import datetime
from sqlalchemy import String, Float, ForeignKey, Boolean, DateTime, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

class InventoryItem(Base):
    """Master inventory item (ingredient, supply, etc.)"""
    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(80), unique=True)
    category: Mapped[str | None] = mapped_column(String(80))  # produce, protein, liquor, dry, etc.
    base_unit: Mapped[str] = mapped_column(String(30), default="each")  # the unit we count in
    current_cost: Mapped[float] = mapped_column(Float, default=0.0)  # cost per base_unit
    par_level: Mapped[float] = mapped_column(Float, default=0.0)  # desired minimum in base units
    preferred_vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    preferred_vendor = relationship("Vendor")
    stock_levels = relationship("StockLevel", back_populates="item")
    conversions = relationship("UnitConversion", back_populates="item", cascade="all, delete-orphan")
    recipe_ingredients = relationship("RecipeIngredient", back_populates="item")

    def __repr__(self):
        return f"<InventoryItem {self.name} ({self.base_unit})>"


class UnitConversion(Base):
    """How to convert between purchase/count/recipe units for an item."""
    __tablename__ = "unit_conversions"
    __table_args__ = (UniqueConstraint("item_id", "from_unit", "to_unit", name="uq_conversion"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), nullable=False)
    from_unit: Mapped[str] = mapped_column(String(30), nullable=False)
    to_unit: Mapped[str] = mapped_column(String(30), nullable=False)
    factor: Mapped[float] = mapped_column(Float, nullable=False)  # 1 from_unit = factor to_units

    item = relationship("InventoryItem", back_populates="conversions")


class StockLevel(Base):
    """Current theoretical and last physical stock per location."""
    __tablename__ = "stock_levels"
    __table_args__ = (UniqueConstraint("item_id", "location_id", name="uq_stock"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)

    theoretical_qty: Mapped[float] = mapped_column(Float, default=0.0)
    last_physical_qty: Mapped[float] = mapped_column(Float, default=0.0)
    last_count_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    item = relationship("InventoryItem", back_populates="stock_levels")
    location = relationship("Location", back_populates="stock_levels")
