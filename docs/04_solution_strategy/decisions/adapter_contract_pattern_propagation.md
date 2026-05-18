# Decision: Propagate CapturingStub + ContractRule pattern to non-LLM adapters

**Status:** Adopted — pattern proven on Gmail; mechanical extension across remaining adapters
**Date:** 2026-05-18
**Context:** Inspection finding R18.2 — `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md`

## Decision

The wire-test pattern in `tests/contracts/adapter_contracts.py` + `tests/integration/adapters/conftest.py` is the canonical shape for adapter-boundary contract testing across the codebase, not LLM-only.

Same shape applies to non-LLM adapters:
- **One CapturingStub class per external SDK / subprocess / HTTP boundary** — captures the request payload at the outermost adapter call.
- **One ContractRule per invariant** in the shared rule repository — validators dict keyed by adapter name (was: provider name).
- **Integration test** drives the real adapter through the stub and runs `Rule.validate(adapter_name, captured)`.

`ContractRule` itself needed no schema change — its `validators: Dict[str, Callable]` was already free-keyed. The key meaning generalizes from "LLM provider" to "adapter identity"; the captured-input shape generalizes from "SDK kwargs" to "whatever the adapter actually sends" (e.g. HTTP request records for Gmail).

## Why

The mechanism existed but only covered 1 of N adapter seams. Inspection finding R18.2 names six additional load-bearing surfaces. Propagating the same shape — rather than introducing a parallel test framework — preserves the rule-repository as the single source of behavioral contracts across all adapter boundaries.

## POC scope (this commit)

`GmailProviderAdapter` is the first non-LLM application:
- `GmailCapturingStub` in `tests/integration/adapters/conftest.py` — patches `aiohttp.ClientSession` at the adapter's import site; records every GET/POST as a request record `{method, url, headers, params, data}`.
- `GMAIL_AUTHORIZATION_HEADER_PRESENT` — every request carries `Authorization: Bearer <token>`. Broad invariant.
- `GMAIL_LIST_EMAILS_PAGE_TOKEN_EXCLUDES_QUERY` — when listing resumes via `page_token`, `q=` must be omitted. Narrow, load-bearing — Gmail embeds the original query in `pageToken`; passing `q=` alongside silently overrides the embedded date filter. Was previously enforced only by an inline comment.
- `tests/integration/adapters/test_gmail_contracts.py` — three tests: positive auth header check, positive pageToken-no-q check, sanity-pair (first page DOES include `q=`).

Both rules were verified to catch the regression they describe by injecting bad payloads at the validator level.

## Remaining R18.2 surface (deferred, mechanical)

Per inspection card: `firestore_indexed_email_repo`, `node_docx_runner`, `deep_research_webhooks`, `main.py` ASGI dispatcher, MCP `get_user_context` tool handler. Each follows the same shape — one stub + one or two contracts + one integration test. ContractRule repository target: ~9+ named rules.

## Rejected alternatives

- **Parallel test framework for non-LLM adapters.** Would split the rule repository, lose the "single source of truth" property the pattern was designed to enforce.
- **Generalize `ContractRule` to a typed-input class hierarchy.** No real benefit; the dict-of-callables is already polymorphic. Premature abstraction.
- **Sweep all 6 adapters in one session.** High LOC, half-done risk. Vertical-slice-per-session aligns with the project's build-process rule.

## Trigger to revise

- A contract rule needs validators across multiple adapter identities (cross-adapter invariant) — would need a clearer "scope" axis on `ContractRule`.
- An adapter boundary cannot be patched at a single SDK entry point — requires re-thinking what "wire-level capture" means for that adapter.
