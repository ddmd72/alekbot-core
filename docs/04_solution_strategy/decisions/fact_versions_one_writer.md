# Decision: fact versions are documents, one writer per account holds a lease (Facts S2, 2026-10-23)

**Decision.** Spec: `docs/10_rfcs/FACT_LIFECYCLE_RFC.md` D3, D8, D9; owner's map: `docs/05_building_blocks/memory/README.md` §2.4, §3.
- A content change writes a new document of the same lineage and closes the old one (`superseded_by`) in one transaction. A lifecycle change is a field-masked in-place write. Every write carries the `update_time` of the document it read; a conflict re-resolves once, a second one is a tool error. Stale ids are followed forward inside the lineage.
- One memory-writing run per account: a Firestore lease per batch (fresh owner token, 900 s run + 60 s margin). A loser defers itself with a task named per 120 s bucket. Under the lease a `processing` batch older than 60 s is a zombie.
- **No per-write fence.** A run is bounded by its timeout (lease end − margin), not by a check before each write; a commit already on the wire at cancellation may land after, which the margin covers. The cache refresh in a `finally` also runs on that cancel: it rebuilds a cache document, it writes no fact.
- A `release` that finds no lease document stays silent, although it can mean "our lease expired, was taken, and was released"; only a release that finds another owner logs a WARNING.
- `consolidate_cluster` has no live caller (dry-run scripts only). If it gets one it needs the lease and the run tracker with refresh-on-failure.
- The plan's "three gathered updates of one fact: a later one may fail" cannot be reproduced on the fake (all three apply); in production a second conflict fails the call.

**Rejected.**
- An in-process lock (the old per-fact lock): it holds inside one process only, and tasks of one user run on several instances.
- A lease check inside every fact transaction: a hot document read by every write and the fact repository coupled to the lease, for a window the 60 s margin already covers.
- A lease per task: `$consolidate` and a draining task would hold it for several batches; per batch bounds a hold to one run.
- Versions in a subcollection: every query, index and vector search runs on the one collection; closed documents are already invisible by `state`.
- A TTL policy on the lease: TTL deletes late, and expiry is the `lease_until` comparison; one document per account does not grow.
- A fixed deferral name: Cloud Tasks reserves a task name for about an hour, so the next deferral would be refused.

**Revise when** a second fact writer appears (the S4 review pass takes the same lease; the janitor writes single documents under a precondition), the run timeout changes (the lease length and the 60 s zombie threshold follow from it), or a write is seen landing after its lease expired (then add the fence).
