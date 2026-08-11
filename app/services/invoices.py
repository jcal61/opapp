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
    PurchaseOrder, PurchaseOrderLine, InventoryItem,
)

QTY_TOLERANCE = 0.01          # base units of slack before flagging qty variance
PRICE_TOLERANCE_PCT = 0.02    # 2% price drift tolerated before flagging


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
) -> InvoiceLine:
    line = InvoiceLine(
        invoice_id=invoice_id,
        item_id=item_id,
        purchase_order_line_id=purchase_order_line_id,
        description=description,
        quantity=quantity,
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


# ---------- Status transitions ----------

def approve_invoice(db: Session, invoice_id: int) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise ValueError("Invoice not found")
    if inv.status not in (InvoiceStatus.MATCHED.value, InvoiceStatus.EXCEPTION.value, InvoiceStatus.RECEIVED.value):
        raise ValueError(f"Cannot approve an invoice in status '{inv.status}'")
    inv.status = InvoiceStatus.APPROVED.value
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
            "quantity": line.quantity,
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
