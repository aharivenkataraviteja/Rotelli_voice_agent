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
                                       "Rotelli Pizza & Pasta, Boca Raton, FL 33434")
RESTAURANT_LAT        = float(os.environ.get("RESTAURANT_LAT", "26.3887"))
RESTAURANT_LNG        = float(os.environ.get("RESTAURANT_LNG", "-80.1450"))
DELIVERY_RADIUS_MILES = float(os.environ.get("DELIVERY_RADIUS_MILES", "6.0"))


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
        "normalized_address":   normalized,
        "distance_miles":       distance_miles,
        "estimated_drive_time": drive_time,
        "reason":               None if eligible else "outside_delivery_area",
    }


# ---------------------------------------------------------------------------
# Mode B — OpenStreetMap Nominatim + Haversine (no API key required)
# ---------------------------------------------------------------------------

def _nominatim_mode(address: str) -> dict:
    """
    Free mode — real driving routes via OSRM (no API key required).

    Step 1: Geocode the customer address with Nominatim (OpenStreetMap).
            Three-pass progressive simplification if the full address fails.
    Step 2: Get real driving distance + duration from OSRM routing engine.
            Falls back to Haversine × 1.3 only if OSRM is unreachable.
    """
    result = _nominatim_geocode(address)

    if result is None:
        # Pass 2: drop leading house number
        simplified = re.sub(r"^\d+\s+", "", address)
        if simplified != address:
            result = _nominatim_geocode(simplified)

    if result is None:
        # Pass 3: keep only the last two comma-separated parts (city, state/zip)
        parts = [p.strip() for p in address.split(",")]
        if len(parts) >= 2:
            city_state = ", ".join(parts[-2:])
            result = _nominatim_geocode(city_state)

    if result is None:
        raise ValueError(f"Could not geocode address: '{address}'")

    cust_lat, cust_lng = result

    # Try OSRM for real driving distance first
    osrm_result = _osrm_route(RESTAURANT_LNG, RESTAURANT_LAT, cust_lng, cust_lat)

    if osrm_result:
        distance_miles = osrm_result["distance_miles"]
        drive_time     = osrm_result["drive_time"]
    else:
        # Fallback: Haversine straight-line × 1.3 road factor
        straight_miles = _haversine_miles(RESTAURANT_LAT, RESTAURANT_LNG, cust_lat, cust_lng)
        road_miles     = straight_miles * 1.3
        drive_time_min = max(1, round(road_miles / 25 * 60))
        drive_time     = f"~{drive_time_min} mins"
        distance_miles = round(road_miles, 1)

    eligible = distance_miles <= DELIVERY_RADIUS_MILES

    return {
        "eligible":             eligible,
        "normalized_address":   address,
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
    """
    encoded = urllib.parse.quote(query)
    url     = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1"
    headers = {"User-Agent": "restaurant-voice-agent/1.0 (delivery-eligibility-check)"}

    try:
        resp = requests.get(url, headers=headers, timeout=6)
        resp.raise_for_status()
    except requests.Timeout:
        raise RuntimeError("Nominatim geocoding request timed out")
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
