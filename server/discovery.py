"""Nearby shops via one SerpAPI google_maps search. No prices. Never writes shops.json."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Literal, TypedDict

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
RESULT_CAP = 8
MAPS_ZOOM = "14z"

ShopType = Literal["mechanic", "dealer"]


class ShopHit(TypedDict):
    name: str
    phone: str
    address: str
    url: str
    type: ShopType


_DEALER_NEEDLES = (
    "dealer",
    "dealership",
    "car dealer",
    "auto dealer",
)
_PARTS_NEEDLES = (
    "auto parts",
    "parts store",
    "auto_parts",
    "parts shop",
)


def maps_query(utterance: str | None = None) -> str:
    """One query string: brake repair if spoken, else car repair."""
    text = (utterance or "").lower()
    if "brake" in text:
        return "brake repair"
    return "car repair"


def discover_shops(
    lat: float,
    lng: float,
    utterance: str | None = None,
) -> list[ShopHit]:
    """One google_maps search near lat/lng. Skip no-phone. Cap 8. No price fields."""
    api_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("SERPAPI_API_KEY is not set")

    payload = _serpapi_maps_once(api_key, lat, lng, maps_query(utterance))
    raw = payload.get("local_results") or []
    if not isinstance(raw, list):
        return []

    shops: list[ShopHit] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        mapped = map_local_result(row)
        if mapped is None:
            continue
        shops.append(mapped)
        if len(shops) >= RESULT_CAP:
            break
    return shops


def map_local_result(row: dict[str, Any]) -> ShopHit | None:
    """SerpAPI local_result → shop dict. Drops no-phone, parts stores, and all price keys."""
    name = str(row.get("title") or "").strip()
    phone = _to_e164(str(row.get("phone") or "").strip())
    if not name or not phone:
        return None
    if _is_parts_store(row):
        return None
    return {
        "name": name,
        "phone": phone,
        "address": str(row.get("address") or "").strip(),
        "url": _shop_url(row),
        "type": _shop_type(row),
    }


def _serpapi_maps_once(api_key: str, lat: float, lng: float, query: str) -> dict[str, Any]:
    params = urllib.parse.urlencode(
        {
            "engine": "google_maps",
            "type": "search",
            "q": query,
            "ll": f"@{lat},{lng},{MAPS_ZOOM}",
            "hl": "en",
            "api_key": api_key,
        }
    )
    req = urllib.request.Request(
        f"{SERPAPI_ENDPOINT}?{params}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"SerpAPI google_maps failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("SerpAPI google_maps request failed") from exc

    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("SerpAPI google_maps returned a non-object")
    return data


def _to_e164(raw: str) -> str:
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return ""
    if raw.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else ""


def _shop_url(row: dict[str, Any]) -> str:
    website = str(row.get("website") or "").strip()
    if website:
        return website
    links = row.get("links")
    if isinstance(links, dict):
        nested = str(links.get("website") or "").strip()
        if nested:
            return nested
    place_id = str(row.get("place_id") or "").strip()
    if place_id:
        return f"https://www.google.com/maps/place/?q=place_id:{place_id}"
    return ""


def _blob(row: dict[str, Any]) -> str:
    bits: list[str] = [
        str(row.get("title") or ""),
        str(row.get("type") or ""),
        str(row.get("type_id") or ""),
    ]
    types = row.get("types")
    if isinstance(types, list):
        bits.extend(str(t) for t in types)
    type_ids = row.get("type_ids")
    if isinstance(type_ids, list):
        bits.extend(str(t) for t in type_ids)
    return " ".join(bits).lower()


def _is_parts_store(row: dict[str, Any]) -> bool:
    blob = _blob(row)
    return any(needle in blob for needle in _PARTS_NEEDLES)


def _shop_type(row: dict[str, Any]) -> ShopType:
    blob = _blob(row)
    if any(needle in blob for needle in _DEALER_NEEDLES):
        return "dealer"
    return "mechanic"


# --------------------------------------------------------------------------- #
# Session 9 — General Compute tool wrapper
# --------------------------------------------------------------------------- #

TOOL_SHOP_KEYS = ("name", "phone", "url", "type")


def discover_shops_tool(
    lat: float,
    lng: float,
    query: str,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """``discover_shops`` tool for the Gemma loop: same search, fewer keys.

    Returns at most ``limit`` shops as ``{"name", "phone", "url", "type"}``
    (``url`` is ``None`` when SerpAPI had none). No address, no prices, no
    invented entries: everything comes straight from ``discover_shops``.
    """
    hits = discover_shops(float(lat), float(lng), query)
    out: list[dict[str, Any]] = []
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        name = str(hit.get("name") or "").strip()
        phone = str(hit.get("phone") or "").strip()
        if not name or not phone:
            continue
        url = str(hit.get("url") or "").strip() or None
        out.append({"name": name, "phone": phone, "url": url, "type": hit.get("type") or "mechanic"})
        if len(out) >= max(1, int(limit)):
            break
    return out
