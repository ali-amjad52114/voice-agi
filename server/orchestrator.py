"""Session 2 orchestrator v1: plan → locate → discover → queue. Do not dial."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable
from uuid import uuid4

from .models import (
    Agent,
    AgentUpdatedEvent,
    Business,
    CallInfo,
    ErrorEvent,
    Location,
    Result,
    Task,
    TaskResultEvent,
    TaskUpdatedEvent,
)

try:
    from . import planner as _planner
except ImportError:  # pragma: no cover
    _planner = None

try:
    from . import location as _location
except ImportError:  # pragma: no cover
    _location = None

try:
    from . import discovery as _discovery
except ImportError:  # pragma: no cover
    _discovery = None

try:
    from . import db as _db
except ImportError:  # pragma: no cover
    _db = None

try:
    from . import web_agent as _web_agent
except ImportError:  # pragma: no cover
    _web_agent = None

EventCallback = Callable[..., Any]


def on_task_created(
    task_id: str | Task,
    events: EventCallback | None = None,
) -> list[str]:
    """Plan, resolve location, discover shops, queue call agents, spawn web slot.

    Call agents stay ``queued`` with no prices. Does not dial Twilio and does
    not invent ``allInPrice``. Optional ``events`` receives task/agent events.
    Returns the flow steps that completed.
    """
    steps: list[str] = []
    tid = _as_task_id(task_id)
    task = _load_task(tid)
    if task is None and isinstance(task_id, Task):
        task = task_id
    if task is None:
        _emit(events, tid, ErrorEvent(message=f"task not found: {tid}"))
        return steps

    try:
        plan = _run_plan(task)
        steps.append("plan")
        _apply_plan(task, plan)
        _persist_task_fields(task)
        _emit(events, tid, TaskUpdatedEvent(task=_snapshot(task)))

        loc = _run_resolve_location(task, plan)
        steps.append("resolve_location")
        task.location = loc
        _persist_task_fields(task)
        _emit(events, tid, TaskUpdatedEvent(task=_snapshot(task)))

        shops = _run_discover(task, plan, loc)
        steps.append("discover")

        call_agents = _insert_call_agents(task, shops)
        steps.append("insert_call_agents")
        for agent in call_agents:
            _emit(events, tid, AgentUpdatedEvent(agent=agent))

        web = _spawn_web_agent(task)
        steps.append("spawn_web_agent")
        if web is not None:
            _replace_web_agent(task, web)
            _persist_agent(web)
            _emit(events, tid, AgentUpdatedEvent(agent=web))

        task.status = "running"
        _persist_task_fields(task)
        if _db is not None:
            fn = getattr(_db, "update_task_status", None)
            if callable(fn):
                try:
                    refreshed = fn(tid, "running")
                    if refreshed is not None:
                        task = _as_task(refreshed)
                except Exception:
                    pass
        _emit(events, tid, TaskUpdatedEvent(task=_snapshot(task)))

        _run_calls_then_complete(task, events)
        steps.append("dial_extract_complete")
    except Exception as exc:
        _emit(events, tid, ErrorEvent(message=str(exc)))
        task.status = "failed"
        try:
            _persist_task_fields(task)
            if _db is not None:
                fn = getattr(_db, "update_task_status", None)
                if callable(fn):
                    fn(tid, "failed")
        except Exception:
            pass
        _emit(events, tid, TaskUpdatedEvent(task=_snapshot(task)))

    return steps


def _as_task_id(value: str | Task | dict[str, Any]) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value["id"])
    return str(value.id)


def _as_task(value: Any) -> Task:
    if isinstance(value, Task):
        return value
    return Task.model_validate(value)


def _mod_fn(mod: Any, *names: str) -> Any | None:
    if mod is None:
        return None
    for name in names:
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    return None


def _load_task(task_id: str) -> Task | None:
    fn = _mod_fn(_db, "get_task", "load_task")
    if fn is not None:
        try:
            row = fn(task_id)
        except Exception:
            row = None
        if row is not None:
            return _as_task(row)
    try:
        from . import api as _api

        row = _api.load_task(task_id)
        return _as_task(row) if row is not None else None
    except Exception:
        return None


def _persist_task_fields(task: Task) -> None:
    """Write title / userQuote / location / status. Agents persist separately."""
    fn = _mod_fn(_db, "save_task", "update_task", "upsert_task")
    if fn is not None:
        try:
            fn(task)
            return
        except Exception:
            pass
    if _db is not None:
        get_client = getattr(_db, "get_client", None)
        if callable(get_client):
            loc = None
            if task.location is not None:
                loc = task.location.model_dump(mode="json", exclude_none=True)
            try:
                get_client().table("tasks").update(
                    {
                        "title": task.title,
                        "user_quote": task.userQuote,
                        "location": loc,
                        "status": task.status,
                    }
                ).eq("id", task.id).execute()
                return
            except Exception:
                pass
    try:
        from . import api as _api

        saver = getattr(_api, "save_task", None)
        if callable(saver):
            saver(task)
    except Exception:
        pass


def _persist_agent(agent: Agent) -> None:
    fn = _mod_fn(_db, "upsert_agent", "save_agent", "insert_agent")
    if fn is not None:
        try:
            fn(agent)
            return
        except Exception:
            pass
    try:
        from . import api as _api

        task = _api.load_task(agent.taskId)
        if task is None:
            return
        replaced = False
        next_agents: list[Agent] = []
        for existing in task.agents:
            if existing.id == agent.id:
                next_agents.append(agent)
                replaced = True
            else:
                next_agents.append(existing)
        if not replaced:
            next_agents.append(agent)
        task.agents = next_agents
        _api.save_task(task)
    except Exception:
        pass


def _run_plan(task: Task) -> dict[str, Any]:
    fn = _mod_fn(_planner, "plan", "plan_task", "plan_utterance")
    if fn is None:
        return {}
    hint = None
    if task.location is not None:
        hint = task.location.label
    try:
        raw = fn(task.request, hint)
    except TypeError:
        raw = fn(task.request)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return {}


def _apply_plan(task: Task, plan: dict[str, Any]) -> None:
    title = plan.get("title")
    if title:
        task.title = str(title).strip() or task.title
    if "userQuote" in plan:
        quote = plan.get("userQuote")
        task.userQuote = float(quote) if quote is not None else None


def _run_resolve_location(task: Task, plan: dict[str, Any]) -> Location:
    fn = _mod_fn(_location, "resolve_location")
    lat = task.location.lat if task.location else None
    lng = task.location.lng if task.location else None
    city = None
    if task.location and task.location.label:
        city = task.location.label
    city = city or plan.get("location")
    if fn is None:
        if lat is None or lng is None:
            raise RuntimeError("location.resolve_location is not available")
        return Location(lat=float(lat), lng=float(lng), label=city)
    resolved = fn(lat=lat, lng=lng, location_str=city)
    if isinstance(resolved, Location):
        return resolved
    return Location.model_validate(resolved)


def _run_discover(task: Task, plan: dict[str, Any], loc: Location) -> list[dict[str, Any]]:
    fn = _mod_fn(_discovery, "discover_shops", "discover", "find_shops")
    if fn is None:
        return []
    try:
        shops = fn(loc.lat, loc.lng, task.request)
    except TypeError:
        shops = fn(lat=loc.lat, lng=loc.lng, utterance=task.request)
    cap = plan.get("businessCount")
    try:
        n = int(cap) if cap is not None else 8
    except (TypeError, ValueError):
        n = 8
    n = max(1, min(8, n))
    out: list[dict[str, Any]] = []
    for shop in shops or []:
        mapped = _shop_dict(shop)
        if mapped is None:
            continue
        out.append(mapped)
        if len(out) >= n:
            break
    return out


def _shop_dict(shop: Any) -> dict[str, Any] | None:
    if isinstance(shop, dict):
        data = shop
    else:
        dump = getattr(shop, "model_dump", None)
        data = dump(mode="json") if callable(dump) else {
            "name": getattr(shop, "name", None),
            "phone": getattr(shop, "phone", None),
            "url": getattr(shop, "url", None),
            "type": getattr(shop, "type", None),
        }
    name = str(data.get("name") or "").strip()
    phone = str(data.get("phone") or "").strip()
    if not name or not phone:
        return None
    kind = data.get("type") or "mechanic"
    if kind not in ("mechanic", "dealer", "parts"):
        kind = "mechanic"
    url = data.get("url")
    url = str(url).strip() if url else None
    # Ignore any price keys from a shop payload — never copy into facts.
    return {"name": name, "phone": phone, "url": url or None, "type": kind}


def _insert_call_agents(task: Task, shops: list[dict[str, Any]]) -> list[Agent]:
    created: list[Agent] = []
    for shop in shops:
        agent = Agent(
            id=f"a_call_{uuid4().hex[:10]}",
            taskId=task.id,
            kind="call",
            status="queued",
            business=Business(
                name=shop["name"],
                type=shop["type"],
                phone=shop["phone"],
                url=shop.get("url"),
            ),
            summary=None,
            facts=None,
            call=None,
            transcript=None,
        )
        created.append(agent)
        _persist_agent(agent)
    existing = [a for a in task.agents if a.kind != "call"]
    task.agents = existing + created
    return created


def _queued_web_slot(task: Task) -> Agent:
    for agent in task.agents:
        if agent.kind == "web":
            return agent
    slot = Agent(
        id=f"a_web_{uuid4().hex[:10]}",
        taskId=task.id,
        kind="web",
        status="queued",
        business=Business(name="Parts lookup", type="parts"),
        summary=None,
        facts=None,
    )
    task.agents.append(slot)
    _persist_agent(slot)
    return slot


def _spawn_web_agent(task: Task) -> Agent | None:
    """One web slot whose job is part-price. Never fill allInPrice here."""
    _queued_web_slot(task)
    fn = _mod_fn(
        _web_agent,
        "run_web_agent",
        "spawn_web_agent",
        "create_web_agent",
        "start_web_agent",
    )
    if fn is None:
        # TODO: call a function in server/web_agent.py (part-price required)
        return next((a for a in task.agents if a.kind == "web"), None)
    try:
        result = fn(task)
    except TypeError:
        result = fn(task_id=task.id)
    if result is None:
        return next((a for a in task.agents if a.kind == "web"), None)
    agent = result if isinstance(result, Agent) else Agent.model_validate(result)
    if agent.facts is not None and agent.facts.allInPrice is not None:
        agent = agent.model_copy(
            update={"facts": agent.facts.model_copy(update={"allInPrice": None})}
        )
    return agent


def _replace_web_agent(task: Task, web: Agent) -> None:
    next_agents: list[Agent] = []
    replaced = False
    for agent in task.agents:
        if agent.kind == "web" and not replaced:
            next_agents.append(web)
            replaced = True
        elif agent.id == web.id:
            next_agents.append(web)
            replaced = True
        else:
            next_agents.append(agent)
    if not replaced:
        next_agents.append(web)
    task.agents = next_agents


def _apply_dial_records(
    task: Task,
    records: list[dict[str, Any]] | None,
    events: EventCallback | None,
) -> None:
    """Write dialer outcomes onto call agents. Never invent quotes."""
    if not records:
        return
    by_id = {str(r.get("agent_id") or ""): r for r in records if r}
    for agent in task.agents:
        rec = by_id.get(agent.id)
        if rec is None or agent.kind != "call":
            continue
        err = rec.get("error") or rec.get("skipped_reason")
        started = bool(rec.get("started"))
        twilio_status = str(rec.get("status") or "")
        outcome_raw = rec.get("outcome")
        if outcome_raw in ("quote", "voicemail", "refused", "error"):
            outcome = outcome_raw
        elif err:
            outcome = "error"
        elif twilio_status in ("no-answer", "busy"):
            outcome = "voicemail"
        elif started and twilio_status == "completed":
            # A human answered and the call ran to completion. The dialer only
            # labels machine / no-answer legs, so "no outcome" here means a
            # real conversation. Extraction decides whether it yielded a quote.
            outcome = "quote"
        else:
            outcome = "error"
        duration = float(rec.get("duration_s") or 0.0)
        agent.call = CallInfo(
            durationS=duration,
            answeredBy=rec.get("answered_by"),
            outcome=outcome,  # type: ignore[arg-type]
        )
        if started and twilio_status == "completed" and outcome != "error":
            from .summary import summary_from_facts

            agent.status = "done"
            # Voicemail → "Voicemail · no quote", refused → "Declined to quote",
            # answered → "Call answered · no quote" until extraction adds facts.
            agent.summary = summary_from_facts(agent)
        else:
            agent.status = "failed"
            agent.summary = str(err or twilio_status or outcome)
        _persist_agent(agent)
        _emit(events, task.id, AgentUpdatedEvent(agent=agent))


def _run_calls_then_complete(task: Task, events: EventCallback | None) -> None:
    """Dial (if Twilio is configured), extract any transcripts, then synthesize."""
    tid = task.id
    try:
        from .dialer import run_dialer

        records = run_dialer(tid, task.agents)
        _apply_dial_records(task, records, events)
    except Exception as exc:
        _emit(events, tid, ErrorEvent(message=f"dialer: {exc}"))

    task = _load_task(tid) or task
    try:
        from .extract import extract_facts

        for agent in task.agents:
            if agent.kind != "call" or not agent.transcript:
                continue
            try:
                facts = extract_facts(agent.transcript)
                agent.facts = facts if not hasattr(facts, "model_validate") else facts
                if not hasattr(agent.facts, "confidence"):
                    from .models import Facts

                    agent.facts = Facts.model_validate(facts)
                from .summary import summary_from_facts

                agent.summary = summary_from_facts(agent)
                _persist_agent(agent)
                _emit(events, tid, AgentUpdatedEvent(agent=agent))
            except Exception:
                continue
    except Exception as exc:
        _emit(events, tid, ErrorEvent(message=f"extract: {exc}"))

    try:
        from .synthesize import synthesize

        task = _load_task(tid) or task
        raw = synthesize(task)
        result = raw if isinstance(raw, Result) else Result.model_validate(raw)
        fn = _mod_fn(_db, "set_result")
        if fn is not None:
            try:
                fn(tid, result)
            except Exception:
                pass
        status_fn = _mod_fn(_db, "update_task_status")
        if status_fn is not None:
            try:
                status_fn(tid, "complete")
            except Exception:
                pass
        task.result = result
        task.status = "complete"
        _persist_task_fields(task)
        try:
            from . import api as _api

            _api.save_task(task)
        except Exception:
            pass
        _emit(events, tid, TaskResultEvent(taskId=tid, result=result))
        _emit(events, tid, TaskUpdatedEvent(task=_snapshot(task)))
    except Exception as exc:
        _emit(events, tid, ErrorEvent(message=f"complete: {exc}"))


def _snapshot(task: Task) -> Task:
    fresh = _load_task(task.id)
    return fresh if fresh is not None else task


def _emit(events: EventCallback | None, task_id: str, event: Any) -> None:
    if events is None:
        return
    payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
    result = None
    try:
        result = events(payload)
    except TypeError:
        try:
            result = events(task_id, payload)
        except TypeError:
            result = events(event)
    if inspect.isawaitable(result):
        try:
            asyncio.get_running_loop().create_task(result)
        except RuntimeError:
            result.close()
