# Adapter Wire Testing — Approach and Conventions

**Arc42 section:** 11 — Quality / Testing
**Last updated:** 2026-11-19

## Problem

The codebase has 1500+ unit tests but they mock at the `LLMPort` level. When an adapter
changes its translation logic (how `LLMRequest` fields are converted to SDK call arguments),
no unit test detects it. A fallback mechanism (LLM post-processing of malformed responses)
can further mask such regressions in production.

**Concrete incident:** `ClaudeAdapter` sent `tool_choice="auto"` instead of
`{"type":"any"}` when `force_tool_use=True`. Claude returned plain text instead of tool
calls. The fallback masked the bug.

## Solution: Three Test Layers

```
Agent tests         mock at Port level      ✅ test agent delegation logic
Port contract tests verify ABC structure    ✅ test interface correctness
────────────────────────────────────────────────────────────────────────
Adapter wire tests  mock at SDK level       ✅ test LLMRequest → SDK translation
Integration tests   stub + contract rule    ✅ test full adapter path vs named rule
────────────────────────────────────────────────────────────────────────
Request conformance real SDK, HTTP replaced ✅ test the request on the wire vs the provider's contract
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

adapter.client.responses.create = mock_create
await adapter.generate_content(request=LLMRequest(...))

assert captured["tool_choice"] == "required"
```

**Per-adapter SDK boundary:**

| Adapter | SDK method to mock |
|---|---|
| `ClaudeAdapter` | `adapter.client.messages.stream` — callable returning async context manager |
| `GeminiAdapter` | `adapter.client.aio.models.generate_content` — async function |
| `GrokAdapter` | `adapter.client.responses.create` — async function (Responses API since 2026-08-14) |
| `OpenAIAdapter` | `adapter.client.responses.create` — async function |

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

**`tests/integration/offline/adapters/`** — integration tests using real adapters + stubs.

`conftest.py` provides the LLM `CapturingStub` classes (it also holds the Gmail, Firestore and embedding stubs):
- `ClaudeCapturingStub` — installs on `adapter.client.messages.stream`
- `GeminiCapturingStub` — installs on `adapter.client.aio.models.generate_content`
- `OpenAIResponsesCapturingStub` — installs on `adapter.client.responses.create` (shared by the OpenAI and Grok adapters)

Integration tests call `.validate()` explicitly, making the rule being tested
machine-readable:

```python
stub = ClaudeCapturingStub.with_tool_response(...).install(adapter)
await adapter.generate_content(request=...)
FORCE_TOOL_USE_SENDS_CORRECT_MODE.validate("claude", stub.captured_kwargs)
```

### Layer 3 — Request conformance (`tests/unit/contracts/`)

Layers 1 and 2 stop at the SDK call's kwargs. The kwargs are not the request: the SDK turns `extra_body`,
`extra_headers`, `timeout` and `stream` into the body and headers that actually leave, and a wrong kwarg that the SDK
accepts still fails at the provider. Layer 3 runs the **real adapter and the real SDK** with the HTTP transport replaced,
records the request, and checks it against the provider's request contract
(`contracts/providers/<provider>/openapi.yaml`, TEST_COVERAGE_SYSTEMIC_RFC §4a, P17).

- **Capture.** `adapter.client = AsyncAnthropic(api_key="x", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(sink)))`
  (`AsyncOpenAI` for OpenAI). The sink records method, URL path, headers and the JSON body and answers 400
  `invalid_request_error`, so the adapter raises its client error and the call ends: the capture is a short-circuit,
  nothing is asserted about a response. A Claude shape that exercises the same-provider model fallback is answered 529
  first; both requests are checked.
- **Capture, aiohttp adapters** (Gmail): `tests/support/http_boundary_fake.install(monkeypatch)` replaces the session; each URL is
  routed with the smallest invented answer that lets the method run to its end, and every recorded request is checked, its URL
  rebuilt as aiohttp builds it (`URL.extend_query(params)`) and split into path and `query`.
- **Check.** `tests/contracts/openapi_contract.py::assert_request(provider, method, path, headers, body, *, query=None)`:
  closed subset, every violation on its own line; a templated contract path (`/messages/{id}`) matches the URL path.
- **Aliases** (Microsoft Graph): when the spec writes an operation under another path than the adapter sends (`/me/drive/...` and
  colon addressing `items/{id}:/{name}:` exist only as `/drives/{drive-id}/items/...`), the provider's `PROVIDERS` entry lists
  `Alias(pattern, target, reason)` rules, written into the subset as `x-alek-aliases` and applied by `assert_request` before the
  lookup (in order, first match); every rule must be used by a shape and every target must be a kept operation.
- **Shapes.** `tests/unit/contracts/<provider>_request_shapes.py` is a table `id -> LLMRequest` (+ model); the shape is an
  input only, the contract says what it must produce. The conformance test also fails when a kept field or union variant of
  the provider's operation is reached by no shape (a dead keep-list entry), when an adapter file has an SDK call site
  `self.client.<a>.<b>(` that no operation covers, and when the closed subset accepts an unknown field.
- **No recordings.** Nothing here is a recorded response. What needs a provider response (a replayed `raw_content`, the
  grounded loop's `container` continuation) is out of the slice; the shapes file names each as `NOT_REACHABLE`.
- **The Anthropic subset comes from the SDK's request types** (Anthropic publishes no reachable OpenAPI), pinned by the installed
  SDK version: a pin bump fails `test_provider_subsets.py` until `make contract-extract PROVIDER=anthropic` is re-run.

When a shape fails: the adapter sends something the contract lacks. If it is known to work (probed, or documented elsewhere),
add an `Allowed` entry to that provider's `PROVIDERS` entry in `scripts/contracts/extract_subset.py` (a factual reason and the
date), regenerate the subset and commit both together. Otherwise the adapter or the contract is wrong: fix the adapter, never the
validator and never the shape.

## Current Rules

The request rules of the LLM adapters (the repository holds more: Gmail, Firestore, the realtime sessions, Gemini batch, KMS;
list them with `grep -n "ContractRule(" tests/contracts/adapter_contracts.py`):

| Rule | Providers covered |
|---|---|
| `FORCE_TOOL_USE_SENDS_CORRECT_MODE` | claude, gemini, grok, openai |
| `GROUNDING_INJECTS_SEARCH_TOOL` | claude, gemini, grok, openai |
| `FORCE_TOOL_USE_WITHOUT_TOOLS_OMITS_TOOL_CHOICE` | claude, grok, openai |
| `NOTE_AFTER_TOOL_RESULT_SURVIVES_CONVERSION` | claude, gemini, grok, openai |
| `SCHEMA_WITH_CLIENT_TOOLS_USES_DELIVER_TOOL` | claude, grok |
| `SCHEMA_WITHOUT_TOOLS_USES_NATIVE_FORMAT` | claude |
| `STRUCTURED_OUTPUT_PDF_SENDS_NO_CITATIONS` | claude |
| `PDF_FILE_INPUT_IS_INLINE` | claude, openai |
| `TURN_ANCHOR_KEYED_WITH_TURN_KEY` | claude, gemini |

## Adding a New Rule

1. Define a `ContractRule` constant in `tests/contracts/adapter_contracts.py`.
2. Provide per-provider validators (skip providers where the feature is not applicable).
3. Import the rule in the relevant unit wire test and call `.validate()` after `generate_content()`.
4. Add an integration test in `tests/integration/offline/adapters/` that covers all applicable providers.
5. A rule about a field of the request body is also a shape in Layer 3 (below): the body must conform to the contract.

## Adding a New Adapter

When a new LLM adapter is added, **mandatory checklist:**

- [ ] Identify the SDK method that receives the final API call arguments
- [ ] Add a `CapturingStub` class in `tests/integration/offline/adapters/conftest.py`
- [ ] Create `tests/unit/adapters/test_{provider}_adapter.py` with wire tests
- [ ] Add a validator for the new provider to each applicable `ContractRule`
- [ ] Add the provider to each integration test in `tests/integration/offline/adapters/`
- [ ] Add the provider's request contract: an entry in `PROVIDERS` (`scripts/contracts/extract_subset.py`: operation, kept
      fields, union variants, open paths), `make contract-extract PROVIDER=<p>`, commit the regenerated
      `contracts/providers/<p>/openapi.yaml`, then `tests/unit/contracts/<p>_request_shapes.py` and
      `test_<p>_request_conformance.py` (Layer 3)

**A new request field in an existing adapter** (a new key in the SDK call, a new tool or content-block type, a new header):
it fails Layer 3 until it is in `PROVIDERS`. Add it to the operation's `fields` / `variants`, run `make contract-extract
PROVIDER=<p>`, commit the regenerated subset with the change, and add a shape that sends it.

## What NOT to Do

- **Do not mock at the Port level** in adapter wire tests — that is what the existing
  agent tests already do; it cannot detect translation regressions.
- **Do not intercept HTTP anywhere except Layer 3.** Layers 1 and 2 test our translation at the SDK boundary. The one allowed
  HTTP interception is `httpx.MockTransport` in `tests/unit/contracts/`, where the request on the wire is what is under test;
  `respx` and ad-hoc transport patches elsewhere stay out (fragile, SDK-version-dependent).
- **Do not define ContractRule validators inside individual test files** — they must live
  in `tests/contracts/` where they serve as the shared specification.
- **Do not introduce a port abstraction for the contract repository** — this is test
  infrastructure, not production domain. A file is sufficient. If cross-repo contract
  sharing is ever needed, adopt the Pact ecosystem (`pact-python` + Pact Broker).
