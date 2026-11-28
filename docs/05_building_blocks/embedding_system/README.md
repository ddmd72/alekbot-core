<!-- living-doc
covers:
  src/adapters/gemini_embedding_adapter.py
  src/ports/embedding_service.py
  src/domain/vector_math.py
-->
# Embedding System

## Purpose

This block turns text into vectors so that memory facts, drive cards and notes, email cards, tasks and other records can be searched by meaning
and so that near-duplicate facts can be detected. Every vector the system stores or compares comes from one port,
`src/ports/embedding_service.py::EmbeddingService`, so callers never name a provider. The only implementation is
`src/adapters/gemini_embedding_adapter.py::GeminiEmbeddingAdapter`. The pure similarity math lives next to it in
`src/domain/vector_math.py::cosine_similarity`.

What it is not: it stores nothing. The vectors live in the owners' collections (see [memory](../memory/README.md), [user_drive](../user_drive/README.md)) and the Firestore KNN indexes over them are
declared in `config/firestore.indexes.json`.

## Owned data

None. The block owns one value that other blocks persist: `GeminiEmbeddingAdapter.EMBEDDING_SPACE`
(`"{model}/{dimension}"`). Composition injects it into the drive index, which stamps it on each card and re-embeds cards
whose stamp differs (`src/composition/service_container.py`, `src/composition/drive_note_setup.py`;
`docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md`).

## Flows

1. **Single text.** `GeminiEmbeddingAdapter.get_embedding` prefixes the text for its task type
   (`_apply_task_prefix`), then calls `_embed_with_throttle`, which returns the first embedding of the response.
2. **Batch.** `GeminiEmbeddingAdapter.get_embeddings_batch` validates every task type first (an unknown one fails before
   any API call), then runs one `get_embedding` per text under `asyncio.gather`. Order is preserved. The port docstring
   says "a single API call"; the adapter makes N calls, because `gemini-embedding-2` treats a list of strings as parts of
   one document and returns one vector.
3. **Throttled call.** `_embed_with_throttle` takes the process-local semaphore (`GEMINI_EMBED_CONCURRENCY`, default
   `_DEFAULT_CONCURRENCY`), then runs `retry_async` under `DEFAULT_RETRY_POLICY` around one SDK call in
   `asyncio.to_thread`. Backoff sleeps happen while the slot is held.
4. **Error mapping.** 429 and 503 (and SDK timeouts, as 503) are typed transient errors and retried; any other 4xx
   is `LLMClientError` with `http_status`, any other 5xx `LLMServerError`, neither retried. A 400 refuses that one
   input, so the drive indexer backs off only that card.
5. **Metering.** After a successful response `_record_embedding_usage` adds `usage_metadata.prompt_token_count` to the
   ledger in scope (an agent execution, or `src/utils/billed_scope.py::billed_scope`). With none in scope the tokens go
   to `UNBILLED_EMBEDDING_TOKENS` and a rate-limited log line. Retried attempts are not metered, only the response
   that came back (`decisions/billing_embeddings.md`).

Task-type rule: the stored side embeds with `RETRIEVAL_DOCUMENT` (also the default when no task is passed); the query
side embeds with `RETRIEVAL_QUERY`. `SEMANTIC_SIMILARITY` has no caller outside the adapter: fact dedup compares stored
vectors with `cosine_similarity` (read side `src/services/search_enrichment_service.py::SearchEnrichmentService._deduplicate_semantic`). Entry points:
`src/services/fact_write_service.py::FactWriteService._generate_multi_vectors` (stored, three vectors per fact from
`src/domain/fact_embedding.py::fact_embedding_texts`) and `src/services/drive_search_service.py` (query).

## Invariants and traps

- **One vector space.** Stored and query vectors must come from the same model and dimension. The dimension
  (`_OUTPUT_DIM`, Matryoshka truncation of the native output) must match the Firestore KNN indexes. A change of
  `_MODEL`, `_OUTPUT_DIM` or `_TASK_PREFIXES` changes every vector. Bumping `EMBEDDING_SPACE` protects only the drive
  cards and notes (the drive index compares the stamp and re-embeds); facts, email cards, tasks and other vector
  owners carry no stamp, so they need an explicit re-embed or old and new vectors are compared silently
  (`tests/unit/services/test_drive_search_embedding_space.py`; `decisions/embedding_model_migration_v1_to_v2.md`).
- **The task is an inline prefix, not a parameter.** v2 has no `task_type` config; `_TASK_PREFIXES` rewrites the text.
  Stored text and query text use different prefixes by design. An unknown task type raises `ValueError`
  (`tests/unit/adapters/test_gemini_embedding_adapter.py`).
- **Batch is fan-out.** Do not swap in `contents=[...]`: it returns one embedding, not N
  (`tests/unit/adapters/test_gemini_embedding_adapter.py::test_get_embeddings_batch_calls_embed_content_per_text`).
- **Retry is per text**, inside the semaphore, so one text's 503 does not re-run the batch.
- **The semaphore is per process.** The cap is not shared across Cloud Run instances. It exists to stay under the
  provider's per-second burst limiter; its default is a fraction of the per-minute quota
  (`_DEFAULT_CONCURRENCY`). Raise `GEMINI_EMBED_CONCURRENCY` only after the tier
  or provider changes.
- **No HTTP timeout by default in the SDK.** `_HTTP_TIMEOUT_MS` is set on the client; without it a hung connection holds
  its worker thread forever. API version stays `GENAI_API_VERSION` on purpose; moving to `v1` is a separate decision.
- **Unbilled tokens are visible, not lost.** A call outside any billing scope lands on `UNBILLED_EMBEDDING_TOKENS`;
  a response without usage metadata logs a warning and is unbilled.

## Links

- `decisions/embedding_model_migration_v1_to_v2.md` — model, dimension and prefix choice.
- `decisions/billing_embeddings.md` — usage metering.
- `decisions/typed_retry_policy.md` — the retry policy the adapter shares with the LLM path.
- `docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md` — the embedding-space stamp.
- Adapter rules: `src/adapters/CLAUDE.md`.

`decisions/` = `docs/04_solution_strategy/decisions/`.

## Where to start reading

- `src/ports/embedding_service.py::EmbeddingService`
- `src/adapters/gemini_embedding_adapter.py::GeminiEmbeddingAdapter._embed_with_throttle`
- `src/adapters/gemini_embedding_adapter.py::_apply_task_prefix`
- `src/adapters/gemini_embedding_adapter.py::_record_embedding_usage`
- `src/domain/vector_math.py::cosine_similarity`
