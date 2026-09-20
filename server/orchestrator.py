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
    ResultPartialEvent,
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

# Planner ``extractionSchema`` per task id, held in memory only for the run
# (no DB column, no model field). Stored after planning, consumed by the
# extraction loop in ``_run_calls_then_complete``.
_EXTRACTION_SCHEMAS: dict[str, dict[str, Any]] = {}


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
        _EXTRACTION_SCHEMAS[tid] = plan.get("extractionSchema") or {}
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
        if web:
            _replace_web_agent(task, web)
            for web_agent in web:
                _persist_agent(web_agent)
                _emit(events, tid, AgentUpdatedEvent(agent=web_agent))

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
            return _merge_memory_transcripts(_as_task(row))
    try:
        from . import api as _api

        row = _api.load_task(task_id)
        return _as_task(row) if row is not None else None
    except Exception:
        return None


def _merge_memory_transcripts(task: Task) -> Task:
    """Prefer the in-memory transcript for an agent when it is richer.

    ``api.apply_transcript_line`` appends each new line to Supabase, but an
    agent line that grew in place ("extending") is only updated in
    ``api._memory``, and a failed append is swallowed. The DB row would then
    be shorter than what the call actually said. Lines are never dropped:
    the longer of the two transcripts wins per agent.
    """
    try:
        from . import api as _api

        cached = getattr(_api, "_memory", {}).get(task.id)
    except Exception:
        return task
    if cached is None or not getattr(cached, "agents", None):
        return task
    memory_by_id = {a.id: a for a in cached.agents if a.transcript}
    if not memory_by_id:
        return task
    for agent in task.agents:
        mem = memory_by_id.get(agent.id)
        if mem is None:
            continue
        if _transcript_weight(mem.transcript) > _transcript_weight(agent.transcript):
            agent.transcript = list(mem.transcript or [])
    return task


def _transcript_weight(lines: list[Any] | None) -> tuple[int, int]:
    if not lines:
        return (0, 0)
    return (len(lines), sum(len(getattr(line, "text", "") or "") for line in lines))


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


def _spawn_web_agent(task: Task) -> list[Agent]:
    """Run the part lookup once. Returns every web agent it produced.

    ``web_agent.run_web_agent`` returns up to three ``kind=web`` agents (one per
    online source) or a single ``failed`` one. Call agents are never touched
    and ``allInPrice`` is never written on a web agent.
    """
    slot = _queued_web_slot(task)
    fn = _mod_fn(
        _web_agent,
        "run_web_agent",
        "spawn_web_agent",
        "create_web_agent",
        "start_web_agent",
    )
    if fn is None:
        return [slot]
    try:
        result = fn(task)
    except TypeError:
        result = fn(task_id=task.id)
    if result is None:
        return [slot]
    raw_items = list(result) if isinstance(result, (list, tuple)) else [result]
    agents: list[Agent] = []
    for item in raw_items:
        if item is None:
            continue
        agent = item if isinstance(item, Agent) else Agent.model_validate(item)
        if agent.kind != "web":
            continue
        agents.append(agent)
    return agents or [slot]


def _replace_web_agent(task: Task, web: list[Agent] | Agent) -> None:
    """Swap the queued web slot for the lookup's agents. Call agents untouched."""
    incoming = list(web) if isinstance(web, (list, tuple)) else [web]
    incoming = [a for a in incoming if a is not None]
    next_agents: list[Agent] = []
    inserted = False
    for agent in task.agents:
        if agent.kind != "web":
            next_agents.append(agent)
            continue
        if not inserted:
            next_agents.extend(incoming)
            inserted = True
        # Any other pre-existing web agent (the old queued slot) is dropped.
    if not inserted:
        next_agents.extend(incoming)
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

        # Planner schema remembered at plan time; merged into the Facts schema
        # by extract_facts. Missing or malformed → default schema.
        schema = _EXTRACTION_SCHEMAS.pop(tid, None)
        if not isinstance(schema, dict) or not schema:
            schema = None
        for agent in task.agents:
            if agent.kind != "call" or not agent.transcript:
                continue
            try:
                facts = extract_facts(agent.transcript, schema=schema)
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
        # The first model attempt streams; each new piece of its ``why``
        # goes out as result.partial from this worker thread (the api-side
        # publisher hands it to the loop with run_coroutine_threadsafe).
        # The verified task.result below replaces whatever was streamed.
        raw = synthesize(
            task,
            on_why_delta=lambda delta: _emit(events, tid, ResultPartialEvent(taskId=tid, whyDelta=delta)),
        )
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
