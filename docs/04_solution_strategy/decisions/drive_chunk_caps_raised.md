# Drive chunk caps raised (2026-10-29)

**Decision (owner).** Long books are indexed in full instead of being cut at 400 chunks:

| Constant | Was | Now |
|----------|-----|-----|
| `MAX_CHUNKS_PER_FILE` | 400 | 1 000 |
| `MAX_CHUNKS_PER_USER` | 50 000 | 150 000 |
| `EMBED_FILE_BUDGET_S` | 180 | 300 |

`MAX_TEXT_CHARS` (2 M characters) stays and binds first: about 925 chunks of 2 400 characters, so the per-file cap
is a ceiling that is not reached.

**Embedding budget.** A file's chunks are embedded one call per chunk, `EMBED_IN_FLIGHT` (4) at a time, under one
`asyncio.wait_for(EMBED_FILE_BUDGET_S)`. 1 000 chunks are 250 rounds: 180 s needs 0.72 s per call, 300 s allows 1.2 s.
A timeout costs the whole file (`embed_timeout`, read again), so the margin was widened. The tick-deadline test holds:
start budget 480 + download 120 + extract 120 + embed 300 + margin 60 + model 600 = 1 680 < 1 800 s.
A re-embed waits twice (chunks, then hints) and downloads nothing: its start gate (`can_start_reembed`) uses
2 × 300 + 60 = 660 s.

**Accepted consequence.** A folder holding several long books passes `FOLDER_INMEM_CHUNKS` (2 000), and its search
falls back to the user-wide chunk streams with the folder filter (covered by
`test_a_folder_over_the_in_memory_chunk_bound_uses_the_wide_streams_with_the_folder_filter`).

**Measured after raising (spike S3, 2026-10-29).** Synthetic chunks (random 768-dim vectors, 2 400-character texts)
in the test database, timed from a Cloud Run Job in the service's region, 30 sequential calls per point:

| Chunks of the user | kNN, limit 300, p50 / p95 | limit 1 000, p50 / p95 |
|--------------------|---------------------------|------------------------|
| 10 000 | 0.24 / 0.29 s | 0.39 / 0.45 s |
| 50 000 | 0.82–0.86 / 1.05–1.24 s | 0.88–0.91 / 1.02–1.24 s |
| 150 000 | 2.37 / 2.70 s | 2.50 / 2.92 s |

- The kNN is exact: time grows linearly with the user's chunks (about 15 ms per 1 000), billed one read per 100
  index entries plus one per returned document.
- A user next to 244 000 chunks of another user measured the same as alone: the `user_id` prefix bounds the scan, so
  the cap is per user and other tenants do not slow a search.
- Four parallel kNN calls take about 15 % longer than one; no errors, no rising tail over a series (no throttling).
  A search runs its card, chunk and hint kNN in parallel, so what a user waits for is about the unfiltered chunk kNN;
  a kind filter scans only that kind.
- About 2.4 s per search at the cap is accepted for drive search (owner, 2026-10-29): it is not the memory fast path.
- Timings from outside Google Cloud are not decision-grade: they add the round trip and the transfer of the results.

**Rejected.**
- A scoped-books design (index a book only when searched in its folder): more machinery for a solo-developer system.

**Revisit when** `[DriveSearch]` latency rises after the long files are requeued, or `embed_timeout` appears in the
tick summary. Past the caps (a user at the card or chunk cap, or a drive of tens of thousands of files) the answer is a
vector store with approximate search behind the `DriveIndexRepository` port. That decision is taken only on timings
measured from inside Google Cloud.
