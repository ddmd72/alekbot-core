# RFC: Voice Companion — a spoken front desk for the existing agent stack

**Status:** Proposed — web transport first, telephony second (§5)
**Date:** 2026-08-15
**Owner:** Dmytro
**Milestone:** New interaction surface — voice

**Related:** `AGENT_NOTES_RFC.md` (considered and rejected for the summary hand-off, §4.7), `PLATFORM_SESSION_ISOLATION_RFC.md` (per-channel `session_id`), `STANDING_DIRECTIVES_RFC.md` (delegation discipline, §4.2), `REMOTE_MCP_SERVER_RFC.md` (the other surface that lifts a capability out of the chat stack)

---

## 1. Problem

alekbot is reachable only as text. The exocortex premise — think out loud, the system remembers — is badly served by typing, and speaking is the natural input mode we do not support.

Three obstacles block the obvious solutions.

**Slack huddles are a closed box.** No API starts a huddle, no app can join one, and there is no audio or transcript endpoint. The only huddle surface is the read-only `user_huddle_changed` event and a shareable link. Third-party "huddle APIs" capture system audio through a desktop SDK on the user's machine — inapplicable to a Cloud Run service. A huddle can be a launcher, never a transport.

**Native voice messages are async.** Telegram supports hands-free recording (hold, swipe to lock); Slack requires hold-to-record on mobile. Both are push-to-talk exchanges, not conversation. Worth having (§3) but not an open-ended spoken session.

**Alek is the wrong shape for live voice, in two ways.** His answers take seconds — Router → Smart → specialist delegation → RRF memory search — and multi-second silence kills a spoken exchange. And his output is *written for reading*: markdown, tables, links, report URLs. Reading a URL aloud is not an answer.

## 2. Goals

1. An open-ended spoken conversation with a responsive persona.
2. The full existing Alek — memory, agents, delegation — reachable from inside it, unchanged.
3. Alek's reading-optimized output delivered to a reading surface, not mangled into speech.
4. What was said reaches long-term memory, so speaking makes the exocortex smarter the way typing does.
5. Reuse the existing session, prompt, notification, and consolidation machinery instead of building a parallel stack.

## 3. Non-goals

- **Voice messages in Slack/Telegram.** A separate, much cheaper feature: the `AudioTranscriptionPort` DI chain already exists end to end with `audio_service=None` (`src/ports/audio_transcription_port.py` documents the three wiring steps). It covers hands-free capture that this RFC may not reach depending on §5, so it should ship independently rather than wait.
- Persisting raw voice sessions (§4.7 — deliberate).
- Multiple concurrent sessions per user.
- **Outbound telephony** (Alek calling the user, e.g. for reminders) — a separate capability with its own port and RFC (§5.2), not a transport for this one.
- A native mobile client. Named here only because it is the fallback the owner would consider if both browser and telephony disappoint — not planned.
- Prod rollout. Dev-only and experimental.

## 4. Key design decisions

### 4.1 Lelik is a front desk for Alek, not a second agent

The persona is the architecture. **Lelik is not an intelligence in his own right — he is a voice assistant *to* Alek.** His job is to hear the request, clarify it if it is ambiguous, forward it to Alek, and keep the conversation alive while the answer is prepared.

**Alek is never spoken.** He answers in text, and Lelik relays it.

This framing exists to solve a specific failure mode (§4.2), but it pays twice more:

- **Clarification before forwarding is an upgrade, not a workaround.** Today a vague request reaches Smart, which either guesses or asks back over a slow round trip through the channel. Lelik can resolve the ambiguity in two seconds of speech and forward a *well-formed* query. This is better than the text path does.
- **Attribution becomes in-character.** A front desk naturally says "Alek says…". The boundary between Lelik's own read and Alek's grounded answer stops being an imposed rule and becomes part of the role — and rules that match a character survive far better than rules bolted onto one.

Latency stops being a defect and becomes the role's purpose. The Realtime API's async function calling is built for this: the model keeps speaking coherently while a long call is pending.

**Note the wording.** The instruction is *narrate what is happening* — "asking Alek about your mail from last week" — not *entertain*. Entertainment as a stated goal produces filler for its own sake; narration fills the same silence, and as a side effect lets the user hear whether the request was understood **before** the answer arrives. That is a free correctness check.

**Rejected — two realtime sessions, Lelik and Alek as two voice models.** Gives genuine cross-talk, but Alek stops being Alek: a realtime model has different weights and no access to the agent stack, so the thing holding the memory is exactly the thing replaced. Doubles audio cost and adds audio routing between peers.

**Rejected — TTS over Alek's answer in a second voice.** Preserves the real Alek and still gives two voices, but fails on content: markdown tables and URLs read aloud are unusable.

### 4.2 Delegation is defined by capability, not by difficulty

The naive rule — "call Alek when the question is hard" — fails predictably in both directions. Calling on every utterance is wasteful; leaving it to the model's discretion means it will almost never call, because LLMs are poor judges of their own competence and will answer confidently instead.

The durable boundary is not difficulty but **possession**: Lelik has no memory, no mail, no tasks, no search. "Do I have this information?" is a presence check, not a self-assessment, and models are markedly better at recognizing missing data than at admitting insufficient intelligence.

So the rule is *forward anything touching the user, their affairs, mail, documents, or current facts* — and §4.1 makes forwarding the default posture rather than an escalation, so nothing has to be admitted to trigger it.

**Residual risk is over-forwarding** — trivia and small talk sent to Alek too. This is the safe failure: it errs toward a correct answer at extra cost rather than toward a confident fabrication about the user's life, and tuning a rate down is easier than tuning confidence up.

**Backstops if the persona proves insufficient:**
- **Standing directives** — delegation discipline is exactly the class of behavioral rule that mechanism exists for.
- **An explicit trigger phrase** ("ask Alek") that forces a call unconditionally, so the user can always reach Alek regardless of Lelik's self-assurance. Costs nothing, closes the worst case.
- **A cheap triage hop** on the Router pattern, if discretion proves too rare. The Router already does exactly this job, and `ROUTER_COGNITIVE_PROCESS` has already been through one painful recalibration against over-escalation — the prior art is ours.

**A tension to hold:** the more context Lelik carries (§4.8), the less he will delegate, since he will believe he already knows. Continuity and delegation discipline are two ends of one lever, not independent settings.

**Persona drift is the standing hazard.** The role lives in a system prompt and a voice session is long by nature; the framing erodes as the conversation goes on and the model grows comfortable answering. This is the same class of problem as `USER_TURN_SYSTEM_ANCHOR` — read `feedback_prompt_anchors.md` and its six failure modes *before* writing Lelik's prompt, not after.

### 4.3 Alek is not told he is in a voice session

The tempting move is to inform Alek so he can adapt. **Do not.** The more he adapts toward speech, the more he degrades the artifact that reaches the reading surface — which is where his format belongs.

In v1 his prompt does not change at all. The mismatch that motivated this design dissolves once each output goes to the surface that suits it.

### 4.4 Alek is invoked through the existing path

`SmartResponseAgent` is already an orchestrator accepting a query. Lelik calls it the way `ConversationHandler` does. No new dispatch, no registration, no manifest change.

### 4.5 One new port at the provider boundary — and Lelik is not an agent

`LLMPort.generate_content(request) -> LLMResponse` (`src/ports/llm_port.py:54`) is strictly request/response. A realtime session is bidirectional and lives for minutes — a genuine new system boundary, and the provider is substitutable (Gemini has a Live API). So:

- `ports/realtime_session_port.py` — create and configure a session, receive tool-call events, return tool results, end the session.
- `adapters/openai_realtime_adapter.py` — implementation.

**The port does not carry audio.** This is easy to get wrong and was wrong in an earlier draft of this RFC. In *every* candidate transport the media bypasses our backend entirely: a browser exchanges SDP directly with OpenAI using an ephemeral key, and on a phone call the media runs carrier ↔ OpenAI while we receive only a webhook. Our service configures sessions and answers tool calls; it never proxies sound. Modeling "audio duplex" in the port would abstract something the system does not do.

`BaseAgent` does not fit either: its lifecycle is built on `_call_llm`, `_execution_billing_scope`, and `record_turn`, all request/response. By the repo's own rule — *if it doesn't extend BaseAgent, it isn't an agent* — **Lelik is not an agent.** A live voice session is a new external event source, which by the layer semantics in `CLAUDE.md` makes it a handler:

- `handlers/voice_session_handler.py` — session entry point and lifecycle.
- `services/voice_session_service.py` — prompt assembly, tool dispatch into the agent stack, summary hand-off.

### 4.6 Transport is handlers, not a port

Given §4.5, only four things differ between transports: how a session is originated, how the caller is identified, where Alek's answer is delivered, and how the session ends. Everything else — session configuration, persona, tools, dispatch into the agent stack, summary, history — is shared.

Those four decompose onto mechanisms that already exist:

- **Answer delivery** is `ResponseChannel`. The web page becomes one more implementation; telephony reuses the Slack/Telegram channels unchanged.
- **Origination and authentication** are entry points for external events, which this repo models as **handlers** — a page endpoint and a telephony webhook, both calling one `VoiceSessionService`.

So transport variation is *two handlers over one service plus the existing `ResponseChannel`*. A dedicated transport port on top of that would be a port created for cleanliness, which `CLAUDE.md` explicitly warns against. Adding the second transport later is a handler, not a refactor.

### 4.7 The raw session is ephemeral; the summary is the artifact

**The raw voice log is not persisted.** When the call ends it has served its purpose. Beyond simplicity this is a privacy gain: voice is the most intimate channel, and not holding it at rest is an advantage rather than a compromise.

**A summary is produced by a separate cheap model call** over the conversation text — not by Lelik. Two reasons, and the second is the stronger:

1. Cost. Bulk work over noisy text is what a cheap Gemini tier is for; the same shape already runs for email classification (`agent_config.py:219` pins `max_tokens` to "near Gemini limit").
2. **Accounting.** Lelik is not an agent (§4.5) — he has no `_call_llm`, no billing scope, no `record_turn`. Summarizing through him would be an LLM call outside the entire observability contour: absent from `TokenLedger`, from BigQuery, from Logfire. A separate pass is an ordinary call under the existing rules and stays inside it.

**The summary is written into the default channel's session as a message** — the mechanism `UserNotificationService` already provides, and structurally identical to the daily email review (a background job produces a digest, posts it, it lands in history, ordinary consolidation absorbs it).

**Rejected — `AgentNote` with `expires_after`.** It looked apt: the orchestrator's notepad is injected into the prompt on every request, so Alek would see the summary immediately. But consolidation is *threshold*-based, not time-based, so a time-based expiry cannot be aligned with it, and the note introduces a second lifecycle needing synchronization. A message in the session has no second lifecycle to synchronize — it lives and is consolidated under the rules everything else already follows.

**Consequently no voice-specific consolidation protocol is needed.** A well-formed summary is ordinary conversational material. The earlier plan — feed the consolidator a raw transcript under a second protocol tuned for filler, false starts, and three speakers — is dropped along with the raw log.

**The accepted cost:** with no raw log retained, summary quality is the single point where anything can be lost, and the loss is irreversible. That is why the summarizer prompt is a first-class deliverable and not a detail — and why the summarizer must never be the participant being summarized.

### 4.8 Continuity lives in the default channel, not in the voice session

Lelik starts each conversation with the default channel's session history. Since summaries of previous voice conversations are written into that same session (§4.7), **Lelik remembers previous calls** — and knows what was discussed in text — with no additional mechanism.

This is why the voice session can be disposable: continuity was never its job.

**Volume must be bounded.** Channel history grows, and in a realtime session audio and text share one window at audio prices. Lelik needs a trimmed slice — recent turns or summaries only, not the full history. Tiered history loading exists for this, but `history_recent_full_turns` is currently a single value shared across agents; Lelik will want his own. Exact shape is deliberately deferred to implementation, once the realtime session's true context economics are measurable rather than assumed.

### 4.9 Billing needs audio legs

`TokenLedger.by_model` prices text tokens per model that ran. Audio is a different rate class and is not expressible today. Without this, voice cost is invisible — the same bug class as the pre-2026-07-30 price attribution. It ships with the first phase, not after.

## 5. Transport — web page first, telephony second

Both ship eventually; the order is what matters, and by §4.6 the second one is a handler rather than a rewrite.

**The web page goes first, and not because it is easier.** The genuinely unknown thing in this RFC is not transport — it is whether the persona works at all: whether Lelik holds his role over twenty minutes, delegates at a sane rate, and does not drift (§4.2). That question is transport-independent, and a page with **one animated button** and an ephemeral-token endpoint answers it. Telephony answers none of it while costing a regulatory bundle measured in days, a number, trunk configuration, and the whole §5.4 authentication path.

Test the unknown and cheap before the expensive and predictable.

A secondary reason, the owner's: **a call is committal.** It occupies the phone for its duration — no looking anything up, no tapping, no reading Alek's answer while he speaks. For an exocortex that is a real cost, not a detail.

**Telephony follows** because it is the only transport that escapes §5.1, and because it is the natural voice mode away from a desk.

### 5.1 A hard constraint that shapes the field

**Browsers cannot capture audio in the background.** iOS Safari suspends WebRTC and Web Audio the moment the screen locks or Safari backgrounds — a deliberate platform restriction, unchanged in Safari 26 (which shipped only narrow WebRTC fixes), with an Apple developer-forum request for a background-WebRTC permission still unimplemented. Android Chrome degrades rather than works: reported hangs 2–4 minutes after screen lock, subject to per-app battery optimization.

Any browser-based transport is therefore a **screen-on** experience.

### 5.2 Candidates

**Telephony (SIP) — second.** The Realtime API supports SIP natively, including an endpoint that accepts an inbound call and configures the session answering it (Twilio Elastic SIP Trunking and Bandwidth are documented paths). Lelik gets a phone number.

> **Depends on a separate capability.** The owner independently wants **outbound** telephony — Alek calling *them*, e.g. for reminders. That is not a transport for this RFC but a capability of its own, with its own callers (reminders, notifications) and value even if Lelik never ships. It deserves its own port and its own RFC. Useful consequence: once outbound telephony exists, inbound-to-Lelik is a small addition to a live integration rather than a new project — which is another reason not to front-load it here.

- Hands-free, screen off, pocket, car, Bluetooth — native OS behavior, nothing to engineer. It defeats §5.1 outright rather than working around it.
- `"Hey Siri, call Lelik"` needs **zero integration** — it is a contact. Platform-neutral: Google Assistant shuts down from 2026-09-04 in favor of Gemini and third-party Conversational Actions were sunset earlier, so Android has no assistant path anyway — and a call needs none.
- Billing is bounded by call duration; a forgotten open microphone is impossible.
- **Concurrency is not a limit.** "Line busy" is an analog-era concept — SIP trunking advertises unlimited concurrent calls and inbound origination on one number. The real ceiling is OpenAI's per-account concurrent-session limit (unverified) and cost, which scales linearly with simultaneous callers.
- **Costs: no screen**, so Alek's answers must go to the user's chat channel via `UserNotificationService`. Plus number provisioning (§5.3) and caller authentication (§5.4).

**Web page — first.** The only candidate with a reading surface: Alek's answers render beside the conversation, so links and tables survive. v1 is deliberately one animated button — a user gesture is mandatory for microphone access anyway, so the button is a platform requirement turned into the whole interface. Auth is already solved — `/auth/login?next=…` sets an `access_token` cookie (1 h) and a `refresh_token` cookie (30 days, `src/config/auth.py:94`), and `auth_required` (`src/web/user_cabinet_app.py:57`) accepts it. Costs: screen-on only (§5.1), a WebRTC page, an ephemeral-token endpoint, and an idle auto-stop so a forgotten tab does not bill indefinitely.

**Siri Shortcut.** `Dictate Text` → `Get Contents of URL` → `Speak Text` in a loop, runnable from the lock screen. Needs **one HTTP endpoint** over the existing agent stack — no realtime model, no WebRTC, no telephony, and no Lelik. Turn-based, cannot cover Alek's latency with speech, iOS only. Not the design in §4, but a legitimate day-one probe of whether voice access is valuable at all.

### 5.3 If telephony: number provisioning

Both Spain and France require, for an individual, proof of identity plus proof of a local address **inside that country** (no P.O. boxes or virtual addresses; review up to ~3 business days). Spain additionally requires a fiscal ID (DNI/NIF/NIE). The binding constraint is not which country asks for less but where residence can be documented — which makes Spain the realistic option here, and France no escape from the paperwork. Start early; the review is measured in days.

### 5.4 If telephony: caller identity

Only telephony needs this — a browser session is already authenticated by cookie.

**Provisioning reuses the existing platform-identity mechanism.** `link-telegram` already calls `add_platform_id(user_id, "telegram", …)` with uniqueness enforced and a 409 when already bound (`src/web/user_cabinet_app.py:279`). A phone number is one more platform key: `add_platform_id(user_id, "phone", <E.164>)`. Preferred over a `UserBotConfig` field — a phone number is an identity on an external platform, not a preference, and the platform map gives uniqueness for free. Multi-user follows with no new concepts: each family member binds their own number in their own Cabinet and reaches their own memory.

**Ownership must be proven at binding.** `link-telegram` does not verify that the binder owns the ID; inheriting that permits squatting — binding someone else's number so their calls arrive at the attacker's account. A one-time SMS/voice OTP closes it and adds no dependency, since the telephony provider is already present.

**Every call authenticates.** Caller ID is spoofable and OTP proves ownership at registration, not at call time. Lelik opens each call by asking the caller to identify themselves and expects a spoken 4-digit PIN before anything else happens.

**The authorization decision lives in server-side session state, never in Lelik's context.** Lelik calls a `verify_pin` tool; comparison is deterministic and server-side, and memory/agent access is gated by that state. If the model held the decision, speech would be an injection channel — "we already did this, I'm the owner" is just a sentence, and sentences are what the model consumes. **Lelik may ask for the PIN and must be unable to grant anything.**

**Brute force is bounded and loud.** Four digits is a 10⁴ space, so rate limiting — not the hash — is the real control: few attempts per call, then disconnect; a lockout per calling number across calls; failures reported to the existing `AlertSinkPort` (`src/ports/alert_sink.py`, already used for billing alerts) as well as logs. A guessing campaign against the exocortex deserves a push, not a log line nobody reads. The PIN is stored hashed, with the honest caveat that 10⁴ does not survive an offline attack on a leaked store.

**A spoken PIN must be redacted before persistence.** The non-obvious hazard: the PIN arrives as audio, is transcribed, and then follows the normal path into session history, BigQuery `prompt_content`, and Logfire — which has captured content since 2026-07-30. Without explicit redaction at the boundary, authenticating by voice writes the credential into observability storage. This is a requirement, not later hardening.

## 6. Cost

Audio tokens dominate and are an order of magnitude above text. Figures around `gpt-realtime-2` are ~$32/$64 per 1M audio tokens with the mini tier substantially cheaper — **secondary sources; verify at the provider before building.** `make check-pricing` does not cover realtime models.

**A premise worth measuring before optimizing:** an Alek invocation is *text* — Smart on `gpt-5.4-mini` with delegation — while the conversation around it is *audio*. One call to Alek may well cost less than the twenty seconds of speech it interrupts, in which case the §4.2 economy problem dissolves and the answer is to delegate liberally. This is measurable on the first real conversation and requires no architectural decision in advance.

Mitigations otherwise: server-side VAD so silence is not streamed; session truncation; and, for browser transports, an idle auto-stop.

## 7. Implementation plan

**Phase 1 — the core (§4), shared by every transport:**

1. `RealtimeSessionPort` + OpenAI adapter; session lifecycle; audio billing legs.
2. Lelik's persona through `PromptBuilder` tokens, written against `feedback_prompt_anchors.md` (§4.2).
3. `ask_alek` as an async function call into the existing stack.
4. Default-channel history as session input, trimmed (§4.8).
5. Summary pass on a cheap tier, written into the default channel session (§4.7); verify a spoken conversation consolidates into facts by the ordinary path.
6. `VoiceSessionService` — the shared body both handlers will call (§4.6).

**Phase 2 — web transport:** the one-button page, ephemeral-token endpoint behind `auth_required`, answer panel as a `ResponseChannel` implementation, idle auto-stop. **This is where the persona gets its first real test** — Phase 3 should not start before that verdict is in.

**Phase 3 — telephony transport:** regulatory bundle and number (start the paperwork early; review is measured in days), SIP trunk, `add_platform_id(…, "phone", …)` + OTP, the §5.4 authentication path, and Alek's answers routed to an existing chat channel. Gated on the separate outbound-telephony capability if that lands first (§5.2).

## 8. Test plan

- **Port/adapter:** wire tests at the SDK boundary per `ADAPTER_WIRE_TESTING.md` plus contract validators in `tests/contracts/adapter_contracts.py`. Session lifecycle, tool-call event translation, reconnect.
- **Billing:** audio legs priced correctly; a conversation spanning Lelik plus an Alek delegation bills each at its own rate.
- **Summary path:** a synthetic conversation yields a summary in the default channel session, which consolidates into facts by the ordinary path with no voice-specific protocol.
- **Delegation discipline:** requests touching user facts reach Alek; the explicit trigger phrase forces a call unconditionally.
- **If telephony:** PIN verification is server-side and cannot be talked past; brute-force lockout fires and alerts; **the spoken PIN appears in no persisted store** — history, BigQuery, or Logfire.

## 9. Open questions

1. **Input slice for Lelik** (§4.8) — deferred to implementation, when the realtime session's context economics can be measured rather than guessed.
2. **Layer placement of Lelik** (§4.5) — handler + service is the least-surprising reading; worth a second look against the code.
3. **Which realtime model tier** — depends on latency feel and verified pricing.
4. **OpenAI's per-account concurrent realtime session limit** — unverified, and the actual ceiling on simultaneous family callers once telephony ships.
5. **Whether outbound telephony lands first** — it is a separate RFC (§5.2) and would change Phase 3 from an integration into an extension.

## 10. Rollback

Self-contained: a new port, a new adapter, a handler/service pair, and whatever §5 adds. Nothing existing changes except the additive billing legs and one summary message written into an existing channel session. Removing the entry point reverts the feature with no effect on Slack or Telegram.
