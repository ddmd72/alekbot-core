# Ports

ABC interfaces. Contracts between domain/services and the external world.

## Key Ports

| Port | Adapter(s) | Purpose |
|------|-----------|---------|
| `LLMService` | GeminiAdapter, ClaudeAdapter, GrokAdapter | Content generation, embedding, capabilities |
| `FactRepository` | FirestoreFactRepository | Facts: CRUD + vector search + SCD2 |
| `SessionStore` | FirestoreSessionStore | Sessions: history + overflow callback |
| `EmbeddingService` | GeminiEmbeddingAdapter | Vector embeddings |
| `ConsolidationQueue` | FirestoreConsolidationQueue | Consolidation queue |
| `IAMPort` | IAMService | Authorization: platform + user_id → AuthResult |
| `QuotaService` | FirestoreQuotaService | Billing: deduct/check quota |

## Conventions

- All methods — `@abstractmethod async def`.
- Import only `domain/` and stdlib.
- One port = one file: `llm_service.py`, `repository.py`, `session_store.py`.
- A new port is needed when: 2+ implementations OR testable substitution OR external boundary.
