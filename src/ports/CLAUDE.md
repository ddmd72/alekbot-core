# Ports

ABC interfaces. Contracts between domain/services and the external world. ~58 ports.

## Key Ports

| Port | File | Adapter(s) | Purpose |
|------|------|-----------|---------|
| `LLMPort` | `llm_port.py` | GeminiAdapter, ClaudeAdapter, GrokAdapter, OpenAIAdapter | Content generation, tool calling, capabilities |
| `FactRepository` | `repository.py` | FirestoreFactRepository | Facts: CRUD + vector search + SCD2 |
| `SessionStore` | `session_store.py` | FirestoreSessionStore | Sessions: history + overflow callback |
| `EmbeddingService` | `embedding_service.py` | GeminiEmbeddingAdapter | Vector embeddings |
| `ConsolidationQueue` | `consolidation_queue.py` | FirestoreConsolidationQueue | Consolidation queue |
| `IAMPort` | `iam_port.py` | IAMService | Authorization: platform + user_id → AuthResult |
| `QuotaService` | `quota_service.py` | FirestoreQuotaService | Billing: deduct/check quota |
| `PromptBuilderPort` | `prompt_builder_port.py` | PromptBuilder | Agent prompt assembly |

## Conventions

- All methods — `@abstractmethod async def`.
- Import only `domain/` and stdlib. Cross-port imports are forbidden
  (enforced by `tests/unit/test_req_arch_01_hexagonal_isolation.py`; the
  `CROSS_PORT_WHITELIST` in `arch_tech_debt.py` is empty — keep it that way).
- One port = one file: `llm_port.py`, `repository.py`, `session_store.py`.
- A new port is needed when: 2+ implementations OR testable substitution OR external boundary.
- Shared data models live in `domain/`, not here (`AgentExecutionContext` in
  `llm_port.py` is the single whitelisted exception — it holds an `LLMPort` reference).
