"""
Full Variance Reporting – Theoretical vs Actual between two physical counts.
This is the classic Craftable control report.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Dict
from sqlalchemy.orm import Session
from app.models import (
    InventoryItem, StockLevel, InventoryCount, CountLine,
    PurchaseOrder, PurchaseOrderLine,
    WasteLog, Transfer, POSSale, POSSaleLine, Recipe, RecipeIngredient
)
from app.services.costing import get_conversion_factor
from app.services.inventory import get_or_create_stock


@dataclass
class ItemVariance:
    item_id: int
    item_name: str
    category: str | None
    base_unit: str
    starting_physical: float
    purchases: float
    transfers_in: float
    transfers_out: float
    pos_depletions: float
    waste: float
    theoretical_ending: float
    actual_ending: float
    variance_qty: float
    variance_pct: Optional[float]
    current_cost: float
    variance_cost: float


def _deplete_qty_for_recipe(
    db: Session,
    recipe_id: int,
    quantity_sold: float,
    target_item_id: int,
    visited: set | None = None,
) -> float:
    """How many base units of target_item_id are consumed by selling quantity_sold of the recipe."""
    if visited is None:
        visited = set()
    if recipe_id in visited:
        return 0.0
    visited.add(recipe_id)

    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        return 0.0

    total = 0.0
    for ing in recipe.ingredients:
        if ing.item_id == target_item_id:
            item = db.get(InventoryItem, target_item_id)
            if item:
                factor = get_conversion_factor(db, item, ing.unit, item.base_unit)
                total += ing.quantity * factor * quantity_sold
        elif ing.sub_recipe_id:
            sub = db.get(Recipe, ing.sub_recipe_id)
            if sub:
                scale = (ing.quantity / (sub.yield_qty or 1.0)) * quantity_sold
                total += _deplete_qty_for_recipe(db, ing.sub_recipe_id, scale, target_item_id, visited)

    visited.discard(recipe_id)
    return total


def calculate_variance_between_counts(
    db: Session,
    location_id: int,
    start_count_id: int,
    end_count_id: int,
) -> List[ItemVariance]:
    """
    Full Craftable-style variance report.

    Theoretical Ending =
        Starting Physical
        + Purchases received in period
        + Transfers In
        - POS Depletions (from sales in period)
        - Waste
        - Transfers Out

    Variance = Actual Ending Physical - Theoretical Ending
    """
    start_count = db.get(InventoryCount, start_count_id)
    end_count = db.get(InventoryCount, end_count_id)

    if not start_count or not end_count:
        raise ValueError("Both counts must exist")
    if start_count.location_id != location_id or end_count.location_id != location_id:
        raise ValueError("Counts must belong to the same location")

    start_dt = start_count.counted_at
    end_dt = end_count.counted_at
    if start_dt > end_dt:
        start_count, end_count = end_count, start_count
        start_dt, end_dt = end_dt, start_dt
        start_count_id, end_count_id = end_count_id, start_count_id

    start_map: Dict[int, float] = {line.item_id: line.quantity for line in start_count.lines}
    end_map: Dict[int, float] = {line.item_id: line.quantity for line in end_count.lines}
    item_ids = set(start_map.keys()) | set(end_map.keys())

    # Pre-load POS sales in the window
    sales = (
        db.query(POSSale)
        .filter(
            POSSale.location_id == location_id,
            POSSale.sold_at >= start_dt,
            POSSale.sold_at <= end_dt,
        )
        .all()
    )

    results: List[ItemVariance] = []

    for item_id in sorted(item_ids):
        item = db.get(InventoryItem, item_id)
        if not item:
            continue

        start_phys = start_map.get(item_id, 0.0)
        end_phys = end_map.get(item_id, 0.0)

        # Purchases
        purchases = 0.0
        po_lines = (
            db.query(PurchaseOrderLine)
            .join(PurchaseOrder)
            .filter(
                PurchaseOrder.location_id == location_id,
                PurchaseOrderLine.item_id == item_id,
            )
            .all()
        )
        for pol in po_lines:
            factor = get_conversion_factor(db, item, pol.unit, item.base_unit)
            purchases += (pol.quantity_received or 0.0) * factor

        # Waste in period
        waste_qty = 0.0
        wastes = (
            db.query(WasteLog)
            .filter(
                WasteLog.location_id == location_id,
                WasteLog.item_id == item_id,
                WasteLog.logged_at >= start_dt,
                WasteLog.logged_at <= end_dt,
            )
            .all()
        )
        for w in wastes:
            factor = get_conversion_factor(db, item, w.unit, item.base_unit)
            waste_qty += w.quantity * factor

        # Transfers
        transfers_in = 0.0
        transfers_out = 0.0
        transfers = (
            db.query(Transfer)
            .filter(
                Transfer.item_id == item_id,
                Transfer.transferred_at >= start_dt,
                Transfer.transferred_at <= end_dt,
            )
            .all()
        )
        for t in transfers:
            factor = get_conversion_factor(db, item, t.unit, item.base_unit)
            qty = t.quantity * factor
            if t.to_location_id == location_id:
                transfers_in += qty
            if t.from_location_id == location_id:
                transfers_out += qty

        # POS Depletions
        pos_depletions = 0.0
        for sale in sales:
            for line in sale.lines:
                if line.recipe_id:
                    pos_depletions += _deplete_qty_for_recipe(
                        db, line.recipe_id, line.quantity, item_id
                    )

        theoretical = (
            start_phys
            + purchases
            + transfers_in
            - pos_depletions
            - waste_qty
            - transfers_out
        )

        variance_qty = end_phys - theoretical
        variance_pct = (variance_qty / theoretical * 100) if theoretical else None
        variance_cost = variance_qty * (item.current_cost or 0.0)

        results.append(
            ItemVariance(
                item_id=item.id,
                item_name=item.name,
                category=item.category,
                base_unit=item.base_unit,
                starting_physical=round(start_phys, 3),
                purchases=round(purchases, 3),
                transfers_in=round(transfers_in, 3),
                transfers_out=round(transfers_out, 3),
                pos_depletions=round(pos_depletions, 3),
                waste=round(waste_qty, 3),
                theoretical_ending=round(theoretical, 3),
                actual_ending=round(end_phys, 3),
                variance_qty=round(variance_qty, 3),
                variance_pct=round(variance_pct, 2) if variance_pct is not None else None,
                current_cost=item.current_cost or 0.0,
                variance_cost=round(variance_cost, 2),
            )
        )

    results.sort(key=lambda x: abs(x.variance_cost), reverse=True)
    return results


def get_current_theoretical_snapshot(db: Session, location_id: int) -> List[dict]:
    """Live view of theoretical stock + last physical."""
    stocks = (
        db.query(StockLevel)
        .filter(StockLevel.location_id == location_id)
        .all()
    )
    rows = []
    for s in stocks:
        item = s.item
        rows.append({
            "item_id": item.id,
            "name": item.name,
            "category": item.category,
            "department": item.department,
            "base_unit": item.base_unit,
            "theoretical_qty": round(s.theoretical_qty or 0, 3),
            "last_physical": round(s.last_physical_qty or 0, 3),
            "par_level": item.par_level,
            "current_cost": item.current_cost,
            "below_par": (s.theoretical_qty or 0) < (item.par_level or 0),
        })
    return sorted(rows, key=lambda r: r["name"])
