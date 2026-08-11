# Craftable Replica – Hospitality Back-Office Platform

Working implementation of the core **Craftable** model for restaurants.

## Implemented Workflows

| Workflow | Status |
|----------|--------|
| Live Recipe Costing (sub-recipes) | ✅ |
| Theoretical Inventory | ✅ |
| POS Sales → Automatic Depletions | ✅ |
| **Toast POS Sales Import** | ✅ |
| **Purchasing & Receiving** | ✅ **New** |
| Physical Inventory Counts | ✅ |
| Full Variance Report | ✅ |
| Par-Level Order Suggestions | ✅ |
| **Invoices (AP) + 3-Way Match** | ✅ |
| **AI Invoice Capture (photo/PDF)** | ✅ **New** |
| **Inventory Item Management (add/edit)** | ✅ **New** |
| **Recipe Management (add/edit ingredients)** | ✅ **New** |
| **Bulk entry (Counts, PO lines)** | ✅ **New** |

## Purchasing Workflow Details

```
1. Create Draft PO
   - Manually add lines, or
   - One-click from "items below par" suggestions

2. Add / edit lines (item, qty, unit, unit cost)

3. Submit PO  → status becomes "submitted"

4. Receive Goods (partial or full)
   - Enter quantities received per line
   - Theoretical inventory is increased
   - Item current_cost is updated from the PO
   - PO status → partially_received or received

5. Cancel (if still open)
```

Supporting features:
- Vendor management
- PO status tracking (draft → submitted → partially_received → received)
- Automatic cost updates on receiving
- Integration with variance reporting (purchases feed the theoretical calculation)

## Invoices (AP) & 3-Way Match

Service: `app/services/invoices.py` · Models: `app/models/invoice.py`

- Capture a vendor invoice with header info and line items, optionally linked to a Purchase Order
- **Auto-match** each invoice line to its PO line (by explicit link or by item) and compare:
  - **Quantity**: invoiced qty vs. what was actually *received* on the PO (not just ordered)
  - **Price**: invoiced unit price vs. PO unit cost (flags if drift > 2%)
- Line results: `matched`, `qty_variance`, `price_variance`, or `unmatched` (nothing received yet / no PO link)
- Invoice header rolls up to `received` → `matched` or `exception` → `approved` → `paid` (or `rejected`)
- Non-PO invoices (utilities, etc.) skip matching and can be approved directly
- UI: **Invoices (AP)** page — capture, match, approve/pay/reject, plus an AP summary (open payable, exception count)
- Demo data: `python -m scripts.seed` creates one invoice with an intentional price + qty variance (exception) and one clean non-PO invoice

## Quick Start

```bash
cd craftable-replica
pip install -r requirements.txt
python -m scripts.seed
streamlit run frontend/app.py
```

## Dashboard Pages
- Dashboard
- Inventory & Stock
- **Purchasing** (Create PO, Receive, Vendors)
- Physical Counts
- Variance Report
- Recipes & Costing
- Simulate Sales (POS)
- Waste & Adjustments
- Order Suggestions (Par)
- About the Model

## Project Structure
```
app/
  models/           # All entities
  services/
    costing.py      # Live plate costing
    inventory.py    # Theoretical stock + depletions
    purchasing.py   # PO lifecycle + receiving
    invoices.py     # AP capture + 3-way match
    counts.py       # Physical count lifecycle
    variance.py     # Theoretical vs Actual report
frontend/app.py     # Full management UI
scripts/seed.py     # Demo restaurant with history
```

## Toast POS Import

Service: `app/services/toast_import.py`

- Maps Toast menu item names → Craftable recipes
- `import_item_rows()` for flat/CSV-style data
- `import_orders_json()` for Toast Orders API / webhook payloads
- Auto-creates POSSale records and depletes theoretical inventory
- UI: **Toast POS Import** page in the Streamlit dashboard

Live connection requires Toast developer credentials (Orders API or nightly export).


## Checklists & SOPs

- Streamlit: **Checklists & SOPs** page
- Models: SOP, templates, runs, photo completions
- Photos: `/tmp/craftable_checklist_photos/checklists/{runId}/`
- iOS: live camera capture for photo tasks


## Checklist API (iOS → Python)

Start API:
```bash
cd craftable-replica
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/checklists/templates | List templates + tasks |
| POST | /api/checklists/runs | Start run |
| POST | /api/checklists/runs/{id}/complete | Checkbox / text / number |
| POST | /api/checklists/runs/{id}/complete-photo | Multipart live photo |
| POST | /api/checklists/runs/{id}/finish | Complete run |
| GET | /media/checklists/... | Served photo files |

iOS: `ChecklistAPIClient` + `ChecklistService` push after local save when `serverRunId` / `serverTaskId` are linked (auto by template name match).

On device, set UserDefaults `craftable_api_base` to `http://<mac-lan-ip>:8000`.


## Jolt-style checklist features

- Photo proof (anti pencil-whip)
- Temperature / numeric ranges with out-of-range flags
- Required corrective action text when out of range
- Employee name + timestamp accountability
- Due times + overdue status
- Manager dashboard: open, overdue, avg completion %, temp exceptions
- List types: opening, closing, food_safety, walkthrough, cleaning
- Info Library SOPs with just-in-time training notes on tasks
- Score tasks (1–5) for manager walkthroughs

Re-seed to get Food Safety Line Check + Manager Walkthrough templates:
`python -m scripts.seed` (only seeds checklists if none exist — clear checklist tables or reset DB to refresh).

