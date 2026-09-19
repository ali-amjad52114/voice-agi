"""Twilio outbound dialer for Voice AGI call agents.

Places one PSTN call from ``TWILIO_FROM`` using the REST API. Gradium/Pipecat
audio is not started here: Twilio answers by fetching TwiML from the FastAPI
app, then (once those routes exist) ``<Connect><Stream>`` into the same
websocket transport shape ``bot.py`` already uses.

Required env (local ``.env``, never hardcode, never commit)::

    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_FROM
    TWILIO_WEBHOOK_BASE   # or PUBLIC_BASE_URL — https origin Twilio can reach
                          # (ngrok/cloudflare tunnel to FastAPI :7860)

Optional env (name only — this module does not read it; ``one_call`` may)::

    TWILIO_DEMO_TO

How to place a call
-------------------
1. Put the required vars in ``server/.env``.
2. Expose ``:7860`` over HTTPS and set ``TWILIO_WEBHOOK_BASE`` to that origin
   (no trailing path). Trial accounts can only ring verified numbers.
3. After FastAPI implements the TODO routes below, from any server code::

       from twilio_call import start_call
       info = start_call(to_number, agent_id, task_id)

   ``to_number`` is E.164 (``+1…``). Pass a Place ``business.phone`` or, for
   the smoke ring, the value of env ``TWILIO_DEMO_TO`` — do not paste a
   personal number into source.

TODO — routes to add on the FastAPI app (``server/api.py``; do not add here)
---------------------------------------------------------------------------
Twilio ``url`` / callbacks point at these paths on ``TWILIO_WEBHOOK_BASE``:

* ``POST /twilio/voice`` — return ``voice_twiml(agent_id, task_id)`` as
  ``application/xml``. Query: ``agent_id``, ``task_id``.
* ``POST /twilio/status`` — call-progress webhook (initiated/ringing/answered/
  completed). Persist duration / ``AnsweredBy`` later.
* ``POST /twilio/recording`` — recording-ready callback (Twilio records the
  leg so we have audio if live STT drops).
* ``WS  /twilio/media`` — Twilio Media Stream. Hand the socket to Pipecat
  (``create_transport`` / ``FastAPIWebsocketParams`` + Twilio serializer),
  then Gradium STT/TTS like ``bot.py``.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlencode, urljoin
from xml.sax.saxutils import escape as xml_escape

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:
    _here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_here, ".env"), override=False)
    load_dotenv(os.path.join(os.path.dirname(_here), ".env"), override=False)

# Paths FastAPI must expose. start_call() builds absolute URLs from these.
VOICE_PATH = "/twilio/voice"
STATUS_PATH = "/twilio/status"
RECORDING_PATH = "/twilio/recording"
MEDIA_WS_PATH = "/twilio/media"

_REQUIRED_CREDS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM")
_DISCLOSURE = "I'm an assistant calling for a customer about a brake quote."
_NON_DIGIT = re.compile(r"\D+")


def to_e164(raw: str, default_cc: str = "1") -> str:
    """Normalize a shop or demo number to E.164. US/CA default country code 1."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("to_number is required (E.164).")
    digits = _NON_DIGIT.sub("", text)
    if not digits:
        raise ValueError("to_number must contain digits.")
    if text.startswith("+"):
        dest = "+" + digits
    elif len(digits) == 10:
        dest = f"+{default_cc}{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        dest = "+" + digits
    else:
        dest = "+" + digits
    if not dest.startswith("+") or not dest[1:].isdigit():
        raise ValueError("to_number must be E.164 (leading + and digits only).")
    return dest


def disclosure_twiml() -> str:
    """Spoken disclosure + hang up. Used when no public media stream is available."""
    say = xml_escape(_DISCLOSURE)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"  <Say>{say}</Say>\n"
        "  <Hangup/>\n"
        "</Response>\n"
    )


def _has_webhook() -> bool:
    raw = (os.getenv("TWILIO_WEBHOOK_BASE") or os.getenv("PUBLIC_BASE_URL") or "").strip()
    return bool(raw)


def _env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env {name}")
    return value


def webhook_base() -> str:
    """Public HTTPS origin for TwiML/status URLs (no trailing slash)."""
    raw = (os.getenv("TWILIO_WEBHOOK_BASE") or os.getenv("PUBLIC_BASE_URL") or "").strip()
    if not raw:
        raise RuntimeError(
            "Missing TWILIO_WEBHOOK_BASE (or PUBLIC_BASE_URL). "
            "Set it to the public https origin of FastAPI :7860."
        )
    return raw.rstrip("/")


def _absolute(path: str, query: dict[str, str] | None = None) -> str:
    url = urljoin(webhook_base() + "/", path.lstrip("/"))
    if query:
        url = f"{url}?{urlencode(query)}"
    return url


def media_stream_url(agent_id: str = "", task_id: str = "") -> str:
    """wss URL for Twilio Media Streams. IDs go in TwiML Parameters, not the query."""
    https = webhook_base()
    if https.startswith("https://"):
        origin = "wss://" + https[len("https://") :]
    elif https.startswith("http://"):
        origin = "ws://" + https[len("http://") :]
    else:
        origin = "wss://" + https
    return f"{origin}{MEDIA_WS_PATH}"


def voice_twiml(agent_id: str, task_id: str) -> str:
    """TwiML for ``POST /twilio/voice``.

    With a public webhook: Connect a media stream immediately (Gradium speaks
    the disclosure). Query-string ``&`` is invalid in XML and makes Twilio
    say \"An application error has occurred\". IDs are Stream parameters.
    Without a webhook: disclosure + hang up.
    """
    if not _has_webhook():
        return disclosure_twiml()
    stream = xml_escape(media_stream_url())
    aid = xml_escape(agent_id or "")
    tid = xml_escape(task_id or "")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f'    <Stream url="{stream}">\n'
        f'      <Parameter name="agent_id" value="{aid}" />\n'
        f'      <Parameter name="task_id" value="{tid}" />\n'
        "    </Stream>\n"
        "  </Connect>\n"
        "</Response>\n"
    )


def _client():
    try:
        from twilio.rest import Client
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Install the twilio package to place calls.") from exc
    return Client(_env("TWILIO_ACCOUNT_SID"), _env("TWILIO_AUTH_TOKEN"))


def start_call(to_number: str, agent_id: str, task_id: str) -> dict[str, Any]:
    """Dial ``to_number`` from ``TWILIO_FROM`` and attach it to this agent/task.

    Twilio immediately POSTs ``VOICE_PATH`` for TwiML. That handler is a
    FastAPI TODO; until it exists the callee hears silence / Twilio's error.

    Args:
        to_number: E.164 destination. Caller supplies it (Place phone or the
            value of env TWILIO_DEMO_TO). Never hardcode a number here.
        agent_id: Voice AGI agent id (query on every webhook).
        task_id: Voice AGI task id (query on every webhook).

    Returns:
        Twilio call metadata: ``sid``, ``status``, ``to``, ``from``,
        ``agent_id``, ``task_id``, plus the webhook URLs used.
    """
    dest = to_e164(to_number)
    from_number = _env("TWILIO_FROM")
    ids = {"agent_id": agent_id, "task_id": task_id}
    voice_url = None
    status_url = None
    recording_url = None
    media_ws = None
    create_kwargs: dict[str, Any] = {
        "to": dest,
        "from_": from_number,
    }
    if _has_webhook():
        voice_url = _absolute(VOICE_PATH, ids)
        status_url = _absolute(STATUS_PATH, ids)
        recording_url = _absolute(RECORDING_PATH, ids)
        media_ws = media_stream_url(agent_id, task_id)
        create_kwargs.update(
            url=voice_url,
            method="POST",
            status_callback=status_url,
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            record=True,
            recording_status_callback=recording_url,
            recording_status_callback_method="POST",
        )
    else:
        # Inline TwiML: Twilio does not need to fetch our localhost.
        create_kwargs["twiml"] = disclosure_twiml()

    call = _client().calls.create(**create_kwargs)
    return {
        "sid": call.sid,
        "status": call.status,
        "to": dest,
        "from": from_number,
        "agent_id": agent_id,
        "task_id": task_id,
        "voice_url": voice_url,
        "status_url": status_url,
        "recording_url": recording_url,
        "media_ws_url": media_ws,
    }
