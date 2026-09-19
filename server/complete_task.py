"""Finish a task: persist synthesized Result and emit ``task.result``.

Called after the dialer and per-agent extract. Does not start Twilio.
Does not invent shop prices — synthesis must use real facts only.
"""

from __future__ import annotations

import inspect
from typing import Any

from .events import hub
from .models import Result, Task, TaskResultEvent, TaskUpdatedEvent

try:
    from . import db as _db
except Exception:
    _db = None

try:
    from . import synthesize as _synthesize_mod
except Exception:
    _synthesize_mod = None


def _fn(mod: Any, *names: str):
    if mod is None:
        return None
    for name in names:
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    return None


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    if fn is None:
        return None
    out = fn(*args, **kwargs)
    if inspect.isawaitable(out):
        return await out
    return out


def _as_task(value: Any) -> Task | None:
    if value is None:
        return None
    if isinstance(value, Task):
        return value
    return Task.model_validate(value)


def _as_result(value: Any) -> Result:
    if isinstance(value, Result):
        return value
    if isinstance(value, dict) and "result" in value and "options" not in value:
        value = value["result"]
    return Result.model_validate(value)


async def _load_task(task_id: str) -> Task | None:
    fn = _fn(_db, "get_task", "load_task")
    return _as_task(await _call(fn, task_id)) if fn else None


async def _persist_result(task_id: str, result: Result) -> None:
    payload = result.model_dump(mode="json")
    set_result = _fn(_db, "set_result")
    if set_result is not None:
        await _call(set_result, task_id, payload)
    set_status = _fn(_db, "update_task_status", "set_task_status")
    if set_status is not None:
        await _call(set_status, task_id, "complete")


async def _run_synthesize(task: Task) -> Result:
    fn = _fn(_synthesize_mod, "synthesize", "synthesize_result", "build_result")
    if fn is None:
        if task.result is not None:
            return task.result
        raise RuntimeError(
            "synthesize.py is missing and the task has no result; "
            "cannot invent prices"
        )
    return _as_result(await _call(fn, task))


async def complete_task(task_id: str, result: Result | dict | None = None) -> dict:
    """Write Result to the db and emit ``{type, taskId, result}``.

    Loads the task (if ``db`` is present), synthesizes when ``result`` is
    omitted, persists via ``db.set_result`` / ``update_task_status``, then
    publishes ``task.result`` (and ``task.updated`` when a snapshot exists).
    """
    task = await _load_task(task_id)
    if result is not None:
        synthesized = _as_result(result)
    elif task is not None:
        synthesized = await _run_synthesize(task)
    else:
        raise RuntimeError(f"cannot complete {task_id}: no db task and no result")

    await _persist_result(task_id, synthesized)

    event = TaskResultEvent(type="task.result", taskId=task_id, result=synthesized)
    payload = event.model_dump(mode="json")
    await hub.publish(task_id, payload)

    snapshot = await _load_task(task_id)
    if snapshot is not None:
        if snapshot.result is None:
            snapshot = snapshot.model_copy(update={"result": synthesized, "status": "complete"})
        elif snapshot.status != "complete":
            snapshot = snapshot.model_copy(update={"status": "complete"})
        await hub.publish(task_id, TaskUpdatedEvent(task=snapshot))

    return payload
