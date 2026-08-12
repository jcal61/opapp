from sqlalchemy import String, Float, ForeignKey, Boolean, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

# A recipe is either something sold directly to a guest (Menu Item) or an
# internal build used inside other recipes (Batch/Prep — sauces, stocks,
# cooked proteins, etc). Kept as a fixed list, deliberately separate from the
# free-text `category` field, mirroring InventoryItem.department.
RECIPE_TYPES = ["Menu Item", "Batch/Prep"]


class Recipe(Base):
    """A recipe or sub-recipe. Can be mapped to POS items."""
    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str | None] = mapped_column(String(50), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    yield_qty: Mapped[float] = mapped_column(Float, default=1.0)  # how many servings / recipe units this makes
    yield_unit: Mapped[str] = mapped_column(String(30), default="serving")
    menu_price: Mapped[float | None] = mapped_column(Float)  # selling price if applicable
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    category: Mapped[str | None] = mapped_column(String(80))  # cocktail, entree, sauce, etc.
    recipe_type: Mapped[str | None] = mapped_column(String(20))  # "Menu Item" or "Batch/Prep" — see RECIPE_TYPES
    instructions: Mapped[str | None] = mapped_column(Text)

    ingredients = relationship(
        "RecipeIngredient",
        back_populates="recipe",
        cascade="all, delete-orphan",
        foreign_keys="RecipeIngredient.recipe_id"
    )

    def __repr__(self):
        return f"<Recipe {self.name}>"


class RecipeIngredient(Base):
    """
    One line in a recipe.
    Can reference either an InventoryItem OR another Recipe (sub-recipe).
    Exactly one of item_id or sub_recipe_id should be set.
    """
    __tablename__ = "recipe_ingredients"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"), nullable=False)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_items.id"))
    sub_recipe_id: Mapped[int | None] = mapped_column(ForeignKey("recipes.id"))

    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    is_throwaway: Mapped[bool] = mapped_column(Boolean, default=False)  # cost it but don't add to yield weight
    notes: Mapped[str | None] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    recipe = relationship("Recipe", back_populates="ingredients", foreign_keys=[recipe_id])
    item = relationship("InventoryItem", back_populates="recipe_ingredients")
    sub_recipe = relationship("Recipe", foreign_keys=[sub_recipe_id])
