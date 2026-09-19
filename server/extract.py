"""Transcript → Facts JSON (frontend ``Facts`` + required ``confidence``).

Uses ``llm.complete`` when that helper is importable. Offline / LLM failure
returns confidence 0 and no price fields — never invent a dollar amount.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .models import Facts, TranscriptLine
except ImportError:  # pragma: no cover
    try:
        from models import Facts, TranscriptLine
    except ImportError:
        from server.models import Facts, TranscriptLine

try:
    from .llm import complete as _llm_complete
except ImportError:  # pragma: no cover
    try:
        from llm import complete as _llm_complete
    except ImportError:
        try:
            from server.llm import complete as _llm_complete
        except ImportError:
            _llm_complete = None

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "extract.md"

_PRICE_KEYS = ("allInPrice", "laborRatePerHour", "partPrice")

_FACTS_JSON_SCHEMA: dict[str, Any] = {
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


def extract_facts(
    transcript: Sequence[TranscriptLine | Mapping[str, Any] | str],
    schema: dict[str, Any] | None = None,
) -> Facts:
    """Turn a call/web transcript into ``Facts``. Never invents a dollar amount.

    If ``llm.complete`` is present, asks the model for Facts JSON. If it is
    missing or the call fails, returns ``confidence=0`` with empty prices.
    ``schema`` is an optional extra JSON Schema merged into the default Facts
    schema (planner extraction schema).
    """
    if _llm_complete is None or not _llm_key_present():
        return _offline_facts()

    system = _load_prompt()
    user = _format_transcript(transcript)
    json_schema = _merge_schema(schema)
    try:
        raw = _llm_complete(system, user, json_schema=json_schema)
    except Exception:
        return _offline_facts()

    parsed = _parse_json_object(raw)
    if parsed is None:
        return _offline_facts()
    return _facts_from_payload(parsed)


def _llm_key_present() -> bool:
    return bool(
        os.getenv("GENERAL_COMPUTE_API_KEY") or os.getenv("GENERALCOMPUTE_API_KEY")
    )


def _offline_facts() -> Facts:
    return Facts(confidence=0.0)


def _load_prompt() -> str:
    if _PROMPT_PATH.is_file():
        return _PROMPT_PATH.read_text(encoding="utf-8").strip()
    return (
        "Extract Facts JSON from the transcript. Required key: confidence. "
        "Omit price fields unless the shop stated a dollar amount. JSON only."
    )


def _format_transcript(
    transcript: Sequence[TranscriptLine | Mapping[str, Any] | str],
) -> str:
    lines: list[str] = []
    for item in transcript or []:
        if isinstance(item, str):
            text = item.strip()
            if text:
                lines.append(text)
            continue
        if isinstance(item, TranscriptLine):
            role, text = item.role, item.text
        elif isinstance(item, Mapping):
            role = str(item.get("role") or "unknown")
            text = str(item.get("text") or "").strip()
        else:
            role = str(getattr(item, "role", "unknown"))
            text = str(getattr(item, "text", "")).strip()
        if not text:
            continue
        lines.append(f"{role}: {text}")
    return "\n".join(lines) if lines else "(empty transcript)"


def _merge_schema(extra: dict[str, Any] | None) -> dict[str, Any]:
    schema = dict(_FACTS_JSON_SCHEMA)
    if not extra:
        return schema
    props = dict(schema.get("properties") or {})
    extra_props = extra.get("properties") if isinstance(extra.get("properties"), dict) else {}
    props.update(extra_props)
    schema["properties"] = props
    required = list(schema.get("required") or [])
    for key in extra.get("required") or []:
        if key not in required:
            required.append(key)
    if "confidence" not in required:
        required.append("confidence")
    schema["required"] = required
    return schema


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _facts_from_payload(data: dict[str, Any]) -> Facts:
    cleaned: dict[str, Any] = {}
    confidence = data.get("confidence", 0)
    try:
        cleaned["confidence"] = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        cleaned["confidence"] = 0.0

    for key in _PRICE_KEYS:
        value = _as_price(data.get(key))
        if value is not None:
            cleaned[key] = value

    hours = data.get("laborHours")
    if isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours > 0:
        cleaned["laborHours"] = float(hours)

    accepts = data.get("acceptsCustomerParts")
    if isinstance(accepts, bool):
        cleaned["acceptsCustomerParts"] = accepts

    parts_type = data.get("partsType")
    if parts_type in ("oem", "aftermarket"):
        cleaned["partsType"] = parts_type

    warranty = data.get("warrantyMonths")
    if isinstance(warranty, (int, float)) and not isinstance(warranty, bool) and warranty >= 0:
        cleaned["warrantyMonths"] = int(warranty)

    slot = data.get("earliestSlot")
    if isinstance(slot, str) and slot.strip():
        cleaned["earliestSlot"] = slot.strip()

    return Facts.model_validate(cleaned)


def _as_price(value: Any) -> float | None:
    """Keep only a stated positive dollar amount. Never coerce missing → 0."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None
