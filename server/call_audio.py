"""Twilio Media Stream → Gradium STT/TTS shop caller.

Uses Pipecat when installed (hackathon venv). Does not invent shop prices.
Transcript lines are appended onto the in-memory task agent for extract.

Call tools (Session 7): the call brain gets ``note_fact`` and ``end_call``
through Pipecat's native function-calling loop. A note is accepted only when
the value appears in the shop's last three transcript lines. ``end_call`` lets
the goodbye play out, then ends the pipeline and hangs up the Twilio leg.
Noted values are logged, never written to ``agent.facts``: extraction over the
full transcript at hangup stays canonical.
"""

from __future__ import annotations

import asyncio
import functools
import os
import time
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from .call_tools import CallToolState, end_call_schema, note_fact_schema

_PROMPT = Path(__file__).resolve().parent / "prompts" / "caller.md"

# Seconds after the goodbye audio stops before the pipeline is ended. Twilio
# plays the last packets a beat after we send them.
_GOODBYE_GRACE_S = 0.5
# Longest we wait for the goodbye to start / finish before ending anyway.
_GOODBYE_START_TIMEOUT_S = 3.0
_GOODBYE_STOP_TIMEOUT_S = 12.0
# REST hangup fallback fires this long after the EndFrame is queued.
_REST_HANGUP_DELAY_S = 3.0


def _hangup_via_rest(call_sid: str) -> bool:
    """Complete the Twilio call leg over REST. Blocking; run in a thread.

    Fallback for when closing the media stream does not end the call. Only
    runs with Twilio credentials present, and never raises into the caller.
    """
    if not call_sid or not (os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN")):
        return False
    try:
        from .twilio_call import _client

        _client().calls(call_sid).update(status="completed")
        return True
    except Exception:
        return False


def _system_instruction(language: str = "en") -> str:
    text = _PROMPT.read_text(encoding="utf-8") if _PROMPT.is_file() else (
        "I'm an assistant calling for a customer about a brake quote."
    )
    from . import lang as _lang

    code = _lang.normalize(language)
    spoken = f"\nSpeak {_lang.name(code)} for the whole call."
    if code != "en":
        spoken += (
            f" Ask every question in {_lang.name(code)}. If the shop answers in English, switch to English"
            " and stay there. Tool arguments (note_fact field names, end_call reasons) stay in English."
        )
    return text.strip() + spoken + (
        "\nSpeak only. No markdown. Never invent a dollar amount the shop did not say."
    )


def _task_language(task_id: str) -> str:
    """Language the planner recorded for the task; detected from the request on a miss."""
    try:
        from . import api as _api
        from . import lang as _lang

        task = _api.load_task(task_id, refresh=True) if task_id else None
        return _lang.get_task_language(task_id, task.request if task is not None else None)
    except Exception:
        return "en"


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
        from loguru import logger
        from pipecat.adapters.schemas.function_schema import FunctionSchema
        from pipecat.adapters.schemas.tools_schema import ToolsSchema
        from pipecat.frames.frames import (
            BotStartedSpeakingFrame,
            BotStoppedSpeakingFrame,
            EndFrame,
            FunctionCallResultProperties,
            LLMFullResponseEndFrame,
            LLMRunFrame,
            LLMTextFrame,
            MetricsFrame,
            TranscriptionFrame,
            TTSTextFrame,
        )
        from pipecat.services.llm_service import FunctionCallParams
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams
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
        from pipecat.turns.user_turn_strategies import (
            ExternalUserTurnStrategies,
            UserTurnStrategies,
        )
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

    # Tool state for this call: last three business lines, accepted notes,
    # end flag. ``speech["last_text_at"]`` is when the LLM last streamed a
    # spoken token; a tool handler uses it to tell whether the model already
    # said its sentence in this turn (text + tool call) or only called the tool.
    state = CallToolState()
    speech: dict[str, float] = {"last_text_at": 0.0}

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
                    state.record_business_line(text)
                    _append_line(task_id, agent_id, "business", text, started)
            if self._role == "agent":
                if isinstance(frame, LLMTextFrame) and text.strip():
                    self._llm_bits.append(text)
                    speech["last_text_at"] = time.monotonic()
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

    class _BotSpeech(FrameProcessor):
        """Track whether the bot's audio is playing out.

        Sits right after ``transport.output()``. The output transport pushes
        ``BotStartedSpeakingFrame`` / ``BotStoppedSpeakingFrame`` downstream as
        it writes audio (bot-stopped fires on ``TTSStoppedFrame`` once the
        audio queue drained, or after 0.35 s of no audio). The FastAPI
        websocket output paces writes at real time, so "stopped" here means
        the goodbye has actually been sent, not merely synthesized. The
        transport itself has no ``on_bot_stopped_speaking`` event in 1.11, so
        this is the one place that sees it.
        """

        def __init__(self) -> None:
            super().__init__(enable_direct_mode=True)
            self.speaking = False
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()

        async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, BotStartedSpeakingFrame):
                self.speaking = True
                self.stopped.clear()
                self.started.set()
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self.speaking = False
                self.stopped.set()
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
    # Turn detection. Measured 2026-09-19: Silero VAD on 8 kHz phone audio
    # fired on only a fraction of the shop's utterances, so Gradium had
    # transcribed the answer but nothing flushed it and the bot sat silent
    # while the caller repeated themselves. Gradium's server-side semantic
    # end-pointing runs on the audio Gradium is already hearing correctly.
    # GRADIUM_TURN_DETECTION=0 falls back to a loosened Silero.
    use_gradium_turns = os.getenv("GRADIUM_TURN_DETECTION", "1") != "0"
    # Language: the planner's detected language grounds STT (better accuracy
    # than auto-detect on 8 kHz phone audio) and picks the TTS voice.
    # GRADIUM_STT_LANGUAGE=any lets Gradium detect the language instead.
    from . import lang as _lang
    from pipecat.transcriptions.language import Language

    call_language = await asyncio.to_thread(_task_language, task_id)
    stt_language_env = (os.getenv("GRADIUM_STT_LANGUAGE") or "").strip().lower()
    if stt_language_env == "any":
        stt_language: Any = "any"
    else:
        stt_language = Language(stt_language_env or call_language)
    stt_settings: dict[str, Any] = {
        "delay_in_frames": int(os.getenv("GRADIUM_STT_DELAY_FRAMES", "7")),
        "language": stt_language,
    }
    if use_gradium_turns:
        # Horizon: which future window the inactivity probability refers to.
        # Threshold: probability at or above which an open turn ends.
        stt_settings["eot_horizon_s"] = float(os.getenv("GRADIUM_EOT_HORIZON_S", "2.0"))
        stt_settings["eot_threshold"] = float(os.getenv("GRADIUM_EOT_THRESHOLD", "0.5"))
    stt = GradiumSTTService(
        api_key=os.getenv("GRADIUM_API_KEY"),
        enable_turn_detection=use_gradium_turns,
        settings=GradiumSTTService.Settings(**stt_settings),
    )
    tts = GradiumTTSService(
        api_key=os.getenv("GRADIUM_API_KEY"),
        settings=GradiumTTSService.Settings(
            # Harper (US) for English; Ximena / Solène / Resi / Rafaela for
            # es / fr / de / pt. Override per language with GRADIUM_VOICE_ID_<LANG>.
            voice=_lang.voice_for(call_language),
            language=Language(call_language),
        ),
    )
    gc_key = os.getenv("GENERAL_COMPUTE_API_KEY") or os.getenv("GENERALCOMPUTE_API_KEY")
    llm = ShopLLM(
        api_key=gc_key,
        base_url=os.getenv("GENERAL_COMPUTE_BASE_URL", "https://api.generalcompute.com/v1"),
        settings=BaseOpenAILLMService.Settings(
            # Model routing: the live call brain needs the lowest time to first
            # token. Measured with the real caller prompt: gpt-oss-120b 0.43 s,
            # minimax-m2.7 1.2-1.9 s, gemma-4-31B-it 1.2-1.3 s. Planner,
            # extraction and the decision keep GENERAL_COMPUTE_MODEL.
            model=os.getenv("CALL_LLM_MODEL", "gpt-oss-120b"),
            system_instruction=_system_instruction(call_language),
            # Turns are under 20 words by prompt; cap generation so a wordy
            # reply cannot stretch the turn. Low temperature keeps it on script.
            max_tokens=int(os.getenv("CALL_LLM_MAX_TOKENS", "80")),
            temperature=float(os.getenv("CALL_LLM_TEMPERATURE", "0.3")),
        ),
    )

    # Tools ride on the context: LLMContext(tools=ToolsSchema(...)) is what the
    # OpenAI adapter reads to emit ``tools=[{"type": "function", ...}]``.
    # Handlers are registered on the service; Pipecat's own loop turns the
    # streamed ``tool_calls`` into handler calls, appends the ``role: tool``
    # result to the context, and re-runs the model when ``run_llm`` says so.
    def _schema(spec: dict[str, Any]) -> Any:
        params = spec["parameters"]
        return FunctionSchema(
            name=spec["name"],
            description=spec["description"],
            properties=params["properties"],
            required=params["required"],
        )

    context = LLMContext(
        tools=ToolsSchema(standard_tools=[_schema(note_fact_schema()), _schema(end_call_schema())])
    )
    bot_speech = _BotSpeech()
    background: set[asyncio.Task[Any]] = set()

    def _model_spoke_this_turn() -> bool:
        # Text streamed in the same response as the tool call lands here well
        # under a second before the handler runs; the previous turn's text is
        # separated by the shop's whole answer.
        return time.monotonic() - speech["last_text_at"] < 1.5

    async def _on_note_fact(params: FunctionCallParams) -> None:
        args = params.arguments or {}
        field = str(args.get("field") or "")
        value = args.get("value")
        result = state.apply_note(field, value)
        if result.get("ok"):
            logger.info(f"note_fact {agent_id}: {field}={result.get('value')!r}")
            if state.should_end():
                result["next"] = (
                    "All eight facts are noted. Say one short thanks-and-goodbye "
                    "sentence and call end_call with reason all_facts in this turn."
                )
            elif not _model_spoke_this_turn():
                result["next"] = "Acknowledge briefly and ask the next question."
        else:
            logger.info(f"note_fact {agent_id} rejected: {field}={value!r} ({result.get('reason')})")
        # If the model already spoke in this turn (text + tool call) do not run
        # inference again: it would speak twice. If it only called the tool,
        # run again so it asks the next question. Once the last fact lands,
        # always run again so the goodbye and end_call happen now, not after
        # the shop's next utterance.
        run_again = state.should_end() or not _model_spoke_this_turn()
        await params.result_callback(
            result, properties=FunctionCallResultProperties(run_llm=run_again)
        )

    async def _finish_after_goodbye() -> None:
        """End the pipeline once the goodbye has played, then hang up over REST.

        Hook choice: ``BotStoppedSpeakingFrame`` from the output transport,
        observed by ``_BotSpeech`` just after ``transport.output()``. It is
        the only signal tied to audio actually written to the websocket:
        ``LLMFullResponseEndFrame`` fires before TTS has even started and
        ``TTSStoppedFrame`` fires when synthesis ends, while the output is
        still draining the paced audio queue. If the goodbye never starts
        (model called the tool without text) we end after a short wait.
        """
        try:
            if not bot_speech.speaking:
                bot_speech.started.clear()
                try:
                    await asyncio.wait_for(bot_speech.started.wait(), _GOODBYE_START_TIMEOUT_S)
                except asyncio.TimeoutError:
                    pass
            if bot_speech.speaking:
                try:
                    await asyncio.wait_for(bot_speech.stopped.wait(), _GOODBYE_STOP_TIMEOUT_S)
                except asyncio.TimeoutError:
                    pass
            await asyncio.sleep(_GOODBYE_GRACE_S)
            # EndFrame is a control frame: it flushes in order behind whatever
            # is still queued, closes the transport, and lets ``runner.run()``
            # return. Closing the <Connect><Stream> websocket ends the TwiML
            # and normally drops the call; REST completes it if Twilio did not.
            await worker.queue_frames([EndFrame()])
            await asyncio.sleep(_REST_HANGUP_DELAY_S)
            done = await asyncio.to_thread(_hangup_via_rest, call_sid)
            logger.info(f"end_call {agent_id}: rest hangup {'sent' if done else 'skipped'}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"end_call {agent_id}: finish failed: {exc}")

    async def _on_end_call(params: FunctionCallParams) -> None:
        args = params.arguments or {}
        result = state.apply_end(str(args.get("reason") or "other"))
        logger.info(f"end_call {agent_id}: reason={state.end_reason} noted={state.noted}")
        spoke = _model_spoke_this_turn()
        if not spoke:
            # Tool call without the goodbye text: let the model say it. The
            # finisher below waits for that audio before ending.
            result["next"] = "Say one short goodbye sentence and nothing else."
        await params.result_callback(
            result, properties=FunctionCallResultProperties(run_llm=not spoke)
        )
        task = asyncio.create_task(_finish_after_goodbye())
        background.add(task)
        task.add_done_callback(background.discard)

    llm.register_function("note_fact", _on_note_fact)
    llm.register_function("end_call", _on_end_call)

    # Gradium STT only finalizes a transcript when a turn ends. With Gradium
    # turn detection the STT service proposes the start and stop itself and
    # ExternalUserTurnStrategies turns those proposals into user turns. In
    # the Silero fallback, VADUserStoppedSpeakingFrame triggers the flush.
    # Either way interruptions stay off so PSTN echo cannot clear the bot's
    # own audio, and the shop is muted until the greeting finishes.
    if use_gradium_turns:
        vad = None
        strategies = ExternalUserTurnStrategies(enable_interruptions=False)
        stop_timeout = float(os.getenv("CALL_TURN_STOP_TIMEOUT_S", "4.0"))
    else:
        # Loosened for 8 kHz phone audio: default confidence 0.7 / min_volume
        # 0.6 missed most utterances. Silero model load is ~1 s of CPU; keep it
        # off the event loop.
        vad = await asyncio.to_thread(
            lambda: SileroVADAnalyzer(
                params=VADParams(
                    confidence=float(os.getenv("SILERO_CONFIDENCE", "0.5")),
                    min_volume=float(os.getenv("SILERO_MIN_VOLUME", "0.3")),
                    stop_secs=0.2,
                )
            )
        )
        strategies = UserTurnStrategies(
            start=[VADUserTurnStartStrategy(enable_interruptions=False)],
            stop=[
                SpeechTimeoutUserTurnStopStrategy(
                    user_speech_timeout=float(os.getenv("CALL_SPEECH_TIMEOUT_S", "0.3")),
                )
            ],
        )
        stop_timeout = 1.5
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad,
            user_turn_stop_timeout=stop_timeout,
            user_turn_strategies=strategies,
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
            bot_speech,
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
                    f"The shop just answered. Speak {_lang.name(call_language)}. In one short "
                    f"sentence say you're an assistant calling for a customer with a {vehicle} "
                    f"who needs {job}, then ask only your first question: the all-in installed "
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
        # Logged only. Extraction over the full transcript at hangup is the
        # canonical source of agent.facts; the notes are the model's tally.
        logger.info(
            f"call {agent_id} disconnected: noted={state.noted} end_reason={state.end_reason}"
        )
        await runner.cancel()

    async def _kickoff_soon() -> None:
        await asyncio.sleep(0.4)
        await _kickoff()

    asyncio.create_task(_kickoff_soon())
    await runner.run()
