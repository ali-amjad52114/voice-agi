# Voice AGI — build spec

Status: v2, 2026-09-19. **One builder owns everything:** `server/` and `frontend/`. This replaces the Cursor-vs-Claude split for the Gemma 4 track. Product owner: Ali.

Thesis: ChatGPT is question → answer. This is intent → work → result. Heavy inference is **Gemma 4 31B-IT on General Compute**. SerpAPI and Twilio are the real world. Python only **verifies** dollars.

```
speak → plan (Gemma) → shops (SerpAPI) → call (Twilio + Gradium + Gemma)
  → extract (Gemma) → fat decision (Gemma) → verify (Python) → result card
```

---

## 1. One-line

A voice-native action agent. You say an outcome. It looks up a real part price, phones shops, then Gemma recommends bring-your-own vs shop-supplied.

## 2. Demo sentence

> One mechanic quoted me $800 for front brakes on my 2019 Camry, I'm in Fremont. Find the part price online, call mechanics and dealers near me, get their all-in price and their hourly labor rate, and tell me whether I should bring my own part or let them supply it.

What the user should see:

1. Home → task screen.
2. Real nearby shops (Maps) + a web part price (Shopping).
3. Phone rings **+1 709 765 6030** from **(945) 277-2309** after discovery (demo: one number, not N shop phones).
4. They play the shop. Gemma asks one question at a time (all-in, shop part, labor rate, hours, customer parts, OEM, warranty, slot).
5. Transcript on the first phone card as lines land.
6. Why **streams** onto the result card, then locks: BYO vs shop-supplied, recommendation, tradeoffs. Dollars only from speech or SerpAPI.

Vertical is brakes only. `Task` stays generic.

## 3. Principles

- Speak one sentence, get a decision. No category picker.
- Hide work until they tap an agent. Transcript is one tap deeper.
- Failures stay visible (“no quote”), never dropped.
- Gemma decides hassle vs warranty vs price. Python recomputes every total. Invented shops or dollars are dropped.
- Book is a no-op.

## 4. UI (this builder)

Existing Vite app on `:5174`, live API `:7860`. Do not rebuild Home / Task chrome. Change only what the new events need.

| Surface | Behavior |
| --- | --- |
| Home | Mic or type → `POST /tasks`. Browser speech is fine (Gradium is for the PSTN call). |
| Task running | Progress card + agent list. First call card: live `transcript` via `agent.updated`. |
| Task complete | Result card: two options, recommended tag, streamed then final `why`, `tradeoffs`. |
| Agent expand | Call length, outcome, facts, transcript. Empty call → “no quote”, not a fake price. |

New event the UI must handle:

```ts
{ type: "result.partial", taskId: string, whyDelta: string }
```

`useTask` appends `whyDelta`. ResultCard shows a live why, then replaces with verified `task.result` on `task.result`.

## 5. Stack

| Piece | Role |
| --- | --- |
| FastAPI `:7860` | Tasks, Twilio webhooks, WS events |
| Vite `:5174` | Home + task |
| Supabase | `tasks`, `agents`, `transcript_lines` |
| SerpAPI | `google_maps` shops, `google_shopping` part |
| Nominatim | City → lat/lng if no GPS |
| Twilio | Outbound from `TWILIO_FROM`; media stream `wss` via Cloudflare tunnel |
| Gradium | PSTN STT + TTS only |
| General Compute | `gemma-4-31B-it` — planner, ShopLLM, extract, synthesize, tools |
| Python | Recompute option totals; drop illegal dollars |

Do not call SambaNova or Mentra. Do not invent Places. Demo dial: `TWILIO_DEMO_TO` or `+17097656030`.

## 6. Pipeline (target)

1. **Intake** — UI text or browser STT → `POST /tasks { request, lat?, lng?, location? }`.
2. **Planner** — Gemma JSON: title, userQuote, vehicle, city, factsNeeded, callScript, extractionSchema. Regex fallback for the Camry sentence.
3. **Discover / part (Session 9: Gemma tools)** — `discover_shops` → SerpAPI Maps. `lookup_part` → Shopping. Until S9, orchestrator calls those modules directly. Gemma must not invent a hit.
4. **Dial** — One demo leg. TwiML Connect Stream (no canned Say). Gradium 48 kHz → Twilio 8 kHz µ-law via serializer.
5. **Call brain** — ShopLLM, one question / ≤20 words. Tools: `note_fact`, `end_call`. Interruptions off (PSTN echo). VAD on so a second turn exists.
6. **Extract** — Gemma over the full transcript → `Facts`. No transcript → confidence 0.
7. **Synthesize** — One fat Gemma pass: all shop facts + capped transcripts + web parts + userQuote + preferences → options, `recommendedOptionIndex`, why, tradeoffs. Stream tokens as `result.partial`. Then Python verify (±$1, allowed agentIds, BYO only if `acceptsCustomerParts`).
8. **Deliver** — `task.result` + `complete`. GET `/tasks` strips transcripts.

## 7. Data model

Same as `frontend/src/types.ts`. Add:

```ts
{ type: "result.partial", taskId: string, whyDelta: string }
```

`Result.tradeoffs?: string[]` already exists.

## 8. API

Base `http://127.0.0.1:7860`

| Method | Path | Returns |
| --- | --- | --- |
| POST | `/tasks` | Task `planning` |
| GET | `/tasks` | Task[] no transcripts |
| GET | `/tasks/{id}` | full Task |
| WS | `/tasks/{id}/events` | `task.updated`, `agent.updated`, `result.partial`, `task.result`, `error` |
| POST | `/twilio/voice` | TwiML Stream |
| WS | `/twilio/media` | Pipecat + Gradium + ShopLLM |
| POST | `/twilio/status`, `/twilio/recording` | ack |
| POST | `/tasks/{id}/book` | no-op Task |
| GET | `/health` | env flags |

## 9. Already built (do not rebuild) — checked 2026-09-19 against `server/`

| Spec item | Status |
| --- | --- |
| Fat-pass **shape** + dollar verifier + preferences | **Done.** `_llm_decision` → `_verify_decision` → deterministic fallback. |
| Sheet: shops / webParts / userQuote / preferences | **Done.** |
| Sheet: per-agent **transcripts** | **Missing.** `_decision_input` has facts only. |
| Verifier retry + `complete()` usage logs | **Missing.** One shot; exception → None. |
| `json_schema` → `json_object` fallback | **Missing.** `llm.complete` is schema-only. |
| Extract after dial if `agent.transcript` | **Done** in `_run_calls_then_complete`. |
| Live `apply_transcript_line` + `agent.updated` | **Done** in `api.py` / `call_audio.py`. |
| ShopLLM `max_tokens` ~80, VAD for turn 2 | **Done.** No `note_fact` / `end_call`. |
| `complete_stream` / `result.partial` | **Missing.** ResultCard already renders `why` + `tradeoffs`. |
| `gc_loop.py` / Gemma-ordered SerpAPI | **Missing.** Orchestrator still calls discovery + web directly. |

**Still the right plan.** Narrow Session 6 to the four gaps (transcripts on the sheet, retry, usage, JSON fallback). Do not rebuild synthesize or the extract loop. S7–S9 unchanged.

**Gate unchanged:** if the call never persists lines, extract skips and the fat pass has no shop dollars.

## 10. Implementation — multi-session, multi-agent

One builder, **four sessions**, **four work-agents per session** (parallelizable; do not edit the same file in two agents). Session N starts when N−1 **Done when** is true.

```
S6 fat decision  →  S7 call tools  →  S8 stream why  →  S9 orch tools
```

### SESSION 6 — Fat decision (gaps only)

Verifier + fact sheet + extract-after-dial already exist. **Do not rewrite them.**

**Achieve:** Transcripts on the sheet. One verifier retry. Usage logs. JSON fallback. Tests for retry.

| Agent | Builds | Files |
| --- | --- | --- |
| 1 | Add capped `transcript` per shop to `_decision_input` | `server/synthesize.py`, `prompts/synthesize.md` |
| 2 | Retry + token/latency logs + `json_object` fallback | `server/llm.py`, `synthesize.py` |
| 3 | Confirm extract still sees memory+DB lines after dial (fix only if `_load_task` drops in-memory transcript) | `server/orchestrator.py` |
| 4 | Tests: retry after bad dollars; transcript in user JSON | `server/tests/test_synthesize.py` |

**Done when:** Fixture with $400 all-in + labor + web part → Gemma why cites those numbers; a hallucinated extra shop is dropped; retry path is tested.

### SESSION 7 — Call tools

**Achieve:** `note_fact` and `end_call`. Short turns. Extract-at-hangup still canonical.

| Agent | Builds | Files |
| --- | --- | --- |
| 1 | Tool JSON schemas (eight fact fields only) | small module or `call_audio.py` |
| 2 | ShopLLM tool loop (`role: tool`) | `server/call_audio.py` |
| 3 | `note_fact` only if value is in last business line; `end_call` hangs up | `server/call_audio.py` |
| 4 | Caller prompt uses tools | `server/prompts/caller.md` |

**Done when:** Last fact → `end_call`; invented `note_fact` dollars rejected.

### SESSION 8 — Stream why (server + UI)

**Achieve:** Synthesize streams. Card shows why live, then verified result.

| Agent | Builds | Files |
| --- | --- | --- |
| 1 | `complete_stream` | `server/llm.py` |
| 2 | Emit `result.partial` then `task.result` | `server/orchestrator.py`, `server/events.py` |
| 3 | Event on `TaskEvent` + API coerce | `server/models.py`, `server/api.py` |
| 4 | `useTask` + ResultCard + types | `frontend/src/hooks/useTask.ts`, `frontend/src/types.ts`, `frontend/src/components/ResultCard.tsx` |

**Done when:** Completing a task shows why appear, then a stable card that still passes the verifier.

### SESSION 9 — Orchestrator tools

**Achieve:** Gemma orders Maps and shopping. No invented shops or part prices. Dial stays one demo number.

| Agent | Builds | Files |
| --- | --- | --- |
| 1 | `discover_shops` / `lookup_part` wrappers | `server/discovery.py`, `server/web_agent.py` |
| 2 | Loop, max 8 steps | `server/gc_loop.py` |
| 3 | Drop unknown shops/prices; cap → old orchestrator | `server/orchestrator.py` |
| 4 | Mocked loop tests | `server/tests/test_gc_loop.py` |

**Done when:** Camry/Fremont agents match SerpAPI (or mocks); logs show `tool_calls` then synthesize.

## 11. File ownership (same session)

| Session | Agent 1 | Agent 2 | Agent 3 | Agent 4 |
| --- | --- | --- | --- | --- |
| 6 | `synthesize.py` | `llm.py` | orchestrator + extract | tests |
| 7 | schemas | `call_audio.py` loop | side effects | `caller.md` |
| 8 | `llm.py` stream | events | models + api | **frontend** |
| 9 | wrappers | `gc_loop.py` | orchestrator | tests |

## 12. Env (never commit, never paste into git)

`SERPAPI_API_KEY` · `TWILIO_*` · `TWILIO_WEBHOOK_BASE` · `TWILIO_DEMO_TO` · `SUPABASE_URL` · `SUPABASE_SERVICE_ROLE_KEY` · `GENERAL_COMPUTE_API_KEY` · `GENERAL_COMPUTE_MODEL=gemma-4-31B-it` · `GRADIUM_API_KEY` · `GRADIUM_VOICE_ID`

Tunnel must stay up or Twilio cannot fetch TwiML.

## 13. Out of scope

Mentra, booking, accounts, second vertical, Gemma 3n, DeepSeek (unless S6 latency is bad), real shop phones, fake price sheets, new Home/Task layouts.

## 14. Track done

Judge submits the Camry line, sees real shops and a web part, answers the demo call, Gemma asks / tools / hangs up, **why streams in**, result BYO vs shop-supplied uses only spoken or looked-up dollars, recommendation from Gemma 4, Python proves the arithmetic.

---

Related: [`docs/product-spec.md`](product-spec.md) (original split), [`docs/multi-agent-plan.md`](multi-agent-plan.md) (Sessions 1–5), [`docs/gemma4-gc-plan.md`](gemma4-gc-plan.md) (Cursor/Claude split — superseded by this file for build-all).
