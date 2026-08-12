"""
Recipe Costing Engine – the heart of live plate costing.
Supports nested sub-recipes and converts units using the item's conversion table.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from sqlalchemy.orm import Session
from app.models import Recipe, RecipeIngredient, InventoryItem, UnitConversion
from app.services.units import generic_conversion_factor


@dataclass
class IngredientCost:
    name: str
    quantity: float
    unit: str
    cost: float
    is_subrecipe: bool = False
    children: List["IngredientCost"] = field(default_factory=list)


@dataclass
class RecipeCostResult:
    recipe_id: int
    recipe_name: str
    yield_qty: float
    yield_unit: str
    total_cost: float
    cost_per_unit: float
    cost_percent: Optional[float]  # vs menu_price
    menu_price: Optional[float]
    breakdown: List[IngredientCost]


def get_conversion_factor(db: Session, item: InventoryItem, from_unit: str, to_unit: str) -> float:
    """
    Return how many `to_unit` equal 1 `from_unit`. Checked in order:
      1. Same unit -> 1.0
      2. An explicit per-item UnitConversion row (or its inverse) — lets a
         specific item override the standard ratio, or define one across
         dimensions (e.g. "1 each = 0.5 lb" for a piece-counted item).
      3. The standard weight/volume/count ratios (g, lb, oz, kg; tsp, tbsp,
         cup, fl oz, pint, quart, gallon; each/piece/count) — these apply to
         any item with no per-item setup needed, so a recipe can freely mix
         "200g" against an item tracked in "lb", etc.
      4. Falls back to 1:1 if genuinely unconvertible and nothing configured
         (demo mode) rather than raising.
    """
    if from_unit.lower() == to_unit.lower():
        return 1.0

    # Direct per-item conversion
    conv = (
        db.query(UnitConversion)
        .filter(
            UnitConversion.item_id == item.id,
            UnitConversion.from_unit == from_unit,
            UnitConversion.to_unit == to_unit,
        )
        .first()
    )
    if conv:
        return conv.factor

    # Inverse per-item conversion
    inv = (
        db.query(UnitConversion)
        .filter(
            UnitConversion.item_id == item.id,
            UnitConversion.from_unit == to_unit,
            UnitConversion.to_unit == from_unit,
        )
        .first()
    )
    if inv and inv.factor != 0:
        return 1.0 / inv.factor

    # Standard weight/volume/count ratios — no per-item setup required.
    generic = generic_conversion_factor(from_unit, to_unit)
    if generic is not None:
        return generic

    # Genuinely unconvertible (e.g. weight <-> count) with nothing configured.
    return 1.0


def find_recipes_using_item(db: Session, item_id: int) -> List[Dict]:
    """
    Every recipe — batch/prep or menu item — whose cost depends on this
    inventory item, directly or through any depth of nested sub-recipes.

    This is the "what does changing this ingredient's price affect" query:
    find every recipe that uses the item directly, then repeatedly widen out
    to any recipe that uses one of those as a sub-recipe, until nothing new
    is found. Used to show the ripple effect after a price update (e.g. an
    approved invoice) changes InventoryItem.current_cost — the cost numbers
    themselves need no recalculation here, since calculate_recipe_cost always
    reads current_cost live; this is purely for surfacing "here's what just
    changed" to the user.
    """
    all_ingredients = db.query(RecipeIngredient).all()
    direct_recipe_ids = {ing.recipe_id for ing in all_ingredients if ing.item_id == item_id}

    used_as_subrecipe_by: Dict[int, Set[int]] = {}
    for ing in all_ingredients:
        if ing.sub_recipe_id:
            used_as_subrecipe_by.setdefault(ing.sub_recipe_id, set()).add(ing.recipe_id)

    affected: Set[int] = set(direct_recipe_ids)
    frontier: Set[int] = set(direct_recipe_ids)
    while frontier:
        next_frontier: Set[int] = set()
        for rid in frontier:
            for parent_id in used_as_subrecipe_by.get(rid, ()):
                if parent_id not in affected:
                    affected.add(parent_id)
                    next_frontier.add(parent_id)
        frontier = next_frontier

    results: List[Dict] = []
    for rid in affected:
        r = db.get(Recipe, rid)
        if not r:
            continue
        results.append({
            "recipe_id": r.id,
            "recipe_name": r.name,
            "recipe_type": r.recipe_type or "Batch/Prep",
            "is_direct": rid in direct_recipe_ids,
        })
    results.sort(key=lambda x: (x["recipe_type"] != "Menu Item", x["recipe_name"]))
    return results


def calculate_recipe_cost(
    db: Session,
    recipe_id: int,
    _visited: Optional[Set[int]] = None,
) -> RecipeCostResult:
    """
    Recursively calculate the full cost of a recipe using current inventory prices.
    """
    if _visited is None:
        _visited = set()

    if recipe_id in _visited:
        # Soft fail for circular – return zero cost rather than crash
        recipe = db.get(Recipe, recipe_id)
        name = recipe.name if recipe else f"Recipe {recipe_id}"
        return RecipeCostResult(
            recipe_id=recipe_id,
            recipe_name=f"[Circular] {name}",
            yield_qty=1.0,
            yield_unit="serving",
            total_cost=0.0,
            cost_per_unit=0.0,
            cost_percent=None,
            menu_price=None,
            breakdown=[],
        )

    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise ValueError(f"Recipe {recipe_id} not found")

    visited = _visited | {recipe_id}  # new set for this branch

    breakdown: List[IngredientCost] = []
    total = 0.0

    for ing in sorted(recipe.ingredients, key=lambda x: x.sort_order or 0):
        if ing.item_id:
            item = db.get(InventoryItem, ing.item_id)
            if not item:
                continue
            factor = get_conversion_factor(db, item, ing.unit, item.base_unit)
            qty_in_base = ing.quantity * factor
            cost = qty_in_base * (item.current_cost or 0.0)
            breakdown.append(IngredientCost(
                name=item.name,
                quantity=ing.quantity,
                unit=ing.unit,
                cost=round(cost, 4),
                is_subrecipe=False,
            ))
            total += cost

        elif ing.sub_recipe_id:
            sub_result = calculate_recipe_cost(db, ing.sub_recipe_id, _visited=visited)
            scale = ing.quantity / (sub_result.yield_qty or 1.0)
            scaled_cost = sub_result.total_cost * scale
            breakdown.append(IngredientCost(
                name=sub_result.recipe_name,
                quantity=ing.quantity,
                unit=ing.unit,
                cost=round(scaled_cost, 4),
                is_subrecipe=True,
                children=sub_result.breakdown,
            ))
            total += scaled_cost

    yield_qty = recipe.yield_qty or 1.0
    cost_per_unit = total / yield_qty if yield_qty else total

    cost_pct = None
    if recipe.menu_price and recipe.menu_price > 0:
        cost_pct = round((cost_per_unit / recipe.menu_price) * 100, 2)

    return RecipeCostResult(
        recipe_id=recipe.id,
        recipe_name=recipe.name,
        yield_qty=yield_qty,
        yield_unit=recipe.yield_unit,
        total_cost=round(total, 4),
        cost_per_unit=round(cost_per_unit, 4),
        cost_percent=cost_pct,
        menu_price=recipe.menu_price,
        breakdown=breakdown,
    )
