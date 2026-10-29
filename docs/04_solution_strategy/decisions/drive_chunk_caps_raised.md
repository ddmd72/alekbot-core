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

**Rejected.**
- A scoped-books design (index a book only when searched in its folder): more machinery for a solo-developer system.
- A latency benchmark before raising: the caps are cheap to lower again.

**Revisit when** `[DriveSearch]` latency rises after the long files are requeued, or `embed_timeout` appears in the
tick summary.
