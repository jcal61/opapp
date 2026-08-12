"""
Craftable Replica – Interactive Management Dashboard
Includes: Inventory, Counts, Variance, Recipes, Purchasing workflow, POS simulation.
"""

import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st
import pandas as pd
from datetime import datetime, date, timezone, timedelta

from app.database import SessionLocal, engine, Base, ensure_schema
from app.models import (
    Location, InventoryItem, StockLevel, Recipe, Vendor,
    InventoryCount, PurchaseOrder, PurchaseOrderLine, User, Role,
)
from app.services.costing import calculate_recipe_cost
from app.services.recipes import (
    create_recipe, update_recipe, set_recipe_active, list_recipes,
    add_ingredient, remove_ingredient,
)
from app.services.inventory import (
    get_or_create_stock, record_pos_sale, log_waste,
    create_item, update_item, set_item_active, list_items,
    get_item_by_name, set_opening_stock,
)
from app.services.inventory_import import parse_workbook, ImportParseError
from app.services.recipe_import import (
    parse_batch_recipe_workbook, suggest_sub_recipe_links,
    parse_menu_costing_workbook,
    ImportParseError as RecipeImportParseError,
)
from app.services.variance import get_current_theoretical_snapshot, calculate_variance_between_counts
from app.services.counts import (
    create_count, add_or_update_count_line, close_count,
    get_open_counts, get_closed_counts, get_count_summary
)
from app.services.purchasing import (
    create_purchase_order, add_po_line, submit_po, receive_against_po,
    cancel_po, get_po_summary, list_purchase_orders, suggest_order_from_par,
    generate_po_number
)
from app.services.invoices import (
    create_invoice, add_invoice_line, delete_invoice_line, auto_match_to_po,
    approve_invoice, mark_paid, reject_invoice, list_invoices, get_invoice_summary,
    ap_aging_summary, find_vendor_by_name, create_invoice_from_extraction,
    find_item_by_name_or_sku,
)
from app.services.invoice_ocr import extract_invoice, InvoiceExtractionError
from app.services.checklists import (
    list_sops, list_templates, start_checklist_run, complete_task, finish_run,
    list_open_runs, get_run_progress, create_sop, create_template, add_task_to_template,
    location_checklist_report,
)
from app.services.locations import list_locations, create_location, update_location, set_location_active, get_location
from app.services.auth import (
    authenticate_pin, create_user, list_users, seed_roles, deactivate_user, user_can,
    get_role_permissions_map, set_role_permission, reset_role_permissions, permissions_for_user,
    set_hourly_rate,
)
from app.services.scheduling import (
    create_shift, update_shift, delete_shift, publish_shifts, list_shifts,
    scheduled_labor_cost, week_bounds,
)
from app.services.training import (
    create_course, set_course_active, list_courses, visible_courses_for_user,
    add_lesson, add_quiz_question, start_course, submit_quiz,
    list_completions_for_user, best_completion, training_report,
)

st.set_page_config(
    page_title="Craftable Replica",
    page_icon="🍽️",
    layout="wide",
    initial_sidebar_state="expanded",
)

ensure_schema()

def get_db():
    return SessionLocal()

# ---------- Auth helpers ----------
PAGE_PERMISSION = {
    "Dashboard": "dashboard",
    "Inventory & Stock": "inventory_view",
    "Purchasing": "purchasing",
    "Invoices (AP)": "invoices",
    "Physical Counts": "counts",
    "Variance Report": "variance",
    "Recipes & Costing": "recipes_view",
    "Simulate Sales (POS)": "pos_import",
    "Toast POS Import": "pos_import",
    "Waste & Adjustments": "waste",
    "Order Suggestions (Par)": "purchasing",
    "Users & Roles": "users_admin",
    "Locations Setup": "users_admin",
    "Checklists & SOPs": "checklists",
    "Scheduling": "scheduling_view",
    "Training & Quizzes": "training_view",
    "About the Model": "dashboard",
}

def current_user(db):
    uid = st.session_state.get("user_id")
    if not uid:
        return None
    return db.get(User, uid)

def require_perm(user, key: str) -> bool:
    return user_can(user, key, db)

def get_anthropic_api_key() -> str:
    """Read the Claude API key from Streamlit secrets (cloud) or an env var (local)."""
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY")
    except Exception:
        key = None
    return key or os.environ.get("ANTHROPIC_API_KEY", "")

# ---------- Login gate ----------
db = get_db()
seed_roles(db)

# First-run auto-seed: if there's no demo data yet (e.g. a fresh cloud deploy
# with an empty database), seed it automatically so there's no separate
# script to run — this makes the app self-contained for hosted deployments.
if not list_locations(db, active_only=False):
    with st.spinner("Setting up demo restaurant data (first run only, ~10 seconds)…"):
        db.close()
        from scripts.seed import seed
        seed()
    st.rerun()

if "user_id" not in st.session_state:
    st.session_state.user_id = None

user = current_user(db)

if user is None:
    st.title("🍽️ Craftable Login")
    st.caption("Enter your employee PIN")
    with st.form("login_form"):
        pin = st.text_input("PIN", type="password", max_chars=8)
        submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            u = authenticate_pin(db, pin)
            if u:
                st.session_state.user_id = u.id
                st.rerun()
            else:
                st.error("Invalid PIN or inactive user")
    st.info("Demo PINs after seeding: **0000** Owner · **1111** Manager · **2222** Kitchen · **3333** Server")
    st.stop()

# ---------- Sidebar (authenticated) ----------
st.sidebar.title("🍽️ Craftable")
st.sidebar.caption(f"{user.name} · {user.role.name if user.role else '—'}")
if st.sidebar.button("Sign out"):
    st.session_state.user_id = None
    st.rerun()

all_pages = [
    "Dashboard",
    "Inventory & Stock",
    "Purchasing",
    "Invoices (AP)",
    "Physical Counts",
    "Variance Report",
    "Recipes & Costing",
    "Simulate Sales (POS)",
    "Toast POS Import",
    "Waste & Adjustments",
    "Order Suggestions (Par)",
    "Users & Roles",
    "Locations Setup",
    "Checklists & SOPs",
    "Scheduling",
    "Training & Quizzes",
    "About the Model",
]
allowed_pages = [p for p in all_pages if require_perm(user, PAGE_PERMISSION.get(p, "dashboard"))]
if not allowed_pages:
    st.error("No permissions assigned to this role.")
    st.stop()

page = st.sidebar.radio("Navigate", allowed_pages)

all_locations = list_locations(db, active_only=False)
active_locations = [loc for loc in all_locations if loc.is_active]
if not all_locations:
    st.warning("No data found. Run:\n\n```bash\npython -m scripts.seed\n```")
    st.stop()

if "location_id" not in st.session_state:
    st.session_state.location_id = (active_locations[0].id if active_locations else all_locations[0].id)

# Location switcher
loc_options = {f"{loc.name} ({loc.code})": loc.id for loc in (active_locations or all_locations)}
# Keep selection valid
if st.session_state.location_id not in loc_options.values():
    st.session_state.location_id = list(loc_options.values())[0]

current_label = next((k for k, v in loc_options.items() if v == st.session_state.location_id), None)
chosen = st.sidebar.selectbox(
    "Working location",
    options=list(loc_options.keys()),
    index=list(loc_options.keys()).index(current_label) if current_label in loc_options else 0,
)
st.session_state.location_id = loc_options[chosen]
location = get_location(db, st.session_state.location_id)
if not location:
    st.error("Selected location not found.")
    st.stop()
st.sidebar.caption(f"{location.city or ''} {location.state or ''} · TZ {location.timezone or '—'} · Closeout {location.closeout_hour}:00")

# ---------- DASHBOARD ----------
if page == "Dashboard":
    st.title("Operations Dashboard")
    snapshot = get_current_theoretical_snapshot(db, location.id)
    df = pd.DataFrame(snapshot)

    col1, col2, col3, col4 = st.columns(4)
    below_par = df[df["below_par"] == True] if not df.empty else pd.DataFrame()
    total_value = (df["theoretical_qty"] * df["current_cost"]).sum() if not df.empty else 0
    open_pos = len(list_purchase_orders(db, location.id, status="submitted")) + \
               len(list_purchase_orders(db, location.id, status="partially_received"))

    col1.metric("Items Tracked", len(df))
    col2.metric("Below Par", len(below_par), delta_color="inverse")
    col3.metric("Theoretical Value", f"${total_value:,.0f}")
    col4.metric("Open POs", open_pos)

    if not df.empty:
        st.subheader("Stock Health")
        display = df[["name", "category", "theoretical_qty", "par_level", "base_unit", "current_cost", "below_par"]].copy()
        display.columns = ["Item", "Category", "Theoretical", "Par", "Unit", "Cost", "Below Par"]
        st.dataframe(display, use_container_width=True, height=380)

    if require_perm(user, "scheduling_edit"):
        wk_start, wk_end = week_bounds(date.today())
        labor = scheduled_labor_cost(db, location.id, wk_start, wk_end)
        st.subheader("Scheduled Labor — this week")
        l1, l2, l3, l4 = st.columns(4)
        l1.metric("Scheduled Hours", f"{labor['total_hours']:.1f}")
        l2.metric("Scheduled Cost", f"${labor['total_cost']:,.0f}")
        l3.metric("Sales", f"${labor['total_sales']:,.0f}")
        l4.metric("Labor Cost %", f"{labor['labor_cost_percent']:.1f}%" if labor["labor_cost_percent"] is not None else "—")

# ---------- INVENTORY ----------
elif page == "Inventory & Stock":
    st.title("Inventory & Theoretical Stock")

    tab_snap, tab_manage = st.tabs(["Stock Snapshot", "Manage Items"])

    with tab_snap:
        snapshot = get_current_theoretical_snapshot(db, location.id)
        df = pd.DataFrame(snapshot)
        if not df.empty:
            st.dataframe(
                df[["name", "category", "theoretical_qty", "last_physical", "par_level", "base_unit", "current_cost"]],
                use_container_width=True,
                column_config={
                    "theoretical_qty": st.column_config.NumberColumn("Theoretical", format="%.2f"),
                    "last_physical": st.column_config.NumberColumn("Last Physical", format="%.2f"),
                    "current_cost": st.column_config.NumberColumn("Cost", format="$%.3f"),
                },
            )
        else:
            st.info("No items yet — add one under Manage Items.")

    with tab_manage:
        if not require_perm(user, "inventory_edit"):
            st.warning("You do not have permission to add or edit inventory items.")
        else:
            vendors_for_items = {"(none)": None}
            vendors_for_items.update({v.name: v.id for v in db.query(Vendor).filter(Vendor.is_active == True).all()})

            st.subheader("Add New Item")
            with st.form("new_item_form", clear_on_submit=True):
                c1, c2 = st.columns(2)
                new_name = c1.text_input("Name *")
                new_sku = c2.text_input("SKU (optional)")
                c3, c4, c5 = st.columns(3)
                new_category = c3.text_input("Category", placeholder="Raw Food, Liquor, Paper…")
                new_subcategory = c4.text_input("Subcategory", placeholder="Spices, Vodka, Utensils…")
                new_unit = c5.text_input("Base Unit *", value="each", help="The unit you count/receive in, e.g. oz, lb, each, case")
                c6, c7 = st.columns(2)
                new_vendor = c6.selectbox("Preferred Vendor", list(vendors_for_items.keys()))
                new_cost = c7.number_input("Cost per Unit $", min_value=0.0, value=0.0, step=0.01, format="%.3f")
                new_par = st.number_input("Par Level", min_value=0.0, value=0.0, step=1.0)
                new_notes = st.text_area("Notes")
                if st.form_submit_button("Add Item", type="primary"):
                    if not new_name.strip() or not new_unit.strip():
                        st.error("Name and base unit are required.")
                    else:
                        try:
                            item = create_item(
                                db, new_name, new_unit,
                                sku=new_sku or None, category=new_category or None,
                                subcategory=new_subcategory or None,
                                current_cost=new_cost, par_level=new_par,
                                preferred_vendor_id=vendors_for_items[new_vendor],
                                notes=new_notes or None,
                            )
                            db.commit()
                            st.success(f"Added {item.name}")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

            st.divider()
            st.subheader("Edit Existing Item")
            all_items = list_items(db, active_only=False)
            if not all_items:
                st.info("No items yet.")
            else:
                item_labels = {f"{i.name}  {'(inactive)' if not i.is_active else ''}".strip(): i.id for i in all_items}
                pick_label = st.selectbox("Item", list(item_labels.keys()), key="edit_item_pick")
                edit_item = db.get(InventoryItem, item_labels[pick_label])

                with st.form("edit_item_form"):
                    c1, c2 = st.columns(2)
                    e_name = c1.text_input("Name *", value=edit_item.name)
                    e_sku = c2.text_input("SKU", value=edit_item.sku or "")
                    c3, c4, c5 = st.columns(3)
                    e_category = c3.text_input("Category", value=edit_item.category or "")
                    e_subcategory = c4.text_input("Subcategory", value=edit_item.subcategory or "")
                    e_unit = c5.text_input("Base Unit *", value=edit_item.base_unit)
                    current_vendor_name = next(
                        (n for n, vid in vendors_for_items.items() if vid == edit_item.preferred_vendor_id),
                        "(none)",
                    )
                    c6, c7 = st.columns(2)
                    e_vendor = c6.selectbox(
                        "Preferred Vendor", list(vendors_for_items.keys()),
                        index=list(vendors_for_items.keys()).index(current_vendor_name),
                    )
                    e_cost = c7.number_input("Cost per Unit $", min_value=0.0, value=float(edit_item.current_cost or 0), step=0.01, format="%.3f")
                    e_par = st.number_input("Par Level", min_value=0.0, value=float(edit_item.par_level or 0), step=1.0)
                    e_notes = st.text_area("Notes", value=edit_item.notes or "")
                    if st.form_submit_button("Save Changes", type="primary"):
                        try:
                            update_item(
                                db, edit_item.id,
                                name=e_name, base_unit=e_unit, sku=e_sku,
                                category=e_category, subcategory=e_subcategory,
                                current_cost=e_cost, par_level=e_par,
                                preferred_vendor_id=vendors_for_items[e_vendor], notes=e_notes,
                            )
                            db.commit()
                            st.success("Saved")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

                cdeact, _ = st.columns(2)
                if edit_item.is_active:
                    if cdeact.button("Deactivate item", key="deact_item"):
                        set_item_active(db, edit_item.id, False)
                        db.commit()
                        st.success(f"Deactivated {edit_item.name}")
                        st.rerun()
                else:
                    if cdeact.button("Reactivate item", key="react_item"):
                        set_item_active(db, edit_item.id, True)
                        db.commit()
                        st.success(f"Reactivated {edit_item.name}")
                        st.rerun()

            st.divider()
            st.subheader("Bulk Import from Spreadsheet")
            st.caption(
                "Upload your existing inventory workbook to add or update items in bulk, with "
                "categories carried over and a starting on-hand quantity set for each. Recognizes "
                "a food/supplies workbook (sheets like Raw, Paper, Cleaning) or a liquor/beer/wine "
                "workbook (an Inventory sheet alongside Summary/Contacts) — nothing is saved until "
                "you review the table below and click Import."
            )
            up_file = st.file_uploader("Inventory spreadsheet (.xlsx)", type=["xlsx"], key="inv_import_upload")
            kind_choice = st.selectbox(
                "Workbook format", ["Auto-detect", "Food & Supplies workbook", "Liquor, Beer & Wine workbook"],
                key="inv_import_kind",
            )
            kind_map = {
                "Auto-detect": None,
                "Food & Supplies workbook": "food",
                "Liquor, Beer & Wine workbook": "liquor",
            }

            if up_file is not None and st.button("Parse File", key="inv_import_parse"):
                try:
                    parsed = parse_workbook(up_file.getvalue(), kind=kind_map[kind_choice])
                    if not parsed:
                        st.warning("No rows were recognized in this file.")
                    else:
                        st.session_state["inv_import_rows"] = parsed
                        st.success(f"Parsed {len(parsed)} rows — review and edit below before importing.")
                except ImportParseError as e:
                    st.error(str(e))

            parsed_rows = st.session_state.get("inv_import_rows")
            if parsed_rows:
                import_df = pd.DataFrame(parsed_rows)[[
                    "name", "category", "subcategory", "base_unit", "sku",
                    "current_cost", "on_hand_qty", "par_level", "notes",
                ]]
                import_df.columns = [
                    "Item", "Category", "Subcategory", "Unit", "SKU",
                    "Cost", "On Hand Qty", "Par Level", "Notes",
                ]
                edited_import_df = st.data_editor(
                    import_df, use_container_width=True, height=420,
                    num_rows="dynamic", key="inv_import_editor",
                )
                total_value = (
                    edited_import_df["On Hand Qty"].fillna(0) * edited_import_df["Cost"].fillna(0)
                ).sum()
                existing_names = {i.name for i in db.query(InventoryItem).all()}
                will_update = sum(
                    1 for n in edited_import_df["Item"] if pd.notna(n) and str(n).strip() in existing_names
                )
                st.caption(
                    f"{len(edited_import_df)} row(s) · computed value ${total_value:,.2f} · "
                    f"{will_update} will update existing items, {len(edited_import_df) - will_update} will be created new."
                )

                ic1, ic2 = st.columns(2)
                if ic1.button("Import These Items", type="primary", key="inv_import_commit"):
                    created, updated, skipped = 0, 0, 0
                    for _, r in edited_import_df.iterrows():
                        name = r["Item"]
                        if pd.isna(name) or not str(name).strip():
                            skipped += 1
                            continue
                        name = str(name).strip()
                        cost = float(r["Cost"]) if pd.notna(r["Cost"]) else 0.0
                        on_hand = float(r["On Hand Qty"]) if pd.notna(r["On Hand Qty"]) else 0.0
                        par = float(r["Par Level"]) if pd.notna(r["Par Level"]) else 0.0
                        unit = str(r["Unit"]).strip() if pd.notna(r["Unit"]) and str(r["Unit"]).strip() else "each"
                        category = str(r["Category"]).strip() if pd.notna(r["Category"]) else None
                        subcategory = str(r["Subcategory"]).strip() if pd.notna(r["Subcategory"]) else None
                        sku = str(r["SKU"]).strip() if pd.notna(r["SKU"]) else None
                        notes = str(r["Notes"]).strip() if pd.notna(r["Notes"]) else None

                        existing = get_item_by_name(db, name)
                        try:
                            if existing:
                                update_item(
                                    db, existing.id, base_unit=unit, category=category,
                                    subcategory=subcategory, current_cost=cost, par_level=par, notes=notes,
                                )
                                item_id = existing.id
                                updated += 1
                            else:
                                new_item = create_item(
                                    db, name, unit, sku=sku, category=category, subcategory=subcategory,
                                    current_cost=cost, par_level=par, notes=notes,
                                )
                                item_id = new_item.id
                                created += 1
                            set_opening_stock(db, item_id, location.id, on_hand)
                        except ValueError:
                            skipped += 1
                            continue
                    db.commit()
                    st.success(f"Imported: {created} created, {updated} updated, {skipped} skipped.")
                    st.session_state.pop("inv_import_rows", None)
                    st.rerun()
                if ic2.button("Discard parsed data", key="inv_import_discard"):
                    st.session_state.pop("inv_import_rows", None)
                    st.rerun()

# ---------- PURCHASING (NEW) ----------
elif page == "Purchasing":
    st.title("Purchasing & Receiving")
    st.caption("Create POs from par suggestions or manually, submit, and receive goods into inventory.")

    tab1, tab2, tab3, tab4 = st.tabs([
        "Open & Recent POs",
        "Create Purchase Order",
        "Receive Goods",
        "Vendors"
    ])

    # --- Tab 1: List POs ---
    with tab1:
        status_filter = st.selectbox("Filter by status", ["All", "draft", "submitted", "partially_received", "received", "cancelled"])
        pos = list_purchase_orders(db, location.id, status=None if status_filter == "All" else status_filter)
        if not pos:
            st.info("No purchase orders yet.")
        else:
            for po in pos:
                summary = get_po_summary(db, po.id)
                status_color = {
                    "draft": "gray", "submitted": "blue", "partially_received": "orange",
                    "received": "green", "cancelled": "red"
                }.get(po.status, "gray")
                with st.expander(
                    f"**{summary['po_number']}**  ·  {summary['vendor']}  ·  "
                    f":{status_color}[{po.status.upper()}]  ·  ${summary['total_ordered']:.2f}"
                ):
                    st.write(f"Order date: {summary['order_date']}  |  Lines: {summary['line_count']}")
                    if summary["lines"]:
                        st.dataframe(pd.DataFrame(summary["lines"])[[
                            "item_name", "quantity_ordered", "quantity_received", "remaining", "unit", "unit_cost", "line_total"
                        ]], use_container_width=True)

                    c1, c2, c3 = st.columns(3)
                    if po.status == "draft" and require_perm(user, "purchasing_submit"):
                        if c1.button("Submit PO", key=f"submit_{po.id}"):
                            try:
                                submit_po(db, po.id)
                                db.commit()
                                st.success("PO submitted")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
                    if po.status in ("draft", "submitted", "partially_received") and require_perm(user, "purchasing_cancel"):
                        if c2.button("Cancel PO", key=f"cancel_{po.id}"):
                            try:
                                cancel_po(db, po.id)
                                db.commit()
                                st.success("PO cancelled")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

    # --- Tab 2: Create PO ---
    with tab2:
        st.subheader("Create New Purchase Order")
        if not require_perm(user, "purchasing_create"):
            st.warning("You do not have permission to create purchase orders.")

        # Option: Start from par suggestions
        with st.expander("Quick-fill from items below par", expanded=True):
            suggestions = suggest_order_from_par(db, location.id)
            if not suggestions:
                st.success("Nothing currently below par.")
            else:
                st.write(f"{len(suggestions)} items below par")
                sug_df = pd.DataFrame(suggestions)[["name", "current_theoretical", "par_level", "suggested_qty", "unit", "est_cost"]]
                st.dataframe(sug_df, use_container_width=True)

                vendors = {v.name: v.id for v in db.query(Vendor).filter(Vendor.is_active == True).all()}
                chosen_vendor = st.selectbox("Vendor for this PO", list(vendors.keys()), key="par_vendor")

                if require_perm(user, "purchasing_create") and st.button("Create PO from all suggestions", type="primary"):
                    po = create_purchase_order(db, vendors[chosen_vendor], location.id)
                    for s in suggestions:
                        add_po_line(db, po.id, s["item_id"], s["suggested_qty"], s["unit"], s["unit_cost"])
                    db.commit()
                    st.success(f"Created {po.po_number} with {len(suggestions)} lines")
                    st.rerun()

        st.divider()
        st.subheader("Manual PO")
        st.caption("Pick items, adjust quantity/unit/cost for each in the table, then create the whole PO in one step.")
        vendors = {v.name: v.id for v in db.query(Vendor).filter(Vendor.is_active == True).all()}
        items = {
            f"{i.name} ({i.base_unit})": i
            for i in db.query(InventoryItem).filter(InventoryItem.is_active == True)
            .order_by(InventoryItem.category, InventoryItem.name).all()
        }

        if not vendors:
            st.warning("No vendors yet — add one in the Vendors tab first.")
        elif not items:
            st.warning("No inventory items yet — add one under Inventory & Stock first.")
        else:
            vendor_name = st.selectbox("Vendor", list(vendors.keys()), key="manual_po_vendor")
            po_notes = st.text_input("Notes", key="manual_po_notes")
            line_pick = st.multiselect("Items to include", list(items.keys()), key="manual_po_items")

            if line_pick:
                line_df = pd.DataFrame([{
                    "Item": label,
                    "Quantity": 10.0,
                    "Unit": items[label].base_unit,
                    "Unit Cost": float(items[label].current_cost or 0),
                } for label in line_pick])
                edited_lines = st.data_editor(
                    line_df, use_container_width=True, disabled=["Item"], key="manual_po_line_editor"
                )
                if st.button("Create PO With These Lines", type="primary", key="manual_po_create"):
                    po = create_purchase_order(db, vendors[vendor_name], location.id, notes=po_notes or None)
                    for _, row in edited_lines.iterrows():
                        item = items[row["Item"]]
                        add_po_line(db, po.id, item.id, float(row["Quantity"]), row["Unit"], float(row["Unit Cost"]))
                    db.commit()
                    st.success(f"Created {po.po_number} with {len(edited_lines)} line(s)")
                    st.rerun()
            else:
                st.info("Pick one or more items above to start building lines.")

        # Add lines to existing draft
        st.divider()
        drafts = list_purchase_orders(db, location.id, status="draft")
        if drafts and items:
            st.subheader("Add lines to an existing Draft PO")
            draft_options = {f"{p.po_number} ({p.vendor.name})": p.id for p in drafts}
            sel_po = st.selectbox("Draft PO", list(draft_options.keys()), key="addline_po_pick")
            add_pick = st.multiselect("Items to add", list(items.keys()), key="addline_items")
            if add_pick:
                add_df = pd.DataFrame([{
                    "Item": label,
                    "Quantity": 5.0,
                    "Unit": items[label].base_unit,
                    "Unit Cost": float(items[label].current_cost or 0),
                } for label in add_pick])
                edited_add_df = st.data_editor(
                    add_df, use_container_width=True, disabled=["Item"], key="addline_editor"
                )
                if st.button("Add These Lines", type="primary", key="addline_submit"):
                    for _, row in edited_add_df.iterrows():
                        item = items[row["Item"]]
                        add_po_line(db, draft_options[sel_po], item.id, float(row["Quantity"]), row["Unit"], float(row["Unit Cost"]))
                    db.commit()
                    st.success(f"Added {len(edited_add_df)} line(s)")
                    st.rerun()

    # --- Tab 3: Receive ---
    with tab3:
        st.subheader("Receive Goods Against a PO")
        if not require_perm(user, "purchasing_receive"):
            st.warning("You do not have permission to receive goods.")
        receivable = list_purchase_orders(db, location.id, status="submitted") + \
                     list_purchase_orders(db, location.id, status="partially_received")
        if not receivable:
            st.info("No submitted or partially received POs available to receive against.")
        else:
            po_opts = {f"{p.po_number} – {p.vendor.name} ({p.status})": p.id for p in receivable}
            selected = st.selectbox("Select PO to receive", list(po_opts.keys()))
            po_id = po_opts[selected]
            summary = get_po_summary(db, po_id)

            st.write(f"**{summary['po_number']}** from {summary['vendor']}")
            receipts = []
            for line in summary["lines"]:
                remaining = line["remaining"]
                col1, col2, col3 = st.columns([3, 2, 2])
                col1.write(f"{line['item_name']}  (ordered {line['quantity_ordered']} {line['unit']})")
                col2.write(f"Already received: {line['quantity_received']}")
                qty_recv = col3.number_input(
                    "Receive now",
                    min_value=0.0,
                    value=float(max(0, remaining)),
                    step=0.5,
                    key=f"recv_{line['line_id']}"
                )
                if qty_recv > 0:
                    receipts.append({"line_id": line["line_id"], "quantity": qty_recv})

            notes = st.text_input("Receiving notes")
            if st.button("Confirm Receiving", type="primary"):
                if not receipts:
                    st.warning("Enter at least one quantity to receive.")
                else:
                    try:
                        receiving = receive_against_po(db, po_id, location.id, receipts, notes=notes or None)
                        db.commit()
                        st.success(f"Receiving recorded (ID {receiving.id}). Theoretical inventory updated.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

    # --- Tab 4: Vendors ---
    with tab4:
        st.subheader("Vendors")
        vendors = db.query(Vendor).all()
        if vendors:
            rows = [{"ID": v.id, "Name": v.name, "Code": v.code, "Email": v.contact_email or "", "Active": v.is_active} for v in vendors]
            st.dataframe(pd.DataFrame(rows), use_container_width=True)

        with st.form("new_vendor"):
            st.write("Add Vendor")
            vname = st.text_input("Name")
            vcode = st.text_input("Code")
            vemail = st.text_input("Email")
            if st.form_submit_button("Create Vendor"):
                if vname:
                    v = Vendor(name=vname, code=vcode or None, contact_email=vemail or None)
                    db.add(v)
                    db.commit()
                    st.success(f"Created vendor {vname}")
                    st.rerun()

# ---------- INVOICES (AP) ----------
elif page == "Invoices (AP)":
    st.title("Invoices — Accounts Payable")
    st.caption("Capture vendor invoices, run a 3-way match against purchase orders (ordered vs received vs billed), and track exceptions through to payment.")

    tab1, tab2, tab3 = st.tabs(["Open & Recent Invoices", "Capture Invoice", "AP Summary"])

    vendors_all = {v.name: v.id for v in db.query(Vendor).filter(Vendor.is_active == True).all()}
    items_all = {f"{i.name} ({i.base_unit})": i for i in db.query(InventoryItem).filter(InventoryItem.is_active == True).all()}

    # --- Tab 1: List + match + approve/pay ---
    with tab1:
        status_filter = st.selectbox(
            "Filter by status", ["All", "received", "matched", "exception", "approved", "paid", "rejected"]
        )
        invoices = list_invoices(db, location.id, status=None if status_filter == "All" else status_filter)
        if not invoices:
            st.info("No invoices yet.")
        else:
            for inv in invoices:
                summary = get_invoice_summary(db, inv.id)
                header = (
                    f"**{summary['invoice_number'] or f'Invoice #{inv.id}'}**  ·  {summary['vendor']}  ·  "
                    f":{'red' if inv.status == 'exception' else 'green' if inv.status in ('matched', 'approved', 'paid') else 'gray'}"
                    f"[{inv.status.upper()}]  ·  ${(summary['total_amount'] or 0):.2f}"
                )
                with st.expander(header):
                    c1, c2 = st.columns(2)
                    c1.write(f"Invoice date: {summary['invoice_date'] or '—'}  |  Due: {summary['due_date'] or '—'}")
                    c2.write(f"Linked PO: {summary['po_number'] or 'None (non-PO invoice)'}")
                    if summary["lines"]:
                        lines_df = pd.DataFrame(summary["lines"])[
                            ["description", "item_name", "quantity", "unit", "unit_price", "line_total", "match_status"]
                        ]
                        st.dataframe(lines_df, use_container_width=True)
                    else:
                        st.caption("No line items yet — add them under Capture Invoice.")

                    ac1, ac2, ac3, ac4 = st.columns(4)
                    if summary["purchase_order_id"] and require_perm(user, "invoices"):
                        if ac1.button("Match to PO", key=f"match_{inv.id}"):
                            result = auto_match_to_po(db, inv.id)
                            db.commit()
                            if result["status"] == "exception":
                                st.warning(
                                    f"Exceptions found — total variance ${result['total_variance_dollars']:.2f} "
                                    f"(price ${result['total_price_variance_dollars']:.2f}, qty ${result['total_qty_variance_dollars']:.2f})"
                                )
                            else:
                                st.success("All lines matched cleanly.")
                            st.rerun()
                    if inv.status in ("received", "matched", "exception") and require_perm(user, "invoices"):
                        if ac2.button("Approve", key=f"approve_{inv.id}"):
                            try:
                                approve_invoice(db, inv.id)
                                db.commit()
                                st.success("Invoice approved")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
                    if inv.status == "approved" and require_perm(user, "invoices"):
                        if ac3.button("Mark Paid", key=f"paid_{inv.id}"):
                            mark_paid(db, inv.id)
                            db.commit()
                            st.success("Marked paid")
                            st.rerun()
                    if inv.status not in ("paid", "rejected") and require_perm(user, "invoices"):
                        if ac4.button("Reject", key=f"reject_{inv.id}"):
                            reject_invoice(db, inv.id)
                            db.commit()
                            st.warning("Invoice rejected")
                            st.rerun()

    # --- Tab 2: Capture ---
    with tab2:
        st.subheader("Capture New Invoice")
        if not require_perm(user, "invoices"):
            st.warning("You do not have permission to capture invoices.")

        po_options = {"None (non-PO invoice)": None}
        for po in list_purchase_orders(db, location.id, status=None):
            if po.status in ("submitted", "partially_received", "received"):
                po_options[f"{po.po_number} – {po.vendor.name} ({po.status})"] = po.id

        # --- AI-assisted capture from a photo or PDF ---
        with st.expander("📷 Extract from photo or PDF (AI-assisted)", expanded=True):
            st.caption(
                "Upload a clear photo or PDF of the invoice. Claude reads it and pre-fills the "
                "fields below — always review before saving, AI extraction can misread a digit."
            )
            uploaded = st.file_uploader(
                "Invoice photo or PDF", type=["jpg", "jpeg", "png", "webp", "pdf"], key="ocr_upload"
            )

            if uploaded is not None and st.button("Extract with AI", type="primary"):
                api_key = get_anthropic_api_key()
                with st.spinner("Reading invoice…"):
                    try:
                        result = extract_invoice(uploaded.getvalue(), uploaded.name, api_key)
                        st.session_state["ocr_extraction"] = result.data
                        st.session_state["ocr_raw"] = result.raw_text
                        st.session_state["ocr_filename"] = uploaded.name
                        for w in result.warnings:
                            st.warning(w)
                        st.success("Extracted — review and correct the fields below before saving.")
                    except InvoiceExtractionError as e:
                        st.error(str(e))

            extraction = st.session_state.get("ocr_extraction")
            if extraction:
                st.markdown("**Review extracted invoice**")

                vendors_active = db.query(Vendor).filter(Vendor.is_active == True).all()
                vendor_names = [v.name for v in vendors_active]
                if not vendor_names:
                    st.error("No vendors exist yet — add one in the Vendors tab first, then come back.")
                else:
                    vendor_guess = find_vendor_by_name(db, extraction.get("vendor_name") or "")
                    default_idx = (
                        vendor_names.index(vendor_guess.name)
                        if vendor_guess and vendor_guess.name in vendor_names else 0
                    )
                    c1, c2 = st.columns(2)
                    chosen_vendor_name = c1.selectbox(
                        f"Vendor  (AI read: “{extraction.get('vendor_name') or '—'}”)",
                        vendor_names, index=default_idx, key="ocr_vendor_pick",
                    )
                    po_label_ocr = c2.selectbox(
                        "Link to Purchase Order (optional)", list(po_options.keys()), key="ocr_po_pick"
                    )

                    inv_num = st.text_input(
                        "Invoice Number", value=extraction.get("invoice_number") or "", key="ocr_inv_num"
                    )
                    c3, c4 = st.columns(2)

                    def _safe_date(s, fallback):
                        try:
                            return datetime.strptime(s, "%Y-%m-%d").date() if s else fallback
                        except ValueError:
                            return fallback

                    inv_date = c3.date_input(
                        "Invoice Date",
                        value=_safe_date(extraction.get("invoice_date"), date.today()),
                        key="ocr_inv_date",
                    )
                    due_date_val = c4.date_input(
                        "Due Date",
                        value=_safe_date(extraction.get("due_date"), date.today() + timedelta(days=30)),
                        key="ocr_due_date",
                    )
                    inv_total = st.number_input(
                        "Invoice Total (as printed)",
                        value=float(extraction.get("invoice_total") or 0.0),
                        step=0.01, format="%.2f", key="ocr_total",
                    )

                    st.caption(
                        "Line items — edit any cell to fix a misread. **Qty Shipped** drives billing; "
                        "**Qty Ordered** is just for reference (spotting backorders/shortages)."
                    )
                    li_rows = extraction.get("line_items") or []
                    df_init = pd.DataFrame([{
                        "Item": r.get("item_name") or "",
                        "SKU": r.get("sku") or "",
                        "Qty Ordered": r.get("quantity_ordered"),
                        "Qty Shipped": r.get("quantity_shipped"),
                        "Unit": r.get("unit") or "",
                        "Unit Price": r.get("unit_price"),
                        "Line Total": r.get("line_total"),
                    } for r in li_rows]) if li_rows else pd.DataFrame(
                        columns=["Item", "SKU", "Qty Ordered", "Qty Shipped", "Unit", "Unit Price", "Line Total"]
                    )
                    edited_df = st.data_editor(
                        df_init, num_rows="dynamic", use_container_width=True, key="ocr_line_editor"
                    )

                    lines_sum = (
                        (edited_df["Qty Shipped"].fillna(0) * edited_df["Unit Price"].fillna(0)).sum()
                        if not edited_df.empty else 0.0
                    )
                    st.caption(f"Sum of edited lines: ${lines_sum:,.2f}" + (f"  ·  printed total: ${inv_total:,.2f}" if inv_total else ""))
                    if inv_total and abs(lines_sum - inv_total) > 0.05:
                        st.warning(
                            f"Line items (${lines_sum:,.2f}) don't add up to the printed total "
                            f"(${inv_total:,.2f}) — check for a missing line, tax, or shipping charge."
                        )

                    colA, colB = st.columns(2)
                    if colA.button("Create Invoice From Extracted Data", type="primary"):
                        vendor_obj = db.query(Vendor).filter(Vendor.name == chosen_vendor_name).first()

                        def _clean(v):
                            """NaN (from an empty data_editor cell) -> None, otherwise pass through."""
                            return None if pd.isna(v) else v

                        extraction_to_save = {
                            "invoice_number": inv_num or None,
                            "invoice_date": inv_date.isoformat(),
                            "due_date": due_date_val.isoformat(),
                            "invoice_total": inv_total or None,
                            "line_items": [
                                {
                                    "item_name": row["Item"],
                                    "sku": _clean(row["SKU"]) or None,
                                    "quantity_ordered": _clean(row["Qty Ordered"]),
                                    "quantity_shipped": _clean(row["Qty Shipped"]),
                                    "unit": _clean(row["Unit"]) or None,
                                    "unit_price": _clean(row["Unit Price"]),
                                    "line_total": _clean(row["Line Total"]),
                                }
                                for _, row in edited_df.iterrows()
                                if pd.notna(row["Item"]) and str(row["Item"]).strip()
                            ],
                        }
                        new_inv = create_invoice_from_extraction(
                            db, extraction_to_save,
                            vendor_id=vendor_obj.id, location_id=location.id,
                            raw_json=st.session_state.get("ocr_raw"),
                            original_filename=st.session_state.get("ocr_filename"),
                            purchase_order_id=po_options[po_label_ocr],
                        )
                        db.commit()
                        st.success(
                            f"Created invoice #{new_inv.id} with {len(extraction_to_save['line_items'])} "
                            f"line item(s). See it under **Open & Recent Invoices** above."
                        )
                        for k in ("ocr_extraction", "ocr_raw", "ocr_filename"):
                            st.session_state.pop(k, None)
                        st.rerun()
                    if colB.button("Discard extracted data"):
                        for k in ("ocr_extraction", "ocr_raw", "ocr_filename"):
                            st.session_state.pop(k, None)
                        st.rerun()

        st.divider()
        st.subheader("Or capture manually")
        with st.form("new_invoice"):
            vendor_name = st.selectbox("Vendor", list(vendors_all.keys()))
            po_label = st.selectbox("Link to Purchase Order (optional, enables matching)", list(po_options.keys()))
            c1, c2 = st.columns(2)
            invoice_number = c1.text_input("Invoice Number")
            invoice_date = c2.date_input("Invoice Date", value=date.today())
            due_date = st.date_input("Due Date", value=date.today() + timedelta(days=30))
            notes = st.text_area("Notes")
            if st.form_submit_button("Create Invoice", type="primary"):
                inv = create_invoice(
                    db,
                    vendor_id=vendors_all[vendor_name],
                    location_id=location.id,
                    invoice_number=invoice_number or None,
                    invoice_date=invoice_date,
                    due_date=due_date,
                    purchase_order_id=po_options[po_label],
                    notes=notes or None,
                )
                db.commit()
                st.success(f"Created invoice #{inv.id}. Add line items below.")
                st.session_state["active_invoice_id"] = inv.id
                st.rerun()

        st.divider()
        st.subheader("Add Line Items")
        open_invoices = [i for i in list_invoices(db, location.id) if i.status in ("received", "exception", "matched")]
        if not open_invoices:
            st.info("Create an invoice above first.")
        else:
            inv_options = {
                f"{i.invoice_number or f'Invoice #{i.id}'} – {i.vendor.name if i.vendor else ''}": i.id
                for i in open_invoices
            }
            default_idx = 0
            active_id = st.session_state.get("active_invoice_id")
            ids_in_order = list(inv_options.values())
            if active_id in ids_in_order:
                default_idx = ids_in_order.index(active_id)

            with st.form("add_invoice_line"):
                sel_label = st.selectbox("Invoice", list(inv_options.keys()), index=default_idx)
                sel_id = inv_options[sel_label]
                use_item = st.checkbox("Match against an inventory item (recommended for PO-linked invoices)", value=True)
                if use_item and items_all:
                    item_label = st.selectbox("Item", list(items_all.keys()))
                    description = items_all[item_label].name
                else:
                    item_label = None
                    description = st.text_input("Description (e.g. a non-inventory charge)", value="")
                qty = st.number_input("Quantity", min_value=0.01, value=1.0, step=0.5)
                unit = st.text_input("Unit", value=items_all[item_label].base_unit if item_label else "each")
                unit_price = st.number_input("Unit Price $", min_value=0.0, value=0.0, step=0.01, format="%.3f")
                if st.form_submit_button("Add Line"):
                    add_invoice_line(
                        db,
                        invoice_id=sel_id,
                        description=description or "Line item",
                        quantity=qty,
                        unit_price=unit_price,
                        unit=unit,
                        item_id=items_all[item_label].id if item_label else None,
                    )
                    db.commit()
                    st.success("Line added. Header total updated.")
                    st.session_state["active_invoice_id"] = sel_id
                    st.rerun()

    # --- Tab 3: AP Summary ---
    with tab3:
        st.subheader("Accounts Payable Summary")
        agg = ap_aging_summary(db, location.id)
        c1, c2, c3 = st.columns(3)
        c1.metric("Open Payable", f"${agg['total_open_payable']:,.2f}")
        c2.metric("Invoices", agg["invoice_count"])
        c3.metric("Exceptions Needing Review", agg["exception_count"], delta_color="inverse")
        if agg["by_status"]:
            rows = [{"Status": k.upper(), "Total $": v} for k, v in agg["by_status"].items()]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption("Matched/exception status comes from the 3-way match: PO ordered price/qty vs. what was actually received vs. what the vendor billed.")

# ---------- PHYSICAL COUNTS ----------
elif page == "Physical Counts":
    st.title("Physical Inventory Counts")
    tab1, tab2, tab3 = st.tabs(["Open Counts", "Start New Count", "Closed Counts"])

    with tab1:
        open_counts = get_open_counts(db, location.id)
        if not open_counts:
            st.info("No open counts.")
        else:
            for c in open_counts:
                with st.expander(f"**{c.name}** (ID {c.id})", expanded=True):
                    summary = get_count_summary(db, c.id)
                    existing_qty = {l["item_id"]: l["counted_qty"] for l in summary.get("lines", [])}
                    all_items_for_count = (
                        db.query(InventoryItem)
                        .filter(InventoryItem.is_active == True)
                        .order_by(InventoryItem.category, InventoryItem.name)
                        .all()
                    )
                    if require_perm(user, "counts_enter"):
                        st.caption(
                            "Enter counted quantities below — leave a row blank for items you haven't "
                            "gotten to yet. **Save** applies every filled-in row at once."
                        )
                        count_df = pd.DataFrame([{
                            "Item": i.name,
                            "Category": i.category or "",
                            "Unit": i.base_unit,
                            "Counted Qty": existing_qty.get(i.id),
                        } for i in all_items_for_count])
                        edited_count_df = st.data_editor(
                            count_df,
                            use_container_width=True,
                            height=380,
                            disabled=["Item", "Category", "Unit"],
                            key=f"count_editor_{c.id}",
                        )
                        if st.button("Save Counted Quantities", key=f"save_count_{c.id}", type="primary"):
                            saved = 0
                            for (_, row), item in zip(edited_count_df.iterrows(), all_items_for_count):
                                qty = row["Counted Qty"]
                                if pd.notna(qty):
                                    add_or_update_count_line(db, c.id, item.id, float(qty))
                                    saved += 1
                            db.commit()
                            st.success(f"Saved {saved} counted item(s).")
                            st.rerun()
                    else:
                        st.caption("You can view this count but cannot enter quantities.")
                    if summary.get("lines"):
                        st.markdown("**Recorded so far:**")
                        st.dataframe(pd.DataFrame(summary["lines"])[["name", "counted_qty", "base_unit"]], use_container_width=True)
                    if require_perm(user, "counts_close"):
                        c1, c2 = st.columns(2)
                        if require_perm(user, "counts_align") and c1.button("Close & Align Theoretical", key=f"ca_{c.id}"):
                            close_count(db, c.id, align_theoretical=True)
                            db.commit()
                            st.success("Closed and aligned")
                            st.rerun()
                        if c2.button("Close (keep theoretical)", key=f"ck_{c.id}"):
                            close_count(db, c.id, align_theoretical=False)
                            db.commit()
                            st.success("Closed")
                            st.rerun()
                    else:
                        st.caption("Only managers can close counts.")

    with tab2:
        with st.form("new_count"):
            name = st.text_input("Count Name", value=f"Count {datetime.now().strftime('%Y-%m-%d')}")
            notes = st.text_area("Notes")
            if st.form_submit_button("Create Open Count", type="primary"):
                create_count(db, location.id, name=name, notes=notes or None)
                db.commit()
                st.success("Count created")
                st.rerun()

    with tab3:
        for c in get_closed_counts(db, location.id):
            st.write(f"**{c.name}** (ID {c.id}) – {c.counted_at.strftime('%Y-%m-%d %H:%M') if c.counted_at else ''} – {len(c.lines)} lines")

# ---------- VARIANCE ----------
elif page == "Variance Report":
    st.title("Variance Report")
    st.caption("Theoretical vs Actual between two physical counts")
    closed = get_closed_counts(db, location.id, limit=30)
    if len(closed) < 2:
        st.warning("Need at least two closed counts.")
    else:
        options = {f"{c.name} (ID {c.id}) – {c.counted_at.strftime('%Y-%m-%d') if c.counted_at else ''}": c.id for c in closed}
        col1, col2 = st.columns(2)
        start_label = col1.selectbox("Starting Count", list(options.keys()), index=min(1, len(options)-1))
        end_label = col2.selectbox("Ending Count", list(options.keys()), index=0)
        if st.button("Run Variance Report", type="primary"):
            results = calculate_variance_between_counts(db, location.id, options[start_label], options[end_label])
            total_var = sum(abs(r.variance_cost) for r in results)
            m1, m2, m3 = st.columns(3)
            m1.metric("Items", len(results))
            m2.metric("With Variance", len([r for r in results if abs(r.variance_qty) > 0.1]))
            if require_perm(user, "view_costs"):
                m3.metric("Total |Variance| $", f"${total_var:,.2f}")
            else:
                m3.metric("Cost visibility", "Hidden")
            rows = [{
                "Item": r.item_name, "Unit": r.base_unit,
                "Start": r.starting_physical, "Purchases": r.purchases,
                "POS Depletions": r.pos_depletions, "Waste": r.waste,
                "Theo Ending": r.theoretical_ending, "Actual": r.actual_ending,
                "Var Qty": r.variance_qty, "Var $": r.variance_cost,
            } for r in results]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, height=450)

# ---------- RECIPES ----------
elif page == "Recipes & Costing":
    st.title("Recipes & Live Costing")

    tab_view, tab_manage, tab_bulk, tab_menu_bulk = st.tabs(
        ["View & Cost", "Manage Recipes", "Import Batch Recipes", "Import Menu Items"]
    )

    with tab_view:
        for recipe in db.query(Recipe).filter(Recipe.is_active == True).order_by(Recipe.name).all():
            with st.expander(f"**{recipe.name}**  ·  ${recipe.menu_price or 0:.2f}"):
                try:
                    result = calculate_recipe_cost(db, recipe.id)
                    if require_perm(user, "view_costs"):
                        c1, c2, c3 = st.columns(3)
                        c1.metric("Cost / Serving", f"${result.cost_per_unit:.3f}")
                        c2.metric("Food Cost %", f"{result.cost_percent or 0:.1f}%")
                        c3.metric("Total Recipe Cost", f"${result.total_cost:.3f}")
                        st.table(pd.DataFrame([{"Component": l.name, "Qty": f"{l.quantity} {l.unit}", "Cost": f"${l.cost:.3f}"} for l in result.breakdown]))
                    else:
                        st.caption("Ingredient list (costs hidden for your role)")
                        st.table(pd.DataFrame([{"Component": l.name, "Qty": f"{l.quantity} {l.unit}"} for l in result.breakdown]))
                except Exception as e:
                    st.error(str(e))

    with tab_manage:
        if not require_perm(user, "recipes_edit"):
            st.warning("You do not have permission to add or edit recipes.")
        else:
            st.subheader("Create New Recipe")
            with st.form("new_recipe_form", clear_on_submit=True):
                c1, c2 = st.columns(2)
                r_name = c1.text_input("Name *")
                r_category = c2.text_input("Category", placeholder="cocktail, entree, appetizer…")
                c3, c4, c5 = st.columns(3)
                r_yield_qty = c3.number_input("Yield Qty", min_value=0.01, value=1.0, step=0.5)
                r_yield_unit = c4.text_input("Yield Unit", value="serving")
                r_menu_price = c5.number_input("Menu Price $", min_value=0.0, value=0.0, step=0.5)
                r_desc = st.text_area("Description")
                r_instructions = st.text_area("Instructions")
                if st.form_submit_button("Create Recipe", type="primary"):
                    if not r_name.strip():
                        st.error("Name is required.")
                    else:
                        try:
                            r = create_recipe(
                                db, r_name, yield_qty=r_yield_qty, yield_unit=r_yield_unit,
                                menu_price=r_menu_price or None, category=r_category or None,
                                description=r_desc or None, instructions=r_instructions or None,
                            )
                            db.commit()
                            st.success(f"Created {r.name} — add ingredients below.")
                            st.session_state["edit_recipe_id"] = r.id
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

            st.divider()
            st.subheader("Edit Recipe & Ingredients")
            all_recipes = db.query(Recipe).order_by(Recipe.name).all()
            if not all_recipes:
                st.info("No recipes yet.")
            else:
                recipe_labels = {f"{r.name}{' (inactive)' if not r.is_active else ''}": r.id for r in all_recipes}
                ids_in_order = list(recipe_labels.values())
                default_idx = 0
                target_id = st.session_state.get("edit_recipe_id")
                if target_id in ids_in_order:
                    default_idx = ids_in_order.index(target_id)
                pick_label = st.selectbox("Recipe", list(recipe_labels.keys()), index=default_idx, key="edit_recipe_pick")
                recipe = db.get(Recipe, recipe_labels[pick_label])
                st.session_state["edit_recipe_id"] = recipe.id

                with st.form("edit_recipe_form"):
                    c1, c2 = st.columns(2)
                    e_name = c1.text_input("Name *", value=recipe.name)
                    e_category = c2.text_input("Category", value=recipe.category or "")
                    c3, c4, c5 = st.columns(3)
                    e_yield_qty = c3.number_input("Yield Qty", min_value=0.01, value=float(recipe.yield_qty or 1), step=0.5)
                    e_yield_unit = c4.text_input("Yield Unit", value=recipe.yield_unit)
                    e_menu_price = c5.number_input("Menu Price $", min_value=0.0, value=float(recipe.menu_price or 0), step=0.5)
                    e_desc = st.text_area("Description", value=recipe.description or "")
                    e_instructions = st.text_area("Instructions", value=recipe.instructions or "")
                    if st.form_submit_button("Save Recipe Details", type="primary"):
                        try:
                            update_recipe(
                                db, recipe.id, name=e_name, category=e_category,
                                yield_qty=e_yield_qty, yield_unit=e_yield_unit,
                                menu_price=e_menu_price or None, description=e_desc, instructions=e_instructions,
                            )
                            db.commit()
                            st.success("Saved")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

                cdeact, _ = st.columns(2)
                if recipe.is_active:
                    if cdeact.button("Deactivate recipe", key="deact_recipe"):
                        set_recipe_active(db, recipe.id, False)
                        db.commit()
                        st.success(f"Deactivated {recipe.name}")
                        st.rerun()
                else:
                    if cdeact.button("Reactivate recipe", key="react_recipe"):
                        set_recipe_active(db, recipe.id, True)
                        db.commit()
                        st.success(f"Reactivated {recipe.name}")
                        st.rerun()

                st.markdown("**Ingredients**")
                if recipe.ingredients:
                    for ing in sorted(recipe.ingredients, key=lambda x: x.sort_order or 0):
                        label = ing.item.name if ing.item else f"[sub-recipe] {ing.sub_recipe.name if ing.sub_recipe else '—'}"
                        ic1, ic2 = st.columns([5, 1])
                        ic1.write(f"{label} — {ing.quantity} {ing.unit}" + (" (throwaway)" if ing.is_throwaway else ""))
                        if ic2.button("Remove", key=f"rm_ing_{ing.id}"):
                            remove_ingredient(db, ing.id)
                            db.commit()
                            st.rerun()
                else:
                    st.caption("No ingredients yet.")

                st.markdown("**Add Ingredient**")
                items_for_recipe = {i.name: i for i in db.query(InventoryItem).filter(InventoryItem.is_active == True).order_by(InventoryItem.name).all()}
                subrecipe_options = {r.name: r.id for r in all_recipes if r.id != recipe.id and r.is_active}
                with st.form("add_ingredient_form", clear_on_submit=True):
                    ing_kind = st.radio("Ingredient type", ["Inventory item", "Sub-recipe"], horizontal=True)
                    if ing_kind == "Inventory item":
                        if not items_for_recipe:
                            st.warning("No inventory items yet — add one under Inventory & Stock.")
                            chosen_item = None
                        else:
                            chosen_item_label = st.selectbox("Item", list(items_for_recipe.keys()))
                            chosen_item = items_for_recipe[chosen_item_label]
                        chosen_sub_id = None
                        default_unit = chosen_item.base_unit if items_for_recipe else "each"
                    else:
                        if not subrecipe_options:
                            st.warning("No other active recipes to use as a sub-recipe.")
                            chosen_sub_id = None
                        else:
                            chosen_sub_label = st.selectbox("Sub-recipe", list(subrecipe_options.keys()))
                            chosen_sub_id = subrecipe_options[chosen_sub_label]
                        chosen_item = None
                        default_unit = "each"

                    c1, c2 = st.columns(2)
                    ing_qty = c1.number_input("Quantity", min_value=0.01, value=1.0, step=0.25)
                    ing_unit = c2.text_input("Unit", value=default_unit)
                    ing_throwaway = st.checkbox("Throwaway (costed but not part of yield weight)")
                    ing_notes = st.text_input("Notes")

                    if st.form_submit_button("Add Ingredient", type="primary"):
                        try:
                            add_ingredient(
                                db, recipe.id, ing_qty, ing_unit,
                                item_id=chosen_item.id if chosen_item else None,
                                sub_recipe_id=chosen_sub_id,
                                is_throwaway=ing_throwaway, notes=ing_notes or None,
                            )
                            db.commit()
                            st.success("Ingredient added")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

    with tab_bulk:
        if not require_perm(user, "recipes_edit"):
            st.warning("You do not have permission to add or edit recipes.")
        else:
            st.subheader("Bulk Import Batch/Prep Recipes")
            st.caption(
                "Upload a batch recipe costing workbook — foundational prep items (rubs, sauces, "
                "cooked proteins, etc.) that go *into* menu items but aren't sold on their own. "
                "Recognizes a workbook where each sheet repeats a two-recipe-per-row card layout "
                "with an Ingredient / Measure / RU / # of RU / RU Cost / Cost header. Ingredients "
                "are matched to existing inventory items where possible, linked to each other as "
                "sub-recipes when one batch recipe is used inside another, and otherwise queued up "
                "as new inventory items — review and edit everything below before anything is saved."
            )
            up_recipe_file = st.file_uploader("Batch recipe workbook (.xlsx)", type=["xlsx"], key="recipe_import_upload")

            if up_recipe_file is not None and st.button("Parse File", key="recipe_import_parse"):
                try:
                    cards = parse_batch_recipe_workbook(up_recipe_file.getvalue())
                    cards = suggest_sub_recipe_links(cards)
                    if not cards:
                        st.warning("No recipe cards were recognized in this file.")
                    else:
                        st.session_state["batch_import_cards"] = cards
                        st.success(f"Parsed {len(cards)} recipe card(s) — review below before importing.")
                except RecipeImportParseError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Couldn't read this file: {e}")

            cards = st.session_state.get("batch_import_cards")
            if cards:
                existing_recipe_names = {r.name for r in db.query(Recipe).all()}

                st.markdown("**1. Choose which recipes to import**")
                sel_rows = []
                for c in cards:
                    already = c["recipe_name"] in existing_recipe_names
                    sel_rows.append({
                        "Import": not already,
                        "Recipe": c["recipe_name"],
                        "Sheet": c["sheet"],
                        "Yield Qty": c["yield_qty"] if c["yield_qty"] is not None else 1.0,
                        "Yield Unit": c["yield_unit"] or "batch",
                        "# Ingredients": len(c["ingredients"]),
                        "Already exists": already,
                    })
                sel_df = pd.DataFrame(sel_rows)
                edited_sel_df = st.data_editor(
                    sel_df,
                    column_config={
                        "Import": st.column_config.CheckboxColumn(help="Uncheck to skip this recipe entirely"),
                    },
                    disabled=["Recipe", "Sheet", "# Ingredients", "Already exists"],
                    hide_index=True, use_container_width=True, height=350, key="batch_recipe_select_editor",
                )
                n_already = int(edited_sel_df["Already exists"].sum())
                if n_already:
                    st.caption(
                        f"{n_already} recipe(s) already exist by name and are pre-unchecked — "
                        "re-importing them would skip re-adding their ingredients to avoid duplicates."
                    )
                selected_names = set(edited_sel_df.loc[edited_sel_df["Import"] == True, "Recipe"])

                st.markdown("**2. Review ingredient links**")
                st.caption(
                    "Link Type: **Sub-Recipe** ties to another batch recipe in this import (or an "
                    "existing one by that exact name); **Existing Item** matches an inventory item "
                    "already in the system; **New Item** creates one; **Skip** drops the line. "
                    "Edit any cell, including Link Target, before importing."
                )
                ing_rows = []
                for c in cards:
                    if c["recipe_name"] not in selected_names:
                        continue
                    for ing in c["ingredients"]:
                        qty = ing["quantity"]
                        if ing.get("suggested_sub_recipe") and ing["suggested_sub_recipe"] in selected_names:
                            link_type, link_target = "Sub-Recipe", ing["suggested_sub_recipe"]
                        elif qty is None or qty <= 0:
                            link_type, link_target = "Skip", ing["name"]
                        else:
                            match = find_item_by_name_or_sku(db, ing["name"], None)
                            if match:
                                link_type, link_target = "Existing Item", match.name
                            else:
                                link_type, link_target = "New Item", ing["name"]
                        ing_rows.append({
                            "Recipe": c["recipe_name"],
                            "Ingredient": ing["name"],
                            "Quantity": qty,
                            "Unit": ing["unit"],
                            "Unit Cost": ing["unit_cost"],
                            "Link Type": link_type,
                            "Link Target": link_target,
                        })

                if not ing_rows:
                    st.info("No recipes selected above.")
                else:
                    ing_df = pd.DataFrame(ing_rows)
                    edited_ing_df = st.data_editor(
                        ing_df,
                        column_config={
                            "Link Type": st.column_config.SelectboxColumn(
                                options=["New Item", "Existing Item", "Sub-Recipe", "Skip"], required=True,
                            ),
                            "Quantity": st.column_config.NumberColumn(format="%.4f"),
                            "Unit Cost": st.column_config.NumberColumn(format="%.4f"),
                        },
                        disabled=["Recipe"],
                        hide_index=True, use_container_width=True, height=480, key="batch_ingredient_editor",
                    )
                    st.caption(
                        f"{len(edited_ing_df)} ingredient line(s) across {len(selected_names)} recipe(s). "
                        f"Changing recipe selection above regenerates this table from scratch, so finalize "
                        f"your recipe picks before fine-tuning ingredient links."
                    )

                    if st.button("Import These Recipes", type="primary", key="batch_recipe_import_commit"):
                        name_to_id = {r.name: r.id for r in db.query(Recipe).all()}
                        newly_created = set()
                        created_recipes = skipped_recipes = 0

                        for _, row in edited_sel_df[edited_sel_df["Import"] == True].iterrows():
                            rname = str(row["Recipe"]).strip()
                            if rname in name_to_id:
                                skipped_recipes += 1
                                continue
                            card = next((c for c in cards if c["recipe_name"] == rname), None)
                            yq = float(row["Yield Qty"]) if pd.notna(row["Yield Qty"]) and row["Yield Qty"] else 1.0
                            yu = str(row["Yield Unit"]).strip() if pd.notna(row["Yield Unit"]) and str(row["Yield Unit"]).strip() else "batch"
                            desc_bits = []
                            if card and card.get("yield_text"):
                                desc_bits.append(f"Yield: {card['yield_text']}")
                            if card and card.get("shelf_life"):
                                desc_bits.append(f"Shelf life: {card['shelf_life']}")
                            try:
                                new_r = create_recipe(
                                    db, rname, yield_qty=yq, yield_unit=yu,
                                    category=card["sheet"] if card else None,
                                    description="; ".join(desc_bits) or None,
                                )
                                db.commit()
                                name_to_id[rname] = new_r.id
                                newly_created.add(rname)
                                created_recipes += 1
                            except ValueError:
                                db.rollback()
                                skipped_recipes += 1

                        ing_created = items_created = sub_links = ing_skipped = ing_errors = 0
                        for _, row in edited_ing_df.iterrows():
                            rname = str(row["Recipe"]).strip()
                            if rname not in newly_created:
                                continue
                            link_type = row["Link Type"]
                            qty = float(row["Quantity"]) if pd.notna(row["Quantity"]) else 0.0
                            unit = str(row["Unit"]).strip() if pd.notna(row["Unit"]) and str(row["Unit"]).strip() else "each"
                            target = str(row["Link Target"]).strip() if pd.notna(row["Link Target"]) else ""
                            ucost = float(row["Unit Cost"]) if pd.notna(row["Unit Cost"]) else 0.0

                            if link_type == "Skip" or qty <= 0 or not target:
                                ing_skipped += 1
                                continue

                            item_id = sub_id = None
                            try:
                                if link_type == "Sub-Recipe":
                                    sub_id = name_to_id.get(target)
                                    if not sub_id:
                                        ing_errors += 1
                                        continue
                                    sub_links += 1
                                else:
                                    existing = find_item_by_name_or_sku(db, target, None)
                                    if existing:
                                        item_id = existing.id
                                    else:
                                        new_item = create_item(db, target, unit, current_cost=ucost)
                                        item_id = new_item.id
                                        items_created += 1
                                add_ingredient(db, name_to_id[rname], qty, unit, item_id=item_id, sub_recipe_id=sub_id)
                                db.commit()
                                ing_created += 1
                            except ValueError:
                                db.rollback()
                                ing_errors += 1
                        db.commit()

                        st.success(
                            f"Recipes: {created_recipes} created, {skipped_recipes} skipped (already existed). "
                            f"Ingredients: {ing_created} linked ({sub_links} to sub-recipes, {items_created} new "
                            f"inventory items created), {ing_skipped} skipped (no quantity/target), {ing_errors} errors."
                        )
                        st.session_state.pop("batch_import_cards", None)
                        st.rerun()

                if st.button("Discard parsed data", key="recipe_import_discard"):
                    st.session_state.pop("batch_import_cards", None)
                    st.rerun()

    with tab_menu_bulk:
        if not require_perm(user, "recipes_edit"):
            st.warning("You do not have permission to add or edit recipes.")
        else:
            st.subheader("Bulk Import Menu Items")
            st.caption(
                "Upload a menu costing workbook — the final products customers order and are "
                "charged for. Recognizes a workbook with an 'Ingredient/Item | Amount | Cost' "
                "header, Total Cost / Menu Price / Percentage Food Cost beneath each card. "
                "Ingredients are matched against your existing recipes first (so batch/prep items "
                "you've already imported link in as sub-recipes), then existing inventory items, "
                "then queued as new items — review and edit everything before anything is saved."
            )
            up_menu_file = st.file_uploader("Menu costing workbook (.xlsx)", type=["xlsx"], key="menu_import_upload")

            if up_menu_file is not None and st.button("Parse File", key="menu_import_parse"):
                try:
                    mcards = parse_menu_costing_workbook(up_menu_file.getvalue())
                    if not mcards:
                        st.warning("No menu item cards were recognized in this file.")
                    else:
                        st.session_state["menu_import_cards"] = mcards
                        st.success(f"Parsed {len(mcards)} menu item(s) — review below before importing.")
                except RecipeImportParseError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Couldn't read this file: {e}")

            mcards = st.session_state.get("menu_import_cards")
            if mcards:
                existing_recipe_names = {r.name for r in db.query(Recipe).all()}

                def _find_recipe_by_exact_name(nm):
                    nm = (nm or "").strip()
                    if not nm:
                        return None
                    return db.query(Recipe).filter(Recipe.name.ilike(nm)).first()

                st.markdown("**1. Choose which menu items to import**")
                seen_menu_names = set()
                msel_rows = []
                for c in mcards:
                    dup = c["recipe_name"] in seen_menu_names
                    seen_menu_names.add(c["recipe_name"])
                    already = c["recipe_name"] in existing_recipe_names
                    msel_rows.append({
                        "Import": not already and not dup,
                        "Recipe": c["recipe_name"],
                        "Category": c["category"],
                        "Menu Price": c["menu_price"] if c["menu_price"] is not None else 0.0,
                        "# Ingredients": len(c["ingredients"]),
                        "Already exists": already,
                    })
                msel_df = pd.DataFrame(msel_rows)
                edited_msel_df = st.data_editor(
                    msel_df,
                    column_config={
                        "Import": st.column_config.CheckboxColumn(help="Uncheck to skip this menu item"),
                        "Menu Price": st.column_config.NumberColumn(format="$%.2f"),
                    },
                    disabled=["Recipe", "Category", "# Ingredients", "Already exists"],
                    hide_index=True, use_container_width=True, height=350, key="menu_select_editor",
                )
                selected_menu_names = set(edited_msel_df.loc[edited_msel_df["Import"] == True, "Recipe"])

                st.markdown("**2. Review ingredient links**")
                st.caption(
                    "Link Type: **Sub-Recipe** ties to an existing recipe by that exact name "
                    "(batch/prep items or other menu items already in the system); **Existing "
                    "Item** matches an inventory item already in the system; **New Item** creates "
                    "one; **Skip** drops the line."
                )
                ming_rows = []
                for c in mcards:
                    if c["recipe_name"] not in selected_menu_names:
                        continue
                    for ing in c["ingredients"]:
                        qty = ing["quantity"]
                        recipe_match = _find_recipe_by_exact_name(ing["name"])
                        if recipe_match:
                            link_type, link_target = "Sub-Recipe", recipe_match.name
                        else:
                            item_match = find_item_by_name_or_sku(db, ing["name"], None)
                            if item_match:
                                link_type, link_target = "Existing Item", item_match.name
                            else:
                                link_type, link_target = "New Item", ing["name"]
                        ming_rows.append({
                            "Recipe": c["recipe_name"],
                            "Ingredient": ing["name"],
                            "Quantity": qty,
                            "Unit": ing["unit"],
                            "Unit Cost": ing["unit_cost"],
                            "Link Type": link_type,
                            "Link Target": link_target,
                        })

                if not ming_rows:
                    st.info("No menu items selected above.")
                else:
                    ming_df = pd.DataFrame(ming_rows)
                    edited_ming_df = st.data_editor(
                        ming_df,
                        column_config={
                            "Link Type": st.column_config.SelectboxColumn(
                                options=["New Item", "Existing Item", "Sub-Recipe", "Skip"], required=True,
                            ),
                            "Quantity": st.column_config.NumberColumn(format="%.4f"),
                            "Unit Cost": st.column_config.NumberColumn(format="%.4f"),
                        },
                        disabled=["Recipe"],
                        hide_index=True, use_container_width=True, height=480, key="menu_ingredient_editor",
                    )
                    st.caption(
                        f"{len(edited_ming_df)} ingredient line(s) across {len(selected_menu_names)} menu item(s). "
                        f"Changing menu item selection above regenerates this table from scratch, so finalize "
                        f"your picks before fine-tuning ingredient links."
                    )

                    if st.button("Import These Menu Items", type="primary", key="menu_import_commit"):
                        name_to_id = {r.name: r.id for r in db.query(Recipe).all()}
                        newly_created = set()
                        created_recipes = skipped_recipes = 0

                        for _, row in edited_msel_df[edited_msel_df["Import"] == True].iterrows():
                            rname = str(row["Recipe"]).strip()
                            if rname in name_to_id:
                                skipped_recipes += 1
                                continue
                            card = next((c for c in mcards if c["recipe_name"] == rname), None)
                            mp = float(row["Menu Price"]) if pd.notna(row["Menu Price"]) else None
                            try:
                                new_r = create_recipe(
                                    db, rname, yield_qty=1.0, yield_unit="serving",
                                    menu_price=mp or None,
                                    category=card["category"] if card else None,
                                )
                                db.commit()
                                name_to_id[rname] = new_r.id
                                newly_created.add(rname)
                                created_recipes += 1
                            except ValueError:
                                db.rollback()
                                skipped_recipes += 1

                        ing_created = items_created = sub_links = ing_skipped = ing_errors = 0
                        for _, row in edited_ming_df.iterrows():
                            rname = str(row["Recipe"]).strip()
                            if rname not in newly_created:
                                continue
                            link_type = row["Link Type"]
                            qty = float(row["Quantity"]) if pd.notna(row["Quantity"]) else 0.0
                            unit = str(row["Unit"]).strip() if pd.notna(row["Unit"]) and str(row["Unit"]).strip() else "each"
                            target = str(row["Link Target"]).strip() if pd.notna(row["Link Target"]) else ""
                            ucost = float(row["Unit Cost"]) if pd.notna(row["Unit Cost"]) else 0.0

                            if link_type == "Skip" or qty <= 0 or not target:
                                ing_skipped += 1
                                continue

                            item_id = sub_id = None
                            try:
                                if link_type == "Sub-Recipe":
                                    sub_id = name_to_id.get(target)
                                    if not sub_id:
                                        match = _find_recipe_by_exact_name(target)
                                        sub_id = match.id if match else None
                                    if not sub_id:
                                        ing_errors += 1
                                        continue
                                    sub_links += 1
                                else:
                                    existing = find_item_by_name_or_sku(db, target, None)
                                    if existing:
                                        item_id = existing.id
                                    else:
                                        new_item = create_item(db, target, unit, current_cost=ucost)
                                        item_id = new_item.id
                                        items_created += 1
                                add_ingredient(db, name_to_id[rname], qty, unit, item_id=item_id, sub_recipe_id=sub_id)
                                db.commit()
                                ing_created += 1
                            except ValueError:
                                db.rollback()
                                ing_errors += 1
                        db.commit()

                        st.success(
                            f"Menu items: {created_recipes} created, {skipped_recipes} skipped (already existed). "
                            f"Ingredients: {ing_created} linked ({sub_links} to sub-recipes, {items_created} new "
                            f"inventory items created), {ing_skipped} skipped, {ing_errors} errors."
                        )
                        st.session_state.pop("menu_import_cards", None)
                        st.rerun()

                if st.button("Discard parsed data", key="menu_import_discard"):
                    st.session_state.pop("menu_import_cards", None)
                    st.rerun()

# ---------- SIMULATE SALES ----------
elif page == "Simulate Sales (POS)":
    st.title("Simulate POS Sales")
    recipes = {r.name: r.id for r in db.query(Recipe).filter(Recipe.is_active == True).all()}
    with st.form("sale_form"):
        selected = st.multiselect("Items sold", list(recipes.keys()))
        quantities = {name: st.number_input(f"Qty – {name}", min_value=1, value=1, key=f"q_{name}") for name in selected}
        if st.form_submit_button("Ring Sale & Deplete", type="primary") and selected:
            lines = [{"recipe_id": recipes[name], "pos_item_name": name, "quantity": quantities[name], "unit_price": db.get(Recipe, recipes[name]).menu_price or 0} for name in selected]
            sale = record_pos_sale(db, location.id, lines)
            db.commit()
            st.success(f"Sale recorded. Inventory depleted.")
            st.rerun()

# ---------- TOAST POS IMPORT ----------
elif page == "Toast POS Import":
    st.title("Toast POS Sales Import")
    st.caption("Port Toast sales into theoretical inventory. Map menu items → recipes, then import.")

    from app.services.toast_import import ToastSalesImporter
    import json

    importer = ToastSalesImporter(db, location.id)
    recipes = {r.name: r.id for r in db.query(Recipe).filter(Recipe.is_active == True).all()}

    tab1, tab2, tab3 = st.tabs(["Import Sample / JSON", "Item Mappings", "How to Connect Live Toast"])

    with tab1:
        st.subheader("Import sales data")
        mode = st.radio("Input format", ["Demo sample (uses your recipes)", "Paste Toast-style JSON orders", "Paste flat item rows (CSV-like JSON)"])

        if mode == "Demo sample (uses your recipes)":
            st.info("Creates a few sample Toast-style sales for your existing recipes and depletes inventory.")
            if st.button("Run demo Toast import", type="primary"):
                sample_rows = []
                for name, rid in list(recipes.items())[:3]:
                    sample_rows.append({
                        "order_id": "DEMO-TOAST-001",
                        "item_name": name,
                        "quantity": 2,
                        "unit_price": 14.0,
                        "sold_at": datetime.now().isoformat(),
                    })
                if not sample_rows:
                    st.warning("No recipes found. Seed data first.")
                else:
                    result = importer.import_item_rows(sample_rows, external_id_prefix="TOAST-DEMO")
                    st.success(
                        f"Sales created: {result.sales_created} · Lines mapped: {result.lines_mapped} · "
                        f"Unmapped: {result.lines_unmapped}"
                    )
                    if result.unmapped_items:
                        st.warning("Unmapped items: " + ", ".join(result.unmapped_items))
                    if result.errors:
                        st.error("\n".join(result.errors))
                    st.rerun()

        elif mode == "Paste Toast-style JSON orders":
            st.markdown("Paste an array of Toast Order objects (from `/ordersBulk` or webhooks).")
            raw = st.text_area("JSON", height=200, placeholder='[{"guid": "...", "checks": [{"selections": [{"displayName": "Old Fashioned", "quantity": 1, "price": 14}]}]}]')
            if st.button("Import orders JSON", type="primary") and raw.strip():
                try:
                    orders = json.loads(raw)
                    if isinstance(orders, dict):
                        orders = [orders]
                    result = importer.import_orders_json(orders)
                    st.success(
                        f"Sales created: {result.sales_created} · Mapped lines: {result.lines_mapped} · "
                        f"Unmapped: {result.lines_unmapped}"
                    )
                    if result.unmapped_items:
                        st.warning("Unmapped: " + ", ".join(result.unmapped_items))
                    if result.errors:
                        st.error("\n".join(result.errors))
                    st.rerun()
                except Exception as e:
                    st.error(f"Parse/import error: {e}")

        else:
            st.markdown("Paste a JSON array of flat rows (`item_name`, `quantity`, optional `order_id`, `unit_price`).")
            raw = st.text_area("JSON rows", height=180, placeholder='[{"order_id": "123", "item_name": "Old Fashioned", "quantity": 2, "unit_price": 14}]')
            if st.button("Import flat rows", type="primary") and raw.strip():
                try:
                    rows = json.loads(raw)
                    if isinstance(rows, dict):
                        rows = [rows]
                    result = importer.import_item_rows(rows)
                    st.success(
                        f"Sales created: {result.sales_created} · Mapped: {result.lines_mapped} · Unmapped: {result.lines_unmapped}"
                    )
                    if result.unmapped_items:
                        st.warning("Unmapped: " + ", ".join(result.unmapped_items))
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    with tab2:
        st.subheader("Toast item → Recipe mappings")
        st.caption("By default, recipe names are auto-mapped (case-insensitive). Add overrides below.")
        mappings = importer.list_current_mappings()
        if mappings:
            st.dataframe(
                pd.DataFrame([
                    {"Toast / key": m.toast_item_name, "Recipe": m.recipe_name or "—", "Ignored": m.ignore}
                    for m in mappings
                ]),
                use_container_width=True,
            )

        with st.form("add_mapping"):
            toast_name = st.text_input("Toast menu item name")
            recipe_choice = st.selectbox("Map to recipe", ["(ignore – no depletion)"] + list(recipes.keys()))
            if st.form_submit_button("Save mapping"):
                if toast_name.strip():
                    if recipe_choice.startswith("(ignore"):
                        importer.set_mapping(toast_name, ignore=True)
                    else:
                        importer.set_mapping(toast_name, recipe_id=recipes[recipe_choice])
                    st.success(f"Mapped “{toast_name}”")
                    st.rerun()

    with tab3:
        st.markdown("""
### Connecting live Toast data

**Option A – Orders API (partner / custom integration)**  
1. Register in Toast Developer Portal and get OAuth client credentials.  
2. Use `GET /orders/v2/ordersBulk?startDate=...&endDate=...` (or the orders-updated webhook).  
3. Pass the returned `Order` array into `ToastSalesImporter.import_orders_json()`.  
4. Header required: `Toast-Restaurant-External-ID`.

**Option B – Nightly CSV / data export**  
Toast can export Order Details / Item Selection Details.  
Convert rows to the flat format (`item_name`, `quantity`, `order_id`, …) and call `import_item_rows()`.

**Option C – Webhook**  
Subscribe to order updates; on each closed/paid order, POST the payload to a small endpoint that calls this importer.

### Mapping tip
Keep Toast display names aligned with Craftable recipe names, or maintain explicit mappings in the **Item Mappings** tab (persist them in a DB table for production).

### What happens on import
- Matched lines → `POSSale` + `POSSaleLine`  
- Recipes deplete theoretical inventory automatically  
- Unmapped items are reported so you can map or ignore them  
- Voided / deleted orders are skipped
        """)

# ---------- WASTE ----------
elif page == "Waste & Adjustments":
    st.title("Waste Logging")
    with st.form("waste_form"):
        items = {i.name: i.id for i in db.query(InventoryItem).filter(InventoryItem.is_active == True).all()}
        item_name = st.selectbox("Item", list(items.keys()))
        qty = st.number_input("Quantity", min_value=0.1, step=0.1)
        reason = st.selectbox("Reason", ["spoilage", "overproduction", "trim loss", "error", "other"])
        if st.form_submit_button("Log Waste"):
            log_waste(db, items[item_name], location.id, qty, "each", reason=reason)
            db.commit()
            st.success("Waste logged")
            st.rerun()

# ---------- ORDER SUGGESTIONS ----------
elif page == "Order Suggestions (Par)":
    st.title("Par-Level Order Suggestions")
    suggestions = suggest_order_from_par(db, location.id)
    if not suggestions:
        st.success("All items at or above par.")
    else:
        cols = ["name", "current_theoretical", "par_level", "suggested_qty", "unit"]
        if require_perm(user, "view_costs"):
            cols.append("est_cost")
        st.dataframe(pd.DataFrame(suggestions)[cols], use_container_width=True)
        if require_perm(user, "view_costs"):
            st.metric("Estimated Order Value", f"${sum(s['est_cost'] for s in suggestions):,.2f}")
        st.info("Go to **Purchasing → Create Purchase Order** to turn these into a real PO.")

# ---------- USERS & ROLES ----------
elif page == "Users & Roles":
    st.title("Users & Roles")
    st.caption("Create employees and assign roles. Only Owners can manage users.")

    if not require_perm(user, "users_admin"):
        st.error("You do not have permission to manage users.")
        st.stop()

    tab1, tab2 = st.tabs(["Team", "Add employee"])

    with tab1:
        users = list_users(db, active_only=False)
        if not users:
            st.info("No users yet.")
        else:
            rows = []
            for u in users:
                rows.append({
                    "ID": u.id,
                    "Name": u.name,
                    "Role": u.role.name if u.role else "—",
                    "PIN": u.pin or "—",
                    "Active": u.is_active,
                    "Email": u.email or "",
                    "Pay rate": f"${u.hourly_rate:.2f}/hr" if u.hourly_rate else "—",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True)

            st.subheader("Deactivate user")
            active = [u for u in users if u.is_active and u.id != user.id]
            if active:
                choice = st.selectbox("User", {f"{u.name} ({u.role.name})": u.id for u in active})
                if st.button("Deactivate", type="secondary"):
                    deactivate_user(db, choice)
                    st.success("User deactivated")
                    st.rerun()

            if require_perm(user, "scheduling_edit"):
                st.subheader("Set pay rate")
                st.caption("Used to estimate scheduled labor cost / labor % on the Scheduling page.")
                if active:
                    rate_choice = st.selectbox(
                        "Employee", {f"{u.name} ({u.role.name})": u.id for u in active}, key="rate_user_pick"
                    )
                    new_rate = st.number_input("Hourly rate ($)", min_value=0.0, step=0.25, value=0.0)
                    if st.button("Update rate"):
                        set_hourly_rate(db, rate_choice, new_rate)
                        st.success("Pay rate updated")
                        st.rerun()

    with tab2:
        with st.form("new_user"):
            name = st.text_input("Full name")
            pin = st.text_input("PIN (4–6 digits recommended)", max_chars=8)
            email = st.text_input("Email (optional)")
            role_code = st.selectbox("Role", ["owner", "manager", "kitchen", "server"])
            hourly_rate = st.number_input("Hourly rate ($, optional)", min_value=0.0, step=0.25, value=0.0)
            if st.form_submit_button("Create user", type="primary"):
                if not name.strip() or not pin.strip():
                    st.error("Name and PIN are required")
                else:
                    try:
                        create_user(db, name, role_code, pin=pin, email=email or None, hourly_rate=hourly_rate)
                        st.success(f"Created {name} as {role_code}")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

    st.divider()
    st.subheader("Permission matrix")
    st.caption("Toggle cells to grant or revoke access. Changes save immediately. Owner must keep **users_admin**.")

    from app.models import PERMISSIONS

    roles_order = ["owner", "manager", "kitchen", "server"]
    role_labels = {"owner": "Owner", "manager": "Manager", "kitchen": "Kitchen", "server": "Server"}
    perm_map = get_role_permissions_map(db)

    groups = {
        "Pages & areas": [
            "dashboard", "inventory_view", "inventory_edit", "recipes_view", "recipes_edit",
            "purchasing", "counts", "variance", "waste", "pos_import", "invoices", "users_admin",
        ],
        "Sensitive data": ["view_costs"],
        "Count actions": ["counts_enter", "counts_close", "counts_align"],
        "Purchasing actions": [
            "purchasing_create", "purchasing_submit", "purchasing_receive", "purchasing_cancel",
        ],
        "Scheduling": [
            "scheduling_view", "scheduling_edit",
        ],
        "Training & quizzes": [
            "training_view", "training_admin",
        ],
    }

    for group_name, keys in groups.items():
        st.markdown(f"**{group_name}**")
        header = st.columns([3] + [1] * len(roles_order))
        header[0].markdown("Permission")
        for i, rc in enumerate(roles_order):
            header[i + 1].markdown(f"**{role_labels[rc]}**")

        for key in keys:
            if key not in PERMISSIONS:
                continue
            cols = st.columns([3] + [1] * len(roles_order))
            cols[0].write(PERMISSIONS[key])
            for i, rc in enumerate(roles_order):
                current = key in perm_map.get(rc, set())
                # Disable removing owner users_admin
                disabled = (rc == "owner" and key == "users_admin" and current)
                new_val = cols[i + 1].checkbox(
                    label=key,
                    value=current,
                    key=f"perm_{rc}_{key}",
                    label_visibility="collapsed",
                    disabled=disabled,
                )
                if new_val != current and not disabled:
                    try:
                        set_role_permission(db, rc, key, new_val)
                        st.toast(f"{'Granted' if new_val else 'Revoked'} {key} for {role_labels[rc]}")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

    st.divider()
    r1, r2 = st.columns(2)
    with r1:
        reset_role = st.selectbox("Reset role to defaults", roles_order, format_func=lambda x: role_labels[x])
    with r2:
        st.write("")
        st.write("")
        if st.button("Reset selected role"):
            reset_role_permissions(db, reset_role)
            st.success(f"Reset {role_labels[reset_role]} to defaults")
            st.rerun()

    with st.expander("Current effective permissions (JSON)"):
        st.json({k: sorted(v) for k, v in perm_map.items()})






# ---------- CHECKLISTS & SOPs (Jolt-style) ----------
elif page == "Checklists & SOPs":
    st.title("Checklists & SOPs")
    st.caption("Jolt-style lists: accountability, photo proof, temperature ranges, corrective actions, and completion reporting.")

    if not require_perm(user, "checklists"):
        st.error("You do not have access to checklists.")
        st.stop()

    tab_dash, tab_run, tab_open, tab_sops, tab_admin = st.tabs([
        "Manager view", "Run checklist", "Open runs", "Info library (SOPs)", "Templates"
    ])

    templates = list_templates(db, location_id=location.id)

    with tab_dash:
        report = location_checklist_report(db, location.id)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Open", report["open_count"])
        c2.metric("Overdue", report["overdue_count"])
        c3.metric("Completed (recent)", report["completed_recent"])
        c4.metric("Avg completion %", f"{report['avg_completion_pct']}%")

        if report["overdue_count"]:
            st.warning(f"{report['overdue_count']} overdue list(s) — follow up with the shift lead.")
            for r in report["overdue_runs"]:
                st.write(f"• **#{r.id}** {r.template.name if r.template else '—'} · due {r.due_at}")

        st.subheader("Temperature / range exceptions")
        if not report["temp_exceptions"]:
            st.success("No out-of-range readings in recent completions.")
        else:
            rows = []
            for ex in report["temp_exceptions"]:
                rows.append({
                    "When": ex.completed_at,
                    "Value": ex.number_value,
                    "Corrective action": ex.corrective_action_taken or "—",
                    "Employee #": ex.employee_user_id,
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.subheader("Open lists")
        if not report["open_runs"]:
            st.info("No open checklists.")
        else:
            for r in report["open_runs"]:
                pct = 0
                if r.template and r.template.tasks:
                    pct = len(r.completions) / max(len(r.template.tasks), 1) * 100
                label = f"#{r.id} {r.template.name if r.template else '—'} · {r.status} · {pct:.0f}%"
                if st.button(label, key=f"dash_open_{r.id}"):
                    st.session_state["active_run_id"] = r.id
                    st.rerun()

    with tab_run:
        if not templates:
            st.info("No checklist templates. Seed data or create under Templates.")
        else:
            tmap = {f"{t.name} ({t.list_type or 'ops'})": t for t in templates}
            pick = st.selectbox("Checklist", list(tmap.keys()))
            tmpl = tmap[pick]
            st.write(tmpl.description or "")
            if tmpl.schedule_hint:
                st.caption(f"Schedule hint: {tmpl.schedule_hint}")
            due_mins = st.number_input("Due in (minutes)", min_value=15, max_value=480, value=120)
            if st.button("Start checklist", type="primary"):
                run = start_checklist_run(
                    db, tmpl.id, location.id, user_id=user.id, due_minutes=int(due_mins)
                )
                st.session_state["active_run_id"] = run.id
                st.success(f"Started run #{run.id}")
                st.rerun()

            run_id = st.session_state.get("active_run_id")
            if run_id:
                progress = get_run_progress(db, run_id)
                if progress:
                    st.subheader(f"{progress['name']} — {progress['completed']}/{progress['total']} ({progress['pct']}%)")
                    st.progress(progress["pct"] / 100)
                    if progress.get("due_at"):
                        st.caption(f"Due {progress['due_at']} · status **{progress['status']}**")
                    for task in progress["tasks"]:
                        badge = "✅" if task["done"] else "⬜"
                        range_txt = ""
                        if task.get("min_value") is not None or task.get("max_value") is not None:
                            unit = task.get("unit_label") or ""
                            range_txt = f" · range {task.get('min_value')}–{task.get('max_value')}{unit}"
                        with st.expander(f"{badge} {task['title']} ({task['type']}{range_txt})", expanded=not task["done"]):
                            if task.get("instructions"):
                                st.caption(task["instructions"])
                            if task.get("training_note"):
                                st.info(f"Training: {task['training_note']}")
                            if task["done"]:
                                c = task["completion"]
                                who = task.get("employee_name") or f"user #{c.employee_user_id}"
                                st.success(f"Completed by **{who}** · {c.completed_at}")
                                if c.number_value is not None:
                                    flag = " ⚠️ OUT OF RANGE" if c.out_of_range else ""
                                    st.write(f"Value: **{c.number_value}**{flag}")
                                if c.out_of_range and c.corrective_action_taken:
                                    st.warning(f"Corrective action: {c.corrective_action_taken}")
                                if c.photo_url:
                                    try:
                                        st.image(c.photo_url if c.photo_url.startswith("/") is False else c.photo_url, width=280)
                                    except Exception:
                                        st.write(c.photo_path or c.photo_url)
                                if c.text_value:
                                    st.write(c.text_value)
                                if c.score_value is not None:
                                    st.write(f"Score: {c.score_value}/5")
                            else:
                                ttype = task["type"]
                                if ttype == "photo" or task["requires_photo"]:
                                    st.info("Photo proof — upload a live capture (camera preferred).")
                                    up = st.file_uploader(
                                        "Photo evidence",
                                        type=["jpg", "jpeg", "png", "webp"],
                                        key=f"photo_{task['task_id']}",
                                    )
                                    if up and st.button("Submit photo", key=f"sub_photo_{task['task_id']}"):
                                        import tempfile
                                        suffix = Path(up.name).suffix or ".jpg"
                                        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                                            tmp.write(up.getbuffer())
                                            tmp_path = tmp.name
                                        try:
                                            complete_task(
                                                db, run_id, task["task_id"], user.id,
                                                completion_type="photo",
                                                photo_source_path=tmp_path,
                                            )
                                            st.success("Photo saved")
                                            st.rerun()
                                        except Exception as e:
                                            st.error(str(e))
                                elif ttype == "number":
                                    unit = task.get("unit_label") or ""
                                    val = st.number_input(
                                        f"Value ({unit})",
                                        key=f"num_{task['task_id']}",
                                        value=float(task.get("min_value") or 0),
                                    )
                                    corr = ""
                                    # preview out of range
                                    oormin = task.get("min_value")
                                    oormax = task.get("max_value")
                                    is_oor = (oormin is not None and val < oormin) or (oormax is not None and val > oormax)
                                    if is_oor:
                                        st.error("Out of range")
                                        if task.get("corrective_action"):
                                            st.warning(task["corrective_action"])
                                        corr = st.text_input(
                                            "Corrective action taken *",
                                            key=f"corr_{task['task_id']}",
                                            placeholder="What did you do?",
                                        )
                                    if st.button("Save reading", key=f"sub_num_{task['task_id']}"):
                                        if is_oor and not corr.strip():
                                            st.error("Record the corrective action before saving.")
                                        else:
                                            complete_task(
                                                db, run_id, task["task_id"], user.id,
                                                completion_type="number",
                                                number_value=float(val),
                                                corrective_action_taken=corr or None,
                                            )
                                            st.rerun()
                                elif ttype == "score":
                                    sc = st.slider("Score", 1, 5, 3, key=f"sc_{task['task_id']}")
                                    if st.button("Save score", key=f"sub_sc_{task['task_id']}"):
                                        complete_task(
                                            db, run_id, task["task_id"], user.id,
                                            completion_type="score", score_value=int(sc),
                                        )
                                        st.rerun()
                                elif ttype == "text":
                                    txt = st.text_input("Note", key=f"txt_{task['task_id']}")
                                    if st.button("Save", key=f"sub_txt_{task['task_id']}"):
                                        complete_task(
                                            db, run_id, task["task_id"], user.id,
                                            completion_type="text", text_value=txt,
                                        )
                                        st.rerun()
                                else:
                                    if st.button("Mark complete", key=f"sub_cb_{task['task_id']}"):
                                        complete_task(
                                            db, run_id, task["task_id"], user.id,
                                            completion_type="checkbox",
                                        )
                                        st.rerun()
                    if st.button("Finish checklist", type="primary"):
                        try:
                            finish_run(db, run_id)
                            st.session_state.pop("active_run_id", None)
                            st.success("Checklist completed — logged with full accountability trail.")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

    with tab_open:
        runs = list_open_runs(db, location.id)
        if not runs:
            st.info("No open checklist runs.")
        else:
            for r in runs:
                status_icon = "🔴" if r.status == "overdue" else "🟢"
                c1, c2 = st.columns([3, 1])
                c1.write(f"{status_icon} **#{r.id}** {r.template.name if r.template else '—'} · {r.status} · started {r.started_at}")
                if c2.button("Open", key=f"open_run_{r.id}"):
                    st.session_state["active_run_id"] = r.id
                    st.rerun()

    with tab_sops:
        sops = list_sops(db, location_id=location.id)
        if not sops:
            st.info("No SOPs yet — add under Templates admin.")
        for sop in sops:
            with st.expander(f"{sop.title} · {sop.category or 'general'}"):
                st.caption(f"Roles: {sop.role_codes or 'all'} · v{sop.version or '1.0'}")
                st.markdown(sop.body or "_No content_")

    with tab_admin:
        if not require_perm(user, "checklists_admin"):
            st.warning("Template admin requires checklists_admin permission.")
        else:
            st.subheader("Create SOP (Info Library)")
            with st.form("new_sop"):
                title = st.text_input("Title")
                category = st.selectbox("Category", ["opening", "closing", "safety", "prep", "FOH", "other"])
                body = st.text_area("Body (steps / markdown)")
                roles = st.text_input("Role codes (comma-separated)", value="kitchen,manager")
                if st.form_submit_button("Save SOP"):
                    create_sop(db, title, body=body, category=category, role_codes=roles, location_id=location.id)
                    st.success("SOP created")
                    st.rerun()
            st.subheader("Create checklist template")
            with st.form("new_tmpl"):
                name = st.text_input("Template name")
                desc = st.text_input("Description")
                ltype = st.selectbox("List type", ["ops", "opening", "closing", "food_safety", "walkthrough", "cleaning"])
                if st.form_submit_button("Create template"):
                    create_template(db, name, description=desc, location_id=location.id, list_type=ltype)
                    st.success("Template created")
                    st.rerun()
            if templates:
                st.subheader("Add task to template")
                tmap = {t.name: t.id for t in templates}
                with st.form("add_task"):
                    tid = tmap[st.selectbox("Template", list(tmap.keys()))]
                    title = st.text_input("Task title")
                    ttype = st.selectbox("Type", ["checkbox", "photo", "text", "number", "score"])
                    instr = st.text_input("Instructions")
                    c1, c2, c3 = st.columns(3)
                    min_v = c1.number_input("Min (temps)", value=0.0)
                    max_v = c2.number_input("Max", value=0.0)
                    unit = c3.text_input("Unit", value="°F")
                    train = st.text_input("Just-in-time training note")
                    corr = st.text_input("Corrective action (if out of range)")
                    req_photo = st.checkbox("Requires photo", value=(ttype == "photo"))
                    if st.form_submit_button("Add task"):
                        add_task_to_template(
                            db, tid, title, task_type=ttype, instructions=instr or None,
                            requires_photo=req_photo,
                            min_value=min_v if ttype == "number" else None,
                            max_value=max_v if ttype == "number" else None,
                            unit_label=unit if ttype == "number" else None,
                            training_note=train or None,
                            corrective_action=corr or None,
                        )
                        st.success("Task added")
                        st.rerun()

# ---------- SCHEDULING ----------
elif page == "Scheduling":
    st.title("Scheduling")
    st.caption("Build the weekly schedule, publish it to staff, and compare planned vs. actual labor.")

    if not require_perm(user, "scheduling_view"):
        st.error("You do not have access to scheduling.")
        st.stop()

    can_edit = require_perm(user, "scheduling_edit")
    team = list_users(db, active_only=True)
    team_by_id = {u.id: u for u in team}

    tab_week, tab_build, tab_labor = st.tabs(["Weekly schedule", "Build shifts", "Labor cost"])

    week_anchor = st.session_state.get("sched_week_anchor", date.today())

    with tab_week:
        picked = st.date_input("Week containing", value=week_anchor, key="sched_week_pick")
        st.session_state["sched_week_anchor"] = picked
        wk_start, wk_end = week_bounds(picked)
        shifts = list_shifts(db, location.id, wk_start, wk_end)

        if not shifts:
            st.info("No shifts scheduled for this week yet — add some under Build shifts.")
        else:
            rows = []
            for s in shifts:
                rows.append({
                    "ID": s.id,
                    "Date": s.start_at.strftime("%a %b %d"),
                    "Start": s.start_at.strftime("%H:%M"),
                    "End": s.end_at.strftime("%H:%M"),
                    "Employee": team_by_id[s.user_id].name if s.user_id in team_by_id else "OPEN",
                    "Position": s.role_code or "—",
                    "Hours": round(s.scheduled_hours(), 2),
                    "Status": s.status,
                })
            df_sched = pd.DataFrame(rows)
            st.dataframe(df_sched, use_container_width=True, hide_index=True)
            st.caption(f"Total scheduled hours: {df_sched['Hours'].sum():.1f}")

            if can_edit:
                draft_ids = [s.id for s in shifts if s.status == "draft"]
                if draft_ids and st.button(f"Publish {len(draft_ids)} draft shift(s)", type="primary"):
                    publish_shifts(db, draft_ids)
                    st.success("Schedule published")
                    st.rerun()

    with tab_build:
        if not can_edit:
            st.warning("Building shifts requires scheduling_edit permission.")
        else:
            st.subheader("Add a shift")
            with st.form("new_shift"):
                assign_options = {"— Open shift —": None}
                assign_options.update({f"{u.name} ({u.role.code})": u.id for u in team})
                assignee_label = st.selectbox("Employee", list(assign_options.keys()))
                role_code = st.selectbox("Position", ["manager", "kitchen", "server", "bar", "host", "other"])
                shift_date = st.date_input("Date", value=date.today())
                c1, c2, c3 = st.columns(3)
                start_t = c1.time_input("Start", value=datetime.strptime("09:00", "%H:%M").time())
                end_t = c2.time_input("End", value=datetime.strptime("17:00", "%H:%M").time())
                break_min = c3.number_input("Break (min)", min_value=0, max_value=120, value=0, step=5)
                notes = st.text_input("Notes (optional)")
                publish_now = st.checkbox("Publish immediately", value=False)
                if st.form_submit_button("Add shift", type="primary"):
                    try:
                        start_dt = datetime.combine(shift_date, start_t, tzinfo=timezone.utc)
                        end_dt = datetime.combine(shift_date, end_t, tzinfo=timezone.utc)
                        create_shift(
                            db, location.id, start_dt, end_dt,
                            user_id=assign_options[assignee_label],
                            role_code=role_code, break_minutes=int(break_min),
                            notes=notes or None,
                            status="published" if publish_now else "draft",
                        )
                        st.success("Shift added")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

            st.subheader("This week's shifts")
            wk_start, wk_end = week_bounds(st.session_state.get("sched_week_anchor", date.today()))
            shifts = list_shifts(db, location.id, wk_start, wk_end)
            for s in shifts:
                who = team_by_id[s.user_id].name if s.user_id in team_by_id else "OPEN"
                c1, c2 = st.columns([4, 1])
                c1.write(
                    f"**{s.start_at.strftime('%a %b %d, %H:%M')}–{s.end_at.strftime('%H:%M')}** "
                    f"· {who} · {s.role_code or '—'} · {s.status}"
                )
                if c2.button("Delete", key=f"del_shift_{s.id}"):
                    delete_shift(db, s.id)
                    st.rerun()

    with tab_labor:
        wk_start, wk_end = week_bounds(st.session_state.get("sched_week_anchor", date.today()))
        labor = scheduled_labor_cost(db, location.id, wk_start, wk_end)
        c1, c2, c3 = st.columns(3)
        c1.metric("Scheduled hrs", f"{labor['total_hours']:.1f}")
        c2.metric("Scheduled cost", f"${labor['total_cost']:,.0f}")
        c3.metric("Sales", f"${labor['total_sales']:,.0f}")

        if labor["by_user"]:
            st.subheader("By employee")
            st.dataframe(
                pd.DataFrame([
                    {"Employee": r["name"], "Hours": round(r["hours"], 2), "Cost": round(r["cost"], 2)}
                    for r in labor["by_user"]
                ]),
                use_container_width=True, hide_index=True,
            )
        if labor["labor_cost_percent"] is not None:
            st.metric("Scheduled labor cost % of sales (this week)", f"{labor['labor_cost_percent']:.1f}%")
        else:
            st.caption("No POS sales recorded for this window yet — labor % needs a sales denominator.")

# ---------- TRAINING & QUIZZES ----------
elif page == "Training & Quizzes":
    st.title("Training & Quizzes")
    st.caption("Short courses with a graded quiz at the end — a pass/fail record for every employee, every course.")

    if not require_perm(user, "training_view"):
        st.error("You do not have access to training.")
        st.stop()

    can_admin = require_perm(user, "training_admin")

    tab_mine, tab_team, tab_manage = st.tabs(["My Training", "Team Progress", "Manage Courses"])

    with tab_mine:
        my_courses = visible_courses_for_user(db, user, location_id=location.id)
        if not my_courses:
            st.info("No training courses assigned to your role yet.")
        else:
            for c in my_courses:
                best = best_completion(db, c.id, user.id)
                if best and best.passed:
                    status = "✅ Passed"
                elif best:
                    status = "❌ Failed — retake available"
                else:
                    status = "⬜ Not started"
                score_txt = f" · best score {best.score_percent}%" if best else ""

                with st.expander(f"{status} · {c.title}{score_txt}"):
                    if c.description:
                        st.write(c.description)
                    st.caption(
                        f"{len(c.lessons)} lesson(s) · {len(c.questions)} quiz question(s) "
                        f"· passing score {c.passing_score}%"
                    )

                    btn_label = "Retake" if (best and not best.passed) else ("Review / retake" if best else "Start")
                    if st.button(btn_label, key=f"start_course_{c.id}"):
                        completion = start_course(db, c.id, user.id)
                        st.session_state["active_training_course_id"] = c.id
                        st.session_state["active_completion_id"] = completion.id
                        st.rerun()

                    if st.session_state.get("active_training_course_id") == c.id:
                        completion_id = st.session_state["active_completion_id"]
                        st.divider()
                        if c.lessons:
                            st.subheader("Lessons")
                            for lesson in c.lessons:
                                with st.expander(lesson.title, expanded=True):
                                    if lesson.content:
                                        st.markdown(lesson.content)
                                    if lesson.video_url:
                                        st.video(lesson.video_url)

                        if c.questions:
                            st.subheader("Quiz")
                            with st.form(f"quiz_{completion_id}"):
                                picks = {}
                                for q in c.questions:
                                    choice_list = q.choice_list()
                                    picked = st.radio(
                                        q.question, choice_list,
                                        key=f"q_{completion_id}_{q.id}", index=None,
                                    )
                                    picks[q.id] = choice_list.index(picked) if picked is not None else None
                                if st.form_submit_button("Submit quiz", type="primary"):
                                    try:
                                        result = submit_quiz(db, completion_id, picks)
                                        st.session_state.pop("active_training_course_id", None)
                                        st.session_state.pop("active_completion_id", None)
                                        if result.passed:
                                            st.success(f"Passed with {result.score_percent}%!")
                                        else:
                                            st.error(
                                                f"Scored {result.score_percent}% — needs "
                                                f"{c.passing_score}% to pass. You can retake it."
                                            )
                                        st.rerun()
                                    except Exception as e:
                                        st.error(str(e))
                        else:
                            st.info("No quiz on this course yet — reading the lessons is enough for now.")

    with tab_team:
        if not can_admin:
            st.warning("Team progress requires training_admin permission.")
        else:
            report = training_report(db, location_id=location.id)
            c1, c2, c3 = st.columns(3)
            c1.metric("Assignments", report["total_assignments"])
            c2.metric("Passed", report["passed"])
            c3.metric("Completion rate", f"{report['completion_rate']}%")

            if report["rows"]:
                st.dataframe(
                    pd.DataFrame([
                        {
                            "Employee": r["user_name"],
                            "Course": r["course_title"],
                            "Status": r["status"],
                            "Score": r["score_percent"],
                            "Completed": r["completed_at"],
                        }
                        for r in report["rows"]
                    ]),
                    use_container_width=True, hide_index=True,
                )
            else:
                st.info("No courses assigned to any active employee yet.")

    with tab_manage:
        if not can_admin:
            st.warning("Managing courses requires training_admin permission.")
        else:
            st.subheader("Create course")
            with st.form("new_course"):
                title = st.text_input("Title")
                desc = st.text_area("Description")
                category = st.selectbox("Category", ["onboarding", "food_safety", "service", "compliance", "other"])
                roles = st.text_input("Role codes (comma-separated; blank = all roles)", value="")
                passing = st.number_input("Passing score (%)", min_value=0, max_value=100, value=80, step=5)
                if st.form_submit_button("Create course", type="primary"):
                    if not title.strip():
                        st.error("Title is required")
                    else:
                        create_course(
                            db, title, description=desc or None, category=category,
                            role_codes=roles or None, location_id=location.id, passing_score=int(passing),
                        )
                        st.success("Course created")
                        st.rerun()

            admin_courses = list_courses(db, active_only=False, location_id=location.id)
            if admin_courses:
                cmap = {f"{c.title} ({'active' if c.is_active else 'inactive'})": c.id for c in admin_courses}

                st.subheader("Add lesson")
                with st.form("new_lesson"):
                    cid = cmap[st.selectbox("Course", list(cmap.keys()), key="lesson_course_pick")]
                    ltitle = st.text_input("Lesson title")
                    content = st.text_area("Content (markdown / plain text)")
                    video = st.text_input("Video URL (optional)")
                    if st.form_submit_button("Add lesson"):
                        try:
                            add_lesson(db, cid, ltitle, content=content or None, video_url=video or None)
                            st.success("Lesson added")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

                st.subheader("Add quiz question")
                with st.form("new_question"):
                    cid2 = cmap[st.selectbox("Course", list(cmap.keys()), key="quiz_course_pick")]
                    qtext = st.text_input("Question")
                    c1, c2 = st.columns(2)
                    opt_a = c1.text_input("Choice A")
                    opt_b = c2.text_input("Choice B")
                    opt_c = c1.text_input("Choice C (optional)")
                    opt_d = c2.text_input("Choice D (optional)")
                    correct = st.selectbox("Correct answer", ["A", "B", "C", "D"])
                    if st.form_submit_button("Add question"):
                        choice_map = {"A": opt_a, "B": opt_b, "C": opt_c, "D": opt_d}
                        try:
                            add_quiz_question(
                                db, cid2, qtext,
                                [opt_a, opt_b, opt_c, opt_d],
                                choice_map[correct],
                            )
                            st.success("Question added")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

                st.subheader("Courses")
                for c in admin_courses:
                    cc1, cc2 = st.columns([4, 1])
                    cc1.write(
                        f"**{c.title}** · {c.category or '—'} · {len(c.lessons)} lessons · "
                        f"{len(c.questions)} questions · {'active' if c.is_active else 'inactive'}"
                    )
                    if c.is_active:
                        if cc2.button("Deactivate", key=f"deact_course_{c.id}"):
                            set_course_active(db, c.id, False)
                            st.rerun()
                    else:
                        if cc2.button("Activate", key=f"act_course_{c.id}"):
                            set_course_active(db, c.id, True)
                            st.rerun()

# ---------- LOCATIONS SETUP ----------
elif page == "Locations Setup":
    st.title("Locations Setup")
    st.caption("Create and customize restaurant locations. Switch the working location from the sidebar.")

    if not require_perm(user, "users_admin"):
        st.error("Only Owners can manage locations.")
        st.stop()

    tab_list, tab_add, tab_edit = st.tabs(["All locations", "Add location", "Edit location"])

    with tab_list:
        locs = list_locations(db, active_only=False)
        if not locs:
            st.info("No locations yet.")
        else:
            rows = []
            for loc in locs:
                rows.append({
                    "ID": loc.id,
                    "Name": loc.name,
                    "Code": loc.code,
                    "City": loc.city or "",
                    "State": loc.state or "",
                    "Timezone": loc.timezone or "",
                    "Closeout hour": loc.closeout_hour if loc.closeout_hour is not None else "",
                    "Active": loc.is_active,
                    "Phone": loc.phone or "",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            st.subheader("Activate / deactivate")
            labels = {f"{l.name} ({l.code})": l.id for l in locs}
            pick = st.selectbox("Location", list(labels.keys()), key="toggle_loc")
            col_a, col_b = st.columns(2)
            if col_a.button("Activate"):
                set_location_active(db, labels[pick], True)
                st.success("Activated")
                st.rerun()
            if col_b.button("Deactivate"):
                set_location_active(db, labels[pick], False)
                st.success("Deactivated")
                st.rerun()

    with tab_add:
        with st.form("add_location"):
            c1, c2 = st.columns(2)
            name = c1.text_input("Name *", placeholder="Downtown Bar")
            code = c2.text_input("Code *", placeholder="DTN", max_chars=20)
            address = st.text_input("Street address")
            c3, c4, c5 = st.columns(3)
            city = c3.text_input("City")
            state = c4.text_input("State")
            postal = c5.text_input("Postal code")
            c6, c7 = st.columns(2)
            timezone = c6.text_input("Timezone", value="America/Chicago")
            closeout = c7.number_input("Closeout hour (0–23)", min_value=0, max_value=23, value=4)
            phone = st.text_input("Phone")
            notes = st.text_area("Notes")
            if st.form_submit_button("Create location", type="primary"):
                if not name.strip() or not code.strip():
                    st.error("Name and code are required.")
                else:
                    try:
                        loc = create_location(
                            db, name, code,
                            address=address or None,
                            city=city or None,
                            state=state or None,
                            postal_code=postal or None,
                            timezone=timezone or "America/Chicago",
                            closeout_hour=int(closeout),
                            phone=phone or None,
                            notes=notes or None,
                        )
                        st.success(f"Created {loc.name} ({loc.code})")
                        st.session_state.location_id = loc.id
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

    with tab_edit:
        locs = list_locations(db, active_only=False)
        if not locs:
            st.info("No locations to edit.")
        else:
            labels = {f"{l.name} ({l.code})": l.id for l in locs}
            pick = st.selectbox("Select location", list(labels.keys()), key="edit_loc")
            loc = get_location(db, labels[pick])
            with st.form("edit_location"):
                c1, c2 = st.columns(2)
                name = c1.text_input("Name *", value=loc.name)
                code = c2.text_input("Code *", value=loc.code, max_chars=20)
                address = st.text_input("Street address", value=loc.address or "")
                c3, c4, c5 = st.columns(3)
                city = c3.text_input("City", value=loc.city or "")
                state = c4.text_input("State", value=loc.state or "")
                postal = c5.text_input("Postal code", value=loc.postal_code or "")
                c6, c7 = st.columns(2)
                timezone = c6.text_input("Timezone", value=loc.timezone or "America/Chicago")
                closeout = c7.number_input(
                    "Closeout hour (0–23)",
                    min_value=0, max_value=23,
                    value=int(loc.closeout_hour if loc.closeout_hour is not None else 4),
                )
                phone = st.text_input("Phone", value=loc.phone or "")
                notes = st.text_area("Notes", value=loc.notes or "")
                is_active = st.checkbox("Active", value=loc.is_active)
                if st.form_submit_button("Save changes", type="primary"):
                    try:
                        update_location(
                            db, loc.id,
                            name=name,
                            code=code,
                            address=address or None,
                            city=city or None,
                            state=state or None,
                            postal_code=postal or None,
                            timezone=timezone or "America/Chicago",
                            closeout_hour=int(closeout),
                            phone=phone or None,
                            notes=notes or None,
                            is_active=is_active,
                        )
                        st.success("Location updated")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))


# ---------- ABOUT ----------
elif page == "About the Model":
    st.title("About This Craftable Replica")
    st.markdown("""
### Implemented Workflows
1. **Live Recipe Costing** (with sub-recipes)
2. **Theoretical Inventory** driven by purchases, sales, waste
3. **Purchasing Workflow**
   - Create PO (manual or from par suggestions)
   - Add lines, submit, cancel
   - Receive against PO (partial or full) → updates theoretical stock + item cost
4. **Physical Counts** → open / enter / close
5. **Full Variance Report** between two counts
6. **Par-driven ordering suggestions**
7. **Toast POS Import** → map menu items to recipes, import orders/rows, auto-deplete inventory

### Purchasing Flow
```
Par suggestions / Manual entry
        ↓
   Create Draft PO
        ↓
   Add / edit lines
        ↓
   Submit PO
        ↓
   Receive goods (partial OK)
        ↓
   Theoretical inventory ↑
   Item current_cost updated
   PO status → partially_received / received
```
    """)

db.close()
