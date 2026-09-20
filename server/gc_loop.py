"""Session 9: Gemma orders shop discovery and the part lookup through tools.

One General Compute chat loop (OpenAI function-tool shape) with exactly two
tools, ``discover_shops`` and ``lookup_part``. The model decides when to call
them; Python executes them in-process and feeds the JSON back as ``role:
tool`` messages until the model answers without a tool call or ``max_steps``
is reached.

Guardrails:

- ``GcLoopResult.shops`` and ``.parts`` are the union of what the tools
  returned, deduped, validated with ``validate_against_tools`` /
  ``validate_prices``. Nothing the model wrote as prose is ever a shop or a
  dollar.
- ``discover_shops`` runs with the task's own coordinates whatever the model
  put in the arguments.
- Every API call goes through the 45 s client from ``llm.py``; any exception
  or the step cap sets ``fell_back=True`` and returns what was gathered.

``plan_agents_via_gc`` is the orchestrator entry point (Session 9 phase 2):
it returns ``(shops, parts)`` in the shape ``_run_discover`` produces, or
``None`` when there is nothing, so the caller can run the old path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    from .llm import DEFAULT_MODEL, _client, _sanitize_params
except ImportError:  # pragma: no cover - script / flat import
    try:
        from llm import DEFAULT_MODEL, _client, _sanitize_params  # type: ignore[no-redef]
    except ImportError:
        from server.llm import DEFAULT_MODEL, _client, _sanitize_params  # type: ignore[no-redef]

try:
    from . import gc_usage as _usage
except ImportError:  # pragma: no cover
    try:
        import gc_usage as _usage  # type: ignore[no-redef]
    except ImportError:
        try:
            from server import gc_usage as _usage  # type: ignore[no-redef]
        except ImportError:
            _usage = None  # type: ignore[assignment]

try:
    from . import discovery as _discovery
except ImportError:  # pragma: no cover
    try:
        import discovery as _discovery  # type: ignore[no-redef]
    except ImportError:
        try:
            from server import discovery as _discovery  # type: ignore[no-redef]
        except ImportError:
            _discovery = None  # type: ignore[assignment]

try:
    from . import web_agent as _web_agent
except ImportError:  # pragma: no cover
    try:
        import web_agent as _web_agent  # type: ignore[no-redef]
    except ImportError:
        try:
            from server import web_agent as _web_agent  # type: ignore[no-redef]
        except ImportError:
            _web_agent = None  # type: ignore[assignment]

try:
    from .models import Agent, Business, Facts
except ImportError:  # pragma: no cover
    try:
        from models import Agent, Business, Facts  # type: ignore[no-redef]
    except ImportError:
        from server.models import Agent, Business, Facts  # type: ignore[no-redef]

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "gc_loop.md"

STAGE = "gc_loop"
DEFAULT_MAX_STEPS = 8
TEMPERATURE = 0.1
MAX_TOKENS = 300

SHOP_KEYS = ("name", "phone", "url", "type")
PART_KEYS = ("seller", "url", "price", "partsType")
_SHOP_TYPES = ("mechanic", "dealer", "parts")
_PRICE_TOLERANCE = 0.005

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "discover_shops",
            "description": (
                "Find nearby auto repair shops and dealers on Google Maps. "
                "Its results are the ONLY allowed source of shops, names and "
                "phone numbers; never list a shop it did not return."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Task latitude, exactly as given."},
                    "lng": {"type": "number", "description": "Task longitude, exactly as given."},
                    "query": {
                        "type": "string",
                        "description": "Maps search, e.g. 'brake repair 2019 Camry Fremont'.",
                    },
                },
                "required": ["lat", "lng", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_part",
            "description": (
                "Look up online prices for a car part on Google Shopping. "
                "Its results are the ONLY allowed source of part prices and "
                "sellers; never state a price it did not return."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Shopping search, e.g. '2019 Camry front brake pads and rotors'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
]

ToolFn = Callable[..., Any]
CompleteFn = Callable[[list[dict[str, Any]]], Any]


@dataclass
class GcLoopResult:
    shops: list[dict[str, Any]] = field(default_factory=list)
    parts: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_message: str | None = None
    fell_back: bool = False
    usage: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #


def _system_prompt() -> str:
    if _PROMPT_PATH.is_file():
        return _PROMPT_PATH.read_text(encoding="utf-8")
    return (
        "You order real-world lookups for a brake quote task. Call discover_shops "
        "with the task's coordinates, then lookup_part for the vehicle's front brake "
        "pads and rotors, then reply with one sentence. Never list a shop or price a "
        "tool did not return."
    )


def _plan_subset(plan: Mapping[str, Any] | None) -> dict[str, Any]:
    plan = plan or {}
    city = plan.get("city") or plan.get("location")
    facts = plan.get("factsNeeded")
    return {
        "title": plan.get("title"),
        "vehicle": plan.get("vehicle"),
        "city": city,
        "factsNeeded": list(facts) if isinstance(facts, (list, tuple)) else [],
        "businessCount": plan.get("businessCount"),
    }


def shop_query(plan: Mapping[str, Any] | None, task_request: str = "") -> str:
    """Maps query from the plan: ``"brake repair 2019 Camry Fremont"``."""
    subset = _plan_subset(plan)
    lower = (task_request or "").lower()
    job = "brake repair" if ("brake" in lower or not lower) else "car repair"
    bits = [job, subset.get("vehicle") or "", subset.get("city") or ""]
    return " ".join(str(b).strip() for b in bits if b).strip()


def part_query(plan: Mapping[str, Any] | None, task_request: str = "") -> str:
    """Shopping query from the plan: ``"2019 Camry front brake pads and rotors"``."""
    vehicle = str((plan or {}).get("vehicle") or "").strip()
    lower = (task_request or "").lower()
    axle = "rear" if ("rear" in lower and "front" not in lower) else "front"
    return f"{vehicle} {axle} brake pads and rotors".strip()


def _user_message(task_request: str, plan: Mapping[str, Any] | None, lat: float, lng: float) -> str:
    payload = {
        "request": task_request,
        "plan": _plan_subset(plan),
        "lat": lat,
        "lng": lng,
        "suggested_shop_query": shop_query(plan, task_request),
        "suggested_part_query": part_query(plan, task_request),
    }
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #


def _digits(value: Any) -> str:
    return "".join(c for c in str(value or "") if c.isdigit())


def _norm_name(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _clean_shop(raw: Any) -> dict[str, Any] | None:
    """Tool / model row → ``{"name", "phone", "url", "type"}`` or None. Drops price keys."""
    if not isinstance(raw, Mapping):
        return None
    name = str(raw.get("name") or "").strip()
    phone = str(raw.get("phone") or "").strip()
    if not name or not phone:
        return None
    kind = raw.get("type") or "mechanic"
    if kind not in _SHOP_TYPES:
        kind = "mechanic"
    url = raw.get("url")
    url = str(url).strip() if url else None
    return {"name": name, "phone": phone, "url": url or None, "type": kind}


def _clean_part(raw: Any) -> dict[str, Any] | None:
    """Tool / model row → ``{"seller", "url", "price", "partsType"}`` or None."""
    if not isinstance(raw, Mapping):
        return None
    price = raw.get("price")
    if price is None or isinstance(price, bool):
        return None
    if isinstance(price, (int, float)):
        price = float(price)
    else:
        try:
            price = float(str(price).replace("$", "").replace(",", ""))
        except ValueError:
            return None
    seller = str(raw.get("seller") or "").strip()
    if not seller:
        return None
    url = raw.get("url")
    return {
        "seller": seller,
        "url": str(url) if url else None,
        "price": round(price, 2),
        "partsType": "oem" if raw.get("partsType") == "oem" else "aftermarket",
    }


def _same_shop(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    da, db = _digits(a.get("phone")), _digits(b.get("phone"))
    if da and db and da == db:
        return True
    na = _norm_name(a.get("name"))
    return bool(na) and na == _norm_name(b.get("name"))


def _same_part(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    if _norm_name(a.get("seller")) != _norm_name(b.get("seller")):
        return False
    try:
        return abs(float(a.get("price")) - float(b.get("price"))) <= _PRICE_TOLERANCE
    except (TypeError, ValueError):
        return False


def validate_against_tools(
    candidate_shops: list[Any],
    tool_shops: list[Any],
) -> list[dict[str, Any]]:
    """Keep only candidates that ``discover_shops`` actually returned.

    A candidate matches by phone digits or by name (case-insensitive). The
    returned record is the *tool's* record, so nothing the model added (an
    address, a price) survives. Deduped; candidate order preserved.
    """
    tool_clean = [s for s in (_clean_shop(t) for t in tool_shops or []) if s]
    out: list[dict[str, Any]] = []
    for raw in candidate_shops or []:
        if not isinstance(raw, Mapping):
            continue
        # A proposed shop may carry only a name or only a phone; either is
        # enough to look it up, and the tool record is what gets returned.
        cand = {"name": str(raw.get("name") or "").strip(), "phone": str(raw.get("phone") or "").strip()}
        if not cand["name"] and not cand["phone"]:
            continue
        match = next((t for t in tool_clean if _same_shop(cand, t)), None)
        if match is None:
            log.info("gc_loop dropped shop not returned by a tool: %s", cand.get("name"))
            continue
        if not any(_same_shop(match, kept) for kept in out):
            out.append(dict(match))
    return out


def validate_prices(
    candidate_parts: list[Any],
    tool_parts: list[Any],
) -> list[dict[str, Any]]:
    """Keep only candidate parts whose seller + price ``lookup_part`` returned.

    Returns the tool's record for each match (never the candidate's), deduped.
    """
    tool_clean = [p for p in (_clean_part(t) for t in tool_parts or []) if p]
    out: list[dict[str, Any]] = []
    for raw in candidate_parts or []:
        cand = _clean_part(raw)
        if cand is None:
            continue
        match = next((t for t in tool_clean if _same_part(cand, t)), None)
        if match is None:
            log.info(
                "gc_loop dropped price not returned by a tool: %s $%s",
                cand.get("seller"),
                cand.get("price"),
            )
            continue
        if not any(_same_part(match, kept) for kept in out):
            out.append(dict(match))
    return out


def _merge_shops(existing: list[dict[str, Any]], incoming: list[Any]) -> list[dict[str, Any]]:
    out = list(existing)
    for raw in incoming or []:
        shop = _clean_shop(raw)
        if shop is None or any(_same_shop(shop, kept) for kept in out):
            continue
        out.append(shop)
    return out


def _merge_parts(existing: list[dict[str, Any]], incoming: list[Any]) -> list[dict[str, Any]]:
    out = list(existing)
    for raw in incoming or []:
        part = _clean_part(raw)
        if part is None or any(_same_part(part, kept) for kept in out):
            continue
        out.append(part)
    return out


# --------------------------------------------------------------------------- #
# Tools and the chat call
# --------------------------------------------------------------------------- #


def _default_tools() -> dict[str, ToolFn]:
    tools: dict[str, ToolFn] = {}
    discover = getattr(_discovery, "discover_shops_tool", None)
    if callable(discover):
        tools["discover_shops"] = discover
    lookup = getattr(_web_agent, "lookup_part_tool", None)
    if callable(lookup):
        tools["lookup_part"] = lookup
    return tools


def _model_name(model: str | None = None) -> str:
    return model or os.getenv("GENERAL_COMPUTE_MODEL", DEFAULT_MODEL)


def _api_complete(messages: list[dict[str, Any]], *, model: str) -> Any:
    """One non-streaming chat call with the two tools. Raises on any failure."""
    params: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }
    return _client().chat.completions.create(**_sanitize_params(params))


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _token_count(usage: Any, *names: str) -> int | None:
    for name in names:
        value = _get(usage, name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _normalize_response(response: Any) -> dict[str, Any]:
    """OpenAI response object / dict → ``{"content", "tool_calls", "usage"}``.

    ``tool_calls`` entries are ``{"id", "name", "arguments"}`` with
    ``arguments`` left as the raw string or dict. A flat dict that already
    has ``content`` / ``tool_calls`` at the top level is accepted too (tests).
    """
    choices = _get(response, "choices") or []
    message = _get(choices[0], "message") if choices else response
    content = _get(message, "content")
    raw_calls = _get(message, "tool_calls") or []
    calls: list[dict[str, Any]] = []
    for index, call in enumerate(raw_calls):
        fn = _get(call, "function")
        name = _get(fn, "name") if fn is not None else _get(call, "name")
        arguments = _get(fn, "arguments") if fn is not None else _get(call, "arguments")
        call_id = _get(call, "id") or f"call_{index}"
        if not name:
            continue
        calls.append({"id": str(call_id), "name": str(name), "arguments": arguments})
    raw_usage = _get(response, "usage")
    usage = {
        "model": _get(response, "model") or None,
        "prompt_tokens": _token_count(raw_usage, "prompt_tokens", "input_tokens"),
        "completion_tokens": _token_count(raw_usage, "completion_tokens", "output_tokens"),
    }
    text = str(content).strip() if content else ""
    return {"content": text or None, "tool_calls": calls, "usage": usage}


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _assistant_message(normalized: dict[str, Any]) -> dict[str, Any]:
    """The assistant turn we echo back so the API can continue after ``role: tool``."""
    message: dict[str, Any] = {"role": "assistant", "content": normalized.get("content") or ""}
    if normalized["tool_calls"]:
        calls = []
        for call in normalized["tool_calls"]:
            arguments = call["arguments"]
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments or {})
            calls.append(
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": arguments},
                }
            )
        message["tool_calls"] = calls
    return message


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _run_tool(
    name: str,
    arguments: dict[str, Any],
    tools: Mapping[str, ToolFn],
    lat: float,
    lng: float,
    plan: Mapping[str, Any] | None,
    task_request: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Execute one tool. Returns ``(rows, error)``; never raises."""
    fn = tools.get(name)
    if fn is None:
        return [], f"unknown tool: {name}"
    try:
        if name == "discover_shops":
            # The task's coordinates win; the model may not move the search.
            if _num(arguments.get("lat")) != lat or _num(arguments.get("lng")) != lng:
                log.info(
                    "gc_loop pinned discover_shops to task coordinates (model sent %s, %s)",
                    arguments.get("lat"),
                    arguments.get("lng"),
                )
            query = str(arguments.get("query") or "").strip() or shop_query(plan, task_request)
            rows = fn(lat, lng, query)
        elif name == "lookup_part":
            query = str(arguments.get("query") or "").strip() or part_query(plan, task_request)
            rows = fn(query)
        else:
            rows = fn(**arguments)
    except Exception as exc:  # a failed lookup is a tool error, not a crash
        log.warning("gc_loop tool %s failed: %s", name, exc)
        return [], str(exc) or exc.__class__.__name__
    if not isinstance(rows, list):
        rows = list(rows) if rows else []
    return [r for r in rows if isinstance(r, Mapping)], None


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def run_gc_loop(
    task_request: str,
    plan: dict[str, Any],
    lat: float,
    lng: float,
    *,
    task_id: str | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    tools: Mapping[str, ToolFn] | None = None,
    complete: CompleteFn | None = None,
    model: str | None = None,
) -> GcLoopResult:
    """Let Gemma order ``discover_shops`` / ``lookup_part``; collect tool output.

    ``tools`` maps tool name → callable (defaults to the two wrappers);
    ``complete`` takes the message list and returns an OpenAI-style response
    (defaults to the General Compute call). Both exist for tests.

    ``shops`` / ``parts`` in the result are exactly what the tools returned,
    deduped. The model's text is only ever ``final_message``.
    """
    lat, lng = float(lat), float(lng)
    tools = dict(tools) if tools is not None else _default_tools()
    model = _model_name(model)
    max_steps = max(1, int(max_steps))
    result = GcLoopResult()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": _user_message(task_request, plan, lat, lng)},
    ]
    tool_shops: list[dict[str, Any]] = []
    tool_parts: list[dict[str, Any]] = []

    for step in range(1, max_steps + 1):
        started = time.monotonic()
        try:
            if complete is not None:
                response = complete(messages)
            else:
                response = _api_complete(messages, model=model)
            normalized = _normalize_response(response)
        except Exception as exc:
            log.warning("gc_loop step %d: API call failed (%s); falling back", step, exc)
            result.fell_back = True
            break
        result.steps = step

        usage = dict(normalized["usage"])
        usage["model"] = usage.get("model") or model
        usage["latency_s"] = round(time.monotonic() - started, 3)
        usage["json_mode"] = "tools"
        usage["step"] = step
        usage["tool_calls"] = [c["name"] for c in normalized["tool_calls"]]
        result.usage.append(usage)
        if _usage is not None:
            try:
                _usage.record(STAGE, usage, task_id=task_id)
            except Exception:
                pass

        messages.append(_assistant_message(normalized))
        if not normalized["tool_calls"]:
            result.final_message = normalized["content"]
            log.info(
                "gc_loop step %d: final message, %d shops, %d parts",
                step,
                len(tool_shops),
                len(tool_parts),
            )
            break

        for call in normalized["tool_calls"]:
            arguments = _parse_arguments(call["arguments"])
            rows, error = _run_tool(call["name"], arguments, tools, lat, lng, plan, task_request)
            if call["name"] == "discover_shops":
                tool_shops = _merge_shops(tool_shops, rows)
            elif call["name"] == "lookup_part":
                tool_parts = _merge_parts(tool_parts, rows)
            entry: dict[str, Any] = {
                "name": call["name"],
                "arguments": arguments,
                "result_count": len(rows),
            }
            if error:
                entry["error"] = error
            result.tool_calls.append(entry)
            log.info(
                "gc_loop step %d: %s -> %d results%s",
                step,
                call["name"],
                len(rows),
                f" ({error})" if error else "",
            )
            body = {"error": error, "results": rows} if error else {"results": rows}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": json.dumps(body, ensure_ascii=False),
                }
            )
    else:
        log.warning("gc_loop hit max_steps=%d without a final message; falling back", max_steps)
        result.fell_back = True

    # Shops and parts are the tool output, validated against itself so the
    # same helper guards any future path where the model proposes a list.
    result.shops = validate_against_tools(tool_shops, tool_shops)
    result.parts = validate_prices(tool_parts, tool_parts)
    return result


# --------------------------------------------------------------------------- #
# Orchestrator entry points (phase 2 wiring; nothing calls these yet)
# --------------------------------------------------------------------------- #


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def plan_agents_via_gc(
    task: Any,
    plan: dict[str, Any],
    loc: Any,
    **loop_kwargs: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Run the loop for a task; ``None`` when it produced nothing.

    ``shops`` come back as ``{"name", "phone", "url", "type"}`` (``url`` is
    ``None`` when unknown, ``type`` one of mechanic / dealer / parts), capped
    by the plan's ``businessCount`` like ``orchestrator._run_discover``, so
    ``_insert_call_agents`` can take them unchanged. ``parts`` are the
    ``lookup_part`` rows. ``loop_kwargs`` are passed to ``run_gc_loop``
    (tests inject ``tools`` / ``complete``).
    """
    lat, lng = _num(_field(loc, "lat")), _num(_field(loc, "lng"))
    if lat is None or lng is None:
        return None
    request = str(_field(task, "request") or "")
    task_id = _field(task, "id")
    task_id = str(task_id) if task_id else None

    result = run_gc_loop(request, plan or {}, lat, lng, task_id=task_id, **loop_kwargs)
    if not result.shops and not result.parts:
        log.info(
            "gc_loop gathered nothing (fell_back=%s); orchestrator should use the old path",
            result.fell_back,
        )
        return None

    cap = (plan or {}).get("businessCount")
    try:
        n = int(cap) if cap is not None else 8
    except (TypeError, ValueError):
        n = 8
    n = max(1, min(8, n))
    shops = [s for s in (_clean_shop(s) for s in result.shops) if s][:n]
    return shops, list(result.parts)


def web_agents_from_parts(task: Any, parts: list[dict[str, Any]]) -> list[Agent]:
    """``lookup_part`` rows → ``kind=web`` agents in ``web_agent.run_web_agent`` shape.

    Reuses the task's queued web slot id for the first agent. Only the rows'
    own prices reach ``Facts.partPrice``; nothing else is written.
    """
    task_id = str(_field(task, "id") or "unknown")
    request = str(_field(task, "request") or "")
    label = "pads + rotors" if "brake" in request.lower() else "part"
    slot_id: str | None = None
    for agent in _field(task, "agents", None) or []:
        if _field(agent, "kind") == "web" and _field(agent, "id"):
            slot_id = str(_field(agent, "id"))
            break

    new_id = getattr(_web_agent, "_new_agent_id", None)
    agents: list[Agent] = []
    for index, raw in enumerate(parts or []):
        part = _clean_part(raw)
        if part is None:
            continue
        if index == 0 and slot_id:
            agent_id = slot_id
        elif callable(new_id):
            agent_id = new_id()
        else:
            agent_id = f"a_web_gc_{index}"
        kind_label = "OEM" if part["partsType"] == "oem" else "Aftermarket"
        agents.append(
            Agent(
                id=agent_id,
                taskId=task_id,
                kind="web",
                status="done",
                business=Business(name=part["seller"], type="parts", url=part["url"]),
                summary=f"{kind_label} {label} ${part['price']:g}",
                facts=Facts(partPrice=part["price"], partsType=part["partsType"], confidence=0.75),
            )
        )
    return agents
