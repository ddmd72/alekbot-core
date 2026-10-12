# Decision: An embedding-model change re-embeds in place; the dual-field switch is not built

**Date:** 2026-10-12
**Status:** Accepted, deferred (User Drive Step 2, Slice 1)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §12.3, RFC Δ25.

**Decision:** The index holds one card vector field. The sync state records the space its cards are in
(`applied_space`); when the service's space changes (`GeminiEmbeddingAdapter.EMBEDDING_SPACE`), the upgrade pass treats
every card embedded elsewhere as stale and re-embeds it in place, the old vector serving until the new one replaces
it. While any card is in the old space, search skips the card stream and answers from names and filters only, saying
so (`refreshing`). The RFC's switch — a second vector field written next to the old one during the transition, with
search moving to it when a full pass finds none left — is not built.

**Rejected alternatives:**
- *Build the dual-field switch now:* a second vector index per user filter, a write path that fills both fields, and
  a field switch in search, for a model change nobody has planned; Slice 1 cards are cheap to re-embed.
- *Mix spaces during the pass:* a kNN over vectors from two models ranks by meaningless distances.
- *Purge and rebuild on a model change:* search would find nothing at all for the length of an onboarding.

**Trigger to revisit:** a planned embedding-model or dimension change once chunks exist (Slice 2), where re-embedding
takes long enough that names-only search for its duration is not acceptable.
