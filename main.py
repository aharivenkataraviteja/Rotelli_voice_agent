from contextlib import asynccontextmanager
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

import pytz

from dotenv import load_dotenv
load_dotenv()  # reads .env into os.environ before anything else imports it

from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
class VapiToolMiddleware:
    """
    Vapi sends tool calls wrapped in:
      {"message": {"type": "tool-calls", "toolCallList": [{"id": "...", "function": {"name": "...", "arguments": "{...}"}}]}}
    and expects back:
      {"results": [{"toolCallId": "...", "result": "<json string>"}]}

    This ASGI middleware unwraps the Vapi payload before the endpoint sees it,
    then re-wraps the response so Vapi can parse the result.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Buffer the full request body
        body = b""
        more_body = True
        while more_body:
            message = await receive()
            body += message.get("body", b"")
            more_body = message.get("more_body", False)

        vapi_tool_call_id = None
        if body:
            try:
                data = json.loads(body)
                msg = data.get("message", {})
                if msg.get("type") == "tool-calls" and "toolCallList" in msg:
                    tool_call = msg["toolCallList"][0]
                    vapi_tool_call_id = tool_call["id"]
                    args_str = tool_call["function"]["arguments"]
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    body = json.dumps(args).encode()
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        body_sent = False

        async def new_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        if vapi_tool_call_id is None:
            await self.app(scope, new_receive, send)
            return

        # Capture the inner response
        response_body = b""

        async def capture_send(message):
            nonlocal response_body
            if message["type"] == "http.response.body":
                response_body += message.get("body", b"")

        await self.app(scope, new_receive, capture_send)

        vapi_response = json.dumps({
            "results": [{
                "toolCallId": vapi_tool_call_id,
                "result": response_body.decode(),
            }]
        }).encode()

        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(vapi_response)).encode()],
            ],
        })
        await send({
            "type": "http.response.body",
            "body": vapi_response,
            "more_body": False,
        })

from database import init_db
from models import (
    LookupByPhoneRequest,
    SaveOrUpdateCustomerRequest,
    DeliveryEligibilityRequest,
    CreateCartRequest,
    AddItemRequest,
    GetCartSummaryRequest,
    UpdateCartItemRequest,
    RemoveCartItemRequest,
    ClearCartRequest,
    CancelCartRequest,
    ConfirmOrderRequest,
    SetOrderTimeRequest,
    ApplyCouponRequest,
    RemoveCouponRequest,
)
from crud import (
    get_customer_by_phone,
    get_customer_order_history,
    upsert_customer,
    create_cart,
    add_item_to_cart,
    get_cart_summary,
    update_cart_item,
    remove_cart_item,
    clear_cart,
    cancel_cart,
    confirm_order,
    set_order_time,
    apply_coupon,
    remove_coupon,
    get_orders,
    CartNotFoundError,
    CartNotActiveError,
    CartItemNotFoundError,
)
from geocoding import check_eligibility
from scheduler import scheduler_loop
from store_hours import check_store_status

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Start background task — releases scheduled orders when prep window arrives
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Restaurant Voice Agent API", lifespan=lifespan)
app.add_middleware(VapiToolMiddleware)  # type: ignore[arg-type]


@app.get("/")
def health():
    """Health check — use this to confirm the server is reachable from Vapi/ngrok."""
    return {"status": "ok", "service": "restaurant-voice-agent-api"}


@app.get("/dashboard", response_class=FileResponse)
def dashboard():
    """Staff order dashboard — auto-refreshes every 30 seconds."""
    return FileResponse(Path(__file__).parent / "dashboard.html")


@app.post("/lookup-customer-by-phone")
def lookup_customer_by_phone(body: LookupByPhoneRequest):
    customer = get_customer_by_phone(body.phone_number)

    # Not found is a normal conversation branch, NOT an error — never return 404
    if customer is None:
        return {
            "found": False,
            "phone_number": body.phone_number,
            "next_action": (
                "NEW CUSTOMER — no record found. This is normal. "
                "Step 1: Ask the caller their first and last name. "
                "Step 2: As soon as you have both names, call save_or_update_customer "
                f"with phone_number='{body.phone_number}', first_name=<first>, last_name=<last>. "
                "Step 3: After save_or_update_customer succeeds, call create_order_cart. "
                "Do NOT say you are having trouble at any point. Do NOT transfer."
            ),
        }

    history = get_customer_order_history(customer["phone_number"])

    # Build a natural greeting hint for the agent
    greeting_hint = (
        f"Returning customer: {customer['first_name']} {customer['last_name']}. "
        f"They have placed {history['total_orders']} order(s) with us. "
    )
    if history["past_orders"]:
        last = history["past_orders"][0]
        item_names = ", ".join(
            f"{i['quantity']}x {i['name']}" for i in last["items"]
        )
        greeting_hint += (
            f"Their last order ({last['date']}) was: {item_names} "
            f"({last['order_type']}, ${last['food_subtotal']}). "
        )
    if history["favorite_item"]:
        greeting_hint += f"Their favourite item is {history['favorite_item']}. "
    if history["usual_order_type"]:
        greeting_hint += f"They usually order {history['usual_order_type']}. "
    greeting_hint += (
        "Greet them warmly by first name. "
        "If they have past orders, offer to repeat their last order naturally. "
        "Do not read all this data out loud — use it to sound like you know them."
    )

    return {
        "found":            True,
        "phone_number":     customer["phone_number"],
        "first_name":       customer["first_name"],
        "last_name":        customer["last_name"],
        "default_address":  customer["default_address"],
        "notes":            customer["notes"],
        "order_history":    history,
        "next_action":      greeting_hint,
    }


@app.post("/save-or-update-customer")
def save_or_update_customer(body: SaveOrUpdateCustomerRequest):
    # body.address (public API name) maps to default_address (DB column name)
    result = upsert_customer(
        phone_number=body.phone_number,
        first_name=body.first_name,
        last_name=body.last_name,
        default_address=body.address,
        notes=body.notes,
    )
    result["next_action"] = (
        "Customer saved successfully. Do NOT say you are having trouble. "
        "Do NOT transfer. Immediately call create_order_cart with the caller's "
        "phone_number, customer_name (first + last name), and order_type."
    )
    return result


@app.post("/check-delivery-eligibility")
def check_delivery_eligibility(body: DeliveryEligibilityRequest):
    """
    Check whether an address is within the delivery radius.
    Always returns 200 — eligible:true/false is the decision, not an HTTP error.

    address_confidence values:
      "high" — geocoded successfully, distance is accurate
      "low"  — could not geocode but address looks local; delivery allowed,
               driver will confirm on arrival

    Only returns eligible:false when:
      - address clearly names an out-of-area city (Miami, Fort Lauderdale, etc.)
      - geocoded distance exceeds DELIVERY_RADIUS_MILES
    """
    try:
        result = check_eligibility(body.address)
        # Add agent guidance based on confidence
        if result.get("address_confidence") == "low":
            result["next_action"] = (
                f"Address accepted with low confidence. "
                f"Confirm back to the caller: "
                f"'Got it — {result['raw_address']}, correct?' "
                f"If they confirm, use raw_address as delivery_address in create_order_cart."
            )
        elif not result["eligible"]:
            result["next_action"] = (
                "Address is outside our delivery area. "
                "Say: 'Sorry, that address is outside our delivery area. "
                "I can set you up for pickup instead — would that work?'"
            )
        return result
    except RuntimeError as e:
        # Upstream API failure — soft pass rather than killing the call
        enriched = body.address if any(
            m in body.address.lower() for m in ("fl", "florida", "delray", "boca", "boynton")
        ) else f"{body.address}, Delray Beach, FL"
        return {
            "eligible":             True,
            "raw_address":          body.address,
            "normalized_address":   enriched,
            "address_confidence":   "low",
            "distance_miles":       None,
            "estimated_drive_time": None,
            "reason":               None,
            "note":                 f"Geocoding service unavailable ({e}). Accepted for local delivery.",
            "next_action": (
                f"Geocoding unavailable — accepting address. "
                f"Confirm: 'Got it — {body.address}, correct?'"
            ),
        }


@app.post("/check-store-status")
def store_status():
    """
    Return the restaurant's current open/closed state based on EST business hours.
    The agent MUST call this at the start of every new order conversation.
    Never decide open/closed from memory.
    """
    return check_store_status()


@app.post("/create-order-cart")
def create_order_cart(body: CreateCartRequest):
    """
    Start a fresh cart for the current call.
    Must be called before any items can be added.
    """
    cart = create_cart(
        phone_number          = body.phone_number,
        order_type            = body.order_type,
        customer_name         = body.customer_name,
        delivery_address      = body.delivery_address,
        raw_delivery_address  = body.raw_delivery_address,
        address_confidence    = body.address_confidence or "high",
    )
    return cart


@app.post("/add-item-to-cart")
def add_item(body: AddItemRequest):
    """
    Add one item to an existing active cart.
    line_total is computed server-side (quantity × unit_price).
    Returns the saved item plus the running cart subtotal.
    """
    try:
        item = add_item_to_cart(
            cart_id    = body.cart_id,
            item_name  = body.item_name,
            quantity   = body.quantity,
            unit_price = body.unit_price,
            size       = body.size,
            modifiers  = body.modifiers,
            notes      = body.notes,
        )
        return item
    except CartNotFoundError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except CartNotActiveError as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "status": e.status})


@app.post("/get-cart-summary")
def cart_summary(body: GetCartSummaryRequest):
    """
    Return all items, subtotal, item count, and delivery-minimum status.
    This is the source of truth the agent must use before confirming any order.
    """
    try:
        return get_cart_summary(body.cart_id)
    except CartNotFoundError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})


@app.post("/update-cart-item")
def update_cart_item_route(body: UpdateCartItemRequest):
    """
    Update quantity, size, modifiers, or notes on an existing cart item.
    Only fields included in the request body are changed.
    line_total is recalculated server-side. Returns the updated item + food_subtotal.
    """
    try:
        return update_cart_item(
            cart_id      = body.cart_id,
            cart_item_id = body.cart_item_id,
            quantity     = body.quantity,
            size         = body.size,
            modifiers    = body.modifiers,
            notes        = body.notes,
        )
    except CartNotFoundError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except CartNotActiveError as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "status": e.status})
    except CartItemNotFoundError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})


@app.post("/remove-cart-item")
def remove_cart_item_route(body: RemoveCartItemRequest):
    """
    Permanently delete one item from an active cart.
    Returns the removed item name and the updated food_subtotal.
    """
    try:
        return remove_cart_item(
            cart_id      = body.cart_id,
            cart_item_id = body.cart_item_id,
        )
    except CartNotFoundError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except CartNotActiveError as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "status": e.status})
    except CartItemNotFoundError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})


@app.post("/clear-cart")
def clear_cart_route(body: ClearCartRequest):
    """
    Remove all items from an active cart but keep the cart itself.
    Use when the caller says 'start over' or 'forget all that'.
    The cart stays active and ready to accept new items.
    """
    try:
        return clear_cart(body.cart_id)
    except CartNotFoundError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except CartNotActiveError as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "status": e.status})


@app.post("/cancel-cart")
def cancel_cart_route(body: CancelCartRequest):
    """
    Mark a cart as cancelled. Preserves the row for reporting.
    Use when the caller abandons the order, the call drops, or a transfer fails.
    Cancelled carts cannot be modified further.
    """
    try:
        return cancel_cart(body.cart_id)
    except CartNotFoundError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})


def _parse_spoken_time(spoken: str) -> "Optional[datetime]":
    """
    Convert a caller's spoken time phrase to a timezone-aware datetime.
    Handles common speech patterns that plain dateparser misses.
    """
    import dateparser
    import re

    TZ  = pytz.timezone("America/New_York")
    now = datetime.now(TZ)
    settings = {
        "PREFER_DATES_FROM":        "future",
        "TIMEZONE":                 "America/New_York",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "RELATIVE_BASE":            now,
    }

    # ── Normalise the phrase before handing to dateparser ──────────────────
    s = spoken.strip().lower()

    # If "tonight" is present and there's a bare time (no am/pm), force PM
    if re.search(r"\btonight\b", s) and not re.search(r"\b(am|pm|a\.m|p\.m)\b", s):
        s = re.sub(r"(\d{1,2}(?::\d{2})?)", r"\1 PM", s, count=1)

    # Vague time-of-day words → explicit hour
    s = re.sub(r"\btonight\b",    "today",   s)
    s = re.sub(r"\bthis evening\b","today",  s)
    s = re.sub(r"\bafternoon\b",  "3 PM",    s)
    s = re.sub(r"\bmorning\b",    "10 AM",   s)
    s = re.sub(r"\bevening\b",    "7 PM",    s)
    s = re.sub(r"\bnoon\b",       "12 PM",   s)
    s = re.sub(r"\bmidnight\b",   "11:59 PM",s)

    # Remove "at" between date and time — dateparser handles "Friday 6 PM" but not "Friday at 6 PM"
    s = re.sub(r"\bat\b", " ", s)

    # Remove "next/this" prefix — dateparser handles "Friday 6 PM" but not "next Friday 6 PM"
    s = re.sub(r"\b(next|this)\s+", "", s)

    # Collapse extra whitespace
    s = re.sub(r"\s+", " ", s).strip()

    # Pass 1: cleaned phrase
    result = dateparser.parse(s, settings=settings)
    if result:
        return result

    # Pass 2: try the original phrase unchanged (in case cleaning hurt it)
    return dateparser.parse(spoken, settings=settings)


@app.post("/set-order-time")
def set_order_time_route(body: SetOrderTimeRequest):
    """
    Parse a spoken time phrase (e.g. "next Friday at 6 PM", "tomorrow at noon")
    into a resolved ISO-8601 datetime in America/New_York, validate it, and store
    it on the cart.  The agent passes whatever the caller said — parsing happens here.

    Returns human_readable confirmation the agent can repeat back to the caller.
    Returns 422 with next_action guidance when the time cannot be parsed or is invalid.
    """
    TZ  = pytz.timezone("America/New_York")
    now = datetime.now(TZ)

    parsed = _parse_spoken_time(body.spoken_time)

    if parsed is None:
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"Could not understand the time '{body.spoken_time}'.",
                "next_action": (
                    "Say: 'I didn't catch that — what time did you have in mind? "
                    "For example, Friday at 6 PM or tomorrow at noon.' "
                    "Then call set_order_time again with the caller's answer."
                ),
            },
        )

    if parsed <= now:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "That time has already passed.",
                "next_action": "Say: 'That time has already passed — did you mean tomorrow?' Then call set_order_time again.",
            },
        )

    if (parsed - now).days > 7:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Cannot schedule orders more than 7 days in advance.",
                "next_action": "Say: 'We can schedule up to 7 days ahead — did you mean a closer date?'",
            },
        )

    iso_time      = parsed.isoformat()
    human_readable = parsed.strftime("%A, %B %-d at %-I:%M %p")

    try:
        result = set_order_time(body.cart_id, iso_time)
        result["human_readable"] = human_readable
        result["next_action"] = (
            f"Time set to {human_readable}. "
            f"Confirm back to the caller: 'Just to confirm — that's {human_readable}, right?' "
            "If they say yes, proceed with get_cart_summary then confirm_order."
        )
        return result
    except CartNotFoundError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except CartNotActiveError as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "status": e.status})


@app.post("/apply-coupon")
def apply_coupon_route(body: ApplyCouponRequest):
    """
    Apply a customer coupon to an active cart.
    coupon_type = 'percent' (e.g. 10 = 10% off food subtotal)
    coupon_type = 'flat'    (e.g. 5  = $5 off food subtotal)
    Returns the full updated cart summary with the new totals.
    """
    try:
        result = apply_coupon(
            cart_id=body.cart_id,
            coupon_type=body.coupon_type,
            coupon_value=body.coupon_value,
            description=body.description,
        )
        result["next_action"] = (
            f"Coupon applied: {body.description}. "
            f"Discount: ${result['discount']:.2f}. "
            f"New total: ${result['final_total']:.2f} (tax included). "
            "Read the updated total back to the caller."
        )
        return result
    except CartNotFoundError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except CartNotActiveError as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "status": e.status})


@app.post("/remove-coupon")
def remove_coupon_route(body: RemoveCouponRequest):
    """
    Remove the coupon from an active cart and restore full pricing.
    Returns the updated cart summary.
    """
    try:
        result = remove_coupon(cart_id=body.cart_id)
        result["next_action"] = "Coupon removed. Full price restored."
        return result
    except CartNotFoundError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except CartNotActiveError as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "status": e.status})


@app.post("/confirm-order")
def confirm_order_route(body: ConfirmOrderRequest):
    """
    Final gate — validate the staged cart, push to Clover, lock as confirmed.
    Only call this after the caller has said yes to the order summary.

    Validates:
      - Cart exists and is active
      - Cart has at least one item
      - Delivery minimum is met (delivery orders only)

    On success: cart status → confirmed, Clover order ID stored.
    On Clover failure: cart stays active so the call can retry or transfer.
    """
    try:
        return confirm_order(body.cart_id)
    except CartNotFoundError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except CartNotActiveError as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "status": e.status})
    except ValueError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})
    except RuntimeError as e:
        # Clover API failure — cart intentionally stays active for retry
        return JSONResponse(status_code=503, content={"detail": str(e)})


@app.get("/orders")
def list_orders(
    status: Optional[str] = Query(
        default=None,
        description="Filter by cart status: active, confirmed, or cancelled",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of orders to return",
    ),
):
    """
    Staff dashboard — list recent orders with compact item summaries.
    Use ?status=confirmed to see today's confirmed orders, ?status=active
    to see in-progress carts, or omit for all.
    Sorted most-recent first (confirmed_at, falling back to created_at).
    """
    if status and status not in ("active", "confirmed", "cancelled"):
        return JSONResponse(
            status_code=422,
            content={"detail": "status must be 'active', 'confirmed', or 'cancelled'"},
        )
    return get_orders(status=status, limit=limit)


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = [
        {"field": ".".join(str(loc) for loc in e["loc"]), "message": e["msg"]}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})
