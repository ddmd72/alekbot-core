# Decision: companion write path — SessionMode, OverflowRoutingService, auto-attach

**Status:** Adopted
**Date:** 2026-08-31

## Context

`docs/10_rfcs/COMPANION_AGENTS_RFC.md` (Phases A-F) designed a second memory subsystem for
companion agents (session-scoped, not biographical) but never wrote down how the write side
actually turns on end to end. Three mechanisms had to agree for a companion turn to leave any
trace: where `ConversationHandler` saves history, where an overflowing session's batch is routed,
and how a channel ever acquires a `companion_config` in the first place. None of this is in the
RFC's §5-§9 (which describe the policy shape and the shared read-side infrastructure, not the
write plumbing), so it is recorded here instead — this is the "how does the feature turn on"
decision the RFC left implicit.

The gap was serious enough to be this session's Critical #1 finding: through Phases A-E, no code
path anywhere ever created a `ChannelBinding` with a non-None `companion_config`. `$agent tutor`
(the only binding-creation call site, `conversation_handler.py::_handle_agent_command`) always left
it `None`. Every companion-specific branch downstream — `SessionMode` resolution, the overflow
router, `CompanionWindowResolver` — was dead code in production. A manual smoke test on 2026-08-31
had shown SUCCESS with an empty `companion_context` block; that was misread at the time as
"correct empty-skip behavior pre-Phase-F" when it was actually the tutor running in fully
stateless mode regardless of Phase F.

## Decision

**1. `SessionMode.write_session_id` — the companion-vs-Alek session-id-shape split.**
`ConversationHandler._resolve_session_mode` (`conversation_handler.py`) branches on
`binding.companion_config`:

- No binding → default `SessionMode()`: Router flow, `SessionStore` history under
  `context.session_id` (`"{user_id}:{channel_id}"`), full persistence.
- Bound, `companion_config is None` (every non-companion bound agent type, e.g.
  `domain_researcher`) → unchanged from pre-Phase-F: direct delegation, platform-API history,
  `write_session=False`. No SessionStore write at all.
- Bound, `companion_config` set (companion-type agents) → direct delegation, platform-API history
  for reads, but `write_session=True` and `write_session_id = f"{platform}:{channel_id}"` — a
  companion-shaped key, distinct from Alek's `"{user_id}:{channel_id}"`. `write_consolidation` and
  `update_notification_channel` stay False; `use_threads=False` (flat response, not chunked).

`ConversationHandler.handle_message`'s history-save call
(`session_id=mode.write_session_id or context.session_id`) is the one new seam this introduced.
`write_session_id` defaulting to `None` means every pre-existing call site is unaffected — the
`or context.session_id` fallback is exercised on every non-companion path, unchanged behavior.

**2. `OverflowRoutingService` branches the sliding-window overflow batch by binding.**
Extracted from what was previously an inline, untested closure in `main.py` (`services/
overflow_routing_service.py`). `route_overflow(user_id, session_id, messages)` extracts
`channel_id` from `session_id` (works for both id shapes — it splits on the first `:`
unconditionally, same trick `CompanionWindowResolver` uses), looks up the `ChannelBinding`, and
routes to `_route_to_companion` (writes a `CompanionExtractionBatch` to
`companion_extraction_queue`) when `binding.companion_config` is set, else `_route_to_alek`
(unchanged `ConsolidationBatch` path). This mirrors the same `binding.companion_config` predicate
`_resolve_session_mode` uses — the two mechanisms don't share code but do share the one governing
fact: whether this channel has a companion policy attached.

**3. Auto-attach a default `CompanionConfig` on bind (Critical #1's resolution).**
`AgentDescriptor` gains `companion_default_config: Optional[CompanionConfig] = None`
(`infrastructure/agent_registry.py`). The `TUTOR` descriptor
(`infrastructure/agent_manifest.py`) sets it to `CompanionConfig(window_threshold=50,
batch_size=30)` — these two numbers mirror Alek's own live-production consolidation
threshold/batch (`config/settings.py`: `if env_config.is_production: consolidation.threshold =
50; consolidation.batch_size = 30`), a proven live magnitude rather than an arbitrary new pair,
and remain per-channel-tunable later via `CompanionWindowResolver`. Every other `CompanionConfig`
field (`text_mode`, `include_biographical`, `session_domains`, `include_standing_directives`,
`include_own_records`) stays at the dataclass's own default — no override.
`_handle_agent_command`'s `ChannelBinding(...)` construction passes
`companion_config=descriptor.companion_default_config`. Every other bindable agent type's
descriptor leaves this `None`, so `ChannelBinding.companion_config` stays `None` for them exactly
as before — this is additive, not a behavior change for existing bound-agent types.

## Alternatives rejected

- **Explicitly declare manual/Firestore-hand-edit activation as the intended shipped v1 state**
  (the other option surfaced alongside auto-attach during the final whole-branch review). Rejected
  by the owner: it ships a feature that requires a human to hand-edit Firestore documents to ever
  turn on, with no code path or admin UI to do so — effectively shipping a dead feature and
  calling it done. Auto-attach costs one field and one assignment; the alternative costs nothing
  today and an undocumented trap for whoever expects `$agent tutor` to actually work.

## Consequences

- Binding to `tutor` via `$agent tutor` now has an observable side effect beyond routing: it also
  turns on session-scoped memory for that channel. This is the intended behavior, not a side
  effect to be surprised by, but it means `$agent tutor` and `$agent domain_researcher` (say) are
  no longer symmetric in what they provision — worth remembering if a third companion type is
  added with different default sizing needs than the tutor's.
- `window_threshold=50`/`batch_size=30` are the tutor's defaults specifically, chosen because they
  match a proven production magnitude for *Alek's* consolidation cadence — a future companion type
  with much shorter or longer natural turns (e.g. a moderator watching a group channel) should not
  assume these numbers transfer; they are a starting point, not a universal constant.
- `OverflowRoutingService.route_overflow`'s binding lookup is now on the hot overflow path for
  *every* session, including plain Alek ones — a transient lookup failure previously had no such
  dependency. Handled by failing open (see Minor #8 in the final whole-branch review fix wave):
  `_resolve_binding` catches lookup failures and falls through to the Alek routing path rather
  than dropping the batch.

## Provider/tier choices (Phases D-E, noted here for the same reason — not written down elsewhere)

- **Tutor (Phase E) → OpenAI, BALANCED (`gpt-5.6-luna`).** `services/agent_context_builder.py`'s
  `STRATEGIES["tutor"]` inline comment: "Conversational tutoring — no reasoning-model requirement,
  general-purpose chat. Default OpenAI (BALANCED -> gpt-5.6-luna) since 2026-08-31 — owner
  judgement that Claude Haiku 4.5 (BALANCED default) felt too weak for live tutoring
  conversation." Tier is BALANCED in `domain/user.py::_DEFAULT_AGENT_TIERS["tutor"]`, annotated
  "conversational chat, not deep reasoning — cost-appropriate default."
- **TutorExtractorAgent (Phase D) → Claude, PERFORMANCE.** `domain/user.py::_DEFAULT_AGENT_TIERS
  ["tutor_extractor"]`: "Same judgment-call quality bar as consolidation (extraction is a
  deliberate 'what's worth remembering' decision, not mechanical work)." Default provider is
  `claude` in `agent_context_builder.py`'s `STRATEGIES["tutor_extractor"]`, matching
  ConsolidationAgent's own PERFORMANCE → `claude-sonnet-5` default.

## Verification

- `tests/unit/handlers/test_conversation_handler_session_mode.py` — `_resolve_session_mode`'s
  three branches (no binding / bound without companion_config / bound with companion_config).
- `tests/unit/handlers/test_conversation_handler_companion_write_session.py` — end-to-end through
  `handle_message`: a companion-bound turn's history save receives `mode.write_session_id`
  ("platform:channel_id"), not `context.session_id`.
- `tests/unit/services/test_overflow_routing_service.py` — binding-present/absent/companion
  branching, binding-lookup fail-open, `account_id` fallback.
- `tests/unit/handlers/test_conversation_handler_agent_command.py` — `$agent tutor` produces a
  `ChannelBinding` with `companion_config == CompanionConfig(window_threshold=50, batch_size=30)`;
  `$agent domain_researcher` still produces `companion_config=None`.
