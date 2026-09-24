# RFC: Voice Companion — a spoken companion over the existing agent stack

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

### 4.1 Lelik talks from what he knows; Alek does what Lelik cannot

**Lelik is a companion on the phone who already knows the caller.** He starts every call warm
(§4.8): the caller's facts, standing rules and the recent chat with Alek are his own memory, and he
talks from it the way a friend does. **Alek is never spoken.** He is the one who reads mail, tasks
and documents, searches, and acts, and he answers in text that Lelik turns into speech (§4.3).

Why not a front desk with a small slice of context: `decisions/lelik_warm_context.md`.

What still follows from the split:

- **Latency is a role, not a failure.** The Realtime API's async function calling lets Lelik keep
  talking while a delegation (Alek's included) is pending and weave the answer in when it arrives.
- **Clarification before forwarding** is an upgrade on the text path, where a vague request reaches
  Smart and gets guessed at or asked back over a slow round trip.
- **Attribution is in character.** "Alek says…" separates Lelik's own read from Alek's grounded
  answer.

**Character is shared and composed, not written for Lelik.** Lelik's prompt profile uses the same
overridable slots as Smart's (`ARCHETYPE_*`, `VIBE_*`, `VOICE_*`, `HUMOR_*`, `LANG_*`, the
`POLICY_*` set). Prompt overrides are per user and matched by class+category, so a persona or
language change the owner makes applies to both agents. Lelik has only two tokens of his own: his
role (`COGNITIVE_PROCESS_LELIK`) and the medium (`SPOKEN_DELIVERY`: turn-taking, giving the floor,
barge-in, repair, opening and closing a phone call). The second has its own category, so a user's
`VOICE_*` choice never replaces the phone mechanics.

**Rejected — two realtime sessions (Lelik and Alek as two voice models).** A realtime model has
different weights and no agent stack: the thing holding the memory is the thing replaced.

**Rejected — TTS over Alek's answer in a second voice.** Markdown tables and URLs read aloud are
unusable.

### 4.2 Delegation is defined by possession, and Lelik possesses a lot

"Call Alek when it's hard" fails both ways: calling on every utterance is wasteful, and left to the
model's discretion it will almost never call. LLMs judge their own competence poorly and answer
confidently instead.

The durable boundary is **possession**. "Do I have this?" is a presence check, not a
self-assessment, and models recognize missing data far better than insufficient intelligence.
Lelik's prompt holds the caller's biography, directives and recent chat (§4.8). What it does not
hold, he reaches the way every agent here does: `delegate_to_specialist` over his descriptor's
`allowed_intents` (§4.7). Fast specialists answer directly: deep memory search, web search (with
the maps fan-out). **Alek is one of those specialists**, reached through `ask_alek`, for anything
that needs his full reach: mail, documents, tasks, actions, synthesis. The rule: *answer from what
is in the prompt; delegate what is not, to the cheapest specialist that holds it.* An explicit "ask
Alek" from the caller always forces `ask_alek`.

**No fabrication about the caller's life** remains the hard line: a fact about the caller that is in
neither the prompt nor a tool result is never stated.

**Backstops if forwarding discipline slips:** standing directives, the "ask Alek" trigger phrase,
raising reasoning effort (§4.3).

**Persona drift is the standing hazard.** The role lives in a system prompt and a voice session is
long by nature. It is the same class of problem as `USER_TURN_SYSTEM_ANCHOR`: read
`feedback_prompt_anchors.md` and its six failure modes *before* changing Lelik's prompt.

### 4.3 Alek stays text-shaped; Lelik does the speaking

**Alek's prompt does not change.** He is not told he is in a voice session and produces his ordinary
reading-optimized answer. The mismatch between that and speech does not dissolve — it **moves to
Lelik**, which is correct, because verbalizing on the user's behalf is Lelik's job.
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

- **`LelikAgent`** owns what agents own, built exactly the way `TutorAgent` builds it:
  - the prompt, through its own `prompt_builder.build_for_agent("lelik", …)`, over context
    assembled by a service (`LelikPersonaService` supplies facts and history, the way
    `CompanionContextAssemblerService` does for the tutor);
  - the tool set, through `coordinator.get_available_intents_for(self._descriptor)` and the shared
    `delegate_to_specialist` declaration;
  - outbound origination.
- **`VoiceSessionService`** owns the loop: two live connections, byte relay, call-scoped buffer,
  lifecycle, and forwarding the model's tool calls to the main service. It dispatches nothing
  itself (§4.7).

**Standard wherever the medium allows.** Lelik differs from a text agent in one respect only: his
LLM call is a realtime session in another process. Everything else — descriptor, allowlist, tool
declaration, dispatch, cycle guard, billing, tracing — is the path Smart, Quick and the tutor
already use. A voice-only variant of any of these is a defect, not a design choice.

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

During the call    delegate_to_specialist → CallControlPlanePort.delegate → HTTP → main service
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
`google_oauth_service.py`), so `VoiceSessionService` cannot make an HTTP call itself. Three
operations justify the contract — `fetch_session_config(ticket)` (instructions **and** tool
declarations), `delegate(call_id, tool_call, call_context)` (one model tool call, whatever its
intent), `submit_transcript(call_id, buffer)` (transcript, usage and turn segments together) — and
the substitution need is named: if the relay
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
delegation, prompt assembly and billing are untouched. **That is the answer to "what happens when
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
  → AuthDecision minted into the short-TTL ticket store (§4.5), one-call marker taken (§3),
    callback parked under the inbound CallSid
  → TwiML: <Say> "calling you back" + <Hangup>
inbound dial ENDS → INBOUND-STATUS webhook ("Call status changes" on the number), CallStatus=completed
  → parked callback claimed atomically (a retried status delivery finds nothing)
  → LelikAgent.execute(purpose="user asked to talk") → TelephonyPort → outbound call
       answer-URL carries the ticket → ANSWER webhook (a different endpoint) → session TwiML
  → relay opens the WS, exchanges the ticket, session begins
```

**The callback waits for the inbound dial to end.** The first build originated it from inside the
auth webhook. It then reached the handset exactly while the ~2 s inbound dial ("calling you back" +
hangup) was being torn down, and the carrier answered **SIP 480 Temporarily Unavailable** within a
second. The owner got a missed-call SMS. A busy line is not the cause in itself: a callback that
lands in the middle of the inbound call shows as a second incoming call. The cause is the teardown
window, and spike 0.6's four instant `no-answer`s were the same 480. A fixed delay would only guess
at that window, while the inbound call's own `completed` status marks its end. It needs one extra piece of number configuration ("Call status changes"
→ `/voice/inbound-status`). While the callback is only parked, the marker holds the ticket's short
TTL, so a status that never arrives locks the caller out for minutes, not an hour.

**Region note.** An inbound dial is processed, and its webhooks are signed, in the region the
number's *Active Region* names, and every region has its own auth token. The callback leg belongs to
the region of the REST API that created it (`api.twilio.com` = US1). v1 keeps the number's Active
Region on **US1**, so one token verifies every webhook.

**The answer-URL is not the auth webhook.** They are two endpoints. Pointing the outbound call's
answer-URL at the inbound handler is a loop that dials until something breaks — worth stating
because both are "a Twilio voice webhook on the main service" and the mistake is one config line.

**Answering-machine detection is mandatory on the callback, not optional polish.** If the owner does
not pick up, the callback lands in voicemail — and Lelik would hold a conversation with a greeting
and then summarize it into the owner's long-term memory (§4.9). Twilio's machine detection on the
outbound call is the cheap fix; the expensive failure is a fabricated memory, not a wasted leg.

*Async, and it gates memory, not the call (2026-09-25).* Slice 1 ran it synchronously and hung up on
a machine at `/voice/answer`. Live, that held every pickup for 4–5 s, and it hung up on the owner
whenever he opened with a request: Twilio calls any first utterance over
`MachineDetectionSpeechThreshold` (2.4 s) a machine greeting. Now the adapter sets `AsyncAmd`: the
call connects at once, and the verdict (`AnsweredBy`, no `CallStatus`) is posted to the same
`/voice/status?ticket=…` callback, which only records it (`voice_amd:{ticket}`). At
`/voice/submit-transcript`, `domain.voice_amd.is_voicemail` skips the summary when the verdict is
`machine_*` **and** the caller spoke in fewer than three turns — a greeting is a monologue, and it can
split into two turns while Lelik talks over it; an owner who answers Lelik gets past two. A missing
or `human`/`unknown` verdict is a live call. The one-call marker is released either way. Accepted
cost: when the owner cannot pick up, Lelik says a line or two to his voicemail, and a real call that
AMD misjudged and that stayed under three caller turns loses its summary.

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

### 4.7 Lelik delegates like every agent; Alek is one of his specialists

**One tool, the standard one.** Lelik's session carries exactly the tool every orchestrator here
carries: `delegate_to_specialist(intent, query, context)`, built by the shared
`_build_delegate_tool_declaration` from `coordinator.get_available_intents_for(LELIK)`. His
descriptor, like the tutor's, declares an allowlist:

```
LELIK.allowed_intents = {search_memory, search_web, ask_alek}
LELIK.intent_fanout   = {search_web: SEARCH_WEB_MAPS_FANOUT}   # the constant Smart and Quick share
```

Maps arrive through the same `search_web → maps_query` fan-out Smart uses. Adding a capability to
Lelik later is one allowlist entry, not a voice feature.

**Alek is a specialist behind an internal intent.** `ask_alek` is declared on an `ALEK` descriptor
(`internal=True`, `ExecutionMode.SYNC`). Its agent is a zero-LLM gateway that routes the query to
`router_agent_{user_id}` exactly as `ConversationHandler` does (`conversation_handler.py:700`).
It goes to the Router, not Smart, because `router_agent.py:332` is where `enrich_context` (RRF
memory search) runs, and Smart without it is Alek without memory.

- **Visibility — one registry rule.** Today `internal=True` hides an intent from every tool list,
  allowlist or not. The rule becomes: *internal intents are not offered by default; an explicit
  allowlist may name one.* Smart and Quick (`allowed_intents=None`) keep seeing exactly what they
  see now. Lelik names `ask_alek` explicitly. No existing allowlist names an internal intent (the
  only internal specialist intents are `maps_query`, reached by fan-out, and the bound-channel
  companions), so no current tool list changes.
- **Loops — the coordinator already refuses them.** `AgentCoordinator._refuse_if_looping` rejects
  any delegation whose target is already in the `_call_chain` riding in the context, and caps
  depth at `MAX_DELEGATION_DEPTH`. The gateway's only obligation is the one every hop has: pass the
  context, chain included, into the Router message. Visibility keeps Alek from calling himself
  once; the chain guard makes a cycle impossible even if visibility were wrong.

**Dispatch — the engine's, not a copy.** The model's tool call reaches the main service through
`CallControlPlanePort.delegate` (relay → HTTP, OIDC as `/worker`). There it runs through
`DelegationEngine`'s single-call dispatch, which the text loop already uses: remap, fan-out, mode,
coordinator, chain guard, spans, billing. That dispatch is made public for this second caller
rather than re-implemented. The context is seeded with `user_id`, `account_id` and
`_call_chain=[lelik_agent]`, and `calling_agent_id="lelik_agent"`. The result is the same
`result_str` a text orchestrator receives.

**Alek is invariant per call, and the relay attaches the call context.** Each `ask_alek` is
self-contained: nothing from one `ask_alek` is kept for the next. The gateway addresses the Router
with the primary channel's `session_id`, the one the call summary is written into (§4.9), so Alek
answers with the chat he shares with the user as **read-only** context. That is safe by
construction: only `ConversationHandler` writes session history (`conversation_handler.py:941`,
`:1049`), and Router, Smart and `BaseAgent` only read it. No in-call request ever lands in the chat
as a turn the user never typed (§4.10). Whatever the model puts
in `context`, the relay adds the call-scoped buffer's last N exchanges to every delegation as
`params.call_context`. Instruction retention is the realtime model's weakest axis (§4.3), and a
context-free question to Alek fails silently: he answers something plausible and wrong. The
`PROTOCOL_LELIK_DELEGATION` token teaches Lelik to pass context, but correctness does not depend on
that token surviving twenty minutes.

**Parallelism is the model's choice.** Several `delegate_to_specialist` calls in one response are
dispatched concurrently, as in the text loop. The protocol token says when: a lookup that a fast
specialist answers roughly and Alek answers well goes to both. Lelik speaks the fast answer as
provisional and folds Alek's in when it arrives. The relay never fans out on its own: choosing the
fast specialist is a judgment about meaning, not a rule.

**Lelik knows where he stands.** His role token states it: Alek is the user's personal exocortex, a
network of agents holding their long-term memory, mail, tasks and documents. Lelik is its voice, the
part the user talks to on the phone, sharing its memory (his `knowledge_base` is a snapshot of it).
Smart's `PROTOCOL_VOICE_PARTNER` is the mirror image on Alek's side.

**Rejected alternatives:**
- **A voice-only HTTP path to the Router** (the earlier version of this section). It worked, but it
  was a second delegation mechanism beside the one every agent uses, with its own dispatch, its own
  retry and trace shape, and no cycle guard.
- **Remote MCP tools in the realtime session.** OpenAI would call the tool servers directly from its
  own infrastructure: outside `TokenLedger`, Logfire and `RequestContext`, a second path to the same
  data. The Realtime API has no hosted tools (web search etc.) to reuse anyway.
- **Relay-side automatic fan-out** (every question to a fast specialist and to Alek). Picking the
  fast specialist is a judgment about meaning, and it doubles the spend on every turn.

**Mechanism:**

```
Lelik emits delegate_to_specialist(intent, query, context)   (one or several per response)
  → relay adds params.call_context, captures call_id
  → relay spawns a tracked asyncio task per call (held in a set — RUF006 forbids fire-and-forget)
       → CallControlPlanePort.delegate(...) → main service, RequestContext(user_id, account_id)
       → DelegationEngine single-call dispatch with LELIK's remap/fan-out → AgentCoordinator
            search_memory / search_web(+maps)  → specialist → result_str
            ask_alek → ALEK gateway → Router → Smart → full_response (+ link_list, rich_content)
       → chat delivery, if any, happens here (§4.10) — the relay never posts to chat
  → relay calls resolve_late_answer(call_id, result_str, interrupted_since_dispatch) (below)
  → Lelik verbalizes (§4.3)
```

**`resolve_late_answer` — one seam, chosen by a spike, swappable without touching the rest of
this mechanism.** Phase 0 spike 0.1 (`docs/04_solution_strategy/decisions/
voice_spike_01_late_function_call_output.md`) found the naive version of this — always submit as
`function_call_output`, trust the model to pick it up — **fails silently on OpenAI specifically
when the caller spoke again during the wait**: no error, no acknowledgment, the model just
continues the interrupting topic and the fact is gone. Absent an interruption, both providers
handle the late injection correctly.

The chosen default is deterministic, not a discretionary judgment call (§4.3's principle applied
here too), and does not depend on either provider's undocumented behavior continuing to hold
across model updates:

```
resolve_late_answer(call_id, full_response, interrupted_since_dispatch):
    if not interrupted_since_dispatch:
        conversation.item.create { type: "function_call_output", call_id, output: full_response }
        response.create
    else:
        # the tool call the model made is stale context by now — don't rely on it noticing a
        # late result tied to a call it may no longer be tracking. Surface the answer as fresh
        # information instead, uncoupled from the original tool call.
        conversation.item.create { type: "message", role: "system",
            content: f"Alek's answer just arrived: {full_response}" }
        response.create
```

`interrupted_since_dispatch` is tracked by the relay from the call-scoped turn log: true if any
new user speech was observed between dispatching the delegation and the answer arriving, regardless of
whether that speech was itself directed at Alek. This function is the one place a future model
update, a provider switch, or real Cloud-Run-build data overturning this default gets changed —
not a broader refactor. The rejected alternative (resubmitting inside the *next* `response.create`
rather than injecting a fresh system message) was not tested this session; it remains a candidate
implementation for this same seam if the chosen default proves unsatisfying against real traffic,
not a reason to reopen this decision from scratch.

Lelik keeps talking while the task runs, which is what makes §4.1's narration load-bearing rather
than decorative. "Async" here means *the provider's own late `function_call_output`* — not this
repo's `ExecutionMode.ASYNC`, which enqueues a Cloud Task delivered by `UserNotificationService` to
a chat channel. **No mechanism in `src/` returns an async result into a still-open caller session**,
so that path cannot serve this one.

**Correction from Slice 2 (Task 4 finding):** the shared declaration's `mode: "later"` does *not*
reach the chat for `ask_alek`, contrary to what this section originally claimed. `ask_alek` is a
SYNC-declared intent, and `AgentWorkerHandler` — the consumer of the Cloud Task
`ExecutionMode.ASYNC` enqueues — delivers only generator-declared (ASYNC) intents; a SYNC intent
forced into `mode: "later"` has nowhere for its answer to land and would drop it silently. So
`LelikAgent.delegate()` strips `mode` from the arguments before dispatch: on the phone, `ask_alek`
always runs synchronously regardless of what the model requests. "Send me the full report" over the
phone therefore still resolves through `resolve_late_answer` (the *provider's* async mechanism
above), not through `ExecutionMode.ASYNC`.

**Corner cases — requirements, not commentary.**

| Case | Required behaviour |
|---|---|
| Call ends while the answer is in flight | Cancel the task; **discard the answer** (it served a conversation that no longer exists). Log it. No chat fallback unless §4.10 already required one. |
| Model is mid-response when the answer arrives | `response.create` while a response is active is an error. Track `response.created`/`response.done`; queue the injection until idle. |
| **Caller speaks again while the answer is still being fetched, before it arrives** | `resolve_late_answer` (above) takes the fresh-message branch, not `function_call_output` — confirmed via spike 0.1 that OpenAI silently drops the latter in this exact case. |
| User barges in during injection itself (after `resolve_late_answer` has already queued something) | Submit the queued item immediately (harmless — it is a conversation item either way), defer `response.create` to the next idle moment. |
| Two delegations in flight | Match by `call_id`; serialize `response.create` so they cannot collide. |
| Main service slow or hung | Hard relay-side timeout, start at 90s — Router → Smart with delegation is the cost, not cold start. On expiry inject a `function_call_output` saying Alek did not answer, so Lelik says so aloud. **Silent non-delivery is the worst outcome.** |
| Transport failure | **No automatic retry** — it re-runs Alek's whole pipeline: double spend, possible double chat delivery. Fail loudly to Lelik. |
| Relay restarts mid-call (deploy) | Call drops; answer in flight lands nowhere; transcript buffer lost. Accepted. |
| Concurrency ceiling | Each in-flight delegation occupies a request slot on the 1 vCPU main service. Named, not solved, in v1. |

**Confirmed by spike, not assumed.** Phase 0 spike 0.1 ran this against both providers' live
Realtime APIs (real calls, 8-case factorial: delay × interruption × provider). Absent an
interruption, both providers pick up a late `function_call_output` coherently — the RFC's original
premise holds there. With an interruption, only xAI does (and that signal came from an
audio-transcript channel, not text — see the decision record's own caveat on that asymmetry);
OpenAI does not, which is exactly the gap `resolve_late_answer` above exists to close. Full data:
`docs/04_solution_strategy/decisions/voice_spike_01_late_function_call_output.md`.

### 4.8 Lelik's context at call start

**Lelik starts warm** (`decisions/lelik_warm_context.md`). `LelikPersonaService` assembles, at
`/voice/answer`:

| Source | What | Cost |
|---|---|---|
| Biographical cache | **every domain**, minus `UserBotConfig.voice_excluded_fact_domains` (default empty) | one cached read, the same one Smart makes |
| Standing directives | the same list, `include_directives=True` | none |
| Primary-channel history | the last 30 messages of the session the call summary is written into (§4.9): the user turn's text (capped at 500 chars) and the model turn's stored `text`, which is already its ≤300-char `response_summary`. Timestamps are in the caller's timezone | one `SessionStore` read, **no LLM call** |
| Date/time, location | `include_datetime=True`; location and timezone from the caller's own `UserBotConfig` via a per-call `UserPromptBuilder` | none |

**Why a denylist, empty by default: give everything, trim what proves out of place.** Too little
context fails silently: Lelik sounds cold or reaches for Alek, and nobody can hear a fact that was
never loaded. Too much fails audibly: he says something out of place, and that domain gets
excluded. The field is plain strings rather than `FactDomain`, so a typo in a hand-edited Firestore
document is logged and ignored instead of failing the whole config load. **No Cabinet UI yet**
(deferred; trigger: the first time a domain actually needs excluding).

**The same channel chain as the write side.** History is read through
`UserNotificationService.resolve_channel` (override → primary → last active), the chain
`notify_call_summary` writes through. A second copy of that chain could drift, and then Lelik would
read one session while his summaries land in another. Earlier call summaries therefore reach the
next call automatically.

**Not included:** RRF memory search (there is no query at call start), agent notes, and prefetched
tasks, reminders or calendar (Alek's data, reached through `ask_alek`).

**Size.** 100 facts plus 30 short history lines is a few thousand tokens, and cached input is
$0.4/1M against $32/1M uncached. The real limit is instruction retention (§4.3), which is why the
prompt carries character through shared tokens rather than a long rulebook.

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
service, which settles three things a direct `SessionStore` write leaves open: channel resolution
lives there (`resolve_channel`), `REQ-ARCH-22` forbids a new service from importing `UserNotificationService`, and the
*role* of the written message is not a free choice —
`serialize_messages_for_consolidation()` reads user and model parts differently, so the
established two-message shape is the one that consolidates correctly.

The earlier framing — "not through `notify()`, which reformats" — was a false dichotomy.
`notify()` is one method among several on that service, and the one being reused writes history
without reformatting anything.

**The note's shape.** The history gets a user turn `[System: phone call with Lelik, HH:MM–HH:MM
(N min). The note below was posted to the user's chat.]` (user's timezone, window taken from the turn
segments), and a model turn `📞 <note>`, which is exactly what was delivered. The first build wrote
a bare `[System: phone call ended]` and a third-person report ("the caller asked…"). Alek read it as
a reply of his own that he never gave. The note speaks with the shared character slots and the
user's `LANG_*` (§4.1). Its length follows what is worth keeping: one line when the call held no new
fact, decision or open request. Smart's `PROTOCOL_VOICE_PARTNER` tells Alek who Lelik is and how to
read these pairs.

**Which session, precisely.** A session is per channel (`session_id = f"{user_id}:{channel_id}"`)
and a phone call has no channel; a synthetic `phone:<E.164>` session would be *isolated* from every
chat channel, failing Goal 4. **The call writes into the session of the user's primary notification
channel**, resolved by the chain `resolve_channel` already uses — override → primary → last active
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

Most delegations are *internal* — Lelik needs data in order to speak — so copying every one
into Slack fills the channel with requests the user never made. It is also the wrong thing to ask
Lelik to judge: the user is on the phone precisely because they cannot look at a screen, so anything
delivered during the call is read *afterwards* anyway, and per §4.3 discretionary judgment held
across a long conversation is the model's weakest axis.

1. **During the call, nothing goes to chat by default.**
2. **Structural exception — reading-shaped content always goes.** Generalized beyond `ask_alek`:
   any Lelik delegation whose result contains links gets a bare-anchor chat copy, sent regardless
   of anything Lelik decided — reading a URL aloud is useless (§1), so this is a property of the
   result, not a judgment call. `LelikAgent.delegate` runs this for every specialist except
   `ask_alek`, whose own structured answer (`link_list`/`structured_data`, or — when both are
   empty — links extracted from Smart's plain text) is instead copied by `AlekGatewayAgent`, so the
   two paths never double-post. A **failed** result (rejection, exception, max retries, a fan-out
   whose primary section errored) never posts a copy — its error string can itself contain a URL
   (e.g. an OpenAI 429 pointing at `platform.openai.com`). Both copy paths are wrapped in a 5 s
   timeout: the spoken answer must not wait on a slow or hung chat delivery.
3. **Explicit request — an intent, deferred.** "Send that to my chat" becomes a `send_to_chat`
   intent on Lelik's allowlist, served by a zero-LLM agent over `notify_raw`. It is not a second
   tool: the standard one carries it. It is the easy kind of judgment, recognizing an explicit
   instruction rather than modelling the user's reading habits. **Not built in slice 2.** Rule 2
   plus `mode: "later"` (§4.7) cover the known cases; build it when a real call asks for it.
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
carrier socket drops, delegation timeouts — the same one-line shape `AgentCoordinator` and
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
relay adds one same-region hop plus frame forwarding, and turn detection stays native to the provider (we
forward bytes and never implement VAD). **"More code":** Twilio sends `audio/x-mulaw` 8 kHz mono
base64 and accepts `mulaw/8000` back, and **both candidate providers accept G.711 μ-law at 8 kHz**
(OpenAI `g711_ulaw`, xAI `audio/pcmu`) — so the relay is a base64 decode and a byte forward, no
resampling, no DSP.

**What the relay does own: barge-in and silence.** Both depend on what the caller has *heard*, and
over a WebSocket only the relay can know that: audio is written into Twilio far ahead of playback.
- **Turn detection** is `semantic_vad` at `eagerness: low`, so a pause mid-thought does not end the
  caller's turn. The provider's `interrupt_response` is **off**. With it on, the provider and the
  relay both cancelled on barge-in, and a `response.cancel` landing on nothing is a provider error
  that ends the call.
- **Playback** is measured with Twilio `mark`s. Every outbound chunk is followed by a mark named by
  the running byte total, and Twilio echoes it once the audio before it has played (μ-law 8 kHz,
  8 bytes/ms; `PlaybackTracker`).
- **Barge-in** runs in a fixed order: read the heard milliseconds of the current assistant item
  (*before* clearing, because Twilio echoes the marks of dropped audio after a `clear`), then
  `clear` → `response.cancel` → `conversation.item.truncate(item_id, heard_ms)`. Without the
  truncate, the model believes it finished a reply the caller heard half of, and cannot resume
  "from where it was cut off". **Only sustained speech barges in** (owner's call, 2026-09-25): live,
  line noise and "uh-huh"s cut replies at ~400 ms, several times a minute. `speech_started` over an
  audible Lelik now only arms a 1 s timer (`barge_in_min_speech_s`); the barge-in runs if the caller
  is still speaking when it fires. Speech that stops sooner is dismissed, and its committed turn gets
  no reply of its own — the words stay in the conversation for Lelik's next turn. Cost: interrupting
  Lelik means talking over him for a second.
- **The call opens with the caller's own request** (`caller_opening`, 2026-09-25). Before the
  pickup note the relay adds one `user` item in the owner's words: talk faster and more like a
  person, no narrator spacing inside a phrase, don't repeat one intonation in every phrase, greet
  me by name and ask how to help. Live, the same delivery rules as system text (anchor,
  `SPOKEN_DELIVERY`) left Lelik a narrator; the owner asking in-call changed him at once, and the
  model then keeps matching its own earlier turns. The first reply answers this item.
- **Every reply is started by the relay, behind a persona anchor.** `create_response` is off. On
  `input_audio_buffer.committed` the relay appends a `system` item, `build_persona_anchor(...)`
  (the same anchor the text path uses, which lists the persona sections present in the prompt,
  `spoken_delivery` among them), then sends `response.create`. Anchors are **not** deleted: a
  `conversation.item.delete` of a missing item is a provider error, and any provider error ends the
  call. The cost is ~500 cached chars per turn. Revisit if long calls show the model habituating
  to the repeated anchor.
- **Silence.** The model speaks only when a turn ends, and the provider's `idle_timeout_ms` exists
  for `server_vad` only. So the relay runs a watchdog: once Lelik's audio has finished *playing*,
  no response is active and the caller is not speaking, 8 s of quiet injects one system note and a
  `response.create` (`SPOKEN_DELIVERY` answers it with a single light check). If that check is met
  with another 20 s of silence (`hangup_after_silence_s`) and no delegation is out, the relay ends the
  call: the session returns, the media handler closes the stream, and with no TwiML after `<Connect>`
  Twilio hangs up — a voicemail box or a phone put down no longer holds the line. It re-arms only when
  the caller speaks — **except while a delegation is pending**, when it re-arms on its own and keeps
  firing a "keep the caller company" note instead (`SPOKEN_DELIVERY`'s `giving_the_floor` allows a
  few sentences here), so a slow specialist never leaves dead air. It stands down while an answer is
  mid-injection (`answers_in_flight`), so an arriving answer always wins the reply slot instead of
  being buried under a waiting note. A **late** answer — injected after the caller has spoken again
  — names the request it answers (`intent: query`, truncated) rather than reading as a reply to
  whatever the caller just said; a late **timeout** uses distinct wording ("got no answer in time")
  so it never reads as an answer that "just arrived". A tool call belonging to a response the caller
  has already barged into is answered with a fixed "not run" output and never dispatched — running
  it would spend real specialist time (and money) on half a question.
- **Thinking cue — built, currently OFF.** With it on, Lelik's replies were cut after ~0.4–0.8 s
  on the live calls of 2026-09-24, and the cuts stopped when the relay was routed back to a
  revision without it; the cause is not found yet, so `relay_main.py` does not pass the clip.
  Design as built: a silent gap reads as a dropped line, so the relay fills it with a quiet
  breath-pulse loop (`src/assets/voice/thinking_cue.ulaw`: 2 s of **G.711** μ-law 8 kHz — encode it
  with a standard encoder such as ffmpeg `pcm_mulaw`, never a home-made companding curve, whose
  silence bytes decode near full scale and whistle on the line; two soft puffs,
  ~25 dB under speech — at the edge of audibility, the owner's call after the first live call;
  picked by ear from band-limited candidates, 2026-09-24). It plays when
  something is owed and nothing is audible: a reply to the caller or to an arriving answer has
  started but produced no audio yet, or a delegation is out and playback has caught up — never
  before the pickup greeting or the watchdog's own notes, never while the caller speaks. It starts
  after 0.7 s, so fast replies carry none. The cue goes to Twilio as `media` **without a `mark`**:
  `PlaybackTracker` never counts it, so the watchdog and barge-in's `heard_ms` see only real speech.
  It is paced in real time in 20 ms frames with ~100 ms lead, and whatever is still queued is
  `clear`ed just before Lelik's first audio frame (or when the caller starts talking), so it never
  delays or overlaps his reply. The relay logs `first audio after N ms` per response, which is the
  thinking gap the cue covers.

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
drift or tool discipline degrades — is paid there and nowhere else. Delegation results injected as
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
8. Lelik's persona: warm context assembled by `LelikPersonaService` (§4.8), prompt composed from
   Smart's shared slots plus two Lelik tokens (§4.1), written against `feedback_prompt_anchors.md`. **Firestore artefacts are part of this item** — blueprint
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

**Slice 2 — Lelik delegates.** Gated by 0.1 (done). One slice: once the standard path exists,
each specialist is an allowlist entry, not a feature.

**Status (2026-09-24):** code complete on `feat/voice-companion` — items 1–7 below implemented and
`make check` green. Live verification (a real call exercising `ask_alek`) is pending the Slice 2
live calls (2026-09-24 build); see §9 items 3, 14, 15 for the open questions that depend on it.

1. **Standard pieces, made shareable rather than copied.** `DelegationEngine`'s single-call
   dispatch becomes public for a second caller. The `search_web → maps_query` `FanoutSpec`, today
   duplicated verbatim in `QUICK_RESPONSE` and `SMART_RESPONSE`, becomes one constant. The registry
   rule changes to "an explicit allowlist may name an internal intent" (§4.7).
2. **Lelik as a standard agent.** `LELIK` gains `allowed_intents` and `intent_fanout`, and
   `LelikAgent` builds the prompt (over `LelikPersonaService`'s context) and the tool declaration the
   way `TutorAgent` does. The session config carries both, and `RealtimeSessionPort.open` translates
   the neutral declaration to the provider's function shape.
3. **Alek as a specialist.** `Intent.ASK_ALEK` on an internal `ALEK` descriptor. Its zero-LLM
   gateway routes to the Router with the context and chain intact, and returns `full_response` plus
   `link_list`/`rich_content`.
4. **The relay forwards, the main service dispatches.** `CallControlPlanePort.delegate` and a
   `/voice/delegate` endpoint (OIDC). `VoiceSessionService` turns `tool_call` events into tracked
   tasks, attaches `params.call_context`, and hands results to `resolve_late_answer`. The full
   corner-case table applies (§4.7).
5. **Prompts.** `COGNITIVE_PROCESS_LELIK` states Lelik's place in the exocortex (§4.7) and a new
   `PROTOCOL_LELIK_DELEGATION` covers which intent, when to call in parallel, and how to speak a
   provisional answer. Smart's `PROTOCOL_VOICE_PARTNER` drops its "no tools yet" line, and
   `capabilities.py` follows.
6. **Chat delivery (§4.10):** rule 2's structural copy — generalized to any Lelik delegation with
   links, not only `ask_alek`'s `link_list`/`rich_content` — and rule 5's asymmetry (a copy already
   sent is not retracted). `send_to_chat` stays deferred.
7. **Nothing new in billing.** Specialists and Alek are priced per model by `TokenLedger` as in text,
   and slice 1 built the realtime legs. Confirm a call spanning both bills each at its own rate (§8).

**Slice 3 — a second way to ask for the call.** Deliberately small, and independent of slice 2: a
Slack "call me" command reaching the same `LelikAgent.execute()` with no inbound leg. Origination
already exists from slice 1, so this is an entry point, not a capability. It can land any time after
slice 1; it is a slice rather than an item of slice 1 only because slice 1 is already the irreducible
unit and this is genuinely optional.

## 8. Test plan

- **Standard delegation** (§4.7): Lelik's tool declaration is the shared
  `_build_delegate_tool_declaration` over `get_available_intents_for(LELIK)`, and contains exactly
  his allowlist. `/voice/delegate` dispatches through `DelegationEngine`'s public single-call
  dispatch, so `search_web` fans out to `maps_query` exactly as for Smart. Smart's and Quick's tool
  lists are unchanged by the registry rule (no `ask_alek`). An explicit allowlist can name an
  internal intent, and `allowed_intents=None` still cannot see one.
- **Cycle guard reaches through the gateway:** a delegation chain `lelik_agent → alek` that tries to
  re-enter `alek` is refused by `_refuse_if_looping`, which proves the gateway propagates
  `_call_chain` into the Router message.
- **Router, not Smart:** the `ask_alek` gateway calls the Router, where `enrich_context` runs, and
  never `SmartResponseAgent` directly (§4.7).
- **Live delivery:** the `ask_alek` path does not go through `enqueue_agent_task` /
  `UserNotificationService` as its primary delivery, and a completed answer reaches the live session.
- **Stateless Alek:** two successive `ask_alek` calls in one call each carry the relay-attached
  `params.call_context`, and the second works with the prompt token absent: the mechanism must not
  depend on Lelik remembering. No in-call turn is written to any chat session.
- **Parallel delegations:** two tool calls in one response are dispatched concurrently and resolved
  independently by `call_id`.
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
- **Chat policy** (§4.10): a plain answer posts nothing; any delegation result with links posts a
  bare-anchor copy regardless (a failed result never does); the end-of-call summary posts once; an
  answer arriving after call end is neither spoken nor summarized — and if rule 2 had already sent a
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
   **Answered (2026-09-21), conditionally:** yes absent an interruption, on both providers; on
   OpenAI, no, if the caller spoke again during the wait — silently dropped, not an error. Design
   response is §4.7's `resolve_late_answer`. `decisions/voice_spike_01_late_function_call_output.md`.
2. **Does μ-law hold end to end on OpenAI's GA API** (§5.2) — Phase 0.2, gates slice 1. (The xAI
   half is closed.)
   **Answered for OpenAI (2026-09-21):** yes, clean on a real call, after fixing four unrelated
   infra bugs (TwiML Bins are US1-region-only, `websockets`' HTTP parser requires GET not POST, a
   stale session schema, a local TLS proxy issue) — none about the audio format itself. **xAI's
   live audio leg was never run** — this is now the only genuinely open half.
   `decisions/voice_spike_02_mulaw_e2e.md`.
3. **Measured relay latency**, and end-to-end time-to-answer once slice 2 exists — Phase 0.3.
   **Answered, partially (2026-09-21):** echo-relay p50 681ms / p95 910ms on OpenAI, over a
   developer laptop + ngrok, not the eventual Cloud Run topology — treat as "not disqualifying,"
   not a production number. The end-to-end (`ask_alek` included) figure is still open; slice 2 now
   exists in code, but the number itself is pending the Slice 2 live calls (2026-09-24 build).
   `decisions/voice_spike_03_latency.md`.
4. **Which provider and tier** (§6) — decided by Phase 0 on latency, format handling and
   late-tool-result support, not price, which is a wash.
   **Deferred by owner decision (2026-09-21), not resolved by Phase 0 data.** Three of six spikes
   only ran against OpenAI; the one spike testing both (0.1) found xAI more tolerant of the
   interrupt case, but that signal came from a non-parity audio-transcript channel, not text — it
   doesn't cleanly outweigh OpenAI's much larger tested surface. Owner's call: pick the provider
   empirically while building Slice 1, not from this data; run the missing xAI-parity spikes only
   if that build surfaces a concrete reason to.
   `docs/superpowers/plans/2026-09-20-voice-companion-phase0-spikes.md` (closing section).
5. **Where reasoning effort should sit** for a phone call, and what it costs on the text-output leg
   (§4.3, §6) — Phase 0.4.
   **Answered (2026-09-21):** 5/5 tested levels retained a planted fact on a short text-only probe;
   the spike's own data-driven pick was `minimal` (cheapest, no retention cost). **Owner's shipping
   default is `medium`** — deliberate margin against the real long-call retention risk this short
   probe doesn't cover, and consistent with this repo's already-validated `medium` default for
   Smart. `decisions/voice_spike_04_reasoning_effort.md`.
6. **Does 128K remove the need for in-call tiering** (§4.9) — provisional on Phase 0.5 and on real
   token growth on a long call.
   **Answered (2026-09-21):** OpenAI's Realtime API has native truncation on by default
   (`truncation: "auto"`) — a free backstop as long as Slice 1 leaves that field untouched; xAI's
   realtime endpoint has no equivalent. §4.9's "no custom tiering in v1" plan needs no change.
   `decisions/voice_spike_05_native_truncation.md`.
7. **Is the callback tolerable, and is machine detection reliable** (§4.6) — Phase 0.6, gates slice
   1. A "no" on the first sends identity to `<Gather>` DTMF on the inbound leg; a "no" on the second
   puts voicemail greetings into long-term memory.
   **Answered (2026-09-21):** yes to both, on a small real sample — 3/3 person-answer trials at
   ~10.3s mean round trip (owner's own read: "instant"), 2/2 forced-voicemail trials correctly
   detected and hung up without opening a session. §4.6 proceeds as designed; no DTMF fallback
   needed on this data. `decisions/voice_spike_06_callback_roundtrip.md`.
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

14. **Several function calls in one realtime response** (§4.7 parallelism). Spike 0.1 exercised one
   call at a time. Confirm that `gpt-realtime-2.1` emits several `function_call` items in one
   response and accepts their outputs out of order. If it does not, parallelism degrades to
   sequential calls; nothing else in §4.7 changes. **Pending the Slice 2 live calls (2026-09-24
   build)** — no number exists yet.
15. **Fast-specialist latency on a call.** `search_web` runs at BALANCED today (ECO only for
   `fetch_url`). Measure spoken time-to-answer on the first slice 2 calls before tuning. If it is
   too slow, `LELIK.intent_remap` can point it at a faster variant, the same lever Smart already has.
   **Pending the Slice 2 live calls (2026-09-24 build)** — no measurement exists yet.


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
