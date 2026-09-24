# Lelik starts warm: full context, shared character, relay-owned barge-in

**Date:** 2026-09-22
**Status:** Accepted (owner decision). Replaces the "front desk with a small slice" framing in
`VOICE_COMPANION_RFC.md` §4.1/§4.2/§4.8.

## Decision

Lelik gets as much context as common sense allows and talks from it himself. He goes to Alek only
for what the prompt does not hold: mail, tasks, documents, the web, current data, actions.
`LelikPersonaService` loads the whole biographical cache minus `UserBotConfig.voice_excluded_fact_domains`
(default empty), standing directives, the primary channel's last 30 messages as their stored
summaries, and date, time and location. Character comes from Smart's own overridable token slots,
and Lelik adds only a role token and a phone-medium token. Barge-in truncation and silence handling
are done by the relay.

## Rejected

- **Small domain allowlist (the Slice 1 build).** Lelik sounded like a switchboard and forwarded
  everything. Missing context also fails silently: nobody hears a fact that was never loaded.
- **Allowlist that expands empirically.** Same silent failure. With a denylist, too much context
  fails audibly, so it can be trimmed. A denylist also picks up new domains.
- **An LLM summary of the history slice.** It adds a model call to the pickup path. Model turns are
  already stored as ≤300-char summaries.
- **Prefetching tasks, reminders or calendar.** That is Alek's data, reached through `ask_alek`.
- **Lelik-only persona tokens transcribed from the owner's conversation playbook.** It became about
  35 rules. Each technique is toxic when repeated, and instruction retention is the realtime model's
  weakest axis. Smart's `POLICY_*` set already covers much of it. Naming techniques by author (Voss,
  MI, Rogers) summons their textbook templates ("it sounds like…"). Only mechanism names whose
  canonical form is the wanted behaviour were kept: continuer, change-of-state reaction,
  dispreferred-response cushion.
- **Provider auto-interrupt plus our own cancel.** The two cancels race, and a cancel landing on
  nothing is a provider error that ends the call.
- **The provider's `idle_timeout_ms`.** It is server_vad-only, and it counts from generation end
  rather than playback end.

## Revisit if

- Real calls show Lelik saying something out of place → add that domain to the denylist, and build
  the Cabinet UI for it at that point.
- The prompt measurably degrades retention on long calls → trim history K before trimming facts.
- `FEW_SHOT_EXAMPLES_RANEVSKAYA_ZHVANETSKY` (text aphorisms) pulls speech toward one-line zingers →
  drop it from the `lelik` profile.
- Twilio stops echoing marks for cleared audio, or the provider auto-truncates over WebSocket →
  re-check `_barge_in`'s read-before-clear ordering.
