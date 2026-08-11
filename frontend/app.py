"""
Craftable Replica – Interactive Management Dashboard
Includes: Inventory, Counts, Variance, Recipes, Purchasing workflow, POS simulation.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st
import pandas as pd
from datetime import datetime, date, timezone, timedelta

from app.database import SessionLocal, engine, Base
from app.models import (
    Location, InventoryItem, StockLevel, Recipe, Vendor,
    InventoryCount, PurchaseOrder, PurchaseOrderLine, User, Role,
)
from app.services.costing import calculate_recipe_cost
from app.services.inventory import get_or_create_stock, record_pos_sale, log_waste
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
    ap_aging_summary,
)
from app.services.checklists import (
    list_sops, list_templates, start_checklist_run, complete_task, finish_run,
    list_open_runs, get_run_progress, create_sop, create_template, add_task_to_template,
    location_checklist_report,
)
from app.services.locations import list_locations, create_location, update_location, set_location_active, get_location
from app.services.auth import (
    authenticate_pin, create_user, list_users, seed_roles, deactivate_user, user_can,
    get_role_permissions_map, set_role_permission, reset_role_permissions, permissions_for_user
)

st.set_page_config(
    page_title="Craftable Replica",
    page_icon="🍽️",
    layout="wide",
    initial_sidebar_state="expanded",
)

Base.metadata.create_all(bind=engine)

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
    "About the Model": "dashboard",
}

def current_user(db):
    uid = st.session_state.get("user_id")
    if not uid:
        return None
    return db.get(User, uid)

def require_perm(user, key: str) -> bool:
    return user_can(user, key, db)

# ---------- Login gate ----------
db = get_db()
seed_roles(db)

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

# ---------- INVENTORY ----------
elif page == "Inventory & Stock":
    st.title("Inventory & Theoretical Stock")
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
        vendors = {v.name: v.id for v in db.query(Vendor).filter(Vendor.is_active == True).all()}
        items = {f"{i.name} ({i.base_unit})": i for i in db.query(InventoryItem).filter(InventoryItem.is_active == True).all()}

        with st.form("manual_po"):
            vendor_name = st.selectbox("Vendor", list(vendors.keys()))
            notes = st.text_input("Notes")
            st.markdown("**Add lines** (you can add more after creation)")
            item_label = st.selectbox("Item", list(items.keys()))
            qty = st.number_input("Quantity", min_value=0.1, value=10.0, step=0.5)
            unit = st.text_input("Unit", value=items[item_label].base_unit)
            cost = st.number_input("Unit Cost $", min_value=0.0, value=float(items[item_label].current_cost or 0), step=0.01, format="%.3f")

            if st.form_submit_button("Create PO with this line"):
                po = create_purchase_order(db, vendors[vendor_name], location.id, notes=notes or None)
                item = items[item_label]
                add_po_line(db, po.id, item.id, qty, unit, cost)
                db.commit()
                st.success(f"Created {po.po_number}")
                st.rerun()

        # Add lines to existing draft
        st.divider()
        drafts = list_purchase_orders(db, location.id, status="draft")
        if drafts:
            st.subheader("Add line to existing Draft PO")
            draft_options = {f"{p.po_number} ({p.vendor.name})": p.id for p in drafts}
            with st.form("add_line_form"):
                sel_po = st.selectbox("Draft PO", list(draft_options.keys()))
                item_label2 = st.selectbox("Item", list(items.keys()), key="add_item")
                qty2 = st.number_input("Quantity", min_value=0.1, value=5.0, key="add_qty")
                unit2 = st.text_input("Unit", value=items[item_label2].base_unit, key="add_unit")
                cost2 = st.number_input("Unit Cost $", min_value=0.0, value=float(items[item_label2].current_cost or 0), format="%.3f", key="add_cost")
                if st.form_submit_button("Add Line"):
                    add_po_line(db, draft_options[sel_po], items[item_label2].id, qty2, unit2, cost2)
                    db.commit()
                    st.success("Line added")
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
                    items = {i.name: i.id for i in db.query(InventoryItem).filter(InventoryItem.is_active == True).all()}
                    if require_perm(user, "counts_enter"):
                        with st.form(f"entry_{c.id}"):
                            col_a, col_b = st.columns([3, 1])
                            sel = col_a.selectbox("Item", list(items.keys()), key=f"sel_{c.id}")
                            qty = col_b.number_input("Qty", min_value=0.0, step=0.1, key=f"qty_{c.id}")
                            if st.form_submit_button("Add / Update Line"):
                                add_or_update_count_line(db, c.id, items[sel], qty)
                                db.commit()
                                st.success("Updated")
                                st.rerun()
                    else:
                        st.caption("You can view this count but cannot enter quantities.")
                    if summary.get("lines"):
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

    with tab2:
        with st.form("new_user"):
            name = st.text_input("Full name")
            pin = st.text_input("PIN (4–6 digits recommended)", max_chars=8)
            email = st.text_input("Email (optional)")
            role_code = st.selectbox("Role", ["owner", "manager", "kitchen", "server"])
            if st.form_submit_button("Create user", type="primary"):
                if not name.strip() or not pin.strip():
                    st.error("Name and PIN are required")
                else:
                    try:
                        create_user(db, name, role_code, pin=pin, email=email or None)
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
