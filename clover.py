"""
Clover POS integration — pushes a staged cart into Clover as a custom order.

Receipt format matches real Rotelli kitchen receipt:
  - Customer info block printed as $0.00 header lines at the top
  - Each modifier printed as a separate $0.00 line item directly under its parent
  - Delivery fee, discount, and tax as their own line items at the bottom

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


def _add_line_item(order_id: str, name: str, price_cents: int, qty_units: int = 1000,
                   note: str = None) -> None:
    """Add a single line item to an existing Clover order. Raises RuntimeError on failure."""
    payload = {"price": price_cents, "name": name, "unitQty": qty_units}
    if note:
        payload["note"] = note
    try:
        resp = requests.post(
            _url(f"orders/{order_id}/line_items"),
            json=payload,
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Clover line item failed for '{name}': {e}")


def create_clover_order(cart: dict) -> Optional[str]:
    """
    Push a staged cart to Clover as a custom order.

    Returns the Clover order ID (str) on success.
    Returns None if Clover is not configured (dev/test mode).
    Raises RuntimeError on any Clover API failure.

    Receipt layout (matches real Rotelli kitchen receipt):
      DELIVERY — Customer Name
      ─────────────────────────
      Customer: Name            $0.00
      Phone: 5615551212         $0.00
      Deliver to: 123 Main St   $0.00
      ─────────────────────────
      Meat Lasagna             $21.99
        Dinner Salad            $0.00
        Garlic Rolls            $0.00
      Cheese Calzone           $14.99
      ...
      Delivery Fee              $2.99
      Sales Tax (6.5%)         $12.57
    """
    if not is_configured():
        return None

    # ── Create the order ──────────────────────────────────────────────────────
    addr = cart.get("raw_delivery_address") or cart.get("delivery_address", "")
    low_conf = cart.get("address_confidence") == "low"

    order_payload = {
        "title": f"{cart['order_type'].capitalize()} — {cart['customer_name']}",
        "note":  (
            f"Customer: {cart['customer_name']}\n"
            f"Phone: {cart['phone_number']}\n"
            f"Order type: {cart['order_type'].upper()}"
            + (f"\nDeliver to: {addr}" if addr else "")
            + ("\n⚠ Address unverified — confirm with customer" if addr and low_conf else "")
        ),
        "state": "open",
    }

    try:
        resp = requests.post(_url("orders"), json=order_payload, headers=_headers(), timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Clover order creation failed: {e}")

    oid = resp.json()["id"]

    # ── Customer info header block (prints at top of receipt) ─────────────────
    _add_line_item(oid, f"Customer: {cart['customer_name']}", 0)
    _add_line_item(oid, f"Phone: {cart['phone_number']}", 0)
    if addr:
        label = f"Deliver to: {addr}"
        if low_conf:
            label += " (unverified)"
        _add_line_item(oid, label, 0)
    _add_line_item(oid, "--------------------", 0)

    # ── Food items — each modifier as its own $0.00 line item ─────────────────
    for item in cart.get("items", []):
        name = item["item_name"]
        if item.get("size"):
            name += f" — {item['size']}"

        # Main line item — kitchen notes go in the note field
        _add_line_item(
            oid,
            name,
            price_cents=int(round(item["unit_price"] * 100)),
            qty_units=item["quantity"] * 1000,
            note=item.get("notes") or None,
        )

        # Each modifier as a separate $0.00 line (matches real receipt format)
        for modifier in (item.get("modifiers") or []):
            _add_line_item(oid, modifier, 0)

    # ── Coupon / discount ─────────────────────────────────────────────────────
    discount = cart.get("discount", 0.0)
    if discount and discount > 0:
        desc = cart.get("coupon_description") or "Coupon discount"
        _add_line_item(oid, desc, -int(round(discount * 100)))

    # ── Delivery fee ──────────────────────────────────────────────────────────
    delivery_fee = cart.get("delivery_fee", 0.0)
    if delivery_fee and delivery_fee > 0:
        _add_line_item(oid, "Delivery Fee", int(round(delivery_fee * 100)))

    # ── Sales tax ─────────────────────────────────────────────────────────────
    tax = cart.get("tax", 0.0)
    if tax and tax > 0:
        _add_line_item(oid, "Sales Tax (6.5%)", int(round(tax * 100)))

    return oid
