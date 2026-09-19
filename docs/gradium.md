# Gradium — feature list

Source: [docs.gradium.ai](https://docs.gradium.ai/) (read 2026-09-19). Role in this repo: **ears and mouth** of the Pipecat voice agent (STT in, TTS out).

## Core products

- Text-to-Speech (TTS) - low-latency streaming speech synthesis over WebSocket, or one-shot REST.
- Speech-to-Text (STT) - real-time streaming transcription over WebSocket, or one-shot REST for files.
- Speech-to-Speech (S2S) - one WebSocket that transcribes, optionally translates, and re-synthesizes audio in real time.
- Voice library - 71 flagship voices across 5 languages (EN 22, FR 12, DE 12, ES 13, PT 12) with regional accents.
- Voice cloning (standard) - clone a voice from a 10-second sample; up to 1,000 voices on subscription plans.
- Voice cloning (pro) - hyper-realistic model from 30 min to 2 h of clean audio with emotional range.
- Voice design - generate 1 to 5 candidate voices from a text description (up to 500 chars), audition, then keep one.
- Pronunciation dictionaries - custom rewrite rules for names, brands, acronyms, applied before built-in rules.
- Text rewriting - auto-expands numbers, currency, emails, URLs, alphanumeric codes, and dates for correct pronunciation.
- Keyword boosting - bias STT toward up to 500 custom words with a boost weight (-6 to 6, default 3).

## Languages

- Supported languages - English, French, German, Spanish, Portuguese; more in development.
- Auto language detection - STT accepts `language: "any"` when the input language is unknown.
- Translation in STT - `target_language` returns the transcript in another supported language.
- Locale rewrite variants - Belgian and Swiss French rules for numbers, currency, and dates.

## TTS features

- Three SDK modes - `tts_realtime` (bidirectional streaming), `tts_stream` (iterable), `tts` (buffered).
- LLM streaming input - feed text chunks from an LLM as they arrive while keeping prosody intact.
- Output formats - PCM 48 kHz 16-bit mono (80 ms chunks), WAV, Ogg Opus, plus 8/16/22.05/24/44.1 kHz PCM.
- Telephony formats - mu-law and A-law at 8 kHz for phone audio.
- Word timestamps - each text segment returns start and stop times in seconds.
- Flush tag - `<flush>` forces audio out for text buffered so far.
- Break tag - `<break time="0.5s" />` inserts a pause from 0.1 to 2.0 s.
- Temperature - `temp` 0.0 to 1.4 (default 0.7) controls variation.
- Voice similarity - `cfg_coef` 1.0 to 4.0 (default 2.0) keeps output close to the target voice.
- Speech speed - `padding_bonus` -4.0 to 4.0 (default 0); negative is faster.
- Rewrite rules - `rewrite_rules` picks a language, a comma list of rule names, or `"none"`.
- Model choice - `model_name` defaults to `default`; `gradium-tts-beta` is available.
- Pronunciation per session - `pronunciation_id` applies a dictionary to a whole WebSocket session.

## STT features

- Two SDK modes - `stt_realtime` (push live audio) and `stt_stream` (pull from complete audio).
- Input formats - raw PCM 24 kHz default plus 8/16/22.05/44.1/48 kHz, WAV 16/24/32-bit, Ogg Opus, mu-law, A-law.
- Semantic VAD - every 80 ms a `step` message gives inactivity probability at 0.5, 1, 2, and 3 s horizons.
- End-of-turn detection - trigger when the 3 s horizon probability passes 0.5 (tunable threshold).
- Adaptive delay - `delay_in_frames` (7 to 48, default 10, 80 ms each) trades latency for accuracy.
- Flush - `send_flush()` finalizes pending audio and returns a `flushed` message for the next pipeline stage.
- Segment timestamps - `text` messages carry `start_s`, `end_text` messages carry `stop_s`.
- Emit timing bias - `padding_bonus` pushes text out sooner (negative) or later (positive).
- Temperature - `temp` 0.0 to 1.5 for decoding diversity.

## Speech-to-Speech features

- Endpoint - `wss://api.gradium.ai/api/speech/s2s`.
- Live translation - `s2s-translate` model with `target_language`; the voice must match that language.
- Configurable inner models - STT model, TTS model, and S2S model are set separately.
- Three SDK modes - `s2s_realtime`, `s2s_stream`, `s2s`.
- Formats - same input and output formats as STT and TTS; PCM in 24 kHz, PCM out 48 kHz.

## Voice management

- List voices - `GET /voices` returns every voice available to the org.
- Get, update, delete - per-voice endpoints by voice UID for custom voices.
- Create voice - upload WAV, MP3, or other audio with optional `start_s` trim and `timeout_s`.
- Voice design endpoints - generate candidates, poll readiness, convert to permanent voice, delete candidate.
- Voice design controls - `cfg_scale` 1 to 20 (default 5), `steps` 1 to 128 (default 16), optional `seed`.
- Candidate retention - design candidates expire after 30 days unless converted.
- Consent rule - explicit permission from the voice owner is required for cloning.

## Transport and connection

- Python SDK - `pip install gradium`, Python 3.10+, reads `GRADIUM_API_KEY`.
- REST base - `https://api.gradium.ai/api`; WebSocket base - `wss://api.gradium.ai/api`.
- Auth - `x-api-key` header; keys look like `gd_...`.
- WebSocket lifecycle - setup, ready, input, flush, end-of-stream messages with structured errors.
- Multiplexing - many logical requests on one socket using `client_req_id` and `close_ws_on_eos: false`.
- Reusable sockets - keep a connection open for sequential requests without reconnecting.
- Browser and mobile tokens - short-lived, single-use tokens from `/api-keys/token` so API keys never ship to clients.
- Browser microphone recipe - capture mic audio in the browser and stream it to STT.
- Telephony recipe - guidance for mu-law, A-law, and low-sample-rate PCM.
- Data residency - pin sessions to `eu.api.gradium.ai` or `us.api.gradium.ai`.
- Self-hosted TTS - dedicated Baseten deployment with the same WebSocket protocol and 346 baked-in voices.

## Limits and billing

- Session length - one TTS or STT session can run up to 3000 seconds.
- Free tier - 1500 characters per session.
- Concurrency - per-plan; contact support for exact numbers.
- TTS pricing - 1 credit per character; about 750 characters per minute of audio.
- STT pricing - 3 credits per second of audio.
- Credit balance - `GET /credits` or `gradium.usages.get()` reports balance and usage.
- Zero data retention - available on paid plans for TTS, STT, and S2S; not for design prompts or clone samples.

## Integrations

- Pipecat - `pipecat-ai[gradium]` provides `GradiumTTSService` (48 kHz streaming, runtime voice switch, word timestamps).
- LiveKit Agents - Gradium STT and TTS plugins.
- Vapi - Gradium speech models inside Vapi agents.
- OpenClaw - Gradium TTS for OpenClaw agents.
- Gradbot - Gradium's own reference voice-agent framework with demos on GitHub.
- Web search partners - Keenable, Linkup, and Tavily recipes for voice agents.
- Migration guides - drop-in swaps from ElevenLabs, Cartesia, Deepgram, and Fish Audio.

## Security and quality

- ISO 27001 - certified, audited by A-LIGN (September 2026).
- Voice design quality - 72.6% win rate vs ElevenLabs, Inworld, MiniMax, and Fish Audio in Gradium's test.
- Studio UI - web console with voice catalogue, filters, voice editing, cloning, and design preview.

## Notes for this repo

- Pipecat integration is TTS only; STT in Pipecat goes through Gradium's own WebSocket or the LiveKit-style plugin. Confirm before wiring.
- Use PCM 48 kHz out and 24 kHz in to match the SDK defaults and avoid resampling.
- Hackathon coupon `HACKATHON-202609` grants free credits (from the reference gist).
