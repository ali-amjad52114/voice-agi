"""One-line agent card summaries built only from extracted facts.

``summary_from_facts`` turns an ``Agent`` (model or plain dict) into the short
string the card shows, e.g. ``"$450 all-in · $150/h · 1-mo warranty"``. It never
invents a number: every dollar figure, hour count or warranty length comes from
``agent.facts`` exactly as extraction stored it. Missing facts are simply left
out; an answered call with nothing extracted reads ``"Call answered · no quote"``.

Every result is shorter than ``MAX_LEN`` characters. When the joined facts would
run over, trailing items are dropped until the line fits, so the most important
numbers (all-in, part, rate) survive.
"""

from __future__ import annotations

from typing import Any

MAX_LEN = 60
SEP = " · "

VOICEMAIL = "Voicemail · no quote"
REFUSED = "Declined to quote"
ANSWERED_NO_QUOTE = "Call answered · no quote"
WEB_NO_PRICE = "No part price found"


def summary_from_facts(agent: Any) -> str:
    """Return the card summary for ``agent`` (an ``Agent`` model or a dict)."""
    kind = _get(agent, "kind")
    facts = _get(agent, "facts")
    if kind == "web":
        text = _web_summary(facts)
    else:
        text = _call_summary(facts, _get(_get(agent, "call"), "outcome"))
    return _fit(text)


# ---------------------------------------------------------------------------
# call agents


def _call_summary(facts: Any, outcome: Any) -> str:
    if outcome == "voicemail":
        return VOICEMAIL
    if outcome == "refused":
        return REFUSED
    items = _call_items(facts)
    if not items:
        return ANSWERED_NO_QUOTE
    return _join_within_limit(items)


def _call_items(facts: Any) -> list[str]:
    """Present facts, in card order. Anything null is skipped."""
    items: list[str] = []

    all_in = _number(_get(facts, "allInPrice"))
    if all_in is not None:
        items.append(f"{_money(all_in)} all-in")

    part = _number(_get(facts, "partPrice"))
    if part is not None:
        items.append(f"part {_money(part)}")

    rate = _number(_get(facts, "laborRatePerHour"))
    if rate is not None:
        items.append(f"{_money(rate)}/h")

    hours = _number(_get(facts, "laborHours"))
    if hours is not None:
        items.append(f"{_plain(hours)} h")

    accepts = _get(facts, "acceptsCustomerParts")
    if accepts is True:
        items.append("takes your parts")
    elif accepts is False:
        items.append("no customer parts")

    parts_label = _parts_type_label(_get(facts, "partsType"))
    if parts_label:
        items.append(parts_label)

    warranty = _warranty(_get(facts, "warrantyMonths"))
    if warranty:
        items.append(warranty)

    return items


def _warranty(months: Any) -> str | None:
    if months is None or isinstance(months, bool):
        return None
    try:
        m = int(months)
    except (TypeError, ValueError):
        return None
    if m > 0 and m % 12 == 0:
        return f"{m // 12}-yr warranty"
    return f"{m}-mo warranty"


# ---------------------------------------------------------------------------
# web agents


def _web_summary(facts: Any) -> str:
    price = _number(_get(facts, "partPrice"))
    if price is None:
        return WEB_NO_PRICE
    label = _parts_type_label(_get(facts, "partsType"))
    if label:
        return f"{label} pads + rotors {_money(price)}"
    return f"part {_money(price)}"


# ---------------------------------------------------------------------------
# formatting helpers


def _parts_type_label(value: Any) -> str | None:
    if value == "oem":
        return "OEM"
    if value == "aftermarket":
        return "aftermarket"
    return None


def _number(value: Any) -> float | None:
    """Coerce a fact to float; booleans and unparsable values count as absent."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _plain(value: float) -> str:
    """2.0 -> "2", 2.5 -> "2.5", 2.25 -> "2.25"."""
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _money(value: float) -> str:
    """450 -> "$450", 186.5 -> "$186.50"."""
    if value.is_integer():
        return f"${int(value)}"
    return f"${value:.2f}"


def _join_within_limit(items: list[str]) -> str:
    """Join with the separator, dropping trailing items until under MAX_LEN."""
    kept = list(items)
    while len(kept) > 1 and len(SEP.join(kept)) >= MAX_LEN:
        kept.pop()
    return SEP.join(kept)


def _fit(text: str) -> str:
    """Last-resort guard so no branch can ever exceed the limit."""
    if len(text) < MAX_LEN:
        return text
    return text[: MAX_LEN - 2].rstrip() + "…"


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` from a pydantic model or a dict; None when absent."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)
