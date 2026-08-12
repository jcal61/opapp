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
    DEPARTMENTS, RECIPE_TYPES,
)
from app.services.costing import calculate_recipe_cost
from app.services.recipes import (
    create_recipe, update_recipe, set_recipe_active, list_recipes,
    add_ingredient, remove_ingredient,
    suggest_recipe_type, auto_assign_recipe_types,
)
from app.services.inventory import (
    get_or_create_stock, record_pos_sale, log_waste,
    create_item, update_item, set_item_active, list_items,
    get_item_by_name, set_opening_stock,
    suggest_department, auto_assign_departments,
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
    update_sop, set_sop_active, delete_sop,
    update_template, set_template_active, delete_template,
    update_task, set_task_active, delete_task, move_task,
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
from app.services.financials import (
    create_expense, delete_expense, list_expenses,
    upsert_budget, get_budget, list_budgets,
    record_cash_count, list_cash_counts, cash_variance_summary,
    profit_and_loss, budget_vs_actual, consolidated_pl, location_family_ids,
)
from app.services.logbook import (
    create_entry as create_log_entry, list_entries as list_log_entries,
    toggle_pin as toggle_log_pin, delete_entry as delete_log_entry,
)
from app.models import EXPENSE_CATEGORIES, LOG_CATEGORIES
from app.services.sales_report import (
    parse_toast_sales_csv, SalesImportParseError,
    create_sales_report_import, list_sales_report_imports,
    get_sales_report_import, delete_sales_report_import,
    set_line_recipe, set_lines_recipe_by_item_name,
)
from app.services.menu_analysis import get_menu_analysis, purchases_comparison

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
    "Financials": "financials_view",
    "Users & Roles": "users_admin",
    "Locations Setup": "users_admin",
    "Checklists & SOPs": "checklists",
    "Manager Logbook": "logbook",
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

@st.dialog("Confirm")
def confirm_action(title: str, message: str, action_fn, action_args: tuple, confirm_label: str, dialog_key: str):
    """A reusable 'are you sure?' modal for anything that can't be undone —
    used consistently everywhere the app deletes or permanently changes a
    record, instead of every page inventing its own version (or none at all).
    Opens its own DB session so it works no matter how stale the caller's
    session is by the time the confirm button is actually clicked."""
    st.markdown(f"**{title}**")
    st.warning(message)
    c1, c2 = st.columns(2)
    if c1.button(confirm_label, type="primary", key=f"confirm_yes_{dialog_key}"):
        d = get_db()
        try:
            action_fn(d, *action_args)
        except Exception as e:
            d.close()
            st.error(str(e))
            st.stop()
        d.close()
        st.toast(f"{confirm_label} — done.", icon="✅")
        st.rerun()
    if c2.button("Cancel", key=f"confirm_no_{dialog_key}"):
        st.rerun()

# A few service calls only flush (not commit) since they're meant to be
# batched by their normal caller — confirm_action always opens a lone,
# single-purpose session, so these thin wrappers commit right after.
def _cancel_po_confirmed(db, po_id):
    cancel_po(db, po_id)
    db.commit()

def _reject_invoice_confirmed(db, invoice_id):
    reject_invoice(db, invoice_id)
    db.commit()

def _remove_ingredient_confirmed(db, ingredient_id):
    remove_ingredient(db, ingredient_id)
    db.commit()

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
st.sidebar.divider()

# Pages are grouped into a handful of areas so the sidebar reads as a menu,
# not a 19-item wall of links. A page can only appear in one group.
NAV_GROUPS = [
    ("🏠 Dashboard", ["Dashboard"]),
    ("📦 Inventory & Purchasing", [
        "Inventory & Stock", "Purchasing", "Invoices (AP)", "Physical Counts",
        "Variance Report", "Waste & Adjustments", "Order Suggestions (Par)",
    ]),
    ("🍳 Recipes & Sales", ["Recipes & Costing", "Simulate Sales (POS)", "Toast POS Import"]),
    ("💰 Financials", ["Financials"]),
    ("👥 Team", ["Scheduling", "Training & Quizzes", "Manager Logbook", "Checklists & SOPs"]),
    ("⚙️ Admin & Setup", ["Users & Roles", "Locations Setup", "About the Model"]),
]

PAGE_ICONS = {
    "Dashboard": "🏠",
    "Inventory & Stock": "📦",
    "Purchasing": "🛒",
    "Invoices (AP)": "🧾",
    "Physical Counts": "🔢",
    "Variance Report": "📉",
    "Recipes & Costing": "🍳",
    "Simulate Sales (POS)": "🧪",
    "Toast POS Import": "🍞",
    "Waste & Adjustments": "🗑️",
    "Order Suggestions (Par)": "📝",
    "Financials": "💰",
    "Users & Roles": "👤",
    "Locations Setup": "🏢",
    "Checklists & SOPs": "✅",
    "Manager Logbook": "📓",
    "Scheduling": "🗓️",
    "Training & Quizzes": "🎓",
    "About the Model": "ℹ️",
}

all_pages = [p for _, pages in NAV_GROUPS for p in pages]

def _page_allowed(p: str) -> bool:
    if p == "Financials":
        # Reachable either for the full financial view, or just to record
        # cash drawer counts (e.g. a server closing out a till) — not every
        # cash_management holder should also see the P&L / budget.
        return require_perm(user, "financials_view") or require_perm(user, "cash_management")
    return require_perm(user, PAGE_PERMISSION.get(p, "dashboard"))

allowed_pages = [p for p in all_pages if _page_allowed(p)]
if not allowed_pages:
    st.error("No permissions assigned to this role.")
    st.stop()

# Only show groups (and pages within them) this role can actually reach.
visible_groups = [(label, [p for p in pages if p in allowed_pages]) for label, pages in NAV_GROUPS]
visible_groups = [(label, pages) for label, pages in visible_groups if pages]
groups_by_label = dict(visible_groups)
group_labels = [g[0] for g in visible_groups]

prior_page = st.session_state.get("nav_page")
if "nav_group" not in st.session_state or st.session_state.nav_group not in group_labels:
    st.session_state.nav_group = next((g for g, pages in visible_groups if prior_page in pages), group_labels[0])

st.sidebar.markdown("**Navigate**")
chosen_group = st.sidebar.radio("Area", group_labels, key="nav_group", label_visibility="collapsed")

pages_in_group = groups_by_label[chosen_group]
if len(pages_in_group) == 1:
    page = pages_in_group[0]
else:
    page_display = {f"{PAGE_ICONS.get(p, '')} {p}": p for p in pages_in_group}
    display_labels = list(page_display.keys())
    default_display = next((d for d, p in page_display.items() if p == prior_page), display_labels[0])
    chosen_display = st.sidebar.radio(
        "Page", display_labels,
        index=display_labels.index(default_display),
        key=f"nav_page_radio_{chosen_group}",
        label_visibility="collapsed",
    )
    page = page_display[chosen_display]
st.session_state.nav_page = page
st.sidebar.divider()

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

# A consistent breadcrumb above every page's own title — same treatment
# everywhere, so the app doesn't feel like 19 differently-designed screens.
st.caption(f"{chosen_group}  ›  {PAGE_ICONS.get(page, '')} {page}")

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

    if require_perm(user, "financials_view"):
        wk_start, wk_end = week_bounds(date.today())
        pl = profit_and_loss(db, location.id, wk_start, wk_end)
        st.subheader("Prime Cost — this week")
        st.caption("Food cost + labor cost as a % of sales — the single most-watched restaurant KPI, straight off R365's playbook.")
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Sales", f"${pl['total_sales']:,.0f}")
        p2.metric("Food Cost %", f"{pl['cogs_pct']:.1f}%" if pl["cogs_pct"] is not None else "—")
        p3.metric("Labor Cost %", f"{pl['labor_pct']:.1f}%" if pl["labor_pct"] is not None else "—")
        p4.metric("Prime Cost %", f"{pl['prime_cost_pct']:.1f}%" if pl["prime_cost_pct"] is not None else "—")

# ---------- INVENTORY ----------
elif page == "Inventory & Stock":
    st.title("Inventory & Theoretical Stock")

    tab_snap, tab_manage = st.tabs(["Stock Snapshot", "Manage Items"])

    UNCATEGORIZED = "— Uncategorized —"

    with tab_snap:
        snapshot = get_current_theoretical_snapshot(db, location.id)
        df = pd.DataFrame(snapshot)
        if df.empty:
            st.info("No items yet — add one under Manage Items.")
        else:
            df["department"] = df["department"].fillna("Uncategorized")
            display_cols = ["name", "category", "theoretical_qty", "last_physical", "par_level", "base_unit", "current_cost"]
            col_config = {
                "theoretical_qty": st.column_config.NumberColumn("Theoretical", format="%.2f"),
                "last_physical": st.column_config.NumberColumn("Last Physical", format="%.2f"),
                "current_cost": st.column_config.NumberColumn("Cost", format="$%.3f"),
            }
            dept_names = ["All"] + DEPARTMENTS + ["Uncategorized"]
            counts = df["department"].value_counts()
            tab_labels = [
                f"All ({len(df)})" if d == "All" else f"{d} ({int(counts.get(d, 0))})"
                for d in dept_names
            ]
            dept_tabs = st.tabs(tab_labels)
            for dept_name, dept_tab in zip(dept_names, dept_tabs):
                with dept_tab:
                    sub_df = df if dept_name == "All" else df[df["department"] == dept_name]
                    if sub_df.empty:
                        st.caption("No items in this department yet.")
                    else:
                        st.dataframe(sub_df[display_cols], use_container_width=True, column_config=col_config, hide_index=True)

    with tab_manage:
        if not require_perm(user, "inventory_edit"):
            st.warning("You do not have permission to add or edit inventory items.")
        else:
            vendors_for_items = {"(none)": None}
            vendors_for_items.update({v.name: v.id for v in db.query(Vendor).filter(Vendor.is_active == True).all()})
            dept_choices = [UNCATEGORIZED] + DEPARTMENTS

            missing_dept_count = db.query(InventoryItem).filter(InventoryItem.department.is_(None)).count()
            if missing_dept_count:
                mc1, mc2 = st.columns([4, 1])
                mc1.info(f"{missing_dept_count} item(s) have no department assigned yet, so they won't show up under one of the Stock Snapshot tabs.")
                if mc2.button("Auto-assign", help="Guess a department from each item's existing category/name"):
                    n = auto_assign_departments(db)
                    st.success(f"Assigned a department to {n} item(s) — spot-check them under Edit Existing Item.")
                    st.rerun()

            st.subheader("Add New Item")
            with st.form("new_item_form", clear_on_submit=True):
                c1, c2 = st.columns(2)
                new_name = c1.text_input("Name *")
                new_sku = c2.text_input("SKU (optional)")
                c3, c4, c5 = st.columns(3)
                new_category = c3.text_input("Category", placeholder="Raw Food, Liquor, Paper…")
                new_subcategory = c4.text_input("Subcategory", placeholder="Spices, Vodka, Utensils…")
                new_unit = c5.text_input("Base Unit *", value="each", help="The unit you count/receive in, e.g. oz, lb, each, case")
                new_department = st.selectbox(
                    "Department", dept_choices,
                    help="Which Stock Snapshot tab this item shows up under.",
                )
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
                                department=None if new_department == UNCATEGORIZED else new_department,
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
                filter_choices = ["All"] + DEPARTMENTS + ["Uncategorized"]
                dept_filter = st.selectbox("Filter by department", filter_choices, key="edit_item_dept_filter")
                if dept_filter == "All":
                    filtered_items = all_items
                elif dept_filter == "Uncategorized":
                    filtered_items = [i for i in all_items if not i.department]
                else:
                    filtered_items = [i for i in all_items if i.department == dept_filter]

                if not filtered_items:
                    st.caption("No items in this department.")
                else:
                    item_labels = {f"{i.name}  {'(inactive)' if not i.is_active else ''}".strip(): i.id for i in filtered_items}
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
                        e_department = st.selectbox(
                            "Department", dept_choices,
                            index=dept_choices.index(edit_item.department) if edit_item.department in dept_choices else 0,
                        )
                        if not edit_item.department:
                            st.caption(f"Suggested: {suggest_department(edit_item.name, edit_item.category, edit_item.subcategory)}")
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
                                    department=None if e_department == UNCATEGORIZED else e_department,
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
                            confirm_action(
                                "Cancel this purchase order?",
                                f"PO {po.po_number} to {po.vendor.name if po.vendor else 'vendor'} will be cancelled. This can't be undone.",
                                _cancel_po_confirmed, (po.id,), "Cancel PO", f"po_{po.id}",
                            )

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
        pending_cost_changes = st.session_state.pop("approved_cost_changes", None)
        if pending_cost_changes:
            changes = pending_cost_changes["changes"]
            if changes:
                st.success(f"Invoice #{pending_cost_changes['invoice_id']} approved — {len(changes)} inventory cost(s) updated.")
                for chg in changes:
                    pct = f"  ({chg['pct_change']:+.1f}%)" if chg["pct_change"] is not None else ""
                    with st.expander(f"{chg['item_name']}: ${chg['old_cost']:.4f} → ${chg['new_cost']:.4f}{pct}"):
                        if chg["affected_recipes"]:
                            st.caption("This price change cascades into the live cost of:")
                            for r in chg["affected_recipes"]:
                                tag = "🍽️ Menu Item" if r["recipe_type"] == "Menu Item" else "🥣 Batch/Prep"
                                via = "" if r["is_direct"] else " (via a sub-recipe)"
                                st.write(f"- {r['recipe_name']} — {tag}{via}")
                        else:
                            st.caption("Not currently used in any recipe.")
            else:
                st.success(f"Invoice #{pending_cost_changes['invoice_id']} approved.")

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
                                approved = approve_invoice(db, inv.id)
                                changes = getattr(approved, "cost_changes", None) or []
                                db.commit()
                                st.session_state["approved_cost_changes"] = {"invoice_id": inv.id, "changes": changes}
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
                            confirm_action(
                                "Reject this invoice?",
                                f"Invoice {inv.invoice_number or f'#{inv.id}'} from {inv.vendor.name if inv.vendor else 'vendor'} will be marked rejected. This can't be undone.",
                                _reject_invoice_confirmed, (inv.id,), "Reject invoice", f"inv_{inv.id}",
                            )

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
        def _render_recipe_list(recipe_list):
            if not recipe_list:
                st.caption("No recipes in this view.")
                return
            for recipe in recipe_list:
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

        all_active_recipes = db.query(Recipe).filter(Recipe.is_active == True).order_by(Recipe.name).all()
        menu_items = [r for r in all_active_recipes if r.recipe_type == "Menu Item"]
        batch_recipes = [r for r in all_active_recipes if r.recipe_type != "Menu Item"]

        sub_all, sub_menu, sub_batch = st.tabs([
            f"All ({len(all_active_recipes)})",
            f"🍽️ Menu Items ({len(menu_items)})",
            f"🥣 Batch & Prep Recipes ({len(batch_recipes)})",
        ])
        with sub_all:
            _render_recipe_list(all_active_recipes)
        with sub_menu:
            if not menu_items:
                st.caption("No recipes in this view.")
            else:
                # Group Menu Items by menu section (stored in Recipe.category —
                # e.g. Appetizers, Entrees, Desserts, Beverages) so a full menu
                # can be browsed the way it reads on the actual menu, not just
                # as one flat list.
                sections = {}
                for r in menu_items:
                    key = (r.category or "").strip() or "Uncategorized"
                    sections.setdefault(key, []).append(r)
                section_names = sorted(sections.keys(), key=lambda s: (s == "Uncategorized", s.lower()))
                section_tabs = st.tabs([f"{s} ({len(sections[s])})" for s in section_names])
                for section_tab, s in zip(section_tabs, section_names):
                    with section_tab:
                        _render_recipe_list(sections[s])
        with sub_batch:
            _render_recipe_list(batch_recipes)

    with tab_manage:
        if not require_perm(user, "recipes_edit"):
            st.warning("You do not have permission to add or edit recipes.")
        else:
            missing_type_count = db.query(Recipe).filter(Recipe.recipe_type.is_(None)).count()
            if missing_type_count:
                mtc1, mtc2 = st.columns([4, 1])
                mtc1.info(f"{missing_type_count} recipe(s) have no type assigned yet, so they won't show up under Menu Items or Batch & Prep Recipes above.")
                if mtc2.button("Auto-assign", help="Guess Menu Item vs. Batch/Prep from each recipe's menu price"):
                    n = auto_assign_recipe_types(db)
                    st.success(f"Assigned a type to {n} recipe(s) — spot-check them below.")
                    st.rerun()

            st.subheader("Create New Recipe")
            with st.form("new_recipe_form", clear_on_submit=True):
                c1, c2 = st.columns(2)
                r_name = c1.text_input("Name *")
                r_category = c2.text_input(
                    "Menu Section / Category",
                    placeholder="Appetizers, Entrees, Desserts, Beverages… (or Sauces, Stocks… for Batch/Prep)",
                    help="For Menu Items this groups recipes by menu section on the View & Cost tab (e.g. Appetizers, Entrees, Desserts, Beverages). For Batch/Prep it's just a free-text category.",
                )
                c3, c4, c5 = st.columns(3)
                r_yield_qty = c3.number_input("Yield Qty", min_value=0.01, value=1.0, step=0.5)
                r_yield_unit = c4.text_input("Yield Unit", value="serving")
                r_menu_price = c5.number_input("Menu Price $", min_value=0.0, value=0.0, step=0.5)
                r_type = st.selectbox(
                    "Recipe Type", RECIPE_TYPES,
                    help="Menu Item = sold directly to guests. Batch/Prep = an internal build used inside other recipes.",
                )
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
                                recipe_type=r_type,
                            )
                            db.commit()
                            st.success(f"Created {r.name} — add ingredients below.")
                            st.session_state["edit_recipe_id"] = r.id
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

            st.divider()
            st.subheader("Edit Recipe & Ingredients")
            fc1, fc2 = st.columns(2)
            type_filter = fc1.selectbox("Filter by type", ["All"] + RECIPE_TYPES, key="edit_recipe_type_filter")
            all_recipes_q = db.query(Recipe).order_by(Recipe.name)
            if type_filter != "All":
                all_recipes_q = all_recipes_q.filter(Recipe.recipe_type == type_filter)

            if type_filter == "Menu Item":
                existing_sections = sorted({
                    (r.category or "").strip() or "Uncategorized"
                    for r in db.query(Recipe).filter(Recipe.recipe_type == "Menu Item").all()
                })
                section_filter = fc2.selectbox("Filter by menu section", ["All"] + existing_sections, key="edit_recipe_section_filter")
                if section_filter != "All":
                    if section_filter == "Uncategorized":
                        all_recipes_q = all_recipes_q.filter((Recipe.category.is_(None)) | (Recipe.category == ""))
                    else:
                        all_recipes_q = all_recipes_q.filter(Recipe.category == section_filter)

            all_recipes = all_recipes_q.all()
            if not all_recipes:
                st.info("No recipes match this filter.")
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
                    e_category = c2.text_input(
                        "Menu Section / Category", value=recipe.category or "",
                        help="For Menu Items this groups recipes by menu section on the View & Cost tab (e.g. Appetizers, Entrees, Desserts, Beverages). For Batch/Prep it's just a free-text category.",
                    )
                    c3, c4, c5 = st.columns(3)
                    e_yield_qty = c3.number_input("Yield Qty", min_value=0.01, value=float(recipe.yield_qty or 1), step=0.5)
                    e_yield_unit = c4.text_input("Yield Unit", value=recipe.yield_unit)
                    e_menu_price = c5.number_input("Menu Price $", min_value=0.0, value=float(recipe.menu_price or 0), step=0.5)
                    e_type_idx = RECIPE_TYPES.index(recipe.recipe_type) if recipe.recipe_type in RECIPE_TYPES else RECIPE_TYPES.index(suggest_recipe_type(recipe.menu_price))
                    e_type = st.selectbox("Recipe Type", RECIPE_TYPES, index=e_type_idx)
                    e_desc = st.text_area("Description", value=recipe.description or "")
                    e_instructions = st.text_area("Instructions", value=recipe.instructions or "")
                    if st.form_submit_button("Save Recipe Details", type="primary"):
                        try:
                            update_recipe(
                                db, recipe.id, name=e_name, category=e_category,
                                yield_qty=e_yield_qty, yield_unit=e_yield_unit,
                                menu_price=e_menu_price or None, description=e_desc, instructions=e_instructions,
                                recipe_type=e_type,
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
                            confirm_action(
                                "Remove this ingredient?",
                                f"“{label}” will be removed from {recipe.name}. This can't be undone.",
                                _remove_ingredient_confirmed, (ing.id,), "Remove", f"ing_{ing.id}",
                            )
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
                                    recipe_type="Batch/Prep",
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
                                    recipe_type="Menu Item",
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
    st.caption(
        "Port Toast order data into theoretical inventory (map menu items → recipes, then import), "
        "or upload a Toast sales-summary report for real COGS and menu analysis — see the tabs below."
    )

    from app.services.toast_import import ToastSalesImporter
    import json

    importer = ToastSalesImporter(db, location.id)
    recipes = {r.name: r.id for r in db.query(Recipe).filter(Recipe.is_active == True).all()}

    tab1, tab2, tab3, tab4 = st.tabs([
        "Import Sample / JSON", "Item Mappings", "How to Connect Live Toast",
        "📊 Sales Report & Menu Analysis",
    ])

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

    with tab4:
        st.subheader("Sales Report & Menu Analysis")
        st.caption(
            "Upload a Toast menu sales-summary export (e.g. the \"All Levels\" report — one row per "
            "menu item with quantity sold and revenue for a reporting period). Toast leaves COGS and "
            "gross margin blank on these exports; this cross-references what actually sold against your "
            "live recipe costs — which already reflect current ingredient prices, including anything an "
            "approved invoice just cascaded in — to fill that gap and flag your best/worst menu items."
        )

        up_c1, up_c2 = st.columns(2)
        with up_c1:
            st.markdown("**Upload a new report**")
            if not require_perm(user, "pos_import"):
                st.warning("You do not have permission to import sales reports.")
            else:
                sales_csv = st.file_uploader("Toast sales-summary CSV", type=["csv"], key="sales_report_upload")
                period_label = st.text_input("Period label (optional)", placeholder="Week of Aug 4–10", key="sales_period_label")
                pl_c1, pl_c2 = st.columns(2)
                period_start = pl_c1.date_input("Period start (optional)", value=None, key="sales_period_start")
                period_end = pl_c2.date_input("Period end (optional)", value=None, key="sales_period_end")
                if sales_csv is not None and st.button("Parse & Import", type="primary", key="sales_report_import_btn"):
                    try:
                        rows, totals = parse_toast_sales_csv(sales_csv.getvalue())
                        batch = create_sales_report_import(
                            db, location.id, rows, report_totals=totals,
                            filename=sales_csv.name, period_label=period_label or None,
                            period_start=period_start or None, period_end=period_end or None,
                        )
                        st.session_state["sales_report_view_id"] = batch.id
                        st.success(f"Imported {len(rows)} row(s) from “{sales_csv.name}”.")
                        st.rerun()
                    except SalesImportParseError as e:
                        st.error(str(e))
                    except Exception as e:
                        st.error(f"Couldn't import this file: {e}")

        with up_c2:
            st.markdown("**View a past import**")
            past_imports = list_sales_report_imports(db, location.id)
            selected_import_id = None
            if not past_imports:
                st.info("No sales reports imported yet.")
            else:
                import_labels = {
                    f"{b.period_label or b.original_filename or f'Import #{b.id}'} — {b.imported_at.strftime('%Y-%m-%d %H:%M')}": b.id
                    for b in past_imports
                }
                ids_order = list(import_labels.values())
                default_idx = 0
                target = st.session_state.get("sales_report_view_id")
                if target in ids_order:
                    default_idx = ids_order.index(target)
                pick_import = st.selectbox("Import", list(import_labels.keys()), index=default_idx, key="sales_report_pick")
                selected_import_id = import_labels[pick_import]
                st.session_state["sales_report_view_id"] = selected_import_id
                if require_perm(user, "pos_import"):
                    if st.button("Delete this import", key=f"del_sales_import_{selected_import_id}"):
                        confirm_action(
                            "Delete this sales report import?",
                            "The uploaded rows and this analysis will be permanently deleted. This doesn't affect inventory, recipes, or invoices.",
                            delete_sales_report_import, (selected_import_id,), "Delete import", f"sales_import_{selected_import_id}",
                        )

        st.divider()

        if selected_import_id:
            analysis = get_menu_analysis(db, selected_import_id)
            summary = analysis["summary"]
            can_cost = require_perm(user, "view_costs")

            st.markdown(f"### {analysis['period_label'] or 'Sales Analysis'}")
            if analysis["period_start"] or analysis["period_end"]:
                st.caption(f"Period: {analysis['period_start'] or '—'} to {analysis['period_end'] or '—'}")

            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Total Net Sales", f"${summary['total_net_sales']:,.2f}")
            if can_cost:
                k2.metric("Theoretical COGS", f"${summary['total_theoretical_cogs']:,.2f}")
                fc_pct = summary["blended_food_cost_pct"]
                k3.metric("Food Cost %", f"{fc_pct}%" if fc_pct is not None else "—")
                k4.metric("Gross Profit", f"${summary['total_gross_profit']:,.2f}")
            else:
                k2.metric("Matched Items", summary["matched_item_count"])
                k3.metric("Unmatched Items", summary["unmatched_item_count"])

            if summary["reported_net_sales"] is not None and abs(summary["reconciliation_delta"] or 0) > 1:
                st.caption(
                    f"ℹ️ Toast's own report total was ${summary['reported_net_sales']:,.2f} net sales; the sum "
                    f"of itemized rows here is ${summary['total_net_sales']:,.2f} "
                    f"(${summary['reconciliation_delta']:,.2f} difference — Toast's grand-total row can include "
                    f"figures not broken out per item)."
                )

            quadrant_labels = {"Star": "⭐ Star", "Plowhorse": "🐴 Plowhorse", "Puzzle": "🧩 Puzzle", "Dog": "🐶 Dog"}

            if analysis["items"]:
                st.markdown("**Menu items — by section**")
                sections = {}
                for it in analysis["items"]:
                    sections.setdefault(it["menu_section"], []).append(it)
                for section in sorted(sections.keys()):
                    its = sections[section]
                    with st.expander(f"{section} ({len(its)})", expanded=False):
                        rows_display = []
                        for it in its:
                            row = {
                                "Item": it["recipe_name"],
                                "Qty Sold": it["qty_sold"],
                                "Net Sales": it["net_sales"],
                                "Avg Price (Toast)": it["effective_avg_price"],
                            }
                            if can_cost:
                                row.update({
                                    "Unit Cost": it["unit_cost"],
                                    "Theoretical COGS": it["theoretical_cogs"],
                                    "Food Cost %": it["food_cost_pct"],
                                    "Gross Profit": it["gross_profit"],
                                })
                            row["Menu Engineering"] = quadrant_labels.get(it["quadrant"], "—")
                            row["Price Drift"] = (
                                f"{it['price_drift_pct']:+.1f}% vs. menu price on file"
                                if it["price_drift_pct"] is not None else ""
                            )
                            rows_display.append(row)
                        st.dataframe(pd.DataFrame(rows_display), use_container_width=True, hide_index=True)
            else:
                st.info("No sales lines matched an existing recipe yet.")

            if analysis["unmatched_items"]:
                with st.expander(f"⚠️ Unmatched items — no recipe on file ({len(analysis['unmatched_items'])})", expanded=False):
                    st.caption(
                        "These sold on Toast but don't match a recipe by name, so they're excluded from the "
                        "COGS numbers above. Add a recipe under Recipes & Costing, or manually map one below."
                    )
                    st.dataframe(
                        pd.DataFrame([
                            {"Item": i["item_name"], "Section": i["menu_section"], "Qty Sold": i["qty_sold"], "Net Sales": i["net_amt"]}
                            for i in analysis["unmatched_items"]
                        ]),
                        use_container_width=True, hide_index=True,
                    )
                    if require_perm(user, "pos_import") and recipes:
                        st.markdown("**Manually map an unmatched item**")
                        unmatched_names = [i["item_name"] for i in analysis["unmatched_items"]]
                        mc1, mc2, mc3 = st.columns([2, 2, 1])
                        pick_name = mc1.selectbox("Toast item", unmatched_names, key="remap_item_pick")
                        pick_recipe_label = mc2.selectbox("Map to recipe", list(recipes.keys()), key="remap_recipe_pick")
                        if mc3.button("Map", key="remap_confirm_btn"):
                            n = set_lines_recipe_by_item_name(
                                db, selected_import_id, pick_name, recipes[pick_recipe_label]
                            )
                            st.success(f"Mapped {n} row(s) of “{pick_name}” to {pick_recipe_label}.")
                            st.rerun()

            if can_cost:
                st.divider()
                st.markdown("**Cross-check against actual purchases**")
                st.caption(
                    "Compares the theoretical COGS above (recipe cost × units sold) against dollars actually "
                    "spent on inventory items via approved invoices in a date range you choose. A large gap "
                    "can mean over-ordering, waste, portioning drift, or that the range doesn't line up with "
                    "the sales report's period — only invoice lines matched to an inventory item are counted."
                )
                pc1, pc2, pc3 = st.columns(3)
                default_start = analysis["period_start"] or (date.today() - timedelta(days=7))
                default_end = analysis["period_end"] or date.today()
                pstart = pc1.date_input("Purchases from", value=default_start, key="purchases_cmp_start")
                pend = pc2.date_input("Purchases to", value=default_end, key="purchases_cmp_end")
                if pc3.button("Compare", key="purchases_cmp_btn"):
                    st.session_state["purchases_cmp_result"] = purchases_comparison(
                        db, location.id, pstart, pend, summary["total_theoretical_cogs"]
                    )
                cmp = st.session_state.get("purchases_cmp_result")
                if cmp:
                    cc1, cc2, cc3 = st.columns(3)
                    cc1.metric("Actual Purchases (Invoices)", f"${cmp['actual_purchases']:,.2f}")
                    cc2.metric("Theoretical COGS", f"${cmp['theoretical_cogs']:,.2f}")
                    cc3.metric(
                        "Variance",
                        f"${cmp['variance_dollars']:,.2f}",
                        delta=f"{cmp['variance_pct']:+.1f}%" if cmp["variance_pct"] is not None else None,
                        delta_color="inverse",
                    )
                    st.caption(
                        f"Based on {cmp['invoice_line_count']} approved/paid invoice line(s) between "
                        f"{cmp['start']} and {cmp['end']}."
                    )
        else:
            st.info("Upload a sales report above to see the menu analysis.")

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

# ---------- FINANCIALS (R365-style operating P&L) ----------
elif page == "Financials":
    st.title("Financials")
    st.caption("An operator's P&L: sales, theoretical food cost, scheduled labor, and other operating expenses rolling up to prime cost — plus budgets and cash drawer counts.")

    can_view = require_perm(user, "financials_view")
    can_admin = require_perm(user, "financials_admin")
    can_cash = require_perm(user, "cash_management")

    if not (can_view or can_cash):
        st.error("You do not have access to financials.")
        st.stop()

    # Everyone who reaches this page sees all five tabs, but content inside
    # each is gated individually — e.g. a server with cash_management but not
    # financials_view can open Cash Management and close out a drawer, while
    # P&L / budget / expense detail stay hidden behind a permission notice.
    tab_pl, tab_budget, tab_cash, tab_expenses, tab_consolidated = st.tabs(
        ["Operating P&L", "Budget vs. Actual", "Cash Management", "Expenses", "Consolidated"]
    )

    # ---- Operating P&L ----
    with tab_pl:
        if not can_view:
            st.warning("Viewing the P&L requires the financials_view permission.")
        else:
            c1, c2 = st.columns(2)
            default_start = date.today().replace(day=1)
            pl_start = c1.date_input("From", value=default_start, key="pl_start")
            pl_end = c2.date_input("To", value=date.today(), key="pl_end")
            if pl_start > pl_end:
                st.error("Start date must be before end date.")
            else:
                start_dt = datetime(pl_start.year, pl_start.month, pl_start.day, tzinfo=timezone.utc)
                end_dt = datetime(pl_end.year, pl_end.month, pl_end.day, 23, 59, 59, tzinfo=timezone.utc)
                pl = profit_and_loss(db, location.id, start_dt, end_dt)

                m1, m2, m3 = st.columns(3)
                m1.metric("Sales", f"${pl['total_sales']:,.2f}")
                m2.metric("Theoretical Food Cost", f"${pl['cogs']:,.2f}", f"{pl['cogs_pct']:.1f}% of sales" if pl["cogs_pct"] is not None else None)
                m3.metric("Scheduled Labor Cost", f"${pl['labor_cost']:,.2f}", f"{pl['labor_pct']:.1f}% of sales" if pl["labor_pct"] is not None else None)

                m4, m5, m6 = st.columns(3)
                m4.metric("Prime Cost", f"${pl['prime_cost']:,.2f}", f"{pl['prime_cost_pct']:.1f}% of sales" if pl["prime_cost_pct"] is not None else None)
                m5.metric("Other Operating Expenses", f"${pl['other_expenses']:,.2f}")
                m6.metric("Est. Operating Income", f"${pl['operating_income']:,.2f}", f"{pl['operating_income_pct']:.1f}% of sales" if pl["operating_income_pct"] is not None else None)

                if pl["other_expenses_by_category"]:
                    st.subheader("Other expenses by category")
                    st.dataframe(
                        pd.DataFrame(
                            [{"Category": k, "Amount": v} for k, v in pl["other_expenses_by_category"].items()]
                        ),
                        use_container_width=True, hide_index=True,
                    )
                st.caption("Theoretical food cost values what was actually sold (per the POS) at each recipe's current cost — it will diverge from an actual cost-of-goods-purchased number by whatever shows up on the Variance Report.")

    # ---- Budget vs Actual ----
    with tab_budget:
        if not can_view:
            st.warning("Viewing the budget requires the financials_view permission.")
        else:
            b1, b2 = st.columns(2)
            budget_year = b1.number_input("Year", min_value=2020, max_value=2100, value=date.today().year, key="budget_year")
            budget_month = b2.selectbox("Month", list(range(1, 13)), index=date.today().month - 1,
                                         format_func=lambda m: date(2000, m, 1).strftime("%B"), key="budget_month")

            if can_admin:
                with st.expander("Set / update budget for this month", expanded=False):
                    existing = get_budget(db, location.id, int(budget_year), int(budget_month))
                    with st.form("budget_form"):
                        sales_t = st.number_input("Sales target ($)", min_value=0.0, step=100.0,
                                                   value=float(existing.sales_target) if existing else 0.0)
                        fc1, fc2 = st.columns(2)
                        food_t = fc1.number_input("Food cost % target", min_value=0.0, max_value=100.0, step=0.5,
                                                   value=float(existing.food_cost_pct_target) if existing else 30.0)
                        labor_t = fc2.number_input("Labor cost % target", min_value=0.0, max_value=100.0, step=0.5,
                                                    value=float(existing.labor_cost_pct_target) if existing else 30.0)
                        other_t = st.number_input("Other operating expense target ($)", min_value=0.0, step=50.0,
                                                   value=float(existing.other_expense_target) if existing else 0.0)
                        notes_t = st.text_area("Notes", value=existing.notes if existing else "")
                        if st.form_submit_button("Save budget", type="primary"):
                            upsert_budget(
                                db, location.id, int(budget_year), int(budget_month),
                                sales_target=sales_t, food_cost_pct_target=food_t,
                                labor_cost_pct_target=labor_t, other_expense_target=other_t,
                                notes=notes_t or None,
                            )
                            st.success("Budget saved")
                            st.rerun()

            bva = budget_vs_actual(db, location.id, int(budget_year), int(budget_month))
            if not bva["budget"]:
                st.info("No budget set for this month yet." + (" Use the form above." if can_admin else ""))
            else:
                budget, actual, variance = bva["budget"], bva["actual"], bva["variance"]
                st.subheader(f"{date(2000, int(budget_month), 1).strftime('%B')} {int(budget_year)}")
                rows = [
                    {"Metric": "Sales", "Budget": budget["sales_target"], "Actual": actual["total_sales"], "Variance": variance["sales_variance"]},
                    {"Metric": "Food Cost", "Budget": budget["budgeted_food_cost"], "Actual": actual["cogs"], "Variance": variance["food_cost_variance"]},
                    {"Metric": "Labor Cost", "Budget": budget["budgeted_labor_cost"], "Actual": actual["labor_cost"], "Variance": variance["labor_cost_variance"]},
                    {"Metric": "Other Expenses", "Budget": budget["other_expense_target"], "Actual": actual["other_expenses"], "Variance": variance["other_expense_variance"]},
                ]
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                st.caption("Variance = Actual − Budget. For costs, negative means under budget (good); for sales, negative means under target.")

    # ---- Cash Management ----
    with tab_cash:
        if not can_cash:
            st.warning("Recording cash counts requires the cash_management permission.")
        else:
            st.subheader("Record a cash drawer count")
            with st.form("cash_count_form"):
                cc1, cc2 = st.columns(2)
                cash_date = cc1.date_input("Business date", value=date.today())
                shift_label = cc2.selectbox("Shift", ["Open", "Mid", "Close"])
                ce1, ce2 = st.columns(2)
                expected = ce1.number_input("Expected amount ($)", min_value=0.0, step=1.0)
                counted = ce2.number_input("Counted amount ($)", min_value=0.0, step=1.0)
                cash_notes = st.text_input("Notes (optional)")
                if st.form_submit_button("Record count", type="primary"):
                    record_cash_count(
                        db, location.id, cash_date, shift_label, expected, counted,
                        counted_by_id=user.id, notes=cash_notes or None,
                    )
                    st.success("Cash count recorded")
                    st.rerun()

            st.subheader("Recent counts")
            cash_start = date.today() - timedelta(days=30)
            summary = cash_variance_summary(db, location.id, cash_start, date.today())
            if not summary["counts"]:
                st.info("No cash counts recorded in the last 30 days.")
            else:
                s1, s2, s3 = st.columns(3)
                s1.metric("Counts (30d)", summary["count"])
                s2.metric("Total Over/Short", f"${summary['total_over_short']:,.2f}")
                s3.metric("Total Short", f"${summary['total_short']:,.2f}")
                st.dataframe(
                    pd.DataFrame([
                        {
                            "Date": c.business_date.strftime("%Y-%m-%d"),
                            "Shift": c.shift_label,
                            "Counted by": c.counted_by.name if c.counted_by else "—",
                            "Expected": c.expected_amount,
                            "Counted": c.counted_amount,
                            "Over/Short": c.over_short,
                            "Notes": c.notes or "",
                        }
                        for c in summary["counts"]
                    ]),
                    use_container_width=True, hide_index=True,
                )

    # ---- Expenses ----
    with tab_expenses:
        if not can_view:
            st.warning("Viewing expenses requires the financials_view permission.")
        else:
            if can_admin:
                st.subheader("Log an operating expense")
                with st.form("expense_form"):
                    ec1, ec2 = st.columns(2)
                    exp_category = ec1.selectbox("Category", EXPENSE_CATEGORIES)
                    exp_amount = ec2.number_input("Amount ($)", min_value=0.0, step=10.0)
                    exp_date = st.date_input("Date", value=date.today(), key="exp_date")
                    exp_desc = st.text_input("Description (optional)")
                    exp_recurring = st.checkbox("Recurring monthly expense", value=False)
                    if st.form_submit_button("Add expense", type="primary"):
                        create_expense(
                            db, location.id, exp_category, exp_amount,
                            expense_date=exp_date, description=exp_desc or None,
                            is_recurring=exp_recurring,
                        )
                        st.success("Expense logged")
                        st.rerun()

            st.subheader("Expenses")
            exp_start = st.session_state.get("pl_start", date.today().replace(day=1))
            exp_end = st.session_state.get("pl_end", date.today())
            expenses = list_expenses(db, location.id, exp_start, exp_end)
            if not expenses:
                st.info("No expenses logged for the P&L date range selected above.")
            else:
                for e in expenses:
                    row1, row2 = st.columns([5, 1])
                    row1.write(f"**{e.expense_date.strftime('%Y-%m-%d')}** · {e.category} · ${e.amount:,.2f}" + (f" — {e.description}" if e.description else ""))
                    if can_admin and row2.button("Delete", key=f"del_exp_{e.id}"):
                        confirm_action(
                            "Delete this expense?",
                            f"{e.category} · ${e.amount:,.2f} on {e.expense_date.strftime('%Y-%m-%d')} will be permanently deleted.",
                            delete_expense, (e.id,), "Delete expense", f"exp_{e.id}",
                        )

    # ---- Consolidated (multi-location) ----
    with tab_consolidated:
        if not can_view:
            st.warning("Consolidated reporting requires the financials_view permission.")
        else:
            family_ids = location_family_ids(db, location.id)
            if len(family_ids) <= 1:
                st.info("This location has no child locations to consolidate. Set a **Parent location** under Locations Setup to roll multiple locations up under this one.")
            else:
                cs1, cs2 = st.columns(2)
                cons_start = cs1.date_input("From", value=date.today().replace(day=1), key="cons_start")
                cons_end = cs2.date_input("To", value=date.today(), key="cons_end")
                start_dt = datetime(cons_start.year, cons_start.month, cons_start.day, tzinfo=timezone.utc)
                end_dt = datetime(cons_end.year, cons_end.month, cons_end.day, 23, 59, 59, tzinfo=timezone.utc)
                cons = consolidated_pl(db, location.id, start_dt, end_dt)

                t = cons["totals"]
                k1, k2, k3, k4 = st.columns(4)
                k1.metric("Consolidated Sales", f"${t['total_sales']:,.0f}")
                k2.metric("Food Cost %", f"{t['cogs_pct']:.1f}%" if t["cogs_pct"] is not None else "—")
                k3.metric("Labor Cost %", f"{t['labor_pct']:.1f}%" if t["labor_pct"] is not None else "—")
                k4.metric("Prime Cost %", f"{t['prime_cost_pct']:.1f}%" if t["prime_cost_pct"] is not None else "—")

                st.subheader("By location")
                st.dataframe(
                    pd.DataFrame([
                        {
                            "Location": r["location_name"],
                            "Sales": r["total_sales"],
                            "Food Cost %": r["cogs_pct"],
                            "Labor %": r["labor_pct"],
                            "Prime Cost %": r["prime_cost_pct"],
                            "Operating Income": r["operating_income"],
                        }
                        for r in cons["locations"]
                    ]),
                    use_container_width=True, hide_index=True,
                )

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
                deact_options = {f"{u.name} ({u.role.name})": u.id for u in active}
                deact_label = st.selectbox("User", list(deact_options.keys()))
                if st.button("Deactivate", type="secondary"):
                    deact_id = deact_options[deact_label]
                    confirm_action(
                        "Deactivate this user?",
                        f"{deact_label} will no longer be able to sign in. You can reactivate them anytime from this page.",
                        deactivate_user, (deact_id,), "Deactivate", f"user_{deact_id}",
                    )

            if require_perm(user, "scheduling_edit"):
                st.subheader("Set pay rate")
                st.caption("Used to estimate scheduled labor cost / labor % on the Scheduling page.")
                if active:
                    rate_options = {f"{u.name} ({u.role.name})": u.id for u in active}
                    rate_label = st.selectbox("Employee", list(rate_options.keys()), key="rate_user_pick")
                    new_rate = st.number_input("Hourly rate ($)", min_value=0.0, step=0.25, value=0.0)
                    if st.button("Update rate"):
                        set_hourly_rate(db, rate_options[rate_label], new_rate)
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
        "Financials & cash": [
            "financials_view", "financials_admin", "cash_management",
        ],
        "Communication": [
            "logbook",
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
            TASK_TYPES = ["checkbox", "photo", "text", "number", "score"]
            SOP_CATEGORIES = ["opening", "closing", "safety", "prep", "FOH", "other"]
            LIST_TYPES = ["ops", "opening", "closing", "food_safety", "walkthrough", "cleaning"]

            admin_sops = list_sops(db, location_id=location.id, active_only=False)
            admin_templates = list_templates(db, location_id=location.id, active_only=False)

            # ---------------- SOPs ----------------
            st.subheader("SOPs (Info Library)")
            if not admin_sops:
                st.caption("No SOPs yet.")
            for sop in admin_sops:
                status = "🟢" if sop.is_active else "⚪ inactive ·"
                with st.expander(f"{status} {sop.title} · {sop.category or 'general'}"):
                    if st.session_state.get("editing_sop_id") == sop.id:
                        with st.form(f"edit_sop_{sop.id}"):
                            e_title = st.text_input("Title", value=sop.title)
                            e_category = st.selectbox(
                                "Category", SOP_CATEGORIES,
                                index=SOP_CATEGORIES.index(sop.category) if sop.category in SOP_CATEGORIES else 0,
                            )
                            e_body = st.text_area("Body (steps / markdown)", value=sop.body or "")
                            e_roles = st.text_input("Role codes (comma-separated)", value=sop.role_codes or "")
                            e_version = st.text_input("Version", value=sop.version or "1.0")
                            s1, s2 = st.columns(2)
                            if s1.form_submit_button("Save", type="primary"):
                                if not e_title.strip():
                                    st.error("Title is required.")
                                else:
                                    update_sop(
                                        db, sop.id, title=e_title, body=e_body, category=e_category,
                                        role_codes=e_roles, version=e_version,
                                    )
                                    st.session_state.pop("editing_sop_id", None)
                                    st.success("SOP updated")
                                    st.rerun()
                            if s2.form_submit_button("Cancel"):
                                st.session_state.pop("editing_sop_id", None)
                                st.rerun()
                    else:
                        st.caption(f"Roles: {sop.role_codes or 'all'} · v{sop.version or '1.0'}")
                        st.markdown(sop.body or "_No content_")
                        b1, b2, b3 = st.columns(3)
                        if b1.button("Edit", key=f"edit_sop_btn_{sop.id}"):
                            st.session_state["editing_sop_id"] = sop.id
                            st.rerun()
                        if sop.is_active:
                            if b2.button("Deactivate", key=f"deact_sop_{sop.id}"):
                                set_sop_active(db, sop.id, False)
                                st.rerun()
                        else:
                            if b2.button("Reactivate", key=f"react_sop_{sop.id}"):
                                set_sop_active(db, sop.id, True)
                                st.rerun()
                        if b3.button("Delete", key=f"del_sop_{sop.id}"):
                            confirm_action(
                                "Delete this SOP?",
                                f"'{sop.title}' will be permanently deleted. This can't be undone.",
                                delete_sop, (sop.id,), "Delete SOP", f"sop_{sop.id}",
                            )

            with st.expander("➕ Create SOP"):
                with st.form("new_sop"):
                    title = st.text_input("Title")
                    category = st.selectbox("Category", SOP_CATEGORIES)
                    body = st.text_area("Body (steps / markdown)")
                    roles = st.text_input("Role codes (comma-separated)", value="kitchen,manager")
                    if st.form_submit_button("Save SOP"):
                        if not title.strip():
                            st.error("Title is required.")
                        else:
                            create_sop(db, title, body=body, category=category, role_codes=roles, location_id=location.id)
                            st.success("SOP created")
                            st.rerun()

            st.divider()

            # ---------------- Checklist templates + their tasks ----------------
            st.subheader("Checklist templates")
            if not admin_templates:
                st.caption("No templates yet.")
            for tmpl in admin_templates:
                status = "🟢" if tmpl.is_active else "⚪ inactive ·"
                with st.expander(f"{status} {tmpl.name} · {tmpl.list_type or 'ops'} · {len(tmpl.tasks)} task(s)"):
                    if st.session_state.get("editing_template_id") == tmpl.id:
                        with st.form(f"edit_tmpl_{tmpl.id}"):
                            e_name = st.text_input("Name", value=tmpl.name)
                            e_desc = st.text_input("Description", value=tmpl.description or "")
                            e_ltype = st.selectbox(
                                "List type", LIST_TYPES,
                                index=LIST_TYPES.index(tmpl.list_type) if tmpl.list_type in LIST_TYPES else 0,
                            )
                            e_roles = st.text_input("Role codes (comma-separated)", value=tmpl.role_codes or "")
                            e_hint = st.text_input("Schedule hint", value=tmpl.schedule_hint or "")
                            sop_options = {"— None —": None}
                            sop_options.update({s.title: s.id for s in admin_sops})
                            current_sop_label = next((k for k, v in sop_options.items() if v == tmpl.sop_id), "— None —")
                            e_sop_label = st.selectbox(
                                "Linked SOP", list(sop_options.keys()),
                                index=list(sop_options.keys()).index(current_sop_label),
                            )
                            s1, s2 = st.columns(2)
                            if s1.form_submit_button("Save", type="primary"):
                                if not e_name.strip():
                                    st.error("Name is required.")
                                else:
                                    update_template(
                                        db, tmpl.id, name=e_name, description=e_desc, list_type=e_ltype,
                                        role_codes=e_roles, schedule_hint=e_hint, sop_id=sop_options[e_sop_label],
                                    )
                                    st.session_state.pop("editing_template_id", None)
                                    st.success("Template updated")
                                    st.rerun()
                            if s2.form_submit_button("Cancel"):
                                st.session_state.pop("editing_template_id", None)
                                st.rerun()
                    else:
                        st.caption(tmpl.description or "")
                        if tmpl.schedule_hint:
                            st.caption(f"Schedule hint: {tmpl.schedule_hint}")
                        b1, b2, b3 = st.columns(3)
                        if b1.button("Edit", key=f"edit_tmpl_btn_{tmpl.id}"):
                            st.session_state["editing_template_id"] = tmpl.id
                            st.rerun()
                        if tmpl.is_active:
                            if b2.button("Deactivate", key=f"deact_tmpl_{tmpl.id}"):
                                set_template_active(db, tmpl.id, False)
                                st.rerun()
                        else:
                            if b2.button("Reactivate", key=f"react_tmpl_{tmpl.id}"):
                                set_template_active(db, tmpl.id, True)
                                st.rerun()
                        if b3.button("Delete", key=f"del_tmpl_{tmpl.id}"):
                            confirm_action(
                                "Delete this checklist template?",
                                f"'{tmpl.name}' and its {len(tmpl.tasks)} task(s) will be permanently deleted. This can't be undone.",
                                delete_template, (tmpl.id,), "Delete template", f"tmpl_{tmpl.id}",
                            )

                        st.markdown("**Tasks**")
                        ordered_tasks = sorted(tmpl.tasks, key=lambda t: t.sort_order)
                        if not ordered_tasks:
                            st.caption("No tasks yet — add one below.")
                        for i, task in enumerate(ordered_tasks):
                            if st.session_state.get("editing_task_id") == task.id:
                                with st.form(f"edit_task_{task.id}"):
                                    et_title = st.text_input("Task title", value=task.title)
                                    et_type = st.selectbox(
                                        "Type", TASK_TYPES,
                                        index=TASK_TYPES.index(task.task_type) if task.task_type in TASK_TYPES else 0,
                                    )
                                    et_instr = st.text_input("Instructions", value=task.instructions or "")
                                    ec1, ec2, ec3 = st.columns(3)
                                    et_min = ec1.number_input("Min (temps)", value=float(task.min_value or 0))
                                    et_max = ec2.number_input("Max", value=float(task.max_value or 0))
                                    et_unit = ec3.text_input("Unit", value=task.unit_label or "°F")
                                    et_train = st.text_input("Just-in-time training note", value=task.training_note or "")
                                    et_corr = st.text_input("Corrective action (if out of range)", value=task.corrective_action or "")
                                    et_req_photo = st.checkbox("Requires photo", value=task.requires_photo)
                                    et_required = st.checkbox("Required to finish checklist", value=task.is_required)
                                    s1, s2 = st.columns(2)
                                    if s1.form_submit_button("Save", type="primary"):
                                        if not et_title.strip():
                                            st.error("Task title is required.")
                                        else:
                                            update_task(
                                                db, task.id, title=et_title, task_type=et_type,
                                                instructions=et_instr or None, requires_photo=et_req_photo,
                                                is_required=et_required,
                                                min_value=et_min if et_type == "number" else None,
                                                max_value=et_max if et_type == "number" else None,
                                                unit_label=et_unit if et_type == "number" else None,
                                                training_note=et_train or None,
                                                corrective_action=et_corr or None,
                                            )
                                            st.session_state.pop("editing_task_id", None)
                                            st.success("Task updated")
                                            st.rerun()
                                    if s2.form_submit_button("Cancel"):
                                        st.session_state.pop("editing_task_id", None)
                                        st.rerun()
                            else:
                                t1, t2, t3, t4, t5, t6 = st.columns([5, 1, 1, 1, 1, 1])
                                inactive_flag = "" if task.is_active else " · ⚪ inactive"
                                req_flag = "" if task.is_required else " (optional)"
                                t1.write(f"{i + 1}. {task.title} — {task.task_type}{req_flag}{inactive_flag}")
                                if t2.button("▲", key=f"up_task_{task.id}", help="Move up", disabled=(i == 0)):
                                    move_task(db, task.id, "up")
                                    st.rerun()
                                if t3.button("▼", key=f"down_task_{task.id}", help="Move down", disabled=(i == len(ordered_tasks) - 1)):
                                    move_task(db, task.id, "down")
                                    st.rerun()
                                if t4.button("✏️", key=f"edit_task_btn_{task.id}", help="Edit"):
                                    st.session_state["editing_task_id"] = task.id
                                    st.rerun()
                                if task.is_active:
                                    if t5.button("⏸️", key=f"deact_task_{task.id}", help="Deactivate"):
                                        set_task_active(db, task.id, False)
                                        st.rerun()
                                else:
                                    if t5.button("▶️", key=f"react_task_{task.id}", help="Reactivate"):
                                        set_task_active(db, task.id, True)
                                        st.rerun()
                                if t6.button("🗑️", key=f"del_task_{task.id}", help="Delete"):
                                    confirm_action(
                                        "Delete this task?",
                                        f"'{task.title}' will be permanently deleted. This can't be undone.",
                                        delete_task, (task.id,), "Delete task", f"task_{task.id}",
                                    )

                        with st.form(f"add_task_{tmpl.id}"):
                            st.caption("Add a task to this template")
                            nt_title = st.text_input("Task title", key=f"nt_title_{tmpl.id}")
                            nt_type = st.selectbox("Type", TASK_TYPES, key=f"nt_type_{tmpl.id}")
                            nt_instr = st.text_input("Instructions", key=f"nt_instr_{tmpl.id}")
                            nc1, nc2, nc3 = st.columns(3)
                            nt_min = nc1.number_input("Min (temps)", value=0.0, key=f"nt_min_{tmpl.id}")
                            nt_max = nc2.number_input("Max", value=0.0, key=f"nt_max_{tmpl.id}")
                            nt_unit = nc3.text_input("Unit", value="°F", key=f"nt_unit_{tmpl.id}")
                            nt_train = st.text_input("Just-in-time training note", key=f"nt_train_{tmpl.id}")
                            nt_corr = st.text_input("Corrective action (if out of range)", key=f"nt_corr_{tmpl.id}")
                            nt_req_photo = st.checkbox("Requires photo", value=(nt_type == "photo"), key=f"nt_photo_{tmpl.id}")
                            nt_required = st.checkbox("Required to finish checklist", value=True, key=f"nt_req_{tmpl.id}")
                            if st.form_submit_button("Add task"):
                                if not nt_title.strip():
                                    st.error("Task title is required.")
                                else:
                                    add_task_to_template(
                                        db, tmpl.id, nt_title, task_type=nt_type, instructions=nt_instr or None,
                                        requires_photo=nt_req_photo, is_required=nt_required,
                                        min_value=nt_min if nt_type == "number" else None,
                                        max_value=nt_max if nt_type == "number" else None,
                                        unit_label=nt_unit if nt_type == "number" else None,
                                        training_note=nt_train or None,
                                        corrective_action=nt_corr or None,
                                    )
                                    st.success("Task added")
                                    st.rerun()

            with st.expander("➕ Create checklist template"):
                with st.form("new_tmpl"):
                    name = st.text_input("Template name")
                    desc = st.text_input("Description")
                    ltype = st.selectbox("List type", LIST_TYPES)
                    sop_options = {"— None —": None}
                    sop_options.update({s.title: s.id for s in admin_sops})
                    new_sop_label = st.selectbox("Linked SOP (optional)", list(sop_options.keys()))
                    if st.form_submit_button("Create template"):
                        if not name.strip():
                            st.error("Template name is required.")
                        else:
                            create_template(
                                db, name, description=desc, location_id=location.id, list_type=ltype,
                                sop_id=sop_options[new_sop_label],
                            )
                            st.success("Template created")
                            st.rerun()

# ---------- MANAGER LOGBOOK ----------
elif page == "Manager Logbook":
    st.title("Manager Logbook")
    st.caption("A running shift journal — the digital replacement for a paper log book at the host stand. Distinct from Checklists & SOPs: this is free-form notes, not a recurring task list.")

    if not require_perm(user, "logbook"):
        st.error("You do not have access to the logbook.")
        st.stop()

    st.subheader("New entry")
    with st.form("new_log_entry"):
        lc1, lc2 = st.columns(2)
        log_category = lc1.selectbox("Category", LOG_CATEGORIES)
        log_date = lc2.date_input("Date", value=date.today())
        log_message = st.text_area("What happened?", placeholder="e.g. Walk-in compressor was making a rattling noise around 3pm — called the repair vendor, appointment set for tomorrow AM.")
        log_pinned = st.checkbox("Pin to top (important)", value=False)
        if st.form_submit_button("Post entry", type="primary"):
            try:
                create_log_entry(
                    db, location.id, user.id, log_message,
                    category=log_category, entry_date=log_date, pinned=log_pinned,
                )
                st.success("Entry posted")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    st.divider()
    st.subheader("Feed")
    f1, f2, f3 = st.columns(3)
    feed_start = f1.date_input("From", value=date.today() - timedelta(days=7), key="log_feed_start")
    feed_end = f2.date_input("To", value=date.today(), key="log_feed_end")
    feed_category = f3.selectbox("Filter category", ["All"] + LOG_CATEGORIES, key="log_feed_cat")

    entries = list_log_entries(
        db, location.id, feed_start, feed_end,
        category=None if feed_category == "All" else feed_category,
    )
    if not entries:
        st.info("No logbook entries in this range.")
    else:
        can_manage = require_perm(user, "users_admin") or require_perm(user, "financials_admin")
        for e in entries:
            with st.container(border=True):
                head1, head2 = st.columns([5, 1])
                pin_flag = "📌 " if e.pinned else ""
                head1.markdown(
                    f"{pin_flag}**{e.category}** · {e.entry_date.strftime('%a %b %d')} · "
                    f"{e.author.name if e.author else 'Unknown'}"
                )
                can_edit_entry = can_manage or (e.author_id == user.id)
                if can_edit_entry:
                    with head2:
                        bcol1, bcol2 = st.columns(2)
                        if bcol1.button("📌", key=f"pin_{e.id}", help="Pin/unpin"):
                            toggle_log_pin(db, e.id)
                            st.rerun()
                        if bcol2.button("🗑️", key=f"del_log_{e.id}", help="Delete"):
                            confirm_action(
                                "Delete this logbook entry?",
                                f"The {e.category} note from {e.entry_date.strftime('%a %b %d')} will be permanently deleted.",
                                delete_log_entry, (e.id,), "Delete entry", f"log_{e.id}",
                            )
                st.write(e.message)

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
                    confirm_action(
                        "Delete this shift?",
                        f"{s.start_at.strftime('%a %b %d, %H:%M')}–{s.end_at.strftime('%H:%M')} · {who} will be permanently deleted.",
                        delete_shift, (s.id,), "Delete shift", f"shift_{s.id}",
                    )

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
            loc_names_by_id = {l.id: l.name for l in locs}
            rows = []
            for loc in locs:
                rows.append({
                    "ID": loc.id,
                    "Name": loc.name,
                    "Code": loc.code,
                    "Parent": loc_names_by_id.get(loc.parent_id, "—") if loc.parent_id else "—",
                    "City": loc.city or "",
                    "State": loc.state or "",
                    "Timezone": loc.timezone or "",
                    "Closeout hour": loc.closeout_hour if loc.closeout_hour is not None else "",
                    "Active": loc.is_active,
                    "Phone": loc.phone or "",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            st.caption("Locations with a Parent roll up under it on Financials → Consolidated.")

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
            existing_locs = list_locations(db, active_only=False)
            parent_options = {"— No parent (standalone) —": None}
            parent_options.update({f"{l.name} ({l.code})": l.id for l in existing_locs})
            parent_label = st.selectbox(
                "Parent location (optional)", list(parent_options.keys()),
                help="Roll this location up under another for consolidated multi-location reporting.",
            )
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
                            parent_id=parent_options[parent_label],
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
                parent_options = {"— No parent (standalone) —": None}
                parent_options.update({f"{l.name} ({l.code})": l.id for l in locs if l.id != loc.id})
                current_parent_label = next(
                    (k for k, v in parent_options.items() if v == loc.parent_id),
                    "— No parent (standalone) —",
                )
                parent_label = st.selectbox(
                    "Parent location (optional)", list(parent_options.keys()),
                    index=list(parent_options.keys()).index(current_parent_label),
                    help="Roll this location up under another for consolidated multi-location reporting.",
                )
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
                            parent_id=parent_options[parent_label],
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
