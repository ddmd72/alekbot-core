<!-- living-doc
covers: none
-->

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

## Contents

### Chat turn and agents

- [Agent Registry](./agent_registry/README.md) — how one turn becomes work done by specialist agents.
- [Hybrid Router](./hybrid_router/README.md) — the front door of a turn: classification and routing.
- [Prompt Design System v3](./prompt_design_system_v3/README.md) — how stored fragments become an agent's system prompt.
- [Prompt Cache Strategy](./prompt_cache_strategy/README.md) — which requests ask the provider for prompt caching.
- [Security Validation](./security_validation/README.md) — keeping written text from being read as a system instruction.
- [External Task Queue](./external_task_queue/README.md) — handing a task to a worker the owner runs elsewhere, and the result coming back.
- [Localization System](./localization_system/README.md) — response language versus UI language.
- [Companion Agents and Bound Channels](./companion_agents/README.md) — a channel bound straight to one agent, and the companions' session-scoped memory.
- [Long-running Turns](./long_running_turns/README.md) — a turn that outlives the 90 s mark: the clock, the registry record, the late answer.
- [Agent Skills and Workspace](./agent_skills_workspace/README.md) — Smart's own procedures and versioned files.
- [Self-Reminders](./self_reminders/README.md) — deferred instructions the notes agent writes and the scheduler fires as system turns.
- [Deep Research](./deep_research/README.md) — background research runs in a Cloud Run Job and the delivery of the report.
- [Media Generation](./media_generation/README.md) — image and video generation and editing through Grok Imagine.

### Memory and search

- [Memory](./memory/README.md) — the owner's map: facts, consolidation, directives, search.
- [Memory Telemetry](./memory_telemetry/README.md) — process memory records that explain a container kill.
- [Embedding System](./embedding_system/README.md) — turning text into vectors for search.

### Chat platforms, web and OAuth

- [Slack Integration](./slack_integration/README.md) — Slack messages in, answers out.
- [Telegram Integration](./telegram_integration/README.md) — the Telegram door into a turn.
- [Rich Content and Notifications](./rich_content_protocol/README.md) — structured items and one delivery routine.
- [OAuth Web API](./oauth_web_api/README.md) — signing in to the web side.
- [OAuth Multi-Tenant](./oauth_multi_tenant/README.md) — who a person is and whether they may be here.
- [User Cabinet](./user_cabinet/README.md) — the owner's web page for what chat is a poor place for.
- [Voice Companion](./voice_companion/README.md) — Lelik, the realtime voice conversation, its modes and what a call leaves behind.
- [Voice Call Transport](./voice_call_transport/README.md) — how a call's audio and control reach Lelik: Twilio, the web call, the relay.
- [Geo Marks](./geo_marks/README.md) — a position the owner shares on purpose, and why nothing tracks location otherwise.
- [Secrets at Rest](./secrets_at_rest/README.md) — the Cloud KMS cipher, keyed digests and the fail-closed boot.

### Files, drive and documents

- [File Storage](./file_storage/README.md) — what the user attaches and the system generates.
- [File Conversion](./file_conversion/README.md) — from a file ref to what a model can read.
- [Document Generation](./document_generation/README.md) — DOCX, PDF and single-page documents from a chat request.
- [User Drive](./user_drive/README.md) — the file area, its index and search.
- [Untrusted-Content Sandbox](./untrusted_content_sandbox/README.md) — the out-of-process service that parses and renders untrusted content.

### External services

- [Gmail Email Indexing](./gmail_email_indexing/README.md) — the mailbox as part of the system's reach.
- [Microsoft To Do Integration](./tasks_integration/README.md) — personal tasks in chat.
- [MCP Infrastructure](./mcp_infrastructure/README.md) — calling tools on external MCP servers.
- [Remote MCP Server](./remote_mcp_server/README.md) — letting outside MCP clients use the owner's data.

### LLM providers and observability

- [Provider Resolution](./provider_resolution/README.md) — performance tier to provider and model.
- [OpenAI Integration](./openai_integration/README.md) — OpenAI behind the LLM port.
- [Grok Integration](./grok_integration/README.md) — xAI Grok behind the LLM port.
- [Billing and Quota](./billing_and_quota/README.md) — what every billed call costs and where it is counted.
- [Observability](./observability_strategy/README.md) — what the system records about itself, and why.
