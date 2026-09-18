# RFC: Voice Companion — a spoken front desk for the existing agent stack

**Status:** Proposed — telephony-only v1 (revised 2026-09-18)
**Date:** 2026-08-15 (revised 2026-09-18)
**Owner:** Dmytro
**Milestone:** New interaction surface — voice

**Related:** `AGENT_NOTES_RFC.md` (considered and rejected for the summary hand-off, §4.7), `PLATFORM_SESSION_ISOLATION_RFC.md` (per-channel `session_id`), `STANDING_DIRECTIVES_RFC.md` (delegation discipline, §4.2), `REMOTE_MCP_SERVER_RFC.md` (the other surface that lifts a capability out of the chat stack), `COMPANION_AGENTS_RFC.md` (settled after this RFC's first draft — its "companions are ordinary agents" decision reverses §4.5 below)

---

## Revision note (2026-09-18)

This RFC was written 2026-08-15, before the Companion Agents work (`COMPANION_AGENTS_RFC.md`, shipped and merged to `main` 2026-09-16) settled how companion-type agents fit the architecture. Two things changed the plan materially since the first draft:

1. **Companions are ordinary agents** — decided during the text-tutor build, explicitly reversing this RFC's original §4.5 ("Lelik is not an agent"). Lelik should be built the same way: a companion-family `BaseAgent`, not a bespoke handler+service pair.
2. **A Spanish Twilio number is already provisioned.** The original §5.3 paperwork blocker (days of ID/address review) that justified shipping a browser transport first no longer applies — telephony is reachable in v1.
3. **The media topology is reversed: audio flows through our backend** (Twilio Media Streams relay), not carrier-direct to the provider over SIP. The first draft asserted that "in *every* candidate transport the media bypasses our backend entirely" and built the port contract on it. That assertion was false — it silently excluded the Media Streams topology — and the architectural consequences of carrier-direct media were never examined. See §5 for the decision and §4.5 for what it does to the port.

Sections §4.4–§4.7, §4.9, §4.10 (new), §4.11 (new), and §5 are rewritten below. §4.1–4.3, §4.8, §6 are materially unchanged. Everything else in this note's absence should be read as still-settled from the first draft.

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

- **Voice messages in Slack/Telegram.** A separate, much cheaper feature: the `AudioTranscriptionPort` DI chain already exists end to end with `audio_service=None` (`src/ports/audio_transcription_port.py` documents the three wiring steps). It covers hands-free capture independent of this RFC.
- Persisting the raw voice session (§4.7 — deliberate).
- Multiple concurrent calls per user.
- **Outbound telephony** (Alek calling the user, e.g. for reminders) — a separate capability with its own port and RFC (§5.2), not a transport for this one. Cheaper to build once this RFC's SIP infrastructure exists.
- A native mobile client. Named here only because it is the fallback the owner would consider if the phone transport disappoints — not planned.
- A browser transport. Considered as the v1 transport in the first draft; superseded by telephony-first (see Revision note). Not ruled out forever — §5.2 keeps it as a fallback if the persona needs a reading surface telephony can't give it.
- Prod rollout. Dev-only and experimental.

## 4. Key design decisions

### 4.1 Lelik is a front desk for Alek, not a second agent

The persona is the architecture. **Lelik is not an intelligence in his own right — he is a voice assistant *to* Alek.** His job is to hear the request, clarify it if it is ambiguous, forward it to Alek, and keep the conversation alive while the answer is prepared.

**Alek is never spoken.** He answers in text, and Lelik relays it.

This is a persona/behavior decision, independent of the implementation-layer question of whether Lelik is literally a `BaseAgent` — see §4.5, reversed from the first draft.

This framing exists to solve a specific failure mode (§4.2), but it pays twice more:

- **Clarification before forwarding is an upgrade, not a workaround.** Today a vague request reaches Smart, which either guesses or asks back over a slow round trip through the channel. Lelik can resolve the ambiguity in two seconds of speech and forward a *well-formed* query. This is better than the text path does.
- **Attribution becomes in-character.** A front desk naturally says "Alek says…". The boundary between Lelik's own read and Alek's grounded answer stops being an imposed rule and becomes part of the role — and rules that match a character survive far better than rules bolted onto one.

Latency stops being a defect and becomes the role's purpose. The Realtime API's async function calling is built for this: the model keeps speaking coherently while a long call is pending — which is also why calling Alek ships async-only in v1 (§4.10).

**Note the wording.** The instruction is *narrate what is happening* — "asking Alek about your mail from last week" — not *entertain*. Entertainment as a stated goal produces filler for its own sake; narration fills the same silence, and as a side effect lets the user hear whether the request was understood **before** the answer arrives. That is a free correctness check.

**Rejected — two realtime sessions, Lelik and Alek as two voice models.** Gives genuine cross-talk, but Alek stops being Alek: a realtime model has different weights and no access to the agent stack, so the thing holding the memory is exactly the thing replaced. Doubles audio cost and adds audio routing between peers.

**Rejected — TTS over Alek's answer in a second voice.** Preserves the real Alek and still gives two voices, but fails on content: markdown tables and URLs read aloud are unusable.

### 4.2 Delegation is defined by capability, not by difficulty

The naive rule — "call Alek when the question is hard" — fails predictably in both directions. Calling on every utterance is wasteful; leaving it to the model's discretion means it will almost never call, because LLMs are poor judges of their own competence and will answer confidently instead.

The durable boundary is not difficulty but **possession**: Lelik has a deliberately small, scoped slice of the user's biography (§4.8) and no mail, tasks, or search of his own. "Do I have this information?" is a presence check, not a self-assessment, and models are markedly better at recognizing missing data than at admitting insufficient intelligence.

So the rule is *forward anything touching the user, their affairs, mail, documents, or current facts outside Lelik's own small slice* — and §4.1 makes forwarding the default posture rather than an escalation, so nothing has to be admitted to trigger it. Mechanically, "forward" means calling the `ask_alek` tool — see §4.10.

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

### 4.4 Alek is invoked through the Router, not directly

**This section is corrected from the first draft, which said "Alek is invoked through the existing path… `SmartResponseAgent` is already an orchestrator accepting a query. Lelik calls it the way `ConversationHandler` does." That claim was already known wrong when the first draft shipped and was never fixed: `ConversationHandler` sends to `router_agent_{user_id}`, not to Smart (`conversation_handler.py:700`), and it is `router_agent.py:331-332` that calls `enrich_context` — the RRF memory search. `smart_response_agent.py` has no equivalent call.**

Calling `SmartResponseAgent` directly is Alek without memory — Goal 2 fails. The `ask_alek` mechanism (§4.10) must resolve to the Router, not to Smart, so the same enrichment step runs regardless of caller.

### 4.5 Lelik is an ordinary companion agent

**Reversed from the first draft**, which argued Lelik cannot be a `BaseAgent` because its lifecycle (`_call_llm`, `_execution_billing_scope`, `record_turn`) is request/response and a realtime session is a persistent bidirectional stream, and concluded Lelik should be a handler+service pair outside the agent system.

`COMPANION_AGENTS_RFC.md`, settled after this RFC's first draft, decided the opposite for the whole companion family: *"Companions are ordinary agents — this reverses `VOICE_COMPANION_RFC.md` §4.5. Being 'not an agent' is exactly what would break bidirectional sync/async calls."* Lelik should be built the same way as the text tutor: a `BaseAgent`-derived companion, registered, participating in delegation, with a prompt assembled through `PromptBuilder`.

**The realtime session loop does not live in the agent — decided, not left open.** A realtime session genuinely does not fit `_call_llm`'s per-turn request/response shape; that part of the first draft's objection stands and is not dissolved by making Lelik an agent. The split:

- **Lelik the agent** owns what agents own: the assembled persona prompt, the tool set, the permission toggles, the `CompanionConfig`-shaped policy, and the handling of discrete tool calls (notably `ask_alek`, §4.10).
- **`VoiceSessionService` owns the session loop**: the two live connections, the byte relay, the call-scoped buffer, lifecycle.

`REQ-ARCH-03` makes this more than a style preference: *every* `*Agent` class must inherit `BaseAgent`, and it is enforced by test (`tests/unit/test_req_arch_01_hexagonal_isolation.py`). So the component that owns a long-lived socket loop must not be named `*Agent` — if it were, it would have to inherit a lifecycle built for request/response. Naming discipline here is load-bearing, not cosmetic.

**The provider boundary needs its own port, and — reversed from the first draft — it carries audio.** `LLMPort.generate_content(request) -> LLMResponse` (`src/ports/llm_port.py:54`) is strictly request/response and cannot express a session that lives for minutes:

- `ports/realtime_session_port.py` — open/configure a session, push audio frames, receive audio frames, receive tool-call events, return tool results, receive usage events, close.
- `adapters/openai_realtime_adapter.py`, `adapters/xai_realtime_adapter.py` — implementations.

**The port is justified on the repo's own test (2+ implementations, substitutable, real system boundary):** the owner intends to run Grok as a second provider, and xAI's realtime surface is wire-compatible with OpenAI's over WebSocket. This justification only holds under the Media Streams topology (§5) — under carrier-direct SIP the provider must additionally offer SIP ingest, which OpenAI does and other providers may not, collapsing the port to a single possible implementation and making it a wrapper rather than a port.

**Audio frames cross the port as a `domain/` value object**, not as either vendor's wire shape. `REQ-ARCH-06` forbids ports importing other ports and `REQ-ARCH-07` forbids port interfaces living in `domain/`, so the frame type belongs in `domain/` (pure: encoding, sample rate, payload bytes, track direction) and both the port and the carrier-side handler speak it. This is what keeps the service layer provider- and carrier-agnostic.

### 4.6 Transport is a handler plus a relay service — and only the provider side gets a port

Twilio connects *inbound* to us: it opens a WebSocket to our endpoint and streams the call's audio, and audio we send back rides that same socket. Architecturally that is an **entry point for an external event**, which this repo models as a handler — not an outbound adapter, and not a second port.

**No `CarrierMediaPort` in v1.** `CLAUDE.md` justifies a port on 2+ implementations plus a substitution need; a second carrier (Vonage, Telnyx) is speculative today, so a carrier port would be exactly the "port created for cleanliness" the project warns against. What keeps the relay carrier-agnostic is the `domain/` audio-frame type (§4.5), not an abstraction over Twilio. If a second carrier ever appears, the handler is the seam to extract at — and by then the frame type already exists.

**The byte pump lives in the service layer, never between adapters.** `REQ-ARCH-08` forbids cross-subpackage adapter imports: the carrier-side handler must not import the provider adapter and vice versa. `VoiceSessionService` holds both sides — the inbound socket from the handler, the provider behind `RealtimeSessionPort` — and moves frames between them. A relay written the obvious naive way (Twilio adapter calling the OpenAI adapter) breaks a tested rule.

The remaining transport concerns decompose onto mechanisms that already exist:

- **Answer delivery** is `ResponseChannel` — telephony reuses the existing Slack/Telegram channels unchanged, since a call has no reading surface (§5.2).
- **Origination and authentication** are the handler's job (§5.4).

A second transport later (a browser page, if the missing reading surface proves a real gap) is another handler over the same service, not a refactor.

### 4.7 The raw call is ephemeral; the summary lands in Alek's own session

**No isolated companion memory for Lelik — unlike the text tutor.** The tutor's memory policy is *isolated* (its own `CompanionRecord` store, its own extractor) because drills and mistakes shouldn't pollute Alek's biography. Lelik's whole value proposition is the opposite: the user should feel they are talking to the same being on the phone as in text. So Lelik's write policy is *transparent into Alek*, not isolated — no `CompanionRecord` collection, no dedicated extractor agent for Lelik.

**The raw call transcript is not persisted long-term.** During the call it lives in a lightweight, ephemeral, call-scoped working buffer inside `VoiceSessionService` — not a persistent `ChannelBinding`-backed companion session the way the tutor's is. It exists only for the duration of one call.

**A long call cannot hold its whole transcript at full-audio-price context.** The buffer must be tiered continuously while the call is live — recent turns kept in full text, older turns compressed to a running summary — the same primitive as the tutor's `_apply_history_tier`, applied progressively during the call rather than once at the end. **Open, unverified: whether the realtime provider offers native context truncation** (the first draft's §6 mentioned "session truncation" as a cost mitigation without confirming it exists) — check before building a custom mechanism.

**At call end, a cheap separate model pass — not Lelik — produces one summary**, for the same reasons as the first draft: bulk noisy-text summarization is a job for a cheap tier, and summarizing through the participant being summarized is the one thing the summarizer must not be.

**Mechanically this must follow the existing extractor pattern, not a direct call.** `VoiceSessionService` lives in `services/`, which may not import `agents/` (`REQ-ARCH-05`). The companion family already solved exactly this for the tutor: `ports/companion_extractor_port.py` fronts the extractor agent, whose docstring records the rule — *"extractor agent (agents/), which only composition/ may import directly"* — with a runner implementation wired in `composition/`. The voice summarizer takes the same shape: a port in `ports/`, a runner wrapping whichever agent does the pass, wired in `composition/`. Reusing this pattern is also why the summarizer's LLM call stays inside the ordinary observability contour rather than escaping it.

**The summary is appended as one ordinary message into Alek's own regular session** — a direct write (`SessionStore.append_messages_batch`), not through `notify()`, which always reformats and delivers rather than quietly writing history. No special "knowledge block", no `AgentNote`-shaped mechanism:

- **Immediate visibility is free.** `history_recent_full_turns` already keeps recent session messages in full text before they age into summary or get consolidated — so a message appended right after a call is already visible to Alek on the very next turn, no new plumbing.
- **Eventual fact extraction is free.** Ordinary threshold-based consolidation absorbs it exactly like any other message — no second consolidation protocol, no new read-path for the consolidator.

**Rejected (reaffirmed) — `AgentNote` or a bespoke "knowledge block" cleared on consolidation.** The first draft rejected `AgentNote`'s `expires_after` because time-based expiry cannot align with threshold-based consolidation. A variant raised during this revision — a dedicated block cleared when the consolidator processes it, not on a timer — would fix that specific mismatch, but is unnecessary: appending an ordinary message achieves the same two goals (immediate recall + eventual facts) with zero new mechanism. Rejected on YAGNI grounds, not because the timing objection reapplies.

**The accepted cost is unchanged from the first draft:** with no raw log retained, summary quality is the single point where anything can be lost, irreversibly. The summarizer prompt remains a first-class deliverable.

### 4.8 Lelik's context: a small trimmed slice, not Alek's full stack

**Cost and capability both force this, independent of the memory-policy decision in §4.7.** Two facts, both already on record from the first draft's own pricing research: audio tokens are priced far above text and the realtime API appears to resend context on every turn (not a one-time system-prompt cost), and the realtime model itself is meaingfully less capable than Alek's own model — it cannot reliably hold the same elaborate multi-token, multi-section prompt built for a flagship text model.

So Lelik's assembled prompt (via the same `PromptBuilder` mechanism the rest of the system uses — see §4.5) must be deliberately small: persona + tool definitions + a trimmed history slice, never Alek's full stack.

**Biographical access is scoped, not off.** The companion framework's existing toggle set (`include_biographical` + `session_domains`) already supports exactly this: the tutor turns biographical access fully off; Lelik turns it on but restricted to a small, explicit list of fact domains — enough for light continuity and small talk ("how did the meeting go"), not a substitute for forwarding. This is a live filtered read of the same fact store Alek uses, not a copy, so there is nothing to keep in sync.

**Consistency between Lelik's trimmed view and Alek's full view is protected by §4.2's forwarding rule, not by the size of the slice.** If a question needs a fact outside Lelik's small domain list, that absence is itself the trigger to forward — the trimmed slice is not meant to approximate full knowledge.

**At call start, Lelik reads a trimmed, summarized slice of Alek's own session** — around the last 10 messages, summarized rather than verbatim — as a "warm-up." This reuses the same tiered-history primitive as the in-call buffer (§4.7), just applied once at read time instead of continuously. Exact size is deliberately left for implementation, once real context economics are measurable.

### 4.9 Billing needs audio legs

`TokenLedger.by_model` prices text tokens per model that ran. Audio is a different rate class and is not expressible today. Without this, voice cost is invisible — the same bug class as the pre-2026-07-30 price attribution. It ships with the first phase, not after.

**The first draft treated this as an unresolved contradiction (its §4.9 vs §4.5); it resolves cleanly, and additively.** The pieces, verified in code:

- `_EXECUTION_LEDGER` and `_execution_billing_scope` are module-private to `base_agent.py` (lines 62, 535) — confirmed. A non-agent component cannot open a ledger, and **it does not need to.** Nothing about them changes.
- `QuotaService.record_usage(account_id, model, tokens, cost)` is a **port** (`src/ports/quota_service.py:10`). `VoiceSessionService` can report usage through it by injection, like any other port consumer.
- **Audio pricing belongs in `domain/billing.py`**, next to the text rates, not in the relay. It is pure arithmetic over rates with no I/O, which is exactly what `domain/` is for — and single-sourcing it is the specific lesson of the 2026-07-30 price-attribution fix. A relay that carried its own private audio rates would re-create that bug in a new place.

So: usage events arrive from the provider on the session (the port surfaces them, §4.5), cost is computed by a `domain/billing.py` function, and the result is reported through the existing quota port. No change to `BaseAgent`, no lifted ledger.

**Transcript observability lands the same way, and is better than the first draft assumed.** `record_turn` is not a `BaseAgent` internal — it is a port (`src/ports/prompt_content_store.py:29`) with a BigQuery adapter (`adapters/bigquery_prompt_content_adapter.py:82`). The relay can write voice turns into `prompt_content` through the same port every agent uses, so a call is inspectable by the project's normal debugging protocol rather than being a blind spot.

### 4.10 Calling Alek from Lelik: the `ask_alek` intent

**Not a bespoke `call_smart()`-shaped method** — an alternative explored and parked during the text tutor's Phase E (where "tutor calls Alek" was fully designed, then scoped out as belonging to this RFC instead). Verified against current code during this revision: the existing specialist-delegation machinery already supports this with no new mechanism, once one gap is closed.

**What already works, verified in code:**
- `AgentCoordinator.handle_delegation` resolves an intent to an agent via `AgentRegistry.get_agent_for_intent`, then dispatches by `ExecutionMode` (`agent_coordinator.py:344-441`) — nothing in this path assumes "specialist" as opposed to "orchestrator"; `_execute_sync`/`_execute_async` (`agent_coordinator.py:520-576`) just resolve a per-user agent id and route a message or enqueue a Cloud Task.
- The delegation cycle guard (`_refuse_if_looping`, `MAX_DELEGATION_DEPTH=8`, `agent_coordinator.py:443-473`) already covers self-recursion safely — "Alek calls Alek" is a case the guard was built for, not a new risk.

**What is missing today:** `SMART_RESPONSE`'s descriptor declares `capabilities={}` ("Smart does not offer intents to other agents") and neither `SMART_RESPONSE` nor `QUICK_RESPONSE` is included in `ALL_DESCRIPTORS` (`agent_manifest.py:307-334`) — so no intent resolves to either orchestrator today. This is a deliberate omission, not a technical wall.

**Per §4.4, the new intent must resolve to the Router, not to Smart** — and the Router currently has no `AgentDescriptor`/registry presence at all the way Quick/Smart do (`ConversationHandler` addresses it directly by a hardcoded `router_agent_{user_id}` recipient, `conversation_handler.py:700`, outside the intent-lookup system entirely). Giving Router a registry-routable capability is real design work, left to the implementation plan — this RFC only fixes the requirement ("must go through Router") and rules out the naive fix ("just register Smart").

**The shape once wired:**
- A new `Intent.ASK_ALEK` constant.
- `internal=True`, matching the existing pattern for `CONSOLIDATE`/`EXECUTE_DEEP_RESEARCH_CLAUDE` — not offered to ordinary LLM tool selection, invoked only from Lelik's own logic.
- Gated via `allowed_intents` on Lelik's own descriptor, the same mechanism already used to remove `search_memory` from the tutor's allowed intents.

**v1 ships async-only** (`ExecutionMode.ASYNC` → `enqueue_agent_task`). Lelik never blocks on Alek's full Router → Smart → specialist pipeline; this is also what makes the "narrate while waiting" persona design (§4.1) load-bearing rather than decorative.

**Sync mode is explicitly deferred**, not designed here. Open question, not investigated: whether a synchronous call into a user's per-user Router/Smart singleton from inside a live call would block that same singleton from handling concurrent requests elsewhere (e.g. a Slack message arriving mid-call). Investigate before ever enabling sync mode — do not assume either answer.

### 4.11 The relay is its own Cloud Run service

**Not part of `alek-bot`.** Verified Cloud Run behaviour makes the two workloads incompatible on one service:

- **An open WebSocket keeps its instance active**: "a Cloud Run instance that has any open WebSocket connection is considered active, so CPU is allocated and the service is billed as instance-based billing." A call therefore pins an instance and switches that service's billing mode for the call's duration. That is legitimate for active use — but mixing it into the service that answers Slack and Telegram, on the 1 vCPU box `CLAUDE.md` calls out ("1 vCPU Cloud Run — async is mandatory"), means one 20-minute call competes with ordinary request serving.
- **Request timeout caps a call at 60 minutes** (default 5). The relay service needs `--timeout=3600`, which is not a setting to apply to the main service wholesale. **A call longer than 60 minutes will be cut — a known, accepted v1 limit.**
- **Its scaling and warm-up profile differ.** `min-instances` was deliberately returned to 0 on 2026-09-16 on cost grounds; a cold start on the inbound call webhook delays *answering* (silence before Lelik speaks), which may justify `min-instances=1` for the relay alone — a decision that should not drag the main service with it.
- **Region may differ** (§5.2): the relay wants to sit next to Twilio's media engine and the provider endpoint, while everything else stays in `us-central1`.

Precedent exists in this repo for a second deployable from one codebase: DeepResearch already runs as a Cloud Run **Job** with its own entrypoint. A second Cloud Run **service** is the same pattern, not a new one.

Session affinity is explicitly *not* needed: the WebSocket **is** the session. Twilio connects once per call and stays, so there is no second request to route to the same instance — which is fortunate, since Cloud Run's affinity is documented as best-effort only.

The relay reaches the main service for tool calls over ordinary HTTP/Cloud Tasks. This costs nothing perceptible because `ask_alek` is async in v1 by design (§4.10) — the one call that would have been latency-sensitive is the one we already decided not to make synchronously.

## 5. Transport — telephony, with media relayed through us

Two separate decisions live here, and the first draft got both wrong for the same reason — it treated infrastructure convenience as architecture.

**Decision 1 — telephony, not a browser page, is the v1 transport.** The first draft ordered browser first for two reasons: (a) cheaply testing whether the persona holds up over a session, which is transport-independent; (b) telephony's number-provisioning paperwork (§5.3) took days. (b) no longer applies — a Spanish Twilio number is already provisioned. (a) is answered just as well by a real call. Building two transports to validate one persona is redundant; ship the one that is actually wanted.

**Decision 2 — the call's audio is relayed through our backend (Twilio Media Streams), not routed carrier-direct to the provider over SIP.** This reverses the first draft, whose §4.5 asserted that media bypasses our backend "in every candidate transport" — an assertion that was false, never examined, and load-bearing for the port design.

The argument that decided it is not latency but **perimeter**. `CLAUDE.md` makes reading actual data the mandatory first step of any production investigation. Under carrier-direct SIP, an entire class of failures — SIP negotiation, codec mismatch, trunk auth, media interruption — produces no data in Cloud Logging, no span in Logfire, and no row in BigQuery; it lives in two third-party consoles. That is not an inconvenience to absorb later, it is opting out of this project's operating discipline at the point of design. Relaying puts the call inside the contour: real logs, a normal span tree, turns in `prompt_content` through the existing port (§4.9), and an independent measure of duration and bytes to cross-check provider-reported usage — which, given this codebase's history of a 3.6× token-inflation bug and a wrong per-model price attribution, is worth having rather than trusting one source.

**The two objections to relaying both collapsed under verification:**

- **Latency.** The estimate that killed it in discussion (+200–250 ms) was wrong, because it assumed the relay *introduces* a transatlantic hop. It does not: that hop is a function of where Twilio's media engine and the provider's endpoint sit — both configurable — and carrier-direct SIP incurs the same crossing inside Twilio's network. With regions aligned the relay adds a single same-region hop plus frame forwarding: tens of milliseconds against a model time-to-first-audio of ~300–800 ms. Barge-in stays native to the provider; we forward bytes and never implement VAD.
- **"More code."** Twilio Media Streams sends `audio/x-mulaw`, 8 kHz, mono, base64, and accepts `mulaw/8000` base64 back. The Realtime API accepts and emits `g711_ulaw`. **The formats match** — the relay is a base64 decode and a byte forward, with no resampling and no DSP. (Had we been forced through `pcm16`, that path is 24 kHz and would require μ-law decode plus 8k→24k upsampling in both directions.)

**Rejected — carrier-direct SIP.** Cheapest to stand up and marginally lower latency, but: it puts the media path outside every observability and debugging mechanism this project relies on; the Twilio↔provider binding lives in a third-party console where no test can reach it, so `make check` cannot detect it broken; swapping providers becomes console reconfiguration rather than an adapter swap; and it requires the provider to offer SIP ingest, which collapses `RealtimeSessionPort` to one possible implementation and voids its own justification (§4.5). Kept on the shelf as a latency optimisation if a real call proves relaying insufficient — but it is a different architecture, not a config flip, and adopting it later means widening the port contract back off audio.

### 5.1 Why a browser transport was even considered (context, not a v1 concern)

**Browsers cannot capture audio in the background.** iOS Safari suspends WebRTC and Web Audio the moment the screen locks or Safari backgrounds — a deliberate platform restriction, unchanged in Safari 26. Android Chrome degrades rather than works: reported hangs 2–4 minutes after screen lock. Any browser-based transport would therefore be screen-on — one more reason telephony, which has no such restriction, is the better v1 choice, not just the cheaper one now.

### 5.2 The media path, concretely

```
caller → PSTN → Twilio media engine → [WebSocket, μ-law 8k] → relay service (Cloud Run)
                                                                      ↕
                                                    [WebSocket, g711_ulaw] → realtime provider
```

The relay forwards frames both ways and additionally sees every session event: tool calls, transcription events, usage. Alek is reached from there by `ask_alek` (§4.10); his answer goes to the user's chat channel via `UserNotificationService`, since a call has no reading surface.

**Region alignment is the whole latency question.** Three things must agree, and each is configurable:

- **Twilio's media region.** Media Streams runs in `US1` (default), `IE1` (Ireland) and `AU1`; Twilio documents the EMEA engine as intended for exactly this use ("connecting to AI bots and related STT/TTS services outside of Twilio"). Region follows how the account and number are homed, and a phone number's inbound processing region is settable via Twilio's REST API. Note that for inbound PSTN, Twilio tags the call with the edge where it arrived — the media anchor is an account/number configuration, not a per-call choice.
- **The provider endpoint.** `/v1/realtime` is on OpenAI's list of endpoints eligible for European processing via `eu.api.openai.com`.
- **The relay's Cloud Run region.**

**Pragmatic v1: keep all three on the US path** — Twilio homed `US1` as it already is, the standard provider endpoint, relay in `us-central1` beside everything else. The transatlantic leg then happens inside Twilio's network exactly as it would under carrier-direct SIP, so it is not a cost of relaying.

**Available optimisation if a real call disappoints:** home the number in `IE1`, use `eu.api.openai.com`, and deploy the relay in a European region — the media path then never leaves Europe. **Two caveats, unverified:** European processing requires approval for abuse-monitoring controls plus an executed Modified Retention amendment (availability on the owner's account type is unknown), and OpenAI documents that "tracing is not currently EU data residency compliant for `/v1/realtime`". Do not plan on this path until both are checked.

**What telephony buys, unchanged from the first draft:** hands-free with the screen off — pocket, car, Bluetooth — as native OS behaviour; `"Hey Siri, call Lelik"` with zero integration because it is just a contact; and cost bounded by call duration, so a forgotten open microphone is impossible. Concurrency is not limited by the number itself; the ceiling is the provider's per-account concurrent-session limit (unverified, §9) and cost scaling linearly with simultaneous callers.

**Rejected for v1 — transcript-only (Twilio transcription → text agent → TTS).** Raised in discussion as a lighter variant; it is not one. Taking transcript instead of audio removes the realtime audio model altogether, which means implementing barge-in ourselves, losing prosody, and *worse* latency (STT finalisation + LLM + TTS start), while abandoning §4.1's premise that the pause is covered by natural speech. Its genuine advantage is cost — cheap text tokens instead of audio tokens — which makes it the right fallback to reconsider if §6's audio pricing proves prohibitive, and a poor choice for validating a conversational persona.

**Also not planned for v1**, kept if the missing reading surface proves a real gap: the one-button **web page** (WebRTC, ephemeral-token endpoint behind `auth_required`) — the only candidate where Alek's links and tables render beside the conversation. A **Siri Shortcut** loop remains a legitimate cheap probe of whether voice access is valuable at all, independent of this design.

### 5.3 Number provisioning — resolved

A Spanish Twilio number is already provisioned and owned; the original identity/address-proof review (measured in days) is a non-issue. What remains is configuration, not paperwork: the number must be reachable by the relay's handler, and if the European path in §5.2 is ever taken, the account and number must be homed in `IE1`.

### 5.4 Caller identity — in scope for v1

**Confirmed in scope: multi-user support ships in v1**, not deferred to "single caller only." The full mechanism from the first draft stands, unchanged:

**Provisioning reuses the existing platform-identity mechanism.** `link-telegram` already calls `add_platform_id(user_id, "telegram", …)` with uniqueness enforced and a 409 when already bound (`src/web/user_cabinet_app.py:279`). A phone number is one more platform key: `add_platform_id(user_id, "phone", <E.164>)`. Preferred over a `UserBotConfig` field — a phone number is an identity on an external platform, not a preference, and the platform map gives uniqueness for free. Each family member binds their own number in their own Cabinet and reaches their own memory.

**Ownership must be proven at binding.** `link-telegram` does not verify that the binder owns the ID; inheriting that permits squatting — binding someone else's number so their calls arrive at the attacker's account. A one-time SMS/voice OTP closes it and adds no dependency, since the telephony provider is already present.

**Every call authenticates.** Caller ID is spoofable and OTP proves ownership at registration, not at call time. Lelik opens each call by asking the caller to identify themselves and expects a spoken 4-digit PIN before anything else happens.

**The authorization decision lives in server-side session state, never in Lelik's context.** Lelik calls a `verify_pin` tool; comparison is deterministic and server-side, and memory/agent access is gated by that state. If the model held the decision, speech would be an injection channel — "we already did this, I'm the owner" is just a sentence, and sentences are what the model consumes. **Lelik may ask for the PIN and must be unable to grant anything.**

**Brute force is bounded and loud.** Four digits is a 10⁴ space, so rate limiting — not the hash — is the real control: few attempts per call, then disconnect; a lockout per calling number across calls; failures reported to the existing `AlertSinkPort` (`src/ports/alert_sink.py`) as well as logs. The PIN is stored hashed, with the honest caveat that 10⁴ does not survive an offline attack on a leaked store.

**A spoken PIN must be redacted before persistence.** The non-obvious hazard: the PIN arrives as audio, is transcribed, and then follows the normal path into session history, BigQuery `prompt_content`, and Logfire — which has captured content since 2026-07-30. Without explicit redaction at the boundary, authenticating by voice writes the credential into observability storage. This is a v1 requirement, not later hardening.

## 6. Cost

Audio tokens dominate and are an order of magnitude above text. Figures around `gpt-realtime-2` are ~$32/$64 per 1M audio tokens with the mini tier substantially cheaper — **secondary sources; verify at the provider before building.** `make check-pricing` does not cover realtime models.

**A premise worth measuring before optimizing:** an Alek invocation is *text* — Router → Smart on `gpt-5.4-mini` with delegation — while the conversation around it is *audio*. One call to Alek may well cost less than the twenty seconds of speech it interrupts, in which case the §4.2 economy problem dissolves and the answer is to delegate liberally. This is measurable on the first real call and requires no architectural decision in advance.

Mitigations otherwise: server-side VAD so silence is not streamed; the continuous in-call tiering from §4.7; provider-native context truncation if it exists (§9).

## 7. Implementation plan

**Phase 1 — the core (§4), transport-independent:**

0. **Spikes before code** (each cheap, each can invalidate a design detail): confirm `g711_ulaw` actually holds end-to-end on the GA API — it is set nested (`session.audio.input.format`) and there are reports of it silently reverting to `pcm16`; measure relay-added latency and cold-start-to-answer on a throwaway echo relay; confirm whether the provider offers native context truncation (§4.7); check whether xAI's realtime surface accepts `g711_ulaw`, since the port's 2+-implementation justification rests on it (§4.5).
1. `domain/` foundations: the audio-frame value object (§4.5) and audio pricing in `domain/billing.py` (§4.9).
2. `RealtimeSessionPort` + one provider adapter, with wire tests at the SDK boundary; session lifecycle, tool-call events, usage events, reconnect.
3. Lelik as a companion-family `BaseAgent` (§4.5) — registry entry, `CompanionConfig`-shaped policy (memory-transparent into Alek, not isolated; scoped biographical toggle), persona prompt via `PromptBuilder` written against `feedback_prompt_anchors.md` (§4.2).
4. `ask_alek` internal intent (§4.10) — including giving the Router a registry-routable capability, verified to preserve `enrich_context` (§4.4).
5. `VoiceSessionService` — the relay (§4.6), call-scoped buffer with continuous tiering (§4.7), usage reporting through `QuotaService` and turns through `PromptContentStore` (§4.9).
6. End-of-call summary via a port + runner wired in `composition/`, mirroring `CompanionExtractorPort` (§4.7); summary written into Alek's own session; warm-up read at call start (§4.8).
7. Verify a spoken conversation's summary consolidates into facts by the ordinary path — no voice-specific consolidation protocol.

**Phase 2 — telephony transport:**

The relay as its own Cloud Run service (§4.11: `--timeout=3600`, own region and `min-instances` decision, own entrypoint), the Twilio Media Streams handler, number/region configuration (§5.2–5.3), the §5.4 caller-identity path (`add_platform_id(…, "phone", …)` + OTP + per-call PIN + redaction), and Alek's answers routed to the user's existing chat channel via `UserNotificationService`.

## 8. Test plan

- **Regression guard for the exact bug already found once:** a test asserting `ask_alek` dispatch calls `enrich_context` (i.e., resolves through the Router) and never reaches `SmartResponseAgent` directly (§4.4, §4.10).
- **Port/adapter:** wire tests at the SDK boundary per `ADAPTER_WIRE_TESTING.md` plus contract validators in `tests/contracts/adapter_contracts.py`. Session lifecycle, tool-call event translation, audio-frame translation in both directions, usage events, reconnect.
- **Architecture rules** (these are tested rules, not preferences — a naive relay breaks them): the carrier-side handler and the provider adapter never import each other (`REQ-ARCH-08`); `services/` never imports `agents/`, so the summarizer goes through its port (`REQ-ARCH-05`); the session-loop component is not named `*Agent` unless it inherits `BaseAgent` (`REQ-ARCH-03`); the audio-frame type lives in `domain/` and the port does not import other ports (`REQ-ARCH-06`, `REQ-ARCH-07`).
- **Billing:** audio priced by the `domain/billing.py` function (not by rates duplicated in the relay) and reported through `QuotaService.record_usage`; a call spanning Lelik plus an Alek delegation bills each at its own rate; `_EXECUTION_LEDGER` is untouched.
- **Summary path:** a synthetic call yields a summary appended to Alek's own session, which consolidates into facts by the ordinary path with no voice-specific protocol.
- **Delegation discipline:** requests touching user facts outside Lelik's scoped domain list reach Alek; the explicit trigger phrase forces a call unconditionally.
- **Cycle guard reuse:** a self-referential `ask_alek` chain is refused by the existing guard, not a new one.
- **Caller identity:** PIN verification is server-side and cannot be talked past; brute-force lockout fires and alerts; **the spoken PIN appears in no persisted store** — history, BigQuery, or Logfire.

## 9. Open questions

1. **In-call and warm-up history sizing** (§4.7, §4.8) — deferred to implementation, once the realtime session's true context economics are measurable rather than guessed.
2. **Does `g711_ulaw` hold end-to-end on the GA API** (§5.2) — documented as supported, but set nested (`session.audio.input.format`) and reported to silently revert to `pcm16`. If it does not hold, the relay gains μ-law↔PCM16 conversion and 8k↔24k resampling, which changes its cost profile. Phase 1 spike.
3. **Does xAI's realtime surface accept `g711_ulaw`** (§4.5) — the port's 2+-implementation justification depends on a second provider being reachable through the same relay. Wire-compatibility over WebSocket is on record; format support is not verified.
4. **Measured relay latency and cold-start-to-answer** — no reasoning replaces one real call. Phase 1 spike.
5. **Which realtime model tier** — depends on latency feel and verified pricing.
6. **Provider's per-account concurrent realtime session limit** — unverified.
7. **Does the realtime provider offer native context truncation** (§4.7) — check before building a custom mechanism.
8. **`min-instances` for the relay service** (§4.11) — whether cold-start silence before Lelik answers justifies a warm instance, given `min-instances` was deliberately returned to 0 on cost grounds 2026-09-16.
9. **Is European processing available on the owner's account** (§5.2) — needs abuse-monitoring approval plus a Modified Retention amendment; also note tracing for `/v1/realtime` is documented as not EU-residency compliant. Only gates the optimisation path.
10. **Sync call-to-Alek concurrency safety** (§4.10) — whether a synchronous call blocks the per-user Router/Smart singleton for other concurrent work. Investigate only once sync mode is actually needed.
11. **Wiring `ask_alek` to resolve to the Router** (§4.4, §4.10) — Router has no `AgentDescriptor`/registry presence today; needs its own design pass during planning.
12. **Outbound telephony** — still a separate capability/RFC (§3), and now genuinely cheap: the carrier integration, number, and relay all already exist once this ships.

## 10. Rollback

Mostly self-contained, and the relay being its own Cloud Run service (§4.11) makes it more so: deleting that service removes the entire audio path with no effect on Slack or Telegram.

New and removable: the companion agent, `RealtimeSessionPort` + adapter, `VoiceSessionService`, the Media Streams handler, the summarizer port + runner, the `ask_alek` intent, §5.4's identity path.

Additive changes to existing code, none of which alter current behaviour: audio rates in `domain/billing.py`; the Router gaining a registry capability (a new entry, not a change to how anything routes today); one summary message written into an existing channel session per call.

**Not rolled back by deleting code:** Twilio number/region configuration and any OpenAI EU-project arrangement live outside the repository — the one place this design still reaches past its own perimeter, and worth noting precisely because §5 rejected carrier-direct SIP for having that property pervasively rather than marginally.
