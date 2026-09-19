# Voice AGI — product spec

Status: draft v1, 2026-09-19. Owner: Ali. Frontend: Claude Code. Backend: Cursor.

## 1. One-line

A voice-native action agent. You say an outcome. It researches websites and phones businesses in parallel, then comes back with a decision.

Thesis: ChatGPT is question → answer. This is intent → work → result.

## 2. First use case (hackathon demo)

User says:

> "One mechanic quoted me $800 for front brakes on my 2019 Camry, I'm in Fremont. Find the part price online, call mechanics and dealers near me, get their all-in price and their hourly labor rate, and tell me whether I should bring my own part or let them supply it."

The agent:

1. Looks up the part price on parts websites.
2. Calls N mechanics and dealers in parallel.
3. Asks each for all-in installed price, hourly labor rate, whether they accept customer parts, OEM or aftermarket, warranty, earliest slot.
4. Computes two options: bring your own part + labor, or shop supplies part.
5. Returns a result card with both numbers, a recommendation, and the reason.

Build only this vertical today. Model everything as a generic `Task` so the same code handles movers, dentists, plumbers later.

## 3. Product principles

- Speak one sentence, get a decision. No forms, no category picker.
- Hide the work. The user never sees a transcript unless they tap into one agent.
- Every task is the same object rendered three ways: progress card, result card, list row.
- Failures stay visible. A voicemail shows as "no quote", never silently dropped.
- The recommendation explains itself in two sentences and cites the tradeoff.

## 4. UI

### 4.1 Home
- Prompt "What do you need done?" and a mic button.
- Hold or tap to talk. Transcript appears as the user speaks. Release creates a Task.
- Below: list of recent tasks (see 4.4).

### 4.2 Task screen, running
Top card:
```
Brake repair · running
9 agents · 6 done · 2 calling · 1 waiting
Best so far   $486   vs your quote $800
```
Below: agent cards fill in as each finishes.

### 4.3 Task screen, complete
Top card, the result:
```
Your quote was $800. Two options.
[ Bring your own part ]   [ Shop supplies part ]
        $486                     $610
 $186 part + 2.5h × $120    all-in, OEM, 1-yr warranty
Bringing your own part saves $124, but Sam's won't warranty
customer parts. If you want the warranty, $610 is still $190 under.
[See all 9 quotes]  [Book Sam's]
```
Below: one thin card per agent. Icon (phone or globe), business name, kind, the one fact it returned. Tap to expand in place: call length, who answered, slot, full transcript. Tap again to collapse.

### 4.4 Task list
One row per task: icon, name, agent count, status (running or complete). Tap opens the task screen.

### 4.5 Depth order
Result card → analysis → all quotes table → agent cards → transcript. Each is one tap deeper. Nothing below the result card is shown by default.

## 5. Backend pipeline

```
speech → STT → planner → [web agents ∥ call agents] → extract → synthesize → result
```

1. **Intake.** Browser streams mic audio to the Pipecat websocket. Gradium STT transcribes. Semantic VAD marks end of utterance.
2. **Planner** (General Compute, one call). Turns the utterance into a `Task`: service, vehicle, location, user's quote, which facts to collect, how many businesses. Emits a call script and an extraction schema.
3. **Discovery.** Browser GPS or spoken city → geocode → **SerpAPI Google Maps** nearby (`brake repair` / `car repair`). Real names, phones, addresses. Cap N (5–8). (Places API is an equivalent later.)
4. **Web agents.** Fetch part prices from parts sites. Fast path, no phone. Jev optional here for typed page-to-fact extraction.
5. **Call agents.** One Pipecat pipeline per business, all in parallel. Pipecat runs the call. Gradium STT hears, Gradium TTS speaks. General Compute decides each turn. Deterministic rules (Jev or plain code) handle voicemail detection, "all fields filled, hang up", and refusal.
6. **Extraction.** Per finished call, one General Compute call over the transcript → strict JSON facts with confidence.
7. **Synthesis.** When all agents finish or the deadline hits, one General Compute call ranks, computes the two options, picks a recommendation, writes the two-sentence why.
8. **Delivery.** Supabase holds tasks, agents, facts, transcripts. UI subscribes over websocket. Every agent state change is an event.
9. **Execution.** "Book" is still a no-op this weekend (same shape, no second call).

Calls: **Twilio** from `TWILIO_FROM` to each Place `business.phone`. Gradium talks; we record/transcribe. The bot opens with a one-line disclosure (assistant calling for a customer). No price on an agent until that call (or voicemail) finishes. No shop-bot, no price sheet on a real business.

## 6. Who does what

| Company | Role | Where |
| --- | --- | --- |
| Pipecat (Daily) | Runs every real-time conversation, turn detection, interruption, transports | Intake + every call agent |
| Gradium | STT and TTS on every conversation | Intake + every call agent |
| General Compute | All reasoning: planner, per-turn call brain, extraction, synthesis | Everywhere an LLM is called |
| SambaNova | Chips under General Compute. Not called directly. | — |
| Jev (TypeSafe) | Optional typed, no-LLM decisions: page extraction, call-state routing | Web agents, call agent rules |
| MentraOS | Future transport: glasses mic in, earpiece out. Not in demo. | Transport only |

Inference math for one request: 1 planner + N calls × ~20 turns + N extractions + 1 synthesis. N=20 is roughly 450 model calls from one sentence.

## 7. Data model

```ts
type TaskStatus = "planning" | "running" | "complete" | "failed"
type AgentKind = "call" | "web"
type AgentStatus = "queued" | "active" | "done" | "failed"

interface Task {
  id: string
  title: string                 // "Brake repair"
  request: string               // raw transcript of what the user said
  createdAt: string             // ISO
  status: TaskStatus
  userQuote?: number            // 800
  location?: { lat: number; lng: number; label?: string }
  agents: Agent[]
  result?: Result               // present when status = complete
}

interface Agent {
  id: string
  taskId: string
  kind: AgentKind
  status: AgentStatus
  business: { name: string; type: "mechanic" | "dealer" | "parts"; phone?: string; url?: string }
  summary?: string              // one line for the card: "$610 all-in · $120/h"
  facts?: Facts
  call?: { durationS: number; answeredBy?: string; outcome: "quote" | "voicemail" | "refused" | "error" }
  transcript?: { role: "agent" | "business"; text: string; t: number }[]
}

interface Facts {
  allInPrice?: number
  laborRatePerHour?: number
  laborHours?: number
  acceptsCustomerParts?: boolean
  partsType?: "oem" | "aftermarket"
  warrantyMonths?: number
  earliestSlot?: string          // ISO
  partPrice?: number             // web agents
  confidence: number             // 0–1
}

interface Result {
  options: { label: string; total: number; breakdown: string; agentIds: string[] }[]
  recommendedOptionIndex: number
  recommendedAgentId: string
  why: string                    // two sentences
  savingsVsQuote?: number
}
```

## 8. API contract

Base: `http://localhost:7860`

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| POST | `/tasks` | `{ request: string, lat?: number, lng?: number, location?: string }` | `Task` (status planning) |
| GET | `/tasks` | — | `Task[]` without transcripts |
| GET | `/tasks/{id}` | — | full `Task` |
| GET | `/tasks/{id}/agents/{agentId}` | — | full `Agent` with transcript |
| POST | `/tasks/{id}/book` | `{ agentId }` | `Task` |
| WS | `/tasks/{id}/events` | — | stream of events below |
| WS | `/ws-client` | PCM audio | existing Pipecat intake, returns transcript text events |

Events on `/tasks/{id}/events`, one JSON object per message:

```ts
{ type: "task.updated",  task: Task }                       // status or counts changed
{ type: "agent.updated", agent: Agent }                     // one agent changed
{ type: "task.result",   taskId: string; result: Result }   // synthesis finished
{ type: "error",         message: string }
```

Frontend renders entirely from `Task` objects. Fixture at `fixtures/brake-task.json` is a complete, valid `Task` for building without the backend.

## 9. Known integration quirks

- General Compute rejects `stream_options`, `service_tier`, and the `developer` role. Strip them. See `GeneralComputeLLMService` in `general-compute-hackathon/server/bot.py`; move it to shared code.
- Env var naming differs: the bot reads `GENERAL_COMPUTE_API_KEY` or `GENERALCOMPUTE_API_KEY`. Keep both.
- Gradium's Pipecat plugin ships TTS and STT services; both are already wired in `bot.py`.
- Gradium languages: en, fr, de, es, pt only.
- Model for talk-time: `gemma-4-31B-it` (hackathon default) or `minimax-m2.7`. Use `deepseek-v3.2` only for synthesis if latency allows.

## 10. Hackathon scope

In (this **is** the product — §14):
- Location → Google Places Nearby → Twilio to those mechanics → transcripts in SQLite → cheapest real option.
- Home, task screen, planner, extract, synthesize, events websocket.
- Brake repair only. Cap 5–8 Places results, a few Twilio calls in parallel (not 20).

Out:
- Booking execution, MentraOS, user accounts, other verticals, fake price sheets on real shops.

See §13 for leftovers.

## 12. Split of work

- Frontend (Claude Code): React app, fixture-driven, sections 4, 7, 8.
- Backend (Cursor): sections 5, 7, 8, 9, 10 using `general-compute-hackathon/server` as the starting point.
- Parallel build: `docs/multi-agent-plan.md` (sessions + file locks + copy-paste prompts).

## 13. Still stubbed (not the core)

| Stub | Swap later |
| --- | --- |
| Book button | A second Twilio call that books the slot |
| MentraOS | Glasses mic / earpiece as transport |
| User accounts | Auth on top of the same SQLite rows |
| Other verticals | New planner prompt, same Task |
| Part price | Optional web fetch; quotes still come from the call |

Frontend fixture is only for UI work without the server. It is not the product.

### Do not stub (core)

- Where the user is (GPS or spoken city)
- Google Places Nearby
- Twilio to those Place phone numbers
- SQLite for tasks / agents / transcripts / result
- No invented prices on real shops
- Voicemail / no-answer stays visible

## 14. Core product (locked)

Simple loop:

```
you speak → we know where you are → Places finds nearby mechanics
  → Twilio calls them → we keep the conversation → cheapest real option
```

**Location.** `POST /tasks` with `lat`/`lng` from the browser, or a city string. Missing coords → geocode (Google Geocoding with the same key, or Nominatim). Saved on `Task.location`.

**Nearby.** Server-only `SERPAPI_API_KEY`, `engine=google_maps`. Query around `Task.location`, N≤8. Each hit with a phone becomes a `call` agent: name, phone, address, url. Skip no-phone. One SerpAPI search per task.

**Dial.** Twilio `TWILIO_FROM` → `agent.business.phone`. Parallel but capped (start at 3). Disclosure first sentence. Transcribe, extract, hang up when facts are in or they refuse. Dead air / voicemail = `outcome: voicemail`, no price.

**Database.** Supabase Postgres (connected project). New tables `tasks` / `agents` / `transcript_lines`. Do not use `guiders` / `requests`. No accounts yet.

**Never.** Price-sheet quotes on a Place result. Shop-bot pretending to be that shop.

**Env (local `.env`, gitignored; rotate any token pasted in chat)**  
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, `SERPAPI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, plus existing GC / Gradium keys.

**Smoke test:** one optional `TWILIO_DEMO_TO` (owner cell) to prove the wire before enabling Place dialing. Product path is Place numbers, not the owner playing a shop.
