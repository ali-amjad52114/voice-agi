"""Utterance → call plan. LLM first; regex fallback for the Camry demo."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from server import lang as _lang
except ImportError:  # pragma: no cover
    from . import lang as _lang  # type: ignore[no-redef]

try:
    from server.llm import complete
except ImportError:  # pragma: no cover - script / missing Session 1 module
    try:
        from .llm import complete
    except ImportError:

        def complete(
            system: str,
            user: str,
            json_schema: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> str:
            raise RuntimeError("server.llm.complete is not available")

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "planner.md"

_FACTS_NEEDED = [
    "allInPrice",
    "laborRatePerHour",
    "laborHours",
    "acceptsCustomerParts",
    "partsType",
    "warrantyMonths",
    "earliestSlot",
    "partPrice",
]

_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["confidence"],
    "properties": {
        "allInPrice": {"type": ["number", "null"]},
        "laborRatePerHour": {"type": ["number", "null"]},
        "laborHours": {"type": ["number", "null"]},
        "acceptsCustomerParts": {"type": ["boolean", "null"]},
        "partsType": {
            "anyOf": [
                {"type": "string", "enum": ["oem", "aftermarket"]},
                {"type": "null"},
            ]
        },
        "warrantyMonths": {"type": ["integer", "null"]},
        "earliestSlot": {"type": ["string", "null"]},
        "partPrice": {"type": ["number", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title",
        "userQuote",
        "vehicle",
        "location",
        "factsNeeded",
        "businessCount",
        "callScript",
        "extractionSchema",
        "language",
    ],
    "properties": {
        "language": {"type": "string", "enum": list(_lang.SUPPORTED)},
        "title": {"type": "string"},
        "userQuote": {"type": ["number", "null"]},
        "vehicle": {"type": ["string", "null"]},
        "location": {"type": ["string", "null"]},
        "factsNeeded": {"type": "array", "items": {"type": "string"}},
        "businessCount": {"type": "integer", "minimum": 5, "maximum": 8},
        "callScript": {"type": "string"},
        "extractionSchema": {"type": "object"},
    },
}

_YEAR_MODEL = re.compile(
    r"\b((?:19|20)\d{2})\s+([A-Za-z][A-Za-z0-9\-]+)\b",
    re.IGNORECASE,
)
_QUOTE = re.compile(
    r"(?:\$\s*|(?:quoted|quote|charging|charged)\s+(?:me\s+)?)(\d{1,3}(?:,\d{3})*(?:\.\d+)?)"
)
_IN_CITY = re.compile(
    r"\b(?:i'?m\s+in|in)\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2})\b"
)


def _system_prompt() -> str:
    if _PROMPT_PATH.is_file():
        return _PROMPT_PATH.read_text(encoding="utf-8")
    return "Turn the utterance into a JSON call plan."


def _call_script(vehicle: str | None, job: str) -> str:
    car = vehicle or "the customer's vehicle"
    return (
        f"Hi, I'm an assistant calling for a customer with a {car}. "
        f"Could I get an all-in installed price for {job}? "
        "I also need your hourly labor rate, whether you install "
        "customer-supplied parts, OEM or aftermarket, the warranty, "
        "and the earliest slot. I'm not booking today."
    )


def _clamp_business_count(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 8
    return max(5, min(8, n))


def _offline_plan(utterance: str, location: str | None) -> dict[str, Any]:
    text = utterance.strip()
    lower = text.lower()

    title = "Service request"
    job = "the work they described"
    if "brake" in lower:
        title = "Brake repair"
        job = "front pads and rotors" if "front" in lower else "brake service"

    vehicle = None
    year_model = _YEAR_MODEL.search(text)
    if year_model:
        vehicle = f"{year_model.group(1)} {year_model.group(2).title()}"
    elif "camry" in lower:
        vehicle = "2019 Camry" if "2019" in text else "Camry"

    user_quote: float | None = None
    quote_match = _QUOTE.search(text)
    if quote_match:
        user_quote = float(quote_match.group(1).replace(",", ""))

    city = (location or "").strip() or None
    if not city:
        city_match = _IN_CITY.search(text)
        if city_match:
            city = city_match.group(1).rstrip(".,")
        elif "fremont" in lower:
            city = "Fremont"

    if "camry" in lower and (user_quote == 800 or "800" in text):
        title = "Brake repair"
        vehicle = vehicle or "2019 Camry"
        city = city or "Fremont"
        if user_quote is None:
            user_quote = 800.0
        job = "front pads and rotors"

    return {
        "title": title,
        "userQuote": user_quote,
        "vehicle": vehicle,
        "location": city,
        "factsNeeded": list(_FACTS_NEEDED),
        "businessCount": 8,
        "callScript": _call_script(vehicle, job),
        "extractionSchema": dict(_EXTRACTION_SCHEMA),
        "language": _lang.detect_language(text),
    }


def _normalize(raw: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    plan = dict(fallback)
    if raw.get("title"):
        plan["title"] = str(raw["title"]).strip()
    if "userQuote" in raw and raw["userQuote"] is not None:
        try:
            plan["userQuote"] = float(raw["userQuote"])
        except (TypeError, ValueError):
            pass
    if raw.get("vehicle"):
        plan["vehicle"] = str(raw["vehicle"]).strip()
    if raw.get("location"):
        plan["location"] = str(raw["location"]).strip()
    facts = raw.get("factsNeeded")
    if isinstance(facts, list) and facts:
        plan["factsNeeded"] = [str(f) for f in facts]
    if "businessCount" in raw:
        plan["businessCount"] = _clamp_business_count(raw["businessCount"])
    if raw.get("callScript"):
        plan["callScript"] = str(raw["callScript"]).strip()
    schema = raw.get("extractionSchema")
    if isinstance(schema, dict) and schema:
        plan["extractionSchema"] = schema
    if raw.get("language"):
        plan["language"] = _lang.normalize(raw["language"])
    return plan


def _llm_plan(utterance: str, location: str | None) -> dict[str, Any] | None:
    user = f"Utterance:\n{utterance.strip()}\n"
    if location and location.strip():
        user += f"\nLocation hint: {location.strip()}\n"
    user += "\nReturn only the JSON object."
    try:
        text = complete(_system_prompt(), user, json_schema=_PLAN_JSON_SCHEMA, stage="planner", model=os.getenv("PLANNER_MODEL"))
    except Exception:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def plan(utterance: str, location: str | None = None) -> dict[str, Any]:
    """Build a planner dict from spoken text plus an optional city/label."""
    fallback = _offline_plan(utterance, location)
    llm = _llm_plan(utterance, location)
    if llm is None:
        return fallback
    return _normalize(llm, fallback)


if __name__ == "__main__":
    demo = (
        "One mechanic quoted me $800 for front brakes on my 2019 Camry, "
        "I'm in Fremont. Find the part price online, call mechanics and "
        "dealers near me, get their all-in price and their hourly labor "
        "rate, and tell me whether I should bring my own part or let "
        "them supply it."
    )
    print(json.dumps(plan(demo), indent=2))
