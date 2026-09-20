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

DEFAULT_MODEL = "gpt-oss-120b"
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
        timeout=float(os.getenv("GENERAL_COMPUTE_TIMEOUT_S", "90")),
        max_retries=int(os.getenv("GENERAL_COMPUTE_MAX_RETRIES", "1")),
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
    *,
    model: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """One-shot completion using system + user only (never ``developer``).

    When ``json_schema`` is set, asks for a JSON object matching that schema.
    Returns assistant text (JSON string if a schema was requested).
    """
    # Measured 2026-09-19 on the real extraction prompt with a JSON schema:
    # gpt-oss-120b 1.8 s, minimax-m2.7 2.6 s, gemma-4-31B-it hung 120 s and
    # returned nothing. Callers pass a per-stage model; the default is the
    # fastest correct one. Every call is capped so a model cannot run away.
    model = model or os.getenv("GENERAL_COMPUTE_MODEL", DEFAULT_MODEL)
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens or int(os.getenv("GENERAL_COMPUTE_MAX_TOKENS", "1200")),
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
