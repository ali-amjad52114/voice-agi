"""Smoke-test the same streaming chat path Pipecat uses for the brain."""

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(override=True)

api_key = os.getenv("GENERAL_COMPUTE_API_KEY") or os.getenv("GENERALCOMPUTE_API_KEY")
base_url = os.getenv("GENERAL_COMPUTE_BASE_URL", "https://api.generalcompute.com/v1")
model = os.getenv("GENERAL_COMPUTE_MODEL", "minimax-m2.7")

if not api_key:
    print("MISSING_KEY")
    sys.exit(1)

client = OpenAI(api_key=api_key, base_url=base_url)
kwargs = {
    "model": model,
    "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
    "stream": True,
    "max_tokens": 16,
}
if "--with-stream-options" in sys.argv:
    kwargs["stream_options"] = {"include_usage": True}

try:
    stream = client.chat.completions.create(**kwargs)
    chunks = 0
    text = []
    event = None
    for event in stream:
        chunks += 1
        delta = event.choices[0].delta.content if event.choices else None
        if delta:
            text.append(delta)
    reply = "".join(text).strip()
    print(f"MODEL={event.model if event else model}")
    print(f"CHUNKS={chunks}")
    print(f"REPLY={reply[:80]}")
    print("STREAM_OK" if chunks and reply else "STREAM_EMPTY")
except Exception as exc:
    msg = str(exc)
    for secret in filter(None, [api_key]):
        msg = msg.replace(secret, "[REDACTED]")
    print(f"STREAM_FAIL={type(exc).__name__}: {msg[:300]}")
    sys.exit(1)
