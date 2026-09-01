# Decision: history_recent_full_turns is per-channel-binding, not per-user

**Status:** Adopted
**Date:** 2026-09-02

## Context

Phase G shipped `TutorAgent`'s tiered history loading (`BaseAgent._apply_history_tier`) with the
depth (`history_recent_full_turns`) hardcoded to match `SmartResponseAgent`'s own value exactly —
threaded through `UserAgentFactory._UserContext.history_recent_full_turns`, i.e. Alek's own
per-user/account-resolved config (`ConfigurationService.get_history_recent_full_turns`). This was
correct for what was asked at the time ("set it exactly like Smart"), and was independently
verified by the final whole-branch review as a real gap that needed fixing (the value wasn't
actually wired through at all until that review).

Live-testing surfaced a design mismatch: `window_threshold`/`batch_size` (the write-side
overflow/extraction sizing) were already per-channel-binding, stored on `ChannelBinding.
companion_config`, overridable per channel independent of who owns it. `history_recent_full_turns`
(the read-side tiering depth) was the odd one out — tied to the OWNING USER's Alek config, not the
channel. The owner's stated requirement: a channel binding should be able to need a different depth
than another channel the same user owns, with a per-agent-type default as the fallback — the same
shape `window_threshold`/`batch_size` already have, not per-user.

## Decision

`history_recent_full_turns` moves onto `CompanionConfig` as a third sizing field (default `5`,
unlike `window_threshold`/`batch_size` which have no dataclass default — see rejected alternative
below for why). Resolution moves from "per-user, fixed at `TutorAgent` construction" to "per-call,
resolved from the CURRENT channel's binding":

- `ConversationHandler._resolve_session_mode` reads `binding.companion_config.
  history_recent_full_turns` into a new `SessionMode.history_recent_full_turns` field — the same
  point that already resolves `write_session_id` for the current channel.
- `handle_message` threads it into `agent_context["history_recent_full_turns"]`.
- `TutorAgent._converse` reads `message.context.get("history_recent_full_turns", 5)` fresh on every
  call, instead of a `self.*` value fixed once.

This is necessary, not stylistic: `TutorAgent` is a per-user singleton (`UserAgentFactory` caches
one instance per user, reused across every channel that user binds it to). A value stored on
`self.*` at construction cannot vary by channel; only a value re-read from the current message's
context can. The `_UserContext.history_recent_full_turns` / `ctx.history_recent_full_turns`
threading from the final-review fix wave is removed — fully superseded, not left running alongside
the new mechanism.

## Alternatives rejected

- **Keep it per-user (Alek's own config), argue channels can't need different depths.** Rejected by
  the owner directly — a channel-scoped memory feature with a user-scoped depth setting is the
  asymmetry that prompted this decision in the first place.
- **No dataclass default on `CompanionConfig.history_recent_full_turns`, matching `window_threshold`/
  `batch_size`'s "genuinely required" strictness.** Rejected: those two have no default because
  Alek's own thresholds are *env-tuned* (different per deployment environment) — there is no single
  correct number to fall back to. `history_recent_full_turns` is not env-tuned for Alek (a plain
  domain constant, `SearchConfig.DEFAULT_HISTORY_RECENT_FULL_TURNS`), so a stable default carries no
  such risk, and it avoids forcing every existing `CompanionConfig(...)` test-construction site to
  learn about a field it doesn't care about.

## Consequences

- `AgentDescriptor.companion_default_config` for `TUTOR` states `history_recent_full_turns=5`
  explicitly (matches the domain default already, stated anyway — the type's own file should show
  its own tuning, not rely on an incidental match).
- A future companion type states its own default the same way; any individual channel can still
  override via `ChannelBinding.companion_config` (same Firestore-hand-edit path
  `window_threshold` already uses — no cabinet UI yet, out of scope here).
- `TutorAgent.__init__` no longer takes a `history_recent_full_turns` parameter at all.

## Verification

- `tests/unit/domain/test_companion_config.py` — default value.
- `tests/unit/adapters/test_firestore_channel_binding_adapter.py` — serialize/deserialize round-trip,
  including a per-channel override value and backward-compatible fallback for docs written before
  this field existed.
- `tests/unit/handlers/test_conversation_handler_session_mode.py` — resolved from the current
  channel's binding; `None` when unbound.
- `tests/unit/agents/test_tutor_agent.py` — a per-channel override actually changes tiering output
  (not just that the field is read — that changing it changes behavior a 5-turn default could not
  produce).
