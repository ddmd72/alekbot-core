# Decision: `AgentExecutionContext.resilience_port` — required field, TYPE_CHECKING-imported

**Status:** Adopted (2026-05-01).

## Decision

`AgentExecutionContext` carries `resilience_port: ProviderResiliencePort` as a **required** pydantic field. Composition layer (`AgentContextBuilder`, constructed by `ServiceContainer`) MUST inject the per-process singleton. Missing wiring fails at `pydantic.ValidationError` construction time, not silently at runtime.

The type reference lives behind `if TYPE_CHECKING:` in `src/ports/llm_port.py`; the runtime annotation is `Any`. Static type-checkers see `ProviderResiliencePort`; runtime treats the value as duck-typed (validated implicitly by callers calling `.is_provider_open` / `.record_*`).

## Rejected alternatives

- **`Optional[ProviderResiliencePort] = None` + `_call_llm` raises if missing**: silent `is None` checks creep in over time; "no resilience tracking when None" becomes an accidental fallback. Required field surfaces the wiring contract at the data shape, where pydantic enforces it.
- **`Optional[...] = None` + `tests/conftest.py` fixture supplies a default**: hidden default in test infra; production wiring regressions invisible to tests. Defeats fail-fast intent.
- **Runtime import of `ProviderResiliencePort` in `llm_port.py`**: violates REQ-ARCH-06 (ports must not import other ports). Adding a `CROSS_PORT_WHITELIST` entry would precedent-set whitelist growth for any future cross-port type reference; whitelist exists for genuinely irreducible cases only.
- **Move `AgentExecutionContext` to its own module**: same cross-port import problem, just relocated. Co-location with `LLMPort` is correct (the context is shaped around the port reference).

## Cost

- Mass-update of 53 test sites that construct `AgentExecutionContext` to add `resilience_port=InMemoryProviderResilience()` kwarg (Phase 1 adapter doubles as canonical test double — pure in-memory, no I/O).
- 3 `AgentContextBuilder` test fixtures updated for the same reason on `AgentContextBuilder.__init__`.
- Static type guarantees on `ctx.resilience_port` only hold for callers that import `ProviderResiliencePort` themselves (TYPE_CHECKING erasure to `Any` at runtime).

## Triggers to revise

- If a third type ever needs a TYPE_CHECKING workaround on `AgentExecutionContext` → revisit moving the context to a `composition/`-layer module that's permitted to bridge ports.
