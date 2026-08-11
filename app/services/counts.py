"""
Physical Inventory Count service.
Handles creating counts, entering quantities, closing counts,
and aligning theoretical stock to physical when desired.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Dict, Optional
from sqlalchemy.orm import Session
from app.models import (
    InventoryCount, CountLine, InventoryItem, StockLevel, Location
)
from app.services.inventory import get_or_create_stock


def create_count(
    db: Session,
    location_id: int,
    name: str = "Physical Count",
    notes: str | None = None,
) -> InventoryCount:
    """Start a new open physical count."""
    count = InventoryCount(
        location_id=location_id,
        name=name,
        counted_at=datetime.now(timezone.utc),
        is_closed=False,
        notes=notes,
    )
    db.add(count)
    db.flush()
    return count


def add_or_update_count_line(
    db: Session,
    count_id: int,
    item_id: int,
    quantity: float,
    notes: str | None = None,
) -> CountLine:
    """Add or update a line on an open count."""
    count = db.get(InventoryCount, count_id)
    if not count:
        raise ValueError("Count not found")
    if count.is_closed:
        raise ValueError("Cannot modify a closed count")

    line = (
        db.query(CountLine)
        .filter(CountLine.count_id == count_id, CountLine.item_id == item_id)
        .first()
    )
    if line:
        line.quantity = quantity
        line.notes = notes
    else:
        line = CountLine(
            count_id=count_id,
            item_id=item_id,
            quantity=quantity,
            notes=notes,
        )
        db.add(line)
    db.flush()
    return line


def close_count(
    db: Session,
    count_id: int,
    align_theoretical: bool = True,
) -> InventoryCount:
    """
    Close a count.
    If align_theoretical=True, set theoretical_qty = physical for every counted item
    (standard practice after a full audit).
    """
    count = db.get(InventoryCount, count_id)
    if not count:
        raise ValueError("Count not found")
    if count.is_closed:
        return count

    for line in count.lines:
        stock = get_or_create_stock(db, line.item_id, count.location_id)
        stock.last_physical_qty = line.quantity
        stock.last_count_at = count.counted_at or datetime.now(timezone.utc)
        if align_theoretical:
            stock.theoretical_qty = line.quantity

    count.is_closed = True
    db.flush()
    return count


def get_open_counts(db: Session, location_id: int) -> List[InventoryCount]:
    return (
        db.query(InventoryCount)
        .filter(
            InventoryCount.location_id == location_id,
            InventoryCount.is_closed == False,
        )
        .order_by(InventoryCount.counted_at.desc())
        .all()
    )


def get_closed_counts(db: Session, location_id: int, limit: int = 20) -> List[InventoryCount]:
    return (
        db.query(InventoryCount)
        .filter(
            InventoryCount.location_id == location_id,
            InventoryCount.is_closed == True,
        )
        .order_by(InventoryCount.counted_at.desc())
        .limit(limit)
        .all()
    )


def get_count_summary(db: Session, count_id: int) -> Dict:
    count = db.get(InventoryCount, count_id)
    if not count:
        return {}
    lines = []
    for line in count.lines:
        item = line.item
        stock = get_or_create_stock(db, item.id, count.location_id)
        lines.append({
            "item_id": item.id,
            "name": item.name,
            "category": item.category,
            "base_unit": item.base_unit,
            "counted_qty": line.quantity,
            "theoretical_at_count": stock.theoretical_qty,  # may have changed after
            "notes": line.notes,
        })
    return {
        "id": count.id,
        "name": count.name,
        "counted_at": count.counted_at,
        "is_closed": count.is_closed,
        "location_id": count.location_id,
        "line_count": len(lines),
        "lines": lines,
    }
