# Decision: expiry is decided from content and enforced by a daily zero-LLM janitor, log-only until S5 (Facts S3, 2026-10-23)

**Decision.** Spec: `docs/10_rfcs/FACT_LIFECYCLE_RFC.md` D1, D2, D4, D5, D10, "S3 as built"; owner's map: `docs/05_building_blocks/memory/README.md` §2.7, §5.5.
- The model decides each fact's end from its content (`date`, `horizon` or `none`, a required `expiry` argument); code computes `expiry_kind`, `ttl_days` and `expires_at` from it, in the owner's timezone, and refuses a decision that is born due. A cleanup pass (Stage 2a/2b, backstop, cluster) floors a due horizon to now + 2 days instead; a closing state ignores `expiry`; directives, principles and mindset facts are pinned to `none`.
- Code owns `reported_date` per pass (clamped into the batch span on conversation and email passes, kept on cleanup passes, inherited through `derived_from` for a cleanup create) and every state change appends a `lifecycle_events` entry.
- The janitor runs daily at 09:30 Madrid: it moves every current fact past its `expires_at` to `stale` (in place, never archived or deleted, text never read), refreshes the baseline of every account it changed, and stores one snapshot per account per day. It skips a due fact with no `expiry_kind`. It is **log-only** (counts `would_stale`, writes nothing to facts) unless `FACT_LIFECYCLE_SWEEP_DRY_RUN` is an explicit off value; S5 switches it on, and only on the owner's word.
- The breaker stops an account's stales while it has stale facts and no successful review in 72 h (S3 has no review, so any backlog trips it) or more than 50. Per run, at most 50 minus the backlog are staled, oldest expiry first; the rest is `deferred` (owner, S3-D10). A job run is not retried and takes no lease.
- **No Firestore TTL policy on the facts collection, and none on the two lifecycle collections.** A TTL policy deletes documents whose field has passed; an `expires_at` here is a decision to stale a fact, so a policy would erase the owner's memory on the day it expired. A test walks the tree and fails on such a policy for these collections; the docs state the rule in words and never spell the command.
- Owner rule for S5 and S4: when unsure, keep the fact.

**Rejected.**
- Deleting or archiving an expired fact: a review decides what an expired fact is now (S4), and a wrong horizon must be reversible.
- A model-written `ttl_days` or `expires_at`: models count dates badly; the model now sends the kind and the date or days, and never sees `ttl_days`.
- Refusing a due horizon on cleanup passes: 467 of 563 live facts have a `reported_date` older than 90 days, so almost every Stage 2a rewrite would have been refused; the floor sends the fact to review soon instead.
- A janitor lease or a per-day run mark: per fact the `update_time` precondition makes a double write a conflict; only the per-run cap could be exceeded by two overlapping runs, so do not trigger a run by hand while the 09:30 run is going.
- A Scheduler retry: a retry after the 180 s deadline could overlap a running sweep.

**Revise when** S4 wires the last successful review into the breaker (interval + 72 h, RFC D6) and enqueues the review, a second janitor writer appears, or the per-run cap proves too low or high after the S5 log-only period.
