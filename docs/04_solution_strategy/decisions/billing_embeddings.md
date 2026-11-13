# Billing: embedding calls count toward the account's daily cost

**Status:** Accepted

## Decision

Every successful Gemini embedding call adds its prompt tokens to the `TokenLedger` in scope, priced as `gemini-embedding-2`
($0.20 per 1M input tokens, `domain/billing.py`). The token count is the SDK's own: google-genai 3.3 maps the Developer
API's `usageMetadata` onto `EmbedContentResponse.usage_metadata`, and 3.3 is the floor in `requirements.txt` for that
reason (2.x dropped the field). Retries are separate API calls: only the response that came back is metered.

- **One ledger mechanism.** The ledger ContextVar, `current_ledger()` and `ledger_scope()` live in `domain/billing.py`; an
  agent execution, the embedding adapter and the background scope all use them.
- **Inside an agent execution** the tokens land on that execution's ledger and `_flush_billing` writes them.
- **Outside one**, `utils/billed_scope.py` opens a ledger for a natural unit (a drive tick, a fact-profile run, an
  email page, a Cabinet request, an MCP call, a note write) and writes one `record_usage` per model leg when the unit ends,
  awaited, best-effort, never raising. A nested scope replaces the current ledger for its block, so nothing is billed twice.
- **No scope in force:** the tokens go to a process tally, logged at INFO at most once a minute
  (`[GeminiEmbedding] unbilled tokens=… calls=…`). A response with no `usage_metadata` logs a WARNING at most once a minute.

## Rejected

- **An account parameter on `EmbeddingService`:** about 20 call sites have no account to pass.
- **A `record_usage` write per call:** a drive tick embeds many texts; one write per unit keeps the write count small.
- **Estimating tokens from characters:** a guess in the billing path; the API reports the real count.
- **An httpx response hook reading the raw body (SDK 2.x):** a workaround around the SDK. Upgrading to the release that
  exposes the field is the standard path.

## Revisit when

A batch-embedding path is added (one request for many texts, one `usageMetadata` for all): the metering already reads one
count per response, so it holds, but the per-text fan-out in `get_embeddings_batch` the scopes were sized for changes.
Also when SDK `usage_metadata` is missing in production (the WARNING above is the signal).
