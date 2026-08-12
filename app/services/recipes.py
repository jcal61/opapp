"""
Recipe CRUD – create/edit recipes and manage their ingredient lines
(items or sub-recipes). Cost calculation itself lives in costing.py;
this module is just the authoring/management side.
"""

from __future__ import annotations
from typing import List, Optional
from sqlalchemy.orm import Session
from app.models import Recipe, RecipeIngredient, InventoryItem


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
) -> Recipe:
    if db.query(Recipe).filter(Recipe.name == name).first():
        raise ValueError(f"A recipe named '{name}' already exists.")
    if code and db.query(Recipe).filter(Recipe.code == code).first():
        raise ValueError(f"Recipe code '{code}' is already in use.")
    recipe = Recipe(
        name=name.strip(),
        yield_qty=yield_qty or 1.0,
        yield_unit=(yield_unit or "serving").strip(),
        menu_price=menu_price or None,
        category=(category or "").strip() or None,
        code=(code or "").strip() or None,
        description=(description or "").strip() or None,
        instructions=(instructions or "").strip() or None,
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
) -> Recipe:
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise ValueError("Recipe not found")
    if code and code.strip():
        dupe = db.query(Recipe).filter(Recipe.code == code.strip(), Recipe.id != recipe_id).first()
        if dupe:
            raise ValueError(f"Recipe code '{code}' is already in use.")
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
    db.flush()
    return recipe


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
