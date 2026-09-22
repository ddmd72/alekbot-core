# `link_platform_identity`: transaction closes same-document races, not phantom-insert races

**Date:** 2026-09-22
**Status:** Accepted (partial fix, gap documented and deferred by owner decision)
**Scope:** `FirestoreUserRepository.link_platform_identity` (`src/adapters/firestore_user_repo.py`)

## Context

`link_platform_identity` binds a platform identity (Slack user id, Telegram chat id, phone
number) to an internal `user_id`. Before commit `e425e97` (Task 21), the method read the
target user, ran a separate query for "does any other user already have this
`platform_user_id`", and only then wrote — three round trips with no atomicity between them.
Two concurrent callers binding the same identity could both pass the conflict check before
either write landed, producing two users each believing they own the identity (most
security-sensitive for phone binding, where OTP possession is the only proof of ownership).

## Decision

Wrap the read, conflict query, and write in a single `@firestore.async_transactional`
function: `user_ref.get(transaction=transaction)`, `conflict_query.stream(transaction=transaction)`,
and `transaction.set(user_ref, ...)` all execute under one transaction handle. `tests/unit/adapters/test_firestore_user_repo_oauth.py::test_link_platform_identity_success`
now asserts both reads pass `transaction=transaction` explicitly — without that assertion, a
regression back to untransacted reads would still pass every test in the file silently, since
the mocks don't otherwise distinguish a transactional call from a bare one.

## What this closes

- **Same-document races.** If two transactions both read `user_ref` for the *same* user
  document and one of them writes to it, Firestore's contention detection re-runs the loser
  ("Firestore retries the transaction if a transaction reads documents and another client
  modifies any of those documents" — [Firestore transactions docs][fs-tx]). This covers, e.g.,
  two concurrent binds to the *same* `user_id`.
- **Read-before-write ordering.** The conflict check and the write can no longer interleave
  with an external write in a way that changes the read's answer mid-flight for the documents
  actually read.
- **Exception propagation.** `ValueError` (not found / already linked) raised inside the
  transactional closure propagates cleanly out of `_transaction(transaction)` — no swallowed
  or misattributed exceptions across the transaction boundary.

## What this does NOT close

**The actual race this method exists to prevent — two different accounts binding the same
`platform_user_id` concurrently — is not reliably closed by this fix.**

Firestore's optimistic-concurrency guarantee fires when a transaction **re-reads a document
that a concurrent transaction modified**. It is not documented to fire, and does not reliably
fire in practice, when a concurrent transaction **inserts a new document** that would newly
match a query that previously returned no results — a phantom read. In this method's actual
race:

1. Transaction A (account 1, binding phone `+1555…` to `user_a`) runs
   `conflict_query.stream(transaction=A)` → sees zero matches.
2. Transaction B (account 2, binding the same phone to `user_b`) runs the same query under
   transaction B → also sees zero matches.
3. A writes to `user_a`'s document. B writes to `user_b`'s document.

Neither transaction reads or writes a document the other one touched — A never reads
`user_b`'s document and vice versa — so there is no shared document for Firestore's
contention detector to catch the collision on. Both writes commit. Both users now believe
they own the phone number.

This is the same class of gap historically documented for Datastore/Firestore's ancestor-query
requirement for serializable transactions: query-based conflict detection across
non-overlapping document sets is not guaranteed the way same-document read/write detection is.

## Rejected (for now): uniqueness-registry collection

The textbook-correct fix is a dedicated uniqueness-registry collection, e.g.
`platform_identity_claims/{platform}:{platform_user_id}`, claimed via
`transaction.create()` inside the same transaction. `create()` on a specific document id
fails atomically if the document already exists, converting the query-based conflict check
into a same-document write conflict — exactly the case Firestore's transaction guarantee
does cover. Both racing transactions would then contend on writing the *same* registry
document, and Firestore is guaranteed to abort the loser.

**Not built now**, by explicit owner decision:

- This is a low-traffic personal exocortex — solo developer plus a handful of invited
  accounts, not a self-serve multi-tenant product.
- Triggering the uncovered gap requires two *different accounts* racing to bind the *same*
  phone number (or Slack/Telegram id) within a sub-second window — a low-realism threat
  for this system's actual current usage. Accidental double-binding by the same user
  (e.g. a double-tapped OAuth callback) largely resolves to the idempotent "already linked
  to same user" path, which does not depend on the registry.
- The current transaction is still a real, non-trivial improvement over the pre-`e425e97`
  code (three independent round trips, no atomicity at all) and was scoped as such — not
  sold as a complete fix.

## Trigger to revisit

Build the uniqueness-registry + `transaction.create()` fix **before**:

- Opening self-serve identity binding (phone/Slack/Telegram linking) to untrusted or
  adversarial multi-tenant use, where a malicious actor could deliberately race a bind
  against a legitimate user's.
- Any observed instance of concurrent-bind abuse or double-binding in production logs,
  even under the current low-traffic assumption.

## Verification

`tests/unit/adapters/test_firestore_user_repo_oauth.py` — 11 tests covering
`link_platform_identity` (success, not-found, conflict, idempotent same-user relink,
multiple platforms, timestamp update) plus the two integration-style query-pattern tests;
`test_link_platform_identity_success` additionally asserts both reads carry
`transaction=transaction`.

[fs-tx]: https://cloud.google.com/firestore/docs/manage-data/transactions
