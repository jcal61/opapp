"""
Accounts Payable service: capture vendor invoices, link them to purchase
orders, and run a 3-way match (PO ordered/priced vs Receiving actual vs
Invoice billed) that flags quantity and price variance for manager review.
"""

from __future__ import annotations
from datetime import datetime, date, timezone
from typing import List, Optional, Dict
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Invoice, InvoiceLine, InvoiceStatus, LineMatchStatus,
    PurchaseOrder, PurchaseOrderLine, InventoryItem, Vendor,
)
from app.services.costing import get_conversion_factor, find_recipes_using_item

QTY_TOLERANCE = 0.01          # base units of slack before flagging qty variance
PRICE_TOLERANCE_PCT = 0.02    # 2% price drift tolerated before flagging
COST_CHANGE_TOLERANCE = 0.0005  # ignore sub-fraction-of-a-cent float noise when updating current_cost


# ---------- Capture ----------

def create_invoice(
    db: Session,
    vendor_id: int,
    location_id: int,
    invoice_number: str | None = None,
    invoice_date: date | None = None,
    due_date: date | None = None,
    purchase_order_id: int | None = None,
    total_amount: float | None = None,
    notes: str | None = None,
    original_filename: str | None = None,
    ai_extraction_raw: str | None = None,
) -> Invoice:
    inv = Invoice(
        invoice_number=invoice_number,
        vendor_id=vendor_id,
        location_id=location_id,
        purchase_order_id=purchase_order_id,
        invoice_date=invoice_date or date.today(),
        due_date=due_date,
        total_amount=total_amount,
        status=InvoiceStatus.RECEIVED.value,
        notes=notes,
        original_filename=original_filename,
        ai_extraction_raw=ai_extraction_raw,
        created_at=datetime.now(timezone.utc),
    )
    db.add(inv)
    db.flush()
    return inv


def add_invoice_line(
    db: Session,
    invoice_id: int,
    description: str,
    quantity: float,
    unit_price: float,
    unit: str | None = None,
    item_id: int | None = None,
    purchase_order_line_id: int | None = None,
    gl_code: str | None = None,
    sku: str | None = None,
    quantity_ordered: float | None = None,
) -> InvoiceLine:
    line = InvoiceLine(
        invoice_id=invoice_id,
        item_id=item_id,
        purchase_order_line_id=purchase_order_line_id,
        description=description,
        sku=sku,
        quantity=quantity,
        quantity_ordered=quantity_ordered,
        unit=unit,
        unit_price=unit_price,
        line_total=round(quantity * unit_price, 2),
        gl_code=gl_code,
    )
    db.add(line)
    db.flush()
    _recompute_header_total(db, invoice_id)
    return line


def delete_invoice_line(db: Session, line_id: int) -> None:
    line = db.get(InvoiceLine, line_id)
    if not line:
        return
    invoice_id = line.invoice_id
    db.delete(line)
    db.flush()
    _recompute_header_total(db, invoice_id)


def _recompute_header_total(db: Session, invoice_id: int) -> None:
    """Keep the header total in sync with line items whenever lines exist."""
    inv = db.get(Invoice, invoice_id)
    if not inv:
        return
    if inv.lines:
        inv.total_amount = round(sum(l.line_total for l in inv.lines), 2)
        db.flush()


# ---------- 3-way matching ----------

def auto_match_to_po(db: Session, invoice_id: int) -> Dict:
    """
    Match each invoice line to its PO line (explicit link, or by item if the
    invoice references a PO) and flag variance:
      - unmatched:      no PO line found, or nothing has been received yet
      - qty_variance:   invoiced qty differs from qty actually received
      - price_variance: invoiced unit price differs from PO unit cost by more
                         than PRICE_TOLERANCE_PCT
      - matched:        within tolerance on both qty and price

    Rolls the invoice header status up to matched / exception based on lines.
    Returns a summary dict with variance line items and dollar impact.
    """
    invoice = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.purchase_order))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    if not invoice:
        raise ValueError("Invoice not found")

    po_lines_by_item: Dict[int, PurchaseOrderLine] = {}
    if invoice.purchase_order_id:
        po_lines = (
            db.query(PurchaseOrderLine)
            .filter(PurchaseOrderLine.purchase_order_id == invoice.purchase_order_id)
            .all()
        )
        for pl in po_lines:
            po_lines_by_item[pl.item_id] = pl

    variance_lines = []
    any_variance_or_unmatched = False
    any_matched = False
    has_any_po_link = False

    for line in invoice.lines:
        po_line: Optional[PurchaseOrderLine] = None
        if line.purchase_order_line_id:
            po_line = db.get(PurchaseOrderLine, line.purchase_order_line_id)
        elif line.item_id and line.item_id in po_lines_by_item:
            po_line = po_lines_by_item[line.item_id]

        if not po_line:
            line.match_status = LineMatchStatus.UNMATCHED.value
            variance_lines.append(_variance_row(line, None, "unmatched"))
            any_variance_or_unmatched = True
            continue

        has_any_po_link = True
        line.purchase_order_line_id = po_line.id
        received = po_line.quantity_received or 0.0
        qty_diff = line.quantity - received
        price_diff = line.unit_price - (po_line.unit_cost or 0.0)
        price_diff_pct = (
            abs(price_diff) / po_line.unit_cost if po_line.unit_cost else (1.0 if line.unit_price else 0.0)
        )

        if received <= 0:
            status = LineMatchStatus.UNMATCHED
        elif abs(qty_diff) > QTY_TOLERANCE:
            status = LineMatchStatus.QTY_VARIANCE
        elif price_diff_pct > PRICE_TOLERANCE_PCT:
            status = LineMatchStatus.PRICE_VARIANCE
        else:
            status = LineMatchStatus.MATCHED

        line.match_status = status.value

        if status == LineMatchStatus.MATCHED:
            any_matched = True
        else:
            any_variance_or_unmatched = True

        variance_lines.append(_variance_row(line, po_line, status.value, qty_diff=qty_diff, price_diff=price_diff))

    if not invoice.lines:
        invoice.status = InvoiceStatus.RECEIVED.value
    elif not has_any_po_link:
        # Non-PO invoice (e.g. utility bill) — nothing to match against.
        invoice.status = InvoiceStatus.RECEIVED.value
    elif any_variance_or_unmatched:
        invoice.status = InvoiceStatus.EXCEPTION.value
    else:
        invoice.status = InvoiceStatus.MATCHED.value

    db.flush()

    total_price_variance = round(sum(r["price_variance_dollars"] for r in variance_lines), 2)
    total_qty_variance = round(sum(r["qty_variance_dollars"] for r in variance_lines), 2)

    return {
        "invoice_id": invoice.id,
        "status": invoice.status,
        "lines": variance_lines,
        "total_price_variance_dollars": total_price_variance,
        "total_qty_variance_dollars": total_qty_variance,
        "total_variance_dollars": round(total_price_variance + total_qty_variance, 2),
    }


def _variance_row(
    line: InvoiceLine,
    po_line: Optional[PurchaseOrderLine],
    status: str,
    qty_diff: float = 0.0,
    price_diff: float = 0.0,
) -> Dict:
    price_variance_dollars = round(price_diff * line.quantity, 2) if status == "price_variance" else 0.0
    qty_variance_dollars = (
        round(qty_diff * (po_line.unit_cost or 0.0), 2) if status == "qty_variance" and po_line else 0.0
    )
    return {
        "line_id": line.id,
        "description": line.description,
        "invoice_qty": line.quantity,
        "invoice_unit_price": line.unit_price,
        "po_line_id": po_line.id if po_line else None,
        "po_quantity_received": po_line.quantity_received if po_line else None,
        "po_unit_cost": po_line.unit_cost if po_line else None,
        "match_status": status,
        "qty_diff": round(qty_diff, 3) if po_line else None,
        "price_diff": round(price_diff, 4) if po_line else None,
        "price_variance_dollars": price_variance_dollars,
        "qty_variance_dollars": qty_variance_dollars,
    }


# ---------- Costing cascade ----------

def apply_invoice_line_costs(db: Session, invoice_id: int) -> List[Dict]:
    """
    Push this invoice's billed unit prices into InventoryItem.current_cost for
    every line that's matched to a real inventory item — e.g. if butter comes
    in on this invoice at a new price, that becomes the item's current cost.

    This is the entire cascade: calculate_recipe_cost always reads
    current_cost live (and recurses into sub-recipes), so every batch recipe
    and menu item that uses the item automatically reflects the new price the
    next time its cost is computed — nothing else needs to be recalculated or
    stored. This function just updates the source of truth and reports what
    changed (and which recipes are affected) so the caller can show the user.
    """
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise ValueError("Invoice not found")

    changes: List[Dict] = []
    for line in invoice.lines:
        if not line.item_id or not line.unit_price or line.unit_price <= 0:
            continue
        item = db.get(InventoryItem, line.item_id)
        if not item:
            continue
        factor = get_conversion_factor(db, item, line.unit or item.base_unit, item.base_unit)
        new_cost = round(line.unit_price / factor, 4) if factor else round(line.unit_price, 4)
        old_cost = item.current_cost or 0.0
        if abs(new_cost - old_cost) <= COST_CHANGE_TOLERANCE:
            continue
        item.current_cost = new_cost
        pct_change = ((new_cost - old_cost) / old_cost * 100) if old_cost else None
        changes.append({
            "item_id": item.id,
            "item_name": item.name,
            "old_cost": round(old_cost, 4),
            "new_cost": new_cost,
            "pct_change": round(pct_change, 1) if pct_change is not None else None,
            "affected_recipes": find_recipes_using_item(db, item.id),
        })
    db.flush()
    return changes


# ---------- Status transitions ----------

def approve_invoice(db: Session, invoice_id: int) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise ValueError("Invoice not found")
    if inv.status not in (InvoiceStatus.MATCHED.value, InvoiceStatus.EXCEPTION.value, InvoiceStatus.RECEIVED.value):
        raise ValueError(f"Cannot approve an invoice in status '{inv.status}'")
    inv.status = InvoiceStatus.APPROVED.value
    # Approval is the trigger point for pushing this invoice's prices into
    # inventory — same idea as PO receiving being the trigger for
    # receive_po_line — so unreviewed/OCR-captured pricing never silently
    # overwrites live costs before a manager has signed off on the invoice.
    # `cost_changes` is a plain (non-persisted) attribute for the caller to
    # read back immediately after this call — it isn't a mapped column.
    inv.cost_changes = apply_invoice_line_costs(db, invoice_id)
    db.flush()
    return inv


def mark_paid(db: Session, invoice_id: int) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise ValueError("Invoice not found")
    if inv.status != InvoiceStatus.APPROVED.value:
        raise ValueError("Only approved invoices can be marked paid")
    inv.status = InvoiceStatus.PAID.value
    db.flush()
    return inv


def reject_invoice(db: Session, invoice_id: int, reason: str | None = None) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise ValueError("Invoice not found")
    inv.status = InvoiceStatus.REJECTED.value
    if reason:
        inv.notes = f"{inv.notes}\nRejected: {reason}" if inv.notes else f"Rejected: {reason}"
    db.flush()
    return inv


# ---------- AI extraction: matching + orchestration ----------

def find_vendor_by_name(db: Session, name: str) -> Optional[Vendor]:
    """Best-effort match of an OCR'd vendor name to an existing Vendor:
    exact (case-insensitive) match first, then a loose substring match."""
    if not name or not name.strip():
        return None
    name = name.strip()
    exact = db.query(Vendor).filter(Vendor.name.ilike(name)).first()
    if exact:
        return exact
    like = db.query(Vendor).filter(Vendor.name.ilike(f"%{name}%")).first()
    if like:
        return like
    # try the reverse: an existing vendor name contained in the OCR'd name
    for v in db.query(Vendor).filter(Vendor.is_active == True).all():
        if v.name.lower() in name.lower():
            return v
    return None


def find_item_by_name_or_sku(db: Session, name: str | None, sku: str | None) -> Optional[InventoryItem]:
    """Best-effort match of an OCR'd line item to an existing InventoryItem:
    SKU match wins (exact, case-insensitive), then falls back to name."""
    if sku and sku.strip():
        by_sku = db.query(InventoryItem).filter(InventoryItem.sku.ilike(sku.strip())).first()
        if by_sku:
            return by_sku
    if name and name.strip():
        exact = db.query(InventoryItem).filter(InventoryItem.name.ilike(name.strip())).first()
        if exact:
            return exact
        like = db.query(InventoryItem).filter(InventoryItem.name.ilike(f"%{name.strip()}%")).first()
        if like:
            return like
    return None


def create_invoice_from_extraction(
    db: Session,
    extraction: Dict,
    vendor_id: int,
    location_id: int,
    raw_json: str | None = None,
    original_filename: str | None = None,
    purchase_order_id: int | None = None,
) -> Invoice:
    """
    Build an Invoice + InvoiceLines from a (user-reviewed) AI extraction dict:
      {
        "invoice_number": str|None, "invoice_date": "YYYY-MM-DD"|None,
        "due_date": "YYYY-MM-DD"|None, "invoice_total": float|None,
        "line_items": [
          {"item_name": str, "sku": str|None,
           "quantity_ordered": float|None, "quantity_shipped": float|None,
           "unit": str|None, "unit_price": float|None, "line_total": float|None},
          ...
        ]
      }
    Each line is auto-matched to an existing InventoryItem by SKU/name when
    possible (used later for PO matching); unmatched lines are still saved
    as free-text so nothing gets dropped.
    """
    def _parse_date(s):
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    inv = create_invoice(
        db,
        vendor_id=vendor_id,
        location_id=location_id,
        purchase_order_id=purchase_order_id,
        invoice_number=extraction.get("invoice_number"),
        invoice_date=_parse_date(extraction.get("invoice_date")),
        due_date=_parse_date(extraction.get("due_date")),
        total_amount=extraction.get("invoice_total"),
        original_filename=original_filename,
        ai_extraction_raw=raw_json,
    )

    for row in extraction.get("line_items", []):
        name = (row.get("item_name") or "Line item").strip()
        sku = row.get("sku")
        item = find_item_by_name_or_sku(db, name, sku)
        qty_shipped = row.get("quantity_shipped")
        qty_ordered = row.get("quantity_ordered")
        # Bill on shipped qty when present; fall back to ordered qty so we
        # never silently drop a line just because one field was blank.
        quantity = qty_shipped if qty_shipped not in (None, 0) else (qty_ordered or 0)
        unit_price = row.get("unit_price") or 0.0
        add_invoice_line(
            db,
            invoice_id=inv.id,
            description=name,
            quantity=quantity,
            unit_price=unit_price,
            unit=row.get("unit"),
            item_id=item.id if item else None,
            sku=sku,
            quantity_ordered=qty_ordered,
        )

    db.flush()
    return inv


# ---------- Reads ----------

def list_invoices(
    db: Session,
    location_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
) -> List[Invoice]:
    q = db.query(Invoice).options(joinedload(Invoice.vendor)).order_by(Invoice.created_at.desc())
    if location_id:
        q = q.filter(Invoice.location_id == location_id)
    if status:
        q = q.filter(Invoice.status == status)
    return q.limit(limit).all()


def get_invoice_summary(db: Session, invoice_id: int) -> Dict:
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.vendor), joinedload(Invoice.purchase_order))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    if not inv:
        return {}
    lines = []
    for line in inv.lines:
        lines.append({
            "id": line.id,
            "description": line.description,
            "sku": line.sku,
            "quantity": line.quantity,
            "quantity_ordered": line.quantity_ordered,
            "unit": line.unit,
            "unit_price": line.unit_price,
            "line_total": line.line_total,
            "match_status": line.match_status,
            "item_id": line.item_id,
            "item_name": line.item.name if line.item else None,
            "purchase_order_line_id": line.purchase_order_line_id,
        })
    return {
        "id": inv.id,
        "invoice_number": inv.invoice_number,
        "vendor": inv.vendor.name if inv.vendor else "",
        "vendor_id": inv.vendor_id,
        "location_id": inv.location_id,
        "purchase_order_id": inv.purchase_order_id,
        "po_number": inv.purchase_order.po_number if inv.purchase_order else None,
        "invoice_date": inv.invoice_date,
        "due_date": inv.due_date,
        "total_amount": inv.total_amount,
        "status": inv.status,
        "notes": inv.notes,
        "lines": lines,
    }


def ap_aging_summary(db: Session, location_id: int | None = None) -> Dict:
    """Simple AP snapshot: totals owed by status, useful for a financials view."""
    q = db.query(Invoice)
    if location_id:
        q = q.filter(Invoice.location_id == location_id)
    invoices = q.all()
    by_status: Dict[str, float] = {}
    for inv in invoices:
        by_status[inv.status] = round(by_status.get(inv.status, 0.0) + (inv.total_amount or 0.0), 2)
    open_statuses = (
        InvoiceStatus.RECEIVED.value, InvoiceStatus.MATCHED.value,
        InvoiceStatus.EXCEPTION.value, InvoiceStatus.APPROVED.value,
    )
    total_open = round(sum(by_status.get(s, 0.0) for s in open_statuses), 2)
    return {
        "by_status": by_status,
        "total_open_payable": total_open,
        "invoice_count": len(invoices),
        "exception_count": sum(1 for i in invoices if i.status == InvoiceStatus.EXCEPTION.value),
    }
