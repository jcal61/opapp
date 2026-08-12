"""
Sales Report Import — periodic sales-summary exports (e.g. Toast's "All
Levels" menu sales report: one row per menu item with quantity sold and
revenue for a reporting period, no per-transaction detail).

This is deliberately separate from POSSale/POSSaleLine, which model
individual transactions and drive live theoretical-inventory depletion.
A sales-summary report has no transaction timestamps to depute against —
its job is retrospective analysis: cross-referencing what was actually
sold (and for how much) against this app's live recipe costing and actual
ingredient purchases, to produce a real (not Toast-reported-blank) COGS
and a menu-engineering view of the menu.
"""

from datetime import datetime, date, timezone
from sqlalchemy import String, Float, ForeignKey, DateTime, Date
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class SalesReportImport(Base):
    """One uploaded sales-summary file (a header row plus its parsed lines)."""
    __tablename__ = "sales_report_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(30), default="toast")
    original_filename: Mapped[str | None] = mapped_column(String(255))
    period_label: Mapped[str | None] = mapped_column(String(120))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Grand-total figures straight off the report's own summary row, kept for
    # reconciliation (does our parsed-line total match what Toast reported?).
    reported_net_sales: Mapped[float | None] = mapped_column(Float)
    reported_gross_sales: Mapped[float | None] = mapped_column(Float)
    reported_tax: Mapped[float | None] = mapped_column(Float)
    reported_qty_sold: Mapped[float | None] = mapped_column(Float)

    location = relationship("Location")
    lines = relationship("SalesReportLine", back_populates="report_import", cascade="all, delete-orphan")


class SalesReportLine(Base):
    """One menu-item row from the sales-summary report."""
    __tablename__ = "sales_report_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_import_id: Mapped[int] = mapped_column(ForeignKey("sales_report_imports.id"), nullable=False)
    recipe_id: Mapped[int | None] = mapped_column(ForeignKey("recipes.id"))

    row_type: Mapped[str | None] = mapped_column(String(30))     # "menuItem", "giftCard", etc.
    menu: Mapped[str | None] = mapped_column(String(120))        # Full Service, Drinks, Catering, Online Ordering…
    menu_group: Mapped[str | None] = mapped_column(String(120))  # menu section, e.g. Plates, Sandwiches, House Cocktails
    subgroup: Mapped[str | None] = mapped_column(String(120))
    item_name: Mapped[str] = mapped_column(String(200), nullable=False)

    qty_sold: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float | None] = mapped_column(Float)
    gross_amt: Mapped[float] = mapped_column(Float, default=0.0)
    discount_amt: Mapped[float] = mapped_column(Float, default=0.0)
    refund_amt: Mapped[float] = mapped_column(Float, default=0.0)
    void_amt: Mapped[float] = mapped_column(Float, default=0.0)
    net_amt: Mapped[float] = mapped_column(Float, default=0.0)
    tax_amt: Mapped[float] = mapped_column(Float, default=0.0)

    report_import = relationship("SalesReportImport", back_populates="lines")
    recipe = relationship("Recipe")
