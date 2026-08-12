"""
AI-assisted invoice capture: send a photo or PDF of a vendor invoice to
Claude and get back structured fields (vendor, invoice #, date, total, and
line items with SKU / ordered qty / shipped qty / unit / price).

This module is intentionally decoupled from Streamlit — it just needs an
API key and file bytes — so it stays reusable from a script, the FastAPI
layer, or the iOS app's own backend calls later.

Nothing here writes to the database. The Streamlit page is expected to show
the extracted fields to a human for review/correction before anything is
saved via app.services.invoices.create_invoice_from_extraction().
"""

from __future__ import annotations
import base64
import json
import re
from dataclasses import dataclass, field
from typing import Optional

EXTRACTION_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You are an accounts-payable data entry assistant for a restaurant. You will be \
shown a photo or scanned/digital PDF of a single vendor invoice, packing slip, or statement. It \
may be a food distributor, a meat/produce purveyor, or a service vendor (linen, waste, repairs, \
etc.) — read it carefully and extract the fields below. The document may span multiple pages; \
read all of them (totals and summaries are often on the last page, not the first).

General rules:
- Respond with ONLY a single JSON object. No markdown code fences, no commentary before or after.
- Use null for any field you cannot find or cannot read confidently. Never guess or invent a \
number — it is much better to leave something null than to make up a plausible-looking value.
- Dates must be formatted "YYYY-MM-DD". If you can only tell the date from context (e.g. only \
month/day printed, with the year implied by other dates on the page), use your best judgement. \
Due dates are sometimes labeled "Due Date", sometimes "Payment Due" / "Please Remit By", and \
sometimes only given as terms (e.g. "NET 14 DAYS") — if so, calculate it from the invoice date.
- Numbers must be plain numbers (no "$", no thousands separators, no "CR"/negative-sign words — \
use a negative number for credits/discounts instead).
- "invoice_total" is the amount owed for THIS SPECIFIC invoice only. Statements sometimes also \
show an account-wide running balance or aging summary (e.g. "TOTAL DUE", "CURRENT", "1-30 DAYS" \
buckets near the top) — that is NOT invoice_total. Use the figure tied to this invoice's own \
line items and charges, usually near a label like "Invoice Total", "Total", "Amount Due", or \
"Please Remit This Amount By" close to the invoice's own subtotal/tax/freight breakdown.
- Do NOT treat section/category headers (e.g. "DRY", "FROZEN", "REFRIGERATED") or annotation \
labels (e.g. "SUBSTITUTION", "TAX EXEMPT") as line items — they're grouping labels, not products.

Quantity columns — read carefully, this varies a lot by distributor:
- Some invoices print two separate quantity columns per line, often labeled "ORD"/"SHP", \
"ORDERED"/"SHIPPED", or similar — these directly map to quantity_ordered / quantity_shipped and \
are valuable for spotting backorders (shipped < ordered, sometimes shipped = 0 for an out-of-stock \
item). Use them as printed.
- If the invoice only prints ONE quantity column (just "QTY" or "Quantity"), put that same value \
in both quantity_ordered and quantity_shipped.
- Catch-weight items (very common for meat, seafood, and produce): a line may show a case count \
(e.g. "3 ordered / 3 shipped CS") AND a separate total weight figure (e.g. "98.65", often with \
several individual weights listed below the description like "32.90  33.20  32.55" that sum to \
it) with a per-pound unit price. In that situation the case count is NOT what the price multiplies \
against — the weight is. When you see this pattern, set quantity_shipped to the WEIGHT figure (so \
that unit_price × quantity_shipped ≈ line_total holds true) and set "unit" to the weight unit \
(e.g. "lb"), not the case unit. Getting line_total right matters more than preserving the case \
count. If a case shows 0 shipped (nothing delivered), quantity_shipped is 0 regardless. When \
possible, report quantity_ordered on the same scale as quantity_shipped (e.g. both in pounds) so \
the two are directly comparable; if an ordered weight truly isn't printed anywhere, the ordered \
case count is an acceptable fallback.
- For flat fee/surcharge lines with a dollar amount but no printed quantity or rate (e.g. "Fuel \
Surcharge $9.00", "Service Charge $1.12", "Sales Tax $2.80", "Delivery Fee"), still include them \
as their own line item: quantity_ordered = quantity_shipped = 1, unit_price = line_total = that \
dollar amount, item_name = the fee's printed label, sku = null. Including tax/freight/fees as \
their own lines is important — it's what lets the line items actually add up to invoice_total.
- Discounts/credits/promotional savings (e.g. "YOUR FLYER SAVINGS ($20.00)") should be their own \
line item with a NEGATIVE unit_price and line_total.

Vendor name: use the company that is asking to be paid (usually in a "Remit To" / "Payable To" \
box, or the letterhead/logo), not the restaurant itself (which appears as "Bill To"/"Sold To"/ \
"Ship To" and should never be reported as the vendor).

Include every line item you can find, even ones you're not fully confident about — flag \
uncertainty in "notes" instead of omitting the line.

Return exactly this JSON shape:
{
  "vendor_name": string or null,
  "invoice_number": string or null,
  "invoice_date": "YYYY-MM-DD" or null,
  "due_date": "YYYY-MM-DD" or null,
  "invoice_total": number or null,
  "line_items": [
    {
      "item_name": string,
      "sku": string or null,
      "quantity_ordered": number or null,
      "quantity_shipped": number or null,
      "unit": string or null,
      "unit_price": number or null,
      "line_total": number or null
    }
  ],
  "notes": string or null
}"""

IMAGE_MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


class InvoiceExtractionError(Exception):
    pass


@dataclass
class InvoiceExtractionResult:
    data: dict
    raw_text: str
    warnings: list = field(default_factory=list)


def _media_type_for(filename: str) -> tuple[str, str]:
    """Return (kind, media_type) where kind is 'image' or 'document'."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        return "document", "application/pdf"
    if ext in IMAGE_MEDIA_TYPES:
        return "image", IMAGE_MEDIA_TYPES[ext]
    raise InvoiceExtractionError(
        f"Unsupported file type '.{ext}'. Upload a JPG, PNG, WEBP photo or a PDF."
    )


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def extract_invoice(file_bytes: bytes, filename: str, api_key: str) -> InvoiceExtractionResult:
    """
    Call Claude to read an invoice image/PDF and return structured fields.
    Raises InvoiceExtractionError on any failure (missing key, API error,
    unparseable response) with a message safe to show directly in the UI.
    """
    if not api_key:
        raise InvoiceExtractionError(
            "No Anthropic API key configured. Add ANTHROPIC_API_KEY under your Streamlit "
            "app's Settings → Secrets, then reload."
        )
    if len(file_bytes) > 10 * 1024 * 1024:
        raise InvoiceExtractionError("File is larger than 10 MB — try a smaller photo or a compressed PDF.")

    kind, media_type = _media_type_for(filename)

    try:
        import anthropic
    except ImportError as e:
        raise InvoiceExtractionError(
            "The 'anthropic' package isn't installed. Add `anthropic` to requirements.txt."
        ) from e

    encoded = base64.standard_b64encode(file_bytes).decode("utf-8")
    content_block = {
        "type": kind,
        "source": {"type": "base64", "media_type": media_type, "data": encoded},
    }
    if kind == "document":
        content_block["citations"] = {"enabled": False}

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=EXTRACTION_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    content_block,
                    {"type": "text", "text": "Extract this invoice's fields as the specified JSON object."},
                ],
            }],
        )
    except Exception as e:  # noqa: BLE001 - surface any SDK/API error to the UI
        raise InvoiceExtractionError(f"Claude API request failed: {e}") from e

    raw_text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
    if not raw_text.strip():
        raise InvoiceExtractionError("The model returned an empty response. Try a clearer photo/scan.")

    cleaned = _strip_code_fences(raw_text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise InvoiceExtractionError(
            f"Couldn't parse the model's response as JSON ({e}). Raw response:\n\n{raw_text[:1500]}"
        ) from e

    warnings = []
    if not parsed.get("vendor_name"):
        warnings.append("Vendor name wasn't detected — you'll need to select or create it manually.")
    if not parsed.get("line_items"):
        warnings.append("No line items were detected — check the image quality or add lines manually.")
    if parsed.get("notes"):
        warnings.append(f"Model note: {parsed['notes']}")

    return InvoiceExtractionResult(data=parsed, raw_text=raw_text, warnings=warnings)
