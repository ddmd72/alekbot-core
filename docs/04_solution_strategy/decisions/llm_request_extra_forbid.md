# Decision: `LLMRequest` rejects unknown kwargs at construction time

**Status:** Adopted
**Date:** 2026-05-01
**Trigger:** R14.3 incident — `max_output_tokens` silent regression (architecture inspection follow-up).

## Context

`LLMRequest` (`src/domain/llm.py`) is the single Pydantic model that crosses every agent → adapter boundary in the multi-provider LLM layer. Pydantic V2's default `model_config` silently drops unknown fields at construction. With ~24 production and ~71 test call sites, that default created a class of regression that is uniquely hard to detect:

- 2026-03-16, commit `ec34bbae`: an AI co-author renamed the kwarg in `DocGeneratorAgent` from `max_tokens` to `max_output_tokens`.
- The renamed kwarg matched no `LLMRequest` field. Pydantic dropped it silently.
- `request.max_tokens` was therefore `None` for every DocGenerator call. Adapter fallbacks took over: Claude `claude_adapter.py:134` → `16_000` (4× truncation versus the configured 64K), Gemini → API default 8192 (8× truncation), OpenAI/Grok → kwarg dropped → provider default.
- Symptom in production: occasional Node.js DOCX scripts truncated mid-emit. No exception, no log of the silent drop.
- Detection: 2026-05-01 architecture inspection follow-up (~46 days after introduction).

The regression class is **typed-attribute drift on the domain boundary** — a wrong field name is silently allowed because the domain object is permissive. The exact same bug shape is reachable for any future rename or typo of any `LLMRequest` field.

## Decision

`LLMRequest` is configured to **reject unknown kwargs at construction**:

```python
class LLMRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ...
```

Any future construction with an unknown field raises `pydantic.ValidationError` immediately — at the call site, with the field name in the error, before any adapter or LLM call.

## Rationale

The decision applies the project rule (`feedback_clean_or_explain.md`) explicitly: **a solution that will likely require strong refactoring later does not count as clean.** Three smaller fixes were considered and rejected:

1. **Add a regression test for `max_output_tokens` specifically.** Closes one instance, leaves the class open. A future rename of any other field reproduces the same incident.
2. **Document the convention in `LLMRequest`'s docstring.** Documentation does not enforce; AI-pair-programming routinely drifts past unread comments. The whole reason this incident slipped is that the convention was implicit.
3. **Add a per-adapter validation step that rejects unknown kwargs.** Wrong layer — the adapters receive `LLMRequest` already constructed. By the time the adapter sees the request the silent drop has already happened.

The fix lives on the **domain boundary** because that is where every LLM call passes through, and because the domain layer is the only layer with zero infrastructure dependencies — the guard cannot be circumvented by a new adapter or service. AST audit at decision time confirmed the guard was safe to enable: 24 production + 71 test `LLMRequest(...)` sites had zero unknown-kwarg uses outside the offending DocGenerator call.

The trade-off is **loud failures over silent drops**. A future rename of an `LLMRequest` field that was not also updated at the call site now raises at construction. That is the desired behavior — the same shape that caught this incident at the test level (`tests/unit/agents/test_doc_generator_agent.py::TestLLMCall::test_max_tokens_matches_config` and `tests/unit/domain/test_llm_domain.py::TestLLMRequest::test_unknown_kwarg_rejected` would now fail loudly).

## Consequences

**Positive:**
- The whole class of typed-attribute-drift regressions on `LLMRequest` is closed at the construction boundary, not at any specific call site.
- Test coverage discipline (`feedback_full_test_coverage.md`) is reinforced: future field additions land with both the field declaration and its at-construction guard automatically in force.
- AI-pair-programming becomes safer: a co-authored rename that misses a call site no longer reaches production silently.

**Negative / cost:**
- Every future call site that accidentally uses a stale field name fails at construction. Acceptable — that is the *point* of the guard.
- Pydantic V2's default permissiveness is overridden, which is unusual repo-wide. If another domain object adopts the same constraint it should reference this record.

## Scope

This record applies **only** to `LLMRequest`. Other domain models in `src/domain/llm.py` (`Message`, `MessagePart`, `LLMResponse`, `ProviderCapabilities`, `UsageMetadata`, `PromptCacheConfig`, `CacheMetadata`) keep Pydantic defaults. Apply the same `extra="forbid"` constraint case-by-case if a similar incident lands on any of them — do not blanket-apply.

## References

- `src/domain/llm.py` — `LLMRequest.model_config`.
- `tests/unit/domain/test_llm_domain.py::TestLLMRequest` — guard contract tests (`test_unknown_kwarg_rejected`, `test_typo_unknown_kwarg_rejected`, `test_max_tokens_accepted`).
- `tests/unit/agents/test_doc_generator_agent.py::TestLLMCall::test_max_tokens_matches_config` — direct regression canary.
- Commit `e3b2773` — fix shipped (rename + guard).
- Commit `ec34bbae` — origin of the regression (2026-03-16).
- Project rule: `feedback_clean_or_explain.md` (clean implementation OR explicit deferral; partial fixes rejected).
