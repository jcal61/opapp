"""
Inventory service – theoretical stock, receiving, waste, transfers, depletions.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional, List, Dict
from sqlalchemy.orm import Session
from app.models import (
    InventoryItem, StockLevel, Location,
    PurchaseOrder, PurchaseOrderLine, Receiving,
    WasteLog, Transfer, POSSale, POSSaleLine, Recipe, RecipeIngredient
)
from app.services.costing import get_conversion_factor, calculate_recipe_cost


# ---------- Inventory item CRUD ----------

def create_item(
    db: Session,
    name: str,
    base_unit: str,
    sku: Optional[str] = None,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    current_cost: float = 0.0,
    par_level: float = 0.0,
    preferred_vendor_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> InventoryItem:
    if db.query(InventoryItem).filter(InventoryItem.name == name).first():
        raise ValueError(f"An item named '{name}' already exists.")
    if sku and db.query(InventoryItem).filter(InventoryItem.sku == sku).first():
        raise ValueError(f"SKU '{sku}' is already used by another item.")
    item = InventoryItem(
        name=name.strip(),
        base_unit=base_unit.strip(),
        sku=(sku or "").strip() or None,
        category=(category or "").strip() or None,
        subcategory=(subcategory or "").strip() or None,
        current_cost=current_cost or 0.0,
        par_level=par_level or 0.0,
        preferred_vendor_id=preferred_vendor_id,
        notes=(notes or "").strip() or None,
    )
    db.add(item)
    db.flush()
    return item


def update_item(
    db: Session,
    item_id: int,
    name: Optional[str] = None,
    base_unit: Optional[str] = None,
    sku: Optional[str] = None,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    current_cost: Optional[float] = None,
    par_level: Optional[float] = None,
    preferred_vendor_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> InventoryItem:
    item = db.get(InventoryItem, item_id)
    if not item:
        raise ValueError("Item not found")
    if sku and sku.strip():
        dupe = db.query(InventoryItem).filter(InventoryItem.sku == sku.strip(), InventoryItem.id != item_id).first()
        if dupe:
            raise ValueError(f"SKU '{sku}' is already used by another item.")
    if name is not None:
        item.name = name.strip()
    if base_unit is not None:
        item.base_unit = base_unit.strip()
    if sku is not None:
        item.sku = sku.strip() or None
    if category is not None:
        item.category = category.strip() or None
    if subcategory is not None:
        item.subcategory = subcategory.strip() or None
    if current_cost is not None:
        item.current_cost = current_cost
    if par_level is not None:
        item.par_level = par_level
    if preferred_vendor_id is not None:
        item.preferred_vendor_id = preferred_vendor_id or None
    if notes is not None:
        item.notes = notes.strip() or None
    db.flush()
    return item


def set_item_active(db: Session, item_id: int, active: bool) -> InventoryItem:
    item = db.get(InventoryItem, item_id)
    if not item:
        raise ValueError("Item not found")
    item.is_active = active
    db.flush()
    return item


def list_items(db: Session, active_only: bool = True) -> List[InventoryItem]:
    q = db.query(InventoryItem).order_by(InventoryItem.category, InventoryItem.name)
    if active_only:
        q = q.filter(InventoryItem.is_active == True)
    return q.all()


def get_item_by_name(db: Session, name: str) -> Optional[InventoryItem]:
    return db.query(InventoryItem).filter(InventoryItem.name == name.strip()).first()


def set_opening_stock(db: Session, item_id: int, location_id: int, quantity: float) -> StockLevel:
    """Set a starting theoretical + physical count directly (bulk data load,
    not a normal receiving/count event)."""
    stock = get_or_create_stock(db, item_id, location_id)
    stock.theoretical_qty = quantity
    stock.last_physical_qty = quantity
    stock.updated_at = datetime.utcnow()
    db.flush()
    return stock


def get_or_create_stock(db: Session, item_id: int, location_id: int) -> StockLevel:
    stock = (
        db.query(StockLevel)
        .filter(StockLevel.item_id == item_id, StockLevel.location_id == location_id)
        .first()
    )
    if not stock:
        stock = StockLevel(item_id=item_id, location_id=location_id, theoretical_qty=0.0)
        db.add(stock)
        db.flush()
    return stock


def adjust_theoretical(
    db: Session,
    item_id: int,
    location_id: int,
    delta: float,
    reason: str = "",
) -> StockLevel:
    """Add (positive) or subtract (negative) from theoretical quantity."""
    stock = get_or_create_stock(db, item_id, location_id)
    stock.theoretical_qty = (stock.theoretical_qty or 0.0) + delta
    stock.updated_at = datetime.utcnow()
    return stock


def receive_po_line(
    db: Session,
    po_line: PurchaseOrderLine,
    qty_received: float,
    location_id: int,
) -> None:
    """Record receiving against a PO line and increase theoretical stock."""
    item = db.get(InventoryItem, po_line.item_id)
    if not item:
        return

    # Convert received quantity into the item's base_unit
    factor = get_conversion_factor(db, item, po_line.unit, item.base_unit)
    qty_base = qty_received * factor

    adjust_theoretical(db, item.id, location_id, qty_base, reason="receiving")

    po_line.quantity_received = (po_line.quantity_received or 0.0) + qty_received

    # Optionally update current cost from the PO
    if po_line.unit_cost and po_line.unit_cost > 0:
        # Convert cost to base unit
        cost_per_base = po_line.unit_cost / factor if factor else po_line.unit_cost
        item.current_cost = cost_per_base


def log_waste(
    db: Session,
    item_id: int,
    location_id: int,
    quantity: float,
    unit: str,
    reason: str = "spoilage",
) -> WasteLog:
    item = db.get(InventoryItem, item_id)
    if not item:
        raise ValueError("Item not found")

    factor = get_conversion_factor(db, item, unit, item.base_unit)
    qty_base = quantity * factor

    adjust_theoretical(db, item_id, location_id, -qty_base, reason="waste")

    waste = WasteLog(
        item_id=item_id,
        location_id=location_id,
        quantity=quantity,
        unit=unit,
        reason=reason,
    )
    db.add(waste)
    return waste


def transfer_stock(
    db: Session,
    item_id: int,
    from_location_id: int,
    to_location_id: int,
    quantity: float,
    unit: str,
) -> Transfer:
    item = db.get(InventoryItem, item_id)
    if not item:
        raise ValueError("Item not found")

    factor = get_conversion_factor(db, item, unit, item.base_unit)
    qty_base = quantity * factor

    adjust_theoretical(db, item_id, from_location_id, -qty_base, reason="transfer out")
    adjust_theoretical(db, item_id, to_location_id, +qty_base, reason="transfer in")

    t = Transfer(
        item_id=item_id,
        from_location_id=from_location_id,
        to_location_id=to_location_id,
        quantity=quantity,
        unit=unit,
    )
    db.add(t)
    return t


def deplete_recipe(
    db: Session,
    recipe_id: int,
    location_id: int,
    quantity_sold: float = 1.0,
    visited: Optional[set] = None,
) -> None:
    """
    Recursively deplete inventory for a sold recipe (and its sub-recipes).
    This is the key link between POS sales and theoretical inventory.
    """
    if visited is None:
        visited = set()

    if recipe_id in visited:
        return  # prevent infinite recursion
    visited.add(recipe_id)

    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        return

    for ing in recipe.ingredients:
        if ing.item_id:
            item = db.get(InventoryItem, ing.item_id)
            if not item:
                continue
            factor = get_conversion_factor(db, item, ing.unit, item.base_unit)
            qty_base = ing.quantity * factor * quantity_sold
            adjust_theoretical(db, item.id, location_id, -qty_base, reason="pos depletion")
        elif ing.sub_recipe_id:
            # Scale the entire sub-recipe
            sub = db.get(Recipe, ing.sub_recipe_id)
            if sub:
                scale = (ing.quantity / (sub.yield_qty or 1.0)) * quantity_sold
                deplete_recipe(db, ing.sub_recipe_id, location_id, scale, visited)

    visited.discard(recipe_id)


def record_pos_sale(
    db: Session,
    location_id: int,
    lines: List[dict],
    external_id: Optional[str] = None,
) -> POSSale:
    """
    lines = [{"recipe_id": 1, "pos_item_name": "Old Fashioned", "quantity": 2, "unit_price": 14.0}, ...]
    """
    sale = POSSale(
        location_id=location_id,
        external_id=external_id,
        sold_at=datetime.utcnow(),
    )
    db.add(sale)
    db.flush()

    total = 0.0
    for line in lines:
        recipe_id = line.get("recipe_id")
        qty = float(line.get("quantity", 1))
        price = line.get("unit_price")

        sale_line = POSSaleLine(
            sale_id=sale.id,
            recipe_id=recipe_id,
            pos_item_name=line.get("pos_item_name", "Unknown"),
            quantity=qty,
            unit_price=price,
        )
        db.add(sale_line)

        if price:
            total += price * qty

        if recipe_id:
            deplete_recipe(db, recipe_id, location_id, qty)

    sale.total_amount = total
    return sale
