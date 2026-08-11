"""
Toast POS Sales Import Port

Supports two practical ingestion modes:
1. Toast Orders API-style JSON (orders with checks → selections)
2. Simplified CSV / dict rows from Toast Item Selection or Order exports

Maps Toast menu item names (or GUIDs) to Craftable Recipes, then
records POSSale + depletes theoretical inventory.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.orm import Session

from app.models import Recipe, Location, POSSale, POSSaleLine, InventoryItem
from app.services.inventory import record_pos_sale, deplete_recipe


@dataclass
class ToastItemMapping:
    """Maps a Toast menu item identifier to a Craftable Recipe."""
    toast_item_name: str
    toast_guid: Optional[str] = None
    recipe_id: Optional[int] = None
    recipe_name: Optional[str] = None
    ignore: bool = False  # e.g. open food, non-depleting items


@dataclass
class ImportResult:
    sales_created: int = 0
    lines_processed: int = 0
    lines_mapped: int = 0
    lines_unmapped: int = 0
    lines_ignored: int = 0
    errors: List[str] = field(default_factory=list)
    unmapped_items: List[str] = field(default_factory=list)


def _parse_toast_datetime(value: Any) -> datetime:
    """Parse common Toast timestamp formats."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            # Handle trailing Z
            cleaned = s.replace("Z", "+0000") if s.endswith("Z") else s
            dt = datetime.strptime(cleaned, fmt.replace("%z", "%z") if "%z" in fmt else fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return datetime.now(timezone.utc)


class ToastSalesImporter:
    """
    Port for bringing Toast POS sales into Craftable theoretical inventory.
    """

    def __init__(self, db: Session, location_id: int):
        self.db = db
        self.location_id = location_id
        self._mapping_cache: Dict[str, ToastItemMapping] = {}
        self._load_default_mappings()

    def _load_default_mappings(self) -> None:
        """
        Build name-based mappings from existing recipes.
        In production you would store these in a ToastItemMap table.
        """
        recipes = self.db.query(Recipe).filter(Recipe.is_active == True).all()
        for r in recipes:
            key = r.name.strip().lower()
            self._mapping_cache[key] = ToastItemMapping(
                toast_item_name=r.name,
                recipe_id=r.id,
                recipe_name=r.name,
            )
            if r.code:
                self._mapping_cache[r.code.strip().lower()] = ToastItemMapping(
                    toast_item_name=r.code,
                    recipe_id=r.id,
                    recipe_name=r.name,
                )

    def set_mapping(self, toast_name: str, recipe_id: Optional[int] = None, ignore: bool = False) -> None:
        """Manually map a Toast item name to a recipe (or ignore it)."""
        key = toast_name.strip().lower()
        recipe_name = None
        if recipe_id:
            recipe = self.db.get(Recipe, recipe_id)
            recipe_name = recipe.name if recipe else None
        self._mapping_cache[key] = ToastItemMapping(
            toast_item_name=toast_name,
            recipe_id=recipe_id,
            recipe_name=recipe_name,
            ignore=ignore,
        )

    def resolve_mapping(self, toast_item_name: str) -> ToastItemMapping:
        key = (toast_item_name or "").strip().lower()
        if key in self._mapping_cache:
            return self._mapping_cache[key]
        # Fuzzy-ish: try contains
        for k, m in self._mapping_cache.items():
            if k in key or key in k:
                return m
        return ToastItemMapping(toast_item_name=toast_item_name)

    # ------------------------------------------------------------------
    # Mode 1: Simplified flat rows (CSV-like or webhook payload lines)
    # ------------------------------------------------------------------
    def import_item_rows(
        self,
        rows: List[Dict[str, Any]],
        *,
        external_id_prefix: str = "TOAST",
    ) -> ImportResult:
        """
        Import flat item-level sales rows.

        Expected keys (flexible):
          - item_name / Item Name / displayName / name  (required)
          - quantity / Quantity / qty                   (default 1)
          - unit_price / price / Unit Price             (optional)
          - order_id / Order Id / check_guid            (groups into one sale)
          - sold_at / Opened / Paid / closedDate        (optional)
        """
        result = ImportResult()
        # Group by order_id so one POSSale can have multiple lines
        groups: Dict[str, List[Dict]] = {}
        for row in rows:
            order_key = str(
                row.get("order_id")
                or row.get("Order Id")
                or row.get("check_guid")
                or row.get("Order #")
                or f"anon-{len(groups)}"
            )
            groups.setdefault(order_key, []).append(row)

        for order_key, order_rows in groups.items():
            lines_for_sale: List[dict] = []
            sold_at = None
            for row in order_rows:
                name = (
                    row.get("item_name")
                    or row.get("Item Name")
                    or row.get("displayName")
                    or row.get("name")
                    or row.get("Menu Item")
                    or ""
                )
                name = str(name).strip()
                if not name:
                    result.errors.append(f"Row missing item name in order {order_key}")
                    continue

                qty = float(row.get("quantity") or row.get("Quantity") or row.get("qty") or 1)
                price = row.get("unit_price") or row.get("Unit Price") or row.get("price") or row.get("Amount")
                price = float(price) if price is not None else None

                if sold_at is None:
                    raw_dt = row.get("sold_at") or row.get("Opened") or row.get("Paid") or row.get("closedDate")
                    sold_at = _parse_toast_datetime(raw_dt)

                mapping = self.resolve_mapping(name)
                result.lines_processed += 1

                if mapping.ignore:
                    result.lines_ignored += 1
                    continue

                if not mapping.recipe_id:
                    result.lines_unmapped += 1
                    if name not in result.unmapped_items:
                        result.unmapped_items.append(name)
                    continue

                result.lines_mapped += 1
                lines_for_sale.append({
                    "recipe_id": mapping.recipe_id,
                    "pos_item_name": name,
                    "quantity": qty,
                    "unit_price": price,
                })

            if lines_for_sale:
                try:
                    record_pos_sale(
                        self.db,
                        self.location_id,
                        lines_for_sale,
                        external_id=f"{external_id_prefix}-{order_key}",
                    )
                    # Optionally override sold_at if we parsed one
                    # (record_pos_sale uses now(); for accuracy we could update)
                    result.sales_created += 1
                except Exception as e:
                    result.errors.append(f"Failed to record sale {order_key}: {e}")

        self.db.commit()
        return result

    # ------------------------------------------------------------------
    # Mode 2: Toast Orders API-style nested JSON
    # ------------------------------------------------------------------
    def import_orders_json(
        self,
        orders: List[Dict[str, Any]],
        *,
        external_id_prefix: str = "TOAST",
    ) -> ImportResult:
        """
        Import from Toast Orders API response shape.

        Each order roughly:
        {
          "guid": "...",
          "openedDate": "...",
          "voided": false,
          "checks": [
            {
              "selections": [
                {
                  "displayName": "Old Fashioned",
                  "quantity": 2,
                  "price": 14.0,
                  "item": {"guid": "..."}
                }
              ]
            }
          ]
        }
        """
        result = ImportResult()

        for order in orders:
            if order.get("voided") or order.get("deleted"):
                continue

            order_guid = order.get("guid") or order.get("externalId") or "unknown"
            sold_at = _parse_toast_datetime(
                order.get("closedDate") or order.get("paidDate") or order.get("openedDate")
            )

            lines_for_sale: List[dict] = []
            checks = order.get("checks") or []
            for check in checks:
                selections = check.get("selections") or []
                for sel in selections:
                    name = (
                        sel.get("displayName")
                        or sel.get("displayName")
                        or (sel.get("item") or {}).get("name")
                        or ""
                    )
                    name = str(name).strip()
                    if not name:
                        continue

                    qty = float(sel.get("quantity") or 1)
                    price = sel.get("price") or sel.get("preDiscountPrice")
                    price = float(price) if price is not None else None

                    mapping = self.resolve_mapping(name)
                    result.lines_processed += 1

                    if mapping.ignore:
                        result.lines_ignored += 1
                        continue
                    if not mapping.recipe_id:
                        result.lines_unmapped += 1
                        if name not in result.unmapped_items:
                            result.unmapped_items.append(name)
                        continue

                    result.lines_mapped += 1
                    lines_for_sale.append({
                        "recipe_id": mapping.recipe_id,
                        "pos_item_name": name,
                        "quantity": qty,
                        "unit_price": price,
                    })

            if lines_for_sale:
                try:
                    record_pos_sale(
                        self.db,
                        self.location_id,
                        lines_for_sale,
                        external_id=f"{external_id_prefix}-{order_guid}",
                    )
                    result.sales_created += 1
                except Exception as e:
                    result.errors.append(f"Order {order_guid}: {e}")

        self.db.commit()
        return result

    def list_current_mappings(self) -> List[ToastItemMapping]:
        return list(self._mapping_cache.values())
