"""
Recipe CRUD – create/edit recipes and manage their ingredient lines
(items or sub-recipes). Cost calculation itself lives in costing.py;
this module is just the authoring/management side.
"""

from __future__ import annotations
from typing import List, Optional
from sqlalchemy.orm import Session
from app.models import Recipe, RecipeIngredient, InventoryItem, RECIPE_TYPES


def create_recipe(
    db: Session,
    name: str,
    yield_qty: float = 1.0,
    yield_unit: str = "serving",
    menu_price: Optional[float] = None,
    category: Optional[str] = None,
    code: Optional[str] = None,
    description: Optional[str] = None,
    instructions: Optional[str] = None,
    recipe_type: Optional[str] = None,
) -> Recipe:
    if db.query(Recipe).filter(Recipe.name == name).first():
        raise ValueError(f"A recipe named '{name}' already exists.")
    if code and db.query(Recipe).filter(Recipe.code == code).first():
        raise ValueError(f"Recipe code '{code}' is already in use.")
    if recipe_type and recipe_type.strip() and recipe_type.strip() not in RECIPE_TYPES:
        raise ValueError(f"'{recipe_type}' isn't a recognized recipe type. Choose one of: {', '.join(RECIPE_TYPES)}.")
    recipe = Recipe(
        name=name.strip(),
        yield_qty=yield_qty or 1.0,
        yield_unit=(yield_unit or "serving").strip(),
        menu_price=menu_price or None,
        category=(category or "").strip() or None,
        code=(code or "").strip() or None,
        description=(description or "").strip() or None,
        instructions=(instructions or "").strip() or None,
        recipe_type=(recipe_type or "").strip() or suggest_recipe_type(menu_price),
    )
    db.add(recipe)
    db.flush()
    return recipe


def update_recipe(
    db: Session,
    recipe_id: int,
    name: Optional[str] = None,
    yield_qty: Optional[float] = None,
    yield_unit: Optional[str] = None,
    menu_price: Optional[float] = None,
    category: Optional[str] = None,
    code: Optional[str] = None,
    description: Optional[str] = None,
    instructions: Optional[str] = None,
    recipe_type: Optional[str] = None,
) -> Recipe:
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise ValueError("Recipe not found")
    if code and code.strip():
        dupe = db.query(Recipe).filter(Recipe.code == code.strip(), Recipe.id != recipe_id).first()
        if dupe:
            raise ValueError(f"Recipe code '{code}' is already in use.")
    if recipe_type is not None and recipe_type.strip() and recipe_type.strip() not in RECIPE_TYPES:
        raise ValueError(f"'{recipe_type}' isn't a recognized recipe type. Choose one of: {', '.join(RECIPE_TYPES)}.")
    if name is not None:
        recipe.name = name.strip()
    if yield_qty is not None:
        recipe.yield_qty = yield_qty
    if yield_unit is not None:
        recipe.yield_unit = yield_unit.strip()
    if menu_price is not None:
        recipe.menu_price = menu_price or None
    if category is not None:
        recipe.category = category.strip() or None
    if code is not None:
        recipe.code = code.strip() or None
    if description is not None:
        recipe.description = description.strip() or None
    if instructions is not None:
        recipe.instructions = instructions.strip() or None
    if recipe_type is not None:
        recipe.recipe_type = recipe_type.strip() or recipe.recipe_type
    db.flush()
    return recipe


# ---------- Recipe type (Menu Item vs Batch/Prep) ----------

def suggest_recipe_type(menu_price: Optional[float] = None) -> str:
    """Best-guess type from menu_price — used as a default suggestion, never
    applied silently without going through auto_assign_recipe_types or an
    explicit save. A recipe with a menu price is being sold directly to
    guests (a Menu Item); one without is treated as an internal Batch/Prep
    build used inside other recipes."""
    return "Menu Item" if menu_price and menu_price > 0 else "Batch/Prep"


def auto_assign_recipe_types(db: Session) -> int:
    """Bulk-assign a type to every recipe that doesn't have one yet, using
    suggest_recipe_type(). Returns how many were updated. Doesn't touch
    recipes that already have a type — re-run after fixing any mis-guesses
    and it will leave those alone."""
    q = db.query(Recipe).filter(Recipe.recipe_type.is_(None))
    updated = 0
    for r in q.all():
        r.recipe_type = suggest_recipe_type(r.menu_price)
        updated += 1
    if updated:
        db.commit()
    return updated


def set_recipe_active(db: Session, recipe_id: int, active: bool) -> Recipe:
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise ValueError("Recipe not found")
    recipe.is_active = active
    db.flush()
    return recipe


def list_recipes(db: Session, active_only: bool = True) -> List[Recipe]:
    q = db.query(Recipe).order_by(Recipe.category, Recipe.name)
    if active_only:
        q = q.filter(Recipe.is_active == True)
    return q.all()


def find_recipe_by_name(db: Session, name: str) -> Optional[Recipe]:
    """Best-effort match of an external system's item name (e.g. a Toast
    sales report row) to an existing Recipe: exact (case-insensitive) match
    first, then a loose substring match in either direction. Mirrors
    find_item_by_name_or_sku / find_vendor_by_name in invoices.py."""
    if not name or not name.strip():
        return None
    name = name.strip()
    exact = db.query(Recipe).filter(Recipe.name.ilike(name)).first()
    if exact:
        return exact
    like = db.query(Recipe).filter(Recipe.name.ilike(f"%{name}%")).first()
    if like:
        return like
    for r in db.query(Recipe).filter(Recipe.is_active == True).all():
        if r.name.lower() in name.lower():
            return r
    return None


def add_ingredient(
    db: Session,
    recipe_id: int,
    quantity: float,
    unit: str,
    item_id: Optional[int] = None,
    sub_recipe_id: Optional[int] = None,
    is_throwaway: bool = False,
    notes: Optional[str] = None,
) -> RecipeIngredient:
    if not item_id and not sub_recipe_id:
        raise ValueError("An ingredient must reference either an inventory item or a sub-recipe.")
    if item_id and sub_recipe_id:
        raise ValueError("An ingredient can't reference both an item and a sub-recipe.")
    if sub_recipe_id == recipe_id:
        raise ValueError("A recipe can't be its own sub-recipe.")
    if quantity is None or quantity <= 0:
        raise ValueError("Quantity must be greater than zero.")

    next_sort = db.query(RecipeIngredient).filter(RecipeIngredient.recipe_id == recipe_id).count()
    ing = RecipeIngredient(
        recipe_id=recipe_id,
        item_id=item_id,
        sub_recipe_id=sub_recipe_id,
        quantity=quantity,
        unit=unit.strip(),
        is_throwaway=is_throwaway,
        notes=(notes or "").strip() or None,
        sort_order=next_sort,
    )
    db.add(ing)
    db.flush()
    return ing


def remove_ingredient(db: Session, ingredient_id: int) -> None:
    ing = db.get(RecipeIngredient, ingredient_id)
    if ing:
        db.delete(ing)
        db.flush()
