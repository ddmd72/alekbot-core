# Decision: Two-Layer Provider Override (Coarse + Fine)

**Status:** Adopted — acknowledged antipattern, deferred refactor
**Date:** 2026-05-18
**Context:** Inspection finding F4.6 — `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md`

## Decision

Two independent mechanisms for overriding LLM provider selection coexist:

- **Coarse:** `UserBotConfig.agent_providers[agent_type]` — static per-agent (e.g. "Smart always Claude").
- **Fine:** `UserBotConfig.complexity_settings_overrides[complexity].provider_override` — dynamic per task-complexity inside the Smart path.

Precedence: fine wins over coarse when both are set, by virtue of
`AgentContextBuilder.resolve_for_task` reading `settings.provider_override or self.resolve_provider_name(...)`.

This shape is kept as-is. The redundancy is acknowledged as an antipattern.

## Why we keep it

The system design is still in flux. Both layers are in active use by the developer
(solo project, no external users); collapsing them now would lock in an abstraction
before the long-term need is clear. The maintenance burden is bounded — both layers
are read-only outside Cabinet UI scripting and have stable surfaces.

## Mitigation: test coverage

Coverage closes the failure mode that would otherwise justify urgent action — a future
refactor silently inverting precedence or dropping one layer.

- `tests/unit/infrastructure/test_task_execution_resolver.py::test_user_override_replaces_provider_override`
  — fine-layer merge propagates through resolver.
- `tests/unit/services/test_agent_context_builder_per_agent_provider.py::test_per_complexity_provider_override_wins_over_per_agent`
  — fine wins over coarse when both are set.
- `tests/unit/services/test_agent_context_builder_per_agent_provider.py::test_per_agent_used_when_complexity_override_absent`
  — coarse applies when fine is absent.

## Rejected alternatives

- **Delete the fine layer.** Loses per-complexity provider switching inside Smart. The
  `ComplexitySettings` struct keeps `tier` / `thinking_effort` / `intent_remap` anyway,
  so removing only `provider_override` is asymmetric.
- **Delete the coarse layer.** Loses per-agent provider control for non-Smart agents
  (consolidation, memory search, web search, etc.) — fine layer only fires through Smart.
- **Collapse into one mechanism.** Would require either lifting complexity awareness
  into every agent (intrusive) or pushing per-agent decisions through Smart's complexity
  classifier (semantic mismatch). Both are larger changes than the redundancy costs.

## Trigger to revisit

- Cabinet UI exposes either knob to non-developer users → friction of two parallel
  controls becomes user-facing.
- Long-term system-design pass converges on a single override paradigm.
- A regression is caused by precedence confusion despite the tests above.
