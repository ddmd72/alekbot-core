# Decision: `AgentExecutionContext.__eq__` excludes `resilience_port`

**Status:** Adopted (2026-05-01).

## Decision

`AgentExecutionContext` overrides pydantic's default `__eq__` to compare via `model_dump(exclude={"resilience_port"})`. `__hash__` restored over the same identity fields. Two contexts that differ only by which `ProviderResiliencePort` instance they hold compare equal.

## Why

`resilience_port` is a process-local singleton — infrastructure, not identity. Code that compares contexts (e.g. `ExecutionOverride.__eq__`, cache-key derivation, deduplication) cares about routing identity (agent_type / provider / model / tier / fallback_*), not which breaker bookkeeping object is plugged in. Without this exclusion, two contexts built in different code paths (worker A vs worker B in same process — possible during async dispatch) would compare unequal despite identical routing.

## Rejected alternatives

- **Test fixture passes shared singleton instance to both contexts** (Variant P from the design pass): masks the equality bug rather than fixing it. Production code that constructs contexts in separate code paths would still hit the inequality silently. Test fixture would have to enforce singleton-sharing as a discipline.
- **Make `InMemoryProviderResilience.__eq__` compare by configuration** (failure_threshold/window/cooldown): equality on infrastructure objects is a weak signal — two distinct singletons with same config still represent independent state machines. Misleading.
- **Document "do not compare contexts" rule**: weak enforcement; easy to violate accidentally.

## Cost

- Adding `__eq__` disables pydantic's auto `__hash__` → must restore manually using identity fields.
- Future contexts holding additional process-local infra (e.g. retry policy singletons) need explicit exclusion in `model_dump(exclude=...)`.

## Triggers to revise

- If a second process-local field ever needs the same exclusion treatment → extract a `_INFRA_FIELDS: ClassVar[set[str]]` class attribute and use it both in `__eq__` and `__hash__`.
