from datetime import datetime
from sqlalchemy import String, Float, ForeignKey, DateTime, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

class InventoryCount(Base):
    """A physical inventory audit / count event."""
    __tablename__ = "inventory_counts"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="Weekly Count")
    counted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    location = relationship("Location", back_populates="counts")
    lines = relationship("CountLine", back_populates="count", cascade="all, delete-orphan")


class CountLine(Base):
    __tablename__ = "count_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    count_id: Mapped[int] = mapped_column(ForeignKey("inventory_counts.id"), nullable=False)
    item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)  # physical quantity in base_unit
    notes: Mapped[str | None] = mapped_column(String(200))

    count = relationship("InventoryCount", back_populates="lines")
    item = relationship("InventoryItem")


class WasteLog(Base):
    __tablename__ = "waste_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(30), default="each")
    reason: Mapped[str | None] = mapped_column(String(120))  # spoilage, overproduction, etc.
    logged_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes: Mapped[str | None] = mapped_column(Text)

    item = relationship("InventoryItem")
    location = relationship("Location")


class Transfer(Base):
    __tablename__ = "transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), nullable=False)
    from_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    to_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(30), default="each")
    transferred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes: Mapped[str | None] = mapped_column(Text)

    item = relationship("InventoryItem")


class POSSale(Base):
    """A sale event from the POS (can be aggregated or individual tickets)."""
    __tablename__ = "pos_sales"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(100))  # Toast check id etc.
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    sold_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    total_amount: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    location = relationship("Location")
    lines = relationship("POSSaleLine", back_populates="sale", cascade="all, delete-orphan")


class POSSaleLine(Base):
    """
    One sold item. We map it to a Recipe so we can deplete ingredients.
    quantity = number of that recipe sold.
    """
    __tablename__ = "pos_sale_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("pos_sales.id"), nullable=False)
    recipe_id: Mapped[int | None] = mapped_column(ForeignKey("recipes.id"))
    pos_item_name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    unit_price: Mapped[float | None] = mapped_column(Float)

    sale = relationship("POSSale", back_populates="lines")
    recipe = relationship("Recipe")
