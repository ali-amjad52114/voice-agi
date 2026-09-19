from __future__ import annotations

import asyncio
import inspect
import threading
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect

from .events import hub
from .models import (
    Agent,
    AgentUpdatedEvent,
    BookBody,
    CreateTaskBody,
    ErrorEvent,
    Location,
    Task,
    TaskEvent,
    TaskResultEvent,
    TaskUpdatedEvent,
    TranscriptLine,
)

router = APIRouter()

_memory: dict[str, Task] = {}

try:
    from . import db as _db
except Exception:
    _db = None

try:
    from .orchestrator import on_task_created as _on_task_created
except Exception:
    _on_task_created = None

try:
    from .twilio_call import voice_twiml as _voice_twiml
except Exception:
    _voice_twiml = None


def _db_fn(*names: str):
    if _db is None:
        return None
    for name in names:
        fn = getattr(_db, name, None)
        if callable(fn):
            return fn
    return None


def _as_task(value) -> Task:
    if isinstance(value, Task):
        return value
    return Task.model_validate(value)


def _as_agent(value) -> Agent:
    if isinstance(value, Agent):
        return value
    return Agent.model_validate(value)


def save_task(task: Task) -> Task:
    persist = _db_fn("save_task", "upsert_task")
    if persist is not None:
        try:
            result = persist(task)
            saved = _as_task(result) if result is not None else task
            _memory[saved.id] = saved
            return saved
        except Exception:
            pass
    create = _db_fn("create_task")
    if create is not None and task.id not in _memory:
        try:
            result = create(
                task.request,
                title=task.title,
                user_quote=task.userQuote,
                location=task.location,
                status=task.status,
                task_id=task.id,
            )
            saved = _as_task(result) if result is not None else task
            _memory[saved.id] = saved
            return saved
        except Exception:
            pass
    _memory[task.id] = task
    return task


def load_task(task_id: str, *, refresh: bool = False) -> Task | None:
    """Return a task. ``refresh=True`` re-reads Supabase so agents written by the
    orchestrator thread (which only touches the DB) are visible here."""
    cached = _memory.get(task_id)
    if cached is not None and not refresh:
        return cached
    fn = _db_fn("load_task", "get_task")
    if fn is not None:
        try:
            row = fn(task_id)
            if row is not None:
                task = _as_task(row)
                _memory[task.id] = task
                return task
        except Exception:
            pass
    return cached


_transcript_lock = threading.Lock()


def apply_transcript_line(
    task_id: str,
    agent_id: str,
    role: str,
    text: str,
    t: float,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> Agent | None:
    """Append one spoken line, persist it, and push agent.updated to the UI.

    Safe to call from a worker thread (the call pipeline offloads it so the
    Supabase write never blocks the event loop). Pass ``loop`` from a thread
    so the event still reaches the websocket hub.
    """
    text = (text or "").strip()
    if not text or not task_id or not agent_id:
        return None
    with _transcript_lock:
        return _apply_transcript_line_locked(task_id, agent_id, role, text, t, loop)


def _apply_transcript_line_locked(
    task_id: str,
    agent_id: str,
    role: str,
    text: str,
    t: float,
    loop: asyncio.AbstractEventLoop | None,
) -> Agent | None:
    task = load_task(task_id)
    if task is None:
        return None
    agent = next((a for a in task.agents if a.id == agent_id), None)
    if agent is None:
        # The in-memory copy was cached at POST /tasks with no agents; the
        # orchestrator inserted them straight into Supabase. Re-read once.
        task = load_task(task_id, refresh=True)
        if task is None:
            return None
        agent = next((a for a in task.agents if a.id == agent_id), None)
        if agent is None:
            return None
    lines = list(agent.transcript or [])
    if lines and lines[-1].role == role and lines[-1].text == text:
        return agent
    extending = bool(
        lines
        and lines[-1].role == role
        and role == "agent"
        and text.startswith(lines[-1].text)
    )
    line = TranscriptLine(role=role, text=text, t=t)  # type: ignore[arg-type]
    if extending:
        lines[-1] = line
    else:
        lines.append(line)
    updated = agent.model_copy(update={"transcript": lines, "status": "active"})
    task.agents = [updated if existing.id == agent_id else existing for existing in task.agents]
    _memory[task.id] = task
    append = _db_fn("append_transcript")
    if append is not None and not extending:
        try:
            append(agent_id, line, task_id=task_id)
        except Exception:
            pass
    event = AgentUpdatedEvent(agent=updated)
    try:
        running = asyncio.get_running_loop()
        running.create_task(hub.publish(task_id, event))
    except RuntimeError:
        if loop is not None:
            asyncio.run_coroutine_threadsafe(hub.publish(task_id, event), loop)
    return updated


def list_tasks() -> list[Task]:
    fn = _db_fn("list_tasks")
    if fn is not None:
        try:
            return [_as_task(row) for row in (fn() or [])]
        except Exception:
            pass
    return list(_memory.values())


def load_agent(task_id: str, agent_id: str) -> Agent | None:
    fn = _db_fn("load_agent", "get_agent")
    if fn is not None:
        try:
            row = fn(task_id, agent_id)
            if row is not None:
                return _as_agent(row)
        except Exception:
            pass
    task = load_task(task_id)
    if task is None:
        return None
    for agent in task.agents:
        if agent.id == agent_id:
            return agent
    return None


def strip_transcripts(task: Task) -> Task:
    copy = task.model_copy(deep=True)
    for agent in copy.agents:
        agent.transcript = None
    return copy


def _title_from_request(request: str) -> str:
    text = " ".join(request.split())
    if not text:
        return "Task"
    if len(text) <= 48:
        return text
    return text[:47].rstrip() + "…"


def _location_from_body(body: CreateTaskBody) -> Location | None:
    if body.lat is None or body.lng is None:
        return None
    label = body.location.strip() if body.location and body.location.strip() else None
    return Location(lat=body.lat, lng=body.lng, label=label)


def _coerce_task_event(value: object) -> TaskEvent | None:
    if isinstance(value, (TaskUpdatedEvent, AgentUpdatedEvent, TaskResultEvent, ErrorEvent)):
        return value
    if not isinstance(value, dict):
        return None
    etype = value.get("type")
    try:
        if etype == "task.updated":
            return TaskUpdatedEvent.model_validate(value)
        if etype == "agent.updated":
            return AgentUpdatedEvent.model_validate(value)
        if etype == "task.result":
            return TaskResultEvent.model_validate(value)
        if etype == "error":
            return ErrorEvent.model_validate(value)
    except Exception:
        return None
    return None


def _event_task_id(event: TaskEvent, fallback: str) -> str:
    if isinstance(event, TaskUpdatedEvent):
        return event.task.id
    if isinstance(event, AgentUpdatedEvent):
        return event.agent.taskId
    if isinstance(event, TaskResultEvent):
        return event.taskId
    return fallback


def _make_events_publisher(task_id: str):
    """Publish orchestrator TaskEvent objects onto the existing WS hub."""
    loop = asyncio.get_running_loop()

    def publish_event(*args: object) -> None:
        raw = args[-1] if args else None
        event = _coerce_task_event(raw)
        if event is None:
            return
        tid = _event_task_id(event, task_id)
        asyncio.run_coroutine_threadsafe(hub.publish(tid, event), loop)

    return publish_event


async def _run_orchestrator(task_id: str) -> None:
    if _on_task_created is None:
        return
    events = _make_events_publisher(task_id)
    try:
        result = await asyncio.to_thread(_on_task_created, task_id, events=events)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        await hub.publish(task_id, ErrorEvent(message=str(exc)))


def _schedule_on_task_created(task_id: str) -> None:
    asyncio.get_running_loop().create_task(_run_orchestrator(task_id))


@router.post("/tasks", response_model=Task)
async def create_task(body: CreateTaskBody) -> Task:
    request = body.request.strip()
    if not request:
        raise HTTPException(status_code=422, detail="request is required")

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    task = Task(
        id=f"task_{uuid4().hex[:12]}",
        title=_title_from_request(request),
        request=request,
        createdAt=now,
        status="planning",
        location=_location_from_body(body),
        agents=[],
    )
    saved = await asyncio.to_thread(save_task, task)
    _schedule_on_task_created(saved.id)
    await hub.publish(saved.id, TaskUpdatedEvent(task=saved))
    return saved


@router.get("/tasks", response_model=list[Task])
async def get_tasks() -> list[Task]:
    tasks = sorted(await asyncio.to_thread(list_tasks), key=lambda t: t.createdAt, reverse=True)
    return [strip_transcripts(task) for task in tasks]


@router.get("/tasks/{task_id}", response_model=Task)
async def get_task(task_id: str) -> Task:
    task = await asyncio.to_thread(load_task, task_id, refresh=True)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.get("/tasks/{task_id}/agents/{agent_id}", response_model=Agent)
async def get_agent(task_id: str, agent_id: str) -> Agent:
    if await asyncio.to_thread(load_task, task_id) is None:
        raise HTTPException(status_code=404, detail="task not found")
    agent = await asyncio.to_thread(load_agent, task_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return agent


@router.post("/tasks/{task_id}/book", response_model=Task)
async def book_task(task_id: str, body: BookBody) -> Task:
    task = load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    _ = body.agentId
    return task


@router.websocket("/tasks/{task_id}/events")
async def task_events(websocket: WebSocket, task_id: str) -> None:
    await websocket.accept()
    task = await asyncio.to_thread(load_task, task_id, refresh=True)
    if task is None:
        await hub.send_error(websocket, "task not found")
        await websocket.close(code=1008)
        return

    await hub.subscribe(task_id, websocket)
    try:
        await hub.send_task_snapshot(websocket, task)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unsubscribe(task_id, websocket)


def _twiml_response(agent_id: str, task_id: str) -> Response:
    if _voice_twiml is not None:
        try:
            xml = _voice_twiml(agent_id, task_id)
        except Exception:
            xml = '<?xml version="1.0" encoding="UTF-8"?>\n<Response></Response>\n'
    else:
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n<Response></Response>\n'
    return Response(content=xml, media_type="application/xml")


@router.post("/twilio/voice")
async def twilio_voice(
    request: Request,
    agent_id: str = Query(default=""),
    task_id: str = Query(default=""),
) -> Response:
    if not agent_id or not task_id:
        try:
            form = await request.form()
        except Exception:
            form = {}
        agent_id = agent_id or str(form.get("agent_id") or "")
        task_id = task_id or str(form.get("task_id") or "")
    return _twiml_response(agent_id, task_id)


@router.post("/twilio/status")
async def twilio_status(
    request: Request,
    agent_id: str = Query(default=""),
    task_id: str = Query(default=""),
) -> dict[str, str]:
    _ = (request, agent_id, task_id)
    return {"ok": "true"}


@router.post("/twilio/recording")
async def twilio_recording(
    request: Request,
    agent_id: str = Query(default=""),
    task_id: str = Query(default=""),
) -> dict[str, str]:
    _ = (request, agent_id, task_id)
    return {"ok": "true"}


@router.websocket("/twilio/media")
async def twilio_media(
    websocket: WebSocket,
    agent_id: str = Query(default=""),
    task_id: str = Query(default=""),
) -> None:
    await websocket.accept()
    try:
        from .call_audio import run_twilio_media

        await run_twilio_media(websocket, agent_id, task_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
