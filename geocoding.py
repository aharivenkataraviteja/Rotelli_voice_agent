"""
Delivery eligibility checker
=============================
Two modes — automatically selected based on env vars:

  Mode A (accurate) — set GOOGLE_MAPS_API_KEY in your .env
    Uses Google Maps Distance Matrix API.
    Returns real driving distance + drive-time estimate.

  Mode B (free fallback) — no API key needed
    Geocodes via OpenStreetMap Nominatim, then computes
    straight-line Haversine distance with a 1.3× road factor.
    Drive-time estimate is approximate (~25 mph average).

Configure in .env:
  GOOGLE_MAPS_API_KEY=   your key (or leave blank to use free mode)
  RESTAURANT_ADDRESS=    full address of your restaurant
  RESTAURANT_LAT=        latitude  (used only in free mode)
  RESTAURANT_LNG=        longitude (used only in free mode)
  DELIVERY_RADIUS_MILES= max miles for delivery (default 6.0)
"""

import math
import os
import re
import urllib.parse
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Configuration — override any of these in .env
# ---------------------------------------------------------------------------

GOOGLE_MAPS_API_KEY   = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
RESTAURANT_ADDRESS    = os.environ.get("RESTAURANT_ADDRESS",
                                       "Rotelli Pizza & Pasta, Delray Beach, FL 33446")
RESTAURANT_LAT        = float(os.environ.get("RESTAURANT_LAT", "26.3887"))
RESTAURANT_LNG        = float(os.environ.get("RESTAURANT_LNG", "-80.1450"))
DELIVERY_RADIUS_MILES = float(os.environ.get("DELIVERY_RADIUS_MILES", "10.0"))

# South Florida Nominatim viewbox — covers Boca Raton / Delray Beach / Boynton area
# Format: west,north,east,south
_NOMINATIM_VIEWBOX = "-80.35,26.75,-79.95,26.20"

# Keywords that indicate the address already has Florida/local context
_FL_MARKERS = (
    "fl", "florida", "delray", "boca raton", "boca", "boynton",
    "lake worth", "lantana", "hypoluxo", "highland beach",
    "deerfield beach", "deerfield", "pompano", "coral springs",
    "coconut creek", "margate", "parkland", "lighthouse point",
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check_eligibility(address: str) -> dict:
    """
    Returns a dict matching the Vapi tool output schema:
      eligible, normalized_address, distance_miles,
      estimated_drive_time, reason
    Raises ValueError for unresolvable addresses.
    Raises RuntimeError for unexpected API failures.
    """
    if GOOGLE_MAPS_API_KEY:
        return _google_mode(address)
    return _nominatim_mode(address)


# ---------------------------------------------------------------------------
# Mode A — Google Maps Distance Matrix
# ---------------------------------------------------------------------------

def _google_mode(address: str) -> dict:
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins":      RESTAURANT_ADDRESS,
        "destinations": address,
        "units":        "imperial",
        "key":          GOOGLE_MAPS_API_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=6)
        resp.raise_for_status()
    except requests.Timeout:
        raise RuntimeError("Google Maps request timed out")
    except requests.RequestException as e:
        raise RuntimeError(f"Google Maps request failed: {e}")

    data = resp.json()

    if data.get("status") != "OK":
        raise RuntimeError(f"Google Maps API returned status: {data.get('status')}")

    element = data["rows"][0]["elements"][0]
    elem_status = element.get("status")

    if elem_status == "NOT_FOUND":
        raise ValueError(f"Address not found: '{address}'")
    if elem_status == "ZERO_RESULTS":
        raise ValueError(f"No route found to: '{address}'")
    if elem_status != "OK":
        raise RuntimeError(f"Distance Matrix element status: {elem_status}")

    # Driving distance in miles (API returns meters)
    distance_miles = round(element["distance"]["value"] / 1609.34, 1)

    # Human-readable drive time, e.g. "14 mins"
    drive_time = element["duration"]["text"]

    # Cleaned-up destination address from Google
    normalized = (data.get("destination_addresses") or [address])[0]

    eligible = distance_miles <= DELIVERY_RADIUS_MILES

    return {
        "eligible":             eligible,
        "raw_address":          address,
        "normalized_address":   normalized,
        "address_confidence":   "high",
        "distance_miles":       distance_miles,
        "estimated_drive_time": drive_time,
        "reason":               None if eligible else "outside_delivery_area",
    }


# ---------------------------------------------------------------------------
# Mode B — OpenStreetMap Nominatim + OSRM (no API key required)
# ---------------------------------------------------------------------------

# If geocoding returns a point more than this far from the restaurant,
# the geocoder found the wrong place — treat it as a soft pass.
_MAX_PLAUSIBLE_MILES = 50.0

# Common first-name patterns prepended to addresses by callers/staff
# e.g. "Ralph 13623 Via Aurora A" → "13623 Via Aurora A"
_NAME_PREFIX_RE = re.compile(
    r"^[A-Z][a-z]{1,14}\s+(?=\d)",   # single capitalized word before a house number
)


def _strip_name_prefix(address: str) -> str:
    """Remove a leading person name before a street number, if present."""
    return _NAME_PREFIX_RE.sub("", address)


def _inject_local_context(address: str) -> str:
    """
    When a customer says "123 Main Street" with no city, Nominatim can't guess
    which city they mean.  If the address has no Florida / local area signal,
    append the restaurant's city so geocoding succeeds.
    """
    lower = address.lower()
    if any(m in lower for m in _FL_MARKERS):
        return address          # already has local context
    return f"{address}, Delray Beach, FL"


# Cities / keywords that are clearly outside the delivery area.
# If the spoken address contains one of these, it's a hard fail.
_OUT_OF_AREA_MARKERS = (
    "miami", "fort lauderdale", "hollywood", "miramar", "pembroke pines",
    "plantation", "davie", "sunrise", "weston", "cooper city", "hallandale",
    "dania", "hialeah", "homestead", "kendall", "aventura", "bal harbour",
    "orlando", "tampa", "jacksonville", "naples", "sarasota", "gainesville",
    "tallahassee", "clearwater", "st. pete", "st pete", "saint pete",
    "west palm beach", "palm beach gardens", "jupiter", "tequesta",
    "stuart", "port st lucie", "port saint lucie", "vero beach",
    "fort pierce", "okeechobee",
)


def _is_clearly_out_of_area(address: str) -> bool:
    """Return True only when the address unambiguously names a city outside the
    delivery zone.  Partial or ambiguous addresses return False so we give the
    benefit of the doubt."""
    lower = address.lower()
    return any(marker in lower for marker in _OUT_OF_AREA_MARKERS)


def _nominatim_mode(address: str) -> dict:
    """
    Free mode — real driving routes via OSRM (no API key required).

    Validation philosophy (local pizza restaurant rules):
      HARD FAIL  — address clearly names a city outside our delivery area
      SOFT PASS  — geocoding fails but address looks local; trust the driver
      FULL CHECK — geocoding succeeds; use actual distance

    This means a partial address like "399 Piedmont" will SOFT PASS rather
    than forcing the caller to spell out a USPS-perfect address.
    """
    # ── Hard fail: caller explicitly named a city we don't serve ──────────────
    if _is_clearly_out_of_area(address):
        return {
            "eligible":           False,
            "raw_address":        address,
            "normalized_address": address,
            "address_confidence": "high",
            "distance_miles":     None,
            "estimated_drive_time": None,
            "reason":             "outside_delivery_area",
        }

    # Strip leading person-name prefix before geocoding (e.g. "Ralph 13623 Via Aurora A")
    clean = _strip_name_prefix(address)
    enriched = _inject_local_context(clean)

    # Four-pass geocoding strategy
    result = _nominatim_geocode(enriched)
    if result is None and enriched != clean:
        result = _nominatim_geocode(clean)
    if result is None and clean != address:
        result = _nominatim_geocode(_inject_local_context(address))
    if result is None:
        no_num = re.sub(r"^\d+\s+", "", enriched)
        if no_num != enriched:
            result = _nominatim_geocode(no_num)
    if result is None:
        parts = [p.strip() for p in enriched.split(",")]
        if len(parts) >= 2:
            result = _nominatim_geocode(", ".join(parts[-2:]))

    # Sanity check — if geocoder found something more than 50 miles away it's
    # probably the wrong "Waterford" or "Springfield".  Treat as soft pass.
    if result is not None:
        sanity_dist = _haversine_miles(RESTAURANT_LAT, RESTAURANT_LNG, result[0], result[1])
        if sanity_dist > _MAX_PLAUSIBLE_MILES:
            result = None   # discard bogus geocode → fall through to soft pass

    # ── Soft pass: geocoding failed but address doesn't look out-of-area ──────
    if result is None:
        return {
            "eligible":             True,
            "raw_address":          address,
            "normalized_address":   enriched,   # enriched = address + ", Delray Beach, FL"
            "address_confidence":   "low",
            "distance_miles":       None,
            "estimated_drive_time": None,
            "reason":               None,
            "note": (
                "Address could not be geocoded but appears local. "
                "Accepting for delivery — driver will confirm on arrival."
            ),
        }

    cust_lat, cust_lng = result

    # ── Full distance check via OSRM / Haversine ──────────────────────────────
    osrm_result = _osrm_route(RESTAURANT_LNG, RESTAURANT_LAT, cust_lng, cust_lat)
    if osrm_result:
        distance_miles = osrm_result["distance_miles"]
        drive_time     = osrm_result["drive_time"]
    else:
        straight_miles = _haversine_miles(RESTAURANT_LAT, RESTAURANT_LNG, cust_lat, cust_lng)
        road_miles     = straight_miles * 1.3
        drive_time_min = max(1, round(road_miles / 25 * 60))
        drive_time     = f"~{drive_time_min} mins"
        distance_miles = round(road_miles, 1)

    eligible   = distance_miles <= DELIVERY_RADIUS_MILES
    normalized = enriched if enriched != address else address

    return {
        "eligible":             eligible,
        "raw_address":          address,
        "normalized_address":   normalized,
        "address_confidence":   "high",
        "distance_miles":       distance_miles,
        "estimated_drive_time": drive_time,
        "reason":               None if eligible else "outside_delivery_area",
    }


def _osrm_route(orig_lng: float, orig_lat: float,
                dest_lng: float, dest_lat: float) -> Optional[dict]:
    """
    Call the public OSRM routing API for real driving distance + duration.
    Returns {"distance_miles": float, "drive_time": str} or None on failure.
    OSRM is free, open-source, and uses real OpenStreetMap road data.
    """
    url = (
        f"http://router.project-osrm.org/route/v1/driving/"
        f"{orig_lng},{orig_lat};{dest_lng},{dest_lat}"
        f"?overview=false&steps=false"
    )
    headers = {"User-Agent": "restaurant-voice-agent/1.0 (delivery-eligibility-check)"}

    try:
        resp = requests.get(url, headers=headers, timeout=6)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None  # fall back to haversine

    if data.get("code") != "Ok" or not data.get("routes"):
        return None

    route = data["routes"][0]
    distance_meters  = route["distance"]    # metres
    duration_seconds = route["duration"]    # seconds

    distance_miles = round(distance_meters / 1609.34, 1)
    drive_time_min = max(1, round(duration_seconds / 60))
    drive_time     = f"~{drive_time_min} mins"

    return {"distance_miles": distance_miles, "drive_time": drive_time}


def _nominatim_geocode(query: str):
    """
    Returns (lat, lng) tuple if found, or None if no results.
    Raises RuntimeError on network / HTTP failures.

    Uses countrycodes=us (US only) and a South Florida viewbox to bias results
    toward the Boca Raton / Delray Beach area without hard-restricting to it.
    bounded=0 means Nominatim prefers the viewbox area but will still return
    a result outside it if nothing is found inside.
    """
    encoded = urllib.parse.quote(query)
    url = (
        f"https://nominatim.openstreetmap.org/search"
        f"?q={encoded}"
        f"&format=json"
        f"&limit=1"
        f"&countrycodes=us"
        f"&viewbox={_NOMINATIM_VIEWBOX}"
        f"&bounded=0"
    )
    headers = {"User-Agent": "restaurant-voice-agent/1.0 (delivery-eligibility-check)"}

    try:
        resp = requests.get(url, headers=headers, timeout=6)
        # 429 = rate-limited; treat as "no result" so caller falls through to soft-pass
        if resp.status_code == 429:
            return None
        resp.raise_for_status()
    except requests.Timeout:
        return None   # timeout → soft-pass, don't block the order
    except requests.RequestException as e:
        raise RuntimeError(f"Nominatim request failed: {e}")

    results = resp.json()
    if not results:
        return None

    return float(results[0]["lat"]), float(results[0]["lon"])


# ---------------------------------------------------------------------------
# Haversine formula — straight-line distance between two lat/lng points
# ---------------------------------------------------------------------------

def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R      = 3958.8  # Earth radius in miles
    phi1   = math.radians(lat1)
    phi2   = math.radians(lat2)
    dphi   = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))
