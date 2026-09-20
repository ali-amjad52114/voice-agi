# Voice AGI

**A voice-native action agent.** You say an outcome. It looks up a real part price, finds real nearby shops, phones them, keeps the conversations, and comes back with a decision that Python has re-checked dollar by dollar.

> "One mechanic quoted me $800 for front brakes on my 2019 Camry, I'm in Fremont. Find the part price online, call mechanics and dealers near me, get their all-in price and hourly labor rate, and tell me whether I should bring my own part or let them supply it."

That one sentence becomes: a General Compute plan, a General Compute tool loop that orders SerpAPI lookups, eight real Fremont shops, three online part prices, a Twilio call driven turn by turn by General Compute with Gradium as ears and mouth, a transcript, extracted facts, a streamed recommendation, and a result card whose every dollar was either spoken by a shop or returned by a search.

Built at the **General Compute Hackathon, September 2026**. ChatGPT is question → answer. This is intent → work → result.

---

## Contents

- [The pipeline](#the-pipeline)
- [General Compute: the brain, used five ways](#general-compute-the-brain-used-five-ways)
- [Gradium: ears and mouth on a phone line](#gradium-ears-and-mouth-on-a-phone-line)
- [Pipecat: the real-time spine](#pipecat-the-real-time-spine)
- [MentraOS: the glasses entry](#mentraos-the-glasses-entry)
- [Twilio, SerpAPI, Supabase](#twilio-serpapi-supabase)
- [What we measured](#what-we-measured)
- [Honesty rules](#honesty-rules)
- [Run it](#run-it)
- [Repo layout](#repo-layout)
- [Tests](#tests)

---

## The pipeline

```
speak
  → plan            General Compute, JSON schema
  → order lookups   General Compute tool loop → discover_shops (SerpAPI Maps), lookup_part (SerpAPI Shopping)
  → dial            Twilio Media Streams → Pipecat → Gradium STT → General Compute → Gradium TTS
  → note facts      General Compute tool calls on the live call: note_fact, end_call
  → extract         General Compute, JSON schema over the full transcript
  → decide          General Compute, streamed JSON; the "why" streams to the card while the model writes
  → verify          Python recomputes every total; drops any dollar or shop not in the inputs
  → result card     BYO vs shop-supplied, recommendation, tradeoffs, live "why" then locked "why"
```

Every stage that thinks runs on General Compute. Every stage that speaks or listens runs on Gradium. Every stage that moves audio in real time runs on Pipecat. Python never invents a number; it only checks them.

---

## General Compute: the brain, used five ways

General Compute is an OpenAI-compatible inference API on SambaNova silicon. We treated it as the only place reasoning happens and used a different part of its surface at each stage.

### 1. Planner (JSON schema output)

`server/planner.py` sends the spoken sentence and an optional location hint and asks for a strict JSON plan: title, the user's existing quote, vehicle, city, the facts the call must collect, a call script, and an extraction schema. We use `response_format: json_schema`. A regex fallback covers the demo sentence if the network is down, so the demo never blocks on inference.

### 2. Tool loop that orders the real world (function calling)

`server/gc_loop.py` gives the model two OpenAI-style tools:

| Tool | Backed by | Returns only |
| --- | --- | --- |
| `discover_shops(lat, lng, query)` | SerpAPI `google_maps` | name, phone, url, type |
| `lookup_part(query)` | SerpAPI `google_shopping` | seller, url, price, partsType |

The model decides what to call and in what order, we execute each `tool_calls` entry in process, append `role: tool` results, and loop until it answers in prose or hits an eight-step cap. Guardrails: the shop list is exactly the union of what `discover_shops` returned, deduplicated by phone. The part list is exactly what `lookup_part` returned. Anything the model writes in text is ignored. Model-supplied coordinates are logged and replaced with the task's real coordinates. Any API exception or step cap falls back to the direct discovery path, so the loop can never fail a task.

Live on 2026-09-19: three steps in 1.8 seconds total. `discover_shops` → 8 shops. `lookup_part` → 3 prices. Then a one-sentence summary. Nothing invented.

### 3. The live call brain (streaming chat through Pipecat)

`server/call_audio.py` subclasses Pipecat's `OpenAILLMService` and points it at `https://api.generalcompute.com/v1`. Every turn on the phone is a streamed General Compute completion, so Gradium TTS starts speaking on the first tokens. We strip the three OpenAI-only extras General Compute rejects (`developer` role, `stream_options`, `service_tier`), cap `max_tokens` at 80 so a wordy reply cannot stretch a turn, and hold temperature at 0.3 to keep the caller on script.

The caller prompt asks one question per turn, under twenty words, in a fixed order: all-in price, part price, labor rate, hours, customer parts accepted, OEM or aftermarket, warranty, earliest slot. It may confirm implied labor as "all-in minus part" but may never state a number the shop did not say.

### 4. Tools on the phone (function calling, live)

The call brain has two tools attached through Pipecat's `ToolsSchema` and `register_function`:

- **`note_fact(field, value)`**. The model calls it the moment the shop states a fact. Before recording, `server/call_tools.py` checks the value against the last three business transcript lines: digits, `$420`, and spoken forms like "four twenty" or "one hundred eighty" all count. A number the shop did not say is rejected and the model is told so. Noted facts are hints for the UI. The canonical facts still come from extraction at hangup.
- **`end_call(reason)`**. After all eight facts, a refusal, or voicemail, the model says one goodbye sentence and calls it. We wait for Pipecat's `BotStoppedSpeakingFrame` so the goodbye finishes playing, queue an `EndFrame`, and hang up the Twilio leg over REST as a backup.

Verified against the live endpoint: General Compute returns `tool_calls`, streams tool-call deltas, honours `tool_choice`, and continues correctly after a `role: tool` message, on both `gpt-oss-120b` and `gemma-4-31B-it`.

### 5. Extract and decide (JSON schema, streaming, retry)

**Extract** (`server/extract.py`) runs once per call over the full transcript and returns a `Facts` object with a confidence score. The planner may add fields to the schema but can never redefine a canonical one. No transcript means confidence zero and no facts, never a guess.

**Decide** (`server/synthesize.py`) is one fat pass. The model sees every shop's facts, the last forty lines of each shop's transcript for hassle and warranty wording, the online part prices, the user's existing quote, and the preference rule. It returns options, a recommended index, a two-sentence "why", and tradeoffs.

- **It streams.** `complete_stream` in `server/llm.py` sends `stream: true` with the JSON schema. `WhyStreamer` pulls the `why` string out of the JSON as characters arrive, decoding escapes across chunk boundaries, and the orchestrator publishes each new piece as a `result.partial` websocket event. The card shows the recommendation being written, then locks to the verified result.
- **Python verifies.** Every option total is recomputed from the cited agents and must match within a dollar. Any option that cites an unknown agent, uses a dollar not in the inputs, or builds bring-your-own for a shop that does not accept customer parts is dropped. Prose dollars are checked too.
- **One retry with reasons.** If verification rejects the first answer, the model gets the same sheet plus a bullet list of exactly what was wrong: unknown agent ids, dollars not in the input, a bad recommended index, the wrong sentence count. If that fails too, a deterministic Python decision runs.

### Model routing, measured not assumed

| Stage | Default model | Why |
| --- | --- | --- |
| Live call brain | `gpt-oss-120b` | 0.43 s time to first token on the real caller prompt, vs 1.2 to 1.3 s for `gemma-4-31B-it` and 1.2 to 1.9 s for `minimax-m2.7` |
| Planner, extract, decision, tool loop | `gpt-oss-120b` | 1.8 s on the real extraction schema; `gemma-4-31B-it` hung for 120 s on the same structured request |
| Overrides | `PLANNER_MODEL`, `EXTRACT_MODEL`, `DECISION_MODEL`, `CALL_LLM_MODEL` | Gemma 4 answers the decision-sized JSON in about 1.5 s and streams it, so `DECISION_MODEL=gemma-4-31B-it` is a supported configuration |

### Client hardening we had to learn

- **Timeouts.** The OpenAI SDK default is a 600 s timeout with two retries. One rejected schema turned into a thirty-minute orchestrator hang. Every one-shot call now has a 90 s timeout and one retry. Every call is capped at 1,200 completion tokens.
- **JSON mode fallback.** If a `json_schema` request is ever refused with a 400, the client retries once as `json_object` with the schema appended to the system prompt as text.
- **Usage ledger.** `server/gc_usage.py` records every General Compute call: stage, model, prompt tokens, completion tokens, latency, JSON mode, and time to first token for streams. One JSON line per call in `server/gc_usage.log` plus an in-memory list per task. This is how we know what each stage costs.

---

## Gradium: ears and mouth on a phone line

Gradium handles every word spoken or heard on the PSTN call. Pipecat's `GradiumSTTService` and `GradiumTTSService` are wired in `server/call_audio.py`.

### Speech-to-text

- **Streaming transcription** over Gradium's WebSocket, on 8 kHz mu-law telephone audio straight from Twilio.
- **Semantic end-of-turn detection.** Gradium's STT emits an inactivity probability every 80 ms at several horizons. We enable `enable_turn_detection` with `eot_horizon_s = 2.0` and `eot_threshold = 0.5`, and let Pipecat's `ExternalUserTurnStrategies` turn Gradium's proposals into user turns. This replaced Silero VAD, which on 8 kHz phone audio fired on roughly one utterance in four and left the bot silent for up to forty seconds while the shop repeated itself. Gradium was already hearing every word; now it also decides when the turn ends.
- **Adaptive delay.** `delay_in_frames = 7` (560 ms of context) instead of the default 12 (960 ms), the fastest Gradium allows, tuned for short spoken answers.
- **Flush on turn end.** Gradium only finalizes a transcript on flush. Pipecat sends the flush when the turn ends, so the transcript for "it's four twenty all in" lands as one line the moment the shop stops talking.
- **Transcript tap.** Every non-empty `TranscriptionFrame` is persisted to Supabase from a worker thread and pushed to the UI as an `agent.updated` event, so the caller's phone card shows lines as they land. We learned not to gate on `finalized`, which Gradium never sets.

### Text-to-speech

- **Streaming synthesis from streaming tokens.** General Compute streams text, Pipecat sentence-aggregates it, Gradium speaks each sentence as it completes. The shop hears the first words while the model is still writing.
- **Voice.** Harper, a natural US adult voice, via `GRADIUM_VOICE_ID`. Two alternates are listed in `server/.env.example`.
- **Telephony format.** Gradium produces 48 kHz PCM; Pipecat's Twilio serializer converts to 8 kHz mu-law for the phone. No WAV header, no resampling surprises.
- **Word timestamps.** Gradium returns per-segment timings, which is what makes "the context contains only what the shop actually heard" possible after an interruption.

### Limits we hit and documented

- A Gradium session caps at 300 seconds on the current plan. Our five-minute test call went deaf at exactly 300 s with `Session exceeded maximum duration`. Calls stay short, or the plan moves to the 3,000 s tier before a long demo.
- Interruptions stay off on phone calls. PSTN echo and a shop saying "hello?" during the greeting were treated as barge-in and cleared the bot's audio. The bot finishes its sentence, then listens.

The rest of Gradium's surface, evaluated and noted in `docs/gradium.md`: speech-to-speech with live translation, voice cloning from a ten-second sample, voice design from a text prompt, pronunciation dictionaries, keyword boosting for STT, five languages with auto-detection, mu-law and A-law telephony formats, browser tokens so API keys never ship to clients, EU and US data residency, zero data retention on paid plans. We used the telephony formats and word timestamps; the rest is where this goes next: a cloned voice per business persona, keyword boosting for part names, and Spanish shops.

---

## Pipecat: the real-time spine

Pipecat is the framework that turns a Twilio media websocket into a conversation. `server/call_audio.py` builds this pipeline per call:

```
transport.input()          FastAPIWebsocketTransport, TwilioFrameSerializer, 8 kHz in and out
  → GradiumSTTService      streaming STT with semantic turn detection
  → business tap           persist every transcript line, feed note_fact's grounding window
  → user aggregator        ExternalUserTurnStrategies, interruptions off, mute until first bot turn completes
  → ShopLLM                OpenAILLMService subclass → General Compute, tools attached via LLMContext
  → agent tap              persist what the bot said
  → GradiumTTSService      streaming TTS, Harper
  → transport.output()
  → bot speech watcher     BotStoppedSpeakingFrame → post-goodbye hangup
  → timing tap             MetricsFrame TTFB per service → server/call_metrics.log
  → assistant aggregator
```

Pipecat features in use:

- **Telephony transport.** `FastAPIWebsocketTransport` with `TwilioFrameSerializer` and `parse_telephony_websocket` to take over Twilio's Media Stream inside FastAPI.
- **Turn strategies.** `ExternalUserTurnStrategies(enable_interruptions=False)` driven by Gradium, with `MuteUntilFirstBotCompleteUserMuteStrategy` so the greeting cannot be interrupted by echo. Silero VAD with loosened confidence and volume remains behind `GRADIUM_TURN_DETECTION=0`.
- **Native function calling.** `LLMContext(tools=ToolsSchema(standard_tools=[FunctionSchema(...)]))`, `llm.register_function`, `FunctionCallParams.result_callback` with `FunctionCallResultProperties(run_llm=...)`. Pipecat runs the `role: tool` round trip; we never hand-rolled a loop.
- **Metrics.** `PipelineParams(enable_metrics=True, enable_usage_metrics=True)` and a processor that writes every TTFB and processing time per service to a log, so a slow turn is attributed to STT, the LLM, or TTS instead of guessed.
- **Workers.** `PipelineWorker` and `WorkerRunner` per call, cancelled cleanly on client disconnect.
- **Frames for lifecycle.** `LLMRunFrame` to kick off the greeting, `BotStoppedSpeakingFrame` to know the goodbye finished, `EndFrame` to end the pipeline after it.
- **Smart Turn v3 and SmallWebRTC** in the browser playground bot under `general-compute-hackathon/`, the original Gradium plus Gemma 4 cascade the product grew out of. That bot also has a Dockerfile and `pcc-deploy.toml` for Pipecat Cloud.

Lessons that cost us a broken call each, all in `docs/lessons-learned.md`: never run the demo server with `--reload`; nothing blocking may run on the event loop; a human-answered call is a quote, not an error.

---

## MentraOS: the glasses entry

`mentra-live-entry/` is a MentraOS miniapp for Mentra Live glasses. It is deliberately thin: the glasses are only the microphone and the speaker. Audio streams as PCM over a websocket to the Pipecat bot's `/ws-client` endpoint, and the same Gradium → General Compute → Gradium cascade answers. The glasses button toggles the socket. The phone tile shows connection state.

It runs against the playground bot and is not part of the phone-call demo, but it is the same brain and the same voice, one step closer to "say it and it gets done" with nothing in your hand.

---

## Twilio, SerpAPI, Supabase

**Twilio** places the outbound call from `TWILIO_FROM`. `POST /twilio/voice` returns TwiML with `<Connect><Stream>` to our media websocket, no canned `<Say>`. Status callbacks land on `/twilio/status` and set outcome from Twilio's answered-by data: machine, no answer, or a human. A completed, started call is a quote; extraction decides what came out of it. The call brain can hang up over REST. Trial-account behaviour is documented: the callee must press a key or Twilio never fetches TwiML, and only verified numbers are reachable, so `TWILIO_DEMO_TO` is the demo phone.

**SerpAPI** is the only source of businesses and part prices. `google_maps` near the task's coordinates (browser GPS, or Nominatim geocoding of the spoken city) returns real names and phones, capped at eight. `google_shopping` returns pads-and-rotors listings for the planner's vehicle, with filters that reject the wrong axle, the wrong model, calipers, and hardware kits, and deduplicate sellers. No Google Places key anywhere.

**Supabase** Postgres holds `tasks`, `agents`, and `transcript_lines` (`server/sql/001_voice_agi.sql`). The orchestrator writes from a worker thread; the API re-reads on a cache miss. Interrupted tasks resume on startup.

---

## What we measured

| Measurement | Value |
| --- | --- |
| Live call brain TTFT, `gpt-oss-120b` | 0.43 s |
| Live call brain TTFT, `gemma-4-31B-it` | 1.2 to 1.3 s |
| Extraction with JSON schema, `gpt-oss-120b` | 1.8 s |
| Extraction with JSON schema, `gemma-4-31B-it` | hung, 120 s, no output |
| Gemma 4 decision JSON, streamed | first chunk 1.3 s, 18 chunks, done at 2.0 s |
| Tool loop, three steps | 0.49 s + 0.38 s + 0.91 s |
| Gradium TTS TTFB on a live call | about 0.5 s |
| Twilio TwiML fetch through the tunnel | 0.15 to 0.6 s |
| Silero VAD on 8 kHz phone audio | fired on roughly 1 in 4 utterances, replaced |
| Gradium session cap | 300 s on the current plan |

All numbers from 2026-09-19 on the real prompts and a real phone leg.

---

## Honesty rules

These hold in every prompt, every tool, and every verifier:

- **Never invent a shop.** Shops come from SerpAPI Maps or nowhere.
- **Never invent a dollar.** A price is either spoken by a shop and extracted from the transcript, or returned by SerpAPI Shopping. The decision model may only combine those by stated formulas. Python recomputes every total.
- **A noted fact must be in the transcript.** `note_fact` rejects any value not present in the last three business lines.
- **No transcript, no facts.** Extraction returns confidence zero rather than a guess.
- **Failures stay visible.** A call with no quote shows "no quote" on its card. An empty decision says "No shop returned a real quote. We did not invent shop prices."
- **Book is a no-op.** The product compares; it does not commit money.

---

## Run it

Backend, from the repo root, using the hackathon venv because it has Pipecat:

```bash
cp server/.env.example server/.env   # fill in keys
general-compute-hackathon/server/.venv/Scripts/python.exe -m uvicorn server.app:app --host 127.0.0.1 --port 7860 --timeout-graceful-shutdown 2
```

Never add `--reload` to the demo server. Expose it for Twilio and put the URL in `TWILIO_WEBHOOK_BASE`:

```bash
cloudflared tunnel --url http://127.0.0.1:7860
```

Frontend:

```bash
cd frontend && npm install && npm run dev
```

Set `VITE_API_MODE=live` in `frontend/.env.local` to talk to the backend, or leave it on `mock` for a fixture-driven demo that also replays the streamed "why".

Submit the demo task:

```bash
curl -s -X POST http://127.0.0.1:7860/tasks -H "content-type: application/json" -d "{\"request\":\"One mechanic quoted me \$800 for front brakes on my 2019 Camry, I am in Fremont. Find the part price online, call mechanics and dealers near me, get their all-in price and their hourly labor rate, and tell me whether I should bring my own part or let them supply it.\",\"location\":\"Fremont, CA\"}"
```

Env names: `GENERAL_COMPUTE_API_KEY`, `GENERAL_COMPUTE_MODEL`, `PLANNER_MODEL`, `EXTRACT_MODEL`, `DECISION_MODEL`, `CALL_LLM_MODEL`, `GRADIUM_API_KEY`, `GRADIUM_VOICE_ID`, `TWILIO_*`, `SERPAPI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`. Never commit `.env`.

---

## Repo layout

| Path | What |
| --- | --- |
| `server/` | FastAPI on 7860. `orchestrator.py` runs a task. `gc_loop.py` is the General Compute tool loop. `call_audio.py` is the Pipecat call pipeline. `call_tools.py` grounds `note_fact`. `llm.py` is the General Compute client with streaming, fallback, and the usage ledger. `synthesize.py` decides and verifies. `prompts/` holds every prompt. |
| `frontend/` | React, Vite, TypeScript. Home with mic, task screen with live transcript, streamed "why", result card, agent cards. |
| `general-compute-hackathon/` | The original Pipecat playground bot: Gradium, Gemma 4 on General Compute, Smart Turn v3, SmallWebRTC. Its venv runs the server. |
| `mentra-live-entry/` | MentraOS glasses miniapp, mic and speaker only. |
| `docs/` | `voice-agi-spec.md` (the build spec), `lessons-learned.md` (read before touching `server/`), `general-compute.md` and `gradium.md` (feature notes), `gemma4-gc-plan.md` (session plan). |
| `fixtures/` | A complete fake task for building the UI without the backend. |

---

## Tests

```bash
python -m pytest -q server/tests
```

192 tests, no network. They cover the verifier and its retry, the streamed "why" parser at every split point, the tool loop with scripted models and hallucinated shops, `note_fact` grounding including spoken numbers, the JSON mode fallback with a fake client, extraction, the planner fallback, part lookup filters, and the orchestrator wiring with the loop present, absent, and failing.

```bash
cd frontend && npx tsc --noEmit -p . && npm run build
```
