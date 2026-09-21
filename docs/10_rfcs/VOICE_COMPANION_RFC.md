# RFC: Voice Companion — a spoken front desk for the existing agent stack

**Status:** Proposed — telephony-only v1, dev-only and experimental
**Date:** 2026-09-20 (supersedes the 2026-08-15 draft and its revisions; no changelog is kept — this document is the spec)
**Owner:** Dmytro
**Milestone:** New interaction surface — voice

**Related:** `COMPANION_AGENTS_RFC.md` (companions are ordinary agents — §4.4), `PLATFORM_SESSION_ISOLATION_RFC.md` (per-channel `session_id` — §4.9), `STANDING_DIRECTIVES_RFC.md` (delegation discipline — §4.2), `AGENT_NOTES_RFC.md` (rejected for the summary hand-off — §4.9)

---

## 1. Problem

**Speaking to alekbot already works; conversing with it does not.** Voice messages are transcribed
end to end today (`main.py:761,868` wires a real `audio_service`;
`conversation_handler.py:522-549` turns a voice memo into the user's own turn; see
`decisions/voice_message_transcription.md`). That is *dictation*: one utterance, one answer, screen
on, thumb on the button.

The gap is a **live exchange** — open-ended, interruptible, hands-free with the screen off. Three
obstacles block the obvious routes:

- **Slack huddles are a closed box.** No API starts or joins one, no audio or transcript endpoint;
  the only surface is the read-only `user_huddle_changed` event. Third-party "huddle APIs" capture
  system audio through a desktop SDK — inapplicable to Cloud Run. A huddle can be a launcher, never
  a transport.
- **Native voice messages are push-to-talk by construction** on both platforms.
- **Alek is the wrong shape for live voice.** His answers take seconds (Router → Smart → delegation
  → RRF search) and multi-second silence kills a spoken exchange; and his output is *written for
  reading* — markdown, tables, report URLs. Reading a URL aloud is not an answer.

## 2. Goals

1. An open-ended spoken conversation with a responsive persona.
2. The full existing Alek — memory, agents, delegation — reachable from inside it, unchanged.
3. Alek's reading-optimized output delivered to a reading surface, not mangled into speech.
4. What was said reaches long-term memory, so speaking makes the exocortex smarter the way typing does.
5. Reuse the existing session, prompt, notification and consolidation machinery.
6. Caller identity resolved in one place, so the authorization model can change without touching
   anything downstream (§4.6).

## 3. Non-goals

- **Voice messages in Slack/Telegram** — already shipped (§1).
- Persisting the raw voice session (§4.9 — deliberate).
- **Multiple concurrent calls per user** — refused at the `AuthDecision` step (§4.6), the one place
  that already knows the caller before a session opens. The marker lives in the same short-TTL
  store the ticket needs (§4.5) and is cleared when the relay submits the transcript. Two other
  releases matter because §4.6 takes the marker before the session exists: a callback that reaches
  a terminal state without connecting — no answer, busy, voicemail — releases it on Twilio's status
  callback, so a missed callback does not lock the user out; the TTL is the last resort, for a
  relay crash.
- **Calls originated by Alek's delegation** ("wake the user") — own RFC. v1 builds and uses the seam
  they need (§4.13), not the caller.
- **Calls to third parties** ("book a haircut") — a different product, not a later phase (§4.13).
- A native mobile client; a browser transport (§5.1 keeps it on the shelf); prod rollout.

## 4. Key design decisions

### 4.1 Lelik is a front desk for Alek, not a second intelligence

**Lelik is a voice assistant *to* Alek**: hear the request, clarify it if ambiguous, forward it, and
keep the conversation alive while the answer is prepared. **Alek is never spoken** — he answers in
text and Lelik says what matters aloud (§4.3).

Three things follow from the framing rather than from a rule:

- **Latency becomes the role's purpose.** The Realtime API's own async function calling lets the
  model keep speaking while a call is pending and weave the result in when it arrives.
- **Clarification before forwarding is an upgrade** on the text path, where a vague request reaches
  Smart and gets guessed at or asked back over a slow round trip.
- **Attribution becomes in-character.** A front desk naturally says "Alek says…", so the boundary
  between Lelik's own read and Alek's grounded answer is part of the role, not bolted on.

**The instruction is *narrate*, not *entertain*.** Entertainment as a goal produces filler;
narration fills the same silence and lets the user hear whether the request was understood before
the answer arrives — a free correctness check.

**Rejected — two realtime sessions (Lelik and Alek as two voice models).** A realtime model has
different weights and no agent stack: the thing holding the memory is the thing replaced.

**Rejected — TTS over Alek's answer in a second voice.** Markdown tables and URLs read aloud are
unusable.

### 4.2 Delegation is defined by capability, not by difficulty

"Call Alek when it's hard" fails both ways: calling on every utterance is wasteful, and left to the
model's discretion it will almost never call — LLMs judge their own competence poorly and answer
confidently instead.

The durable boundary is **possession**. Lelik has a scoped slice of the biography (§4.8) and no
mail, tasks or search of his own, so "do I have this?" is a presence check, not a self-assessment —
and models recognize missing data far better than insufficient intelligence. The rule: *forward
anything touching the user, their affairs, mail, documents, or current facts outside Lelik's slice.*

**Over-forwarding is the safe failure** — extra cost rather than confident fabrication about the
user's life, and §4.7's relay-attached context makes a redundant forward cheap.

**Backstops if the persona proves insufficient:** standing directives (exactly this class of rule);
an explicit trigger phrase ("ask Alek") that forces a call; raising reasoning effort (§4.3).

**A tension to hold:** more context for Lelik means less delegation. Continuity and delegation
discipline are two ends of one lever — which is why §4.8 keeps his slice small.

**Persona drift is the standing hazard** — the role lives in a system prompt and a voice session is
long by nature. Same class as `USER_TURN_SYSTEM_ANCHOR`: read `feedback_prompt_anchors.md` and its
six failure modes *before* writing Lelik's prompt.

### 4.3 Alek stays text-shaped; Lelik does the speaking

**Alek's prompt does not change.** He is not told he is in a voice session and produces his ordinary
reading-optimized answer. The mismatch between that and speech does not dissolve — it **moves to
Lelik**, which is correct, because verbalizing on the user's behalf is the front desk's job.
Reading-shaped fragments are not spoken at all (§4.10).

**Lelik receives `full_response`, not `response_summary`.** Both come from Smart's `_RESPONSE_SCHEMA`
(`smart_response_agent.py:95-100`), but `response_summary` is capped at 300 chars and documented as
being *for conversation history* — a compression artifact, not an answer.

**What the realtime model can do.** `gpt-realtime-2` (2026-05-07) brought GPT-5-class reasoning and
**configurable reasoning effort, `minimal`→`xhigh`**; `-2.1` / `-2.1-mini` (2026-07-06) cut p95
latency ≥25% and improved alphanumeric recognition, silence handling and interruption behaviour.

| Axis | Benchmark | Score (vs 1.5) |
|---|---|---|
| Audio intelligence | Big Bench Audio | **96.6%** (was 81.4%) |
| Instruction following | Audio MultiChallenge | **48.5%** (was 34.7%) |
| Instruction retention | Scale AI Audio MultiChallenge S2S | **70.8% APR** (was 36.7%), ranked #1 |

**The model is not weak, but instruction retention across a long spoken conversation is its weakest
axis** — and those figures were measured at `high`/`xhigh`, while a phone call runs near the latency
end. Two consequences, both load-bearing elsewhere:

1. **Reasoning effort is a per-session lever and the first knob to reach for** when persona drift or
   tool discipline degrades. Where it sits for a phone call is a spike (§7), not a guess — and the
   spike reports **cost**, not only latency and retention: reasoning tokens bill as text output at
   $24/1M (§6), the one leg that grows when this knob is raised.
2. **No discretionary judgment where a deterministic rule suffices.** §4.7's relay-attached context
   and §4.10's chat policy are this principle applied twice.

### 4.4 Lelik is an ordinary companion agent; the session loop is not

`COMPANION_AGENTS_RFC.md` §7 settled this for the family: companions inherit `BaseAgent`, declare
intents, and are reached through the registry. **Its stated reason does not hold.** "Both
mechanisms work in either direction provided both sides are registered agents" is false in the Alek
direction — `agent_manifest.py:19` fixes the invariant that the coordinator never routes *to* an
orchestrator, and §4.7 depends on exactly that. Left uncorrected upstream; nothing the tutor does
rests on it.

What carries the decision here is a caller. **Lelik has a real one from the first slice**: every
call is placed by us (§4.6), and placing it is `LelikAgent.execute()` originating an outbound call
with a brief (§4.13). Without that caller the class would be a naming-convention shell —
`PromptBuilder.build_for_agent(agent_type: str, …)` takes a **string**, so an agent class buys
nothing on its own. Alek's delegation is a second caller later, against a path already built,
tested and billed.

**The session loop does not live in the agent** — minutes-long sessions do not fit `_call_llm`'s
per-turn shape:

- **`LelikAgent`** owns what agents own: persona prompt, tool set, permission toggles, policy,
  outbound origination.
- **`VoiceSessionService`** owns the loop: two live connections, byte relay, call-scoped buffer,
  lifecycle, tool dispatch.

`REQ-ARCH-03` + `REQ-ARCH-30` together mean the loop component **cannot be named `*Agent` at all** —
naming discipline here is load-bearing, not cosmetic.

**The provider boundary gets a port, and it carries audio.** `LLMPort.generate_content(request) ->
LLMResponse` (`src/ports/llm_port.py:54`) is strictly request/response:

- `ports/realtime_session_port.py` — open/configure, push and receive audio frames, receive
  tool-call events, submit tool results (including late — §4.7), receive usage events, close.
- `adapters/openai_realtime_adapter.py`, `adapters/xai_realtime_adapter.py`.

**The adapter strips `PROMPT_CACHE_BOUNDARY`** (`"<!-- CACHE_BOUNDARY -->"`) before the assembled
prompt becomes the session's `instructions`, exactly as all four existing LLM adapters do. It is an
Anthropic-only cut point with no meaning in a realtime session, and a session prompt is set once —
so the marker would not be stripped anywhere downstream, it would be spoken.

**Two real implementations, verified.** xAI serves speech-to-speech over WebSocket at
`wss://api.x.ai/v1/realtime?model=…`, supports `audio/pcmu` (G.711 μ-law 8 kHz), and uses the **same
tool-calling event shape as OpenAI** (`response.function_call_arguments.done` →
`conversation.item.create` / `function_call_output` → `response.create`).

**Audio frames cross the port as a `domain/` value object** — pure: encoding, sample rate, payload
bytes, track direction. Both the port and the carrier-side handler speak it, which is what keeps the
service layer provider- and carrier-agnostic (`REQ-ARCH-06`/`-07` force the placement).

### 4.5 Deployment split: the main service holds state, the relay holds media

**The relay touches no Firestore, resolves no channels, posts to no chat, assembles no prompt.** It
is a handler, a service, one inbound port and one outbound port.

```
Trigger            a dial from a bound number → auth webhook on the MAIN service
                     → AuthDecision (§4.6) → ticket minted → <Say> + <Hangup>
                   or a Slack "call me" command, already authenticated — no inbound leg

Origination        LelikAgent.execute(purpose) → TelephonyPort (Twilio REST) → outbound call
                     → on answer, the ANSWER webhook (MAIN service) resolves the ticket
                     → PromptBuilder assembles Lelik's persona
                     → TwiML <Connect><Stream> to the relay, carrying the same opaque TICKET
                   relay opens the WS, exchanges the ticket for the session config, connects to provider

During the call    ask_alek / send_to_chat → CallControlPlanePort → HTTP → main service
End of the call    relay flushes the call buffer — transcript, usage events, turn segments —
                     → main service summarizes, writes to the primary-channel session,
                     delivers one message, records usage and turns (§4.12)
```

**The relay reports nothing on its own.** Usage and turn segments accumulate in the same
call-scoped buffer as the transcript and ride out on `submit_transcript`; the main service, which
already owns `QuotaService` and `PromptContentStore`, does the writing. This is what keeps "holds
only media" true rather than aspirational — the alternative is a relay with its own Firestore and
BigQuery legs, which is a second stateful deployable wearing a stateless label. The cost is that a
relay crash loses the call's usage along with its transcript, already accepted in §4.14.

**The ticket is a reference, not a payload.** Lelik's prompt contains the user's biography, and TwiML
parameters are visible in the carrier's console and logs. Twilio's `<Connect><Stream>` delivers
custom parameters natively in the stream `start` event, so the handle needs no side channel.

**It is an opaque handle, and it needs a store.** Twilio caps a `<Parameter>`'s name and value at
500 characters combined, so a signed token carrying claims does not fit — the ticket is a random
identifier resolved server-side. The main service is multi-instance, so the ticket may be minted on
one instance and exchanged against another: the store is shared with a short TTL, not in-process.
Same store, same TTL as §3's one-call-per-user marker.

**`CallControlPlanePort` is a real port, not cleanliness.** `REQ-ARCH-18` forbids
`httpx`/`aiohttp`/`requests` anywhere in `src/services/` (whitelist: one file,
`google_oauth_service.py`), so `VoiceSessionService` cannot make an HTTP call itself. Four
operations justify the contract — `fetch_session_config(ticket)`, `ask_alek(call_id, query,
context)`, `send_to_chat(call_id, text)`, `submit_transcript(call_id, buffer)` (the buffer carries
transcript, usage and turn segments together) — and the substitution need is named: if the relay
ever merges back into the main service, the adapter becomes an in-process call and nothing above it
changes.

**No `CarrierMediaPort` in v1.** Twilio connects *inbound* — that is a handler, not an outbound
adapter. Carrier-agnosticism comes from the `domain/` frame type, not an abstraction over Twilio.
(Outbound origination is the other direction and *does* get a port — §4.13.) A second transport
later is another handler over the same service, not a refactor.

Architecture rules this shape satisfies that a naive relay would break are enumerated in §8.

### 4.6 Caller identity: we place the call, always

**The requirement is a shape, not a mechanism.** Authentication is **one step, before the session
opens**, returning `AuthDecision(user_id, account_id)` or a refusal. The relay never sees the phone
number — it receives a resolved identity in the session config, and nothing below consults caller ID
again. Swapping the mechanism is then a change to one component with a typed result: relay,
`ask_alek`, prompt assembly and billing are untouched. **That is the answer to "what happens when
other people get access" — one component, not a new architecture.**

**No port for it in v1** — one implementation, no I/O boundary. `AuthDecision` in `domain/` plus a
single call site is the seam; an external verifier, when it arrives, gets its own port additively.
The type carries no authorization level either: v1 has exactly one, and a field with one value is a
seam without a user. `AuthDecision` gains the level when §4.13's deferred modes need it — a field
on a domain value object read in one place, not a refactor.

**There is one authenticated path: the system places the call.** Two things can trigger it, and the
difference between them is *who asks*, not how identity is established:

- **A dial from a bound number (primary).** The caller dials, we answer only long enough to say we
  are calling back, hang up, and originate an outbound call to that number.
- **A Slack command** ("call me"). Already an authenticated surface — the workspace signature
  identifies the caller, the same way every other command in this bot does. No inbound leg at all.

**Why the callback authenticates.** Caller ID is a claim; the outbound leg is a test of it. If the
`From` was forged, our call goes to the **real subscriber of that number** and the attacker gets
nothing — while the owner's phone rings for a call they did not place, so the tripwire is the
mechanism rather than an alert bolted beside it. This is classic callback authentication, and it
rests on the same assumption as an SMS OTP — possession of the line — without the SMS. It works over
plain PSTN, needs no credential on the device, and therefore **needs nothing from Siri but an
ordinary dial**: `"Hey Siri, call Lelik"` is a phone call, not an integration.

That last point settles a real blocker. `auth_required` accepts `Authorization: Bearer`
(`user_cabinet_app.py:57-82`), but the access token's TTL is one hour (`session_service.py:43`) and
this codebase has no long-lived API-key mechanism — so a Shortcut hitting an endpoint had nothing to
carry, and designing a scoped revocable key is its own security surface. The callback removes the
requirement instead of satisfying it.

**Mechanically:**

```
inbound dial → auth webhook (MAIN service)
  → number in the caller's platform map?  no → <Reject>, AlertSinkPort, done
  → AuthDecision minted into the short-TTL ticket store (§4.5), one-call marker taken (§3)
  → TwiML: <Say> "calling you back" + <Hangup>
  → LelikAgent.execute(purpose="user asked to talk") → TelephonyPort → outbound call
       answer-URL carries the ticket → ANSWER webhook (a different endpoint) → session TwiML
  → relay opens the WS, exchanges the ticket, session begins
```

**The answer-URL is not the auth webhook.** They are two endpoints. Pointing the outbound call's
answer-URL at the inbound handler is a loop that dials until something breaks — worth stating
because both are "a Twilio voice webhook on the main service" and the mistake is one config line.

**Answering-machine detection is mandatory on the callback, not optional polish.** If the owner does
not pick up, the callback lands in voicemail — and Lelik would hold a conversation with a greeting
and then summarize it into the owner's long-term memory (§4.9). Twilio's machine detection on the
outbound call, hanging up on a machine, is the cheap fix; the expensive failure is a fabricated
memory, not a wasted leg.

**No session exists until the callback connects.** There is no partially-authenticated window, no
`session.update` upgrade, and one persona assembly. The gate sits on the same side of the boundary
as the data it protects.

**Binding proves ownership once.** `add_platform_id(user_id, "phone", <E.164>)` — the same mechanism
`link-telegram` uses (`src/web/user_cabinet_app.py:279`), uniqueness for free, 409 when already
bound. `link-telegram` does not verify that the binder owns the ID; inheriting that would permit
squatting, so binding requires a one-time SMS/voice OTP — no new dependency.

**Notifications remain, on top of the ringing phone.** Every session opened posts to the user's chat
channel; every refused dial from an unknown number goes to `AlertSinkPort`.

**Rejected mechanisms:**

- **Opening the session on the inbound leg directly** — the cheapest thing, and the obvious one. It
  authenticates nothing: caller ID is spoofable from any VoIP provider, so this hands the biography
  and the memory-write path to whoever forges a `From`. The callback costs one
  extra leg and 5–15 seconds to close it.
- **A spoken PIN every call** — friction on the exact use case the feature exists for, and it puts a
  credential through the provider, transcript, session history, BigQuery and Logfire, then requires
  redaction at every one of those boundaries to undo its own choice.
- **A spoken key phrase** — mechanically a PIN with a worse leak surface: longer, more memorable,
  therefore more reusable, and spoken in front of whoever is in the car.
- **Voice biometrics** — not exposed by either provider (so: a third-party vendor); a *probabilistic*
  gate whose false rejects are intolerable on a personal tool; measured on μ-law 8 kHz PSTN audio,
  the degraded case these systems handle worst; defeated by replay without a liveness layer. Decisive:
  a voiceprint is **GDPR Art. 9 special-category biometric data** — heavier compliance than the PIN it
  replaces. Possible later as a *passive risk signal* behind this seam; not as a gate.
- **STIR/SHAKEN** — the right mechanism (carrier attestation, free, zero friction) but a US/Canada
  framework, not deployed in Spain. Recorded because it plugs in behind the same seam if it lands.

**Kept on the shelf — DTMF on the inbound leg.** A keypad code collected by `<Gather>` before any
media stream exists never reaches the model, the transcript, BigQuery or Logfire, which is what made
the *spoken* PIN unacceptable. It is the fallback if the callback's extra legs or seconds prove
intolerable in practice — a better-shaped PIN than the rejected one, not a revival of it.

**Accepted risks, two:**

- **Call forwarding on the owner's line.** If an attacker controls forwarding, the callback reaches
  them. That is takeover of the line rather than spoofing of it, and it composes with the tripwire:
  Twilio Lookup exposes a call-forwarding signal, cheap to query at auth time. Not queried in v1;
  named as the first thing to add if the threat stops being theoretical.
- **Someone else answers the owner's phone.** A bound number is a *number*, not a person, and
  whoever speaks has their speech transcribed into the call buffer and summarized into the owner's
  memory (§4.9). At N=1 this is self-recording. Not solved in v1 — the fix is a disclosure at call
  start, not an architectural change.

**Triggers to revisit:** the first non-owner user (both risks at once); any alert on a refused dial;
STIR/SHAKEN in Spain.

### 4.7 `ask_alek` — Alek is invariant per call

**Alek does not remember between calls.** Each `ask_alek` is self-contained, like any specialist
invocation. The alternative — writing every in-call turn into a session — pollutes chat history with
requests the user never made and contradicts §4.10.

**Providing context is therefore the caller's job, and it is not left to the model:**

```
ask_alek(query, context)
```

`query` comes from Lelik. `context` is **always attached by the relay** from the call-scoped buffer
— the last N exchanges of this call — whether or not Lelik supplies anything; Lelik may add to it
("he means Ivan Petrov, the colleague") but cannot omit it.

The relay attaches it rather than the prompt demanding it because §4.3's figure says instruction
retention across a long conversation is the model's weakest axis, and **this failure is silent**:
Alek gets a context-free question and answers something plausible and wrong. A prompt token still
teaches Lelik that Alek is stateless, but correctness does not depend on that token surviving twenty
minutes.

**Mechanism:**

```
Lelik emits function call ask_alek(query)
  → relay attaches call context, captures call_id
  → relay spawns a tracked asyncio task (held in a set — RUF006 forbids fire-and-forget)
       → CallControlPlanePort.ask_alek(...) → HTTP to the main service, OIDC-authenticated as /worker is
       → main service: RequestContext(user_id, account_id) → Router → Smart → delegation
       → responds with full_response, link_list, rich_content
       → chat delivery, if any, happens here (§4.10) — the relay never posts to chat
  → relay queues the injection; when no response is active:
       conversation.item.create { type: "function_call_output", call_id, output: full_response }
       response.create
  → Lelik verbalizes (§4.3)
```

Lelik keeps talking while the task runs, which is what makes §4.1's narration load-bearing rather
than decorative. "Async" here means *the provider's own late `function_call_output`* — not this
repo's `ExecutionMode.ASYNC`, which enqueues a Cloud Task delivered by `UserNotificationService` to
a chat channel. **No mechanism in `src/` returns an async result into a still-open caller session**,
so that path cannot serve this one.

**Alek is reached through the Router, not Smart.** `ConversationHandler` sends to
`router_agent_{user_id}` (`conversation_handler.py:700`) and it is `router_agent.py:332` that calls
`enrich_context` — the RRF memory search; `smart_response_agent.py` has none. Calling Smart directly
is Alek without memory and Goal 2 fails, so the main service's entry point targets the Router
exactly as `ConversationHandler` does. `SMART_RESPONSE` and `QUICK_RESPONSE` carry `capabilities={}`
and are absent from `ALL_DESCRIPTORS` — that deliberate omission is unchanged and nothing here needs
either registered.

**Why not the in-process delegation machinery.** The relay is a separate Cloud Run service (§4.14);
HTTP is the boundary either way. The main service then runs Router → Smart as an ordinary request —
**the same concurrency shape as a second Slack message arriving mid-call**. There is no
`asyncio.Lock` in `src/agents/core/`, so concurrent executions on a per-user singleton do not
serialize.

**Corner cases — requirements, not commentary.**

| Case | Required behaviour |
|---|---|
| Call ends while the answer is in flight | Cancel the task; **discard the answer** (it served a conversation that no longer exists). Log it. No chat fallback unless §4.10 already required one. |
| Model is mid-response when the answer arrives | `response.create` while a response is active is an error. Track `response.created`/`response.done`; queue the injection until idle. |
| User barges in during injection | Submit the `function_call_output` item immediately (harmless — it is a conversation item), defer `response.create` to the next idle moment. |
| Two `ask_alek` calls in flight | Match by `call_id`; serialize `response.create` so they cannot collide. |
| Main service slow or hung | Hard relay-side timeout, start at 90s — Router → Smart with delegation is the cost, not cold start. On expiry inject a `function_call_output` saying Alek did not answer, so Lelik says so aloud. **Silent non-delivery is the worst outcome.** |
| Transport failure | **No automatic retry** — it re-runs Alek's whole pipeline: double spend, possible double chat delivery. Fail loudly to Lelik. |
| Relay restarts mid-call (deploy) | Call drops; answer in flight lands nowhere; transcript buffer lost. Accepted. |
| Concurrency ceiling | Each in-flight `ask_alek` occupies a request slot on the 1 vCPU main service. Named, not solved, in v1. |

**A late `function_call_output` is the load-bearing assumption of this section.** OpenAI documents
async function calling; xAI documents the same event sequence but does not explicitly sanction
submitting the output after the model has moved on. Nothing structurally forbids it in an
event-based protocol, but it is undocumented on at least one provider. **Spike it on both, first**
(§7).

### 4.8 Lelik's context at call start

**Biographical access is scoped, not off.** `include_biographical` + `session_domains` — the tutor
turns biographical access fully off; Lelik turns it on restricted to a small explicit list of fact
domains: enough for light continuity and small talk, not a substitute for forwarding. A live
filtered read of the same fact store Alek uses, so nothing needs syncing. One list, because §4.6
issues one authorization level.

**Only the biographical leg of the companion assembler is live for Lelik.** Of
`CompanionContextAssemblerService.assemble_context()`'s three: `get_summary(session_id)` reads
`CompanionCacheRepository`, written **only by a companion's extractor** — and §4.9 builds none for
Lelik, so it is always `None`; `own_records` reads the `CompanionRecord` store §4.9 rejects, so
`include_own_records=False`. The toggles are plain keyword arguments, so **Lelik constructs no
`CompanionConfig`**: its write-side fields (`window_threshold`, `batch_size`) are
required-without-default and meaningless here, and the mechanism that delivers one to the tutor
(`AgentDescriptor.companion_default_config` on a `ChannelBinding` via `$agent`) never runs for a
phone call, which has no `channel_id`.

**The history slice comes from `SessionStore`, not from the assembler** — a small summarized slice
of the primary-channel session (§4.9's destination, read side), the path `TutorAgent` uses via
`HistorySummaryService`. Keep it small: §4.2's tension says more context means less delegation, and
§4.7 makes a redundant forward cheap.

**The prompt stays deliberately small** — persona, tool definitions, trimmed history. The reasons are
instruction retention (§4.3) and latency, not token spend: cached input is $0.4/1M against $32/1M
uncached, and `-2.1` specifically improved caching.

### 4.9 The call is ephemeral; one summary lands in a named session

**No isolated companion memory for Lelik — unlike the tutor.** The tutor's policy is *isolated* (own
`CompanionRecord` store, own extractor) so drills don't pollute Alek's biography. Lelik's value
proposition is the opposite — the same being on the phone as in text — so his write policy is
*transparent into Alek*: no `CompanionRecord` collection, no dedicated extractor, none of the
threshold-driven `CompanionExtractionQueue` / `OverflowRoutingService` machinery.

**The raw transcript is not persisted.** It lives in a call-scoped buffer in `VoiceSessionService`;
at call end the relay flushes it to the main service and it is gone.

**No in-call tiering in v1.** A one-hour call — the ceiling imposed by Cloud Run's request timeout
(§4.14) — plausibly fits the 128K realtime context with no custom mechanism. Measure real token
growth on the first long call; build tiering only if the measurement demands it. Provider-native
truncation, if offered, is checked first (§9).

**At call end a cheap separate model pass — not Lelik — produces one summary.** Bulk noisy-text
summarization is a cheap-tier job, and summarizing through the participant being summarized is the
one thing the summarizer must not be.

**It runs on the main service, and it reuses the companion extractor's port.** `services/` may not
import `agents/` (`REQ-ARCH-01`), and the companion family already solved this for the tutor. The
obvious reading — that a new port is needed because `CompanionExtractionService` unconditionally
persists into the `CompanionRecord` store this section just rejected — argues from the *consumer*
to the *port*, and does not hold: `CompanionExtractorPort.extract()` persists nothing, it returns
`{"records": [...], "summary": str}`, and `CompanionExtractorRunner._EXTRACTORS` is a
`companion_type → (agent_type, agent class)` map that a second entry extends.

So: **a `"voice"` entry in that map, and a different consumer** — one that takes `summary` and
discards `records`. A new port here would be a port for cleanliness, which this repo does not do.
Two things to settle while wiring it, neither a reason to fork the port: the runner hardcodes
`TUTOR_EXTRACTOR.timeout_ms` (parameterize per type), and `companion_type` becomes a slightly
loose name for a non-companion. If either turns out to be more than cosmetic, the fork gets written
down here with the real reason.

**The summary is written and delivered by `UserNotificationService`.** Not a bespoke path:
`notify_document_link` (`user_notification_service.py:425-495`) already performs exactly this
sequence — resolve the channel, deliver, then `append_messages_batch` a synthetic `user` +
`model` pair into `f"{user_id}:{channel_id}"`. The call summary is a sibling method on that
service, which settles three things a direct `SessionStore` write leaves open: `_resolve_channel`
is private, `REQ-ARCH-22` forbids a new service from importing `UserNotificationService`, and the
*role* of the written message is not a free choice —
`serialize_messages_for_consolidation()` reads user and model parts differently, so the
established two-message shape is the one that consolidates correctly.

The earlier framing — "not through `notify()`, which reformats" — was a false dichotomy.
`notify()` is one method among several on that service, and the one being reused writes history
without reformatting anything.

**Which session, precisely.** A session is per channel (`session_id = f"{user_id}:{channel_id}"`)
and a phone call has no channel; a synthetic `phone:<E.164>` session would be *isolated* from every
chat channel, failing Goal 4. **The call writes into the session of the user's primary notification
channel**, resolved by the chain `_resolve_channel` already uses — override → primary → last active
(`user_notification_service.py:95-125`). If neither resolves, the summary is dropped with a logged
warning rather than written somewhere arbitrary. All of this is main-service work; the relay only
submits the buffer.

Both downstream effects are free: `history_recent_full_turns` keeps recent session messages in full
text, so the message is visible to Alek on the very next turn; and ordinary threshold-based
consolidation absorbs it like any other message (`serialize_messages_for_consolidation()` handles a
plain text `MessagePart` with no special casing).

**A dropped call still summarizes** — if the call ends by network loss, the summarizer runs against
whatever accumulated. Total loss of the conversation is the one outcome this section cannot
tolerate.

**Rejected — `AgentNote` or a bespoke "knowledge block" cleared on consolidation.** An ordinary
message achieves both goals with zero new mechanism.

**The accepted cost:** with no raw log retained, summary quality is the single point where anything
can be lost, irreversibly. The summarizer prompt is a first-class deliverable.

### 4.10 What reaches the chat, and when

Most `ask_alek` calls are *internal* — Lelik needs data in order to speak — so copying every one
into Slack fills the channel with requests the user never made. It is also the wrong thing to ask
Lelik to judge: the user is on the phone precisely because they cannot look at a screen, so anything
delivered during the call is read *afterwards* anyway, and per §4.3 discretionary judgment held
across a long conversation is the model's weakest axis.

1. **During the call, nothing goes to chat by default.**
2. **Structural exception — reading-shaped content always goes.** If Smart's `link_list` or
   `rich_content` is non-empty, a chat copy is sent regardless of anything Lelik decided. Reading a
   URL aloud is useless (§1), so this is a property of the answer, not a judgment call.
3. **Explicit request — one simple tool.** `send_to_chat` posts the last answer through
   `notify_raw`. The one chat-related thing Lelik may do, and deliberately the easy kind:
   recognizing an explicit instruction, not modelling the user's future reading habits.
4. **At the end of the call, one message** — §4.9's summary, *delivered* as well as written to
   history: what was discussed plus the links and artefacts that came up. One clean trace to open
   after hanging up, instead of a scatter of messages that arrived while driving.
5. **An answer arriving after the call has ended is not spoken and does not reach the summary**
   (§4.7) — but a chat delivery that rule 2 already triggered is **not** retracted. The structural
   copy is sent on the main service the moment Alek answers, before the relay sees the response at
   all, so by then it has happened. Discarding the *spoken* answer and un-sending a message are
   different acts; only the first is possible.

**Note on history.** `notify_raw` delivers without writing session history
(`user_notification_service.py:129-172`), so §4.9's `append_messages_batch` plus delivery here is
one write and one delivery, not a double write. It also means a `send_to_chat` post leaves no trace
in Alek's history on its own — deliberate; the end-of-call summary carries the call into memory.

**Why this over "Lelik decides."** Predictability: the user learns "everything that matters arrives
at the end", and a learned rule is one they can rely on, whereas per-turn model judgment is
unlearnable and reads as random. And no silent loss: the raw transcript is not retained, so a link
Lelik chose not to forward would be gone permanently.

**Trigger to revisit:** if real calls show the end-of-call summary is consistently too coarse, raise
reasoning effort (§4.3) and give Lelik the judgment — do not add more structural rules.

### 4.11 `RequestContext` for the lifetime of a call

`RequestContext` (`src/domain/request_context.py`) is built on `contextvars`. Each `asyncio.Task`
gets its own copy at creation, so concurrent tasks cannot see each other's values — safe for
concurrent calls from different users on one relay instance.

**But nothing in this codebase has ever held one open for minutes** — every current call site wraps
a single bounded operation. And the companion work already shipped a bug of exactly this shape:
`CompanionContextAssemblerService._fetch_biographical` fell back to whatever context was ambient
when no `user_id` was supplied, across a permission boundary. Caught on a purity audit, not by a
test.

**It is load-bearing for observability too**: `PromptContentStore.record_turn` pulls `trace_id` and
`user_id` from the request-scoped context (`src/ports/prompt_content_store.py:29`), so a call that
loses its context loses its BigQuery attribution.

**The invariant, stated so it can be tested:**

- One call = one `asyncio.Task`, entering `RequestContext(user_id, account_id)` exactly once, from
  the `AuthDecision` (§4.6) and never from an unresolved caller.
- No work for that call escapes the task's context — every sub-operation is awaited within it or
  spawned as a child task (which inherits the copy). Nothing goes to a shared worker, executor, or
  long-lived queue consumer that would run under a different context.
- Ports receive `user_id`/`account_id` explicitly where their signature allows it, rather than
  relying on ambient resolution — the fail-closed posture the companion fix established.

### 4.12 Billing and observability need audio legs

`TokenLedger.by_model` prices text tokens per model that ran; audio is a different rate class and is
not expressible today. Without this, voice cost is invisible — the same bug class as the
pre-2026-07-30 price attribution. Ships with the first phase, not after.

- `_EXECUTION_LEDGER` / `_execution_billing_scope` are module-private to `base_agent.py` (lines 62,
  535). A non-agent component cannot open a ledger and **does not need to**; nothing about them
  changes.
- `QuotaService.record_usage(account_id, model, tokens, cost)` is a **port**
  (`src/ports/quota_service.py:10`) with no agent coupling. **The main service calls it, not the
  relay** — its only implementation is `FirestoreQuotaService`, and §4.5 keeps the relay off
  Firestore. The relay buffers the provider's usage events and submits them with the transcript;
  the same holds for `PromptContentStore`, whose only implementation writes BigQuery.
- **Audio pricing belongs in `domain/billing.py`**, next to the text rates. `calculate_external_cost()`
  and `_PER_SECOND_SERVICES` (line 313) already establish a non-token rate class beside the per-token
  table, so a per-minute rate (xAI) and a per-audio-token rate (OpenAI) both fit without
  restructuring. A relay carrying private rates would re-create the 2026-07-30 attribution bug in a
  new place.
- **Four legs for OpenAI, not one.** A realtime session bills audio input, audio output, **text
  input and text output** — and reasoning tokens land on the text-output leg (§6). Pricing a call
  on its audio alone would under-report exactly the part that Phase 0's reasoning-effort decision
  makes bigger. xAI's flat per-minute rate needs the per-second class and its small text-input
  leg.

**Transcript observability needs a segmentation policy *and* a synthesis decision.** `record_turn`
takes fully-typed `LLMRequest`/`LLMResponse` and writes one `request_text`/`response_text` pair per
row, while a duplex audio stream has no natural turn boundary: speech overlaps, barge-in truncates,
tool calls resolve out of band.

- **Segment on the provider's `response.created` → `response.done` boundaries**, treating user audio
  transcribed since the previous boundary as `request_text` and the model's output as
  `response_text`. Interruptions become a `finish_reason` on the truncated response, not a discard.
- **Decide what the synthesized objects mean** for an audio turn — `model`, `tools`, `messages` —
  rather than filling them with plausible-looking values. A partly fabricated row is worse than an
  honest narrow one.
- If either proves lossy, the fallback is a voice-specific store — but not before the simple thing
  has been tried.

### 4.13 Outbound: what v1 builds, and what it defers

Calls originated by Alek's delegation, and calls to third parties, are **not** in v1 (§3). What v1
does build is outbound origination itself — not as a seam for later, but because §4.6 makes it the
only way any call starts:

1. **`TelephonyPort`** — outbound origination. Twilio connecting inbound is a handler; us telling
   Twilio to place a call is an outbound adapter call, so it gets a port. Used by every call.
2. **`LelikAgent` with a descriptor and an intent**, whose `execute()` originates a call with a
   brief. Its v1 caller is the callback itself; Alek's delegation becomes a second caller later.
   **The descriptor ships `internal=True`** — otherwise registering the intent puts "place a phone
   call" straight into Smart's tool list, handing an LLM the deferred capability on day one.
   Flipping that flag is precisely what the outbound RFC does; until then the seam is closed, not
   merely unused.
3. **A `purpose` field on the session config** — `"user asked to talk"` in v1, a real brief later.
4. **Resolved identity on the session config** (§4.6) — also where the later question "who actually
   picked up?" will live, and where an authorization level is added when a deferred mode needs one.
**Not built in v1: persona assembly by call mode.** A deferred mode ("wake him", "book a haircut")
needs a different persona, but v1 has exactly one mode, so a mode parameter would be a seam with no
user. Named here only so the place is agreed: it is an argument to the same assembly call §4.6
already makes, not a fork of the service.

What the deferred RFC adds is therefore a *caller and a gate*, not a transport: flip `internal`,
register the intent, pass a mode to the assembly, and bound which numbers an LLM may reach at what
rate.

**Symmetry with §4.7:** `ExecutionMode.ASYNC` is wrong for Lelik→Alek (nowhere to return the result
in a live session) and **right** for Alek→Lelik — a call lasts minutes and its outcome is naturally
delivered later by `UserNotificationService`. Same mechanism, opposite verdict by direction.

**Third-party calls are a separate RFC, not a later phase.** They share the transport and the port
and nothing else: the callee is not a user, so §4.6's identity model does not apply; the persona
speaks *as the user's representative* rather than serving him; the failure mode is committing the
user to something; and there is a legal surface this RFC does not have — AI-disclosure obligations
under the EU AI Act and recording-consent rules in Spain. A hard gate on outbound destinations and
rate is a prerequisite of that RFC, because the decision to place a call would be made by an LLM.

### 4.14 The relay is its own Cloud Run service

**Not part of `alek-bot`** — Cloud Run behaviour makes the workloads incompatible on one service:

- **An open WebSocket is one long request.** Under Cloud Run's default request-based billing, CPU
  and memory are charged for the whole time a request is in flight — so a 20-minute call is 20
  minutes of billed CPU, and the instance stays up throughout. (It does not switch the service to
  instance-based billing; that is a separate opt-in setting.) Fine for active use, but on a 1 vCPU
  box that call competes with Slack and Telegram request serving for the duration.
- **Request timeout caps a call at 60 minutes** (default 5). The relay needs `--timeout=3600`, not a
  setting to apply to the main service wholesale. **A call over 60 minutes is cut — accepted v1
  limit.**
- **Scaling and warm-up profile differ**, and it may want its own region (§5.2).

Precedent: DeepResearch already runs as a Cloud Run **Job** with its own entrypoint (`job_main.py`).
A second Cloud Run **service** is the same pattern.

Session affinity is explicitly *not* needed — the WebSocket **is** the session. This matters for
§4.7 too: the relay *initiates* the call to the main service and holds the response, so nothing ever
needs routing back to the instance holding a given socket.

**Failures report to `AlertSinkPort`** (`src/ports/alert_sink.py`) — provider connection failures,
carrier socket drops, `ask_alek` timeouts — the same one-line shape `AgentCoordinator` and
`FirestoreQuotaService` use. A call's only user-visible error surface is silence; it must not fail
quietly.

**No `CircuitBreaker` or retry policy on the session connection.** A human is on the line: a failed
connect or dropped socket ends the call, alerts, and lets the user redial. Retrying into a live
conversation produces worse artefacts than a clean failure.

## 5. Transport — telephony, with media relayed through us

**Telephony, not a browser page.** A Spanish Twilio number is already provisioned and owned; what
remains is configuration, not paperwork. If the European path (§5.2) is ever taken, account and
number must be homed in `IE1`.

**The call's audio is relayed through our backend (Twilio Media Streams), not routed carrier-direct
over SIP.** The deciding argument is **perimeter**, not latency: `CLAUDE.md` makes reading actual
data the mandatory first step of any production investigation, and under carrier-direct SIP an
entire class of failures — SIP negotiation, codec mismatch, trunk auth, media interruption —
produces no Cloud Logging entry, no Logfire span and no BigQuery row; it lives in two third-party
consoles. Relaying puts the call inside the contour, and adds an independent measure of duration and
bytes to cross-check provider-reported usage — worth having, given this codebase's 3.6×
token-inflation bug and its wrong per-model price attribution.

Both objections to relaying fail on inspection. **Latency:** the relay does not introduce the
transatlantic hop — that is a function of where Twilio's media engine and the provider endpoint sit,
and carrier-direct SIP incurs the same crossing inside Twilio's network; with regions aligned the
relay adds one same-region hop plus frame forwarding, and barge-in stays native to the provider (we
forward bytes and never implement VAD). **"More code":** Twilio sends `audio/x-mulaw` 8 kHz mono
base64 and accepts `mulaw/8000` back, and **both candidate providers accept G.711 μ-law at 8 kHz**
(OpenAI `g711_ulaw`, xAI `audio/pcmu`) — so the relay is a base64 decode and a byte forward, no
resampling, no DSP.

**Rejected — carrier-direct SIP.** Cheapest and marginally lower latency, but it puts the media path
outside every observability mechanism we rely on; the Twilio↔provider binding lives in a console no
test can reach; swapping providers becomes console reconfiguration rather than an adapter swap; and
it requires provider SIP ingest, which collapses `RealtimeSessionPort` to one implementation. On the
shelf as a latency optimisation — a different architecture, not a config flip.

**Rejected for v1 — transcript-only (carrier transcription → text agent → TTS).** Removes the
realtime audio model, so barge-in becomes ours to build, prosody is lost, latency is *worse*, and
§4.1's premise that the pause is covered by natural speech is abandoned. Its genuine advantage is
cost, which makes it the fallback if §6's economics disappoint.

### 5.1 Why a browser transport is not v1

**Browsers cannot capture audio in the background.** iOS Safari suspends WebRTC and Web Audio the
moment the screen locks or Safari backgrounds — unchanged in Safari 26; Android Chrome degrades
rather than works. Any browser transport would be screen-on, which defeats the point. Kept on the
shelf if the missing reading surface proves a real gap: a one-button page (WebRTC, ephemeral-token
endpoint behind `auth_required`).

### 5.2 The media path and region alignment

```
caller → PSTN → Twilio media engine → [WebSocket, μ-law 8k] → relay service (Cloud Run)
                                                                      ↕
                                                    [WebSocket, μ-law 8k] → realtime provider
```

The relay forwards frames both ways and sees every session event: tool calls, transcription events,
usage.

**Region alignment is the whole latency question** — three configurable things must agree: Twilio's
media region (`US1` default, `IE1`, `AU1`; follows account/number homing), the provider endpoint,
and the relay's Cloud Run region.

**Pragmatic v1: all three on the US path** — number homed `US1`, standard provider endpoint, relay in
`us-central1` beside everything else. The transatlantic leg then happens inside Twilio's network
exactly as it would under carrier-direct SIP.

**Optimisation if a real call disappoints:** home the number in `IE1`, use a European provider
endpoint, deploy the relay in a European region. **Two unverified caveats:** OpenAI's European
processing requires abuse-monitoring approval plus a Modified Retention amendment, and tracing for
`/v1/realtime` is documented as not EU-residency compliant. Do not plan on this path until both are
checked.

**What telephony buys:** hands-free with the screen off — pocket, car, Bluetooth — as native OS
behaviour; `"Hey Siri"` with zero integration; and cost bounded by call duration, so a forgotten
open microphone is impossible. Concurrency is bounded by the provider's per-account session limit
(unverified, §9).

## 6. Cost

| Provider | Model | Rate |
|---|---|---|
| OpenAI | `gpt-realtime-2.1` | audio **$32 in / $64 out**, text **$4 in / $24 out**, cached **$0.40** (audio and text alike) / 1M |
| xAI | `grok-voice-think-fast-2.0` | **$0.08 / min** ($4.80 / hr) audio + $0.004 / 1M text input |

Audio tokenizes at roughly 600 tokens per minute of user speech and 1,200 per minute of model
speech — ~$0.019/min while the user talks, ~$0.077/min while the model talks. A typical agent lands
at **$0.06–0.11/min on `gpt-realtime-2.1`** and **$0.02–0.05/min on `-2.1-mini`** once caching works.

**The text legs are not decoration.** `gpt-realtime-2.1` prices reasoning tokens as text output at
$24/1M, so §4.3's reasoning-effort lever — the first knob this design reaches for when persona
drift or tool discipline degrades — is paid there and nowhere else. `ask_alek` results injected as
`function_call_output` are text input at $4/1M, which is why §4.2 can afford to over-forward. A
cost model built on the audio rates alone would miss both, and §4.12 would ship a ledger that
under-reports the one dimension Phase 0 is tuning.

**The providers are in the same band and the mini tier is cheaper than either.** The real distinction
is **predictability, not price**: xAI's flat per-minute rate is immune to conversation length and
cache behaviour, OpenAI's grows with context and depends on hit rate. Not enough to settle the
provider choice on its own — §7's spikes decide it on latency, format handling and
late-`function_call_output` support.

**Worth measuring before optimizing:** an Alek invocation is *text* (Router → Smart on `gpt-5.4-mini`
with delegation) while the conversation around it is *audio*. One call to Alek may cost less than
the twenty seconds of speech it interrupts — in which case §4.2's economy concern dissolves and the
answer is to delegate liberally.

**Carrier cost is a separate, much smaller line, and §4.6 doubles it.** Every call is two legs — the
inbound dial we hang up on, and the callback we place — priced per minute by the carrier, not by the
model. The inbound leg lasts one sentence; the callback lasts the conversation. Against $0.06–0.11
per minute of model time, a second leg of PSTN minutes is noise, but it belongs in the ledger as
carrier spend rather than being folded into the audio rates.

Mitigations otherwise: server-side VAD so silence is not streamed; prompt caching (the biggest
lever, 80×); provider-native context truncation if it exists (§9).

**Pricing audit gap.** `make check-pricing`'s sources (LiteLLM, models.dev) may not carry realtime
audio rates at all, so `domain/billing.py`'s audio legs would rot silently — the exact failure this
repo has paid for twice. Either extend the script's sources or carry the audio rates as a dated
`PRICE_SCHEDULE` entry, which by repo rule outranks the catalogs anyway. Decide during slice 1,
with the rate legs.

## 7. Implementation plan

**Phase 0 — spikes before code.** Each is cheap and each can invalidate a design detail.

| # | Spike | Why it can change the design | Gates |
|---|---|---|---|
| 0.1 | **Late `function_call_output`** on both providers — submit a tool result 20–30s later, with intervening turns, confirm coherent incorporation | Load-bearing assumption of §4.7. If it fails, `ask_alek` changes shape. **Do this first anyway** — it is the only spike that can redraw the design rather than tune it. | Slice 2 |
| 0.2 | **μ-law end to end**, Twilio → relay → provider → back, both providers, no transcoding | OpenAI sets format nested (`session.audio.input.format`) with reports of silent reversion to `pcm16`; xAI takes `audio/pcmu` | Slice 1 |
| 0.3 | **Latency** on a throwaway echo relay — media round trip; and once slice 2 exists, question → `ask_alek` → Router → Smart → spoken answer | The number that decides whether the feature is pleasant | Slice 1 |
| 0.4 | **Reasoning effort** — where retention holds against latency for a phone call (§4.3), reporting **cost** alongside both (§6) | Sets the session default | Slice 1 |
| 0.5 | **Native context truncation** — offered by either provider? | Makes §4.9's dropped tiering safe or not | Slice 1 |
| 0.6 | **Callback round trip** — dial, hear the hand-off, measure wall time to the callback being answered on a real handset; and whether Twilio's machine detection reliably separates voicemail from a person (§4.6) | Identity rests on the callback. If the round trip is intolerable in the car, the fallback is `<Gather>` DTMF on the inbound leg — a different identity mechanism, not a tuning knob. If machine detection is unreliable, voicemail greetings reach long-term memory | Slice 1 |

**Three vertical slices.** Each ends in something a person can do with a telephone, and no slice
ships a component whose only caller is a later slice. The transport-independent core cannot be
exercised on its own — without a carrier there is no audio, and without an identity path there is no
prompt to assemble — so "core first, transport later" would be six components with nothing to run
them.

**Slice 1 — you dial, Lelik calls back, you talk, and a summary lands in memory.** No `ask_alek`, no
Slack trigger. This slice is large because it is irreducible: a phone call that authenticates and
remembers is the smallest thing worth having.

1. `domain/` foundations: audio-frame value object (§4.4), `AuthDecision` (§4.6).
2. `RealtimeSessionPort` + one provider adapter, wire tests at the SDK boundary: session lifecycle,
   audio frames both directions, usage events, `PROMPT_CACHE_BOUNDARY` stripped (§4.4).
3. The relay as its own Cloud Run service (§4.14: `--timeout=3600`, own region and `min-instances`
   decision, own entrypoint) — plus everything a second deployable needs and nothing in this repo
   currently has twice: cloudbuild config, Dockerfile/entrypoint, service account and IAM (Secret
   Manager for provider and carrier credentials, an OIDC identity for calling the main service),
   Logfire token, health check, a `make` target, and a row in the deployment docs. Two new pinned
   dependencies (`twilio`, a WebSocket client), neither currently in `requirements.txt`.
4. **Two Twilio voice webhooks on the main service, deliberately distinct** (§4.6): the *auth*
   webhook that answers an inbound dial, decides, and hangs up; and the *answer* webhook the
   callback points at, which resolves the ticket and returns the session TwiML. Plus the Media
   Streams handler and ticket exchange (§4.5), the opaque-handle store and its TTL, and number and
   region configuration (§5).
5. **Origination** (§4.13): `TelephonyPort` + `LelikAgent` (descriptor `internal=True`) whose
   `execute()` places the callback, carrying `purpose` on the session config; **machine detection on
   the outbound call**, hanging up on voicemail (§4.6).
6. The rest of §4.6's identity path: `add_platform_id(…, "phone", …)` + OTP at binding + allowlist +
   session-opened notification + refused-dial alerting + the one-call-per-user marker (§3).
7. `VoiceSessionService` (§4.5) — relay loop, call-scoped buffer, lifecycle, `RequestContext`
   invariant (§4.11), failures to `AlertSinkPort`.
8. Lelik's persona: read-side toggles passed directly (§4.8), prompt via `PromptBuilder` written
   against `feedback_prompt_anchors.md`. **Firestore artefacts are part of this item** — blueprint
   plus tokens, uploaded by hand (an AI may not run the uploader), with the new token listed in the
   blueprint's `class_order` or it renders as nothing, and `build_for_agent` failing closed on
   every call until the upload happens.
9. End-of-call summary (§4.9): a `"voice"` entry in `CompanionExtractorRunner._EXTRACTORS`, its own
   consumer, its own Firestore prompt artefacts on the same terms as item 8, written and delivered
   through `UserNotificationService`; dropped-call path included.
10. Billing and observability (§4.12): all four rate legs in `domain/billing.py`, turn segmentation,
    usage and turns buffered by the relay and written by the main service. **Here, not later** — the
    first call already burns audio in both directions and reasoning tokens on top, so a slice that
    can hold a conversation can already spend invisibly. The callback's extra leg is carrier cost,
    not model cost (§6).
11. Verify a spoken conversation's summary consolidates into facts by the ordinary path — no
    voice-specific consolidation protocol.

**Slice 2 — the call reaches Alek.** Gated by 0.1.

1. `CallControlPlanePort` + adapter (§4.5) and the main-service entry points behind it: the
   session-config exchange moves behind the port, `ask_alek` resolves through the Router (§4.7),
   `send_to_chat`, transcript submission.
2. Relay-attached call context and the full §4.7 corner-case table, including injection sequencing.
3. The §4.10 delivery policy, including the structural `link_list`/`rich_content` rule and rule 5's
   asymmetry (a structural copy already sent is not retracted).
4. Nothing new in billing: Alek's own leg is already priced per model by `TokenLedger`, and slice
   1 built the realtime legs. Confirm a call spanning both bills each at its own rate (§8).

**Slice 3 — a second way to ask for the call.** Deliberately small, and independent of slice 2: a
Slack "call me" command reaching the same `LelikAgent.execute()` with no inbound leg. Origination
already exists from slice 1, so this is an entry point, not a capability. It can land any time after
slice 1; it is a slice rather than an item of slice 1 only because slice 1 is already the irreducible
unit and this is genuinely optional.

## 8. Test plan

- **Router, not Smart:** `ask_alek` dispatch calls `enrich_context` and never reaches
  `SmartResponseAgent` directly (§4.7).
- **Live delivery:** the `ask_alek` path does not go through `enqueue_agent_task` /
  `UserNotificationService` as its primary delivery, and a completed answer reaches the live session.
- **Stateless Alek:** two successive `ask_alek` calls in one call each carry relay-attached context,
  and the second works with the prompt token absent — the mechanism must not depend on Lelik
  remembering.
- **Corner-case table as tests** (§4.7): call ends mid-flight → task cancelled, answer discarded,
  nothing posted; injection deferred while a response is active; two in-flight calls matched by
  `call_id`; timeout produces a spoken failure rather than silence; no retry on transport failure.
- **Port/adapter:** wire tests at the SDK boundary per `ADAPTER_WIRE_TESTING.md` plus contract
  validators in `tests/contracts/adapter_contracts.py` — session lifecycle, tool-call event
  translation, **late `function_call_output` submission**, audio-frame translation both directions,
  usage events, and `PROMPT_CACHE_BOUNDARY` absent from the `instructions` the adapter sends.
- **Architecture rules** (tested, not preferences): carrier-side handler and provider adapter never
  import each other (`REQ-ARCH-08`, `-23`); `services/` never imports `agents/`, so the summarizer
  goes through its port (`REQ-ARCH-01`); the relay makes no direct HTTP call (`REQ-ARCH-18`); the
  relay never imports `UserNotificationService` (`REQ-ARCH-22`); no `*Agent` class outside `agents/`
  and every one inherits `BaseAgent` (`REQ-ARCH-30`, `-03`); the audio-frame type lives in `domain/`
  and the port imports no other port (`REQ-ARCH-06`, `-07`); the Media Streams handler imports no
  port (`REQ-ARCH-25`).
- **`RequestContext` lifetime** (§4.11): entered once per call from the `AuthDecision`; two
  concurrent simulated calls from different users never observe each other's identity; no context is
  entered for a refused call.
- **Identity** (§4.6): a dial from an unbound number is rejected and alerted **before any callback
  is placed**; a dial from a bound number opens no session on the inbound leg and places exactly one
  outbound call, to the bound number and not to the `From` header; the answer webhook refuses a
  ticket that is unknown, expired, or already consumed; the outbound call's answer-URL is not the
  auth webhook's route (the loop guard); machine detection hangs up without opening a session or
  writing a summary; binding without OTP is refused.
- **Chat policy** (§4.10): a plain answer posts nothing; an answer with non-empty `link_list` posts
  regardless; `send_to_chat` posts on request; the end-of-call summary posts once; an answer
  arriving after call end is neither spoken nor summarized — and if rule 2 had already sent a
  structural copy, that copy stays sent.
- **Billing:** all four rate legs priced by `domain/billing.py` (not duplicated in the relay) and
  reported through `QuotaService.record_usage` **by the main service**, from usage the relay
  submitted with the transcript; the relay itself touches neither Firestore nor BigQuery; a call
  spanning Lelik plus an Alek delegation bills each at its own rate; `_EXECUTION_LEDGER` untouched.
- **Summary path:** a synthetic call yields a summary written to the primary-channel session as the
  established `user` + `model` pair, delivered once, consolidated by the ordinary path; a *dropped*
  call still summarizes; the `"voice"` extractor's records are discarded and nothing reaches the
  `CompanionRecord` store.
- **Concurrency guard** (§3): a second call from a user with one in flight is refused at
  `AuthDecision`, and the marker is released by transcript submission or by TTL after a relay
  crash.

## 9. Open questions

1. **Does a late `function_call_output` work on both providers** (§4.7) — documented by OpenAI,
   undocumented by xAI. Phase 0.1, the highest-stakes unknown here; gates slice 2.
2. **Does μ-law hold end to end on OpenAI's GA API** (§5.2) — Phase 0.2, gates slice 1. (The xAI
   half is closed.)
3. **Measured relay latency**, and end-to-end time-to-answer once slice 2 exists — Phase 0.3.
4. **Which provider and tier** (§6) — decided by Phase 0 on latency, format handling and
   late-tool-result support, not price, which is a wash.
5. **Where reasoning effort should sit** for a phone call, and what it costs on the text-output leg
   (§4.3, §6) — Phase 0.4.
6. **Does 128K remove the need for in-call tiering** (§4.9) — provisional on Phase 0.5 and on real
   token growth on a long call.
7. **Is the callback tolerable, and is machine detection reliable** (§4.6) — Phase 0.6, gates slice
   1. A "no" on the first sends identity to `<Gather>` DTMF on the inbound leg; a "no" on the second
   puts voicemail greetings into long-term memory.
8. **Provider's per-account concurrent realtime session limit** — unverified. (Per-*user*
   concurrency is settled: one, enforced at `AuthDecision` per §3.)
9. **`min-instances` for the relay service** (§4.14) — the main service is held warm by scheduler
   pings today, so this is a question about the relay alone.
10. **Is European processing available on the owner's account** (§5.2) — gates only the optimisation
    path.
11. **Does §4.12's turn segmentation survive real barge-in** — if the boundaries produce unusable
    rows, a voice-specific store is the fallback.
12. **Where audio pricing is audited** (§6) — extend `make check-pricing` sources, or a dated
    `PRICE_SCHEDULE` entry. Four legs now, not one.
13. **Does reusing `CompanionExtractorPort` for the voice summarizer hold** (§4.9) — the two known
    frictions are the runner's hardcoded `TUTOR_EXTRACTOR.timeout_ms` and `companion_type` naming.
    If either turns out to be structural, the fork gets its reason written down; "the existing
    consumer persists" is not one.

## 10. Rollback

Mostly self-contained, and the relay being its own Cloud Run service (§4.14) makes it more so:
deleting that service removes the entire audio path with no effect on Slack or Telegram.

New and removable: `LelikAgent`, `RealtimeSessionPort` + adapter, `CallControlPlanePort` + adapter,
`TelephonyPort` + adapter, `VoiceSessionService`, the Media Streams handler, the voice summary
consumer, the main-service control-plane entry points, §4.6's identity path, the relay's own
deployment artefacts.

Additive, altering no current behaviour: the four realtime rate legs in `domain/billing.py`; a
`"voice"` entry in `CompanionExtractorRunner._EXTRACTORS` and one method on
`UserNotificationService`; one summary message per call into an existing channel session; one
platform key (`"phone"`) in the existing platform map.

**Rolled back by slice.** Slice 3 comes out by deleting one Slack command; dialling still works.
Slice 2 comes out and leaves slice 1's standalone companion. Only slice 1's removal takes the
feature with it — and because §4.6 makes origination the way every call starts, `TelephonyPort` and
`LelikAgent` belong to slice 1, not to the deferred outbound work.

**Not rolled back by deleting code:** Twilio number/region configuration and any provider-side EU
arrangement live outside the repository — the one place this design still reaches past its own
perimeter, worth noting precisely because §5 rejected carrier-direct SIP for having that property
pervasively rather than marginally.
