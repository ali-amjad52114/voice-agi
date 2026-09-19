"""Supabase persistence for Task + agents + transcript lines.

Env (never hardcode)::

    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY

# server/db_requirements (do not rewrite pyproject):
#   supabase>=2.0.0
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

try:
    from .models import (
        Agent,
        Facts,
        Location,
        Result,
        Task,
        TaskStatus,
        TranscriptLine,
    )
except ImportError:  # pragma: no cover
    from models import (
        Agent,
        Facts,
        Location,
        Result,
        Task,
        TaskStatus,
        TranscriptLine,
    )

# Tables (public): tasks, agents, transcript_lines — see Session 1 Agent 2 SQL.

_client: Any = None


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def is_configured() -> bool:
    return bool(
        (os.environ.get("SUPABASE_URL") or "").strip()
        and (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    )


def get_client() -> Any:
    """Lazy Supabase client using the service role (server-side only)."""
    global _client
    if _client is not None:
        return _client
    try:
        from supabase import create_client
    except ImportError as exc:
        raise RuntimeError(
            "supabase package missing — install supabase>=2.0.0 "
            "(see server/db_requirements comment at top of db.py)"
        ) from exc
    url = _require_env("SUPABASE_URL")
    key = _require_env("SUPABASE_SERVICE_ROLE_KEY")
    _client = create_client(url, key)
    return _client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _as_iso(value: Any) -> str:
    if value is None:
        return _now_iso()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    text = str(value)
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def _dump_json(model: Any) -> Any:
    if model is None:
        return None
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json", exclude_none=True)
    return model


def _location_from_row(raw: Any) -> Location | None:
    if not raw:
        return None
    if isinstance(raw, Location):
        return raw
    if isinstance(raw, dict) and "lat" in raw and "lng" in raw:
        return Location.model_validate(raw)
    return None


def _location_payload(location: Location | dict | None) -> dict[str, Any] | None:
    parsed = _location_from_row(location)
    return _dump_json(parsed)


def _task_row(task_id: str, request: str, title: str, status: str, user_quote: float | None, location: Location | dict | None, created_at: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": title,
        "request": request,
        "created_at": created_at,
        "status": status,
        "user_quote": user_quote,
        "location": _location_payload(location),
    }


def _agent_row(agent: Agent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "task_id": agent.taskId,
        "kind": agent.kind,
        "status": agent.status,
        "business": _dump_json(agent.business) or {},
        "summary": agent.summary,
        "facts": _dump_json(agent.facts),
        "call": _dump_json(agent.call),
    }


def _task_from_row(row: dict[str, Any], agents: list[Agent] | None = None) -> Task:
    result_raw = row.get("result")
    return Task(
        id=str(row["id"]),
        title=row.get("title") or "Task",
        request=row.get("request") or "",
        createdAt=_as_iso(row.get("created_at") or row.get("createdAt")),
        status=row.get("status") or "planning",
        userQuote=_as_float(row.get("user_quote") if "user_quote" in row else row.get("userQuote")),
        location=_location_from_row(row.get("location")),
        agents=agents or [],
        result=Result.model_validate(result_raw) if result_raw else None,
    )


def _agent_from_row(row: dict[str, Any], transcript: list[TranscriptLine] | None = None) -> Agent:
    facts_raw = row.get("facts")
    call_raw = row.get("call")
    return Agent(
        id=str(row["id"]),
        taskId=str(row.get("task_id") or row.get("taskId")),
        kind=row.get("kind") or "call",
        status=row.get("status") or "queued",
        business=row.get("business") or {"name": "", "type": "mechanic"},
        summary=row.get("summary"),
        facts=Facts.model_validate(facts_raw) if facts_raw else None,
        call=call_raw,
        transcript=transcript,
    )


def _line_from_row(row: dict[str, Any]) -> TranscriptLine:
    t = row.get("t")
    if t is None:
        t = row.get("at_s") or 0
    return TranscriptLine(
        role=row.get("role") or "agent",
        text=row.get("text") or "",
        t=_as_float(t) or 0.0,
    )


def _fetch_agents(task_ids: list[str], *, include_transcripts: bool) -> dict[str, list[Agent]]:
    if not task_ids:
        return {}
    client = get_client()
    agent_rows = (
        client.table("agents")
        .select("*")
        .in_("task_id", task_ids)
        .execute()
        .data
        or []
    )
    lines_by_agent: dict[str, list[TranscriptLine]] = {}
    if include_transcripts and agent_rows:
        agent_ids = [str(row["id"]) for row in agent_rows]
        line_rows = (
            client.table("transcript_lines")
            .select("*")
            .in_("agent_id", agent_ids)
            .order("t")
            .execute()
            .data
            or []
        )
        for line in line_rows:
            aid = str(line.get("agent_id"))
            lines_by_agent.setdefault(aid, []).append(_line_from_row(line))

    grouped: dict[str, list[Agent]] = {tid: [] for tid in task_ids}
    for row in agent_rows:
        tid = str(row.get("task_id"))
        aid = str(row["id"])
        transcript = lines_by_agent.get(aid) if include_transcripts else None
        grouped.setdefault(tid, []).append(_agent_from_row(row, transcript))
    return grouped


def create_task(
    request: str,
    *,
    title: str = "Task",
    user_quote: float | None = None,
    location: Location | dict | None = None,
    status: TaskStatus = "planning",
    task_id: str | None = None,
) -> Task:
    """Insert a task row and return it (empty agents). Restart-safe."""
    tid = task_id or str(uuid.uuid4())
    created = _now_iso()
    row = _task_row(tid, request, title, status, user_quote, location, created)
    client = get_client()
    inserted = client.table("tasks").insert(row).execute()
    data = (inserted.data or [row])[0]
    return _task_from_row(data, [])


def get_task(task_id: str) -> Task | None:
    """Load one task with agents and transcript lines."""
    client = get_client()
    result = client.table("tasks").select("*").eq("id", task_id).limit(1).execute()
    rows = result.data or []
    if not rows:
        return None
    agents = _fetch_agents([task_id], include_transcripts=True).get(task_id, [])
    return _task_from_row(rows[0], agents)


def list_tasks() -> list[Task]:
    """All tasks with agents; transcripts omitted (GET /tasks)."""
    client = get_client()
    result = client.table("tasks").select("*").order("created_at", desc=True).execute()
    rows = result.data or []
    ids = [str(row["id"]) for row in rows]
    by_task = _fetch_agents(ids, include_transcripts=False)
    return [_task_from_row(row, by_task.get(str(row["id"]), [])) for row in rows]


def upsert_agent(agent: Agent) -> Agent:
    """Insert or update an agent row (transcripts stay on transcript_lines)."""
    client = get_client()
    client.table("agents").upsert(_agent_row(agent)).execute()
    if agent.transcript:
        existing = (
            client.table("transcript_lines")
            .select("id")
            .eq("agent_id", agent.id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not existing:
            for line in agent.transcript:
                append_transcript(agent.id, line, task_id=agent.taskId)
    loaded = (
        client.table("agents")
        .select("*")
        .eq("id", agent.id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not loaded:
        return agent
    lines = None
    if agent.transcript is not None:
        line_rows = (
            client.table("transcript_lines")
            .select("*")
            .eq("agent_id", agent.id)
            .order("t")
            .execute()
            .data
            or []
        )
        lines = [_line_from_row(r) for r in line_rows]
    return _agent_from_row(loaded[0], lines)


def append_transcript(
    agent_id: str,
    line: TranscriptLine | dict[str, Any],
    *,
    task_id: str | None = None,
) -> TranscriptLine:
    """Append one transcript line for an agent."""
    parsed = line if isinstance(line, TranscriptLine) else TranscriptLine.model_validate(line)
    client = get_client()
    tid = task_id
    if tid is None:
        found = (
            client.table("agents")
            .select("task_id")
            .eq("id", agent_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if found:
            tid = str(found[0]["task_id"])
    if tid is None:
        raise RuntimeError(f"append_transcript: no task_id for agent {agent_id}")
    payload: dict[str, Any] = {
        "agent_id": agent_id,
        "role": parsed.role,
        "text": parsed.text,
        "t": parsed.t,
    }
    if tid is not None:
        payload["task_id"] = tid
    client.table("transcript_lines").insert(payload).execute()
    return parsed


def set_result(task_id: str, result: Result | dict[str, Any]) -> Task | None:
    """Persist Result JSON on the task (does not change status)."""
    parsed = result if isinstance(result, Result) else Result.model_validate(result)
    client = get_client()
    client.table("tasks").update({"result": _dump_json(parsed)}).eq("id", task_id).execute()
    return get_task(task_id)


def update_task_status(task_id: str, status: TaskStatus) -> Task | None:
    """Update task.status only."""
    client = get_client()
    client.table("tasks").update({"status": status}).eq("id", task_id).execute()
    return get_task(task_id)


__all__ = [
    "get_client",
    "create_task",
    "get_task",
    "list_tasks",
    "upsert_agent",
    "append_transcript",
    "set_result",
    "update_task_status",
]
