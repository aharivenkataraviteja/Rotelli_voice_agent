import json
import os
from datetime import datetime, timedelta
from typing import List, Optional

import pytz

from database import get_conn

# Timezone for all scheduled-order calculations
_TZ = pytz.timezone("America/New_York")

# How many minutes before the scheduled time to fire the order to Clover
PICKUP_PREP_BUFFER_MIN   = 30
DELIVERY_PREP_BUFFER_MIN = 60

# Business rules — configurable via .env
DELIVERY_MINIMUM = float(os.environ.get("DELIVERY_MINIMUM", "20.00"))
DELIVERY_FEE     = float(os.environ.get("DELIVERY_FEE",     "2.99"))
TAX_RATE         = float(os.environ.get("TAX_RATE",         "0.065"))   # 6.5% Florida sales tax

def _calculate_discount(food_subtotal: float,
                        coupon_type: Optional[str],
                        coupon_value: float) -> float:
    """
    Return the coupon discount amount (>= 0).
    coupon_type: 'percent' → coupon_value is a percentage (e.g. 10 = 10%)
                 'flat'    → coupon_value is a dollar amount (e.g. 5 = $5 off)
                 None      → no coupon, return 0.0
    The discount is capped at the food subtotal so the total never goes negative.
    """
    if not coupon_type or coupon_value <= 0:
        return 0.0
    if coupon_type == "percent":
        discount = round(food_subtotal * (coupon_value / 100.0), 2)
    elif coupon_type == "flat":
        discount = round(float(coupon_value), 2)
    else:
        return 0.0
    return round(min(discount, food_subtotal), 2)  # never exceed the food total


# ---------------------------------------------------------------------------
# Custom exceptions — caught in main.py and turned into clean JSON responses
# ---------------------------------------------------------------------------

class CartNotFoundError(Exception):
    pass


class CartNotActiveError(Exception):
    def __init__(self, status: str):
        self.status = status
        super().__init__(f"Cart is {status} and cannot be modified")


class CartItemNotFoundError(Exception):
    pass


def get_customer_by_phone(phone_number: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE phone_number = ?", (phone_number,)
        ).fetchone()
        return dict(row) if row else None


def get_customer_order_history(phone_number: str, limit: int = 5) -> dict:
    """
    Return the last `limit` confirmed orders for a customer plus smart
    summary fields the agent can use to personalise the call.

    Returns:
      {
        "total_orders": int,
        "past_orders":  [ { date, order_type, items, food_subtotal } ... ],
        "favorite_item": str | None,
        "usual_order_type": "pickup" | "delivery" | None,
      }
    """
    with get_conn() as conn:
        cart_rows = conn.execute(
            """
            SELECT cart_id, order_type, confirmed_at, created_at
            FROM   carts
            WHERE  phone_number = ? AND status = 'confirmed'
            ORDER  BY COALESCE(confirmed_at, created_at) DESC
            LIMIT  ?
            """,
            (phone_number, limit),
        ).fetchall()

        total_orders = conn.execute(
            "SELECT COUNT(*) FROM carts WHERE phone_number = ? AND status = 'confirmed'",
            (phone_number,),
        ).fetchone()[0]

        past_orders = []
        item_counter: dict = {}
        order_type_counter: dict = {"pickup": 0, "delivery": 0}

        for cart in cart_rows:
            cart_id    = cart["cart_id"]
            order_type = cart["order_type"]
            date_str   = (cart["confirmed_at"] or cart["created_at"] or "")[:10]

            order_type_counter[order_type] = order_type_counter.get(order_type, 0) + 1

            item_rows = conn.execute(
                "SELECT item_name, size, quantity, line_total FROM cart_items WHERE cart_id = ?",
                (cart_id,),
            ).fetchall()

            items = []
            food_subtotal = 0.0
            for item in item_rows:
                label = item["item_name"]
                if item["size"]:
                    label = f"{item['size'].title()} {label}"
                items.append({
                    "name":     label,
                    "quantity": item["quantity"],
                })
                food_subtotal += item["line_total"]
                # count for favourite item
                item_counter[item["item_name"]] = (
                    item_counter.get(item["item_name"], 0) + item["quantity"]
                )

            past_orders.append({
                "date":         date_str,
                "order_type":   order_type,
                "items":        items,
                "food_subtotal": round(food_subtotal, 2),
            })

        # Derive favourite item (most ordered by quantity)
        favorite_item = (
            max(item_counter, key=lambda k: item_counter[k])
            if item_counter else None
        )

        # Derive usual order type
        if order_type_counter["pickup"] > order_type_counter["delivery"]:
            usual_order_type = "pickup"
        elif order_type_counter["delivery"] > order_type_counter["pickup"]:
            usual_order_type = "delivery"
        else:
            usual_order_type = None

    return {
        "total_orders":     total_orders,
        "past_orders":      past_orders,
        "favorite_item":    favorite_item,
        "usual_order_type": usual_order_type,
    }


def upsert_customer(
    phone_number: str,
    first_name: str,
    last_name: str,
    default_address: Optional[str],
    notes: Optional[str],
) -> dict:
    with get_conn() as conn:
        # Determine action before the upsert so we can report it accurately.
        existing = conn.execute(
            "SELECT id FROM customers WHERE phone_number = ?", (phone_number,)
        ).fetchone()
        action = "updated" if existing else "created"

        # Atomic upsert — eliminates the TOCTOU race that the old
        # SELECT + conditional INSERT/UPDATE pattern had.
        conn.execute(
            """
            INSERT INTO customers (phone_number, first_name, last_name, default_address, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(phone_number) DO UPDATE SET
                first_name      = excluded.first_name,
                last_name       = excluded.last_name,
                default_address = excluded.default_address,
                notes           = excluded.notes,
                updated_at      = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (phone_number, first_name, last_name, default_address, notes),
        )

        row = conn.execute(
            "SELECT * FROM customers WHERE phone_number = ?", (phone_number,)
        ).fetchone()
        return {"action": action, "customer": dict(row)}


# ---------------------------------------------------------------------------
# Cart CRUD
# ---------------------------------------------------------------------------

def create_cart(
    phone_number:         str,
    order_type:           str,
    customer_name:        str,
    delivery_address:     Optional[str] = None,  # normalized (for display)
    raw_delivery_address: Optional[str] = None,  # raw spoken (for driver / receipt)
    address_confidence:   str = "high",
) -> dict:
    """Insert a new active cart and return it."""
    # If caller didn't supply raw address, fall back to normalized
    raw = raw_delivery_address or delivery_address
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO carts
              (phone_number, order_type, customer_name,
               delivery_address, raw_delivery_address, address_confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (phone_number, order_type, customer_name,
             delivery_address, raw, address_confidence),
        )
        row = conn.execute(
            "SELECT * FROM carts WHERE cart_id = last_insert_rowid()"
        ).fetchone()
        return dict(row)


def add_item_to_cart(
    cart_id: int,
    item_name: str,
    quantity: int,
    unit_price: float,
    size: Optional[str] = None,
    modifiers: Optional[List[str]] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Append one item to an existing active cart.
    line_total is computed here — never trusted from the caller.
    Raises CartNotFoundError or CartNotActiveError on bad cart state.
    """
    line_total     = round(quantity * unit_price, 2)
    modifiers_json = json.dumps(modifiers or [])

    with get_conn() as conn:
        cart = conn.execute(
            "SELECT cart_id, status FROM carts WHERE cart_id = ?", (cart_id,)
        ).fetchone()

        if cart is None:
            raise CartNotFoundError(f"Cart {cart_id} not found")
        if cart["status"] != "active":
            raise CartNotActiveError(cart["status"])

        conn.execute(
            """
            INSERT INTO cart_items
                (cart_id, item_name, size, quantity, unit_price, line_total, modifiers_json, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cart_id, item_name, size, quantity, unit_price, line_total, modifiers_json, notes),
        )

        item_row = conn.execute(
            "SELECT * FROM cart_items WHERE id = last_insert_rowid()"
        ).fetchone()
        item = dict(item_row)
        item["modifiers"] = json.loads(item.pop("modifiers_json"))

        # Running food subtotal so the agent can echo the live total after each add
        subtotal_row = conn.execute(
            "SELECT ROUND(SUM(line_total), 2) AS subtotal FROM cart_items WHERE cart_id = ?",
            (cart_id,),
        ).fetchone()
        food_subtotal = subtotal_row["subtotal"] or 0.0
        item["food_subtotal"] = food_subtotal

        return item


def get_cart_summary(cart_id: int) -> dict:
    """
    Return the full cart with all items plus a clean totals block:
      food_subtotal      — sum of all line_totals (the "food" cost only)
      delivery_fee       — 2.99 for delivery, 0.00 for pickup
      final_total        — food_subtotal + delivery_fee
      meets_delivery_min — True if food_subtotal >= DELIVERY_MINIMUM (delivery only)

    Raises CartNotFoundError if cart_id doesn't exist.
    """
    with get_conn() as conn:
        cart_row = conn.execute(
            "SELECT * FROM carts WHERE cart_id = ?", (cart_id,)
        ).fetchone()

        if cart_row is None:
            raise CartNotFoundError(f"Cart {cart_id} not found")

        cart = dict(cart_row)

        item_rows = conn.execute(
            "SELECT * FROM cart_items WHERE cart_id = ? ORDER BY id",
            (cart_id,),
        ).fetchall()

    # Deserialise modifiers outside the connection (read-only, safe)
    items = []
    for row in item_rows:
        item = dict(row)
        item["modifiers"] = json.loads(item.pop("modifiers_json"))
        items.append(item)

    # ── Totals block ──────────────────────────────────────────────────────────
    food_subtotal = round(sum(i["line_total"] for i in items), 2)
    delivery_fee  = round(DELIVERY_FEE if cart["order_type"] == "delivery" else 0.0, 2)

    # Coupon discount — only applied when caller has a coupon (no automatic discounts)
    coupon_type  = cart.get("coupon_type")
    coupon_value = cart.get("coupon_value") or 0.0
    coupon_desc  = cart.get("coupon_description")
    discount     = _calculate_discount(food_subtotal, coupon_type, coupon_value)
    taxable_amt  = round(food_subtotal - discount, 2)
    tax          = round(taxable_amt * TAX_RATE, 2)
    final_total  = round(taxable_amt + delivery_fee + tax, 2)

    summary = {
        "cart_id":            cart_id,
        "phone_number":       cart["phone_number"],
        "order_type":         cart["order_type"],
        "customer_name":      cart["customer_name"],
        "delivery_address":      cart["delivery_address"],
        "raw_delivery_address":  cart.get("raw_delivery_address") or cart["delivery_address"],
        "address_confidence":    cart.get("address_confidence", "high"),
        "status":                cart["status"],
        "clover_order_id":       cart["clover_order_id"],
        "confirmed_at":          cart["confirmed_at"],
        # Scheduled order fields
        "scheduled_for":         cart["scheduled_for"],
        "scheduled_status":      cart["scheduled_status"],
        "scheduled_timezone":    cart["scheduled_timezone"],
        # Coupon fields
        "coupon_applied":        coupon_type is not None,
        "coupon_description":    coupon_desc,
        "item_count":         len(items),
        "items":              items,
        # ── Totals ────────────────────────────────────────────────────────────
        "food_subtotal":      food_subtotal,
        "discount":           discount,
        "taxable_amount":     taxable_amt,
        "tax":                tax,
        "tax_rate":           "6.5%",
        "delivery_fee":       delivery_fee,
        "final_total":        final_total,
    }

    # meets_delivery_minimum is based on food cost alone, NOT final_total.
    if cart["order_type"] == "delivery":
        summary["meets_delivery_minimum"] = food_subtotal >= DELIVERY_MINIMUM

    return summary


def update_cart_item(
    cart_id:      int,
    cart_item_id: int,
    quantity:     Optional[int]       = None,
    size:         Optional[str]       = None,
    modifiers:    Optional[List[str]] = None,
    notes:        Optional[str]       = None,
) -> dict:
    """
    Update one or more fields of an existing cart item.
    Only fields explicitly passed (not None) are changed.
    line_total is always recalculated from the final quantity × unit_price.
    Raises CartNotFoundError, CartNotActiveError, or CartItemNotFoundError.
    """
    with get_conn() as conn:
        cart = conn.execute(
            "SELECT cart_id, status FROM carts WHERE cart_id = ?", (cart_id,)
        ).fetchone()
        if cart is None:
            raise CartNotFoundError(f"Cart {cart_id} not found")
        if cart["status"] != "active":
            raise CartNotActiveError(cart["status"])

        item = conn.execute(
            "SELECT * FROM cart_items WHERE id = ? AND cart_id = ?",
            (cart_item_id, cart_id),
        ).fetchone()
        if item is None:
            raise CartItemNotFoundError(
                f"Item {cart_item_id} not found in cart {cart_id}"
            )

        # Apply only the fields the caller provided; keep existing values otherwise
        new_quantity  = quantity  if quantity  is not None else item["quantity"]
        new_size      = size      if size      is not None else item["size"]
        new_notes     = notes     if notes     is not None else item["notes"]
        new_modifiers = modifiers if modifiers is not None else json.loads(item["modifiers_json"] or "[]")
        new_line_total = round(item["unit_price"] * new_quantity, 2)

        conn.execute(
            """
            UPDATE cart_items
               SET quantity       = ?,
                   size           = ?,
                   notes          = ?,
                   modifiers_json = ?,
                   line_total     = ?
             WHERE id = ? AND cart_id = ?
            """,
            (new_quantity, new_size, new_notes,
             json.dumps(new_modifiers), new_line_total,
             cart_item_id, cart_id),
        )

        updated_row = conn.execute(
            "SELECT * FROM cart_items WHERE id = ? AND cart_id = ?",
            (cart_item_id, cart_id),
        ).fetchone()
        updated = dict(updated_row)
        updated["modifiers"] = json.loads(updated.pop("modifiers_json"))

        subtotal_row = conn.execute(
            "SELECT ROUND(COALESCE(SUM(line_total), 0), 2) AS subtotal "
            "FROM cart_items WHERE cart_id = ?",
            (cart_id,),
        ).fetchone()

    return {
        "status":        "updated",
        "cart_item":     updated,
        "food_subtotal": subtotal_row["subtotal"],
    }


def remove_cart_item(cart_id: int, cart_item_id: int) -> dict:
    """
    Permanently delete one item from an active cart.
    Raises CartNotFoundError, CartNotActiveError, or CartItemNotFoundError.
    """
    with get_conn() as conn:
        cart = conn.execute(
            "SELECT cart_id, status FROM carts WHERE cart_id = ?", (cart_id,)
        ).fetchone()
        if cart is None:
            raise CartNotFoundError(f"Cart {cart_id} not found")
        if cart["status"] != "active":
            raise CartNotActiveError(cart["status"])

        item = conn.execute(
            "SELECT * FROM cart_items WHERE id = ? AND cart_id = ?",
            (cart_item_id, cart_id),
        ).fetchone()
        if item is None:
            raise CartItemNotFoundError(
                f"Item {cart_item_id} not found in cart {cart_id}"
            )

        removed_name = item["item_name"]

        conn.execute(
            "DELETE FROM cart_items WHERE id = ? AND cart_id = ?",
            (cart_item_id, cart_id),
        )

        subtotal_row = conn.execute(
            "SELECT ROUND(COALESCE(SUM(line_total), 0), 2) AS subtotal "
            "FROM cart_items WHERE cart_id = ?",
            (cart_id,),
        ).fetchone()

    return {
        "status":            "removed",
        "removed_item_name": removed_name,
        "food_subtotal":     subtotal_row["subtotal"],
    }


def clear_cart(cart_id: int) -> dict:
    """
    Delete all items from an active cart but keep the cart row itself.
    Use this when the caller wants to start the order over mid-call.
    Raises CartNotFoundError or CartNotActiveError.
    """
    with get_conn() as conn:
        cart = conn.execute(
            "SELECT cart_id, status FROM carts WHERE cart_id = ?", (cart_id,)
        ).fetchone()
        if cart is None:
            raise CartNotFoundError(f"Cart {cart_id} not found")
        if cart["status"] != "active":
            raise CartNotActiveError(cart["status"])

        result = conn.execute(
            "DELETE FROM cart_items WHERE cart_id = ?", (cart_id,)
        )
        removed_count = result.rowcount

    return {
        "status":             "cleared",
        "cart_id":            cart_id,
        "removed_item_count": removed_count,
    }


def cancel_cart(cart_id: int) -> dict:
    """
    Mark a cart as cancelled. Preserves the cart row for reporting.
    Prevents any further modification.
    Raises CartNotFoundError if the cart doesn't exist.
    Already-cancelled carts return the same response (idempotent).
    """
    with get_conn() as conn:
        cart = conn.execute(
            "SELECT cart_id, status FROM carts WHERE cart_id = ?", (cart_id,)
        ).fetchone()
        if cart is None:
            raise CartNotFoundError(f"Cart {cart_id} not found")

        conn.execute(
            """
            UPDATE carts
               SET status     = 'cancelled',
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE cart_id = ?
            """,
            (cart_id,),
        )

    return {
        "status":  "cancelled",
        "cart_id": cart_id,
    }


# ---------------------------------------------------------------------------
# Coupon / discount
# ---------------------------------------------------------------------------

def apply_coupon(
    cart_id:     int,
    coupon_type:  str,   # 'percent' or 'flat'
    coupon_value: float,
    description:  str,
) -> dict:
    """
    Apply a coupon to an active cart.
    coupon_type  = 'percent' → coupon_value is the percentage (e.g. 10 = 10% off)
    coupon_type  = 'flat'    → coupon_value is dollar amount (e.g. 5 = $5 off)
    Raises CartNotFoundError or CartNotActiveError.
    Returns the updated cart summary so the agent can read back the new total.
    """
    with get_conn() as conn:
        cart = conn.execute(
            "SELECT cart_id, status FROM carts WHERE cart_id = ?", (cart_id,)
        ).fetchone()
        if cart is None:
            raise CartNotFoundError(f"Cart {cart_id} not found")
        if cart["status"] != "active":
            raise CartNotActiveError(cart["status"])

        conn.execute(
            """
            UPDATE carts
               SET coupon_type        = ?,
                   coupon_value       = ?,
                   coupon_description = ?,
                   updated_at         = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE cart_id = ?
            """,
            (coupon_type, coupon_value, description.strip(), cart_id),
        )

    return get_cart_summary(cart_id)


def remove_coupon(cart_id: int) -> dict:
    """
    Remove any coupon from an active cart (restore full price).
    Returns the updated cart summary.
    """
    with get_conn() as conn:
        cart = conn.execute(
            "SELECT cart_id, status FROM carts WHERE cart_id = ?", (cart_id,)
        ).fetchone()
        if cart is None:
            raise CartNotFoundError(f"Cart {cart_id} not found")
        if cart["status"] != "active":
            raise CartNotActiveError(cart["status"])

        conn.execute(
            """
            UPDATE carts
               SET coupon_type        = NULL,
                   coupon_value       = 0.0,
                   coupon_description = NULL,
                   updated_at         = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE cart_id = ?
            """,
            (cart_id,),
        )

    return get_cart_summary(cart_id)


# ---------------------------------------------------------------------------
# Staff dashboard — order list
# ---------------------------------------------------------------------------

def get_orders(
    status: Optional[str] = None,
    limit: int = 50,
) -> list:
    """
    Return a list of carts (most recent first) for the staff dashboard.
    Each row includes a compact item summary so staff can glance at an order
    without needing a separate /get-cart-summary call.

    status — filter by 'active', 'confirmed', or 'cancelled'; None returns all.
    limit  — max rows returned (default 50, capped at 200).
    """
    limit = min(limit, 200)

    with get_conn() as conn:
        if status:
            cart_rows = conn.execute(
                """
                SELECT * FROM carts
                WHERE status = ?
                ORDER BY COALESCE(confirmed_at, created_at) DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        else:
            cart_rows = conn.execute(
                """
                SELECT * FROM carts
                ORDER BY COALESCE(confirmed_at, created_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        orders = []
        for cart_row in cart_rows:
            cart = dict(cart_row)
            cart_id = cart["cart_id"]

            item_rows = conn.execute(
                "SELECT item_name, size, quantity, unit_price, line_total "
                "FROM cart_items WHERE cart_id = ? ORDER BY id",
                (cart_id,),
            ).fetchall()

            items = [dict(r) for r in item_rows]
            food_subtotal = round(sum(i["line_total"] for i in items), 2)
            delivery_fee  = round(DELIVERY_FEE if cart["order_type"] == "delivery" else 0.0, 2)

            orders.append({
                "cart_id":          cart_id,
                "status":           cart["status"],
                "order_type":       cart["order_type"],
                "customer_name":    cart["customer_name"],
                "phone_number":     cart["phone_number"],
                "delivery_address": cart["delivery_address"],
                "item_count":       len(items),
                "items":            items,
                "food_subtotal":    food_subtotal,
                "delivery_fee":     delivery_fee,
                "final_total":      round(food_subtotal + delivery_fee, 2),
                "clover_order_id":  cart["clover_order_id"],
                "confirmed_at":     cart["confirmed_at"],
                "created_at":       cart["created_at"],
            })

    return orders


# ---------------------------------------------------------------------------
# Scheduled order support
# ---------------------------------------------------------------------------

def set_order_time(cart_id: int, scheduled_for: str) -> dict:
    """
    Store a resolved ISO-8601 scheduled_for datetime on an active cart.
    Marks scheduled_status = 'pending' so confirm_order knows not to fire
    the order to Clover immediately.

    scheduled_for must be a valid ISO-8601 string (America/New_York aware).
    Raises CartNotFoundError or CartNotActiveError on bad cart state.
    """
    with get_conn() as conn:
        cart = conn.execute(
            "SELECT cart_id, status FROM carts WHERE cart_id = ?", (cart_id,)
        ).fetchone()
        if cart is None:
            raise CartNotFoundError(f"Cart {cart_id} not found")
        if cart["status"] != "active":
            raise CartNotActiveError(cart["status"])

        conn.execute(
            """
            UPDATE carts
               SET scheduled_for    = ?,
                   scheduled_status = 'pending',
                   updated_at       = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE cart_id = ?
            """,
            (scheduled_for, cart_id),
        )

    return {
        "status":           "scheduled",
        "cart_id":          cart_id,
        "scheduled_for":    scheduled_for,
        "scheduled_status": "pending",
    }


def _is_future_scheduled(summary: dict) -> bool:
    """
    Return True if the cart has a future scheduled time that has NOT yet
    entered its prep window.  Immediate orders (or ones whose prep window
    has already arrived) return False so confirm_order fires them now.
    """
    scheduled_for = summary.get("scheduled_for")
    if not scheduled_for or summary.get("scheduled_status") == "not_scheduled":
        return False

    try:
        scheduled_dt = datetime.fromisoformat(scheduled_for)
        if scheduled_dt.tzinfo is None:
            scheduled_dt = _TZ.localize(scheduled_dt)

        buffer = (
            PICKUP_PREP_BUFFER_MIN
            if summary["order_type"] == "pickup"
            else DELIVERY_PREP_BUFFER_MIN
        )
        release_time = scheduled_dt - timedelta(minutes=buffer)
        return datetime.now(_TZ) < release_time
    except (ValueError, TypeError):
        return False   # unparseable — treat as immediate


def _fmt_scheduled(iso: str) -> str:
    """Return a human-readable string like 'Friday, May 8 at 6:00 PM'."""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = _TZ.localize(dt)
        return dt.strftime("%A, %B %-d at %-I:%M %p")
    except Exception:
        return iso


# ---------------------------------------------------------------------------
# Order confirmation + Clover push
# ---------------------------------------------------------------------------

def confirm_order(cart_id: int) -> dict:
    """
    Final gate before an order becomes real:
      1. Load and validate the staged cart (active, non-empty, delivery minimum met).
      2. Push to Clover via clover.create_clover_order().
         If Clover is not configured, the push is skipped and the order is
         confirmed locally (clover_order_id stays null).
      3. Flip cart status to 'confirmed' and store the Clover order ID.

    Raises:
      CartNotFoundError      — cart_id does not exist
      CartNotActiveError     — cart is already confirmed or cancelled
      ValueError             — cart is empty, or delivery minimum not met
      RuntimeError           — Clover API call failed (cart stays active for retry)
    """
    from clover import create_clover_order, is_configured

    # Load full summary for validation — raises CartNotFoundError if missing
    summary = get_cart_summary(cart_id)

    if summary["status"] != "active":
        raise CartNotActiveError(summary["status"])

    if summary["item_count"] == 0:
        raise ValueError("Cannot confirm an empty cart — add at least one item first")

    if (summary["order_type"] == "delivery"
            and not summary.get("meets_delivery_minimum", True)):
        raise ValueError(
            f"Delivery minimum of ${DELIVERY_MINIMUM:.2f} not met. "
            f"Current food subtotal: ${summary['food_subtotal']:.2f}"
        )

    # ── SCHEDULED ORDER — prep window has not arrived yet ─────────────────────
    if _is_future_scheduled(summary):
        human = _fmt_scheduled(summary["scheduled_for"])

        with get_conn() as conn:
            conn.execute(
                """
                UPDATE carts
                   SET status           = 'confirmed',
                       scheduled_status = 'pending',
                       confirmed_at     = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                       updated_at       = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                 WHERE cart_id = ?
                """,
                (cart_id,),
            )

        return {
            "status":          "confirmed",
            "scheduled":       True,
            "cart_id":         cart_id,
            "scheduled_for":   summary["scheduled_for"],
            "human_readable":  human,
            "clover_order_id": None,
            "clover_enabled":  is_configured(),
            "customer_name":   summary["customer_name"],
            "order_type":      summary["order_type"],
            "food_subtotal":   summary["food_subtotal"],
            "delivery_fee":    summary["delivery_fee"],
            "final_total":     summary["final_total"],
            "message":         f"Scheduled for {human}. Order will be sent to the kitchen at prep time.",
        }

    # ── IMMEDIATE ORDER (or scheduled order whose prep window just opened) ────
    # Push to Clover now
    clover_order_id = create_clover_order(summary)

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE carts
               SET status           = 'confirmed',
                   scheduled_status = 'released',
                   clover_order_id  = ?,
                   confirmed_at     = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                   updated_at       = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE cart_id = ?
            """,
            (clover_order_id, cart_id),
        )

    return {
        "status":          "confirmed",
        "scheduled":       False,
        "cart_id":         cart_id,
        "clover_order_id": clover_order_id,
        "clover_enabled":  is_configured(),
        "customer_name":   summary["customer_name"],
        "order_type":      summary["order_type"],
        "food_subtotal":   summary["food_subtotal"],
        "delivery_fee":    summary["delivery_fee"],
        "final_total":     summary["final_total"],
    }
