# Voice AI & Voice Agents: An Illustrated Primer — Summary

Source: https://voiceaiandvoiceagents.com/ (by the Pipecat / Daily team, updated June 2026)

## What it is
- **Guide** - A practitioner's primer on building production voice agents, written around Pipecat, the most widely used voice AI framework (AWS, NVIDIA, Fortune 500, 100+ services).

## The basic loop
- **Pipeline** - Mic → encode → cloud → STT → LLM (with context) → TTS → audio back to user.
- **Cloud-centric** - Best models need cloud compute; phones/telephony have no local device to run code on.
- **Latency target** - 1,500 ms voice-to-voice is the goal; humans expect ~500 ms in natural conversation.

## Latency
- **Measure end-to-end** - Only waveform-based voice-to-voice timing is accurate; server inference time hides audio, network, and endpointing costs.
- **Typical budget (~1,300 ms)** - Transcription/endpointing ~300 ms, LLM TTFB ~650 ms, TTS TTFB ~120 ms, plus ~200 ms of audio/network overhead.
- **Best case** - Pipecat has shown ~500 ms when all models sit in the same GPU cluster tuned for latency over throughput.

## LLMs for voice
- **Most used models (2026)** - GPT-4.1, GPT-5.1, Gemini 2.5 Flash.
- **Requirements** - Low latency, strong instruction following, reliable function calling, low hallucination, stable tone, low cost.
- **TTFT rule** - 600 ms or less time-to-first-token is fast enough; watch P95 too, which is often 2-3x median.
- **Reasoning models** - Too slow for the voice loop because thinking tokens delay the first spoken token.
- **Cost** - A 10-min call costs roughly $0.006 (Gemini 2.5 Flash) to $0.069 (GPT-4.1) in LLM tokens; prompt caching cuts this further.
- **Open models** - Nemotron 3 Ultra is the first open model to saturate voice benchmarks under 700 ms TTFT; Kimi 2.6, Gemma 4, GLM 5 also competitive.
- **Speech-to-speech** - More natural and better at nuance, but slower, 3-5x pricier, weaker transcripts, and less flexible context control than cascaded pipelines.

## Benchmarks
- **Why standard benchmarks fail** - They ignore multi-turn behavior and never report TTFT.
- **Pipecat voice benchmark** - 30-turn conversation testing tool use, instruction following, KB grounding, and TTFT.
- **Two Pareto frontiers** - Open (Nemotron 3 Ultra, Kimi 2.6, Gemma 4) vs proprietary (GPT-4.1, Claude Haiku 4.5, Claude Sonnet 5.6).

## Speech-to-text
- **Leaders** - Deepgram, Soniox, Speechmatics, AssemblyAI, Cartesia, NVIDIA (open source).
- **Self-host when** - Privacy, geographic compliance, or to cut the 250-350 ms cross-region API round trip.
- **Prompt the LLM** - Tell it the input is transcribed speech so it silently corrects STT errors using conversation context.

## Text-to-speech
- **Selection criteria** - Voice naturalness, time-to-first-audio, cost, language coverage, word timestamps, pronunciation control.
- **Leaders** - Cartesia (~195 ms TTFA), Gradium, Deepgram Aura-2, ElevenLabs, Inworld.
- **Word-level timestamps** - Needed to know exactly what the user heard before an interruption.
- **Pronunciation hack** - Have the LLM emit sounds-like spellings ("in vidia", "gee pee you").
- **Non-English** - Quality varies widely; test extensively.

## Audio processing
- **Mics and AGC** - Hardware processing usually helps; Bluetooth can add hundreds of ms you cannot control.
- **Echo cancellation** - Must run on-device; built into WebRTC and telephony SDKs; Firefox is weak, prefer Chrome/Safari.
- **Encoding** - Use Opus (32 kbps speech, 96 kbps hi-fi); avoid raw PCM on networks and legacy 8 kHz G.711.
- **Music over WebRTC** - Disable echo cancel and noise suppression, raise Opus bitrate to 64-128 kbps.
- **Speaker isolation** - Krisp suppresses background speech and greatly improves transcription in noisy places.
- **VAD** - Silero VAD classifies speech vs non-speech and sits in nearly every pipeline.

## Network transport
- **WebRTC for production** - Purpose-built for realtime media with jitter buffers, FEC, bandwidth estimation, and stats.
- **WebSockets** - Fine for server-to-server or prototypes; TCP head-of-line blocking makes them bad for client media.
- **Serverless WebRTC** - Pipecat SmallWebRTCTransport connects client directly to agent with no media server.
- **HTTP** - Used for LLM calls, REST, and webhooks, not for streaming audio.
- **QUIC / MoQ** - The future path; Safari WebTransport support is still the blocker.
- **Edge routing** - Connect users to a nearby edge, then private backbone; saves 25+ ms and sharply lowers P95 jitter.

## Turn detection
- **The number one complaint** - From-scratch agents interrupt too often; frameworks like Pipecat largely solve this.
- **Pause-based VAD** - Pipecat defaults: stop 0.8 s, start 0.2 s, confidence 0.7, min volume 0.6.
- **Push-to-talk** - Unambiguous but unnatural and impossible on phones.
- **Endpoint markers** - "Over"-style keywords; rarely used.
- **Smart Turn** - Pipecat's open-source native-audio model (23 languages) classifies turn end in ~250 ms using intonation and pacing.
- **State of the art (3 layers)** - 200 ms VAD trigger + Smart Turn audio model + LLM single-token tagging (respond now / wait 5 s / wait 10 s).

## Interruption handling
- **Everything cancellable** - Every pipeline stage must stop instantly on user speech.
- **Spurious interrupts** - Tune VAD start length/confidence, smooth volume, and use speaker isolation for background voices.
- **Context accuracy** - Use TTS word timestamps so context contains only what the user actually heard (Pipecat does this automatically).

## Conversation context
- **LLMs are stateless** - Resend system prompt, messages, tools, and params every turn.
- **API differences** - OpenAI, Google, Anthropic formats differ; Pipecat normalizes to OpenAI format.
- **Edit between turns** - Shorten or summarize history to cut latency and cost and improve reliability.

## Function calling
- **Core of production agents** - Powers RAG, backend APIs, telephony actions (transfer, DTMF), and script state transitions.
- **Reliability is jagged** - Multi-turn voice stresses tool calling; build custom evals for your app.
- **Latency cost** - Each call means two inferences; ~1,450 ms before execution is common, so play a filler ("Please wait") or watchdog message.
- **Always pair request/response** - Insert a status response even if interrupted; Pipecat does this automatically.
- **Execution patterns** - Direct call, generic mapped call, client-side proxy, or HTTP endpoint proxy.
- **Async functions** - Return in_progress immediately and inject completion into context later.
- **Parallel/composite calls** - Powerful but variable; disable parallel calling unless needed.

## Multimodality
- **Token cost** - 1 min speech is ~150 text tokens vs ~2,000 audio tokens; 1 image ~250; 1 min video ~15,000.
- **Applications** - Image description, screen-aware assistants, voice-enabled coding (Claude Code, Codex).
- **Mitigation** - Summarize video to text, embed for RAG, trigger lookups via functions, and lean on context caching.

## Using multiple models
- **Cascade is normal** - STT + LLM + TTS plus helper models (VAD, turn, guardrails).
- **Prompt before fine-tuning** - Prompting with retrieved knowledge and examples matches fine-tuning in most cases; fine-tune later for speed/cost.
- **Async inference** - Offload slow tasks (guardrails, image gen, code) to background reasoning models via function calls.
- **Guardrails** - Run safety checks async with a separate small model so they never add latency.
- **Self-improving loop** - Feed production transcripts and eval results back into prompts and fine-tuning data.

## Scripting and instruction following
- **Hybrid control** - Combine system-prompt workflows with function-driven state machines (Pipecat Flows) for strict sequences.

## Voice AI evals
- **Not unit tests** - Must be multi-turn, non-deterministic, and measure latency, naturalness, tool reliability, and coherence together.
- **Failure modes** - Spurious interrupts, tool misfires, compounding transcription errors, turn-detection edge cases, P95 latency spikes.
- **Strategy** - 30-turn scenarios with tools, real accents and vocab, and multi-dimensional scoring refreshed for each new model.

## Telephony
- **Providers** - Twilio, Telnyx, Vonage, Plivo, Exotel via SIP/media streams.
- **Requirements** - DTMF, transfers, queuing, voicemail via function calls; cope with 8 kHz audio.

## RAG and memory
- **RAG via functions** - LLM triggers semantic search over embedded knowledge bases.
- **Memory** - Persist structured context across sessions; summarize long history; mind privacy.

## Hosting and scaling
- **Architecture** - Containerized agents, load-balanced per session, async queues for non-realtime work, latency/cost dashboards.
- **Per-minute cost** - Sum LLM (~$0.001-0.01/min), STT (~$0.003-0.008/min), TTS (~$0.009-0.05/min), plus infra; caching cuts LLM 50%+.

## What's coming (2026-2027)
- **Speech-to-speech matures** - But cascaded pipelines stay competitive on cost and control.
- **Open weights win on latency** - Nemotron, Gemma 4, GLM 5 match proprietary models for voice.
- **Better context caching** - Driven by multimodal and long-session agents.
- **Edge / on-device inference** - Grows as small models improve.
