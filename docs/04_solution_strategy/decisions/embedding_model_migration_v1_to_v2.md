# Decision: Migrate embeddings from gemini-embedding-001 to gemini-embedding-2

**Status:** Adopted
**Date:** 2026-05-29
**Context:** `gemini-embedding-001` scheduled for shutdown 2026-07-14 (Gemini API deprecations). Embedding spaces between `001` and `2` are documented as incompatible — full re-embedding of all stored vectors is required, not a drop-in model swap.

## Decision

Switch the embedding adapter to `gemini-embedding-2`, keep the **768-dimension** output via Matryoshka truncation, translate legacy `task_type` parameter into v2's inline instruction prefix inside the adapter, and re-embed all 28k existing vectors in dev via a one-shot script. Prod (~867 vectors total) is not used and will not be migrated as part of this change — it gets the new adapter on deploy and any future writes use the v2 space.

## Migration scope (dev, measured 2026-05-29)

| Collection | Docs | Vectors per doc | Total |
|---|---:|---:|---:|
| `development_domain_facts_v2` | 905 | 3 | 2,715 |
| `development_domain_email_facts_v1` | 6,332 | 4 | 25,328 |
| `development_task_search_index` | 8 | 2 | 16 |
| **Total** | | | **~28,059** |

At our throttled rate (~28 RPS), re-embedding takes 15–30 minutes via a local script.

## Adapter contract change (internal)

Public `EmbeddingService` interface unchanged — `get_embedding(text, task_type=…)` and `get_embeddings_batch(texts, task_type=…)` stay. Inside the adapter:

- Model: `gemini-embedding-2`.
- Config: drops `task_type`; keeps `output_dimensionality=768`.
- `task_type` is translated to an inline prefix on the input text:
  - `RETRIEVAL_DOCUMENT` → `"title: | text: {content}"`
  - `RETRIEVAL_QUERY`    → `"task: search result | query: {content}"`
  - `SEMANTIC_SIMILARITY` → `{content}` (no prefix)
  - Unknown values → `ValueError` (no silent fall-through; every caller in this repo passes a known value).

### v2 has no true batch endpoint — `get_embeddings_batch` fans out

The `gemini-embedding-2` model interprets `contents=List[str]` on `embed_content`
as **multimodal parts of one document**, returning a single embedding for the
whole list. This was empirically confirmed against the live API on 2026-05-29
(probe script: a 3-string list returned 1 embedding, not 3). The v1 SDK's
implicit-batch shape was effectively removed.

To preserve the existing `get_embeddings_batch(texts) → list[vec]` contract,
the adapter now fans out via `asyncio.gather` over N parallel single-content
`get_embedding` calls. The existing `asyncio.Semaphore` (default 20) caps
in-flight calls; at our typical batch sizes (3–4 texts) the wall-clock cost is
unchanged from the v1 batch path. Tests assert call counts and call contents
as sets (parallel-gather makes SDK call arrival order non-deterministic; result
order is still preserved by `gather`).

## Migration runtime

- One-shot `scripts/migration/migrate_to_embedding_v2.py`. Iterates each collection, regenerates the vector fields with the new adapter, writes back. Idempotent — overwriting v2 vectors with v2 vectors converges.
- User pauses bot usage during the run; Cloud Scheduler-triggered background jobs (e.g. `EmailEmbeddingRepairService`, hourly) may fire and write v2 vectors during the window — that's fine, they're in the target space.
- No Firestore index changes (768 dim preserved → all 21 vector indexes in `firestore.indexes.json` reused as-is).

## Rejected alternatives

- **Upgrade to 3072 dimensions for top MTEB quality.** Requires dropping and recreating 21 Firestore vector indexes (hours per index, write storage cost ×4). MTEB shows 768 ≈ 1536 ≈ 3072 on retrieval — no measurable win on our use case. YAGNI.
- **Dual-write + shadow-read window (write to both 001 and v2, switch reads when v2 indexed).** Doubles write cost and storage during the transition, requires per-collection migration flag tracking. Overkill for 28k vectors that migrate in 30 minutes.
- **Remove `task_type` from the public adapter API and refactor every caller.** Five+ caller files to touch (FactWriteService, EmailIndexingService, TaskIndexingService, SearchEnrichmentService, EmailEmbeddingRepairService, …). The prefix translation cleanly belongs inside the adapter — it's a serialization detail of the v2 protocol. Internal contract change, not a domain change.
- **Cloud Run Job for migration.** 30 minutes of local script work fits in any window; Job adds cold-start, job runner adapter, env wiring, log indirection for no benefit at this scale.

## Triggers to revisit

- Embedding quality regression visible in user feedback (vector search returns less relevant results) → consider 1536 or 3072 dim with index rebuild.
- Future writes need a new `task_type` (e.g. `CLASSIFICATION`, `CLUSTERING`) → extend the prefix mapping; the unknown-type `ValueError` will be the regression signal at the boundary.
- Multimodal embeddings (image/audio in the same space) become a feature request → v2 already supports them, but our pipeline (text-only inputs) would need adapter + repo schema work.

## Related

- `gemini_deep_research_adapter_removal.md` — the SDK pin bump (`google-genai>=2.0.0`) shipped earlier this session is the prerequisite for v2 embedding calls.
- `feedback_clean_or_explain.md` — chose clean cold-cut over deferred dual-write half-measure.
