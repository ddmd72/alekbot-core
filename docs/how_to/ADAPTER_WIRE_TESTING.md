# Adapter Wire Testing — Approach and Conventions

**Arc42 section:** 11 — Quality / Testing
**Last updated:** 2026-03-08

## Problem

The codebase has 1500+ unit tests but they mock at the `LLMPort` level. When an adapter
changes its translation logic (how `LLMRequest` fields are converted to SDK call arguments),
no unit test detects it. A fallback mechanism (LLM post-processing of malformed responses)
can further mask such regressions in production.

**Concrete incident:** `ClaudeAdapter` sent `tool_choice="auto"` instead of
`{"type":"any"}` when `force_tool_use=True`. Claude returned plain text instead of tool
calls. The fallback masked the bug.

## Solution: Two Test Layers

```
Agent tests         mock at Port level      ✅ test agent delegation logic
Port contract tests verify ABC structure    ✅ test interface correctness
────────────────────────────────────────────────────────────────────────
Adapter wire tests  mock at SDK level       ✅ test LLMRequest → SDK translation
Integration tests   stub + contract rule    ✅ test full adapter path vs named rule
```

### Layer 1 — Adapter Wire Tests (`tests/unit/adapters/`)

Mock the SDK client method (not the port). Call `adapter.generate_content()` end-to-end.
Capture what kwargs the adapter sends to the SDK. Assert on those kwargs.

**Reference pattern (already used by `test_openai_adapter.py`):**

```python
adapter = SomeAdapter(api_key="test-key")
captured = {}

async def mock_create(**kwargs):
    captured.update(kwargs)
    return _make_valid_response()

adapter.client.chat.completions.create = mock_create
await adapter.generate_content(request=LLMRequest(...))

assert captured["tool_choice"] == "required"
```

**Per-adapter SDK boundary:**

| Adapter | SDK method to mock |
|---|---|
| `ClaudeAdapter` | `adapter.client.messages.stream` — callable returning async context manager |
| `GeminiAdapter` | `adapter.client.aio.models.generate_content` — async function |
| `GrokAdapter` | `adapter.client.chat.completions.create` — async function |
| `OpenAIAdapter` | `adapter.client.chat.completions.create` — async function |

**Claude-specific detail:** `messages.stream()` returns an async context manager, not a
coroutine. The mock must be a plain callable (not `AsyncMock`) that returns a context manager
object with `__aenter__`/`__aexit__` as `AsyncMock`:

```python
def capturing_stream(**kwargs):
    captured.update(kwargs)
    return mock_cm  # has __aenter__/__aexit__ as AsyncMock

adapter.client.messages.stream = capturing_stream
```

### Layer 2 — Contract Repository + Integration Tests

**`tests/contracts/adapter_contracts.py`** — the rule repository.

Contains `ContractRule` objects, each defining a named behavioral invariant with
per-provider validator callables. Rules are defined once and applied in both
unit wire tests and integration tests.

```python
@dataclass
class ContractRule:
    name: str
    description: str
    validators: Dict[str, Callable[[dict], None]]

    def validate(self, provider: str, captured_kwargs: dict) -> None:
        ...
```

**`tests/integration/adapters/`** — integration tests using real adapters + stubs.

`conftest.py` provides three `CapturingStub` classes:
- `ClaudeCapturingStub` — installs on `adapter.client.messages.stream`
- `GeminiCapturingStub` — installs on `adapter.client.aio.models.generate_content`
- `OpenAILikeCapturingStub` — installs on `adapter.client.chat.completions.create`

Integration tests call `.validate()` explicitly, making the rule being tested
machine-readable:

```python
stub = ClaudeCapturingStub.with_tool_response(...).install(adapter)
await adapter.generate_content(request=...)
FORCE_TOOL_USE_SENDS_CORRECT_MODE.validate("claude", stub.captured_kwargs)
```

## Current Rules

| Rule | Providers covered |
|---|---|
| `FORCE_TOOL_USE_SENDS_CORRECT_MODE` | claude, gemini, grok, openai |
| `GROUNDING_INJECTS_SEARCH_TOOL` | claude, gemini, grok, openai |
| `FORCE_TOOL_USE_WITHOUT_TOOLS_OMITS_TOOL_CHOICE` | claude, grok, openai |

## Adding a New Rule

1. Define a `ContractRule` constant in `tests/contracts/adapter_contracts.py`.
2. Provide per-provider validators (skip providers where the feature is not applicable).
3. Import the rule in the relevant unit wire test and call `.validate()` after `generate_content()`.
4. Add an integration test in `tests/integration/adapters/` that covers all applicable providers.

## Adding a New Adapter

When a new LLM adapter is added, **mandatory checklist:**

- [ ] Identify the SDK method that receives the final API call arguments
- [ ] Add a `CapturingStub` class in `tests/integration/adapters/conftest.py`
- [ ] Create `tests/unit/adapters/test_{provider}_adapter.py` with wire tests
- [ ] Add a validator for the new provider to each applicable `ContractRule`
- [ ] Add the provider to each integration test in `tests/integration/adapters/`

## What NOT to Do

- **Do not mock at the Port level** in adapter wire tests — that is what the existing
  agent tests already do; it cannot detect translation regressions.
- **Do not use `respx` or HTTP-level interception** — we test translation logic, not HTTP.
  The SDK is the correct boundary. HTTP mocking is fragile and provider-SDK-version-dependent.
- **Do not define ContractRule validators inside individual test files** — they must live
  in `tests/contracts/` where they serve as the shared specification.
- **Do not introduce a port abstraction for the contract repository** — this is test
  infrastructure, not production domain. A file is sufficient. If cross-repo contract
  sharing is ever needed, adopt the Pact ecosystem (`pact-python` + Pact Broker).
