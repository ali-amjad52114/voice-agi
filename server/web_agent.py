"""Web agent: one real part-price lookup. Never writes call-agent quotes."""

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

PART_QUERY = "2019 Toyota Camry front brake pads"
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
HTTP_FALLBACK_URL = (
    "https://shop.advanceautoparts.com/web/SearchResults"
    "?searchTerm=2019+Toyota+Camry+front+brake+pads"
)
# Pads/rotors kits are typically tens to a few hundred dollars. Reject junk.
_MIN_USD = 15.0
_MAX_USD = 500.0
_PRICE_RE = re.compile(r"\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)")
_JSONLD_PRICE_RE = re.compile(
    r'"price"\s*:\s*"?(?P<n>\d+(?:\.\d{1,2})?)"?',
    re.IGNORECASE,
)


def run_web_agent(task: Any) -> Agent:
    """Look up a 2019 Camry front brake-pad price. Returns a kind=web Agent.

    Uses one SerpAPI ``google_shopping`` search when ``SERPAPI_API_KEY`` is set,
    otherwise one simple HTTP fetch. Does not mutate call agents or invent a
    price: lookup failure → ``status=failed`` and no ``facts.partPrice``.
    """
    task_id = _task_id(task)
    agent_id = _existing_web_agent_id(task) or f"a_web_{uuid.uuid4().hex[:8]}"

    hit, err = _lookup_part_price()
    if hit is None:
        detail = f"Part price lookup failed: {err}" if err else "Part price lookup failed"
        return Agent(
            id=agent_id,
            taskId=task_id,
            kind="web",
            status="failed",
            business=Business(name="Parts lookup", type="parts"),
            summary=detail,
            facts=None,
        )

    price, source, url, parts_type = hit
    return Agent(
        id=agent_id,
        taskId=task_id,
        kind="web",
        status="done",
        business=Business(name=source, type="parts", url=url),
        summary=f"{PART_QUERY} ${price:g}",
        facts=Facts(partPrice=price, partsType=parts_type, confidence=0.75),
    )


def _lookup_part_price() -> tuple[tuple[float, str, str | None, str] | None, str | None]:
    api_key = (os.environ.get("SERPAPI_API_KEY") or "").strip()
    last_err: str | None = None
    if api_key:
        hit, last_err = _serpapi_shopping_once(api_key)
        if hit is not None:
            return hit, None
    hit = _http_fetch_once()
    if hit is not None:
        return hit, None
    return None, last_err or "no in-range shopping price"


def _serpapi_shopping_once(
    api_key: str,
) -> tuple[tuple[float, str, str | None, str] | None, str | None]:
    params = {
        "engine": "google_shopping",
        "q": PART_QUERY,
        "hl": "en",
        "gl": "us",
        "api_key": api_key,
    }
    payload, err = _http_json(f"{SERPAPI_ENDPOINT}?{urlencode(params)}")
    if not isinstance(payload, dict):
        return None, err or "SerpAPI shopping returned no JSON"
    if payload.get("error"):
        return None, str(payload.get("error"))
    results = payload.get("shopping_results")
    if not isinstance(results, list):
        return None, "SerpAPI shopping_results missing"
    for item in results:
        if not isinstance(item, dict):
            continue
        price = _coerce_price(item.get("extracted_price"))
        if price is None:
            price = _parse_price_text(item.get("price"))
        if price is None:
            continue
        title = str(item.get("title") or "")
        source = str(item.get("source") or "Google Shopping").strip() or "Google Shopping"
        url = item.get("link") or item.get("product_link")
        url = str(url) if url else None
        return (price, source, url, _parts_type(title)), None
    return None, "no shopping result in $15–$500 range"


def _http_fetch_once() -> tuple[float, str, str | None, str] | None:
    html, _err = _http_text(HTTP_FALLBACK_URL)
    if not html:
        return None
    for match in _JSONLD_PRICE_RE.finditer(html):
        price = _coerce_price(match.group("n"))
        if price is not None:
            return price, "Advance Auto Parts", HTTP_FALLBACK_URL, "aftermarket"
    for match in _PRICE_RE.finditer(html):
        price = _coerce_price(match.group(1))
        if price is not None:
            return price, "Advance Auto Parts", HTTP_FALLBACK_URL, "aftermarket"
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
    lowered = title.lower()
    if "oem" in lowered or "genuine" in lowered:
        return "oem"
    return "aftermarket"


def _task_id(task: Any) -> str:
    if task is None:
        return "unknown"
    if isinstance(task, dict):
        return str(task.get("id") or "unknown")
    return str(getattr(task, "id", None) or "unknown")


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
