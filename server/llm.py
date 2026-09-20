"""General Compute chat client (OpenAI-compatible).

GC rejects OpenAI-only extras: ``developer`` roles, ``stream_options``,
and ``service_tier``. Quirks copied from ``GeneralComputeLLMService`` in
``general-compute-hackathon/server/bot.py``. This module is a one-shot
``complete()`` helper — no planner prompts, no Pipecat pipeline.

``complete_with_usage`` is the full version: it returns the text plus a usage
dict (model, tokens, latency, JSON mode), records that dict in ``gc_usage``,
and falls back from ``json_schema`` to ``json_object`` (schema appended to the
system prompt as text) when the endpoint rejects the structured request.

``complete_stream`` is the same call with ``stream=True``: it hands every
content delta to ``on_delta`` as it arrives (the synthesizer uses that to
show the ``why`` sentence while the model is still writing) and returns the
same ``(text, usage)`` pair, plus ``ttfb_s`` for the first token.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[misc, assignment]

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

try:
    from . import gc_usage as _usage
except ImportError:  # pragma: no cover - script / flat import
    try:
        import gc_usage as _usage  # type: ignore[no-redef]
    except ImportError:
        try:
            from server import gc_usage as _usage  # type: ignore[no-redef]
        except ImportError:
            _usage = None  # type: ignore[assignment]

DEFAULT_MODEL = "gemma-4-31B-it"
DEFAULT_BASE_URL = "https://api.generalcompute.com/v1"


def _api_key() -> str:
    key = os.getenv("GENERAL_COMPUTE_API_KEY") or os.getenv("GENERALCOMPUTE_API_KEY")
    if not key:
        raise RuntimeError(
            "Set GENERAL_COMPUTE_API_KEY or GENERALCOMPUTE_API_KEY"
        )
    return key


def _client() -> OpenAI:
    if OpenAI is None:
        raise RuntimeError("openai package missing — pip install openai")
    # One-shot helper calls (planner, extract, decision) normally finish in
    # 2-5 s. The SDK default is a 600 s timeout with 2 retries, which turned a
    # rejected request into a 30-minute hang of the orchestrator thread.
    return OpenAI(
        api_key=_api_key(),
        base_url=os.getenv("GENERAL_COMPUTE_BASE_URL", DEFAULT_BASE_URL),
        timeout=float(os.getenv("GENERAL_COMPUTE_TIMEOUT_S", "45")),
        max_retries=int(os.getenv("GENERAL_COMPUTE_MAX_RETRIES", "1")),
    )


def _sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Drop fields General Compute rejects (same as GeneralComputeLLMService)."""
    params.pop("stream_options", None)
    params.pop("service_tier", None)
    return params


def _is_schema_rejection(exc: BaseException) -> bool:
    """True when the endpoint refused the structured ``json_schema`` request.

    ``openai.BadRequestError`` carries ``status_code == 400``; other clients
    may only say so in the message.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if status == 400:
        return True
    message = str(exc).lower()
    return "response_format" in message or "json_schema" in message


def _schema_as_text(json_schema: dict[str, Any]) -> str:
    return "Return only a JSON object matching this schema: " + json.dumps(json_schema)


def _params(
    model: str,
    system: str,
    user: str,
    json_schema: dict[str, Any] | None,
    json_mode: str,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode == "json_schema" and json_schema is not None:
        params["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "schema": json_schema,
            },
        }
    elif json_mode == "json_object":
        params["response_format"] = {"type": "json_object"}
    return _sanitize_params(params)


def _token_count(usage: Any, *names: str) -> int | None:
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _create_with_fallback(
    client: Any,
    model: str,
    system: str,
    user: str,
    json_schema: dict[str, Any] | None,
    *,
    stream: bool = False,
) -> tuple[Any, str]:
    """Send the request; on a schema rejection retry once as ``json_object``.

    Returns ``(response, json_mode)``. ``response`` is a completion object
    or, with ``stream=True``, the chunk iterator.
    """
    json_mode = "json_schema" if json_schema is not None else "text"
    extra: dict[str, Any] = {"stream": True} if stream else {}
    try:
        response = client.chat.completions.create(
            **_params(model, system, user, json_schema, json_mode), **extra
        )
    except Exception as exc:
        if json_schema is None or not _is_schema_rejection(exc):
            raise
        json_mode = "json_object"
        response = client.chat.completions.create(
            **_params(model, _fallback_system(system, json_schema), user, json_schema, json_mode),
            **extra,
        )
    return response, json_mode


def _fallback_system(system: str, json_schema: dict[str, Any]) -> str:
    return system.rstrip() + "\n\n" + _schema_as_text(json_schema)


def _usage_dict(
    model: str,
    response_model: Any,
    raw_usage: Any,
    latency_s: float,
    json_mode: str,
) -> dict[str, Any]:
    return {
        "model": response_model or model,
        "prompt_tokens": _token_count(raw_usage, "prompt_tokens", "input_tokens"),
        "completion_tokens": _token_count(raw_usage, "completion_tokens", "output_tokens"),
        "latency_s": latency_s,
        "json_mode": json_mode,
    }


def _record(stage: str, usage: dict[str, Any], task_id: str | None) -> None:
    if _usage is None:
        return
    try:
        _usage.record(stage, usage, task_id=task_id)
    except Exception:
        pass


def complete_with_usage(
    system: str,
    user: str,
    json_schema: dict[str, Any] | None = None,
    *,
    stage: str = "",
    model: str | None = None,
    task_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """One-shot completion returning ``(text, usage)``.

    ``usage`` has ``model``, ``prompt_tokens``, ``completion_tokens``,
    ``latency_s`` and ``json_mode`` (``"json_schema"``, ``"json_object"`` or
    ``"text"``). If the ``json_schema`` request is rejected (HTTP 400 or a
    message naming response_format / json_schema), the call is retried once
    as ``json_object`` with the schema appended to the system prompt. Every
    call is recorded in ``gc_usage`` under ``stage``.
    """
    model = model or os.getenv("GENERAL_COMPUTE_MODEL", DEFAULT_MODEL)
    client = _client()

    started = time.monotonic()
    response, json_mode = _create_with_fallback(client, model, system, user, json_schema)
    latency_s = round(time.monotonic() - started, 3)

    usage = _usage_dict(
        model,
        getattr(response, "model", None),
        getattr(response, "usage", None),
        latency_s,
        json_mode,
    )
    _record(stage, usage, task_id)

    choices = getattr(response, "choices", None) or []
    content = choices[0].message.content if choices else None
    return (content or "").strip(), usage


def _chunk_delta(chunk: Any) -> str:
    """Content text of one streaming chunk, or ``""``."""
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    content = getattr(delta, "content", None) if delta is not None else None
    return content if isinstance(content, str) else ""


def complete_stream(
    system: str,
    user: str,
    json_schema: dict[str, Any] | None = None,
    *,
    stage: str = "",
    model: str | None = None,
    task_id: str | None = None,
    on_delta: Callable[[str], Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Streaming completion: ``on_delta(piece)`` per content delta, then ``(text, usage)``.

    Same client, timeout, sanitising and ``json_object`` fallback as
    ``complete_with_usage``. ``usage`` carries the same keys plus ``ttfb_s``
    (seconds to the first non-empty delta, or None). General Compute stream
    chunks may not carry ``usage``; token counts are then None and latency
    is still recorded under ``stage``.
    """
    model = model or os.getenv("GENERAL_COMPUTE_MODEL", DEFAULT_MODEL)
    client = _client()

    started = time.monotonic()
    stream, json_mode = _create_with_fallback(client, model, system, user, json_schema, stream=True)

    pieces: list[str] = []
    ttfb_s: float | None = None
    raw_usage: Any = None
    response_model: Any = None

    def _consume(chunks: Any) -> None:
        nonlocal ttfb_s, raw_usage, response_model
        for chunk in chunks:
            if response_model is None:
                response_model = getattr(chunk, "model", None)
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                raw_usage = chunk_usage
            piece = _chunk_delta(chunk)
            if not piece:
                continue
            if ttfb_s is None:
                ttfb_s = round(time.monotonic() - started, 3)
            pieces.append(piece)
            if on_delta is not None:
                on_delta(piece)

    try:
        _consume(stream)
    except Exception as exc:
        # Some servers only refuse the schema once the body is read; if
        # nothing has arrived yet, retry the same way as the non-stream path.
        if pieces or json_schema is None or json_mode != "json_schema" or not _is_schema_rejection(exc):
            raise
        json_mode = "json_object"
        stream = client.chat.completions.create(
            **_params(model, _fallback_system(system, json_schema), user, json_schema, json_mode),
            stream=True,
        )
        _consume(stream)
    latency_s = round(time.monotonic() - started, 3)

    usage = _usage_dict(model, response_model, raw_usage, latency_s, json_mode)
    usage["ttfb_s"] = ttfb_s
    _record(stage, usage, task_id)
    return "".join(pieces).strip(), usage


def complete(
    system: str,
    user: str,
    json_schema: dict[str, Any] | None = None,
    *,
    stage: str = "",
) -> str:
    """One-shot completion using system + user only (never ``developer``).

    When ``json_schema`` is set, asks for a JSON object matching that schema.
    Returns assistant text (JSON string if a schema was requested). Thin
    wrapper over ``complete_with_usage`` for the planner / extract callers.
    """
    text, _usage_row = complete_with_usage(system, user, json_schema, stage=stage)
    return text
