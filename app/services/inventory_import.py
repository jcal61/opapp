"""
Bulk inventory import from a restaurant's existing spreadsheet.

Written against two real-world formats a customer supplied:
  1. A food/paper/cleaning/merchandise workbook with one sheet per category
     (e.g. "Raw", "CookedPrepped", "Soda", "Paper", "Cleaning", "Merchandise"),
     each laid out as repeating blocks of:
        <section header row, only the item-name column filled>
        <item rows: Item | [Case Size] | [Item Number] | On Hand | Price |
                    Inventory | Inventory Value | [Order Quantity]>
  2. A liquor/beer/wine workbook with a single "Inventory" sheet containing
     several mini-tables (Beer Package, Beer Kegs, Liquor-by-spirit, Wine)
     stacked on top of each other, each with slightly different columns.

Real spreadsheets are messy — section headers, subtotal rows, and repeated
column-header rows are mixed in with the actual data, and the two quantity
columns per line don't always agree (a stale "On Hand" carried over from the
prior count vs. the actual current "Inventory"/"On Hand" for this count).
This module makes a best effort and normalizes everything into a flat list
of dicts; the Streamlit page shows the result in an editable table so a
human can fix or drop any row before anything is written to the database.
"""

from __future__ import annotations
import io
import datetime as dt
from typing import List, Dict, Optional
import pandas as pd

# Size tokens that only make sense as a child of the previous header row
# (used by the Merchandise sheet: "Black GB Staff Shirt" -> "S" / "M" / "L" ...)
_SIZE_TOKENS = {"XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL", "3XL", "OS"}

_TABLE_HEADER_WORDS = {
    "distributor", "size", "par", "on hand", "order", "unit cost",
    "cost", "bottle cost", "cost per pint", "price per pint", "cost %", "rate",
}


class ImportParseError(Exception):
    pass


def detect_workbook_type(file_bytes: bytes) -> str:
    """Returns 'food' or 'liquor' based on sheet names, or raises if neither matches."""
    xl = pd.ExcelFile(io.BytesIO(file_bytes))
    sheets = {s.lower() for s in xl.sheet_names}
    if "raw" in sheets or "cookedprepped" in sheets or "paper" in sheets:
        return "food"
    if "inventory" in sheets and ("summary" in sheets or "contacts" in sheets):
        return "liquor"
    raise ImportParseError(
        "Couldn't recognize this workbook's layout. Expected either a food/supplies "
        "workbook (sheets like Raw, Paper, Cleaning) or a liquor/beer/wine workbook "
        "(a 'Inventory' sheet alongside 'Summary'/'Contacts')."
    )


def _clean(v) -> Optional[str]:
    if pd.isna(v):
        return None
    s = str(v).strip()
    return s or None


def _num(v) -> Optional[float]:
    if pd.isna(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _infer_unit(name: str) -> str:
    """Pull a unit hint from a trailing parenthetical, e.g. 'Brisket (#)' -> 'lb'."""
    low = name.lower()
    if low.endswith("(cs)"):
        return "case"
    if low.endswith("(#)"):
        return "lb"
    if low.endswith("(pc)"):
        return "piece"
    if low.endswith("(ea)"):
        return "each"
    return "case"


# ---------- Food / supplies workbook ----------

_FOOD_SHEET_CATEGORY = {
    "raw": "Raw Food",
    "cookedprepped": "Prepped/Cooked Food",
    "soda": "Soda",
    "paper": "Paper",
    "cleaning": "Cleaning",
    "merchandise": "Merchandise",
}

# Sheets that include Case Size + Item Number columns (8 cols) vs the
# simpler 6-column layout used by CookedPrepped / Soda / Merchandise.
_FOOD_WIDE_SHEETS = {"raw", "paper", "cleaning"}


def parse_gbb_food_workbook(file_bytes: bytes) -> List[Dict]:
    xl = pd.ExcelFile(io.BytesIO(file_bytes))
    rows_out: List[Dict] = []

    for sheet_name in xl.sheet_names:
        key = sheet_name.strip().lower()
        if key not in _FOOD_SHEET_CATEGORY:
            continue
        category = _FOOD_SHEET_CATEGORY[key]
        wide = key in _FOOD_WIDE_SHEETS

        df = xl.parse(sheet_name, header=None)
        current_sub = None

        for i in range(1, len(df)):  # row 0 is the literal column-header row
            row = df.iloc[i].tolist()
            name = _clean(row[0])
            if not name:
                continue
            if name.lower().startswith("total"):
                continue

            if wide:
                case_size, item_no, on_hand, price, inventory = row[1], row[2], row[3], row[4], row[5]
            else:
                on_hand, price, inventory = row[1], row[2], row[3]
                case_size = item_no = None

            is_header_row = all(
                pd.isna(v) for v in (row[1:6] if wide else row[1:4])
            )
            if is_header_row:
                current_sub = name
                continue

            item_name = name
            if key == "merchandise" and name.upper() in _SIZE_TOKENS and current_sub:
                item_name = f"{current_sub} - {name}"

            qty = _num(inventory)
            if qty is None:
                qty = _num(on_hand) or 0.0
            cost = _num(price) or 0.0

            notes = f"Case pack: {case_size}" if _clean(case_size) else None

            rows_out.append({
                "name": item_name,
                "category": category,
                "subcategory": current_sub,
                "base_unit": _infer_unit(item_name),
                "sku": _clean(item_no),
                "current_cost": round(cost, 4),
                "on_hand_qty": round(qty, 4),
                "par_level": 0.0,
                "notes": notes,
                "source_sheet": sheet_name,
            })

    return rows_out


# ---------- Liquor / beer / wine workbook ----------

def parse_gbb_lbw_workbook(file_bytes: bytes) -> List[Dict]:
    df = pd.read_excel(io.BytesIO(file_bytes), sheet_name="Inventory", header=None)
    rows_out: List[Dict] = []

    current_top = None      # "Beer" | "Liquor" | "Wine"
    current_sub = None
    # Liquor/Wine sub-tables print columns as Item|Size|Distributor|Par|OnHand|...
    # Beer sub-tables print them as Item|Distributor|Size|Par|OnHand|...
    size_first = False

    for i in range(len(df)):
        row = df.iloc[i].tolist()
        c0 = _clean(row[0])
        if not c0:
            continue
        low = c0.lower()
        if low.startswith("total"):
            continue

        rest_text = {str(v).strip().lower() for v in row[1:] if pd.notna(v)}
        looks_like_table_header = bool(rest_text & _TABLE_HEADER_WORDS) or low in ("liqour", "liquor")

        if looks_like_table_header:
            if "keg" in low:
                current_top, current_sub, size_first = "Beer", "Keg", False
            elif "package" in low or low == "beer":
                current_top, current_sub, size_first = "Beer", "Package", False
            elif low in ("liqour", "liquor"):
                current_top, current_sub, size_first = "Liquor", None, True
            # otherwise: an unrecognized repeated header row — keep current context
            continue

        is_pure_header = all(pd.isna(v) for v in row[1:])
        if is_pure_header:
            current_sub = c0
            if "wine" in low:
                current_top, size_first = "Wine", True
            continue

        if not current_top:
            continue  # data row before we've identified a section — skip rather than guess

        if size_first:
            size, distributor, par, on_hand, cost = row[1], row[2], row[3], row[4], row[6]
        else:
            distributor, size, par, on_hand, cost = row[1], row[2], row[3], row[4], row[6]

        # A few source rows have a stray date typed into the "size" column
        # (a data-entry slip in the original sheet, seen on keg rows) —
        # don't surface that as a unit.
        if isinstance(size, dt.date):
            size = "keg" if current_sub == "Keg" else None

        qty = _num(on_hand)
        if qty is None:
            qty = _num(par) or 0.0
        cost_val = _num(cost) or 0.0

        notes_bits = []
        if _clean(distributor):
            notes_bits.append(f"Distributor: {distributor}")
        notes = "; ".join(notes_bits) or None

        rows_out.append({
            "name": c0,
            "category": current_top,
            "subcategory": current_sub,
            "base_unit": _clean(size) or "bottle",
            "sku": None,
            "current_cost": round(cost_val, 4),
            "on_hand_qty": round(qty, 4),
            "par_level": round(_num(par) or 0.0, 4),
            "notes": notes,
            "source_sheet": "Inventory",
        })

    return rows_out


def _dedupe_names(rows: List[Dict]) -> List[Dict]:
    """
    The same product name sometimes appears twice with a different pack format
    (e.g. "Miller Lite" as both a Package item and a Keg item) — since items
    are matched/created by name, two rows with the same name would silently
    collapse into one on import. Disambiguate using the subcategory first,
    falling back to a counter if that's still not unique.
    """
    from collections import defaultdict
    by_name: Dict[str, List[int]] = defaultdict(list)
    for idx, r in enumerate(rows):
        by_name[r["name"]].append(idx)

    for name, idxs in by_name.items():
        if len(idxs) < 2:
            continue
        seen_labels = set()
        for idx in idxs:
            sub = rows[idx].get("subcategory")
            label = f"{name} ({sub})" if sub else name
            n = 2
            while label in seen_labels:
                label = f"{name} ({sub or 'dup'} {n})"
                n += 1
            seen_labels.add(label)
            rows[idx]["name"] = label
    return rows


def parse_workbook(file_bytes: bytes, kind: Optional[str] = None) -> List[Dict]:
    """kind: 'food', 'liquor', or None to auto-detect."""
    kind = kind or detect_workbook_type(file_bytes)
    if kind == "food":
        return _dedupe_names(parse_gbb_food_workbook(file_bytes))
    if kind == "liquor":
        return _dedupe_names(parse_gbb_lbw_workbook(file_bytes))
    raise ImportParseError(f"Unknown workbook kind '{kind}'")
