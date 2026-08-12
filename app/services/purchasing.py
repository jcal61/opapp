"""
Purchasing & Receiving workflow service.
Supports creating POs, adding lines, submitting, and receiving goods
(with automatic theoretical inventory updates and cost updates).
"""

from __future__ import annotations
from datetime import datetime, date, timezone
from typing import List, Optional, Dict
from sqlalchemy.orm import Session
from app.models import (
    PurchaseOrder, PurchaseOrderLine, Receiving, Vendor,
    InventoryItem, Location, POStatus
)
from app.services.inventory import receive_po_line, get_or_create_stock
from app.services.costing import get_conversion_factor


def generate_po_number(db: Session) -> str:
    """Simple sequential PO number."""
    last = db.query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).first()
    next_num = (last.id + 1) if last else 1
    return f"PO-{next_num:05d}"


def create_purchase_order(
    db: Session,
    vendor_id: int,
    location_id: int,
    order_date: date | None = None,
    expected_date: date | None = None,
    notes: str | None = None,
    po_number: str | None = None,
) -> PurchaseOrder:
    po = PurchaseOrder(
        po_number=po_number or generate_po_number(db),
        vendor_id=vendor_id,
        location_id=location_id,
        status=POStatus.DRAFT.value,
        order_date=order_date or date.today(),
        expected_date=expected_date,
        notes=notes,
        created_at=datetime.now(timezone.utc),
    )
    db.add(po)
    db.flush()
    return po


def add_po_line(
    db: Session,
    purchase_order_id: int,
    item_id: int,
    quantity: float,
    unit: str,
    unit_cost: float = 0.0,
) -> PurchaseOrderLine:
    po = db.get(PurchaseOrder, purchase_order_id)
    if not po:
        raise ValueError("Purchase order not found")
    if po.status not in (POStatus.DRAFT.value, POStatus.SUBMITTED.value):
        raise ValueError(f"Cannot add lines to a PO in status '{po.status}'")

    # If line already exists for this item, update it
    existing = (
        db.query(PurchaseOrderLine)
        .filter(
            PurchaseOrderLine.purchase_order_id == purchase_order_id,
            PurchaseOrderLine.item_id == item_id,
        )
        .first()
    )
    if existing:
        existing.quantity_ordered = quantity
        existing.unit = unit
        existing.unit_cost = unit_cost
        db.flush()
        return existing

    line = PurchaseOrderLine(
        purchase_order_id=purchase_order_id,
        item_id=item_id,
        quantity_ordered=quantity,
        unit=unit,
        unit_cost=unit_cost,
        quantity_received=0.0,
    )
    db.add(line)
    db.flush()
    return line


def submit_po(db: Session, purchase_order_id: int) -> PurchaseOrder:
    po = db.get(PurchaseOrder, purchase_order_id)
    if not po:
        raise ValueError("PO not found")
    if po.status != POStatus.DRAFT.value:
        raise ValueError("Only draft POs can be submitted")
    if not po.lines:
        raise ValueError("Cannot submit an empty PO")
    po.status = POStatus.SUBMITTED.value
    db.flush()
    return po


def receive_against_po(
    db: Session,
    purchase_order_id: int,
    location_id: int,
    receipts: List[Dict],  # [{"line_id": 1, "quantity": 10.0}, ...]
    notes: str | None = None,
) -> Receiving:
    """
    Receive goods against a PO.
    receipts = list of {line_id, quantity} (quantity in the line's unit)
    Updates quantity_received on lines, adjusts theoretical inventory,
    updates item current_cost, and sets PO status.
    """
    po = db.get(PurchaseOrder, purchase_order_id)
    if not po:
        raise ValueError("Purchase order not found")
    if po.status in (POStatus.CANCELLED.value, POStatus.RECEIVED.value):
        raise ValueError(f"Cannot receive against a PO in status '{po.status}'")

    receiving = Receiving(
        purchase_order_id=po.id,
        location_id=location_id,
        received_at=datetime.now(timezone.utc),
        notes=notes,
    )
    db.add(receiving)
    db.flush()

    for r in receipts:
        line_id = r.get("line_id")
        qty = float(r.get("quantity", 0))
        if qty <= 0:
            continue

        line = db.get(PurchaseOrderLine, line_id)
        if not line or line.purchase_order_id != po.id:
            continue

        # Cap at remaining ordered quantity (optional – allow over-receive if desired)
        remaining = (line.quantity_ordered or 0) - (line.quantity_received or 0)
        # For demo we allow over-receiving; uncomment to enforce:
        # qty = min(qty, max(0, remaining))

        receive_po_line(db, line, qty, location_id)

    # Update PO status
    all_received = all(
        (l.quantity_received or 0) >= (l.quantity_ordered or 0)
        for l in po.lines
    )
    any_received = any((l.quantity_received or 0) > 0 for l in po.lines)

    if all_received:
        po.status = POStatus.RECEIVED.value
    elif any_received:
        po.status = POStatus.PARTIALLY_RECEIVED.value
    else:
        po.status = POStatus.SUBMITTED.value

    db.flush()
    return receiving


def cancel_po(db: Session, purchase_order_id: int) -> PurchaseOrder:
    po = db.get(PurchaseOrder, purchase_order_id)
    if not po:
        raise ValueError("PO not found")
    if po.status == POStatus.RECEIVED.value:
        raise ValueError("Cannot cancel a fully received PO")
    po.status = POStatus.CANCELLED.value
    db.flush()
    return po


def get_po_summary(db: Session, purchase_order_id: int) -> Dict:
    po = db.get(PurchaseOrder, purchase_order_id)
    if not po:
        return {}
    lines = []
    total_ordered = 0.0
    total_received = 0.0
    for line in po.lines:
        item = line.item
        line_total = (line.quantity_ordered or 0) * (line.unit_cost or 0)
        total_ordered += line_total
        total_received += (line.quantity_received or 0) * (line.unit_cost or 0)
        lines.append({
            "line_id": line.id,
            "item_id": item.id,
            "item_name": item.name,
            "sku": item.sku,
            "quantity_ordered": line.quantity_ordered,
            "quantity_received": line.quantity_received or 0,
            "remaining": (line.quantity_ordered or 0) - (line.quantity_received or 0),
            "unit": line.unit,
            "unit_cost": line.unit_cost,
            "line_total": round(line_total, 2),
        })
    return {
        "id": po.id,
        "po_number": po.po_number,
        "vendor": po.vendor.name if po.vendor else "",
        "vendor_id": po.vendor_id,
        "location_id": po.location_id,
        "status": po.status,
        "order_date": po.order_date,
        "expected_date": po.expected_date,
        "notes": po.notes,
        "lines": lines,
        "total_ordered": round(total_ordered, 2),
        "total_received_value": round(total_received, 2),
        "line_count": len(lines),
    }


def list_purchase_orders(
    db: Session,
    location_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
) -> List[PurchaseOrder]:
    q = db.query(PurchaseOrder).order_by(PurchaseOrder.created_at.desc())
    if location_id:
        q = q.filter(PurchaseOrder.location_id == location_id)
    if status:
        q = q.filter(PurchaseOrder.status == status)
    return q.limit(limit).all()


def suggest_order_from_par(
    db: Session,
    location_id: int,
) -> List[Dict]:
    """
    Return items below par with suggested order quantities.
    Useful as a starting point for creating a PO.
    """
    from app.services.variance import get_current_theoretical_snapshot
    snapshot = get_current_theoretical_snapshot(db, location_id)
    suggestions = []
    for row in snapshot:
        if row["below_par"]:
            needed = max(0.0, row["par_level"] - row["theoretical_qty"])
            item = db.get(InventoryItem, row["item_id"])
            suggestions.append({
                "item_id": row["item_id"],
                "name": row["name"],
                "category": row["category"],
                "current_theoretical": row["theoretical_qty"],
                "par_level": row["par_level"],
                "suggested_qty": round(needed, 2),
                "unit": row["base_unit"],
                "unit_cost": row["current_cost"],
                "est_cost": round(needed * row["current_cost"], 2),
                "preferred_vendor_id": item.preferred_vendor_id if item else None,
            })
    return sorted(suggestions, key=lambda x: x["est_cost"], reverse=True)
