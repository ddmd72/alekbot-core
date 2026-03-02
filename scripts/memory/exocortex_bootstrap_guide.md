# Exocortex Bootstrap Guide — LLM Instruction Document

> **Purpose**: This document is a complete specification for an LLM (Claude, GPT-4, etc.)
> to build a knowledge management system ("exocortex") from scratch.
> No code is copied — only architectural patterns and hard-won design lessons.
>
> **Target stack**: AWS, OpenAI, PostgreSQL, Python.
> **Interface**: REST API + Web UI (no messenger integration).

---

## 1. WHAT ARE WE BUILDING

A personal/team knowledge management system that:
- Accepts user input via REST API or web chat interface
- Responds using accumulated knowledge (facts about the user/team/domain)
- Automatically extracts and stores new facts from conversations (background process)
- Gets smarter over time without manual curation

**It is NOT a chatbot.** It is a system with memory that thinks in the background.

### Core Loop

```
User sends message
  → Router classifies complexity (1-5 = Quick, 6-10 = Smart)
  → Router performs memory search, attaches relevant facts to context
  → Selected agent responds using enriched context
  → Response sent to user
  → [Background] Consolidation extracts new facts from conversation
  → Next conversation already knows new information
```

---

## 2. ARCHITECTURE

### 2.1 Hexagonal Architecture (Ports & Adapters)

This is non-negotiable. It enables swapping any infrastructure component without touching business logic.

```
src/
  domain/         — Models, enums, value objects. ZERO external dependencies.
                    Only stdlib + pydantic.
  ports/          — Abstract interfaces (ABC). Import only domain/ + stdlib.
  adapters/       — Implementations of ports (PostgreSQL, OpenAI, Web).
  services/       — Business logic. Receive ports via constructor DI.
  agents/         — Multi-agent system. Each agent is a specialist.
  handlers/       — Orchestrators (ConversationHandler, ConsolidationHandler).
  composition/    — ServiceContainer: wires ports to adapters. Single composition root.
  config/         — Environment config, settings.
  web/            — FastAPI/Quart app (REST API + UI).
main.py           — Bootstrap: creates ServiceContainer, starts web server.
```

### 2.2 Import Rules (Enforced)

```
domain/   → ONLY stdlib, pydantic. Never adapters/, services/, config/.
ports/    → domain/ + stdlib + ABC. Nothing else.
adapters/ → domain/, ports/, config/. Can import external libraries.
services/ → domain/, ports/. NEVER import concrete adapters.
agents/   → Receive dependencies via constructor. Never instantiate adapters directly.
```

### 2.3 Manual Dependency Injection

No DI frameworks. A single `ServiceContainer` class in `composition/` wires everything:

```python
class ServiceContainer:
    def __init__(self, config):
        # 1. Infrastructure adapters
        self.db = create_async_engine(config.DATABASE_URL)
        self.embedding_service = OpenAIEmbeddingAdapter(config.OPENAI_API_KEY)

        # 2. Repositories (implement port interfaces)
        self.fact_repository = PgVectorFactRepository(self.db, self.embedding_service)

        # 3. Application services (depend on ports, not adapters)
        self.search_service = SearchEnrichmentService(
            repository=self.fact_repository,
            embedding_service=self.embedding_service,
        )

        # 4. Agents (receive services via constructor)
        self.router_agent = RouterAgent(llm=self.quick_llm, search=self.search_service)
```

---

## 3. MULTI-AGENT SYSTEM

### 3.1 Agent Topology

Do NOT build one monolithic LLM call. Build specialists:

| Agent | Model | Purpose | Cost Tier |
|---|---|---|---|
| **Router** | gpt-4o-mini | Classify complexity (1-10), search memory, route | Cheap |
| **Quick** | gpt-4o-mini | Fast responses for simple questions (70% of traffic) | Cheap |
| **Smart** | gpt-4o | Complex reasoning, multi-step tasks (30% of traffic) | Expensive |
| **WebSearch** | gpt-4o-mini + API | Web search + synthesis (called by Smart only) | Cheap |
| **MemorySearch** | gpt-4o-mini | Formulates search keys from raw query | Cheap |
| **Consolidation** | gpt-4o | Background fact extraction from conversations | Expensive |

### 3.2 Router Logic

```
User message → Router agent:
  1. Classify complexity 1-10
  2. Extract search keys (keywords + 2 semantic phrases)
  3. Execute memory search
  4. Build enriched context (facts + conversation history)
  5. Route to Quick (complexity 1-5) or Smart (6-10)
```

### 3.3 Agent Base Class

Every agent inherits from BaseAgent with:
- Circuit breaker (prevent cascading failures)
- Retry logic with exponential backoff
- Structured output parsing (JSON) with retry on parse failure
- Token/cost tracking
- Consistent error handling

### 3.4 Performance Tiers

Abstract agents from concrete models:

```python
class PerformanceTier(Enum):
    ECO = "eco"             # gpt-4o-mini — cheap, fast
    BALANCED = "balanced"   # gpt-4o-mini — moderate tasks
    PERFORMANCE = "performance"  # gpt-4o — complex reasoning
```

Agents declare their tier. A ProviderRegistry maps tier → concrete model.
When OpenAI releases new models, change one mapping, not every agent.

---

## 4. MEMORY SYSTEM

This is the core differentiator. Without memory, it's just a ChatGPT wrapper.

### 4.1 Fact Entity (Domain Model)

```python
class FactEntity(BaseModel):
    id: str                          # UUID
    account_id: str                  # Multi-tenant: who owns this
    text: str                        # The actual fact content
    tags: List[str]                  # Domain keywords
    source: str                      # Where this fact came from

    # Four-Dimensional Taxonomy
    domain: FactDomain               # WHAT: biographical, health, work, network, etc.
    temporal_class: TemporalClass    # HOW LONG: permanent, stable, dynamic, ephemeral
    state: FactState                 # IS CURRENT: current, stale, archived, superseded
    context_priority: ContextPriority # IMPORTANCE: critical, high, medium, low

    # Three vectors for multi-dimensional search
    vector: Optional[List[float]]           # Semantic embedding of text
    tags_vector: Optional[List[float]]      # Embedding of domain keywords
    metadata_vector: Optional[List[float]]  # Embedding of structured metadata

    # SCD Type 2 versioning
    lineage_id: str                  # Links versions of the same fact
    version: int
    valid_from: datetime
    valid_to: Optional[datetime]
    is_current: bool

    created_at: datetime
```

### 4.2 Why Three Vectors (Not One)

Single-vector search misses facts that are semantically related but worded differently.

| Vector | Content | Catches queries like |
|---|---|---|
| `vector` | Semantic embedding of fact text | Direct meaning match |
| `tags_vector` | Embedding of domain keywords | "health facts", "work stuff" |
| `metadata_vector` | Embedding of structured data | Metadata-oriented queries |

Three vectors × two search phrases = 6 parallel searches, merged via RRF.

### 4.3 Multi-Vector Search with RRF (Reciprocal Rank Fusion)

This is the key algorithm. Do not skip it.

**Input** (from MemorySearchAgent via LLM):
```json
{
  "keywords": ["family", "mother"],
  "primary_query": "what does his mother do for work",
  "alternative_query": "mom's profession and occupation"
}
```

**Search execution** (6 parallel queries):
```
keywords     → tags_vector search    (limit=5)
keywords     → metadata_vector search (limit=5)
phrase_1     → vector search         (limit=10)
phrase_1     → tags_vector search    (limit=10)
phrase_2     → vector search         (limit=10)
phrase_2     → metadata_vector search (limit=10)
```

**RRF merge**:
```
For each fact appearing in any result list:
    RRF_score = Σ 1/(k + rank_in_list_i)    where k=60

Sort all facts by RRF_score descending.
Return top N.
```

Why RRF works: a fact ranked #2 in three different searches beats a fact ranked #1 in only one search. This dramatically improves recall for ambiguous queries.

### 4.4 PostgreSQL + pgvector Schema

```sql
CREATE EXTENSION vector;

CREATE TABLE facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id TEXT NOT NULL,
    text TEXT NOT NULL,
    tags TEXT[] DEFAULT '{}',
    source TEXT,

    -- Taxonomy
    domain TEXT NOT NULL DEFAULT 'general',
    temporal_class TEXT NOT NULL DEFAULT 'stable',
    state TEXT NOT NULL DEFAULT 'current',
    context_priority TEXT NOT NULL DEFAULT 'medium',
    context_priority_rank INT NOT NULL DEFAULT 3,  -- for ORDER BY

    -- Three vectors (OpenAI text-embedding-3-small = 1536 dims)
    vector vector(1536),
    tags_vector vector(1536),
    metadata_vector vector(1536),

    -- SCD2 versioning
    lineage_id UUID NOT NULL,
    version INT NOT NULL DEFAULT 1,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to TIMESTAMPTZ,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_facts_account_state ON facts (account_id, state);
CREATE INDEX idx_facts_account_domain ON facts (account_id, domain);
CREATE INDEX idx_facts_lineage ON facts (lineage_id, version);

-- HNSW vector indexes (build AFTER initial data load for speed)
CREATE INDEX idx_facts_vector ON facts
    USING hnsw (vector vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_facts_tags_vector ON facts
    USING hnsw (tags_vector vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_facts_metadata_vector ON facts
    USING hnsw (metadata_vector vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

### 4.5 Vector Search Query Pattern

```sql
-- Single vector search with tenant isolation + state filter
SELECT id, text, tags, domain, state, context_priority,
       vector <=> $1::vector AS distance
FROM facts
WHERE account_id = $2
  AND state = 'current'
ORDER BY vector <=> $1::vector
LIMIT $3;
```

Run 6 such queries in parallel (asyncio.gather), then merge via RRF in Python.

### 4.6 Semantic Deduplication

When adding new facts, check for duplicates:

```
1. Vector search for similar facts (cosine similarity)
2. If similarity < 0.96 → NOT duplicate (quick exit, save it)
3. If similarity >= 0.96:
   a. Extract numbers from both texts. If numbers differ → NOT duplicate
      ("Weight 83 kg" ≠ "Weight 84 kg")
   b. If similarity < 0.98 AND new fact is more detailed → NOT duplicate
      (new fact adds information)
   c. Otherwise → DUPLICATE (skip)
```

**Bias toward inclusion**: better to have a duplicate than lose information.

---

## 5. CONSOLIDATION (Background Memory Formation)

This is what makes the system get smarter over time.

### 5.1 Pipeline

```
Conversation messages accumulate (sliding window: 100-200 messages)
  → Window fills up
  → Batch sent to SQS queue
  → Lambda/ECS worker picks up the job
  → ConsolidationAgent (gpt-4o) processes the batch:
      1. Read raw messages
      2. Search existing facts (to avoid duplicates)
      3. Extract new facts with full taxonomy
      4. For each new fact:
         - If truly new → CREATE
         - If updates existing → MERGE (supersede old, create new version)
         - If duplicate → DISCARD
      5. Refresh biographical context cache
  → Next conversation already uses new facts
```

### 5.2 ConsolidationAgent Output Format

The LLM must output structured JSON:

```json
{
  "candidates": [
    {
      "action": "CREATE",
      "text": "User started a new job at Acme Corp in January 2026",
      "tags": ["work", "employment", "acme"],
      "domain": "work",
      "temporal_class": "stable",
      "context_priority": "high"
    },
    {
      "action": "MERGE",
      "merge_ids": ["fact-uuid-1", "fact-uuid-2"],
      "text": "User weighs 84 kg (updated from 83 kg)",
      "tags": ["health", "weight"],
      "domain": "health",
      "temporal_class": "dynamic",
      "context_priority": "medium"
    },
    {
      "action": "DISCARD",
      "reason": "Already captured in fact-uuid-3"
    }
  ]
}
```

### 5.3 SCD Type 2 Versioning

When a fact is updated (MERGE):
1. Old fact: set `is_current=False`, `valid_to=now()`, `state=superseded`
2. New fact: `is_current=True`, `valid_from=now()`, same `lineage_id`, `version=old+1`

This preserves full history. You can always see what the system knew and when.

---

## 6. PROMPT ENGINEERING

### 6.1 Token System (Composable Prompts)

Do NOT hardcode prompts. Build them from reusable fragments:

```
Token — a verified prompt fragment with a purpose:
  - VOICE_STYLE: How the bot talks
  - COGNITIVE_PROCESS: How it thinks step-by-step
  - OUTPUT_FORMAT_X: Structured output spec for agent X
  - HUMOR: Personality traits
  - KNOWLEDGE_DOMAINS: What domains it knows about

Blueprint — a static template that assembles tokens:
  "You are {{AGENT_NAME}}. {{VOICE_STYLE}} {{COGNITIVE_PROCESS}}
   {{OUTPUT_FORMAT_ROUTER}}"
```

Benefits:
- Change personality in one place, all agents update
- A/B test prompt fragments independently
- Version control prompts like code

### 6.2 Context Assembly

Final prompt = static template + dynamic context:

```
[Static: blueprint with tokens — cacheable]
---
[Dynamic: biographical facts from memory]
[Dynamic: conversation history]
[Dynamic: current datetime, user info]
[Dynamic: user's message]
```

If using OpenAI, the static prefix can leverage prompt caching (automatic for long prompts).

---

## 7. AWS INFRASTRUCTURE

### 7.1 Minimal Viable Stack

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│  API Gateway │────▶│  ECS Fargate │────▶│  RDS PostgreSQL │
│  (REST API)  │     │  (Python)    │     │  + pgvector     │
└─────────────┘     └──────┬───────┘     └─────────────────┘
                           │
                    ┌──────▼───────┐
                    │     SQS      │
                    │  (async jobs)│
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   Lambda     │
                    │ (consolidation worker) │
                    └──────────────┘
```

| Component | AWS Service | Est. Cost |
|---|---|---|
| Compute (API) | ECS Fargate (0.25 vCPU, 0.5GB) | ~$10/mo |
| Database | RDS PostgreSQL db.t4g.micro | ~$13/mo |
| Queue | SQS | ~$0 (free tier) |
| Worker | Lambda (consolidation) | ~$1/mo |
| API | API Gateway | ~$3/mo |
| LLM | OpenAI API | ~$50-100/mo |
| **Total** | | **~$80-130/mo** |

### 7.2 Alternative: Fully Serverless

```
API Gateway → Lambda (API handler) → RDS Proxy → RDS PostgreSQL
                                   → SQS → Lambda (consolidation)
```

Cheaper at low traffic, but cold starts add 1-3s latency.

---

## 8. OPENAI-SPECIFIC NOTES

### 8.1 Model Mapping

| Role | Model | Notes |
|---|---|---|
| Router | gpt-4o-mini | Fast, cheap, good at classification |
| Quick | gpt-4o-mini | 70% of requests |
| Smart | gpt-4o | Complex reasoning |
| Consolidation | gpt-4o | Needs precision for fact extraction |
| Embeddings | text-embedding-3-small | 1536 dims, $0.02/1M tokens |

### 8.2 Structured Output

OpenAI supports `response_format: { type: "json_schema", json_schema: {...} }`.
Use it for all agents that need structured output (Router, Consolidation).
This eliminates the need for retry-on-parse-failure for most cases.

### 8.3 Embedding Batching

OpenAI embedding API accepts multiple texts in one call:

```python
response = client.embeddings.create(
    model="text-embedding-3-small",
    input=["text 1", "text 2", "text 3"]  # batch
)
```

Always batch the 3 vectors (text, tags, metadata) into one API call.

---

## 9. IMPLEMENTATION ORDER

Build in this exact sequence. Each phase is independently deployable and testable.

### Phase 1: Foundation (Week 1)
- [ ] Project structure (hexagonal layout)
- [ ] Domain models (FactEntity, enums, value objects)
- [ ] Port interfaces (FactRepository, EmbeddingService, LLMService)
- [ ] PostgreSQL adapter (PgVectorFactRepository) with basic CRUD
- [ ] OpenAI embedding adapter
- [ ] Unit tests for domain logic

### Phase 2: Single-Agent Chat (Week 2)
- [ ] OpenAI LLM adapter (chat completions)
- [ ] Single "Smart" agent (no routing yet — everything goes to gpt-4o)
- [ ] REST API endpoint: POST /api/chat
- [ ] Conversation history (in-memory or PostgreSQL)
- [ ] ServiceContainer wiring
- [ ] Integration test: send message → get response

### Phase 3: Memory Search (Week 3)
- [ ] Vector search in PgVectorFactRepository
- [ ] MemorySearchAgent (LLM formulates 3-key search)
- [ ] SearchEnrichmentService (6 parallel queries + RRF)
- [ ] Semantic deduplication (SmartDeduplicationService)
- [ ] Manually insert test facts → verify search quality
- [ ] Wire memory into agent context

### Phase 4: Multi-Agent Routing (Week 4)
- [ ] RouterAgent (complexity classification)
- [ ] QuickResponseAgent (gpt-4o-mini)
- [ ] PerformanceTier + ProviderRegistry
- [ ] Agent base class (circuit breaker, retry, cost tracking)
- [ ] End-to-end test: message → route → response with memory

### Phase 5: Consolidation (Week 5)
- [ ] ConsolidationAgent (fact extraction from conversations)
- [ ] SQS integration (queue conversation batches)
- [ ] Lambda worker (processes consolidation jobs)
- [ ] SCD2 versioning (fact updates create new versions)
- [ ] Biographical context cache (materialized or cached query)
- [ ] Integration test: conversation → consolidation → facts appear in search

### Phase 6: Web UI + Polish (Week 6)
- [ ] Simple chat UI (React/Next.js or plain HTML + HTMX)
- [ ] Authentication (corporate SSO / API keys)
- [ ] Fact browser (view/edit/invalidate facts)
- [ ] Monitoring (CloudWatch metrics for agent costs, latencies)
- [ ] Rate limiting

---

## 10. KEY DESIGN LESSONS (HARD-WON)

These are mistakes you will avoid because someone already made them:

1. **Do NOT use a single vector per fact.** Single-vector search has terrible recall for
   ambiguous queries. Three vectors + RRF is dramatically better.

2. **Do NOT skip deduplication.** Without it, consolidation creates hundreds of near-duplicates.
   But make dedup number-aware: "weight 83 kg" ≠ "weight 84 kg".

3. **Bias toward saving, not discarding.** A duplicate is annoying but recoverable.
   A lost fact is gone forever. Set thresholds conservatively (0.96+).

4. **Do NOT consolidate synchronously.** Consolidation is slow (10-30s for a batch).
   Always async via queue. User should never wait for it.

5. **Do NOT let agents import concrete adapters.** The moment an agent imports
   `PostgresRepository` directly, you've lost the ability to test without a database.

6. **Router search MUST happen before routing.** The router needs memory context to
   correctly classify complexity. A question about "my weight last month" is simple
   if the fact exists, complex if it doesn't.

7. **Track LLM costs per agent.** You will be surprised which agent costs the most.
   Without tracking, you cannot optimize.

8. **Prompt caching matters.** Static prompt prefix (tokens + blueprint) should be
   cacheable. Dynamic context (facts, history) appended at the end. This cuts
   input token costs by 50%+ for repeated conversations.

9. **Do NOT regex-parse LLM output.** If JSON parsing fails, retry with the error
   message appended. Never try to extract partial JSON with regex — it creates
   silent data corruption.

10. **SCD2 from day one.** Adding versioning later to an existing fact table is painful.
    Start with lineage_id + version + valid_from/valid_to from the first migration.

---

## 11. PORT INTERFACES (Minimal Set)

```python
# ports/repository.py
class FactRepository(ABC):
    async def add_fact(self, fact: FactEntity) -> str: ...
    async def get_fact_by_id(self, fact_id: str) -> Optional[FactEntity]: ...
    async def search_facts(self, query_vector: List[float], limit: int,
                           account_id: str) -> List[FactEntity]: ...
    async def add_fact_if_unique(self, fact: FactEntity,
                                  similarity_threshold: float) -> Tuple[bool, Optional[str]]: ...
    async def update_fact(self, fact: FactEntity) -> None: ...
    async def get_active_facts_ordered(self, account_id: str,
                                        domain: Optional[str], limit: Optional[int]) -> List[FactEntity]: ...

# ports/embedding_service.py
class EmbeddingService(ABC):
    async def get_embedding(self, text: str) -> List[float]: ...
    async def get_embeddings_batch(self, texts: List[str]) -> List[List[float]]: ...

# ports/llm_service.py
class LLMService(ABC):
    async def generate(self, messages: List[Dict], **kwargs) -> str: ...
    async def generate_structured(self, messages: List[Dict],
                                   response_schema: Dict) -> Dict: ...

# ports/search_enrichment_port.py
class SearchEnrichmentPort(ABC):
    async def enrich_context(self, keywords: List[str],
                              search_phrase_1: str, search_phrase_2: str,
                              account_id: str) -> EnrichedContext: ...

# ports/fact_write_port.py
class FactWritePort(ABC):
    async def add_facts_batch(self, account_id: str, user_id: str,
                               facts_data: List[Dict]) -> Tuple[int, int, List[str]]: ...
```

---

## 12. WHAT THIS DOCUMENT IS NOT

- This is NOT code to copy. It is architectural knowledge.
- This is NOT specific to any one codebase. It is a pattern language.
- Every code snippet is illustrative pseudocode, not production code.
- The LLM using this document should write all code from scratch,
  following the patterns described, adapted to the specific tech stack.

---

*Generated from architectural experience. No proprietary code included.*
