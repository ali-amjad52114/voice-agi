"""General Compute chat client (OpenAI-compatible).

GC rejects OpenAI-only extras: ``developer`` roles, ``stream_options``,
and ``service_tier``. Quirks copied from ``GeneralComputeLLMService`` in
``general-compute-hackathon/server/bot.py``. This module is a one-shot
``complete()`` helper — no planner prompts, no Pipecat pipeline.
"""

from __future__ import annotations

import os
from typing import Any

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[misc, assignment]

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

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
    return OpenAI(
        api_key=_api_key(),
        base_url=os.getenv("GENERAL_COMPUTE_BASE_URL", DEFAULT_BASE_URL),
    )


def _sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Drop fields General Compute rejects (same as GeneralComputeLLMService)."""
    params.pop("stream_options", None)
    params.pop("service_tier", None)
    return params


def complete(
    system: str,
    user: str,
    json_schema: dict[str, Any] | None = None,
) -> str:
    """One-shot completion using system + user only (never ``developer``).

    When ``json_schema`` is set, asks for a JSON object matching that schema.
    Returns assistant text (JSON string if a schema was requested).
    """
    model = os.getenv("GENERAL_COMPUTE_MODEL", DEFAULT_MODEL)
    params: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_schema is not None:
        params["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "schema": json_schema,
            },
        }

    params = _sanitize_params(params)
    response = _client().chat.completions.create(**params)
    content = response.choices[0].message.content if response.choices else None
    return (content or "").strip()
