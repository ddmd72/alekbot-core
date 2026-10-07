# Usage increment: lock-free hot path, transaction only for window rotation (2026-10-07)

**Decision.** `FirestoreAccountRepository.increment_account_usage` does a plain pre-read, then one
non-transactional `update` of server-side `Increment`s. The old read-modify-write transaction runs only
when the daily or monthly window has to rotate (first call of a day/month). The returned
`UsageIncrement` comes from the server's post-increment value (`WriteResult.transform_results`), so
concurrent callers get distinct, ordered before/after slices and exactly one of them crosses the budget limit.

**Why.** Specialist executions fan out with `asyncio.gather` and each flushes its own ledger to the
same account document. The transaction aborted under that contention and exhausted the SDK's 5
attempts; `FirestoreQuotaService` swallows that by design, so usage was silently under-counted
(prod log audit C-04). Probed live: Firestore returns the new value of each `Increment`, in the
alphabetical order of the field paths — not insertion order — so results are mapped by sorted path.

**Rejected.**
- In-memory per-turn accumulation, one write at the end — brings back the shared accumulator across
  concurrent executions that billed 3.6x tokens until 2026-07-28 (`billing_execution_scoped_ledger.md`).
- Sharded counters — read fan-out on every quota check and still needs rotation logic across shards.
- Keeping the transaction and retrying more — treats the contention, not its cause.

**Known edge.** Two callers straddling the rollover each pre-read the stale stamp and both take the
transaction path; the second re-reads inside it and applies plain increments, so nothing is lost. A call
at the exact day boundary can be attributed to the neighbouring day by milliseconds — as before, `now` is
taken outside the transaction.

**Revisit when.** Sustained writes to one account document approach Firestore's ~1/s soft limit
(then shard), or Firestore stops returning transform results in path order (`_applied_value` falls back
to an estimate and logs a warning).
