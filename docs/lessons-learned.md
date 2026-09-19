# Lessons learned — keep the calls working

Written 2026-09-19 after the first fully working call: six questions asked one at a time, both sides transcribed, facts extracted, result synthesized. Read this before touching anything in `server/`. Every item here cost us a broken demo call once.

## The working configuration (do not change without re-testing a live call)

| Piece | Value | Where |
| --- | --- | --- |
| Call pipeline | Twilio Media Stream → Pipecat → Gradium STT → General Compute → Gradium TTS | `server/call_audio.py` |
| Turn taking | Silero VAD, `VADUserTurnStartStrategy(enable_interruptions=False)`, `SpeechTimeoutUserTurnStopStrategy()`, `MuteUntilFirstBotCompleteUserMuteStrategy()` | `server/call_audio.py` |
| Audio | 8 kHz in and out, Twilio serializer, no WAV header | `server/call_audio.py` |
| Voice | Harper `4SZHfMpw-p46Ywgs` via `GRADIUM_VOICE_ID` | `server/.env` |
| LLM | `gemma-4-31B-it` on General Compute with `stream_options`, `service_tier`, and the `developer` role stripped | `server/call_audio.py`, `server/llm.py` |
| Caller prompt | One question per turn, under 20 words, fixed order, thanks and goodbye at the end | `server/prompts/caller.md` |
| Interpreter | `general-compute-hackathon/server/.venv` (has Pipecat 1.11.0). The system Python does not. | uvicorn command |
| Tunnel | cloudflared quick tunnel to `127.0.0.1:7860`, URL in `TWILIO_WEBHOOK_BASE` | `server/.env` |
| Persistence | Supabase `tasks`, `agents`, `transcript_lines` | `server/db.py`, `server/sql/` |

## 1. Never run the demo server with `--reload` (confirmed root cause of every "application error")

Measured on 2026-09-19 with a probe hitting `/health` every second:

- A `.py` edit is noticed about 90 seconds later, not immediately.
- With no client websocket open, the worker swaps with no dropped request.
- With the app open on a task screen (its events websocket live), the old worker hung for 45 seconds, still accepting connections but answering none. Twilio's TwiML fetch timed out at 15 seconds and the caller heard "an application error has occurred, goodbye". Four test calls died exactly this way.

Fixes now in place:
- The server closes every event websocket on shutdown (`hub.close_all()` in `server/app.py`), so a restart can never hang on them.
- Start the backend from `.claude/launch.json` config `backend`, which runs uvicorn **without** `--reload` and with `--timeout-graceful-shutdown 2`. Its stdout is readable from the session.
- Code changes require an explicit restart of that process. That is the point: nothing restarts by itself mid-call.

Old rule, still true when someone insists on `--reload`: never save a `.py` file while a call is in progress, and remember the restart lands up to 90 seconds after the save.

The server runs with `uvicorn --reload`. Any save to any `.py` file under the repo restarts the worker. A restart drops the Twilio media websocket mid-call, and for about 15 seconds the tunnel answers 502, so Twilio's TwiML fetch times out and the caller hears "an application error has occurred, goodbye". Two of our test calls died exactly this way while files were being edited.

## 2. Gradium STT needs a VAD or it never finalizes a transcript

Gradium's Pipecat service only emits a `TranscriptionFrame` after a flush, and the flush is sent when the pipeline sees `VADUserStoppedSpeakingFrame`. With `vad_analyzer=None` the bot greets and then waits forever. Silero VAD, the same analyzer the playground bot uses, restores the loop. If you ever remove VAD again, you must switch the STT to `enable_turn_detection=True` and use `ExternalUserTurnStrategies` instead.

## 3. Do not gate transcripts on `frame.finalized`

`TranscriptionFrame.finalized` defaults to `False` and Gradium never sets it. Checking it drops every line the shop says. The tap saves any non-empty `TranscriptionFrame`.

## 4. Interruptions stay off on phone calls

PSTN echo and the shop saying "hello?" during the greeting were treated as barge-in and sent Twilio a clear, which wiped the bot's audio. `enable_interruptions=False` on the start strategy plus `MuteUntilFirstBotCompleteUserMuteStrategy` fixes it. The bot finishes its sentence, then listens.

## 5. The in-memory task cache is not the source of truth

`api._memory` is filled at `POST /tasks` with zero agents. The orchestrator writes agents straight to Supabase from a worker thread. Anything that looks an agent up must re-read from Supabase when it misses (`load_task(..., refresh=True)`). Transcript lines were silently dropped for hours because of this.

## 6. Nothing blocking may run on the event loop

Supabase reads, the Silero model load, and transcript writes all used to run synchronously inside async handlers. One slow database moment froze the whole server, including the two-millisecond `/twilio/voice` endpoint, and Twilio gave up after 15 seconds. Every such call is now wrapped in `asyncio.to_thread` or `run_in_executor`. Keep it that way. `server/loop_watchdog.log` records any stall over half a second; check it after a bad call.

## 7. A human-answered call is a quote, not an error

The dialer only labels machine and no-answer legs. A completed call with no label was being mapped to `outcome: error`, status `failed`, so every good conversation showed as failed and extraction was skipped. Completed plus started now means `quote`; extraction decides what came out of it.

## 8. Twilio trial account behaviour

- The callee hears "you have a trial account, press any key" first. Without a key press Twilio never fetches our TwiML and the call ends after 13 seconds. Every 13 to 14 second call in the log is this.
- Trial accounts can only dial verified numbers, so `TWILIO_DEMO_TO` is the only reachable destination and every task currently rings that phone. Upgrading the account lifts both limits.
- `TWILIO_DEMO_TO` has no default in code. Set it in `server/.env`. Never commit a phone number.

## 9. How to tell what went wrong on a call

In this order:
1. Twilio call events and debugger alerts (`client.calls(sid).events.list()`, `client.monitor.v1.alerts.list()`). A 502 with "Total timeout" means our server did not answer.
2. `server/loop_watchdog.log` for event-loop stalls at that time.
3. Was a reload happening? Check whether any `.py` file was saved in the previous 30 seconds.
4. `GET /tasks/{id}` for transcript lines on the dialed agent. Zero lines with a long call means the tap or the cache path broke.
5. The cloudflared quick tunnel occasionally takes 4 to 5 seconds. If that becomes common, use a named tunnel or ngrok.

## 10. Things that are still stubbed

- Booking is a no-op.
- Only the first agent is dialed and it goes to `TWILIO_DEMO_TO`. Parallel dialing to real shop numbers waits on the account upgrade.
- Nothing hangs up automatically. The bot says goodbye; the callee ends the call.
- The web agent searches for 2019 Camry pads regardless of the vehicle spoken.
- Agent summaries after extraction still read "Call answered" rather than "$450 all-in · $150/h".

## Smoke test after any change

```bash
curl -s http://127.0.0.1:7860/health
curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" -X POST "$TWILIO_WEBHOOK_BASE/twilio/voice?agent_id=x&task_id=y"
python -m pytest -q server/tests
```

Then one real call with a key press, and confirm transcript lines on the dialed agent.
