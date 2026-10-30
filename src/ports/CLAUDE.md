# Ports

ABC interfaces. Contracts between domain/services and the external world. ~93 ports.

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
| `UserDrivePort` | `user_drive_port.py` | `DriveProviderRouter` (`composition/`, dispatches by `handle.kind`) over OneDriveAdapter (`adapters/microsoft/`) | User's long-term file area: list, read, write, move, delete (provider-neutral). Every method takes a `DriveHandle` (user, connection, credential, kind), never a bare `user_id` |
| `DriveConnectionRepository` | `drive_connection_repository.py` | FirestoreDriveConnectionRepository | Drive connections and the user's primary pointer |

- **Drive port trap:** an adapter keys caches, locks and token caches by `(user_id, credential_id)` or the connection, never by user alone, and builds item/drive identities (`{id_space}:{…}`), `ContentIdentity` and `DriveMediaFacts` itself; the domain never parses a provider id. The contract suite is `tests/contracts/user_drive_contract.py`.

## Conventions

- All methods — `@abstractmethod async def`.
- Import only `domain/` and stdlib. Cross-port imports are forbidden
  (enforced by `tests/unit/test_req_arch_01_hexagonal_isolation.py`; the
  `CROSS_PORT_WHITELIST` in `arch_tech_debt.py` is empty — keep it that way).
- One port = one file: `llm_port.py`, `repository.py`, `session_store.py`.
- A new port is needed when: 2+ implementations OR testable substitution OR external boundary.
- Shared data models live in `domain/`, not here (`AgentExecutionContext` in
  `llm_port.py` is the single whitelisted exception — it holds an `LLMPort` reference).
