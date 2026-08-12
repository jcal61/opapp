"""
Financial reporting — a lightweight operating P&L / prime cost view, budget
vs. actual, cash drawer management, and multi-location consolidated
reporting, in the spirit of Restaurant365's Accounting module but built
entirely from data this app already tracks (POS sales, live recipe costs,
scheduled labor) plus two small new inputs (Expense, Budget).

This is intentionally not a general ledger — no chart of accounts, no
double-entry, no bank feeds. It's the day-to-day profitability view an
operator actually watches: sales, theoretical food cost, scheduled labor
cost, other operating expenses, prime cost, and an estimated operating
income.
"""

from __future__ import annotations
import calendar
from collections import defaultdict
from datetime import datetime, date, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import Expense, Budget, CashDrawerCount, Location, POSSale
from app.services.costing import calculate_recipe_cost
from app.services.scheduling import scheduled_labor_cost


def _as_date(d) -> date:
    return d.date() if isinstance(d, datetime) else d


# ---------- Expenses ----------

def create_expense(
    db: Session,
    location_id: int,
    category: str,
    amount: float,
    expense_date: Optional[date] = None,
    description: Optional[str] = None,
    vendor_id: Optional[int] = None,
    is_recurring: bool = False,
    notes: Optional[str] = None,
) -> Expense:
    if amount is None or amount < 0:
        raise ValueError("Amount must be zero or greater.")
    exp = Expense(
        location_id=location_id,
        vendor_id=vendor_id,
        category=(category or "Other").strip() or "Other",
        description=(description or "").strip() or None,
        amount=float(amount),
        expense_date=expense_date or date.today(),
        is_recurring=bool(is_recurring),
        notes=(notes or "").strip() or None,
    )
    db.add(exp)
    db.commit()
    db.refresh(exp)
    return exp


def delete_expense(db: Session, expense_id: int) -> None:
    exp = db.get(Expense, expense_id)
    if exp:
        db.delete(exp)
        db.commit()


def list_expenses(
    db: Session, location_id: int, start: date, end: date, category: Optional[str] = None
) -> List[Expense]:
    start, end = _as_date(start), _as_date(end)
    q = db.query(Expense).filter(
        Expense.location_id == location_id,
        Expense.expense_date >= start,
        Expense.expense_date <= end,
    )
    if category:
        q = q.filter(Expense.category == category)
    return q.order_by(Expense.expense_date.desc(), Expense.id.desc()).all()


# ---------- Budgets ----------

def upsert_budget(
    db: Session,
    location_id: int,
    period_year: int,
    period_month: int,
    sales_target: float = 0.0,
    food_cost_pct_target: float = 30.0,
    labor_cost_pct_target: float = 30.0,
    other_expense_target: float = 0.0,
    notes: Optional[str] = None,
) -> Budget:
    b = (
        db.query(Budget)
        .filter(
            Budget.location_id == location_id,
            Budget.period_year == period_year,
            Budget.period_month == period_month,
        )
        .first()
    )
    if not b:
        b = Budget(location_id=location_id, period_year=period_year, period_month=period_month)
        db.add(b)
    b.sales_target = float(sales_target or 0.0)
    b.food_cost_pct_target = float(food_cost_pct_target or 0.0)
    b.labor_cost_pct_target = float(labor_cost_pct_target or 0.0)
    b.other_expense_target = float(other_expense_target or 0.0)
    b.notes = (notes or "").strip() or None
    db.commit()
    db.refresh(b)
    return b


def get_budget(db: Session, location_id: int, period_year: int, period_month: int) -> Optional[Budget]:
    return (
        db.query(Budget)
        .filter(
            Budget.location_id == location_id,
            Budget.period_year == period_year,
            Budget.period_month == period_month,
        )
        .first()
    )


def list_budgets(db: Session, location_id: int) -> List[Budget]:
    return (
        db.query(Budget)
        .filter(Budget.location_id == location_id)
        .order_by(Budget.period_year.desc(), Budget.period_month.desc())
        .all()
    )


# ---------- Cash management ----------

def record_cash_count(
    db: Session,
    location_id: int,
    business_date: date,
    shift_label: str,
    expected_amount: float,
    counted_amount: float,
    counted_by_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> CashDrawerCount:
    over_short = round((counted_amount or 0.0) - (expected_amount or 0.0), 2)
    c = CashDrawerCount(
        location_id=location_id,
        business_date=business_date or date.today(),
        shift_label=(shift_label or "Close").strip() or "Close",
        counted_by_id=counted_by_id,
        expected_amount=float(expected_amount or 0.0),
        counted_amount=float(counted_amount or 0.0),
        over_short=over_short,
        notes=(notes or "").strip() or None,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def list_cash_counts(db: Session, location_id: int, start: date, end: date) -> List[CashDrawerCount]:
    start, end = _as_date(start), _as_date(end)
    return (
        db.query(CashDrawerCount)
        .filter(
            CashDrawerCount.location_id == location_id,
            CashDrawerCount.business_date >= start,
            CashDrawerCount.business_date <= end,
        )
        .order_by(CashDrawerCount.business_date.desc(), CashDrawerCount.id.desc())
        .all()
    )


def cash_variance_summary(db: Session, location_id: int, start: date, end: date) -> Dict[str, Any]:
    counts = list_cash_counts(db, location_id, start, end)
    total_over_short = sum(c.over_short for c in counts)
    total_short = sum(c.over_short for c in counts if c.over_short < 0)
    total_over = sum(c.over_short for c in counts if c.over_short > 0)
    return {
        "count": len(counts),
        "total_over_short": round(total_over_short, 2),
        "total_short": round(total_short, 2),
        "total_over": round(total_over, 2),
        "counts": counts,
    }


# ---------- Operating P&L / prime cost ----------

def _sales_and_theoretical_cogs(db: Session, location_id: int, start: datetime, end: datetime) -> Dict[str, float]:
    """Sales total and theoretical COGS (recipe cost of what was actually
    rung on the POS, valued at current recipe cost) for a datetime window."""
    sales_rows = (
        db.query(POSSale)
        .filter(POSSale.location_id == location_id, POSSale.sold_at >= start, POSSale.sold_at < end)
        .all()
    )
    total_sales = sum(s.total_amount or 0.0 for s in sales_rows)

    cost_cache: Dict[int, float] = {}
    cogs = 0.0
    for sale in sales_rows:
        for line in sale.lines:
            if not line.recipe_id:
                continue
            if line.recipe_id not in cost_cache:
                try:
                    result = calculate_recipe_cost(db, line.recipe_id)
                    cost_cache[line.recipe_id] = result.cost_per_unit
                except Exception:
                    cost_cache[line.recipe_id] = 0.0
            cogs += cost_cache[line.recipe_id] * (line.quantity or 0.0)

    return {"total_sales": total_sales, "cogs": cogs}


def profit_and_loss(db: Session, location_id: int, start: datetime, end: datetime) -> Dict[str, Any]:
    """
    A lightweight *operating* P&L — not a general ledger, but the numbers a
    restaurant manager actually watches day to day: sales, theoretical food
    cost, scheduled labor cost, and other tracked operating expenses,
    rolling up to prime cost and an estimated operating income.
    """
    base = _sales_and_theoretical_cogs(db, location_id, start, end)
    total_sales = base["total_sales"]
    cogs = base["cogs"]

    labor = scheduled_labor_cost(db, location_id, start, end)
    labor_cost = labor["total_cost"]

    expenses = list_expenses(db, location_id, _as_date(start), _as_date(end))
    other_total = sum(e.amount for e in expenses)
    by_category: Dict[str, float] = defaultdict(float)
    for e in expenses:
        by_category[e.category] += e.amount

    prime_cost = cogs + labor_cost
    total_expenses = prime_cost + other_total
    operating_income = total_sales - total_expenses

    def pct(x: float) -> Optional[float]:
        return round(x / total_sales * 100, 1) if total_sales else None

    return {
        "location_id": location_id,
        "start": start,
        "end": end,
        "total_sales": round(total_sales, 2),
        "cogs": round(cogs, 2),
        "cogs_pct": pct(cogs),
        "labor_cost": round(labor_cost, 2),
        "labor_pct": pct(labor_cost),
        "prime_cost": round(prime_cost, 2),
        "prime_cost_pct": pct(prime_cost),
        "other_expenses": round(other_total, 2),
        "other_expenses_by_category": {k: round(v, 2) for k, v in sorted(by_category.items())},
        "total_expenses": round(total_expenses, 2),
        "operating_income": round(operating_income, 2),
        "operating_income_pct": pct(operating_income),
    }


def budget_vs_actual(db: Session, location_id: int, period_year: int, period_month: int) -> Dict[str, Any]:
    """Compares a stored Budget target for the month against the actual P&L
    computed live for that same calendar month."""
    budget = get_budget(db, location_id, period_year, period_month)
    days_in_month = calendar.monthrange(period_year, period_month)[1]
    start = datetime(period_year, period_month, 1, tzinfo=timezone.utc)
    end = datetime(period_year, period_month, days_in_month, 23, 59, 59, tzinfo=timezone.utc)
    actual = profit_and_loss(db, location_id, start, end)

    if not budget:
        return {"budget": None, "actual": actual, "variance": None}

    budgeted_food_cost = budget.sales_target * (budget.food_cost_pct_target / 100.0)
    budgeted_labor_cost = budget.sales_target * (budget.labor_cost_pct_target / 100.0)

    return {
        "budget": {
            "sales_target": budget.sales_target,
            "food_cost_pct_target": budget.food_cost_pct_target,
            "labor_cost_pct_target": budget.labor_cost_pct_target,
            "other_expense_target": budget.other_expense_target,
            "budgeted_food_cost": round(budgeted_food_cost, 2),
            "budgeted_labor_cost": round(budgeted_labor_cost, 2),
        },
        "actual": actual,
        "variance": {
            "sales_variance": round(actual["total_sales"] - budget.sales_target, 2),
            "food_cost_variance": round(actual["cogs"] - budgeted_food_cost, 2),
            "labor_cost_variance": round(actual["labor_cost"] - budgeted_labor_cost, 2),
            "other_expense_variance": round(actual["other_expenses"] - budget.other_expense_target, 2),
        },
    }


# ---------- Multi-location consolidated reporting ----------

def location_family_ids(db: Session, root_location_id: int) -> List[int]:
    """The root location plus every descendant, walking Location.parent_id —
    reuses the parent/child hierarchy already modeled on Location so a
    corporate/region node can roll up all of its child locations."""
    all_locs = db.query(Location).all()
    children_map: Dict[Optional[int], List[int]] = defaultdict(list)
    for loc in all_locs:
        children_map[loc.parent_id].append(loc.id)

    ids = [root_location_id]
    frontier = [root_location_id]
    while frontier:
        nxt: List[int] = []
        for lid in frontier:
            nxt.extend(children_map.get(lid, []))
        ids.extend(nxt)
        frontier = nxt
    return ids


def consolidated_pl(db: Session, root_location_id: int, start: datetime, end: datetime) -> Dict[str, Any]:
    """Per-location P&L breakdown plus a combined total across a location and
    all of its descendants — the multi-unit consolidated report R365 leads
    with for restaurant groups."""
    ids = location_family_ids(db, root_location_id)
    by_location = []
    totals = {
        "total_sales": 0.0, "cogs": 0.0, "labor_cost": 0.0, "other_expenses": 0.0,
        "prime_cost": 0.0, "total_expenses": 0.0, "operating_income": 0.0,
    }
    for lid in ids:
        loc = db.get(Location, lid)
        pl = profit_and_loss(db, lid, start, end)
        by_location.append({"location_id": lid, "location_name": loc.name if loc else f"#{lid}", **pl})
        for k in totals:
            totals[k] += pl[k]

    total_sales = totals["total_sales"]

    def pct(x: float) -> Optional[float]:
        return round(x / total_sales * 100, 1) if total_sales else None

    totals["cogs_pct"] = pct(totals["cogs"])
    totals["labor_pct"] = pct(totals["labor_cost"])
    totals["prime_cost_pct"] = pct(totals["prime_cost"])
    totals["operating_income_pct"] = pct(totals["operating_income"])
    for k in ("total_sales", "cogs", "labor_cost", "other_expenses", "prime_cost", "total_expenses", "operating_income"):
        totals[k] = round(totals[k], 2)

    return {"locations": by_location, "totals": totals, "location_ids": ids}
