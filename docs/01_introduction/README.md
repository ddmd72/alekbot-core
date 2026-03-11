# 01 Introduction

## 📖 HowTo: Using This Document

### Purpose

Defines the system overview, goals, stakeholders, quality attributes, and core terminology for Alek-Core.

### When to Read

- **For AI Agents:** First step after Tier 1 initialization to understand project purpose and scope.
- **For New Developers:** Onboarding - understand what Alek-Core is and why it exists.
- **For Product Owners:** Business context and quality goals.

### When to Update

This document MUST be updated when:

- [ ] The project goals or product vision change.
- [ ] New stakeholder groups are identified.
- [ ] Quality goals are added or reprioritized.
- [ ] Core terminology changes or new terms are introduced.

### Cross-References

- **Solution Strategy:** [../04_solution_strategy/README.md](../04_solution_strategy/README.md)
- **Building Blocks:** [../05_building_blocks/README.md](../05_building_blocks/README.md)
- **Roadmap:** [../12_risks/IMPLEMENTATION_ROADMAP.md](../12_risks/IMPLEMENTATION_ROADMAP.md)

---

## 1. Purpose and Goals

### 1.1 What is Alek-Core?

**Alek-Core** is a **Sovereign Exocortex** — a personal knowledge management system ("second brain") built on principles of **Clean Architecture**, cognitive psychology, and modern Large Language Models (LLMs).

The system acts as an intelligent memory layer that:

- **Learns** from conversations and documents
- **Retrieves** relevant context using multi-vector semantic search
- **Consolidates** episodic memories into long-term knowledge
- **Responds** with personalized, context-aware intelligence

### 1.2 Core Value Proposition

**For Individual Users:**

- Personal AI assistant accessible via Slack and Telegram
- Persistent memory of conversations, facts, and documents
- Proactive context injection based on semantic relevance
- Customizable personality and response style

### 1.3 Strategic Goals

1. **Architectural Excellence**
   - Maintain strict Hexagonal Architecture (Ports & Adapters)
   - Domain-driven design with zero infrastructure coupling
   - Platform-agnostic core (Slack, Telegram, future: Discord, WhatsApp)

2. **Multi-Tenancy & Scalability**
   - Account-based billing and quota management
   - Per-user agent isolation with 1-hour TTL cache
   - Horizontal scalability via async/await and Cloud Run

3. **Security & Privacy**
   - 5-layer defense against prompt injection
   - HMAC-verified webhooks for all platforms
   - User-private and account-shared fact visibility
   - OAuth 2.0 / OIDC authentication

4. **Cognitive Fidelity**
   - Dual memory system (hot conversation + cold consolidated facts)
   - SCD Type 2 temporal model for fact evolution
   - Multi-vector search (text, tags, metadata embeddings)
   - Sliding window consolidation with semantic synthesis

5. **Developer Experience**
   - Self-documenting codebase with Arc42 documentation
   - Comprehensive test coverage (unit, integration, E2E)
   - CI/CD with Cloud Build and automated deployment
   - Feature flags for gradual rollout (prompt v3, history optimization)

---

## 2. Stakeholders

### 2.1 Primary Stakeholders

| Stakeholder        | Role                    | Concerns                                                   |
| ------------------ | ----------------------- | ---------------------------------------------------------- |
| **End Users**      | Slack/Telegram users    | - Conversation quality<br>- Response speed<br>- Privacy    |
| **Account Owners** | Billing decision makers | - Cost control<br>- Team management (IAM)<br>- Data access |
| **Developers**     | Code contributors       | - Architecture clarity<br>- Test coverage<br>- Deployment  |
| **DevOps/SRE**     | Operations & monitoring | - Reliability<br>- Performance<br>- Incident response      |
| **Product Owner**  | Vision & roadmap        | - Feature prioritization<br>- Market fit<br>- Metrics      |

### 2.2 External Systems

| System               | Integration Type   | Purpose                      |
| -------------------- | ------------------ | ---------------------------- |
| **Slack API**        | Webhook + OAuth    | Message platform             |
| **Telegram Bot API** | Webhook            | Message platform             |
| **Google OAuth**     | OIDC provider      | Authentication               |
| **Gemini API**       | LLM provider       | Text generation + embeddings |
| **Firestore**        | NoSQL database     | Facts, users, sessions       |
| **Cloud Run**        | Container platform | HTTP/async deployment        |
| **Cloud Tasks**      | Job queue          | Background consolidation     |

---

## 3. Quality Goals

### 3.1 Top 3 Quality Attributes (Prioritized)

| Priority | Quality Attribute           | Motivation                                                                                     | Trade-offs                                            |
| -------- | --------------------------- | ---------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| **#1**   | **Architectural Integrity** | Hexagonal Architecture prevents vendor lock-in, enables platform expansion (Telegram, Discord) | Higher upfront design effort, more abstraction layers |
| **#2**   | **Multi-Tenancy Isolation** | Secure data segregation between accounts                                                        | Complex IAM logic, per-user overhead                  |
| **#3**   | **Conversational Quality**  | Context-aware responses with memory retrieval define core user experience                      | Higher latency (semantic search), LLM costs           |

### 3.2 Additional Quality Goals

**Reliability:**

- Circuit breakers on all agents (3 failures → open for 60s)
- Automatic retry with exponential backoff (3 attempts max)
- Graceful degradation (v3 prompt system falls back to v2)
- Health checks and structured logging

**Security:**

- 5-layer validation (Token Creation → Assignment → Runtime → Output → RAG)
- HMAC webhook verification (Slack, Telegram)
- OAuth 2.0 / OIDC authentication
- Trust zone model (TRUSTED, SEMI_TRUSTED, UNTRUSTED)

**Performance:**

- Prompt assembly cache (24h TTL, 20x speedup on hits)
- Parallel repository calls (asyncio.gather)
- Multi-vector search with RRF ranking (2-3s per query)
- Per-user agent cache (1h TTL, warm start optimization)

**Scalability:**

- Stateless Cloud Run instances (auto-scaling 0-100)
- Async I/O throughout (asyncio, Quart, async Slack SDK)
- Firestore horizontal partitioning (account_id sharding)
- Background consolidation via Cloud Tasks

**Observability:**

- Structured JSON logging with trace correlation
- OpenTelemetry spans for critical paths
- Agent-level metrics (circuit breaker status, cache hits)
- Firestore query performance tracking

---

## 4. Business Requirements (High-Level)

### 4.1 Functional Requirements

**FR-1: Multi-Platform Messaging**

- Support Slack (Events API, Socket Mode) and Telegram (Webhook)
- Platform-agnostic conversation handler
- Markdown formatting per platform (Slack mrkdwn, Telegram MarkdownV2)

**FR-2: Persistent Memory**

- Store facts with SCD Type 2 temporal model
- Multi-vector embeddings (text, tags, metadata)
- Semantic search with RRF ranking (top-k from 3 vectors)
- Fact visibility controls (account-shared, user-private)

**FR-3: Multi-Agent System**

- Router classifies intent (quick, smart, entertainment, help)
- Quick agent for simple queries (< 2s response)
- Smart agent delegates to specialists (memory, web search)
- Consolidation agent synthesizes facts from conversation history

**FR-4: OAuth & User Management**

- Google OAuth authentication
- User Cabinet web UI (invite codes, configuration)
- IAM-based role management (owner, member, viewer)
- Platform linking (Slack ↔ OAuth ↔ Telegram)

**FR-5: Prompt Customization**

- Token-based prompt system (v3) with whitelisted library
- 4-level resolution (USER > ACCOUNT > AGENT > SYSTEM)
- Security validation at token creation + runtime injection
- Per-user overrides for humor, voice, response style

### 4.2 Non-Functional Requirements

**NFR-1: Response Time**

- Quick agent: < 2s (95th percentile)
- Smart agent: < 10s (95th percentile)
- Memory search: < 3s (multi-vector query)

**NFR-2: Availability**

- Cloud Run uptime: 99.5% SLA
- Graceful degradation on LLM provider outage
- Circuit breaker prevents cascade failures

**NFR-3: Data Retention**

- Hot conversation: 100 messages (sliding window)
- Cold facts: Unlimited (SCD Type 2 with is_current flag)
- Audit logs: 90 days (structured JSON)

**NFR-4: Cost Control**

- Account-level token quotas (default: 100k/day)
- Monthly cost limits (default: $50)
- Billing agent tracks usage per request
- Performance tier controls model selection (ECO, BALANCED, PERFORMANCE)

---

## 5. Core Terminology (Glossary)

### 5.1 Architecture Terms

| Term                       | Definition                                                                           |
| -------------------------- | ------------------------------------------------------------------------------------ |
| **Hexagonal Architecture** | Ports & Adapters pattern - Domain isolated from infrastructure                       |
| **Port**                   | Interface defining domain contract (e.g., UserRepository, SecurityPort)              |
| **Adapter**                | Infrastructure implementation of port (e.g., FirestoreUserRepository, GeminiAdapter) |
| **Domain Layer**           | Pure business logic with zero external dependencies (src/domain/)                    |
| **Application Layer**      | Orchestration services using ports (src/services/, src/handlers/)                    |
| **Infrastructure Layer**   | Concrete implementations of ports (src/adapters/, src/infrastructure/)               |

### 5.2 Domain Concepts

| Term                    | Definition                                                                            |
| ----------------------- | ------------------------------------------------------------------------------------- |
| **Fact**                | Atomic unit of knowledge (FactEntity) - biographical, event, principle, system, alert |
| **SCD Type 2**          | Slowly Changing Dimension - temporal model with valid_from/valid_to/is_current        |
| **Lineage ID**          | UUID linking all versions of the same fact (for temporal queries)                     |
| **Multi-Vector Search** | Parallel search across 3 embeddings (text, tags, metadata) with RRF ranking           |
| **Consolidation**       | Background process synthesizing conversation history into long-term facts             |
| **Hot Storage**         | Recent conversation messages (100-message sliding window in Firestore)                |
| **Cold Storage**        | Consolidated facts with vector embeddings (unlimited retention)                       |

### 5.3 Agent System

| Term                             | Definition                                                                      |
| -------------------------------- | ------------------------------------------------------------------------------- |
| **Agent**                        | Autonomous specialist with can_handle() + execute() interface (BaseAgent)       |
| **Agent Communication Protocol** | Standard message format (AgentMessage, AgentResponse, AgentIntent, AgentStatus) |
| **Agent Coordinator**            | Central routing hub - explicit routing, broadcast, parallel execution           |
| **Router Agent**                 | Classifier routing user queries to Quick, Smart, or specialist agents           |
| **Quick Agent**                  | Fast responses using ECO tier (Gemini Flash)                                    |
| **Smart Agent**                  | Deep reasoning with specialist delegation (PERFORMANCE tier, Claude Opus)       |
| **Memory Search Agent**          | Semantic search across user facts (multi-vector RRF ranking)                    |
| **Web Search Agent**             | Google Search API integration with result synthesis                             |
| **Consolidation Agent**          | Batch processor extracting facts from conversation history                      |
| **Circuit Breaker**              | Resilience pattern - open after 3 failures, auto-recover after 60s              |

### 5.4 Multi-Tenancy

| Term                 | Definition                                                                           |
| -------------------- | ------------------------------------------------------------------------------------ |
| **User**             | Individual identity (UserProfile) with OAuth external_user_id                        |
| **Account**          | Billing entity (BillingAccount) - can contain multiple users                         |
| **IAM Policy**       | Role mapping (user_id → role) - owner, member, viewer                                |
| **Account Defaults** | Shared config for 99% of users (UserBotConfig at account level)                      |
| **User Overrides**   | Individual customization (UserBotConfig at user level, merges with account defaults) |
| **Fact Visibility**  | ACCOUNT_SHARED (all members) or USER_PRIVATE (creator only)                          |
| **Whitelist**        | Email-based invite system (WhitelistEntry) - controls account creation               |
| **Invite Code**      | Time-limited registration token (InviteCode) - PERSONAL, FAMILY, ORGANIZATION        |

### 5.5 Platform Concepts

| Term                  | Definition                                                                    |
| --------------------- | ----------------------------------------------------------------------------- |
| **Response Channel**  | Platform-agnostic interface for sending messages (ResponseChannel protocol)   |
| **Message Context**   | Request metadata (user_id, session_id, thread_id, attachments)                |
| **Smart Response**    | Structured response with text + RichContent (tables, lists, code blocks)      |
| **Rich Content**      | Platform-specific formatting (Slack blocks, Telegram reply markup)            |
| **Platform Adapter**  | Webhook handler for Slack/Telegram (SlackHTTPAdapter, TelegramWebhookAdapter) |
| **Deduplication**     | Prevents double-processing of webhooks (update_id hash with 5-min TTL)        |
| **HMAC Verification** | Webhook signature validation (SHA256 HMAC with platform secret)               |

### 5.6 Prompt System

| Term                   | Definition                                                                      |
| ---------------------- | ------------------------------------------------------------------------------- |
| **Token**              | Immutable, validated prompt fragment (Token domain entity)                      |
| **Blueprint**          | Template with placeholders ({{TOKENIZED}}, [[RUNTIME]])                         |
| **Profile Slot**       | Token assignment at SYSTEM/AGENT/ACCOUNT/USER level                             |
| **4-Level Resolution** | Merge priority: USER > ACCOUNT > AGENT > SYSTEM (higher level overrides)        |
| **Section Type**       | TOKENIZED (slots), STATIC (embedded), RUNTIME (validated injection)             |
| **Trust Zone**         | TRUSTED (tokens), SEMI_TRUSTED (RAG), UNTRUSTED (user input + model output)     |
| **Security Port**      | Validation interface with Regex, LLM, External API adapters                     |
| **Assembly Service**   | Orchestrator resolving profiles → tokens → final prompt (PromptAssemblyService) |

### 5.7 Infrastructure

| Term                     | Definition                                                                           |
| ------------------------ | ------------------------------------------------------------------------------------ |
| **Firestore**            | NoSQL database (Google Cloud) - document collections with vector indexes             |
| **Named Database**       | Multi-region Firestore (database_id parameter) - used for US production migration    |
| **Semantic Collections** | ADR-006 naming (domain_facts, domain_sessions vs legacy dev_facts_v2)                |
| **Vector Index**         | Firestore KNN vector index with flat configuration (768 dimensions, cosine distance) |
| **Cloud Run**            | Serverless container platform - auto-scaling HTTP/async workers                      |
| **Cloud Tasks**          | Managed job queue - background consolidation processing                              |
| **Cloud Build**          | CI/CD pipeline - automated builds on git push                                        |
| **Secret Manager**       | Encrypted credential storage (SLACK_BOT_TOKEN, GEMINI_API_KEY, etc.)                 |

---

## 6. Scope and Boundaries

### 6.1 In Scope

✅ **Messaging Platforms:**

- Slack (Events API, Socket Mode, Slack App manifest)
- Telegram (Bot API, Webhook)

✅ **Core Features:**

- Conversational AI with persistent memory
- Multi-vector semantic search
- Background fact consolidation
- Multi-agent coordination (router, quick, smart, specialists)
- OAuth authentication and User Cabinet

✅ **Multi-Tenancy:**

- Account-based billing and quota management
- IAM role-based access control
- Shared knowledge base with privacy controls

✅ **Prompt System:**

- Token-based prompt assembly (v3)
- 4-level configuration resolution
- Security validation (5-layer defense)

### 6.2 Out of Scope (Future Roadmap)

❌ **Platforms:**

- Discord, WhatsApp, Microsoft Teams (planned Phase 3+)
- Web chat widget (planned Phase 4)
- Mobile apps (planned Phase 5)

❌ **Advanced Features:**

- Voice/audio processing (speech-to-text)
- Image generation (DALL-E, Stable Diffusion)
- Agentic workflows (tool chaining, function calling)
- Collaborative editing (real-time co-authoring)

❌ **Enterprise:**

- On-premise deployment (self-hosted)
- LDAP/Active Directory integration
- SAML/SSO (Okta, Auth0)
- Custom LLM fine-tuning

---

## 7. Success Metrics

### 7.2 System Performance

- **P95 Response Time:** < 10s (smart agent), < 2s (quick agent)
- **Uptime:** 99.5% (Cloud Run SLA)
- **Cache Hit Rate:** 70-80% (prompt assembly)
- **Vector Search Recall:** 90% @ k=10 (multi-vector RRF)

### 7.3 Cost Efficiency

- **Token Utilization:** 80% of quota consumed (waste reduction)

### 7.4 Code Quality

- **Test Coverage:** 70%+ (unit + integration)
- **Documentation Freshness:** < 3 months since last update
- **Deployment Frequency:** Daily (automated CI/CD)
- **Mean Time to Recovery (MTTR):** < 1 hour

---

## 8. Constraints

See [02_constraints/README.md](../02_constraints/README.md) for detailed technical and organizational constraints.

---

**Last Updated:** 2026-02-10  
**Status:** ✅ Complete  
**Phase:** Documentation Audit Phase 1.1
