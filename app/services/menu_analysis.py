"""
Menu analysis — turns an imported sales report (sales_report.py) into a real
COGS and menu-engineering view by cross-referencing it against this app's
live recipe costing (which itself always reflects current inventory prices,
including anything cascaded in from an approved invoice — see costing.py /
invoices.py). This is the "Toast leaves COGS/Gross margin blank" gap-filler.

Two complementary lenses are produced:
  1. Theoretical (bottom-up): recipe cost/unit x units actually sold, per
     Toast — "what it should have cost if every plate matched its recipe
     exactly." This is get_menu_analysis().
  2. Actual (top-down): dollars actually spent on inventory items via
     approved invoices in the same window — "what was actually bought."
     This is purchases_comparison(). Comparing the two is the same idea as
     the physical-count variance report, just at the P&L level instead of
     the per-item quantity level: a big gap signals over-ordering, waste,
     theft, or portioning drift worth investigating.
"""

from __future__ import annotations
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session

from app.models import SalesReportImport, SalesReportLine, Invoice, InvoiceStatus, InvoiceLine, Recipe
from app.services.sales_report import get_sales_report_import
from app.services.costing import calculate_recipe_cost

PRICE_DRIFT_THRESHOLD_PCT = 10.0  # flag when Toast's realized avg price differs from our menu_price by more than this


def get_menu_analysis(db: Session, import_id: int) -> Dict[str, Any]:
    batch = get_sales_report_import(db, import_id)
    if not batch:
        raise ValueError("Sales report import not found")

    # ---- Aggregate lines by recipe (a matched item may appear on several
    # channels — Full Service, Online Ordering, Catering — as separate rows) ----
    agg: Dict[int, Dict[str, Any]] = {}
    unmatched_agg: Dict[str, Dict[str, Any]] = {}
    non_menu_net_sales = 0.0
    all_lines_net_sales = 0.0

    for line in batch.lines:
        all_lines_net_sales += line.net_amt or 0.0

        if (line.row_type or "menuItem") != "menuItem":
            non_menu_net_sales += line.net_amt or 0.0
            continue

        if line.recipe_id:
            bucket = agg.setdefault(line.recipe_id, {
                "recipe_id": line.recipe_id,
                "recipe_name": line.recipe.name if line.recipe else line.item_name,
                "menu_section": line.menu_group or "Uncategorized",
                "qty_sold": 0.0,
                "net_amt": 0.0,
                "gross_amt": 0.0,
            })
            bucket["qty_sold"] += line.qty_sold or 0.0
            bucket["net_amt"] += line.net_amt or 0.0
            bucket["gross_amt"] += line.gross_amt or 0.0
        else:
            bucket = unmatched_agg.setdefault(line.item_name, {
                "item_name": line.item_name,
                "menu_section": line.menu_group or "Uncategorized",
                "qty_sold": 0.0,
                "net_amt": 0.0,
            })
            bucket["qty_sold"] += line.qty_sold or 0.0
            bucket["net_amt"] += line.net_amt or 0.0

    # ---- Cost each matched recipe (live — reflects current ingredient
    # prices, including anything an approved invoice just cascaded in) ----
    items: List[Dict[str, Any]] = []
    for recipe_id, row in agg.items():
        try:
            result = calculate_recipe_cost(db, recipe_id)
            unit_cost = result.cost_per_unit
        except Exception:
            unit_cost = None

        qty = row["qty_sold"]
        net = row["net_amt"]
        theoretical_cogs = round(unit_cost * qty, 2) if unit_cost is not None else None
        gross_profit = round(net - theoretical_cogs, 2) if theoretical_cogs is not None else None
        food_cost_pct = round(theoretical_cogs / net * 100, 1) if theoretical_cogs is not None and net else None
        contribution_margin = round(gross_profit / qty, 4) if gross_profit is not None and qty else None
        effective_avg_price = round(net / qty, 2) if qty else None

        recipe = db.get(Recipe, recipe_id)
        price_drift = None
        if recipe and recipe.menu_price and effective_avg_price is not None and recipe.menu_price > 0:
            drift_pct = (effective_avg_price - recipe.menu_price) / recipe.menu_price * 100
            if abs(drift_pct) >= PRICE_DRIFT_THRESHOLD_PCT:
                price_drift = round(drift_pct, 1)

        items.append({
            "recipe_id": recipe_id,
            "recipe_name": row["recipe_name"],
            "menu_section": row["menu_section"],
            "qty_sold": round(qty, 2),
            "net_sales": round(net, 2),
            "unit_cost": round(unit_cost, 4) if unit_cost is not None else None,
            "theoretical_cogs": theoretical_cogs,
            "gross_profit": gross_profit,
            "food_cost_pct": food_cost_pct,
            "contribution_margin": contribution_margin,
            "effective_avg_price": effective_avg_price,
            "menu_price_on_file": recipe.menu_price if recipe else None,
            "price_drift_pct": price_drift,
            "quadrant": None,  # filled in below, per section
        })

    # ---- Menu-engineering quadrant, computed per menu section (peer group)
    # against that section's own average popularity and average margin —
    # comparing a $4 side to a $130 catering feast on the same axis isn't
    # meaningful, so each section is judged against its own mix. ----
    by_section: Dict[str, List[Dict]] = defaultdict(list)
    for it in items:
        by_section[it["menu_section"]].append(it)

    for section, section_items in by_section.items():
        priced = [i for i in section_items if i["contribution_margin"] is not None]
        if len(priced) < 2:
            continue
        avg_qty = sum(i["qty_sold"] for i in priced) / len(priced)
        avg_margin = sum(i["contribution_margin"] for i in priced) / len(priced)
        for i in priced:
            popular = i["qty_sold"] >= avg_qty
            profitable = i["contribution_margin"] >= avg_margin
            if popular and profitable:
                i["quadrant"] = "Star"
            elif popular and not profitable:
                i["quadrant"] = "Plowhorse"
            elif not popular and profitable:
                i["quadrant"] = "Puzzle"
            else:
                i["quadrant"] = "Dog"

    items.sort(key=lambda i: (i["menu_section"] or "", -(i["net_sales"] or 0)))

    unmatched_items = sorted(unmatched_agg.values(), key=lambda i: -(i["net_amt"] or 0))

    matched_net_sales = round(sum(i["net_sales"] for i in items), 2)
    total_theoretical_cogs = round(sum(i["theoretical_cogs"] for i in items if i["theoretical_cogs"] is not None), 2)
    total_gross_profit = round(matched_net_sales - total_theoretical_cogs, 2)
    blended_food_cost_pct = round(total_theoretical_cogs / matched_net_sales * 100, 1) if matched_net_sales else None
    unmatched_net_sales = round(sum(i["net_amt"] for i in unmatched_items), 2)

    reported_net_sales = batch.reported_net_sales
    reconciliation_delta = (
        round(all_lines_net_sales - reported_net_sales, 2) if reported_net_sales is not None else None
    )

    return {
        "import_id": batch.id,
        "period_label": batch.period_label,
        "period_start": batch.period_start,
        "period_end": batch.period_end,
        "location_id": batch.location_id,
        "items": items,
        "unmatched_items": unmatched_items,
        "summary": {
            "matched_net_sales": matched_net_sales,
            "unmatched_net_sales": unmatched_net_sales,
            "non_menu_net_sales": round(non_menu_net_sales, 2),
            "total_net_sales": round(all_lines_net_sales, 2),
            "total_theoretical_cogs": total_theoretical_cogs,
            "total_gross_profit": total_gross_profit,
            "blended_food_cost_pct": blended_food_cost_pct,
            "matched_item_count": len(items),
            "unmatched_item_count": len(unmatched_items),
            "reported_net_sales": reported_net_sales,
            "reconciliation_delta": reconciliation_delta,
        },
    }


def purchases_comparison(
    db: Session,
    location_id: int,
    start: date,
    end: date,
    theoretical_cogs: float,
) -> Dict[str, Any]:
    """
    Actual (top-down) COGS cross-check: dollars actually spent on inventory
    items via approved/paid invoices within [start, end], compared against
    the theoretical (bottom-up) COGS computed from a sales report for the
    same window. Only invoice lines matched to a real inventory item are
    counted — a freight or misc line with no item_id isn't a food cost.
    """
    lines = (
        db.query(InvoiceLine)
        .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
        .filter(
            Invoice.location_id == location_id,
            Invoice.status.in_([InvoiceStatus.APPROVED.value, InvoiceStatus.PAID.value]),
            Invoice.invoice_date >= start,
            Invoice.invoice_date <= end,
            InvoiceLine.item_id.isnot(None),
        )
        .all()
    )
    actual_purchases = round(sum(l.line_total or 0.0 for l in lines), 2)
    theoretical_cogs = round(theoretical_cogs or 0.0, 2)
    variance_dollars = round(actual_purchases - theoretical_cogs, 2)
    variance_pct = round(variance_dollars / theoretical_cogs * 100, 1) if theoretical_cogs else None

    return {
        "start": start,
        "end": end,
        "actual_purchases": actual_purchases,
        "theoretical_cogs": theoretical_cogs,
        "variance_dollars": variance_dollars,
        "variance_pct": variance_pct,
        "invoice_line_count": len(lines),
    }
