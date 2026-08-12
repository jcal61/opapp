"""
Financial back-office models, inspired by Restaurant365's Accounting suite.

This is deliberately *not* a general ledger / chart-of-accounts / double-entry
system — that's a much bigger build than fits this app's scope. Instead it's
the operator-facing profitability layer R365 customers actually check day to
day: a P&L built from data this app already has (POS sales, live recipe
costs, scheduled labor) plus two small new inputs — operating expenses below
the food/labor line, and budget targets to compare against. Cash drawer
counts cover the daily cash-management habit R365 also tracks.
"""

from __future__ import annotations
from datetime import datetime, date, timezone
from sqlalchemy import String, Float, ForeignKey, DateTime, Date, Text, Integer, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


EXPENSE_CATEGORIES = [
    "Rent & Occupancy", "Utilities", "Repairs & Maintenance", "Marketing",
    "Insurance", "Admin & Office", "Bank & Merchant Fees", "Licenses & Fees",
    "Technology & Software", "Other",
]


class Expense(Base):
    """A non-inventory operating expense (rent, utilities, marketing, ...).
    Inventory/COGS spend is already captured via Invoices/PurchaseOrders —
    this is everything below the food-and-labor line on a restaurant P&L."""
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    category: Mapped[str] = mapped_column(String(60), default="Other")
    description: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    expense_date: Mapped[date] = mapped_column(Date, default=date.today)
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    location = relationship("Location")
    vendor = relationship("Vendor")


class Budget(Base):
    """A monthly budget target per location: sales + cost-percent targets.
    Actuals are computed live (via app.services.financials.budget_vs_actual)
    from POS sales, recipe costs, scheduled labor, and Expense rows — this
    table only stores the targets a manager sets."""
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("location_id", "period_year", "period_month", name="uq_budget_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_month: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-12
    sales_target: Mapped[float] = mapped_column(Float, default=0.0)
    food_cost_pct_target: Mapped[float] = mapped_column(Float, default=30.0)
    labor_cost_pct_target: Mapped[float] = mapped_column(Float, default=30.0)
    other_expense_target: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text)

    location = relationship("Location")


class CashDrawerCount(Base):
    """End-of-shift cash drawer reconciliation. `expected_amount` is what the
    till should have (manager-entered — starting bank + cash sales minus
    drops, however this operator tracks it); `counted_amount` is what was
    physically counted. `over_short` is stored (not just derived) so the
    trend can be reported without recomputing it from potentially-edited
    history."""
    __tablename__ = "cash_drawer_counts"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, default=date.today)
    shift_label: Mapped[str] = mapped_column(String(30), default="Close")
    counted_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    expected_amount: Mapped[float] = mapped_column(Float, default=0.0)
    counted_amount: Mapped[float] = mapped_column(Float, default=0.0)
    over_short: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text)
    counted_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    location = relationship("Location")
    counted_by = relationship("User")
