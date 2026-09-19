# General Compute — features and API

Source: [docs.generalcompute.com](https://docs.generalcompute.com/) (read 2026-09-19).  
This is the **brain** in the Pipecat voice stack: OpenAI-compatible chat inference, optimized for low latency.

Support: [support@generalcompute.com](mailto:support@generalcompute.com)  
GitHub: [github.com/generalcompute](https://github.com/generalcompute)

---

## What it is

General Compute is an **OpenAI-compatible inference API**. You send chat messages; it streams tokens back. You do not pick SambaCloud vs SambaStack SKUs — GC hides that and runs models (MiniMax, DeepSeek, and others) on their fast runtime (SambaNova silicon underneath).

Pitch from the docs:

- Fastest inference + simple developer experience
- Drop-in replacement for the OpenAI SDK
- Same endpoints, parameters, and streaming semantics
- Tool calling, JSON mode, and reasoning-ready models
- Migrate by swapping base URL + API key (and model IDs)

Production region today: **`us-west-2`**. Extra regions are Enterprise-only.

---

## Feature list

### 1. OpenAI-compatible surface

Works with:

- Official SDKs: `@generalcompute/sdk` (Node) and `generalcompute` (Python)
- Stock `openai` SDK pointed at GC
- Vercel AI SDK (`@ai-sdk/openai-compatible`)
- LangChain, LlamaIndex, LiteLLM, OpenClaw, curl

Keep `client.chat.completions.create()`, streaming, tools, JSON mode, and the usual parameters.

### 2. Chat completions

`POST https://api.generalcompute.com/v1/chat/completions`

Required: `model`, `messages` (at least one).

Message roles: `system`, `user`, `assistant`, `function`, `tool`.

Documented request fields:

| Field | Notes |
| --- | --- |
| `temperature` | 0–2 |
| `top_p` | 0–1 |
| `n` | multiple completions |
| `stream` | default `false`; `true` → SSE `text/event-stream` |
| `stop` | string or array of strings |
| `max_tokens` | positive int |
| `max_completion_tokens` | used in reasoning examples |
| `presence_penalty` / `frequency_penalty` | −2 to 2 |
| `logit_bias` | token → number |
| `user` | end-user id |
| `top_k` | extra vs classic OpenAI |
| `repetition_penalty` | extra vs classic OpenAI |
| `tools` | OpenAI-style function tools |
| `response_format` | e.g. `{ "type": "json_object" }` |

Response object: `chat.completion` with `id`, `created`, `model`, `choices[]` (`message`, `finish_reason`, optional `logprobs`), `usage` (`prompt_tokens`, `completion_tokens`, `total_tokens`), optional `system_fingerprint`.

Streaming chunks: `chat.completion.chunk` with `choices[].delta`.

HTTP errors documented on this endpoint: **400** (bad request), **401** (missing/invalid key). Rate limits return **429**.

### 3. Streaming

`"stream": true` returns `text/event-stream` in the OpenAI chunk format. Tool-call deltas stream the same way, so agent frameworks work without custom parsers.

### 4. Tool / function calling

Define tools exactly like OpenAI (`type: "function"` + JSON Schema). Inspect `tool_calls` on the assistant message (or stream deltas), run the function, send `role: "tool"` results back.

Works together with reasoning models for agent loops.

### 5. JSON mode

`response_format: { type: "json_object" }` so the model returns structured JSON your code can parse.

### 6. Reasoning models

`deepseek-v3.2` and `deepseek-v3.1` are deployed with **higher thinking timeouts** and built-in chain-of-thought. Docs recommend lower temperature (example: `0.1`) and enough `max_completion_tokens` (example: `2048`).

Use them for math, code, and analysis — not for the cheapest/fastest voice turn.

### 7. Model catalog (docs pricing page)

From [Models & Pricing](https://docs.generalcompute.com/models). All prices USD per 1M tokens. Pay-as-you-go has no minimum.

| Model | ID | Context (docs) | Input | Output | Capability |
| --- | --- | --- | --- | --- | --- |
| MiniMax M2.7 | `minimax-m2.7` | 192k | $0.28 | $1.20 | General purpose (recommended) |
| DeepSeek V3.2 | `deepseek-v3.2` | 32k | $0.25 | $0.38 | Reasoning |
| DeepSeek V3.1 | `deepseek-v3.1` | 128k | $0.21 | $0.79 | Reasoning, longer context |
| GPT-OSS 120B | `gpt-oss-120b` | 128k | $0.21 | $0.79 | — |

**Pick a model**

- Everyday chat / voice replies: `minimax-m2.7`
- Hard reasoning: `deepseek-v3.2`
- Long-context reasoning: `deepseek-v3.1`
- Huge context cheaply: `minimax-m2.7`

### 8. Live public catalog (extra model)

`GET /v1/public/models` (no auth) on 2026-09-19 also listed:

| ID | Display name | Max completion tokens |
| --- | --- | --- |
| `minimax-m2.7` | MiniMax M2.7 | 192000 |
| `deepseek-v3.2` | DeepSeek V3.2 | 32000 |
| `deepseek-v3.1` | DeepSeek V3.1 | 128000 |
| `gpt-oss-120b` | GPT-OSS 120B | 128000 |
| `gemma-4-31B-it` | Gemma 4 31B-IT | 128000 |

`gemma-4-31B-it` is **live but not on the pricing page**. Public schema in the OpenAPI docs also has `supportsReasoning` and `supportsVision`; the live payload used `id` / `name` / `top_provider.max_completion_tokens`.

OpenClaw docs quote different MiniMax context (160k) and different prices. Prefer the **Models & Pricing** page + live `/v1/public/models` when they disagree.

### 9. Custom checkpoints (Enterprise / onboarded)

Bring your own **LoRA, GGUF, or full finetune**:

1. Share artifact (S3, Hugging Face, or upload)
2. GC containerizes it in `us-west-2` and gives a private ID (`acme/my-custom-model`)
3. That ID works like any public `model` — streaming, tools, functions
4. Enterprise: SLAs, dedicated pools, per-org allow lists, extra regions

Contact `support@generalcompute.com`.

### 10. Org model list vs public catalog

| Endpoint | Auth | What you get |
| --- | --- | --- |
| `POST /v1/models/list` | Bearer | Models **enabled for your org**, including private checkpoints (`id`, `created`, `owned_by`) |
| `GET /v1/public/models` | None | Marketing/docs catalog |

Docs say use `POST /v1/models/list` instead of OpenAI’s `GET /v1/models`.

### 11. Auth and keys

- Header: `Authorization: Bearer <key>`
- Env var SDKs read: `GENERALCOMPUTE_API_KEY`
- Keys look like `gc_…` (docs also show `gc_live_…`)
- Create in dashboard → Developers → API keys; copy once
- Rotate/scope: one key per service/env; delete unused keys

**Base URLs**

| Env | URL | Region |
| --- | --- | --- |
| Production | `https://api.generalcompute.com` | us-west-2 |
| OpenAI client base | `https://api.generalcompute.com/v1` | same |
| Local router | `http://localhost:3000` | local |

### 12. Plans and rate limits

Every paid/free plan can use **every public model**.

| Plan | Price | RPM | TPM | RPD | TPD |
| --- | --- | --- | --- | --- | --- |
| Pay As You Go | $0 + card auto-reload | 100 | 200k | 50k | 10M |
| Developer | $50/mo | 500 | 1M | 250k | 100M |
| Scale | $1,000/mo | 2,000 | 5M | 1M | 500M |
| Enterprise | Custom | Custom + dedicated infra + SLA | | | |

Response headers:

- `x-ratelimit-limit-requests`
- `x-ratelimit-remaining-requests`
- `x-ratelimit-reset-requests`

Over limit → **429**. Official SDKs retry with exponential backoff (default `maxRetries` / `max_retries` = 2).

Pay-as-you-go reliability is **best-effort**. Enterprise adds contractual SLA, escalation, dedicated capacity.

### 13. Agent signup (for coding agents)

Public, no auth. New accounts get **$5 free credit**.

1. `POST /v1/public/agent-signups` `{ "email": "..." }` → `signupId`, 15-minute expiry
2. User gets 6-digit code from `noreply@generalcompute.com` (max 5 tries)
3. `POST /v1/public/agent-signups/{signupId}/verify` `{ "code": "......" }` → `apiKey` **once**, plus `organizationId`, `userId`

Errors: signup already active, invalid code, expired code.

### 14. First-party SDKs

```bash
npm install @generalcompute/sdk
pip install generalcompute
```

```ts
import GeneralCompute from "@generalcompute/sdk";
const client = new GeneralCompute(); // reads GENERALCOMPUTE_API_KEY
```

```python
from generalcompute import GeneralCompute
client = GeneralCompute()  # reads GENERALCOMPUTE_API_KEY
```

Or keep `openai` / `OpenAI()` and set `baseURL` / `base_url` to `https://api.generalcompute.com/v1`.

### 15. Integrations documented

- **Vercel AI SDK / Next.js** — `createOpenAICompatible` + `streamText` + `toUIMessageStreamResponse()`; example repo `github.com/generalcompute/docs` → `examples/vercel-ai-chat`
- **Node streaming CLI** — `@generalcompute/sdk` + `stream: true`; example `examples/node-streaming`
- **OpenClaw** — provider `api: "openai-completions"`, `baseUrl` GC `/v1`, literal `apiKey` in `openclaw.json` (gateway does **not** see shell env vars). Default `generalcompute/minimax-m2.7`, fallbacks DeepSeek. Claim: ~5x faster inference.
- **Migrate-with-AI prompts** for OpenAI SDK, Anthropic SDK, Vercel AI SDK, and any OpenAI-compatible client (LangChain, LiteLLM, curl)
- **opencode** — mentioned as a follow-on auth/config path

### 16. Observability / routing (API intro)

The API “mirrors OpenAI while adding **routing, observability, and ultra-low-latency infrastructure**.” Docs do not expose a separate traces UI in this index; you still get standard `usage` on completions.

### 17. What it is *not*

- Not STT or TTS (that is Gradium / Hume)
- Not a voice transport (that is Pipecat / Daily / WebRTC)
- Not a typed decision model (that is TypeSafe Jev)
- Not a chip-kernel company (that is Infinity). GC is the **HTTP inference product**.

---

## Voice-agent notes (this repo)

In Pipecat: **General Compute = Brain**.

```
mic → Gradium STT → General Compute (minimax-m2.7) → Gradium TTS → speakers
```

Use streaming completions so TTS can start before the full reply exists. Prefer `minimax-m2.7` for talk-time; route hard questions to `deepseek-v3.2` if latency allows.

Env already used here: `GENERALCOMPUTE_API_KEY` in `.env.local`.

Verified 2026-09-19: `POST /v1/chat/completions` with `minimax-m2.7` returned HTTP 200 (`MiniMax-M2.7`).

---

## Minimal curl

```bash
curl https://api.generalcompute.com/v1/chat/completions \
  -H "Authorization: Bearer $GENERALCOMPUTE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "minimax-m2.7",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

Stream: add `"stream": true`.

List org models:

```bash
curl https://api.generalcompute.com/v1/models/list \
  -H "Authorization: Bearer $GENERALCOMPUTE_API_KEY"
```

---

## Doc index (canonical)

- [Introduction](https://docs.generalcompute.com/)
- [Quickstart](https://docs.generalcompute.com/quickstart)
- [Capabilities](https://docs.generalcompute.com/features)
- [Models & Pricing](https://docs.generalcompute.com/models)
- [Rate Limits](https://docs.generalcompute.com/rate-limits)
- [API Keys & Base URLs](https://docs.generalcompute.com/api-keys)
- [API reference](https://docs.generalcompute.com/api-reference/introduction)
- [Chat completions](https://docs.generalcompute.com/api-reference/endpoints/chat-completions)
- [Models list](https://docs.generalcompute.com/api-reference/endpoints/models-list)
- [Public models](https://docs.generalcompute.com/api-reference/endpoints/public-models)
- [Agent signup](https://docs.generalcompute.com/agent-signup)
- [Migrate with AI](https://docs.generalcompute.com/migrate-with-ai)
- [OpenClaw](https://docs.generalcompute.com/openclaw)
- [OpenAPI](https://docs.generalcompute.com/api-reference/openapi.json)
