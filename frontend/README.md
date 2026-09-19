# Voice AGI — frontend

React + Vite + TypeScript. No UI framework, one CSS file, light and dark.

## Run

```bash
cd frontend
npm install
npm run dev
```

Opens on http://localhost:5173. Default is **demo mode**: a fixture-driven fake backend that replays a brake-repair task as a live run (agents start in waves, finish at different times, then the result card lands). Click "brake quote in Fremont" under the composer, hit send, and watch.

## Talk to the real backend

```bash
cp .env.example .env
# set VITE_API_MODE=live and VITE_API_BASE to the Pipecat server
```

Live mode uses the endpoints in `docs/product-spec.md` section 8. Nothing else changes.

## Where things are

| Path | What |
| --- | --- |
| `src/types.ts` | `Task`, `Agent`, `Facts`, `Result` — mirrors the spec's data model |
| `src/fixtures/brakeTask.ts` | The demo task. Same data as `../fixtures/brake-task.json` |
| `src/api/mock.ts` | Fake backend with the simulated timeline |
| `src/api/live.ts` | Real backend client (fetch + WebSocket events) |
| `src/components/Home.tsx` | Mic, composer, task list |
| `src/components/TaskScreen.tsx` | Progress card or result card, then agent cards |
| `src/components/AgentCard.tsx` | One agent, expands to facts + transcript |
| `src/hooks/useSpeech.ts` | Browser speech recognition for the demo mic |

## Mic

Demo mode uses the browser's built-in speech recognition (Chrome). When the backend is up, swap `useSpeech` for a PCM stream to the Pipecat `/ws-client` socket so Gradium does the STT.
