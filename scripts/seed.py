"""
Seed a realistic single-location restaurant with inventory, recipes,
two physical counts, and activity between them so variance reports work.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, date, timedelta, timezone
from app.database import engine, SessionLocal, Base
from app.models import (
    Location, Vendor, InventoryItem, UnitConversion, StockLevel,
    Recipe, RecipeIngredient,
    PurchaseOrder, PurchaseOrderLine,
    InventoryCount, CountLine,
)
from app.services.inventory import receive_po_line, record_pos_sale, log_waste
from app.services.costing import calculate_recipe_cost
from app.services.counts import create_count, add_or_update_count_line, close_count

def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()

    # --- Location ---
    loc = Location(
        name="Main Dining Room",
        code="MAIN",
        address="123 Market Street",
        city="Chicago",
        state="IL",
        postal_code="60601",
        timezone="America/Chicago",
        closeout_hour=4,
        phone="312-555-0100",
        notes="Primary service floor",
    )
    loc2 = Location(
        name="Rooftop Bar",
        code="ROOF",
        address="123 Market Street — Roof",
        city="Chicago",
        state="IL",
        postal_code="60601",
        timezone="America/Chicago",
        closeout_hour=3,
        phone="312-555-0101",
        notes="Seasonal rooftop; separate pars recommended",
        is_active=True,
    )
    db.add(loc2)
    db.add(loc)
    db.flush()

    # --- Vendors ---
    sysco = Vendor(name="Sysco", code="SYSCO")
    usfoods = Vendor(name="US Foods", code="USF")
    local = Vendor(name="Local Produce Co", code="LOCAL")
    db.add_all([sysco, usfoods, local])
    db.flush()

    # --- Inventory Items ---
    items_data = [
        ("Bourbon (Bulk)", "BOURBON-1L", "liquor", "oz", 0.45, 64),
        ("Sweet Vermouth", "VERMOUTH-SW", "liquor", "oz", 0.28, 32),
        ("Angostura Bitters", "BITTERS-ANG", "liquor", "dash", 0.05, 200),
        ("Simple Syrup", "SYRUP-SIMPLE", "dry", "oz", 0.08, 48),
        ("Orange Peel", "GARNISH-ORANGE", "produce", "each", 0.15, 50),
        ("Chicken Breast", "PROT-CHICKEN", "protein", "oz", 0.22, 160),
        ("Romaine Lettuce", "PROD-ROMAINE", "produce", "oz", 0.09, 80),
        ("Caesar Dressing", "DRESS-CAESAR", "dry", "oz", 0.18, 64),
        ("Parmesan", "DAIRY-PARM", "dairy", "oz", 0.35, 32),
        ("Croutons", "DRY-CROUTON", "dry", "oz", 0.12, 40),
        ("Vodka", "VODKA-1L", "liquor", "oz", 0.32, 64),
        ("Tomato Juice", "JUICE-TOMATO", "beverage", "oz", 0.06, 96),
        ("Lemon Juice", "JUICE-LEMON", "produce", "oz", 0.15, 32),
        ("Worcestershire", "COND-WORC", "dry", "oz", 0.10, 16),
        ("Hot Sauce", "COND-HOT", "dry", "oz", 0.08, 16),
        ("Celery Stalk", "GARNISH-CELERY", "produce", "each", 0.25, 30),
    ]

    items = {}
    for name, sku, cat, unit, cost, par in items_data:
        item = InventoryItem(
            name=name,
            sku=sku,
            category=cat,
            base_unit=unit,
            current_cost=cost,
            par_level=par,
            preferred_vendor_id=sysco.id if cat in ("liquor", "dry", "protein") else local.id,
        )
        db.add(item)
        items[name] = item
    db.flush()

    # --- Starting Stock Levels ---
    for item in items.values():
        stock = StockLevel(
            item_id=item.id,
            location_id=loc.id,
            theoretical_qty=item.par_level * 1.8,
            last_physical_qty=item.par_level * 1.8,
            last_count_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        db.add(stock)
    db.flush()

    # --- Recipes ---
    of = Recipe(
        name="Old Fashioned", code="COCKTAIL-OF", category="cocktail",
        yield_qty=1, yield_unit="drink", menu_price=14.00,
        instructions="Stir bourbon, syrup, bitters over ice. Garnish with orange peel.",
    )
    db.add(of)
    db.flush()
    db.add_all([
        RecipeIngredient(recipe_id=of.id, item_id=items["Bourbon (Bulk)"].id, quantity=2.0, unit="oz", sort_order=1),
        RecipeIngredient(recipe_id=of.id, item_id=items["Simple Syrup"].id, quantity=0.25, unit="oz", sort_order=2),
        RecipeIngredient(recipe_id=of.id, item_id=items["Angostura Bitters"].id, quantity=2.0, unit="dash", sort_order=3),
        RecipeIngredient(recipe_id=of.id, item_id=items["Orange Peel"].id, quantity=1.0, unit="each", sort_order=4),
    ])

    caesar = Recipe(
        name="Caesar Salad", code="APP-CAESAR", category="appetizer",
        yield_qty=1, yield_unit="plate", menu_price=13.00,
    )
    db.add(caesar)
    db.flush()
    db.add_all([
        RecipeIngredient(recipe_id=caesar.id, item_id=items["Romaine Lettuce"].id, quantity=5.0, unit="oz", sort_order=1),
        RecipeIngredient(recipe_id=caesar.id, item_id=items["Caesar Dressing"].id, quantity=1.5, unit="oz", sort_order=2),
        RecipeIngredient(recipe_id=caesar.id, item_id=items["Parmesan"].id, quantity=0.75, unit="oz", sort_order=3),
        RecipeIngredient(recipe_id=caesar.id, item_id=items["Croutons"].id, quantity=1.0, unit="oz", sort_order=4),
    ])

    chicken_caesar = Recipe(
        name="Grilled Chicken Caesar", code="ENTREE-CHICKEN-CAESAR", category="entree",
        yield_qty=1, yield_unit="plate", menu_price=19.00,
    )
    db.add(chicken_caesar)
    db.flush()
    db.add_all([
        RecipeIngredient(recipe_id=chicken_caesar.id, sub_recipe_id=caesar.id, quantity=1.0, unit="plate", sort_order=1),
        RecipeIngredient(recipe_id=chicken_caesar.id, item_id=items["Chicken Breast"].id, quantity=6.0, unit="oz", sort_order=2),
    ])

    bloody = Recipe(
        name="Bloody Mary", code="COCKTAIL-BLOODY", category="cocktail",
        yield_qty=1, yield_unit="drink", menu_price=12.00,
    )
    db.add(bloody)
    db.flush()
    db.add_all([
        RecipeIngredient(recipe_id=bloody.id, item_id=items["Vodka"].id, quantity=2.0, unit="oz", sort_order=1),
        RecipeIngredient(recipe_id=bloody.id, item_id=items["Tomato Juice"].id, quantity=4.0, unit="oz", sort_order=2),
        RecipeIngredient(recipe_id=bloody.id, item_id=items["Lemon Juice"].id, quantity=0.5, unit="oz", sort_order=3),
        RecipeIngredient(recipe_id=bloody.id, item_id=items["Worcestershire"].id, quantity=0.25, unit="oz", sort_order=4),
        RecipeIngredient(recipe_id=bloody.id, item_id=items["Hot Sauce"].id, quantity=0.15, unit="oz", sort_order=5),
        RecipeIngredient(recipe_id=bloody.id, item_id=items["Celery Stalk"].id, quantity=1.0, unit="each", sort_order=6),
    ])
    db.commit()

    # ========== COUNT 1 (Starting physical – 7 days ago) ==========
    count1 = create_count(db, loc.id, name="Week Start Count", notes="Opening inventory")
    count1.counted_at = datetime.now(timezone.utc) - timedelta(days=7)
    for item in items.values():
        # Physical roughly matches theoretical at start
        qty = item.par_level * 1.8
        add_or_update_count_line(db, count1.id, item.id, qty)
    close_count(db, count1.id, align_theoretical=True)
    db.commit()

    # ========== Activity between counts ==========
    # Purchase Order + Receiving
    po = PurchaseOrder(
        po_number="PO-1001",
        vendor_id=sysco.id,
        location_id=loc.id,
        status="received",
        order_date=date.today() - timedelta(days=5),
    )
    db.add(po)
    db.flush()

    for item, qty, unit, cost in [
        (items["Bourbon (Bulk)"], 67.6, "oz", 0.45),
        (items["Chicken Breast"], 320, "oz", 0.22),
        (items["Vodka"], 67.6, "oz", 0.32),
        (items["Romaine Lettuce"], 100, "oz", 0.09),
    ]:
        line = PurchaseOrderLine(
            purchase_order_id=po.id,
            item_id=item.id,
            quantity_ordered=qty,
            unit=unit,
            unit_cost=cost,
            quantity_received=0,
        )
        db.add(line)
        db.flush()
        receive_po_line(db, line, qty, loc.id)
    db.commit()

    # POS Sales (will deplete)
    record_pos_sale(db, loc.id, [
        {"recipe_id": of.id, "pos_item_name": "Old Fashioned", "quantity": 18, "unit_price": 14.0},
        {"recipe_id": bloody.id, "pos_item_name": "Bloody Mary", "quantity": 12, "unit_price": 12.0},
        {"recipe_id": chicken_caesar.id, "pos_item_name": "Grilled Chicken Caesar", "quantity": 14, "unit_price": 19.0},
        {"recipe_id": caesar.id, "pos_item_name": "Caesar Salad", "quantity": 9, "unit_price": 13.0},
    ], external_id="TOAST-WEEK-001")
    db.commit()

    # Waste
    log_waste(db, items["Romaine Lettuce"].id, loc.id, 12.0, "oz", reason="spoilage")
    log_waste(db, items["Orange Peel"].id, loc.id, 8.0, "each", reason="over-prepped")
    log_waste(db, items["Chicken Breast"].id, loc.id, 6.0, "oz", reason="trim loss")
    db.commit()

    # ========== COUNT 2 (Ending physical – today) ==========
    # We intentionally introduce some variance (actual different from pure theoretical)
    count2 = create_count(db, loc.id, name="Week End Count", notes="Closing inventory for variance")
    count2.counted_at = datetime.now(timezone.utc)
    
    # Start from current theoretical and add small intentional variances for demo
    from app.services.variance import get_current_theoretical_snapshot
    snap = get_current_theoretical_snapshot(db, loc.id)
    theo_map = {r["item_id"]: r["theoretical_qty"] for r in snap}

    for item in items.values():
        theo = theo_map.get(item.id, item.par_level)
        # Small realistic variance: some items a bit high, some a bit low
        if "Romaine" in item.name:
            actual = theo - 4.5   # more waste than logged
        elif "Bourbon" in item.name:
            actual = theo - 3.0   # free pours / overpour
        elif "Chicken" in item.name:
            actual = theo + 2.0   # slight over-receive or count error
        else:
            actual = theo
        add_or_update_count_line(db, count2.id, item.id, max(0, actual))
    
    close_count(db, count2.id, align_theoretical=False)  # do NOT force theoretical = physical so variance remains visible
    db.commit()

    # ========== Invoices (AP) demo data ==========
    from app.services.invoices import create_invoice, add_invoice_line, auto_match_to_po, approve_invoice

    # Invoice against PO-1001: two lines match cleanly, two carry variance
    # (price crept up on Chicken Breast, and Vodka was under-billed on quantity)
    # so the invoice lands in "exception" for a manager to review.
    inv1 = create_invoice(
        db, vendor_id=sysco.id, location_id=loc.id,
        invoice_number="SYSCO-88213", invoice_date=date.today() - timedelta(days=4),
        due_date=date.today() + timedelta(days=26),
        purchase_order_id=po.id,
        notes="Weekly Sysco delivery invoice.",
    )
    add_invoice_line(db, inv1.id, "Bourbon (Bulk)", 67.6, 0.45, unit="oz", item_id=items["Bourbon (Bulk)"].id)
    add_invoice_line(db, inv1.id, "Chicken Breast", 320, 0.23, unit="oz", item_id=items["Chicken Breast"].id)  # price crept up
    add_invoice_line(db, inv1.id, "Vodka", 60.0, 0.32, unit="oz", item_id=items["Vodka"].id)  # billed less than received
    add_invoice_line(db, inv1.id, "Romaine Lettuce", 100, 0.09, unit="oz", item_id=items["Romaine Lettuce"].id)
    db.commit()
    auto_match_to_po(db, inv1.id)
    db.commit()

    # Non-PO invoice (utilities) — approved, waiting on payment
    inv2 = create_invoice(
        db, vendor_id=local.id, location_id=loc.id,
        invoice_number="LOCAL-4471", invoice_date=date.today() - timedelta(days=2),
        due_date=date.today() + timedelta(days=13),
        notes="Weekly produce delivery — no PO on file.",
    )
    add_invoice_line(db, inv2.id, "Produce delivery (misc)", 1, 412.50)
    db.commit()
    approve_invoice(db, inv2.id)
    db.commit()

    # --- Users & roles ---
    from app.services.auth import seed_roles, create_user
    from app.services.checklists import seed_demo_checklists
    seed_roles(db)
    owner_u = create_user(db, "Alex Owner", "owner", pin="0000", email="owner@demo.restaurant", hourly_rate=0.0)
    mgr_u = create_user(db, "Sam Manager", "manager", pin="1111", email="manager@demo.restaurant", hourly_rate=28.0)
    kitchen_u = create_user(db, "Jordan Kitchen", "kitchen", pin="2222", hourly_rate=19.5)
    server_u = create_user(db, "Casey Server", "server", pin="3333", hourly_rate=12.0)
    seed_demo_checklists(db, loc.id)
    db.commit()

    # --- Scheduling & time clock demo data ---
    from app.services.scheduling import create_shift, clock_in, clock_out, week_bounds

    today = date.today()
    wk_start, _ = week_bounds(today)
    wk_start_date = wk_start.date()

    shift_plan = [
        # (day_offset, user, role_code, start_hr, end_hr, status)
        (0, mgr_u, "manager", 8, 16, "published"),
        (0, kitchen_u, "kitchen", 9, 17, "published"),
        (0, server_u, "server", 11, 19, "published"),
        (1, mgr_u, "manager", 8, 16, "published"),
        (1, kitchen_u, "kitchen", 9, 17, "published"),
        (2, kitchen_u, "kitchen", 9, 17, "published"),
        (2, server_u, "server", 11, 19, "published"),
        (3, mgr_u, "manager", 8, 16, "draft"),
        (3, server_u, "server", 11, 19, "draft"),
        (4, kitchen_u, "kitchen", 9, 17, "draft"),
        (4, None, "server", 11, 19, "draft"),  # open shift, unfilled
    ]
    shifts_by_key = {}
    for day_off, u, role_code, sh, eh, status in shift_plan:
        d = wk_start_date + timedelta(days=day_off)
        start_dt = datetime(d.year, d.month, d.day, sh, tzinfo=timezone.utc)
        end_dt = datetime(d.year, d.month, d.day, eh, tzinfo=timezone.utc)
        shift = create_shift(
            db, loc.id, start_dt, end_dt,
            user_id=u.id if u else None,
            role_code=role_code, status=status,
        )
        if u:
            shifts_by_key[(day_off, u.id)] = shift

    # Actual punches for the days already worked this week (0 and 1), close to
    # but not exactly matching the schedule, so scheduled-vs-actual has a
    # realistic small variance. Day 2's manager is left clocked in (open entry)
    # to demo the live "currently clocked in" state.
    def _punch(u, day_off, in_hr, in_min, out_hr, out_min, break_minutes=15):
        d = wk_start_date + timedelta(days=day_off)
        if d > today:
            return
        entry = clock_in(db, u.id, loc.id, shift_id=shifts_by_key.get((day_off, u.id)) and shifts_by_key[(day_off, u.id)].id)
        entry.clock_in = datetime(d.year, d.month, d.day, in_hr, in_min, tzinfo=timezone.utc)
        entry.break_minutes = break_minutes
        db.commit()
        if out_hr is not None:
            entry.clock_out = datetime(d.year, d.month, d.day, out_hr, out_min, tzinfo=timezone.utc)
            db.commit()

    if wk_start_date <= today:
        _punch(mgr_u, 0, 8, 4, 16, 10)
        _punch(kitchen_u, 0, 8, 55, 17, 20)
        _punch(server_u, 0, 11, 10, 18, 50)
    if wk_start_date + timedelta(days=1) <= today:
        _punch(mgr_u, 1, 8, 0, None, None)  # still clocked in — demo of live state

    db.commit()

    # --- Training & quizzes demo data ---
    from app.services.training import create_course, add_lesson, add_quiz_question, start_course, submit_quiz

    orientation = create_course(
        db, "New Hire Orientation",
        description="The basics every new team member needs before their first shift.",
        category="onboarding", role_codes=None, location_id=loc.id, passing_score=80,
    )
    add_lesson(db, orientation.id, "Welcome & Dress Code",
               content="Uniforms are provided. Closed-toe shoes required. Clock in 5 minutes before your shift.")
    add_lesson(db, orientation.id, "Guest Service Basics",
               content="Greet every guest within 30 seconds. Never say 'I don't know' without offering to find out.")
    oq1 = add_quiz_question(db, orientation.id, "How many minutes before your shift should you clock in?",
                             ["On time is fine", "5 minutes early", "30 minutes early", "It doesn't matter"], "5 minutes early")
    oq2 = add_quiz_question(db, orientation.id, "What footwear is required?",
                             ["Any shoes", "Closed-toe shoes", "Sandals", "Barefoot is fine"], "Closed-toe shoes")
    oq3 = add_quiz_question(db, orientation.id, "How quickly should you greet a guest?",
                             ["Within 30 seconds", "Within 10 minutes", "When you get a chance", "Only if they wave"], "Within 30 seconds")

    food_safety = create_course(
        db, "Food Safety Basics",
        description="Core temperature and handling rules for anyone working the line.",
        category="food_safety", role_codes="kitchen,manager", location_id=loc.id, passing_score=80,
    )
    add_lesson(db, food_safety.id, "Danger Zone Temperatures",
               content="Keep hot food above 140°F and cold food below 40°F. The 'danger zone' in between is where bacteria grow fastest.")
    add_lesson(db, food_safety.id, "Handwashing",
               content="Wash hands for at least 20 seconds: before shift, after handling raw protein, after touching your face, after the restroom.")
    fq1 = add_quiz_question(db, food_safety.id, "Cold food should be held below what temperature?",
                             ["40°F", "50°F", "60°F", "32°F"], "40°F")
    fq2 = add_quiz_question(db, food_safety.id, "Minimum handwashing time?",
                             ["5 seconds", "10 seconds", "20 seconds", "1 minute"], "20 seconds")

    # Jordan Kitchen: passes both courses on the first attempt.
    c1 = start_course(db, orientation.id, kitchen_u.id)
    submit_quiz(db, c1.id, {oq1.id: oq1.correct_index, oq2.id: oq2.correct_index, oq3.id: oq3.correct_index})
    c2 = start_course(db, food_safety.id, kitchen_u.id)
    submit_quiz(db, c2.id, {fq1.id: fq1.correct_index, fq2.id: fq2.correct_index})

    # Casey Server: fails the first attempt at orientation (misses two), then
    # retakes and passes — demonstrates the retry/accountability trail.
    c3 = start_course(db, orientation.id, server_u.id)
    wrong = {q.id: next(i for i in range(len(q.choice_list())) if i != q.correct_index) for q in [oq1, oq2, oq3]}
    submit_quiz(db, c3.id, {oq1.id: wrong[oq1.id], oq2.id: wrong[oq2.id], oq3.id: oq3.correct_index})
    c3b = start_course(db, orientation.id, server_u.id)
    submit_quiz(db, c3b.id, {oq1.id: oq1.correct_index, oq2.id: oq2.correct_index, oq3.id: oq3.correct_index})

    db.commit()

    # --- Summary ---
    print("\n=== Seeded Craftable Replica Demo Data ===")
    print(f"Location: {loc.name}")
    print("\nDemo logins (PIN):")
    print("  Owner   → 0000")
    print("  Manager → 1111")
    print("  Kitchen → 2222")
    print("  Server  → 3333")
    print("\nRecipe Costs (live):")
    for r in [of, caesar, chicken_caesar, bloody]:
        result = calculate_recipe_cost(db, r.id)
        print(f"  {result.recipe_name:30}  Cost/unit: ${result.cost_per_unit:6.3f}   "
              f"Cost %: {result.cost_percent or 0:5.1f}%   Menu: ${result.menu_price or 0:.2f}")

    print(f"\nPhysical Counts created:")
    print(f"  Count 1 (start): id={count1.id}  – {count1.name}")
    print(f"  Count 2 (end):   id={count2.id}  – {count2.name}")
    print("\nYou can now run a variance report between these two counts.")
    print(f"\nInvoices seeded: {inv1.invoice_number} (status: {inv1.status}, expect 'exception'), "
          f"{inv2.invoice_number} (status: {inv2.status})")
    print(f"\nScheduling: {len(shift_plan)} shifts seeded this week (some draft, one unfilled).")
    print("Time clock: Sam Manager is left clocked in from yesterday — try Time Clock > My clock as PIN 1111.")
    print("\nTraining: 2 courses seeded. Jordan Kitchen (2222) passed both on the first try; "
          "Casey Server (3333) failed New Hire Orientation once, then passed on retake.")
    print("Database ready. Run:  streamlit run frontend/app.py")
    db.close()


if __name__ == "__main__":
    seed()
