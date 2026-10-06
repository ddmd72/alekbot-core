# `delegate_to_specialist` loses its `mode` parameter

**Date:** 2026-10-06
**Status:** Done

## Decision

Sync/async is the intent's declared `ExecutionMode` in `agent_manifest.py`, and only that. The
per-call `mode: "now" | "later"` on `delegate_to_specialist` (commit `ba805bb`, 2026-08-25,
`COMPANION_AGENTS_RFC.md` §6) is removed: tool schema property, `DelegationEngine` parsing,
`AgentCoordinator.handle_delegation(mode_override=…)`, and Lelik's stripping of it. A stray `mode`
argument is ignored. The cycle guard shipped in the same commit stays.

## Incident

2026-10-06, the daily morning-briefing self-reminder: Smart (on `gpt-6.1-sol` since 2026-10-05;
Grok before) called `create_html_page` with `mode: "now"`. The generator ran inside Smart's turn
(205 s) and returned its page as a `document` delivery item. That turn was started by
`UserNotificationService.notify()`, which publishes the reply text only, so the page was never
stored or delivered. On every earlier day the intent ran ASYNC and `AgentWorkerHandler` delivered
it through `notify_document_link`.

## Why the switch was wrong, not just this call

The commit promised "answer me now" vs "get back to me later". The second half does not exist:

- An ASYNC Cloud Task's result is delivered **to the user** by `AgentWorkerHandler`, and only for
  intents it knows (deep research, DOCX, PDF/HTML/image, `tell_alek` failures). Nothing returns a
  result to the calling orchestrator.
- So `later` on a SYNC-declared intent (`search_web`, `search_memory`, …) computed the answer and
  dropped it — the reason Lelik already stripped `mode`.
- And `now` on a generator gave the orchestrator only a status string (`"html_page_generated"`)
  while moving the document onto a path that may not deliver it.

The parameter chose the result's **recipient** (orchestrator vs user), worded as timing. No intent
had both values meaningful. The wording made it worse: "later" reads as "postpone" for a
time-sensitive newspaper, and the description's "not needed to continue this conversation" matched
the reminder's "delegate create_html_page… *then* reply".

Data (BigQuery `prompt_content`, 30 days): OpenAI models set `mode` on every call (275 `now`,
3 `later`, 2 omitted) — "omit to use the intent's normal mode" never happened on them. Grok mostly
omitted it (2235 of 2286). Only two calls diverged from the manifest, both `now` on an ASYNC
generator; none ever used `later` on a SYNC intent, the case the feature was built for.

## Alternatives rejected

- **Teach `notify()` to deliver `delivery_items`.** `notify()` publishes an agent's reply; files
  have their own methods (`notify_document_link`, `notify_file_bytes`) called by the path that
  produced them. Fixing the symptom would leave `later` dropping answers.
- **An async-locked flag on generator intents.** A patch on a switch that has no correct use.
- **Rename to `sync` / `async`.** Clearer words, same missing return path.

## Open

A real async call whose result **returns to the orchestrator** (continue a multi-step task once a
generator finishes) is a separate feature needing its own design: where the pending task's state
lives, how Smart is woken, what the user sees meanwhile, how it fits the 25-min turn clock.
Re-adding a per-call flag is not that.
