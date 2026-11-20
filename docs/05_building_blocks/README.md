# 05 Building Blocks

## Contract

A block README tells a developer, a reviewer or the owner how one subsystem works **now**, before they open a file. Why
the contract is shaped this way:
[`living_docs_contract.md`](../04_solution_strategy/decisions/living_docs_contract.md).

**Which subsystem gets a README.** One that owns stored data (a collection, a bucket, an index), sits on an external
boundary (a provider, a mail or chat platform, OAuth, MCP), or runs a flow across three or more layers or processes.
Anything smaller is described by its code and docstrings.

**What it holds,** in this order:

1. **Purpose** — what the block is for and what it is not for, in a paragraph.
2. **Owned data** — what it stores, what each field means, who writes it and who reads it.
3. **Flows** — the main flows step by step, each step naming its entry point as `path::Symbol`.
4. **Invariants and traps** — what must hold, what breaks silently, and the code or test that enforces it.
5. **Links** — the RFCs and decision records behind the block.
6. **Where to start reading** — three to seven `path::Symbol` entry points.

Everything general (the `<!-- living-doc` header and `covers:` ownership, what a living doc never holds, how it stays
current) is in [`../README.md`](../README.md#living-docs).

## 📂 Contents

### Core Systems

- Prompt Design System v3: [prompt_design_system_v3/README.md](./prompt_design_system_v3/README.md)
- Security Validation: [security_validation/README.md](./security_validation/README.md)
- Multi-Agent System: [multi_agent_system/README.md](./multi_agent_system/README.md)
- Agent Registry (ACP v2): [agent_registry/README.md](./agent_registry/README.md)
- Agent Logging: [agent_logging/README.md](./agent_logging/README.md)
- Hybrid Router: [hybrid_router/README.md](./hybrid_router/README.md)
- Quick Agent Delegation: [quick_agent_delegation/README.md](./quick_agent_delegation/README.md)

### Memory & Context

- Memory (owner's map: facts, consolidation, baseline, directives, search): [memory/README.md](./memory/README.md)
- Sliding Window Consolidation (pointer to the map): [sliding_window_consolidation/README.md](./sliding_window_consolidation/README.md)
- Fact Write Service (pointer to the map): [fact_write_service/README.md](./fact_write_service/README.md)
- Biographical Context Cache (pointer to the map): [biographical_context_cache/README.md](./biographical_context_cache/README.md)
- Search Enrichment (pointer to the map): [search_enrichment/README.md](./search_enrichment/README.md)
- Embedding System: [embedding_system/README.md](./embedding_system/README.md)

### Platform Integration & External Services

- MCP Infrastructure: [mcp_infrastructure/README.md](./mcp_infrastructure/README.md)
- Gmail Email Indexing: [gmail_email_indexing/README.md](./gmail_email_indexing/README.md)
- OAuth Web API: [oauth_web_api/README.md](./oauth_web_api/README.md)
- OAuth Multi-Tenant: [oauth_multi_tenant/README.md](./oauth_multi_tenant/README.md)
- User Cabinet: [user_cabinet/README.md](./user_cabinet/README.md)
- Slack Integration: [slack_integration/README.md](./slack_integration/README.md)
- Telegram Integration: [telegram_integration/README.md](./telegram_integration/README.md)
- Rich Content Protocol: [rich_content_protocol/README.md](./rich_content_protocol/README.md)
- File Conversion Service: [file_conversion/README.md](./file_conversion/README.md)
- User Drive (file area + index and search): [user_drive/README.md](./user_drive/README.md)

### Infrastructure & Providers

- Provider Resolution: [provider_resolution/README.md](./provider_resolution/README.md)
- OpenAI Integration: [openai_integration/README.md](./openai_integration/README.md)
- Grok Integration: [grok_integration/README.md](./grok_integration/README.md)
- Prompt Cache Strategy: [prompt_cache_strategy/README.md](./prompt_cache_strategy/README.md)
- Observability Strategy: [observability_strategy/README.md](./observability_strategy/README.md)
- Localization System: [localization_system/README.md](./localization_system/README.md)
- User Management System: [user_management_system/README.md](./user_management_system/README.md)
