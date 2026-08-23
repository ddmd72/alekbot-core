# Decision: enforce that every LLM-calling agent has a default tier

**Status:** Implemented
**Date:** 2026-08-23

## Context

Found while investigating why `image_generation`'s prompt-crafting LLM call ran on ECO
instead of a deliberately chosen tier (surfaced from a production log showing the crafting
call resolved to `grok-4.3` rather than the expected default). Root cause: `UserBotConfig.
get_tier_for_agent()` resolves tier from `self.agent_tiers` (per-user override) → `_DEFAULT_
AGENT_TIERS` (class-level default) → `self.default_tier` (ECO unless configured) — the third
fallback fires silently, with no error and no log line, whenever an agent_type is simply
absent from the middle dict.

`_DEFAULT_AGENT_TIERS` (`src/domain/user.py`) is hand-maintained — every specialist agent
needs a line added by whoever implements it. `NEW_AGENT_PLAYBOOK.md`'s Phase 0 asks "Which
PerformanceTier?" as a design question, but never instructed adding the answer to this dict,
and nothing enforced it. Checking every `agent_type` in `agent_manifest.ALL_DESCRIPTORS`
against the dict's keys found the gap was not unique to `image_generation` — `compute` and
`tasks` had the same silent fallthrough, undetected since their own implementation.

## Decision

1. **Close all three gaps**, not just the one that prompted the investigation:
   - `"image_generation": PerformanceTier.PERFORMANCE` — a single LLM call crafts the Aurora
     prompt; RFC decision #1 for `ImageGenerationAgent` is that this is a full LLM agent
     specifically because ECO-quality crafting degrades the technique clusters (text-heavy
     layouts, asset-set style-locking) the RFC identified as needing real reasoning.
   - `"compute": PerformanceTier.ECO` — matches the agent's own roster description
     ("Compute (SYNC, ECO)"); it runs Python in a Gemini `code_execution` sandbox, mechanical
     rather than reasoning-bound. This was already the *de facto* behavior for any user on
     the global ECO default — the fix makes it explicit and immune to a user's
     `default_tier` override changing compute's cost/behavior as a side effect.
   - `"tasks": PerformanceTier.BALANCED` — multi-turn CRUD over `TasksProviderPort`
     (search-before-mutate, recurrence patterns) is real reasoning, not a mechanical pass;
     matches the tier given to comparable multi-turn specialists (`web_search`,
     `deep_research`).

2. **Add a regression test** (`tests/unit/domain/test_user.py::
   test_every_llm_agent_has_a_default_tier`) that enumerates every `agent_type` in
   `agent_manifest.ALL_DESCRIPTORS`, subtracts a small explicit `_ZERO_LLM_AGENT_TYPES`
   allowlist (`help`, `file_management` — verified by grep that neither module calls
   `_call_llm`/`self.llm`/`self._llm` at all), and fails if anything remains outside
   `_DEFAULT_AGENT_TIERS`'s keys. A tier is meaningless for a zero-LLM agent, so the
   allowlist is the correct escape hatch, not a broadened dict.

3. **Update `NEW_AGENT_PLAYBOOK.md` Step 3** to state explicitly that the
   `AgentProviderStrategy.STRATEGIES` entry (provider resolution) and the
   `_DEFAULT_AGENT_TIERS` entry (tier resolution) are two separate, independently-required
   steps — the previous wording made only the first an explicit file edit, leaving the
   second to be inferred from a Phase 0 question with no corresponding "write it here"
   instruction.

## Alternatives rejected

- **Make `get_tier_for_agent` raise or log on a missing class-level default**, instead of
  silently falling through to `self.default_tier`. Rejected: `self.default_tier` is a
  legitimate, intentional resolution path for genuinely unknown/dynamic agent types (see
  `test_get_tier_for_agent_returns_default_tier_for_unknown_agent`) — turning that into a
  hard failure would break that contract. A compile-time-ish test against the known, finite
  set of registered agents is the correct place to catch a *known* agent missing its entry,
  not a runtime change to a deliberately permissive fallback.
- **Derive `_DEFAULT_AGENT_TIERS` requirements from a new field on `AgentDescriptor`**
  instead of a separate test-side allowlist for zero-LLM exemption. Would keep the two
  facts (agent type, whether it needs a tier) in one place, but touches every existing
  `AgentDescriptor` construction site for a property only two agents need to declare.
  Rejected as disproportionate; revisit if the zero-LLM list grows past a handful.

## Consequences

- Any future specialist agent that makes an LLM call and is added to `ALL_DESCRIPTORS`
  without a `_DEFAULT_AGENT_TIERS` entry now fails `make test-unit` immediately, with a
  message naming the missing agent_type and pointing at both remediation paths (add the
  entry, or add to the zero-LLM allowlist).
- `compute` and `tasks`'s effective tier does not change for users currently on the global
  ECO default (compute) — `tasks` moves from whatever `self.default_tier` happened to be
  (ECO for most users) to a pinned BALANCED, which is a real behavior change for any user
  whose `default_tier` is ECO. Not expected to be user-visible as a regression (BALANCED is
  strictly more capable), but it is a cost change for tasks-heavy users on the ECO default.
- The playbook fix is process-only — it does not prevent a future implementer from ignoring
  the instruction, only the test does that. The instruction exists so the test's failure
  message points somewhere authoritative.

## Verification

`tests/unit/domain/test_user.py::test_every_llm_agent_has_a_default_tier` — enumerates
`ALL_DESCRIPTORS`, currently green with all 14 LLM-calling specialists present in
`_DEFAULT_AGENT_TIERS` and 2 zero-LLM agents allowlisted. Full `make test-unit` and
`ruff check src/` green after the change.
