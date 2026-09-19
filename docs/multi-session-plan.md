# Multi-session plan — the decision engine

Goal: a mechanic call yields three numbers per shop (all-in price, labor rate and hours, the shop's own part price), we find the part online ourselves, and General Compute reasons over all of it to decide bring-your-own versus shop-supplied, including the hassle tradeoff.

Repo: https://github.com/ali-amjad52114/voice-agi · branch off `main` · one PR per session.
Monitor: the Claude Code session that wrote this. It does not write feature code. It reviews PRs, merges in order, runs the tests, places the one live call at the end.

Read `docs/lessons-learned.md` first. Every session. No exceptions.

## Hard rules for every agent

1. **Never save a `.py` file while a live call is running.** The dev server reloads on save and kills the call. Ask the monitor before any live call. Unit tests only, until integration.
2. **Never invent a price.** No defaults, no "typical" rates, no price sheets. If a number was not said or found, leave it null.
3. **Do not touch `server/call_audio.py`** except Session 1's one string. The pipeline section is the thing that works.
4. **Only edit the files your session owns** (table below). If you need a change elsewhere, write it in your PR description and the monitor will route it.
5. **Tests must not call the network.** Mock SerpAPI, Twilio, Supabase, General Compute. `python -m pytest -q server/tests` must pass before you open the PR.
6. **Contract is frozen.** `server/models.py`, `frontend/src/types.ts`, and spec §7 are owned by the monitor. `Facts.partPrice` on a call agent now means the shop's own part price. `Result.tradeoffs` is an optional list of short strings. Do not add fields.
7. Branch name is your session id. Commit messages say what and why. Open the PR against `main` and put the acceptance checklist from your session in the description, ticked.

## File ownership

| Session | Owns | Must not touch |
| --- | --- | --- |
| S1 Call questions | `server/prompts/caller.md`, `server/call_rules.py`, `server/tests/test_call_rules.py`, the kickoff string in `server/call_audio.py` (`_kickoff` message only) | rest of `call_audio.py`, `orchestrator.py` |
| S2 Extraction | `server/extract.py`, `server/prompts/extract.md`, `server/tests/test_extract.py` (new), `orchestrator.py` **only inside `_run_calls_then_complete`, the extraction loop** | `synthesize.py`, `web_agent.py` |
| S3 Part lookup | `server/web_agent.py`, `server/tests/test_web_agent.py` (new), `orchestrator.py` **only `_spawn_web_agent` and `_replace_web_agent`** | `extract.py`, `synthesize.py` |
| S4 Decision | `server/synthesize.py`, `server/prompts/synthesize.md`, `server/tests/test_synthesize.py`, `server/llm.py` (may add a helper, must keep `complete()` signature) | `orchestrator.py`, `extract.py` |
| S6 Summaries | `server/summary.py` (new), `server/tests/test_summary.py` (new), `orchestrator.py` **only `_apply_dial_records` and the two lines after extraction that set `agent.summary`** | everything else |

Merge order: S1 → S2 → S6 → S3 → S4. S1 to S4 and S6 can all be worked in parallel; the order is only for merging. S2, S3, S6 each touch different functions in `orchestrator.py`; rebase on `main` before opening the PR.

## Shared fixture: the real call

Use this transcript in tests. It is the first call that worked (Fremont Auto Tech, 81 s).

```json
[
  {"role":"agent","t":3,"text":"I'm an assistant calling for a customer with a 2019 Camry for front pads and rotors. What is the all-in installed price?"},
  {"role":"business","t":15,"text":"Well, $450."},
  {"role":"agent","t":16,"text":"Got it. What is your hourly labor rate?"},
  {"role":"business","t":28,"text":"$150. $150."},
  {"role":"agent","t":29,"text":"Okay, thanks. Will you install parts that the customer brings in?"},
  {"role":"business","t":39,"text":"Yeah, I can do for $150."},
  {"role":"agent","t":40,"text":"Got it. Do you use OEM or aftermarket parts?"},
  {"role":"business","t":48,"text":"We can use either depending on"},
  {"role":"agent","t":50,"text":"Okay. How long is the warranty on the work?"},
  {"role":"business","t":56,"text":"one month."},
  {"role":"agent","t":57,"text":"Got it. When is the earliest appointment you have available?"},
  {"role":"business","t":64,"text":"Tomorrow."},
  {"role":"agent","t":66,"text":"Thanks for your help, goodbye."}
]
```

What extraction produced today, and what it should produce:

| Field | Today | Should be |
| --- | --- | --- |
| allInPrice | 450 | 450 |
| laborRatePerHour | 150 | 150 |
| acceptsCustomerParts | null | true ("Yeah, I can do") |
| laborHours | null | null (not asked yet; S1 adds the question) |
| partsType | null | null (he said either) |
| warrantyMonths | 1 | 1 |
| earliestSlot | "Tomorrow" | "Tomorrow" |

---

## Session 1 — Three numbers per shop

**Achieve:** the caller asks for the shop's part price and the labor hours, so every call yields all-in, part, rate, hours.

| Agent | Task | Files |
| --- | --- | --- |
| 1A Prompt | Rewrite the question list in `caller.md` to eight questions in this order: all-in installed price; their price for the part alone if they supply it; hourly labor rate; roughly how many hours; will they fit a customer part; OEM or aftermarket; warranty; earliest slot. Keep one question per turn, under 20 words, brief acknowledgement. Add: if they give all-in and part price, the agent may say "so labor is about X, is that right?" only as a confirmation, never as a new number. | `server/prompts/caller.md` |
| 1B Kickoff | Update the `_kickoff` user message in `call_audio.py` so the first question is still the all-in price and the wording matches 1A. Change nothing else in that file. | `server/call_audio.py` (one string) |
| 1C Rules | `REQUIRED_CALL_FIELDS` in `call_rules.py` gains `laborHours` and `partPrice`. `all_fields_filled` treats `partPrice` as optional when `acceptsCustomerParts` is false (they won't sell you the part alone). Update tests. | `server/call_rules.py`, `server/tests/test_call_rules.py` |

**Acceptance**
- [ ] `caller.md` lists eight questions in the stated order, each under 20 words.
- [ ] `should_hang_up` returns True only when all required fields are present, with the `partPrice` exception.
- [ ] `python -m pytest -q server/tests` passes.
- [ ] No other line in `call_audio.py` changed (`git diff --stat` shows the one hunk).

**Copy-paste prompt for the session**
> You are Session 1 of `docs/multi-session-plan.md` in this repo. Read `docs/lessons-learned.md` and the plan. Branch `s1-call-questions`. Do agents 1A, 1B, 1C. Follow the hard rules and file ownership exactly. Open a PR to main with the acceptance checklist ticked.

---

## Session 2 — Extraction that understands speech

**Achieve:** the extractor infers what a person meant, not just literal numbers, and uses the planner's schema.

| Agent | Task | Files |
| --- | --- | --- |
| 2A Prompt | Rewrite `extract.md` with inference rules and examples: "yeah I can do that" after the customer-parts question → `acceptsCustomerParts: true`; "no we only use our parts" → false; "about two and a half", "couple hours" → `laborHours`; "six ten out the door" → 610; "one twenty an hour" → 120; "a year" → `warrantyMonths: 12`; "we can use either" → `partsType: null`. Prices only when spoken. `confidence` reflects how many required fields were clearly stated. | `server/prompts/extract.md` |
| 2B Schema pass-through | The planner emits `extractionSchema`. Store it on the task in memory during the run (no DB change) and pass it to `extract_facts(transcript, schema=...)`. `extract.py` already has `_merge_schema`; wire it. Keep the offline fallback (confidence 0, no prices). | `server/extract.py`, `orchestrator.py` (extraction loop only) |
| 2C Tests | New `test_extract.py`. Mock `llm.complete` to return canned JSON and assert parsing, null handling, price rejection when the model returns a number the transcript never contained (add a guard: every price in the output must appear as digits or as spoken money words in the transcript, else drop it and lower confidence). Include the shared fixture. | `server/tests/test_extract.py`, `server/extract.py` (guard) |

**Acceptance**
- [ ] On the shared fixture with a mocked model reply, `acceptsCustomerParts` is true and no price is dropped.
- [ ] A mocked reply containing `allInPrice: 999` on the fixture is rejected (999 never spoken).
- [ ] Offline (no key) returns confidence 0 and no prices.
- [ ] Tests pass.

**Copy-paste prompt**
> You are Session 2 of `docs/multi-session-plan.md`. Read `docs/lessons-learned.md` and the plan. Branch `s2-extraction`. Do agents 2A, 2B, 2C. Only edit the files you own; in `orchestrator.py` touch only the extraction loop inside `_run_calls_then_complete`. Rebase on main before the PR. Tick the acceptance list in the PR.

---

## Session 3 — Find the real part

**Achieve:** the web lookup searches for the part the planner named, returns up to three sources, and labels OEM versus aftermarket.

| Agent | Task | Files |
| --- | --- | --- |
| 3A Query | Replace the hardcoded `PART_QUERY`. Build the query from the task: vehicle from `planner._offline_plan(task.request)["vehicle"]` (no LLM call needed) plus the job ("front brake pads and rotors kit" when the request says brakes). Fallback to the old string only if no vehicle is found. | `server/web_agent.py` |
| 3B Sources | One SerpAPI `google_shopping` call (never more, quota). Take up to three results in a sane range for the job (pads+rotors kit $60 to $600), each becoming its own `web` Agent: `business.name` = seller, `business.url` = link, `facts.partPrice`, `facts.partsType` from the title (genuine/OEM/Toyota → oem, else aftermarket), `summary` like "OEM pads + rotors $186". Keep the single HTTP fallback for no-key runs. | `server/web_agent.py` |
| 3C Orchestrator | `_spawn_web_agent` returns a list; `_replace_web_agent` replaces the queued slot with the list. Never touch call agents. | `orchestrator.py` (those two functions only) |
| 3D Tests | New `test_web_agent.py` with a canned SerpAPI JSON: three results, one out of range, one with no price. Assert query text, count, OEM labeling, no network. | `server/tests/test_web_agent.py` |

**Acceptance**
- [ ] For "front brakes on my 2019 Camry" the query contains "2019 Camry" and "pads and rotors".
- [ ] Canned SerpAPI JSON yields exactly the in-range results as separate web agents.
- [ ] Zero results yields one web agent with `status: failed` and no `partPrice`.
- [ ] Tests pass.

**Copy-paste prompt**
> You are Session 3 of `docs/multi-session-plan.md`. Read `docs/lessons-learned.md` and the plan. Branch `s3-part-lookup`. Do agents 3A to 3D. In `orchestrator.py` touch only `_spawn_web_agent` and `_replace_web_agent`. One SerpAPI call per task, mocked in tests. Rebase on main before the PR.

---

## Session 4 — The decision runs on General Compute

**Achieve:** General Compute reads every shop's facts, the web part prices, the user's quote and preferences, and returns the decision. Python verifies the arithmetic so the model cannot invent a number.

| Agent | Task | Files |
| --- | --- | --- |
| 4A Prompt + schema | Rewrite `synthesize.md`. Input: list of shops with facts, list of web parts, `userQuote`, preferences (default: prefer warranty and fewer trips unless savings exceed $150). Output JSON, strict schema: `options[]` with `label`, `total`, `breakdown`, `agentIds`, `hassle` (one clause); `recommendedOptionIndex`; `why` (two sentences); `tradeoffs` (2 to 4 short strings). Options allowed: "Bring your own part" per shop that accepts customer parts (web part + rate × hours), "Shop supplies part" per shop with an all-in. The model may only use numbers present in the input. | `server/prompts/synthesize.md` |
| 4B LLM + verifier | `synthesize()` builds the input, calls `llm.complete` with the schema, then verifies: every option total must equal a recomputation from the cited agents within $1; every cited agentId must exist; drop any option that fails; if the model output is unusable, fall back to the existing deterministic path. Map into `Result` (`hassle` folds into `breakdown` or `tradeoffs`, since `ResultOption` has no `hassle` field). Keep `savingsVsQuote`. If no shop quoted, say so, no options. | `server/synthesize.py`, `server/llm.py` (optional helper) |
| 4C Tests | Extend `test_synthesize.py`: three shops (one refuses customer parts, one cheapest all-in, one cheapest labor) plus two web parts; mocked model returns a valid decision → passes verification; mocked model returns a wrong total → that option is dropped; mocked model fails → deterministic fallback; one shop only → `why` says only one shop answered. | `server/tests/test_synthesize.py` |

**Acceptance**
- [ ] With the mocked valid reply, `Result` has two options, a recommendation, two-sentence why, and 2 to 4 tradeoffs.
- [ ] A hallucinated total is dropped and never reaches `Result`.
- [ ] Deterministic fallback still works with the model offline.
- [ ] Tests pass.

**Copy-paste prompt**
> You are Session 4 of `docs/multi-session-plan.md`. Read `docs/lessons-learned.md`, the plan, and `server/synthesize.py` as it stands. Branch `s4-decision`. Do agents 4A to 4C. General Compute makes the decision; Python verifies every dollar. Never let an unverified number into `Result`. Mock the model in tests.

---

## Session 6 — Cards show the facts

**Achieve:** after extraction each agent card reads like "$450 all-in · $150/h · 1-mo warranty" instead of "Call answered".

| Agent | Task | Files |
| --- | --- | --- |
| 6A Helper | New `server/summary.py` with `summary_from_facts(agent) -> str`. Call agents: join the present facts in order all-in, part, rate, customer parts ("takes your parts" / "no customer parts"), OEM/aftermarket, warranty. Voicemail → "Voicemail · no quote". Refused → "Declined to quote". No facts → "Call answered · no quote". Web agents: "<oem|aftermarket> pads + rotors $186". Under 60 characters. | `server/summary.py` |
| 6B Wire | In `orchestrator.py`, after `extract_facts` sets `agent.facts`, set `agent.summary = summary_from_facts(agent)` before persisting and emitting. In `_apply_dial_records`, use the helper for the voicemail and answered cases. | `orchestrator.py` (those spots only) |
| 6C Tests | `test_summary.py` covering each branch, including the shared fixture's facts. | `server/tests/test_summary.py` |

**Acceptance**
- [ ] Shared fixture facts → "$450 all-in · $150/h · 1-mo warranty".
- [ ] Every branch under 60 characters.
- [ ] Tests pass.

**Copy-paste prompt**
> You are Session 6 of `docs/multi-session-plan.md`. Read `docs/lessons-learned.md` and the plan. Branch `s6-summaries`. Do agents 6A to 6C. In `orchestrator.py` touch only `_apply_dial_records` and the summary assignment after extraction. Rebase on main before the PR.

---

## Monitor checklist (this session)

For each PR, in merge order S1 → S2 → S6 → S3 → S4:
- [ ] Diff touches only owned files and functions.
- [ ] No hardcoded prices, phone numbers, or keys.
- [ ] `python -m pytest -q server/tests` green on the merged tree.
- [ ] `general-compute-hackathon/server/.venv/Scripts/python.exe -c "import server.api, server.call_audio, server.orchestrator"` succeeds.
- [ ] Merge, push.

After all five are merged:
- [ ] Restart uvicorn **without** `--reload`.
- [ ] One live call with a key press. Confirm eight questions, transcript saved, facts include part price and hours, summaries on cards, result card shows two options with tradeoffs.
- [ ] Update `docs/lessons-learned.md` with anything new that broke.

## Out of scope for these sessions

Dialing every shop (needs the Twilio upgrade), booking, MentraOS, other verticals, any change to `frontend/` beyond rendering `tradeoffs` (monitor does that).
