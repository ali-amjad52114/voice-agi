from __future__ import annotations

import asyncio
from collections import defaultdict

from fastapi import WebSocket
from pydantic import BaseModel

from .models import ErrorEvent, Task, TaskEvent, TaskUpdatedEvent


class EventHub:
    """In-process fan-out for WS /tasks/{id}/events."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subs: dict[str, set[WebSocket]] = defaultdict(set)

    async def subscribe(self, task_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._subs[task_id].add(ws)

    async def unsubscribe(self, task_id: str, ws: WebSocket) -> None:
        async with self._lock:
            sockets = self._subs.get(task_id)
            if not sockets:
                return
            sockets.discard(ws)
            if not sockets:
                self._subs.pop(task_id, None)

    async def publish(self, task_id: str, event: TaskEvent | dict | BaseModel) -> None:
        payload = _event_payload(event)
        async with self._lock:
            sockets = list(self._subs.get(task_id, ()))
        dead: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                live = self._subs.get(task_id)
                if live:
                    for ws in dead:
                        live.discard(ws)
                    if not live:
                        self._subs.pop(task_id, None)

    async def close_all(self) -> None:
        """Close every subscriber socket. Called at app shutdown so a restart
        never waits on long-lived event streams."""
        async with self._lock:
            sockets = [ws for subs in self._subs.values() for ws in subs]
            self._subs.clear()
        for ws in sockets:
            try:
                await ws.close(code=1001)
            except Exception:
                pass

    async def send_task_snapshot(self, ws: WebSocket, task: Task) -> None:
        await ws.send_json(TaskUpdatedEvent(task=task).model_dump(mode="json"))

    async def send_error(self, ws: WebSocket, message: str) -> None:
        await ws.send_json(ErrorEvent(message=message).model_dump(mode="json"))


def _event_payload(event: TaskEvent | dict | BaseModel) -> dict:
    if isinstance(event, BaseModel):
        return event.model_dump(mode="json")
    return event


hub = EventHub()
