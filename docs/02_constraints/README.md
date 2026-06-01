# 02 Constraints

## 1. Technical Constraints

### 1.1 Programming Language & Runtime

| Constraint        | Value             | Rationale                                        |
| ----------------- | ----------------- | ------------------------------------------------ |
| **Language**      | Python 3.11       | Async/await maturity, type hints, modern tooling |
| **Docker Base**   | python:3.11-slim  | Minimal attack surface, optimized for Cloud Run  |
| **Async Runtime** | asyncio           | Native async I/O for high concurrency            |
| **Type System**   | Type hints + mypy | Static analysis, IDE support, runtime safety     |

**Implications:**

- No backward compatibility with Python 3.9 or earlier
- All I/O operations must be async-first (asyncio, aiohttp, async Firestore SDK)
- Third-party libraries must support asyncio or provide async wrappers

### 1.2 Core Dependencies

**Framework Stack:**

| Dependency              | Version  | Purpose                  | Lock-in Risk |
| ----------------------- | -------- | ------------------------ | ------------ |
| **slack_bolt**          | Latest   | Slack integration        | Medium       |
| **python-telegram-bot** | 21.0     | Telegram integration     | Medium       |
| **quart**               | >=0.18.0 | Async web framework      | Low          |
| **hypercorn**           | >=0.14.0 | ASGI server              | Low          |
| **PyJWT**               | >=2.8.0  | OAuth session management | Low          |
| **google-genai**        | Latest   | Gemini LLM API           | High         |
| **anthropic**           | Latest   | Claude LLM API           | High         |

**Google Cloud SDK:**

| Dependency                      | Purpose              | Lock-in Risk |
| ------------------------------- | -------------------- | ------------ |
| **google-cloud-firestore**      | NoSQL database       | High         |
| **google-cloud-storage**        | Object storage       | High         |
| **google-cloud-logging**        | Centralized logging  | High         |
| **google-cloud-secret-manager** | Credential storage   | High         |
| **google-cloud-tasks**          | Background job queue | High         |
| **firebase-admin**              | OAuth authentication | High         |

**Observability:**

| Dependency                           | Purpose                 | Lock-in Risk |
| ------------------------------------ | ----------------------- | ------------ |
| **opentelemetry-api**                | Distributed tracing API | Low          |
| **opentelemetry-sdk**                | Tracing SDK             | Low          |
| **opentelemetry-exporter-gcp-trace** | GCP trace exporter      | Medium       |
| **structlog**                        | Structured logging      | Low          |

**Data Science:**

| Dependency | Purpose            | Lock-in Risk |
| ---------- | ------------------ | ------------ |
| **numpy**  | Vector operations  | Low          |
| **lark**   | Groovy DSL parsing | Low          |

**Testing:**

| Dependency         | Purpose            |
| ------------------ | ------------------ |
| **pytest**         | Test framework     |
| **pytest-asyncio** | Async test support |
| **pytest-mock**    | Mocking utilities  |

**Lock-in Risk Assessment:**

- **High (70% of dependencies):** Google Cloud platform deeply embedded
- **Medium (15%):** Platform-specific SDKs (Slack, Telegram) with adapter layer
- **Low (15%):** Standard libraries easily replaceable

**Migration Strategy:**

- Hexagonal Architecture isolates infrastructure dependencies
- Ports (interfaces) in `src/ports/` define contracts
- Adapters (implementations) in `src/adapters/` can be swapped
- Example: `LLMPort` → `GeminiAdapter` or `ClaudeAdapter` or `OpenAIAdapter`

### 1.3 Platform Constraints

#### Cloud Run (Serverless Container Platform)

| Constraint          | Limit                 | Impact                                            |
| ------------------- | --------------------- | ------------------------------------------------- |
| **Memory**          | 1024Mi (1GB)          | Per-user agent cache limited, no heavy ML models  |
| **CPU**             | 1 vCPU                | Single-threaded bottleneck, async I/O compensates |
| **Request Timeout** | 60 minutes (max)      | Long-running tasks must use Cloud Tasks           |
| **Cold Start**      | 3-5 seconds           | Min instances = 1 for prod to avoid cold starts   |
| **Port**            | 8080 (fixed)          | All services (Slack, Telegram, OAuth) on one port |
| **Concurrency**     | 80 requests (default) | Shared among Slack, Telegram, OAuth endpoints     |
| **Stateless**       | No persistent disk    | All state in Firestore, no local file caching     |
| **Max Instances**   | 1 (current budget)    | Horizontal scaling blocked by cost                |

**Workarounds:**

- Per-user agent cache (1h TTL) reduces memory footprint
- Background consolidation via Cloud Tasks (async queue)
- Firestore for all persistent state
- Min instances = 1 to avoid cold starts in production

#### Firestore (NoSQL Database)

| Constraint                  | Limit                  | Impact                                              |
| --------------------------- | ---------------------- | --------------------------------------------------- |
| **Document Size**           | 1MB                    | Facts, sessions split if exceeded                   |
| **Transaction Size**        | 500 documents          | Batch writes chunked to 500                         |
| **Index Size**              | 40KB per entry         | Vector embeddings (768 dims) = ~3KB, within limit   |
| **Vector Index Dimensions** | 768 (Gemini embedding) | Cannot use larger models (e.g., 1536-dim OpenAI)    |
| **Vector Index Type**       | KNN (flat)             | Firestore standard vector index, 90%+ recall @ k=10 |
| **Queries per Second**      | 10k reads, 1k writes   | Rate limiting per collection                        |
| **Multi-Region**            | us-production database | Primary region: us-central1 (Iowa)                  |

**Design Decisions:**

- SCD Type 2 (temporal model) to avoid updates → append-only facts
- Multi-vector search (3 parallel queries) within rate limits
- Vector index: cosine distance, 768 dimensions (Gemini standard)
- Named database migration: `(default)` → `us-production` (multi-region)

#### LLM Provider Limits

**Gemini API:**

| Constraint          | Limit                        | Impact                                          |
| ------------------- | ---------------------------- | ----------------------------------------------- |
| **Context Window**  | 1M tokens (Gemini 2.0 Flash) | Biographical context + conversation fits easily |
| **Output Tokens**   | 8192 tokens                  | Chunking required for long responses            |
| **Rate Limit**      | 2000 RPM (requests)          | Per-user throttling to stay within limits       |
| **Rate Limit**      | 4M TPM (tokens)              | Token counting required for quota enforcement   |
| **Embedding Model** | gemini-embedding-2           | 768 dimensions (Matryoshka truncation), cosine distance |
| **Embedding Batch** | N parallel single-content calls | v2 has no true batch — fan-out via asyncio.gather |

**Claude API (Anthropic):**

| Constraint         | Limit              | Impact                                       |
| ------------------ | ------------------ | -------------------------------------------- |
| **Context Window** | 200k tokens (Opus) | Sufficient for smart agent with full context |
| **Output Tokens**  | 4096 tokens        | Standard limit, chunking may be needed       |
| **Rate Limit**     | Varies by tier     | Tracked via account usage                    |

**Cost Optimization:**

- Performance tier system (ECO, BALANCED, PERFORMANCE)
- Router agent uses ECO tier (Gemini Flash)
- Smart agent uses PERFORMANCE tier (Claude Opus or Gemini Pro)
- Account-level quotas: 100k tokens/day (default FREE tier)

#### External APIs

**Slack API:**

| Constraint          | Limit      | Impact                                    |
| ------------------- | ---------- | ----------------------------------------- |
| **Message Rate**    | 1 msg/sec  | Status updates throttled to 5s intervals  |
| **Payload Size**    | 3000 chars | Message chunking for long responses       |
| **File Upload**     | 1GB        | Gemini upload service proxies large files |
| **Webhook Timeout** | 3 seconds  | Response channel async, immediate 200 OK  |

**Telegram API:**

| Constraint          | Limit            | Impact                                      |
| ------------------- | ---------------- | ------------------------------------------- |
| **Message Length**  | 4096 chars       | Truncation with 70% safety margin (2867)    |
| **Message Rate**    | 30 msgs/sec      | Burst allowed, no throttling needed         |
| **Webhook Timeout** | 60 seconds       | Same as Slack, immediate 200 OK             |
| **MarkdownV2**      | Complex escaping | Special chars escaped, +30% length overhead |

**Google OAuth:**

| Constraint         | Limit           | Impact                                   |
| ------------------ | --------------- | ---------------------------------------- |
| **Redirect URIs**  | Whitelist       | DEV + PROD domains registered separately |
| **Token Lifetime** | 1 hour          | Refresh token rotation every session     |
| **Scopes**         | `profile email` | Minimal scope for privacy                |

### 1.4 Development & Tooling

| Tool            | Version     | Purpose                      |
| --------------- | ----------- | ---------------------------- |
| **Docker**      | Latest      | Containerization             |
| **Cloud Build** | GCP service | CI/CD pipeline               |
| **gcloud CLI**  | Latest      | Deployment automation        |
| **pytest**      | Latest      | Testing framework            |
| **mypy**        | (optional)  | Static type checking         |
| **mkdocs**      | Latest      | Documentation site generator |

**CI/CD Pipeline:**

- Cloud Build triggers on git push (dev/prod branches)
- Automated: Docker build → Push to GCR → Deploy to Cloud Run
- Firestore indexes deployed before service (safe migrations)
- Secret Manager for credentials (no secrets in git)

---

## 2. Organizational Constraints

### 2.1 Team Structure

| Constraint          | Value               | Impact                                              |
| ------------------- | ------------------- | --------------------------------------------------- |
| **Team Size**       | 1 developer (solo)  | All roles: dev, ops, QA, product, architecture      |
| **Working Hours**   | Part-time           | Development velocity limited, focus on MVP features |
| **Decision Making** | Single owner        | Fast decisions, risk of blind spots                 |
| **Code Review**     | AI-assisted (Cline) | AI agent reviews, no human peer review              |

**Implications:**

- **Documentation IS Code:** Self-documenting architecture critical
- **Target Architecture First:** No time for temporary hacks
- **Hexagonal Architecture:** Future-proof for team growth
- **Test Coverage:** 70%+ target to compensate for lack of QA team

### 2.2 Budget Constraints

| Resource           | Monthly Budget | Current Spend | Headroom   |
| ------------------ | -------------- | ------------- | ---------- |
| **LLM API Costs**  | $50            | $30-40        | $10-20     |
| **Cloud Run**      | $20            | $5-10         | $10-15     |
| **Firestore**      | $20            | $5-15         | $5-15      |
| **Secret Manager** | $5             | $2            | $3         |
| **Cloud Storage**  | $5             | $1-2          | $3-4       |
| **Total**          | **$100**       | **$43-69**    | **$31-57** |

**Cost Optimization Strategies:**

- **LLM:** Performance tier system (ECO for router, PERFORMANCE for complex)
- **Cloud Run:** Max instances = 1 (no horizontal scaling)
- **Firestore:** Semantic collection naming → efficient queries
- **Storage:** Gemini file API → no permanent storage
- **Caching:** Prompt assembly cache (24h TTL) → 20x fewer LLM calls

**Scale Limits:**

- Max users: ~100 DAU before cost becomes unsustainable
- Current: ~10-20 DAU (MVP stage)

### 2.3 Development Process

| Constraint          | Value                   | Impact                                      |
| ------------------- | ----------------------- | ------------------------------------------- |
| **Version Control** | Git (GitHub)            | Private repo, no public contributions       |
| **Branching**       | Feature branches → main | Main = production, dev branches for testing |
| **Deployment**      | Automated CI/CD         | Cloud Build triggers on push                |
| **Testing**         | Manual + automated      | Pytest for unit/integration, manual for E2E |
| **Documentation**   | Arc42 + MkDocs          | Self-documenting, AI-readable               |

**Quality Gates:**

- Code pushed with documentation updates (same commit)
- Building block docs updated in same session as code
- `mkdocs build --strict` must pass before merge
- Session context logged in IMPLEMENTATION_ROADMAP.md

---

## 3. Architectural Constraints

### 3.1 Hexagonal Architecture (Mandatory)

**Constraint:** All infrastructure dependencies must be isolated behind ports.

**Rules:**

- ✅ Domain layer (`src/domain/`) has ZERO external imports
- ✅ Ports (`src/ports/`) define interfaces (abstract classes)
- ✅ Adapters (`src/adapters/`) implement ports
- ✅ Dependency direction: Always inward (Adapters → Ports → Domain)
- ❌ Domain NEVER imports adapters or infrastructure

**Enforcement:**

- Code reviews check import statements
- Architectural tests (future): Validate dependency graph
- AI agent trained on Hexagonal Architecture principles

**Example:**

```
✅ CORRECT:
src/domain/user.py          # No imports from adapters/
src/ports/user_repository.py # Abstract class
src/adapters/firestore_user_repo.py # Implements UserRepository

❌ WRONG:
src/domain/user.py
from src.adapters.firestore_user_repo import FirestoreUserRepository  # VIOLATION
```

### 3.2 Async-First Design (Mandatory)

**Constraint:** All I/O operations must be async.

**Rules:**

- ✅ Use `async def` for all I/O methods
- ✅ Use `await` for all async calls
- ✅ Use `asyncio.gather()` for parallel operations
- ✅ Use `async with` for context managers (e.g., Firestore transactions)
- ❌ No synchronous blocking calls in hot path (requests)

**Allowed Exceptions:**

- Initialization code (startup, config loading)
- Background scripts (admin tools)
- Test setup/teardown

### 3.3 Multi-Tenancy (Mandatory)

**Constraint:** All data must be scoped to account_id for isolation.

**Rules:**

- ✅ Facts, sessions, users have `account_id` field
- ✅ Firestore queries filtered by `account_id`
- ✅ IAM policy enforced at repository layer
- ✅ RequestContext sets `account_id` implicitly
- ❌ No global queries without account_id filter

**Example:**

```python
# ✅ CORRECT:
facts = await repo.search_facts(
    account_id=context.account_id,
    query="birthday"
)

# ❌ WRONG:
facts = await repo.search_facts(query="birthday")  # Missing account_id
```

### 3.4 Platform-Agnostic Core (Mandatory)

**Constraint:** ConversationHandler must not know about Slack/Telegram.

**Rules:**

- ✅ Use `MessageContext` (platform-agnostic DTO)
- ✅ Use `ResponseChannel` protocol (interface)
- ✅ Platform adapters implement ResponseChannel
- ❌ No Slack/Telegram imports in handlers/services/domain

**Benefit:** Easy to add Discord, WhatsApp, SMS in future.

---

## 4. Conventions & Standards

### 4.1 Code Style

| Convention      | Rule                       | Tool          |
| --------------- | -------------------------- | ------------- |
| **Naming**      | snake_case (Python PEP 8)  | Manual        |
| **Imports**     | Absolute imports only      | Manual        |
| **Type Hints**  | All public methods         | mypy (future) |
| **Docstrings**  | Google style (public APIs) | Manual        |
| **Line Length** | 100 chars (flexible)       | Manual        |

### 4.2 Collection Naming (ADR-006)

**Constraint:** Semantic collection names (not technical prefixes).

**Rules:**

- ✅ `domain_facts_v2` (semantic + version)
- ✅ `domain_users_v2` (semantic + version)
- ✅ `sessions` (infrastructure, no version)
- ❌ `dev_facts_v2` (technical prefix, deprecated)

**Environment Prefixes:**

- Development: `development_domain_facts_v2`
- Production: `domain_facts_v2`
- Test: `test_domain_facts_v2`

See [ADR-006](../09_decisions/adr-006-semantic-collection-naming/README.md) for details.

### 4.3 Documentation Standards

**Constraint:** Documentation IS Code (updated in same commit).

**Rules:**

- ✅ Building block docs updated with code changes
- ✅ Session context logged in IMPLEMENTATION_ROADMAP.md
- ✅ `mkdocs build --strict` passes before merge
- ✅ Cross-references between docs maintained
- ❌ No "TODO" docs (create skeleton with dates)

See [CLAUDE.md](../../CLAUDE.md) for the working contract and conventions.

---

## 5. Security & Compliance

### 5.1 Authentication & Authorization

| Constraint             | Requirement                        | Implementation            |
| ---------------------- | ---------------------------------- | ------------------------- |
| **OAuth Provider**     | Google OAuth 2.0 / OIDC            | Firebase Auth             |
| **Session Management** | JWT tokens (access + refresh)      | PyJWT library             |
| **IAM Model**          | Role-based (owner, member, viewer) | Firestore IAM policy      |
| **Webhook Security**   | HMAC-SHA256 signature              | Slack + Telegram verified |
| **Secrets**            | No secrets in code/git             | Google Secret Manager     |

### 5.2 Data Privacy

| Constraint          | Requirement                    | Implementation                  |
| ------------------- | ------------------------------ | ------------------------------- |
| **Data Residency**  | US-only (us-central1)          | Firestore multi-region DB       |
| **Fact Visibility** | ACCOUNT_SHARED or USER_PRIVATE | Enum field, repository filter   |
| **PII Handling**    | No logging of message content  | Structured logs (metadata only) |
| **Encryption**      | In-transit (TLS) + at-rest     | Firestore default encryption    |

**GDPR/Privacy:**

- No GDPR compliance required (US-only, MVP)
- Future: Data export API (Phase 3+)
- Future: Account deletion (Phase 3+)

### 5.3 Prompt Injection Defense

**Constraint:** 5-layer defense against prompt injection.

**Layers:**

1. **Token Creation:** SecurityPort validates tokens at upload
2. **Assignment Validation:** Blueprint enforces permissions
3. **Runtime Injection:** Biographical + conversation validated
4. **Output Validation:** Model responses validated before storage
5. **RAG Validation:** Search results validated before injection

**Trust Zones:**

- TRUSTED: System tokens (admin-created)
- SEMI_TRUSTED: RAG facts (user-stored, validated)
- UNTRUSTED: User input, model output

See [Security Validation Building Block](../05_building_blocks/security_validation/README.md) for details.

---

## 6. External Dependencies (Lock-in Risk)

### 6.1 Google Cloud Platform (HIGH Risk)

**Services Used:**

- Firestore (NoSQL database)
- Cloud Run (serverless containers)
- Cloud Tasks (job queue)
- Secret Manager (credentials)
- Cloud Logging (observability)
- Cloud Storage (file upload proxy)

**Migration Difficulty:**

- **Very Hard:** Firestore (NoSQL structure, vector indexes)
- **Hard:** Cloud Run (Kubernetes alternative exists)
- **Medium:** Cloud Tasks (Redis queue alternative)
- **Easy:** Secret Manager, Logging (standard APIs)

**Mitigation:**

- Hexagonal Architecture isolates GCP dependencies
- All GCP services behind ports (interfaces)
- Example: `FactRepository` → `FirestoreFactRepository` (adapter)
- Alternative: `FactRepository` → `MongoDBFactRepository` (future)

### 6.2 LLM Providers (MEDIUM-HIGH Risk)

**Primary:** Gemini (Google)
**Secondary:** Claude (Anthropic)

**Migration Difficulty:**

- **Medium:** Prompt engineering differs per model
- **Medium:** Context window differences (1M vs 200k tokens)
- **Easy:** Provider abstraction via `LLMPort`

**Mitigation:**

- Provider preference system (Gemini default, user can switch to Claude)
- Model overrides per agent (power users)
- Prompt templates stored in Firestore (not hardcoded)

### 6.3 Platform APIs (MEDIUM Risk)

**Slack API:**

- Official SDK: `slack_bolt`
- Webhook-based (HTTP mode) or Socket Mode
- Adapter: `SlackHTTPAdapter`, `SlackSocketAdapter`

**Telegram API:**

- Official SDK: `python-telegram-bot==21.0`
- Webhook-based only
- Adapter: `TelegramWebhookAdapter`

**Migration Difficulty:**

- **Easy:** Adapters isolated, platform-agnostic `ResponseChannel` protocol
- **Easy:** Add Discord/WhatsApp without changing core logic

---

## 7. Versioning & Deprecation

### 7.1 API Versioning

**Current:** No public API (internal services only)

**Future (Phase 4+):**

- REST API for web client
- Versioning scheme: `/api/v1/facts`
- Backward compatibility: 1 major version overlap

### 7.2 Schema Versioning

**Collections:**

- Versioned: `domain_facts_v2`, `domain_users_v2` (breaking changes)
- Stable: `sessions`, `consolidation_queue` (no version, stable schema)

**Migration Strategy:**

- New collection: `domain_facts_v3` (parallel to v2)
- Migration script: Copy data from v2 → v3
- Cutover: Switch repository to v3 collection
- Cleanup: Archive v2 collection after 30 days

### 7.3 Deprecation Policy

**Code:**

- Mark `@deprecated` in docstring
- Log warning on use
- Remove after 2 releases (no semantic versioning yet)

**Collections:**

- Suffix `_deprecated_YYYYMMDD`
- Archive to Cloud Storage after 90 days
- Delete after 1 year (compliance permitting)

---

## 8. Known Limitations

### 8.1 Current Blockers

| Limitation              | Impact                   | Workaround/Plan                  |
| ----------------------- | ------------------------ | -------------------------------- |
| **Max Instances = 1**   | No horizontal scaling    | Vertical scaling (2GB memory)    |
| **No Redis Cache**      | All caching in Firestore | In-memory cache (1h TTL)         |
| **No Load Balancer**    | Single Cloud Run service | Domain Mapping (no LB cost)      |
| **Budget = $100/month** | ~100 concurrent users    | Vertical scaling or increase budget  |

### 8.2 Future Constraints (Anticipated)

| Risk                      | Threshold       | Mitigation Strategy                 |
| ------------------------- | --------------- | ----------------------------------- |
| **Cost Scaling**          | > $200/month    | Usage-based quotas + billing alerts |
| **Firestore Rate Limits** | > 10k reads/sec | Redis cache layer (Phase 4+)        |
| **Cold Start Latency**    | > 10s           | Keep-alive pings every 5 min        |
| **LLM Context Overflow**  | > 1M tokens     | Conversation summarization          |

---

## 9. Change Management

### 9.1 Adding New Dependency

**Process:**

1. Evaluate lock-in risk (Low/Medium/High)
2. Check license compatibility (MIT/Apache preferred)
3. Add to `requirements.txt` with version pin
4. Update this Constraints document (Section 1.2)
5. Document in Building Block (if architectural)

**Approval:** Solo dev (self-approval), but document rationale.

### 9.2 Changing Infrastructure

**Examples:** Firestore → MongoDB, Cloud Run → Kubernetes

**Process:**

1. Create RFC in `docs/10_rfcs/`
2. Evaluate migration cost (time + money)
3. Create adapter implementation (parallel to existing)
4. Test with feature flag
5. Gradual cutover (5% → 25% → 50% → 100%)
6. Archive old adapter after 30 days

**Critical:** Hexagonal Architecture makes this feasible.

---

**Last Updated:** 2026-02-10  
**Status:** ✅ Complete  
**Phase:** Documentation Audit Phase 1.2
