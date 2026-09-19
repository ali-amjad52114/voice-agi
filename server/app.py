import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

try:
    from dotenv import load_dotenv

    _root = Path(__file__).resolve().parent
    load_dotenv(_root / ".env", override=True)
    load_dotenv(_root.parent / "general-compute-hackathon" / "server" / ".env", override=False)
except ImportError:
    pass

from .api import router

app = FastAPI(title="Voice AGI", version="0.1.0")  # env: supabase secret loaded from server/.env
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.on_event("startup")
async def _loop_watchdog() -> None:
    """Log event-loop stalls > 0.5s to server/loop_watchdog.log (diagnostic)."""
    import asyncio
    import time

    log = Path(__file__).resolve().parent / "loop_watchdog.log"

    async def _tick() -> None:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} watchdog started (pid {os.getpid()})\n")
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(0.25)
            lag = time.monotonic() - t0 - 0.25
            if lag > 0.5:
                with log.open("a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%H:%M:%S')} loop stalled {lag:.2f}s\n")

    asyncio.get_running_loop().create_task(_tick())


@app.on_event("shutdown")
async def _close_event_streams() -> None:
    """Drop event websockets so graceful shutdown completes immediately.

    Confirmed 2026-09-19: with a client websocket open, a restart hung ~45 s
    while still accepting connections, so Twilio's TwiML fetch timed out and
    callers heard "an application error has occurred".
    """
    from .events import hub

    await hub.close_all()


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "name": "Voice AGI API",
        "health": "/health",
        "tasks": "/tasks",
        "docs": "/docs",
        "ui": "http://localhost:5174/",
    }


@app.get("/health")
async def health() -> dict[str, object]:
    import os

    names = (
        "SERPAPI_API_KEY",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM",
        "TWILIO_DEMO_TO",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "GENERAL_COMPUTE_API_KEY",
        "GRADIUM_API_KEY",
        "TWILIO_WEBHOOK_BASE",
        "PUBLIC_BASE_URL",
    )
    present = {name: bool((os.getenv(name) or "").strip()) for name in names}
    return {
        "status": "ok",
        "env": present,
        "twilio_inline_twiml": not (
            present.get("TWILIO_WEBHOOK_BASE") or present.get("PUBLIC_BASE_URL")
        ),
        "supabase_persistence": present.get("SUPABASE_SERVICE_ROLE_KEY", False),
    }
