"""Parallel Twilio dialer: cap 3 in-flight, queue the rest, honor a deadline.

Does not synthesize. Does not invent quotes. Voicemail / no-answer agents stay
on the returned list (visible "no quote"), never dropped.
"""

from __future__ import annotations

import os
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
    from .one_call import run_one_call
except ImportError:  # pragma: no cover
    try:
        from one_call import run_one_call
    except ImportError:
        try:
            from server.one_call import run_one_call
        except ImportError:
            run_one_call = None  # type: ignore[assignment]

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

MAX_CONCURRENT = 3
DEFAULT_DEADLINE_S = 480.0
_POLL_S = 2.0
_TERMINAL = frozenset({"completed", "busy", "failed", "no-answer", "canceled"})
_MACHINE = frozenset(
    {
        "machine",
        "machine_start",
        "machine_end_beep",
        "machine_end_silence",
        "machine_end_other",
        "fax",
        "fax_detected",
    }
)


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
        "kind": getattr(obj, "kind", None),
        "business": getattr(obj, "business", None),
        "call": getattr(obj, "call", None),
    }


def _agent_id(agent: Any) -> str | None:
    value = _as_mapping(agent).get("id")
    if value is None:
        value = getattr(agent, "id", None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _agent_kind(agent: Any) -> str:
    value = _as_mapping(agent).get("kind")
    if value is None:
        value = getattr(agent, "kind", None)
    return str(value or "call").strip().lower() or "call"


def _agent_phone(agent: Any) -> str | None:
    raw = _as_mapping(agent).get("business")
    if raw is None:
        raw = getattr(agent, "business", None)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raw = {"phone": getattr(raw, "phone", None)}
    phone = raw.get("phone")
    if phone is None:
        return None
    text = str(phone).strip()
    if not text:
        return None
    try:
        from .twilio_call import to_e164
    except ImportError:
        try:
            from twilio_call import to_e164
        except ImportError:
            return text
    try:
        return to_e164(text)
    except ValueError:
        return None


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


def _call_agents(agents: Sequence[Any]) -> list[Any]:
    return [a for a in agents if _agent_kind(a) != "web"]


def _record(
    agent: Any,
    *,
    started: bool,
    skipped_reason: str | None = None,
    sid: str | None = None,
    status: str | None = None,
    outcome: str | None = None,
    answered_by: str | None = None,
    duration_s: float = 0.0,
    error: str | None = None,
    to: str | None = None,
) -> dict[str, Any]:
    return {
        "agent_id": _agent_id(agent),
        "to": to or _agent_phone(agent),
        "started": started,
        "skipped_reason": skipped_reason,
        "sid": sid,
        "status": status,
        "outcome": outcome,
        "answered_by": answered_by,
        "duration_s": duration_s,
        "error": error,
    }


def _outcome(status: str | None, answered_by: str | None) -> str | None:
    ab = (answered_by or "").strip().lower()
    if ab in _MACHINE or "machine" in ab:
        return "voicemail"
    if status in {"no-answer", "busy"}:
        return "voicemail"
    if status in {"failed", "canceled"}:
        return "error"
    return None


def _place_call(to_number: str, agent_id: str, task_id: str) -> dict[str, Any]:
    if start_call is not None:
        return start_call(to_number, agent_id, task_id)
    if run_one_call is not None:
        return run_one_call(task_id, agent_id=agent_id)
    raise RuntimeError("neither twilio_call.start_call nor one_call.run_one_call is available")


def _twilio_client():
    try:
        from twilio.rest import Client
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Install the twilio package to poll calls.") from exc
    sid = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    token = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    if not sid or not token:
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN")
    return Client(sid, token)


def _poll_until_done(sid: str, stop_at: float) -> dict[str, Any]:
    client = _twilio_client()
    last: Any = None
    while True:
        last = client.calls(sid).fetch()
        if last.status in _TERMINAL:
            break
        if time.monotonic() >= stop_at:
            break
        time.sleep(_POLL_S)
    status = getattr(last, "status", None) if last is not None else None
    answered_by = getattr(last, "answered_by", None) if last is not None else None
    raw_dur = getattr(last, "duration", None) if last is not None else None
    try:
        duration_s = float(raw_dur) if raw_dur is not None else 0.0
    except (TypeError, ValueError):
        duration_s = 0.0
    return {
        "status": status,
        "answered_by": answered_by,
        "duration_s": duration_s,
        "outcome": _outcome(status, answered_by),
    }


def _demo_destination() -> str:
    """Owner cell only. Do not dial Place phones on the trial account."""
    raw = (os.getenv("TWILIO_DEMO_TO") or "").strip()
    if not raw:
        raise RuntimeError("TWILIO_DEMO_TO is not set (trial accounts can only ring verified numbers)")
    try:
        from .twilio_call import to_e164

        return to_e164(raw)
    except Exception:
        return raw


def _dial_one(
    agent: Any,
    task_id: str,
    wait_until: float,
    *,
    to_number: str | None = None,
) -> dict[str, Any]:
    phone = to_number or _agent_phone(agent)
    aid = _agent_id(agent)
    if not phone or not aid:
        return _record(agent, started=False, skipped_reason="no_phone")
    try:
        info = _place_call(phone, aid, task_id)
    except Exception as exc:
        return _record(agent, started=False, to=phone, outcome="error", error=str(exc))
    sid = str(info.get("sid") or "") or None
    if not sid:
        return _record(
            agent,
            started=True,
            to=info.get("to") or phone,
            status=str(info.get("status") or "") or None,
        )
    try:
        done = _poll_until_done(sid, wait_until)
    except Exception as exc:
        return _record(
            agent,
            started=True,
            to=info.get("to") or phone,
            sid=sid,
            status=str(info.get("status") or "") or None,
            error=str(exc),
        )
    return _record(
        agent,
        started=True,
        to=info.get("to") or phone,
        sid=sid,
        status=done["status"],
        outcome=done["outcome"],
        answered_by=done["answered_by"],
        duration_s=done["duration_s"],
    )


def run_dialer(
    task_id: str,
    agents: Sequence[Any] | None = None,
    *,
    deadline_s: float | None = None,
    max_concurrent: int = MAX_CONCURRENT,
) -> list[dict[str, Any]]:
    """Dial call agents with at most 3 Twilio legs at once.

    Args:
        task_id: Voice AGI task id forwarded to ``start_call``.
        agents: Task agents. If omitted and ``db`` is importable, load them.
        deadline_s: Seconds from now after which no *new* call is started.
            In-flight calls are allowed to finish. Default ``DEFAULT_DEADLINE_S``
            or env ``DIALER_DEADLINE_S``.
        max_concurrent: In-flight cap (default 3). Extra agents wait in queue.

    Returns:
        One record per call agent, including voicemail and deadline-skipped
        rows. Never synthesizes a result or writes prices.
    """
    tid = (task_id or "").strip()
    if not tid:
        raise ValueError("task_id is required")
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be >= 1")

    loaded = list(agents) if agents is not None else _load_agents(tid)
    calls = _call_agents(loaded)
    if not calls:
        return []
    dest = _demo_destination()
    first, rest = calls[0], calls[1:]
    results: list[dict[str, Any]] = []
    for skipped in rest:
        results.append(
            _record(skipped, started=False, skipped_reason="single_demo_number")
        )
    pending = deque([first])

    if deadline_s is None:
        raw = (os.getenv("DIALER_DEADLINE_S") or "").strip()
        deadline_s = float(raw) if raw else DEFAULT_DEADLINE_S
    start = time.monotonic()
    cutoff = start + float(deadline_s)
    # In-flight legs may outlive the start cutoff by one typical call.
    wait_until = cutoff + float(deadline_s)

    in_flight: dict[Any, Any] = {}

    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        while pending or in_flight:
            while pending and len(in_flight) < max_concurrent:
                if time.monotonic() >= cutoff:
                    break
                agent = pending.popleft()
                if not _agent_id(agent):
                    results.append(_record(agent, started=False, skipped_reason="no_phone"))
                    continue
                fut = pool.submit(_dial_one, agent, tid, wait_until, to_number=dest)
                in_flight[fut] = agent

            if time.monotonic() >= cutoff:
                while pending:
                    results.append(
                        _record(pending.popleft(), started=False, skipped_reason="deadline")
                    )

            if not in_flight:
                break

            done, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED, timeout=_POLL_S)
            for fut in done:
                try:
                    results.append(fut.result())
                except Exception as exc:
                    agent = in_flight[fut]
                    results.append(
                        _record(agent, started=True, outcome="error", error=str(exc))
                    )
                del in_flight[fut]

    return results
