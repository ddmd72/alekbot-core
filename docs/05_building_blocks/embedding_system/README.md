<!-- living-doc
covers:
  src/adapters/gemini_embedding_adapter.py
  src/ports/embedding_service.py
  src/domain/vector_math.py
-->
# Embedding System

## Purpose

This block turns text into vectors so that memory facts, drive cards, email cards and tasks can be searched by meaning
and so that near-duplicate facts can be detected. Every vector the system stores or compares comes from one port,
`src/ports/embedding_service.py::EmbeddingService`, so callers never name a provider. The only implementation is
`src/adapters/gemini_embedding_adapter.py::GeminiEmbeddingAdapter`. The pure similarity math lives next to it in
`src/domain/vector_math.py::cosine_similarity`.

What it is not: it stores nothing. The vectors live in the owners' collections (facts, drive cards, email cards, tasks;
see [memory](../memory/README.md), [user_drive](../user_drive/README.md)) and the Firestore KNN indexes over them are
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
4. **Error mapping** (inside the call): 429 or `RESOURCE_EXHAUSTED` becomes `LLMRateLimitError`; 503 and SDK timeouts
   become `LLMUnavailableError`; both are retried (`TRANSIENT_RETRY_TYPES`). Any other 4xx becomes `LLMClientError`
   with `http_status` (a 400 refuses that one input; the drive indexer backs off only that card), any other 5xx
   `LLMServerError`; neither is retried.
5. **Metering.** After a successful response `_record_embedding_usage` adds `usage_metadata.prompt_token_count` to the
   ledger in scope (an agent execution, or `src/utils/billed_scope.py::billed_scope`). With none in scope the tokens go
   to `UNBILLED_EMBEDDING_TOKENS` and a rate-limited log line. Retried attempts are not metered, only the response
   that came back (`decisions/billing_embeddings.md`).

Who calls the port, by task type:

- `RETRIEVAL_DOCUMENT` (stored side): fact write (`src/services/fact_write_service.py::FactWriteService._generate_multi_vectors`,
  three vectors per fact from `src/domain/fact_embedding.py::fact_embedding_texts`), drive and email card indexing,
  task indexing.
- `RETRIEVAL_QUERY` (search side): `src/services/search_enrichment_service.py`, `src/services/drive_search_service.py`,
  `src/services/email_card_search_service.py`, `src/services/fact_search_service.py`, `src/services/task_indexing_service.py`.
- `SEMANTIC_SIMILARITY` has no caller outside the adapter. Fact dedup compares stored `RETRIEVAL_DOCUMENT` vectors with
  `cosine_similarity` (`src/adapters/firestore_repo.py::FirestoreFactRepository.add_fact_if_unique`; read-side
  `src/services/search_enrichment_service.py::SearchEnrichmentService._deduplicate_semantic`).

## Invariants and traps

- **One vector space.** Stored and query vectors must come from the same model and dimension. The dimension
  (`_OUTPUT_DIM`, Matryoshka truncation of the native output) must match the Firestore KNN indexes. A change of
  `_MODEL`, `_OUTPUT_DIM` or `_TASK_PREFIXES` changes every vector and must change `EMBEDDING_SPACE`, or old and new
  vectors are compared silently (`tests/unit/services/test_drive_search_embedding_space.py`). The migration
  record is `decisions/embedding_model_migration_v1_to_v2.md`.
- **The task is an inline prefix, not a parameter.** v2 has no `task_type` config; `_TASK_PREFIXES` rewrites the text.
  Stored text and query text use different prefixes by design. An unknown task type raises `ValueError`
  (`tests/unit/adapters/test_gemini_embedding_adapter.py`).
- **Batch is fan-out.** Do not swap in `contents=[...]`: it returns one embedding, not N
  (`tests/unit/adapters/test_gemini_embedding_adapter.py::test_get_embeddings_batch_calls_embed_content_per_text`).
- **Retry is per text**, inside the semaphore, so one text's 503 does not re-run the batch.
- **The semaphore is per process.** The cap is not shared across Cloud Run instances; the default is sized for the
  provider's per-second burst limiter, not its per-minute quota. Raise `GEMINI_EMBED_CONCURRENCY` only after the tier
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
