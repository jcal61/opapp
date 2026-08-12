"""
Sales report ingestion — parses a Toast (or Toast-shaped) menu sales-summary
export like the "All Levels" report: one row per menu item/gift card/open
item with quantity sold and revenue for a reporting period.

This is intentionally separate from toast_import.py, which ingests
per-transaction order data and drives live theoretical-inventory depletion
(POSSale/POSSaleLine). A sales-summary report has no transaction timestamps
to deplete against; its value is retrospective — feeding menu_analysis.py to
derive a real COGS and menu-engineering view from data this app already has
(live recipe costing, invoice-driven ingredient prices).
"""

from __future__ import annotations
import io
from datetime import date
from typing import Dict, List, Optional, Tuple
import pandas as pd
from sqlalchemy.orm import Session, joinedload

from app.models import SalesReportImport, SalesReportLine
from app.services.recipes import find_recipe_by_name


class SalesImportParseError(Exception):
    pass


# ---------- Parsing ----------

def _find_col(columns, *candidates: str) -> Optional[str]:
    norm = {str(c).strip().lower(): c for c in columns}
    for cand in candidates:
        key = cand.strip().lower()
        if key in norm:
            return norm[key]
    return None


def _num(v) -> float:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0.0
    try:
        if pd.isna(v):
            return 0.0
    except (TypeError, ValueError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _text(v) -> Optional[str]:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s or None


def parse_toast_sales_csv(file_bytes: bytes) -> Tuple[List[Dict], Dict]:
    """
    Parse a Toast "All levels" style menu sales-summary export.

    Returns (rows, report_totals):
      rows           - one dict per menu item / gift card / open-item line,
                        each with row_type, menu, menu_group, subgroup,
                        item_name, qty_sold, avg_price, gross_amt,
                        discount_amt, refund_amt, void_amt, net_amt, tax_amt.
      report_totals  - the report's own grand-total row (qty/gross/net
                        sales, tax) if present, for a reconciliation check
                        against the sum of the parsed rows.
    """
    try:
        df = pd.read_csv(io.BytesIO(file_bytes), dtype=str)
    except Exception as e:
        raise SalesImportParseError(f"Couldn't read this file as a CSV: {e}")

    col_type = _find_col(df.columns, "Type")
    col_menu = _find_col(df.columns, "Menu")
    col_group = _find_col(df.columns, "Menu group")
    col_subgroup = _find_col(df.columns, "Subgroup")
    col_item = _find_col(df.columns, "Item, open item", "Item", "Item name", "Menu Item")
    col_qty = _find_col(df.columns, "Qty sold", "Quantity", "Qty")
    col_avg_price = _find_col(df.columns, "Avg. price", "Avg price", "Average price")
    col_gross = _find_col(df.columns, "Gross item amt", "Gross sales")
    col_discount = _find_col(df.columns, "Discount amt")
    col_refund = _find_col(df.columns, "Refund amt")
    col_void = _find_col(df.columns, "Void amt")
    col_net = _find_col(df.columns, "Net item amt", "Net sales")
    col_tax = _find_col(df.columns, "Tax amt")

    if not col_item or not col_qty:
        raise SalesImportParseError(
            "This doesn't look like a Toast menu sales report — couldn't find an item "
            "name column and a quantity-sold column. Expected headers like "
            "'Item, open item' and 'Qty sold' (the Toast \"All Levels\" sales export)."
        )

    rows: List[Dict] = []
    report_totals = {"qty_sold": 0.0, "gross_sales": 0.0, "net_sales": 0.0, "tax": 0.0}

    for _, r in df.iterrows():
        item_name = _text(r.get(col_item))
        row_type = _text(r.get(col_type)) if col_type else None

        if not item_name and not row_type:
            # The report's own grand-total row: blank Type and item name,
            # but real aggregate figures in the numeric columns.
            has_numbers = col_qty and _text(r.get(col_qty)) is not None
            if has_numbers:
                report_totals["qty_sold"] = _num(r.get(col_qty))
                report_totals["gross_sales"] = _num(r.get(col_gross)) if col_gross else 0.0
                report_totals["net_sales"] = _num(r.get(col_net)) if col_net else 0.0
                report_totals["tax"] = _num(r.get(col_tax)) if col_tax else 0.0
            continue

        if not item_name:
            continue  # stray blank row

        rows.append({
            "row_type": row_type or "menuItem",
            "menu": _text(r.get(col_menu)) if col_menu else None,
            "menu_group": _text(r.get(col_group)) if col_group else None,
            "subgroup": _text(r.get(col_subgroup)) if col_subgroup else None,
            "item_name": item_name,
            "qty_sold": _num(r.get(col_qty)),
            "avg_price": _num(r.get(col_avg_price)) if col_avg_price else None,
            "gross_amt": _num(r.get(col_gross)) if col_gross else 0.0,
            "discount_amt": _num(r.get(col_discount)) if col_discount else 0.0,
            "refund_amt": _num(r.get(col_refund)) if col_refund else 0.0,
            "void_amt": _num(r.get(col_void)) if col_void else 0.0,
            "net_amt": _num(r.get(col_net)) if col_net else 0.0,
            "tax_amt": _num(r.get(col_tax)) if col_tax else 0.0,
        })

    if not rows:
        raise SalesImportParseError("No item rows were found in this file.")

    return rows, report_totals


# ---------- Import (persist + auto-match to recipes) ----------

def create_sales_report_import(
    db: Session,
    location_id: int,
    rows: List[Dict],
    report_totals: Optional[Dict] = None,
    filename: Optional[str] = None,
    period_label: Optional[str] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    source: str = "toast",
) -> SalesReportImport:
    report_totals = report_totals or {}
    batch = SalesReportImport(
        location_id=location_id,
        source=source,
        original_filename=filename,
        period_label=(period_label or "").strip() or None,
        period_start=period_start,
        period_end=period_end,
        reported_qty_sold=report_totals.get("qty_sold"),
        reported_gross_sales=report_totals.get("gross_sales"),
        reported_net_sales=report_totals.get("net_sales"),
        reported_tax=report_totals.get("tax"),
    )
    db.add(batch)
    db.flush()

    for row in rows:
        recipe = None
        if row.get("row_type", "menuItem") == "menuItem":
            recipe = find_recipe_by_name(db, row["item_name"])
        db.add(SalesReportLine(
            report_import_id=batch.id,
            recipe_id=recipe.id if recipe else None,
            row_type=row.get("row_type"),
            menu=row.get("menu"),
            menu_group=row.get("menu_group"),
            subgroup=row.get("subgroup"),
            item_name=row["item_name"],
            qty_sold=row.get("qty_sold") or 0.0,
            avg_price=row.get("avg_price"),
            gross_amt=row.get("gross_amt") or 0.0,
            discount_amt=row.get("discount_amt") or 0.0,
            refund_amt=row.get("refund_amt") or 0.0,
            void_amt=row.get("void_amt") or 0.0,
            net_amt=row.get("net_amt") or 0.0,
            tax_amt=row.get("tax_amt") or 0.0,
        ))
    db.commit()
    db.refresh(batch)
    return batch


def list_sales_report_imports(db: Session, location_id: int, limit: int = 50) -> List[SalesReportImport]:
    return (
        db.query(SalesReportImport)
        .filter(SalesReportImport.location_id == location_id)
        .order_by(SalesReportImport.imported_at.desc())
        .limit(limit)
        .all()
    )


def get_sales_report_import(db: Session, import_id: int) -> Optional[SalesReportImport]:
    return (
        db.query(SalesReportImport)
        .options(joinedload(SalesReportImport.lines))
        .filter(SalesReportImport.id == import_id)
        .first()
    )


def delete_sales_report_import(db: Session, import_id: int) -> None:
    batch = db.get(SalesReportImport, import_id)
    if batch:
        db.delete(batch)
        db.commit()


def set_line_recipe(db: Session, line_id: int, recipe_id: Optional[int]) -> SalesReportLine:
    """Manually (re)map one sales-report line to a recipe — used to fix
    items the auto-matcher couldn't resolve (different naming between Toast
    and the recipe book) without re-uploading the whole file."""
    line = db.get(SalesReportLine, line_id)
    if not line:
        raise ValueError("Sales report line not found")
    line.recipe_id = recipe_id or None
    db.commit()
    return line


def set_lines_recipe_by_item_name(db: Session, report_import_id: int, item_name: str, recipe_id: int) -> int:
    """Map every unmatched line with this exact item name (across every
    channel it sold on — Full Service, Online Ordering, Catering, etc. often
    repeat the same item) to one recipe in a single action. Returns how many
    lines were updated."""
    lines = (
        db.query(SalesReportLine)
        .filter(
            SalesReportLine.report_import_id == report_import_id,
            SalesReportLine.item_name == item_name,
            SalesReportLine.recipe_id.is_(None),
        )
        .all()
    )
    for line in lines:
        line.recipe_id = recipe_id
    if lines:
        db.commit()
    return len(lines)
