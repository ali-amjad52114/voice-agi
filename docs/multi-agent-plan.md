# Voice AGI — Cursor backend plan (revised)

Core (`docs/product-spec.md` §14):

```
speak → location → SerpAPI Maps (nearby mechanics + phones)
  → Twilio calls them → transcript in Supabase → cheapest real option
```

Cursor owns `server/` only. Never `frontend/**`. No fake prices on real shops. No shop-bot. Book stays a no-op. Mentra out.

**Stack:** FastAPI `:7860` · Supabase (connected project) · SerpAPI `google_maps` · Twilio · Gradium + General Compute (from `bot.py` quirks)

**Env (local `.env`, never commit, never chat):**  
`SERPAPI_API_KEY` · `TWILIO_ACCOUNT_SID` · `TWILIO_AUTH_TOKEN` · `TWILIO_FROM` · `SUPABASE_URL` · `SUPABASE_SERVICE_ROLE_KEY` · `GENERAL_COMPUTE_API_KEY` · Gradium keys  
Optional: `TWILIO_DEMO_TO` for the first ring (owner cell).

---

## SESSION 1 — The API and the database

**Achieve:** A task can be created, stored in Supabase, and streamed over WS. Nothing is called yet.

| Agent | Builds |
| --- | --- |
| 1 | `server/` FastAPI: `POST/GET /tasks`, get one agent, no-op `book`, `WS /tasks/{id}/events`. Pydantic matches `frontend/src/types.ts` plus `Task.location`. |
| 2 | Supabase tables via MCP: `tasks`, `agents`, `transcript_lines` (and `result` JSON on `tasks`). RLS: service role only for now. Do not touch `guiders` / `requests`. |
| 3 | `server/db.py` — save/load Task through Supabase. Restart still has the row. |
| 4 | `server/llm.py` — GC client copied from `bot.py` quirks. No planner prompts yet. |

**Done when:** `POST /tasks` returns `planning`; row exists in Supabase; a WS client can get a test `task.updated`.

---

## SESSION 2 — Find real shops (no dollars)

**Achieve:** A sentence + lat/lng (or a city) becomes N real mechanics with **phones**. Agents are `queued`. **No prices.**

| Agent | Builds |
| --- | --- |
| 1 | Planner: utterance → title, userQuote, vehicle, city, call script, extraction schema. Offline fallback for the Camry sentence. |
| 2 | Location: use `lat`/`lng` from POST, else geocode the spoken city (SerpAPI or Nominatim). Write `Task.location`. |
| 3 | Discovery: **one** SerpAPI `google_maps` search (`brake repair` / `car repair` near that point). Cap 8. Skip no-phone. Each hit → `call` agent (name, phone, address, url). |
| 4 | Orchestrator v1: plan → discover → insert agents → `running` + events. **Stop. Do not dial. Do not invent quotes.** |

**Done when:** `POST` Camry + Fremont (or GPS) → 5–8 agents with real names/phones in Supabase, every `allInPrice` empty.

**SerpAPI:** 1 search per task. Free plan had ~193 left — do not retry-loop.

---

## SESSION 3 — One real phone call

**Achieve:** Twilio rings **one** number. Gradium talks. We keep the transcript. Extract facts. That agent may get a price; others stay empty.

| Agent | Builds |
| --- | --- |
| 1 | Caller prompt + disclosure first line. Hang-up rules (voicemail, all fields filled, refuse). |
| 2 | Twilio outbound from `TWILIO_FROM`. Media/stream or `<Gather>`/Pipecat-on-Twilio — pick the shortest path that already fits `bot.py`. Record or live-transcribe. |
| 3 | Wire **one** agent: if `TWILIO_DEMO_TO` set, call that first (prove the line). Then call `agent.business.phone` for the first Place result. |
| 4 | Extract: transcript → `Facts` + `summary` + `call.outcome`. Persist lines in Supabase. Event `agent.updated`. |

**Done when:** your phone (or one shop) rings from `+1945…`, conversation is in the DB, that agent has facts or `voicemail` — **not** a made-up number.

Trial Twilio only reaches verified numbers until the account is upgraded.

---

## SESSION 4 — Many calls, then a decision

**Achieve:** Up to 3 Twilio calls in parallel (8 queued). Deadline. Synthesize two options from **only agents that have real facts**. Persist `result`.

| Agent | Builds |
| --- | --- |
| 1 | Parallel dialer, cap 3, deadline, voicemail stays on the list. |
| 2 | Synthesize: BYO vs shop-supplied from extracted facts + optional web part price. Two-sentence why. `savingsVsQuote` if `userQuote` exists. If only one real quote, say so — do not pad with fake shops. |
| 3 | **Required** `web` agent: part price (one SerpAPI or parts-site fetch). Never overwrite a call quote. |
| 4 | Events: `task.result` + `status=complete`. List endpoint omits transcripts. |

**Done when:** one task in Supabase has a `result` whose dollars came from calls (or a visible “only 1 shop answered”).

---

## SESSION 5 — Runnable and proven

**Achieve:** Someone else can run it; a test proves the contract without burning SerpAPI/Twilio in CI.

| Agent | Builds |
| --- | --- |
| 1 | `server/.env.example` + root README: env vars, `uvicorn`, curl to create a task. What is real vs still stubbed (book, Mentra). |
| 2 | Tests: planner fallback, hang-up rules, synthesize on fixture facts. Discovery/Twilio mocked. |
| 3 | Integrator: smoke SerpAPI once, Twilio to `TWILIO_DEMO_TO` once, then one Place number if trial allows. File gaps in `docs/demo-gaps.md`. |

**Done when:** README path works; CI does not call Maps/Twilio.

---

## Parallel map

```
S1 (API + Supabase + llm)  →  S2 (plan + SerpAPI, no $)
  →  S3 (one Twilio call)  →  S4 (parallel + synthesize)  →  S5 (runbook)
```

S2 is already useful (real nearby list). S3 is the product. S4 is the decision card.

## Do not build

- `shops.json` price sheets  
- Shop-bot personas  
- Google Places key (SerpAPI Maps replaces it)  
- SQLite (Supabase is the DB)  
- Booking, accounts, Mentra, a second vertical  
- Frontend screens (Claude Code)

## Claude Code (not these sessions)

Home + task UI, `lat`/`lng` on `POST /tasks`, `VITE_API_MODE=live` → `:7860`.
