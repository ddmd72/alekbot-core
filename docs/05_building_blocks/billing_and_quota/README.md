<!-- living-doc
covers:
  src/domain/billing.py
  src/services/cost_calculator.py
  src/ports/quota_service.py
  src/adapters/firestore_quota_service.py
  src/agents/infrastructure/billing_agent.py
  src/utils/billed_scope.py
  scripts/billing/
  scripts/validation/check_pricing.py
  scripts/validation/price_consensus.py
-->
# Billing and Quota

## Purpose

This block answers one question: what did each billed call cost, and on which account does it land. It prices token
usage per model, collects it into a ledger scoped to one unit of work, writes it to the account's counters, and tells
the ops channel when a day's spend crosses the account's limit. It also holds the audit that keeps the price table
honest.

What it is not: **it is not a cap.** The owner dropped the monthly cap and ruled the daily limit advisory: crossing it
posts one alert and nothing is refused (`src/adapters/firestore_quota_service.py::FirestoreQuotaService`). A counter that
is wrong must never switch the bot off, which is a worse failure than an overspent day. `check_quota` on the account
repository exists and has no caller, by decision.

The account counters are also **not a historical source.** They were inflated before the ledger became per-execution and
mispriced before it became per-model, and the old values were left as they were. For any past cost question use the
BigQuery `prompt_content` table, never the counters.

## Owned data

**The account document** (`EnvironmentConfig.domain_accounts_collection`, one per account) holds the usage this block
writes: `src/domain/billing.py::AccountUsageStats`.

- Total, daily and monthly token and cost counters, plus `total_requests`. The daily and monthly windows are UTC and
  rotate lazily, on the first write of a new day or month; the rotation keeps a snapshot of the window that ended
  (`prev_daily_*` with `prev_daily_date`, `prev_monthly_*` with its month). Readers that run on the wall clock must go
  through `AccountUsageStats.usage_for_date` and `usage_for_month`, which resolve a day or month against those stamps
  instead of trusting the raw counters (an idle yesterday reads 0, not the last active day).
- `daily_cost_limit` on the account (default `DEFAULT_DAILY_COST_LIMIT`): the alert threshold. `daily_token_limit` and
  `monthly_cost_limit` are stored but nothing enforces them.
- Written by `FirestoreAccountRepository.increment_account_usage`, only; read by the daily summary and by the increment
  itself.

**The price tables** are constants in `src/domain/billing.py`, the one place a price is changed:
`_PRICING_PER_MILLION_TOKENS` (text and embedding models, with cache multipliers), `_IMAGE_TIER_PRICING_USD` and
`_EXTERNAL_COST_PER_UNIT` (image and video services), `_REALTIME_PRICING_PER_MILLION_TOKENS`,
`_REALTIME_PRICING_PER_SECOND` and `_REALTIME_PRICING_PER_CALL` (voice), `_TRANSCRIPTION_PRICING` (audio files).
`BATCH_DISCOUNT` halves a Gemini batch request.

**The price schedule** is `scripts/validation/price_consensus.py::PRICE_SCHEDULE`: dated price changes the providers have
announced, and `HOLD_FINAL_PRICE`, the models whose table row deliberately holds the final price of a promotion. It is
data for the audit only; no runtime code reads it.

Nothing else is stored. The ledger lives in memory for one unit of work.

## Flows

### Pricing a call

`src/domain/billing.py::calculate_cost` prices the three non-overlapping legs of a request (uncached input, output,
cache read, cache write) at the model's row. **An unknown model prices at 0.0, silently** (the same fail-open contract in
`calculate_external_cost` and `calculate_realtime_cost`), which is why a model is added to the table before any tier
points at it. `src/services/cost_calculator.py` is a re-export kept for old import paths; new code imports the domain.

Adapters must hand `calculate_cost` legs that do not overlap, the way Anthropic reports them. OpenAI reports cache-write
tokens inside its input total, so `src/adapters/openai_adapter.py::OpenAIAdapter._parse_response` subtracts both cached
and written tokens from the prompt leg and logs a warning if they overlap.

### One agent execution

1. `src/agents/base_agent.py::BaseAgent.process` wraps the whole retry loop in `_execution_billing_scope`, which opens a
   `src/domain/billing.py::ledger_scope`: a fresh `TokenLedger` held in the `_EXECUTION_LEDGER` ContextVar and bound to
   the account from the message context.
2. Each LLM turn in `BaseAgent._call_llm` adds its usage with `TokenLedger.add` under the model that actually served it
   (the failover model after a cross-provider failover, not the one asked for).
3. `BaseAgent._flush_billing` writes the ledger once at the end: `QuotaService.record_usage` with `TokenLedger.cost`
   (each model leg at its own rates, summed), `total_tokens`, and `dominant_model` as a label only.
4. `src/adapters/firestore_quota_service.py::FirestoreQuotaService.record_usage` calls
   `FirestoreAccountRepository.increment_account_usage` and, when the returned `UsageIncrement.crossed_daily_limit` is
   true, posts to the alert sink (`AlertSinkPort`, wired in `src/composition/operations_setup.py`).

The write is awaited, never detached (Cloud Run throttles CPU after the response), and **swallows its errors**: a failed
write loses that call's usage and is only logged.

### Billed work outside an agent

Indexers, the fact profile step and some web routes embed text with no agent around them. The embedding adapter adds
each response's prompt tokens to the ledger in scope (`src/adapters/gemini_embedding_adapter.py::_record_embedding_usage`).
`src/utils/billed_scope.py::billed_scope` opens a ledger for one natural unit (a drive tick, an email page, a fact profile
run, a Cabinet request, an MCP call, a note write) and records one `record_usage` per model leg when the block ends,
also on error. Callers: `src/services/drive_indexing_service.py`, `src/services/email_card_indexer.py`,
`src/services/fact_profile_service.py`, `src/services/companion_extraction_service.py`,
`src/services/drive_note_service.py`, `src/web/user_cabinet_app.py`, `src/composition/drive_mcp_setup.py`.

A scope opened inside an agent execution replaces its ledger for the block, so the inner scope bills its own usage and
the agent the rest, nothing twice. With no scope at all, embedding tokens go to a process tally
(`UNBILLED_EMBEDDING_TOKENS`) that is logged at most once a minute: a visible gap, not billed spend. A scope with usage
but no quota service or no account logs a warning and records nothing.

### Spend that bypasses the ledger

Some paths price their own spend and call `record_usage` directly with the figure they computed:

- Image generation: `src/agents/image_generation_agent.py::ImageGenerationAgent` (`calculate_external_cost`, zero
  tokens). Video: `src/services/video_generation_delivery.py::deliver_video`, booked at delivery with the real duration.
- Voice calls: `src/web/voice_control_plane_app.py::create_voice_control_plane_blueprint` (the `submit_transcript`
  route) prices each model's usage with `calculate_realtime_cost`, reports a per-call leg that has no price
  (`unpriced_realtime_legs`), and counts only token legs into the token counter (`realtime_token_count`).
- Drive transcript batches: `src/composition/drive_transcript_runner.py::DriveTranscriptRunner` prices with
  `BatchUsage`. Opened audio files: `src/services/document_service.py::DocumentService._audio` with
  `transcription_cost`.
- Deep research job: `job_main.py::_record_billing` prices with `calculate_cost` and goes through its own
  `FirestoreQuotaService`, so its crossing is announced too, but only if the job carries the alert webhook variable.

**Every writer of usage must go through `QuotaService.record_usage`.** A writer that calls the repository directly
increments the counter but never announces that day's crossing (`src/ports/quota_service.py::QuotaService`).

### Writing the counters and the alert

`FirestoreAccountRepository.increment_account_usage` is lock-free on the hot path: a pre-read, then one `update` of
server-side `Increment`s. The returned `UsageIncrement` is built from the server's post-increment value, so concurrent
callers get distinct, ordered before and after slices and exactly one of them crosses the limit. Only an increment that
finds the day or month turned takes `_increment_with_rotation`, a transaction, so the snapshot of the ended window is
made once. The alert fires on the **crossing** (`before < limit <= after`), so it fires once a day and stays quiet on a
day already over the limit. The alert needs the billing webhook variable; without it the crossing is logged as
`budget_daily_limit_crossed` and nothing is posted.

### The batched path nobody uses

`src/agents/infrastructure/billing_agent.py::BillingAgent` is built, started and registered with the coordinator. It
accepts INFORM messages carrying `account_id`, `tokens`, `cost` and `model`, batches them and flushes per account to the
same quota service. **No production code sends it a message**; executions flush through `_flush_billing` directly. Do not
route new spend through it expecting it to be the entry point, and treat any change there as dead-path work unless
someone starts sending to it.

### The daily summary

`src/services/billing_daily_summary_task.py::BillingDailySummaryTask` (the `billing_daily_summary` `/worker` task,
scheduler rows in [`SCHEDULERS.md`](../../07_deployment/SCHEDULERS.md)) posts yesterday's, the month's and the total
usage of every account that had activity, through the same alert sink. It reads the counters through
`usage_for_date` and `usage_for_month`.

### The pricing audit

`make check-pricing` runs `scripts/validation/check_pricing.py`. It resolves moving aliases (`*-latest`) to the concrete
model live, reads two voting catalogs (LiteLLM and models.dev), compares them with the table, and writes a gitignored
report. OpenRouter is shown for reference only: it is a reseller, and a lower price there is a lead, not a correction.
The verdict rule is the pure module `scripts/validation/price_consensus.py` (`CONFIRMED`, `CONSENSUS_DIFFERS`,
`SCHEDULE_DRIFT`, `SCHEDULE_STALE`, and the rest of `NEEDS_REVIEW`). **A dated `PRICE_SCHEDULE` entry outranks catalog
consensus**, because both catalogs publish today's price with no expiry and lag an announced change by days. Act on
`consensus_differs` and `schedule_drift`; verify everything else at the provider.

`scripts/billing/usage_report.py` is a separate BigQuery report (daily and per-model, cost split into uncached input,
output and cache) that reads the prices from `src/domain/billing.py`, so its arithmetic is the production one.

## Invariants and traps

- **Accumulation is per execution, never per agent instance.** An agent instance is a per-user singleton and a tool
  batch runs executions of it concurrently; instance counters once billed running totals (several times the real
  spend) and lost usage. The ContextVar is the isolation, and `ledger_scope`'s `reset` is load-bearing: an agent
  awaited inline has no task boundary, so without the reset its ledger would stay current for the caller. No ledger in
  scope means no accumulation, not a lazily created unowned ledger. Test:
  `tests/unit/agents/test_base_agent_billing_isolation.py`.
- **The price belongs to the model that ran the turn.** The ledger holds one leg per model; nothing prices an execution
  with the agent's default model. Smart resolves its tier per request and `fetch_url` runs on a cheaper tier, so the
  default under- and over-reported. Test: `tests/unit/domain/test_billing_accounting.py`.
- **An unknown model costs 0.0 without an error.** Add a price row before any tier or config points at a model.
- **Hold the final price of a promotion; never under-report.** `HOLD_FINAL_PRICE` members keep the standard price in the
  table while a promotion lasts, so spend reads high until it ends; the audit states that instead of calling it drift.
  A member must also have a `PRICE_SCHEDULE` entry, and a scheduled promotion that ends makes the verdict plain
  `confirmed`. Remove entries once the catalogs carry the change.
- **The table models one price per model.** Long-context tariffs (OpenAI and xAI double input past a threshold) are not
  modelled, so very long requests under-report.
- **Provider usage shapes differ; the adapter must normalise.** OpenAI cache writes are a subset of input; Anthropic's
  legs are separate; Gemini has no write leg. xAI voice `billable_audio_seconds` is a **running total**, so
  `src/adapters/xai_realtime_adapter.py::XaiRealtimeAdapter` emits only the increment and `VoiceCallBuffer` stays
  provider-agnostic and sums. Known remaining under-reports: silence between the last reply and hang-up, and xAI text
  input priced per token where the provider prices per event.
- **Cost is not tokens.** A per-second voice leg is priced but kept out of the token counter (`realtime_token_count`).
- **Speech-to-text of a chat voice message is not billed**, deliberately, as infrastructure overhead, with a recorded
  list of revisit triggers. Opened audio files (`DocumentService._audio`) are billed.
- **Embeddings are billed from the SDK's own `usage_metadata`**, so the Gemini SDK version floor in `requirements.txt` is
  load-bearing; a response without usage logs a warning and is not billed.
- **The increment stays lock-free.** Reintroducing a read-modify-write transaction on the hot path made concurrent
  executions abort each other and silently lose usage. The mapping of server transform results to field paths is by
  sorted path, not insertion order (`FirestoreAccountRepository._applied_value`); a mismatch falls back to an estimate
  with a warning. Test: `tests/unit/adapters/test_quota_service_kit.py`,
  `tests/unit/adapters/test_firestore_quota_service_alert.py`.
- **A new price constant belongs only in `src/domain/billing.py`.** Provider-model-shaped string literals are
  restricted elsewhere (REQ-ARCH-12); callers reference the exported constants.
- **Never write a price into a doc.** Name the constant; the table moves under you.

## Links

- Per-execution ledger and the advisory alert: [`billing_execution_scoped_ledger.md`](../../04_solution_strategy/decisions/billing_execution_scoped_ledger.md)
- Per-model pricing: [`billing_per_model_pricing.md`](../../04_solution_strategy/decisions/billing_per_model_pricing.md)
- Cache writes and voice seconds: [`billing_cache_write_and_voice_seconds.md`](../../04_solution_strategy/decisions/billing_cache_write_and_voice_seconds.md)
- Embeddings: [`billing_embeddings.md`](../../04_solution_strategy/decisions/billing_embeddings.md)
- Lock-free increment: [`billing_usage_increment_lock_free.md`](../../04_solution_strategy/decisions/billing_usage_increment_lock_free.md)
- Transcription deferral: [`transcription_cost_not_billed.md`](../../04_solution_strategy/decisions/transcription_cost_not_billed.md)
- The audit and the schedule: [`price_refresh_2026_10.md`](../../04_solution_strategy/decisions/price_refresh_2026_10.md),
  [`openai_gpt56_price_cut.md`](../../04_solution_strategy/decisions/openai_gpt56_price_cut.md),
  [`claude_sonnet_5_intro_pricing_made_permanent.md`](../../04_solution_strategy/decisions/claude_sonnet_5_intro_pricing_made_permanent.md)
- Daily summary task: [`SCHEDULERS.md`](../../07_deployment/SCHEDULERS.md)

## Where to start reading

- `src/domain/billing.py::calculate_cost` and `src/domain/billing.py::TokenLedger`: the price rule and the accumulator.
- `src/domain/billing.py::ledger_scope`: how a unit of work gets its ledger.
- `src/agents/base_agent.py::BaseAgent._flush_billing`: where an execution becomes a write.
- `src/utils/billed_scope.py::billed_scope`: the same for work outside an agent.
- `src/adapters/firestore_quota_service.py::FirestoreQuotaService.record_usage`: the write and the alert.
- `src/adapters/firestore_account_repo.py::FirestoreAccountRepository.increment_account_usage`: the counters.
- `scripts/validation/price_consensus.py::PRICE_SCHEDULE`: the audit's override data.
