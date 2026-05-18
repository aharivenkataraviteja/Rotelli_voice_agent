"""
Background scheduled-order release task.

Runs every 60 seconds inside the FastAPI process via asyncio.
Finds confirmed carts whose prep window has arrived and pushes them to Clover.

Prep windows (configurable via env):
  pickup:   fire 30 min before scheduled_for
  delivery: fire 60 min before scheduled_for
"""

import asyncio
import logging
from datetime import datetime, timedelta

import pytz

from crud import PICKUP_PREP_BUFFER_MIN, DELIVERY_PREP_BUFFER_MIN, get_cart_summary
from database import get_conn

log = logging.getLogger(__name__)

_TZ = pytz.timezone("America/New_York")
_RELEASE_INTERVAL_SEC = 60   # how often to check


async def release_scheduled_orders() -> int:
    """
    One pass: find all confirmed-but-pending scheduled orders whose prep window
    has arrived, push them to Clover, and mark them released.

    Returns the number of orders released this pass.
    """
    from clover import create_clover_order, is_configured

    now = datetime.now(_TZ)

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT cart_id, order_type, scheduled_for
            FROM   carts
            WHERE  status           = 'confirmed'
              AND  scheduled_status = 'pending'
              AND  scheduled_for    IS NOT NULL
            """
        ).fetchall()

    released = 0
    for row in rows:
        cart_id       = row["cart_id"]
        order_type    = row["order_type"]
        scheduled_str = row["scheduled_for"]

        try:
            scheduled_dt = datetime.fromisoformat(scheduled_str)
            if scheduled_dt.tzinfo is None:
                scheduled_dt = _TZ.localize(scheduled_dt)

            buffer       = PICKUP_PREP_BUFFER_MIN if order_type == "pickup" else DELIVERY_PREP_BUFFER_MIN
            release_time = scheduled_dt - timedelta(minutes=buffer)

            if now < release_time:
                continue   # not yet

            # Prep window reached — push to Clover
            summary         = get_cart_summary(cart_id)
            clover_order_id = create_clover_order(summary) if is_configured() else None

            with get_conn() as conn:
                conn.execute(
                    """
                    UPDATE carts
                       SET scheduled_status = 'released',
                           clover_order_id  = COALESCE(?, clover_order_id),
                           updated_at       = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                     WHERE cart_id = ?
                    """,
                    (clover_order_id, cart_id),
                )

            log.info(
                "Released scheduled order cart_id=%s to Clover (clover_id=%s)",
                cart_id, clover_order_id,
            )
            released += 1

        except Exception as exc:
            log.error("Failed to release scheduled order cart_id=%s: %s", cart_id, exc)

    return released


async def scheduler_loop() -> None:
    """Run release_scheduled_orders every 60 seconds indefinitely."""
    log.info("Scheduled-order release loop started (interval=%ss)", _RELEASE_INTERVAL_SEC)
    while True:
        try:
            n = await release_scheduled_orders()
            if n:
                log.info("Released %d scheduled order(s) this pass", n)
        except Exception as exc:
            log.error("Scheduler loop error: %s", exc)
        await asyncio.sleep(_RELEASE_INTERVAL_SEC)
