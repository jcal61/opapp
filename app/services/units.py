"""
Generic, item-independent unit conversions — the fixed kitchen-standard
ratios for weight, volume, and discrete count, usable for *any* ingredient
without per-item setup:

    Weight:  1 lb = 454 g   (also handles oz, kg)
    Volume:  1 gallon = 16 cups = 256 tbsp = 768 tsp   (also handles fl oz, pint, quart)
    Count:   each / piece / count are all 1:1 with each other

Every recipe sheet in this app (batch recipes, menu items, manual entry)
can freely mix these units on ingredient lines — a recipe can call for
"200g" of something whose inventory base_unit is "lb", or "2 tbsp" of
something tracked in "gallon", and costing/receiving/waste will convert
correctly. This does NOT cover conversions *across* dimensions (weight to
volume, or count to weight/volume) since those depend on the specific
ingredient's density or average piece weight — those still need an
item-specific UnitConversion row (see app/models/inventory.py).
"""

from __future__ import annotations
from typing import Optional

# Grams per unit of weight.
_WEIGHT_TO_GRAMS = {
    "g": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "lb": 454.0, "lbs": 454.0, "#": 454.0, "pound": 454.0, "pounds": 454.0,
    "oz": 454.0 / 16, "ounce": 454.0 / 16, "ounces": 454.0 / 16,  # 1 lb = 16 oz
}

# Teaspoons per unit of volume. 768 tsp = 256 tbsp = 16 cups = 1 gallon.
_VOLUME_TO_TSP = {
    "tsp": 1.0, "t": 1.0, "teaspoon": 1.0, "teaspoons": 1.0,
    "tbsp": 3.0, "tbl": 3.0, "tablespoon": 3.0, "tablespoons": 3.0,
    "fl oz": 6.0, "floz": 6.0, "fl. oz": 6.0, "fluid ounce": 6.0, "fluid ounces": 6.0,
    "cup": 48.0, "cups": 48.0, "c": 48.0,
    "pint": 96.0, "pt": 96.0, "pints": 96.0,
    "quart": 192.0, "qt": 192.0, "quarts": 192.0,
    "gallon": 768.0, "gal": 768.0, "gallons": 768.0,
}

# Units that just count discrete items — always 1:1 with each other.
_COUNT_UNITS = {"each", "ea", "piece", "pieces", "pc", "count", "item", "items", "unit", "units"}


def unit_dimension(unit: Optional[str]) -> Optional[str]:
    """Returns 'weight', 'volume', 'count', or None (unrecognized/other, e.g.
    a purchase-pack unit like 'case' that only makes sense per-item)."""
    key = (unit or "").strip().lower()
    if key in _WEIGHT_TO_GRAMS:
        return "weight"
    if key in _VOLUME_TO_TSP:
        return "volume"
    if key in _COUNT_UNITS:
        return "count"
    return None


def generic_conversion_factor(from_unit: str, to_unit: str) -> Optional[float]:
    """
    How many `to_unit` equal 1 `from_unit`, using fixed kitchen-standard
    ratios. Returns None if the two units aren't both weight, both volume,
    or both count — conversion across dimensions (e.g. grams to cups, or
    each to pounds) needs item-specific data and isn't attempted here.
    """
    f_key = (from_unit or "").strip().lower()
    t_key = (to_unit or "").strip().lower()
    if not f_key or not t_key:
        return None
    if f_key == t_key:
        return 1.0

    if f_key in _WEIGHT_TO_GRAMS and t_key in _WEIGHT_TO_GRAMS:
        return _WEIGHT_TO_GRAMS[f_key] / _WEIGHT_TO_GRAMS[t_key]

    if f_key in _VOLUME_TO_TSP and t_key in _VOLUME_TO_TSP:
        return _VOLUME_TO_TSP[f_key] / _VOLUME_TO_TSP[t_key]

    if f_key in _COUNT_UNITS and t_key in _COUNT_UNITS:
        return 1.0

    return None
