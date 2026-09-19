# Voice AGI server

FastAPI on **:7860**. Cursor owns this directory. Frontend is out of scope.

Copy `.env.example` → `.env` and set values locally. Never commit `.env`. This file lists **names only**.

## Env names

| Name | Role |
| --- | --- |
| `SERPAPI_API_KEY` | SerpAPI `google_maps` nearby (Places). Also optional `google_shopping` part lookup. |
| `TWILIO_ACCOUNT_SID` | Twilio account |
| `TWILIO_AUTH_TOKEN` | Twilio auth |
| `TWILIO_FROM` | Outbound caller ID |
| `TWILIO_DEMO_TO` | Optional first ring (owner cell) before Place numbers |
| `TWILIO_WEBHOOK_BASE` | HTTPS origin Twilio can reach (callbacks). Fallback name: `PUBLIC_BASE_URL` |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only DB access |
| `GENERAL_COMPUTE_API_KEY` | General Compute LLM (preferred) |
| `GENERALCOMPUTE_API_KEY` | Same key, alternate name (bot.py quirk) |
| `GENERAL_COMPUTE_BASE_URL` | GC API base |
| `GENERAL_COMPUTE_MODEL` | GC model id |
| `GRADIUM_API_KEY` | Gradium STT / TTS |
| `GRADIUM_VOICE_ID` | Gradium voice |

No Google Places API key. Nearby is SerpAPI Maps.

## How to uvicorn

From the **repo root** (`Voice_agi`), so `server` is a package (`from .api` imports):

```bash
cd C:\AI\Projects\Voice_agi
pip install -r server/requirements.txt
# or: cd server && uv sync && cd ..

# load server/.env into the process (PowerShell example)
Get-Content server\.env | ForEach-Object {
  if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
  $k,$v = $_ -split '=',2
  Set-Item -Path "Env:$($k.Trim())" -Value $v.Trim()
}

python -m uvicorn server.app:app --host 0.0.0.0 --port 7860
```

Health: `http://localhost:7860/health`

Create a task:

```bash
curl -s -X POST http://localhost:7860/tasks ^
  -H "Content-Type: application/json" ^
  -d "{\"request\":\"One mechanic quoted me $800 for front brakes on my 2019 Camry, I'm in Fremont.\",\"location\":\"Fremont\"}"
```

List (no transcripts): `GET http://localhost:7860/tasks`  
One task: `GET http://localhost:7860/tasks/{id}`  
Events: `ws://localhost:7860/tasks/{id}/events`

CI tests must mock SerpAPI and Twilio. Do not burn Maps or calls in CI.

## Real vs stub

**Real (core product)**

- **Places via SerpAPI** — one `google_maps` search near GPS or geocoded city. Real names, phones, addresses. Cap 8. No invented shop prices.
- **Twilio** — `TWILIO_FROM` dials `agent.business.phone` (or `TWILIO_DEMO_TO` first to prove the line). Transcripts persist when Supabase is wired.
- **Supabase** — `tasks` / `agents` / `transcript_lines`. Not SQLite. Not `guiders` / `requests`.

**Stub (do not treat as done)**

- **Book** — `POST /tasks/{id}/book` returns the task unchanged. No second call to reserve a slot.
- **Mentra** — glasses mic / earpiece is out of this demo. Do not start Mentra transports here.
