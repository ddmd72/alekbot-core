# Decision: Provider resilience layer is its own port — closure deferred

**Status:** Pending (deferred 2026-05-01) — **target shape committed, implementation deferred**.
**Trigger:** F4.5 — architecture inspection finding flagged the existing fallback chain in `BaseAgent._call_llm` as the largest acknowledged tech debt in the LLM provider subsystem.

## Context

`BaseAgent._call_llm` (`src/agents/base_agent.py:920–939`) is the single point in the codebase that every agent uses to invoke an LLM. Today it implements a one-shot fallback policy inline:

- Catches only `LLMRateLimitError` (HTTP 429) and `LLMUnavailableError` (HTTP 503).
- On either, performs a single instant retry on `fallback_provider` from `AgentExecutionContext`.
- No exponential backoff, no jitter, no provider-level circuit breaker, no timeout-driven fallback, no parse-error fallback.
- Existing coverage: 8 tests in `tests/unit/agents/core/test_base_agent_fallback.py`.

The audit identified five acceptance gaps around this code path. The author confirmed it is the largest known tech debt in §4 of the inspection. Closure has historically been deferred with the rationale "not interesting work".

This record exists because **the followup tracker (`docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md`) is gitignored** — without a tracked record the deferral context evaporates the moment the followup file is removed or the maintainer onboards a future helper.

## Decision

The clean closure is **not a series of point fixes inside `_call_llm`**. It is a new port: `ProviderResiliencePort`, with state and policy living outside `BaseAgent`.

### Target shape (specification, not implemented)

**Domain (`src/domain/exceptions.py`):**
- Add `LLMTimeoutError`, `LLMNetworkError`, `LLMServerError` as `LLMError` subclasses. Pure data classes, no I/O dependency. Existing `LLMRateLimitError` / `LLMUnavailableError` remain.

**Ports (`src/ports/provider_resilience_port.py`, new):**

```python
class ProviderResiliencePort(ABC):
    @abstractmethod
    def should_retry_same_provider(self, error: LLMError, attempt: int) -> bool: ...
    @abstractmethod
    def should_failover(self, error: LLMError, attempt: int) -> bool: ...
    @abstractmethod
    def compute_backoff_delay(self, error: LLMError, attempt: int) -> float: ...
    @abstractmethod
    async def record_failure(self, provider_name: str, error: LLMError) -> None: ...
    @abstractmethod
    async def record_success(self, provider_name: str) -> None: ...
    @abstractmethod
    def is_provider_open(self, provider_name: str) -> bool: ...
```

The last three methods carry **provider-level** state — distinct from the per-agent `CircuitBreaker` already in `base_agent.py:72`, which serves a different concern (agent crash isolation, not provider failover).

**Adapters:**
- `InMemoryProviderResilience` (`src/adapters/in_memory_provider_resilience.py`) — port implementation backing the policy. Tenacity-style exponential backoff with decorrelated jitter for `compute_backoff_delay`. Provider-level rolling-window failure tracking for `is_provider_open` (open after N failures in M seconds, half-open after K seconds).
- A future `LiteLLMResilienceAdapter` is a parallel port implementation — the LiteLLM-vs-inhouse choice is then a swap, not a refactor. The port stays. **The "LiteLLM swap" question is decoupled from this decision** by design.

**Per-LLM adapters (`claude_adapter.py`, `openai_adapter.py`, `grok_adapter.py`):**
- Wrap `await client.create(...)` in `asyncio.wait_for(_, timeout=request.timeout)` and translate `asyncio.TimeoutError` → `LLMTimeoutError`.
- Translate SDK-specific network errors → `LLMNetworkError`.
- `gemini_adapter.py:226` already does the wait_for half — needs the translation step.

**Composition (`main.py`):**
- `ProviderResiliencePort` constructed once and threaded into `AgentExecutionContext`.

**`BaseAgent._call_llm`:**

```python
for attempt in range(MAX_TURNS):
    if resilience.is_provider_open(primary): break
    try:
        return await primary.generate_content(...)
    except LLMError as e:
        await resilience.record_failure(primary, e)
        if resilience.should_retry_same_provider(e, attempt):
            await asyncio.sleep(resilience.compute_backoff_delay(e, attempt))
            continue
        if resilience.should_failover(e, attempt) and fallback:
            return await fallback.generate_content(...)
        raise
```

Policy lives in the port, not in the agent. The existing 8 tests in `test_base_agent_fallback.py` remain valid contracts at the agent boundary.

**Estimated cost:** ~150–300 LOC net new + state design + composition wiring + ~30 new unit tests + per-adapter translation work.

## Rationale

Per the project rule (`feedback_clean_or_explain.md`): every non-trivial change is binary — clean hexagonal implementation OR explicit deferral with rationale. **A solution that will likely require strong refactoring later does not count as clean.**

Each of the five audit-listed gaps (backoff / jitter / circuit breaker / timeout fallback / parse-error fallback) reads as an independent fix, but all are instances of policy that belong in **one port**. Closing them piecemeal inside `_call_llm` (e.g. "just add jitter", "just add `LLMTimeoutError` to the except clause") locks in the inline-policy-in-agent pattern that the clean closure has to invert. Every such patch becomes throwaway work the day the port lands.

## Why deferred (not done now)

- **Scope:** ~150–300 LOC + state design + composition wiring + ~30 new tests + per-adapter changes. Multi-day architectural unit, not an item-by-item sprint shape.
- **Coupling with two other Bucket E items:** F2.11 (Smart stateless) and F4.6 (two-layer provider override under indefinite UAT) both touch BaseAgent / `AgentExecutionContext` state design. Doing F4.5 in isolation risks designing a `ProviderResiliencePort` that conflicts with F2.11's statelessness target or duplicates F4.6's override surface. The three should be designed together.
- **LiteLLM evaluation is unresolved.** The audit names LiteLLM swap as one of two acceptance paths. That decision is its own portfolio-relevant call (vendor abstraction surface vs. hexagonal-port discipline) — not a sub-bullet of F4.5.

## Triggers to revisit

1. **A real production incident driven by any of the five gaps.** Most plausible: Anthropic 60s stall on long DocGenerator responses — the comment at `claude_adapter.py:104` already names this hazard. Real signal beats speculative fix.
2. **F2.11 design pass starts.** Bundle decision lands then.
3. **LiteLLM evaluation completes** (chosen or rejected with rationale).
4. **Multi-instance deployment.** Per-instance in-memory state stops being good enough — Redis/Firestore-backed circuit-breaker state needed; the architectural question becomes forced.

## What stays true while deferred

The current 8-test fallback coverage continues to assert the documented contract: 429/503 → instant fallback, no fallback → re-raise. **No silent degradation.** The deferral is honest — it does not pretend partial coverage is complete; the five listed gaps remain documented as gaps, not silently dropped.

## Consequences

**Positive:**
- The shape of the eventual fix is committed in writing. Future work does not re-derive the design from scratch.
- LiteLLM-vs-inhouse becomes a port-swap decision, not a refactor.
- Three correlated Bucket E items (F4.5, F2.11, F4.6) get designed together — avoiding the most likely failure mode (two of three landing first and forcing the third to be retrofitted).

**Negative / cost:**
- Production keeps the existing five gaps until a trigger fires. The current behavior is "instant single failover on 429/503" — workable at current scale, but a long Anthropic stall (the named hazard) results in a hung request rather than a fallback.
- Future helpers reading `_call_llm` will see inline policy and (correctly) think it is incomplete; this record is the answer to the "why isn't this a port already?" question.

## References

- `src/agents/base_agent.py:920–939` — current inline fallback.
- `tests/unit/agents/core/test_base_agent_fallback.py` — 8 tests covering the existing contract.
- `src/agents/base_agent.py:72` — per-agent `CircuitBreaker` (different concern; do not confuse with provider-level).
- `src/adapters/claude_adapter.py:104` — the comment naming the named hazard (Anthropic stall on long responses).
- `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md` — F4.5 card with full audit history (note: gitignored; this record is the durable copy).
- Coupling targets: F2.11 (Smart stateless), F4.6 (UAT layered overrides).
- Project rule: `feedback_clean_or_explain.md` (clean implementation OR explicit deferral).
