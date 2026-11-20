<!-- living-doc
covers:
  tests/contracts/
  contracts/
  scripts/contracts/
  .github/workflows/spec_drift.yml
  tests/integration/offline/adapters/
-->
# Adapter Wire Testing

**Arc42 section:** 11 — Quality / Testing

## Why

Agent tests mock at `LLMPort`, so they cannot see an adapter change how it turns an `LLMRequest` into SDK arguments.
A fallback that post-processes a malformed answer can hide such a regression in production. The incident behind this
protocol: `ClaudeAdapter` sent `tool_choice="auto"` instead of `{"type":"any"}` for `force_tool_use=True`, Claude
answered in plain text, and the fallback masked it.

Rule: **SDK-level tests prove translation; transport-level tests prove conformance.** Never mock at the Port in an
adapter test.

## The layers

| Layer | Boundary | Proves |
|---|---|---|
| 1. Wire tests, `tests/unit/adapters/` | the SDK method, mocked | `LLMRequest` becomes the right SDK kwargs |
| 2. Contract rules + offline integration, `tests/contracts/adapter_contracts.py`, `tests/integration/offline/adapters/` | the same SDK method, a capturing stub | a named behavioural rule holds for every provider it applies to |
| 3. Conformance, `tests/unit/contracts/` | the real SDK, HTTP transport replaced | the request on the wire, and the response the adapter reads, match the provider's contract |
| Recordings, `scripts/contracts/record.py` | real provider | the provider's actual exchanges (paid, never in CI) |

### Layer 1 — wire tests

Mock the SDK client method (not the port), call `adapter.generate_content()` end to end, capture the kwargs, assert on
them. The method to mock is the one the adapter's final call goes through: `ClaudeAdapter` →
`client.messages.stream`; `GeminiAdapter` → `client.aio.models.generate_content`; `OpenAIAdapter` and `GrokAdapter` →
`client.responses.create`. Non-LLM adapters mock the boundary they own (`aiohttp.ClientSession`, the KMS client, the
websocket), never the port they implement.

Trap: `messages.stream()` returns an async context manager, not a coroutine. The mock is a plain callable that returns
an object whose `__aenter__`/`__aexit__` are `AsyncMock` (`AsyncMock` for the method itself fails). The stub
`tests/integration/offline/adapters/conftest.py::ClaudeCapturingStub` already does this.

### Layer 2 — contract rules and offline integration

`tests/contracts/adapter_contracts.py::ContractRule` is a named invariant with a validator per provider. Rules live only
there (shared specification) and are applied in both the unit wire tests and the integration tests, which call
`.validate(provider, captured_kwargs)` explicitly so the rule under test is machine-readable. List the rules with
`grep -n "ContractRule(" tests/contracts/adapter_contracts.py`; a provider a rule does not apply to has no validator.

There is one capturing stub per SDK boundary in `tests/integration/offline/adapters/conftest.py` (the OpenAI one is shared
by the OpenAI and Grok adapters).

### Layer 3 — conformance (`tests/unit/contracts/`)

Layers 1 and 2 stop at the SDK call's kwargs, but the SDK turns `extra_body`, `extra_headers`, `timeout` and `stream`
into the body and headers that actually leave, and a kwarg the SDK accepts can still fail at the provider. Layer 3 runs
the **real adapter and the real SDK** with only the HTTP transport replaced, records the request and checks it against
the provider's contract `contracts/providers/<provider>/openapi.yaml` (`docs/10_rfcs/TEST_COVERAGE_SYSTEMIC_RFC.md` §4a).
The contract is a closed subset: only the fields, union variants and paths the adapters use; anything else is a
violation.

- **Capture.** `httpx.MockTransport` under `AsyncAnthropic` / `AsyncOpenAI` (`max_retries=0`); the sink answers 400 so
  the adapter ends in its client-error path (a Claude shape that exercises the same-provider model fallback is answered
  529 first, and both requests are checked). Nothing is asserted about a response here. aiohttp adapters (Gmail, Graph):
  `tests/support/http_boundary_fake.py` replaces the session (Graph wraps its `FakeSession` to add `put`); the URL is rebuilt as aiohttp builds it
  (`URL.extend_query`).
- **Check.** `tests/contracts/openapi_contract.py::assert_request` checks the request as sent (the decoded
  path includes the server's base path), one violation per line. A field the source marks `deprecated` fails in a
  request unless the contract allows it (an `Allowed` entry); a response accepts it.
- **Shapes.** `tests/unit/contracts/<provider>_request_shapes.py` maps a shape id to an `LLMRequest` (claude, openai; the Gmail and Graph files list adapter-method calls, and URLs
  they leave uncovered go to `NOT_COVERED`); a shape is only an
  input, the contract says what it must produce. The conformance test also fails for a kept field or union variant that
  no shape reaches (dead keep-list entry), an adapter SDK call site `self.client.<a>.<b>(` no operation covers, and a
  closed subset that accepts an unknown field. What needs a provider response (a replayed real answer, the grounded loop's
  `container` continuation) is named in the shapes file's `NOT_REACHABLE` and covered by the recorder.
- **Aliases** (Microsoft Graph). When the spec writes an operation under another path than the adapter sends,
  `scripts/contracts/extract_subset.py::Alias` rules (in the provider's `PROVIDERS` entry) are written into the subset as
  `x-alek-aliases`; `assert_request` applies the first match before the lookup and requires `raw_path=` (still
  percent-encoded) for such a contract. Every rule must be used by a shape and every target must be a kept operation.
- **Anthropic's subset** is extracted from the SDK's request types (no reachable OpenAPI), so an SDK pin bump fails
  `tests/unit/contracts/test_provider_subsets.py` until `make contract-extract PROVIDER=anthropic` is re-run.
- **Response conformance** (`test_<provider>_response_conformance.py`). The contract also carries the responses the adapter
  reads (`Operation.responses`): fields read and union variants kept; a top-level source field not read stays by
  name as `x-alek-unread` so the object stays closed (below the top level every kept object is closed). `assert_response` / `assert_stream` (Anthropic always streams) resolve the
  path exactly as `assert_request` does. Samples (`<provider>_response_samples.py`) are bodies as the wire carries them;
  each is served through the real SDK and adapter, and replayed as `raw_content` in the next request, which must conform.
  A sample is a test input, not a recording. Meta-tests require every kept top-level field and variant in a sample, and an `Allowed` response gap in a sample or a
  committed recording.
- **Recordings.** `make contract-record PROVIDER=openai|anthropic [ONLY=<scenario>] [MAX_USD=] [DRY_RUN=1] [ENV_FILE=]`
  (`scripts/contracts/record.py`) runs named scenarios through the real adapter with a transport that forwards to the
  provider and writes `tests/fixtures/providers/<provider>/<scenario>.json`. Never edit a recording by hand. Always run
  `DRY_RUN=1` first (plan and estimate, no network or key). The script enforces the budget: it stops the run
  before a call whose pessimistic estimate would pass `MAX_USD` (nothing is sent) and stops when a call with tokens costs $0 (unpriced model in `billing.py`).
  Keys come from the environment (the Makefile exports `.env`) or `ENV_FILE`. Headers are scrubbed to an allowlist. A scenario that ends in a provider error is written with that outcome: it is a
  finding. `test_recorded_exchanges.py` and `test_recorded_exchanges_replay.py` check every committed recording against
  both contracts and re-run its scenario through the real adapter; a mismatch means re-record.
- **Spec drift.** `.github/workflows/spec_drift.yml` re-fetches the providers' published specs weekly and diffs them
  against `contracts/spec_drift/`; it never turns red on drift, the monthly provider-contract review reads it.

When a shape fails, the adapter sends something the contract lacks. If it is known to work (probed or documented), add
an `Allowed` entry (`scripts/contracts/extract_subset.py::Allowed`, with a factual reason and date) to that provider's
`PROVIDERS` entry, regenerate the subset and commit both together; a recording that shows the provider sending or
accepting the field is the best evidence (`Allowed(null=True)` for a null, `variant=` to keep the entry to the one block
type the evidence shows). Otherwise fix the adapter, never the validator and never the shape.

## Procedures

### Adding a rule

0. **Owner confirmation first.** No `ContractRule` or validator is added, changed or removed without the owner's explicit
   confirmation in the current session (policy at the top of `tests/contracts/adapter_contracts.py`). A failing rule
   means the adapter is wrong until the owner says otherwise.
1. Define a `ContractRule` constant in `tests/contracts/adapter_contracts.py` with a validator per applicable provider.
2. Import it in the relevant unit wire test and call `.validate()` after `generate_content()`.
3. Add an integration test in `tests/integration/offline/adapters/` covering all applicable providers.
4. A rule about a request-body field is also a Layer 3 shape.

### Adding an adapter (mandatory checklist)

- [ ] Identify the SDK method that receives the final call arguments.
- [ ] Add a `CapturingStub` to `tests/integration/offline/adapters/conftest.py`.
- [ ] Create `tests/unit/adapters/test_{provider}_adapter.py` with wire tests.
- [ ] Add a validator for the provider to each applicable `ContractRule`, and the provider to each integration test.
- [ ] Add the request contract: a `PROVIDERS` entry in `scripts/contracts/extract_subset.py` (operation, kept fields, union
      variants, open paths), `make contract-extract PROVIDER=<p>`, commit the regenerated
      `contracts/providers/<p>/openapi.yaml`, then `<p>_request_shapes.py` and `test_<p>_request_conformance.py`.
- [ ] Add the responses the adapter reads (`Operation.responses`), regenerate, then `<p>_response_samples.py`,
      `test_<p>_response_conformance.py` and the provider's scenarios in `scripts/contracts/record.py::SCENARIOS`; every scenario not listed in
      `UNREACHABLE` needs a committed (paid) recording, or `test_recorded_exchanges.py` fails.

**A new request field in an existing adapter** (a new SDK-call key, tool or content-block type, header) fails Layer 3
until it is in `PROVIDERS`: add it to the operation's `fields` / `variants`, regenerate, commit the subset with the
change, and add a shape that sends it.

**A response field or block the adapter starts reading** (or one a recording shows the provider sends): add it to the
response's `fields` / `variants` (or an `Allowed` with a schema when the source lacks it), regenerate, and add a sample
that carries it.

## What NOT to do

- Do not mock at the Port level in adapter wire tests; agent tests already do, and it cannot detect translation regressions.
- Do not intercept HTTP outside Layer 3. Layers 1 and 2 test translation at the SDK boundary. The allowed interception is
  `httpx.MockTransport` in `tests/unit/contracts/` (where the wire is what is under test) and the scripted provider in
  `tests/unit/scripts/test_contract_record.py`; `respx` and ad-hoc transport patches elsewhere are fragile and
  SDK-version-dependent.
- Do not define `ContractRule` validators inside test files; they live in `tests/contracts/` as the shared specification.
- Do not put a port in front of the contract repository: it is test infrastructure. If cross-repo sharing is ever needed,
  adopt Pact (`pact-python` + Pact Broker).
