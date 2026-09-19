"""BYO vs shop-supplied Result from agents that already have real facts.

Dollars are always computed here. ``llm.complete`` (when importable) only
writes the two-sentence ``why``. Missing LLM or a bad reply falls back to
deterministic copy. Never invent a shop price.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from models import Agent, Result, Task
except ImportError:  # pragma: no cover
    from server.models import Agent, Result, Task

try:
    from llm import complete as _llm_complete
except ImportError:  # pragma: no cover
    try:
        from server.llm import complete as _llm_complete
    except ImportError:
        _llm_complete = None

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "synthesize.md"

_BYO_LABEL = "Bring your own part"
_SHOP_LABEL = "Shop supplies part"

_WHY_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["why"],
    "properties": {"why": {"type": "string"}},
}


def synthesize(
    task: Task | Mapping[str, Any] | None = None,
    agents: Sequence[Agent | Mapping[str, Any]] | None = None,
    userQuote: float | None = None,
) -> dict[str, Any]:
    """Build a Result dict from real agent facts plus optional ``userQuote``.

    ``complete_task`` calls this with a ``Task``. Tests may pass ``agents``
    and ``userQuote`` directly.
    """
    agent_list, quote = _inputs(task, agents, userQuote)
    parts = _web_parts(agent_list)
    shop_quotes = _shop_quotes(agent_list)
    byo = _best_byo(agent_list, parts)
    shop = _best_shop(shop_quotes)

    options: list[dict[str, Any]] = []
    if byo is not None:
        options.append(byo)
    if shop is not None:
        options.append(shop)

    recommended_index = _recommend(options)
    recommended_agent_id = ""
    if options:
        recommended_agent_id = _booking_agent_id(options[recommended_index])

    savings: float | None = None
    if quote is not None and options:
        savings = _money(quote - options[recommended_index]["total"])

    why = _why(
        options=options,
        shop_quote_count=len(shop_quotes),
        user_quote=quote,
        savings=savings,
        recommended_index=recommended_index,
    )

    payload: dict[str, Any] = {
        "options": _strip_private(options),
        "recommendedOptionIndex": recommended_index,
        "recommendedAgentId": recommended_agent_id,
        "why": why,
    }
    if savings is not None:
        payload["savingsVsQuote"] = savings
    return Result.model_validate(payload).model_dump(exclude_none=True)


def _inputs(
    task: Task | Mapping[str, Any] | None,
    agents: Sequence[Agent | Mapping[str, Any]] | None,
    userQuote: float | None,
) -> tuple[list[Any], float | None]:
    if task is not None:
        mapped = _as_mapping(task)
        if agents is None:
            agents = mapped.get("agents") or getattr(task, "agents", None) or []
        if userQuote is None:
            userQuote = mapped.get("userQuote")
            if userQuote is None:
                userQuote = getattr(task, "userQuote", None)
    return list(agents or []), _as_price(userQuote)


def _as_mapping(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return dict(obj)
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    return {}


def _facts(agent: Any) -> dict[str, Any]:
    mapped = _as_mapping(agent)
    raw = mapped.get("facts")
    if raw is None:
        raw = getattr(agent, "facts", None)
    return _as_mapping(raw)


def _business_name(agent: Any) -> str:
    mapped = _as_mapping(agent)
    raw = mapped.get("business")
    if raw is None:
        raw = getattr(agent, "business", None)
    biz = _as_mapping(raw)
    name = biz.get("name") or getattr(raw, "name", None) or "the shop"
    return str(name)


def _agent_id(agent: Any) -> str:
    mapped = _as_mapping(agent)
    value = mapped.get("id")
    if value is None:
        value = getattr(agent, "id", "")
    return str(value or "")


def _kind(agent: Any) -> str:
    mapped = _as_mapping(agent)
    value = mapped.get("kind")
    if value is None:
        value = getattr(agent, "kind", "")
    return str(value or "")


def _as_price(value: Any) -> float | None:
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


def _money(value: float) -> float:
    rounded = round(float(value), 2)
    if rounded == int(rounded):
        return float(int(rounded))
    return rounded


def _fmt_money(value: float) -> str:
    amount = _money(value)
    if amount == int(amount):
        return f"${int(amount)}"
    return f"${amount:.2f}"


def _fmt_hours(hours: float) -> str:
    if hours == int(hours):
        return str(int(hours))
    return str(hours)


def _web_parts(agents: Sequence[Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for agent in agents:
        if _kind(agent) != "web":
            continue
        facts = _facts(agent)
        price = _as_price(facts.get("partPrice"))
        if price is None:
            continue
        found.append(
            {
                "agentId": _agent_id(agent),
                "name": _business_name(agent),
                "partPrice": _money(price),
                "partsType": facts.get("partsType"),
            }
        )
    found.sort(key=lambda row: row["partPrice"])
    return found


def _shop_quotes(agents: Sequence[Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for agent in agents:
        if _kind(agent) == "web":
            continue
        facts = _facts(agent)
        price = _as_price(facts.get("allInPrice"))
        if price is None:
            continue
        found.append(_shop_row(agent, facts, all_in=_money(price)))
    found.sort(key=lambda row: row["allInPrice"])
    return found


def _shop_row(agent: Any, facts: Mapping[str, Any], all_in: float | None) -> dict[str, Any]:
    rate = _as_price(facts.get("laborRatePerHour"))
    hours = facts.get("laborHours")
    labor_hours: float | None = None
    if isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours > 0:
        labor_hours = float(hours)
    return {
        "agentId": _agent_id(agent),
        "name": _business_name(agent),
        "allInPrice": all_in,
        "laborRatePerHour": _money(rate) if rate is not None else None,
        "laborHours": labor_hours,
        "acceptsCustomerParts": facts.get("acceptsCustomerParts") is True,
        "partsType": facts.get("partsType"),
        "warrantyMonths": facts.get("warrantyMonths")
        if isinstance(facts.get("warrantyMonths"), int)
        else None,
    }


def _best_byo(
    agents: Sequence[Any],
    parts: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if not parts:
        return None
    part = parts[0]
    best: dict[str, Any] | None = None
    for agent in agents:
        if _kind(agent) == "web":
            continue
        facts = _facts(agent)
        if facts.get("acceptsCustomerParts") is not True:
            continue
        rate = _as_price(facts.get("laborRatePerHour"))
        hours = facts.get("laborHours")
        if rate is None or not isinstance(hours, (int, float)) or isinstance(hours, bool):
            continue
        if hours <= 0:
            continue
        labor = rate * float(hours)
        total = _money(part["partPrice"] + labor)
        row = _shop_row(agent, facts, all_in=_as_price(facts.get("allInPrice")))
        candidate = {
            "label": _BYO_LABEL,
            "total": total,
            "breakdown": (
                f"{_fmt_money(part['partPrice'])} part from {part['name']} + "
                f"{_fmt_hours(float(hours))} h × {_fmt_money(rate)} labor at {row['name']}"
            ),
            "agentIds": [part["agentId"], row["agentId"]],
            "_shopName": row["name"],
            "_warrantyMonths": row["warrantyMonths"],
            "_acceptsCustomerParts": True,
        }
        if best is None or candidate["total"] < best["total"]:
            best = candidate
    return _public_option(best) if best is not None else None


def _best_shop(quotes: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not quotes:
        return None
    row = quotes[0]
    bits = [f"All-in at {row['name']}"]
    parts_type = row.get("partsType")
    if parts_type in ("oem", "aftermarket"):
        bits.append(f"{parts_type} parts")
    warranty = _warranty_phrase(row.get("warrantyMonths"))
    if warranty:
        bits.append(warranty)
    return {
        "label": _SHOP_LABEL,
        "total": _money(row["allInPrice"]),
        "breakdown": ", ".join(bits),
        "agentIds": [row["agentId"]],
        "_shopName": row["name"],
        "_warrantyMonths": row.get("warrantyMonths"),
        "_acceptsCustomerParts": row.get("acceptsCustomerParts"),
    }


def _public_option(option: dict[str, Any] | None) -> dict[str, Any] | None:
    if option is None:
        return None
    return option


def _warranty_phrase(months: Any) -> str | None:
    if not isinstance(months, int) or months <= 0:
        return None
    if months % 12 == 0:
        years = months // 12
        unit = "year" if years == 1 else "years"
        return f"{years}-{unit} warranty"
    unit = "month" if months == 1 else "months"
    return f"{months}-{unit} warranty"


def _recommend(options: Sequence[Mapping[str, Any]]) -> int:
    if not options:
        return 0
    best_i = 0
    best_total = options[0]["total"]
    for i, option in enumerate(options):
        total = option["total"]
        if total < best_total:
            best_i, best_total = i, total
        elif total == best_total and option.get("label") == _SHOP_LABEL:
            best_i, best_total = i, total
    return best_i


def _booking_agent_id(option: Mapping[str, Any]) -> str:
    ids = list(option.get("agentIds") or [])
    return str(ids[-1]) if ids else ""


def _strip_private(options: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for option in options:
        clean.append(
            {
                "label": option["label"],
                "total": option["total"],
                "breakdown": option["breakdown"],
                "agentIds": list(option["agentIds"]),
            }
        )
    return clean


def _why(
    *,
    options: list[dict[str, Any]],
    shop_quote_count: int,
    user_quote: float | None,
    savings: float | None,
    recommended_index: int,
) -> str:
    fallback = _deterministic_why(
        options=options,
        shop_quote_count=shop_quote_count,
        user_quote=user_quote,
        savings=savings,
        recommended_index=recommended_index,
    )
    llm_why = _llm_why(
        options=_strip_private(options),
        shop_quote_count=shop_quote_count,
        user_quote=user_quote,
        savings=savings,
        recommended_index=recommended_index,
    )
    if llm_why and _is_two_sentences(llm_why):
        return llm_why
    return fallback


def _deterministic_why(
    *,
    options: Sequence[Mapping[str, Any]],
    shop_quote_count: int,
    user_quote: float | None,
    savings: float | None,
    recommended_index: int,
) -> str:
    byo = next((o for o in options if o.get("label") == _BYO_LABEL), None)
    shop = next((o for o in options if o.get("label") == _SHOP_LABEL), None)

    if shop_quote_count == 0 and not options:
        return (
            "No shop returned a real quote. "
            "We did not invent shop prices."
        )

    if shop_quote_count == 1:
        first = (
            f"Only one shop answered with a real quote"
            f"{_named(shop or byo)}."
        )
        if byo and shop:
            delta = _money(shop["total"] - byo["total"])
            second = (
                f"Bring-your-own is {_fmt_money(byo['total'])} versus "
                f"{_fmt_money(shop['total'])} shop-supplied"
                f"{_vs_quote_clause(user_quote, savings)}."
            )
            if delta > 0:
                second = (
                    f"Bring-your-own is {_fmt_money(byo['total'])}, "
                    f"{_fmt_money(delta)} under shop-supplied"
                    f"{_vs_quote_clause(user_quote, savings)}."
                )
            return f"{first} {second}"
        if shop:
            return (
                f"{first} "
                f"Shop-supplied is {_fmt_money(shop['total'])}"
                f"{_vs_quote_clause(user_quote, savings)}."
            )
        if byo:
            return (
                f"{first} "
                f"Bring-your-own labor is {_fmt_money(byo['total'])}; "
                "there is no second shop price."
            )

    if byo and shop:
        rec = options[recommended_index]
        other = shop if rec.get("label") == _BYO_LABEL else byo
        rec_short = "Bring-your-own" if rec.get("label") == _BYO_LABEL else "Shop-supplied"
        other_short = "shop-supplied" if other.get("label") == _SHOP_LABEL else "bring-your-own"
        return (
            f"{rec_short} is {_fmt_money(rec['total'])}, "
            f"{_fmt_money(abs(shop['total'] - byo['total']))} "
            f"{'under' if rec['total'] < other['total'] else 'over'} "
            f"{other_short} from the shops that quoted. "
            f"{_second_multi(shop_quote_count, user_quote, savings)}"
        )

    if shop:
        return (
            f"Shop-supplied is {_fmt_money(shop['total'])} from "
            f"{shop_quote_count} real shop quote"
            f"{'s' if shop_quote_count != 1 else ''}. "
            f"{_second_no_byo(user_quote, savings)}"
        )

    if byo:
        return (
            f"Bring-your-own is {_fmt_money(byo['total'])} from a real part "
            f"price plus stated labor. "
            "No shop gave an all-in price, so we did not invent one."
        )

    return (
        "No shop returned a real quote. "
        "We did not invent shop prices."
    )


def _named(option: Mapping[str, Any] | None) -> str:
    if not option:
        return ""
    name = option.get("_shopName")
    if not name:
        return ""
    return f" ({name})"


def _vs_quote_clause(user_quote: float | None, savings: float | None) -> str:
    if user_quote is None or savings is None:
        return ""
    if savings > 0:
        return f", {_fmt_money(savings)} under your {_fmt_money(user_quote)} quote"
    if savings < 0:
        return f", {_fmt_money(abs(savings))} over your {_fmt_money(user_quote)} quote"
    return f", matching your {_fmt_money(user_quote)} quote"


def _second_multi(shop_quote_count: int, user_quote: float | None, savings: float | None) -> str:
    extra = _vs_quote_clause(user_quote, savings)
    if extra:
        return f"That is{extra[1:]}."
    return f"{shop_quote_count} shops returned real all-in quotes; none were invented."


def _second_no_byo(user_quote: float | None, savings: float | None) -> str:
    clause = _vs_quote_clause(user_quote, savings)
    if clause:
        return (
            "Bring-your-own was not computable from stated labor and a real part price"
            f"{clause}."
        )
    return "Bring-your-own was not computable from stated labor and a real part price."


def _llm_why(
    *,
    options: list[dict[str, Any]],
    shop_quote_count: int,
    user_quote: float | None,
    savings: float | None,
    recommended_index: int,
) -> str | None:
    if _llm_complete is None:
        return None
    system = _load_prompt()
    user = json.dumps(
        {
            "options": options,
            "recommendedOptionIndex": recommended_index,
            "shopQuoteCount": shop_quote_count,
            "userQuote": user_quote,
            "savingsVsQuote": savings,
        },
        indent=2,
    )
    try:
        raw = _llm_complete(system, user, json_schema=_WHY_JSON_SCHEMA)
    except Exception:
        return None
    parsed = _parse_json_object(raw)
    if parsed is None:
        text = (raw or "").strip()
        return text or None
    why = parsed.get("why")
    return why.strip() if isinstance(why, str) and why.strip() else None


def _load_prompt() -> str:
    if _PROMPT_PATH.is_file():
        return _PROMPT_PATH.read_text(encoding="utf-8").strip()
    return (
        "Write exactly two sentences of why for BYO vs shop-supplied. "
        "Do not change dollar amounts. Do not invent shops. JSON: {\"why\": \"...\"}."
    )


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


def _is_two_sentences(text: str) -> bool:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    return len(parts) == 2


def synthesize_result(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Alias used by ``complete_task``."""
    return synthesize(*args, **kwargs)


def build_result(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Alias used by ``complete_task``."""
    return synthesize(*args, **kwargs)
