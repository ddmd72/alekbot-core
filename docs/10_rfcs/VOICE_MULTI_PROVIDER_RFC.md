# RFC: Voice on more than one realtime provider

**Status:** Draft, approved in direction by the owner (2026-09-30)
**Date:** 2026-09-30
**Owner:** Dmytro
**Milestone:** Voice — provider choice

**Related:** `VOICE_COMPANION_RFC.md` (§4.4 the port, §4.16 the xAI adapter),
`VOICE_WEB_TRANSPORT_RFC.md`, `decisions/voice_xai_protocol_probe.md` (**authoritative** for
xAI's protocol behaviour and the four UAT calls). The probe script
`scripts/voice/test_xai_realtime_protocol_poc.py` is the reference for the xAI wire shapes.
Branch `feat/voice-xai-realtime` holds the experiment this RFC turns into a design.

---

## 1. Problem

Lelik was built for one provider, OpenAI `gpt-realtime-2.1`, and his whole call behaviour was
tuned to how that model reads a prompt. Four owner calls on xAI `grok-voice-think-fast-2.0`
(2026-09-29/30) showed that the same Lelik is **not portable across providers**:

| Call | Setup | Owner's verdict |
|---|---|---|
| 1 | Full Lelik prompt, relay-owned turns (anchor, notes, own barge-in) | "ужас": no emotion, few-shot lines recited verbatim |
| 2 | Bare: Grok owns its turns, no anchor/notes, role + plain tool list | natural and brief, **no character at all** |
| 3 | Bare + character as a third-person prose portrait | balanced character; a familiar address became a refrain |
| 4 | Bare + second-person portrait in xAI's prompting-guide format | **"бомба"**; opened the briefing file from history on its own initiative |

Two things differ per provider, and neither is a detail an adapter can hide:
- **Who owns the turn.** On OpenAI, the relay adds a persona anchor and starts every reply itself.
  Grok replies on its own (it ignores `create_response: false`) and does best when left to.
- **What the prompt is.** Content: which rules exist at all. Form: xAI's guide asks for second
  person and fixed markdown sections, which our Groovy wrapper does not produce.

The experiment ships this as five env knobs on two services, which must agree:
- relay: `VOICE_REALTIME_PROVIDER`, `VOICE_XAI_BARE`, `VOICE_XAI_VOICE`, `VOICE_XAI_REASONING_EFFORT`;
- main service: `LELIK_PROMPT_PROFILE`.

It also has an adapter that emulates relay-owned turns on xAI by cancelling Grok's own replies. And
the port contract (`RealtimeSessionPort`: "the provider does not reply on its own") is false in
bare mode.

## 2. Goals

1. **Provider is a per-user setting** with a system default. The default is the setup from call
   4: xAI, `lelik_you`, voice `castor`, effort `high`, provider-owned turns.
2. **One source of truth per call.** Provider, prompt profile, turn ownership, voice and effort
   travel together, from the main service to the relay, inside the session config.
3. **The port states turn ownership honestly**, and the service stays provider-agnostic:
   no `if provider == "xai"` anywhere in `services/`.
4. **Voice character is user-overridable**, through the existing USER token override mechanism.
   It is separate from the text persona.
5. **Adding a provider (e.g. Gemini Live)** means an adapter, a profile entry, a prompt profile and
   a UAT cycle. It needs no change to the service or the relay loop.

## 3. Non-goals

- A Cabinet UI for choosing the provider or the character; this RFC only adds the stored fields.
- Making one character text work on every provider. §6 gives this up on purpose.
- Tuning Alek's latency for voice: owner decision 2026-09-30, Alek is expected to take its time.
- The topic-generating character trait and `idle_timeout_ms`. Those belong to a separate test
  session (plan `docs/superpowers/plans/2026-09-30-lelik-on-grok.md`).

## 4. Design

### 4.1 `VoiceProviderProfile` — the per-provider bundle

`src/domain/voice_provider_profile.py` is pure data with no I/O, so it lives in `domain/`:

```python
class TurnOwnership(str, Enum):
    RELAY = "relay"        # relay places the persona anchor and starts every reply (OpenAI today)
    PROVIDER = "provider"  # provider replies and handles interruptions itself (xAI)

@dataclass(frozen=True)
class VoiceProviderProfile:
    provider: str             # "openai" | "xai"
    prompt_profile: str       # Firestore prompt profile id, e.g. "lelik", "lelik_you"
    turn_ownership: TurnOwnership
    voice: str
    reasoning_effort: str

VOICE_PROVIDER_PROFILES = {
    "xai":    VoiceProviderProfile("xai", "lelik_you", TurnOwnership.PROVIDER, "castor", "high"),
    "openai": VoiceProviderProfile("openai", "lelik", TurnOwnership.RELAY, "verse", "medium"),
}
DEFAULT_VOICE_PROVIDER = "xai"   # owner, 2026-09-30: call 4's setup

def resolve_voice_profile(voice_provider: Optional[str]) -> VoiceProviderProfile: ...
```

Changing the system default is a one-line, dated edit, the same convention as `PRICE_SCHEDULE`.
The web path's `VOICE_WEB_REASONING_EFFORT` knob exists today and is out of scope. It stays an
override on the web path and applies only when the profile's provider accepts that value.

### 4.2 Per-user selection and the flow of a call

- `UserBotConfig.voice_provider: Optional[str] = None`. `None` means `DEFAULT_VOICE_PROVIDER`, the
  same pattern as `agent_providers`.
- **An unknown value** falls back to the default and logs an error. A stale string must not make
  the user's calls fail; the future Cabinet write path validates values on write.
- **Composition** (`UserAgentFactory._build_lelik`) resolves the profile from the user's config
  and injects it into `LelikAgent`. This replaces the experiment's `prompt_profile` parameter and
  the `LELIK_PROMPT_PROFILE` knob.
- **`LelikAgent.session_config()`** builds the prompt from `profile.prompt_profile` and adds a
  `voice` entry to what it returns (provider, turn_ownership, voice, reasoning_effort).
  `VoiceCallSetupService.prepare` already spreads that dict into the ticket, so
  `/voice/session-config` needs no change.
- **In the relay**, the provider and the turn ownership arrive **per call**. One relay serves every
  user, so they cannot stay constructor arguments of the two `VoiceSessionService` instances.
  - `realtime_session_factory` becomes `Callable[[VoiceSessionSpec], RealtimeSessionPort]`, where
    `VoiceSessionSpec` is the relay-side view of the profile.
  - The relay keeps a registry from provider to adapter builder, holding the API keys.
  - `reasoning_effort` and turn ownership move from the constructor into the per-call state.
- **Env knobs removed:** `VOICE_REALTIME_PROVIDER`, `VOICE_XAI_BARE`, `VOICE_XAI_VOICE`,
  `VOICE_XAI_REASONING_EFFORT`, `LELIK_PROMPT_PROFILE`. `XAI_API_KEY` stays a relay secret.

Agent cache: `LelikAgent` is cached per user for about an hour, the same as Smart. A provider
change applies at the next rebuild, which is acceptable for a setting changed rarely. See §8 Q1.

### 4.3 The port states turn ownership

`RealtimeSessionPort` changes in two places:
- `open(instructions, reasoning_effort, tools, turn_ownership)`;
- `supported_turn_ownership: FrozenSet[TurnOwnership]`, a class-level declaration.

The `receive_events` docstring changes from "the provider does not reply on its own" to "under
RELAY ownership the provider does not reply on its own".

- **OpenAI** supports `{RELAY}`. Its provider-owned mode was never tested, so it is not claimed.
- **xAI** supports `{PROVIDER}`.
- `open()` raises on an unsupported value. A unit test checks that every entry in
  `VOICE_PROVIDER_PROFILES` uses an ownership its adapter supports.

`VoiceSessionService` branches on `TurnOwnership`, never on a provider name. The branch points are
the ones the experiment already has (`provider_owns_turns`):
- the persona anchor;
- the caller opening;
- the reply on `turn_committed`;
- the silence and waiting watchdog;
- barge-in: under PROVIDER the relay only drops queued audio, with no cancel and no truncate.

Under PROVIDER the relay still starts the greeting and delivers delegation answers: only the relay
knows when an answer has arrived.

### 4.4 The xAI turn emulation is deleted

Call 1 showed that relay-owned turns on xAI lose, and xAI supports only PROVIDER (§4.3). So this
goes:
- the tagging, cancel-by-id and error-absorbing code in `XaiRealtimeAdapter`;
- its `_carried_usage`;
- its tests (per-test approval, §7).

`decisions/voice_xai_protocol_probe.md` keeps the probe findings for the record.

### 4.5 Voice character as user-overridable tokens

Today the character is one token, `PERSONA_LELIK_YOU`, and no user can change it. It is split
into overridable tokens in **voice-only categories**:

| Token (system default = today's portrait) | Class / category | USER-overridable |
|---|---|---|
| `VOICE_PERSONA_LELIK`: who he is to the caller, opinions, no flattery, steady in trouble | properties / `voice_persona` | yes |
| `VOICE_HUMOR_LELIK`: Ranevskaya / Zhvanetsky, self-directed | properties / `voice_humor` | yes |
| `VOICE_TEMPERAMENT_LELIK`: lively reactions, mood audible in the words | properties / `voice_temperament` | yes |
| `VOICE_CALL_MANNERS`: familiar address once or twice a call, no repeated phrase or opener | properties / `voice_call_manners` | **no** |

- **Voice-only categories, not Smart's `archetype`/`vibe`/`humor_engine`.** Otherwise a user's
  text-persona override would put a structured Groovy block into the Grok prompt, the form call 1
  failed on.
- **Call manners are not overridable.** They are what stopped the refrain in call 3; a user style
  must not drop them by accident.
- **The catalog of alternatives** users can pick lives in the USER token collection, next to
  `ARCHETYPE_*`, as second-person prose with no quoted phrases.
- **The override mechanism does not change** (`PromptAssemblyService._apply_overrides`, matching
  class and category).
- **The prompt profile is renamed** `lelik_you` → `lelik_xai` in the same step, so the id says
  what it is for.
- **Heading placement is order-dependent until §4.6.** The `## Role & Persona` heading rides inside
  `VOICE_PERSONA_LELIK` and `## Voice & Communication Style` inside `VOICE_HUMOR_LELIK`.

### 4.6 Blueprint format (optional phase)

`PromptAssemblyService` hardcodes Groovy: steps 8–9 in `prompt_assembly_service.py:250-272`
produce `class X extends Agent { section { … } }`. The change:
- a blueprint gets `format: "groovy" | "sections"`;
- rendering becomes a pure function in `domain/`, one per format, so no port is needed;
- a `lelik_xai_v1` blueprint maps classes to xAI's sections (Role & Persona → Objective →
  Conversation Flow → Guardrails → Voice & Communication Style);
- tokens then lose their inline headings, which removes §4.5's ordering fragility;
- the service stays provider-agnostic: it renders whichever blueprint the profile names.

**Gated on evidence.** Call 4 already worked with an xAI-format portrait inside the Groovy
wrapper. Do this phase when a UAT shows the wrapper hurting, or when §4.5's heading fragility
bites.

### 4.7 Readiness for a third provider (Gemini Live)

It follows the xAI path, in this order:
1. a protocol probe like `voice_xai_protocol_probe.md`, which determines the supported
   `TurnOwnership`;
2. an adapter;
3. a profile entry;
4. a prompt profile written to Google's own prompting guidance;
5. a UAT cycle.

Nothing in §4.1–4.3 assumes two providers.

## 5. Why this stays hexagonal

- **Provider specifics** live in adapters (wire shape, voice, VAD) and in data (profile entries,
  prompt profiles, tokens, blueprints).
- **The service** knows `TurnOwnership`, which is a domain concept, and never a provider name.
- **The main service** chooses a prompt profile id and never inspects its content.
- **The relay** owns only the builder registry; it is the composition root of that deploy unit.

## 6. What is given up, on purpose

1. **One character across text and voice, and across providers.** The owner called this
   "idempotence". Smart's persona overrides no longer reach Lelik on xAI, and each provider's
   character is its own text with its own UAT tuning.
2. **Uniform provider behaviour.** Provider-owned turns are a first-class mode, not something the
   relay emulates away. Its costs, all recorded in the decision record:
   - no blip filter, so any word stops Lelik;
   - no persona anchor;
   - an echo can make the provider interrupt itself on web calls.
3. **Prompt reuse.** The prompt becomes profile × provider; every new provider is a prompt project.

## 7. Implementation plan

Phases A and B ship together on `feat/voice-xai-realtime`, and the branch merges when they are
green and UAT'd (owner merges). C is its own step after that; D is gated (§4.6).

| Phase | Content | Tests |
|---|---|---|
| **A. Profile + per-user choice** | §4.1, §4.2: domain profile + resolver, `UserBotConfig.voice_provider`, composition injection, session-config `voice` entry, relay per-call factory + registry, knobs removed | resolver (default, unknown value, each entry); composition; `session_config` carries `voice`; relay factory picks adapter per call |
| **B. Honest port** | §4.3, §4.4: `turn_ownership` in `open()` + `supported_turn_ownership`, per-call ownership in the service, xAI emulation deleted | registry ⇄ adapter support; per-call ownership in the service |
| **C. Voice character tokens** | §4.5: split tokens, voice categories, `lelik_xai` profile, one alternative per category in the USER catalog as the proof | assembly with and without a USER override (voice categories replace, `voice_call_manners` does not); owner uploads to Firestore |
| **D. Blueprint format** | §4.6, gated | renderer unit tests per format |

**Existing tests.** Phases A and B rewrite tests that the experiment added:
- `test_voice_session_provider_owns_turns.py` (flag → per-call ownership);
- the emulation tests in `test_xai_realtime_adapter.py`;
- `test_lelik_agent_prompt_profile.py` and `test_user_agent_factory_lelik_prompt_profile.py`
  (knob → profile).

Each one needs the owner's per-test approval (CLAUDE.md), requested when the phase starts. Tests
that predate the experiment are expected to stay untouched. Any that break are reported, not
edited.

**Docs** updated in the same phases: this RFC's status, `VOICE_COMPANION_RFC.md` §4.16, CLAUDE.md
(voice section), `docs/07_deployment/README.md` (knobs removed, `XAI_API_KEY` stays).

## 8. Open questions

1. **Agent cache on provider change.** Is an up-to-an-hour lag acceptable, or should a
   `voice_provider` write invalidate the user's cached `LelikAgent`? Settle it when the Cabinet
   write path exists.
2. **Phone path on xAI.** Calls 2–4 were web calls. Before the phone path defaults to xAI, it needs
   one UAT call over Twilio: μ-law at 8 kHz, and whether the echo and VAD behave.
3. **Summary and consolidation.** Grok's transcripts feed the call summary and memory the same way.
   Watch the first summaries for quality.

## 9. Rollback

- **For one user:** set `voice_provider: "openai"`.
- **For everyone:** set `DEFAULT_VOICE_PROVIDER = "openai"` (one line and a deploy).
- The OpenAI path keeps its relay-owned behaviour, and its tests stay untouched.
