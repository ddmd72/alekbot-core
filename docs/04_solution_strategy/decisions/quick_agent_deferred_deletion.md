# Decision: QuickResponseAgent deferred deletion (F3.10)

**Status:** Acknowledged tech debt — deferred deletion, target architecture documented
**Date:** 2026-05-18
**Context:** Inspection finding F3.10 — `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md`

## Decision

`QuickResponseAgent` is a rudiment of the early Quick/Smart split. It is no longer load-bearing as a primary-path agent and should be deleted. Deletion is **deferred** under the "don't touch what works" principle — refactor-for-refactor's-sake without portfolio gain does not earn its cost. Documented as tech debt with a clear target architecture.

## Current state (verified 2026-05-18)

Quick is half-deprecated:
- **Primary routing**: bypassed. `RouterAgent._apply_routing_rules()` always returns `smart_agent_id` (`src/agents/core/router_agent.py:491-493`). Every user message reaches Smart.
- **Fallback chain**: Quick still fires when Smart times out / fails (`AgentFallbackService.try_quick_fallback`, invoked by `ConversationHandler`). This is the "silent fallback to degraded mode" the audit specifically warned against.
- **Notification formatting**: `UserNotificationService.notify()` routes system alerts (reminders, deep research delivery, daily email review) through Quick for LLM-formatted delivery.

Functionally, Quick is now an artefact in the dispatch graph — it carries fallback + notification formatting load that survived after primary routing migrated to Smart.

## Target architecture (when deletion happens)

1. **Primary** — Smart only. No change from current.
2. **Smart hard-fail fallback** — synthetic code response. Drop `AgentFallbackService` and the Quick fallback chain entirely. When Smart fails, return a deterministic in-code apology message — no second LLM call, no "degraded LLM mode". Provider-level resilience is already in `ProviderResiliencePort` (F4.5); a second LLM-based fallback is double-belt against the same class of failure.
3. **Notification formatting** — fold the alert-formatting layer into Smart, or strip it (decide at deletion time based on actual notification quality requirements). `UserNotificationService` stops depending on Quick.
4. **Deletion scope** — `quick_response_agent.py`, factory wiring in `UserAgentFactory`, descriptor in `agent_manifest.py`, `AgentFallbackService` (entire service), `WebSearchLightAgent` (existed only for Quick's `search_web_light` intent), all related tests.

## Why deletion is deferred

- The current state is functional. Smart receives all traffic; fallback fires rarely; notification formatting works.
- Deletion is a multi-class refactor (~6-8 files + tests) with no functional change observable to users. The portfolio narrative gain is "less dead code", not "new capability".
- The codebase already has the same pattern flagged elsewhere (F3.11 "deprecation-without-deletion is a system-wide pattern"); pursuing this one without a wider sweep does not close the underlying issue.
- Project priority is functionality showcase, not internal-architecture cleanup, until release branch (see `feedback_solo_portfolio_doc_priority.md`).

## Trigger to revisit

- A Quick-related bug surfaces in production → cost of keeping the dead path exceeds cost of removing it.
- Pre-release-branch cleanup pass — fold this into a broader F3.11 deprecation-debt sweep.
- Provider resilience (F4.5) proves sufficient on its own — Smart no longer needs a separate degraded fallback.
- Notification volume / quality demands change such that QuickAgent formatting becomes a constraint rather than a service.

## Rejected alternatives

- **Delete now.** Multi-session refactor with no visible user-facing improvement. Real engineering cost, near-zero portfolio gain. The "clean OR explain" rule applies — this is the explain side.
- **Keep Quick in primary routing for cheap requests (revive cost-tier split).** Quick's strategy already defaults to Claude (not the cheap Gemini Flash Lite of earlier sessions); the cost-saving rationale no longer holds. Reviving it would require model-tier rework, not a routing flip.
- **Refactor Quick into "FallbackAgent" + "NotificationFormatter".** Narrower scope, but still substantial; same "no functional gain" objection applies. If we accept the cost of touching this surface, we may as well delete it.
