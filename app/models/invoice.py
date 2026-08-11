"""
Accounts Payable: vendor invoices captured against (optional) purchase orders,
with 3-way match support (PO vs Receiving vs Invoice).
"""

from datetime import datetime, date, timezone
from sqlalchemy import String, Float, ForeignKey, DateTime, Date, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum
from app.database import Base


class InvoiceStatus(str, enum.Enum):
    RECEIVED = "received"          # captured, not yet matched
    MATCHED = "matched"            # all lines matched cleanly to PO
    EXCEPTION = "exception"        # one or more lines have qty/price variance or are unmatched
    APPROVED = "approved"          # manager approved despite/after reviewing exceptions
    PAID = "paid"
    REJECTED = "rejected"


class LineMatchStatus(str, enum.Enum):
    MATCHED = "matched"
    QTY_VARIANCE = "qty_variance"
    PRICE_VARIANCE = "price_variance"
    UNMATCHED = "unmatched"


class Invoice(Base):
    """A vendor invoice (AP capture), optionally matched to a PurchaseOrder."""
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_number: Mapped[str | None] = mapped_column(String(80))
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    purchase_order_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"))

    invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    total_amount: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default=InvoiceStatus.RECEIVED.value)
    notes: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    vendor = relationship("Vendor")
    location = relationship("Location")
    purchase_order = relationship("PurchaseOrder")
    lines = relationship("InvoiceLine", back_populates="invoice", cascade="all, delete-orphan")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_items.id"))
    purchase_order_line_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_order_lines.id"))

    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(30))
    unit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    line_total: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gl_code: Mapped[str | None] = mapped_column(String(30))
    match_status: Mapped[str | None] = mapped_column(String(30))

    invoice = relationship("Invoice", back_populates="lines")
    item = relationship("InventoryItem")
    purchase_order_line = relationship("PurchaseOrderLine")
