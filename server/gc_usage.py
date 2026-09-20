"""Usage ledger for one-shot General Compute calls.

``llm.complete_with_usage`` calls ``record`` once per request with the stage
name ("planner", "extract", "synthesize"), the model, token counts, latency
and which JSON mode was used. Each entry is appended as one JSON line to
``server/gc_usage.log`` (gitignored via ``*.log``) and kept in a bounded
in-memory list so the UI can show the recent calls for a task.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Mapping

_LOG_PATH = Path(os.getenv("GC_USAGE_LOG") or Path(__file__).resolve().parent / "gc_usage.log")
_RECENT_CAP = 200

_recent: Deque[dict[str, Any]] = deque(maxlen=_RECENT_CAP)
_lock = threading.Lock()


def record(stage: str, usage: Mapping[str, Any], task_id: str | None = None) -> dict[str, Any]:
    """Append one call to the ledger; returns the stored entry.

    Never raises: a failed file write is swallowed so an LLM call cannot fail
    because the disk did.
    """
    entry: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "stage": stage or "",
        "taskId": task_id,
    }
    entry.update(dict(usage))
    with _lock:
        _recent.append(entry)
        try:
            with _LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass
    return entry


def recent(task_id: str | None = None) -> list[dict[str, Any]]:
    """Recent entries (oldest first), optionally only those for ``task_id``."""
    with _lock:
        entries = list(_recent)
    if task_id is None:
        return entries
    return [e for e in entries if e.get("taskId") == task_id]


def clear() -> None:
    """Forget the in-memory entries (tests)."""
    with _lock:
        _recent.clear()
