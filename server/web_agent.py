"""Web agent: one real part-price lookup per task. Never writes call-agent quotes.

Session 3 (``docs/multi-session-plan.md``):

* the query is built from the task request (vehicle from the planner's offline
  regex plan plus the job), never hardcoded unless no vehicle is found;
* exactly one SerpAPI ``google_shopping`` call per task, never more;
* up to three in-range results become separate ``kind="web"`` agents;
* zero results yields one ``status="failed"`` agent with no ``partPrice``.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:
    _here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_here, ".env"), override=False)
    load_dotenv(os.path.join(os.path.dirname(_here), ".env"), override=False)

try:
    from .models import Agent, Business, Facts
except ImportError:  # pragma: no cover
    try:
        from models import Agent, Business, Facts
    except ImportError:
        from server.models import Agent, Business, Facts

try:
    from . import planner as _planner
except ImportError:  # pragma: no cover
    try:
        import planner as _planner  # type: ignore[no-redef]
    except ImportError:
        try:
            from server import planner as _planner  # type: ignore[no-redef]
        except ImportError:
            _planner = None  # type: ignore[assignment]

# Old hardcoded query. Used only when the request names no vehicle.
DEFAULT_PART_QUERY = "2019 Toyota Camry front brake pads"
PART_QUERY = DEFAULT_PART_QUERY  # backward-compatible name

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
HTTP_FALLBACK_BASE = "https://shop.advanceautoparts.com/web/SearchResults"
HTTP_FALLBACK_URL = f"{HTTP_FALLBACK_BASE}?{urlencode({'searchTerm': DEFAULT_PART_QUERY})}"

# Sane range for a pads + rotors kit. Anything outside is junk (a single
# clip, a full caliper set, a shipping quote) and is dropped, never used.
_MIN_USD = 60.0
_MAX_USD = 600.0
MAX_SOURCES = 3

_PRICE_RE = re.compile(r"\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)")
_JSONLD_PRICE_RE = re.compile(
    r'"price"\s*:\s*"?(?P<n>\d+(?:\.\d{1,2})?)"?',
    re.IGNORECASE,
)
_OEM_RE = re.compile(r"\b(?:oem|genuine|toyota)\b", re.IGNORECASE)

_BRAKE_JOB = "brake pads and rotors"
_BRAKE_LABEL = "pads + rotors"
_GENERIC_JOB = "replacement parts"
_GENERIC_LABEL = "part"


# --------------------------------------------------------------------------- #
# 3A — query
# --------------------------------------------------------------------------- #


def build_part_query(request: str) -> str:
    """Pure: spoken request → shopping query.

    ``"front brakes on my 2019 Camry"`` → ``"2019 Camry front brake pads and
    rotors kit"``. "front"/"rear" appear only when spoken. Falls back to the
    old hardcoded string only when no vehicle can be found.
    """
    vehicle = _vehicle_from_request(request)
    if not vehicle:
        return DEFAULT_PART_QUERY
    return f"{_with_make(vehicle)} {_job_from_request(request)}"


# Google Shopping needs the make: "2019 Camry ..." returned nothing while
# "2019 Toyota Camry ..." returned 40 results (measured 2026-09-19).
_MAKE_BY_MODEL = {
    "camry": "Toyota", "corolla": "Toyota", "rav4": "Toyota", "prius": "Toyota", "tacoma": "Toyota",
    "highlander": "Toyota", "sienna": "Toyota", "tundra": "Toyota", "4runner": "Toyota",
    "civic": "Honda", "accord": "Honda", "cr-v": "Honda", "crv": "Honda", "pilot": "Honda", "odyssey": "Honda",
    "altima": "Nissan", "sentra": "Nissan", "rogue": "Nissan", "maxima": "Nissan",
    "f-150": "Ford", "f150": "Ford", "escape": "Ford", "explorer": "Ford", "focus": "Ford", "fusion": "Ford", "mustang": "Ford",
    "silverado": "Chevrolet", "malibu": "Chevrolet", "equinox": "Chevrolet", "tahoe": "Chevrolet", "cruze": "Chevrolet",
    "elantra": "Hyundai", "sonata": "Hyundai", "tucson": "Hyundai", "santa fe": "Hyundai",
    "optima": "Kia", "sorento": "Kia", "forte": "Kia", "sportage": "Kia",
    "outback": "Subaru", "forester": "Subaru", "impreza": "Subaru", "crosstrek": "Subaru",
    "jetta": "Volkswagen", "passat": "Volkswagen", "golf": "Volkswagen", "tiguan": "Volkswagen",
    "mazda3": "Mazda", "cx-5": "Mazda", "mazda6": "Mazda",
    "3 series": "BMW", "c-class": "Mercedes-Benz", "a4": "Audi",
}
_KNOWN_MAKES = {m.lower() for m in _MAKE_BY_MODEL.values()} | {"toyota", "honda", "nissan", "ford", "chevy", "chevrolet", "hyundai", "kia", "subaru", "volkswagen", "vw", "mazda", "bmw", "mercedes", "audi", "lexus", "acura", "jeep", "dodge", "ram", "gmc", "tesla", "volvo"}


def _with_make(vehicle: str) -> str:
    """Insert the make after the year when the spoken vehicle lacks one."""
    words = vehicle.split()
    lower = [w.lower() for w in words]
    if any(w in _KNOWN_MAKES for w in lower):
        return vehicle
    for i, w in enumerate(lower):
        make = _MAKE_BY_MODEL.get(w)
        if make:
            return " ".join(words[:i] + [make] + words[i:])
    return vehicle


def _vehicle_from_request(request: str) -> str | None:
    text = (request or "").strip()
    if not text:
        return None
    fn = getattr(_planner, "_offline_plan", None) if _planner is not None else None
    if not callable(fn):
        return None
    try:
        plan = fn(text, None)
    except TypeError:
        plan = fn(text)
    except Exception:
        return None
    vehicle = plan.get("vehicle") if isinstance(plan, dict) else None
    vehicle = str(vehicle).strip() if vehicle else ""
    return vehicle or None


def _is_brake_job(request: str) -> bool:
    return "brake" in (request or "").lower()


def _job_from_request(request: str) -> str:
    lower = (request or "").lower()
    if not _is_brake_job(lower):
        return _GENERIC_JOB
    if "front" in lower:
        return f"front {_BRAKE_JOB}"
    if "rear" in lower:
        return f"rear {_BRAKE_JOB}"
    return _BRAKE_JOB


def _job_label(request: str) -> str:
    return _BRAKE_LABEL if _is_brake_job(request) else _GENERIC_LABEL


# --------------------------------------------------------------------------- #
# 3B — sources
# --------------------------------------------------------------------------- #


def run_web_agent(task: Any) -> list[Agent]:
    """Look up the part named by the task. Returns one ``kind=web`` Agent per source.

    One SerpAPI ``google_shopping`` search when ``SERPAPI_API_KEY`` is set,
    otherwise one simple HTTP fetch. Never mutates call agents and never
    invents a price: no usable result → a single ``status=failed`` agent with
    ``facts=None`` and a summary saying why.
    """
    task_id = _task_id(task)
    request = _task_request(task)
    query = build_part_query(request)
    label = _job_label(request)
    slot_id = _existing_web_agent_id(task)

    hits, err = _lookup_part_prices(query)
    if not hits:
        reason = err or "no in-range shopping price"
        return [
            Agent(
                id=slot_id or _new_agent_id(),
                taskId=task_id,
                kind="web",
                status="failed",
                business=Business(name="Parts lookup", type="parts"),
                summary=f"No part found online for '{query}': {reason}",
                facts=None,
            )
        ]

    agents: list[Agent] = []
    for index, (price, source, url, parts_type) in enumerate(hits[:MAX_SOURCES]):
        agent_id = slot_id if (index == 0 and slot_id) else _new_agent_id()
        kind_label = "OEM" if parts_type == "oem" else "Aftermarket"
        agents.append(
            Agent(
                id=agent_id,
                taskId=task_id,
                kind="web",
                status="done",
                business=Business(name=source, type="parts", url=url),
                summary=f"{kind_label} {label} ${price:g}",
                facts=Facts(partPrice=price, partsType=parts_type, confidence=0.75),
            )
        )
    return agents


def run_web_agent_single(task: Any) -> Agent:
    """Backward-compatible single-agent shape: the first (best) source."""
    return run_web_agent(task)[0]


Hit = tuple[float, str, str | None, str]


def _lookup_part_prices(query: str) -> tuple[list[Hit], str | None]:
    api_key = (os.environ.get("SERPAPI_API_KEY") or "").strip()
    last_err: str | None = None
    if api_key:
        hits, last_err = _serpapi_shopping_once(api_key, query)
        if hits:
            return hits, None
    hit = _http_fetch_once(query)
    if hit is not None:
        return [hit], None
    return [], last_err or "no in-range shopping price"


def _serpapi_shopping_once(api_key: str, query: str) -> tuple[list[Hit], str | None]:
    """Exactly one ``google_shopping`` request. Returns every in-range result."""
    params = {
        "engine": "google_shopping",
        "q": query,
        "hl": "en",
        "gl": "us",
        "api_key": api_key,
    }
    payload, err = _http_json(f"{SERPAPI_ENDPOINT}?{urlencode(params)}")
    if not isinstance(payload, dict):
        return [], err or "SerpAPI shopping returned no JSON"
    if payload.get("error"):
        return [], str(payload.get("error"))
    results = payload.get("shopping_results")
    if not isinstance(results, list):
        return [], "SerpAPI shopping_results missing"
    hits: list[Hit] = []
    seen_keys: set[tuple[str, float]] = set()
    seen_titles: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        price = _coerce_price(item.get("extracted_price"))
        if price is None:
            price = _parse_price_text(item.get("price"))
        if price is None:
            continue
        title = str(item.get("title") or "")
        if not _title_relevant(title, query):
            continue
        source = str(item.get("source") or "Google Shopping").strip() or "Google Shopping"
        url = item.get("link") or item.get("product_link")
        url = str(url) if url else None
        key = (source.lower(), round(price, 2))
        norm_title = " ".join(title.lower().split())
        if key in seen_keys or norm_title in seen_titles:
            continue
        seen_keys.add(key)
        seen_titles.add(norm_title)
        hits.append((price, source, url, _parts_type(title)))
        if len(hits) >= MAX_SOURCES:
            break
    if not hits:
        return [], f"no shopping result in ${_MIN_USD:g}–${_MAX_USD:g} range"
    return hits, None


def _http_fetch_once(query: str) -> Hit | None:
    url = f"{HTTP_FALLBACK_BASE}?{urlencode({'searchTerm': query})}"
    html, _err = _http_text(url)
    if not html:
        return None
    for match in _JSONLD_PRICE_RE.finditer(html):
        price = _coerce_price(match.group("n"))
        if price is not None:
            return price, "Advance Auto Parts", url, "aftermarket"
    for match in _PRICE_RE.finditer(html):
        price = _coerce_price(match.group(1))
        if price is not None:
            return price, "Advance Auto Parts", url, "aftermarket"
    return None


def _http_json(url: str) -> tuple[Any | None, str | None]:
    text, err = _http_text(url)
    if not text:
        return None, err
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, "SerpAPI returned non-JSON"


def _http_text(url: str) -> tuple[str | None, str | None]:
    req = Request(
        url,
        headers={
            "User-Agent": "VoiceAGI/1.0 (part-price lookup)",
            "Accept": "application/json,text/html",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=20) as resp:
            if getattr(resp, "status", 200) >= 400:
                return None, f"HTTP {getattr(resp, 'status', '?')}"
            raw = resp.read(2_000_000)
    except HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        return None, str(exc.reason if hasattr(exc, "reason") else exc)
    return raw.decode("utf-8", errors="replace"), None


def _coerce_price(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        price = float(value)
    else:
        text = str(value).strip().replace(",", "").replace("$", "")
        try:
            price = float(text)
        except ValueError:
            return None
    if price < _MIN_USD or price > _MAX_USD:
        return None
    return round(price, 2)


def _parse_price_text(value: Any) -> float | None:
    if value is None:
        return None
    match = _PRICE_RE.search(str(value))
    if not match:
        return _coerce_price(value)
    return _coerce_price(match.group(1))


def _parts_type(title: str) -> str:
    """genuine / OEM / Toyota in the title → oem, else aftermarket."""
    return "oem" if _OEM_RE.search(title or "") else "aftermarket"


def _new_agent_id() -> str:
    return f"a_web_{uuid.uuid4().hex[:8]}"


def _task_id(task: Any) -> str:
    if task is None:
        return "unknown"
    if isinstance(task, dict):
        return str(task.get("id") or "unknown")
    return str(getattr(task, "id", None) or "unknown")


def _task_request(task: Any) -> str:
    if task is None:
        return ""
    if isinstance(task, dict):
        return str(task.get("request") or "")
    return str(getattr(task, "request", None) or "")


def _existing_web_agent_id(task: Any) -> str | None:
    """Reuse a queued web slot. Never read/write call-agent facts."""
    agents = _agents(task)
    for agent in agents:
        kind = agent.get("kind") if isinstance(agent, dict) else getattr(agent, "kind", None)
        if kind != "web":
            continue
        agent_id = agent.get("id") if isinstance(agent, dict) else getattr(agent, "id", None)
        if agent_id:
            return str(agent_id)
    return None


def _agents(task: Any) -> list[Any]:
    if task is None:
        return []
    if isinstance(task, dict):
        raw = task.get("agents") or []
    else:
        raw = getattr(task, "agents", None) or []
    return list(raw)


# --------------------------------------------------------------------------- #
# Session 9 — General Compute tool wrapper
# --------------------------------------------------------------------------- #

TOOL_PART_KEYS = ("seller", "url", "price", "partsType")


def lookup_part_tool(query: str, *, limit: int = MAX_SOURCES) -> list[dict[str, Any]]:
    """``lookup_part`` tool for the Gemma loop: the same one-shot lookup, fewer keys.

    Returns at most ``limit`` results as ``{"seller", "url", "price",
    "partsType"}``. ``price`` is the looked-up float (never made up); a
    result without a usable price is dropped, and no result means ``[]``.
    """
    query = str(query or "").strip() or DEFAULT_PART_QUERY
    hits, _err = _lookup_part_prices(query)
    out: list[dict[str, Any]] = []
    for hit in hits or []:
        try:
            price, source, url, parts_type = hit
        except (TypeError, ValueError):
            continue
        price = _coerce_price(price)
        if price is None:
            continue
        out.append(
            {
                "seller": str(source or "").strip() or "Google Shopping",
                "url": str(url) if url else None,
                "price": float(price),
                "partsType": "oem" if parts_type == "oem" else "aftermarket",
            }
        )
        if len(out) >= max(1, int(limit)):
            break
    return out


def _title_relevant(title: str, query: str) -> bool:
    """Drop results for the wrong axle or the wrong model.

    Shopping results for "front brake pads and rotors" included rear rotor
    sets and RAV4 parts. Require the model word from the query when the
    query has one, and reject the opposite axle.
    """
    t = title.lower()
    q = query.lower()
    if "front" in q and "rear" in t and "front" not in t:
        return False
    if "rear" in q and "front" in t and "rear" not in t:
        return False
    model = next((w for w in q.split() if w in _MAKE_BY_MODEL), None)
    if model:
        # Reject only titles that name a different known model; a title with
        # no model word at all (e.g. a generic kit listing) is kept.
        others = [m for m in _MAKE_BY_MODEL if m != model and m in t]
        if others and model not in t:
            return False
    if "brake" in q:
        # A $127 "Disc Brake Caliper" was being shown as "pads + rotors".
        # The listing must be pads, rotors, or a pads-and-rotors kit.
        if not any(w in t for w in ("pad", "rotor", "brake kit")):
            return False
        if any(w in t for w in _NOT_THE_PART):
            return False
    return True


_NOT_THE_PART = (
    "caliper", "clip", "hose", "sensor", "hardware kit", "bleeder", "shim",
    "wear indicator", "retaining", "spring", "bracket", "line", "fluid",
)
