"""Twilio Media Stream → Gradium STT/TTS shop caller.

Uses Pipecat when installed (hackathon venv). Does not invent shop prices.
Transcript lines are appended onto the in-memory task agent for extract.
"""

from __future__ import annotations

import asyncio
import functools
import os
import time
from pathlib import Path
from typing import Any

from fastapi import WebSocket

_PROMPT = Path(__file__).resolve().parent / "prompts" / "caller.md"


def _system_instruction() -> str:
    text = _PROMPT.read_text(encoding="utf-8") if _PROMPT.is_file() else (
        "I'm an assistant calling for a customer about a brake quote."
    )
    return text.strip() + (
        "\nSpeak only. No markdown. Never invent a dollar amount the shop did not say."
    )


def _vehicle_and_job(task_id: str) -> tuple[str, str]:
    """Vehicle and job wording for the opening line, from the task's request.

    Uses the planner's offline parser (no LLM call) so the caller names the
    car the user actually said. Falls back to generic wording.
    """
    vehicle, job = "the customer's vehicle", "brake service"
    try:
        from . import api as _api
        from .planner import _offline_plan

        task = _api.load_task(task_id, refresh=True) if task_id else None
        if task is not None:
            plan = _offline_plan(task.request, task.location.label if task.location else None)
            if plan.get("vehicle"):
                vehicle = str(plan["vehicle"])
            lower = task.request.lower()
            if "brake" in lower:
                job = "front pads and rotors" if "front" in lower else "brake service"
    except Exception:
        pass
    return vehicle, job


def _append_line(task_id: str, agent_id: str, role: str, text: str, started: float) -> None:
    """Persist a transcript line without blocking the audio pipeline.

    The Supabase write runs in a worker thread; the agent.updated event is
    published back onto this loop.
    """
    try:
        from . import api as _api

        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            functools.partial(
                _api.apply_transcript_line,
                task_id,
                agent_id,
                role,
                text,
                max(0.0, time.monotonic() - started),
                loop=loop,
            ),
        )
    except Exception:
        return


async def run_twilio_media(websocket: WebSocket, agent_id: str, task_id: str) -> None:
    """Take over an accepted Twilio media websocket and run the shop caller."""
    try:
        from pipecat.frames.frames import (
            LLMFullResponseEndFrame,
            LLMRunFrame,
            LLMTextFrame,
            MetricsFrame,
            TranscriptionFrame,
            TTSTextFrame,
        )
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.turns.user_mute import MuteUntilFirstBotCompleteUserMuteStrategy
        from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
            LLMUserAggregatorParams,
        )
        from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
        from pipecat.runner.utils import parse_telephony_websocket
        from pipecat.serializers.twilio import TwilioFrameSerializer
        from pipecat.services.gradium.stt import GradiumSTTService
        from pipecat.services.gradium.tts import GradiumTTSService
        from pipecat.services.openai.base_llm import BaseOpenAILLMService
        from pipecat.services.openai.llm import OpenAILLMService
        from pipecat.transports.websocket.fastapi import (
            FastAPIWebsocketParams,
            FastAPIWebsocketTransport,
        )
        from pipecat.turns.user_start import VADUserTurnStartStrategy
        from pipecat.turns.user_turn_strategies import UserTurnStrategies
        from pipecat.workers.runner import WorkerRunner
    except ImportError as exc:
        raise RuntimeError(
            "Pipecat/Gradium not installed in this interpreter. "
            "Run uvicorn with general-compute-hackathon/server/.venv"
        ) from exc

    transport_type, call_data = await parse_telephony_websocket(websocket)
    if transport_type != "twilio":
        raise RuntimeError(f"expected twilio media stream, got {transport_type}")

    stream_sid = call_data["stream_id"]
    call_sid = call_data["call_id"]
    body = call_data.get("body") or {}
    if isinstance(body, dict):
        agent_id = agent_id or str(body.get("agent_id") or "")
        task_id = task_id or str(body.get("task_id") or "")
    started = time.monotonic()

    class _Tap(FrameProcessor):
        def __init__(self, role: str) -> None:
            super().__init__(enable_direct_mode=True)
            self._role = role
            self._llm_bits: list[str] = []

        async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            text = getattr(frame, "text", "") or ""
            # Gradium emits one TranscriptionFrame per flushed utterance and never
            # sets ``finalized`` (defaults False), so do not gate on it.
            if self._role == "business" and isinstance(frame, TranscriptionFrame):
                if text.strip():
                    _append_line(task_id, agent_id, "business", text, started)
            if self._role == "agent":
                if isinstance(frame, LLMTextFrame) and text.strip():
                    self._llm_bits.append(text)
                elif isinstance(frame, LLMFullResponseEndFrame):
                    spoken = "".join(self._llm_bits).strip()
                    self._llm_bits.clear()
                    if spoken:
                        _append_line(task_id, agent_id, "agent", spoken, started)
                elif isinstance(frame, TTSTextFrame) and text.strip() and not self._llm_bits:
                    _append_line(task_id, agent_id, "agent", text, started)
            await self.push_frame(frame, direction)

    class _Timing(FrameProcessor):
        """Append per-service TTFB / processing times to server/call_metrics.log.

        One line per measurement so a slow turn can be attributed to STT,
        the LLM, or TTS after the call instead of guessed at.
        """

        def __init__(self) -> None:
            super().__init__(enable_direct_mode=True)
            self._log = Path(__file__).resolve().parent / "call_metrics.log"

        async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, MetricsFrame):
                lines = []
                for m in frame.data or []:
                    kind = type(m).__name__.replace("MetricsData", "").lower()
                    value = getattr(m, "value", None)
                    if kind in ("ttfb", "processing") and isinstance(value, (int, float)):
                        lines.append(
                            f"{time.strftime('%H:%M:%S')} {agent_id} {kind} "
                            f"{getattr(m, 'processor', '?')} {value:.3f}s\n"
                        )
                if lines:
                    try:
                        with self._log.open("a", encoding="utf-8") as fh:
                            fh.writelines(lines)
                    except OSError:
                        pass
            await self.push_frame(frame, direction)

    class ShopLLM(OpenAILLMService):
        supports_developer_role = False

        def build_chat_completion_params(self, params_from_context):
            params = super().build_chat_completion_params(params_from_context)
            params.pop("stream_options", None)
            params.pop("service_tier", None)
            return params

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=os.getenv("TWILIO_ACCOUNT_SID") or "",
        auth_token=os.getenv("TWILIO_AUTH_TOKEN") or "",
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    # delay_in_frames is the audio context Gradium holds before emitting text,
    # 80 ms per frame. Default 12 (960 ms) is tuned for accuracy; 7 (560 ms)
    # is the fastest allowed and fine for short spoken answers.
    stt = GradiumSTTService(
        api_key=os.getenv("GRADIUM_API_KEY"),
        settings=GradiumSTTService.Settings(
            delay_in_frames=int(os.getenv("GRADIUM_STT_DELAY_FRAMES", "7")),
        ),
    )
    tts = GradiumTTSService(
        api_key=os.getenv("GRADIUM_API_KEY"),
        settings=GradiumTTSService.Settings(
            voice=os.getenv("GRADIUM_VOICE_ID", "4SZHfMpw-p46Ywgs"),  # Harper, natural US adult
        ),
    )
    gc_key = os.getenv("GENERAL_COMPUTE_API_KEY") or os.getenv("GENERALCOMPUTE_API_KEY")
    llm = ShopLLM(
        api_key=gc_key,
        base_url=os.getenv("GENERAL_COMPUTE_BASE_URL", "https://api.generalcompute.com/v1"),
        settings=BaseOpenAILLMService.Settings(
            model=os.getenv("GENERAL_COMPUTE_MODEL", "gemma-4-31B-it"),
            system_instruction=_system_instruction(),
            # Turns are under 20 words by prompt; cap generation so a wordy
            # reply cannot stretch the turn. Low temperature keeps it on script.
            max_tokens=int(os.getenv("CALL_LLM_MAX_TOKENS", "80")),
            temperature=float(os.getenv("CALL_LLM_TEMPERATURE", "0.3")),
        ),
    )

    # Silero model load is ~1s of CPU; keep it off the event loop.
    vad = await asyncio.to_thread(SileroVADAnalyzer)

    context = LLMContext()
    # Gradium STT only finalizes a transcript when the pipeline flushes it, and
    # that flush is triggered by VADUserStoppedSpeakingFrame. Without a VAD
    # analyzer no TranscriptionFrame is ever emitted, so the LLM never gets a
    # second turn: the bot greets and the call goes silent. Silero VAD (same as
    # bot.py) restores that. Interruptions stay off so PSTN echo cannot clear
    # the bot's own audio, and the shop is muted until the greeting finishes.
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad,
            user_turn_stop_timeout=1.5,
            user_turn_strategies=UserTurnStrategies(
                start=[VADUserTurnStartStrategy(enable_interruptions=False)],
                # 0.6 s default is the window for the caller to resume after a
                # pause. Shop answers are short ("four fifty", "tomorrow"), so
                # 0.3 s is enough and shaves 300 ms off every turn.
                stop=[
                    SpeechTimeoutUserTurnStopStrategy(
                        user_speech_timeout=float(os.getenv("CALL_SPEECH_TIMEOUT_S", "0.3")),
                    )
                ],
            ),
            user_mute_strategies=[MuteUntilFirstBotCompleteUserMuteStrategy()],
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            _Tap("business"),
            user_aggregator,
            llm,
            _Tap("agent"),
            tts,
            transport.output(),
            _Timing(),
            assistant_aggregator,
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[],
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    kicked = {"done": False}
    vehicle, job = await asyncio.to_thread(_vehicle_and_job, task_id)

    async def _kickoff() -> None:
        if kicked["done"]:
            return
        kicked["done"] = True
        context.add_message(
            {
                "role": "user",
                "content": (
                    "The shop just answered. In one short sentence say you're an "
                    f"assistant calling for a customer with a {vehicle} who needs "
                    f"{job}, then ask only your first question: the all-in installed "
                    "price for that job. Then stop and wait. Do not ask anything "
                    "else yet. Do not invent numbers."
                ),
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        await _kickoff()

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await _kickoff()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await runner.cancel()

    async def _kickoff_soon() -> None:
        await asyncio.sleep(0.4)
        await _kickoff()

    asyncio.create_task(_kickoff_soon())
    await runner.run()
