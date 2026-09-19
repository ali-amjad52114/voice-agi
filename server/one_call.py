"""Dial exactly one Voice AGI call agent.

Destination: env ``TWILIO_DEMO_TO`` if set, else the first agent's
``business.phone``. Never invent a number or a quote.

This module only starts the call. Facts / prices stay empty until extract
runs on a real transcript.
"""

from __future__ import annotations

import os
from typing import Any, Sequence

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:
    _here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_here, ".env"), override=False)
    load_dotenv(os.path.join(os.path.dirname(_here), ".env"), override=False)

try:
    from .twilio_call import start_call
except ImportError:  # pragma: no cover
    try:
        from twilio_call import start_call
    except ImportError:
        try:
            from server.twilio_call import start_call
        except ImportError:
            start_call = None  # type: ignore[assignment]

try:
    from . import db
except ImportError:  # pragma: no cover
    try:
        import db
    except ImportError:
        try:
            from server import db
        except ImportError:
            db = None  # type: ignore[assignment]


def demo_to() -> str | None:
    """Owner-cell number from env. No default: never hardcode a phone number."""
    value = (os.getenv("TWILIO_DEMO_TO") or "").strip()
    return value or None


def _as_mapping(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    dump = getattr(obj, "dict", None)
    if callable(dump):
        return dump()
    return {
        "id": getattr(obj, "id", None),
        "business": getattr(obj, "business", None),
    }


def _business(agent: Any) -> dict[str, Any]:
    raw = _as_mapping(agent).get("business")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    return {
        "name": getattr(raw, "name", None),
        "phone": getattr(raw, "phone", None),
    }


def _agent_id(agent: Any) -> str | None:
    mapped = _as_mapping(agent)
    value = mapped.get("id")
    if value is None:
        value = getattr(agent, "id", None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _agent_phone(agent: Any) -> str | None:
    phone = _business(agent).get("phone")
    if phone is None:
        return None
    text = str(phone).strip()
    return text or None


def first_agent_phone(agents: Sequence[Any] | None) -> str | None:
    """``business.phone`` on the first agent, if any."""
    if not agents:
        return None
    return _agent_phone(agents[0])


def _load_agents(task_id: str) -> list[Any]:
    if db is None:
        return []
    task = None
    for name in ("get_task", "load_task", "fetch_task", "get_task_by_id"):
        fn = getattr(db, name, None)
        if callable(fn):
            task = fn(task_id)
            break
    if task is None:
        return []
    agents = getattr(task, "agents", None)
    if agents is None and isinstance(task, dict):
        agents = task.get("agents")
    return list(agents or [])


def resolve_to_number(agents: Sequence[Any] | None = None) -> str:
    """``TWILIO_DEMO_TO`` wins; otherwise first agent's Place phone."""
    dest = demo_to() or first_agent_phone(agents)
    if not dest:
        raise ValueError(
            "No destination: set TWILIO_DEMO_TO or give an agent with business.phone"
        )
    return dest


def run_one_call(
    task_id: str,
    agents: Sequence[Any] | None = None,
    *,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Ring one number for this task. Does not write prices or quotes.

    Args:
        task_id: Voice AGI task id passed through to ``start_call``.
        agents: Task agents. If omitted and ``db`` is importable, load them.
        agent_id: Agent to attach the call to. Defaults to the first agent's id.

    Returns:
        Whatever ``start_call`` returns (sid, status, to, ids).
    """
    if start_call is None:
        raise RuntimeError("twilio_call.start_call is not available")

    tid = (task_id or "").strip()
    if not tid:
        raise ValueError("task_id is required")

    loaded = list(agents) if agents is not None else _load_agents(tid)
    dest = resolve_to_number(loaded)

    aid = (agent_id or "").strip()
    if not aid and loaded:
        aid = _agent_id(loaded[0]) or ""
    if not aid:
        raise ValueError("agent_id is required when no agents are loaded")

    return start_call(dest, aid, tid)
