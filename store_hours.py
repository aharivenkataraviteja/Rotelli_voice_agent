"""
Store hours module — backend owns ALL time/open logic.
The AI never decides whether the store is open from memory.
It always calls /check-store-status first.

Hours (America/New_York):
  Monday–Thursday:  11:00 AM – 9:00 PM
  Friday–Saturday:  11:00 AM – 10:00 PM
  Sunday:           12:00 PM – 9:00 PM
"""

from datetime import datetime, time
from typing import Optional
import pytz

_TZ = pytz.timezone("America/New_York")

# day-of-week (0=Monday … 6=Sunday) → (open_time, close_time)
_HOURS: dict[int, tuple[time, time]] = {
    0: (time(11, 0), time(21, 0)),   # Monday
    1: (time(11, 0), time(21, 0)),   # Tuesday
    2: (time(11, 0), time(21, 0)),   # Wednesday
    3: (time(11, 0), time(21, 0)),   # Thursday
    4: (time(11, 0), time(22, 0)),   # Friday
    5: (time(11, 0), time(22, 0)),   # Saturday
    6: (time(12, 0), time(21, 0)),   # Sunday
}

_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _opens_at_str(day: int) -> str:
    """Human-readable opening time for a given weekday index."""
    t = _HOURS[day][0]
    return t.strftime("%-I:%M %p")          # e.g. "11:00 AM" or "12:00 PM"


def _closes_at_str(day: int) -> str:
    t = _HOURS[day][1]
    return t.strftime("%-I:%M %p")


def _next_opening(now: datetime) -> dict:
    """
    Find the next opening time after `now`.
    Returns {day_name, opens_at} for the soonest upcoming open.
    """
    dow = now.weekday()
    # Check today first (in case we're before opening), then up to 6 days ahead
    for delta in range(7):
        check_day = (dow + delta) % 7
        open_t, close_t = _HOURS[check_day]

        # Build a candidate open datetime in the same timezone
        candidate_open = _TZ.localize(
            datetime(now.year, now.month, now.day,
                     open_t.hour, open_t.minute)
        )
        # Shift forward by delta days
        from datetime import timedelta
        candidate_open = candidate_open.replace(
            day=(now + timedelta(days=delta)).day,
            month=(now + timedelta(days=delta)).month,
            year=(now + timedelta(days=delta)).year,
        )

        if candidate_open > now:
            label = "today" if delta == 0 else ("tomorrow" if delta == 1 else _DAY_NAMES[check_day])
            return {
                "day":      label,
                "opens_at": _opens_at_str(check_day),
            }

    return {"day": "tomorrow", "opens_at": "11:00 AM"}   # fallback, should never hit


def check_store_status() -> dict:
    """
    Return the current open/closed state of the restaurant.

    Fields:
      is_open          — bool
      accepting_orders — same as is_open (future: can differ for kitchen-closed)
      current_time     — ISO-8601 with ET offset, e.g. "2026-05-20T09:32:00-04:00"
      current_day      — "Tuesday"
      timezone         — "America/New_York"
      opens_at         — when store opens today (even if already open)
      closes_at        — when store closes today
      next_opening     — if closed, when it next opens {"day": "tomorrow", "opens_at": "11:00 AM"}
      message          — human-readable sentence for the agent to say if closed
    """
    now = datetime.now(_TZ)
    dow = now.weekday()
    open_t, close_t = _HOURS[dow]
    current_t = now.time()

    is_open = open_t <= current_t < close_t

    result: dict = {
        "is_open":          is_open,
        "accepting_orders": is_open,
        "current_time":     now.isoformat(),
        "current_day":      _DAY_NAMES[dow],
        "timezone":         "America/New_York",
        "opens_at":         _opens_at_str(dow),
        "closes_at":        _closes_at_str(dow),
        "next_opening":     None,
        "message":          None,
    }

    if not is_open:
        nxt = _next_opening(now)
        result["next_opening"] = nxt

        if current_t < open_t:
            result["message"] = (
                f"We open at {nxt['opens_at']} {nxt['day']}. "
                f"I can schedule an order for you — would that work?"
            )
        else:
            result["message"] = (
                f"We're closed for the night. We reopen {nxt['day']} at {nxt['opens_at']}. "
                f"I can schedule an order for you — would that work?"
            )

    return result
