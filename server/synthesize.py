"""BYO vs shop-supplied Result from agents that already have real facts.

General Compute (``llm.complete``) reads every shop's facts, the web part
prices, the user's quote and the preferences, and returns the decision:
options, a recommendation, a two-sentence ``why`` and 2 to 4 tradeoffs.

Python verifies every dollar before it reaches ``Result``:

- every option total is recomputed from the cited agents and must match
  within $1 (the recomputed value is what gets stored);
- every cited agentId must exist;
- a shop cited for "Bring your own part" must accept customer parts;
- dollar figures in prose (breakdown, hassle, why, tradeoffs) must be
  numbers present in the input or derived from them by the allowed formulas;
- any option that fails is dropped.

If the reply is unusable (no verified option, the recommended option was
dropped, bad JSON, or the model is offline) we fall back to the
deterministic path below, which builds the cheapest BYO and the cheapest
shop-supplied option from the same facts. Never invent a shop price.
"""

from __future__ import annotations

import json
import os
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

_DEFAULT_PREFERENCES = (
    "Prefer the option with a warranty and fewer trips unless the savings "
    "from the other option exceed $150."
)

_TOTAL_TOLERANCE = 1.0  # dollars; model total vs Python recomputation
_PROSE_TOLERANCE = 0.5  # dollars; a "$486" in prose may stand for 486.40

_DECISION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["options", "recommendedOptionIndex", "why", "tradeoffs"],
    "properties": {
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "total", "breakdown", "agentIds", "hassle"],
                "properties": {
                    "label": {"type": "string", "enum": [_BYO_LABEL, _SHOP_LABEL]},
                    "total": {"type": "number"},
                    "breakdown": {"type": "string"},
                    "agentIds": {"type": "array", "items": {"type": "string"}},
                    "hassle": {"type": "string"},
                },
            },
        },
        "recommendedOptionIndex": {"type": "integer"},
        "why": {"type": "string"},
        "tradeoffs": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "maxItems": 4,
        },
    },
}


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


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
    shops = _shops(agent_list)
    shop_quotes = [s for s in shops if s["allInPrice"] is not None]

    decision = _llm_decision(shops=shops, parts=parts, quote=quote)
    if decision is None:
        decision = _deterministic_decision(shops=shops, parts=parts, quote=quote)

    options = decision["options"]
    recommended_index = decision["recommendedOptionIndex"]
    recommended_agent_id = _booking_agent_id(options[recommended_index]) if options else ""

    savings: float | None = None
    if quote is not None and options:
        savings = _money(quote - options[recommended_index]["total"])

    why = decision.get("why")
    if not why:
        why = _deterministic_why(
            options=options,
            shop_quote_count=len(shop_quotes),
            user_quote=quote,
            savings=savings,
            recommended_index=recommended_index,
        )

    tradeoffs = decision.get("tradeoffs")
    if not tradeoffs:
        tradeoffs = _deterministic_tradeoffs(options)

    payload: dict[str, Any] = {
        "options": _strip_private(options),
        "recommendedOptionIndex": recommended_index,
        "recommendedAgentId": recommended_agent_id,
        "why": why,
    }
    if savings is not None:
        payload["savingsVsQuote"] = savings
    if tradeoffs:
        payload["tradeoffs"] = tradeoffs
    return Result.model_validate(payload).model_dump(exclude_none=True)


# --------------------------------------------------------------------------- #
# Input normalisation
# --------------------------------------------------------------------------- #


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


def _as_hours(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


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


def _shops(agents: Sequence[Any]) -> list[dict[str, Any]]:
    """Call agents that gave at least one real number, cheapest all-in first."""
    found: list[dict[str, Any]] = []
    for agent in agents:
        if _kind(agent) == "web":
            continue
        facts = _facts(agent)
        row = _shop_row(agent, facts)
        if row["allInPrice"] is None and row["laborRatePerHour"] is None:
            continue
        found.append(row)
    found.sort(key=lambda row: (row["allInPrice"] is None, row["allInPrice"] or 0.0))
    return found


def _shop_row(agent: Any, facts: Mapping[str, Any]) -> dict[str, Any]:
    rate = _as_price(facts.get("laborRatePerHour"))
    all_in = _as_price(facts.get("allInPrice"))
    part = _as_price(facts.get("partPrice"))
    warranty = facts.get("warrantyMonths")
    slot = facts.get("earliestSlot")
    return {
        "agentId": _agent_id(agent),
        "name": _business_name(agent),
        "allInPrice": _money(all_in) if all_in is not None else None,
        "partPrice": _money(part) if part is not None else None,
        "laborRatePerHour": _money(rate) if rate is not None else None,
        "laborHours": _as_hours(facts.get("laborHours")),
        "acceptsCustomerParts": facts.get("acceptsCustomerParts") is True,
        "partsType": facts.get("partsType"),
        "warrantyMonths": warranty if isinstance(warranty, int) and not isinstance(warranty, bool) else None,
        "earliestSlot": str(slot) if isinstance(slot, str) and slot.strip() else None,
    }


def _labor(shop: Mapping[str, Any]) -> tuple[float, str] | None:
    """Labor dollars at ``shop`` and how they were obtained.

    ``rate × hours`` when both were said; otherwise ``allInPrice − partPrice``
    when both were said (the shop told us both, so the difference is theirs).
    """
    rate, hours = shop.get("laborRatePerHour"), shop.get("laborHours")
    if rate is not None and hours is not None:
        return _money(rate * hours), "hours"
    all_in, part = shop.get("allInPrice"), shop.get("partPrice")
    if all_in is not None and part is not None and all_in > part:
        return _money(all_in - part), "derived"
    return None


# --------------------------------------------------------------------------- #
# Option builders (shared by the verifier and the deterministic path)
# --------------------------------------------------------------------------- #


def _byo_option(shop: Mapping[str, Any], part: Mapping[str, Any]) -> dict[str, Any] | None:
    if shop.get("acceptsCustomerParts") is not True:
        return None
    labor = _labor(shop)
    if labor is None:
        return None
    labor_dollars, source = labor
    if source == "hours":
        labor_text = (
            f"{_fmt_hours(shop['laborHours'])} h × {_fmt_money(shop['laborRatePerHour'])} "
            f"labor at {shop['name']}"
        )
    else:
        labor_text = (
            f"{_fmt_money(labor_dollars)} labor at {shop['name']} "
            f"(derived: {_fmt_money(shop['allInPrice'])} all-in − "
            f"{_fmt_money(shop['partPrice'])} shop part price)"
        )
    return {
        "label": _BYO_LABEL,
        "total": _money(part["partPrice"] + labor_dollars),
        "breakdown": f"{_fmt_money(part['partPrice'])} part from {part['name']} + {labor_text}",
        "agentIds": [part["agentId"], shop["agentId"]],
        "_shopName": shop["name"],
        "_warrantyMonths": shop.get("warrantyMonths"),
        "_laborSource": source,
        "_labor": labor_dollars,
    }


def _shop_option(shop: Mapping[str, Any]) -> dict[str, Any] | None:
    if shop.get("allInPrice") is None:
        return None
    bits = [f"All-in at {shop['name']}"]
    parts_type = shop.get("partsType")
    if parts_type in ("oem", "aftermarket"):
        bits.append(f"{parts_type} parts")
    warranty = _warranty_phrase(shop.get("warrantyMonths"))
    if warranty:
        bits.append(warranty)
    return {
        "label": _SHOP_LABEL,
        "total": _money(shop["allInPrice"]),
        "breakdown": ", ".join(bits),
        "agentIds": [shop["agentId"]],
        "_shopName": shop["name"],
        "_warrantyMonths": shop.get("warrantyMonths"),
    }


def _warranty_phrase(months: Any) -> str | None:
    if not isinstance(months, int) or isinstance(months, bool) or months <= 0:
        return None
    if months % 12 == 0:
        years = months // 12
        unit = "year" if years == 1 else "years"
        return f"{years}-{unit} warranty"
    unit = "month" if months == 1 else "months"
    return f"{months}-{unit} warranty"


# --------------------------------------------------------------------------- #
# General Compute decision + verification
# --------------------------------------------------------------------------- #


def _llm_decision(
    *,
    shops: list[dict[str, Any]],
    parts: list[dict[str, Any]],
    quote: float | None,
) -> dict[str, Any] | None:
    """Ask the model, verify every dollar, return a decision or None."""
    if _llm_complete is None:
        return None
    if not _any_option_possible(shops, parts):
        return None  # nothing to decide; deterministic path says so

    user = json.dumps(_decision_input(shops, parts, quote), indent=2)
    try:
        raw = _llm_complete(_load_prompt(), user, json_schema=_DECISION_JSON_SCHEMA, model=os.getenv("DECISION_MODEL"))
    except Exception:
        return None
    parsed = _parse_json_object(raw)
    if parsed is None:
        return None
    return _verify_decision(parsed, shops=shops, parts=parts, quote=quote)


def _any_option_possible(shops: Sequence[Mapping[str, Any]], parts: Sequence[Mapping[str, Any]]) -> bool:
    if any(s["allInPrice"] is not None for s in shops):
        return True
    return bool(parts) and any(_byo_option(s, parts[0]) is not None for s in shops)


def _decision_input(
    shops: Sequence[Mapping[str, Any]],
    parts: Sequence[Mapping[str, Any]],
    quote: float | None,
) -> dict[str, Any]:
    return {
        "shops": [
            {
                "agentId": s["agentId"],
                "name": s["name"],
                "allInPrice": s["allInPrice"],
                "partPrice": s["partPrice"],
                "laborRatePerHour": s["laborRatePerHour"],
                "laborHours": s["laborHours"],
                "acceptsCustomerParts": s["acceptsCustomerParts"],
                "partsType": s["partsType"],
                "warrantyMonths": s["warrantyMonths"],
                "earliestSlot": s["earliestSlot"],
            }
            for s in shops
        ],
        "webParts": [
            {
                "agentId": p["agentId"],
                "seller": p["name"],
                "partPrice": p["partPrice"],
                "partsType": p["partsType"],
            }
            for p in parts
        ],
        "userQuote": quote,
        "preferences": _DEFAULT_PREFERENCES,
    }


def _verify_decision(
    parsed: Mapping[str, Any],
    *,
    shops: Sequence[Mapping[str, Any]],
    parts: Sequence[Mapping[str, Any]],
    quote: float | None,
) -> dict[str, Any] | None:
    raw_options = parsed.get("options")
    if not isinstance(raw_options, list):
        return None

    shop_by_id = {s["agentId"]: s for s in shops}
    part_by_id = {p["agentId"]: p for p in parts}
    allowed = _allowed_dollars(shops, parts, quote)

    verified: list[dict[str, Any]] = []
    kept_indexes: list[int] = []
    for i, raw in enumerate(raw_options):
        option = _verify_option(raw, shop_by_id=shop_by_id, part_by_id=part_by_id, allowed=allowed)
        if option is None:
            continue
        verified.append(option)
        kept_indexes.append(i)
    if not verified:
        return None

    # Numbers the prose may cite: input facts plus the verified totals and gaps.
    allowed = allowed | _derived_dollars([o["total"] for o in verified], quote)
    for option in verified:
        option["breakdown"] = _fold_hassle(option, allowed)

    raw_index = parsed.get("recommendedOptionIndex")
    if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index not in kept_indexes:
        return None  # the model's reasoning pointed at a dropped/invalid option
    recommended = kept_indexes.index(raw_index)

    shop_quote_count = sum(1 for s in shops if s["allInPrice"] is not None)
    why = parsed.get("why")
    if not _why_is_valid(why, allowed=allowed, shop_quote_count=shop_quote_count):
        why = None  # synthesize() substitutes the deterministic why

    tradeoffs = _verify_tradeoffs(parsed.get("tradeoffs"), allowed)

    return {
        "options": verified,
        "recommendedOptionIndex": recommended,
        "why": why,
        "tradeoffs": tradeoffs,
    }


def _verify_option(
    raw: Any,
    *,
    shop_by_id: Mapping[str, Mapping[str, Any]],
    part_by_id: Mapping[str, Mapping[str, Any]],
    allowed: set[float],
) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    label = _normalise_label(raw.get("label"))
    total = _as_price(raw.get("total"))
    ids = raw.get("agentIds")
    if label is None or total is None or not isinstance(ids, list) or not ids:
        return None
    ids = [str(i) for i in ids]
    if any(i not in shop_by_id and i not in part_by_id for i in ids):
        return None  # cited an agent that does not exist

    cited_shops = [shop_by_id[i] for i in ids if i in shop_by_id]
    cited_parts = [part_by_id[i] for i in ids if i in part_by_id]

    if label == _SHOP_LABEL:
        if len(cited_shops) != 1 or cited_parts:
            return None
        rebuilt = _shop_option(cited_shops[0])
    else:
        if len(cited_shops) != 1 or len(cited_parts) != 1:
            return None
        rebuilt = _byo_option(cited_shops[0], cited_parts[0])
    if rebuilt is None:
        return None  # e.g. BYO at a shop that refuses customer parts
    if abs(rebuilt["total"] - total) > _TOTAL_TOLERANCE:
        return None  # hallucinated total

    # The recomputed total is what we keep. Prose from the model is used only
    # when every dollar figure in it is a number we know.
    breakdown = raw.get("breakdown")
    if isinstance(breakdown, str) and breakdown.strip() and _dollars_ok(breakdown, allowed | {rebuilt["total"], rebuilt.get("_labor", rebuilt["total"])}):
        if rebuilt.get("_laborSource") != "derived" or re.search(r"all-in|derived|minus|−", breakdown, flags=re.I):
            rebuilt["breakdown"] = breakdown.strip()
    hassle = raw.get("hassle")
    rebuilt["_hassle"] = hassle.strip() if isinstance(hassle, str) and hassle.strip() else None
    return rebuilt


def _normalise_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text.startswith("bring your own"):
        return _BYO_LABEL
    if text.startswith("shop supplies"):
        return _SHOP_LABEL
    return None


def _fold_hassle(option: dict[str, Any], allowed: set[float]) -> str:
    """ResultOption has no hassle field; append the clause to the breakdown."""
    hassle = option.pop("_hassle", None)
    breakdown = option["breakdown"]
    if hassle and _dollars_ok(hassle, allowed):
        clause = hassle.rstrip(".")
        clause = clause[0].lower() + clause[1:] if clause else clause
        return f"{breakdown} — hassle: {clause}"
    return breakdown


def _why_is_valid(why: Any, *, allowed: set[float], shop_quote_count: int) -> bool:
    if not isinstance(why, str) or not why.strip():
        return False
    text = why.strip()
    if not _is_two_sentences(text):
        return False
    if not _dollars_ok(text, allowed):
        return False
    if shop_quote_count == 1 and "one shop" not in text.lower():
        return False
    return True


def _verify_tradeoffs(raw: Any, allowed: set[float]) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    clean: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        if not text or not _dollars_ok(text, allowed):
            continue
        clean.append(text)
    if len(clean) < 2:
        return None
    return clean[:4]


_DOLLAR_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")


def _dollars_in(text: str) -> list[float]:
    found: list[float] = []
    for match in _DOLLAR_RE.finditer(text):
        try:
            found.append(float(match.group(1).replace(",", "")))
        except ValueError:
            continue
    return found


def _dollars_ok(text: str, allowed: set[float]) -> bool:
    return all(
        any(abs(value - known) <= _PROSE_TOLERANCE for known in allowed)
        for value in _dollars_in(text)
    )


def _allowed_dollars(
    shops: Sequence[Mapping[str, Any]],
    parts: Sequence[Mapping[str, Any]],
    quote: float | None,
) -> set[float]:
    """Every dollar figure that appeared in the input or follows from it."""
    known: set[float] = set()
    for s in shops:
        for key in ("allInPrice", "partPrice", "laborRatePerHour"):
            if s.get(key) is not None:
                known.add(float(s[key]))
        labor = _labor(s)
        if labor is not None:
            known.add(labor[0])
            for p in parts:
                known.add(_money(p["partPrice"] + labor[0]))
    for p in parts:
        known.add(float(p["partPrice"]))
    if quote is not None:
        known.add(float(quote))
    known.add(150.0)  # the preference threshold, stated in the prompt
    return known


def _derived_dollars(totals: Sequence[float], quote: float | None) -> set[float]:
    """Gaps between verified totals and against the user's quote."""
    derived: set[float] = set(float(t) for t in totals)
    for a in totals:
        for b in totals:
            if a != b:
                derived.add(_money(abs(a - b)))
        if quote is not None:
            derived.add(_money(abs(quote - a)))
    return derived


# --------------------------------------------------------------------------- #
# Deterministic path (fallback; also the offline path)
# --------------------------------------------------------------------------- #


def _deterministic_decision(
    *,
    shops: list[dict[str, Any]],
    parts: list[dict[str, Any]],
    quote: float | None,
) -> dict[str, Any]:
    options: list[dict[str, Any]] = []
    byo = _best_byo(shops, parts)
    if byo is not None:
        options.append(byo)
    shop = _best_shop(shops)
    if shop is not None:
        options.append(shop)
    return {
        "options": options,
        "recommendedOptionIndex": _recommend(options),
        "why": None,
        "tradeoffs": None,
    }


def _best_byo(
    shops: Sequence[Mapping[str, Any]],
    parts: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if not parts:
        return None
    part = parts[0]
    best: dict[str, Any] | None = None
    for shop in shops:
        candidate = _byo_option(shop, part)
        if candidate is None:
            continue
        if best is None or candidate["total"] < best["total"]:
            best = candidate
    return best


def _best_shop(shops: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    quoted = [s for s in shops if s["allInPrice"] is not None]
    if not quoted:
        return None
    return _shop_option(min(quoted, key=lambda s: s["allInPrice"]))


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


def _deterministic_tradeoffs(options: Sequence[Mapping[str, Any]]) -> list[str] | None:
    byo = next((o for o in options if o.get("label") == _BYO_LABEL), None)
    shop = next((o for o in options if o.get("label") == _SHOP_LABEL), None)
    if byo is None and shop is None:
        return None
    bullets: list[str] = []
    if byo and shop:
        gap = _money(shop["total"] - byo["total"])
        if gap > 0:
            bullets.append(f"Bring-your-own saves {_fmt_money(gap)} over shop-supplied")
        elif gap < 0:
            bullets.append(f"Shop-supplied is {_fmt_money(abs(gap))} cheaper than bring-your-own")
        else:
            bullets.append("Both options cost the same")
    if byo:
        bullets.append("Bring-your-own means ordering the part first, then one shop visit")
    if shop:
        warranty = _warranty_phrase(shop.get("_warrantyMonths"))
        if warranty:
            bullets.append(f"Shop-supplied at {shop.get('_shopName')} includes a {warranty}")
        else:
            bullets.append(f"Shop-supplied at {shop.get('_shopName')} is one visit; warranty not stated")
    if byo and byo.get("_warrantyMonths") is None:
        bullets.append("Warranty on a customer-supplied part usually covers labor only")
    if len(bullets) < 2:
        bullets.append("Only one option was computable from real quotes")
    return bullets[:4]


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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_prompt() -> str:
    if _PROMPT_PATH.is_file():
        return _PROMPT_PATH.read_text(encoding="utf-8").strip()
    return (
        "Decide bring-your-own versus shop-supplied from the shops and web parts "
        "given. Use only numbers in the input. Output strict JSON with options "
        "(label, total, breakdown, agentIds, hassle), recommendedOptionIndex, "
        "a two-sentence why, and 2 to 4 tradeoffs."
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
