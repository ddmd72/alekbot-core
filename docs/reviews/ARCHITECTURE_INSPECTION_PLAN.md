# Architecture Inspection Plan

**Purpose:** Deep technical inspection of alekbot-core for use as an AI architecture portfolio artifact.
**Format per section:** code reading → web search (current standards/alternatives) → Q&A with author → findings (decision + rationale + pros/cons).
**Status legend:** `TODO` | `IN_PROGRESS` | `DONE` | `BLOCKED`

---

## How to Continue This Inspection (New Session Guide)

### Context you must read first

Before writing a single line of analysis, read in this order:
1. **This file** — understand what's DONE and what's next
2. **`CLAUDE.md`** (project root) — full system description, architecture rules, agent inventory
3. **`docs/08_concepts/hexagonal_architecture_patterns.md`** — how hexagonal is applied here
4. **`docs/09_decisions/`** — ADR-001 through ADR-008 (currently placeholder, but read anyway)
5. The section you're about to work on — key files listed there

For any section touching a specific subsystem, also read its building block doc in `docs/05_building_blocks/`.

### Methodology (do not deviate)

**Every section follows this exact sequence:**

1. **Read the code** — key files listed in the section. Read actual implementations, not just interfaces. Look for session-date comments (format: `Session YYYY-MM-DD:`) — they are evolution breadcrumbs.
2. **Read arc42 docs** — relevant RFC in `docs/10_rfcs/`, building block in `docs/05_building_blocks/`, decisions in `docs/09_decisions/`.
3. **Web search** — use the "Web search targets" listed in the section. Search for practitioner perspectives (2024-2025), not textbook definitions. Prioritize: conference talks, post-mortems, arxiv papers, HN/Reddit discussions with engineers.
4. **Ask the author** — formulate specific questions based on gaps between (a) what the code does and (b) what it was designed to do. Post questions in chat. Wait for answers before finalizing findings.
5. **Write findings** — fill in the section's Findings block with: decision description, rationale (author context + code evidence), pros, cons, comparison to current standards.
6. **Update document + commit + push** — update status to DONE, add session to Session Log, commit with descriptive message, push to `claude/architecture-inspection-plan-8g2jS`.

### Principles established (do not compromise)

- **Honest, not flattering.** If something is tech debt, call it tech debt. If a decision was wrong, say so with evidence. The author explicitly wants unbiased assessment.
- **Evidence-based.** Every claim needs: code line reference OR web source OR author statement. No speculation.
- **Web search before verdict.** Current standards (2024-2025) must be checked before judging any decision. The field moves fast.
- **Author context is not defense.** When the author explains a decision, record it accurately. Then still evaluate it against the evidence. A well-intentioned decision can still be a bad one.
- **Arc42 docs first.** Many answers are in `docs/`. Check there before asking the author.
- **Parallel agents for independent work.** Web search and code analysis can run simultaneously. Use Agent tool with subagent_type=Explore for both.

### What DONE means

A section is DONE when:
- All key files have been read
- Relevant arc42 docs have been checked
- Web search on stated targets has been performed
- Author questions have been asked AND answered (or explicitly noted as unanswered)
- Findings block is filled: decision + rationale + pros + cons + standard comparison
- Document committed and pushed

### Next section to work on

Look at the Sections table — find the first `TODO`. That's where to start.
Current next: **Section 2 — Multi-agent topology & LLM-based routing**.

### Tone when reporting to the author

- Write findings in chat after each section completes — author reads on mobile
- Keep the chat summary tight: key finding per bullet, no repetition of what's in the doc
- Ask questions explicitly and numbered — author answers one message at a time
- If blocked waiting for author answer, mark section `BLOCKED` and move to next `TODO`

---

## Sections

| # | Area | Status | Session |
|---|------|--------|---------|
| 1 | [Hexagonal Architecture — boundary enforcement](#1-hexagonal-architecture) | DONE | 1 |
| 2 | [Multi-agent topology & LLM-based routing](#2-multi-agent-topology) | TODO | - |
| 3 | [DelegationEngine — multi-turn tool loop](#3-delegationengine) | TODO | - |
| 4 | [LLM provider abstraction & PerformanceTier](#4-llm-provider-abstraction) | TODO | - |
| 5 | [Prompt management system (v3 token system)](#5-prompt-management) | TODO | - |
| 6 | [Prompt caching strategy](#6-prompt-caching) | TODO | - |
| 7 | [Memory & knowledge pipeline (RRF, vectors)](#7-memory--knowledge-pipeline) | TODO | - |
| 8 | [Consolidation — background memory formation](#8-consolidation) | TODO | - |
| 9 | [Agent manifest, registry & intent system](#9-agent-manifest--intent-system) | TODO | - |
| 10 | [Structured output enforcement across providers](#10-structured-output-enforcement) | TODO | - |
| 11 | [Async background processing (Cloud Tasks + Workers)](#11-async-background-processing) | TODO | - |
| 12 | [Deep research pipeline](#12-deep-research-pipeline) | TODO | - |
| 13 | [Email indexing & inbox-as-memory](#13-email-indexing) | TODO | - |
| 14 | [Document generation pipeline (DOCX/PDF/HTML)](#14-document-generation-pipeline) | TODO | - |
| 15 | [Security layer (composite adapter, IAM, OAuth)](#15-security-layer) | TODO | - |
| 16 | [Remote MCP server (OAuth 2.1 in-process)](#16-remote-mcp-server) | TODO | - |
| 17 | [Cost model & billing architecture](#17-cost--billing-architecture) | TODO | - |
| 18 | [Testing strategy & architecture](#18-testing-strategy) | TODO | - |

---

## 1. Hexagonal Architecture

**Status:** DONE

**What to inspect:**
- Actual import rule enforcement: do adapters import services? do services import adapters?
- Port justification: which ports have 2+ implementations vs. single-impl ports
- Domain purity: any I/O, logging, or config leaking into `domain/`
- Composition layer role: does it actually own all cross-boundary wiring?
- `REQ-ARCH-01`, `REQ-ARCH-22`, `REQ-ARCH-23` — find and count violations if any

**Key files inspected:**
```
src/domain/         — purity check
src/ports/          — 59 ABCs (vs ~51 documented)
src/adapters/       — import discipline, 69 adapters
src/services/       — no concrete adapter imports rule
src/composition/    — only layer allowed to cross all boundaries
docs/08_concepts/hexagonal_architecture_patterns.md
docs/09_decisions/adr-001-actor-model/
docs/09_decisions/adr-002-firestore-adapter/
```

**Author context:**
1. Single-impl ports — conscious policy: system evolved incrementally with infrastructure uncertainty (GCP vs AWS, Firestore vs other DB). Uniform hexagonal rule prevents refactoring when second implementation arrives (validated: Google Tasks → Microsoft ToDo added with zero refactoring). Plus: hexagonal boundaries chosen as defence against AI hallucinations during pair programming — strict layer rules prevent AI assistant from accidentally cross-contaminating layers.
2. `FactManagementPort` "two implementations" — tech debt (see below).
3. 15 ports with no named adapter — covered by `AsyncMock(spec=PortClass)` test doubles in-line in tests, not separate adapter classes. Conscious testing pattern.
4. No import-linter in CI — manual test suite before merge serves same purpose. Includes layer-boundary checks.

**Findings:**

**Boundary audit: 0 violations across all 5 rules.** 59 ports total.

Port breakdown:
- Justified multi-impl ports (7): LLMPort (4 providers), SecurityPort (4 layers), DeepResearchPort (3 providers), TasksProviderPort (Google/Microsoft), PlatformPort + PlatformMediaPort (Slack/Telegram), FactManagementPort (see tech debt below)
- Single-impl ports (37): justified via ADR-002 (infrastructure invariance / Firestore-as-replaceable-adapter) + AI-pair-programming defence rationale
- No named adapter (15): covered by AsyncMock test doubles

**Tech debt found:**
- `src/adapters/firestore_fact_management_adapter.py` — zombie file. Renamed to `FactManagementAdapter` on 2026-02-20 (see docstring: "Renamed from FirestoreFactManagementAdapter — does not access Firestore directly"). Old file not deleted. Not imported anywhere in production or tests. `FactManagementPort` effectively has one active implementation.

**ADR status:**
- ADR-001 through ADR-008 all in `PROPOSED (placeholder)` status — structural scaffolding exists, no decision content filled in. Gap between code quality and decision documentation quality.

**Pros:**
- Zero boundary violations in ~150 files / 69 adapters — rare at this scale and pace
- Absolute domain purity: no I/O, no logging, no config
- Security composite (4-layer) is textbook correct
- Multi-provider LLM abstraction fully justified — 4 providers is real external volatility
- Architecture discipline maintained solo without automated CI enforcement
- Novel rationale: hexagonal as AI-assistant guardrail — not in any 2025 literature, genuine contribution to the discourse

**Cons / Tensions:**
- 37 single-impl ports: maintenance tax — each field addition = domain + port + adapter + service + test change. Real cost for solo dev
- No automated import-linter in CI — boundary discipline is manual. Works now, fragile when team grows
- ADR documentation is empty scaffolding — decisions exist in code and conversation, not in structured records
- 1 zombie file (`firestore_fact_management_adapter.py`) — minor but indicates cleanup debt

**Standard comparison (2025):** Hexagonal is strongly justified here — high external system volatility (4 LLM providers, 2 platforms, 2 task providers, 3 deep research backends) is exactly the use case where ports earn their cost. Industry consensus confirms this. The single-impl port count is a legitimate academic criticism but is answered by the project's specific rationale.

**Deep assessment (web research + code evolution analysis):**

Evolution timeline (27 session date comments in code):
- Pre-2026-02-07: 29 boundary violations, score 7.5/10
- 2026-02-07: "Hexagonal refactoring epoch" — 5 new ports created, FactWriteService + SearchEnrichmentService extracted
- 2026-02-16–20: DI cleanup, adapter decoupling, BiographicalContextService refactor
- Post-epoch: 3 violations, score 9.0/10
- This is measurable architectural learning over ~2 weeks, not initial design

**AI-guardrail rationale — independently validated by 2025 research:**
- arxiv "Architecture Without Architects" (2025): AI agents drift architecture within 3-4 months without mechanical enforcement
- Bardia Khosravi (Medium 2025): "Backend Coding Rules for AI Coding Agents: DDD and Hexagonal Architecture" — prescribes exactly this approach
- rulebricks/claude-code-guardrails, Codacy Guardrails (2025): dedicated tools emerging for this exact problem
- The user arrived at this solution in early 2026 before it became mainstream practitioner advice
- Weakness: research says documentation doesn't work as guardrail, only mechanical enforcement (CI linter) does. Manual test suite before merge is the weakest link in the strategy.

**Port count re-evaluation (all 6 specific single-impl ports analyzed in code):**
- ConsolidationQueue: real swap candidate (Firestore → Cloud Tasks/Pub/Sub) ✅
- DedupStore: real swap candidate (Firestore → Redis/Memcached) ✅
- JobRunnerPort: real swap candidate (Cloud Run → Lambda/Kubernetes) ✅
- SearchEnrichmentPort: complex evolving algorithm (6-query RRF), testable isolation ✅
- FactWritePort: embedding generation isolation from agent logic ✅
- SessionStore: complex overflow callback semantics ✅
Practitioner consensus says >18 ports is over-engineering for solo dev, but code analysis shows each examined port is genuinely justified. The maintenance tax (each field = 4-file change) is the real cost.

**Blind spot identified — Domain Volatility:**
Classical hexagonal assumes stable domain. In AI agent systems, the "domain" (agent behavior, routing logic, prompts) is volatile — changes with every LLM update, prompt refinement, or context window expansion. Anthropic Engineering Blog (2025) defines "Context Engineering" as a new first-class architectural concern not addressed by traditional ports/adapters. The project partially compensates through Firestore-backed prompt tokens (externalizing volatile prompt logic) and ConsolidationAgent versioning, but has no explicit architectural pattern for managing volatile domain behavior. This is a gap — but it is manageable and closeable without major refactoring (see Recommendations below).

**ServiceContainer:** ~55 singletons, layered, composition root pattern — correct. One circular dependency workaround (`BiographicalContextService.set_repository()`). Manageable now, approaching the limit where sub-container decomposition becomes warranted.

**ADR documentation gap:** 8 ADRs all `PROPOSED (placeholder)`. Decisions exist in code session comments and conversation history, not in structured records. Significant gap for portfolio presentation — "why did you choose X?" needs a better answer than code archaeology.

**Recommendations — closing the Domain Volatility gap:**

The architecture already contains the right extension points. Closing the gap requires no major refactoring — only incremental additions within the existing hexagonal structure.

What already exists and can be used immediately:
- `UserBotConfig.agent_providers / agent_thinking / model_overrides` — per-user behavioral flags. Already a feature flag system for provider and model selection.
- `PromptBuilder` 4-level priority (USER > ACCOUNT > AGENT > SYSTEM) — USER level is a prompt feature flag. Give user X a different token = different behavior, zero code change.
- `DelegationEngine` `**context` spread — any key added to `message.context` (e.g. `experiment_variant`) propagates automatically through the full agent graph to every specialist. Experiment propagation infrastructure already in place.
- `LogSink` port (`src/ports/log_sink.py`) + `GcpLogSink` adapter — structured JSON emission to Cloud Logging. Adding routing decision metrics = one `log_sink.log({...})` call per agent. No new ports, no new adapters.
- `PromptDebugLogger` (`src/utils/debug_logger.py`) — already saves full LLM requests/responses to GCS. GCS backend activated by `DEBUG_PROMPTS_BUCKET` env var. This is observability infrastructure that can be repurposed for behavioral baselining.

What needs to be added (incremental, no rework):
1. **Prompt versioning** — extend `Token` domain model with `version`/`updated_at`, add `get_history(token_id)` to `TokenRepository` port, update Firestore adapter. 3 files. Nothing else changes.
2. **Routing metrics** — add `log_sink.log({"event": "routing_decision", "complexity": N, "path": "quick|smart"})` in `RouterAgent`. 1 line. Immediately queryable in Cloud Logging → BigQuery.
3. **Provider canary** — extend `ProviderRegistry.get()` to support weighted selection. 1 service file. Agents don't change.
4. **Evaluation framework** — new `EvaluationPort` + adapter (e.g. wrapping Braintrust or custom). Agents emit `evaluation_signal` via existing `**context` passthrough or a new lifecycle hook in `BaseAgent._on_agent_success`. No existing code changes.

Note on `LogSink` as validation of the port policy: adding behavioral observability requires zero architectural changes because `LogSink` is already a port. This is a concrete example of where the "port everywhere" policy pays off — swapping GCP Logging for Datadog or a custom evaluation sink is a single adapter swap.

---

## 2. Multi-Agent Topology

**Status:** TODO

**What to inspect:**
- Router: LLM-based triage vs. rule-based fallback — how the complexity 1–10 scale works in practice
- Quick vs. Smart: actual code differences beyond what CLAUDE.md says
- Confidence safety net: low-confidence → Smart fallback implementation
- Vision attachment forcing complexity ≥ 7 — where this lives in code
- Cost model: 70% Quick / 30% Smart claim — is it configurable or hardcoded?

**Key files:**
```
src/agents/core/router_agent.py
src/agents/core/quick_response_agent.py
src/agents/core/smart_response_agent.py
src/domain/task_complexity.py
src/domain/complexity_settings.py
```

**Web search targets:** LLM-based routing patterns 2024-2025, multi-agent orchestration taxonomy, complexity-based routing alternatives

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 3. DelegationEngine

**Status:** TODO

**What to inspect:**
- Multi-turn tool loop implementation — how iteration, dispatch, and termination work
- Memory-first parallel execution: `search_memory` sequential, others `asyncio.gather` — why?
- Context passthrough: `**context` spread — what fields propagate and how
- `intent_fanout`: 1:N parallel dispatch, result merging, labeled sections, failure handling
- `terminal_tool="deliver_response"` — how Smart uses it vs. Quick's plain text
- `intent_remap` — currently disabled, what it was designed for

**Key files:**
```
src/infrastructure/delegation_engine.py
src/infrastructure/agent_coordinator.py
src/domain/llm.py           — build_tool_turn()
src/domain/tool_result.py
```

**Web search targets:** multi-turn tool calling patterns, ReAct vs. tool-loop architectures, fan-out patterns in multi-agent systems

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 4. LLM Provider Abstraction

**Status:** TODO

**What to inspect:**
- `LLMPort` contract: what it enforces and what each adapter must handle
- PerformanceTier (ECO/BALANCED/PERFORMANCE) — mapping to concrete models, configurability
- ProviderRegistry: runtime selection logic
- Provider-specific quirks absorbed in adapters vs. leaking to agents
- `ProviderCapabilities` domain object — what capabilities are declared
- AgentProviderStrategy: per-agent provider locking

**Key files:**
```
src/ports/llm_port.py
src/domain/llm.py               — LLMRequest, LLMResponse, UsageMetadata
src/adapters/gemini_adapter.py
src/adapters/claude_adapter.py
src/adapters/openai_adapter.py
src/adapters/grok_adapter.py
src/services/provider_registry.py
src/services/agent_context_builder.py   — AgentProviderStrategy
src/infrastructure/agent_config.py     — PerformanceTier bindings
```

**Web search targets:** LLM provider abstraction patterns, provider-agnostic Python SDK design, multi-provider resilience patterns 2025

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 5. Prompt Management

**Status:** TODO

**What to inspect:**
- Token system: what a "token" is, how tokens compose into blueprints
- 4 priority levels (USER > ACCOUNT > AGENT > SYSTEM) — resolution algorithm
- Blueprints: static templates with `{{CLASS_NAME}}` slots — where Firestore fits
- `knowledge_base {}` block: biographical facts + conversation history assembly
- `extra_static_blocks`: large payload injection without context pollution
- 24h in-memory cache: implementation and invalidation
- Groovy DSL prompt format — why Groovy syntax, transformer to markdown

**Key files:**
```
src/domain/prompt_v3/           — token, blueprint, profile, section, slot domain models
src/ports/prompt_v3/
src/adapters/prompt_v3/
src/services/prompt_builder.py
src/services/prompt_v3/prompt_assembly_service.py
src/adapters/groovy_prompt_assembler.py
src/adapters/xml_prompt_assembler.py
src/utils/groovy_to_markdown_transformer.py
```

**Web search targets:** prompt management architectures 2025, dynamic prompt assembly patterns, prompt versioning strategies

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 6. Prompt Caching

**Status:** TODO

**What to inspect:**
- `PROMPT_CACHE_BOUNDARY` split: what goes into static prefix vs. dynamic suffix
- `PromptCacheStrategy` — proxy pattern, how agents declare their type without knowing about caching
- `CachingLLMProxy`: actual implementation
- Provider-specific cache mechanisms: Anthropic 5-min TTL, Gemini, OpenAI
- `cache_read_tokens` in UsageMetadata — how all adapters populate it
- Billing impact: 0.1× Claude, 0.1× OpenAI, 0.25× Gemini cache pricing

**Key files:**
```
src/services/prompt_cache_strategy.py
src/ports/prompt_cache_strategy_port.py
src/domain/llm.py               — PromptCacheConfig, CacheMetadata, PROMPT_CACHE_BOUNDARY
src/adapters/claude_adapter.py  — cache_control injection
src/adapters/gemini_adapter.py  — CachedContent API
src/adapters/openai_adapter.py  — cached_tokens
```

**Web search targets:** LLM prompt caching strategies 2024-2025, Anthropic context caching vs. Gemini CachedContent, prompt caching cost analysis

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 7. Memory & Knowledge Pipeline

**Status:** TODO

**What to inspect:**
- 6-vector RRF search: what 6 vectors represent, why 6
- RRF algorithm: implementation in `domain/vector_math.py`
- `SearchEnrichmentService`: full flow from query to enriched context
- `MemorySearchAgent`: ECO-tier LLM key extraction — why LLM instead of direct embedding
- Memory-first execution in DelegationEngine — architectural decision
- Biographical cache: what it is, update triggers, TTL

**Key files:**
```
src/agents/memory_search_agent.py
src/services/search_enrichment_service.py
src/domain/vector_math.py
src/adapters/gemini_embedding_adapter.py
src/ports/embedding_service.py
src/adapters/firestore_repo.py      — vector search queries
```

**Web search targets:** RRF vs. other re-ranking algorithms, multi-vector search patterns, memory-augmented AI agents 2025, biographical/episodic memory in AI assistants

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 8. Consolidation

**Status:** TODO

**What to inspect:**
- Sliding window mechanics: overflow_threshold, batch_size, how counting works
- Cloud Tasks dispatch: serialization, idempotency, retry behavior
- `ConsolidationAgent` LLM protocol: "Life Chronicler" — what prompt does, extract format
- Deduplication: threshold 0.96, number-aware — what "number-aware" means in practice
- SCD2 versioning: valid_from/valid_to/is_current — how updates work
- Serialization choice: `p.text` vs. `p.full_text` — why the distinction matters

**Key files:**
```
src/agents/consolidation_agent.py
src/services/consolidation_service.py
src/handlers/consolidation_handler.py
src/domain/consolidation.py
src/domain/deduplication_service.py
src/adapters/firestore_consolidation_queue.py
src/adapters/firestore_dedup_store.py
src/ports/consolidation_queue.py
src/ports/dedup_store.py
```

**Web search targets:** memory consolidation in AI systems, SCD2 pattern in document databases, semantic deduplication algorithms, Firestore vector search patterns

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 9. Agent Manifest & Intent System

**Status:** TODO

**What to inspect:**
- `AgentDescriptor` dataclass: full field inventory and what each field drives
- `AgentManifest` / `ALL_DESCRIPTORS`: registration flow into main.py
- `eager: bool` — lifecycle difference, which agents are lazy and why
- `context_schemas`: typed param contracts — how orchestrator fills structured `context`
- `Intent` constants: naming conventions, semantic load
- `internal=True` — hidden from LLM tool descriptions — mechanism

**Key files:**
```
src/infrastructure/agent_manifest.py
src/infrastructure/agent_registry.py
src/infrastructure/agent_config.py
src/agents/base_agent.py            — descriptor class-level attribute
main.py                             — ALL_DESCRIPTORS registration
```

**Web search targets:** agent registry patterns, intent-based routing vs. function-name routing, agent capability declaration patterns 2025

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 10. Structured Output Enforcement

**Status:** TODO

**What to inspect:**
- Three mechanisms: `response_mime_type`, `response_schema`, OUTPUT_FORMAT token — interaction matrix
- Provider-specific translation: Gemini native, OpenAI json_object, Claude `output_config.format`
- `_RESPONSE_SCHEMA` on Quick/Smart: top-level envelope only, flat `data` — why flat (Gemini depth limit)
- `rich_content.data.rows` format: `{cells: [...]}` — why not nested arrays (Gemini hang)
- `MAX_PARSE_RETRIES` retry loop: appending bad response + correction — implementation
- `EmailClassificationAgent` exception: markdown code block extraction — rationale

**Key files:**
```
src/agents/base_agent.py
src/agents/core/quick_response_agent.py
src/agents/core/smart_response_agent.py
src/adapters/gemini_adapter.py      — response_schema translation
src/adapters/claude_adapter.py      — output_config.format
src/adapters/openai_adapter.py      — json_object mode
src/agents/email_classification_agent.py
```

**Web search targets:** structured output enforcement patterns 2025, JSON mode vs. response schema, provider-specific structured output APIs

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 11. Async Background Processing

**Status:** TODO

**What to inspect:**
- `WorkerHandler`: `task_type` dispatch table — full inventory of task types
- `GcpTaskQueue` + `_DomainEncoder`: Pydantic serialization in Cloud Task payloads
- Cloud Tasks vs. Cloud Run Jobs — where each is used and why
- `AgentWorkerHandler`: how agent execution tasks are dispatched and results delivered
- `UserNotificationService`: `notify()` vs. `notify_raw()` — routing difference
- `origin_channel_id` propagation through context — async delivery back to right channel

**Key files:**
```
src/handlers/worker_handler.py
src/handlers/agent_worker_handler.py
src/adapters/gcp_task_queue.py
src/ports/task_queue.py
src/services/user_notification_service.py
src/infrastructure/message_queue.py
```

**Web search targets:** Cloud Tasks vs. Cloud Run Jobs use cases, async agent result delivery patterns, GCP task queue patterns 2025

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 12. Deep Research Pipeline

**Status:** TODO

**What to inspect:**
- Three providers: Gemini (polling 120s), OpenAI (webhook), Claude (Cloud Run Job + native thinking)
- `ClaudeDeepResearchRunnerAgent`: escape hatch from `LLMPort` — why and how
- Two-pass critic: what the second pass does, per-user toggle
- `job_main.py` entrypoint: Cloud Run Job lifecycle
- `max_tokens=64K` for thinking models — reasoning behind the limit
- Debug prompt saving to GCS: `end_turn` and `max_tokens` triggers

**Key files:**
```
src/agents/deep_research_agent.py
src/agents/claude_deep_research_runner_agent.py
src/adapters/gemini_deep_research_adapter.py
src/adapters/openai_deep_research_adapter.py
src/adapters/claude_deep_research_adapter.py
src/adapters/cloud_run_jobs_adapter.py
src/ports/deep_research_port.py
src/ports/job_runner_port.py
job_main.py
```

**Web search targets:** deep research agent patterns 2025, extended thinking Claude patterns, Cloud Run Jobs for long-running AI tasks, polling vs. webhook for async AI

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 13. Email Indexing

**Status:** TODO

**What to inspect:**
- `EmailIndexingService` full pipeline: OAuth → paginated fetch → classification → vector index
- `EmailClassificationAgent`: tool-calling mode for classification — why tool-calling
- `IndexedEmail` schema: 4-vector design (mirrors FactEntity — why mirror?)
- Daily email review: 200-email cap, 500-char truncation, SmartAgent protocol phases
- Watchdog: 2h stale job detection
- `EmailEmbeddingRepairService`: what breaks embeddings, repair strategy

**Key files:**
```
src/services/email_indexing_service.py
src/services/email_search_service.py
src/services/email_review_service.py
src/agents/email_classification_agent.py
src/agents/email_search_agent.py
src/adapters/gmail_provider_adapter.py
src/adapters/firestore_indexed_email_repo.py
src/domain/email.py
```

**Web search targets:** email-as-memory patterns, inbox intelligence architectures, email classification with LLMs 2025

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 14. Document Generation Pipeline

**Status:** TODO

**What to inspect:**
- DOCX: DocPlanner → JSON layout spec → DocGenerator (Node.js subprocess) — two-agent split rationale
- PDF: single LLM call → HTML+CSS → Puppeteer → bytes — why not direct PDF generation
- HTML page: same but no Node.js subprocess — what's the difference vs. PDF path
- `NodeDocxRunner`: temp script pattern, `docx_generator/` dir, node_modules resolution
- Unsplash integration in HtmlPageGenerator: placeholder URLs → real photos, post-processing
- File delivery: `DeliveryItem` types, GCS link, Slack file upload

**Key files:**
```
src/agents/doc_planner_agent.py
src/agents/doc_generator_agent.py
src/agents/pdf_generator_agent.py
src/agents/html_page_generator_agent.py
src/adapters/node_docx_runner.py
src/adapters/node_puppeteer_runner.py
src/adapters/playwright_html_renderer.py
src/adapters/unsplash_adapter.py
src/ports/docx_runner_port.py
src/ports/puppeteer_runner_port.py
src/ports/image_search_port.py
docx_generator/
pdf_generator/
```

**Web search targets:** LLM-driven document generation architectures, DOCX generation patterns (Node.js vs. Python), PDF generation from HTML 2025, multi-format document pipelines

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 15. Security Layer

**Status:** TODO

**What to inspect:**
- `CompositeSecurityAdapter`: what it composes (regex + LLM + external API)
- `SecurityPort`: what the contract enforces
- IAM: `FirestoreIAMAdapter`, `IAMService` — what access control model is used
- Firebase Auth integration: token validation flow
- Platform auth: Slack/Telegram request verification
- OAuth credential storage: `FirestoreOAuthCredentialsAdapter` — encryption at rest?

**Key files:**
```
src/adapters/security/composite_adapter.py
src/adapters/security/regex_adapter.py
src/adapters/security/llm_adapter.py
src/adapters/security/external_api_adapter.py
src/ports/security_port.py
src/services/iam_service.py
src/adapters/firestore_iam_adapter.py
src/adapters/firebase_auth_adapter.py
src/domain/auth.py
src/adapters/firestore_oauth_credentials_adapter.py
```

**Web search targets:** LLM application security patterns 2025, prompt injection defenses, multi-layer content moderation, OAuth credential storage best practices

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 16. Remote MCP Server

**Status:** TODO

**What to inspect:**
- OAuth 2.1 full flow: DCR → PKCE → consent → token — in-process implementation
- ASGI dispatcher: why not Starlette `Mount` (three specific failure modes documented)
- `AlekAccessToken` subclass: user_id + account_id in JWT — token design
- Firestore collections: `mcp_oauth_clients`, `mcp_auth_codes`, `mcp_refresh_tokens` — TTL, rotation
- `OAuthAuthorizationServerProvider` shim in `composition/` — why not `adapters/`
- Consent binding: Cabinet JWT cookie → identity — security implications

**Key files:**
```
src/composition/mcp_setup.py
src/composition/mcp_sdk_oauth_provider.py
src/services/mcp_authorization_service.py
src/adapters/firestore_mcp_client_repository.py
src/web/mcp_consent_app.py
src/domain/mcp.py
src/ports/mcp_client_repository.py
main.py                         — ASGI dispatcher wiring
```

**Web search targets:** MCP server Python implementation patterns, OAuth 2.1 in-process AS, RFC 8414 / RFC 7591 implementation patterns, FastMCP production patterns 2025

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 17. Cost & Billing Architecture

**Status:** TODO

**What to inspect:**
- `UsageMetadata`: `prompt_tokens` always = uncached — adapter normalization for each provider
- `cache_read_tokens`: how each of 4 providers populates it
- `CostCalculator`: formula, per-provider rates, cache discount multipliers
- Daily billing summary: snapshot mechanism (`prev_daily_tokens/prev_daily_cost`), reset timing
- Per-user quota: `FirestoreQuotaService` — what limits are enforced
- $100/month budget target — does the architecture actually support it?

**Key files:**
```
src/services/cost_calculator.py
src/agents/infrastructure/billing_agent.py
src/domain/billing.py
src/domain/llm.py               — UsageMetadata
src/adapters/firestore_quota_service.py
src/ports/quota_service.py
src/handlers/worker_handler.py  — billing_daily_summary task
```

**Web search targets:** LLM cost optimization patterns 2025, token usage accounting multi-provider, prompt caching ROI analysis

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## 18. Testing Strategy

**Status:** TODO

**What to inspect:**
- Wire test pattern: mock at SDK boundary, not port — rationale and implementation
- `ContractRule` objects in `tests/contracts/adapter_contracts.py` — contract validation model
- `CapturingStub` in integration layer — what it captures
- `AsyncMock(spec=PortClass)` convention — enforcement
- Test coverage distribution: unit vs. integration vs. e2e vs. performance
- Gaps: what's NOT tested and why (intentional or not)

**Key files:**
```
tests/
tests/conftest.py
tests/contracts/adapter_contracts.py
tests/unit/adapters/
tests/integration/adapters/
pytest.ini
```

**Web search targets:** testing LLM applications patterns 2025, SDK boundary mocking vs. port mocking, contract testing for adapters, pytest-asyncio patterns

**Questions for author:** *(to be filled)*

**Findings:** *(to be filled)*

---

## Cross-cutting Observations

*(Filled incrementally as sections complete)*

- Patterns that appear in 3+ sections (signal of system-level design choices)
- Tension points between architectural purity and pragmatism
- Decisions that reflect $100/month / 1 vCPU constraint
- Non-obvious choices that require author context to evaluate fairly

---

## Session Log

| Session | Date | Sections worked | Key decisions uncovered |
|---------|------|-----------------|------------------------|
| 1 | 2026-04-24 | Plan creation | — |
| 2 | 2026-04-24 | §1 Hexagonal Architecture | 0 boundary violations; 59 ports (37 single-impl); no import-linter |
