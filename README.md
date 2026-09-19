# Voice AGI

A voice-native action agent. You say an outcome. It finds real nearby businesses, phones them in parallel, keeps the conversations, and comes back with a decision.

First vertical: brake repair. "One mechanic quoted me $800 for front brakes on my 2019 Camry, I'm in Fremont" becomes real calls to Fremont shops, a transcript per call, extracted facts, and a bring-your-own-part versus shop-supplies-part comparison.

Built at the General Compute hackathon, September 2026.

## Stack

| Layer | Provider |
| --- | --- |
| Reasoning: planner, per-turn call brain, extraction, synthesis | General Compute (`gemma-4-31B-it`, SambaNova silicon underneath) |
| Speech in and out on every call | Gradium STT and TTS |
| Real-time call pipeline, turn taking | Pipecat |
| Outbound phone calls | Twilio Media Streams |
| Nearby businesses | SerpAPI Google Maps |
| Storage | Supabase Postgres |
| Frontend | React, Vite, TypeScript |

## Layout

| Path | What |
| --- | --- |
| `frontend/` | The app: home with mic, task screen with result card and agent cards, task list. See `frontend/README.md`. |
| `server/` | FastAPI backend on port 7860: tasks API, events websocket, Twilio webhooks, orchestrator, dialer, call pipeline. See `server/README.md`. |
| `general-compute-hackathon/` | The original Pipecat playground bot. Its `.venv` is the interpreter the server runs with. |
| `mentra-live-entry/` | MentraOS glasses miniapp, mic and speaker only. Not part of the demo. |
| `docs/` | Product spec, lessons learned, provider notes. |
| `fixtures/` | A complete fake task for building the UI without the backend. |

## Run

Backend, from the repo root, using the hackathon venv because it has Pipecat:

```bash
cp server/.env.example server/.env   # fill in keys
general-compute-hackathon/server/.venv/Scripts/python.exe -m uvicorn server.app:app --host 127.0.0.1 --port 7860
```

Expose it for Twilio and put the URL in `TWILIO_WEBHOOK_BASE`:

```bash
cloudflared tunnel --url http://127.0.0.1:7860
```

Frontend:

```bash
cd frontend && npm install && npm run dev
```

Set `VITE_API_MODE=live` in `frontend/.env.local` to talk to the backend, or leave it on `mock` for a fixture-driven demo.

## Read first

- [docs/product-spec.md](docs/product-spec.md) for what the product is and the API contract.
- [docs/lessons-learned.md](docs/lessons-learned.md) before changing anything in `server/`. It lists every way the call broke and how it was fixed.
