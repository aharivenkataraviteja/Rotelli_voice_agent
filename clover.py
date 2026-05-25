"""
Clover POS integration — pushes a staged cart into Clover as a custom order.

Receipt format matches real Rotelli Clover receipts:
  - Items listed with quantity prefix
  - Each modifier as a $0.00 line item directly under its parent item
  - Customer Info block (Name / Address / Phone) prints at the BOTTOM,
    generated automatically by Clover when a customer profile is attached
  - Delivery fee and tax as their own line items

Config (set in .env):
  CLOVER_API_BASE     sandbox: https://sandbox.dev.clover.com
                      production: https://api.clover.com
  CLOVER_MERCHANT_ID  your Clover merchant ID
  CLOVER_API_TOKEN    your Clover REST API token
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


def _add_line_item(order_id: str, name: str, price_cents: int,
                   qty_units: int = 1000, note: str = None) -> None:
    """Add a single line item to a Clover order. Raises RuntimeError on failure."""
    payload = {"price": price_cents, "name": name, "unitQty": qty_units}
    if note:
        payload["note"] = note
    try:
        resp = requests.post(
            _url(f"orders/{order_id}/line_items"),
            json=payload, headers=_headers(), timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Clover line item failed for '{name}': {e}")


def _get_or_create_customer(phone: str, name: str, address: str) -> Optional[str]:
    """
    Find an existing Clover customer by phone number, or create a new one.
    Returns the Clover customer ID, or None on any failure (non-fatal).

    Attaching this ID to the order makes Clover print the
    'Customer Info / Name / Address / Phone' block at the bottom of the receipt.
    """
    parts     = name.strip().split(None, 1)
    first     = parts[0] if parts else name
    last      = parts[1] if len(parts) > 1 else ""

    # ── Search for existing customer by phone ────────────────────────────────
    try:
        resp = requests.get(
            _url("customers"),
            params={"filter": f"phoneNumbers.phoneNumber={phone}"},
            headers=_headers(), timeout=10,
        )
        if resp.status_code == 200:
            elements = resp.json().get("elements", [])
            if elements:
                cid = elements[0]["id"]
                # Update name (best effort)
                requests.post(
                    _url(f"customers/{cid}"),
                    json={"firstName": first, "lastName": last},
                    headers=_headers(), timeout=10,
                )
                # Update/add address (best effort)
                if address:
                    existing_addrs = (
                        elements[0].get("addresses", {}).get("elements", [])
                    )
                    if existing_addrs:
                        aid = existing_addrs[0]["id"]
                        requests.post(
                            _url(f"customers/{cid}/addresses/{aid}"),
                            json={"address1": address},
                            headers=_headers(), timeout=10,
                        )
                    else:
                        requests.post(
                            _url(f"customers/{cid}/addresses"),
                            json={"address1": address},
                            headers=_headers(), timeout=10,
                        )
                return cid
    except Exception:
        pass

    # ── Create new customer ──────────────────────────────────────────────────
    try:
        resp = requests.post(
            _url("customers"),
            json={"firstName": first, "lastName": last},
            headers=_headers(), timeout=10,
        )
        resp.raise_for_status()
        cid = resp.json()["id"]

        # Add phone number
        try:
            requests.post(
                _url(f"customers/{cid}/phoneNumbers"),
                json={"phoneNumber": phone},
                headers=_headers(), timeout=10,
            )
        except Exception:
            pass

        # Add delivery address
        if address:
            try:
                requests.post(
                    _url(f"customers/{cid}/addresses"),
                    json={"address1": address},
                    headers=_headers(), timeout=10,
                )
            except Exception:
                pass

        return cid

    except Exception:
        return None  # Customer creation failed — order still goes through


def _attach_customer(order_id: str, customer_id: str) -> None:
    """Link customer profile to order so Clover prints the Customer Info section."""
    try:
        requests.post(
            _url(f"orders/{order_id}/customers/{customer_id}"),
            headers=_headers(), timeout=10,
        ).raise_for_status()
    except Exception:
        pass  # Non-fatal — order still confirmed without it


def create_clover_order(cart: dict) -> Optional[str]:
    """
    Push a staged cart to Clover as a custom order.

    Returns the Clover order ID (str) on success.
    Returns None if Clover is not configured (dev/test mode).
    Raises RuntimeError on any Clover API failure.

    Receipt layout (matches real Rotelli Clover receipts):
      ROTELLI PIZZA & PASTA
      ...
      Delivery
      Cashier: ...
      Transaction XXXXXX
      ─────────────────────────────────────────
      1  Delivery Fee              $2.99
      1  Meat Lasagna             $21.99
               Dinner Salad        $0.00
               Garlic Rolls        $0.00
               ranch dressing      $0.00
      1  Cheese Calzone           $14.99
      ...
      Subtotal                   $XXX.XX
      Sales Tax   6.5%            $XX.XX
      Total                      $XXX.XX
      ─────────────────────────────────────────
      Customer Info
      Name:
      Robin Anu

      Address:
      399 Piedmont

      Phone:
      5619709487
    """
    if not is_configured():
        return None

    addr     = cart.get("raw_delivery_address") or cart.get("delivery_address", "")
    low_conf = cart.get("address_confidence") == "low"

    # ── Create the order ──────────────────────────────────────────────────────
    note = (
        f"Order type: {cart['order_type'].upper()}"
        + (f"\nDeliver to: {addr}" if addr else "")
        + ("\n⚠ Address unverified — confirm with customer" if addr and low_conf else "")
    )
    try:
        resp = requests.post(
            _url("orders"),
            json={
                "title": f"{cart['order_type'].capitalize()} — {cart['customer_name']}",
                "note":  note,
                "state": "open",
            },
            headers=_headers(), timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Clover order creation failed: {e}")

    oid = resp.json()["id"]

    # ── Attach customer profile (prints Customer Info block at receipt bottom) ─
    cid = _get_or_create_customer(
        phone=cart["phone_number"],
        name=cart["customer_name"],
        address=addr,
    )
    if cid:
        _attach_customer(oid, cid)

    # ── Delivery fee first (matches real receipt order) ───────────────────────
    delivery_fee = cart.get("delivery_fee", 0.0)
    if delivery_fee and delivery_fee > 0:
        _add_line_item(oid, "Delivery Fee", int(round(delivery_fee * 100)))

    # ── Food items — each modifier as its own $0.00 line item ─────────────────
    for item in cart.get("items", []):
        name = item["item_name"]
        if item.get("size"):
            name += f" — {item['size']}"

        _add_line_item(
            oid,
            name,
            price_cents=int(round(item["unit_price"] * 100)),
            qty_units=item["quantity"] * 1000,
            note=item.get("notes") or None,   # kitchen special instructions only
        )

        # Each modifier as a $0.00 sub-line (e.g. "Dinner Salad $0.00")
        for modifier in (item.get("modifiers") or []):
            _add_line_item(oid, modifier, 0)

    # ── Coupon / discount ─────────────────────────────────────────────────────
    discount = cart.get("discount", 0.0)
    if discount and discount > 0:
        desc = cart.get("coupon_description") or "Coupon discount"
        _add_line_item(oid, desc, -int(round(discount * 100)))

    # ── Sales tax ─────────────────────────────────────────────────────────────
    tax = cart.get("tax", 0.0)
    if tax and tax > 0:
        _add_line_item(oid, "Sales Tax (6.5%)", int(round(tax * 100)))

    return oid
