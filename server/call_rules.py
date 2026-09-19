"""Deterministic hang-up rules for outbound shop calls.

No network, no Twilio, no LLM. Safe to unit-test with transcript strings
and a facts dict (or ``models.Facts``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

# Call-agent facts that mean we have a usable quote. On a call agent
# ``partPrice`` is the shop's own price for the part alone; it is optional
# when the shop does not fit customer parts (they won't sell the part alone).
REQUIRED_CALL_FIELDS: tuple[str, ...] = (
    "allInPrice",
    "partPrice",
    "laborRatePerHour",
    "laborHours",
    "acceptsCustomerParts",
    "partsType",
    "warrantyMonths",
    "earliestSlot",
)

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "caller.md"

_VOICEMAIL_MARKERS: tuple[str, ...] = (
    "voicemail",
    "voice mail",
    "leave a message",
    "leave your message",
    "leave a voicemail",
    "after the beep",
    "after the tone",
    "at the tone",
    "at the beep",
    "record your message",
    "please record",
    "mailbox is full",
    "mailbox full",
    "not available to take your call",
    "unable to take your call",
    "can't take your call",
    "cannot take your call",
    "no one is available",
    "person you are trying to reach",
    "the number you have dialed",
    "forwarded to voicemail",
)

_REFUSAL_MARKERS: tuple[str, ...] = (
    "not interested",
    "no thank you",
    "don't call",
    "do not call",
    "stop calling",
    "we don't give quotes",
    "don't give quotes",
    "no quotes over the phone",
    "can't give a quote",
    "cannot give a quote",
    "won't give a quote",
    "we don't quote",
    "not going to quote",
    "we don't do quotes",
    "we don't do that",
    "we don't do brakes",
    "don't take customer parts",
    "please don't call",
    "take us off",
    "remove us from",
    "we can't help",
    "we cannot help",
    "not able to help",
)


def load_caller_prompt() -> str:
    """Return ``prompts/caller.md`` (disclosure is the first line)."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def disclosure_line() -> str:
    """First line of the caller prompt — spoken at the start of every call."""
    first = load_caller_prompt().lstrip("\ufeff").splitlines()[0].strip()
    return first


def _as_text(source: Any) -> str:
    if source is None:
        return ""
    if isinstance(source, str):
        return source
    if isinstance(source, Mapping):
        return str(source.get("text") or "")
    if isinstance(source, Iterable) and not isinstance(source, (bytes, bytearray)):
        parts: list[str] = []
        for item in source:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                role = str(item.get("role") or "")
                if role and role != "business":
                    continue
                parts.append(str(item.get("text") or ""))
            else:
                role = getattr(item, "role", None)
                text = getattr(item, "text", None)
                if text is None:
                    continue
                if role is not None and str(role) != "business":
                    continue
                parts.append(str(text))
        return "\n".join(parts)
    return str(source)


def _normalize(text: Any) -> str:
    return " ".join(_as_text(text).casefold().split())


def _contains_any(haystack: str, needles: Iterable[str]) -> bool:
    return any(needle in haystack for needle in needles)


def _facts_map(facts: Any) -> Mapping[str, Any]:
    if facts is None:
        return {}
    if isinstance(facts, Mapping):
        return facts
    dump = getattr(facts, "model_dump", None)
    if callable(dump):
        return dump()
    raise TypeError("facts must be a mapping or a Pydantic model")


def _field_present(facts: Mapping[str, Any], key: str) -> bool:
    if key not in facts or facts[key] is None:
        return False
    value = facts[key]
    if key in ("allInPrice", "partPrice", "laborRatePerHour", "laborHours"):
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return False
    if key == "acceptsCustomerParts":
        return isinstance(value, bool)
    if key == "partsType":
        return str(value).casefold() in {"oem", "aftermarket"}
    if key == "warrantyMonths":
        try:
            return int(value) >= 0
        except (TypeError, ValueError):
            return False
    if key == "earliestSlot":
        return bool(str(value).strip())
    return True


def is_voicemail(text: Any) -> bool:
    """True when the far side sounds like a mailbox or carrier greeting."""
    return _contains_any(_normalize(text), _VOICEMAIL_MARKERS)


def is_refusal(text: Any) -> bool:
    """True when the shop declines to quote or asks us to stop."""
    return _contains_any(_normalize(text), _REFUSAL_MARKERS)


def _part_price_optional(facts: Mapping[str, Any]) -> bool:
    """``partPrice`` is not required when the shop refuses customer parts."""
    return facts.get("acceptsCustomerParts") is False


def all_fields_filled(facts: Any) -> bool:
    """True when every required call-quote field is present and usable.

    ``partPrice`` is skipped when ``acceptsCustomerParts`` is ``False``: a shop
    that will not fit a customer part has no reason to sell the part alone.
    """
    mapped = _facts_map(facts)
    skip_part = _part_price_optional(mapped)
    return all(
        _field_present(mapped, key)
        for key in REQUIRED_CALL_FIELDS
        if not (skip_part and key == "partPrice")
    )


def should_hang_up(text: Any = None, facts: Any = None) -> bool:
    """Hang up on voicemail, refusal, or a complete quote."""
    return is_voicemail(text) or is_refusal(text) or all_fields_filled(facts)


__all__ = (
    "REQUIRED_CALL_FIELDS",
    "all_fields_filled",
    "disclosure_line",
    "is_refusal",
    "is_voicemail",
    "load_caller_prompt",
    "should_hang_up",
)
