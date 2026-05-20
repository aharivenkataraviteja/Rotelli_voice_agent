"""
Clover POS integration — pushes a staged cart into Clover as a custom order.

PHASE 1 (now):  custom orders — ad-hoc line items, proves connectivity fast.
PHASE 2 (later): atomic orders — inventory-backed items, better device visibility.

Config (set in .env):
  CLOVER_API_BASE     sandbox: https://sandbox.dev.clover.com
                      production: https://api.clover.com
  CLOVER_MERCHANT_ID  your Clover merchant ID
  CLOVER_API_TOKEN    your Clover REST API token

If CLOVER_MERCHANT_ID or CLOVER_API_TOKEN is blank, Clover push is skipped
and create_clover_order() returns None.  The order is still confirmed locally.
"""

import os
from typing import Optional

import requests

CLOVER_API_BASE    = os.environ.get("CLOVER_API_BASE",    "https://sandbox.dev.clover.com")
CLOVER_MERCHANT_ID = os.environ.get("CLOVER_MERCHANT_ID", "")
CLOVER_API_TOKEN   = os.environ.get("CLOVER_API_TOKEN",   "")


def is_configured() -> bool:
    return bool(CLOVER_MERCHANT_ID and CLOVER_API_TOKEN)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {CLOVER_API_TOKEN}",
        "Content-Type":  "application/json",
    }


def _url(path: str) -> str:
    return f"{CLOVER_API_BASE}/v3/merchants/{CLOVER_MERCHANT_ID}/{path.lstrip('/')}"


def create_clover_order(cart: dict) -> Optional[str]:
    """
    Push a staged cart to Clover as a custom order.

    Returns the Clover order ID (str) on success.
    Returns None if Clover is not configured (dev/test mode).
    Raises RuntimeError on any Clover API failure — caller must not confirm the
    cart until this succeeds.

    Line items:
      - One line item per cart item (uses unitQty for multi-unit quantities).
      - Modifiers and kitchen notes are packed into the line item note field.
      - Delivery fee (if any) is added as a separate "Delivery Fee" line item.

    Order note contains customer context staff need:
      Customer name, phone number, order type, delivery address.
    """
    if not is_configured():
        return None  # Clover not configured — skip, confirm locally only

    # ── Build order note with all customer context staff need ─────────────────
    note_lines = [
        f"Customer: {cart['customer_name']}",
        f"Phone: {cart['phone_number']}",
        f"Order type: {cart['order_type'].upper()}",
    ]
    if cart.get("delivery_address"):
        # Use the raw spoken address for the driver (exactly what the caller said)
        addr = cart.get("raw_delivery_address") or cart["delivery_address"]
        note_lines.append(f"Deliver to: {addr}")
        if cart.get("address_confidence") == "low":
            note_lines.append("⚠ Address not fully verified — confirm with customer")

    order_payload = {
        "title": f"{cart['order_type'].capitalize()} — {cart['customer_name']}",
        "note":  "\n".join(note_lines),
        "state": "open",
    }

    try:
        resp = requests.post(
            _url("orders"),
            json=order_payload,
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Clover order creation failed: {e}")

    clover_order_id = resp.json()["id"]

    # ── Add each cart item as a line item ─────────────────────────────────────
    for item in cart.get("items", []):
        # Build display name — include size when present
        name = item["item_name"]
        if item.get("size"):
            name += f" — {item['size']}"

        # Combine modifiers + notes into one note string for kitchen/staff
        note_parts = list(item.get("modifiers") or [])
        if item.get("notes"):
            note_parts.append(item["notes"])

        line_payload = {
            "price":   int(round(item["unit_price"] * 100)),  # Clover uses cents
            "name":    name,
            "unitQty": item["quantity"] * 1000,               # Clover milli-units (1000 = 1)
        }
        if note_parts:
            line_payload["note"] = ", ".join(note_parts)

        try:
            resp = requests.post(
                _url(f"orders/{clover_order_id}/line_items"),
                json=line_payload,
                headers=_headers(),
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"Clover line item add failed for '{name}': {e}")

    # ── Add discount line item (negative price) ───────────────────────────────
    discount = cart.get("discount", 0.0)
    if discount and discount > 0:
        try:
            resp = requests.post(
                _url(f"orders/{clover_order_id}/line_items"),
                json={
                    "price":   -int(round(discount * 100)),   # negative = discount
                    "name":    "Discount (10% off $40+)",
                    "unitQty": 1000,
                },
                headers=_headers(),
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"Clover discount line item failed: {e}")

    # ── Add delivery fee as a separate line item (delivery orders only) ───────
    delivery_fee = cart.get("delivery_fee", 0.0)
    if delivery_fee and delivery_fee > 0:
        try:
            resp = requests.post(
                _url(f"orders/{clover_order_id}/line_items"),
                json={
                    "price":   int(round(delivery_fee * 100)),
                    "name":    "Delivery Fee",
                    "unitQty": 1000,
                },
                headers=_headers(),
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"Clover delivery fee line item failed: {e}")

    # ── Add tax line item ─────────────────────────────────────────────────────
    tax = cart.get("tax", 0.0)
    if tax and tax > 0:
        try:
            resp = requests.post(
                _url(f"orders/{clover_order_id}/line_items"),
                json={
                    "price":   int(round(tax * 100)),
                    "name":    "Sales Tax (6.5%)",
                    "unitQty": 1000,
                },
                headers=_headers(),
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"Clover tax line item failed: {e}")

    return clover_order_id
