# Billing: embedding calls count toward the account's daily cost

**Status:** Accepted

## Decision

Every successful Gemini embedding call adds its prompt tokens to the `TokenLedger` in scope, priced as `gemini-embedding-2`
($0.20 per 1M input tokens, `domain/billing.py`). The count is the SDK's own: google-genai 3.3 maps the Developer API's
`usageMetadata` onto `EmbedContentResponse.usage_metadata` (2.x dropped it), so 3.3 is the floor in `requirements.txt`.
Retries are separate API calls: only the response that came back is metered.

- **One ledger mechanism:** the ContextVar, `current_ledger()` and `ledger_scope()` live in `domain/billing.py`.
- **Inside an agent execution** the tokens land on its ledger and `_flush_billing` writes them.
- **Outside one**, `utils/billed_scope.py` opens a ledger for a natural unit (drive tick, fact-profile run, email page,
  Cabinet request, MCP call, note write) and writes one `record_usage` per model leg at the end: awaited, best-effort. A
  nested scope replaces the current ledger for its block, so nothing is billed twice. A scope with spend but no wired
  quota service logs a WARNING (once a minute per site).
- **No scope in force:** a process tally, logged at INFO at most once a minute. No `usage_metadata`: a WARNING, same rate.
- **SDK 3.x side effects:** every client pins `api_version="v1beta"` (3.x defaults to v1; a move is a separate decision);
  the Gemini SDK's OTel spans are off (the instrumentation supports <3; re-enable when it does); our own `llm.call` spans
  are unaffected.

## Rejected

An account parameter on `EmbeddingService` (about 20 call sites have none); a write per call (a tick embeds many
texts); estimating tokens from characters; an httpx response hook on SDK 2.x (a workaround; the upgrade is the standard path).

## Revisit when

A batch-embedding path is added (one request, one `usageMetadata` for many texts), or `usage_metadata` goes missing in production.
