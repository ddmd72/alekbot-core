# Billing: execution-scoped token ledger + advisory daily budget alert

**Date:** 2026-07-28
**Status:** Accepted

## Context

A day with no interactive use (two self-reminders: the 06:05 HTML morning briefing and
the 16:30 Valencia event radar) was reported as **$9.69 / 9,037,142 tokens** by the
Slack billing summary. BigQuery `prompt_content` — which records every LLM turn —
accounted for only **2,516,130 tokens / $3.75** over the same window. The counters were
over-reporting by ~3.6x.

Cause: the four token accumulators (`_billing_prompt_tokens` and siblings) were
**instance** attributes on `BaseAgent`. An agent instance is a per-user singleton in the
`AgentCoordinator` registry, and `DelegationEngine` dispatches a tool batch through
`asyncio.gather` — so N executions of one instance run concurrently. Each `process()`
reset the shared fields and each `_flush_billing()` billed whatever the running total
happened to be. Production logs show Smart dispatching **22 parallel `fetch_url`** calls
into a single `web_search_agent` instance at 06:05:29, then 6 and 12 more.

The regression test reproduces it exactly: ten concurrent executions billing 21 tokens
each produced `[21, 42, 63, …, 210]` — prefix sums, total 1155 instead of 210. For a
22-way batch the closed form is `21 × (22·23/2)`, an 11.5x ceiling. Production saw 3.6x
because partial interleaving means some resets land after some accumulations and simply
discard tokens — the same race also *loses* usage.

Separately, `check_quota` existed on `AccountRepository` with **no callers anywhere**.
The account was at `monthly_cost` $77.68 against a `monthly_cost_limit` of $50 and
nothing had happened. The runaway went unnoticed for a day.

## Decision

**1. Token accumulation is scoped to one execution, held in a `ContextVar`.**

`TokenLedger` (`domain/billing.py`) is a mutable accumulator created per execution by
`BaseAgent._execution_billing_scope()` and published in the module-level
`_EXECUTION_LEDGER` ContextVar. `_call_llm` adds each turn's usage to the current
ledger; `_flush_billing` reads it. The instance attributes are gone.

`ContextVar` is the correct primitive because `asyncio.gather` wraps each coroutine in a
Task and a Task copies the context at creation — so a `set()` inside one execution is
invisible to its siblings, with no locking and no plumbing through every agent's
`execute()` signature.

`reset(token)` in the scope's `finally` is load-bearing, not hygiene: a specialist
awaited *inline* by an orchestrator (no intervening Task, therefore no context copy)
would otherwise leave its own ledger current, and the orchestrator's later turns — and
its flush — would land on the specialist's ledger.

The scope wraps the whole retry loop, so retries of one execution share one ledger
(their tokens all belong to that execution). `_run_retry_loop` was split out of
`process()` purely so the scope could wrap it as one expression; behavior is unchanged.

No ledger in scope → accumulation is **skipped**, not lazily banked into an unowned
ledger that nothing would flush. Every production `_call_llm` call site sits inside an
agent's `execute()`, which only runs under `process()`, which always opens a scope.

**2. The daily budget limit alerts; it does not gate.**

New `daily_cost_limit` on `BillingAccount`, default `$5.00` (`DEFAULT_DAILY_COST_LIMIT`)
— sized against measured usage, where a normal day is ~$3.75. `increment_account_usage`
now returns `UsageIncrement(daily_cost_before, daily_cost_after, daily_cost_limit)`;
`FirestoreQuotaService` posts to the ops channel when `crossed_daily_limit` is true.

Three properties follow from detecting a **crossing** rather than testing "is over":

- It fires exactly once per day, so the alert cannot spam every subsequent request.
- It needs no second read — the transaction already held the document.
- The currently inflated counters stay quiet: a day already above the limit never
  crosses it again, and the next daily rotation resets the comparison to zero.

Alerting is advisory by owner decision. The ~$100/month cap was dropped 2026-07-26, and
a miscounted quota that silently disables the bot is a worse failure than an overspent
day. Delivery is best-effort: a dead webhook is logged, never raised into the agent's
response path.

**3. `AlertSinkPort` (`ports/alert_sink.py`) — one method, `post(text)`.**

`SlackWebhookAdapter` now declares it. Previously the adapter was passed around
duck-typed as `Any` (in `worker_handler` and, to dodge REQ-ARCH-22, in
`agent_fallback_service`); the budget alert was the third consumer, so the abstraction
paid for itself. Retrofitting the two existing call sites is deliberately **out of
scope** — they keep working unchanged.

## Alternatives rejected

- **Per-execution agent instances.** Defeats the coordinator registry and prompt
  caching for a problem that is purely about accumulator scope.
- **`asyncio.Lock` around the accumulators.** Serializes the parallel batch and still
  pools unrelated executions — it fixes the data race, not the wrong scope.
- **Keying accumulators by `task_id` on the instance.** `_call_llm` has no access to
  the task id, so it needs the same context plumbing ContextVar already provides, plus
  manual cleanup.
- **Compatibility `@property` shims over the ledger** (keeping `_billing_*` readable).
  Would have required zero test edits, but leaves a `self.`-looking API that is not
  instance state — a standing trap. Clean removal chosen; the four affected tests were
  rewritten with per-test approval, preserving their assertions and changing only how
  the accumulator is reached.
- **Hard quota enforcement via the existing `check_quota`.** Rejected by the owner:
  alert-only. Wiring it as written would also have locked the account out immediately
  (`daily_token_limit` 100k vs ~2.5M actual, `monthly_cost` already past its cap).
- **Flagging overspend in the 09:00 daily summary instead.** Nearly free, but that is
  exactly the delay that let this incident go unnoticed for a day.

## Consequences

- Counters written **before 2026-07-28 are inflated** and were deliberately left
  untouched (owner decision). `monthly_cost`/`monthly_tokens` self-correct on the 1st;
  `total_cost`/`total_tokens` stay permanently dirty. Use BigQuery `prompt_content` for
  any historical cost question, never the account counters.
- `AccountRepository.increment_account_usage` returns `UsageIncrement` instead of
  `None`. `FirestoreUserRepository.increment_usage` ignores the value; only the quota
  service consumes it.
- The alert needs `BILLING_SLACK_WEBHOOK_URL` (already set — it is the same sink as the
  daily summary). Unset → the crossing is logged as
  `event=budget_daily_limit_crossed` and nothing is posted.
- `check_quota` remains unwired. It is now dead by decision, not by oversight.
- The dead `meta`/`cost` computation in `_on_agent_success` (a leftover of the
  PromptDebugLogger removal, TD-1) was dropped; `output_text` stays in the signature
  for call-site compatibility.

## Verification

`tests/unit/agents/test_base_agent_billing_isolation.py` — 10- and 22-way concurrent
executions bill their own tokens; multi-turn executions still sum their turns; an
inline-awaited nested agent does not capture the caller's ledger.
`tests/unit/domain/test_billing_accounting.py` — ledger arithmetic and crossing
predicate incl. rotation and exact-boundary cases.
`tests/unit/adapters/test_firestore_quota_service_alert.py` — alert fires once, stays
silent when already over, survives a dead sink, and never displaces the billing write.
