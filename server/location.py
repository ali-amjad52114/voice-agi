"""Resolve Task.location from browser GPS or a spoken city (Nominatim only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "Voice-AGI"


def resolve_location(
    lat: float | None = None,
    lng: float | None = None,
    location_str: str | None = None,
) -> dict[str, Any]:
    """Return ``{lat, lng, label}``. Prefer coords; else geocode ``location_str``.

    Uses Nominatim (no API key). Does not call SerpAPI.
    """
    city = (location_str or "").strip() or None
    if lat is not None and lng is not None:
        return {
            "lat": float(lat),
            "lng": float(lng),
            "label": city or f"{float(lat)}, {float(lng)}",
        }
    if not city:
        raise ValueError("Need lat/lng or a location string to resolve")
    return _geocode_nominatim(city)


def _geocode_nominatim(query: str) -> dict[str, Any]:
    params = urllib.parse.urlencode({"q": query, "format": "json", "limit": "1"})
    req = urllib.request.Request(
        f"{NOMINATIM_SEARCH}?{params}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Nominatim geocode failed for {query!r}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Nominatim geocode failed for {query!r}") from exc

    if not payload:
        raise ValueError(f"Nominatim found no results for {query!r}")

    hit = payload[0]
    display = (hit.get("display_name") or "").strip() or query
    return {
        "lat": float(hit["lat"]),
        "lng": float(hit["lon"]),
        "label": display,
    }
